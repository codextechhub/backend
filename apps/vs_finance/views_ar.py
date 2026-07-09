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
from __future__ import annotations  # Import dependency used by this finance module.

from django.db import transaction  # Import dependency used by this finance module.
from django.db.models import F, Q  # Import dependency used by this finance module.
from django.http import HttpResponse  # Import dependency used by this finance module.
from rest_framework.exceptions import NotFound, ValidationError  # Import dependency used by this finance module.
from rest_framework.response import Response  # Import dependency used by this finance module.

from core.pagination import XVSPagination  # Import dependency used by this finance module.
from core.response import success_response  # Import dependency used by this finance module.


def _paginate(request, qs, serializer_cls, view, **ser_kwargs):  # Function handles this finance operation.
    """Paginate a queryset through the platform's XVSPagination envelope.

    ``_FinanceBase`` is a plain ``APIView`` (which ignores ``pagination_class``), so
    list views call this to get the standard ``{pagination, data}`` response. Page size
    is a fixed 25 (override per-request with ?page_size=, capped at 100).
    """
    paginator = XVSPagination()  # Store intermediate finance value.
    paginator.page_size = 25  # Store intermediate finance value.
    page = paginator.paginate_queryset(qs, request, view=view)  # Store intermediate finance value.
    return paginator.get_paginated_response(serializer_cls(page, many=True, **ser_kwargs).data)  # Return the computed finance response.

from .constants import DocumentStatus, FeeAppliesTo, FinanceAuditAction, FinanceAuditStatus  # Import dependency used by this finance module.
from .money import format_naira  # Import dependency used by this finance module.
from .models import (  # Import dependency used by this finance module.
    Concession,  # Finance processing step.
    CreditNote,  # Finance processing step.
    CreditNoteLine,  # Finance processing step.
    Customer,  # Finance processing step.
    DunningNotice,  # Finance processing step.
    DunningPolicy,  # Finance processing step.
    DunningStage,  # Finance processing step.
    FeeItem,  # Finance processing step.
    FeeStructure,  # Finance processing step.
    FinanceAuditLog,  # Finance processing step.
    Invoice,  # Finance processing step.
    PaymentPlan,  # Finance processing step.
    Refund,  # Finance processing step.
    WriteOffRequest,  # Finance processing step.
)  # Continue structured finance payload.
from .serializers import (  # Import dependency used by this finance module.
    ConcessionSerializer,  # Finance processing step.
    CreditNoteSerializer,  # Finance processing step.
    CustomerSerializer,  # Finance processing step.
    DunningNoticeSerializer,  # Finance processing step.
    DunningPolicySerializer,  # Finance processing step.
    FeeStructureSerializer,  # Finance processing step.
    InvoiceSerializer,  # Finance processing step.
    PaymentPlanSerializer,  # Finance processing step.
    PaymentSerializer,  # Finance processing step.
    RefundSerializer,  # Finance processing step.
    WriteOffRequestSerializer,  # Finance processing step.
)  # Continue structured finance payload.
from .views import resolve_entity  # Import dependency used by this finance module.
from .views_ops import (  # Import dependency used by this finance module.
    _FinanceBase,  # Finance processing step.
    _date,  # Finance processing step.
    _money,  # Finance processing step.
    _dec,  # Finance processing step.
    _require_lines,  # Finance processing step.
    _resolve_account,  # Finance processing step.
    _resolve_bank_account,  # Finance processing step.
    _resolve_cost_center,  # Finance processing step.
    _resolve_currency,  # Finance processing step.
    _resolve_tax,  # Finance processing step.
)  # Continue structured finance payload.


def _resolve_customer(entity, ref, field="customer", *, required=True):  # Function handles this finance operation.
    """Resolve a customer by **code** or id within ``entity``."""
    if ref in (None, ""):  # Branch when this finance condition is true.
        if required:  # Branch when this finance condition is true.
            raise ValidationError({field: "A customer (code or id) is required."})  # Surface validation or finance error.
        return None  # Return the computed finance response.
    qs = Customer.objects.filter(entity=entity)  # Query finance data from the database.
    customer = (  # Store intermediate finance value.
        qs.filter(code=str(ref).upper()).first()  # Store intermediate finance value.
        or (qs.filter(pk=int(ref)).first() if str(ref).isdigit() else None)  # Store intermediate finance value.
    )  # Continue structured finance payload.
    if customer is None:  # Branch when this finance condition is true.
        raise NotFound(f"No customer matches '{ref}' for this entity.")  # Surface validation or finance error.
    return customer  # Return the computed finance response.


def _resolve_invoice(entity, ref, field="invoice", *, required=True):  # Function handles this finance operation.
    """Resolve an invoice by document number or id within ``entity``."""
    if ref in (None, ""):  # Branch when this finance condition is true.
        if required:  # Branch when this finance condition is true.
            raise ValidationError({field: "An invoice (document number or id) is required."})  # Surface validation or finance error.
        return None  # Return the computed finance response.
    qs = Invoice.objects.filter(entity=entity)  # Query finance data from the database.
    invoice = (  # Store intermediate finance value.
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()  # Store intermediate finance value.
        else qs.filter(document_number=str(ref)).first()  # Store intermediate finance value.
    )  # Continue structured finance payload.
    if invoice is None:  # Branch when this finance condition is true.
        raise NotFound(f"No invoice matches '{ref}' for this entity.")  # Surface validation or finance error.
    return invoice  # Return the computed finance response.


def _resolve_debit_note(entity, ref, field="debit_note"):  # Function handles this finance operation.
    """Resolve a posted DEBIT note by document number or id within ``entity``."""
    from .constants import CreditNoteKind, DocumentStatus  # Import dependency used by this finance module.
    from .models import CreditNote  # Import dependency used by this finance module.

    qs = CreditNote.objects.filter(  # Query finance data from the database.
        entity=entity, kind=CreditNoteKind.DEBIT, status=DocumentStatus.POSTED)  # Store intermediate finance value.
    note = (  # Store intermediate finance value.
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()  # Store intermediate finance value.
        else qs.filter(document_number=str(ref)).first()  # Store intermediate finance value.
    )  # Continue structured finance payload.
    if note is None:  # Branch when this finance condition is true.
        raise NotFound(f"No debit note matches '{ref}' for this entity.")  # Surface validation or finance error.
    return note  # Return the computed finance response.


def _allocation_plan(entity, raw_allocations):  # Function handles this finance operation.
    """Coerce a request ``allocations`` list into ``[(target, amount_kobo), ...]``.

    Each item settles an invoice (``{"invoice": ref, "amount": …}``) or a DEBIT note
    (``{"debit_note": ref, "amount": …}``) — both debit AR and are settled by receipts.
    """
    if not raw_allocations:  # Branch when this finance condition is true.
        return None  # Return the computed finance response.
    plan = []  # Store intermediate finance value.
    for i, item in enumerate(raw_allocations):  # Iterate through finance records.
        if item.get("debit_note") not in (None, ""):  # Branch when this finance condition is true.
            target = _resolve_debit_note(entity, item.get("debit_note"), f"allocations[{i}].debit_note")  # Store intermediate finance value.
        else:  # Fallback finance branch.
            target = _resolve_invoice(entity, item.get("invoice"), f"allocations[{i}].invoice")  # Store intermediate finance value.
        plan.append((target, _money(item.get("amount"), f"allocations[{i}].amount")))  # Finance processing step.
    return plan  # Return the computed finance response.


def _allocation_strategy(raw):  # Function handles this finance operation.
    """Validate an optional ``allocation_strategy`` request value (default 'oldest')."""
    from .receivables import ALLOCATION_STRATEGIES  # Import dependency used by this finance module.

    val = (raw or "oldest").lower()  # Store intermediate finance value.
    if val not in ALLOCATION_STRATEGIES:  # Branch when this finance condition is true.
        raise ValidationError(  # Surface validation or finance error.
            {"allocation_strategy": f"Must be one of: {', '.join(ALLOCATION_STRATEGIES)}."})  # Continue structured finance payload.
    return val  # Return the computed finance response.


# --------------------------------------------------------------------------- #
# Customers / payers                                                          #
# --------------------------------------------------------------------------- #

def _customer_ledger(entity, customer_ids=None):  # Function handles this finance operation.
    """Net AR position per customer, in two aggregate queries (no per-row N+1).

    Returns ``{customer_id: {"outstanding", "credit", "overdue", "lifetime_paid"}}``
    where ``outstanding`` is the sum of open invoice balances and ``credit`` the
    customer's available credit (their 2140 liability position: unapplied receipts +
    unapplied CREDIT notes − refunds already paid). Net = outstanding − credit
    (positive owes, negative in credit). Computed in a few aggregate queries (no N+1).
    """
    import datetime  # Import dependency used by this finance module.
    from django.db.models import F, Q, Sum  # Import dependency used by this finance module.
    from django.db.models.functions import Coalesce  # Import dependency used by this finance module.

    from .constants import CreditNoteKind, DocumentStatus  # Import dependency used by this finance module.
    from .models import CreditNote, Invoice, Payment, Refund  # Import dependency used by this finance module.

    today = datetime.date.today()  # Store intermediate finance value.
    bal = F("total") - F("amount_paid") - F("amount_credited")  # Store intermediate finance value.
    inv = Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)  # Query finance data from the database.
    pay = Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED)  # Query finance data from the database.
    note = CreditNote.objects.filter(entity=entity, status=DocumentStatus.POSTED, kind=CreditNoteKind.CREDIT)  # Query finance data from the database.
    # Open DEBIT notes are supplementary AR charges: their unsettled balance is
    # outstanding, exactly like an open invoice.
    dn = CreditNote.objects.filter(entity=entity, status=DocumentStatus.POSTED, kind=CreditNoteKind.DEBIT)  # Query finance data from the database.
    ref = Refund.objects.filter(entity=entity, status=DocumentStatus.POSTED)  # Query finance data from the database.
    if customer_ids is not None:  # Branch when this finance condition is true.
        inv = inv.filter(customer_id__in=customer_ids)  # Store intermediate finance value.
        pay = pay.filter(customer_id__in=customer_ids)  # Store intermediate finance value.
        note = note.filter(customer_id__in=customer_ids)  # Store intermediate finance value.
        dn = dn.filter(customer_id__in=customer_ids)  # Store intermediate finance value.
        ref = ref.filter(customer_id__in=customer_ids)  # Store intermediate finance value.

    out: dict[int, dict] = {}  # Store intermediate finance value.

    def slot(cid):  # Function handles this finance operation.
        return out.setdefault(cid, {"outstanding": 0, "overdue": False, "lifetime_paid": 0,  # Return the computed finance response.
                                    "_receipts": 0, "_notes": 0, "_refunded": 0})  # Finance processing step.

    for r in inv.values("customer_id").annotate(  # Iterate through finance records.
        outstanding=Coalesce(Sum(bal), 0),  # Store intermediate finance value.
        overdue_bal=Coalesce(Sum(bal, filter=Q(due_date__lt=today)), 0),  # Store intermediate finance value.
    ):  # Continue structured finance payload.
        d = slot(r["customer_id"])  # Store intermediate finance value.
        d["outstanding"] = int(r["outstanding"] or 0)  # Store intermediate finance value.
        d["overdue"] = int(r["overdue_bal"] or 0) > 0  # Store intermediate finance value.
    for r in pay.values("customer_id").annotate(  # Iterate through finance records.
        credit=Coalesce(Sum(F("amount") - F("allocated_amount")), 0),  # Store intermediate finance value.
        lifetime=Coalesce(Sum("amount"), 0),  # Store intermediate finance value.
    ):  # Continue structured finance payload.
        d = slot(r["customer_id"])  # Store intermediate finance value.
        d["_receipts"] = int(r["credit"] or 0)  # Store intermediate finance value.
        d["lifetime_paid"] = int(r["lifetime"] or 0)  # Store intermediate finance value.
    for r in note.values("customer_id").annotate(  # Iterate through finance records.
        c=Coalesce(Sum(F("total") - F("allocated_amount")), 0)):  # Store intermediate finance value.
        slot(r["customer_id"])["_notes"] = int(r["c"] or 0)  # Store intermediate finance value.
    for r in dn.values("customer_id").annotate(  # Iterate through finance records.
        c=Coalesce(Sum(F("total") - F("amount_paid")), 0)):  # Store intermediate finance value.
        d = slot(r["customer_id"])  # Store intermediate finance value.
        d["outstanding"] += int(r["c"] or 0)  # Store intermediate finance value.
    for r in ref.values("customer_id").annotate(c=Coalesce(Sum("amount"), 0)):  # Iterate through finance records.
        slot(r["customer_id"])["_refunded"] = int(r["c"] or 0)  # Store intermediate finance value.

    for d in out.values():  # Iterate through finance records.
        d["credit"] = max(0, d["_receipts"] + d["_notes"] - d["_refunded"])  # Store intermediate finance value.
    return out  # Return the computed finance response.


def _account_status(net: int, overdue: bool) -> str:  # Function handles this finance operation.
    """Derive the customer's account status pill from net balance + aging."""
    if net < 0:  # Branch when this finance condition is true.
        return "CREDIT"  # Return the computed finance response.
    if overdue:  # Branch when this finance condition is true.
        return "OVERDUE"  # Return the computed finance response.
    return "ACTIVE"  # Return the computed finance response.


def _money_obj(kobo) -> dict:  # Function handles this finance operation.
    """Money payload {kobo, naira} — the AR drawer shape (mirrors views._money)."""
    from .money import format_naira  # Import dependency used by this finance module.
    return {"kobo": int(kobo), "naira": format_naira(int(kobo))}  # Return the computed finance response.

class CustomerListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """Customers / payers for an entity.

    List filters: ``?search=`` (code or name), ``?is_active=true|false``.

    docstring-name: Customers
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.customer.create" if self.request.method == "POST" \
            else "finance.customer.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        from .money import format_naira  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = Customer.objects.filter(entity=entity).select_related("receivable_account")  # Query finance data from the database.
        if (search := request.query_params.get("search")):  # Branch when this finance condition is true.
            from django.db.models import Q  # Import dependency used by this finance module.
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))  # Store intermediate finance value.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.

        # Derived account-status filter. INACTIVE is the is_active column; ACTIVE/CREDIT/
        # OVERDUE come from the ledger (not a column), so resolve them for the active set
        # and keep matching ids before paginating (a few aggregate queries, no N+1).
        status_f = request.query_params.get("status")  # Store intermediate finance value.
        if status_f == "INACTIVE":  # Branch when this finance condition is true.
            qs = qs.filter(is_active=False)  # Store intermediate finance value.
        elif status_f in ("ACTIVE", "CREDIT", "OVERDUE"):  # Alternative finance branch.
            base_ids = list(qs.filter(is_active=True).values_list("id", flat=True))  # Store intermediate finance value.
            led_all = _customer_ledger(entity, base_ids)  # Store intermediate finance value.
            keep = [  # Store intermediate finance value.
                cid for cid in base_ids  # Finance processing step.
                if _account_status(  # Branch when this finance condition is true.
                    (l := led_all.get(cid, {})).get("outstanding", 0) - l.get("credit", 0),  # Continue structured finance payload.
                    l.get("overdue", False),  # Finance processing step.
                ) == status_f  # Continue structured finance payload.
            ]  # Continue structured finance payload.
            qs = qs.filter(id__in=keep)  # Store intermediate finance value.

        paginator = XVSPagination()  # Store intermediate finance value.
        paginator.page_size = 25  # Store intermediate finance value.
        page = paginator.paginate_queryset(qs.order_by("code"), request, view=self)  # Store intermediate finance value.
        ledger = _customer_ledger(entity, [c.id for c in page])  # Store intermediate finance value.
        rows = []  # Store intermediate finance value.
        for c in page:  # Iterate through finance records.
            row = CustomerSerializer(c).data  # Store intermediate finance value.
            led = ledger.get(c.id, {})  # Store intermediate finance value.
            net = led.get("outstanding", 0) - led.get("credit", 0)  # Store intermediate finance value.
            row["balance"] = net                      # signed kobo: + owes, − in credit
            row["balance_naira"] = format_naira(net)  # Store intermediate finance value.
            row["account_status"] = _account_status(net, led.get("overdue", False))  # Store intermediate finance value.
            rows.append(row)  # Finance processing step.
        return paginator.get_paginated_response(rows)  # Return the computed finance response.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        code = str(body.get("code", "")).strip().upper()  # Store intermediate finance value.
        # TODO: code should be created automatically if it wasn't provided.
        if not code:  # Branch when this finance condition is true.
            raise ValidationError({"code": "A customer code is required."})  # Surface validation or finance error.
        if Customer.objects.filter(entity=entity, code=code).exists():  # Branch when this finance condition is true.
            raise ValidationError({"code": f"A customer with code '{code}' already exists."})  # Surface validation or finance error.
        name = str(body.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "A customer name is required."})  # Surface validation or finance error.
        # Default the AR control to the entity's 1200 Accounts Receivable if not given.
        receivable = _resolve_account(  # Store intermediate finance value.
            entity, body.get("receivable_account") or "1200",  # Finance processing step.
            "receivable_account", required=True)  # Store intermediate finance value.
        customer = Customer.objects.create(  # Query finance data from the database.
            entity=entity, code=code, name=name,  # Store intermediate finance value.
            billing_email=body.get("billing_email", ""),  # Store intermediate finance value.
            billing_phone=body.get("billing_phone", ""),  # Store intermediate finance value.
            billing_address=body.get("billing_address", ""),  # Store intermediate finance value.
            receivable_account=receivable,  # Store intermediate finance value.
            opening_balance=_money(body.get("opening_balance", 0), "opening_balance"),  # Store intermediate finance value.
            source_type=body.get("source_type", ""),  # Store intermediate finance value.
            source_id=str(body.get("source_id", "")),  # Store intermediate finance value.
            is_active=bool(body.get("is_active", True)),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        # Seat any opening balance as a posted opening invoice (Dr AR / Cr Retained
        # Earnings) so it shows in the customer's outstanding and the GL. Inside this
        # atomic block, so a posting failure (e.g. no open period) rolls the whole
        # customer-create back with a clear error.
        from .receivables import post_opening_balance  # Import dependency used by this finance module.
        # An optional historical opening_date backdates the opening invoice + its journal
        # (falls back to today inside the service); the posting guards roll the whole
        # create back if that date lands in a closed/missing period.
        post_opening_balance(  # Finance processing step.
            customer, actor_user=request.user,  # Store intermediate finance value.
            date=_date(body.get("opening_date"), "opening_date"),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Customer {customer.code} created.",  # Finance processing step.
            data=CustomerSerializer(customer).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class CustomerDetailView(_FinanceBase):  # Class groups related finance API or service behavior.
    """Get the details of one customer (by **code or id**).

    docstring-name: Customers
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.customer.update" if self.request.method == "PATCH" \
            else "finance.customer.view"  # Finance processing step.

    def get(self, request, pk):  # Function handles this finance operation.
        import datetime  # Import dependency used by this finance module.

        from .constants import CreditNoteKind, DocumentStatus, InvoicePaymentStatus  # Import dependency used by this finance module.
        from .models import CreditNote, Invoice, Payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        customer = _resolve_customer(entity, pk)  # Store intermediate finance value.
        led = _customer_ledger(entity, [customer.id]).get(customer.id, {})  # Store intermediate finance value.
        net = led.get("outstanding", 0) - led.get("credit", 0)  # Store intermediate finance value.
        today = datetime.date.today()  # Store intermediate finance value.

        invoices = list(Invoice.objects.filter(  # Query finance data from the database.
            entity=entity, customer=customer, status=DocumentStatus.POSTED,  # Store intermediate finance value.
        ).order_by("invoice_date", "id")[:500])  # Continue structured finance payload.
        payments = list(Payment.objects.filter(  # Query finance data from the database.
            entity=entity, customer=customer, status=DocumentStatus.POSTED,  # Store intermediate finance value.
        ).order_by("payment_date", "id")[:500])  # Continue structured finance payload.
        # DEBIT notes are supplementary AR charges — they belong in the statement (debit
        # side) and their unsettled balance is an open item, just like an invoice.
        debit_notes = list(CreditNote.objects.filter(  # Query finance data from the database.
            entity=entity, customer=customer, status=DocumentStatus.POSTED,  # Store intermediate finance value.
            kind=CreditNoteKind.DEBIT,  # Store intermediate finance value.
        ).order_by("note_date", "id")[:500])  # Continue structured finance payload.

        def inv_status(i):  # Function handles this finance operation.
            if i.payment_status == InvoicePaymentStatus.PAID:  # Branch when this finance condition is true.
                return "PAID"  # Return the computed finance response.
            if i.due_date and i.due_date < today and i.balance_due > 0:  # Branch when this finance condition is true.
                return "OVERDUE"  # Return the computed finance response.
            if i.payment_status == InvoicePaymentStatus.PARTIAL:  # Branch when this finance condition is true.
                return "PARTIAL"  # Return the computed finance response.
            return "ISSUED"  # Return the computed finance response.

        def dn_status(n):  # Function handles this finance operation.
            if n.settlement_status == InvoicePaymentStatus.PAID:  # Branch when this finance condition is true.
                return "PAID"  # Return the computed finance response.
            if n.settlement_status == InvoicePaymentStatus.PARTIAL:  # Branch when this finance condition is true.
                return "PARTIAL"  # Return the computed finance response.
            return "ISSUED"  # Return the computed finance response.

        open_invoices = [  # Store intermediate finance value.
            {  # Continue structured finance payload.
                "document_number": i.document_number,  # Finance processing step.
                "invoice_date": i.invoice_date.isoformat(),  # Finance processing step.
                "due_date": i.due_date.isoformat() if i.due_date else None,  # Finance processing step.
                "total": _money_obj(i.total), "balance": _money_obj(i.balance_due),  # Finance processing step.
                "status": inv_status(i),  # Finance processing step.
            }  # Continue structured finance payload.
            for i in invoices if i.balance_due > 0  # Iterate through finance records.
        ]  # Continue structured finance payload.
        open_debit_notes = [  # Store intermediate finance value.
            {  # Continue structured finance payload.
                "document_number": n.document_number,  # Finance processing step.
                "note_date": n.note_date.isoformat() if n.note_date else None,  # Finance processing step.
                "total": _money_obj(n.total), "balance": _money_obj(n.balance_due),  # Finance processing step.
                "status": dn_status(n),  # Finance processing step.
            }  # Continue structured finance payload.
            for n in debit_notes if n.balance_due > 0  # Iterate through finance records.
        ]  # Continue structured finance payload.

        # Transactions: invoices + debit notes (debit) and receipts (credit),
        # reverse-chronological (newest first).
        transactions = (  # Store intermediate finance value.
            [{"date": i.invoice_date.isoformat(), "type": "INVOICE",  # Continue structured finance payload.
              "reference": i.document_number, "amount": _money_obj(i.total),  # Finance processing step.
              "status": inv_status(i)} for i in invoices]  # Finance processing step.
            + [{"date": n.note_date.isoformat(), "type": "DEBIT_NOTE",  # Finance processing step.
                "reference": n.document_number, "amount": _money_obj(n.total),  # Finance processing step.
                "status": dn_status(n)} for n in debit_notes]  # Finance processing step.
            + [{"date": p.payment_date.isoformat(), "type": "PAYMENT",  # Finance processing step.
                "reference": p.document_number, "amount": _money_obj(p.amount),  # Finance processing step.
                "status": "POSTED"} for p in payments]  # Finance processing step.
        )  # Continue structured finance payload.
        transactions.sort(key=lambda t: t["date"], reverse=True)  # Store intermediate finance value.

        # Statement: invoices + debit notes (debit) and receipts (credit),
        # chronological (oldest first), with a running balance. An opening balance is
        # already materialised as a posted OPENING invoice (see post_opening_balance)
        # and rides in `invoices` below — we must NOT also add a synthetic opening row
        # or the balance double-counts. This mirrors reports.customer_statement, which
        # is likewise document-driven.
        events = []  # Store intermediate finance value.
        events += [(i.invoice_date, f"Invoice {i.document_number}", i.total, 0) for i in invoices]  # Store intermediate finance value.
        events += [(n.note_date, f"Debit note {n.document_number}", n.total, 0) for n in debit_notes]  # Store intermediate finance value.
        events += [(p.payment_date, f"Receipt {p.document_number}", 0, p.amount) for p in payments]  # Store intermediate finance value.
        events.sort(key=lambda e: e[0])  # Store intermediate finance value.
        running = 0  # Store intermediate finance value.
        statement = []  # Store intermediate finance value.
        for d, desc, debit, credit in events:  # Iterate through finance records.
            running += debit - credit  # Store intermediate finance value.
            statement.append({  # Finance processing step.
                "date": None if d == datetime.date.min else d.isoformat(),  # Store intermediate finance value.
                "description": desc, "debit": _money_obj(debit),  # Finance processing step.
                "credit": _money_obj(credit), "balance": _money_obj(running),  # Finance processing step.
            })  # Continue structured finance payload.

        return success_response("Customer retrieved.", data={  # Return the computed finance response.
            "customer": CustomerSerializer(customer).data,  # Finance processing step.
            "summary": {  # Finance processing step.
                "current_balance": _money_obj(net),  # Finance processing step.
                "lifetime_paid": _money_obj(led.get("lifetime_paid", 0)),  # Finance processing step.
                "open_invoice_count": len(open_invoices),  # Finance processing step.
                "account_status": _account_status(net, led.get("overdue", False)),  # Finance processing step.
            },  # Continue structured finance payload.
            "open_invoices": open_invoices,  # Finance processing step.
            "open_debit_notes": open_debit_notes,  # Finance processing step.
            "transactions": transactions,  # Finance processing step.
            "statement": statement,  # Finance processing step.
        })  # Continue structured finance payload.

    @transaction.atomic  # Decorator configures the following callable.
    def patch(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        customer = _resolve_customer(entity, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        for field in ("name", "billing_email", "billing_phone", "billing_address",  # Iterate through finance records.
                      "source_type", "source_id"):  # Finance processing step.
            if field in body:  # Branch when this finance condition is true.
                setattr(customer, field, body[field])  # Finance processing step.
        if "receivable_account" in body:  # Branch when this finance condition is true.
            customer.receivable_account = _resolve_account(  # Store intermediate finance value.
                entity, body.get("receivable_account"), "receivable_account", required=True)  # Store intermediate finance value.
        if "opening_balance" in body:  # Branch when this finance condition is true.
            customer.opening_balance = _money(body.get("opening_balance"), "opening_balance")  # Store intermediate finance value.
        if "is_active" in body:  # Branch when this finance condition is true.
            customer.is_active = bool(body.get("is_active"))  # Store intermediate finance value.
        customer.save()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Customer {customer.code} updated.", data=CustomerSerializer(customer).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class CustomerReceiptView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST /customers/<pk>/receipt/ — record a receipt for a customer and auto-
    allocate it across their open invoices (oldest first). Any excess stays as
    unallocated credit on the customer.

    docstring-name: Record a customer receipt
    """

    rbac_permission = "finance.payment.create"  # Store intermediate finance value.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request, pk):  # Function handles this finance operation.
        from .models import Payment  # Import dependency used by this finance module.
        from .receivables import post_payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        customer = _resolve_customer(entity, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        amount = _money(body.get("amount"), "amount")  # Store intermediate finance value.
        if amount <= 0:  # Branch when this finance condition is true.
            raise ValidationError({"amount": "A positive amount is required."})  # Surface validation or finance error.
        payment = Payment.objects.create(  # Query finance data from the database.
            entity=entity, customer=customer,  # Store intermediate finance value.
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),  # Store intermediate finance value.
            method=body.get("method") or "BANK_TRANSFER", amount=amount,  # Store intermediate finance value.
            deposit_account=_resolve_account(  # Store intermediate finance value.
                entity, body.get("deposit_account"), "deposit_account", required=True),  # Store intermediate finance value.
            reference=body.get("reference", ""), narration=body.get("narration", ""),  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        auto = body.get("auto_allocate", True)  # Store intermediate finance value.
        if isinstance(auto, str):  # Branch when this finance condition is true.
            auto = auto.lower() not in ("false", "0", "no")  # Store intermediate finance value.
        post_payment(payment, actor_user=request.user, auto_allocate=bool(auto),  # Store intermediate finance value.
                     strategy=_allocation_strategy(body.get("allocation_strategy")))  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            f"Receipt {payment.document_number} recorded for {customer.code}.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "id": payment.id,  # Finance processing step.
                "payment": payment.document_number,  # Finance processing step.
                "allocated": payment.allocated_amount,  # Finance processing step.
                "unallocated": payment.unallocated_amount,  # Finance processing step.
            },  # Continue structured finance payload.
            status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Receipts & allocation                                                       #
# --------------------------------------------------------------------------- #

class CustomerSummaryView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET /finance/customers/summary/ — entity-wide KPI totals + status counts for the
    Customers header cards (computed over ALL rows, so they stay accurate while the list
    itself paginates). Honors the same ``?search=``/``?is_active=`` as the list.

    docstring-name: Customer summary
    """

    rbac_permission = "finance.customer.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from django.db.models import Q  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = Customer.objects.filter(entity=entity)  # Query finance data from the database.
        if (search := request.query_params.get("search")):  # Branch when this finance condition is true.
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))  # Store intermediate finance value.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.

        custs = list(qs.values("id", "is_active"))  # Store intermediate finance value.
        ledger = _customer_ledger(entity, [c["id"] for c in custs])  # Store intermediate finance value.
        receivable = 0  # Store intermediate finance value.
        on_credit = 0  # Store intermediate finance value.
        counts = {"ACTIVE": 0, "CREDIT": 0, "OVERDUE": 0, "INACTIVE": 0}  # Store intermediate finance value.
        for c in custs:  # Iterate through finance records.
            led = ledger.get(c["id"], {})  # Store intermediate finance value.
            net = led.get("outstanding", 0) - led.get("credit", 0)  # Store intermediate finance value.
            status = "INACTIVE" if not c["is_active"] else _account_status(net, led.get("overdue", False))  # Store intermediate finance value.
            counts[status] += 1  # Store intermediate finance value.
            if net > 0:  # Branch when this finance condition is true.
                receivable += net  # Store intermediate finance value.
            elif net < 0:  # Alternative finance branch.
                on_credit += 1  # Store intermediate finance value.
        return success_response("Customer summary retrieved.", data={  # Return the computed finance response.
            "total": len(custs),  # Finance processing step.
            "receivable": _money_obj(receivable),  # Finance processing step.
            "on_credit": on_credit,  # Finance processing step.
            "overdue": counts["OVERDUE"],  # Finance processing step.
            "status_counts": counts,  # Finance processing step.
        })  # Continue structured finance payload.


class PaymentListView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET /finance/payments/ — customer receipts and their allocation state.

    Filters: ``?status=`` (ALLOCATED|PARTIAL|UNALLOCATED), ``?method=``,
    ``?customer=`` (code/id), ``?search=`` (doc no / customer / reference).

    docstring-name: Customer receipts
    """

    rbac_permission = "finance.payment.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from django.db.models import Q  # Import dependency used by this finance module.

        from .constants import DocumentStatus  # Import dependency used by this finance module.
        from .models import Payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = (Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED)  # Query finance data from the database.
              .select_related("customer", "deposit_account"))  # Finance processing step.
        if (method := request.query_params.get("method")):  # Branch when this finance condition is true.
            qs = qs.filter(method=method)  # Store intermediate finance value.
        if (customer := request.query_params.get("customer")):  # Branch when this finance condition is true.
            qs = qs.filter(customer=_resolve_customer(entity, customer))  # Store intermediate finance value.
        if (search := request.query_params.get("search")):  # Branch when this finance condition is true.
            qs = qs.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search) | Q(customer__name__icontains=search)  # Store intermediate finance value.
                | Q(customer__code__icontains=search) | Q(reference__icontains=search))  # Store intermediate finance value.

        # allocation_status is derived from allocated_amount vs amount; express it as a
        # DB filter so paging counts are correct (it used to filter post-slice in Python).
        status_f = request.query_params.get("status")  # Store intermediate finance value.
        if status_f == "ALLOCATED":  # Branch when this finance condition is true.
            qs = qs.filter(allocated_amount__gte=F("amount"))  # Store intermediate finance value.
        elif status_f == "UNALLOCATED":  # Alternative finance branch.
            qs = qs.filter(allocated_amount__lte=0)  # Store intermediate finance value.
        elif status_f == "PARTIAL":  # Alternative finance branch.
            qs = qs.filter(allocated_amount__gt=0, allocated_amount__lt=F("amount"))  # Store intermediate finance value.
        return _paginate(request, qs.order_by("-payment_date", "-id"), PaymentSerializer, self)  # Return the computed finance response.


class PaymentSummaryView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET /finance/payments/summary/ — receipts KPI totals + allocation-status counts
    for the header cards, over ALL rows (accurate while the list paginates). Honors the
    same ``?method=``/``?customer=``/``?search=`` as the list.

    docstring-name: Receipts summary
    """

    rbac_permission = "finance.payment.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        import datetime  # Import dependency used by this finance module.

        from django.db.models import Count, F, Q, Sum  # Import dependency used by this finance module.
        from django.db.models.functions import Coalesce  # Import dependency used by this finance module.

        from .constants import DocumentStatus  # Import dependency used by this finance module.
        from .models import Payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = (Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED))  # Query finance data from the database.
        if (method := request.query_params.get("method")):  # Branch when this finance condition is true.
            qs = qs.filter(method=method)  # Store intermediate finance value.
        if (customer := request.query_params.get("customer")):  # Branch when this finance condition is true.
            qs = qs.filter(customer=_resolve_customer(entity, customer))  # Store intermediate finance value.
        if (search := request.query_params.get("search")):  # Branch when this finance condition is true.
            qs = qs.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search) | Q(customer__name__icontains=search)  # Store intermediate finance value.
                | Q(customer__code__icontains=search) | Q(reference__icontains=search))  # Store intermediate finance value.

        today = datetime.date.today()  # Store intermediate finance value.
        week_start = today - datetime.timedelta(days=6)  # 7-day window incl. today
        agg = qs.aggregate(  # Store intermediate finance value.
            count=Count("id"),  # Store intermediate finance value.
            today=Coalesce(Sum("amount", filter=Q(payment_date=today)), 0),  # Store intermediate finance value.
            week=Coalesce(Sum("amount", filter=Q(payment_date__gte=week_start)), 0),  # Store intermediate finance value.
            unallocated=Coalesce(Sum(F("amount") - F("allocated_amount")), 0),  # Store intermediate finance value.
            allocated_c=Count("id", filter=Q(allocated_amount__gte=F("amount"))),  # Store intermediate finance value.
            unallocated_c=Count("id", filter=Q(allocated_amount__lte=0)),  # Store intermediate finance value.
            partial_c=Count("id", filter=Q(allocated_amount__gt=0, allocated_amount__lt=F("amount"))),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response("Receipts summary retrieved.", data={  # Return the computed finance response.
            "count": agg["count"],  # Finance processing step.
            "today": _money_obj(agg["today"]),  # Finance processing step.
            "week": _money_obj(agg["week"]),  # Finance processing step.
            "unallocated": _money_obj(agg["unallocated"]),  # Finance processing step.
            "status_counts": {  # Finance processing step.
                "ALLOCATED": agg["allocated_c"],  # Finance processing step.
                "PARTIAL": agg["partial_c"],  # Finance processing step.
                "UNALLOCATED": agg["unallocated_c"],  # Finance processing step.
            },  # Continue structured finance payload.
        })  # Continue structured finance payload.


class PaymentDetailView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET /finance/payments/<id>/ — a receipt, its current allocations, the
    customer's open invoices (allocation candidates) and the receipt's GL posting.

    docstring-name: Customer receipts
    """

    rbac_permission = "finance.payment.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        from .constants import CreditNoteKind, DocumentStatus, InvoicePaymentStatus  # Import dependency used by this finance module.
        from .models import CreditNote, Invoice, Payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        p = (Payment.objects.filter(entity=entity, pk=pk)  # Query finance data from the database.
             .select_related("customer", "deposit_account", "journal")  # Finance processing step.
             .prefetch_related("allocations__invoice", "debit_note_allocations__note",  # Finance processing step.
                               "journal__lines__account").first())  # Finance processing step.
        if p is None:  # Branch when this finance condition is true.
            raise NotFound("Receipt not found for this entity.")  # Surface validation or finance error.

        allocations = [  # Store intermediate finance value.
            {"invoice": a.invoice.document_number, "invoice_id": a.invoice_id,  # Continue structured finance payload.
             "amount": _money_obj(a.amount)}  # Finance processing step.
            for a in p.allocations.all()  # Iterate through finance records.
        ]  # Continue structured finance payload.
        allocations += [  # Store intermediate finance value.
            {"debit_note": a.note.document_number, "debit_note_id": a.note_id,  # Continue structured finance payload.
             "amount": _money_obj(a.amount)}  # Finance processing step.
            for a in p.debit_note_allocations.all()  # Iterate through finance records.
        ]  # Continue structured finance payload.
        open_invoices = [  # Store intermediate finance value.
            {"id": i.id, "document_number": i.document_number,  # Continue structured finance payload.
             "due_date": i.due_date.isoformat() if i.due_date else None,  # Finance processing step.
             "balance": _money_obj(i.balance_due)}  # Finance processing step.
            for i in Invoice.objects.filter(  # Iterate through finance records.
                entity=entity, customer=p.customer, status=DocumentStatus.POSTED,  # Store intermediate finance value.
            ).exclude(payment_status=InvoicePaymentStatus.PAID).order_by("due_date", "invoice_date", "id")  # Continue structured finance payload.
            if i.balance_due > 0  # Branch when this finance condition is true.
        ]  # Continue structured finance payload.
        # DEBIT notes are settleable AR items too — offer the customer's open ones.
        open_debit_notes = [  # Store intermediate finance value.
            {"id": n.id, "document_number": n.document_number,  # Continue structured finance payload.
             "note_date": n.note_date.isoformat() if n.note_date else None,  # Finance processing step.
             "balance": _money_obj(n.balance_due)}  # Finance processing step.
            for n in CreditNote.objects.filter(  # Iterate through finance records.
                entity=entity, customer=p.customer, status=DocumentStatus.POSTED,  # Store intermediate finance value.
                kind=CreditNoteKind.DEBIT,  # Store intermediate finance value.
            ).exclude(settlement_status=InvoicePaymentStatus.PAID).order_by("note_date", "id")  # Continue structured finance payload.
            if n.balance_due > 0  # Branch when this finance condition is true.
        ]  # Continue structured finance payload.
        gl_postings = []  # Store intermediate finance value.
        if p.journal_id:  # Branch when this finance condition is true.
            for gl in p.journal.lines.all():  # Iterate through finance records.
                gl_postings.append({  # Finance processing step.
                    "account_code": gl.account.code, "account_name": gl.account.name,  # Finance processing step.
                    "debit": _money_obj(gl.debit), "credit": _money_obj(gl.credit),  # Finance processing step.
                })  # Continue structured finance payload.
        return success_response("Receipt retrieved.", data={  # Return the computed finance response.
            "payment": PaymentSerializer(p).data,  # Finance processing step.
            "allocations": allocations,  # Finance processing step.
            "open_invoices": open_invoices,  # Finance processing step.
            "open_debit_notes": open_debit_notes,  # Finance processing step.
            "gl_postings": gl_postings,  # Finance processing step.
        })  # Continue structured finance payload.


class PaymentReceiptView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET /finance/payments/<id>/receipt/ — printable HTML payment receipt."""

    rbac_permission = "finance.payment.view"  # Store intermediate finance value.

    def _payment(self, request, pk):  # Function handles this finance operation.
        from .constants import DocumentStatus  # Import dependency used by this finance module.
        from .models import Payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        payment = (  # Store intermediate finance value.
            Payment.objects.filter(entity=entity, pk=pk, status=DocumentStatus.POSTED)  # Query finance data from the database.
            .select_related("entity__source_school", "branch", "customer")  # Finance processing step.
            .prefetch_related("allocations__invoice")  # Finance processing step.
            .first()  # Finance processing step.
        )  # Continue structured finance payload.
        if payment is None:  # Branch when this finance condition is true.
            raise NotFound("Receipt not found for this entity.")  # Surface validation or finance error.
        return payment  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        from .documents import render_receipt_document_html  # Import dependency used by this finance module.

        html = render_receipt_document_html(self._payment(request, pk), request=request)  # Store intermediate finance value.
        return HttpResponse(html, content_type="text/html; charset=utf-8")  # Return the computed finance response.


class PaymentReceiptPDFView(PaymentReceiptView):  # Class groups related finance API or service behavior.
    """GET /finance/payments/<id>/receipt.pdf — printable PDF payment receipt."""

    def get(self, request, pk):  # Function handles this finance operation.
        from .documents import DocumentRenderUnavailable, render_receipt_document_pdf  # Import dependency used by this finance module.

        payment = self._payment(request, pk)  # Store intermediate finance value.
        try:  # Start protected finance operation.
            pdf = render_receipt_document_pdf(payment, request=request)  # Store intermediate finance value.
        except DocumentRenderUnavailable:  # Handle finance operation failure.
            return Response(  # Return the computed finance response.
                {"detail": "PDF rendering is unavailable on this server."},  # Continue structured finance payload.
                status=503,  # Store intermediate finance value.
            )  # Continue structured finance payload.
        response = HttpResponse(pdf, content_type="application/pdf")  # Store intermediate finance value.
        filename = f"receipt-{payment.document_number or payment.pk}.pdf"  # Store intermediate finance value.
        response["Content-Disposition"] = f'inline; filename="{filename}"'  # Store intermediate finance value.
        return response  # Return the computed finance response.


class PaymentAllocateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST /finance/payments/<id>/allocate/ — apply a receipt to open invoices.

    Body ``{allocations:[{invoice, amount}]}`` for an explicit split, or
    ``{auto_allocate:true}`` to settle oldest-first. Each amount is capped at the
    invoice balance and the receipt's remaining cash; excess stays as credit.

    docstring-name: Allocate a receipt
    """

    rbac_permission = "finance.payment.allocate"  # Store intermediate finance value.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request, pk):  # Function handles this finance operation.
        from .models import Payment  # Import dependency used by this finance module.
        from .receivables import allocate_payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        p = Payment.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if p is None:  # Branch when this finance condition is true.
            raise NotFound("Receipt not found for this entity.")  # Surface validation or finance error.
        body = request.data or {}  # Store intermediate finance value.
        plan = _allocation_plan(entity, body.get("allocations"))  # Store intermediate finance value.
        if plan:  # Branch when this finance condition is true.
            allocate_payment(p, allocations=plan, actor_user=request.user)  # Store intermediate finance value.
        elif body.get("auto_allocate"):  # Alternative finance branch.
            allocate_payment(p, actor_user=request.user,  # Store intermediate finance value.
                             strategy=_allocation_strategy(body.get("allocation_strategy")))  # Store intermediate finance value.
        else:  # Fallback finance branch.
            raise ValidationError({"allocations": "Provide allocations or auto_allocate=true."})  # Surface validation or finance error.
        p.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Receipt {p.document_number} allocated.",  # Finance processing step.
            data=PaymentSerializer(p).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Fee structures (billing catalogue → invoices)                               #
# --------------------------------------------------------------------------- #

def _build_fee_items(structure, entity, raw_items):  # Function handles this finance operation.
    """(Re)create a structure's fee items from a request ``items`` list."""
    if not raw_items:  # Branch when this finance condition is true.
        raise ValidationError({"items": "At least one fee item is required."})  # Surface validation or finance error.
    for i, item in enumerate(raw_items, start=1):  # Iterate through finance records.
        amount = _money(item.get("amount"), f"items[{i}].amount")  # Store intermediate finance value.
        if amount <= 0:  # Branch when this finance condition is true.
            raise ValidationError({f"items[{i}].amount": "A positive amount is required."})  # Surface validation or finance error.
        FeeItem.objects.create(  # Query finance data from the database.
            structure=structure, line_no=item.get("line_no", i),  # Store intermediate finance value.
            code=str(item.get("code", "")).strip()[:32],  # Store intermediate finance value.
            description=str(item.get("description", "")).strip() or f"Fee {i}",  # Store intermediate finance value.
            revenue_account=_resolve_account(  # Store intermediate finance value.
                entity, item.get("revenue_account"), f"items[{i}].revenue_account", required=True),  # Store intermediate finance value.
            amount=amount,  # Store intermediate finance value.
            tax_code=_resolve_tax(entity, item.get("tax_code"), f"items[{i}].tax_code"),  # Store intermediate finance value.
            is_optional=bool(item.get("is_optional", False)),  # Store intermediate finance value.
        )  # Continue structured finance payload.


def _resolve_applies_to(raw):  # Function handles this finance operation.
    """Validate a fee-structure ``applies_to`` value, defaulting to CUSTOMER."""
    if raw in (None, ""):  # Branch when this finance condition is true.
        return FeeAppliesTo.CUSTOMER  # Return the computed finance response.
    value = str(raw).upper()  # Store intermediate finance value.
    if value not in FeeAppliesTo.values:  # Branch when this finance condition is true.
        raise ValidationError({"applies_to":  # Surface validation or finance error.
            f"Must be one of {', '.join(FeeAppliesTo.values)}."})  # Finance processing step.
    return value  # Return the computed finance response.


def _resolve_fee_structure(entity, ref):  # Function handles this finance operation.
    qs = FeeStructure.objects.filter(entity=entity)  # Query finance data from the database.
    structure = (  # Store intermediate finance value.
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()  # Store intermediate finance value.
        else qs.filter(code=str(ref).upper()).first()  # Store intermediate finance value.
    )  # Continue structured finance payload.
    if structure is None:  # Branch when this finance condition is true.
        raise NotFound(f"No fee structure matches '{ref}' for this entity.")  # Surface validation or finance error.
    return structure  # Return the computed finance response.


class FeeStructureListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """Fee structures for an entity. Invoices can only be created from **active** structures. 
    The structure's ``applies_to`` determines whether it can be used for a customer, a vendor, etc. 
    Multiple structures can be active at once, but each must have a unique code. Each structure has one 
    or more fee items (lines) with a description, revenue account, amount and optional tax code.

    POST body: ``{code, name, applies_to?, description?, is_active?, items:[{description,
    revenue_account, amount, tax_code?}]}``.

    docstring-name: Fee structures
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.feestructure.create" if self.request.method == "POST" \
            else "finance.feestructure.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = FeeStructure.objects.filter(entity=entity).prefetch_related(  # Query finance data from the database.
            "items__revenue_account", "items__tax_code")  # Finance processing step.
        if (active := request.query_params.get("is_active")) in ("true", "false"):  # Branch when this finance condition is true.
            qs = qs.filter(is_active=active == "true")  # Store intermediate finance value.
        if (applies_to := request.query_params.get("applies_to")):  # Branch when this finance condition is true.
            qs = qs.filter(applies_to=applies_to.upper())  # Store intermediate finance value.
        if (search := request.query_params.get("search")):  # Branch when this finance condition is true.
            from django.db.models import Q  # Import dependency used by this finance module.
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Fee structures retrieved.",  # Finance processing step.
            data=FeeStructureSerializer(qs.order_by("-created_at", "code"), many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        code = str(body.get("code", "")).strip().upper()  # Store intermediate finance value.
        if not code:  # Branch when this finance condition is true.
            raise ValidationError({"code": "A fee structure code is required."})  # Surface validation or finance error.
        if FeeStructure.objects.filter(entity=entity, code=code).exists():  # Branch when this finance condition is true.
            raise ValidationError({"code": f"A fee structure with code '{code}' already exists."})  # Surface validation or finance error.
        name = str(body.get("name", "")).strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "A fee structure name is required."})  # Surface validation or finance error.
        structure = FeeStructure.objects.create(  # Query finance data from the database.
            entity=entity, code=code, name=name,  # Store intermediate finance value.
            applies_to=_resolve_applies_to(body.get("applies_to")),  # Store intermediate finance value.
            description=body.get("description", ""),  # Store intermediate finance value.
            is_active=bool(body.get("is_active", True)), created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        _build_fee_items(structure, entity, body.get("items"))  # Finance processing step.
        structure.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Fee structure {structure.code} created.",  # Finance processing step.
            data=FeeStructureSerializer(structure).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FeeStructureDetailView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET / PATCH one fee structure (by **code or id**). PATCH may replace ``items``.

    docstring-name: Fee structures
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.feestructure.edit" if self.request.method == "PATCH" \
            else "finance.feestructure.view"  # Finance processing step.

    def get(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        structure = _resolve_fee_structure(entity, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Fee structure retrieved.",  # Finance processing step.
            data=FeeStructureSerializer(structure, context={"with_usage": True}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    @transaction.atomic  # Decorator configures the following callable.
    def patch(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        structure = _resolve_fee_structure(entity, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        for field in ("name", "description"):  # Iterate through finance records.
            if field in body:  # Branch when this finance condition is true.
                setattr(structure, field, body[field])  # Finance processing step.
        if "applies_to" in body:  # Branch when this finance condition is true.
            structure.applies_to = _resolve_applies_to(body.get("applies_to"))  # Store intermediate finance value.
        if "is_active" in body:  # Branch when this finance condition is true.
            structure.is_active = bool(body.get("is_active"))  # Store intermediate finance value.
        structure.save()  # Finance processing step.
        if "items" in body:  # full replace
            structure.items.all().delete()  # Finance processing step.
            _build_fee_items(structure, entity, body.get("items"))  # Finance processing step.
        structure.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Fee structure {structure.code} updated.",  # Finance processing step.
            data=FeeStructureSerializer(structure, context={"with_usage": True}).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FeeStructureDuplicateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """Clone a fee structure (code + lines) into a new **draft** structure.

    Body: ``{code, name?}`` — a new unique code is required; the clone copies
    applies_to, description and every line (incl. fee code / optional flag) and is
    created **inactive** so it can be reviewed before use.

    docstring-name: Fee structures
    """

    rbac_permission = "finance.feestructure.create"  # Store intermediate finance value.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        source = _resolve_fee_structure(entity, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        new_code = str(body.get("code", "")).strip().upper()  # Store intermediate finance value.
        if not new_code:  # Branch when this finance condition is true.
            raise ValidationError({"code": "A code for the new structure is required."})  # Surface validation or finance error.
        if FeeStructure.objects.filter(entity=entity, code=new_code).exists():  # Branch when this finance condition is true.
            raise ValidationError({"code": f"A fee structure with code '{new_code}' already exists."})  # Surface validation or finance error.
        clone = FeeStructure.objects.create(  # Query finance data from the database.
            entity=entity, code=new_code,  # Store intermediate finance value.
            name=str(body.get("name", "")).strip() or f"{source.name} (copy)",  # Store intermediate finance value.
            applies_to=source.applies_to, description=source.description,  # Store intermediate finance value.
            is_active=False, created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        for item in source.items.all():  # Iterate through finance records.
            FeeItem.objects.create(  # Query finance data from the database.
                structure=clone, line_no=item.line_no, code=item.code,  # Store intermediate finance value.
                description=item.description, revenue_account=item.revenue_account,  # Store intermediate finance value.
                amount=item.amount, tax_code=item.tax_code, is_optional=item.is_optional,  # Store intermediate finance value.
            )  # Continue structured finance payload.
        clone.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Fee structure {clone.code} created from {source.code}.",  # Finance processing step.
            data=FeeStructureSerializer(clone, context={"with_usage": True}).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class FeeStructureGenerateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST — raise a posted invoice per customer from this fee structure.

    Body: ``{customers:[code|id, ...]}`` or ``{all_active:true}``; optional
    ``invoice_date``, ``due_date`` (ISO). Returns the invoices created.

    docstring-name: Generate invoices from a fee structure
    """

    rbac_permission = "finance.feestructure.generate"  # Store intermediate finance value.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request, pk):  # Function handles this finance operation.
        from .fees import generate_invoices  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        structure = _resolve_fee_structure(entity, pk)  # Store intermediate finance value.
        if structure.applies_to != FeeAppliesTo.CUSTOMER:  # Branch when this finance condition is true.
            raise ValidationError({"applies_to":  # Surface validation or finance error.
                "Only customer fee structures can generate AR invoices."})  # Finance processing step.
        body = request.data or {}  # Store intermediate finance value.
        if body.get("all_active"):  # Branch when this finance condition is true.
            customers = list(Customer.objects.filter(entity=entity, is_active=True))  # Query finance data from the database.
        else:  # Fallback finance branch.
            refs = body.get("customers") or []  # Store intermediate finance value.
            if not refs:  # Branch when this finance condition is true.
                raise ValidationError(  # Surface validation or finance error.
                    {"customers": "Provide a customers list or all_active=true."})  # Continue structured finance payload.
            customers = [_resolve_customer(entity, r, "customers") for r in refs]  # Store intermediate finance value.
        invoices = generate_invoices(  # Store intermediate finance value.
            structure, customers,  # Finance processing step.
            invoice_date=_date(body.get("invoice_date"), "invoice_date"),  # Store intermediate finance value.
            due_date=_date(body.get("due_date"), "due_date"),  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"{len(invoices)} invoice(s) generated from {structure.code}.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "structure": structure.code,  # Finance processing step.
                "generated": len(invoices),  # Finance processing step.
                "invoices": InvoiceSerializer(invoices, many=True).data,  # Store intermediate finance value.
            },  # Continue structured finance payload.
            status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Credit / debit notes                                                        #
# --------------------------------------------------------------------------- #

class CreditNoteListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft) credit or debit notes for an entity.

    docstring-name: Credit notes
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.creditnote.create" if self.request.method == "POST" \
            else "finance.creditnote.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = (CreditNote.objects.filter(entity=entity)  # Query finance data from the database.
              .select_related("customer", "invoice").prefetch_related("lines"))  # Finance processing step.
        if (kind := request.query_params.get("kind")):  # Branch when this finance condition is true.
            qs = qs.filter(kind=kind)  # Store intermediate finance value.
        if (customer := request.query_params.get("customer")):  # Branch when this finance condition is true.
            qs = qs.filter(customer=_resolve_customer(entity, customer))  # Store intermediate finance value.
        if (search := (request.query_params.get("search") or "").strip()):  # Branch when this finance condition is true.
            qs = qs.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search) | Q(reason__icontains=search)  # Store intermediate finance value.
                | Q(customer__name__icontains=search) | Q(customer__code__icontains=search)  # Store intermediate finance value.
            )  # Continue structured finance payload.
        # Derived status: applied = a fully-allocated credit note; issued = any other
        # posted note; draft = not yet posted.
        applied_q = Q(kind="CREDIT", allocated_amount__gt=0) & Q(allocated_amount__gte=F("total"))  # Store intermediate finance value.
        status_val = (request.query_params.get("status") or "").lower()  # Store intermediate finance value.
        if status_val == "draft":  # Branch when this finance condition is true.
            qs = qs.exclude(status=DocumentStatus.POSTED)  # Store intermediate finance value.
        elif status_val == "applied":  # Alternative finance branch.
            qs = qs.filter(status=DocumentStatus.POSTED).filter(applied_q)  # Store intermediate finance value.
        elif status_val == "issued":  # Alternative finance branch.
            qs = qs.filter(status=DocumentStatus.POSTED).exclude(applied_q)  # Store intermediate finance value.
        return _paginate(request, qs.order_by("-note_date", "-id"), CreditNoteSerializer, self)  # Return the computed finance response.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        from .credit_notes import price_credit_note  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        lines = _require_lines(body)  # Store intermediate finance value.
        note = CreditNote.objects.create(  # Query finance data from the database.
            entity=entity,  # Store intermediate finance value.
            customer=_resolve_customer(entity, body.get("customer")),  # Store intermediate finance value.
            kind=body.get("kind", "CREDIT"),  # Store intermediate finance value.
            note_date=_date(body.get("note_date"), "note_date", required=True),  # Store intermediate finance value.
            currency=_resolve_currency(body.get("currency")),  # Store intermediate finance value.
            reason=body.get("reason", ""),  # Store intermediate finance value.
            reference=body.get("reference", ""),  # Store intermediate finance value.
            invoice=_resolve_invoice(entity, body.get("invoice"), required=False),  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        for i, ln in enumerate(lines, start=1):  # Iterate through finance records.
            CreditNoteLine.objects.create(  # Query finance data from the database.
                note=note, line_no=i,  # Store intermediate finance value.
                description=ln.get("description", ""),  # Store intermediate finance value.
                revenue_account=_resolve_account(  # Store intermediate finance value.
                    entity, ln.get("revenue_account"),  # Finance processing step.
                    f"lines[{i}].revenue_account", required=True),  # Store intermediate finance value.
                quantity=_dec(ln.get("quantity", 1), f"lines[{i}].quantity"),  # Store intermediate finance value.
                unit_price=_money(ln.get("unit_price", 0), f"lines[{i}].unit_price"),  # Store intermediate finance value.
                tax_code=_resolve_tax(entity, ln.get("tax_code"), f"lines[{i}].tax_code"),  # Store intermediate finance value.
                cost_center=_resolve_cost_center(  # Store intermediate finance value.
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),  # Finance processing step.
            )  # Continue structured finance payload.
        price_credit_note(note)  # Finance processing step.
        note.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"{note.get_kind_display()} {note.document_number} created.",  # Finance processing step.
            data=CreditNoteSerializer(note).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _CreditNoteActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _note(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        note = CreditNote.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if note is None:  # Branch when this finance condition is true.
            raise NotFound("Credit note not found for this entity.")  # Surface validation or finance error.
        return entity, note  # Return the computed finance response.


class CreditNoteDetailView(_CreditNoteActionBase):  # Class groups related finance API or service behavior.
    """GET /finance/credit-notes/<id>/ — retrieve one credit or debit note (by id),
    with its lines and current allocation state.

    docstring-name: Credit notes
    """
    rbac_permission = "finance.creditnote.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, note = self._note(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Credit note retrieved.", data=CreditNoteSerializer(note).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class CreditNotePostView(_CreditNoteActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/credit-notes/<id>/post/ — post a draft credit/debit note to the GL.

    Body ``{allocations:[{invoice, amount}]}`` for an explicit split, or
    ``{auto_allocate:true}`` (the default when no allocations are given) to apply a
    CREDIT note oldest-first against the customer's open invoices. A debit note raises
    the receivable; a credit note reduces it and settles/credits the invoices.

    docstring-name: Post a credit note
    """
    rbac_permission = "finance.creditnote.post"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .credit_notes import post_credit_note  # Import dependency used by this finance module.

        entity, note = self._note(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        plan = _allocation_plan(entity, body.get("allocations"))  # Store intermediate finance value.
        auto = bool(body.get("auto_allocate", plan is None))  # Store intermediate finance value.
        post_credit_note(  # Finance processing step.
            note, actor_user=request.user,  # Store intermediate finance value.
            auto_allocate=auto, allocations=plan,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        note.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"{note.get_kind_display()} {note.document_number} posted.",  # Finance processing step.
            data=CreditNoteSerializer(note).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class CreditNoteAllocateView(_CreditNoteActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/credit-notes/<id>/allocate/ — apply an already-posted CREDIT note to
    the customer's open invoices. Body ``{allocations:[{invoice, amount}]}``; each amount
    is capped at the invoice balance and the note's unallocated remainder.

    docstring-name: Allocate a credit note
    """
    rbac_permission = "finance.creditnote.allocate"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .credit_notes import allocate_credit_note  # Import dependency used by this finance module.

        entity, note = self._note(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        plan = _allocation_plan(entity, body.get("allocations"))  # Store intermediate finance value.
        allocate_credit_note(note, allocations=plan, actor_user=request.user)  # Store intermediate finance value.
        note.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Credit note {note.document_number} allocated.",  # Finance processing step.
            data=CreditNoteSerializer(note).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Customer refunds                                                             #
# --------------------------------------------------------------------------- #

class RefundListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft) customer refunds for an entity.

    docstring-name: Refunds
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.refund.create" if self.request.method == "POST" \
            else "finance.refund.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = Refund.objects.filter(entity=entity).select_related("customer")  # Query finance data from the database.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        if (customer := request.query_params.get("customer")):  # Branch when this finance condition is true.
            qs = qs.filter(customer=_resolve_customer(entity, customer))  # Store intermediate finance value.
        return _paginate(  # Return the computed finance response.
            request, qs.order_by("-refund_date", "-id"), RefundSerializer, self)  # Finance processing step.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        refund = Refund.objects.create(  # Query finance data from the database.
            entity=entity,  # Store intermediate finance value.
            customer=_resolve_customer(entity, body.get("customer")),  # Store intermediate finance value.
            refund_date=_date(body.get("refund_date"), "refund_date", required=True),  # Store intermediate finance value.
            currency=_resolve_currency(body.get("currency")),  # Store intermediate finance value.
            method=body.get("method", "BANK_TRANSFER"),  # Store intermediate finance value.
            amount=_money(body.get("amount", 0), "amount"),  # Store intermediate finance value.
            bank_account=_resolve_bank_account(  # Store intermediate finance value.
                entity, body.get("bank_account"), required=False),  # Store intermediate finance value.
            reference=body.get("reference", ""),  # Store intermediate finance value.
            narration=body.get("narration", ""),  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Refund {refund.document_number} created.",  # Finance processing step.
            data=RefundSerializer(refund).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _RefundActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _refund(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        refund = Refund.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if refund is None:  # Branch when this finance condition is true.
            raise NotFound("Refund not found for this entity.")  # Surface validation or finance error.
        return entity, refund  # Return the computed finance response.


class RefundDetailView(_RefundActionBase):  # Class groups related finance API or service behavior.
    """GET /finance/refunds/<id>/ — retrieve one customer refund (by id).

    docstring-name: Refunds
    """
    rbac_permission = "finance.refund.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, refund = self._refund(request, pk)  # Store intermediate finance value.
        return success_response("Refund retrieved.", data=RefundSerializer(refund).data)  # Return the computed finance response.


class RefundSubmitView(_RefundActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/refunds/<id>/submit/ — submit a draft refund for approval.

    Hands the refund to the ``vs_workflow`` engine via
    :func:`vs_workflow.services.submission.submit_for_approval`. The handler's
    ``validate_document`` runs the refund preflight now (positive amount, within the
    customer's available credit, a resolvable deposit account) so a doomed refund is
    refused before it enters the queue, and moves it to ``PENDING_APPROVAL``; the GL
    is not touched until final approval fires the handler's ``on_approved`` payout.
    Only meaningful when a template exists for ``finance.refund`` at this refund's
    scope (see :func:`approvals.approval_required`).

    docstring-name: Submit a refund for approval
    """
    rbac_permission = "finance.refund.submit"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from vs_workflow.services.submission import submit_for_approval  # Import dependency used by this finance module.

        _, refund = self._refund(request, pk)  # Store intermediate finance value.
        submit_for_approval(refund, requested_by=request.user)  # Store intermediate finance value.
        refund.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Refund {refund.document_number} submitted for approval.",  # Finance processing step.
            data=RefundSerializer(refund).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class RefundPostView(_RefundActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/refunds/<id>/post/ — post a draft refund, paying the customer's
    credit back out (Dr customer credit / Cr bank) and recording the GL journal.

    When a workflow template is published for this refund's ``finance.refund``
    document type (opt-in gate), direct posting is refused: the refund must go
    through ``/submit/`` and pays out only on approval. With no template, this
    behaves exactly as it always has.

    docstring-name: Post a refund
    """
    rbac_permission = "finance.refund.post"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .approvals import approval_required  # Import dependency used by this finance module.
        from .credit_notes import post_refund  # Import dependency used by this finance module.

        _, refund = self._refund(request, pk)  # Store intermediate finance value.
        if approval_required(refund):  # Branch when this finance condition is true.
            raise ValidationError({  # Surface validation or finance error.
                "detail": "This refund is approval-gated; submit it for approval "  # Finance processing step.
                          "instead of posting directly.",  # Finance processing step.
            })  # Continue structured finance payload.
        post_refund(refund, actor_user=request.user)  # Store intermediate finance value.
        refund.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Refund {refund.document_number} posted.",  # Finance processing step.
            data=RefundSerializer(refund).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Bad-debt write-off                                                          #
# --------------------------------------------------------------------------- #

def _build_write_off_request(entity, body, *, actor_user):  # Function handles this finance operation.
    """Create a DRAFT :class:`WriteOffRequest` from an API body (shared by the
    write-off-request create view and the invoice-write-off bridge).

    ``amount`` defaults to the invoice's outstanding balance when omitted. Resolves
    the invoice, optional write-off account and optional date within ``entity``.
    """
    invoice = _resolve_invoice(entity, body.get("invoice"))  # Store intermediate finance value.
    amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") \
        else invoice.balance_due  # Finance processing step.
    return WriteOffRequest.objects.create(  # Return the computed finance response.
        entity=entity, invoice=invoice, amount=amount,  # Store intermediate finance value.
        write_off_account=_resolve_account(  # Store intermediate finance value.
            entity, body.get("write_off_account"), "write_off_account"),  # Finance processing step.
        write_off_date=_date(body.get("write_off_date"), "write_off_date"),  # Store intermediate finance value.
        narration=body.get("narration", ""),  # Store intermediate finance value.
        reason=body.get("reason", ""),  # Store intermediate finance value.
        created_by=actor_user,  # Store intermediate finance value.
    )  # Continue structured finance payload.


class WriteOffRequestListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft) bad-debt write-off requests for an entity.

    POST body: ``{invoice (doc-no|id, required), amount? (kobo; defaults to the
    invoice's outstanding balance), write_off_account? (code|id), write_off_date?
    (ISO), narration?, reason?}``. Creates a DRAFT request; the actual GL write-off
    runs later, on approval (when gated) or a direct ``/post/`` (when not).

    docstring-name: Write-off requests
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.writeoff.create" if self.request.method == "POST" \
            else "finance.writeoff.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = WriteOffRequest.objects.filter(entity=entity).select_related(  # Query finance data from the database.
            "invoice", "invoice__customer")  # Finance processing step.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        if (invoice := request.query_params.get("invoice")):  # Branch when this finance condition is true.
            qs = qs.filter(invoice=_resolve_invoice(entity, invoice))  # Store intermediate finance value.
        return _paginate(  # Return the computed finance response.
            request, qs.order_by("-id"), WriteOffRequestSerializer, self)  # Finance processing step.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        wor = _build_write_off_request(entity, request.data or {}, actor_user=request.user)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            f"Write-off request {wor.document_number} created.",  # Finance processing step.
            data=WriteOffRequestSerializer(wor).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _WriteOffActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _wor(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        wor = WriteOffRequest.objects.filter(entity=entity, pk=pk).select_related(  # Query finance data from the database.
            "invoice", "invoice__customer").first()  # Finance processing step.
        if wor is None:  # Branch when this finance condition is true.
            raise NotFound("Write-off request not found for this entity.")  # Surface validation or finance error.
        return entity, wor  # Return the computed finance response.


class WriteOffRequestDetailView(_WriteOffActionBase):  # Class groups related finance API or service behavior.
    """GET /finance/write-offs/<id>/ — retrieve one bad-debt write-off request.

    docstring-name: Write-off requests
    """
    rbac_permission = "finance.writeoff.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, wor = self._wor(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Write-off request retrieved.", data=WriteOffRequestSerializer(wor).data)  # Store intermediate finance value.


class WriteOffRequestSubmitView(_WriteOffActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/write-offs/<id>/submit/ — submit a draft write-off for approval.

    Hands the request to ``vs_workflow``; the handler's ``validate_document`` runs the
    write-off preflight (invoice POSTED, outstanding balance, amount within balance)
    now, and moves the request to ``PENDING_APPROVAL``. The invoice is not touched
    until final approval fires the handler's ``on_approved`` write-off.

    docstring-name: Submit a write-off for approval
    """
    rbac_permission = "finance.writeoff.submit"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from vs_workflow.services.submission import submit_for_approval  # Import dependency used by this finance module.

        _, wor = self._wor(request, pk)  # Store intermediate finance value.
        submit_for_approval(wor, requested_by=request.user)  # Store intermediate finance value.
        wor.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Write-off request {wor.document_number} submitted for approval.",  # Finance processing step.
            data=WriteOffRequestSerializer(wor).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class WriteOffRequestPostView(_WriteOffActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/write-offs/<id>/post/ — post a draft write-off request.

    When a workflow template is published for this request's ``finance.write_off``
    document type (opt-in gate), direct posting is refused: it must go through
    ``/submit/`` and writes off only on approval. With no template, this posts the
    bad-debt journal and clears the invoice immediately.

    docstring-name: Post a write-off request
    """
    rbac_permission = "finance.writeoff.post"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .approvals import approval_required  # Import dependency used by this finance module.
        from .credit_notes import post_write_off_request  # Import dependency used by this finance module.

        _, wor = self._wor(request, pk)  # Store intermediate finance value.
        if approval_required(wor):  # Branch when this finance condition is true.
            raise ValidationError({  # Surface validation or finance error.
                "detail": "This write-off is approval-gated; submit it for approval instead.",  # Finance processing step.
            })  # Continue structured finance payload.
        post_write_off_request(wor, actor_user=request.user)  # Store intermediate finance value.
        wor.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Write-off request {wor.document_number} posted.",  # Finance processing step.
            data=WriteOffRequestSerializer(wor).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class InvoiceWriteOffView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST /invoices/<pk>/write-off/ — write off an uncollectable balance as bad debt.

    Now routes through the first-class :class:`WriteOffRequest` document so the same
    entry point picks up approval gating transparently: it builds a DRAFT request from
    the body, then — if a ``finance.write_off`` template is published for this
    invoice's scope — submits it for approval and returns the request; otherwise it
    posts the write-off directly and returns the invoice **exactly as before**, so the
    ungated UX is unchanged.

    docstring-name: Write off an invoice
    """

    rbac_permission = "finance.invoice.writeoff"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .approvals import approval_required  # Import dependency used by this finance module.
        from .credit_notes import post_write_off_request  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        invoice = Invoice.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if invoice is None:  # Branch when this finance condition is true.
            raise NotFound("Invoice not found for this entity.")  # Surface validation or finance error.

        body = dict(request.data or {})  # Store intermediate finance value.
        # The bridge resolves the invoice from the body; pin it to the URL's invoice.
        body["invoice"] = invoice.pk  # Store intermediate finance value.
        wor = _build_write_off_request(entity, body, actor_user=request.user)  # Store intermediate finance value.

        if approval_required(wor):  # Branch when this finance condition is true.
            from vs_workflow.services.submission import submit_for_approval  # Import dependency used by this finance module.

            submit_for_approval(wor, requested_by=request.user)  # Store intermediate finance value.
            wor.refresh_from_db()  # Finance processing step.
            return success_response(  # Return the computed finance response.
                f"Write-off request {wor.document_number} submitted for approval.",  # Finance processing step.
                data=WriteOffRequestSerializer(wor).data,  # Store intermediate finance value.
            )  # Continue structured finance payload.

        post_write_off_request(wor, actor_user=request.user)  # Store intermediate finance value.
        invoice.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Invoice {invoice.document_number} written off.",  # Finance processing step.
            data=InvoiceSerializer(invoice).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


def _writeoff_rows(entity, *, limit=1000):  # Function handles this finance operation.
    """Normalised bad-debt write-off rows from the finance audit log."""
    logs = list(  # Store intermediate finance value.
        FinanceAuditLog.objects.filter(  # Query finance data from the database.
            entity=entity, action=FinanceAuditAction.INVOICE_WRITTEN_OFF,  # Store intermediate finance value.
            status=FinanceAuditStatus.SUCCESS,  # Store intermediate finance value.
        ).order_by("-created_at", "-id")[:limit]  # Continue structured finance payload.
    )  # Continue structured finance payload.
    need_ids = [int(l.target_id) for l in logs  # Store intermediate finance value.
                if not l.metadata.get("customer_code") and str(l.target_id).isdigit()]  # Branch when this finance condition is true.
    invs = {i.id: i for i in Invoice.objects.filter(id__in=need_ids).select_related("customer")} \
        if need_ids else {}  # Branch when this finance condition is true.
    rows = []  # Store intermediate finance value.
    for l in logs:  # Iterate through finance records.
        inv = invs.get(int(l.target_id)) if str(l.target_id).isdigit() else None  # Store intermediate finance value.
        rows.append({  # Finance processing step.
            "key": f"W{l.id}", "kind": "WRITEOFF", "reference": l.document_number,  # Finance processing step.
            "date": l.created_at.date().isoformat(),  # Finance processing step.
            "customer_code": l.metadata.get("customer_code") or (inv.customer.code if inv else ""),  # Finance processing step.
            "customer_name": l.metadata.get("customer_name") or (inv.customer.name if inv else "—"),  # Finance processing step.
            "reason": l.metadata.get("narration") or "Bad-debt write-off",  # Finance processing step.
            "amount": int(l.metadata.get("amount") or 0), "amount_naira": format_naira(int(l.metadata.get("amount") or 0)),  # Finance processing step.
            "status": "POSTED", "refund_id": None,  # Finance processing step.
        })  # Continue structured finance payload.
    return rows  # Return the computed finance response.


class ARAdjustmentListView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET /finance/ar-adjustments/ — unified customer refunds + bad-debt write-offs.

    Filters: ``?type=(refund|writeoff)`` and ``?search=``. The merged list is sorted
    by date and paginated; KPI totals (written-off YTD, pending refund count) ride
    in the response so they stay accurate across pages.

    docstring-name: Refunds & write-offs
    """

    rbac_permission = "finance.refund.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        import math  # Import dependency used by this finance module.
        from rest_framework.response import Response  # Import dependency used by this finance module.
        from django.utils import timezone  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        type_f = (request.query_params.get("type") or "").lower()  # Store intermediate finance value.
        search = (request.query_params.get("search") or "").strip().lower()  # Store intermediate finance value.

        refund_rows = []  # Store intermediate finance value.
        for r in (Refund.objects.filter(entity=entity).select_related("customer")  # Iterate through finance records.
                  .order_by("-refund_date", "-id")[:1000]):  # Finance processing step.
            refund_rows.append({  # Finance processing step.
                "key": f"R{r.id}", "kind": "REFUND", "reference": r.document_number,  # Finance processing step.
                "date": r.refund_date.isoformat() if r.refund_date else "",  # Finance processing step.
                "customer_code": r.customer.code, "customer_name": r.customer.name,  # Finance processing step.
                "reason": r.narration or "Customer refund", "amount": r.amount,  # Finance processing step.
                "amount_naira": format_naira(r.amount), "status": r.status, "refund_id": r.id,  # Finance processing step.
            })  # Continue structured finance payload.
        writeoff_rows = _writeoff_rows(entity)  # Store intermediate finance value.

        # KPI totals — from the full sets, independent of the type filter / page.
        year = timezone.now().year  # Store intermediate finance value.
        written_off_ytd = sum(w["amount"] for w in writeoff_rows if w["date"][:4] == str(year))  # Store intermediate finance value.
        pending = Refund.objects.filter(entity=entity).exclude(status=DocumentStatus.POSTED).count()  # Query finance data from the database.

        rows = []  # Store intermediate finance value.
        if type_f in ("", "refund"):  # Branch when this finance condition is true.
            rows += refund_rows  # Store intermediate finance value.
        if type_f in ("", "writeoff"):  # Branch when this finance condition is true.
            rows += writeoff_rows  # Store intermediate finance value.
        if search:  # Branch when this finance condition is true.
            rows = [x for x in rows if any(  # Store intermediate finance value.
                search in (x.get(k) or "").lower()  # Finance processing step.
                for k in ("reference", "customer_name", "customer_code", "reason"))]  # Iterate through finance records.
        rows.sort(key=lambda x: x["date"], reverse=True)  # Store intermediate finance value.

        page = max(int(request.query_params.get("page", 1) or 1), 1)  # Store intermediate finance value.
        page_size = min(max(int(request.query_params.get("page_size", 20) or 20), 1), 100)  # Store intermediate finance value.
        total = len(rows)  # Store intermediate finance value.
        total_pages = math.ceil(total / page_size) if total else 1  # Store intermediate finance value.
        start = (page - 1) * page_size  # Store intermediate finance value.
        return Response({  # Return the computed finance response.
            "success": True,  # Finance processing step.
            "message": "AR adjustments retrieved.",  # Finance processing step.
            "pagination": {  # Finance processing step.
                "currentPage": page, "pageSize": page_size, "totalItems": total,  # Finance processing step.
                "totalPages": total_pages, "next": None, "previous": None,  # Finance processing step.
            },  # Continue structured finance payload.
            "kpis": {"written_off_ytd": written_off_ytd, "pending": pending},  # Finance processing step.
            "data": rows[start:start + page_size],  # Finance processing step.
        })  # Continue structured finance payload.


class InvoicePayView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST /invoices/<pk>/pay/ — record a customer receipt and settle this invoice.

    Body: ``{amount(kobo), payment_date, method?, deposit_account, reference?,
    narration?}``. Posts the receipt (Dr bank/cash, Cr AR) and allocates it to this
    invoice; any excess remains as unallocated credit on the customer.

    docstring-name: Record a payment
    """

    rbac_permission = "finance.payment.create"  # Store intermediate finance value.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request, pk):  # Function handles this finance operation.
        from .models import Payment  # Import dependency used by this finance module.
        from .receivables import post_payment  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        invoice = Invoice.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if invoice is None:  # Branch when this finance condition is true.
            raise NotFound("Invoice not found for this entity.")  # Surface validation or finance error.
        if invoice.status != "POSTED":  # Branch when this finance condition is true.
            raise ValidationError({"invoice": "Only a posted invoice can be paid."})  # Surface validation or finance error.

        body = request.data or {}  # Store intermediate finance value.
        amount = _money(body.get("amount"), "amount")  # Store intermediate finance value.
        if amount <= 0:  # Branch when this finance condition is true.
            raise ValidationError({"amount": "A positive amount is required."})  # Surface validation or finance error.

        payment = Payment.objects.create(  # Query finance data from the database.
            entity=entity, customer=invoice.customer,  # Store intermediate finance value.
            payment_date=_date(body.get("payment_date"), "payment_date", required=True),  # Store intermediate finance value.
            method=body.get("method") or "BANK_TRANSFER",  # Store intermediate finance value.
            amount=amount,  # Store intermediate finance value.
            deposit_account=_resolve_account(  # Store intermediate finance value.
                entity, body.get("deposit_account"), "deposit_account", required=True),  # Store intermediate finance value.
            currency=invoice.currency,  # Store intermediate finance value.
            reference=body.get("reference", ""),  # Store intermediate finance value.
            narration=body.get("narration", ""),  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        post_payment(payment, actor_user=request.user, allocations=[(invoice, amount)])  # Store intermediate finance value.
        invoice.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Receipt {payment.document_number} recorded against {invoice.document_number}.",  # Finance processing step.
            data=InvoiceSerializer(invoice).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class InvoiceRemindView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST /invoices/<pk>/remind/ — raise & send a dunning reminder for this invoice.

    docstring-name: Send an invoice reminder
    """

    rbac_permission = "finance.dunning.send"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .dunning import remind_invoice  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        invoice = Invoice.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if invoice is None:  # Branch when this finance condition is true.
            raise NotFound("Invoice not found for this entity.")  # Surface validation or finance error.
        notice = remind_invoice(  # Store intermediate finance value.
            invoice, actor_user=request.user,  # Store intermediate finance value.
            message=(request.data or {}).get("message", ""),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Reminder {notice.document_number} sent for {invoice.document_number}.",  # Finance processing step.
            data=DunningNoticeSerializer(notice).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Concessions — discounts / waivers / scholarships                            #
# --------------------------------------------------------------------------- #

class ConcessionListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft) concessions for an entity.

    docstring-name: Concessions
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.concession.create" if self.request.method == "POST" \
            else "finance.concession.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = Concession.objects.filter(entity=entity).select_related("customer", "invoice")  # Query finance data from the database.
        if (kind := request.query_params.get("kind")):  # Branch when this finance condition is true.
            qs = qs.filter(kind=kind)  # Store intermediate finance value.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(status=status_val)  # Store intermediate finance value.
        if (customer := request.query_params.get("customer")):  # Branch when this finance condition is true.
            qs = qs.filter(customer=_resolve_customer(entity, customer))  # Store intermediate finance value.
        if (search := (request.query_params.get("search") or "").strip()):  # Branch when this finance condition is true.
            qs = qs.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search) | Q(reason__icontains=search)  # Store intermediate finance value.
                | Q(invoice__document_number__icontains=search)  # Store intermediate finance value.
                | Q(customer__name__icontains=search) | Q(customer__code__icontains=search)  # Store intermediate finance value.
            )  # Continue structured finance payload.
        paginator = XVSPagination()  # Store intermediate finance value.
        page = paginator.paginate_queryset(qs.order_by("-concession_date", "-id"), request, view=self)  # Store intermediate finance value.
        return paginator.get_paginated_response(ConcessionSerializer(page, many=True).data)  # Return the computed finance response.

    def post(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        concession = Concession.objects.create(  # Query finance data from the database.
            entity=entity,  # Store intermediate finance value.
            customer=_resolve_customer(entity, body.get("customer")),  # Store intermediate finance value.
            invoice=_resolve_invoice(entity, body.get("invoice")),  # Store intermediate finance value.
            kind=body.get("kind", "DISCOUNT"),  # Store intermediate finance value.
            concession_date=_date(body.get("concession_date"), "concession_date", required=True),  # Store intermediate finance value.
            amount=_money(body.get("amount", 0), "amount"),  # Store intermediate finance value.
            allowance_account=_resolve_account(  # Store intermediate finance value.
                entity, body.get("allowance_account"), "allowance_account", required=False),  # Store intermediate finance value.
            reason=body.get("reason", ""),  # Store intermediate finance value.
            reference=body.get("reference", ""),  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"{concession.get_kind_display()} {concession.document_number} created.",  # Finance processing step.
            data=ConcessionSerializer(concession).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _ConcessionActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _concession(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        concession = Concession.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if concession is None:  # Branch when this finance condition is true.
            raise NotFound("Concession not found for this entity.")  # Surface validation or finance error.
        return entity, concession  # Return the computed finance response.


class ConcessionDetailView(_ConcessionActionBase):  # Class groups related finance API or service behavior.
    """GET /finance/concessions/<id>/ — retrieve one concession (discount / waiver /
    scholarship) by id.

    docstring-name: Concessions
    """
    rbac_permission = "finance.concession.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, concession = self._concession(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Concession retrieved.", data=ConcessionSerializer(concession).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ConcessionPostView(_ConcessionActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/concessions/<id>/post/ — post a draft concession, writing the
    discount/waiver/scholarship off against the allowance account (Dr allowance / Cr AR)
    so it reduces the linked invoice's balance and the customer's outstanding.

    docstring-name: Post a concession
    """
    rbac_permission = "finance.concession.post"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .installments import post_concession  # Import dependency used by this finance module.

        _, concession = self._concession(request, pk)  # Store intermediate finance value.
        post_concession(concession, actor_user=request.user)  # Store intermediate finance value.
        concession.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"{concession.get_kind_display()} {concession.document_number} posted.",  # Finance processing step.
            data=ConcessionSerializer(concession).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class ConcessionSummaryView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET /finance/concessions/summary/ — KPI totals (kobo) for the header cards.

    docstring-name: Concession summary
    """

    rbac_permission = "finance.concession.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from django.db.models import Sum  # Import dependency used by this finance module.
        from django.utils import timezone  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = Concession.objects.filter(entity=entity)  # Query finance data from the database.
        posted_ytd = qs.filter(  # Store intermediate finance value.
            status=DocumentStatus.POSTED, concession_date__year=timezone.now().year,  # Store intermediate finance value.
        ).aggregate(s=Sum("amount"))["s"] or 0  # Continue structured finance payload.
        draft_pending = qs.filter(status=DocumentStatus.DRAFT).aggregate(s=Sum("amount"))["s"] or 0  # Store intermediate finance value.
        return success_response("Concession summary retrieved.", data={  # Return the computed finance response.
            "posted_ytd": int(posted_ytd),  # Finance processing step.
            "draft_pending": int(draft_pending),  # Finance processing step.
            "active_count": qs.count(),  # Finance processing step.
        })  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Installment payment plans                                                   #
# --------------------------------------------------------------------------- #

class PaymentPlanListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) / POST (create draft + build schedule) payment plans for an entity.

    docstring-name: Payment plans
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.paymentplan.create" if self.request.method == "POST" \
            else "finance.paymentplan.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = (  # Store intermediate finance value.
            PaymentPlan.objects.filter(entity=entity)  # Query finance data from the database.
            .select_related("customer", "invoice").prefetch_related("installments")  # Finance processing step.
        )  # Continue structured finance payload.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(plan_status=status_val)  # Store intermediate finance value.
        if (customer := request.query_params.get("customer")):  # Branch when this finance condition is true.
            qs = qs.filter(customer=_resolve_customer(entity, customer))  # Store intermediate finance value.
        if (search := (request.query_params.get("search") or "").strip()):  # Branch when this finance condition is true.
            qs = qs.filter(  # Store intermediate finance value.
                Q(document_number__icontains=search) | Q(invoice__document_number__icontains=search)  # Store intermediate finance value.
                | Q(customer__name__icontains=search) | Q(customer__code__icontains=search)  # Store intermediate finance value.
            )  # Continue structured finance payload.
        paginator = XVSPagination()  # Store intermediate finance value.
        page = paginator.paginate_queryset(qs.order_by("-start_date", "-id"), request, view=self)  # Store intermediate finance value.
        return paginator.get_paginated_response(PaymentPlanSerializer(page, many=True).data)  # Return the computed finance response.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        from .installments import build_installments  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        invoice = _resolve_invoice(entity, body.get("invoice"), required=False)  # Store intermediate finance value.
        # Default the spread total to the invoice's outstanding balance when omitted.
        raw_total = body.get("total_amount")  # Store intermediate finance value.
        if raw_total in (None, "") and invoice is not None:  # Branch when this finance condition is true.
            total = invoice.balance_due  # Store intermediate finance value.
        else:  # Fallback finance branch.
            total = _money(raw_total, "total_amount")  # Store intermediate finance value.
        count = int(body.get("installment_count", 1) or 1)  # Store intermediate finance value.
        plan = PaymentPlan.objects.create(  # Query finance data from the database.
            entity=entity,  # Store intermediate finance value.
            customer=_resolve_customer(entity, body.get("customer")),  # Store intermediate finance value.
            invoice=invoice,  # Store intermediate finance value.
            start_date=_date(body.get("start_date"), "start_date", required=True),  # Store intermediate finance value.
            frequency=body.get("frequency", "MONTHLY"),  # Store intermediate finance value.
            installment_count=count,  # Store intermediate finance value.
            total_amount=total,  # Store intermediate finance value.
            notes=body.get("notes", ""),  # Store intermediate finance value.
            created_by=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        amounts = body.get("amounts")  # Store intermediate finance value.
        if amounts:  # Branch when this finance condition is true.
            amounts = [_money(a, f"amounts[{i}]") for i, a in enumerate(amounts)]  # Store intermediate finance value.
        build_installments(plan, amounts=amounts)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            f"Payment plan {plan.document_number} created.",  # Finance processing step.
            data=PaymentPlanSerializer(plan).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class _PaymentPlanActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _plan(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        plan = PaymentPlan.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if plan is None:  # Branch when this finance condition is true.
            raise NotFound("Payment plan not found for this entity.")  # Surface validation or finance error.
        return entity, plan  # Return the computed finance response.


class PaymentPlanDetailView(_PaymentPlanActionBase):  # Class groups related finance API or service behavior.
    """GET /finance/payment-plans/<id>/ — retrieve one installment payment plan (by id),
    including its scheduled installments and progress.

    docstring-name: Payment plans
    """
    rbac_permission = "finance.paymentplan.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, plan = self._plan(request, pk)  # Store intermediate finance value.
        return success_response("Payment plan retrieved.", data=PaymentPlanSerializer(plan).data)  # Return the computed finance response.


class PaymentPlanActivateView(_PaymentPlanActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/payment-plans/<id>/activate/ — move a draft plan into ACTIVE so its
    installment schedule becomes live and can be tracked against customer receipts.

    docstring-name: Activate a payment plan
    """
    rbac_permission = "finance.paymentplan.activate"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .installments import activate_payment_plan  # Import dependency used by this finance module.

        _, plan = self._plan(request, pk)  # Store intermediate finance value.
        activate_payment_plan(plan, actor_user=request.user)  # Store intermediate finance value.
        plan.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Payment plan {plan.document_number} activated.",  # Finance processing step.
            data=PaymentPlanSerializer(plan).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PaymentPlanRefreshView(_PaymentPlanActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/payment-plans/<id>/refresh/ — recompute the plan's progress, marking
    installments paid and advancing plan status. Body may carry a ``settled_amount``
    (kobo) to apply against the schedule; omit it to just re-derive from what's settled.

    docstring-name: Refresh payment plan status
    """
    rbac_permission = "finance.paymentplan.activate"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .installments import refresh_plan_progress  # Import dependency used by this finance module.

        _, plan = self._plan(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        settled = (  # Store intermediate finance value.
            _money(body["settled_amount"], "settled_amount")  # Finance processing step.
            if body.get("settled_amount") not in (None, "") else None  # Branch when this finance condition is true.
        )  # Continue structured finance payload.
        refresh_plan_progress(plan, settled_amount=settled, actor_user=request.user)  # Store intermediate finance value.
        plan.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Payment plan {plan.document_number} progress refreshed.",  # Finance processing step.
            data=PaymentPlanSerializer(plan).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class PaymentPlanCancelView(_PaymentPlanActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/payment-plans/<id>/cancel/ — cancel a plan, closing out its remaining
    installments so it no longer tracks against the customer's balance.

    docstring-name: Cancel a payment plan
    """
    rbac_permission = "finance.paymentplan.cancel"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .installments import cancel_payment_plan  # Import dependency used by this finance module.

        _, plan = self._plan(request, pk)  # Store intermediate finance value.
        cancel_payment_plan(plan, actor_user=request.user)  # Store intermediate finance value.
        plan.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Payment plan {plan.document_number} cancelled.",  # Finance processing step.
            data=PaymentPlanSerializer(plan).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Customer statement of account                                               #
# --------------------------------------------------------------------------- #

class CustomerStatementView(_FinanceBase):  # Class groups related finance API or service behavior.
    """A dated statement of account for one customer (``?customer=<code|id>``).

    Optional ``?start=`` / ``?end=`` ISO dates bound the period (``end`` defaults to
    today; an absent ``start`` runs from inception with a zero opening balance).
    Supports ``?export=csv|xlsx|pdf``. All money is reported in kobo + naira.

    docstring-name: Customer statement
    """

    rbac_permission = "finance.report.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        from .money import format_naira  # Import dependency used by this finance module.
        from .reports import customer_statement  # Import dependency used by this finance module.
        from .views import _maybe_export, _money as _money_pair  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        customer = _resolve_customer(entity, request.query_params.get("customer"))  # Store intermediate finance value.
        start = _date(request.query_params.get("start"), "start")  # Store intermediate finance value.
        end = _date(request.query_params.get("end"), "end")  # Store intermediate finance value.
        stmt = customer_statement(customer, start_date=start, end_date=end)  # Store intermediate finance value.

        from .exports import ReportTable  # Import dependency used by this finance module.

        columns = ["Date", "Type", "Document", "Description", "Debit", "Credit", "Balance"]  # Store intermediate finance value.
        rows = [  # Store intermediate finance value.
            [  # Continue structured finance payload.
                str(e.date), e.doc_type, e.document_number, e.description,  # Finance processing step.
                format_naira(e.debit) if e.debit else "",  # Finance processing step.
                format_naira(e.credit) if e.credit else "",  # Finance processing step.
                format_naira(e.balance),  # Finance processing step.
            ]  # Continue structured finance payload.
            for e in stmt.entries  # Iterate through finance records.
        ]  # Continue structured finance payload.
        summary = ["", "", "", "TOTAL",  # Store intermediate finance value.
                   format_naira(stmt.total_debits), format_naira(stmt.total_credits),  # Finance processing step.
                   format_naira(stmt.closing_balance)]  # Finance processing step.
        period = f"{stmt.start_date or 'inception'} → {stmt.end_date}"  # Store intermediate finance value.
        export = _maybe_export(request, ReportTable(  # Store intermediate finance value.
            title=f"Statement of Account — {stmt.customer_name}",  # Store intermediate finance value.
            subtitle=f"{entity.code} · {stmt.customer_code} · {period} · "  # Store intermediate finance value.
                     f"opening {format_naira(stmt.opening_balance)}",  # Finance processing step.
            columns=columns,  # Store intermediate finance value.
            rows=rows,  # Store intermediate finance value.
            summary_rows=[summary],  # Store intermediate finance value.
        ), filename=f"statement_{entity.code}_{stmt.customer_code}")  # Continue structured finance payload.
        if export is not None:  # Branch when this finance condition is true.
            return export  # Return the computed finance response.

        return success_response(  # Return the computed finance response.
            "Customer statement retrieved.",  # Finance processing step.
            data={  # Store intermediate finance value.
                "entity": entity.code,  # Finance processing step.
                "customer": {  # Finance processing step.
                    "id": stmt.customer_id, "code": stmt.customer_code,  # Finance processing step.
                    "name": stmt.customer_name,  # Finance processing step.
                },  # Continue structured finance payload.
                "start_date": str(stmt.start_date) if stmt.start_date else None,  # Finance processing step.
                "end_date": str(stmt.end_date),  # Finance processing step.
                "opening_balance": _money_pair(stmt.opening_balance),  # Finance processing step.
                "entries": [  # Finance processing step.
                    {  # Continue structured finance payload.
                        "date": str(e.date), "doc_type": e.doc_type,  # Finance processing step.
                        "document_number": e.document_number, "description": e.description,  # Finance processing step.
                        "debit": _money_pair(e.debit), "credit": _money_pair(e.credit),  # Finance processing step.
                        "balance": _money_pair(e.balance),  # Finance processing step.
                    }  # Continue structured finance payload.
                    for e in stmt.entries  # Iterate through finance records.
                ],  # Continue structured finance payload.
                "total_debits": _money_pair(stmt.total_debits),  # Finance processing step.
                "total_credits": _money_pair(stmt.total_credits),  # Finance processing step.
                "closing_balance": _money_pair(stmt.closing_balance),  # Finance processing step.
                "aging": {b: _money_pair(v) for b, v in stmt.aging.items()},  # Finance processing step.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


# --------------------------------------------------------------------------- #
# Dunning — policies, stages and automated reminder notices                   #
# --------------------------------------------------------------------------- #

def _normalize_channels(raw):  # Function handles this finance operation.
    """Coerce a stage channel input (CSV string or list) into a normalised CSV of
    valid DunningChannel values, in enum order, deduped; defaults to EMAIL."""
    from .constants import DunningChannel  # Import dependency used by this finance module.

    parts = ([str(x).strip().upper() for x in raw] if isinstance(raw, (list, tuple))  # Store intermediate finance value.
             else [p.strip().upper() for p in str(raw or "").split(",")])  # Finance processing step.
    chosen = [c for c in DunningChannel.values if c in parts]  # Store intermediate finance value.
    return ",".join(chosen) if chosen else DunningChannel.EMAIL  # Return the computed finance response.


class DunningPolicyListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET (list) dunning policies, or POST to create one (optionally with stages).

    docstring-name: Dunning policies
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.dunning.manage" if self.request.method == "POST" \
            else "finance.dunning.view"  # Finance processing step.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = DunningPolicy.objects.filter(entity=entity).prefetch_related("stages")  # Query finance data from the database.
        return success_response(  # Return the computed finance response.
            "Dunning policies retrieved.",  # Finance processing step.
            data=DunningPolicySerializer(qs.order_by("name"), many=True).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    @transaction.atomic  # Decorator configures the following callable.
    def post(self, request):  # Function handles this finance operation.
        from .dunning import ensure_default_policy  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.

        # Shortcut: seed the standard ladder when explicitly requested.
        if body.get("use_default"):  # Branch when this finance condition is true.
            policy = ensure_default_policy(  # Store intermediate finance value.
                entity, name=body.get("name") or "Standard reminders",  # Store intermediate finance value.
            )  # Continue structured finance payload.
            return success_response(  # Return the computed finance response.
                f"Default dunning policy '{policy.name}' ready.",  # Finance processing step.
                data=DunningPolicySerializer(policy).data, status=201,  # Store intermediate finance value.
            )  # Continue structured finance payload.

        name = (body.get("name") or "").strip()  # Store intermediate finance value.
        if not name:  # Branch when this finance condition is true.
            raise ValidationError({"name": "A policy name is required."})  # Surface validation or finance error.
        policy = DunningPolicy.objects.create(  # Query finance data from the database.
            entity=entity, name=name,  # Store intermediate finance value.
            is_active=bool(body.get("is_active", True)),  # Store intermediate finance value.
            is_default=bool(body.get("is_default", False)),  # Store intermediate finance value.
        )  # Continue structured finance payload.
        for i, raw in enumerate(body.get("stages") or [], start=1):  # Iterate through finance records.
            DunningStage.objects.create(  # Query finance data from the database.
                policy=policy,  # Store intermediate finance value.
                level=int(raw.get("level", i)),  # Store intermediate finance value.
                name=raw.get("name") or f"Stage {i}",  # Store intermediate finance value.
                min_days_overdue=int(raw.get("min_days_overdue", 0)),  # Store intermediate finance value.
                channel=_normalize_channels(raw.get("channel")),  # Store intermediate finance value.
                message=raw.get("message") or "",  # Store intermediate finance value.
            )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Dunning policy '{policy.name}' created.",  # Finance processing step.
            data=DunningPolicySerializer(policy).data, status=201,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class DunningPolicyDetailView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET / PATCH one dunning policy (by id). PATCH updates name / active / default and,
    if ``stages`` is given, replaces the whole reminder ladder.

    docstring-name: Dunning policies
    """

    @property  # Decorator configures the following callable.
    def rbac_permission(self):  # Function handles this finance operation.
        return "finance.dunning.manage" if self.request.method == "PATCH" \
            else "finance.dunning.view"  # Finance processing step.

    def _policy(self, request, pk):  # Function handles this finance operation.
        policy = DunningPolicy.objects.filter(entity=resolve_entity(request), pk=pk).first()  # Query finance data from the database.
        if policy is None:  # Branch when this finance condition is true.
            raise NotFound("Dunning policy not found for this entity.")  # Surface validation or finance error.
        return policy  # Return the computed finance response.

    def get(self, request, pk):  # Function handles this finance operation.
        return success_response(  # Return the computed finance response.
            "Dunning policy retrieved.", data=DunningPolicySerializer(self._policy(request, pk)).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.

    @transaction.atomic  # Decorator configures the following callable.
    def patch(self, request, pk):  # Function handles this finance operation.
        """Update a policy's name / active / default; pass ``stages`` to replace the ladder."""
        policy = self._policy(request, pk)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        if (name := (body.get("name") or "").strip()):  # Branch when this finance condition is true.
            policy.name = name  # Store intermediate finance value.
        if "is_active" in body:  # Branch when this finance condition is true.
            policy.is_active = bool(body["is_active"])  # Store intermediate finance value.
        if body.get("is_default"):  # Branch when this finance condition is true.
            DunningPolicy.objects.filter(entity=policy.entity, is_default=True).exclude(pk=policy.pk).update(is_default=False)  # Query finance data from the database.
            policy.is_default = True  # Store intermediate finance value.
        elif "is_default" in body:  # Alternative finance branch.
            policy.is_default = False  # Store intermediate finance value.
        policy.save()  # Finance processing step.
        if "stages" in body:  # Branch when this finance condition is true.
            policy.stages.all().delete()  # Finance processing step.
            for i, raw in enumerate(body.get("stages") or [], start=1):  # Iterate through finance records.
                DunningStage.objects.create(  # Query finance data from the database.
                    policy=policy, level=int(raw.get("level", i)),  # Store intermediate finance value.
                    name=raw.get("name") or f"Stage {i}",  # Store intermediate finance value.
                    min_days_overdue=int(raw.get("min_days_overdue", 0)),  # Store intermediate finance value.
                    channel=_normalize_channels(raw.get("channel")), message=raw.get("message") or "",  # Store intermediate finance value.
                )  # Continue structured finance payload.
        policy.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Dunning policy '{policy.name}' updated.", data=DunningPolicySerializer(policy).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class DunningGenerateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """POST: run a dunning policy over the entity's overdue invoices, raising notices.

    docstring-name: Generate dunning notices
    """

    rbac_permission = "finance.dunning.generate"  # Store intermediate finance value.

    def post(self, request):  # Function handles this finance operation.
        from .dunning import generate_dunning  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        body = request.data or {}  # Store intermediate finance value.
        as_of = _date(body.get("as_of"), "as_of")  # Store intermediate finance value.
        policy = None  # Store intermediate finance value.
        if body.get("policy") not in (None, ""):  # Branch when this finance condition is true.
            policy = DunningPolicy.objects.filter(  # Query finance data from the database.
                entity=entity, pk=body["policy"],  # Store intermediate finance value.
            ).first() if str(body["policy"]).isdigit() else \
                DunningPolicy.objects.filter(entity=entity, name=body["policy"]).first()  # Query finance data from the database.
            if policy is None:  # Branch when this finance condition is true.
                raise NotFound(f"No dunning policy matches '{body['policy']}'.")  # Surface validation or finance error.
        customer = _resolve_customer(entity, body.get("customer"), required=False)  # Store intermediate finance value.

        notices = generate_dunning(  # Store intermediate finance value.
            entity, as_of=as_of, policy=policy, customer=customer,  # Store intermediate finance value.
            actor_user=request.user,  # Store intermediate finance value.
        )  # Continue structured finance payload.
        return success_response(  # Return the computed finance response.
            f"Generated {len(notices)} dunning notice(s).",  # Finance processing step.
            data={  # Store intermediate finance value.
                "created": len(notices),  # Finance processing step.
                "notices": DunningNoticeSerializer(notices, many=True).data,  # Store intermediate finance value.
            },  # Continue structured finance payload.
        )  # Continue structured finance payload.


class DunningSummaryView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET /finance/dunning/summary/ — open-receivable aging buckets for the header.

    ``due_soon`` is the next 7 days (not yet overdue); the rest are days past due.

    docstring-name: Dunning summary
    """

    rbac_permission = "finance.dunning.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        import datetime  # Import dependency used by this finance module.

        from django.db.models import F  # Import dependency used by this finance module.

        entity = resolve_entity(request)  # Store intermediate finance value.
        today = datetime.date.today()  # Store intermediate finance value.
        buckets = {k: {"amount": 0, "count": 0} for k in  # Store intermediate finance value.
                   ("due_soon", "overdue_1_30", "overdue_31_60", "overdue_60_plus")}  # Continue structured finance payload.
        # Drop fully-settled invoices in SQL (balance_due is a property); only the
        # date-bucketing is left to Python, over the still-owing set.
        balance = F("total") - F("amount_paid") - F("amount_credited")  # Store intermediate finance value.
        owing = (Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)  # Query finance data from the database.
                 .exclude(due_date__isnull=True)  # Store intermediate finance value.
                 .annotate(_balance=balance).filter(_balance__gt=0)  # Store intermediate finance value.
                 .only("due_date", "total", "amount_paid", "amount_credited"))  # Finance processing step.
        for inv in owing:  # Iterate through finance records.
            bal = inv.balance_due  # Store intermediate finance value.
            d = (today - inv.due_date).days  # >0 overdue, <=0 upcoming
            if -7 <= d <= 0:  # Branch when this finance condition is true.
                key = "due_soon"  # Store intermediate finance value.
            elif 1 <= d <= 30:  # Alternative finance branch.
                key = "overdue_1_30"  # Store intermediate finance value.
            elif 31 <= d <= 60:  # Alternative finance branch.
                key = "overdue_31_60"  # Store intermediate finance value.
            elif d > 60:  # Alternative finance branch.
                key = "overdue_60_plus"  # Store intermediate finance value.
            else:  # Fallback finance branch.
                continue  # Finance processing step.
            buckets[key]["amount"] += bal  # Store intermediate finance value.
            buckets[key]["count"] += 1  # Store intermediate finance value.
        return success_response("Dunning summary retrieved.", data=buckets)  # Return the computed finance response.


class DunningNoticeListCreateView(_FinanceBase):  # Class groups related finance API or service behavior.
    """GET dunning notices for an entity (filterable by status / customer / invoice).

    docstring-name: Dunning notices
    """

    rbac_permission = "finance.dunning.view"  # Store intermediate finance value.

    def get(self, request):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        qs = DunningNotice.objects.filter(entity=entity).select_related("customer", "invoice")  # Query finance data from the database.
        if (status_val := request.query_params.get("status")):  # Branch when this finance condition is true.
            qs = qs.filter(notice_status=status_val)  # Store intermediate finance value.
        if (customer := request.query_params.get("customer")):  # Branch when this finance condition is true.
            qs = qs.filter(customer=_resolve_customer(entity, customer))  # Store intermediate finance value.
        if (invoice := request.query_params.get("invoice")):  # Branch when this finance condition is true.
            qs = qs.filter(invoice=_resolve_invoice(entity, invoice))  # Store intermediate finance value.
        return _paginate(  # Return the computed finance response.
            request, qs.order_by("-notice_date", "-id"), DunningNoticeSerializer, self)  # Finance processing step.


class _DunningNoticeActionBase(_FinanceBase):  # Class groups related finance API or service behavior.
    def _notice(self, request, pk):  # Function handles this finance operation.
        entity = resolve_entity(request)  # Store intermediate finance value.
        notice = DunningNotice.objects.filter(entity=entity, pk=pk).first()  # Query finance data from the database.
        if notice is None:  # Branch when this finance condition is true.
            raise NotFound("Dunning notice not found for this entity.")  # Surface validation or finance error.
        return entity, notice  # Return the computed finance response.


class DunningNoticeDetailView(_DunningNoticeActionBase):  # Class groups related finance API or service behavior.
    """GET /finance/dunning-notices/<id>/ — retrieve one dunning (reminder) notice by id.

    docstring-name: Dunning notices
    """
    rbac_permission = "finance.dunning.view"  # Store intermediate finance value.

    def get(self, request, pk):  # Function handles this finance operation.
        _, notice = self._notice(request, pk)  # Store intermediate finance value.
        return success_response(  # Return the computed finance response.
            "Dunning notice retrieved.", data=DunningNoticeSerializer(notice).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class DunningNoticeSendView(_DunningNoticeActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/dunning-notices/<id>/send/ — dispatch a pending notice over its
    stage's channels (in-app + email) and mark it SENT.

    docstring-name: Send a dunning notice
    """
    rbac_permission = "finance.dunning.send"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .dunning import mark_notice_sent  # Import dependency used by this finance module.

        _, notice = self._notice(request, pk)  # Store intermediate finance value.
        mark_notice_sent(notice, actor_user=request.user)  # Store intermediate finance value.
        notice.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Dunning notice {notice.document_number} marked sent.",  # Finance processing step.
            data=DunningNoticeSerializer(notice).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.


class DunningNoticeCancelView(_DunningNoticeActionBase):  # Class groups related finance API or service behavior.
    """POST /finance/dunning-notices/<id>/cancel/ — cancel a notice before it goes out,
    recording an optional ``reason``.

    docstring-name: Cancel a dunning notice
    """
    rbac_permission = "finance.dunning.send"  # Store intermediate finance value.

    def post(self, request, pk):  # Function handles this finance operation.
        from .dunning import cancel_notice  # Import dependency used by this finance module.

        _, notice = self._notice(request, pk)  # Store intermediate finance value.
        reason = (request.data or {}).get("reason", "")  # Store intermediate finance value.
        cancel_notice(notice, reason=reason, actor_user=request.user)  # Store intermediate finance value.
        notice.refresh_from_db()  # Finance processing step.
        return success_response(  # Return the computed finance response.
            f"Dunning notice {notice.document_number} cancelled.",  # Finance processing step.
            data=DunningNoticeSerializer(notice).data,  # Store intermediate finance value.
        )  # Continue structured finance payload.
