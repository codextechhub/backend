"""Closing your own account: the person's screens, the public page and the review.

Four doors lead to one rule (:mod:`vs_user.services.account_closure`):

* the signed-in person, from inside an app (``me/account-closure/``);
* the same person on a web page, proving ownership of the account by the email
  link the page sends (``account-closure/request/`` and ``.../confirm/``);
* a reviewer at the tenant, reading the requests that wait for a decision and
  closing or declining them (``account-closures/``).
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import error_response, success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import transaction_branch_visible

from ..models import AccountClosureRequest
from ..serializers import (
    AccountClosureConfirmSerializer,
    AccountClosureLinkConfirmSerializer,
    AccountClosureLinkRequestSerializer,
    AccountClosureRequestSerializer,
)
from ..services import account_closure as closures

__all__ = [
    "AccountClosureView",
    "AccountClosureLinkRequestView",
    "AccountClosureLinkConfirmView",
    "AccountClosureRequestListView",
    "AccountClosureRequestCloseView",
    "AccountClosureRequestDeclineView",
]

NOT_OFFERED = "ACCOUNT_CLOSURE_NOT_OFFERED"
LINK_INVALID = "ACCOUNT_CLOSURE_LINK_INVALID"
NOT_PENDING = "ACCOUNT_CLOSURE_NOT_PENDING"


def _refusal(code: str, message: str, http_status: int):
    """An error carrying its stable code both at the top and inside ``error``."""
    return error_response(
        message=message, error={"error_code": code, "message": message},
        status=http_status, code=code,
    )


def _outcome_payload(outcome) -> dict:
    if outcome.status == closures.CLOSED:
        return {"status": closures.CLOSED}
    return {"status": closures.REQUESTED, "requested_at": outcome.record.requested_at}


class AccountClosureView(APIView):
    """
    GET  /user/me/account-closure/
    POST /user/me/account-closure/   {confirm: true}

    The signed-in person's own account. GET answers whether they may delete it
    and how: ``{eligible, mode, pending}``, where ``mode`` is ``close`` (the
    account closes the moment they confirm) or ``request`` (the confirmation
    goes to the tenant, and ``pending`` carries when it was asked). POST
    confirms. An account closing at once answers ``{status: "closed"}``, and the
    app then discards its stored credential; any other answers
    ``{status: "requested", requested_at}``, and asking again returns the
    request already waiting. Missing ``confirm`` is 400. An account no product
    lets its holder close, such as a member of staff, is 403
    ``ACCOUNT_CLOSURE_NOT_OFFERED``, as is anyone viewing the account as another
    person: a proxy session never closes the account it stands in for.

    docstring-name: My account deletion
    """

    permission_classes = [IsAuthenticatedAndActive]
    tenant_param_required = False

    def _refused_message(self):
        return (
            "This account cannot be deleted from here. Ask your administrator "
            "to close it."
        )

    def _proxied(self, request) -> bool:
        return bool(getattr(request, "impersonation_session", None)) or bool(
            request.META.get("HTTP_X_IMPERSONATION_SESSION")
        )

    def get(self, request):
        if self._proxied(request):
            state = {"eligible": False, "mode": None, "pending": None}
        else:
            state = closures.closure_state(request.user)
        return success_response(message="Account deletion state retrieved.", data=state)

    def post(self, request):
        if self._proxied(request) or not closures.closure_state(request.user)["eligible"]:
            return _refusal(NOT_OFFERED, self._refused_message(), status.HTTP_403_FORBIDDEN)
        serializer = AccountClosureConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message="Invalid request.", error=serializer.errors)
        try:
            outcome = closures.request_closure(
                request.user, channel=AccountClosureRequest.Channel.APP, request=request,
            )
        except closures.ClosureNotOffered:
            return _refusal(NOT_OFFERED, self._refused_message(), status.HTTP_403_FORBIDDEN)
        message = (
            "Your account has been deleted."
            if outcome.status == closures.CLOSED
            else "Your request has been sent to your school."
        )
        return success_response(message=message, data=_outcome_payload(outcome))


class AccountClosureLinkRequestView(APIView):
    """
    POST /user/account-closure/request/   {email | identifier, tenant}

    The web page for a person without the app. Names the account by email or by
    an identifier the tenant issued, inside the tenant the page was opened on.
    The answer is the same 200 whether or not an eligible account exists; an
    eligible, active account is emailed a one-time link that expires, and nobody
    else is emailed anything.

    docstring-name: Request an account deletion link
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "account_closure_request"
    tenant_param_required = False

    def post(self, request):
        serializer = AccountClosureLinkRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message="Invalid request.", error=serializer.errors)
        closures.request_link(
            email=serializer.validated_data["email"],
            identifier=serializer.validated_data["identifier"],
            tenant=serializer.validated_data.get("tenant", ""),
            request=request,
        )
        return success_response(
            message="If an account can be deleted from here, we have emailed a link to confirm.",
        )


class AccountClosureLinkConfirmView(APIView):
    """
    POST /user/account-closure/confirm/   {token}

    Follows the emailed link and applies the rule the app applies: an account
    that closes at once closes, and any other becomes a request for the tenant.
    The link is spent by following it. 400 ``ACCOUNT_CLOSURE_LINK_INVALID`` for
    a link that is unknown, already used or expired.

    docstring-name: Confirm an account deletion link
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "account_closure_confirm"
    tenant_param_required = False

    def post(self, request):
        serializer = AccountClosureLinkConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message="Invalid request.", error=serializer.errors)
        try:
            outcome = closures.confirm_link(serializer.validated_data["token"], request=request)
        except closures.ClosureLinkInvalid:
            return _refusal(
                LINK_INVALID, "This link is invalid, has been used or has expired.",
                status.HTTP_400_BAD_REQUEST,
            )
        return success_response(
            message="Your request has been recorded.", data=_outcome_payload(outcome),
        )


def _reviewable(request):
    """The closure requests the caller's tenant holds, narrowed to their branches."""
    queryset = (
        AccountClosureRequest.all_objects.filter(tenant=request.tenant)
        .select_related("user", "decided_by", "branch")
    )
    return transaction_branch_visible(request, queryset)


class AccountClosureRequestListView(APIView):
    """
    GET /user/account-closures/?status=pending|closed|declined

    The people who asked to delete their account, newest first and paginated.
    A reviewer bound to branches reads only those branches' requests; a request
    carrying no branch is read by a reviewer whose reach is the whole tenant.

    RBAC: school.account_closures.view

    docstring-name: Account deletion requests
    """

    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = closures.PERM_VIEW

    def get(self, request):
        queryset = _reviewable(request)
        wanted = (request.query_params.get("status") or "").strip().lower()
        if wanted:
            if wanted not in AccountClosureRequest.Status.values:
                return error_response(
                    message="status must be pending, closed or declined.",
                    error={"status": ["Not a valid status."]},
                )
            queryset = queryset.filter(status=wanted)
        paginator = XVSPagination()
        page = paginator.paginate_queryset(queryset.order_by("-requested_at", "-pk"), request)
        rows = AccountClosureRequestSerializer(
            page, many=True, context={"request": request},
        ).data
        return paginator.get_paginated_response(rows)


class _DecisionView(APIView):
    """Shared body of the two decisions on one pending request."""

    permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]
    rbac_permission = closures.PERM_UPDATE
    approve = True
    done_message = ""

    def post(self, request, pk):
        record = _reviewable(request).filter(pk=pk).first()
        if record is None:
            return error_response(
                message="Request not found.", status=status.HTTP_404_NOT_FOUND,
            )
        try:
            decided = closures.decide(
                record, actor=request.user, approve=self.approve, request=request,
            )
        except closures.ClosureNotPending:
            return _refusal(
                NOT_PENDING, "This request has already been decided.",
                status.HTTP_409_CONFLICT,
            )
        except closures.ClosureNotOffered:
            return _refusal(
                NOT_OFFERED,
                "This account can no longer be closed from a request. Close it "
                "from the user's account instead.",
                status.HTTP_409_CONFLICT,
            )
        decided = _reviewable(request).get(pk=decided.pk)
        return success_response(
            message=self.done_message,
            data=AccountClosureRequestSerializer(decided, context={"request": request}).data,
        )


class AccountClosureRequestCloseView(_DecisionView):
    """
    POST /user/account-closures/{id}/close/

    Closes the account behind a pending request: its sessions end, its devices
    are forgotten and its password stops working. The tenant's records about
    the person are untouched. 409 when the request has been decided already.

    RBAC: school.account_closures.update

    docstring-name: Close the account behind a request
    """

    approve = True
    done_message = "Account closed."


class AccountClosureRequestDeclineView(_DecisionView):
    """
    POST /user/account-closures/{id}/decline/

    Declines a pending request and leaves the account as it is. 409 when the
    request has been decided already.

    RBAC: school.account_closures.update

    docstring-name: Decline an account deletion request
    """

    approve = False
    done_message = "Request declined."
