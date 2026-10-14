"""Topping up the platform's provider balance (``/v1/payments/platform/provider-topups/``).

The platform pays money from its own bank account into its payment provider
balance (:mod:`vs_payments.topups`). These routes are the platform finance
team's screen for recording one and putting it forward; the two approvals run
through the workflow engine, like a held settlement's, and are not routes here.

* ``GET`` / ``POST`` the collection: list top-ups (``payments.platform_topup.view``)
  and record a draft (``payments.platform_topup.create``).
* ``GET`` / ``PATCH`` one top-up: read it, and correct it while it is a draft.
* ``GET .../bank-accounts/``: the platform's bank accounts the form's picker offers.
* ``POST .../submit/``: put a draft forward for approval
  (``payments.platform_topup.submit``).

Platform staff only (``IsVisionStaff``): a school user is refused before any key
is read, and every row belongs to the platform's own books, so there is no tenant
to cross into. Reads and writes go through
:class:`~vs_payments.serializers.ProviderBalanceTopUpSerializer`, which applies
Field Access to every field.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.field_enforcement import can_read
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive, IsVisionStaff

from . import held, topups
from .constants import ProviderTopUpStatus
from .reach import PaymentsReach
from .serializers import ProviderBalanceTopUpSerializer

#: Most rows the list answers in one page.
_LIMIT = 200


def _limit(request) -> int:
    """The page size ``?limit=`` asks for: 50 by default, at least 1, at most 200."""
    try:
        return min(max(int(request.query_params.get("limit") or 50), 1), _LIMIT)
    except ValueError as exc:
        raise ValidationError({"limit": "Expected a whole number."}) from exc


def _reach(request):
    """The caller's reach over the platform's books; a refusal where they are not set up."""
    platform = held.platform_books()
    if platform is None:
        raise NotFound("The platform's books are not set up.")
    return PaymentsReach.for_request(request, platform)


def _rows(reach):
    return reach.topups().select_related("bank_account", "branch", "journal", "created_by")


def _row_or_404(request, pk):
    row = _rows(_reach(request)).filter(pk=pk).first()
    if row is None:
        raise NotFound("No such top-up.")
    return row


def _represent(request, row, approval=None) -> dict:
    """One top-up as the caller may read it, with the latest approval request's id.

    ``approval`` is the submission's approval block; it says where the approval
    stands, so it travels only to a caller who may read the status.
    """
    from vs_finance.approvals import with_approval_request

    data = dict(ProviderBalanceTopUpSerializer(row, context={"request": request}).data)
    if approval is not None and can_read(request, "payments.platform_topup.status"):
        data["approval"] = approval
    return with_approval_request(data, row)


class PlatformProviderTopUpListCreateView(APIView):
    """GET: the platform's top-ups of its provider balance, newest first. POST: record one as a draft.

    ``?status=`` lists one status (``DRAFT``, ``PENDING_APPROVAL``, ``POSTED`` or
    ``REJECTED``) and ``?limit=`` caps the page (at most 200). Read with
    ``payments.platform_topup.view``.

    POST takes ``amount`` (kobo, more than nothing), ``bank_account`` (the id of
    an active bank account of the platform's books), ``topped_up_on`` (the day the
    money left the bank, YYYY-MM-DD, not in the future), and optionally
    ``provider`` (the configured default when omitted), ``reference`` (unique for
    the provider among top-ups not rejected) and ``note``. It books nothing: the
    top-up is a draft until it is put forward and approved. Needs
    ``payments.platform_topup.create``; answers 201 with the draft.

    docstring-name: Provider balance top-ups
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]

    @property
    def rbac_permission(self):
        if self.request.method in SAFE_METHODS:
            return "payments.platform_topup.view"
        return "payments.platform_topup.create"

    def get(self, request):
        rows = _rows(_reach(request)).order_by("-id")
        status = str(request.query_params.get("status") or "").strip().upper()
        if status:
            if status not in ProviderTopUpStatus.values:
                raise ValidationError(
                    {"status": f"Use one of: {', '.join(ProviderTopUpStatus.values)}."})
            rows = rows.filter(status=status)
        page = list(rows[:_limit(request)])
        return success_response("Provider balance top-ups retrieved.", data=ProviderBalanceTopUpSerializer(
            page, many=True, context={"request": request}).data)

    def post(self, request):
        reach = _reach(request)
        serializer = ProviderBalanceTopUpSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            row = topups.create_topup(data=serializer.validated_data, actor_user=request.user)
            if not reach.topups().filter(pk=row.pk).exists():
                raise PermissionDenied("This bank account belongs to a branch you do not work in.")
        row = _rows(reach).get(pk=row.pk)
        return success_response("Provider balance top-up recorded.", data=_represent(request, row),
                                status=201)


class PlatformProviderTopUpBankAccountsView(APIView):
    """GET: the platform's active bank accounts a top-up can be paid from.

    The picker of the top-up form, answered from the platform's own books
    whichever ledger entity the operator has open. Each row is ``{id, name,
    bank_name, branch_id, branch_name, currency}``, ordered by name; account
    numbers are never served. A caller bound to some branches gets those
    branches' accounts only, the accounts a top-up of theirs may name. Needs
    ``payments.platform_topup.create``, the key of the form it feeds, and the
    Read switch of ``payments.platform_topup.bank_account``: the rows are the
    values of that field, so a role that may not read it is refused (403).

    docstring-name: Bank accounts for a provider balance top-up
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]
    rbac_permission = "payments.platform_topup.create"

    def get(self, request):
        from vs_finance.models import BankAccount
        from vs_rbac.field_enforcement import FieldReadDenied

        reach = _reach(request)
        if not can_read(request, "payments.platform_topup.bank_account"):
            raise FieldReadDenied(["bank_account"], labels={"bank_account": "Bank account paid from"})
        accounts = (reach.scope.filter(BankAccount.objects.filter(entity=reach.entity, is_active=True))
                    .select_related("branch", "currency").order_by("name", "pk"))
        return success_response("Bank accounts retrieved.", data=[{
            "id": bank.pk, "name": bank.name, "bank_name": bank.bank_name or None,
            "branch_id": bank.branch_id,
            "branch_name": bank.branch.name if bank.branch_id else None,
            "currency": bank.currency.code if bank.currency_id else reach.entity.base_currency.code,
        } for bank in accounts])


class PlatformProviderTopUpDetailView(APIView):
    """GET one top-up; PATCH to correct it while it is a draft.

    GET needs ``payments.platform_topup.view`` and answers the top-up with
    ``workflow_instance_id``, its latest approval request (null before it is first
    put forward), and ``_read_only_fields``, the fields the caller may read but
    not change. PATCH takes any of the POST fields, needs
    ``payments.platform_topup.create`` and the Write switch of each field it
    changes, and is refused (409) once the top-up has been put forward. A
    top-up an approver sent back is a draft again and is corrected here, then
    resumed from the approvals screen.

    docstring-name: Provider balance top-up
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]

    @property
    def rbac_permission(self):
        if self.request.method in SAFE_METHODS:
            return "payments.platform_topup.view"
        return "payments.platform_topup.create"

    def get(self, request, pk):
        return success_response("Provider balance top-up retrieved.",
                                data=_represent(request, _row_or_404(request, pk)))

    def patch(self, request, pk):
        row = _row_or_404(request, pk)
        serializer = ProviderBalanceTopUpSerializer(
            row, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        reach = _reach(request)
        with transaction.atomic():
            topups.update_topup(row, data=serializer.validated_data, actor_user=request.user)
            if not reach.topups().filter(pk=row.pk).exists():
                raise PermissionDenied("This bank account belongs to a branch you do not work in.")
        return success_response("Provider balance top-up updated.",
                                data=_represent(request, _row_or_404(request, pk)))


class PlatformProviderTopUpSubmitView(APIView):
    """POST: put a draft top-up forward for the platform's two-person approval.

    The caller becomes its requester and cannot approve it; two other platform
    people in the settlement approver group do, and it is booked only when the
    second approves. Needs ``payments.platform_topup.submit``. Answers the
    top-up and an ``approval`` block (``instance_id`` and, when nobody can yet
    approve it, why). A top-up already put forward answers its existing approval;
    one that is not a draft is refused (409).

    docstring-name: Submit a provider balance top-up
    """

    permission_classes = [IsAuthenticatedAndActive & IsVisionStaff & HasRBACPermission]
    rbac_permission = "payments.platform_topup.submit"

    def post(self, request, pk):
        from vs_workflow.services import release as release_svc

        row = _row_or_404(request, pk)
        instance = topups.submit_topup(row, requested_by=request.user)
        return success_response("Top-up submitted for approval.", data=_represent(
            request, _row_or_404(request, pk), approval=release_svc.approval_block(instance)))
