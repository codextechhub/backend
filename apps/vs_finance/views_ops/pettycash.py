"""Petty cash funds and vouchers.
"""
from __future__ import annotations  # Import dependency used by this finance module.


from django.db import transaction  # Import dependency used by this finance module.
from rest_framework.exceptions import NotFound, ValidationError  # Import dependency used by this finance module.

from core.response import success_response  # Import dependency used by this finance module.

from ..constants import DocumentStatus  # Import dependency used by this finance module.
from ..money import format_naira  # Import dependency used by this finance module.
from ..views import resolve_entity  # Import dependency used by this finance module.
from ..models import (  # Import dependency used by this finance module.
    JournalLine,  # Finance processing step.
    PettyCashFund,  # Finance processing step.
    PettyCashVoucher,  # Finance processing step.
    PettyCashVoucherLine,  # Finance processing step.
)  # Continue structured finance payload.
from ..serializers import (  # Import dependency used by this finance module.
    PettyCashFundSerializer,  # Finance processing step.
    PettyCashVoucherSerializer,  # Finance processing step.
)  # Continue structured finance payload.


from .base import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _bool,  # Finance processing step.
    _date,  # Finance processing step.
    _dec,  # Finance processing step.
    _int,  # Finance processing step.
    _money,  # Finance processing step.
    _require_lines,  # Finance processing step.
    _resolve_account,  # Finance processing step.
    _resolve_bank_account,  # Finance processing step.
    _resolve_cost_center,  # Finance processing step.
    _resolve_currency,  # Finance processing step.
    _resolve_tax,  # Finance processing step.
)  # Continue structured finance payload.

# --------------------------------------------------------------------------- #
# Petty cash                                                                  #
# --------------------------------------------------------------------------- #

def _resolve_user(ref, field):  # Function handles this finance operation.
    """Resolve a platform user by id (or return None for a blank ref)."""
    if ref in (None, ""):  # Branch when this finance condition is true.
        return None  # Return the computed finance response.
    from django.contrib.auth import get_user_model  # Import dependency used by this finance module.

    user = get_user_model().objects.filter(pk=ref).first()  # Query finance data from the database.
    if user is None:  # Branch when this finance condition is true.
        raise ValidationError({field: f"No user '{ref}'."})  # Surface validation or finance error.
    return user  # Return the computed finance response.


class PettyCashFundListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) petty-cash funds for an entity.

    docstring-name: Petty cash funds
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.pettycash.create" if self.request.method == "POST" \
            else "finance.pettycash.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = PettyCashFund.objects.filter(entity=entity).select_related("gl_account")  # Query finance data from the database.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Petty cash funds retrieved.",  # Finance processing step.
            data=PettyCashFundSerializer(qs.order_by("name"), many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        if not body.get("name"):  # Branch when this finance condition is true.
            raise ValidationError({"name": "A fund name is required."})  # Surface validation or finance error.
        fund = PettyCashFund.objects.create(  # Query finance data from the database.
            entity=entity, name=body["name"],  # Store intermediate finance value.
            gl_account=_resolve_account(entity, body.get("gl_account"), "gl_account", required=True),  # Store intermediate finance value.
            custodian=_resolve_user(body.get("custodian"), "custodian"),  # Store intermediate finance value.
            custodian_name=body.get("custodian_name", ""),  # Store intermediate finance value.
            float_amount=_money(body.get("float_amount", 0), "float_amount"),  # Store intermediate finance value.
            currency=_resolve_currency(body.get("currency")),  # Store intermediate finance value.
            is_active=_bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            "Petty cash fund created.",  # Finance processing step.
            data=PettyCashFundSerializer(fund).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _PettyCashFundActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _fund(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        fund = PettyCashFund.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if fund is None:  # Branch when this finance condition is true.
            raise NotFound("Petty cash fund not found for this entity.")  # Surface validation or finance error.
        return entity, fund  # Return the computed finance response.


class PettyCashFundDetailView(_PettyCashFundActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Petty cash funds"""
    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.pettycash.update" if self.request.method == "PATCH" \
            else "finance.pettycash.view"  # Finance processing step.

    def _register(self, fund, *, limit=80):  # Function handles this finance operation.
        """The fund's GL ledger as a movement register, newest first, running balance.

        ``in``/``out`` are the petty-cash debit/credit. ``category`` is derived from
        the journal's counter line: 'Top-up' for cash coming in, else the expense
        account's name for a spend.
        """
        lines = list(  # Store intermediate finance value.
            JournalLine.objects  # Query finance data from the database.
            .filter(account=fund.gl_account, entry__status=DocumentStatus.POSTED)  # Store intermediate finance value.
            .select_related("entry")  # Finance processing step.
            .prefetch_related("entry__lines__account")  # Finance processing step.
            .order_by("-entry__date", "-id")[:limit]  # Finance processing step.
        )  # Continue structured finance payload.
        running = fund.current_balance  # Store intermediate finance value.
        out = []  # Store intermediate finance value.
        for ln in lines:  # Iterate through finance records.
            inflow, outflow = int(ln.debit or 0), int(ln.credit or 0)  # Store intermediate finance value.
            if inflow:  # Branch when this finance condition is true.
                category = "Top-up"  # Store intermediate finance value.
            else:  # Fallback finance branch.
                counter = next((l for l in ln.entry.lines.all()  # Store intermediate finance value.
                                if l.account_id != fund.gl_account_id and (l.debit or 0)), None)  # Branch when this finance condition is true.
                category = counter.account.name if counter else "—"  # Store intermediate finance value.
            out.append({  # Finance processing step.
                "id": ln.id, "date": ln.entry.date,  # Finance processing step.
                "description": ln.description or ln.entry.narration or "—",  # Finance processing step.
                "category": category, "in": inflow, "out": outflow,  # Finance processing step.
                "balance": int(running),  # Finance processing step.
            })  # Continue structured finance payload.
            running -= (inflow - outflow)  # Store intermediate finance value.
        return out  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        import datetime  # Import dependency used by this finance module.

        _, fund = self._fund(request, pk)  # Store intermediate finance value.
        week_ago = datetime.date.today() - datetime.timedelta(days=7)  # Store intermediate finance value.
        spent_week = sum(  # Store intermediate finance value.
            v.total for v in PettyCashVoucher.objects.filter(  # Query finance data from the database.
                fund=fund, status=DocumentStatus.POSTED, voucher_date__gte=week_ago))  # Store intermediate finance value.
        data = PettyCashFundSerializer(fund).data  # Store intermediate finance value.
        data["spent_this_week"] = int(spent_week)  # Store intermediate finance value.
        data["register"] = self._register(fund)  # Store intermediate finance value.
        return success_response("Petty cash fund retrieved.", data=data)  # Return the computed finance response.

    def patch(self, request, pk):  # Function handles this finance operation.
        entity, fund = self._fund(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        if "name" in body:  # Branch when this finance condition is true.
            fund.name = body["name"]  # Store intermediate finance value.
        if "custodian" in body:  # Branch when this finance condition is true.
            fund.custodian = _resolve_user(body.get("custodian"), "custodian")  # Store intermediate finance value.
        if "custodian_name" in body:  # Branch when this finance condition is true.
            fund.custodian_name = body["custodian_name"]  # Store intermediate finance value.
        if "float_amount" in body:  # Branch when this finance condition is true.
            fund.float_amount = _money(body.get("float_amount", 0), "float_amount")  # Store intermediate finance value.
        if "is_active" in body:  # Branch when this finance condition is true.
            fund.is_active = _bool(body["is_active"])  # Store intermediate finance value.
        fund.save()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Petty cash fund updated.", data=PettyCashFundSerializer(fund).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PettyCashFundEstablishView(_PettyCashFundActionBase):  # Class groups related finance API or service behavior.
    """POST — move cash from a bank account into the tin (Dr petty cash, Cr bank).

    docstring-name: Establish a petty cash fund
    """

    rbac_permission = "finance.pettycash.establish"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..petty_cash import establish_fund  # Import dependency used by this finance module.

        entity, fund = self._fund(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        bank = _resolve_bank_account(entity, body.get("bank_account"))  # Store intermediate finance value.
        establish_fund(  # Finance processing step.
            fund, bank_account=bank,  # Store intermediate finance value.
            amount=_money(body.get("amount"), "amount"),  # Store intermediate finance value.
            date=_date(body.get("date"), "date", required=True),  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        fund.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Established cash into petty cash '{fund.name}'.",  # Finance processing step.
            data=PettyCashFundSerializer(fund).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PettyCashFundReplenishView(_PettyCashFundActionBase):  # Class groups related finance API or service behavior.
    """POST — top the tin back up to its float (Dr petty cash, Cr bank).

    docstring-name: Replenish a petty cash fund
    """

    rbac_permission = "finance.pettycash.replenish"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..petty_cash import replenish_fund  # Import dependency used by this finance module.

        entity, fund = self._fund(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        bank = _resolve_bank_account(entity, body.get("bank_account"))  # Store intermediate finance value.
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None  # Store intermediate finance value.
        replenish_fund(  # Finance processing step.
            fund, bank_account=bank,  # Store intermediate finance value.
            date=_date(body.get("date"), "date", required=True),  # Store intermediate finance value.
            amount=amount, actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        fund.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Replenished petty cash '{fund.name}'.",  # Finance processing step.
            data=PettyCashFundSerializer(fund).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PettyCashStatusView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET — per-fund cash position + low-balance flags (replenishment alerts).

    docstring-name: Petty cash status
    """

    rbac_permission = "finance.pettycash.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from ..petty_cash import fund_status  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        threshold = _int(  # Store intermediate finance value.
            request.query_params.get("threshold_bps", 2500), "threshold_bps", minimum=0,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        rows = fund_status(entity, threshold_bps=threshold)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Petty cash status retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "rows": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        **r,  # Finance processing step.
                        "float_amount": {"kobo": r["float_amount"], "naira": format_naira(r["float_amount"])},  # Finance processing step.
                        "current_balance": {"kobo": r["current_balance"], "naira": format_naira(r["current_balance"])},  # Finance processing step.
                        "shortfall": {"kobo": r["shortfall"], "naira": format_naira(r["shortfall"])},  # Finance processing step.
                        "last_replenished_at": str(r["last_replenished_at"]) if r["last_replenished_at"] else None,  # Finance processing step.
                    }  # Continue structured finance payload.
                    for r in rows  # Iterate through finance records.
                ],  # Continue structured finance payload.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class PettyCashVoucherListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft + lines) petty-cash vouchers.

    docstring-name: Petty cash vouchers
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.pettycashvoucher.create" if self.request.method == "POST" \
            else "finance.pettycashvoucher.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = PettyCashVoucher.objects.filter(entity=entity).prefetch_related("lines__expense_account")  # Query finance data from the database.
        if (fund := request.query_params.get("fund")):  # Branch when this finance condition is true.
            qs = qs.filter(fund_id=fund)  # Store intermediate finance value.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        return self.paginate(  # Return the computed finance response.
            request, qs.order_by("-voucher_date", "-id"), PettyCashVoucherSerializer)  # Finance processing step.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        from ..petty_cash import price_voucher  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        lines = _require_lines(body)  # Store intermediate finance value.
        fund_ref = body.get("fund")  # Store intermediate finance value.
        if fund_ref in (None, ""):  # Branch when this finance condition is true.
            raise ValidationError({"fund": "A petty cash fund is required."})  # Surface validation or finance error.
        fund = PettyCashFund.objects.filter(entity=entity, pk=fund_ref).first()  # Query finance data from the database.
        if fund is None:  # Branch when this finance condition is true.
            raise ValidationError({"fund": f"No petty cash fund '{fund_ref}' in this entity."})  # Surface validation or finance error.
        voucher = PettyCashVoucher.objects.create(  # Query finance data from the database.
            entity=entity, fund=fund,  # Store intermediate finance value.
            voucher_date=_date(body.get("voucher_date"), "voucher_date", required=True),  # Store intermediate finance value.
            payee=body.get("payee", ""),  # Store intermediate finance value.
            spent_by=_resolve_user(body.get("spent_by"), "spent_by"),  # Store intermediate finance value.
            narration=body.get("narration", ""),  # Store intermediate finance value.
            reference=body.get("reference", ""),  # Store intermediate finance value.
            currency=_resolve_currency(body.get("currency")) or fund.currency,  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        for i, ln in enumerate(lines, start=1):  # Iterate through finance records.
            PettyCashVoucherLine.objects.create(  # Query finance data from the database.
                voucher=voucher, line_no=i,  # Store intermediate finance value.
                description=ln.get("description", ""),  # Store intermediate finance value.
                expense_account=_resolve_account(  # Store intermediate finance value.
                    entity, ln.get("expense_account"),  # Finance processing step.
                    f"lines[{i}].expense_account", required=True),  # Store intermediate finance value.
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),  # Store intermediate finance value.
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),  # Store intermediate finance value.
                tax_code=_resolve_tax(entity, ln.get("tax_code"), f"lines[{i}].tax_code"),  # Store intermediate finance value.
                cost_center=_resolve_cost_center(  # Store intermediate finance value.
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),  # Finance processing step.
            )  # Continue structured finance payload.
        price_voucher(voucher)  # Finance processing step.
        voucher.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Petty cash voucher {voucher.document_number} created.",  # Finance processing step.
            data=PettyCashVoucherSerializer(voucher).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _PettyCashVoucherActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _voucher(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        voucher = PettyCashVoucher.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if voucher is None:  # Branch when this finance condition is true.
            raise NotFound("Petty cash voucher not found for this entity.")  # Surface validation or finance error.
        return entity, voucher  # Return the computed finance response.


class PettyCashVoucherDetailView(_PettyCashVoucherActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Petty cash vouchers"""
    rbac_permission = "finance.pettycashvoucher.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, voucher = self._voucher(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Petty cash voucher retrieved.",  # Finance processing step.
            data=PettyCashVoucherSerializer(voucher).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PettyCashVoucherPostView(_PettyCashVoucherActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Post a petty cash voucher"""
    rbac_permission = "finance.pettycashvoucher.post"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..petty_cash import post_voucher  # Import dependency used by this finance module.

        _, voucher = self._voucher(request, pk)  # Store intermediate finance value.
        post_voucher(voucher, actor_user=request.user)  # Store intermediate finance value.
        voucher.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Petty cash voucher {voucher.document_number} posted.",  # Finance processing step.
            data=PettyCashVoucherSerializer(voucher).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PettyCashVoucherVoidView(_PettyCashVoucherActionBase):  # Class groups related finance API or service behavior.
    """POST — void a posted voucher (reverses its journal, returns the cash to the tin).

    docstring-name: Void a petty cash voucher
    """
    rbac_permission = "finance.pettycashvoucher.post"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..petty_cash import void_voucher  # Import dependency used by this finance module.

        _, voucher = self._voucher(request, pk)  # Store intermediate finance value.
        void_voucher(voucher, actor_user=request.user)  # Store intermediate finance value.
        voucher.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Petty cash voucher {voucher.document_number} voided.",  # Finance processing step.
            data=PettyCashVoucherSerializer(voucher).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


