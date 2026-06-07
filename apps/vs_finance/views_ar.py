"""REST API for the AR adjustment cycle (mounted at ``/v1/finance/``).

Credit/debit notes, customer refunds, bad-debt write-offs, concessions
(discounts/waivers/scholarships) and installment payment plans — the give-back and
"how they pay" side of receivables that complements the invoice/payment endpoints. Same
conventions as the rest of the surface: entity-scoped via ``?entity=<id|code>``, the
platform ``{success, message, data}`` envelope, RBAC-gated
(``finance.<resource>.<action>``), and thin views that resolve by **code or id** then
hand off to the :mod:`vs_finance.credit_notes` / :mod:`vs_finance.installments` services
which own every posting. Money is integer kobo.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response

from .models import (
    Concession,
    CreditNote,
    CreditNoteLine,
    Customer,
    Invoice,
    PaymentPlan,
    Refund,
)
from .serializers import (
    ConcessionSerializer,
    CreditNoteSerializer,
    InvoiceSerializer,
    PaymentPlanSerializer,
    RefundSerializer,
)
from .views import resolve_entity
from .views_ops import (
    _FinanceBase,
    _date,
    _money,
    _dec,
    _require_lines,
    _resolve_account,
    _resolve_bank_account,
    _resolve_cost_center,
    _resolve_currency,
    _resolve_tax,
)


def _resolve_customer(entity, ref, field="customer", *, required=True):
    """Resolve a customer by **code** or id within ``entity``."""
    if ref in (None, ""):
        if required:
            raise ValidationError({field: "A customer (code or id) is required."})
        return None
    qs = Customer.objects.filter(entity=entity)
    customer = (
        qs.filter(code=str(ref).upper()).first()
        or (qs.filter(pk=int(ref)).first() if str(ref).isdigit() else None)
    )
    if customer is None:
        raise NotFound(f"No customer matches '{ref}' for this entity.")
    return customer


def _resolve_invoice(entity, ref, field="invoice", *, required=True):
    """Resolve an invoice by document number or id within ``entity``."""
    if ref in (None, ""):
        if required:
            raise ValidationError({field: "An invoice (document number or id) is required."})
        return None
    qs = Invoice.objects.filter(entity=entity)
    invoice = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(document_number=str(ref)).first()
    )
    if invoice is None:
        raise NotFound(f"No invoice matches '{ref}' for this entity.")
    return invoice


def _allocation_plan(entity, raw_allocations):
    """Coerce a request ``allocations`` list into ``[(invoice, amount_kobo), ...]``."""
    if not raw_allocations:
        return None
    plan = []
    for i, item in enumerate(raw_allocations):
        invoice = _resolve_invoice(entity, item.get("invoice"), f"allocations[{i}].invoice")
        plan.append((invoice, _money(item.get("amount"), f"allocations[{i}].amount")))
    return plan


# --------------------------------------------------------------------------- #
# Credit / debit notes                                                        #
# --------------------------------------------------------------------------- #

class CreditNoteListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) credit or debit notes for an entity."""

    @property
    def rbac_permission(self):
        return "finance.creditnote.create" if self.request.method == "POST" \
            else "finance.creditnote.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = CreditNote.objects.filter(entity=entity).prefetch_related("lines")
        if (kind := request.query_params.get("kind")):
            qs = qs.filter(kind=kind)
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        return success_response(
            "Credit notes retrieved.",
            data=CreditNoteSerializer(qs.order_by("-note_date", "-id")[:200], many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from .credit_notes import price_credit_note

        entity = resolve_entity(request)
        body = request.data or {}
        lines = _require_lines(body)
        note = CreditNote.objects.create(
            entity=entity,
            customer=_resolve_customer(entity, body.get("customer")),
            kind=body.get("kind", "CREDIT"),
            note_date=_date(body.get("note_date"), "note_date", required=True),
            currency=_resolve_currency(body.get("currency")),
            reason=body.get("reason", ""),
            reference=body.get("reference", ""),
            invoice=_resolve_invoice(entity, body.get("invoice"), required=False),
            created_by=request.user,
        )
        for i, ln in enumerate(lines, start=1):
            CreditNoteLine.objects.create(
                note=note, line_no=i,
                description=ln.get("description", ""),
                revenue_account=_resolve_account(
                    entity, ln.get("revenue_account"),
                    f"lines[{i}].revenue_account", required=True),
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),
                tax_code=_resolve_tax(entity, ln.get("tax_code"), f"lines[{i}].tax_code"),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        price_credit_note(note)
        note.refresh_from_db()
        return success_response(
            f"{note.get_kind_display()} {note.document_number} created.",
            data=CreditNoteSerializer(note).data, status=201,
        )


class _CreditNoteActionBase(_FinanceBase):
    def _note(self, request, pk):
        entity = resolve_entity(request)
        note = CreditNote.objects.filter(entity=entity, pk=pk).first()
        if note is None:
            raise NotFound("Credit note not found for this entity.")
        return entity, note


class CreditNoteDetailView(_CreditNoteActionBase):
    rbac_permission = "finance.creditnote.view"

    def get(self, request, pk):
        _, note = self._note(request, pk)
        return success_response(
            "Credit note retrieved.", data=CreditNoteSerializer(note).data,
        )


class CreditNotePostView(_CreditNoteActionBase):
    rbac_permission = "finance.creditnote.post"

    def post(self, request, pk):
        from .credit_notes import post_credit_note

        entity, note = self._note(request, pk)
        body = request.data or {}
        plan = _allocation_plan(entity, body.get("allocations"))
        auto = bool(body.get("auto_allocate", plan is None))
        post_credit_note(
            note, actor_user=request.user,
            auto_allocate=auto, allocations=plan,
        )
        note.refresh_from_db()
        return success_response(
            f"{note.get_kind_display()} {note.document_number} posted.",
            data=CreditNoteSerializer(note).data,
        )


class CreditNoteAllocateView(_CreditNoteActionBase):
    rbac_permission = "finance.creditnote.allocate"

    def post(self, request, pk):
        from .credit_notes import allocate_credit_note

        entity, note = self._note(request, pk)
        body = request.data or {}
        plan = _allocation_plan(entity, body.get("allocations"))
        allocate_credit_note(note, allocations=plan, actor_user=request.user)
        note.refresh_from_db()
        return success_response(
            f"Credit note {note.document_number} allocated.",
            data=CreditNoteSerializer(note).data,
        )


# --------------------------------------------------------------------------- #
# Customer refunds                                                             #
# --------------------------------------------------------------------------- #

class RefundListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) customer refunds for an entity."""

    @property
    def rbac_permission(self):
        return "finance.refund.create" if self.request.method == "POST" \
            else "finance.refund.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = Refund.objects.filter(entity=entity).select_related("customer")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        return success_response(
            "Refunds retrieved.",
            data=RefundSerializer(qs.order_by("-refund_date", "-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        refund = Refund.objects.create(
            entity=entity,
            customer=_resolve_customer(entity, body.get("customer")),
            refund_date=_date(body.get("refund_date"), "refund_date", required=True),
            currency=_resolve_currency(body.get("currency")),
            method=body.get("method", "BANK_TRANSFER"),
            amount=_money(body.get("amount", 0), "amount"),
            bank_account=_resolve_bank_account(
                entity, body.get("bank_account"), required=False),
            reference=body.get("reference", ""),
            narration=body.get("narration", ""),
            created_by=request.user,
        )
        return success_response(
            f"Refund {refund.document_number} created.",
            data=RefundSerializer(refund).data, status=201,
        )


class _RefundActionBase(_FinanceBase):
    def _refund(self, request, pk):
        entity = resolve_entity(request)
        refund = Refund.objects.filter(entity=entity, pk=pk).first()
        if refund is None:
            raise NotFound("Refund not found for this entity.")
        return entity, refund


class RefundDetailView(_RefundActionBase):
    rbac_permission = "finance.refund.view"

    def get(self, request, pk):
        _, refund = self._refund(request, pk)
        return success_response("Refund retrieved.", data=RefundSerializer(refund).data)


class RefundPostView(_RefundActionBase):
    rbac_permission = "finance.refund.post"

    def post(self, request, pk):
        from .credit_notes import post_refund

        _, refund = self._refund(request, pk)
        post_refund(refund, actor_user=request.user)
        refund.refresh_from_db()
        return success_response(
            f"Refund {refund.document_number} posted.",
            data=RefundSerializer(refund).data,
        )


# --------------------------------------------------------------------------- #
# Bad-debt write-off                                                          #
# --------------------------------------------------------------------------- #

class InvoiceWriteOffView(_FinanceBase):
    """POST /invoices/<pk>/write-off/ — write off an uncollectable balance as bad debt."""

    rbac_permission = "finance.invoice.writeoff"

    def post(self, request, pk):
        from .credit_notes import write_off_invoice

        entity = resolve_entity(request)
        invoice = Invoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("Invoice not found for this entity.")
        body = request.data or {}
        amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") else None
        write_off_invoice(
            invoice,
            amount=amount,
            write_off_account=_resolve_account(
                entity, body.get("write_off_account"), "write_off_account"),
            write_off_date=_date(body.get("write_off_date"), "write_off_date"),
            narration=body.get("narration", ""),
            actor_user=request.user,
        )
        invoice.refresh_from_db()
        return success_response(
            f"Invoice {invoice.document_number} written off.",
            data=InvoiceSerializer(invoice).data,
        )


# --------------------------------------------------------------------------- #
# Concessions — discounts / waivers / scholarships                            #
# --------------------------------------------------------------------------- #

class ConcessionListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) concessions for an entity."""

    @property
    def rbac_permission(self):
        return "finance.concession.create" if self.request.method == "POST" \
            else "finance.concession.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = Concession.objects.filter(entity=entity).select_related("customer", "invoice")
        if (kind := request.query_params.get("kind")):
            qs = qs.filter(kind=kind)
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        return success_response(
            "Concessions retrieved.",
            data=ConcessionSerializer(qs.order_by("-concession_date", "-id")[:200], many=True).data,
        )

    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        concession = Concession.objects.create(
            entity=entity,
            customer=_resolve_customer(entity, body.get("customer")),
            invoice=_resolve_invoice(entity, body.get("invoice")),
            kind=body.get("kind", "DISCOUNT"),
            concession_date=_date(body.get("concession_date"), "concession_date", required=True),
            amount=_money(body.get("amount", 0), "amount"),
            allowance_account=_resolve_account(
                entity, body.get("allowance_account"), "allowance_account", required=False),
            reason=body.get("reason", ""),
            reference=body.get("reference", ""),
            created_by=request.user,
        )
        return success_response(
            f"{concession.get_kind_display()} {concession.document_number} created.",
            data=ConcessionSerializer(concession).data, status=201,
        )


class _ConcessionActionBase(_FinanceBase):
    def _concession(self, request, pk):
        entity = resolve_entity(request)
        concession = Concession.objects.filter(entity=entity, pk=pk).first()
        if concession is None:
            raise NotFound("Concession not found for this entity.")
        return entity, concession


class ConcessionDetailView(_ConcessionActionBase):
    rbac_permission = "finance.concession.view"

    def get(self, request, pk):
        _, concession = self._concession(request, pk)
        return success_response(
            "Concession retrieved.", data=ConcessionSerializer(concession).data,
        )


class ConcessionPostView(_ConcessionActionBase):
    rbac_permission = "finance.concession.post"

    def post(self, request, pk):
        from .installments import post_concession

        _, concession = self._concession(request, pk)
        post_concession(concession, actor_user=request.user)
        concession.refresh_from_db()
        return success_response(
            f"{concession.get_kind_display()} {concession.document_number} posted.",
            data=ConcessionSerializer(concession).data,
        )


# --------------------------------------------------------------------------- #
# Installment payment plans                                                   #
# --------------------------------------------------------------------------- #

class PaymentPlanListCreateView(_FinanceBase):
    """GET (list) / POST (create draft + build schedule) payment plans for an entity."""

    @property
    def rbac_permission(self):
        return "finance.paymentplan.create" if self.request.method == "POST" \
            else "finance.paymentplan.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = (
            PaymentPlan.objects.filter(entity=entity)
            .select_related("customer", "invoice").prefetch_related("installments")
        )
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(plan_status=status_val)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        return success_response(
            "Payment plans retrieved.",
            data=PaymentPlanSerializer(qs.order_by("-start_date", "-id")[:200], many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from .installments import build_installments

        entity = resolve_entity(request)
        body = request.data or {}
        invoice = _resolve_invoice(entity, body.get("invoice"), required=False)
        # Default the spread total to the invoice's outstanding balance when omitted.
        raw_total = body.get("total_amount")
        if raw_total in (None, "") and invoice is not None:
            total = invoice.balance_due
        else:
            total = _money(raw_total, "total_amount")
        count = int(body.get("installment_count", 1) or 1)
        plan = PaymentPlan.objects.create(
            entity=entity,
            customer=_resolve_customer(entity, body.get("customer")),
            invoice=invoice,
            start_date=_date(body.get("start_date"), "start_date", required=True),
            frequency=body.get("frequency", "MONTHLY"),
            installment_count=count,
            total_amount=total,
            notes=body.get("notes", ""),
            created_by=request.user,
        )
        amounts = body.get("amounts")
        if amounts:
            amounts = [_money(a, f"amounts[{i}]") for i, a in enumerate(amounts)]
        build_installments(plan, amounts=amounts)
        return success_response(
            f"Payment plan {plan.document_number} created.",
            data=PaymentPlanSerializer(plan).data, status=201,
        )


class _PaymentPlanActionBase(_FinanceBase):
    def _plan(self, request, pk):
        entity = resolve_entity(request)
        plan = PaymentPlan.objects.filter(entity=entity, pk=pk).first()
        if plan is None:
            raise NotFound("Payment plan not found for this entity.")
        return entity, plan


class PaymentPlanDetailView(_PaymentPlanActionBase):
    rbac_permission = "finance.paymentplan.view"

    def get(self, request, pk):
        _, plan = self._plan(request, pk)
        return success_response("Payment plan retrieved.", data=PaymentPlanSerializer(plan).data)


class PaymentPlanActivateView(_PaymentPlanActionBase):
    rbac_permission = "finance.paymentplan.activate"

    def post(self, request, pk):
        from .installments import activate_payment_plan

        _, plan = self._plan(request, pk)
        activate_payment_plan(plan, actor_user=request.user)
        plan.refresh_from_db()
        return success_response(
            f"Payment plan {plan.document_number} activated.",
            data=PaymentPlanSerializer(plan).data,
        )


class PaymentPlanRefreshView(_PaymentPlanActionBase):
    rbac_permission = "finance.paymentplan.activate"

    def post(self, request, pk):
        from .installments import refresh_plan_progress

        _, plan = self._plan(request, pk)
        body = request.data or {}
        settled = (
            _money(body["settled_amount"], "settled_amount")
            if body.get("settled_amount") not in (None, "") else None
        )
        refresh_plan_progress(plan, settled_amount=settled, actor_user=request.user)
        plan.refresh_from_db()
        return success_response(
            f"Payment plan {plan.document_number} progress refreshed.",
            data=PaymentPlanSerializer(plan).data,
        )


class PaymentPlanCancelView(_PaymentPlanActionBase):
    rbac_permission = "finance.paymentplan.cancel"

    def post(self, request, pk):
        from .installments import cancel_payment_plan

        _, plan = self._plan(request, pk)
        cancel_payment_plan(plan, actor_user=request.user)
        plan.refresh_from_db()
        return success_response(
            f"Payment plan {plan.document_number} cancelled.",
            data=PaymentPlanSerializer(plan).data,
        )
