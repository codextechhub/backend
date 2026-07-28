"""Bank accounts, statement import, reconciliation.
"""
from __future__ import annotations

import hashlib

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from ..audit import record
from ..constants import BankLineStatus, DocumentStatus, FinanceAuditAction
from ..views import resolve_entity
from ..models import (
    BankAccount,
    BankStatement,
    BankStatementImportContext,
    BankStatementLine,
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
    _bool,
    _date,
    _int,
    _require_lines,
    _resolve_account,
    _resolve_currency,
    _signed_money,
)

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
        qs = BankAccount.objects.filter(entity=entity).select_related("gl_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Bank accounts retrieved.", data=BankAccountSerializer(qs, many=True).data,
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
        gl_account = _resolve_account(entity, body.get("gl_account"), "gl_account", required=True)
        is_primary = _bool(body.get("is_primary", False), default=False)
        is_primary_collection = _bool(body.get("is_primary_collection", False), default=False)
        currency = _resolve_currency(body.get("currency"))
        with transaction.atomic():
            if is_primary_collection:
                BankAccount.objects.filter(entity=entity, is_primary_collection=True).update(
                    is_primary_collection=False)
            bank = BankAccount.objects.create(
                entity=entity, name=name,
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
            data=BankAccountSerializer(bank).data, status=201,
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
        bank = (BankAccount.objects.filter(entity=resolve_entity(request), pk=pk)
                .select_related("gl_account").first())
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
        return bank

    # Support the transactions workflow.
    def _transactions(self, bank, *, book_balance, limit=50):
        """Recent posted GL cash lines, newest first, with a running balance."""
        lines = list(
            JournalLine.objects
            .filter(account=bank.gl_account, entry__status=DocumentStatus.POSTED)
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
                "description": ln.description or ln.entry.narration or "—",
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
        data = BankAccountSerializer(bank).data
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
            bank.statements.annotate(
                has_acted_lines=Exists(acted_lines),
                is_bulk_import=Exists(bulk_context),
            )[:50],
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
            if make_primary_collection:
                with transaction.atomic():
                    BankAccount.objects.filter(
                        entity=bank.entity, is_primary_collection=True).exclude(
                        pk=bank.pk).update(is_primary_collection=False)
                    bank.save()
            else:
                bank.save()
        else:
            bank.save()
        return success_response(
            f"Bank account '{bank.name}' updated.", data=BankAccountSerializer(bank).data)


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
        bank = BankAccount.objects.filter(entity=resolve_entity(request), pk=pk).first()
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
        return bank

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
        message = f"Imported {len(created)} statement line(s)."
        if suspected:
            message += (f" {len(suspected)} suspected duplicate(s) held back — "
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
                .filter(pk=pk, bank_account__entity=entity)
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

        bank = BankAccount.objects.filter(
            entity=resolve_entity(request),
            pk=pk,
            is_active=True,
        ).first()
        if bank is None:
            raise NotFound("Active bank account not found for this entity.")
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
        bank = BankAccount.objects.filter(entity=entity, pk=pk, is_active=True).first()
        if bank is None:
            raise NotFound("Active bank account not found for this entity.")

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
            context={"request": request},
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
    """POST — auto-match unmatched statement lines to posted cash journal lines.

    docstring-name: Auto-reconcile a bank statement
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import auto_reconcile

        entity = resolve_entity(request)
        bank = BankAccount.objects.filter(entity=entity, pk=pk).first()
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
        body = request.data or {}
        tolerance = _int(body.get("tolerance_days", 4), "tolerance_days", minimum=0) or 4
        group = _bool(body.get("group", True), default=True)
        matched = auto_reconcile(
            bank, tolerance_days=tolerance, group=group, actor_user=request.user)
        return success_response(
            f"Auto-matched {len(matched)} statement line(s).",
            data=BankStatementLineSerializer(matched, many=True).data,
        )


# Group endpoint behavior for Bank Book Lines View.
class BankBookLinesView(_FinanceBase):
    """GET — posted cash-account journal lines not yet matched to a statement line.

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
        bank = BankAccount.objects.filter(entity=entity, pk=pk).select_related("gl_account").first()
        if bank is None:
            raise NotFound("Bank account not found for this entity.")

        # Posting bakes the customer *code* into line descriptions ("Receipt: CUST-002").
        # Resolve it to the human name for the reconciliation view.
        names = dict(Customer.objects.filter(entity=entity).values_list("code", "name"))

        # Handle the humanize workflow.
        def humanize(desc: str) -> str:
            label, sep, tail = (desc or "").partition(": ")
            return f"{label}: {names[tail]}" if sep and tail in names else (desc or "—")

        # Paginate the unmatched book lines (was capped at [:200]); build rows per page.
        paginator = XVSPagination()
        page = paginator.paginate_queryset(_unmatched_gl_lines(bank), request, view=self)
        rows = [{
            "id": ln.id,
            "date": ln.entry.date,
            "description": humanize(ln.description or ln.entry.narration or "—"),
            "reference": ln.entry.document_number or ln.entry.reference or "",
            "amount": int((ln.debit or 0) - (ln.credit or 0)),
        } for ln in page]
        return paginator.get_paginated_response(rows)


# Group endpoint behavior for Bank Reconcile Complete View.
class BankReconcileCompleteView(_FinanceBase):
    """POST — finalise the reconciliation, recording a snapshot of the current state.

    docstring-name: Complete a bank reconciliation
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import complete_reconciliation

        entity = resolve_entity(request)
        bank = BankAccount.objects.filter(entity=entity, pk=pk).select_related("gl_account").first()
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
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
            .filter(pk=pk, bank_account__entity=entity)
            .select_related("bank_account").first()
        )
        if line is None:
            raise NotFound("Statement line not found for this entity.")
        return entity, line


# Group endpoint behavior for Bank Statement Line Match View.
class BankStatementLineMatchView(_StatementLineActionBase):
    """POST {journal_line} — manually pair a statement line to a cash journal line.

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
    """POST {journal_lines:[ids]} — match a statement line to several cash journal lines
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
    """POST {journal_line, statement_lines:[ids]} — match one cash journal line to
    several statement lines that sum to it (one ledger movement the bank split).

    docstring-name: Split-match a cash journal line
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import split_match

        entity = resolve_entity(request)
        bank = BankAccount.objects.filter(entity=entity, pk=pk).select_related("gl_account").first()
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
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
    """POST {counter_account?, counter_code?, narration?} — book + match an unrecorded line.

    docstring-name: Post an adjustment from a statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..banking import post_bank_adjustment

        entity, line = self._line(request, pk)
        body = request.data or {}
        counter = _resolve_account(entity, body.get("counter_account"), "counter_account")
        post_bank_adjustment(
            line, counter_account=counter,
            counter_code=body.get("counter_code"),
            narration=body.get("narration", ""), actor_user=request.user,
        )
        line.refresh_from_db()
        return success_response(
            "Bank adjustment booked and line matched.",
            data=BankStatementLineSerializer(line).data, status=201,
        )


# Group endpoint behavior for Bank Statement Line Unmatch View.
class BankStatementLineUnmatchView(_StatementLineActionBase):
    """POST — undo a match (reverses the adjusting journal if the match created one).

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
    """POST {ignored?: true, reason?} — mark an unmatched line IGNORED (a known
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
