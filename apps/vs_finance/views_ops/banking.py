"""Bank accounts, statement import, reconciliation.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from ..constants import BankLineStatus, DocumentStatus
from ..views import resolve_entity
from ..models import (
    BankAccount,
    BankStatementLine,
    JournalLine,
)
from ..serializers import (
    BankAccountSerializer,
    BankReconciliationSerializer,
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

class BankAccountListCreateView(_FinanceBase):
    """GET (list) / POST (create) bank accounts for an entity.

    docstring-name: Bank accounts
    """

    @property
    def rbac_permission(self):
        return "finance.bankaccount.create" if self.request.method == "POST" \
            else "finance.bankaccount.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = BankAccount.objects.filter(entity=entity).select_related("gl_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Bank accounts retrieved.", data=BankAccountSerializer(qs, many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A bank account name is required."})
        gl_account = _resolve_account(entity, body.get("gl_account"), "gl_account", required=True)
        bank = BankAccount.objects.create(
            entity=entity, name=name,
            bank_name=body.get("bank_name", ""),
            account_number=body.get("account_number", ""),
            gl_account=gl_account,
            currency=_resolve_currency(body.get("currency")),
            is_active=_bool(body.get("is_active", True), default=True),
        )
        return success_response(
            f"Bank account '{name}' created.",
            data=BankAccountSerializer(bank).data, status=201,
        )


class BankAccountDetailView(_FinanceBase):
    """GET one bank account (with metrics, transactions, statements, reconciliations)
    or PATCH its settings (name, bank, number, currency, active, primary).

    docstring-name: Bank accounts
    """

    @property
    def rbac_permission(self):
        return "finance.bankaccount.update" if self.request.method == "PATCH" \
            else "finance.bankaccount.view"

    def _bank(self, request, pk):
        bank = (BankAccount.objects.filter(entity=resolve_entity(request), pk=pk)
                .select_related("gl_account").first())
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
        return bank

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
        data["statements"] = BankStatementSerializer(
            bank.statements.all()[:50], many=True).data
        data["reconciliations"] = BankReconciliationSerializer(
            bank.reconciliations.all()[:50], many=True).data
        return success_response("Bank account retrieved.", data=data)

    def patch(self, request, pk):
        bank = self._bank(request, pk)
        body = request.data or {}
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
        bank.save()
        return success_response(
            f"Bank account '{bank.name}' updated.", data=BankAccountSerializer(bank).data)


class BankStatementLineView(_FinanceBase):
    """GET (list) statement lines / POST import a batch of statement lines.

    docstring-name: Bank statement lines
    """

    @property
    def rbac_permission(self):
        return "finance.bankaccount.import" if self.request.method == "POST" \
            else "finance.bankaccount.view"

    def _bank(self, request, pk):
        bank = BankAccount.objects.filter(entity=resolve_entity(request), pk=pk).first()
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
        return bank

    def get(self, request, pk):
        bank = self._bank(request, pk)
        qs = BankStatementLine.objects.filter(bank_account=bank)
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        return success_response(
            "Statement lines retrieved.",
            data=BankStatementLineSerializer(qs[:500], many=True).data,
        )

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
        _, created = import_statement_lines(
            bank, parsed, actor_user=request.user,
            statement_date=_date(body.get("statement_date"), "statement_date"),
            period_label=str(body.get("period_label", "")).strip(),
            opening_balance=_signed_money(body.get("opening_balance", 0), "opening_balance"),
            closing_balance=(_signed_money(body.get("closing_balance"), "closing_balance")
                             if body.get("closing_balance") not in (None, "") else None),
        )
        return success_response(
            f"Imported {len(created)} statement line(s) "
            f"({len(rows) - len(created)} skipped as duplicates).",
            data=BankStatementLineSerializer(created, many=True).data, status=201,
        )


class BankAutoReconcileView(_FinanceBase):
    """POST — auto-match unmatched statement lines to posted cash journal lines.

    docstring-name: Auto-reconcile a bank statement
    """

    rbac_permission = "finance.bankaccount.reconcile"

    def post(self, request, pk):
        from ..banking import auto_reconcile

        entity = resolve_entity(request)
        bank = BankAccount.objects.filter(entity=entity, pk=pk).first()
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
        body = request.data or {}
        tolerance = _int(body.get("tolerance_days", 4), "tolerance_days", minimum=0) or 4
        matched = auto_reconcile(bank, tolerance_days=tolerance, actor_user=request.user)
        return success_response(
            f"Auto-matched {len(matched)} statement line(s).",
            data=BankStatementLineSerializer(matched, many=True).data,
        )


class _StatementLineActionBase(_FinanceBase):
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


class BankStatementLineMatchView(_StatementLineActionBase):
    """POST {journal_line} — manually pair a statement line to a cash journal line.

    docstring-name: Match a bank statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"

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


class BankStatementLineAdjustView(_StatementLineActionBase):
    """POST {counter_account?, counter_code?, narration?} — book + match an unrecorded line.

    docstring-name: Post an adjustment from a statement line
    """

    rbac_permission = "finance.bankaccount.reconcile"

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


