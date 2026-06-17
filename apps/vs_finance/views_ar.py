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
    DunningNotice,
    DunningPolicy,
    DunningStage,
    FeeItem,
    FeeStructure,
    Invoice,
    PaymentPlan,
    Refund,
)
from .serializers import (
    ConcessionSerializer,
    CreditNoteSerializer,
    CustomerSerializer,
    DunningNoticeSerializer,
    DunningPolicySerializer,
    FeeStructureSerializer,
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
# Customers / payers                                                          #
# --------------------------------------------------------------------------- #

def _customer_ledger(entity, customer_ids=None):
    """Net AR position per customer, in two aggregate queries (no per-row N+1).

    Returns ``{customer_id: {"outstanding", "credit", "overdue", "lifetime_paid"}}``
    where ``outstanding`` is the sum of open invoice balances, ``credit`` the
    customer's unallocated receipts, and ``overdue`` whether any open invoice is
    past due. Net balance = outstanding − credit (positive owes, negative in credit).
    """
    import datetime
    from django.db.models import F, Q, Sum
    from django.db.models.functions import Coalesce

    from .constants import DocumentStatus
    from .models import Invoice, Payment

    today = datetime.date.today()
    bal = F("total") - F("amount_paid") - F("amount_credited")
    inv = Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)
    pay = Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED)
    if customer_ids is not None:
        inv = inv.filter(customer_id__in=customer_ids)
        pay = pay.filter(customer_id__in=customer_ids)

    out: dict[int, dict] = {}
    for r in inv.values("customer_id").annotate(
        outstanding=Coalesce(Sum(bal), 0),
        overdue_bal=Coalesce(Sum(bal, filter=Q(due_date__lt=today)), 0),
    ):
        out.setdefault(r["customer_id"], {})
        out[r["customer_id"]]["outstanding"] = int(r["outstanding"] or 0)
        out[r["customer_id"]]["overdue"] = int(r["overdue_bal"] or 0) > 0
    for r in pay.values("customer_id").annotate(
        credit=Coalesce(Sum(F("amount") - F("allocated_amount")), 0),
        lifetime=Coalesce(Sum("amount"), 0),
    ):
        d = out.setdefault(r["customer_id"], {})
        d["credit"] = int(r["credit"] or 0)
        d["lifetime_paid"] = int(r["lifetime"] or 0)
    return out


def _account_status(net: int, overdue: bool) -> str:
    """Derive the customer's account status pill from net balance + aging."""
    if net < 0:
        return "CREDIT"
    if overdue:
        return "OVERDUE"
    return "ACTIVE"


def _money_obj(kobo) -> dict:
    """Money payload {kobo, naira} — the AR drawer shape (mirrors views._money)."""
    from .money import format_naira
    return {"kobo": int(kobo), "naira": format_naira(int(kobo))}

class CustomerListCreateView(_FinanceBase):
    """GET (list) / POST (create) customers / payers for an entity.

    List filters: ``?search=`` (code or name), ``?is_active=true|false``.

    docstring-name: Customers
    """

    @property
    def rbac_permission(self):
        return "finance.customer.create" if self.request.method == "POST" \
            else "finance.customer.view"

    def get(self, request):
        from .money import format_naira

        entity = resolve_entity(request)
        qs = Customer.objects.filter(entity=entity).select_related("receivable_account")
        if (search := request.query_params.get("search")):
            from django.db.models import Q
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")

        customers = list(qs.order_by("code")[:500])
        ledger = _customer_ledger(entity, [c.id for c in customers])
        rows = []
        for c in customers:
            row = CustomerSerializer(c).data
            led = ledger.get(c.id, {})
            net = led.get("outstanding", 0) - led.get("credit", 0)
            row["balance"] = net                      # signed kobo: + owes, − in credit
            row["balance_naira"] = format_naira(net)
            row["account_status"] = _account_status(net, led.get("overdue", False))
            rows.append(row)
        return success_response("Customers retrieved.", data=rows)

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip().upper()
        if not code:
            raise ValidationError({"code": "A customer code is required."})
        if Customer.objects.filter(entity=entity, code=code).exists():
            raise ValidationError({"code": f"A customer with code '{code}' already exists."})
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A customer name is required."})
        # Default the AR control to the entity's 1200 Accounts Receivable if not given.
        receivable = _resolve_account(
            entity, body.get("receivable_account") or "1200",
            "receivable_account", required=True)
        customer = Customer.objects.create(
            entity=entity, code=code, name=name,
            billing_email=body.get("billing_email", ""),
            billing_phone=body.get("billing_phone", ""),
            billing_address=body.get("billing_address", ""),
            receivable_account=receivable,
            opening_balance=_money(body.get("opening_balance", 0), "opening_balance"),
            source_type=body.get("source_type", ""),
            source_id=str(body.get("source_id", "")),
            is_active=bool(body.get("is_active", True)),
        )
        return success_response(
            f"Customer {customer.code} created.",
            data=CustomerSerializer(customer).data, status=201,
        )


class CustomerDetailView(_FinanceBase):
    """GET / PATCH one customer (by **code or id**).

    docstring-name: Customers
    """

    @property
    def rbac_permission(self):
        return "finance.customer.update" if self.request.method == "PATCH" \
            else "finance.customer.view"

    def get(self, request, pk):
        import datetime

        from .constants import DocumentStatus, InvoicePaymentStatus
        from .models import Invoice, Payment

        entity = resolve_entity(request)
        customer = _resolve_customer(entity, pk)
        led = _customer_ledger(entity, [customer.id]).get(customer.id, {})
        net = led.get("outstanding", 0) - led.get("credit", 0)
        today = datetime.date.today()

        invoices = list(Invoice.objects.filter(
            entity=entity, customer=customer, status=DocumentStatus.POSTED,
        ).order_by("invoice_date", "id")[:500])
        payments = list(Payment.objects.filter(
            entity=entity, customer=customer, status=DocumentStatus.POSTED,
        ).order_by("payment_date", "id")[:500])

        def inv_status(i):
            if i.payment_status == InvoicePaymentStatus.PAID:
                return "PAID"
            if i.due_date and i.due_date < today and i.balance_due > 0:
                return "OVERDUE"
            if i.payment_status == InvoicePaymentStatus.PARTIAL:
                return "PARTIAL"
            return "ISSUED"

        open_invoices = [
            {
                "document_number": i.document_number,
                "invoice_date": i.invoice_date.isoformat(),
                "due_date": i.due_date.isoformat() if i.due_date else None,
                "total": _money_obj(i.total), "balance": _money_obj(i.balance_due),
                "status": inv_status(i),
            }
            for i in invoices if i.balance_due > 0
        ]

        transactions = (
            [{"date": i.invoice_date.isoformat(), "type": "INVOICE",
              "reference": i.document_number, "amount": _money_obj(i.total),
              "status": inv_status(i)} for i in invoices]
            + [{"date": p.payment_date.isoformat(), "type": "PAYMENT",
                "reference": p.document_number, "amount": _money_obj(p.amount),
                "status": "POSTED"} for p in payments]
        )
        transactions.sort(key=lambda t: t["date"], reverse=True)

        # Statement: opening balance, then invoices (debit) and receipts (credit),
        # chronological, with a running balance.
        events = []
        if customer.opening_balance:
            events.append((datetime.date.min, "Opening balance", customer.opening_balance, 0))
        events += [(i.invoice_date, f"Invoice {i.document_number}", i.total, 0) for i in invoices]
        events += [(p.payment_date, f"Receipt {p.document_number}", 0, p.amount) for p in payments]
        events.sort(key=lambda e: e[0])
        running = 0
        statement = []
        for d, desc, debit, credit in events:
            running += debit - credit
            statement.append({
                "date": None if d == datetime.date.min else d.isoformat(),
                "description": desc, "debit": _money_obj(debit),
                "credit": _money_obj(credit), "balance": _money_obj(running),
            })

        return success_response("Customer retrieved.", data={
            "customer": CustomerSerializer(customer).data,
            "summary": {
                "current_balance": _money_obj(net),
                "lifetime_paid": _money_obj(led.get("lifetime_paid", 0)),
                "open_invoice_count": len(open_invoices),
                "account_status": _account_status(net, led.get("overdue", False)),
            },
            "open_invoices": open_invoices,
            "transactions": transactions,
            "statement": statement,
        })

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        customer = _resolve_customer(entity, pk)
        body = request.data or {}
        for field in ("name", "billing_email", "billing_phone", "billing_address",
                      "source_type", "source_id"):
            if field in body:
                setattr(customer, field, body[field])
        if "receivable_account" in body:
            customer.receivable_account = _resolve_account(
                entity, body.get("receivable_account"), "receivable_account", required=True)
        if "opening_balance" in body:
            customer.opening_balance = _money(body.get("opening_balance"), "opening_balance")
        if "is_active" in body:
            customer.is_active = bool(body.get("is_active"))
        customer.save()
        return success_response(
            f"Customer {customer.code} updated.", data=CustomerSerializer(customer).data,
        )


class CustomerReceiptView(_FinanceBase):
    """POST /customers/<pk>/receipt/ — record a receipt for a customer and auto-
    allocate it across their open invoices (oldest first). Any excess stays as
    unallocated credit on the customer.

    docstring-name: Record a customer receipt
    """

    rbac_permission = "finance.payment.create"

    @transaction.atomic
    def post(self, request, pk):
        from .models import Payment
        from .receivables import post_payment

        entity = resolve_entity(request)
        customer = _resolve_customer(entity, pk)
        body = request.data or {}
        amount = _money(body.get("amount"), "amount")
        if amount <= 0:
            raise ValidationError({"amount": "A positive amount is required."})
        payment = Payment.objects.create(
            entity=entity, customer=customer,
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),
            method=body.get("method") or "BANK_TRANSFER", amount=amount,
            deposit_account=_resolve_account(
                entity, body.get("deposit_account"), "deposit_account", required=True),
            reference=body.get("reference", ""), narration=body.get("narration", ""),
            created_by=request.user,
        )
        post_payment(payment, actor_user=request.user, auto_allocate=True)
        return success_response(
            f"Receipt {payment.document_number} recorded for {customer.code}.",
            data={
                "payment": payment.document_number,
                "allocated": payment.allocated_amount,
                "unallocated": payment.unallocated_amount,
            },
            status=201,
        )


# --------------------------------------------------------------------------- #
# Fee structures (billing catalogue → invoices)                               #
# --------------------------------------------------------------------------- #

def _build_fee_items(structure, entity, raw_items):
    """(Re)create a structure's fee items from a request ``items`` list."""
    if not raw_items:
        raise ValidationError({"items": "At least one fee item is required."})
    for i, item in enumerate(raw_items, start=1):
        amount = _money(item.get("amount"), f"items[{i}].amount")
        if amount <= 0:
            raise ValidationError({f"items[{i}].amount": "A positive amount is required."})
        FeeItem.objects.create(
            structure=structure, line_no=item.get("line_no", i),
            description=str(item.get("description", "")).strip() or f"Fee {i}",
            revenue_account=_resolve_account(
                entity, item.get("revenue_account"), f"items[{i}].revenue_account", required=True),
            amount=amount,
            tax_code=_resolve_tax(entity, item.get("tax_code"), f"items[{i}].tax_code"),
        )


def _resolve_fee_structure(entity, ref):
    qs = FeeStructure.objects.filter(entity=entity)
    structure = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(code=str(ref).upper()).first()
    )
    if structure is None:
        raise NotFound(f"No fee structure matches '{ref}' for this entity.")
    return structure


class FeeStructureListCreateView(_FinanceBase):
    """GET (list) / POST (create) fee structures for an entity.

    POST body: ``{code, name, term?, description?, is_active?, items:[{description,
    revenue_account, amount, tax_code?}]}``.

    docstring-name: Fee structures
    """

    @property
    def rbac_permission(self):
        return "finance.feestructure.create" if self.request.method == "POST" \
            else "finance.feestructure.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = FeeStructure.objects.filter(entity=entity).prefetch_related("items__revenue_account")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (search := request.query_params.get("search")):
            from django.db.models import Q
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        return success_response(
            "Fee structures retrieved.",
            data=FeeStructureSerializer(qs.order_by("code"), many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip().upper()
        if not code:
            raise ValidationError({"code": "A fee structure code is required."})
        if FeeStructure.objects.filter(entity=entity, code=code).exists():
            raise ValidationError({"code": f"A fee structure with code '{code}' already exists."})
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A fee structure name is required."})
        structure = FeeStructure.objects.create(
            entity=entity, code=code, name=name,
            term=body.get("term", ""), description=body.get("description", ""),
            is_active=bool(body.get("is_active", True)), created_by=request.user,
        )
        _build_fee_items(structure, entity, body.get("items"))
        structure.refresh_from_db()
        return success_response(
            f"Fee structure {structure.code} created.",
            data=FeeStructureSerializer(structure).data, status=201,
        )


class FeeStructureDetailView(_FinanceBase):
    """GET / PATCH one fee structure (by **code or id**). PATCH may replace ``items``.

    docstring-name: Fee structures
    """

    @property
    def rbac_permission(self):
        return "finance.feestructure.edit" if self.request.method == "PATCH" \
            else "finance.feestructure.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        structure = _resolve_fee_structure(entity, pk)
        return success_response(
            "Fee structure retrieved.", data=FeeStructureSerializer(structure).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        structure = _resolve_fee_structure(entity, pk)
        body = request.data or {}
        for field in ("name", "term", "description"):
            if field in body:
                setattr(structure, field, body[field])
        if "is_active" in body:
            structure.is_active = bool(body.get("is_active"))
        structure.save()
        if "items" in body:  # full replace
            structure.items.all().delete()
            _build_fee_items(structure, entity, body.get("items"))
        structure.refresh_from_db()
        return success_response(
            f"Fee structure {structure.code} updated.",
            data=FeeStructureSerializer(structure).data,
        )


class FeeStructureGenerateView(_FinanceBase):
    """POST — raise a posted invoice per customer from this fee structure.

    Body: ``{customers:[code|id, ...]}`` or ``{all_active:true}``; optional
    ``invoice_date``, ``due_date`` (ISO). Returns the invoices created.

    docstring-name: Generate invoices from a fee structure
    """

    rbac_permission = "finance.feestructure.generate"

    @transaction.atomic
    def post(self, request, pk):
        from .fees import generate_invoices

        entity = resolve_entity(request)
        structure = _resolve_fee_structure(entity, pk)
        body = request.data or {}
        if body.get("all_active"):
            customers = list(Customer.objects.filter(entity=entity, is_active=True))
        else:
            refs = body.get("customers") or []
            if not refs:
                raise ValidationError(
                    {"customers": "Provide a customers list or all_active=true."})
            customers = [_resolve_customer(entity, r, "customers") for r in refs]
        invoices = generate_invoices(
            structure, customers,
            invoice_date=_date(body.get("invoice_date"), "invoice_date"),
            due_date=_date(body.get("due_date"), "due_date"),
            actor_user=request.user,
        )
        return success_response(
            f"{len(invoices)} invoice(s) generated from {structure.code}.",
            data={
                "structure": structure.code,
                "generated": len(invoices),
                "invoices": InvoiceSerializer(invoices, many=True).data,
            },
            status=201,
        )


# --------------------------------------------------------------------------- #
# Credit / debit notes                                                        #
# --------------------------------------------------------------------------- #

class CreditNoteListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) credit or debit notes for an entity.

    docstring-name: Credit notes
    """

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
    """docstring-name: Credit notes"""
    rbac_permission = "finance.creditnote.view"

    def get(self, request, pk):
        _, note = self._note(request, pk)
        return success_response(
            "Credit note retrieved.", data=CreditNoteSerializer(note).data,
        )


class CreditNotePostView(_CreditNoteActionBase):
    """docstring-name: Post a credit note"""
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
    """docstring-name: Allocate a credit note"""
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
    """GET (list) / POST (create draft) customer refunds for an entity.

    docstring-name: Refunds
    """

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
    """docstring-name: Refunds"""
    rbac_permission = "finance.refund.view"

    def get(self, request, pk):
        _, refund = self._refund(request, pk)
        return success_response("Refund retrieved.", data=RefundSerializer(refund).data)


class RefundPostView(_RefundActionBase):
    """docstring-name: Post a refund"""
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
    """POST /invoices/<pk>/write-off/ — write off an uncollectable balance as bad debt.

    docstring-name: Write off an invoice
    """

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


class InvoicePayView(_FinanceBase):
    """POST /invoices/<pk>/pay/ — record a customer receipt and settle this invoice.

    Body: ``{amount(kobo), payment_date, method?, deposit_account, reference?,
    narration?}``. Posts the receipt (Dr bank/cash, Cr AR) and allocates it to this
    invoice; any excess remains as unallocated credit on the customer.

    docstring-name: Record a payment
    """

    rbac_permission = "finance.payment.create"

    @transaction.atomic
    def post(self, request, pk):
        from .models import Payment
        from .receivables import post_payment

        entity = resolve_entity(request)
        invoice = Invoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("Invoice not found for this entity.")
        if invoice.status != "POSTED":
            raise ValidationError({"invoice": "Only a posted invoice can be paid."})

        body = request.data or {}
        amount = _money(body.get("amount"), "amount")
        if amount <= 0:
            raise ValidationError({"amount": "A positive amount is required."})

        payment = Payment.objects.create(
            entity=entity, customer=invoice.customer,
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),
            method=body.get("method") or "BANK_TRANSFER",
            amount=amount,
            deposit_account=_resolve_account(
                entity, body.get("deposit_account"), "deposit_account", required=True),
            currency=invoice.currency,
            reference=body.get("reference", ""),
            narration=body.get("narration", ""),
            created_by=request.user,
        )
        post_payment(payment, actor_user=request.user, allocations=[(invoice, amount)])
        invoice.refresh_from_db()
        return success_response(
            f"Receipt {payment.document_number} recorded against {invoice.document_number}.",
            data=InvoiceSerializer(invoice).data, status=201,
        )


class InvoiceRemindView(_FinanceBase):
    """POST /invoices/<pk>/remind/ — raise & send a dunning reminder for this invoice.

    docstring-name: Send an invoice reminder
    """

    rbac_permission = "finance.dunning.send"

    def post(self, request, pk):
        from .dunning import remind_invoice

        entity = resolve_entity(request)
        invoice = Invoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("Invoice not found for this entity.")
        notice = remind_invoice(
            invoice, actor_user=request.user,
            message=(request.data or {}).get("message", ""),
        )
        return success_response(
            f"Reminder {notice.document_number} sent for {invoice.document_number}.",
            data=DunningNoticeSerializer(notice).data,
        )


# --------------------------------------------------------------------------- #
# Concessions — discounts / waivers / scholarships                            #
# --------------------------------------------------------------------------- #

class ConcessionListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) concessions for an entity.

    docstring-name: Concessions
    """

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
    """docstring-name: Concessions"""
    rbac_permission = "finance.concession.view"

    def get(self, request, pk):
        _, concession = self._concession(request, pk)
        return success_response(
            "Concession retrieved.", data=ConcessionSerializer(concession).data,
        )


class ConcessionPostView(_ConcessionActionBase):
    """docstring-name: Post a concession"""
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
    """GET (list) / POST (create draft + build schedule) payment plans for an entity.

    docstring-name: Payment plans
    """

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
    """docstring-name: Payment plans"""
    rbac_permission = "finance.paymentplan.view"

    def get(self, request, pk):
        _, plan = self._plan(request, pk)
        return success_response("Payment plan retrieved.", data=PaymentPlanSerializer(plan).data)


class PaymentPlanActivateView(_PaymentPlanActionBase):
    """docstring-name: Activate a payment plan"""
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
    """docstring-name: Refresh payment plan status"""
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
    """docstring-name: Cancel a payment plan"""
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


# --------------------------------------------------------------------------- #
# Customer statement of account                                               #
# --------------------------------------------------------------------------- #

class CustomerStatementView(_FinanceBase):
    """A dated statement of account for one customer (``?customer=<code|id>``).

    Optional ``?start=`` / ``?end=`` ISO dates bound the period (``end`` defaults to
    today; an absent ``start`` runs from inception with a zero opening balance).
    Supports ``?export=csv|xlsx|pdf``. All money is reported in kobo + naira.

    docstring-name: Customer statement
    """

    rbac_permission = "finance.report.view"

    def get(self, request):
        from .money import format_naira
        from .reports import customer_statement
        from .views import _maybe_export, _money as _money_pair

        entity = resolve_entity(request)
        customer = _resolve_customer(entity, request.query_params.get("customer"))
        start = _date(request.query_params.get("start"), "start")
        end = _date(request.query_params.get("end"), "end")
        stmt = customer_statement(customer, start_date=start, end_date=end)

        from .exports import ReportTable

        columns = ["Date", "Type", "Document", "Description", "Debit", "Credit", "Balance"]
        rows = [
            [
                str(e.date), e.doc_type, e.document_number, e.description,
                format_naira(e.debit) if e.debit else "",
                format_naira(e.credit) if e.credit else "",
                format_naira(e.balance),
            ]
            for e in stmt.entries
        ]
        summary = ["", "", "", "TOTAL",
                   format_naira(stmt.total_debits), format_naira(stmt.total_credits),
                   format_naira(stmt.closing_balance)]
        period = f"{stmt.start_date or 'inception'} → {stmt.end_date}"
        export = _maybe_export(request, ReportTable(
            title=f"Statement of Account — {stmt.customer_name}",
            subtitle=f"{entity.code} · {stmt.customer_code} · {period} · "
                     f"opening {format_naira(stmt.opening_balance)}",
            columns=columns,
            rows=rows,
            summary_rows=[summary],
        ), filename=f"statement_{entity.code}_{stmt.customer_code}")
        if export is not None:
            return export

        return success_response(
            "Customer statement retrieved.",
            data={
                "entity": entity.code,
                "customer": {
                    "id": stmt.customer_id, "code": stmt.customer_code,
                    "name": stmt.customer_name,
                },
                "start_date": str(stmt.start_date) if stmt.start_date else None,
                "end_date": str(stmt.end_date),
                "opening_balance": _money_pair(stmt.opening_balance),
                "entries": [
                    {
                        "date": str(e.date), "doc_type": e.doc_type,
                        "document_number": e.document_number, "description": e.description,
                        "debit": _money_pair(e.debit), "credit": _money_pair(e.credit),
                        "balance": _money_pair(e.balance),
                    }
                    for e in stmt.entries
                ],
                "total_debits": _money_pair(stmt.total_debits),
                "total_credits": _money_pair(stmt.total_credits),
                "closing_balance": _money_pair(stmt.closing_balance),
                "aging": {b: _money_pair(v) for b, v in stmt.aging.items()},
            },
        )


# --------------------------------------------------------------------------- #
# Dunning — policies, stages and automated reminder notices                   #
# --------------------------------------------------------------------------- #

class DunningPolicyListCreateView(_FinanceBase):
    """GET (list) dunning policies, or POST to create one (optionally with stages).

    docstring-name: Dunning policies
    """

    @property
    def rbac_permission(self):
        return "finance.dunning.manage" if self.request.method == "POST" \
            else "finance.dunning.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = DunningPolicy.objects.filter(entity=entity).prefetch_related("stages")
        return success_response(
            "Dunning policies retrieved.",
            data=DunningPolicySerializer(qs.order_by("name"), many=True).data,
        )

    @transaction.atomic
    def post(self, request):
        from .dunning import ensure_default_policy

        entity = resolve_entity(request)
        body = request.data or {}

        # Shortcut: seed the standard ladder when explicitly requested.
        if body.get("use_default"):
            policy = ensure_default_policy(
                entity, name=body.get("name") or "Standard reminders",
            )
            return success_response(
                f"Default dunning policy '{policy.name}' ready.",
                data=DunningPolicySerializer(policy).data, status=201,
            )

        name = (body.get("name") or "").strip()
        if not name:
            raise ValidationError({"name": "A policy name is required."})
        policy = DunningPolicy.objects.create(
            entity=entity, name=name,
            is_active=bool(body.get("is_active", True)),
            is_default=bool(body.get("is_default", False)),
        )
        for i, raw in enumerate(body.get("stages") or [], start=1):
            DunningStage.objects.create(
                policy=policy,
                level=int(raw.get("level", i)),
                name=raw.get("name") or f"Stage {i}",
                min_days_overdue=int(raw.get("min_days_overdue", 0)),
                channel=raw.get("channel") or "EMAIL",
                message=raw.get("message") or "",
            )
        return success_response(
            f"Dunning policy '{policy.name}' created.",
            data=DunningPolicySerializer(policy).data, status=201,
        )


class DunningPolicyDetailView(_FinanceBase):
    """docstring-name: Dunning policies"""
    rbac_permission = "finance.dunning.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        policy = DunningPolicy.objects.filter(entity=entity, pk=pk).first()
        if policy is None:
            raise NotFound("Dunning policy not found for this entity.")
        return success_response(
            "Dunning policy retrieved.", data=DunningPolicySerializer(policy).data,
        )


class DunningGenerateView(_FinanceBase):
    """POST: run a dunning policy over the entity's overdue invoices, raising notices.

    docstring-name: Generate dunning notices
    """

    rbac_permission = "finance.dunning.generate"

    def post(self, request):
        from .dunning import generate_dunning

        entity = resolve_entity(request)
        body = request.data or {}
        as_of = _date(body.get("as_of"), "as_of")
        policy = None
        if body.get("policy") not in (None, ""):
            policy = DunningPolicy.objects.filter(
                entity=entity, pk=body["policy"],
            ).first() if str(body["policy"]).isdigit() else \
                DunningPolicy.objects.filter(entity=entity, name=body["policy"]).first()
            if policy is None:
                raise NotFound(f"No dunning policy matches '{body['policy']}'.")
        customer = _resolve_customer(entity, body.get("customer"), required=False)

        notices = generate_dunning(
            entity, as_of=as_of, policy=policy, customer=customer,
            actor_user=request.user,
        )
        return success_response(
            f"Generated {len(notices)} dunning notice(s).",
            data={
                "created": len(notices),
                "notices": DunningNoticeSerializer(notices, many=True).data,
            },
        )


class DunningNoticeListCreateView(_FinanceBase):
    """GET dunning notices for an entity (filterable by status / customer / invoice).

    docstring-name: Dunning notices
    """

    rbac_permission = "finance.dunning.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = DunningNotice.objects.filter(entity=entity).select_related("customer", "invoice")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(notice_status=status_val)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        if (invoice := request.query_params.get("invoice")):
            qs = qs.filter(invoice=_resolve_invoice(entity, invoice))
        return success_response(
            "Dunning notices retrieved.",
            data=DunningNoticeSerializer(qs.order_by("-notice_date", "-id")[:300], many=True).data,
        )


class _DunningNoticeActionBase(_FinanceBase):
    def _notice(self, request, pk):
        entity = resolve_entity(request)
        notice = DunningNotice.objects.filter(entity=entity, pk=pk).first()
        if notice is None:
            raise NotFound("Dunning notice not found for this entity.")
        return entity, notice


class DunningNoticeDetailView(_DunningNoticeActionBase):
    """docstring-name: Dunning notices"""
    rbac_permission = "finance.dunning.view"

    def get(self, request, pk):
        _, notice = self._notice(request, pk)
        return success_response(
            "Dunning notice retrieved.", data=DunningNoticeSerializer(notice).data,
        )


class DunningNoticeSendView(_DunningNoticeActionBase):
    """docstring-name: Send a dunning notice"""
    rbac_permission = "finance.dunning.send"

    def post(self, request, pk):
        from .dunning import mark_notice_sent

        _, notice = self._notice(request, pk)
        mark_notice_sent(notice, actor_user=request.user)
        notice.refresh_from_db()
        return success_response(
            f"Dunning notice {notice.document_number} marked sent.",
            data=DunningNoticeSerializer(notice).data,
        )


class DunningNoticeCancelView(_DunningNoticeActionBase):
    """docstring-name: Cancel a dunning notice"""
    rbac_permission = "finance.dunning.send"

    def post(self, request, pk):
        from .dunning import cancel_notice

        _, notice = self._notice(request, pk)
        reason = (request.data or {}).get("reason", "")
        cancel_notice(notice, reason=reason, actor_user=request.user)
        notice.refresh_from_db()
        return success_response(
            f"Dunning notice {notice.document_number} cancelled.",
            data=DunningNoticeSerializer(notice).data,
        )
