"""REST API for vs_finance operational + setup capabilities (mounted at ``/v1/finance/``).

These are the modules whose models and services shipped in Phase 4 but had no HTTP
envelope yet: reference/setup data (currencies, FX rates, tax codes, cost centres,
dimensions), banking + reconciliation, expense claims, payroll, budgets, fixed assets
and the finance audit trail. The endpoints follow the exact same conventions as the
core finance/procurement surface:

* entity-scoped via ``?entity=<id|code>`` (currencies and FX rates are **global**
  reference data and are the only exceptions — a naira is a naira in any book);
* the platform ``{success, message, data}`` envelope (:func:`core.response.success_response`);
* RBAC-gated (``finance.<resource>.<action>``);
* thin views that resolve accounts / tax codes / etc. by **code or id**, build the
  documents and hand off to the existing **services** (``banking``, ``expenses``,
  ``payroll``, ``budgets``, ``assets``) which own every journal posting.

Money is integer **kobo** throughout; never a float. Bank-statement amounts are *signed*
kobo (``+`` inflow / ``-`` outflow), so they use :func:`_signed_money` rather than the
non-negative :func:`_money`.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from .money import format_naira
from .views import resolve_entity
from .models import (
    Account,
    BankAccount,
    BankStatementLine,
    Budget,
    CostCenter,
    Currency,
    Dimension,
    ExpenseClaim,
    ExpenseClaimLine,
    FinanceAuditLog,
    FiscalYear,
    FixedAsset,
    FxRate,
    JournalLine,
    PayrollLine,
    PayrollRun,
    PettyCashFund,
    PettyCashVoucher,
    PettyCashVoucherLine,
    TaxCode,
    TaxFiling,
    TaxObligation,
)
from .serializers import (
    BankAccountSerializer,
    BankStatementLineSerializer,
    BudgetSerializer,
    CostCenterSerializer,
    CurrencySerializer,
    DimensionSerializer,
    ExpenseClaimSerializer,
    FinanceAuditLogSerializer,
    FixedAssetSerializer,
    FxRateSerializer,
    PayrollRunSerializer,
    PettyCashFundSerializer,
    PettyCashVoucherSerializer,
    TaxCodeSerializer,
    TaxFilingSerializer,
    TaxObligationSerializer,
)


# --------------------------------------------------------------------------- #
# Shared resolution + coercion helpers (mirror the procurement conventions)   #
# --------------------------------------------------------------------------- #

def _resolve_account(entity, ref, field, *, required=False):
    """Resolve a GL account by **code** (e.g. "1100") or id within ``entity``.

    Codes are numeric strings, so match on code first, then fall back to a pk lookup.
    Returns ``None`` for a blank ``ref`` unless ``required``.
    """
    if ref in (None, ""):
        if required:
            raise ValidationError({field: "An account (code or id) is required."})
        return None
    qs = Account.objects.filter(entity=entity)
    acc = qs.filter(code=str(ref)).first()
    if acc is None and str(ref).isdigit():
        acc = qs.filter(pk=int(ref)).first()
    if acc is None:
        raise ValidationError({field: f"No account '{ref}' in this entity."})
    return acc


def _resolve_tax(entity, ref, field="tax_code"):
    if ref in (None, ""):
        return None
    qs = TaxCode.objects.filter(entity=entity)
    tc = qs.filter(code=str(ref)).first()
    if tc is None and str(ref).isdigit():
        tc = qs.filter(pk=int(ref)).first()
    if tc is None:
        raise ValidationError({field: f"No tax code '{ref}' in this entity."})
    return tc


def _resolve_cost_center(entity, ref, field="cost_center"):
    if ref in (None, ""):
        return None
    qs = CostCenter.objects.filter(entity=entity)
    cc = qs.filter(code=str(ref)).first()
    if cc is None and str(ref).isdigit():
        cc = qs.filter(pk=int(ref)).first()
    if cc is None:
        raise ValidationError({field: f"No cost centre '{ref}' in this entity."})
    return cc


def _resolve_currency(ref, field="currency"):
    if ref in (None, ""):
        return None
    cur = Currency.objects.filter(code=str(ref).upper()).first()
    if cur is None:
        raise ValidationError({field: f"No currency '{ref}'."})
    return cur


def _resolve_bank_account(entity, ref, field="bank_account", *, required=True):
    """Resolve a bank account by id or name within ``entity``."""
    if ref in (None, ""):
        if required:
            raise ValidationError({field: "A bank account (id or name) is required."})
        return None
    qs = BankAccount.objects.filter(entity=entity)
    ba = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(name=str(ref)).first()
    )
    if ba is None:
        raise ValidationError({field: f"No bank account '{ref}' in this entity."})
    return ba


def _resolve_fiscal_year(entity, ref, field="fiscal_year"):
    """Resolve a fiscal year by its ``year`` label (preferred) or id within ``entity``."""
    if ref in (None, ""):
        raise ValidationError({field: "A fiscal_year (year or id) is required."})
    qs = FiscalYear.objects.filter(entity=entity)
    fy = qs.filter(year=int(ref)).first() if str(ref).isdigit() else None
    if fy is None and str(ref).isdigit():
        fy = qs.filter(pk=int(ref)).first()
    if fy is None:
        raise ValidationError({field: f"No fiscal year '{ref}' in this entity."})
    return fy


def _date(value, field, *, required=False):
    if value in (None, ""):
        if required:
            raise ValidationError({field: "An ISO date (YYYY-MM-DD) is required."})
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError({field: "Expected an ISO date (YYYY-MM-DD)."})


def _dec(value, field):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({field: "Expected a number."})


def _money(value, field):
    """Coerce to non-negative integer kobo, rejecting floats-as-naira mistakes."""
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected an integer amount in kobo."})
    if amount < 0:
        raise ValidationError({field: "Amount cannot be negative."})
    return amount


def _signed_money(value, field):
    """Coerce to a *signed* integer kobo (bank lines can be negative outflows)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected an integer amount in kobo (may be negative)."})


def _require_lines(body):
    lines = body.get("lines")
    if not lines or not isinstance(lines, list):
        raise ValidationError({"lines": "At least one line is required."})
    return lines


def _int(value, field, *, required=False, minimum=None):
    if value in (None, ""):
        if required:
            raise ValidationError({field: "An integer is required."})
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected an integer."})
    if minimum is not None and out < minimum:
        raise ValidationError({field: f"Must be ≥ {minimum}."})
    return out


def _bool(value, default=False):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


class _FinanceBase(APIView):
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]


# --------------------------------------------------------------------------- #
# Setup / reference data                                                      #
# --------------------------------------------------------------------------- #

class CurrencyListCreateView(_FinanceBase):
    """GET (list) / POST (create) currencies — **global** reference data (no entity)."""

    @property
    def rbac_permission(self):
        return "finance.currency.create" if self.request.method == "POST" \
            else "finance.currency.view"

    def get(self, request):
        qs = Currency.objects.all().order_by("code")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Currencies retrieved.", data=CurrencySerializer(qs, many=True).data,
        )

    def post(self, request):
        body = request.data or {}
        code = str(body.get("code", "")).upper().strip()
        if not code:
            raise ValidationError({"code": "A 3-letter ISO currency code is required."})
        currency, created = Currency.objects.update_or_create(
            code=code,
            defaults={
                "name": body.get("name", code),
                "symbol": body.get("symbol", ""),
                "minor_unit": _int(body.get("minor_unit", 2), "minor_unit", minimum=0),
                "is_active": _bool(body.get("is_active", True), default=True),
            },
        )
        return success_response(
            f"Currency {code} {'created' if created else 'updated'}.",
            data=CurrencySerializer(currency).data, status=201 if created else 200,
        )


class FxRateListCreateView(_FinanceBase):
    """GET (list) / POST (create) FX rates — **global** reference data (no entity)."""

    @property
    def rbac_permission(self):
        return "finance.fxrate.create" if self.request.method == "POST" \
            else "finance.fxrate.view"

    def get(self, request):
        qs = FxRate.objects.select_related("base", "quote").all()
        if (base := request.query_params.get("base")):
            qs = qs.filter(base_id=base.upper())
        if (quote := request.query_params.get("quote")):
            qs = qs.filter(quote_id=quote.upper())
        return success_response(
            "FX rates retrieved.", data=FxRateSerializer(qs[:500], many=True).data,
        )

    def post(self, request):
        body = request.data or {}
        base = _resolve_currency(body.get("base"), "base")
        quote = _resolve_currency(body.get("quote"), "quote")
        if base is None or quote is None:
            raise ValidationError({"base": "Both base and quote currencies are required."})
        rate = _dec(body.get("rate"), "rate")
        if rate <= 0:
            raise ValidationError({"rate": "Rate must be positive."})
        fx, created = FxRate.objects.update_or_create(
            base=base, quote=quote,
            as_of=_date(body.get("as_of"), "as_of", required=True),
            source=body.get("source", ""),
            defaults={"rate": rate},
        )
        return success_response(
            f"FX rate {base.code}/{quote.code} recorded.",
            data=FxRateSerializer(fx).data, status=201 if created else 200,
        )


class TaxCodeListCreateView(_FinanceBase):
    """GET (list) / POST (create) tax codes for an entity."""

    @property
    def rbac_permission(self):
        return "finance.taxcode.create" if self.request.method == "POST" \
            else "finance.taxcode.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = TaxCode.objects.filter(entity=entity).select_related(
            "collected_account", "paid_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Tax codes retrieved.", data=TaxCodeSerializer(qs, many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip()
        if not code:
            raise ValidationError({"code": "A tax code is required."})
        tax, created = TaxCode.objects.update_or_create(
            entity=entity, code=code,
            defaults={
                "name": body.get("name", code),
                "rate_bps": _int(body.get("rate_bps", 0), "rate_bps", minimum=0),
                "is_recoverable": _bool(body.get("is_recoverable", True), default=True),
                "collected_account": _resolve_account(
                    entity, body.get("collected_account"), "collected_account"),
                "paid_account": _resolve_account(
                    entity, body.get("paid_account"), "paid_account"),
                "is_active": _bool(body.get("is_active", True), default=True),
            },
        )
        return success_response(
            f"Tax code {code} {'created' if created else 'updated'}.",
            data=TaxCodeSerializer(tax).data, status=201 if created else 200,
        )


class CostCenterListCreateView(_FinanceBase):
    """GET (list) / POST (create) cost centres for an entity."""

    @property
    def rbac_permission(self):
        return "finance.costcenter.create" if self.request.method == "POST" \
            else "finance.costcenter.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = CostCenter.objects.filter(entity=entity).select_related("parent")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Cost centres retrieved.", data=CostCenterSerializer(qs, many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip()
        if not code:
            raise ValidationError({"code": "A cost centre code is required."})
        parent = None
        if body.get("parent"):
            parent = _resolve_cost_center(entity, body.get("parent"), "parent")
        cc, created = CostCenter.objects.update_or_create(
            entity=entity, code=code,
            defaults={
                "name": body.get("name", code),
                "parent": parent,
                "is_active": _bool(body.get("is_active", True), default=True),
            },
        )
        return success_response(
            f"Cost centre {code} {'created' if created else 'updated'}.",
            data=CostCenterSerializer(cc).data, status=201 if created else 200,
        )


class DimensionListCreateView(_FinanceBase):
    """GET (list) / POST (create) analytical dimensions for an entity."""

    @property
    def rbac_permission(self):
        return "finance.dimension.create" if self.request.method == "POST" \
            else "finance.dimension.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = Dimension.objects.filter(entity=entity)
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Dimensions retrieved.", data=DimensionSerializer(qs, many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip()
        if not code:
            raise ValidationError({"code": "A dimension code is required."})
        dim, created = Dimension.objects.update_or_create(
            entity=entity, code=code,
            defaults={
                "name": body.get("name", code),
                "is_active": _bool(body.get("is_active", True), default=True),
            },
        )
        return success_response(
            f"Dimension {code} {'created' if created else 'updated'}.",
            data=DimensionSerializer(dim).data, status=201 if created else 200,
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
        from .banking import import_statement_lines

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
        from .banking import auto_reconcile

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
        from .banking import match_line

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
        from .banking import post_bank_adjustment

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


# --------------------------------------------------------------------------- #
# Expense claims                                                              #
# --------------------------------------------------------------------------- #

class ExpenseClaimListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) expense claims for an entity."""

    @property
    def rbac_permission(self):
        return "finance.expenseclaim.create" if self.request.method == "POST" \
            else "finance.expenseclaim.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = ExpenseClaim.objects.filter(entity=entity).prefetch_related("lines")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (pay := request.query_params.get("payment_status")):
            qs = qs.filter(payment_status=pay)
        return success_response(
            "Expense claims retrieved.",
            data=ExpenseClaimSerializer(qs.order_by("-claim_date", "-id")[:200], many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from .expenses import price_expense_claim

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        claim = ExpenseClaim.objects.create(
            entity=entity,
            claimant_name=body.get("claimant_name", ""),
            claim_date=_date(body.get("claim_date"), "claim_date", required=True),
            title=body.get("title", ""),
            narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")),
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            ExpenseClaimLine.objects.create(
                claim=claim, line_no=i,
                description=ln.get("description", ""),
                expense_account=_resolve_account(
                    entity, ln.get("expense_account"),
                    f"lines[{i}].expense_account", required=True),
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),
                tax_code=_resolve_tax(entity, ln.get("tax_code"), f"lines[{i}].tax_code"),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        price_expense_claim(claim)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} created.",
            data=ExpenseClaimSerializer(claim).data, status=201,
        )


class _ExpenseClaimActionBase(_FinanceBase):
    def _claim(self, request, pk):
        entity = resolve_entity(request)
        claim = ExpenseClaim.objects.filter(entity=entity, pk=pk).first()
        if claim is None:
            raise NotFound("Expense claim not found for this entity.")
        return entity, claim


class ExpenseClaimDetailView(_ExpenseClaimActionBase):
    rbac_permission = "finance.expenseclaim.view"

    def get(self, request, pk):
        _, claim = self._claim(request, pk)
        return success_response(
            "Expense claim retrieved.", data=ExpenseClaimSerializer(claim).data,
        )


class ExpenseClaimPostView(_ExpenseClaimActionBase):
    rbac_permission = "finance.expenseclaim.post"

    def post(self, request, pk):
        from .expenses import post_expense_claim

        _, claim = self._claim(request, pk)
        post_expense_claim(claim, actor_user=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} posted.",
            data=ExpenseClaimSerializer(claim).data,
        )


class ExpenseClaimSettleView(_ExpenseClaimActionBase):
    rbac_permission = "finance.expenseclaim.settle"

    def post(self, request, pk):
        from .expenses import settle_expense_claim

        entity, claim = self._claim(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"))
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None
        settle_expense_claim(
            claim, bank_account=bank,
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            amount=amount, actor_user=request.user,
        )
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} reimbursed.",
            data=ExpenseClaimSerializer(claim).data,
        )


# --------------------------------------------------------------------------- #
# Petty cash                                                                  #
# --------------------------------------------------------------------------- #

def _resolve_user(ref, field):
    """Resolve a platform user by id (or return None for a blank ref)."""
    if ref in (None, ""):
        return None
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=ref).first()
    if user is None:
        raise ValidationError({field: f"No user '{ref}'."})
    return user


class PettyCashFundListCreateView(_FinanceBase):
    """GET (list) / POST (create) petty-cash funds for an entity."""

    @property
    def rbac_permission(self):
        return "finance.pettycash.manage" if self.request.method == "POST" \
            else "finance.pettycash.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PettyCashFund.objects.filter(entity=entity).select_related("gl_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Petty cash funds retrieved.",
            data=PettyCashFundSerializer(qs.order_by("name"), many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        if not body.get("name"):
            raise ValidationError({"name": "A fund name is required."})
        fund = PettyCashFund.objects.create(
            entity=entity, name=body["name"],
            gl_account=_resolve_account(entity, body.get("gl_account"), "gl_account", required=True),
            custodian=_resolve_user(body.get("custodian"), "custodian"),
            custodian_name=body.get("custodian_name", ""),
            float_amount=_money(body.get("float_amount", 0), "float_amount"),
            currency=_resolve_currency(body.get("currency")),
            is_active=_bool(body.get("is_active", True), default=True),
        )
        return success_response(
            "Petty cash fund created.",
            data=PettyCashFundSerializer(fund).data, status=201,
        )


class _PettyCashFundActionBase(_FinanceBase):
    def _fund(self, request, pk):
        entity = resolve_entity(request)
        fund = PettyCashFund.objects.filter(entity=entity, pk=pk).first()
        if fund is None:
            raise NotFound("Petty cash fund not found for this entity.")
        return entity, fund


class PettyCashFundDetailView(_PettyCashFundActionBase):
    @property
    def rbac_permission(self):
        return "finance.pettycash.manage" if self.request.method == "PATCH" \
            else "finance.pettycash.view"

    def get(self, request, pk):
        _, fund = self._fund(request, pk)
        return success_response(
            "Petty cash fund retrieved.", data=PettyCashFundSerializer(fund).data,
        )

    def patch(self, request, pk):
        entity, fund = self._fund(request, pk)
        body = request.data or {}
        if "name" in body:
            fund.name = body["name"]
        if "custodian" in body:
            fund.custodian = _resolve_user(body.get("custodian"), "custodian")
        if "custodian_name" in body:
            fund.custodian_name = body["custodian_name"]
        if "float_amount" in body:
            fund.float_amount = _money(body.get("float_amount", 0), "float_amount")
        if "is_active" in body:
            fund.is_active = _bool(body["is_active"])
        fund.save()
        return success_response(
            "Petty cash fund updated.", data=PettyCashFundSerializer(fund).data,
        )


class PettyCashFundEstablishView(_PettyCashFundActionBase):
    """POST — move cash from a bank account into the tin (Dr petty cash, Cr bank)."""

    rbac_permission = "finance.pettycash.replenish"

    def post(self, request, pk):
        from .petty_cash import establish_fund

        entity, fund = self._fund(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"))
        establish_fund(
            fund, bank_account=bank,
            amount=_money(body.get("amount"), "amount"),
            date=_date(body.get("date"), "date", required=True),
            actor_user=request.user,
        )
        fund.refresh_from_db()
        return success_response(
            f"Established cash into petty cash '{fund.name}'.",
            data=PettyCashFundSerializer(fund).data,
        )


class PettyCashFundReplenishView(_PettyCashFundActionBase):
    """POST — top the tin back up to its float (Dr petty cash, Cr bank)."""

    rbac_permission = "finance.pettycash.replenish"

    def post(self, request, pk):
        from .petty_cash import replenish_fund

        entity, fund = self._fund(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"))
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None
        replenish_fund(
            fund, bank_account=bank,
            date=_date(body.get("date"), "date", required=True),
            amount=amount, actor_user=request.user,
        )
        fund.refresh_from_db()
        return success_response(
            f"Replenished petty cash '{fund.name}'.",
            data=PettyCashFundSerializer(fund).data,
        )


class PettyCashStatusView(_FinanceBase):
    """GET — per-fund cash position + low-balance flags (replenishment alerts)."""

    rbac_permission = "finance.pettycash.view"

    def get(self, request):
        from .petty_cash import fund_status

        entity = resolve_entity(request)
        threshold = _int(
            request.query_params.get("threshold_bps", 2500), "threshold_bps", minimum=0,
        )
        rows = fund_status(entity, threshold_bps=threshold)
        return success_response(
            "Petty cash status retrieved.",
            data={
                "entity": entity.code,
                "rows": [
                    {
                        **r,
                        "float_amount": {"kobo": r["float_amount"], "naira": format_naira(r["float_amount"])},
                        "current_balance": {"kobo": r["current_balance"], "naira": format_naira(r["current_balance"])},
                        "shortfall": {"kobo": r["shortfall"], "naira": format_naira(r["shortfall"])},
                        "last_replenished_at": str(r["last_replenished_at"]) if r["last_replenished_at"] else None,
                    }
                    for r in rows
                ],
            },
        )


class PettyCashVoucherListCreateView(_FinanceBase):
    """GET (list) / POST (create draft + lines) petty-cash vouchers."""

    @property
    def rbac_permission(self):
        return "finance.pettycash.create" if self.request.method == "POST" \
            else "finance.pettycash.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PettyCashVoucher.objects.filter(entity=entity).prefetch_related("lines")
        if (fund := request.query_params.get("fund")):
            qs = qs.filter(fund_id=fund)
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        return success_response(
            "Petty cash vouchers retrieved.",
            data=PettyCashVoucherSerializer(
                qs.order_by("-voucher_date", "-id")[:200], many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from .petty_cash import price_voucher

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        fund_ref = body.get("fund")
        if fund_ref in (None, ""):
            raise ValidationError({"fund": "A petty cash fund is required."})
        fund = PettyCashFund.objects.filter(entity=entity, pk=fund_ref).first()
        if fund is None:
            raise ValidationError({"fund": f"No petty cash fund '{fund_ref}' in this entity."})
        voucher = PettyCashVoucher.objects.create(
            entity=entity, fund=fund,
            voucher_date=_date(body.get("voucher_date"), "voucher_date", required=True),
            payee=body.get("payee", ""),
            spent_by=_resolve_user(body.get("spent_by"), "spent_by"),
            narration=body.get("narration", ""),
            reference=body.get("reference", ""),
            currency=_resolve_currency(body.get("currency")) or fund.currency,
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            PettyCashVoucherLine.objects.create(
                voucher=voucher, line_no=i,
                description=ln.get("description", ""),
                expense_account=_resolve_account(
                    entity, ln.get("expense_account"),
                    f"lines[{i}].expense_account", required=True),
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),
                tax_code=_resolve_tax(entity, ln.get("tax_code"), f"lines[{i}].tax_code"),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        price_voucher(voucher)
        voucher.refresh_from_db()
        return success_response(
            f"Petty cash voucher {voucher.document_number} created.",
            data=PettyCashVoucherSerializer(voucher).data, status=201,
        )


class _PettyCashVoucherActionBase(_FinanceBase):
    def _voucher(self, request, pk):
        entity = resolve_entity(request)
        voucher = PettyCashVoucher.objects.filter(entity=entity, pk=pk).first()
        if voucher is None:
            raise NotFound("Petty cash voucher not found for this entity.")
        return entity, voucher


class PettyCashVoucherDetailView(_PettyCashVoucherActionBase):
    rbac_permission = "finance.pettycash.view"

    def get(self, request, pk):
        _, voucher = self._voucher(request, pk)
        return success_response(
            "Petty cash voucher retrieved.",
            data=PettyCashVoucherSerializer(voucher).data,
        )


class PettyCashVoucherPostView(_PettyCashVoucherActionBase):
    rbac_permission = "finance.pettycash.post"

    def post(self, request, pk):
        from .petty_cash import post_voucher

        _, voucher = self._voucher(request, pk)
        post_voucher(voucher, actor_user=request.user)
        voucher.refresh_from_db()
        return success_response(
            f"Petty cash voucher {voucher.document_number} posted.",
            data=PettyCashVoucherSerializer(voucher).data,
        )


# --------------------------------------------------------------------------- #
# Tax remittance / filing                                                     #
# --------------------------------------------------------------------------- #

class TaxObligationListCreateView(_FinanceBase):
    """GET (list) / POST (create) statutory tax obligations for an entity."""

    @property
    def rbac_permission(self):
        return "finance.tax.manage" if self.request.method == "POST" \
            else "finance.tax.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = TaxObligation.objects.filter(entity=entity).select_related(
            "liability_account", "recoverable_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        return success_response(
            "Tax obligations retrieved.",
            data=TaxObligationSerializer(qs.order_by("code"), many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        if not body.get("code"):
            raise ValidationError({"code": "An obligation code is required."})
        if not body.get("obligation_type"):
            raise ValidationError({"obligation_type": "An obligation type is required."})
        obligation = TaxObligation.objects.create(
            entity=entity, code=body["code"], name=body.get("name", body["code"]),
            obligation_type=body["obligation_type"],
            liability_account=_resolve_account(
                entity, body.get("liability_account"), "liability_account", required=True),
            recoverable_account=_resolve_account(
                entity, body.get("recoverable_account"), "recoverable_account"),
            authority_name=body.get("authority_name", ""),
            frequency=body.get("frequency", "MONTHLY"),
            filing_day=_int(body.get("filing_day", 21), "filing_day", minimum=1),
            is_active=_bool(body.get("is_active", True), default=True),
        )
        return success_response(
            "Tax obligation created.",
            data=TaxObligationSerializer(obligation).data, status=201,
        )


class TaxObligationDetailView(_FinanceBase):
    @property
    def rbac_permission(self):
        return "finance.tax.manage" if self.request.method == "PATCH" \
            else "finance.tax.view"

    def _obligation(self, request, pk):
        entity = resolve_entity(request)
        obligation = TaxObligation.objects.filter(entity=entity, pk=pk).first()
        if obligation is None:
            raise NotFound("Tax obligation not found for this entity.")
        return entity, obligation

    def get(self, request, pk):
        _, obligation = self._obligation(request, pk)
        return success_response(
            "Tax obligation retrieved.", data=TaxObligationSerializer(obligation).data,
        )

    def patch(self, request, pk):
        entity, obligation = self._obligation(request, pk)
        body = request.data or {}
        if "name" in body:
            obligation.name = body["name"]
        if "liability_account" in body:
            obligation.liability_account = _resolve_account(
                entity, body.get("liability_account"), "liability_account", required=True)
        if "recoverable_account" in body:
            obligation.recoverable_account = _resolve_account(
                entity, body.get("recoverable_account"), "recoverable_account")
        if "authority_name" in body:
            obligation.authority_name = body["authority_name"]
        if "frequency" in body:
            obligation.frequency = body["frequency"]
        if "filing_day" in body:
            obligation.filing_day = _int(body["filing_day"], "filing_day", minimum=1)
        if "is_active" in body:
            obligation.is_active = _bool(body["is_active"])
        obligation.save()
        return success_response(
            "Tax obligation updated.", data=TaxObligationSerializer(obligation).data,
        )


class TaxObligationOutstandingView(_FinanceBase):
    """GET — per-obligation unremitted balance sitting in each control account."""

    rbac_permission = "finance.tax.view"

    def get(self, request):
        from .tax_filing import outstanding_obligations

        entity = resolve_entity(request)
        rows = outstanding_obligations(entity)
        return success_response(
            "Outstanding tax obligations retrieved.",
            data={
                "entity": entity.code,
                "rows": [
                    {
                        **r,
                        "payable_balance": {"kobo": r["payable_balance"], "naira": format_naira(r["payable_balance"])},
                        "recoverable_balance": {"kobo": r["recoverable_balance"], "naira": format_naira(r["recoverable_balance"])},
                        "net_outstanding": {"kobo": r["net_outstanding"], "naira": format_naira(r["net_outstanding"])},
                    }
                    for r in rows
                ],
            },
        )


class TaxFilingListCreateView(_FinanceBase):
    """GET (list) / POST (prepare from GL) tax filings for an entity."""

    @property
    def rbac_permission(self):
        return "finance.tax.file" if self.request.method == "POST" \
            else "finance.tax.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = TaxFiling.objects.filter(entity=entity).select_related("obligation")
        if (ob := request.query_params.get("obligation")):
            qs = qs.filter(obligation_id=ob)
        if (status_val := request.query_params.get("filing_status")):
            qs = qs.filter(filing_status=status_val)
        return success_response(
            "Tax filings retrieved.",
            data=TaxFilingSerializer(
                qs.order_by("-period_end", "-id")[:200], many=True).data,
        )

    def post(self, request):
        from .tax_filing import prepare_filing

        entity = resolve_entity(request)
        body = request.data or {}
        ref = body.get("obligation")
        if ref in (None, ""):
            raise ValidationError({"obligation": "A tax obligation is required."})
        obligation = TaxObligation.objects.filter(entity=entity, pk=ref).first()
        if obligation is None:
            raise ValidationError({"obligation": f"No tax obligation '{ref}' in this entity."})
        filing = prepare_filing(
            obligation,
            period_start=_date(body.get("period_start"), "period_start", required=True),
            period_end=_date(body.get("period_end"), "period_end", required=True),
            due_date=_date(body.get("due_date"), "due_date"),
            currency=_resolve_currency(body.get("currency")),
            actor_user=request.user,
        )
        return success_response(
            f"Tax filing {filing.document_number} prepared.",
            data=TaxFilingSerializer(filing).data, status=201,
        )


class _TaxFilingActionBase(_FinanceBase):
    def _filing(self, request, pk):
        entity = resolve_entity(request)
        filing = TaxFiling.objects.filter(entity=entity, pk=pk).select_related(
            "obligation").first()
        if filing is None:
            raise NotFound("Tax filing not found for this entity.")
        return entity, filing


class TaxFilingDetailView(_TaxFilingActionBase):
    rbac_permission = "finance.tax.view"

    def get(self, request, pk):
        _, filing = self._filing(request, pk)
        return success_response(
            "Tax filing retrieved.", data=TaxFilingSerializer(filing).data,
        )


class TaxFilingFileView(_TaxFilingActionBase):
    """POST — submit a draft return (net input VAT, book any penalty)."""

    rbac_permission = "finance.tax.file"

    def post(self, request, pk):
        from .tax_filing import file_filing

        entity, filing = self._filing(request, pk)
        body = request.data or {}
        adjustment = body.get("adjustment_amount")
        file_filing(
            filing,
            filed_date=_date(body.get("filed_date"), "filed_date", required=True),
            filing_reference=body.get("filing_reference", ""),
            adjustment_amount=_money(adjustment, "adjustment_amount") if adjustment not in (None, "") else 0,
            adjustment_account=_resolve_account(
                entity, body.get("adjustment_account"), "adjustment_account"),
            actor_user=request.user,
        )
        filing.refresh_from_db()
        return success_response(
            f"Tax filing {filing.document_number} filed.",
            data=TaxFilingSerializer(filing).data,
        )


class TaxFilingPayView(_TaxFilingActionBase):
    """POST — remit a filed return (Dr liability, Cr bank)."""

    rbac_permission = "finance.tax.pay"

    def post(self, request, pk):
        from .tax_filing import pay_filing

        entity, filing = self._filing(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"))
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None
        pay_filing(
            filing, bank_account=bank,
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            amount=amount, actor_user=request.user,
        )
        filing.refresh_from_db()
        return success_response(
            f"Tax filing {filing.document_number} remitted.",
            data=TaxFilingSerializer(filing).data,
        )


# --------------------------------------------------------------------------- #
# Payroll                                                                     #
# --------------------------------------------------------------------------- #

class PayrollRunListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) payroll runs for an entity."""

    @property
    def rbac_permission(self):
        return "finance.payrollrun.create" if self.request.method == "POST" \
            else "finance.payrollrun.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = PayrollRun.objects.filter(entity=entity).prefetch_related("lines")
        if (status_val := request.query_params.get("run_status")):
            qs = qs.filter(run_status=status_val)
        return success_response(
            "Payroll runs retrieved.",
            data=PayrollRunSerializer(qs.order_by("-pay_date", "-id")[:200], many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from .payroll import compute_payroll

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        run = PayrollRun.objects.create(
            entity=entity,
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            period_label=body.get("period_label", ""),
            narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")),
            bank_account=_resolve_bank_account(
                entity, body.get("bank_account"), required=False),
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            PayrollLine.objects.create(
                run=run, line_no=i,
                employee_name=ln.get("employee_name", ""),
                gross_amount=_money(ln.get("gross_amount", 0), f"lines[{i}].gross_amount"),
                paye_amount=_money(ln.get("paye_amount", 0), f"lines[{i}].paye_amount"),
                pension_amount=_money(ln.get("pension_amount", 0), f"lines[{i}].pension_amount"),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        compute_payroll(run)
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} created.",
            data=PayrollRunSerializer(run).data, status=201,
        )


class _PayrollActionBase(_FinanceBase):
    def _run(self, request, pk):
        entity = resolve_entity(request)
        run = PayrollRun.objects.filter(entity=entity, pk=pk).first()
        if run is None:
            raise NotFound("Payroll run not found for this entity.")
        return entity, run


class PayrollRunDetailView(_PayrollActionBase):
    rbac_permission = "finance.payrollrun.view"

    def get(self, request, pk):
        _, run = self._run(request, pk)
        return success_response(
            "Payroll run retrieved.", data=PayrollRunSerializer(run).data,
        )


class PayrollRunPostView(_PayrollActionBase):
    rbac_permission = "finance.payrollrun.post"

    def post(self, request, pk):
        from .payroll import post_payroll

        _, run = self._run(request, pk)
        post_payroll(run, actor_user=request.user)
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} accrued.",
            data=PayrollRunSerializer(run).data,
        )


class PayrollRunPayView(_PayrollActionBase):
    rbac_permission = "finance.payrollrun.pay"

    def post(self, request, pk):
        from .payroll import pay_payroll

        entity, run = self._run(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"), required=False)
        pay_payroll(
            run, bank_account=bank,
            pay_date=_date(body.get("pay_date"), "pay_date"),
            actor_user=request.user,
        )
        run.refresh_from_db()
        return success_response(
            f"Payroll run {run.document_number} disbursed.",
            data=PayrollRunSerializer(run).data,
        )


# --------------------------------------------------------------------------- #
# Budgets                                                                     #
# --------------------------------------------------------------------------- #

class BudgetListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) budgets for an entity."""

    @property
    def rbac_permission(self):
        return "finance.budget.create" if self.request.method == "POST" \
            else "finance.budget.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = Budget.objects.filter(entity=entity).select_related("fiscal_year").prefetch_related("lines")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        return success_response(
            "Budgets retrieved.", data=BudgetSerializer(qs[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A budget name is required."})
        budget = Budget.objects.create(
            entity=entity,
            fiscal_year=_resolve_fiscal_year(entity, body.get("fiscal_year")),
            name=name,
        )
        return success_response(
            f"Budget '{name}' created.",
            data=BudgetSerializer(budget).data, status=201,
        )


class _BudgetActionBase(_FinanceBase):
    def _budget(self, request, pk):
        entity = resolve_entity(request)
        budget = Budget.objects.filter(entity=entity, pk=pk).select_related("fiscal_year").first()
        if budget is None:
            raise NotFound("Budget not found for this entity.")
        return entity, budget


class BudgetDetailView(_BudgetActionBase):
    rbac_permission = "finance.budget.view"

    def get(self, request, pk):
        _, budget = self._budget(request, pk)
        return success_response("Budget retrieved.", data=BudgetSerializer(budget).data)


class BudgetLineCreateView(_BudgetActionBase):
    """POST {account, period_no, amount, cost_center?} — add/update one budget cell."""

    rbac_permission = "finance.budget.edit"

    def post(self, request, pk):
        from .budgets import add_budget_line

        entity, budget = self._budget(request, pk)
        body = request.data or {}
        add_budget_line(
            budget,
            account=_resolve_account(entity, body.get("account"), "account", required=True),
            period_no=_int(body.get("period_no"), "period_no", required=True, minimum=1),
            amount=_money(body.get("amount", 0), "amount"),
            cost_center=_resolve_cost_center(entity, body.get("cost_center"), "cost_center"),
        )
        budget.refresh_from_db()
        return success_response(
            "Budget line saved.", data=BudgetSerializer(budget).data, status=201,
        )


class BudgetApproveView(_BudgetActionBase):
    rbac_permission = "finance.budget.approve"

    def post(self, request, pk):
        from .budgets import approve_budget

        _, budget = self._budget(request, pk)
        approve_budget(budget, actor_user=request.user)
        budget.refresh_from_db()
        return success_response(
            f"Budget '{budget.name}' approved and locked.",
            data=BudgetSerializer(budget).data,
        )


class BudgetVarianceView(_BudgetActionBase):
    """GET ?period_no — budget-vs-actual variance for the budget."""

    rbac_permission = "finance.budget.view"

    def get(self, request, pk):
        from .reports import budget_vs_actual

        _, budget = self._budget(request, pk)
        period_no = _int(request.query_params.get("period_no"), "period_no", minimum=1)
        report = budget_vs_actual(budget, period_no=period_no)

        def _money_pair(amount):
            return {"kobo": amount, "naira": format_naira(amount)}

        return success_response(
            "Budget variance retrieved.",
            data={
                "budget_id": report.budget_id,
                "fiscal_year_id": report.fiscal_year_id,
                "period_no": report.period_no,
                "rows": [
                    {
                        "account_id": r.account_id, "code": r.code, "name": r.name,
                        "account_type": r.account_type,
                        "budget": _money_pair(r.budget),
                        "actual": _money_pair(r.actual),
                        "variance": _money_pair(r.variance),
                    }
                    for r in report.rows
                ],
                "total_budget": _money_pair(report.total_budget),
                "total_actual": _money_pair(report.total_actual),
                "total_variance": _money_pair(report.total_variance),
            },
        )


# --------------------------------------------------------------------------- #
# Fixed assets                                                                #
# --------------------------------------------------------------------------- #

class FixedAssetListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) fixed assets for an entity."""

    @property
    def rbac_permission(self):
        return "finance.fixedasset.create" if self.request.method == "POST" \
            else "finance.fixedasset.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = FixedAsset.objects.filter(entity=entity).prefetch_related("schedule")
        if (status_val := request.query_params.get("asset_status")):
            qs = qs.filter(asset_status=status_val)
        return success_response(
            "Fixed assets retrieved.",
            data=FixedAssetSerializer(qs.order_by("-acquisition_date", "-id")[:200], many=True).data,
        )

    def post(self, request):
        from .constants import DepreciationMethod

        entity = resolve_entity(request)
        body = request.data or {}
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "An asset name is required."})
        asset = FixedAsset.objects.create(
            entity=entity, name=name,
            asset_code=body.get("asset_code", ""),
            acquisition_date=_date(body.get("acquisition_date"), "acquisition_date", required=True),
            cost=_money(body.get("cost", 0), "cost"),
            salvage_value=_money(body.get("salvage_value", 0), "salvage_value"),
            useful_life_months=_int(
                body.get("useful_life_months"), "useful_life_months", required=True, minimum=1),
            method=body.get("method") or DepreciationMethod.STRAIGHT_LINE,
            asset_account=_resolve_account(entity, body.get("asset_account"), "asset_account"),
            accumulated_depreciation_account=_resolve_account(
                entity, body.get("accumulated_depreciation_account"),
                "accumulated_depreciation_account"),
            depreciation_expense_account=_resolve_account(
                entity, body.get("depreciation_expense_account"),
                "depreciation_expense_account"),
            created_by=request.user,
        )
        return success_response(
            f"Fixed asset {asset.document_number} created.",
            data=FixedAssetSerializer(asset).data, status=201,
        )


class _FixedAssetActionBase(_FinanceBase):
    def _asset(self, request, pk):
        entity = resolve_entity(request)
        asset = FixedAsset.objects.filter(entity=entity, pk=pk).first()
        if asset is None:
            raise NotFound("Fixed asset not found for this entity.")
        return entity, asset


class FixedAssetDetailView(_FixedAssetActionBase):
    rbac_permission = "finance.fixedasset.view"

    def get(self, request, pk):
        _, asset = self._asset(request, pk)
        return success_response(
            "Fixed asset retrieved.", data=FixedAssetSerializer(asset).data,
        )


class FixedAssetAcquireView(_FixedAssetActionBase):
    """POST {bank_account?, credit_account?} — capitalise + build the schedule."""

    rbac_permission = "finance.fixedasset.acquire"

    def post(self, request, pk):
        from .assets import acquire_asset

        entity, asset = self._asset(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(entity, body.get("bank_account"), required=False)
        credit = _resolve_account(entity, body.get("credit_account"), "credit_account")
        acquire_asset(
            asset, bank_account=bank, credit_account=credit, actor_user=request.user,
        )
        asset.refresh_from_db()
        return success_response(
            f"Fixed asset {asset.document_number} capitalised.",
            data=FixedAssetSerializer(asset).data,
        )


class FixedAssetDepreciateView(_FixedAssetActionBase):
    """POST {up_to_date} — post every due depreciation charge up to a date."""

    rbac_permission = "finance.fixedasset.depreciate"

    def post(self, request, pk):
        from .assets import post_depreciation

        _, asset = self._asset(request, pk)
        body = request.data or {}
        posted = post_depreciation(
            asset,
            up_to_date=_date(body.get("up_to_date"), "up_to_date", required=True),
            actor_user=request.user,
        )
        asset.refresh_from_db()
        return success_response(
            f"Posted {len(posted)} depreciation charge(s) for {asset.name}.",
            data=FixedAssetSerializer(asset).data,
        )


# --------------------------------------------------------------------------- #
# Audit trail                                                                 #
# --------------------------------------------------------------------------- #

class FinanceAuditLogListView(_FinanceBase):
    """GET — the append-only finance audit trail for an entity (filter action/status)."""

    rbac_permission = "finance.audit.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = FinanceAuditLog.objects.filter(entity=entity).select_related("actor")
        params = request.query_params
        if (action := params.get("action")):
            qs = qs.filter(action=action)
        if (status_val := params.get("status")):
            qs = qs.filter(status=status_val)
        if (target_type := params.get("target_type")):
            qs = qs.filter(target_type=target_type)
        return success_response(
            "Audit log retrieved.",
            data=FinanceAuditLogSerializer(qs[:500], many=True).data,
        )
