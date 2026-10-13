"""Branch payment terminals and the payments made on them (``/v1/payments/``).

* ``terminals/``: each branch's payment terminal in the caller's reach, with its
  status, the payment page and transfer details the provider reported, and where
  its money goes. Read with ``payments.terminal.view``.
* ``terminals/<id>/retry/``: try again to open a terminal that failed to set up,
  or refresh an open one's details and route. ``payments.terminal.update``.
* ``terminals/<id>/destinations/``: the WhatsApp numbers a terminal notifies, and
  adding one (a branch's own phone is the first); ``terminals/<id>/destinations/remove/``
  removes one, never the last. ``payments.terminal.update``.
* ``terminal-payments/`` and ``terminal-payments/<id>/``: payments made on the
  terminals, the unmatched ones by default, each with up to five suggested
  customers. Read with ``payments.terminal_payment.view``.
* ``terminal-payments/<id>/match/``: apply a payment to a customer of its branch.
  ``payments.terminal_payment.match``.
* ``terminal-payments/<id>/unmatch/``: put a matched payment back on the list.
  ``payments.terminal_payment.reverse``.

Every route starts from :class:`vs_payments.reach.PaymentsReach`, so a branch-bound
reader sees, and can act on, their own branches' terminals and payments only; any
other answers 404, exactly like one that does not exist. The suggested customers,
the payer, the sender and the matched customer are registered fields
(``payments.terminal_payment``), absent from the payload for a caller who may not
read them, and the suggestions are not even ranked for such a caller.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response
from vs_finance.models import Customer
from vs_rbac.field_enforcement import FieldReadDenied, assert_writable, can_read
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import branch_q

from . import terminal_payments, terminals
from .constants import TerminalStatus
from .serializers import (
    TerminalDestinationSerializer,
    TerminalPaymentSerializer,
    VirtualTerminalSerializer,
)
from .views import _paginate, _reach

SUGGESTIONS_KEY = "payments.terminal_payment.suggestions"


class TerminalListView(APIView):
    """GET: the payment terminals of the branches the caller reaches.

    Filters: ``status`` (``PENDING``, ``ACTIVE``, ``FAILED``, ``DEACTIVATED``) and
    ``branch`` (id). A branch-bound reader sees their own branches' terminals and
    no other's; a one-branch tenant sees its one.

    docstring-name: Payment terminals
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.terminal.view"

    def get(self, request):
        _, reach = _reach(request)
        qs = reach.terminals().select_related("branch")
        status = str(request.query_params.get("status") or "").strip().upper()
        if status:
            if status not in TerminalStatus.values:
                raise ValidationError({"status": f"Use one of: {', '.join(TerminalStatus.values)}."})
            qs = qs.filter(status=status)
        branch = request.query_params.get("branch")
        if branch:
            if not str(branch).isdigit():
                raise ValidationError({"branch": "Name the branch by its id."})
            qs = qs.filter(branch_id=int(branch))
        return _paginate(request, qs.order_by("branch__name", "-id"),
                         VirtualTerminalSerializer, self, context={"request": request})


class TerminalRetryView(APIView):
    """POST: open a terminal that is pending or failed now, or refresh an open one.

    A terminal that is open is asked about again at the provider, which fills in
    payment details the provider had not produced yet, and its settlement route
    is brought in line with the tenant's custody. A deactivated terminal is
    refused (409): the provider offers no way to switch one back on.

    docstring-name: Retry a payment terminal
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.terminal.update"

    def post(self, request, pk):
        _, reach = _reach(request)
        terminal = reach.get_or_404(
            reach.terminals().select_related("branch", "tenant"), pk,
            "No payment terminal matches this id.")
        if terminal.status == TerminalStatus.DEACTIVATED:
            from .exceptions import PaymentStateError

            raise PaymentStateError(
                "This terminal has been deactivated and cannot be switched back on. "
                "A new one is opened if the branch returns to service.", terminal=terminal.pk)
        terminal = terminals.retry_terminal(terminal)
        return success_response(
            "Payment terminal retried.",
            data=VirtualTerminalSerializer(terminal, context={"request": request}).data)


DESTINATION_RESOURCE = "payments.terminal_destination"


def _terminal(request, pk):
    """One terminal in the caller's reach, or the 404 of one that does not exist."""
    _, reach = _reach(request)
    return reach.get_or_404(
        reach.terminals().select_related("branch", "tenant"), pk, "No payment terminal matches this id.")


def _destinations(request, destinations):
    return TerminalDestinationSerializer(
        destinations, many=True, context={"request": request}).data


class TerminalDestinationListView(APIView):
    """GET the numbers a terminal notifies; POST to add one.

    POST body: ``name`` (the person) and ``number`` (international form, or a
    Nigerian number starting with 0). The terminal keeps at most five. A
    terminal that was waiting for a number is opened once the number is added.
    Needs ``payments.terminal.update``; the name and number are switched fields
    of ``payments.terminal_destination``, absent for a role that may not read
    them and refused on write for one that may not write them.

    docstring-name: Terminal notification numbers
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.terminal.update"

    def get(self, request, pk):
        terminal = _terminal(request, pk)
        return success_response("Terminal numbers retrieved.",
                                data=_destinations(request, terminal.destinations))

    def post(self, request, pk):
        terminal = _terminal(request, pk)
        body = request.data or {}
        assert_writable(request, DESTINATION_RESOURCE, {k: body[k] for k in ("name", "number") if k in body},
                        creating=True)
        destinations = terminals.add_destination(
            terminal, name=body.get("name"), number=body.get("number"), actor_user=request.user)
        return success_response("Terminal number added.",
                                data=_destinations(request, destinations), status=201)


class TerminalDestinationRemoveView(APIView):
    """POST: remove one number from a terminal. Body: ``number``.

    The terminal's last number cannot be removed (409): the provider requires
    one. A caller who may not read the numbers cannot remove one, since naming
    it would let them probe which numbers the terminal holds.
    Needs ``payments.terminal.update``.

    docstring-name: Remove a terminal notification number
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.terminal.update"

    def post(self, request, pk):
        terminal = _terminal(request, pk)
        if not can_read(request, f"{DESTINATION_RESOURCE}.number"):
            raise FieldReadDenied(["number"])
        destinations = terminals.remove_destination(
            terminal, number=(request.data or {}).get("number"), actor_user=request.user)
        return success_response("Terminal number removed.", data=_destinations(request, destinations))


def _suggestions_for(request, rows) -> dict:
    """Suggested customers for the payments in ``rows`` that are waiting, when the caller may read them."""
    if not can_read(request, SUGGESTIONS_KEY):
        return {}
    return {row.pk: terminal_payments.suggestions(row)
            for row in rows if terminal_payments.is_open(row)}


class TerminalPaymentListView(APIView):
    """GET: payments made on the caller's branches' terminals.

    ``status`` is ``UNMATCHED`` (the default: payments waiting for a customer,
    including ones whose matching receipt was voided), ``MATCHED`` or ``ALL``;
    ``branch`` filters by branch id and ``search`` by payment reference. Each
    waiting payment carries up to five suggested customers.

    docstring-name: Terminal payments
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.terminal_payment.view"

    def get(self, request):
        _, reach = _reach(request)
        qs = reach.terminal_payments().select_related(
            "collection", "branch", "terminal", "customer", "matched_by", "unmatched_by",
            "match_payment")
        status = str(request.query_params.get("status") or "UNMATCHED").strip().upper()
        if status == "UNMATCHED":
            qs = qs.filter(terminal_payments.open_payments_q())
        elif status == "MATCHED":
            qs = qs.exclude(terminal_payments.open_payments_q())
        elif status != "ALL":
            raise ValidationError({"status": "Use UNMATCHED, MATCHED or ALL."})
        branch = request.query_params.get("branch")
        if branch:
            if not str(branch).isdigit():
                raise ValidationError({"branch": "Name the branch by its id."})
            qs = qs.filter(branch_id=int(branch))
        if search := str(request.query_params.get("search") or "").strip():
            qs = qs.filter(Q(collection__reference__icontains=search)
                           | Q(collection__provider_reference__icontains=search))
        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs.order_by("-id"), request, view=self)
        context = {"request": request, "suggestions": _suggestions_for(request, page)}
        return paginator.get_paginated_response(
            TerminalPaymentSerializer(page, many=True, context=context).data)


def _payment(request, pk):
    """One terminal payment in the caller's reach, or the 404 of one that does not exist."""
    entity, reach = _reach(request)
    payment = reach.get_or_404(
        reach.terminal_payments().select_related(
            "collection", "branch", "terminal", "customer", "matched_by", "unmatched_by",
            "match_payment", "entity"),
        pk, "No terminal payment matches this id.")
    return entity, payment


def _render(request, payment):
    context = {"request": request, "suggestions": _suggestions_for(request, [payment])}
    return TerminalPaymentSerializer(payment, context=context).data


class TerminalPaymentDetailView(APIView):
    """GET one terminal payment with its suggested customers.

    docstring-name: Terminal payments
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.terminal_payment.view"

    def get(self, request, pk):
        _, payment = _payment(request, pk)
        return success_response("Terminal payment retrieved.", data=_render(request, payment))


def _named_customer(request, entity, ref):
    """The customer ``ref`` (id or code) names, among those the caller reaches.

    Customers are shared records, so a customer of no branch is reachable from
    every branch. One the caller does not reach, or that does not exist, is
    answered alike, in words that also tell a branch-bound clerk why a Lekki
    family cannot be chosen for an Ikeja payment.
    """
    if ref in (None, ""):
        raise ValidationError({"customer": "Name the customer this payment is for."})
    qs = Customer.objects.filter(entity=entity).filter(branch_q(request))
    match = Q(code__iexact=str(ref))
    if str(ref).isdigit():
        match |= Q(pk=int(ref))
    customer = qs.filter(match).first()
    if customer is None:
        raise ValidationError({"customer": (
            "No such customer for this payment's branch. A payment can only be matched to "
            "a customer of its own branch.")})
    return customer


class TerminalPaymentMatchView(APIView):
    """POST: apply an unmatched terminal payment to a customer of its branch.

    Body: ``customer`` (id or code). Posts a receipt that debits unidentified
    receipts and credits the customer's receivable, settling their oldest bills
    first, so a payment that differs from the bill is a part payment. A customer
    of another branch is refused (400), and so is a payment that is already
    matched (409).

    docstring-name: Match a terminal payment
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.terminal_payment.match"

    def post(self, request, pk):
        entity, payment = _payment(request, pk)
        customer = _named_customer(request, entity, (request.data or {}).get("customer"))
        payment = terminal_payments.match(payment, customer, actor_user=request.user)
        _, payment = _payment(request, payment.pk)
        return success_response("Terminal payment matched.", data=_render(request, payment))


class TerminalPaymentUnmatchView(APIView):
    """POST: reverse a match and put the payment back on the unmatched list.

    Voids the receipt that applied the payment, so its journals and allocations
    are reversed. Refused (409) when the payment is not matched, or when the
    receipt cannot be voided; nothing changes then.

    docstring-name: Unmatch a terminal payment
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "payments.terminal_payment.reverse"

    def post(self, request, pk):
        _, payment = _payment(request, pk)
        terminal_payments.unmatch(payment, actor_user=request.user)
        _, payment = _payment(request, pk)
        return success_response("Terminal payment unmatched.", data=_render(request, payment))
