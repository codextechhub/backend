"""Vendor contracts and milestones.
"""
from __future__ import annotations

import datetime

from django.db import models, transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.money import format_naira
from vs_finance.views import resolve_entity

from .. import contracts
from ..constants import ContractStatus, PaymentTerms
from ..models import (
    ContractMilestone,
    PurchaseOrder,
    VendorContract,
)
from ..serializers import (
    VendorContractListSerializer,
    VendorContractSerializer,
)


from .base import (
    _ProcBase,
    _date,
    _resolve_vendor,
    _strict_kobo,
    _text,
)

# --------------------------------------------------------------------------- #
# Vendor contracts                                                            #
# --------------------------------------------------------------------------- #

#: Contract statuses that are terminal — no further header edits allowed.
_TERMINAL_STATUSES = (ContractStatus.EXPIRED, ContractStatus.TERMINATED, ContractStatus.RENEWED)


def _payment_terms(value, field="payment_terms"):
    """Validate an optional payment-terms enum value (defaults to NET_30)."""
    if value in (None, ""):
        return PaymentTerms.NET_30
    if value not in PaymentTerms.values:
        raise ValidationError({field: "Select a valid payment term."})
    return value


def _notice_days(value, field="renewal_notice_days"):
    """Optional renewal-notice period, bounded to a sane 0–365 day window."""
    if value in (None, ""):
        return 30
    if isinstance(value, bool):
        raise ValidationError({field: "Expected a whole number of days."})
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field: "Expected a whole number of days."})
    if days < 0 or days > 365:
        raise ValidationError({field: "Renewal notice must be between 0 and 365 days."})
    return days


def _check_date_order(start_date, end_date):
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValidationError({"end_date": "The end date cannot precede the start date."})


def _build_milestones(contract, items, *, start_line_no=0):
    """Append :class:`ContractMilestone` rows from a request ``milestones`` list (optional).

    Hardened: name required (≤200), amount is strict integer kobo ≥0, due_date is a valid
    ISO date. Uses *append* semantics (never deletes) so completed-milestone history — the
    audit-recorded evidence a deliverable was met — is never clobbered by an edit.
    """
    for i, ms in enumerate(items or [], start=1):
        ContractMilestone.objects.create(
            contract=contract,
            line_no=ms.get("line_no", start_line_no + i),
            name=_text(ms.get("name"), "milestones.name", 200, required=True),
            due_date=_date(ms.get("due_date"), "milestones.due_date"),
            amount=_strict_kobo(ms.get("amount", 0), "milestones.amount"),
            note=_text(ms.get("note"), "milestones.note", 255),
        )


class ContractListCreateView(_ProcBase):
    """GET (list) / POST (create a DRAFT contract + optional milestones).

    docstring-name: Contracts
    """

    @property
    def rbac_permission(self):
        return "procurement.contract.create" if self.request.method == "POST" \
            else "procurement.contract.view"

    def get(self, request):
        entity = resolve_entity(request)
        today = timezone.localdate()
        qs = VendorContract.objects.filter(entity=entity).select_related("vendor").annotate(
            milestone_count=Count("milestones", distinct=True),
        )
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        # ``?expiring=1`` — ACTIVE contracts whose end_date falls in [today, today+30].
        if request.query_params.get("expiring") in ("1", "true", "True"):
            qs = qs.filter(
                status=ContractStatus.ACTIVE, end_date__isnull=False,
                end_date__gte=today, end_date__lte=today + datetime.timedelta(days=30),
            )
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() \
                else qs.filter(vendor__code=vendor)
        if (q := (request.query_params.get("q") or request.query_params.get("search") or "").strip()):
            qs = qs.filter(
                models.Q(reference__icontains=q) | models.Q(title__icontains=q)
                | models.Q(vendor__code__icontains=q) | models.Q(vendor__name__icontains=q),
            )
        return self.paginate(request, qs.order_by("-id"), VendorContractListSerializer)

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        vendor = _resolve_vendor(entity, body.get("vendor"))
        # Reference is auto-generated server-side (sequential per entity, collision-safe);
        # a caller-supplied reference still works and stays unique per the DB constraint.
        reference = _text(body.get("reference"), "reference", 64) or contracts.next_contract_reference(entity)
        start_date = _date(body.get("start_date"), "start_date")
        end_date = _date(body.get("end_date"), "end_date")
        _check_date_order(start_date, end_date)
        contract = VendorContract.objects.create(
            entity=entity, vendor=vendor,
            reference=reference,
            title=_text(body.get("title"), "title", 200, required=True),
            start_date=start_date, end_date=end_date,
            contract_value=_strict_kobo(body.get("contract_value", 0), "contract_value"),
            payment_terms=_payment_terms(body.get("payment_terms")),
            auto_renew=bool(body.get("auto_renew", False)),
            renewal_notice_days=_notice_days(body.get("renewal_notice_days")),
            notes=_text(body.get("notes"), "notes", 255),
            created_by=request.user if request.user.is_authenticated else None,
        )
        _build_milestones(contract, body.get("milestones"))
        return success_response(
            "Vendor contract created.", data=VendorContractSerializer(contract).data, status=201,
        )


class ContractDetailView(_ProcBase):
    """GET (retrieve) / PATCH (update header fields + add milestones) one contract.

    docstring-name: Contracts
    """

    @property
    def rbac_permission(self):
        return "procurement.contract.update" if self.request.method == "PATCH" \
            else "procurement.contract.view"

    def _get(self, entity, pk):
        contract = VendorContract.objects.filter(entity=entity, pk=pk).select_related(
            "vendor", "renews").first()
        if contract is None:
            raise NotFound("No such contract in this entity.")
        return contract

    def get(self, request, pk):
        entity = resolve_entity(request)
        contract = self._get(entity, pk)
        return success_response("Vendor contract retrieved.", data=VendorContractSerializer(contract).data)

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        contract = self._get(entity, pk)
        # Only DRAFT / ACTIVE are editable; terminal states are a settled record.
        if contract.status in _TERMINAL_STATUSES:
            raise ValidationError(
                {"status": f"A '{contract.status}' contract can no longer be edited."})
        body = request.data
        if "title" in body:
            contract.title = _text(body.get("title"), "title", 200, required=True)
        if "start_date" in body:
            contract.start_date = _date(body.get("start_date"), "start_date")
        if "end_date" in body:
            contract.end_date = _date(body.get("end_date"), "end_date")
        _check_date_order(contract.start_date, contract.end_date)
        if "contract_value" in body:
            contract.contract_value = _strict_kobo(body.get("contract_value", 0), "contract_value")
        if "payment_terms" in body:
            contract.payment_terms = _payment_terms(body.get("payment_terms"))
        if "auto_renew" in body:
            contract.auto_renew = bool(body["auto_renew"])
        if "renewal_notice_days" in body:
            contract.renewal_notice_days = _notice_days(body.get("renewal_notice_days"))
        if "notes" in body:
            contract.notes = _text(body.get("notes"), "notes", 255)
        contract.save()
        # Milestones added on edit are appended (existing ones — some completed — are preserved).
        if body.get("milestones"):
            _build_milestones(contract, body["milestones"], start_line_no=contract.milestones.count())
        return success_response("Vendor contract updated.", data=VendorContractSerializer(contract).data)


class ContractSummaryView(_ProcBase):
    """Entity-scoped KPI counts for the contract list header.

    docstring-name: Contract summary
    """
    rbac_permission = "procurement.contract.view"

    def get(self, request):
        entity = resolve_entity(request)
        today = timezone.localdate()
        qs = VendorContract.objects.filter(entity=entity)
        # A single aggregate over the contract table. Expiry is derived from dates (honest
        # without a sweep): "active" excludes ACTIVE rows already past their end_date, which
        # instead count as "expired" alongside the persisted EXPIRED status.
        agg = qs.aggregate(
            active=Count("id", filter=models.Q(
                status=ContractStatus.ACTIVE,
            ) & (models.Q(end_date__isnull=True) | models.Q(end_date__gte=today))),
            expiring_soon=Count("id", filter=models.Q(
                status=ContractStatus.ACTIVE, end_date__isnull=False,
                end_date__gte=today, end_date__lte=today + datetime.timedelta(days=30),
            )),
            expired=Count("id", filter=models.Q(status=ContractStatus.EXPIRED) | models.Q(
                status=ContractStatus.ACTIVE, end_date__isnull=False, end_date__lt=today,
            )),
            total_active_value=models.Sum(
                "contract_value", filter=models.Q(status=ContractStatus.ACTIVE),
            ),
        )
        return success_response("Contract summary retrieved.", data={
            "active": agg["active"] or 0,
            "expiring_soon": agg["expiring_soon"] or 0,
            "expired": agg["expired"] or 0,
            "total_active_value": agg["total_active_value"] or 0,
            "total_active_value_naira": format_naira(agg["total_active_value"] or 0),
        })


class ContractLinkedPurchaseOrdersView(_ProcBase):
    """GET — real purchase orders with this contract's vendor during its term.

    docstring-name: Contract linked purchase orders
    """
    # Reading POs is a *different* resource than reading contracts, so this leaf is gated
    # on the purchase-order view key: a user who can see the contract but not POs is
    # (honestly) shown a forbidden panel here.
    rbac_permission = "procurement.purchase_order.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        contract = VendorContract.objects.filter(entity=entity, pk=pk).first()
        if contract is None:
            raise NotFound("No such contract in this entity.")

        def _row(po, link_type):
            return {
                "id": po.id, "document_number": po.document_number,
                "total": po.total, "total_naira": format_naira(po.total),
                "status": po.status, "order_date": po.order_date,
                # "linked" = an explicit call-off against THIS contract; "association" = a
                # same-vendor PO inside the term with no explicit link (legacy/other scope).
                "link_type": link_type,
            }

        # 1) Explicit call-offs: POs whose contract FK points at this exact contract.
        explicit = PurchaseOrder.objects.filter(
            entity=entity, contract_id=contract.pk,
        ).order_by("-order_date", "-id")[:100]
        rows = [_row(po, "linked") for po in explicit]

        # 2) Term fallback: same-vendor POs with NO explicit link whose order_date falls in
        # the contract window. This preserves the honest association for unlinked/legacy POs
        # without claiming a hard link, and (via contract__isnull) never double-counts a PO
        # that is explicitly linked to some other overlapping contract of the same vendor.
        fallback = PurchaseOrder.objects.filter(
            entity=entity, vendor_id=contract.vendor_id, contract__isnull=True,
        )
        if contract.start_date is not None:
            fallback = fallback.filter(order_date__gte=contract.start_date)
        if contract.end_date is not None:
            fallback = fallback.filter(order_date__lte=contract.end_date)
        rows += [_row(po, "association") for po in fallback.order_by("-order_date", "-id")[:100]]

        return success_response("Linked purchase orders retrieved.", data=rows)


class _ContractActionBase(_ProcBase):
    def _get(self, request, pk):
        entity = resolve_entity(request)
        contract = VendorContract.objects.filter(entity=entity, pk=pk).first()
        if contract is None:
            raise NotFound("No such contract in this entity.")
        return contract


class ContractActivateView(_ContractActionBase):
    """docstring-name: Activate a contract"""
    rbac_permission = "procurement.contract.activate"

    def post(self, request, pk):
        contract = self._get(request, pk)
        contracts.activate_contract(contract, actor_user=request.user)
        return success_response("Vendor contract activated.", data=VendorContractSerializer(contract).data)


class ContractTerminateView(_ContractActionBase):
    """docstring-name: Terminate a contract"""
    rbac_permission = "procurement.contract.terminate"

    def post(self, request, pk):
        contract = self._get(request, pk)
        contracts.terminate_contract(
            contract, reason=_text(request.data.get("reason"), "reason", 255),
            actor_user=request.user)
        return success_response("Vendor contract terminated.", data=VendorContractSerializer(contract).data)


class ContractRenewView(_ContractActionBase):
    """POST — create a successor contract that renews this one (marks this RENEWED).

    docstring-name: Renew a contract
    """

    rbac_permission = "procurement.contract.renew"

    def post(self, request, pk):
        contract = self._get(request, pk)
        entity = contract.entity
        body = request.data
        # A reference is auto-generated (like create) when the renewal payload omits one.
        reference = _text(body.get("reference"), "reference", 64) or contracts.next_contract_reference(entity)
        start_date = _date(body.get("start_date"), "start_date", required=True)
        end_date = _date(body.get("end_date"), "end_date", required=True)
        _check_date_order(start_date, end_date)
        value = None
        if "contract_value" in body:
            value = _strict_kobo(body.get("contract_value", 0), "contract_value")
        successor = contracts.renew_contract(
            contract, reference=reference,
            start_date=start_date, end_date=end_date,
            contract_value=value,
            copy_milestones=bool(body.get("copy_milestones", False)),
            actor_user=request.user,
        )
        return success_response(
            f"Contract renewed → {successor.reference}.",
            data=VendorContractSerializer(successor).data, status=201,
        )


class ContractMilestoneCompleteView(_ProcBase):
    """POST — mark a milestone COMPLETED.

    docstring-name: Complete a contract milestone
    """

    rbac_permission = "procurement.contract.update"

    def post(self, request, pk, milestone_id):
        entity = resolve_entity(request)
        milestone = ContractMilestone.objects.filter(
            contract__entity=entity, contract_id=pk, pk=milestone_id).first()
        if milestone is None:
            raise NotFound("No such milestone on this contract.")
        contracts.complete_milestone(
            milestone, on=_date(request.data.get("completed_date"), "completed_date"),
            actor_user=request.user,
        )
        milestone.contract.refresh_from_db()
        return success_response(
            "Milestone completed.", data=VendorContractSerializer(milestone.contract).data,
        )


class ContractRenewalsView(_ProcBase):
    """GET — contracts due for renewal (inside their notice window or a ``within_days`` horizon).

    docstring-name: Contracts due for renewal
    """

    rbac_permission = "procurement.contract.view"

    def get(self, request):
        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of")
        within = request.query_params.get("within_days")
        rows = contracts.expiring_contracts(
            entity, as_of=as_of, within_days=int(within) if within else None,
        )
        return success_response(
            "Contracts due for renewal retrieved.",
            data=VendorContractSerializer(rows, many=True).data,
        )
