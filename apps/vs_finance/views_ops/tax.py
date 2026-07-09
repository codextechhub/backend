"""Tax obligations and filings.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from ..money import format_naira
from ..views import resolve_entity
from ..models import (
    TaxFiling,
    TaxObligation,
)
from ..serializers import (
    TaxFilingSerializer,
    TaxObligationSerializer,
)


from .base import (
    _FinanceBase,
    _bool,
    _date,
    _int,
    _money,
    _resolve_account,
    _resolve_bank_account,
    _resolve_currency,
)

# --------------------------------------------------------------------------- #
# Tax remittance / filing                                                     #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Tax Obligation List Create View.
class TaxObligationListCreateView(_FinanceBase):
    """GET (list) / POST (create) statutory tax obligations for an entity.

    docstring-name: Tax obligations
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.tax.manage" if self.request.method == "POST" \
            else "finance.tax.view"

    # Handle GET requests for this endpoint.
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

    # Handle POST requests for this endpoint.
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


# Group endpoint behavior for Tax Obligation Detail View.
class TaxObligationDetailView(_FinanceBase):
    """docstring-name: Tax obligations"""
    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.tax.manage" if self.request.method == "PATCH" \
            else "finance.tax.view"

    # Support the obligation workflow.
    def _obligation(self, request, pk):
        entity = resolve_entity(request)
        obligation = TaxObligation.objects.filter(entity=entity, pk=pk).first()
        if obligation is None:
            raise NotFound("Tax obligation not found for this entity.")
        return entity, obligation

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, obligation = self._obligation(request, pk)
        return success_response(
            "Tax obligation retrieved.", data=TaxObligationSerializer(obligation).data,
        )

    # Handle PATCH requests for this endpoint.
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


# Group endpoint behavior for Tax Obligation Outstanding View.
class TaxObligationOutstandingView(_FinanceBase):
    """GET — per-obligation unremitted balance sitting in each control account.

    docstring-name: Outstanding tax obligations
    """

    rbac_permission = "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from ..tax_filing import outstanding_obligations

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


# Group endpoint behavior for Tax Filing Summary View.
class TaxFilingSummaryView(_FinanceBase):
    """GET — header KPIs over **all** tax filings (accurate under pagination).

    docstring-name: Tax filings
    """

    rbac_permission = "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Count, F, Q, Sum
        from django.db.models.functions import Coalesce

        from ..constants import TaxFilingStatus

        entity = resolve_entity(request)
        agg = TaxFiling.objects.filter(entity=entity).aggregate(
            outstanding=Coalesce(
                Sum(F("amount_due") - F("amount_paid"),
                    filter=~Q(filing_status=TaxFilingStatus.PAID)), 0),
            open=Count("id", filter=Q(filing_status=TaxFilingStatus.DRAFT)),
            filed=Count("id", filter=Q(filing_status=TaxFilingStatus.FILED)),
            paid=Count("id", filter=Q(filing_status=TaxFilingStatus.PAID)),
        )
        return success_response("Tax filing summary retrieved.", data=agg)


# Group endpoint behavior for Tax Filing List Create View.
class TaxFilingListCreateView(_FinanceBase):
    """GET (list) / POST (prepare from GL) tax filings for an entity.

    docstring-name: Tax filings
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.tax.file" if self.request.method == "POST" \
            else "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = TaxFiling.objects.filter(entity=entity).select_related("obligation")
        if (ob := request.query_params.get("obligation")):
            qs = qs.filter(obligation_id=ob)
        if (status_val := request.query_params.get("filing_status")):
            qs = qs.filter(filing_status=status_val)
        return self.paginate(
            request, qs.order_by("-period_end", "-id"), TaxFilingSerializer)

    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..tax_filing import prepare_filing

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


# Define Tax Filing Action Base values.
class _TaxFilingActionBase(_FinanceBase):
    # Support the filing workflow.
    def _filing(self, request, pk):
        entity = resolve_entity(request)
        filing = TaxFiling.objects.filter(entity=entity, pk=pk).select_related(
            "obligation").first()
        if filing is None:
            raise NotFound("Tax filing not found for this entity.")
        return entity, filing


# Group endpoint behavior for Tax Filing Detail View.
class TaxFilingDetailView(_TaxFilingActionBase):
    """docstring-name: Tax filings"""
    rbac_permission = "finance.tax.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, filing = self._filing(request, pk)
        return success_response(
            "Tax filing retrieved.", data=TaxFilingSerializer(filing).data,
        )


# Group endpoint behavior for Tax Filing File View.
class TaxFilingFileView(_TaxFilingActionBase):
    """POST — submit a draft return (net input VAT, book any penalty).

    docstring-name: File a tax return
    """

    rbac_permission = "finance.tax.file"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..tax_filing import file_filing

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


# Group endpoint behavior for Tax Filing Unfile View.
class TaxFilingUnfileView(_TaxFilingActionBase):
    """POST — revert a filed return to draft (reverse its netting/penalty journal).

    docstring-name: Un-file a tax return
    """

    rbac_permission = "finance.tax.file"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..tax_filing import unfile_filing

        _, filing = self._filing(request, pk)
        unfile_filing(filing, actor_user=request.user)
        filing.refresh_from_db()
        return success_response(
            f"Tax filing {filing.document_number} un-filed.",
            data=TaxFilingSerializer(filing).data,
        )


# Group endpoint behavior for Tax Filing Pay View.
class TaxFilingPayView(_TaxFilingActionBase):
    """POST — remit a filed return (Dr liability, Cr bank).

    docstring-name: Pay a tax filing
    """

    rbac_permission = "finance.tax.pay"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..tax_filing import pay_filing

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


