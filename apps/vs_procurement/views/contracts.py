"""Vendor contracts and milestones.
"""
from __future__ import annotations


from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_finance.views import resolve_entity

from .. import contracts
from ..models import (
    ContractMilestone,
    VendorContract,
)
from ..serializers import (
    VendorContractSerializer,
)


from .base import (
    _ProcBase,
    _date,
    _money,
    _resolve_vendor,
)

# --------------------------------------------------------------------------- #
# Vendor contracts                                                            #
# --------------------------------------------------------------------------- #

def _build_milestones(contract, items):
    """Create ContractMilestone rows from a request ``milestones`` list (optional)."""
    for i, ms in enumerate(items or [], start=1):
        if not ms.get("name"):
            raise ValidationError({"milestones": "Each milestone needs a name."})
        ContractMilestone.objects.create(
            contract=contract, line_no=ms.get("line_no", i), name=ms["name"],
            due_date=_date(ms.get("due_date"), "due_date"),
            amount=_money(ms.get("amount", 0), "amount"),
            note=ms.get("note", ""),
        )


class ContractListCreateView(_ProcBase):
    """GET (list) / POST (create a DRAFT contract + optional milestones)."""

    @property
    def rbac_permission(self):
        return "procurement.contract.create" if self.request.method == "POST" \
            else "procurement.contract.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = VendorContract.objects.filter(entity=entity).select_related(
            "vendor").prefetch_related("milestones")
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        if (vendor := request.query_params.get("vendor")):
            qs = qs.filter(vendor_id=vendor) if str(vendor).isdigit() \
                else qs.filter(vendor__code=vendor)
        return success_response(
            "Vendor contracts retrieved.",
            data=VendorContractSerializer(qs.order_by("-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data
        if not body.get("reference") or not body.get("title"):
            raise ValidationError({"reference": "reference and title are required."})
        vendor = _resolve_vendor(entity, body.get("vendor"))
        contract = VendorContract.objects.create(
            entity=entity, vendor=vendor,
            reference=body["reference"], title=body["title"],
            start_date=_date(body.get("start_date"), "start_date"),
            end_date=_date(body.get("end_date"), "end_date"),
            contract_value=_money(body.get("contract_value", 0), "contract_value"),
            payment_terms=body.get("payment_terms") or "NET_30",
            auto_renew=bool(body.get("auto_renew", False)),
            renewal_notice_days=body.get("renewal_notice_days") or 30,
            notes=body.get("notes", ""),
            created_by=request.user if request.user.is_authenticated else None,
        )
        _build_milestones(contract, body.get("milestones"))
        return success_response(
            "Vendor contract created.", data=VendorContractSerializer(contract).data, status=201,
        )


class ContractDetailView(_ProcBase):
    """GET (retrieve) / PATCH (update header fields) one contract."""

    @property
    def rbac_permission(self):
        return "procurement.contract.update" if self.request.method == "PATCH" \
            else "procurement.contract.view"

    def _get(self, entity, pk):
        contract = VendorContract.objects.filter(entity=entity, pk=pk).first()
        if contract is None:
            raise NotFound("No such contract in this entity.")
        return contract

    def get(self, request, pk):
        entity = resolve_entity(request)
        contract = self._get(entity, pk)
        return success_response("Vendor contract retrieved.", data=VendorContractSerializer(contract).data)

    def patch(self, request, pk):
        entity = resolve_entity(request)
        contract = self._get(entity, pk)
        body = request.data
        if "title" in body:
            contract.title = body["title"]
        if "start_date" in body:
            contract.start_date = _date(body.get("start_date"), "start_date")
        if "end_date" in body:
            contract.end_date = _date(body.get("end_date"), "end_date")
        if "contract_value" in body:
            contract.contract_value = _money(body.get("contract_value", 0), "contract_value")
        if "payment_terms" in body:
            contract.payment_terms = body["payment_terms"] or "NET_30"
        if "auto_renew" in body:
            contract.auto_renew = bool(body["auto_renew"])
        if "renewal_notice_days" in body:
            contract.renewal_notice_days = body.get("renewal_notice_days") or 30
        if "notes" in body:
            contract.notes = body["notes"]
        contract.save()
        return success_response("Vendor contract updated.", data=VendorContractSerializer(contract).data)


class _ContractActionBase(_ProcBase):
    def _get(self, request, pk):
        entity = resolve_entity(request)
        contract = VendorContract.objects.filter(entity=entity, pk=pk).first()
        if contract is None:
            raise NotFound("No such contract in this entity.")
        return contract


class ContractActivateView(_ContractActionBase):
    rbac_permission = "procurement.contract.activate"

    def post(self, request, pk):
        contract = self._get(request, pk)
        contracts.activate_contract(contract, actor_user=request.user)
        return success_response("Vendor contract activated.", data=VendorContractSerializer(contract).data)


class ContractTerminateView(_ContractActionBase):
    rbac_permission = "procurement.contract.terminate"

    def post(self, request, pk):
        contract = self._get(request, pk)
        contracts.terminate_contract(
            contract, reason=request.data.get("reason", ""), actor_user=request.user)
        return success_response("Vendor contract terminated.", data=VendorContractSerializer(contract).data)


class ContractRenewView(_ContractActionBase):
    """POST — create a successor contract that renews this one (marks this RENEWED)."""

    rbac_permission = "procurement.contract.renew"

    def post(self, request, pk):
        contract = self._get(request, pk)
        body = request.data
        if not body.get("reference"):
            raise ValidationError({"reference": "A reference for the renewal contract is required."})
        value = None
        if "contract_value" in body:
            value = _money(body.get("contract_value", 0), "contract_value")
        successor = contracts.renew_contract(
            contract, reference=body["reference"],
            start_date=_date(body.get("start_date"), "start_date", required=True),
            end_date=_date(body.get("end_date"), "end_date", required=True),
            contract_value=value,
            copy_milestones=bool(body.get("copy_milestones", False)),
            actor_user=request.user,
        )
        return success_response(
            f"Contract renewed → {successor.reference}.",
            data=VendorContractSerializer(successor).data, status=201,
        )


class ContractMilestoneCompleteView(_ProcBase):
    """POST — mark a milestone COMPLETED."""

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
    """GET — contracts due for renewal (inside their notice window or a ``within_days`` horizon)."""

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


