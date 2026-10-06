"""Inter-branch transfers, money held for another branch, recharges and the pair balances.

Who may act for which branch:

* the **receiving** branch requests money and confirms it arrived;
* the **sending** branch sends (through its approval route), declines a request,
  forwards money it holds for another branch and recharges a cost it paid;
* **voiding** reverses both branches' books, so it needs a caller who covers both;
* moving a customer's open balance between branches, and the tenant's shared-cost
  rules, bind more than one branch and need a caller who reaches the whole tenant.

A transfer is read by anyone whose reach includes either of its branches
(:func:`vs_finance.inter_branch.transfers_visible_q`). A reader outside both gets
the same 404 as for a transfer that does not exist, and the pair balances show a
branch-bound reader only the pairs their branches are part of.
"""
from __future__ import annotations

from vs_workflow.services.approval_filter import filter_by_approval_param
from django.db import transaction
from django.db.models import Prefetch, Q
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from vs_config.clock import branch_today
from vs_rbac.scoping import (
    WholeTenantWriteMixin,
    caller_branch_ids,
    caller_may_change,
    caller_reaches_whole_tenant,
    inherited_branch_id,
    shared_write_refusal,
    transaction_branch_scope,
)

from core.response import success_response

from ..approvals import with_approval_request
from ..constants import (
    DocumentStatus,
    InterBranchTransferKind,
    PaymentMethod,
    RechargeBasis,
    SharedCostTreatment,
)
from ..models import (
    BankAccount,
    Customer,
    HeldForBranchReceipt,
    InterBranchRecharge,
    InterBranchTransfer,
    ReceivableTransferItem,
    SharedCostRule,
    SharedCostRuleShare,
)
from ..serializers import ApprovalStateMixin
from ..views import resolve_entity
from .base import _FinanceBase, _bank_account_in_reach, _date, _resolve_account, _transaction_branch

__all__ = [
    "HeldReceiptCustomerLookupView",
    "HeldReceiptDetailView",
    "HeldReceiptForwardView",
    "HeldReceiptListCreateView",
    "HeldReceiptVoidView",
    "InterBranchBalancesView",
    "InterBranchConfirmView",
    "InterBranchDeclineView",
    "InterBranchReceivableMoveView",
    "InterBranchRequestView",
    "InterBranchSendView",
    "InterBranchTransferDetailView",
    "InterBranchTransferListCreateView",
    "InterBranchVoidView",
    "RechargeDetailView",
    "RechargeListCreateView",
    "RechargeVoidView",
    "SharedCostRuleDetailView",
    "SharedCostRuleListCreateView",
]


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #

def _kobo(value, field="amount"):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError({field: "Amount must be a whole number greater than zero."})
    return value


def _text(value, field, limit, *, required=False, message=""):
    text = str(value or "").strip()
    if required and not text:
        raise ValidationError({field: message or "This field is required."})
    return text[:limit]


def _branch(entity, ref, field):
    """A branch of ``entity``'s tenant by id; another tenant's reads as unknown."""
    from vs_rbac.scoping import resolve_branch

    if ref in (None, ""):
        raise ValidationError({field: "Name the branch."})
    branch = resolve_branch(entity.tenant, ref, field)
    if branch is None:
        raise ValidationError({field: "No such branch."})
    return branch


def _branch_account(entity, ref, branch, field):
    """A live bank account of ``branch`` named by id, whether or not the caller reaches it.

    The sending branch names where the money lands at the receiving branch, whose
    accounts it cannot otherwise list. Only the account's name is returned to it.
    """
    if ref in (None, ""):
        return None
    qs = BankAccount.objects.filter(entity=entity, branch=branch, is_active=True)
    account = qs.filter(pk=int(ref)).first() if str(ref).isdigit() else qs.filter(name=str(ref)).first()
    if account is None:
        raise ValidationError({field: f"No live bank account '{ref}' of {branch.name}."})
    return account


def _receiving_account(entity, ref, branch, field="to_bank_account"):
    from ..inter_branch import default_receiving_account

    account = _branch_account(entity, ref, branch, field) or default_receiving_account(entity, branch.pk)
    if account is None:
        raise ValidationError({field: (
            f"{branch.name} has several bank accounts and none is its collection account. "
            f"Name the one that receives the money."
        )})
    return account


def _customer_for_branch(entity, ref, branch, field="customer"):
    """A customer of ``branch``, or one shared by every branch, named by code or id.

    The branch that received a payment names whose it is, though the customer is
    another branch's and outside its own lists; only the name comes back.
    """
    if ref in (None, ""):
        raise ValidationError({field: "Name the customer who paid."})
    qs = Customer.objects.filter(entity=entity).filter(Q(branch=branch) | Q(branch__isnull=True))
    customer = qs.filter(code=str(ref).upper()).first() or (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit() else None
    )
    if customer is None:
        raise ValidationError({field: f"No customer '{ref}' of {branch.name}."})
    return customer


def _reach(request):
    """The branch ids the caller works in, or ``None`` for the whole tenant."""
    return caller_branch_ids(request)


# --------------------------------------------------------------------------- #
# Serializers                                                                 #
# --------------------------------------------------------------------------- #

class InterBranchTransferSerializer(ApprovalStateMixin, serializers.ModelSerializer):
    """One transfer, both branches named.

    A receivable move also says what it carried and who owes whom for it:

    * ``moved_items``: each document the move carried (:class:`ReceivableTransferItem`),
      as ``kind`` (``INVOICE``, ``DEBIT_NOTE``, ``RECEIPT_CREDIT``, ``NOTE_CREDIT``),
      ``document_number``, ``invoice_id`` / ``note_id`` / ``payment_id``,
      ``amount`` (the balance moved, or the credit drawn) and ``deferred_amount``
      (income not yet earned that moved with an invoice);
    * ``net_owed``: open bills less the credit and unearned income handed over,
      which is the inter-branch balance the move booked, as ``amount`` with
      ``owed_by`` and ``owed_to`` (``{id, name}``, both ``null`` when it nets to
      nothing). Tunde moves from Ikeja to Lekki owing 170k with 20k of credit:
      Lekki owes Ikeja 150k.

    Every other kind has no moved items and ``net_owed`` is ``null``: what it
    moved is ``amount``. A move is party to both its branches, so a reader of
    either sees what it carried.
    """

    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    to_branch_name = serializers.CharField(source="to_branch.name", read_only=True)
    from_bank_account_name = serializers.CharField(source="from_bank_account.name", read_only=True, default=None)
    to_bank_account_name = serializers.CharField(source="to_bank_account.name", read_only=True, default=None)
    customer_name = serializers.CharField(source="customer.name", read_only=True, default=None)
    stage = serializers.CharField(read_only=True)
    journals = serializers.SerializerMethodField()
    moved_items = serializers.SerializerMethodField()
    net_owed = serializers.SerializerMethodField()

    class Meta:
        model = InterBranchTransfer
        fields = [
            "id", "document_number", "kind", "kind_label", "status", "stage",
            "branch_id", "branch_name", "to_branch_id", "to_branch_name",
            "amount", "transfer_date", "purpose", "reference", "repay_by",
            "from_bank_account_id", "from_bank_account_name",
            "to_bank_account_id", "to_bank_account_name",
            "customer_id", "customer_name", "held_receipt_id", "receipt_id", "recharge_id",
            "adjustment_entry_id",
            "requested_at", "sent_at", "received_at", "arrival_date",
            "declined_at", "decline_reason", "journals", "moved_items", "net_owed",
            "approval_state", "approval_returned",
        ]

    def get_journals(self, obj):
        return [
            {"role": leg.role, "branch_id": leg.branch_id, "journal_id": leg.journal_id}
            for leg in obj.legs.all()
        ]

    def _items(self, obj):
        if obj.kind != InterBranchTransferKind.RECEIVABLE:
            return []
        return list(obj.moved_items.all())

    def get_moved_items(self, obj):
        rows = []
        for item in self._items(obj):
            document = item.invoice or item.note or item.payment
            rows.append({
                "kind": item.kind,
                "document_number": getattr(document, "document_number", "") or "",
                "invoice_id": item.invoice_id,
                "note_id": item.note_id,
                "payment_id": item.payment_id,
                "amount": int(item.amount),
                "deferred_amount": int(item.deferred_amount),
            })
        return rows

    def get_net_owed(self, obj):
        from ..constants import ReceivableMoveItemKind as Kind

        if obj.kind != InterBranchTransferKind.RECEIVABLE:
            return None
        net = 0
        for item in self._items(obj):
            if item.kind in (Kind.INVOICE, Kind.DEBIT_NOTE):
                net += int(item.amount) - int(item.deferred_amount)
            else:
                net -= int(item.amount)
        if net == 0:
            return {"amount": 0, "owed_by": None, "owed_to": None}
        sender = {"id": obj.branch_id, "name": obj.branch.name}
        receiver = {"id": obj.to_branch_id, "name": obj.to_branch.name}
        owed_by, owed_to = (receiver, sender) if net > 0 else (sender, receiver)
        return {"amount": abs(net), "owed_by": owed_by, "owed_to": owed_to}


class HeldReceiptSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    for_branch_name = serializers.CharField(source="for_branch.name", read_only=True)
    bank_account_name = serializers.CharField(source="bank_account.name", read_only=True)
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    forwarded_by = serializers.SerializerMethodField()

    class Meta:
        model = HeldForBranchReceipt
        fields = [
            "id", "document_number", "status", "branch_id", "branch_name",
            "for_branch_id", "for_branch_name", "bank_account_id", "bank_account_name",
            "customer_id", "customer_name", "amount", "receipt_date", "method",
            "reference", "narration", "journal_id", "forwarded_by",
        ]

    def get_forwarded_by(self, obj):
        live = [t for t in obj.forwards.all() if t.status != DocumentStatus.REVERSED
                and t.status != DocumentStatus.CANCELLED]
        return ({"id": live[0].pk, "document_number": live[0].document_number,
                 "status": live[0].status} if live else None)


class RechargeSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source="branch.name", read_only=True)
    expense_account_code = serializers.CharField(source="expense_account.code", read_only=True)
    lines = serializers.SerializerMethodField()

    class Meta:
        model = InterBranchRecharge
        fields = [
            "id", "document_number", "status", "branch_id", "branch_name", "rule_id",
            "expense_account_id", "expense_account_code", "amount", "recharge_date",
            "basis", "narration", "reference", "lines",
        ]

    def get_lines(self, obj):
        """Each branch's share, narrowed to the branches the reader works in.

        Ikeja's bursar sees the share Ikeja carries, never Yaba's.
        """
        reach = self.context.get("reach")
        return [
            {"branch_id": line.branch_id, "branch_name": line.branch.name, "weight": line.weight,
             "amount": line.amount, "transfer_id": line.transfer_id}
            for line in obj.lines.all()
            if reach is None or line.branch_id in reach or obj.branch_id in reach
        ]


class SharedCostRuleSerializer(serializers.ModelSerializer):
    expense_account_code = serializers.CharField(source="expense_account.code", read_only=True, default=None)
    shares = serializers.SerializerMethodField()

    class Meta:
        model = SharedCostRule
        fields = [
            "id", "name", "treatment", "basis", "expense_account_id", "expense_account_code",
            "is_active", "shares",
        ]

    def get_shares(self, obj):
        return [
            {"branch_id": share.branch_id, "branch_name": share.branch.name,
             "percent": share.percent_bps / 100}
            for share in sorted(obj.shares.all(), key=lambda share: share.branch_id)
        ]


# --------------------------------------------------------------------------- #
# Transfers                                                                   #
# --------------------------------------------------------------------------- #

def _transfers_in_reach(request, entity):
    from ..inter_branch import transfers_visible_q

    moved = ReceivableTransferItem.objects.select_related("invoice", "note", "payment").only(
        "id", "transfer_id", "kind", "invoice_id", "note_id", "payment_id", "amount",
        "deferred_amount", "invoice__document_number", "note__document_number",
        "payment__document_number",
    ).order_by("kind", "id")
    return (
        InterBranchTransfer.objects
        .filter(transfers_visible_q(transaction_branch_scope(request)), entity=entity)
        .select_related("branch", "to_branch", "from_bank_account", "to_bank_account", "customer")
        .prefetch_related("legs", Prefetch("moved_items", queryset=moved))
    )


def _transfer_or_404(request, entity, pk):
    transfer = _transfers_in_reach(request, entity).filter(pk=pk).first()
    if transfer is None:
        raise NotFound("Inter-branch transfer not found for this entity.")
    return transfer


def _route(request, transfer, *, verb):
    """Send ``transfer`` through the sending branch's approval route, or post it.

    Steps on the route hold it for approval; an empty route needs
    ``confirm_without_approval``; no route sends it at once.
    """
    from ..approvals import approval_required, confirm_unconfigured_post
    from ..inter_branch import post_inter_branch_transfer

    if approval_required(transfer):
        from vs_workflow.services import release as release_svc
        from vs_workflow.services.submission import submit_for_approval

        instance = submit_for_approval(transfer, requested_by=request.user)
        transfer.refresh_from_db()
        return success_response(
            message=(
                f"Transfer {transfer.document_number} is waiting for approval at "
                f"{transfer.branch.name}. It is sent once it is approved."
            ),
            data=InterBranchTransferSerializer(transfer).data
            | {"approval": release_svc.approval_block(instance)},
            status=201,
        )
    confirm_unconfigured_post(transfer, request, noun="inter-branch transfer")
    post_inter_branch_transfer(transfer, actor_user=request.user)
    transfer.refresh_from_db()
    return success_response(
        message=f"{verb} {transfer.to_branch.name} as {transfer.document_number}.",
        data=InterBranchTransferSerializer(transfer).data, status=201,
    )


class InterBranchTransferListCreateView(_FinanceBase):
    """GET/POST /finance/inter-branch-transfers/?entity= - the register, and sending money.

    GET lists every transfer whose sending or receiving branch the caller works
    in, newest first. Filters: ``kind``, ``status``, ``branch`` (either side),
    ``counterparty`` (with ``branch``, the other side), ``date_from``/``date_to``,
    and ``adjustment`` (a journal id: the income the credit note or concession
    with that journal gave back).

    POST sends money unprompted from one of the caller's own branches' accounts:
    ``from_bank_account`` (id or name), ``to_branch``, optional ``to_bank_account``
    (the receiving branch's collection account when omitted), ``amount`` (kobo),
    ``transfer_date``, ``purpose``, optional ``repay_by`` and ``reference``. It
    follows the ``finance.inter_branch_transfer`` approval route of the sending
    branch, as a bank transfer follows its own.

    docstring-name: Inter-branch transfers
    """

    @property
    def rbac_permission(self):
        return "finance.interbranch.transfer" if self.request.method == "POST" \
            else "finance.interbranch.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _transfers_in_reach(request, entity)
        params = request.query_params
        if (kind := params.get("kind")):
            qs = qs.filter(kind=kind)
        if (status_ := params.get("status")):
            qs = qs.filter(status=status_)
        if (branch := params.get("branch")) and str(branch).isdigit():
            side = Q(branch_id=int(branch)) | Q(to_branch_id=int(branch))
            qs = qs.filter(side)
            if (other := params.get("counterparty")) and str(other).isdigit():
                qs = qs.filter(Q(branch_id=int(other)) | Q(to_branch_id=int(other)))
        if (start := _date(params.get("date_from"), "date_from")):
            qs = qs.filter(transfer_date__gte=start)
        if (end := _date(params.get("date_to"), "date_to")):
            qs = qs.filter(transfer_date__lte=end)
        if (adjustment := params.get("adjustment")) not in (None, ""):
            if not str(adjustment).isdigit():
                raise ValidationError({"adjustment": "Expected a journal id."})
            qs = qs.filter(adjustment_entry_id=int(adjustment))
        qs = filter_by_approval_param(qs, request.query_params)
        return self.paginate(request, qs.order_by("-transfer_date", "-id"), InterBranchTransferSerializer)

    def post(self, request):
        from ..inter_branch import require_several_branches, validate_money_transfer

        entity = resolve_entity(request)
        require_several_branches(entity)
        body = request.data or {}
        source = _bank_account_in_reach(request, entity, body.get("from_bank_account"), "from_bank_account")
        if source.branch_id is None:
            raise ValidationError({"from_bank_account": (
                f"{source.name} has not been given a branch, so no branch can send money from it."
            )})
        to_branch = _branch(entity, body.get("to_branch"), "to_branch")
        transfer = InterBranchTransfer(
            entity=entity, kind=InterBranchTransferKind.CASH, branch_id=source.branch_id,
            to_branch=to_branch, from_bank_account=source,
            to_bank_account=_receiving_account(entity, body.get("to_bank_account"), to_branch),
            amount=_kobo(body.get("amount")),
            transfer_date=_date(body.get("transfer_date"), "transfer_date")
            or branch_today(entity.tenant, source.branch_id),
            purpose=_text(body.get("purpose"), "purpose", 255, required=True,
                          message="Say what the money is for."),
            repay_by=_date(body.get("repay_by"), "repay_by"),
            reference=_text(body.get("reference"), "reference", 64),
            created_by=request.user,
        )
        validate_money_transfer(transfer)
        with transaction.atomic():
            transfer.save()
            return _route(request, transfer, verb="Sent to")


class InterBranchRequestView(_FinanceBase):
    """POST /finance/inter-branch-transfers/requests/?entity= - ask another branch for money.

    Body: ``from_branch`` (the branch asked), ``amount`` (kobo), ``purpose``,
    optional ``to_branch`` (the caller's own branch; required only for a caller
    who works in several), ``transfer_date``, ``repay_by`` and ``to_bank_account``
    (one of the caller's own branch's accounts). Nothing is booked until the
    branch asked sends it.

    docstring-name: Request money from another branch
    """

    rbac_permission = "finance.interbranch.request"

    def post(self, request):
        from ..inter_branch import request_cash_transfer, require_several_branches

        entity = resolve_entity(request)
        require_several_branches(entity)
        body = request.data or {}
        to_branch = _transaction_branch(request, entity, body, field="to_branch")
        from_branch = _branch(entity, body.get("from_branch"), "from_branch")
        account = None
        if body.get("to_bank_account") not in (None, ""):
            account = _bank_account_in_reach(request, entity, body.get("to_bank_account"), "to_bank_account")
        transfer = request_cash_transfer(
            entity, from_branch=from_branch, to_branch=to_branch, amount=_kobo(body.get("amount")),
            transfer_date=_date(body.get("transfer_date"), "transfer_date")
            or branch_today(entity.tenant, to_branch.pk),
            purpose=_text(body.get("purpose"), "purpose", 255, required=True,
                          message="Say what the money is for."),
            repay_by=_date(body.get("repay_by"), "repay_by"), to_bank_account=account,
            actor_user=request.user,
        )
        transfer = _transfer_or_404(request, entity, transfer.pk)
        return success_response(
            f"Asked {from_branch.name} for the money as {transfer.document_number}.",
            data=InterBranchTransferSerializer(transfer).data, status=201,
        )


class InterBranchTransferDetailView(_FinanceBase):
    """GET /finance/inter-branch-transfers/<id>/?entity= - one transfer."""

    rbac_permission = "finance.interbranch.view"

    def get(self, request, pk):
        transfer = _transfer_or_404(request, resolve_entity(request), pk)
        return success_response(
            "Inter-branch transfer retrieved.",
            data=with_approval_request(InterBranchTransferSerializer(transfer).data, transfer))


class InterBranchSendView(_FinanceBase):
    """POST /finance/inter-branch-transfers/<id>/send/?entity= - send a requested transfer.

    The sending branch meets a request: ``from_bank_account`` (one of its own
    accounts), optional ``to_bank_account`` (overrides the one asked for),
    ``transfer_date`` and ``confirm_without_approval``. Only a caller who works
    in the sending branch may send (403 otherwise).

    docstring-name: Send a requested inter-branch transfer
    """

    rbac_permission = "finance.interbranch.transfer"

    def post(self, request, pk):
        from ..inter_branch import validate_money_transfer

        entity = resolve_entity(request)
        body = request.data or {}
        with transaction.atomic():
            transfer = _transfer_or_404(request, entity, pk)
            inherited_branch_id(request, transfer)
            transfer = InterBranchTransfer.objects.select_for_update().get(pk=transfer.pk)
            if transfer.status != DocumentStatus.DRAFT or transfer.kind != InterBranchTransferKind.CASH:
                raise ValidationError({"status": "Only a request still waiting to be sent can be sent."})
            source = _bank_account_in_reach(request, entity, body.get("from_bank_account"), "from_bank_account")
            transfer.from_bank_account = source
            if body.get("to_bank_account") not in (None, "") or transfer.to_bank_account_id is None:
                transfer.to_bank_account = _receiving_account(
                    entity, body.get("to_bank_account"), transfer.to_branch)
            if (on := _date(body.get("transfer_date"), "transfer_date")):
                transfer.transfer_date = on
            validate_money_transfer(transfer)
            transfer.save(update_fields=[
                "from_bank_account", "to_bank_account", "transfer_date", "updated_at"])
            return _route(request, transfer, verb="Sent to")


class InterBranchDeclineView(_FinanceBase):
    """POST /finance/inter-branch-transfers/<id>/decline/?entity= - decline a request.

    Optional body ``reason``. Only a caller who works in the branch asked may decline.

    docstring-name: Decline an inter-branch request
    """

    rbac_permission = "finance.interbranch.transfer"

    def post(self, request, pk):
        from ..inter_branch import decline_request

        entity = resolve_entity(request)
        transfer = _transfer_or_404(request, entity, pk)
        inherited_branch_id(request, transfer)
        decline_request(transfer, reason=_text((request.data or {}).get("reason"), "reason", 255),
                        actor_user=request.user)
        transfer = _transfer_or_404(request, entity, pk)
        return success_response(f"Request {transfer.document_number} declined.",
                                data=InterBranchTransferSerializer(transfer).data)


class InterBranchConfirmView(_FinanceBase):
    """POST /finance/inter-branch-transfers/<id>/confirm/?entity= - confirm the money arrived.

    Optional body ``arrival_date``. Only a caller who works in the receiving
    branch may confirm.

    docstring-name: Confirm an inter-branch transfer arrived
    """

    rbac_permission = "finance.interbranch.confirm"

    def post(self, request, pk):
        from ..inter_branch import confirm_arrival

        entity = resolve_entity(request)
        transfer = _transfer_or_404(request, entity, pk)
        inherited_branch_id(request, transfer, field="to_branch")
        confirm_arrival(
            transfer, arrival_date=_date((request.data or {}).get("arrival_date"), "arrival_date"),
            actor_user=request.user,
        )
        transfer = _transfer_or_404(request, entity, pk)
        return success_response(f"Transfer {transfer.document_number} confirmed as arrived.",
                                data=InterBranchTransferSerializer(transfer).data)


class InterBranchVoidView(_FinanceBase):
    """POST /finance/inter-branch-transfers/<id>/void/?entity= - reverse both sides.

    Voiding touches both branches' books, so the caller must work in both (403
    otherwise). Refused while either bank side is matched on a reconciliation.
    Optional body ``date`` dates the reversals.

    docstring-name: Void an inter-branch transfer
    """

    rbac_permission = "finance.interbranch.reverse"

    def post(self, request, pk):
        from ..inter_branch import void_inter_branch_transfer

        entity = resolve_entity(request)
        transfer = _transfer_or_404(request, entity, pk)
        if not caller_may_change(request.user, entity.tenant, (transfer.branch_id, transfer.to_branch_id)):
            raise PermissionDenied(
                f"Voiding this transfer changes both {transfer.branch.name}'s and "
                f"{transfer.to_branch.name}'s books, so it needs somebody who works in both.",
            )
        void_inter_branch_transfer(
            transfer, actor_user=request.user, date=_date((request.data or {}).get("date"), "date"),
        )
        transfer = _transfer_or_404(request, entity, pk)
        return success_response(f"Transfer {transfer.document_number} voided.",
                                data=InterBranchTransferSerializer(transfer).data)


class InterBranchBalancesView(_FinanceBase):
    """GET /finance/inter-branch-balances/?entity= - who owes whom between branches.

    ``pairs`` lists each pair of branches once ("Lekki owes Ikeja N"), ``held``
    the money one branch holds for another, and ``net_total`` (whole-tenant
    readers only) the inter-branch account across every branch, which is zero.
    A branch-bound reader sees only the pairs their own branches are part of.

    docstring-name: Inter-branch balances
    """

    rbac_permission = "finance.interbranch.view"

    def get(self, request):
        from ..inter_branch import pair_balances

        entity = resolve_entity(request)
        reach = _reach(request)
        return success_response(
            "Inter-branch balances retrieved.",
            data=pair_balances(entity, branch_ids=None if reach is None else set(reach)),
        )


class InterBranchReceivableMoveView(_FinanceBase):
    """POST /finance/inter-branch-transfers/receivable-moves/?entity= - move a customer's open balance.

    Body: ``customer`` (code or id), ``from_branch``, ``to_branch``, optional
    ``move_date``, ``purpose`` and ``move_key``. The customer's open invoices and
    debit notes, unapplied credit and income not yet earned at ``from_branch``
    move to ``to_branch``
    (:func:`vs_finance.inter_branch.transfer_open_receivables`). It binds two
    branches' books and another branch's lists, so only a caller who reaches
    the whole tenant may do it.

    docstring-name: Move a customer's open balance to another branch
    """

    rbac_permission = "finance.interbranch.transfer"

    def post(self, request):
        from ..inter_branch import require_several_branches, transfer_open_receivables

        entity = resolve_entity(request)
        if not caller_reaches_whole_tenant(request.user, entity.tenant):
            raise PermissionDenied(shared_write_refusal("where a customer's open balance is kept"))
        require_several_branches(entity)
        body = request.data or {}
        ref = body.get("customer")
        if ref in (None, ""):
            raise ValidationError({"customer": "Name the customer."})
        qs = Customer.objects.filter(entity=entity)
        customer = qs.filter(code=str(ref).upper()).first() or (
            qs.filter(pk=int(ref)).first() if str(ref).isdigit() else None)
        if customer is None:
            raise NotFound(f"No customer matches '{ref}' for this entity.")
        with transaction.atomic():
            moved = transfer_open_receivables(
                customer, _branch(entity, body.get("from_branch"), "from_branch"),
                _branch(entity, body.get("to_branch"), "to_branch"), request.user,
                move_date=_date(body.get("move_date"), "move_date"),
                move_key=_text(body.get("move_key"), "move_key", 96),
                purpose=_text(body.get("purpose"), "purpose", 255),
            )
        data = {
            "transfer_id": moved.transfer_id, "amount": moved.amount,
            "invoice_count": moved.invoice_count, "invoice_ids": list(moved.invoice_ids),
            "debit_note_count": moved.debit_note_count, "credit_count": moved.credit_count,
            "credit_amount": moved.credit_amount, "deferred_amount": moved.deferred_amount,
        }
        if moved.transfer_id is None:
            return success_response("Nothing was open at that branch, so nothing moved.", data=data)
        return success_response("The customer's balance moved to the new branch.", data=data, status=201)


# --------------------------------------------------------------------------- #
# Money held for another branch                                               #
# --------------------------------------------------------------------------- #

def _held_in_reach(request, entity):
    scope = transaction_branch_scope(request)
    visible = Q() if not scope.is_narrowed else scope.q() | scope.q(field="for_branch")
    return (
        HeldForBranchReceipt.objects.filter(visible, entity=entity)
        .select_related("branch", "for_branch", "bank_account", "customer")
        .prefetch_related("forwards")
    )


def _held_or_404(request, entity, pk):
    held = _held_in_reach(request, entity).filter(pk=pk).first()
    if held is None:
        raise NotFound("Held receipt not found for this entity.")
    return held


class HeldReceiptListCreateView(_FinanceBase):
    """GET/POST /finance/held-receipts/?entity= - money received for another branch.

    POST body: ``bank_account`` (one of the caller's own branches', id or name),
    ``for_branch``, ``customer`` (the other branch's customer, or one every branch
    shares; code or id), ``amount`` (kobo), ``receipt_date``, optional ``method``,
    ``reference`` and ``narration``. Booked ``Dr bank, Cr held for other branches``
    at once; forward it to settle the customer's bill at their branch. GET lists
    receipts held by or for the caller's branches.

    docstring-name: Receipts held for another branch
    """

    @property
    def rbac_permission(self):
        return "finance.payment.create" if self.request.method == "POST" else "finance.payment.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _held_in_reach(request, entity)
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        return self.paginate(request, qs.order_by("-receipt_date", "-id"), HeldReceiptSerializer)

    def post(self, request):
        from ..inter_branch import post_held_receipt, require_several_branches, validate_held_receipt

        entity = resolve_entity(request)
        require_several_branches(entity)
        body = request.data or {}
        bank = _bank_account_in_reach(request, entity, body.get("bank_account"), "bank_account")
        for_branch = _branch(entity, body.get("for_branch"), "for_branch")
        method = body.get("method") or PaymentMethod.BANK_TRANSFER
        if method not in PaymentMethod.values:
            raise ValidationError({"method": "Unknown payment method."})
        held = HeldForBranchReceipt(
            entity=entity, branch_id=bank.branch_id, bank_account=bank, for_branch=for_branch,
            customer=_customer_for_branch(entity, body.get("customer"), for_branch),
            amount=_kobo(body.get("amount")),
            receipt_date=_date(body.get("receipt_date"), "receipt_date", required=True),
            method=method, reference=_text(body.get("reference"), "reference", 64),
            narration=_text(body.get("narration"), "narration", 255), created_by=request.user,
        )
        validate_held_receipt(held)
        with transaction.atomic():
            held.save()
            post_held_receipt(held, actor_user=request.user)
        held = _held_or_404(request, entity, held.pk)
        return success_response(
            f"Held for {for_branch.name} as {held.document_number}.",
            data=HeldReceiptSerializer(held).data, status=201,
        )


class HeldReceiptCustomerLookupView(_FinanceBase):
    """GET /finance/held-receipts/customer-lookup/?entity=&for_branch=&code= - whose money it is.

    Mrs Adeyemi pays her son's Lekki fees into Ikeja's account, and Ikeja's
    bursar, who cannot list Lekki's customers, records it as held for Lekki. This
    answers the one question that form asks before posting: which customer does
    the code on her teller name at Lekki? It returns ``id``, ``code``, ``name``
    and ``branch_id`` for an exact code of ``for_branch``'s customer, or of one
    every branch shares, and nothing else: no balance, contact or invoice.

    The exposure is the one the POST already makes: it accepts the same code at
    the same branch and echoes the customer's name back. An exact code only,
    never a search, so another branch's customer list cannot be walked from here.
    A code that names no such customer is a 404, the same as one at another
    tenant. Gated on ``finance.payment.create``, the key that records the money.

    docstring-name: Find a customer for a held receipt
    """

    rbac_permission = "finance.payment.create"

    def get(self, request):
        from ..inter_branch import require_several_branches

        entity = resolve_entity(request)
        require_several_branches(entity)
        params = request.query_params
        for_branch = _branch(entity, params.get("for_branch"), "for_branch")
        code = str(params.get("code") or "").strip().upper()
        if not code:
            raise ValidationError({"code": "Give the customer's code."})
        customer = (
            Customer.objects.filter(entity=entity, code=code)
            .filter(Q(branch=for_branch) | Q(branch__isnull=True))
            .values("id", "code", "name", "branch_id")
            .first()
        )
        if customer is None:
            raise NotFound(f"No customer '{code}' of {for_branch.name}.")
        return success_response("Customer found.", data=customer)


class HeldReceiptDetailView(_FinanceBase):
    """GET /finance/held-receipts/<id>/?entity= - one held receipt."""

    rbac_permission = "finance.payment.view"

    def get(self, request, pk):
        held = _held_or_404(request, resolve_entity(request), pk)
        return success_response("Held receipt retrieved.", data=HeldReceiptSerializer(held).data)


class HeldReceiptForwardView(_FinanceBase):
    """POST /finance/held-receipts/<id>/forward/?entity= - forward it to its branch.

    Body: optional ``to_bank_account`` (the receiving branch's collection account
    when omitted), ``from_bank_account`` (the account that received it when
    omitted), ``transfer_date``, ``purpose`` and ``confirm_without_approval``.
    The branch that holds the money sends it, through the same approval route as
    a cash transfer; at the other branch it is receipted against the customer and
    settles their bills there.

    docstring-name: Forward a held receipt
    """

    rbac_permission = "finance.interbranch.transfer"

    def post(self, request, pk):
        from ..inter_branch import forward_held_receipt

        entity = resolve_entity(request)
        body = request.data or {}
        held = _held_or_404(request, entity, pk)
        inherited_branch_id(request, held)
        source = None
        if body.get("from_bank_account") not in (None, ""):
            source = _bank_account_in_reach(request, entity, body.get("from_bank_account"), "from_bank_account")
        with transaction.atomic():
            held = HeldForBranchReceipt.objects.select_for_update().get(pk=held.pk)
            transfer = forward_held_receipt(
                held, from_bank_account=source,
                to_bank_account=_receiving_account(entity, body.get("to_bank_account"), held.for_branch),
                transfer_date=_date(body.get("transfer_date"), "transfer_date")
                or branch_today(entity.tenant, held.branch_id),
                purpose=_text(body.get("purpose"), "purpose", 255), actor_user=request.user,
            )
            return _route(request, transfer, verb="Forwarded to")


class HeldReceiptVoidView(_FinanceBase):
    """POST /finance/held-receipts/<id>/void/?entity= - reverse a receipt never forwarded.

    Only a caller who works in the branch that received it may void it.

    docstring-name: Void a held receipt
    """

    rbac_permission = "finance.payment.reverse"

    def post(self, request, pk):
        from ..inter_branch import void_held_receipt

        entity = resolve_entity(request)
        held = _held_or_404(request, entity, pk)
        inherited_branch_id(request, held)
        void_held_receipt(held, actor_user=request.user,
                          date=_date((request.data or {}).get("date"), "date"))
        held = _held_or_404(request, entity, pk)
        return success_response(f"Held receipt {held.document_number} voided.",
                                data=HeldReceiptSerializer(held).data)


# --------------------------------------------------------------------------- #
# Recharges and the tenant's shared-cost rules                                #
# --------------------------------------------------------------------------- #

def _recharges_in_reach(request, entity):
    scope = transaction_branch_scope(request)
    qs = InterBranchRecharge.objects.filter(entity=entity)
    if scope.is_narrowed:
        qs = qs.filter(scope.q() | scope.q("lines__")).distinct()
    return qs.select_related("branch", "expense_account").prefetch_related("lines__branch")


def _recharge_or_404(request, entity, pk):
    recharge = _recharges_in_reach(request, entity).filter(pk=pk).first()
    if recharge is None:
        raise NotFound("Recharge not found for this entity.")
    return recharge


def _weights(body, basis):
    """``{branch_id: weight}`` from ``weights``: ``[{branch, count}]`` or ``[{branch, percent}]``."""
    from decimal import Decimal, InvalidOperation

    raw = body.get("weights")
    if raw in (None, "", []):
        return None
    if not isinstance(raw, list):
        raise ValidationError({"weights": "Give a list of {branch, count} or {branch, percent}."})
    weights = {}
    for row in raw:
        if not isinstance(row, dict) or not str(row.get("branch", "")).isdigit():
            raise ValidationError({"weights": "Each weight names a branch by id."})
        branch_id = int(row["branch"])
        if branch_id in weights:
            raise ValidationError({"weights": "Each branch is weighed once."})
        if basis == RechargeBasis.PERCENTAGES:
            try:
                bps = Decimal(str(row.get("percent"))) * 100
            except InvalidOperation:
                raise ValidationError({"weights": "Each percentage is a number."})
            if bps != bps.to_integral_value() or bps < 0:
                raise ValidationError({"weights": "Percentages go to two decimal places at most."})
            weights[branch_id] = int(bps)
        else:
            count = row.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValidationError({"weights": "Each count is a whole number, zero or more."})
            weights[branch_id] = count
    return weights


class RechargeListCreateView(_FinanceBase):
    """GET/POST /finance/recharges/?entity= - recharge a shared cost to other branches.

    POST body: ``branch`` (the paying branch; implied for a caller who works in
    one), ``amount`` (kobo, the whole cost), ``recharge_date``, ``narration``,
    ``expense_account`` (code or id; the rule's when omitted), optional ``rule``,
    ``basis`` (``COUNTS``, the default, or ``PERCENTAGES``) and ``weights``:
    ``[{"branch": id, "count": n}]`` or ``[{"branch": id, "percent": 40}]``,
    totalling 100 for percentages, which a rule's own percentages replace when
    omitted. GET lists recharges paid or shared by the caller's branches, each
    showing only the shares of branches the caller works in.

    docstring-name: Shared cost recharges
    """

    @property
    def rbac_permission(self):
        return "finance.interbranch.recharge" if self.request.method == "POST" \
            else "finance.interbranch.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _recharges_in_reach(request, entity).order_by("-recharge_date", "-id")
        return self.paginate(request, qs, RechargeSerializer,
                             context={"request": request, "reach": _reach(request)})

    def post(self, request):
        from ..inter_branch import require_several_branches, run_recharge

        entity = resolve_entity(request)
        require_several_branches(entity)
        body = request.data or {}
        payer = _transaction_branch(request, entity, body, field="branch")
        rule = None
        if body.get("rule") not in (None, ""):
            rule = SharedCostRule.objects.filter(
                entity=entity, pk=int(body["rule"]) if str(body["rule"]).isdigit() else 0,
            ).prefetch_related("shares").first()
            if rule is None:
                raise ValidationError({"rule": "No such shared cost rule."})
        basis = body.get("basis") or (rule.basis if rule else RechargeBasis.COUNTS)
        if basis not in RechargeBasis.values:
            raise ValidationError({"basis": "Choose COUNTS or PERCENTAGES."})
        recharge = run_recharge(
            entity, paying_branch=payer,
            expense_account=_resolve_account(request, entity, body.get("expense_account"), "expense_account"),
            amount=_kobo(body.get("amount")),
            recharge_date=_date(body.get("recharge_date"), "recharge_date")
            or branch_today(entity.tenant, payer.pk),
            narration=_text(body.get("narration"), "narration", 255, required=True,
                            message="Say what the cost was."),
            basis=basis, weights=_weights(body, basis), rule=rule,
            reference=_text(body.get("reference"), "reference", 64), actor_user=request.user,
        )
        recharge = _recharge_or_404(request, entity, recharge.pk)
        return success_response(
            f"Recharged as {recharge.document_number}.",
            data=RechargeSerializer(recharge, context={"reach": _reach(request)}).data, status=201,
        )


class RechargeDetailView(_FinanceBase):
    """GET /finance/recharges/<id>/?entity= - one recharge."""

    rbac_permission = "finance.interbranch.view"

    def get(self, request, pk):
        recharge = _recharge_or_404(request, resolve_entity(request), pk)
        return success_response("Recharge retrieved.",
                                data=RechargeSerializer(recharge, context={"reach": _reach(request)}).data)


class RechargeVoidView(_FinanceBase):
    """POST /finance/recharges/<id>/void/?entity= - void a recharge and all its shares.

    It changes every sharing branch's books, so the caller must work in all of them.

    docstring-name: Void a recharge
    """

    rbac_permission = "finance.interbranch.reverse"

    def post(self, request, pk):
        from ..inter_branch import void_recharge

        entity = resolve_entity(request)
        recharge = _recharge_or_404(request, entity, pk)
        branches = {recharge.branch_id, *(line.branch_id for line in recharge.lines.all() if line.transfer_id)}
        if not caller_may_change(request.user, entity.tenant, branches):
            raise PermissionDenied("Voiding this recharge changes every sharing branch's books, "
                                   "so it needs somebody who works in all of them.")
        void_recharge(recharge, actor_user=request.user,
                      date=_date((request.data or {}).get("date"), "date"))
        recharge = _recharge_or_404(request, entity, pk)
        return success_response(f"Recharge {recharge.document_number} voided.",
                                data=RechargeSerializer(recharge, context={"reach": _reach(request)}).data)


def _apply_rule(request, entity, rule, body, *, creating):
    """Write the fields ``body`` names onto ``rule`` and replace its percentages if given."""
    from ..audit import record
    from ..constants import AccountType, FinanceAuditAction

    if creating or "name" in body:
        rule.name = _text(body.get("name"), "name", 120, required=True, message="Name the cost.")
    if creating or "treatment" in body:
        treatment = body.get("treatment") or SharedCostTreatment.ABSORB
        if treatment not in SharedCostTreatment.values:
            raise ValidationError({"treatment": "Choose ABSORB or RECHARGE."})
        rule.treatment = treatment
    if creating or "basis" in body:
        basis = body.get("basis") or RechargeBasis.COUNTS
        if basis not in RechargeBasis.values:
            raise ValidationError({"basis": "Choose COUNTS or PERCENTAGES."})
        rule.basis = basis
    if "expense_account" in body:
        account = _resolve_account(request, entity, body.get("expense_account"), "expense_account")
        if account is not None and account.account_type != AccountType.EXPENSE:
            raise ValidationError({"expense_account": "Choose an expense account."})
        rule.expense_account = account
    if "is_active" in body:
        if not isinstance(body["is_active"], bool):
            raise ValidationError({"is_active": "Expected true or false."})
        rule.is_active = body["is_active"]
    if SharedCostRule.objects.filter(entity=entity, name=rule.name).exclude(pk=rule.pk).exists():
        raise ValidationError({"name": "A rule with this name already exists."})
    shares = None
    if "shares" in body:
        shares = _weights({"weights": body.get("shares")}, RechargeBasis.PERCENTAGES) or {}
        if shares and sum(shares.values()) != 10000:
            raise ValidationError({"shares": "Fixed percentages must total 100."})
        from vs_tenants.models import Branch

        if set(shares) - set(Branch.all_objects.filter(tenant_id=entity.tenant_id).values_list("pk", flat=True)):
            raise ValidationError({"shares": "A share names a branch outside these books."})
    with transaction.atomic():
        rule.save()
        if shares is not None:
            rule.shares.all().delete()
            SharedCostRuleShare.objects.bulk_create([
                SharedCostRuleShare(rule=rule, branch_id=branch_id, percent_bps=bps)
                for branch_id, bps in sorted(shares.items())
            ])
        record(
            entity=entity, action=FinanceAuditAction.SHARED_COST_RULE_CHANGED,
            actor_user=request.user, target_type="SharedCostRule", target_id=str(rule.pk),
            branch=None, message=f"Shared cost rule {rule.name}: {rule.get_treatment_display()}.",
            treatment=rule.treatment, basis=rule.basis,
        )
    return rule


class SharedCostRuleListCreateView(WholeTenantWriteMixin, _FinanceBase):
    """GET/POST /finance/shared-cost-rules/?entity= - the tenant's choice per shared cost.

    POST body: ``name``, ``treatment`` (``ABSORB``, the default, or ``RECHARGE``),
    ``basis`` (``COUNTS``, the default, or ``PERCENTAGES``), optional
    ``expense_account`` and ``shares`` (``[{"branch": id, "percent": 40}]``,
    totalling 100). Configuration every branch shares, so only a caller who
    reaches the whole tenant may write it.

    docstring-name: Shared cost rules
    """

    shared_subject = "the shared cost rules"

    @property
    def rbac_permission(self):
        return "finance.interbranch.recharge" if self.request.method == "POST" \
            else "finance.interbranch.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = SharedCostRule.objects.filter(entity=entity).select_related(
            "expense_account").prefetch_related("shares__branch")
        return self.paginate(request, qs.order_by("name"), SharedCostRuleSerializer)

    def post(self, request):
        entity = resolve_entity(request)
        rule = _apply_rule(request, entity, SharedCostRule(entity=entity, created_by=request.user),
                           request.data or {}, creating=True)
        return success_response(f"Shared cost rule {rule.name} saved.",
                                data=SharedCostRuleSerializer(rule).data, status=201)


class SharedCostRuleDetailView(WholeTenantWriteMixin, _FinanceBase):
    """GET/PATCH /finance/shared-cost-rules/<id>/?entity= - one rule."""

    shared_subject = "the shared cost rules"

    @property
    def rbac_permission(self):
        return "finance.interbranch.recharge" if self.request.method == "PATCH" \
            else "finance.interbranch.view"

    def _rule(self, request, pk):
        rule = SharedCostRule.objects.filter(entity=resolve_entity(request), pk=pk).first()
        if rule is None:
            raise NotFound("Shared cost rule not found for this entity.")
        return rule

    def get(self, request, pk):
        return success_response("Shared cost rule retrieved.",
                                data=SharedCostRuleSerializer(self._rule(request, pk)).data)

    def patch(self, request, pk):
        rule = self._rule(request, pk)
        rule = _apply_rule(request, rule.entity, rule, request.data or {}, creating=False)
        return success_response(f"Shared cost rule {rule.name} saved.",
                                data=SharedCostRuleSerializer(rule).data)
