"""Tax obligations and filings.
"""
from __future__ import annotations  # Import dependency used by this finance module.


from rest_framework.exceptions import NotFound, ValidationError  # Import dependency used by this finance module.

from core.response import success_response  # Import dependency used by this finance module.

from ..money import format_naira  # Import dependency used by this finance module.
from ..views import resolve_entity  # Import dependency used by this finance module.
from ..models import (  # Import dependency used by this finance module.
    TaxFiling,  # Finance processing step.
    TaxObligation,  # Finance processing step.
)  # Continue structured finance payload.
from ..serializers import (  # Import dependency used by this finance module.
    TaxFilingSerializer,  # Finance processing step.
    TaxObligationSerializer,  # Finance processing step.
)  # Continue structured finance payload.


from .base import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _bool,  # Finance processing step.
    _date,  # Finance processing step.
    _int,  # Finance processing step.
    _money,  # Finance processing step.
    _resolve_account,  # Finance processing step.
    _resolve_bank_account,  # Finance processing step.
    _resolve_currency,  # Finance processing step.
)  # Continue structured finance payload.

# --------------------------------------------------------------------------- #
# Tax remittance / filing                                                     #
# --------------------------------------------------------------------------- #

class TaxObligationListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create) statutory tax obligations for an entity.

    docstring-name: Tax obligations
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.tax.manage" if self.request.method == "POST" \
            else "finance.tax.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = TaxObligation.objects.filter(entity=entity).select_related(  # Query finance data from the database.
            "liability_account", "recoverable_account")  # Finance processing step.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Tax obligations retrieved.",  # Finance processing step.
            data=TaxObligationSerializer(qs.order_by("code"), many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        if not body.get("code"):  # Branch when this finance condition is true.
            raise ValidationError({"code": "An obligation code is required."})  # Surface validation or finance error.
        if not body.get("obligation_type"):  # Branch when this finance condition is true.
            raise ValidationError({"obligation_type": "An obligation type is required."})  # Surface validation or finance error.
        obligation = TaxObligation.objects.create(  # Query finance data from the database.
            entity=entity, code=body["code"], name=body.get("name", body["code"]),  # Store intermediate finance value.
            obligation_type=body["obligation_type"],  # Store intermediate finance value.
            liability_account=_resolve_account(  # Store intermediate finance value.
                entity, body.get("liability_account"), "liability_account", required=True),  # Store intermediate finance value.
            recoverable_account=_resolve_account(  # Store intermediate finance value.
                entity, body.get("recoverable_account"), "recoverable_account"),  # Finance processing step.
            authority_name=body.get("authority_name", ""),  # Store intermediate finance value.
            frequency=body.get("frequency", "MONTHLY"),  # Store intermediate finance value.
            filing_day=_int(body.get("filing_day", 21), "filing_day", minimum=1),  # Store intermediate finance value.
            is_active=_bool(body.get("is_active", True), default=True),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            "Tax obligation created.",  # Finance processing step.
            data=TaxObligationSerializer(obligation).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class TaxObligationDetailView(_FinanceBase):  # Class groups related finance API or service behavior.
    """docstring-name: Tax obligations"""
    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.tax.manage" if self.request.method == "PATCH" \
            else "finance.tax.view"  # Finance processing step.

    def _obligation(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        obligation = TaxObligation.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if obligation is None:  # Branch when this finance condition is true.
            raise NotFound("Tax obligation not found for this entity.")  # Surface validation or finance error.
        return entity, obligation  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        _, obligation = self._obligation(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Tax obligation retrieved.", data=TaxObligationSerializer(obligation).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    def patch(self, request, pk):  # Function handles this finance operation.
        entity, obligation = self._obligation(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        if "name" in body:  # Branch when this finance condition is true.
            obligation.name = body["name"]  # Store intermediate finance value.
        if "liability_account" in body:  # Branch when this finance condition is true.
            obligation.liability_account = _resolve_account(  # Store intermediate finance value.
                entity, body.get("liability_account"), "liability_account", required=True)  # Store intermediate finance value.
        if "recoverable_account" in body:  # Branch when this finance condition is true.
            obligation.recoverable_account = _resolve_account(  # Store intermediate finance value.
                entity, body.get("recoverable_account"), "recoverable_account")  # Finance processing step.
        if "authority_name" in body:  # Branch when this finance condition is true.
            obligation.authority_name = body["authority_name"]  # Store intermediate finance value.
        if "frequency" in body:  # Branch when this finance condition is true.
            obligation.frequency = body["frequency"]  # Store intermediate finance value.
        if "filing_day" in body:  # Branch when this finance condition is true.
            obligation.filing_day = _int(body["filing_day"], "filing_day", minimum=1)  # Store intermediate finance value.
        if "is_active" in body:  # Branch when this finance condition is true.
            obligation.is_active = _bool(body["is_active"])  # Store intermediate finance value.
        obligation.save()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            "Tax obligation updated.", data=TaxObligationSerializer(obligation).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class TaxObligationOutstandingView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET — per-obligation unremitted balance sitting in each control account.

    docstring-name: Outstanding tax obligations
    """

    rbac_permission = "finance.tax.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from ..tax_filing import outstanding_obligations  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        rows = outstanding_obligations(entity)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Outstanding tax obligations retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "rows": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        **r,  # Finance processing step.
                        "payable_balance": {"kobo": r["payable_balance"], "naira": format_naira(r["payable_balance"])},  # Finance processing step.
                        "recoverable_balance": {"kobo": r["recoverable_balance"], "naira": format_naira(r["recoverable_balance"])},  # Finance processing step.
                        "net_outstanding": {"kobo": r["net_outstanding"], "naira": format_naira(r["net_outstanding"])},  # Finance processing step.
                    }  # Continue structured finance payload.
                    for r in rows  # Iterate through finance records.
                ],  # Continue structured finance payload.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class TaxFilingSummaryView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET — header KPIs over **all** tax filings (accurate under pagination).

    docstring-name: Tax filings
    """

    rbac_permission = "finance.tax.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from django.db.models import Count, F, Q, Sum  # Import dependency used by this finance module.
        from django.db.models.functions import Coalesce  # Import dependency used by this finance module.

        from ..constants import TaxFilingStatus  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        agg = TaxFiling.objects.filter(entity=entity).aggregate(  # Query finance data from the database.
            outstanding=Coalesce(  # Store intermediate finance value.
                Sum(F("amount_due") - F("amount_paid"),  # Finance processing step.
                    filter=~Q(filing_status=TaxFilingStatus.PAID)), 0),  # Store intermediate finance value.
            open=Count("id", filter=Q(filing_status=TaxFilingStatus.DRAFT)),  # Store intermediate finance value.
            filed=Count("id", filter=Q(filing_status=TaxFilingStatus.FILED)),  # Store intermediate finance value.
            paid=Count("id", filter=Q(filing_status=TaxFilingStatus.PAID)),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response("Tax filing summary retrieved.", data=agg)  # Return the computed finance response.


class TaxFilingListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (prepare from GL) tax filings for an entity.

    docstring-name: Tax filings
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.tax.file" if self.request.method == "POST" \
            else "finance.tax.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = TaxFiling.objects.filter(entity=entity).select_related("obligation")  # Query finance data from the database.
        if (ob := request.query_params.get("obligation")):  # Branch when this finance condition is true.
            qs = qs.filter(obligation_id=ob)  # Store intermediate finance value.
        if (status_val := request.query_params.get("filing_status")):  # Branch when this finance condition is true.
            qs = qs.filter(filing_status=status_val)  # Store intermediate finance value.
        return self.paginate(  # Return the computed finance response.
            request, qs.order_by("-period_end", "-id"), TaxFilingSerializer)  # Finance processing step.

    def post(self, request):  # Function handles this finance operation.
        from ..tax_filing import prepare_filing  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        ref = body.get("obligation")  # Store intermediate finance value.
        if ref in (None, ""):  # Branch when this finance condition is true.
            raise ValidationError({"obligation": "A tax obligation is required."})  # Surface validation or finance error.
        obligation = TaxObligation.objects.filter(entity=entity, pk=ref).first()  # Query finance data from the database.
        if obligation is None:  # Branch when this finance condition is true.
            raise ValidationError({"obligation": f"No tax obligation '{ref}' in this entity."})  # Surface validation or finance error.
        filing = prepare_filing(  # Store intermediate finance value.
            obligation,  # Finance processing step.
            period_start=_date(body.get("period_start"), "period_start", required=True),  # Store intermediate finance value.
            period_end=_date(body.get("period_end"), "period_end", required=True),  # Store intermediate finance value.
            due_date=_date(body.get("due_date"), "due_date"),  # Store intermediate finance value.
            currency=_resolve_currency(body.get("currency")),  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Tax filing {filing.document_number} prepared.",  # Finance processing step.
            data=TaxFilingSerializer(filing).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _TaxFilingActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _filing(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        filing = TaxFiling.objects.filter(entity=entity, pk=pk).select_related(  # Query finance data from the database.
            "obligation").first()  # Finance processing step.
        if filing is None:  # Branch when this finance condition is true.
            raise NotFound("Tax filing not found for this entity.")  # Surface validation or finance error.
        return entity, filing  # Return the computed finance response.


class TaxFilingDetailView(_TaxFilingActionBase):  # Class groups related finance API or service behavior.
    """docstring-name: Tax filings"""
    rbac_permission = "finance.tax.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, filing = self._filing(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Tax filing retrieved.", data=TaxFilingSerializer(filing).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class TaxFilingFileView(_TaxFilingActionBase):  # Class groups related finance API or service behavior.
    """POST — submit a draft return (net input VAT, book any penalty).

    docstring-name: File a tax return
    """

    rbac_permission = "finance.tax.file"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..tax_filing import file_filing  # Import dependency used by this finance module.

        entity, filing = self._filing(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        adjustment = body.get("adjustment_amount")  # Store intermediate finance value.
        file_filing(  # Finance processing step.
            filing,  # Finance processing step.
            filed_date=_date(body.get("filed_date"), "filed_date", required=True),  # Store intermediate finance value.
            filing_reference=body.get("filing_reference", ""),  # Store intermediate finance value.
            adjustment_amount=_money(adjustment, "adjustment_amount") if adjustment not in (None, "") else 0,  # Store intermediate finance value.
            adjustment_account=_resolve_account(  # Store intermediate finance value.
                entity, body.get("adjustment_account"), "adjustment_account"),  # Finance processing step.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        filing.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Tax filing {filing.document_number} filed.",  # Finance processing step.
            data=TaxFilingSerializer(filing).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class TaxFilingUnfileView(_TaxFilingActionBase):  # Class groups related finance API or service behavior.
    """POST — revert a filed return to draft (reverse its netting/penalty journal).

    docstring-name: Un-file a tax return
    """

    rbac_permission = "finance.tax.file"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..tax_filing import unfile_filing  # Import dependency used by this finance module.

        _, filing = self._filing(request, pk)  # Store intermediate finance value.
        unfile_filing(filing, actor_user=request.user)  # Store intermediate finance value.
        filing.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Tax filing {filing.document_number} un-filed.",  # Finance processing step.
            data=TaxFilingSerializer(filing).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class TaxFilingPayView(_TaxFilingActionBase):  # Class groups related finance API or service behavior.
    """POST — remit a filed return (Dr liability, Cr bank).

    docstring-name: Pay a tax filing
    """

    rbac_permission = "finance.tax.pay"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from ..tax_filing import pay_filing  # Import dependency used by this finance module.

        entity, filing = self._filing(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        bank = _resolve_bank_account(entity, body.get("bank_account"))  # Store intermediate finance value.
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None  # Store intermediate finance value.
        pay_filing(  # Finance processing step.
            filing, bank_account=bank,  # Store intermediate finance value.
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),  # Store intermediate finance value.
            amount=amount, actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        filing.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Tax filing {filing.document_number} remitted.",  # Finance processing step.
            data=TaxFilingSerializer(filing).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


