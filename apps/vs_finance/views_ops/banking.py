"""Bank accounts, statement import, reconciliation.
"""
from __future__ import annotations

from vs_finance.wording import counted

from vs_workflow.services.approval_filter import filter_by_approval_param, filter_by_stored_status
import hashlib

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from vs_rbac.scoping import (
    caller_reaches_whole_tenant,
    shared_write_refusal,
    transaction_branch_q,
)

from core.response import success_response

from ..approvals import with_approval_request
from ..audit import record
from ..constants import (
    BankLineStatus,
    BankSplitDifferenceTreatment,
    DocumentStatus,
    FinanceAuditAction,
)
from ..statement_imports import annotate_statement_rollback
from ..views import resolve_entity
from ..models import (
    BankAccount,
    BankStatement,
    BankStatementImportContext,
    BankStatementLine,
    BankTransaction,
    BankTransfer,
    JournalLine,
)
from ..serializers import (
    BankAccountSerializer,
    BankReconciliationSerializer,
    BankStatementDetailSerializer,
    BankStatementLineSerializer,
    BankStatementSerializer,
)

from .base import (
    _FinanceBase,
    _bank_account_in_reach,
    _bool,
    _date,
    _inherited_branch_id,
    _int,
    _transaction_branch,
    _require_lines,
    _resolve_account,
    _resolve_currency,
    _signed_money,
)


# --------------------------------------------------------------------------- #
# Resolving one account, statement or line for the caller in front of you     #
# --------------------------------------------------------------------------- #
#
# These take the ``request`` rather than the entity alone, and that is the whole
# point of them. Entity scoping answers "whose books?" and never "whose site?",
# so an account reached by id is not narrowed by the fact that the list withheld
# it: a bursar covering Ikeja could read Lekki's account number and balance,
# rename it, import statements onto it and reconcile them, by naming its id.
#
# Doing the check per endpoint is what lets it drift. There are eleven ways into
# a bank account in this file and one of them is the list, so the resolver is
# shared and the other ten do not each get a chance to forget it.
#
# A bank account holds one branch's money, so it is read as a transaction
# (:func:`vs_rbac.scoping.transaction_branch_q`): a branch-bound caller reaches
# only their own branches' accounts, and an account not yet given a branch only
# by a whole-school caller. Statements and lines carry no branch of their own and
# take the account's, through the ``bank_account__`` prefix, so an account a
# caller may open and the rows hanging off it cannot give different answers.
#
# A row belonging to another site answers with the same message as one that does
# not exist, so an id cannot be used to find out that another site holds it.
# That is why these raise NotFound rather than PermissionDenied, and it matches
# the AR resolvers and procurement's ``_document_or_404``.


def _bank_or_404(request, pk, *, entity=None, active_only=False):
    """The bank account behind *pk* that this caller may work in.

    ``active_only`` narrows to a live account and says so in the refusal, which
    is what the import surfaces need: a closed account is not a place to file a
    new statement, and the reason a caller sees should name which of the two
    stopped them.
    """
    filters = {
        "entity": entity if entity is not None else resolve_entity(request),
        "pk": pk,
    }
    if active_only:
        filters["is_active"] = True
    bank = (
        BankAccount.objects
        .filter(transaction_branch_q(request), **filters)
        .select_related("gl_account", "branch")
        .first()
    )
    if bank is None:
        raise NotFound(
            "Active bank account not found for this entity." if active_only
            else "Bank account not found for this entity."
        )
    return bank


# --------------------------------------------------------------------------- #
# Banking + reconciliation                                                    #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Bank Account List Create View.
class BankAccountListCreateView(_FinanceBase):
    """GET (list) / POST (create) bank accounts for an entity.

    docstring-name: Bank accounts
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.bankaccount.create" if self.request.method == "POST" \
            else "finance.bankaccount.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = BankAccount.objects.filter(
            transaction_branch_q(request), entity=entity,
        ).select_related("gl_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Bank accounts retrieved.", data=BankAccountSerializer(
                qs, many=True, context={"request": request},
            ).data,
        )

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A bank account name is required."})
        if BankAccount.objects.filter(entity=entity, name=name).exists():
            raise ValidationError({"name": f"A bank account named '{name}' already exists."})
        gl_account = _resolve_account(request, entity, body.get("gl_account"), "gl_account", required=True)
        is_primary = _bool(body.get("is_primary", False), default=False)
        is_primary_collection = _bool(body.get("is_primary_collection", False), default=False)
        currency = _resolve_currency(body.get("currency"))
        branch = _transaction_branch(request, entity, body)
        with transaction.atomic():
            if is_primary_collection:  # At most one pay-to account per branch.
                BankAccount.objects.filter(
                    entity=entity, branch=branch, is_primary_collection=True,
                ).update(is_primary_collection=False)
            bank = BankAccount.objects.create(
                entity=entity, name=name,
                branch=branch,
                bank_name=body.get("bank_name", ""),
                account_number=body.get("account_number", ""),
                gl_account=gl_account,
                currency=currency,
                is_active=_bool(body.get("is_active", True), default=True),
                is_primary=is_primary,
                is_primary_collection=is_primary_collection,
            )
            if is_primary:  # at most one primary per entity
                BankAccount.objects.filter(entity=entity, is_primary=True).exclude(
                    pk=bank.pk).update(is_primary=False)
        return success_response(
            f"Bank account '{name}' created.",
            data=BankAccountSerializer(bank, context={"request": request}).data,
            status=201,
        )


class _BankBranchAllocationSerializer(serializers.Serializer):
    """One explicitly agreed branch share of a legacy bank balance."""

    branch = serializers.IntegerField(min_value=1)
    opening_balance = serializers.IntegerField()
    bank_account_name = serializers.CharField(max_length=160, trim_whitespace=True)
    ledger_account_code = serializers.RegexField(r"^[0-9]{4}$")
    ledger_account_name = serializers.CharField(max_length=160, trim_whitespace=True)
    is_primary = serializers.BooleanField(required=True)
    is_primary_collection = serializers.BooleanField(required=True)


class _BankAccountBranchSplitSerializer(serializers.Serializer):
    """The cutover date, evidence reference, difference treatment and complete branch allocation."""

    split_date = serializers.DateField()
    agreement_reference = serializers.CharField(max_length=64, trim_whitespace=True)
    difference_treatment = serializers.ChoiceField(
        choices=BankSplitDifferenceTreatment.choices,
        default=BankSplitDifferenceTreatment.DEBT,
    )
    allocations = _BankBranchAllocationSerializer(
        many=True,
        min_length=2,
        max_length=100,
    )


class BankAccountBranchSplitView(_FinanceBase):
    """Split one legacy unbranched bank ledger into branch-owned successors.

    Every allocation states its signed opening balance, new bank name, ledger
    code and both primary choices. ``difference_treatment`` chooses what happens
    where a branch's book balance on the shared ledger differs from its agreed
    share: ``DEBT`` (the default) books it as inter-branch balances, returned as
    ``inter_branch_transfers``; ``PERMANENT_MOVE`` passes it through retained
    earnings. The operation changes several branches at once, so the
    bank-account update key must come through whole-tenant reach.

    GET previews the split before anyone agrees a share: ``?split_date=`` (the
    tenant's today when omitted, never later) returns ``legacy_balance``,
    ``unbranched_balance`` and ``branches`` (``branch_id``, ``branch_name``,
    ``book_balance``), each branch's own entries on the shared ledger read the
    way the split itself reads them (:func:`vs_finance.bank_splits.split_preview`).
    It needs the bank-account view key through whole-tenant reach: a shared
    account is visible only to a whole-tenant reader, and the preview shows
    every branch's figure on it.

    docstring-name: Split a shared bank account by branch
    """

    @property
    def rbac_permission(self):
        return "finance.bankaccount.view" if self.request.method == "GET" \
            else "finance.bankaccount.update"

    def _shared_source(self, request, pk):
        """The entity and the account behind ``pk``, for a caller who reaches every branch."""
        entity = resolve_entity(request)
        if not caller_reaches_whole_tenant(request.user, entity.tenant):
            raise PermissionDenied(
                shared_write_refusal("a bank account used by several branches")
            )
        source = (
            BankAccount.objects.filter(entity=entity, pk=pk)
            .select_related("entity__tenant", "gl_account", "branch")
            .first()
        )
        if source is None:
            raise NotFound("Bank account not found for this entity.")
        return entity, source

    def get(self, request, pk):
        from vs_config.clock import tenant_today

        from ..bank_splits import split_preview

        entity, source = self._shared_source(request, pk)
        today = tenant_today(entity.tenant)
        split_date = _date(request.query_params.get("split_date"), "split_date") or today
        if split_date > today:
            raise ValidationError({"split_date": "The split date cannot be after today."})
        preview = split_preview(source, split_date=split_date)
        return success_response(
            f"Split preview for bank account '{source.name}' retrieved.",
            data={"bank_account_id": source.pk, **preview},
        )

    def post(self, request, pk):
        from ..bank_splits import split_shared_bank_account

        entity, source = self._shared_source(request, pk)
        payload = _BankAccountBranchSplitSerializer(data=request.data or {})
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        result = split_shared_bank_account(
            source,
            values["allocations"],
            split_date=values["split_date"],
            agreement_reference=values["agreement_reference"],
            difference_treatment=values["difference_treatment"],
            actor_user=request.user,
        )
        return success_response(
            f"Bank account '{source.name}' split into branch accounts.",
            data={
                "legacy_bank_account_id": source.pk,
                "legacy_balance": result.legacy_balance,
                "bank_accounts": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "branch_id": row.branch_id,
                        "gl_account_id": row.gl_account_id,
                        "gl_account_code": row.gl_account_code,
                        "opening_balance": row.opening_balance,
                        "is_primary": row.is_primary,
                        "is_primary_collection": row.is_primary_collection,
                    }
                    for row in result.allocations
                ],
                "journal_ids": [entry.pk for entry in result.journals],
                "difference_treatment": result.difference_treatment,
                "inter_branch_transfers": [
                    {
                        "id": transfer.pk,
                        "document_number": transfer.document_number,
                        "from_branch_id": transfer.branch_id,
                        "to_branch_id": transfer.to_branch_id,
                        "amount": int(transfer.amount),
                    }
                    for transfer in result.transfers
                ],
            },
            status=201,
        )


# Group endpoint behavior for Bank Account Detail View.
class BankAccountDetailView(_FinanceBase):
    """GET one bank account (with metrics, transactions, statements, reconciliations)
    or PATCH its settings (name, bank, number, currency, active, primary).

    docstring-name: Bank accounts
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.bankaccount.update" if self.request.method == "PATCH" \
            else "finance.bankaccount.view"

    # Support the bank workflow.
    def _bank(self, request, pk):
        return _bank_or_404(request, pk)

    # Support the transactions workflow.
    def _transactions(self, bank, *, book_balance, limit=50):
        """Recent GL cash lines, newest first, with a running balance.

        The running balance walks back from ``book_balance``, which is read from
        ``AccountBalance``, so the lines walked must be the same ledger
        (:func:`vs_finance.branch_ledger.ledger_lines`): a reversed entry and its
        reversal both appear, and a void leaves the earlier balances as they were.
        """
        from ..branch_ledger import ledger_lines

        lines = list(
            ledger_lines(bank.entity)
            .filter(account=bank.gl_account)
            .select_related("entry")
            .prefetch_related("bank_statement_lines")
            .order_by("-entry__date", "-id")[:limit]
        )
        running = book_balance
        out = []
        for ln in lines:
            signed = (ln.debit or 0) - (ln.credit or 0)
            out.append({
                "id": ln.id,
                "date": ln.entry.date,
                "description": ln.description or ln.entry.narration or "-",
                "reference": ln.entry.document_number or ln.entry.reference or "",
                "debit": int(ln.debit or 0),
                "credit": int(ln.credit or 0),
                "running_balance": int(running),
                "matched": bool(ln.bank_statement_lines.all()),
            })
            running -= signed
        return out

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        from ..banking import gl_account_balance, statement_balance

        bank = self._bank(request, pk)
        book = gl_account_balance(bank.gl_account)
        stmt = statement_balance(bank)
        stmt_val = stmt if stmt is not None else book
        unreconciled = bank.statement_lines.filter(status=BankLineStatus.UNMATCHED).count()
        data = BankAccountSerializer(bank, context={"request": request}).data
        data["metrics"] = {
            "book_balance": book, "statement_balance": stmt_val,
            "unreconciled_diff": book - stmt_val, "unreconciled_count": unreconciled,
        }
        data["transactions"] = self._transactions(bank, book_balance=book)
        acted_lines = BankStatementLine.objects.filter(
            statement=OuterRef("pk"),
        ).exclude(status=BankLineStatus.UNMATCHED)
        bulk_context = BankStatementImportContext.objects.filter(
            published_statement=OuterRef("pk"),
        )
        data["statements"] = BankStatementSerializer(
            annotate_statement_rollback(bank.statements.annotate(
                has_acted_lines=Exists(acted_lines),
                is_bulk_import=Exists(bulk_context),
            ))[:50],
            many=True,
        ).data
        data["reconciliations"] = BankReconciliationSerializer(
            bank.reconciliations.all()[:50], many=True).data
        return success_response("Bank account retrieved.", data=data)

    # Handle PATCH requests for this endpoint.
    def patch(self, request, pk):
        bank = self._bank(request, pk)
        body = request.data or {}
        if "name" in body:
            new_name = str(body["name"]).strip()
            if BankAccount.objects.filter(entity=bank.entity, name=new_name).exclude(pk=bank.pk).exists():
                raise ValidationError({"name": f"A bank account named '{new_name}' already exists."})
        for field in ("name", "bank_name", "account_number"):
            if field in body:
                setattr(bank, field, str(body[field]).strip())
        if "currency" in body:
            bank.currency = _resolve_currency(body.get("currency"))
        if "is_active" in body:
            bank.is_active = _bool(body.get("is_active"), default=bank.is_active)
        if "is_primary" in body:
            make_primary = _bool(body.get("is_primary"), default=bank.is_primary)
            bank.is_primary = make_primary
            if make_primary:  # at most one primary per entity
                BankAccount.objects.filter(entity=bank.entity, is_primary=True).exclude(
                    pk=bank.pk).update(is_primary=False)
        if "is_primary_collection" in body:
            make_primary_collection = _bool(
                body.get("is_primary_collection"), default=bank.is_primary_collection)
            bank.is_primary_collection = make_primary_collection
            if make_primary_collection:  # At most one pay-to account per branch.
                with transaction.atomic():
                    BankAccount.objects.filter(
                        entity=bank.entity, branch_id=bank.branch_id,
                        is_primary_collection=True).exclude(
                        pk=bank.pk).update(is_primary_collection=False)
                    bank.save()
            else:
                bank.save()
        else:
            bank.save()
        return success_response(
            f"Bank account '{bank.name}' updated.",
            data=BankAccountSerializer(bank, context={"request": request}).data)


# Group endpoint behavior for Bank Statement Line View.
class BankStatementLineView(_FinanceBase):
    """GET (list) statement lines / POST import a batch of statement lines.

    docstring-name: Bank statement lines
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.bankaccount.import" if self.request.method == "POST" \
            else "finance.bankaccount.view"

    # Support the bank workflow.
    def _bank(self, request, pk):
        return _bank_or_404(request, pk)

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        bank = self._bank(request, pk)
        acted_lines = BankStatementLine.objects.filter(
            statement_id=OuterRef("statement_id"),
        ).exclude(status=BankLineStatus.UNMATCHED)
        bulk_context = BankStatementImportContext.objects.filter(
            published_statement_id=OuterRef("statement_id"),
        )
        qs = (
            BankStatementLine.objects.filter(bank_account=bank)
            .select_related(
                "statement",
                "matched_line__entry",
                "adjusting_journal",
            )
            .annotate(
                statement_has_acted_lines=Exists(acted_lines),
                statement_is_bulk_import=Exists(bulk_context),
            )
        )
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        return self.paginate(request, qs, BankStatementLineSerializer)

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import import_statement_lines

        bank = self._bank(request, pk)
        rows = _require_lines(request.data or {})
        parsed = []
        for i, row in enumerate(rows):
            parsed.append({
                "txn_date": _date(row.get("txn_date"), f"lines[{i}].txn_date", required=True),
                "amount": _signed_money(row.get("amount"), f"lines[{i}].amount"),
                "description": row.get("description", ""),
                "reference": row.get("reference", ""),
                "external_id": row.get("external_id", ""),
            })
        body = request.data or {}
        _, created, suspected = import_statement_lines(
            bank, parsed, actor_user=request.user,
            force=_bool(body.get("force", False), default=False),
            statement_date=_date(body.get("statement_date"), "statement_date"),
            period_label=str(body.get("period_label", "")).strip(),
            opening_balance=_signed_money(body.get("opening_balance", 0), "opening_balance"),
            closing_balance=(_signed_money(body.get("closing_balance"), "closing_balance")
                             if body.get("closing_balance") not in (None, "") else None),
        )
        message = f"Imported {counted(len(created), 'statement line')}."
        if suspected:
            message += (f" {counted(len(suspected), 'suspected duplicate')} held back - "
                        f"re-send with force=true to import them anyway.")
        return success_response(
            message,
            data={
                "imported": BankStatementLineSerializer(created, many=True).data,
                "suspected_duplicates": suspected,
            },
            status=201,
        )


class BankStatementLineDetailView(_FinanceBase):
    """Delete one incorrect, unmatched line from a manual statement."""

    rbac_permission = "finance.bankaccount.import"

    def delete(self, request, pk):
        entity = resolve_entity(request)
        with transaction.atomic():
            line = (
                BankStatementLine.objects.select_for_update()
                .select_related("bank_account__entity")
                .filter(
                    transaction_branch_q(request, "bank_account__"),
                    pk=pk, bank_account__entity=entity,
                )
                .first()
            )
            if line is None:
                raise NotFound("Statement line not found for this entity.")

            from ..banking import statement_line_delete_block_reason

            if line.statement_id is None:
                if reason := statement_line_delete_block_reason(line):
                    raise ValidationError({"line": reason})
                line_id = line.pk
                before = {
                    "txn_date": line.txn_date.isoformat(),
                    "description": line.description,
                    "reference": line.reference,
                    "amount": int(line.amount),
                    "external_id": line.external_id,
                }
                bank_account = line.bank_account
                line.delete()
                record(
                    entity=bank_account.entity,
                    action=FinanceAuditAction.BANK_STATEMENT_CORRECTED,
                    actor_user=request.user,
                    target_type="BankStatementLine",
                    target_id=str(line_id),
                    message=f"Deleted orphan bank statement line {line_id}.",
                    before=before,
                    after={"deleted": True},
                    bank_account_id=bank_account.pk,
                    branch=bank_account.branch_id,
                )
                deleted_statement_id = None
            else:
                statement = (
                    BankStatement.objects.select_for_update()
                    .select_related("bank_account__entity")
                    .get(pk=line.statement_id)
                )
                line.statement = statement
                if reason := statement_line_delete_block_reason(line):
                    raise ValidationError({"line": reason})

                current_lines = list(
                    BankStatementLine.objects.select_for_update()
                    .filter(statement=statement)
                    .order_by("txn_date", "id")
                )
                before = _statement_snapshot(statement, current_lines)
                line_id = line.pk
                statement_id = statement.pk
                bank_account = statement.bank_account
                line.delete()

                remaining = list(statement.lines.order_by("txn_date", "id"))
                if remaining:
                    statement.closing_balance = (
                        statement.opening_balance
                        + sum(int(item.amount) for item in remaining)
                    )
                    statement.save(
                        update_fields=["closing_balance", "updated_at"],
                    )
                    after = _statement_snapshot(statement, remaining)
                    record(
                        entity=bank_account.entity,
                        action=FinanceAuditAction.BANK_STATEMENT_CORRECTED,
                        actor_user=request.user,
                        target=statement,
                        message=(
                            f"Deleted line {line_id} from bank statement "
                            f"{statement_id}."
                        ),
                        before=before,
                        after=after,
                        bank_account_id=bank_account.pk,
                        deleted_line_id=line_id,
                        branch=bank_account.branch_id,
                    )
                    deleted_statement_id = None
                else:
                    statement.delete()
                    record(
                        entity=bank_account.entity,
                        action=FinanceAuditAction.BANK_STATEMENT_CORRECTED,
                        actor_user=request.user,
                        target_type="BankStatement",
                        target_id=str(statement_id),
                        message=(
                            f"Deleted bank statement {statement_id} after its "
                            "final line was removed."
                        ),
                        before=before,
                        after={"deleted": True, "lines": []},
                        bank_account_id=bank_account.pk,
                        deleted_line_id=line_id,
                        branch=bank_account.branch_id,
                    )
                    deleted_statement_id = statement_id

        return success_response(
            (
                "Statement line and empty statement deleted."
                if deleted_statement_id
                else "Statement line deleted."
            ),
            data={
                "deleted_line_id": line_id,
                "deleted_statement_id": deleted_statement_id,
            },
        )


class _BankStatementCorrectionLineSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, min_value=1)
    txn_date = serializers.DateField()
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
    )
    reference = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,
    )
    amount = serializers.IntegerField()
    external_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
    )


class _BankStatementCorrectionSerializer(serializers.Serializer):
    statement_date = serializers.DateField()
    period_label = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=120,
    )
    opening_balance = serializers.IntegerField()
    lines = _BankStatementCorrectionLineSerializer(many=True, allow_empty=False)

    def validate_lines(self, lines):
        ids = [line["id"] for line in lines if line.get("id") is not None]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("A statement line can appear only once.")

        external_ids = [
            str(line.get("external_id") or "").strip()
            for line in lines
            if str(line.get("external_id") or "").strip()
        ]
        if len(external_ids) != len(set(external_ids)):
            raise serializers.ValidationError(
                "Transaction IDs must be unique within the statement."
            )
        return lines


def _statement_snapshot(statement, lines) -> dict:
    return {
        "statement_date": statement.statement_date.isoformat(),
        "period_label": statement.period_label,
        "opening_balance": int(statement.opening_balance),
        "closing_balance": int(statement.closing_balance),
        "lines": [
            {
                "id": line.id,
                "txn_date": line.txn_date.isoformat(),
                "description": line.description,
                "reference": line.reference,
                "amount": int(line.amount),
                "external_id": line.external_id,
            }
            for line in lines
        ],
    }


class BankStatementDetailView(_FinanceBase):
    """Read or safely correct one manually imported, unreconciled statement."""

    @property
    def rbac_permission(self):
        return (
            "finance.bankaccount.import"
            if self.request.method == "PATCH"
            else "finance.bankaccount.view"
        )

    def _statement(self, request, pk, statement_id, *, lock=False):
        entity = resolve_entity(request)
        acted_lines = BankStatementLine.objects.filter(
            statement=OuterRef("pk"),
        ).exclude(status=BankLineStatus.UNMATCHED)
        bulk_context = BankStatementImportContext.objects.filter(
            published_statement=OuterRef("pk"),
        )
        queryset = (
            BankStatement.objects.filter(
                transaction_branch_q(request, "bank_account__"),
                pk=statement_id,
                bank_account_id=pk,
                bank_account__entity=entity,
            )
            .select_related("bank_account__entity")
            .annotate(
                has_acted_lines=Exists(acted_lines),
                is_bulk_import=Exists(bulk_context),
            )
        )
        if lock:
            queryset = queryset.select_for_update()
        statement = queryset.first()
        if statement is None:
            raise NotFound("Bank statement not found for this entity and account.")
        return statement

    def get(self, request, pk, statement_id):
        statement = self._statement(request, pk, statement_id)
        return success_response(
            "Bank statement retrieved.",
            data=BankStatementDetailSerializer(statement).data,
        )

    def patch(self, request, pk, statement_id):
        correction = _BankStatementCorrectionSerializer(data=request.data)
        correction.is_valid(raise_exception=True)
        values = correction.validated_data

        with transaction.atomic():
            statement = self._statement(
                request,
                pk,
                statement_id,
                lock=True,
            )
            from ..banking import statement_edit_block_reason

            if reason := statement_edit_block_reason(statement):
                raise ValidationError({"statement": reason})

            current_lines = list(
                BankStatementLine.objects.select_for_update()
                .filter(statement=statement)
                .order_by("txn_date", "id")
            )
            current_by_id = {line.id: line for line in current_lines}
            submitted_ids = {
                line["id"]
                for line in values["lines"]
                if line.get("id") is not None
            }
            unknown_ids = submitted_ids - set(current_by_id)
            if unknown_ids:
                raise ValidationError({
                    "lines": "One or more lines do not belong to this statement.",
                })

            external_ids = {
                str(line.get("external_id") or "").strip()
                for line in values["lines"]
                if str(line.get("external_id") or "").strip()
            }
            if external_ids and BankStatementLine.objects.filter(
                bank_account=statement.bank_account,
                external_id__in=external_ids,
            ).exclude(statement=statement).exists():
                raise ValidationError({
                    "lines": (
                        "A transaction ID is already used by another statement "
                        "on this bank account."
                    ),
                })

            before = _statement_snapshot(statement, current_lines)
            BankStatementLine.objects.filter(
                statement=statement,
            ).exclude(id__in=submitted_ids).delete()

            updated = []
            created = []
            movement = 0
            for line_data in values["lines"]:
                normalized = {
                    "txn_date": line_data["txn_date"],
                    "description": str(line_data.get("description") or "").strip(),
                    "reference": str(line_data.get("reference") or "").strip(),
                    "amount": int(line_data["amount"]),
                    "external_id": str(line_data.get("external_id") or "").strip(),
                }
                movement += normalized["amount"]
                if line_data.get("id") is None:
                    created.append(
                        BankStatementLine(
                            statement=statement,
                            bank_account=statement.bank_account,
                            **normalized,
                        )
                    )
                else:
                    line = current_by_id[line_data["id"]]
                    for field, value in normalized.items():
                        setattr(line, field, value)
                    line.updated_at = timezone.now()
                    updated.append(line)

            if updated:
                BankStatementLine.objects.bulk_update(
                    updated,
                    [
                        "txn_date",
                        "description",
                        "reference",
                        "amount",
                        "external_id",
                        "updated_at",
                    ],
                )
            if created:
                BankStatementLine.objects.bulk_create(created)

            statement.statement_date = values["statement_date"]
            statement.period_label = str(values.get("period_label") or "").strip()
            statement.opening_balance = int(values["opening_balance"])
            statement.closing_balance = statement.opening_balance + movement
            statement.save(
                update_fields=[
                    "statement_date",
                    "period_label",
                    "opening_balance",
                    "closing_balance",
                    "updated_at",
                ]
            )
            final_lines = list(statement.lines.order_by("txn_date", "id"))
            after = _statement_snapshot(statement, final_lines)
            record(
                entity=statement.bank_account.entity,
                action=FinanceAuditAction.BANK_STATEMENT_CORRECTED,
                actor_user=request.user,
                target=statement,
                message=f"Corrected bank statement {statement.pk}.",
                before=before,
                after=after,
                bank_account_id=statement.bank_account_id,
                branch=statement.bank_account.branch_id,
            )

        return success_response(
            "Bank statement corrected.",
            data=BankStatementDetailSerializer(statement).data,
        )


class _BankStatementWizardUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    statement_date = serializers.DateField()
    opening_balance = serializers.DecimalField(max_digits=20, decimal_places=2)
    closing_balance = serializers.DecimalField(max_digits=20, decimal_places=2)
    period_label = serializers.CharField(required=False, allow_blank=True, max_length=120)
    sheet_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    header_row_index = serializers.IntegerField(required=False, default=1, min_value=1)
    notes = serializers.CharField(required=False, allow_blank=True)


class BankStatementImportTemplateView(_FinanceBase):
    """Download the canonical statement file used by the shared import wizard."""

    rbac_permission = "finance.bankaccount.import"

    def get(self, request, pk):
        from vs_import_data.models import (
            FileFormatChoices,
            ImportTemplate,
            TemplateStatusChoices,
        )
        from vs_import_data.services.template_file import (
            generate_template_csv,
            generate_template_xlsx,
        )

        bank = _bank_or_404(request, pk, active_only=True)
        template = ImportTemplate.objects.filter(
            code="bank_statements_v1",
            status=TemplateStatusChoices.ACTIVE,
            is_download_enabled=True,
        ).prefetch_related("columns").first()
        if template is None:
            raise ValidationError({
                "template": "Bank statement import template is not installed.",
            })

        requested_format = str(
            request.query_params.get("file_format", FileFormatChoices.XLSX)
        ).lower()
        if requested_format == FileFormatChoices.CSV:
            response = HttpResponse(generate_template_csv(template), content_type="text/csv")
            extension = "csv"
        elif requested_format == FileFormatChoices.XLSX:
            response = HttpResponse(
                generate_template_xlsx(template),
                content_type=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
            )
            extension = "xlsx"
        else:
            raise ValidationError({"file_format": "Choose csv or xlsx."})
        response["Content-Disposition"] = (
            f'attachment; filename="bank_statement_{bank.id}_template.{extension}"'
        )
        return response


class BankStatementImportWizardView(_FinanceBase):
    """Upload a statement file, attach finance context and enter the import wizard."""

    rbac_permission = "finance.bankaccount.import"

    def post(self, request, pk):
        from vs_import_data.models import ImportTemplate, TemplateStatusChoices
        from vs_import_data.serializers import (
            ImportBatchDetailSerializer,
            ImportBatchUploadSerializer,
        )
        from vs_import_data.services.validation_service import validate_import_batch
        from ..models import BankStatementImportContext
        from ..money import to_kobo

        entity = resolve_entity(request)
        bank = _bank_or_404(request, pk, entity=entity, active_only=True)

        # A statement takes its account's branch, never the uploader's.
        statement_branch_id = _inherited_branch_id(request, bank)
        statement_branch = bank.branch if statement_branch_id is not None else None

        metadata = _BankStatementWizardUploadSerializer(data=request.data)
        metadata.is_valid(raise_exception=True)
        values = metadata.validated_data
        uploaded_file = values["file"]

        template = ImportTemplate.objects.filter(
            code="bank_statements_v1",
            status=TemplateStatusChoices.ACTIVE,
            is_download_enabled=True,
        ).prefetch_related("columns").first()
        if template is None:
            raise ValidationError({
                "template": "Bank statement import template is not installed.",
            })

        digest = hashlib.sha256()
        for chunk in uploaded_file.chunks():
            digest.update(chunk)
        uploaded_file.seek(0)

        upload = ImportBatchUploadSerializer(
            data={
                "template_id": template.pk,
                "file": uploaded_file,
                "sheet_name": values.get("sheet_name", ""),
                "header_row_index": values.get("header_row_index", 1),
                "notes": values.get("notes", ""),
            },
            context={"request": request, "branch": statement_branch},
        )
        upload.is_valid(raise_exception=True)

        with transaction.atomic():
            import_batch = upload.save()
            BankStatementImportContext.objects.create(
                import_batch=import_batch,
                bank_account=bank,
                statement_date=values["statement_date"],
                period_label=values.get("period_label", "").strip(),
                opening_balance=to_kobo(values["opening_balance"]),
                closing_balance=to_kobo(values["closing_balance"]),
                source_file_hash=digest.hexdigest(),
            )
            # Validation is the wizard's pre-publish gate. Run it immediately so
            # the next screen already has the balance and duplicate report.
            validate_import_batch(import_batch)

        import_batch = (
            import_batch.__class__.objects
            .select_related(
                "tenant",
                "uploaded_by",
                "template",
                "bank_statement_context__bank_account__entity",
                "bank_statement_context__published_statement",
            )
            .prefetch_related(
                "template__columns",
                "validation_issues",
                "notifications",
            )
            .get(pk=import_batch.pk)
        )
        data = dict(ImportBatchDetailSerializer(
            import_batch,
            context={"request": request},
        ).data)
        data["wizard"] = {
            "detail": f"/v1/import/batches/{import_batch.pk}/",
            "validate": f"/v1/import/batches/{import_batch.pk}/validate/",
            "publish": f"/v1/import/batches/{import_batch.pk}/start-import/",
            "jobs": f"/v1/import/batches/{import_batch.pk}/jobs/",
        }
        return success_response(
            "Bank statement uploaded to the import wizard.",
            data=data,
            status=201,
        )


# Group endpoint behavior for Bank Auto Reconcile View.
class BankAutoReconcileView(_FinanceBase):
    """POST - auto-match unmatched statement lines to posted cash journal lines.

    docstring-name: Auto-reconcile a bank statement
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import auto_reconcile

        entity = resolve_entity(request)
        bank = _bank_or_404(request, pk, entity=entity)
        body = request.data or {}
        from ..banking_settings import resolve_finance_banking_settings
        policy = resolve_finance_banking_settings(entity)
        if "tolerance_days" in body:
            supplied_tolerance = _int(
                body.get("tolerance_days"), "tolerance_days", minimum=0,
            )
            tolerance = (
                policy.default_bank_reconciliation_tolerance_days
                if supplied_tolerance is None else supplied_tolerance
            )
        else:
            tolerance = policy.default_bank_reconciliation_tolerance_days
        group = _bool(
            body.get("group"), default=policy.default_group_reconciliation_matches,
        )
        matched = auto_reconcile(
            bank, tolerance_days=tolerance, group=group, actor_user=request.user)
        return success_response(
            f"Auto-matched {counted(len(matched), 'statement line')}.",
            data=BankStatementLineSerializer(matched, many=True).data,
        )


# Group endpoint behavior for Bank Book Lines View.
class BankBookLinesView(_FinanceBase):
    """GET - posted cash-account journal lines not yet matched to a statement line.

    The "book" side of the reconciliation workbench. ``amount`` is signed kobo
    (debit − credit) so it lines up with a statement line's signed amount.

    docstring-name: Unmatched book lines
    """

    rbac_permission = "finance.bankaccount.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        from core.pagination import XVSPagination
        from ..banking import _unmatched_gl_lines
        from ..models import Customer

        entity = resolve_entity(request)
        bank = _bank_or_404(request, pk, entity=entity)

        # Posting bakes the customer *code* into line descriptions ("Receipt: CUST-002").
        # Resolve it to the human name for the reconciliation view.
        names = dict(Customer.objects.filter(entity=entity).values_list("code", "name"))

        # Handle the humanize workflow.
        def humanize(desc: str) -> str:
            label, sep, tail = (desc or "").partition(": ")
            return f"{label}: {names[tail]}" if sep and tail in names else (desc or "-")

        # Paginate the unmatched book lines (was capped at [:200]); build rows per page.
        paginator = XVSPagination()
        page = paginator.paginate_queryset(_unmatched_gl_lines(bank), request, view=self)
        rows = [{
            "id": ln.id,
            "date": ln.entry.date,
            "description": humanize(ln.description or ln.entry.narration or "-"),
            "reference": ln.entry.document_number or ln.entry.reference or "",
            "amount": int((ln.debit or 0) - (ln.credit or 0)),
        } for ln in page]
        return paginator.get_paginated_response(rows)


# Group endpoint behavior for Bank Reconcile Complete View.
class BankReconcileCompleteView(_FinanceBase):
    """POST - finalise the reconciliation, recording a snapshot of the current state.

    docstring-name: Complete a bank reconciliation
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import complete_reconciliation

        entity = resolve_entity(request)
        bank = _bank_or_404(request, pk, entity=entity)
        recon = complete_reconciliation(bank, actor_user=request.user)
        return success_response(
            "Reconciliation recorded.",
            data=BankReconciliationSerializer(recon).data, status=201,
        )


# Define Statement Line Action Base values.
class _StatementLineActionBase(_FinanceBase):
    # Support the line workflow.
    def _line(self, request, pk):
        entity = resolve_entity(request)
        line = (
            BankStatementLine.objects
            .filter(
                transaction_branch_q(request, "bank_account__"),
                pk=pk, bank_account__entity=entity,
            )
            .select_related("bank_account").first()
        )
        if line is None:
            raise NotFound("Statement line not found for this entity.")
        return entity, line


# Group endpoint behavior for Bank Statement Line Match View.
class BankStatementLineMatchView(_StatementLineActionBase):
    """POST {journal_line} - manually pair a statement line to a cash journal line.

    docstring-name: Match a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import match_line

        entity, line = self._line(request, pk)
        ref = (request.data or {}).get("journal_line")
        if ref in (None, ""):
            raise ValidationError({"journal_line": "A journal line id is required."})
        jl = JournalLine.objects.filter(pk=ref, entry__entity=entity).first()
        if jl is None:
            raise ValidationError({"journal_line": f"No journal line '{ref}' in this entity."})
        match_line(line, jl, actor_user=request.user)
        line.refresh_from_db()
        return success_response(
            "Statement line matched.", data=BankStatementLineSerializer(line).data,
        )


# Group endpoint behavior for Bank Statement Line Group Match View.
class BankStatementLineGroupMatchView(_StatementLineActionBase):
    """POST {journal_lines:[ids]} - match a statement line to several cash journal lines
    whose signed amounts sum to it (one settlement covering many receipts).

    docstring-name: Group-match a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import group_match

        entity, line = self._line(request, pk)
        ids = (request.data or {}).get("journal_lines") or []
        if not isinstance(ids, list) or len(ids) < 2:
            raise ValidationError(
                {"journal_lines": "Provide a list of at least two journal line ids."})
        jls = list(
            JournalLine.objects.filter(pk__in=ids, entry__entity=entity).select_related("entry"))
        missing = set(map(str, ids)) - {str(jl.id) for jl in jls}
        if missing:
            raise ValidationError(
                {"journal_lines": f"Not found in this entity: {', '.join(sorted(missing))}."})
        group_match(line, jls, actor_user=request.user)
        line.refresh_from_db()
        return success_response(
            "Statement line group-matched.",
            data=BankStatementLineSerializer(line).data, status=201,
        )


# Group endpoint behavior for Bank Split Match View.
class BankSplitMatchView(_FinanceBase):
    """POST {journal_line, statement_lines:[ids]} - match one cash journal line to
    several statement lines that sum to it (one ledger movement the bank split).

    docstring-name: Split-match a cash journal line
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import split_match

        entity = resolve_entity(request)
        bank = _bank_or_404(request, pk, entity=entity)
        body = request.data or {}
        jl_ref = body.get("journal_line")
        jl = (JournalLine.objects.filter(pk=jl_ref, entry__entity=entity).select_related("entry").first()
              if jl_ref not in (None, "") else None)
        if jl is None:
            raise ValidationError({"journal_line": "A valid journal line id is required."})
        ids = body.get("statement_lines") or []
        if not isinstance(ids, list) or len(ids) < 2:
            raise ValidationError(
                {"statement_lines": "Provide a list of at least two statement line ids."})
        slines = list(
            BankStatementLine.objects.filter(pk__in=ids, bank_account=bank).select_related("bank_account"))
        missing = set(map(str, ids)) - {str(s.id) for s in slines}
        if missing:
            raise ValidationError(
                {"statement_lines": f"Not found on this bank account: {', '.join(sorted(missing))}."})
        split_match(jl, slines, actor_user=request.user)
        rows = BankStatementLine.objects.filter(pk__in=[s.id for s in slines])
        return success_response(
            "Journal line split-matched.",
            data=BankStatementLineSerializer(rows, many=True).data, status=201,
        )


# Group endpoint behavior for Bank Statement Line Adjust View.
class BankStatementLineAdjustView(_StatementLineActionBase):
    """POST {counter_account?, counter_code?, narration?, posting_date?} - book + match.

    ``posting_date`` is optional: omitted, the adjustment books on the statement
    line's own date, or on the nearest open day when that date's period is closed
    (a statement legitimately covers a closed month). The line's date is kept on
    the journal as the bank's value date either way.

    docstring-name: Post an adjustment from a statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import post_bank_adjustment

        entity, line = self._line(request, pk)
        body = request.data or {}
        # Both spellings resolve here, under the caller's reach, never in the service.
        # Another bank's ledger as the counter moves that bank's money, so it obeys
        # the statement's own bank's branch.
        rule = {"document_branch": line.bank_account.branch_id, "noun": "bank adjustment",
                "verb": "Book it against"}
        counter = (
            _resolve_account(request, entity, body.get("counter_account"), "counter_account", **rule)
            or _resolve_account(request, entity, body.get("counter_code"), "counter_code", **rule)
        )
        post_bank_adjustment(
            line, counter_account=counter,
            narration=body.get("narration", ""),
            posting_date=_date(body.get("posting_date"), "posting_date"),
            actor_user=request.user,
        )
        line.refresh_from_db()
        return success_response(
            "Bank adjustment booked and line matched.",
            data=BankStatementLineSerializer(line).data, status=201,
        )


# Group endpoint behavior for Bank Statement Line Unmatch View.
class BankStatementLineUnmatchView(_StatementLineActionBase):
    """POST - undo a match (reverses the adjusting journal if the match created one).

    docstring-name: Unmatch a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import unmatch_line

        _, line = self._line(request, pk)
        unmatch_line(line, actor_user=request.user)
        line.refresh_from_db()
        return success_response(
            "Statement line unmatched.", data=BankStatementLineSerializer(line).data,
        )


# Group endpoint behavior for Bank Statement Line Ignore View.
class BankStatementLineIgnoreView(_StatementLineActionBase):
    """POST {ignored?: true, reason?} - mark an unmatched line IGNORED (a known
    duplicate / opening-balance line), or revert it with ``ignored: false``.

    docstring-name: Ignore a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import set_line_ignored

        _, line = self._line(request, pk)
        body = request.data or {}
        set_line_ignored(
            line, ignored=_bool(body.get("ignored", True), default=True),
            reason=body.get("reason", ""), actor_user=request.user,
        )
        line.refresh_from_db()
        return success_response(
            "Statement line updated.", data=BankStatementLineSerializer(line).data,
        )


# --------------------------------------------------------------------------- #
# Bank transactions                                                           #
# --------------------------------------------------------------------------- #

def _overview_key(model) -> str:
    """The serializer-context key a page's approval overview is cached under, per model."""
    return f"approval_overview:{model._meta.label_lower}"


class _ApprovalStateListSerializer(serializers.ListSerializer):
    """Read a page's approval overview in one query (:func:`vs_finance.approvals.approval_overview`)."""

    def to_representation(self, data):
        from ..approvals import approval_overview

        rows = list(data)
        if rows:
            self.context.setdefault(_overview_key(type(rows[0])), {}).update(
                approval_overview(rows))
        return super().to_representation(rows)


class _ApprovalStateMixin(serializers.Serializer):
    """``branch_name``, ``approval_state`` and ``approval_returned`` for a money document.

    ``approval_state`` is ``NOT_SUBMITTED``, ``PENDING``, ``APPROVED`` or ``REJECTED``,
    the procurement documents' vocabulary for where a document stands with its route.
    ``approval_returned`` is True while an approver has handed the document back to
    whoever sent it: its sender corrects it with PATCH and resumes it from the
    approvals screen (:class:`vs_finance.serializers.ApprovalStateMixin`).
    """

    branch_name = serializers.CharField(source="branch.name", read_only=True, default=None)
    approval_state = serializers.SerializerMethodField()
    approval_returned = serializers.SerializerMethodField()

    def _approval(self, obj) -> tuple:
        from ..approvals import approval_overview

        overview = self.context.setdefault(_overview_key(type(obj)), {})
        if obj.pk not in overview:
            overview.update(approval_overview([obj]))
        return overview[obj.pk]

    def get_approval_state(self, obj) -> str:
        return self._approval(obj)[0]

    def get_approval_returned(self, obj) -> bool:
        return self._approval(obj)[1]


class BankTransactionSerializer(_ApprovalStateMixin, serializers.ModelSerializer):
    """A bank transaction with its bank account, counter-account and branch named."""

    bank_account_name = serializers.CharField(source="bank_account.name", read_only=True)
    counter_account_code = serializers.CharField(source="counter_account.code", read_only=True)
    counter_account_name = serializers.CharField(source="counter_account.name", read_only=True)

    class Meta:
        model = BankTransaction
        list_serializer_class = _ApprovalStateListSerializer
        fields = [
            "id", "document_number", "status", "branch_id",
            "bank_account_id", "bank_account_name", "direction", "amount",
            "counter_account_id", "counter_account_code", "counter_account_name",
            "transaction_date", "narration", "reference", "journal_id",
            "branch_name", "approval_state", "approval_returned",
        ]


def _transaction_or_404(request, entity, pk):
    """A bank transaction of the caller's own branches, or 404 (see :func:`_transactions_in_reach`)."""
    txn = (
        _transactions_in_reach(request, entity).filter(pk=pk)
        .select_related("bank_account", "counter_account", "branch")
        .first()
    )
    if txn is None:
        raise NotFound("Bank transaction not found for this entity.")
    return txn


def _transactions_in_reach(request, entity):
    """Bank transactions read as every transaction is: by their own branch, exclusively.

    A branch-bound bursar sees their branches' transactions and none that has not
    been given a branch (:func:`vs_rbac.scoping.transaction_branch_q`).
    """
    return BankTransaction.objects.filter(transaction_branch_q(request), entity=entity)


def _send_or_post(document, request, *, label, noun, serializer, post, status=200):
    """Route a draft bank document for approval, or post it when nothing routes it.

    The one way a bank transaction or transfer leaves the draft state, whether it
    was just written or was corrected after a rejection: a route with steps holds
    it for approval (the response carries the ``approval`` block), an empty route
    needs ``confirm_without_approval``, and no route posts it at once
    (:func:`vs_finance.approvals.approval_required`). Run inside the caller's
    transaction.
    """
    from vs_workflow.services import release as release_svc
    from vs_workflow.services.submission import submit_for_approval

    from ..approvals import approval_required, confirm_unconfigured_post

    if approval_required(document):
        instance = submit_for_approval(document, requested_by=request.user)
        document.refresh_from_db()
        return success_response(
            message=(
                f"{label} {document.document_number} is waiting for approval. "
                f"It reaches the books once it is approved."
            ),
            data=serializer(document).data | {"approval": release_svc.approval_block(instance)},
            status=status,
        )
    confirm_unconfigured_post(document, request, noun=noun)
    post(document, actor_user=request.user)
    document.refresh_from_db()
    return success_response(
        message=f"{label} posted as {document.document_number}.",
        data=serializer(document).data, status=status,
    )


def _transaction_fields(request, entity, body, *, editing=False) -> dict:
    """The bank transaction fields ``body`` sets, checked as creating one checks them.

    When ``editing``, a key the body leaves out keeps the draft's value, and one it
    names is checked exactly as on create: the bank account within the caller's
    branches (404 otherwise), the document taking that account's branch.
    """
    from ..banking import money_branch_id
    from ..constants import BankTransactionDirection

    fields = {}
    if not editing or "bank_account" in body:
        bank = _bank_account_in_reach(request, entity, body.get("bank_account"), "bank_account")
        fields["bank_account"] = bank
        fields["branch_id"] = money_branch_id(entity, bank)
    if not editing or "direction" in body:
        direction = str(body.get("direction") or "").upper()
        if direction not in BankTransactionDirection.values:
            raise ValidationError({"direction": "Choose IN (money in) or OUT (money out)."})
        fields["direction"] = direction
    if not editing or "amount" in body:
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValidationError({"amount": "Amount must be a whole number greater than zero."})
        fields["amount"] = amount
    if not editing or "narration" in body:
        narration = str(body.get("narration") or "").strip()
        if not narration:
            raise ValidationError({"narration": "Say what the money is."})
        fields["narration"] = narration[:255]
    if not editing or "counter_account" in body:
        fields["counter_account"] = _resolve_account(
            request, entity, body.get("counter_account"), "counter_account", required=True,
        )
    if not editing or "transaction_date" in body:
        fields["transaction_date"] = _date(
            body.get("transaction_date"), "transaction_date", required=True)
    if not editing or "reference" in body:
        fields["reference"] = str(body.get("reference") or "")[:64]
    return fields


class BankTransactionListCreateView(_FinanceBase):
    """GET/POST /finance/bank-transactions/?entity= - money in or out of a bank account.

    POST body: ``bank_account`` (id or name), ``direction`` (``IN`` or ``OUT``),
    ``amount`` (kobo), ``counter_account`` (code or id), ``transaction_date``,
    ``narration`` and an optional ``reference``. The other side must be an ordinary
    account: one a sub-ledger keeps (AR, AP, a bank ledger, tax, stock...) is refused
    and the message names the document to use instead. The bank account is one of
    the caller's own branches' (404 otherwise) and the transaction carries its branch
    (:func:`vs_finance.banking.money_branch_id`); an account not yet given a branch
    at a school with several moves nothing (400).

    Approval follows the ``finance.bank_transaction`` route exactly as a direct entry
    follows the journal route: a route with steps holds it for approval (201 with an
    ``approval`` block), an empty route needs ``confirm_without_approval``, and no route
    posts it at once.

    docstring-name: Bank transactions
    """

    @property
    def rbac_permission(self):
        return "finance.banktransaction.create" if self.request.method == "POST" \
            else "finance.banktransaction.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _transactions_in_reach(request, entity).select_related(
            "bank_account", "counter_account", "branch")
        if (bank := request.query_params.get("bank_account")) and str(bank).isdigit():
            qs = qs.filter(bank_account_id=int(bank))
        qs = filter_by_stored_status(qs, request.query_params.get("status"))
        qs = filter_by_approval_param(qs, request.query_params)
        return self.paginate(request, qs.order_by("-transaction_date", "-id"), BankTransactionSerializer)

    def post(self, request):
        from ..banking import post_bank_transaction, validate_bank_transaction
        from ..exceptions import PostingError

        entity = resolve_entity(request)
        fields = _transaction_fields(request, entity, request.data or {})
        with transaction.atomic():
            txn = BankTransaction.objects.create(
                entity=entity, created_by=request.user, **fields)
            try:
                validate_bank_transaction(txn)
            except PostingError as exc:
                raise ValidationError({"counter_account": exc.message})
            return _send_or_post(
                txn, request, label="Bank transaction", noun="bank transaction",
                serializer=BankTransactionSerializer, post=post_bank_transaction, status=201,
            )


class BankTransactionDetailView(_FinanceBase):
    """GET/PATCH /finance/bank-transactions/<id>/?entity= - one bank transaction.

    PATCH corrects a draft that has come back from approval: any of the create
    fields, each checked as on create and within the same branch reach, the
    others kept. Back from a rejection, withdrawal or cancellation, ``submit/``
    sends it again. Returned by its approver (``approval_returned``), only the
    person who sent it may correct it (403 for anybody else), it stays at its
    branch (400), and it is resumed from the approvals screen
    (:func:`vs_finance.approvals.correcting_returned`). A transaction waiting on
    its approvers, posted, voided or cancelled is refused (422). The correction is
    audited and posts nothing. Held by whoever may create bank transactions,
    since creating one sends it for approval.

    GET and PATCH name the transaction's latest approval request as
    ``workflow_instance_id``, null before it is first sent: the id Resume posts
    to (:func:`vs_finance.approvals.with_approval_request`).

    docstring-name: Bank transactions
    """

    @property
    def rbac_permission(self):
        return "finance.banktransaction.create" if self.request.method == "PATCH" \
            else "finance.banktransaction.view"

    def get(self, request, pk):
        txn = _transaction_or_404(request, resolve_entity(request), pk)
        return success_response(
            "Bank transaction retrieved.",
            data=with_approval_request(BankTransactionSerializer(txn).data, txn))

    def patch(self, request, pk):
        from ..banking import revise_bank_document

        entity = resolve_entity(request)
        txn = _transaction_or_404(request, entity, pk)
        fields = _transaction_fields(request, entity, request.data or {}, editing=True)
        txn = revise_bank_document(txn, fields, actor_user=request.user)
        txn = _transaction_or_404(request, entity, pk)
        return success_response(
            f"Bank transaction {txn.document_number} corrected.",
            data=with_approval_request(BankTransactionSerializer(txn).data, txn),
        )


class BankTransactionSubmitView(_FinanceBase):
    """POST /finance/bank-transactions/<id>/submit/?entity= - send a corrected draft again.

    For a draft back from approval (rejected, or its request withdrawn or
    cancelled): it goes through the ``finance.bank_transaction`` route exactly as
    a new one does, so steps hold it for approval, an empty route needs
    ``confirm_without_approval``, and no route posts it. Refused (422) while its
    approvers hold it or once it is posted, voided or cancelled. Same key and
    branch reach as creating one (404 outside the caller's branches).

    docstring-name: Send a bank transaction for approval again
    """

    rbac_permission = "finance.banktransaction.create"

    def post(self, request, pk):
        from ..banking import post_bank_transaction, require_reworkable

        entity = resolve_entity(request)
        _transaction_or_404(request, entity, pk)
        with transaction.atomic():
            txn = BankTransaction.objects.select_for_update().get(pk=pk)
            require_reworkable(txn)
            return _send_or_post(
                txn, request, label="Bank transaction", noun="bank transaction",
                serializer=BankTransactionSerializer, post=post_bank_transaction,
            )


class BankTransactionCancelView(_FinanceBase):
    """POST /finance/bank-transactions/<id>/cancel/?entity= - cancel a draft that will not be sent.

    For a draft back from approval (rejected, or its request withdrawn or
    cancelled). It becomes ``CANCELLED``, the cancellation is audited, and
    nothing is posted. Refused (422) while its approvers hold it; a posted one is
    voided instead. Same key and branch reach as creating one.

    docstring-name: Cancel a bank transaction
    """

    rbac_permission = "finance.banktransaction.create"

    def post(self, request, pk):
        from ..banking import cancel_bank_document

        entity = resolve_entity(request)
        txn = cancel_bank_document(_transaction_or_404(request, entity, pk),
                                   actor_user=request.user)
        return success_response(
            f"Bank transaction {txn.document_number} cancelled.",
            data=BankTransactionSerializer(_transaction_or_404(request, entity, pk)).data,
        )


class BankTransactionVoidView(_FinanceBase):
    """POST /finance/bank-transactions/<id>/void/?entity= - reverse a posted bank transaction.

    Optional body ``date`` dates the reversal. The transaction must be one of the
    caller's own branches' (404 otherwise), as reading it is.

    docstring-name: Void a bank transaction
    """

    rbac_permission = "finance.banktransaction.reverse"

    def post(self, request, pk):
        from ..banking import void_bank_transaction

        entity = resolve_entity(request)
        txn = _transaction_or_404(request, entity, pk)
        void_bank_transaction(
            txn, actor_user=request.user,
            date=_date((request.data or {}).get("date"), "date"),
        )
        txn.refresh_from_db()
        return success_response(
            f"Bank transaction {txn.document_number} voided.",
            data=BankTransactionSerializer(txn).data,
        )



# --------------------------------------------------------------------------- #
# Transfers between a branch's own bank accounts                              #
# --------------------------------------------------------------------------- #

class BankTransferSerializer(_ApprovalStateMixin, serializers.ModelSerializer):
    """A transfer with both accounts and its branch named."""

    from_account_name = serializers.CharField(source="from_account.name", read_only=True)
    to_account_name = serializers.CharField(source="to_account.name", read_only=True)

    class Meta:
        model = BankTransfer
        list_serializer_class = _ApprovalStateListSerializer
        fields = [
            "id", "document_number", "status", "branch_id",
            "from_account_id", "from_account_name", "to_account_id", "to_account_name",
            "amount", "transfer_date", "narration", "reference", "journal_id",
            "branch_name", "approval_state", "approval_returned",
        ]


def _transfers_in_reach(request, entity):
    """Transfers read by their own branch, exclusively, as every transaction is.

    A transfer's two accounts are its branch's (:func:`vs_finance.banking.validate_bank_transfer`),
    so its branch is the whole of the question.
    """
    return BankTransfer.objects.filter(
        transaction_branch_q(request), entity=entity,
    ).select_related("from_account", "to_account", "branch")


def _transfer_fields(request, entity, body, *, current=None) -> dict:
    """The transfer fields ``body`` sets, checked as creating one checks them.

    With ``current`` (an edit), a key the body leaves out keeps the draft's value.
    Naming either account re-derives the branch from both, so a correction that
    leaves the two accounts in different branches is refused as on create.
    """
    from ..banking import money_branch_id

    fields = {}
    if current is None or "from_account" in body or "to_account" in body:
        source = (
            _bank_account_in_reach(request, entity, body.get("from_account"), "from_account")
            if current is None or "from_account" in body else current.from_account
        )
        target = (
            _bank_account_in_reach(request, entity, body.get("to_account"), "to_account")
            if current is None or "to_account" in body else current.to_account
        )
        fields.update(from_account=source, to_account=target,
                      branch_id=money_branch_id(entity, source, target, field="from_account"))
    if current is None or "amount" in body:
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValidationError({"amount": "Amount must be a whole number greater than zero."})
        fields["amount"] = amount
    if current is None or "narration" in body:
        narration = str(body.get("narration") or "").strip()
        if not narration:
            raise ValidationError({"narration": "Say why the money is moving."})
        fields["narration"] = narration[:255]
    if current is None or "transfer_date" in body:
        fields["transfer_date"] = _date(body.get("transfer_date"), "transfer_date", required=True)
    if current is None or "reference" in body:
        fields["reference"] = str(body.get("reference") or "")[:64]
    return fields


def _transfer_or_404(request, entity, pk):
    """A transfer of the caller's own branches, or 404 (see :func:`_transfers_in_reach`)."""
    transfer = _transfers_in_reach(request, entity).filter(pk=pk).first()
    if transfer is None:
        raise NotFound("Transfer not found for this entity.")
    return transfer


class BankTransferListCreateView(_FinanceBase):
    """GET/POST /finance/bank-transfers/?entity= - move money between own accounts.

    POST body: ``from_account`` and ``to_account`` (id or name), ``amount`` (kobo),
    ``transfer_date``, ``narration`` and an optional ``reference``. Both accounts must
    be of the caller's own branches (404 otherwise) and belong to the same branch;
    accounts of two branches are refused, because that is an inter-branch transfer.
    The transfer carries the accounts' branch (:func:`vs_finance.banking.money_branch_id`),
    and accounts not yet given a branch at a school with several move nothing (400).

    Approval follows the ``finance.bank_transfer`` route as a bank transaction follows
    its own: steps hold it for approval, an empty route needs
    ``confirm_without_approval``, and no route posts it at once.

    docstring-name: Transfers between own accounts
    """

    @property
    def rbac_permission(self):
        return "finance.banktransfer.create" if self.request.method == "POST" \
            else "finance.banktransfer.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _transfers_in_reach(request, entity)
        if (bank := request.query_params.get("bank_account")) and str(bank).isdigit():
            from django.db.models import Q

            qs = qs.filter(Q(from_account_id=int(bank)) | Q(to_account_id=int(bank)))
        qs = filter_by_stored_status(qs, request.query_params.get("status"))
        qs = filter_by_approval_param(qs, request.query_params)
        return self.paginate(request, qs.order_by("-transfer_date", "-id"), BankTransferSerializer)

    def post(self, request):
        from ..banking import post_bank_transfer, validate_bank_transfer
        from ..exceptions import PostingError

        entity = resolve_entity(request)
        transfer = BankTransfer(
            entity=entity, created_by=request.user,
            **_transfer_fields(request, entity, request.data or {}),
        )
        try:
            validate_bank_transfer(transfer)
        except PostingError as exc:
            raise ValidationError({"to_account": exc.message})
        with transaction.atomic():
            transfer.save()
            return _send_or_post(
                transfer, request, label="Transfer", noun="transfer",
                serializer=BankTransferSerializer, post=post_bank_transfer, status=201,
            )


class BankTransferDetailView(_FinanceBase):
    """GET/PATCH /finance/bank-transfers/<id>/?entity= - one transfer.

    PATCH corrects a draft that has come back from approval, as a bank
    transaction's does (:class:`BankTransactionDetailView`): the create fields,
    each checked as on create, both accounts still of one branch.

    docstring-name: Transfers between own accounts
    """

    @property
    def rbac_permission(self):
        return "finance.banktransfer.create" if self.request.method == "PATCH" \
            else "finance.banktransfer.view"

    def get(self, request, pk):
        transfer = _transfer_or_404(request, resolve_entity(request), pk)
        return success_response(
            "Transfer retrieved.",
            data=with_approval_request(BankTransferSerializer(transfer).data, transfer))

    def patch(self, request, pk):
        from ..banking import revise_bank_document

        entity = resolve_entity(request)
        transfer = _transfer_or_404(request, entity, pk)
        fields = _transfer_fields(request, entity, request.data or {}, current=transfer)
        transfer = revise_bank_document(transfer, fields, actor_user=request.user)
        transfer = _transfer_or_404(request, entity, pk)
        return success_response(
            f"Transfer {transfer.document_number} corrected.",
            data=with_approval_request(BankTransferSerializer(transfer).data, transfer),
        )


class BankTransferSubmitView(_FinanceBase):
    """POST /finance/bank-transfers/<id>/submit/?entity= - send a corrected draft again.

    Through the ``finance.bank_transfer`` route, as
    :class:`BankTransactionSubmitView` sends a bank transaction.

    docstring-name: Send a transfer between own accounts for approval again
    """

    rbac_permission = "finance.banktransfer.create"

    def post(self, request, pk):
        from ..banking import post_bank_transfer, require_reworkable

        entity = resolve_entity(request)
        _transfer_or_404(request, entity, pk)
        with transaction.atomic():
            transfer = BankTransfer.objects.select_for_update().get(pk=pk)
            require_reworkable(transfer)
            return _send_or_post(
                transfer, request, label="Transfer", noun="transfer",
                serializer=BankTransferSerializer, post=post_bank_transfer,
            )


class BankTransferCancelView(_FinanceBase):
    """POST /finance/bank-transfers/<id>/cancel/?entity= - cancel a draft that will not be sent.

    As :class:`BankTransactionCancelView` cancels a bank transaction: audited,
    nothing posted.

    docstring-name: Cancel a transfer between own accounts
    """

    rbac_permission = "finance.banktransfer.create"

    def post(self, request, pk):
        from ..banking import cancel_bank_document

        entity = resolve_entity(request)
        transfer = cancel_bank_document(_transfer_or_404(request, entity, pk),
                                        actor_user=request.user)
        return success_response(
            f"Transfer {transfer.document_number} cancelled.",
            data=BankTransferSerializer(_transfer_or_404(request, entity, pk)).data,
        )


class BankTransferVoidView(_FinanceBase):
    """POST /finance/bank-transfers/<id>/void/?entity= - reverse a posted transfer.

    Refused (422) while either side is matched to a bank statement line. Optional body
    ``date`` dates the reversal.

    docstring-name: Void a transfer between own accounts
    """

    rbac_permission = "finance.banktransfer.reverse"

    def post(self, request, pk):
        from ..banking import void_bank_transfer

        entity = resolve_entity(request)
        transfer = _transfer_or_404(request, entity, pk)
        void_bank_transfer(
            transfer, actor_user=request.user,
            date=_date((request.data or {}).get("date"), "date"),
        )
        transfer.refresh_from_db()
        return success_response(
            f"Transfer {transfer.document_number} voided.",
            data=BankTransferSerializer(transfer).data,
        )
