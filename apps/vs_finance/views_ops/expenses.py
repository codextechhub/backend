"""Expense claims.

A claim reimburses one claimant. By default that is a member of staff picked
from the staff list the host app offers, sent as ``claimant`` (their account
id) and resolved inside the entity's own tenant (:func:`_claimant`); their name
is copied into ``claimant_name``, so the claim reads the same wherever it is
shown and stays findable by person through ``claimant_id``. Somebody who is not
on the staff list, such as a parent volunteer, is named by typing
``claimant_name`` with no ``claimant``. Either way the claimant is the
``finance.expenseclaim.claimant`` switch (:mod:`vs_finance.field_access`).
"""
from __future__ import annotations


from vs_workflow.services.approval_filter import (
    filter_by_approval_param, filter_by_stored_status, filter_by_status_word, returned_condition,
)
from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError
from vs_rbac.field_enforcement import assert_writable, readable_lookups
from vs_rbac.scoping import transaction_branch_q

from core.response import success_response
from vs_config.clock import tenant_today

from ..approvals import with_approval_request
from ..views import resolve_entity
from ..models import (
    ExpenseClaim,
    ExpenseClaimLine,
)
from ..serializers import (
    ExpenseClaimSerializer,
)


from .base import (
    _FinanceBase,
    _date,
    _dec,
    _transaction_branch,
    _money,
    _require_lines,
    _resolve_account,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
    _resolve_person,
    _resolve_tax,
)

#: The Field Access resource of an expense claim.
CLAIM_FIELDS = "finance.expenseclaim"

#: Room for a claimant's name, as :class:`~vs_finance.models.ExpenseClaim` stores it.
CLAIMANT_NAME_MAX = 160

#: What a claim with no claimant is refused with.
CLAIMANT_REQUIRED = (
    "Choose the claimant from the staff list, or type the name of somebody who is "
    "not on it."
)


def _claimant(request, entity, body):
    """The person a new claim reimburses and the name it reads under, as ``(user, name)``.

    ``claimant`` (an account id) picks a member of staff and wins over any
    typed name: Ngozi records Tolu Fashola's claim for lab chemicals, and the
    claim is tied to Tolu's account and reads "Tolu Fashola" even if the form
    also sent what was typed before she chose. Without one, ``claimant_name``
    is required: Mrs Okafor, a PTA volunteer with no staff record, is typed.

    A person from another school, an unknown id and one that is not a number
    are refused alike, as every finance person reference is
    (:func:`~vs_finance.views_ops.base._resolve_person`). The write switch is
    judged first, so a caller who may not set the claimant learns nothing
    about the id they sent. Neither key is open on create, and a claim needs a
    claimant, so such a caller cannot raise a claim at all.
    """
    raw = body.get("claimant")
    typed = str(body.get("claimant_name") or "").strip()
    submitted = {}
    if "claimant" in body:
        submitted["claimant"] = None if raw in (None, "", 0, "0") else str(raw)
    if "claimant_name" in body:
        submitted["claimant_name"] = typed
    assert_writable(
        request, CLAIM_FIELDS, submitted, creating=True,
        current={"claimant": None, "claimant_name": ""},
    )
    person = _resolve_person(entity, raw, "claimant")
    if person is not None:
        return person, (person.full_name or person.email or "")[:CLAIMANT_NAME_MAX]
    if not typed:
        raise ValidationError({"claimant_name": CLAIMANT_REQUIRED})
    if len(typed) > CLAIMANT_NAME_MAX:
        raise ValidationError({
            "claimant_name": f"Keep the name to {CLAIMANT_NAME_MAX} characters.",
        })
    return None, typed

# --------------------------------------------------------------------------- #
# Expense claims                                                              #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Expense Claim List Create View.
class ExpenseClaimListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) expense claims for an entity.

    docstring-name: Expense claims
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.expenseclaim.create" if self.request.method == "POST" \
            else "finance.expenseclaim.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from ..approvals import ApprovalGate
        from ..expenses import claim_display_rules

        entity = resolve_entity(request)
        qs = ExpenseClaim.objects.filter(
            transaction_branch_q(request), entity=entity,
        ).prefetch_related("lines")
        qs = filter_by_stored_status(qs, request.query_params.get("status"))
        if (pay := request.query_params.get("payment_status")):
            qs = qs.filter(payment_status=pay)
        # The words the list shows a claim as (vs_finance.expenses.CLAIM_DISPLAY_WORDS).
        qs = filter_by_status_word(
            qs, request.query_params.get("display_status"), claim_display_rules(),
            param="display_status",
        )
        if (search := request.query_params.get("q")):
            from django.db.models import Q
            # A claimant the caller may not read is not searched either.
            lookups = readable_lookups(request, CLAIM_FIELDS, {
                "claimant_name__icontains": "claimant_name",
                "title__icontains": None,
                "document_number__icontains": None,
            })
            match = Q(pk__in=[])
            for lookup in lookups:
                match |= Q(**{lookup: search})
            qs = qs.filter(match)
        qs = filter_by_approval_param(qs, request.query_params)
        return self.paginate(
            request, qs.order_by("-claim_date", "-id"), ExpenseClaimSerializer,
            context={"request": request, "approval_gate": ApprovalGate()},
        )

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        from ..expenses import price_expense_claim

        entity = resolve_entity(request)
        body = request.data or {}
        claimant, claimant_name = _claimant(request, entity, body)
        lines = _require_lines(body)
        claim = ExpenseClaim.objects.create(
            entity=entity,
            branch=_transaction_branch(request, entity, body),
            claimant=claimant,
            claimant_name=claimant_name,
            claim_date=_date(body.get("claim_date"), "claim_date", required=True),
            title=body.get("title", ""),
            narration=body.get("narration", ""),
            currency=_resolve_currency(body.get("currency")),
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            ExpenseClaimLine.objects.create(
                claim=claim, line_no=i,
                description=ln.get("description", ""),
                expense_account=_resolve_account(
                    request, entity, ln.get("expense_account"),
                    f"lines[{i}].expense_account", required=True),
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),
                tax_code=_resolve_tax(
                    entity, ln.get("tax_code"), f"lines[{i}].tax_code",
                    usage="purchase",
                ),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        price_expense_claim(claim)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} created.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data, status=201,
        )


# Define Expense Claim Action Base values.
class _ExpenseClaimActionBase(_FinanceBase):
    # Support the claim workflow.
    def _claim(self, request, pk):
        entity = resolve_entity(request)
        claim = ExpenseClaim.objects.filter(
            transaction_branch_q(request), entity=entity, pk=pk,
        ).first()
        if claim is None:
            raise NotFound("Expense claim not found for this entity.")
        return entity, claim


# Group endpoint behavior for Expense Claim Detail View.
class ExpenseClaimDetailView(_ExpenseClaimActionBase):
    """docstring-name: Expense claims"""
    rbac_permission = "finance.expenseclaim.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, claim = self._claim(request, pk)
        return success_response(
            "Expense claim retrieved.",
            data=with_approval_request(
                ExpenseClaimSerializer(claim, context={"request": request}).data, claim),
        )


# Group endpoint behavior for Expense Claim Post View.
class ExpenseClaimPostView(_ExpenseClaimActionBase):
    """Post a draft expense claim straight to the ledger.

    A claim whose approval route has stages must be submitted instead. A school
    starts with an expense-claim route that has no stages, and posting against
    it needs ``confirm_without_approval`` and is recorded as posted without
    approval, as refunds and write-offs are
    (:func:`vs_finance.approvals.guard_direct_post`).

    docstring-name: Post an expense claim
    """
    rbac_permission = "finance.expenseclaim.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..approvals import guard_direct_post
        from ..expenses import post_expense_claim

        _, claim = self._claim(request, pk)
        guard_direct_post(claim, request, noun="expense claim")
        post_expense_claim(claim, actor_user=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} posted.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


class ExpenseClaimSubmitView(_ExpenseClaimActionBase):
    """POST a draft expense claim into its configured approval workflow."""

    # Submitting cannot post or pay anything. Claim creators already hold the
    # authority to finish and hand off their own draft, so the hand-off needs
    # no tenant grant of its own.
    rbac_permission = "finance.expenseclaim.create"

    def post(self, request, pk):
        from vs_workflow.services import release as release_svc
        from vs_workflow.services.submission import submit_for_approval

        _, claim = self._claim(request, pk)
        instance = submit_for_approval(claim, requested_by=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} submitted for approval.",
            data=ExpenseClaimSerializer(
                claim, context={"request": request},
            ).data | {"approval": release_svc.approval_block(instance)},
        )


# Group endpoint behavior for Expense Claim Reject View.
class ExpenseClaimRejectView(_ExpenseClaimActionBase):
    """POST - reject (cancel) a draft expense claim.

    Refused (422, ``APPROVAL_REQUEST_OPEN``) while the claim's approval request
    is open, returned to its sender included: the request is withdrawn first
    (:func:`vs_finance.approvals.refuse_while_request_open`).

    docstring-name: Reject an expense claim
    """
    rbac_permission = "finance.expenseclaim.post"  # the approver decides approve OR reject

    # Handle POST requests for this endpoint.
    @transaction.atomic
    def post(self, request, pk):
        from ..approvals import refuse_while_request_open
        from ..constants import DocumentStatus
        from ..wording import state_word
        from rest_framework.exceptions import ValidationError

        _, claim = self._claim(request, pk)
        claim = ExpenseClaim.objects.select_for_update().get(pk=claim.pk)
        if claim.status != DocumentStatus.DRAFT:
            raise ValidationError(
                {"status": f"Only a draft claim can be rejected; this one is {state_word(claim)}."})
        refuse_while_request_open(claim, noun="expense claim", act="rejected")
        claim.status = DocumentStatus.CANCELLED
        claim.save(update_fields=["status", "updated_at"])
        return success_response(
            f"Expense claim {claim.document_number} rejected.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


# Group endpoint behavior for Expense Claim Receipt View.
class ExpenseClaimReceiptView(_ExpenseClaimActionBase):
    """POST (multipart ``file``) attach / DELETE a receipt on a claim line.

    Only while the claim is a draft (400 otherwise): what its approvers decide on
    is the evidence they were shown. A claim an approver returned to whoever sent
    it (``approval_returned``) is that person's to change (403 for anybody else),
    and the approver sees the new evidence when the request is resumed from the
    approvals screen (:func:`vs_finance.approvals.correcting_returned`). The
    claim's row is locked for the change, so a resumption and a receipt change
    take turns.

    docstring-name: Expense line receipt
    """

    rbac_permission = "finance.expenseclaim.create"

    # Support the line workflow.
    def _line(self, request, pk, line_id):
        from rest_framework.exceptions import ValidationError

        from ..approvals import correcting_returned
        from ..constants import DocumentStatus

        _, claim = self._claim(request, pk)
        claim = ExpenseClaim.objects.select_for_update().get(pk=claim.pk)
        line = claim.lines.filter(pk=line_id).first()
        if line is None:
            raise NotFound("Line not found on this claim.")
        if claim.status != DocumentStatus.DRAFT:
            raise ValidationError({
                "detail": "Receipt evidence can only be changed while the claim is a draft."
            })
        correcting_returned(claim, request.user, noun="expense claim")
        return claim, line

    # Handle POST requests for this endpoint.
    @transaction.atomic
    def post(self, request, pk, line_id):
        from rest_framework.exceptions import ValidationError

        from core.uploads import validate_upload

        claim, line = self._line(request, pk, line_id)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "A receipt file is required."})
        # Validate before saving. DatabaseStorage re-checks type and size, but it raises
        # from inside _save, which surfaces as a 500 rather than a 400, and it never
        # inspects content - so a corrupt or mislabelled receipt was stored and only
        # discovered when somebody tried to read it.
        validate_upload(upload)
        line.receipt.save(upload.name, upload, save=True)
        claim.refresh_from_db()
        return success_response(
            "Receipt attached.",
            data=with_approval_request(
                ExpenseClaimSerializer(claim, context={"request": request}).data, claim),
            status=201,
        )

    # Handle DELETE requests for this endpoint.
    @transaction.atomic
    def delete(self, request, pk, line_id):
        claim, line = self._line(request, pk, line_id)
        if line.receipt:
            line.receipt.delete(save=True)
        claim.refresh_from_db()
        return success_response(
            "Receipt removed.",
            data=with_approval_request(
                ExpenseClaimSerializer(claim, context={"request": request}).data, claim),
        )


# Group endpoint behavior for Expense Claim Settle View.
class ExpenseClaimSettleView(_ExpenseClaimActionBase):
    """docstring-name: Settle an expense claim"""
    rbac_permission = "finance.expenseclaim.settle"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..expenses import settle_expense_claim

        entity, claim = self._claim(request, pk)
        body = request.data or {}
        bank = _resolve_bank_account(
            request, entity, body.get("bank_account"),
            document_branch=claim.branch_id, noun="expense claim")
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None
        settle_expense_claim(
            claim, bank_account=bank,
            pay_date=_date(body.get("pay_date"), "pay_date", required=True),
            amount=amount, actor_user=request.user,
        )
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} reimbursed.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


# Group endpoint behavior for Expense Claim Void View.
class ExpenseClaimVoidView(_ExpenseClaimActionBase):
    """POST - void a posted, un-reimbursed claim (reverses its journal, marks CANCELLED).

    docstring-name: Void an expense claim
    """
    rbac_permission = "finance.expenseclaim.post"  # the approver undoes their approval

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from ..expenses import void_expense_claim

        _, claim = self._claim(request, pk)
        void_expense_claim(claim, actor_user=request.user)
        claim.refresh_from_db()
        return success_response(
            f"Expense claim {claim.document_number} voided.",
            data=ExpenseClaimSerializer(claim, context={"request": request}).data,
        )


# Group endpoint behavior for Expense Claim Summary View.
class ExpenseClaimSummaryView(_FinanceBase):
    """GET - header KPIs over **all** expense claims (accurate under pagination).

    ``open`` counts the claims the list shows under Draft, Awaiting approval,
    Approved and Part-paid (:data:`vs_finance.expenses.CLAIM_DISPLAY_WORDS`),
    each read as its word lists it, so a claim an approver sent back is not
    among them; ``sent_back`` counts those. ``awaiting`` is what is still owed
    on the Approved and Part-paid claims. ``month_total`` and ``avg`` are over
    every claim not rejected, by claim date. The claims counted are the ones the
    list shows: the same branch and period (:mod:`vs_finance.list_scope`), and
    archived years left out unless asked for.

    docstring-name: Expense claims
    """

    rbac_permission = "finance.expenseclaim.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        from django.db.models import Count, Q, Sum
        from django.db.models.functions import Coalesce

        from vs_workflow.services.approval_filter import word_condition

        from ..constants import DocumentStatus, InvoicePaymentStatus
        from ..expenses import claim_display_rules

        entity = resolve_entity(request)
        today = tenant_today(entity.tenant)
        live = ~Q(status=DocumentStatus.CANCELLED)
        awaiting_q = Q(status=DocumentStatus.POSTED) & ~Q(
            payment_status=InvoicePaymentStatus.PAID)
        words = claim_display_rules()
        open_q = Q(pk__in=[])
        for word in ("DRAFT", "PENDING", "APPROVED", "PART_PAID"):
            open_q |= words[word]

        from ..list_scope import narrow_finance_document_list

        agg = narrow_finance_document_list(ExpenseClaim.objects.filter(
            transaction_branch_q(request), entity=entity,
        ), request, entity=entity).aggregate(
            open=Count("id", filter=word_condition(ExpenseClaim, open_q)),
            month_total=Coalesce(Sum("total", filter=live & Q(
                claim_date__year=today.year, claim_date__month=today.month)), 0),
            live_total=Coalesce(Sum("total", filter=live), 0),
            live_count=Count("id", filter=live),
            awaiting_total=Coalesce(Sum("total", filter=awaiting_q), 0),
            awaiting_paid=Coalesce(Sum("amount_paid", filter=awaiting_q), 0),
            sent_back=Count("id", filter=returned_condition(ExpenseClaim)),
        )
        avg = agg["live_total"] // agg["live_count"] if agg["live_count"] else 0
        return success_response(
            "Expense claim summary retrieved.",
            data={
                "open": agg["open"],
                "month_total": agg["month_total"],
                "avg": avg,
                "awaiting": agg["awaiting_total"] - agg["awaiting_paid"],
                "sent_back": agg["sent_back"],
            },
        )
