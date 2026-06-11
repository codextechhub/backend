"""Bank accounts, statement import, reconciliation.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from ..views import resolve_entity
from ..models import (
    BankAccount,
    BankStatementLine,
    JournalLine,
)
from ..serializers import (
    BankAccountSerializer,
    BankStatementLineSerializer,
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
    """GET (list) / POST (create) bank accounts for an entity."""

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
    """GET one bank account."""

    rbac_permission = "finance.bankaccount.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        bank = BankAccount.objects.filter(entity=entity, pk=pk).select_related("gl_account").first()
        if bank is None:
            raise NotFound("Bank account not found for this entity.")
        return success_response(
            "Bank account retrieved.", data=BankAccountSerializer(bank).data,
        )


class BankStatementLineView(_FinanceBase):
    """GET (list) statement lines / POST import a batch of statement lines."""

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
        created = import_statement_lines(bank, parsed, actor_user=request.user)
        return success_response(
            f"Imported {len(created)} statement line(s) "
            f"({len(rows) - len(created)} skipped as duplicates).",
            data=BankStatementLineSerializer(created, many=True).data, status=201,
        )


class BankAutoReconcileView(_FinanceBase):
    """POST — auto-match unmatched statement lines to posted cash journal lines."""

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
    """POST {journal_line} — manually pair a statement line to a cash journal line."""

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
    """POST {counter_account?, counter_code?, narration?} — book + match an unrecorded line."""

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


