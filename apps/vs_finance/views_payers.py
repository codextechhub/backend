"""Payers and the payments they make for several customers at once.

``/finance/payer-links/`` says which customers a payer pays for, and
``/finance/payer-payments/`` records one payment from a payer and splits it into
one receipt per customer (:mod:`vs_finance.payer_payments`).

Who may act:

* a payment is recorded into one of the caller's own branches' bank accounts,
  and that bank's branch is the document's. Every share whose bills sit at that
  branch is a receipt there; every share whose bills sit at another branch is
  money held for that branch, which the caller may book without reaching it, as
  any held receipt is;
* a payment is read by a caller who reaches the branch that received it, who
  sees every share, or by one who reaches a branch a share is held for, who sees
  only their own branches' shares;
* a void needs the branch that received it, because every document it voids is
  booked there;
* a caller sees the bills behind a share only for branches they reach. For any
  other branch they see the customer and the amount, never that branch's bills.

The keys are the receipt keys (``finance.payment.view``, ``.create``,
``.reverse``), because a payer payment is nothing but receipts and held receipts
booked together. Links are customer records: ``finance.customer.view`` to read
and ``finance.customer.update`` to change.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_rbac.scoping import caller_branch_ids, inherited_branch_id, transaction_branch_scope

from .constants import DocumentStatus, PaymentMethod
from .models import CreditNote, PayerLink, PayerPayment
from .views import resolve_entity
from .views_ar import _resolve_customer
from .views_ops.base import (
    _FinanceBase,
    _bank_account_in_reach,
    _date,
    _filter_by_branch,
    _money,
)

__all__ = [
    "PayerLinkDetailView",
    "PayerLinkListCreateView",
    "PayerPaymentDetailView",
    "PayerPaymentListCreateView",
    "PayerPaymentPreviewView",
    "PayerPaymentVoidView",
]


def _reaches(reach, branch_id) -> bool:
    return reach is None or branch_id in reach


# --------------------------------------------------------------------------- #
# Links                                                                       #
# --------------------------------------------------------------------------- #

def _link_row(link):
    return {
        "id": link.pk,
        "payer": {"id": link.payer_id, "code": link.payer.code, "name": link.payer.name},
        "customer": {"id": link.customer_id, "code": link.customer.code, "name": link.customer.name},
        "is_active": link.is_active,
        "source_type": link.source_type,
        "source_id": link.source_id,
    }


def _links_in_reach(request, entity):
    """Links whose payer and customer the caller can both see (shared ones included)."""
    from vs_rbac.scoping import branch_q

    return (
        PayerLink.objects.filter(entity=entity)
        .filter(branch_q(request, "payer__", include_shared=True))
        .filter(branch_q(request, "customer__", include_shared=True))
        .select_related("payer", "customer")
    )


class PayerLinkListCreateView(_FinanceBase):
    """GET/POST /finance/payer-links/?entity= - who pays for whom.

    GET lists links, narrowed by ``?payer=`` or ``?customer=`` (code or id) and
    ``?is_active=``. POST ``{payer, customer}`` (codes or ids) records that the
    payer pays for the customer; linking a pair already linked changes nothing,
    and linking an ended pair switches it back on. Both must be customers the
    caller can see.

    docstring-name: Payer links
    """

    @property
    def rbac_permission(self):
        return "finance.customer.update" if self.request.method == "POST" else "finance.customer.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _links_in_reach(request, entity)
        params = request.query_params
        if params.get("payer"):
            qs = qs.filter(payer=_resolve_customer(request, entity, params.get("payer"), "payer"))
        if params.get("customer"):
            qs = qs.filter(customer=_resolve_customer(request, entity, params.get("customer")))
        if (active := params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        rows = [_link_row(link) for link in qs.order_by("payer__code", "customer__code")[:500]]
        return success_response("Payer links retrieved.", data=rows)

    def post(self, request):
        from .payer_payments import link_customer

        entity = resolve_entity(request)
        body = request.data or {}
        payer = _resolve_customer(request, entity, body.get("payer"), "payer")
        customer = _resolve_customer(request, entity, body.get("customer"))
        link = link_customer(
            payer, customer, actor_user=request.user,
            source_type=str(body.get("source_type") or ""), source_id=str(body.get("source_id") or ""),
        )
        return success_response(
            f"{payer.name} pays for {customer.name}.", data=_link_row(link), status=201,
        )


class PayerLinkDetailView(_FinanceBase):
    """DELETE /finance/payer-links/<id>/?entity= - end a link; payments already made keep their shares.

    docstring-name: Payer links
    """

    rbac_permission = "finance.customer.update"

    def delete(self, request, pk):
        from .payer_payments import unlink_customer

        entity = resolve_entity(request)
        link = _links_in_reach(request, entity).filter(pk=pk).first()
        if link is None:
            raise NotFound("Payer link not found for this entity.")
        unlink_customer(link, actor_user=request.user)
        return success_response(
            f"{link.payer.name} no longer pays for {link.customer.name}.", data=_link_row(link),
        )


# --------------------------------------------------------------------------- #
# Payments                                                                    #
# --------------------------------------------------------------------------- #

def _shares_from_body(entity, payer, raw):
    """``[{customer, amount}]`` from the body as ``[(customer, kobo)]``, or None.

    A customer is looked up among those the payer pays for only, by code or id,
    so a branch-bound bursar may name a child billed at another branch (the money
    is held for that branch) and nobody may name a customer the payer does not
    pay for. An unknown customer and an unlinked one read the same.
    """
    from .payer_payments import payer_customers

    if raw in (None, ""):
        return None
    if not isinstance(raw, list) or not raw:
        raise ValidationError({"shares": "Give a list of {customer, amount}."})
    allowed = payer_customers(payer)
    by_code = {c.code: c for c in allowed}
    by_id = {c.pk: c for c in allowed}
    shares = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError({"shares": "Give a list of {customer, amount}."})
        ref = str(item.get("customer") or "").strip()
        customer = by_code.get(ref.upper()) or (by_id.get(int(ref)) if ref.isdigit() else None)
        if customer is None:
            raise ValidationError({f"shares[{index}].customer": (
                f"No customer '{ref}' that {payer.name} pays for."
            )})
        shares.append((customer, _money(item.get("amount"), f"shares[{index}].amount")))
    return shares


def _payment_inputs(request, entity):
    body = request.data or {}
    payer = _resolve_customer(request, entity, body.get("payer"), "payer")
    bank = _bank_account_in_reach(request, entity, body.get("bank_account"), "bank_account")
    method = body.get("method") or PaymentMethod.BANK_TRANSFER
    if method not in PaymentMethod.values:
        raise ValidationError({"method": "Unknown payment method."})
    return {
        "payer": payer,
        "bank_account": bank,
        "amount": _money(body.get("amount"), "amount"),
        "payment_date": _date(body.get("payment_date"), "payment_date", required=True),
        "method": method,
        "reference": str(body.get("reference") or "")[:64],
        "narration": str(body.get("narration") or "")[:255],
        "shares": _shares_from_body(entity, payer, body.get("shares")),
        "split": body.get("split") or None,
    }


def _branch_names(ids):
    from vs_tenants.models import Branch

    return dict(Branch.all_objects.filter(pk__in=ids).values_list("pk", "name"))


def _plan_payload(request, plan):
    """The proposal, with bill detail only for the branches the caller reaches."""
    reach = caller_branch_ids(request)
    shares = plan.ordered_shares()
    names = _branch_names({plan.branch_id, *(s.branch_id for s in shares)})
    rows = []
    for share in shares:
        visible = _reaches(reach, share.branch_id)
        row = {
            "customer": {"id": share.customer.pk, "code": share.customer.code, "name": share.customer.name},
            "branch_id": share.branch_id,
            "branch_name": names.get(share.branch_id, ""),
            "kind": "RECEIPT" if share.branch_id == plan.branch_id else "HELD",
            "amount": share.amount,
            "credit": share.surplus,
        }
        if visible:
            row["outstanding"] = plan.outstanding.get((share.customer.pk, share.branch_id), 0)
            row["bills"] = [{
                "id": target.pk,
                "document_number": target.document_number,
                "kind": "DEBIT_NOTE" if isinstance(target, CreditNote) else "INVOICE",
                "balance": int(target.balance_due),
                "applied": int(applied),
            } for target, applied in share.items]
        rows.append(row)
    return {
        "payer": {"id": plan.payer.pk, "code": plan.payer.code, "name": plan.payer.name},
        "bank_account": {"id": plan.bank_account.pk, "name": plan.bank_account.name},
        "branch_id": plan.branch_id,
        "branch_name": names.get(plan.branch_id, ""),
        "amount": plan.amount,
        "payment_date": plan.payment_date.isoformat(),
        "split": plan.split,
        "shares": rows,
    }


def _payment_payload(request, document):
    """One payment; a caller outside the receiving branch sees only their branches' shares."""
    reach = caller_branch_ids(request)
    whole = _reaches(reach, document.branch_id)
    rows = []
    for share in document.shares.all():
        if not whole and not _reaches(reach, share.branch_id):
            continue
        source = share.receipt or share.held_receipt
        row = {
            "id": share.pk,
            "customer": {"id": share.customer_id, "code": share.customer.code, "name": share.customer.name},
            "branch_id": share.branch_id,
            "branch_name": share.branch.name,
            "kind": "RECEIPT" if share.receipt_id else "HELD",
            "amount": int(share.amount),
            "credit": int(share.surplus),
            "document": {"id": source.pk, "document_number": source.document_number,
                         "status": source.status},
        }
        if share.held_receipt_id:
            live = [t for t in share.held_receipt.forwards.all()
                    if t.status not in (DocumentStatus.REVERSED, DocumentStatus.CANCELLED)]
            row["forwarded_by"] = (
                {"id": live[0].pk, "document_number": live[0].document_number, "status": live[0].status}
                if live else None
            )
        if share.receipt_id:
            row["bills"] = [{
                "invoice_id": allocation.invoice_id,
                "document_number": allocation.invoice.document_number,
                "applied": int(allocation.amount),
            } for allocation in share.receipt.allocations.all()]
        rows.append(row)
    return {
        "id": document.pk,
        "document_number": document.document_number,
        "status": document.status,
        "payer": {"id": document.payer_id, "code": document.payer.code, "name": document.payer.name},
        "branch_id": document.branch_id,
        "branch_name": document.branch.name if document.branch_id else "",
        "bank_account": {"id": document.bank_account_id, "name": document.bank_account.name},
        "amount": int(document.amount) if whole else sum(r["amount"] for r in rows),
        "payment_date": document.payment_date.isoformat(),
        "method": document.method,
        "split": document.split,
        "reference": document.reference,
        "narration": document.narration,
        "shares": rows,
    }


def _payments_in_reach(request, entity):
    """Payments received by the caller's branches, or holding money for them."""
    scope = transaction_branch_scope(request)
    visible = Q() if not scope.is_narrowed else scope.q() | scope.q("shares__")
    ids = PayerPayment.objects.filter(visible, entity=entity).values("pk")
    return (
        PayerPayment.objects.filter(pk__in=ids)
        .select_related("payer", "branch", "bank_account")
        .prefetch_related(
            "shares__customer", "shares__branch", "shares__receipt__allocations__invoice",
            "shares__held_receipt__forwards",
        )
    )


def _payment_or_404(request, entity, pk):
    document = _payments_in_reach(request, entity).filter(pk=pk).first()
    if document is None:
        raise NotFound("Payment not found for this entity.")
    return document


class PayerPaymentPreviewView(_FinanceBase):
    """POST /finance/payer-payments/preview/?entity= - how a payment would be split, writing nothing.

    Same body as recording one. Answers each share: the customer, the branch its
    bills sit at, whether it is a receipt here or money held for that branch, the
    amount and the part left as credit, and, for branches the caller reaches, the
    bills it settles.

    docstring-name: Preview a payment from a payer
    """

    rbac_permission = "finance.payment.create"

    def post(self, request):
        from .payer_payments import plan_payer_payment

        entity = resolve_entity(request)
        inputs = _payment_inputs(request, entity)
        plan = plan_payer_payment(
            inputs["payer"], bank_account=inputs["bank_account"], amount=inputs["amount"],
            payment_date=inputs["payment_date"], shares=inputs["shares"], split=inputs["split"],
        )
        return success_response("Payment split proposed.", data=_plan_payload(request, plan))


class PayerPaymentListCreateView(_FinanceBase):
    """GET/POST /finance/payer-payments/?entity= - payments from a payer for several customers.

    POST body: ``payer`` (code or id), ``bank_account`` (one of the caller's own
    branches', id or name), ``amount`` (kobo), ``payment_date``, optional
    ``method``, ``reference``, ``narration``, ``split`` (overrides the books'
    setting for this payment) and ``shares`` (``[{customer, amount}]``, the
    bursar's own split, which overrides any setting). Booked at once: one receipt
    per customer for bills at the bank's branch, one held receipt per customer for
    bills at another branch. GET lists payments received by, or holding money
    for, the caller's branches; ``?payer=`` narrows it, and ``?branch=`` keeps
    the payments received by that branch or holding a share for it (one branch
    the caller works in; another is answered like one that does not exist).

    docstring-name: Payments from a payer
    """

    @property
    def rbac_permission(self):
        return "finance.payment.create" if self.request.method == "POST" else "finance.payment.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = _payments_in_reach(request, entity)
        if request.query_params.get("payer"):
            qs = qs.filter(payer=_resolve_customer(request, entity, request.query_params["payer"], "payer"))
        if (status_ := request.query_params.get("status")):
            qs = qs.filter(status=status_)
        qs = _filter_by_branch(qs, request, entity, also="shares__branch")
        from core.pagination import XVSPagination

        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs.order_by("-payment_date", "-id"), request, view=self)
        return paginator.get_paginated_response([_payment_payload(request, d) for d in page])

    def post(self, request):
        from .payer_payments import record_payer_payment

        entity = resolve_entity(request)
        inputs = _payment_inputs(request, entity)
        document = record_payer_payment(actor_user=request.user, **inputs)
        document = _payment_or_404(request, entity, document.pk)
        return success_response(
            f"Payment {document.document_number} from {document.payer.name} recorded.",
            data=_payment_payload(request, document), status=201,
        )


class PayerPaymentDetailView(_FinanceBase):
    """GET /finance/payer-payments/<id>/?entity= - one payment and its shares.

    docstring-name: Payments from a payer
    """

    rbac_permission = "finance.payment.view"

    def get(self, request, pk):
        document = _payment_or_404(request, resolve_entity(request), pk)
        return success_response("Payment retrieved.", data=_payment_payload(request, document))


class PayerPaymentVoidView(_FinanceBase):
    """POST /finance/payer-payments/<id>/void/?entity= - void it and every receipt it made.

    Only a caller who works in the branch that received the money may void it.
    Refused while a held share is forwarded: void the forward first.

    docstring-name: Void a payment from a payer
    """

    rbac_permission = "finance.payment.reverse"

    def post(self, request, pk):
        from .payer_payments import void_payer_payment

        entity = resolve_entity(request)
        document = _payment_or_404(request, entity, pk)
        inherited_branch_id(request, document)
        void_payer_payment(
            document, actor_user=request.user,
            date=_date((request.data or {}).get("date"), "date"),
        )
        document = _payment_or_404(request, entity, pk)
        return success_response(
            f"Payment {document.document_number} voided.", data=_payment_payload(request, document),
        )
