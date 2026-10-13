"""REST API for refunds returned to payers through the payment provider.

The finance refund (``/v1/finance/refunds/``) stays the document: it is raised,
submitted for approval and posted there. These routes are the provider half:
tying a draft online refund to the payment it returns, reading where the
provider has got to, and moving a failed refund to the branch's own bank. The
path as a whole is described in :mod:`vs_payments.refunds`.

Every row is read through :class:`~vs_payments.reach.PaymentsReach`, so a
branch-bound caller reaches their own branches' refunds only and a refund of
another branch answers 404, like one that does not exist.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_finance.models import Refund
from vs_finance.views import resolve_entity
from vs_finance.views_ops.base import _resolve_bank_account
from vs_rbac.field_enforcement import assert_writable
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import transaction_branch_q

from . import refunds
from .models import ProviderRefund
from .serializers import ProviderRefundSerializer
from .views import _paginate, _reach

_BASE = ("refund", "collection", "entity", "branch")


def _refund_in_reach(request, entity, ref):
    """The finance refund named by id, within the caller's branches; else the 400 of one that is absent."""
    if ref in (None, "") or not str(ref).isdigit():
        raise ValidationError({"refund": "Name the refund by its id."})
    refund = Refund.objects.filter(
        transaction_branch_q(request), entity=entity, pk=int(ref)).first()
    if refund is None:
        raise ValidationError({"refund": f"No refund '{ref}' in this entity."})
    return refund


class ProviderRefundListCreateView(APIView):
    """GET (list) / POST (tie a draft online refund to the payment it returns).

    ``?status=``, ``?refund=`` and ``?collection=`` narrow the list. The POST body
    is ``{refund, collection}``: the finance refund's id, and the payment's id or
    reference. The refund must be a draft raised with the method Online / Gateway,
    for the same customer and branch as the payment, and within what is left of
    it; posting the finance refund then sends it.

    docstring-name: Refunds through the provider
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def rbac_permission(self):
        return "payments.refund.create" if self.request.method == "POST" \
            else "payments.refund.view"

    def get(self, request):
        _, reach = _reach(request)
        qs = reach.provider_refunds().select_related(*_BASE)
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_.upper())
        if (refund := request.query_params.get("refund")) and refund.isdigit():
            qs = qs.filter(refund_id=int(refund))
        if (collection := request.query_params.get("collection")) and collection.isdigit():
            qs = qs.filter(collection_id=int(collection))
        return _paginate(request, qs.order_by("-created_at", "-id"), ProviderRefundSerializer, self)

    def post(self, request):
        entity, reach = _reach(request)
        body = request.data
        refund = _refund_in_reach(request, entity, body.get("refund"))
        ref = str(body.get("collection") or "").strip()
        if not ref:
            raise ValidationError({"collection": "Name the online payment the refund returns."})
        match = Q(reference=ref) | (Q(pk=int(ref)) if ref.isdigit() else Q())
        collection = reach.collections().filter(match).first()
        if collection is None:
            raise ValidationError({"collection": f"No collection '{ref}' in this entity."})
        assert_writable(request, "payments.refund",
                        {"collection_id": collection.pk}, creating=True)
        link = refunds.link_refund(
            entity=entity, refund=refund, collection=collection, actor_user=request.user)
        return success_response(
            "Refund tied to the payment.",
            data=ProviderRefundSerializer(link, context={"request": request}).data, status=201)


class ProviderRefundDetailView(APIView):
    """GET one refund through the provider; ``?verify=1`` asks the provider and books what it confirms.

    docstring-name: Refunds through the provider
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.refund.view"

    def get(self, request, pk):
        _, reach = _reach(request)
        link = reach.get_or_404(
            reach.provider_refunds().select_related(*_BASE), pk, "No such refund in this entity.")
        if request.query_params.get("verify") in ("1", "true", "True"):
            link = refunds.confirm_provider_refund(link)
        return success_response(
            "Refund retrieved.",
            data=ProviderRefundSerializer(link, context={"request": request}).data)


class ProviderRefundPayFromBankView(APIView):
    """POST: stop returning this refund through the provider and pay it from a bank account.

    For a refund not yet sent, or one the provider failed. Body: ``bank_account``
    (the refund's own branch's account) and optionally ``method`` (a bank method;
    bank transfer by default). The finance refund is a draft again, ready to
    submit or post as any bank refund.

    docstring-name: Pay a refund from the bank
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.refund.create"

    def post(self, request, pk):
        entity, reach = _reach(request)
        link = reach.get_or_404(
            reach.provider_refunds().select_related(*_BASE), pk, "No such refund in this entity.")
        body = request.data
        bank = _resolve_bank_account(
            request, entity, body.get("bank_account"), required=False,
            document_branch=link.branch_id, noun="refund")
        refunds.pay_from_bank(
            link, method=body.get("method") or "BANK_TRANSFER", bank_account=bank,
            actor_user=request.user)
        link = ProviderRefund.objects.select_related(*_BASE).get(pk=link.pk)
        return success_response(
            "The refund will be paid from the bank.",
            data=ProviderRefundSerializer(link, context={"request": request}).data)
