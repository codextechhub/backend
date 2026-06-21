"""Petty cash funds and vouchers.
"""
from __future__ import annotations


from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from ..constants import DocumentStatus
from ..money import format_naira
from ..views import resolve_entity
from ..models import (
    JournalLine,
    PettyCashFund,
    PettyCashVoucher,
    PettyCashVoucherLine,
)
from ..serializers import (
    PettyCashFundSerializer,
    PettyCashVoucherSerializer,
)


from .base import (
    _FinanceBase,
    _bool,
    _date,
    _dec,
    _int,
    _money,
    _require_lines,
    _resolve_account,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
    _resolve_tax,
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
    """GET (list) / POST (create) petty-cash funds for an entity.

    docstring-name: Petty cash funds
    """

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
    """docstring-name: Petty cash funds"""
    @property
    def rbac_permission(self):
        return "finance.pettycash.manage" if self.request.method == "PATCH" \
            else "finance.pettycash.view"

    def _register(self, fund, *, limit=80):
        """The fund's GL ledger as a movement register, newest first, running balance.

        ``in``/``out`` are the petty-cash debit/credit. ``category`` is derived from
        the journal's counter line: 'Top-up' for cash coming in, else the expense
        account's name for a spend.
        """
        lines = list(
            JournalLine.objects
            .filter(account=fund.gl_account, entry__status=DocumentStatus.POSTED)
            .select_related("entry")
            .prefetch_related("entry__lines__account")
            .order_by("-entry__date", "-id")[:limit]
        )
        running = fund.current_balance
        out = []
        for ln in lines:
            inflow, outflow = int(ln.debit or 0), int(ln.credit or 0)
            if inflow:
                category = "Top-up"
            else:
                counter = next((l for l in ln.entry.lines.all()
                                if l.account_id != fund.gl_account_id and (l.debit or 0)), None)
                category = counter.account.name if counter else "—"
            out.append({
                "id": ln.id, "date": ln.entry.date,
                "description": ln.description or ln.entry.narration or "—",
                "category": category, "in": inflow, "out": outflow,
                "balance": int(running),
            })
            running -= (inflow - outflow)
        return out

    def get(self, request, pk):
        import datetime

        _, fund = self._fund(request, pk)
        week_ago = datetime.date.today() - datetime.timedelta(days=7)
        spent_week = sum(
            v.total for v in PettyCashVoucher.objects.filter(
                fund=fund, status=DocumentStatus.POSTED, voucher_date__gte=week_ago))
        data = PettyCashFundSerializer(fund).data
        data["spent_this_week"] = int(spent_week)
        data["register"] = self._register(fund)
        return success_response("Petty cash fund retrieved.", data=data)

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
    """POST — move cash from a bank account into the tin (Dr petty cash, Cr bank).

    docstring-name: Establish a petty cash fund
    """

    rbac_permission = "finance.pettycash.replenish"

    def post(self, request, pk):
        from ..petty_cash import establish_fund

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
    """POST — top the tin back up to its float (Dr petty cash, Cr bank).

    docstring-name: Replenish a petty cash fund
    """

    rbac_permission = "finance.pettycash.replenish"

    def post(self, request, pk):
        from ..petty_cash import replenish_fund

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
    """GET — per-fund cash position + low-balance flags (replenishment alerts).

    docstring-name: Petty cash status
    """

    rbac_permission = "finance.pettycash.view"

    def get(self, request):
        from ..petty_cash import fund_status

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
    """GET (list) / POST (create draft + lines) petty-cash vouchers.

    docstring-name: Petty cash vouchers
    """

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
        from ..petty_cash import price_voucher

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
    """docstring-name: Petty cash vouchers"""
    rbac_permission = "finance.pettycash.view"

    def get(self, request, pk):
        _, voucher = self._voucher(request, pk)
        return success_response(
            "Petty cash voucher retrieved.",
            data=PettyCashVoucherSerializer(voucher).data,
        )


class PettyCashVoucherPostView(_PettyCashVoucherActionBase):
    """docstring-name: Post a petty cash voucher"""
    rbac_permission = "finance.pettycash.post"

    def post(self, request, pk):
        from ..petty_cash import post_voucher

        _, voucher = self._voucher(request, pk)
        post_voucher(voucher, actor_user=request.user)
        voucher.refresh_from_db()
        return success_response(
            f"Petty cash voucher {voucher.document_number} posted.",
            data=PettyCashVoucherSerializer(voucher).data,
        )


