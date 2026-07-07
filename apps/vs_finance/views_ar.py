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
from django.db.models import F, Q
from rest_framework.exceptions import NotFound, ValidationError

from core.pagination import XVSPagination
from core.response import success_response


def _paginate(request, qs, serializer_cls, view, **ser_kwargs):
    """Paginate a queryset through the platform's XVSPagination envelope.

    ``_FinanceBase`` is a plain ``APIView`` (which ignores ``pagination_class``), so
    list views call this to get the standard ``{pagination, data}`` response. Page size
    is a fixed 25 (override per-request with ?page_size=, capped at 100).
    """
    paginator = XVSPagination()
    paginator.page_size = 25
    page = paginator.paginate_queryset(qs, request, view=view)
    return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)

from .constants import DocumentStatus, FeeAppliesTo, FinanceAuditAction, FinanceAuditStatus
from .money import format_naira
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
    FinanceAuditLog,
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
    PaymentSerializer,
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


def _resolve_debit_note(entity, ref, field="debit_note"):
    """Resolve a posted DEBIT note by document number or id within ``entity``."""
    from .constants import CreditNoteKind, DocumentStatus
    from .models import CreditNote

    qs = CreditNote.objects.filter(
        entity=entity, kind=CreditNoteKind.DEBIT, status=DocumentStatus.POSTED)
    note = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(document_number=str(ref)).first()
    )
    if note is None:
        raise NotFound(f"No debit note matches '{ref}' for this entity.")
    return note


def _allocation_plan(entity, raw_allocations):
    """Coerce a request ``allocations`` list into ``[(target, amount_kobo), ...]``.

    Each item settles an invoice (``{"invoice": ref, "amount": …}``) or a DEBIT note
    (``{"debit_note": ref, "amount": …}``) — both debit AR and are settled by receipts.
    """
    if not raw_allocations:
        return None
    plan = []
    for i, item in enumerate(raw_allocations):
        if item.get("debit_note") not in (None, ""):
            target = _resolve_debit_note(entity, item.get("debit_note"), f"allocations[{i}].debit_note")
        else:
            target = _resolve_invoice(entity, item.get("invoice"), f"allocations[{i}].invoice")
        plan.append((target, _money(item.get("amount"), f"allocations[{i}].amount")))
    return plan


def _allocation_strategy(raw):
    """Validate an optional ``allocation_strategy`` request value (default 'oldest')."""
    from .receivables import ALLOCATION_STRATEGIES

    val = (raw or "oldest").lower()
    if val not in ALLOCATION_STRATEGIES:
        raise ValidationError(
            {"allocation_strategy": f"Must be one of: {', '.join(ALLOCATION_STRATEGIES)}."})
    return val


# --------------------------------------------------------------------------- #
# Customers / payers                                                          #
# --------------------------------------------------------------------------- #

def _customer_ledger(entity, customer_ids=None):
    """Net AR position per customer, in two aggregate queries (no per-row N+1).

    Returns ``{customer_id: {"outstanding", "credit", "overdue", "lifetime_paid"}}``
    where ``outstanding`` is the sum of open invoice balances and ``credit`` the
    customer's available credit (their 2140 liability position: unapplied receipts +
    unapplied CREDIT notes − refunds already paid). Net = outstanding − credit
    (positive owes, negative in credit). Computed in a few aggregate queries (no N+1).
    """
    import datetime
    from django.db.models import F, Q, Sum
    from django.db.models.functions import Coalesce

    from .constants import CreditNoteKind, DocumentStatus
    from .models import CreditNote, Invoice, Payment, Refund

    today = datetime.date.today()
    bal = F("total") - F("amount_paid") - F("amount_credited")
    inv = Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)
    pay = Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED)
    note = CreditNote.objects.filter(entity=entity, status=DocumentStatus.POSTED, kind=CreditNoteKind.CREDIT)
    # Open DEBIT notes are supplementary AR charges: their unsettled balance is
    # outstanding, exactly like an open invoice.
    dn = CreditNote.objects.filter(entity=entity, status=DocumentStatus.POSTED, kind=CreditNoteKind.DEBIT)
    ref = Refund.objects.filter(entity=entity, status=DocumentStatus.POSTED)
    if customer_ids is not None:
        inv = inv.filter(customer_id__in=customer_ids)
        pay = pay.filter(customer_id__in=customer_ids)
        note = note.filter(customer_id__in=customer_ids)
        dn = dn.filter(customer_id__in=customer_ids)
        ref = ref.filter(customer_id__in=customer_ids)

    out: dict[int, dict] = {}

    def slot(cid):
        return out.setdefault(cid, {"outstanding": 0, "overdue": False, "lifetime_paid": 0,
                                    "_receipts": 0, "_notes": 0, "_refunded": 0})

    for r in inv.values("customer_id").annotate(
        outstanding=Coalesce(Sum(bal), 0),
        overdue_bal=Coalesce(Sum(bal, filter=Q(due_date__lt=today)), 0),
    ):
        d = slot(r["customer_id"])
        d["outstanding"] = int(r["outstanding"] or 0)
        d["overdue"] = int(r["overdue_bal"] or 0) > 0
    for r in pay.values("customer_id").annotate(
        credit=Coalesce(Sum(F("amount") - F("allocated_amount")), 0),
        lifetime=Coalesce(Sum("amount"), 0),
    ):
        d = slot(r["customer_id"])
        d["_receipts"] = int(r["credit"] or 0)
        d["lifetime_paid"] = int(r["lifetime"] or 0)
    for r in note.values("customer_id").annotate(
        c=Coalesce(Sum(F("total") - F("allocated_amount")), 0)):
        slot(r["customer_id"])["_notes"] = int(r["c"] or 0)
    for r in dn.values("customer_id").annotate(
        c=Coalesce(Sum(F("total") - F("amount_paid")), 0)):
        d = slot(r["customer_id"])
        d["outstanding"] += int(r["c"] or 0)
    for r in ref.values("customer_id").annotate(c=Coalesce(Sum("amount"), 0)):
        slot(r["customer_id"])["_refunded"] = int(r["c"] or 0)

    for d in out.values():
        d["credit"] = max(0, d["_receipts"] + d["_notes"] - d["_refunded"])
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
    """Customers / payers for an entity.

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

        # Derived account-status filter. INACTIVE is the is_active column; ACTIVE/CREDIT/
        # OVERDUE come from the ledger (not a column), so resolve them for the active set
        # and keep matching ids before paginating (a few aggregate queries, no N+1).
        status_f = request.query_params.get("status")
        if status_f == "INACTIVE":
            qs = qs.filter(is_active=False)
        elif status_f in ("ACTIVE", "CREDIT", "OVERDUE"):
            base_ids = list(qs.filter(is_active=True).values_list("id", flat=True))
            led_all = _customer_ledger(entity, base_ids)
            keep = [
                cid for cid in base_ids
                if _account_status(
                    (l := led_all.get(cid, {})).get("outstanding", 0) - l.get("credit", 0),
                    l.get("overdue", False),
                ) == status_f
            ]
            qs = qs.filter(id__in=keep)

        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs.order_by("code"), request, view=self)
        ledger = _customer_ledger(entity, [c.id for c in page])
        rows = []
        for c in page:
            row = CustomerSerializer(c).data
            led = ledger.get(c.id, {})
            net = led.get("outstanding", 0) - led.get("credit", 0)
            row["balance"] = net                      # signed kobo: + owes, − in credit
            row["balance_naira"] = format_naira(net)
            row["account_status"] = _account_status(net, led.get("overdue", False))
            rows.append(row)
        return paginator.get_paginated_response(rows)

    @transaction.atomic
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip().upper()
        # TODO: code should be created automatically if it wasn't provided.
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
        # Seat any opening balance as a posted opening invoice (Dr AR / Cr Retained
        # Earnings) so it shows in the customer's outstanding and the GL. Inside this
        # atomic block, so a posting failure (e.g. no open period) rolls the whole
        # customer-create back with a clear error.
        from .receivables import post_opening_balance
        # An optional historical opening_date backdates the opening invoice + its journal
        # (falls back to today inside the service); the posting guards roll the whole
        # create back if that date lands in a closed/missing period.
        post_opening_balance(
            customer, actor_user=request.user,
            date=_date(body.get("opening_date"), "opening_date"),
        )
        return success_response(
            f"Customer {customer.code} created.",
            data=CustomerSerializer(customer).data, status=201,
        )


class CustomerDetailView(_FinanceBase):
    """Get the details of one customer (by **code or id**).

    docstring-name: Customers
    """

    @property
    def rbac_permission(self):
        return "finance.customer.update" if self.request.method == "PATCH" \
            else "finance.customer.view"

    def get(self, request, pk):
        import datetime

        from .constants import CreditNoteKind, DocumentStatus, InvoicePaymentStatus
        from .models import CreditNote, Invoice, Payment

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
        # DEBIT notes are supplementary AR charges — they belong in the statement (debit
        # side) and their unsettled balance is an open item, just like an invoice.
        debit_notes = list(CreditNote.objects.filter(
            entity=entity, customer=customer, status=DocumentStatus.POSTED,
            kind=CreditNoteKind.DEBIT,
        ).order_by("note_date", "id")[:500])

        def inv_status(i):
            if i.payment_status == InvoicePaymentStatus.PAID:
                return "PAID"
            if i.due_date and i.due_date < today and i.balance_due > 0:
                return "OVERDUE"
            if i.payment_status == InvoicePaymentStatus.PARTIAL:
                return "PARTIAL"
            return "ISSUED"

        def dn_status(n):
            if n.settlement_status == InvoicePaymentStatus.PAID:
                return "PAID"
            if n.settlement_status == InvoicePaymentStatus.PARTIAL:
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
        open_debit_notes = [
            {
                "document_number": n.document_number,
                "note_date": n.note_date.isoformat() if n.note_date else None,
                "total": _money_obj(n.total), "balance": _money_obj(n.balance_due),
                "status": dn_status(n),
            }
            for n in debit_notes if n.balance_due > 0
        ]

        # Transactions: invoices + debit notes (debit) and receipts (credit),
        # reverse-chronological (newest first).
        transactions = (
            [{"date": i.invoice_date.isoformat(), "type": "INVOICE",
              "reference": i.document_number, "amount": _money_obj(i.total),
              "status": inv_status(i)} for i in invoices]
            + [{"date": n.note_date.isoformat(), "type": "DEBIT_NOTE",
                "reference": n.document_number, "amount": _money_obj(n.total),
                "status": dn_status(n)} for n in debit_notes]
            + [{"date": p.payment_date.isoformat(), "type": "PAYMENT",
                "reference": p.document_number, "amount": _money_obj(p.amount),
                "status": "POSTED"} for p in payments]
        )
        transactions.sort(key=lambda t: t["date"], reverse=True)

        # Statement: invoices + debit notes (debit) and receipts (credit),
        # chronological (oldest first), with a running balance. An opening balance is
        # already materialised as a posted OPENING invoice (see post_opening_balance)
        # and rides in `invoices` below — we must NOT also add a synthetic opening row
        # or the balance double-counts. This mirrors reports.customer_statement, which
        # is likewise document-driven.
        events = []
        events += [(i.invoice_date, f"Invoice {i.document_number}", i.total, 0) for i in invoices]
        events += [(n.note_date, f"Debit note {n.document_number}", n.total, 0) for n in debit_notes]
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
            "open_debit_notes": open_debit_notes,
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
        auto = body.get("auto_allocate", True)
        if isinstance(auto, str):
            auto = auto.lower() not in ("false", "0", "no")
        post_payment(payment, actor_user=request.user, auto_allocate=bool(auto),
                     strategy=_allocation_strategy(body.get("allocation_strategy")))
        return success_response(
            f"Receipt {payment.document_number} recorded for {customer.code}.",
            data={
                "id": payment.id,
                "payment": payment.document_number,
                "allocated": payment.allocated_amount,
                "unallocated": payment.unallocated_amount,
            },
            status=201,
        )


# --------------------------------------------------------------------------- #
# Receipts & allocation                                                       #
# --------------------------------------------------------------------------- #

class CustomerSummaryView(_FinanceBase):
    """GET /finance/customers/summary/ — entity-wide KPI totals + status counts for the
    Customers header cards (computed over ALL rows, so they stay accurate while the list
    itself paginates). Honors the same ``?search=``/``?is_active=`` as the list.

    docstring-name: Customer summary
    """

    rbac_permission = "finance.customer.view"

    def get(self, request):
        from django.db.models import Q

        entity = resolve_entity(request)
        qs = Customer.objects.filter(entity=entity)
        if (search := request.query_params.get("search")):
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")

        custs = list(qs.values("id", "is_active"))
        ledger = _customer_ledger(entity, [c["id"] for c in custs])
        receivable = 0
        on_credit = 0
        counts = {"ACTIVE": 0, "CREDIT": 0, "OVERDUE": 0, "INACTIVE": 0}
        for c in custs:
            led = ledger.get(c["id"], {})
            net = led.get("outstanding", 0) - led.get("credit", 0)
            status = "INACTIVE" if not c["is_active"] else _account_status(net, led.get("overdue", False))
            counts[status] += 1
            if net > 0:
                receivable += net
            elif net < 0:
                on_credit += 1
        return success_response("Customer summary retrieved.", data={
            "total": len(custs),
            "receivable": _money_obj(receivable),
            "on_credit": on_credit,
            "overdue": counts["OVERDUE"],
            "status_counts": counts,
        })


class PaymentListView(_FinanceBase):
    """GET /finance/payments/ — customer receipts and their allocation state.

    Filters: ``?status=`` (ALLOCATED|PARTIAL|UNALLOCATED), ``?method=``,
    ``?customer=`` (code/id), ``?search=`` (doc no / customer / reference).

    docstring-name: Customer receipts
    """

    rbac_permission = "finance.payment.view"

    def get(self, request):
        from django.db.models import Q

        from .constants import DocumentStatus
        from .models import Payment

        entity = resolve_entity(request)
        qs = (Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED)
              .select_related("customer", "deposit_account"))
        if (method := request.query_params.get("method")):
            qs = qs.filter(method=method)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        if (search := request.query_params.get("search")):
            qs = qs.filter(
                Q(document_number__icontains=search) | Q(customer__name__icontains=search)
                | Q(customer__code__icontains=search) | Q(reference__icontains=search))

        # allocation_status is derived from allocated_amount vs amount; express it as a
        # DB filter so paging counts are correct (it used to filter post-slice in Python).
        status_f = request.query_params.get("status")
        if status_f == "ALLOCATED":
            qs = qs.filter(allocated_amount__gte=F("amount"))
        elif status_f == "UNALLOCATED":
            qs = qs.filter(allocated_amount__lte=0)
        elif status_f == "PARTIAL":
            qs = qs.filter(allocated_amount__gt=0, allocated_amount__lt=F("amount"))
        return _paginate(request, qs.order_by("-payment_date", "-id"), PaymentSerializer, self)


class PaymentSummaryView(_FinanceBase):
    """GET /finance/payments/summary/ — receipts KPI totals + allocation-status counts
    for the header cards, over ALL rows (accurate while the list paginates). Honors the
    same ``?method=``/``?customer=``/``?search=`` as the list.

    docstring-name: Receipts summary
    """

    rbac_permission = "finance.payment.view"

    def get(self, request):
        import datetime

        from django.db.models import Count, F, Q, Sum
        from django.db.models.functions import Coalesce

        from .constants import DocumentStatus
        from .models import Payment

        entity = resolve_entity(request)
        qs = (Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED))
        if (method := request.query_params.get("method")):
            qs = qs.filter(method=method)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        if (search := request.query_params.get("search")):
            qs = qs.filter(
                Q(document_number__icontains=search) | Q(customer__name__icontains=search)
                | Q(customer__code__icontains=search) | Q(reference__icontains=search))

        today = datetime.date.today()
        week_start = today - datetime.timedelta(days=6)  # 7-day window incl. today
        agg = qs.aggregate(
            count=Count("id"),
            today=Coalesce(Sum("amount", filter=Q(payment_date=today)), 0),
            week=Coalesce(Sum("amount", filter=Q(payment_date__gte=week_start)), 0),
            unallocated=Coalesce(Sum(F("amount") - F("allocated_amount")), 0),
            allocated_c=Count("id", filter=Q(allocated_amount__gte=F("amount"))),
            unallocated_c=Count("id", filter=Q(allocated_amount__lte=0)),
            partial_c=Count("id", filter=Q(allocated_amount__gt=0, allocated_amount__lt=F("amount"))),
        )
        return success_response("Receipts summary retrieved.", data={
            "count": agg["count"],
            "today": _money_obj(agg["today"]),
            "week": _money_obj(agg["week"]),
            "unallocated": _money_obj(agg["unallocated"]),
            "status_counts": {
                "ALLOCATED": agg["allocated_c"],
                "PARTIAL": agg["partial_c"],
                "UNALLOCATED": agg["unallocated_c"],
            },
        })


class PaymentDetailView(_FinanceBase):
    """GET /finance/payments/<id>/ — a receipt, its current allocations, the
    customer's open invoices (allocation candidates) and the receipt's GL posting.

    docstring-name: Customer receipts
    """

    rbac_permission = "finance.payment.view"

    def get(self, request, pk):
        from .constants import CreditNoteKind, DocumentStatus, InvoicePaymentStatus
        from .models import CreditNote, Invoice, Payment

        entity = resolve_entity(request)
        p = (Payment.objects.filter(entity=entity, pk=pk)
             .select_related("customer", "deposit_account", "journal")
             .prefetch_related("allocations__invoice", "debit_note_allocations__note",
                               "journal__lines__account").first())
        if p is None:
            raise NotFound("Receipt not found for this entity.")

        allocations = [
            {"invoice": a.invoice.document_number, "invoice_id": a.invoice_id,
             "amount": _money_obj(a.amount)}
            for a in p.allocations.all()
        ]
        allocations += [
            {"debit_note": a.note.document_number, "debit_note_id": a.note_id,
             "amount": _money_obj(a.amount)}
            for a in p.debit_note_allocations.all()
        ]
        open_invoices = [
            {"id": i.id, "document_number": i.document_number,
             "due_date": i.due_date.isoformat() if i.due_date else None,
             "balance": _money_obj(i.balance_due)}
            for i in Invoice.objects.filter(
                entity=entity, customer=p.customer, status=DocumentStatus.POSTED,
            ).exclude(payment_status=InvoicePaymentStatus.PAID).order_by("due_date", "invoice_date", "id")
            if i.balance_due > 0
        ]
        # DEBIT notes are settleable AR items too — offer the customer's open ones.
        open_debit_notes = [
            {"id": n.id, "document_number": n.document_number,
             "note_date": n.note_date.isoformat() if n.note_date else None,
             "balance": _money_obj(n.balance_due)}
            for n in CreditNote.objects.filter(
                entity=entity, customer=p.customer, status=DocumentStatus.POSTED,
                kind=CreditNoteKind.DEBIT,
            ).exclude(settlement_status=InvoicePaymentStatus.PAID).order_by("note_date", "id")
            if n.balance_due > 0
        ]
        gl_postings = []
        if p.journal_id:
            for gl in p.journal.lines.all():
                gl_postings.append({
                    "account_code": gl.account.code, "account_name": gl.account.name,
                    "debit": _money_obj(gl.debit), "credit": _money_obj(gl.credit),
                })
        return success_response("Receipt retrieved.", data={
            "payment": PaymentSerializer(p).data,
            "allocations": allocations,
            "open_invoices": open_invoices,
            "open_debit_notes": open_debit_notes,
            "gl_postings": gl_postings,
        })


class PaymentAllocateView(_FinanceBase):
    """POST /finance/payments/<id>/allocate/ — apply a receipt to open invoices.

    Body ``{allocations:[{invoice, amount}]}`` for an explicit split, or
    ``{auto_allocate:true}`` to settle oldest-first. Each amount is capped at the
    invoice balance and the receipt's remaining cash; excess stays as credit.

    docstring-name: Allocate a receipt
    """

    rbac_permission = "finance.payment.allocate"

    @transaction.atomic
    def post(self, request, pk):
        from .models import Payment
        from .receivables import allocate_payment

        entity = resolve_entity(request)
        p = Payment.objects.filter(entity=entity, pk=pk).first()
        if p is None:
            raise NotFound("Receipt not found for this entity.")
        body = request.data or {}
        plan = _allocation_plan(entity, body.get("allocations"))
        if plan:
            allocate_payment(p, allocations=plan, actor_user=request.user)
        elif body.get("auto_allocate"):
            allocate_payment(p, actor_user=request.user,
                             strategy=_allocation_strategy(body.get("allocation_strategy")))
        else:
            raise ValidationError({"allocations": "Provide allocations or auto_allocate=true."})
        p.refresh_from_db()
        return success_response(
            f"Receipt {p.document_number} allocated.",
            data=PaymentSerializer(p).data,
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
            code=str(item.get("code", "")).strip()[:32],
            description=str(item.get("description", "")).strip() or f"Fee {i}",
            revenue_account=_resolve_account(
                entity, item.get("revenue_account"), f"items[{i}].revenue_account", required=True),
            amount=amount,
            tax_code=_resolve_tax(entity, item.get("tax_code"), f"items[{i}].tax_code"),
            is_optional=bool(item.get("is_optional", False)),
        )


def _resolve_applies_to(raw):
    """Validate a fee-structure ``applies_to`` value, defaulting to CUSTOMER."""
    if raw in (None, ""):
        return FeeAppliesTo.CUSTOMER
    value = str(raw).upper()
    if value not in FeeAppliesTo.values:
        raise ValidationError({"applies_to":
            f"Must be one of {', '.join(FeeAppliesTo.values)}."})
    return value


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
    """Fee structures for an entity. Invoices can only be created from **active** structures. 
    The structure's ``applies_to`` determines whether it can be used for a customer, a vendor, etc. 
    Multiple structures can be active at once, but each must have a unique code. Each structure has one 
    or more fee items (lines) with a description, revenue account, amount and optional tax code.

    POST body: ``{code, name, applies_to?, description?, is_active?, items:[{description,
    revenue_account, amount, tax_code?}]}``.

    docstring-name: Fee structures
    """

    @property
    def rbac_permission(self):
        return "finance.feestructure.create" if self.request.method == "POST" \
            else "finance.feestructure.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = FeeStructure.objects.filter(entity=entity).prefetch_related(
            "items__revenue_account", "items__tax_code")
        if (active := request.query_params.get("is_active")) in ("true", "false"):
            qs = qs.filter(is_active=active == "true")
        if (applies_to := request.query_params.get("applies_to")):
            qs = qs.filter(applies_to=applies_to.upper())
        if (search := request.query_params.get("search")):
            from django.db.models import Q
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        return success_response(
            "Fee structures retrieved.",
            data=FeeStructureSerializer(qs.order_by("-created_at", "code"), many=True).data,
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
            applies_to=_resolve_applies_to(body.get("applies_to")),
            description=body.get("description", ""),
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
            "Fee structure retrieved.",
            data=FeeStructureSerializer(structure, context={"with_usage": True}).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        entity = resolve_entity(request)
        structure = _resolve_fee_structure(entity, pk)
        body = request.data or {}
        for field in ("name", "description"):
            if field in body:
                setattr(structure, field, body[field])
        if "applies_to" in body:
            structure.applies_to = _resolve_applies_to(body.get("applies_to"))
        if "is_active" in body:
            structure.is_active = bool(body.get("is_active"))
        structure.save()
        if "items" in body:  # full replace
            structure.items.all().delete()
            _build_fee_items(structure, entity, body.get("items"))
        structure.refresh_from_db()
        return success_response(
            f"Fee structure {structure.code} updated.",
            data=FeeStructureSerializer(structure, context={"with_usage": True}).data,
        )


class FeeStructureDuplicateView(_FinanceBase):
    """Clone a fee structure (code + lines) into a new **draft** structure.

    Body: ``{code, name?}`` — a new unique code is required; the clone copies
    applies_to, description and every line (incl. fee code / optional flag) and is
    created **inactive** so it can be reviewed before use.

    docstring-name: Fee structures
    """

    rbac_permission = "finance.feestructure.create"

    @transaction.atomic
    def post(self, request, pk):
        entity = resolve_entity(request)
        source = _resolve_fee_structure(entity, pk)
        body = request.data or {}
        new_code = str(body.get("code", "")).strip().upper()
        if not new_code:
            raise ValidationError({"code": "A code for the new structure is required."})
        if FeeStructure.objects.filter(entity=entity, code=new_code).exists():
            raise ValidationError({"code": f"A fee structure with code '{new_code}' already exists."})
        clone = FeeStructure.objects.create(
            entity=entity, code=new_code,
            name=str(body.get("name", "")).strip() or f"{source.name} (copy)",
            applies_to=source.applies_to, description=source.description,
            is_active=False, created_by=request.user,
        )
        for item in source.items.all():
            FeeItem.objects.create(
                structure=clone, line_no=item.line_no, code=item.code,
                description=item.description, revenue_account=item.revenue_account,
                amount=item.amount, tax_code=item.tax_code, is_optional=item.is_optional,
            )
        clone.refresh_from_db()
        return success_response(
            f"Fee structure {clone.code} created from {source.code}.",
            data=FeeStructureSerializer(clone, context={"with_usage": True}).data, status=201,
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
        if structure.applies_to != FeeAppliesTo.CUSTOMER:
            raise ValidationError({"applies_to":
                "Only customer fee structures can generate AR invoices."})
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
        qs = (CreditNote.objects.filter(entity=entity)
              .select_related("customer", "invoice").prefetch_related("lines"))
        if (kind := request.query_params.get("kind")):
            qs = qs.filter(kind=kind)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        if (search := (request.query_params.get("search") or "").strip()):
            qs = qs.filter(
                Q(document_number__icontains=search) | Q(reason__icontains=search)
                | Q(customer__name__icontains=search) | Q(customer__code__icontains=search)
            )
        # Derived status: applied = a fully-allocated credit note; issued = any other
        # posted note; draft = not yet posted.
        applied_q = Q(kind="CREDIT", allocated_amount__gt=0) & Q(allocated_amount__gte=F("total"))
        status_val = (request.query_params.get("status") or "").lower()
        if status_val == "draft":
            qs = qs.exclude(status=DocumentStatus.POSTED)
        elif status_val == "applied":
            qs = qs.filter(status=DocumentStatus.POSTED).filter(applied_q)
        elif status_val == "issued":
            qs = qs.filter(status=DocumentStatus.POSTED).exclude(applied_q)
        return _paginate(request, qs.order_by("-note_date", "-id"), CreditNoteSerializer, self)

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
    """GET /finance/credit-notes/<id>/ — retrieve one credit or debit note (by id),
    with its lines and current allocation state.

    docstring-name: Credit notes
    """
    rbac_permission = "finance.creditnote.view"

    def get(self, request, pk):
        _, note = self._note(request, pk)
        return success_response(
            "Credit note retrieved.", data=CreditNoteSerializer(note).data,
        )


class CreditNotePostView(_CreditNoteActionBase):
    """POST /finance/credit-notes/<id>/post/ — post a draft credit/debit note to the GL.

    Body ``{allocations:[{invoice, amount}]}`` for an explicit split, or
    ``{auto_allocate:true}`` (the default when no allocations are given) to apply a
    CREDIT note oldest-first against the customer's open invoices. A debit note raises
    the receivable; a credit note reduces it and settles/credits the invoices.

    docstring-name: Post a credit note
    """
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
    """POST /finance/credit-notes/<id>/allocate/ — apply an already-posted CREDIT note to
    the customer's open invoices. Body ``{allocations:[{invoice, amount}]}``; each amount
    is capped at the invoice balance and the note's unallocated remainder.

    docstring-name: Allocate a credit note
    """
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
        return _paginate(
            request, qs.order_by("-refund_date", "-id"), RefundSerializer, self)

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
    """GET /finance/refunds/<id>/ — retrieve one customer refund (by id).

    docstring-name: Refunds
    """
    rbac_permission = "finance.refund.view"

    def get(self, request, pk):
        _, refund = self._refund(request, pk)
        return success_response("Refund retrieved.", data=RefundSerializer(refund).data)


class RefundPostView(_RefundActionBase):
    """POST /finance/refunds/<id>/post/ — post a draft refund, paying the customer's
    credit back out (Dr customer credit / Cr bank) and recording the GL journal.

    docstring-name: Post a refund
    """
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


def _writeoff_rows(entity, *, limit=1000):
    """Normalised bad-debt write-off rows from the finance audit log."""
    logs = list(
        FinanceAuditLog.objects.filter(
            entity=entity, action=FinanceAuditAction.INVOICE_WRITTEN_OFF,
            status=FinanceAuditStatus.SUCCESS,
        ).order_by("-created_at", "-id")[:limit]
    )
    need_ids = [int(l.target_id) for l in logs
                if not l.metadata.get("customer_code") and str(l.target_id).isdigit()]
    invs = {i.id: i for i in Invoice.objects.filter(id__in=need_ids).select_related("customer")} \
        if need_ids else {}
    rows = []
    for l in logs:
        inv = invs.get(int(l.target_id)) if str(l.target_id).isdigit() else None
        rows.append({
            "key": f"W{l.id}", "kind": "WRITEOFF", "reference": l.document_number,
            "date": l.created_at.date().isoformat(),
            "customer_code": l.metadata.get("customer_code") or (inv.customer.code if inv else ""),
            "customer_name": l.metadata.get("customer_name") or (inv.customer.name if inv else "—"),
            "reason": l.metadata.get("narration") or "Bad-debt write-off",
            "amount": int(l.metadata.get("amount") or 0), "amount_naira": format_naira(int(l.metadata.get("amount") or 0)),
            "status": "POSTED", "refund_id": None,
        })
    return rows


class ARAdjustmentListView(_FinanceBase):
    """GET /finance/ar-adjustments/ — unified customer refunds + bad-debt write-offs.

    Filters: ``?type=(refund|writeoff)`` and ``?search=``. The merged list is sorted
    by date and paginated; KPI totals (written-off YTD, pending refund count) ride
    in the response so they stay accurate across pages.

    docstring-name: Refunds & write-offs
    """

    rbac_permission = "finance.refund.view"

    def get(self, request):
        import math
        from rest_framework.response import Response
        from django.utils import timezone

        entity = resolve_entity(request)
        type_f = (request.query_params.get("type") or "").lower()
        search = (request.query_params.get("search") or "").strip().lower()

        refund_rows = []
        for r in (Refund.objects.filter(entity=entity).select_related("customer")
                  .order_by("-refund_date", "-id")[:1000]):
            refund_rows.append({
                "key": f"R{r.id}", "kind": "REFUND", "reference": r.document_number,
                "date": r.refund_date.isoformat() if r.refund_date else "",
                "customer_code": r.customer.code, "customer_name": r.customer.name,
                "reason": r.narration or "Customer refund", "amount": r.amount,
                "amount_naira": format_naira(r.amount), "status": r.status, "refund_id": r.id,
            })
        writeoff_rows = _writeoff_rows(entity)

        # KPI totals — from the full sets, independent of the type filter / page.
        year = timezone.now().year
        written_off_ytd = sum(w["amount"] for w in writeoff_rows if w["date"][:4] == str(year))
        pending = Refund.objects.filter(entity=entity).exclude(status=DocumentStatus.POSTED).count()

        rows = []
        if type_f in ("", "refund"):
            rows += refund_rows
        if type_f in ("", "writeoff"):
            rows += writeoff_rows
        if search:
            rows = [x for x in rows if any(
                search in (x.get(k) or "").lower()
                for k in ("reference", "customer_name", "customer_code", "reason"))]
        rows.sort(key=lambda x: x["date"], reverse=True)

        page = max(int(request.query_params.get("page", 1) or 1), 1)
        page_size = min(max(int(request.query_params.get("page_size", 20) or 20), 1), 100)
        total = len(rows)
        total_pages = math.ceil(total / page_size) if total else 1
        start = (page - 1) * page_size
        return Response({
            "success": True,
            "message": "AR adjustments retrieved.",
            "pagination": {
                "currentPage": page, "pageSize": page_size, "totalItems": total,
                "totalPages": total_pages, "next": None, "previous": None,
            },
            "kpis": {"written_off_ytd": written_off_ytd, "pending": pending},
            "data": rows[start:start + page_size],
        })


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
        if (search := (request.query_params.get("search") or "").strip()):
            qs = qs.filter(
                Q(document_number__icontains=search) | Q(reason__icontains=search)
                | Q(invoice__document_number__icontains=search)
                | Q(customer__name__icontains=search) | Q(customer__code__icontains=search)
            )
        paginator = XVSPagination()
        page = paginator.paginate_queryset(qs.order_by("-concession_date", "-id"), request, view=self)
        return paginator.get_paginated_response(ConcessionSerializer(page, many=True).data)

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
    """GET /finance/concessions/<id>/ — retrieve one concession (discount / waiver /
    scholarship) by id.

    docstring-name: Concessions
    """
    rbac_permission = "finance.concession.view"

    def get(self, request, pk):
        _, concession = self._concession(request, pk)
        return success_response(
            "Concession retrieved.", data=ConcessionSerializer(concession).data,
        )


class ConcessionPostView(_ConcessionActionBase):
    """POST /finance/concessions/<id>/post/ — post a draft concession, writing the
    discount/waiver/scholarship off against the allowance account (Dr allowance / Cr AR)
    so it reduces the linked invoice's balance and the customer's outstanding.

    docstring-name: Post a concession
    """
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


class ConcessionSummaryView(_FinanceBase):
    """GET /finance/concessions/summary/ — KPI totals (kobo) for the header cards.

    docstring-name: Concession summary
    """

    rbac_permission = "finance.concession.view"

    def get(self, request):
        from django.db.models import Sum
        from django.utils import timezone

        entity = resolve_entity(request)
        qs = Concession.objects.filter(entity=entity)
        posted_ytd = qs.filter(
            status=DocumentStatus.POSTED, concession_date__year=timezone.now().year,
        ).aggregate(s=Sum("amount"))["s"] or 0
        draft_pending = qs.filter(status=DocumentStatus.DRAFT).aggregate(s=Sum("amount"))["s"] or 0
        return success_response("Concession summary retrieved.", data={
            "posted_ytd": int(posted_ytd),
            "draft_pending": int(draft_pending),
            "active_count": qs.count(),
        })


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
        if (search := (request.query_params.get("search") or "").strip()):
            qs = qs.filter(
                Q(document_number__icontains=search) | Q(invoice__document_number__icontains=search)
                | Q(customer__name__icontains=search) | Q(customer__code__icontains=search)
            )
        paginator = XVSPagination()
        page = paginator.paginate_queryset(qs.order_by("-start_date", "-id"), request, view=self)
        return paginator.get_paginated_response(PaymentPlanSerializer(page, many=True).data)

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
    """GET /finance/payment-plans/<id>/ — retrieve one installment payment plan (by id),
    including its scheduled installments and progress.

    docstring-name: Payment plans
    """
    rbac_permission = "finance.paymentplan.view"

    def get(self, request, pk):
        _, plan = self._plan(request, pk)
        return success_response("Payment plan retrieved.", data=PaymentPlanSerializer(plan).data)


class PaymentPlanActivateView(_PaymentPlanActionBase):
    """POST /finance/payment-plans/<id>/activate/ — move a draft plan into ACTIVE so its
    installment schedule becomes live and can be tracked against customer receipts.

    docstring-name: Activate a payment plan
    """
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
    """POST /finance/payment-plans/<id>/refresh/ — recompute the plan's progress, marking
    installments paid and advancing plan status. Body may carry a ``settled_amount``
    (kobo) to apply against the schedule; omit it to just re-derive from what's settled.

    docstring-name: Refresh payment plan status
    """
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
    """POST /finance/payment-plans/<id>/cancel/ — cancel a plan, closing out its remaining
    installments so it no longer tracks against the customer's balance.

    docstring-name: Cancel a payment plan
    """
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

def _normalize_channels(raw):
    """Coerce a stage channel input (CSV string or list) into a normalised CSV of
    valid DunningChannel values, in enum order, deduped; defaults to EMAIL."""
    from .constants import DunningChannel

    parts = ([str(x).strip().upper() for x in raw] if isinstance(raw, (list, tuple))
             else [p.strip().upper() for p in str(raw or "").split(",")])
    chosen = [c for c in DunningChannel.values if c in parts]
    return ",".join(chosen) if chosen else DunningChannel.EMAIL


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
                channel=_normalize_channels(raw.get("channel")),
                message=raw.get("message") or "",
            )
        return success_response(
            f"Dunning policy '{policy.name}' created.",
            data=DunningPolicySerializer(policy).data, status=201,
        )


class DunningPolicyDetailView(_FinanceBase):
    """GET / PATCH one dunning policy (by id). PATCH updates name / active / default and,
    if ``stages`` is given, replaces the whole reminder ladder.

    docstring-name: Dunning policies
    """

    @property
    def rbac_permission(self):
        return "finance.dunning.manage" if self.request.method == "PATCH" \
            else "finance.dunning.view"

    def _policy(self, request, pk):
        policy = DunningPolicy.objects.filter(entity=resolve_entity(request), pk=pk).first()
        if policy is None:
            raise NotFound("Dunning policy not found for this entity.")
        return policy

    def get(self, request, pk):
        return success_response(
            "Dunning policy retrieved.", data=DunningPolicySerializer(self._policy(request, pk)).data,
        )

    @transaction.atomic
    def patch(self, request, pk):
        """Update a policy's name / active / default; pass ``stages`` to replace the ladder."""
        policy = self._policy(request, pk)
        body = request.data or {}
        if (name := (body.get("name") or "").strip()):
            policy.name = name
        if "is_active" in body:
            policy.is_active = bool(body["is_active"])
        if body.get("is_default"):
            DunningPolicy.objects.filter(entity=policy.entity, is_default=True).exclude(pk=policy.pk).update(is_default=False)
            policy.is_default = True
        elif "is_default" in body:
            policy.is_default = False
        policy.save()
        if "stages" in body:
            policy.stages.all().delete()
            for i, raw in enumerate(body.get("stages") or [], start=1):
                DunningStage.objects.create(
                    policy=policy, level=int(raw.get("level", i)),
                    name=raw.get("name") or f"Stage {i}",
                    min_days_overdue=int(raw.get("min_days_overdue", 0)),
                    channel=_normalize_channels(raw.get("channel")), message=raw.get("message") or "",
                )
        policy.refresh_from_db()
        return success_response(
            f"Dunning policy '{policy.name}' updated.", data=DunningPolicySerializer(policy).data,
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


class DunningSummaryView(_FinanceBase):
    """GET /finance/dunning/summary/ — open-receivable aging buckets for the header.

    ``due_soon`` is the next 7 days (not yet overdue); the rest are days past due.

    docstring-name: Dunning summary
    """

    rbac_permission = "finance.dunning.view"

    def get(self, request):
        import datetime

        from django.db.models import F

        entity = resolve_entity(request)
        today = datetime.date.today()
        buckets = {k: {"amount": 0, "count": 0} for k in
                   ("due_soon", "overdue_1_30", "overdue_31_60", "overdue_60_plus")}
        # Drop fully-settled invoices in SQL (balance_due is a property); only the
        # date-bucketing is left to Python, over the still-owing set.
        balance = F("total") - F("amount_paid") - F("amount_credited")
        owing = (Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)
                 .exclude(due_date__isnull=True)
                 .annotate(_balance=balance).filter(_balance__gt=0)
                 .only("due_date", "total", "amount_paid", "amount_credited"))
        for inv in owing:
            bal = inv.balance_due
            d = (today - inv.due_date).days  # >0 overdue, <=0 upcoming
            if -7 <= d <= 0:
                key = "due_soon"
            elif 1 <= d <= 30:
                key = "overdue_1_30"
            elif 31 <= d <= 60:
                key = "overdue_31_60"
            elif d > 60:
                key = "overdue_60_plus"
            else:
                continue
            buckets[key]["amount"] += bal
            buckets[key]["count"] += 1
        return success_response("Dunning summary retrieved.", data=buckets)


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
        return _paginate(
            request, qs.order_by("-notice_date", "-id"), DunningNoticeSerializer, self)


class _DunningNoticeActionBase(_FinanceBase):
    def _notice(self, request, pk):
        entity = resolve_entity(request)
        notice = DunningNotice.objects.filter(entity=entity, pk=pk).first()
        if notice is None:
            raise NotFound("Dunning notice not found for this entity.")
        return entity, notice


class DunningNoticeDetailView(_DunningNoticeActionBase):
    """GET /finance/dunning-notices/<id>/ — retrieve one dunning (reminder) notice by id.

    docstring-name: Dunning notices
    """
    rbac_permission = "finance.dunning.view"

    def get(self, request, pk):
        _, notice = self._notice(request, pk)
        return success_response(
            "Dunning notice retrieved.", data=DunningNoticeSerializer(notice).data,
        )


class DunningNoticeSendView(_DunningNoticeActionBase):
    """POST /finance/dunning-notices/<id>/send/ — dispatch a pending notice over its
    stage's channels (in-app + email) and mark it SENT.

    docstring-name: Send a dunning notice
    """
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
    """POST /finance/dunning-notices/<id>/cancel/ — cancel a notice before it goes out,
    recording an optional ``reason``.

    docstring-name: Cancel a dunning notice
    """
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
