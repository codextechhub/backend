"""REST API for the AR adjustment cycle (mounted at ``/v1/finance/``).

Credit/debit notes, customer refunds, bad-debt write-offs, concessions
(discounts/waivers/scholarships) and installment payment plans - the give-back and
"how they pay" side of receivables that complements the invoice/payment endpoints. Same
conventions as the rest of the surface: entity-scoped via ``?entity=<id|code>``, the
platform ``{success, message, data}`` envelope, RBAC-gated
(``finance.<resource>.<action>``), and thin views that resolve by **code or id** then
hand off to the :mod:`vs_finance.credit_notes` / :mod:`vs_finance.installments` services
which own every posting. Money is integer kobo.
"""
from __future__ import annotations

import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F, Q
from django.http import HttpResponse
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from core.pagination import XVSPagination
from core.response import success_response
from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission


# Support the paginate workflow.
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
    WriteOffRequest,
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
    WriteOffRequestSerializer,
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


# Support the resolve customer workflow.
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


# Support the resolve invoice workflow.
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


# Support the resolve debit note workflow.
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


# Support the allocation plan workflow.
def _allocation_plan(entity, raw_allocations):
    """Coerce a request ``allocations`` list into ``[(target, amount_kobo), ...]``.

    Each item settles an invoice (``{"invoice": ref, "amount": …}``) or a DEBIT note
    (``{"debit_note": ref, "amount": …}``) - both debit AR and are settled by receipts.
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


# Support the allocation strategy workflow.
def _allocation_strategy(raw, *, default="oldest"):
    """Validate an optional allocation strategy using the supplied entity default."""
    from .receivables import ALLOCATION_STRATEGIES

    val = str(raw or default).lower()
    if val not in ALLOCATION_STRATEGIES:
        raise ValidationError(
            {"allocation_strategy": f"Must be one of: {', '.join(ALLOCATION_STRATEGIES)}."})
    return val


def _reversal_date(body):
    """Parse the shared optional date/reversal_date void request field."""
    body = body or {}
    raw = body.get("date") or body.get("reversal_date")
    return _date(raw, "date")


# --------------------------------------------------------------------------- #
# Customers / payers                                                          #
# --------------------------------------------------------------------------- #

# Support the customer ledger workflow.
def _customer_ledger(entity, customer_ids=None):
    """Net AR position per customer, in two aggregate queries (no per-row N+1).

    Returns ``{customer_id: {"outstanding", "credit", "overdue", "lifetime_paid"}}``
    where ``outstanding`` is the sum of open invoice balances and ``credit`` the
    customer's stored 2140 position. Net = outstanding − credit (positive owes,
    negative in credit). Computed in a few aggregate queries (no N+1).

    ``credit`` comes from :func:`~vs_finance.receivables.customer_credit_balances`
    rather than being re-derived here. This screen used to keep its own copy of that
    arithmetic, which is how the console ended up with two definitions of "available
    credit" that could disagree - one of them refund-blind.
    """
    import datetime
    from django.db.models import F, Q, Sum
    from django.db.models.functions import Coalesce

    from .constants import CreditNoteKind, DocumentStatus
    from .models import CreditNote, Invoice, Payment
    from .receivables import customer_credit_balances

    today = datetime.date.today()
    bal = F("total") - F("amount_paid") - F("amount_credited")
    inv = Invoice.objects.filter(entity=entity, status=DocumentStatus.POSTED)
    pay = Payment.objects.filter(entity=entity, status=DocumentStatus.POSTED)
    # Open DEBIT notes are supplementary AR charges: their unsettled balance is
    # outstanding, exactly like an open invoice.
    dn = CreditNote.objects.filter(entity=entity, status=DocumentStatus.POSTED, kind=CreditNoteKind.DEBIT)
    if customer_ids is not None:
        inv = inv.filter(customer_id__in=customer_ids)
        pay = pay.filter(customer_id__in=customer_ids)
        dn = dn.filter(customer_id__in=customer_ids)

    out: dict[int, dict] = {}

    # Handle the slot workflow.
    def slot(cid):
        return out.setdefault(cid, {"outstanding": 0, "credit": 0, "overdue": False,
                                    "lifetime_paid": 0})

    for r in inv.values("customer_id").annotate(
        outstanding=Coalesce(Sum(bal), 0),
        overdue_bal=Coalesce(Sum(bal, filter=Q(due_date__lt=today)), 0),
    ):
        d = slot(r["customer_id"])
        d["outstanding"] = int(r["outstanding"] or 0)
        d["overdue"] = int(r["overdue_bal"] or 0) > 0
    for r in pay.values("customer_id").annotate(lifetime=Coalesce(Sum("amount"), 0)):
        slot(r["customer_id"])["lifetime_paid"] = int(r["lifetime"] or 0)
    for r in dn.values("customer_id").annotate(
        c=Coalesce(Sum(F("total") - F("amount_paid")), 0)):
        slot(r["customer_id"])["outstanding"] += int(r["c"] or 0)
    for cid, credit in customer_credit_balances(entity, customer_ids).items():
        slot(cid)["credit"] = credit
    return out


# Support the account status workflow.
def _account_status(net: int, overdue: bool) -> str:
    """Derive the customer's account status pill from net balance + aging."""
    if net < 0:
        return "CREDIT"
    if overdue:
        return "OVERDUE"
    return "ACTIVE"


# Support the money obj workflow.
def _money_obj(kobo) -> dict:
    """Money payload {kobo, naira} - the AR drawer shape (mirrors views._money)."""
    from .money import format_naira
    return {"kobo": int(kobo), "naira": format_naira(int(kobo))}

# Group endpoint behavior for Customer List Create View.
class CustomerListCreateView(_FinanceBase):
    """Customers / payers for an entity.

    List filters: ``?search=`` (code or name), ``?is_active=true|false``.
    Customer codes are allocated by the model when a create request omits one;
    explicit codes remain accepted for trusted imports and existing API clients.

    docstring-name: Customers
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.customer.create" if self.request.method == "POST" \
            else "finance.customer.view"

    # Handle GET requests for this endpoint.
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
    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        body = request.data or {}
        code = str(body.get("code", "")).strip().upper()
        if code and Customer.objects.filter(entity=entity, code=code).exists():
            raise ValidationError({"code": f"A customer with code '{code}' already exists."})
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValidationError({"name": "A customer name is required."})
        billing_email = str(body.get("billing_email", "")).strip().lower()
        if not billing_email:
            raise ValidationError({"billing_email": "A billing email is required."})
        try:
            validate_email(billing_email)
        except DjangoValidationError:
            raise ValidationError({"billing_email": "Enter a valid billing email."})
        billing_phone = str(body.get("billing_phone", "")).strip()
        if not billing_phone:
            raise ValidationError({"billing_phone": "A billing phone number is required."})
        if len(billing_phone) > Customer._meta.get_field("billing_phone").max_length:
            raise ValidationError({"billing_phone": "Billing phone number cannot exceed 32 characters."})
        # A caller may choose a customer-specific control account; otherwise use
        # the entity's audited Accounts Receivable mapping.
        if body.get("receivable_account"):
            receivable = _resolve_account(
                entity, body.get("receivable_account"),
                "receivable_account", required=True,
            )
        else:
            from .account_mappings import resolve_mapped_account
            from .constants import AccountMappingKey
            receivable = resolve_mapped_account(
                entity, AccountMappingKey.ACCOUNTS_RECEIVABLE,
                label="receivable account",
            )
        opening_balance = _money(body.get("opening_balance", 0), "opening_balance")
        from .document_settings import resolve_finance_document_settings
        policy = resolve_finance_document_settings(entity)
        if opening_balance and not policy.allow_customer_opening_balances:
            raise ValidationError({
                "opening_balance": "Opening balances are disabled by this entity's Finance document policy.",
            })
        customer = Customer.objects.create(
            entity=entity, code=code, name=name,
            billing_email=billing_email,
            billing_phone=billing_phone,
            billing_address=body.get("billing_address", ""),
            receivable_account=receivable,
            opening_balance=opening_balance,
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


# Group endpoint behavior for Customer Detail View.
class CustomerDetailView(_FinanceBase):
    """Get the details of one customer (by **code or id**).

    docstring-name: Customers
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.customer.update" if self.request.method == "PATCH" \
            else "finance.customer.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        import datetime

        from .constants import CreditNoteKind, DocumentStatus, InvoicePaymentStatus
        from .models import Concession, CreditNote, Invoice, Payment, Refund
        from .reports import VOID_MOVEMENT_TYPE, customer_account_movements

        entity = resolve_entity(request)
        customer = _resolve_customer(entity, pk)
        led = _customer_ledger(entity, [customer.id]).get(customer.id, {})
        net = led.get("outstanding", 0) - led.get("credit", 0)
        today = datetime.date.today()

        # A voided document is still part of the account's history: it moved the
        # balance on its own date and was undone on the reversal's date, and
        # ``customer_account_movements`` renders both rows. Load REVERSED alongside
        # POSTED so it can - the open-item panels below re-filter to POSTED, because a
        # voided invoice is history but is not something the customer still owes.
        history = (DocumentStatus.POSTED, DocumentStatus.REVERSED)
        invoices = list(Invoice.objects.filter(
            entity=entity, customer=customer, status__in=history,
        ).order_by("invoice_date", "id")[:500])
        payments = list(Payment.objects.filter(
            entity=entity, customer=customer, status__in=history,
        ).order_by("payment_date", "id")[:500])
        credit_notes = list(CreditNote.objects.filter(
            entity=entity, customer=customer, status__in=history,
        ).order_by("note_date", "id")[:500])
        # DEBIT notes are supplementary AR charges - their unsettled balance is an
        # open item, just like an invoice. CREDIT notes remain account movements but
        # are value returned to the customer, not amounts the customer still owes.
        debit_notes = [
            n for n in credit_notes
            if n.kind == CreditNoteKind.DEBIT and n.status == DocumentStatus.POSTED
        ]
        open_invoice_pool = [i for i in invoices if i.status == DocumentStatus.POSTED]
        refunds = list(Refund.objects.filter(
            entity=entity, customer=customer, status__in=history,
        ).order_by("refund_date", "id")[:500])
        concessions = list(Concession.objects.filter(
            entity=entity, customer=customer, status__in=history,
        ).order_by("concession_date", "id")[:500])

        # Handle the inv status workflow.
        def inv_status(i):
            if i.payment_status == InvoicePaymentStatus.PAID:
                return "PAID"
            if i.due_date and i.due_date < today and i.balance_due > 0:
                return "OVERDUE"
            if i.payment_status == InvoicePaymentStatus.PARTIAL:
                return "PARTIAL"
            return "ISSUED"

        # Handle the dn status workflow.
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
            for i in open_invoice_pool if i.balance_due > 0
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

        # Transactions and statement share the exportable statement's authoritative
        # movement source. This includes CREDIT notes (credit), refunds (debit) and
        # concessions (credit), not only invoices, DEBIT notes and receipts.
        movements = customer_account_movements(
            customer,
            invoices=invoices,
            credit_notes=credit_notes,
            refunds=refunds,
            payments=payments,
            concessions=concessions,
        )
        transaction_types = {
            "Invoice": "INVOICE",
            "Debit note": "DEBIT_NOTE",
            "Credit note": "CREDIT_NOTE",
            "Receipt": "PAYMENT",
            "Refund": "REFUND",
        }
        transaction_statuses = {
            **{("Invoice", invoice.document_number): inv_status(invoice)
               for invoice in open_invoice_pool},
            **{("Debit note", note.document_number): dn_status(note)
               for note in debit_notes},
        }
        transactions = [
            {
                "date": date.isoformat(),
                "type": ("VOID" if doc_type == VOID_MOVEMENT_TYPE
                         else transaction_types.get(doc_type, "ADJUSTMENT")),
                "reference": number,
                "amount": _money_obj(debit or credit),
                # A void row is the undo, not the document, so it never inherits the
                # document's settlement status.
                "status": ("REVERSED" if doc_type == VOID_MOVEMENT_TYPE
                           else transaction_statuses.get((doc_type, number), "POSTED")),
            }
            for date, _order, doc_type, number, _description, debit, credit
            in sorted(movements, key=lambda movement: movement[0], reverse=True)
        ]

        # An opening balance is already materialised as a posted OPENING invoice (see
        # post_opening_balance), so it rides in the shared movements and must not be
        # added again.
        running = 0
        statement = []
        for date, _order, doc_type, number, description, debit, credit in movements:
            running += debit - credit
            detail = (
                f" · {description}"
                if description and description.casefold() != doc_type.casefold()
                else ""
            )
            statement.append({
                "date": None if date == datetime.date.min else date.isoformat(),
                "description": f"{doc_type} {number}{detail}",
                "debit": _money_obj(debit),
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
    # Handle PATCH requests for this endpoint.
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
            opening_balance = _money(body.get("opening_balance"), "opening_balance")
            from .document_settings import resolve_finance_document_settings
            if (
                opening_balance
                and not resolve_finance_document_settings(entity).allow_customer_opening_balances
            ):
                raise ValidationError({
                    "opening_balance": "Opening balances are disabled by this entity's Finance document policy.",
                })
            customer.opening_balance = opening_balance
        if "is_active" in body:
            customer.is_active = bool(body.get("is_active"))
        customer.save()
        return success_response(
            f"Customer {customer.code} updated.", data=CustomerSerializer(customer).data,
        )


# Group endpoint behavior for Customer Receipt View.
class CustomerReceiptView(_FinanceBase):
    """POST /customers/<pk>/receipt/ - record a receipt for a customer and auto-
    allocate it across their open invoices (oldest first). Any excess stays as
    unallocated credit on the customer.

    docstring-name: Record a customer receipt
    """

    rbac_permission = "finance.payment.create"

    @transaction.atomic
    # Handle POST requests for this endpoint.
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
        from .banking_settings import resolve_finance_banking_settings
        policy = resolve_finance_banking_settings(entity)
        post_payment(payment, actor_user=request.user, auto_allocate=bool(auto),
                     strategy=_allocation_strategy(
                         body.get("allocation_strategy"),
                         default=policy.default_receipt_allocation_strategy,
                     ))
        return success_response(
            f"Receipt {payment.document_number} recorded for {customer.code}.",
            data={
                "id": payment.id,
                "payment": payment.document_number,
                "allocated": payment.allocated_amount,
                "unallocated": payment.credit_remaining,
            },
            status=201,
        )


# --------------------------------------------------------------------------- #
# Receipts & allocation                                                       #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Customer Summary View.
class CustomerSummaryView(_FinanceBase):
    """GET /finance/customers/summary/ - entity-wide KPI totals + status counts for the
    Customers header cards (computed over ALL rows, so they stay accurate while the list
    itself paginates). Honors the same ``?search=``/``?is_active=`` as the list.

    docstring-name: Customer summary
    """

    rbac_permission = "finance.customer.view"

    # Handle GET requests for this endpoint.
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


# Group endpoint behavior for Payment List View.
class PaymentListView(_FinanceBase):
    """GET /finance/payments/ - customer receipts and their allocation state.

    Filters: ``?status=`` (ALLOCATED|PARTIAL|UNALLOCATED|REFUNDED), ``?method=``,
    ``?customer=`` (code/id), ``?search=`` (doc no / customer / reference).

    REFUNDED is a receipt whose cash never settled a bill but has since been paid
    back out; it is excluded from UNALLOCATED because that money is gone.

    docstring-name: Customer receipts
    """

    rbac_permission = "finance.payment.view"

    # Handle GET requests for this endpoint.
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

        # allocation_status is derived from allocated_amount/refunded_amount vs amount;
        # express it as a DB filter so paging counts are correct (it used to filter
        # post-slice in Python). Mirror PaymentSerializer.get_allocation_status exactly
        # - refunded is checked before unallocated, or refunded cash would be counted
        # as still available.
        status_f = request.query_params.get("status")
        if status_f == "ALLOCATED":
            qs = qs.filter(allocated_amount__gte=F("amount"))
        elif status_f == "REFUNDED":
            qs = qs.filter(allocated_amount__lt=F("amount"),
                           refunded_amount__gte=F("amount") - F("allocated_amount"))
        elif status_f == "UNALLOCATED":
            qs = qs.filter(allocated_amount__lte=0,
                           refunded_amount__lt=F("amount") - F("allocated_amount"))
        elif status_f == "PARTIAL":
            qs = qs.filter(allocated_amount__gt=0, allocated_amount__lt=F("amount"),
                           refunded_amount__lt=F("amount") - F("allocated_amount"))
        return _paginate(request, qs.order_by("-payment_date", "-id"), PaymentSerializer, self)


# Group endpoint behavior for Payment Summary View.
class PaymentSummaryView(_FinanceBase):
    """GET /finance/payments/summary/ - receipts KPI totals + allocation-status counts
    for the header cards, over ALL rows (accurate while the list paginates). Honors the
    same ``?method=``/``?customer=``/``?search=`` as the list.

    docstring-name: Receipts summary
    """

    rbac_permission = "finance.payment.view"

    # Handle GET requests for this endpoint.
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
        week_start = today - datetime.timedelta(days=6)
        # "Sitting unapplied" must mean money that is still there. Summing
        # ``amount - allocated_amount`` counted cash that had already been refunded
        # back out, so the KPI kept advertising credit the customer no longer had.
        # ``credit_remaining`` nets the refunds off; ``refunded`` is surfaced beside it
        # so the difference is visible rather than mysterious.
        unspent = F("amount") - F("allocated_amount") - F("refunded_amount")
        fully_refunded = Q(allocated_amount__lt=F("amount"),
                           refunded_amount__gte=F("amount") - F("allocated_amount"))
        agg = qs.aggregate(
            count=Count("id"),
            today=Coalesce(Sum("amount", filter=Q(payment_date=today)), 0),
            week=Coalesce(Sum("amount", filter=Q(payment_date__gte=week_start)), 0),
            unallocated=Coalesce(Sum(unspent), 0),
            refunded=Coalesce(Sum("refunded_amount"), 0),
            allocated_c=Count("id", filter=Q(allocated_amount__gte=F("amount"))),
            refunded_c=Count("id", filter=fully_refunded),
            unallocated_c=Count("id", filter=Q(allocated_amount__lte=0) & ~fully_refunded),
            partial_c=Count("id", filter=Q(
                allocated_amount__gt=0, allocated_amount__lt=F("amount")) & ~fully_refunded),
        )
        return success_response("Receipts summary retrieved.", data={
            "count": agg["count"],
            "today": _money_obj(agg["today"]),
            "week": _money_obj(agg["week"]),
            "unallocated": _money_obj(agg["unallocated"]),
            "refunded": _money_obj(agg["refunded"]),
            "status_counts": {
                "ALLOCATED": agg["allocated_c"],
                "PARTIAL": agg["partial_c"],
                "UNALLOCATED": agg["unallocated_c"],
                "REFUNDED": agg["refunded_c"],
            },
        })


# Group endpoint behavior for Payment Detail View.
class PaymentDetailView(_FinanceBase):
    """GET /finance/payments/<id>/ - a receipt, its current allocations, the
    customer's open invoices (allocation candidates) and the receipt's GL posting.

    docstring-name: Customer receipts
    """

    rbac_permission = "finance.payment.view"

    # Handle GET requests for this endpoint.
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
        # DEBIT notes are settleable AR items too - offer the customer's open ones.
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


# Group endpoint behavior for Payment Receipt View.
class PaymentReceiptView(_FinanceBase):
    """GET /finance/payments/<id>/receipt/ - printable HTML payment receipt."""

    rbac_permission = "finance.payment.view"

    # Support the payment workflow.
    def _payment(self, request, pk):
        from .constants import DocumentStatus
        from .models import Payment

        entity = resolve_entity(request)
        payment = (
            Payment.objects.filter(entity=entity, pk=pk, status=DocumentStatus.POSTED)
            .select_related("entity__tenant__school_profile", "branch", "customer")
            .prefetch_related("allocations__invoice")
            .first()
        )
        if payment is None:
            raise NotFound("Receipt not found for this entity.")
        return payment

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        from .documents import render_receipt_document_html

        html = render_receipt_document_html(self._payment(request, pk), request=request)
        return HttpResponse(html, content_type="text/html; charset=utf-8")


# Group endpoint behavior for Payment Allocate View.
class PaymentAllocateView(_FinanceBase):
    """POST /finance/payments/<id>/allocate/ - apply a receipt to open invoices.

    Body ``{allocations:[{invoice, amount}]}`` for an explicit split, or
    ``{auto_allocate:true}`` to settle oldest-first. Each amount is capped at the
    invoice balance and the receipt's remaining cash; excess stays as credit.

    docstring-name: Allocate a receipt
    """

    rbac_permission = "finance.payment.allocate"

    @transaction.atomic
    # Handle POST requests for this endpoint.
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
            from .banking_settings import resolve_finance_banking_settings
            policy = resolve_finance_banking_settings(entity)
            allocate_payment(p, actor_user=request.user,
                             strategy=_allocation_strategy(
                                 body.get("allocation_strategy"),
                                 default=policy.default_receipt_allocation_strategy,
                             ))
        else:
            raise ValidationError({"allocations": "Provide allocations or auto_allocate=true."})
        p.refresh_from_db()
        return success_response(
            f"Receipt {p.document_number} allocated.",
            data=PaymentSerializer(p).data,
        )


class PaymentVoidView(_FinanceBase):
    """POST /finance/payments/<id>/void/ - void a posted receipt atomically."""

    rbac_permission = "finance.payment.reverse"

    def post(self, request, pk):
        from .models import Payment
        from .voids import void_payment

        entity = resolve_entity(request)
        payment = Payment.objects.filter(entity=entity, pk=pk).first()
        if payment is None:
            raise NotFound("Receipt not found for this entity.")
        void_payment(
            payment, actor_user=request.user,
            date=_reversal_date(request.data),
        )
        payment.refresh_from_db()
        return success_response(
            f"Receipt {payment.document_number} voided.",
            data=PaymentSerializer(payment).data,
        )


class InvoiceVoidView(_FinanceBase):
    """POST /finance/invoices/<id>/void/ - void an unencumbered posted invoice."""

    rbac_permission = "finance.invoice.reverse"

    def post(self, request, pk):
        from .models import Invoice
        from .voids import void_invoice

        entity = resolve_entity(request)
        invoice = Invoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("Invoice not found for this entity.")
        void_invoice(
            invoice, actor_user=request.user,
            date=_reversal_date(request.data),
        )
        invoice.refresh_from_db()
        return success_response(
            f"Invoice {invoice.document_number} voided.",
            data=InvoiceSerializer(invoice).data,
        )


# --------------------------------------------------------------------------- #
# Fee structures (billing catalogue → invoices)                               #
# --------------------------------------------------------------------------- #

# Support the build fee items workflow.
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
            tax_code=_resolve_tax(
                entity, item.get("tax_code"), f"items[{i}].tax_code",
                usage="sales",
            ),
            is_optional=bool(item.get("is_optional", False)),
        )


# Support the resolve applies to workflow.
def _resolve_applies_to(raw):
    """Validate a fee-structure ``applies_to`` value, defaulting to CUSTOMER."""
    if raw in (None, ""):
        return FeeAppliesTo.CUSTOMER
    value = str(raw).upper()
    if value not in FeeAppliesTo.values:
        raise ValidationError({"applies_to":
            f"Must be one of {', '.join(FeeAppliesTo.values)}."})
    return value


# Support the resolve fee structure workflow.
def _resolve_fee_structure(entity, ref):
    qs = FeeStructure.objects.filter(entity=entity)
    structure = (
        qs.filter(pk=int(ref)).first() if str(ref).isdigit()
        else qs.filter(code=str(ref).upper()).first()
    )
    if structure is None:
        raise NotFound(f"No fee structure matches '{ref}' for this entity.")
    return structure


# Group endpoint behavior for Fee Structure List Create View.
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
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.feestructure.create" if self.request.method == "POST" \
            else "finance.feestructure.view"

    # Handle GET requests for this endpoint.
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
    # Handle POST requests for this endpoint.
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


# Group endpoint behavior for Fee Structure Detail View.
class FeeStructureDetailView(_FinanceBase):
    """GET / PATCH one fee structure (by **code or id**). PATCH may replace ``items``.

    docstring-name: Fee structures
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.feestructure.edit" if self.request.method == "PATCH" \
            else "finance.feestructure.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        entity = resolve_entity(request)
        structure = _resolve_fee_structure(entity, pk)
        return success_response(
            "Fee structure retrieved.",
            data=FeeStructureSerializer(structure, context={"with_usage": True}).data,
        )

    @transaction.atomic
    # Handle PATCH requests for this endpoint.
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


# Group endpoint behavior for Fee Structure Duplicate View.
class FeeStructureDuplicateView(_FinanceBase):
    """Clone a fee structure (code + lines) into a new **draft** structure.

    Body: ``{code, name?}`` - a new unique code is required; the clone copies
    applies_to, description and every line (incl. fee code / optional flag) and is
    created **inactive** so it can be reviewed before use.

    docstring-name: Fee structures
    """

    rbac_permission = "finance.feestructure.create"

    @transaction.atomic
    # Handle POST requests for this endpoint.
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


# Group endpoint behavior for Fee Structure Generate View.
class FeeStructureGenerateView(_FinanceBase):
    """POST - raise a posted invoice per customer from this fee structure.

    Body: ``{customers:[code|id, ...]}`` or ``{all_active:true}``; optional
    ``invoice_date``, ``due_date`` (ISO). Returns the invoices created.

    docstring-name: Generate invoices from a fee structure
    """

    rbac_permission = "finance.feestructure.generate"

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from .fees import generate_invoices

        entity = resolve_entity(request)
        structure = _resolve_fee_structure(entity, pk)
        if structure.applies_to != FeeAppliesTo.CUSTOMER:
            raise ValidationError({"applies_to":
                "Only customer fee structures can generate AR invoices."})
        body = request.data or {}
        from .document_settings import resolve_finance_document_settings
        policy = resolve_finance_document_settings(entity)
        invoice_date = _date(body.get("invoice_date"), "invoice_date") or datetime.date.today()
        due_date = _date(body.get("due_date"), "due_date")
        if due_date is None:
            due_date = invoice_date + datetime.timedelta(
                days=policy.default_invoice_due_days,
            )
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
            invoice_date=invoice_date,
            due_date=due_date,
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

# Group endpoint behavior for Credit Note List Create View.
class CreditNoteListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) credit or debit notes for an entity.

    docstring-name: Credit notes
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.creditnote.create" if self.request.method == "POST" \
            else "finance.creditnote.view"

    # Handle GET requests for this endpoint.
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
    # Handle POST requests for this endpoint.
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
                tax_code=_resolve_tax(
                    entity, ln.get("tax_code"), f"lines[{i}].tax_code",
                    usage="sales",
                ),
                cost_center=_resolve_cost_center(
                    entity, ln.get("cost_center"), f"lines[{i}].cost_center"),
            )
        price_credit_note(note)
        note.refresh_from_db()
        return success_response(
            f"{note.get_kind_display()} {note.document_number} created.",
            data=CreditNoteSerializer(note).data, status=201,
        )


# Define Credit Note Action Base values.
class _CreditNoteActionBase(_FinanceBase):
    # Support the note workflow.
    def _note(self, request, pk):
        entity = resolve_entity(request)
        note = CreditNote.objects.filter(entity=entity, pk=pk).first()
        if note is None:
            raise NotFound("Credit note not found for this entity.")
        return entity, note


# Group endpoint behavior for Credit Note Detail View.
class CreditNoteDetailView(_CreditNoteActionBase):
    """GET /finance/credit-notes/<id>/ - retrieve one credit or debit note (by id),
    with its lines and current allocation state.

    docstring-name: Credit notes
    """
    rbac_permission = "finance.creditnote.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, note = self._note(request, pk)
        return success_response(
            "Credit note retrieved.", data=CreditNoteSerializer(note).data,
        )


# Group endpoint behavior for Credit Note Post View.
class CreditNotePostView(_CreditNoteActionBase):
    """POST /finance/credit-notes/<id>/post/ - post a draft credit/debit note to the GL.

    Body ``{allocations:[{invoice, amount}]}`` for an explicit split, or
    ``{auto_allocate:true}`` (the default when no allocations are given) to apply a
    CREDIT note oldest-first against the customer's open invoices. A debit note raises
    the receivable; a credit note reduces it and settles/credits the invoices.

    docstring-name: Post a credit note
    """
    rbac_permission = "finance.creditnote.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from .approvals import approval_required
        from .credit_notes import post_credit_note

        entity, note = self._note(request, pk)
        # Same opt-in gate as refunds and write-offs.
        if approval_required(note):
            raise ValidationError({
                "detail": "This note is approval-gated; submit it for approval "
                          "instead of posting directly.",
            })
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


# Group endpoint behavior for Credit Note Allocate View.
class CreditNoteAllocateView(_CreditNoteActionBase):
    """POST /finance/credit-notes/<id>/allocate/ - apply an already-posted CREDIT note to
    the customer's open invoices. Body ``{allocations:[{invoice, amount}]}``; each amount
    is capped at the invoice balance and the note's unallocated remainder.

    docstring-name: Allocate a credit note
    """
    rbac_permission = "finance.creditnote.allocate"

    # Handle POST requests for this endpoint.
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



# Group endpoint behavior for Credit Note Submit View.
class CreditNoteSubmitView(_CreditNoteActionBase):
    """POST /finance/credit-notes/<id>/submit/ - submit a draft note for approval.

    Covers both directions: a credit note reduces a receivable, a debit note increases
    it, and one gate serves both because the risk is a mistaken note either way. The
    handler's ``validate_document`` runs the write-free guards now so a note with no
    lines, or a customer with no AR control, is refused before it reaches a queue.

    Only meaningful when a template exists for ``finance.credit_note`` at this note's
    scope; the seeded ladder gates it at or above the configured threshold.

    docstring-name: Submit a credit note for approval
    """
    rbac_permission = "finance.creditnote.submit"

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from vs_workflow.services import release as release_svc
        from vs_workflow.services.submission import submit_for_approval

        _, note = self._note(request, pk)
        instance = submit_for_approval(note, requested_by=request.user)
        note.refresh_from_db()
        return success_response(
            f"{note.get_kind_display()} note {note.document_number} submitted "
            f"for approval.",
            data=CreditNoteSerializer(note).data
            | {"approval": release_svc.approval_block(instance)},
        )


class CreditNoteVoidView(_CreditNoteActionBase):
    """POST /finance/credit-notes/<id>/void/ - void a posted credit/debit note."""

    rbac_permission = "finance.creditnote.reverse"

    def post(self, request, pk):
        from .voids import void_credit_note

        _, note = self._note(request, pk)
        void_credit_note(
            note, actor_user=request.user,
            date=_reversal_date(request.data),
        )
        note.refresh_from_db()
        return success_response(
            f"{note.get_kind_display()} {note.document_number} voided.",
            data=CreditNoteSerializer(note).data,
        )


# --------------------------------------------------------------------------- #
# Customer refunds                                                             #
# --------------------------------------------------------------------------- #

class RefundAvailabilityView(_FinanceBase):
    """GET customers with credit available for a new refund request.

    ``?as_of=YYYY-MM-DD`` measures availability on that accounting date rather than
    today - the refund date the user has picked. Credit that only arrives afterwards
    cannot fund a refund dated before it, so the picker must not offer it: without
    this the screen advertises credit the posting guard will refuse.

    docstring-name: Refund availability
    """

    rbac_permission = "finance.refund.create"

    def get(self, request):
        from .receivables import customer_refund_available_balances

        entity = resolve_entity(request)
        as_of = _date(request.query_params.get("as_of"), "as_of", required=False)
        qs = Customer.objects.filter(entity=entity, is_active=True)
        if (search := (request.query_params.get("search") or "").strip()):
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))

        customer_ids = list(qs.values_list("id", flat=True))
        available = customer_refund_available_balances(entity, customer_ids, as_of=as_of)
        qs = qs.filter(id__in=[
            customer_id for customer_id in customer_ids
            if available.get(customer_id, 0) > 0
        ]).order_by("code")

        paginator = XVSPagination()
        paginator.page_size = 25
        page = paginator.paginate_queryset(qs, request, view=self)
        rows = [{
            "customer_id": customer.pk,
            "customer_code": customer.code,
            "customer_name": customer.name,
            "refundable_credit": available[customer.pk],
            "refundable_credit_naira": format_naira(available[customer.pk]),
        } for customer in page]
        response = paginator.get_paginated_response(rows)
        response.data["as_of"] = as_of.isoformat() if as_of else None  # Echo the basis of the figures.
        return response


# Group endpoint behavior for Refund List Create View.
class RefundListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) customer refunds for an entity.

    docstring-name: Refunds
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.refund.create" if self.request.method == "POST" \
            else "finance.refund.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = Refund.objects.filter(entity=entity).select_related("customer")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (customer := request.query_params.get("customer")):
            qs = qs.filter(customer=_resolve_customer(entity, customer))
        return _paginate(
            request, qs.order_by("-refund_date", "-id"), RefundSerializer, self)

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        refund = _build_refund(entity, request.data or {}, actor_user=request.user)
        return success_response(
            f"Refund {refund.document_number} created.",
            data=RefundSerializer(refund).data, status=201,
        )


def _build_refund(entity, body, *, actor_user):
    """Build a valid draft refund for both single and batch creation paths."""
    from .receivables import customer_refund_available_balance

    customer = _resolve_customer(entity, body.get("customer"))
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    refund_date = _date(body.get("refund_date"), "refund_date", required=True)
    # Measure the credit on the refund's own date, so a doomed backdated draft is
    # refused at creation rather than surviving all the way to the posting guard.
    available = customer_refund_available_balance(customer, as_of=refund_date)
    amount = _validated_refund_amount(
        customer, body.get("amount", 0), available, as_of=refund_date)
    return Refund.objects.create(
        entity=entity,
        customer=customer,
        refund_date=refund_date,
        currency=_resolve_currency(body.get("currency")),
        method=body.get("method", "BANK_TRANSFER"),
        amount=amount,
        bank_account=_resolve_bank_account(
            entity, body.get("bank_account"), required=False),
        reference=body.get("reference", ""),
        narration=body.get("narration", ""),
        created_by=actor_user,
    )


def _validated_refund_amount(customer, raw_amount, available, *, as_of=None):
    """Apply the shared positive/available-credit boundary to a refund amount.

    ``as_of`` is the refund's accounting date and only shapes the message: when the
    credit exists but not yet on that date, saying so ("you have it today, just not
    on 1 Sep") is the difference between a fixable error and a baffling one.
    """
    amount = _money(raw_amount, "amount")
    if amount <= 0:
        raise ValidationError({"amount": "A refund amount must be greater than zero."})
    if amount > available:
        basis = f" as at {as_of}" if as_of else ""
        detail = ""
        if as_of is not None:  # Distinguish "no credit" from "not yet".
            from .receivables import customer_refund_available_balance

            today_available = customer_refund_available_balance(customer)
            if today_available > available:
                detail = (
                    f" {format_naira(today_available)} is available today - pick a "
                    f"later refund date to use it."
                )
        raise ValidationError({
            "amount": (
                f"Refund amount cannot exceed {customer.code}'s available credit"
                f"{basis} ({format_naira(available)}).{detail}"
            ),
        })
    return amount


# Define Refund Action Base values.
class _RefundActionBase(_FinanceBase):
    # Support the refund workflow.
    def _refund(self, request, pk):
        entity = resolve_entity(request)
        refund = Refund.objects.filter(entity=entity, pk=pk).first()
        if refund is None:
            raise NotFound("Refund not found for this entity.")
        return entity, refund


# Group endpoint behavior for Refund Detail View.
class RefundDetailView(_RefundActionBase):
    """GET /finance/refunds/<id>/ - retrieve one customer refund (by id).

    docstring-name: Refunds
    """
    rbac_permission = "finance.refund.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, refund = self._refund(request, pk)
        return success_response("Refund retrieved.", data=RefundSerializer(refund).data)


# Group endpoint behavior for Refund Submit View.
class RefundSubmitView(_RefundActionBase):
    """POST /finance/refunds/<id>/submit/ - submit a draft refund for approval.

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
    rbac_permission = "finance.refund.submit"

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from vs_workflow.services.submission import submit_for_approval

        from vs_workflow.services import release as release_svc

        _, refund = self._refund(request, pk)
        Customer.objects.select_for_update().get(pk=refund.customer_id)
        instance = submit_for_approval(refund, requested_by=request.user)
        refund.refresh_from_db()
        return success_response(
            f"Refund {refund.document_number} submitted for approval.",
            data=RefundSerializer(refund).data
            # Same contract as procurement and payouts: the client learns here that
            # nobody can approve this, and can offer to continue without approval.
            | {"approval": release_svc.approval_block(instance)},
        )


# Group endpoint behavior for Refund Post View.
class RefundPostView(_RefundActionBase):
    """POST /finance/refunds/<id>/post/ - post a draft refund, paying the customer's
    credit back out (Dr customer credit / Cr bank) and recording the GL journal.

    When a workflow template is published for this refund's ``finance.refund``
    document type (opt-in gate), direct posting is refused: the refund must go
    through ``/submit/`` and pays out only on approval. With no template, this
    behaves exactly as it always has.

    docstring-name: Post a refund
    """
    rbac_permission = "finance.refund.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from .approvals import approval_required
        from .credit_notes import post_refund

        _, refund = self._refund(request, pk)
        if approval_required(refund):
            raise ValidationError({
                "detail": "This refund is approval-gated; submit it for approval "
                          "instead of posting directly.",
            })
        post_refund(refund, actor_user=request.user)
        refund.refresh_from_db()
        return success_response(
            f"Refund {refund.document_number} posted.",
            data=RefundSerializer(refund).data,
        )


class RefundVoidView(_RefundActionBase):
    """POST /finance/refunds/<id>/void/ - void a posted customer refund."""

    rbac_permission = "finance.refund.reverse"

    def post(self, request, pk):
        from .voids import void_refund

        _, refund = self._refund(request, pk)
        void_refund(
            refund, actor_user=request.user,
            date=_reversal_date(request.data),
        )
        refund.refresh_from_db()
        return success_response(
            f"Refund {refund.document_number} voided.",
            data=RefundSerializer(refund).data,
        )


# --------------------------------------------------------------------------- #
# Bad-debt write-off                                                          #
# --------------------------------------------------------------------------- #

# Support the build write off request workflow.
def _build_write_off_request(entity, body, *, actor_user):
    """Create a DRAFT :class:`WriteOffRequest` from an API body (shared by the
    write-off-request create view and the invoice-write-off bridge).

    ``amount`` defaults to the invoice's outstanding balance when omitted. Resolves
    the invoice, optional write-off account and optional date within ``entity``.
    """
    invoice = _resolve_invoice(entity, body.get("invoice"))
    amount = _money(body["amount"], "amount") if body.get("amount") not in (None, "") \
        else invoice.balance_due
    return WriteOffRequest.objects.create(
        entity=entity, invoice=invoice, amount=amount,
        write_off_account=_resolve_account(
            entity, body.get("write_off_account"), "write_off_account"),
        write_off_date=_date(body.get("write_off_date"), "write_off_date"),
        narration=body.get("narration", ""),
        reason=body.get("reason", ""),
        created_by=actor_user,
    )


# Group endpoint behavior for Write Off Request List Create View.
class WriteOffRequestListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) bad-debt write-off requests for an entity.

    POST body: ``{invoice (doc-no|id, required), amount? (kobo; defaults to the
    invoice's outstanding balance), write_off_account? (code|id), write_off_date?
    (ISO), narration?, reason?}``. Creates a DRAFT request; the actual GL write-off
    runs later, on approval (when gated) or a direct ``/post/`` (when not).

    docstring-name: Write-off requests
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.writeoff.create" if self.request.method == "POST" \
            else "finance.writeoff.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = WriteOffRequest.objects.filter(entity=entity).select_related(
            "invoice", "invoice__customer")
        if (status_val := request.query_params.get("status")):
            qs = qs.filter(status=status_val)
        if (invoice := request.query_params.get("invoice")):
            qs = qs.filter(invoice=_resolve_invoice(entity, invoice))
        return _paginate(
            request, qs.order_by("-id"), WriteOffRequestSerializer, self)

    # Handle POST requests for this endpoint.
    def post(self, request):
        entity = resolve_entity(request)
        wor = _build_write_off_request(entity, request.data or {}, actor_user=request.user)
        return success_response(
            f"Write-off request {wor.document_number} created.",
            data=WriteOffRequestSerializer(wor).data, status=201,
        )


# Define Write Off Action Base values.
class _WriteOffActionBase(_FinanceBase):
    # Support the wor workflow.
    def _wor(self, request, pk):
        entity = resolve_entity(request)
        wor = WriteOffRequest.objects.filter(entity=entity, pk=pk).select_related(
            "invoice", "invoice__customer").first()
        if wor is None:
            raise NotFound("Write-off request not found for this entity.")
        return entity, wor


# Group endpoint behavior for Write Off Request Detail View.
class WriteOffRequestDetailView(_WriteOffActionBase):
    """GET /finance/write-offs/<id>/ - retrieve one bad-debt write-off request.

    docstring-name: Write-off requests
    """
    rbac_permission = "finance.writeoff.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, wor = self._wor(request, pk)
        return success_response(
            "Write-off request retrieved.", data=WriteOffRequestSerializer(wor).data)


# Group endpoint behavior for Write Off Request Submit View.
class WriteOffRequestSubmitView(_WriteOffActionBase):
    """POST /finance/write-offs/<id>/submit/ - submit a draft write-off for approval.

    Hands the request to ``vs_workflow``; the handler's ``validate_document`` runs the
    write-off preflight (invoice POSTED, outstanding balance, amount within balance)
    now, and moves the request to ``PENDING_APPROVAL``. The invoice is not touched
    until final approval fires the handler's ``on_approved`` write-off.

    docstring-name: Submit a write-off for approval
    """
    rbac_permission = "finance.writeoff.submit"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from vs_workflow.services.submission import submit_for_approval

        from vs_workflow.services import release as release_svc

        _, wor = self._wor(request, pk)
        instance = submit_for_approval(wor, requested_by=request.user)
        wor.refresh_from_db()
        return success_response(
            f"Write-off request {wor.document_number} submitted for approval.",
            data=WriteOffRequestSerializer(wor).data
            # Same contract as procurement and payouts: the client learns here that
            # nobody can approve this, and can offer to continue without approval.
            | {"approval": release_svc.approval_block(instance)},
        )


# Group endpoint behavior for Write Off Request Post View.
class WriteOffRequestPostView(_WriteOffActionBase):
    """POST /finance/write-offs/<id>/post/ - post a draft write-off request.

    When a workflow template is published for this request's ``finance.write_off``
    document type (opt-in gate), direct posting is refused: it must go through
    ``/submit/`` and writes off only on approval. With no template, this posts the
    bad-debt journal and clears the invoice immediately.

    docstring-name: Post a write-off request
    """
    rbac_permission = "finance.writeoff.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from .approvals import approval_required
        from .credit_notes import post_write_off_request

        _, wor = self._wor(request, pk)
        if approval_required(wor):
            raise ValidationError({
                "detail": "This write-off is approval-gated; submit it for approval instead.",
            })
        post_write_off_request(wor, actor_user=request.user)
        wor.refresh_from_db()
        return success_response(
            f"Write-off request {wor.document_number} posted.",
            data=WriteOffRequestSerializer(wor).data,
        )


# Group endpoint behavior for Invoice Write Off View.
class InvoiceWriteOffView(_FinanceBase):
    """POST /invoices/<pk>/write-off/ - write off an uncollectable balance as bad debt.

    Now routes through the first-class :class:`WriteOffRequest` document so the same
    entry point picks up approval gating transparently: it builds a DRAFT request from
    the body, then - if a ``finance.write_off`` template is published for this
    invoice's scope - submits it for approval and returns the request; otherwise it
    posts the write-off directly and returns the invoice **exactly as before**, so the
    ungated UX is unchanged.

    docstring-name: Write off an invoice
    """

    rbac_permission = "finance.invoice.writeoff"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from .approvals import approval_required
        from .credit_notes import post_write_off_request

        entity = resolve_entity(request)
        invoice = Invoice.objects.filter(entity=entity, pk=pk).first()
        if invoice is None:
            raise NotFound("Invoice not found for this entity.")

        body = dict(request.data or {})
        # The bridge resolves the invoice from the body; pin it to the URL's invoice.
        body["invoice"] = invoice.pk
        wor = _build_write_off_request(entity, body, actor_user=request.user)

        if approval_required(wor):
            from vs_workflow.services.submission import submit_for_approval

            submit_for_approval(wor, requested_by=request.user)
            wor.refresh_from_db()
            return success_response(
                f"Write-off request {wor.document_number} submitted for approval.",
                data=WriteOffRequestSerializer(wor).data,
            )

        post_write_off_request(wor, actor_user=request.user)
        invoice.refresh_from_db()
        return success_response(
            f"Invoice {invoice.document_number} written off.",
            data=InvoiceSerializer(invoice).data,
        )


# --------------------------------------------------------------------------- #
# Batch refunds and write-offs                                                #
# --------------------------------------------------------------------------- #

_BATCH_KIND_PERMISSIONS = {
    "REFUND": {
        "create": "finance.refund.create",
        "POST": "finance.refund.post",
        "SUBMIT": "finance.refund.submit",
    },
    "WRITEOFF": {
        "create": "finance.writeoff.create",
        "POST": "finance.writeoff.post",
        "SUBMIT": "finance.writeoff.submit",
    },
}
_BATCH_ACTIONS = {"DRAFT", "POST", "SUBMIT"}
_MAX_AR_BATCH_ITEMS = 100


def _normalise_batch_kind(value):
    kind = str(value or "").strip().upper().replace("-", "").replace("_", "")
    if kind not in _BATCH_KIND_PERMISSIONS:
        raise ValidationError({"kind": "Choose REFUND or WRITEOFF."})
    return kind


def _normalise_batch_action(value):
    action = str(value or "DRAFT").strip().upper()
    if action not in _BATCH_ACTIONS:
        raise ValidationError({"action": "Choose DRAFT, POST, or SUBMIT."})
    return action


def _require_batch_permissions(request, entity, kind, action):
    """Require create plus the exact lifecycle permission for this batch."""
    if is_vision_super_admin(request.user):
        return
    required = [_BATCH_KIND_PERMISSIONS[kind]["create"]]
    if action != "DRAFT":
        required.append(_BATCH_KIND_PERMISSIONS[kind][action])
    missing = [
        key for key in required
        if not user_has_rbac_permission(
            request.user,
            key,
            tenant=entity.tenant,
            branch=getattr(request, "branch", None),
        )
    ]
    if missing:
        raise PermissionDenied(
            "You do not have permission to create and advance this adjustment batch."
        )


def _batch_items(body):
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError({"items": "Add at least one batch line."})
    if len(items) > _MAX_AR_BATCH_ITEMS:
        raise ValidationError({
            "items": f"A batch can contain at most {_MAX_AR_BATCH_ITEMS} lines.",
        })
    if any(not isinstance(item, dict) for item in items):
        raise ValidationError({"items": "Every batch line must be an object."})
    return items


def _batch_customers(entity, items):
    """Resolve and lock a batch's customer refs with one scoped query."""
    refs = [str(item.get("customer") or "").strip() for item in items]
    if any(not ref for ref in refs):
        index = next(index for index, ref in enumerate(refs) if not ref)
        raise ValidationError({
            "items": {index: {"customer": "A customer is required."}},
        })
    codes = [ref.upper() for ref in refs]
    ids = [int(ref) for ref in refs if ref.isdigit()]
    customers = list(
        Customer.objects.select_for_update().filter(entity=entity).filter(
            Q(code__in=codes) | Q(pk__in=ids)
        )
    )
    by_code = {customer.code.upper(): customer for customer in customers}
    by_id = {customer.pk: customer for customer in customers}
    resolved = []
    for index, ref in enumerate(refs):
        customer = by_code.get(ref.upper())
        if customer is None and ref.isdigit():
            customer = by_id.get(int(ref))
        if customer is None:
            raise NotFound(
                f"No customer matches line {index + 1} for this entity."
            )
        resolved.append(customer)
    return resolved


def _batch_invoices(entity, items):
    """Resolve and lock a batch's invoice refs with one scoped related read."""
    refs = [str(item.get("invoice") or "").strip() for item in items]
    if any(not ref for ref in refs):
        index = next(index for index, ref in enumerate(refs) if not ref)
        raise ValidationError({
            "items": {index: {"invoice": "An invoice is required."}},
        })
    ids = [int(ref) for ref in refs if ref.isdigit()]
    numbers = [ref for ref in refs if not ref.isdigit()]
    invoices = list(
        Invoice.objects.select_for_update().select_related("customer")
        .filter(entity=entity)
        .filter(Q(pk__in=ids) | Q(document_number__in=numbers))
    )
    by_id = {invoice.pk: invoice for invoice in invoices}
    by_number = {invoice.document_number: invoice for invoice in invoices}
    resolved = []
    for index, ref in enumerate(refs):
        invoice = by_id.get(int(ref)) if ref.isdigit() else by_number.get(ref)
        if invoice is None:
            raise NotFound(
                f"No invoice matches line {index + 1} for this entity."
            )
        resolved.append(invoice)
    return resolved


class ARAdjustmentBatchView(_FinanceBase):
    """Create and optionally advance up to 100 refunds or write-offs atomically.

    Every line becomes the existing first-class Refund or WriteOffRequest document
    and goes through the same posting/workflow service as its single-item endpoint.
    If any line fails, the enclosing transaction rolls the whole batch back.
    """

    # The exact create + lifecycle pair is enforced below after the entity has been
    # resolved. This outer any-of gate prevents unrelated users from reaching the
    # endpoint while still allowing a useful 400 for malformed batch bodies.
    rbac_permission = [
        "finance.refund.create",
        "finance.refund.post",
        "finance.refund.submit",
        "finance.writeoff.create",
        "finance.writeoff.post",
        "finance.writeoff.submit",
    ]

    @transaction.atomic
    def post(self, request):
        from .approvals import approval_required
        from .credit_notes import post_refund, post_write_off_request
        from vs_workflow.services.submission import submit_for_approval

        entity = resolve_entity(request)
        body = request.data or {}
        kind = _normalise_batch_kind(body.get("kind"))
        action = _normalise_batch_action(body.get("action"))
        items = _batch_items(body)
        _require_batch_permissions(request, entity, kind, action)

        common_date = _date(body.get("date"), "date", required=True)
        narration = str(body.get("narration") or body.get("reason") or "").strip()
        seen_targets = set()
        documents = []

        if kind == "REFUND":
            from .receivables import customer_refund_available_balances

            bank_account = _resolve_bank_account(
                entity, body.get("bank_account"), required=True)
            customers = _batch_customers(entity, items)
            # The whole batch shares one accounting date, so availability is measured
            # on that date - not today. A batch dated before the credit arrived is
            # refused per line here rather than blowing up mid-loop in the posting
            # service and rolling the whole batch back with a cryptic 409.
            available = customer_refund_available_balances(
                entity, [customer.pk for customer in customers], as_of=common_date)
            for index, (item, customer) in enumerate(zip(items, customers)):
                if customer.pk in seen_targets:
                    raise ValidationError({
                        "items": {
                            index: {
                                "customer": "A customer may appear only once per batch.",
                            },
                        },
                    })
                seen_targets.add(customer.pk)
                try:
                    amount = _validated_refund_amount(
                        customer,
                        item.get("amount", 0),
                        available.get(customer.pk, 0),
                        as_of=common_date,
                    )
                except ValidationError as exc:  # Re-key onto the offending batch line.
                    raise ValidationError({"items": {index: exc.detail}}) from exc
                documents.append(Refund.objects.create(
                    entity=entity,
                    customer=customer,
                    refund_date=common_date,
                    method="BANK_TRANSFER",
                    amount=amount,
                    bank_account=bank_account,
                    reference=item.get("reference", ""),
                    narration=item.get("narration") or narration,
                    created_by=request.user,
                ))

            if action == "POST":
                if any(approval_required(document) for document in documents):
                    raise ValidationError({
                        "action": "One or more refunds are approval-gated; submit this "
                                  "batch for approval instead of posting it.",
                    })
                for refund in documents:
                    post_refund(refund, actor_user=request.user)
            elif action == "SUBMIT":
                for refund in documents:
                    submit_for_approval(refund, requested_by=request.user)
        else:
            write_off_account = _resolve_account(
                entity, body.get("write_off_account"), "write_off_account")
            invoices = _batch_invoices(entity, items)
            for index, (item, invoice) in enumerate(zip(items, invoices)):
                if invoice.pk in seen_targets:
                    raise ValidationError({
                        "items": {
                            index: {
                                "invoice": "An invoice may appear only once per batch.",
                            },
                        },
                    })
                seen_targets.add(invoice.pk)
                amount = (
                    _money(item["amount"], "amount")
                    if item.get("amount") not in (None, "")
                    else invoice.balance_due
                )
                if invoice.status != DocumentStatus.POSTED:
                    raise ValidationError({
                        "items": {
                            index: {
                                "invoice": "Only posted invoices can be written off.",
                            },
                        },
                    })
                if common_date < invoice.invoice_date:  # A debt cannot be conceded before it is owed.
                    raise ValidationError({
                        "items": {
                            index: {
                                "invoice": (
                                    f"{invoice.document_number} is dated "
                                    f"{invoice.invoice_date}; it cannot be written off "
                                    f"on {common_date}. Date the batch "
                                    f"{invoice.invoice_date} or later."
                                ),
                            },
                        },
                    })
                if amount <= 0 or amount > invoice.balance_due:
                    raise ValidationError({
                        "items": {
                            index: {
                                "amount": "Amount must be greater than zero and no more "
                                          "than the invoice balance.",
                            },
                        },
                    })
                documents.append(WriteOffRequest.objects.create(
                    entity=entity,
                    invoice=invoice,
                    amount=amount,
                    write_off_account=write_off_account,
                    write_off_date=common_date,
                    narration=item.get("narration") or narration,
                    reason=item.get("reason") or narration,
                    created_by=request.user,
                ))

            if action == "POST":
                if any(approval_required(document) for document in documents):
                    raise ValidationError({
                        "action": "One or more write-offs are approval-gated; submit "
                                  "this batch for approval instead of posting it.",
                    })
                for write_off in documents:
                    post_write_off_request(write_off, actor_user=request.user)
            elif action == "SUBMIT":
                for write_off in documents:
                    submit_for_approval(write_off, requested_by=request.user)

        for document in documents:
            document.refresh_from_db()
        payload = (
            RefundSerializer(documents, many=True).data
            if kind == "REFUND"
            else WriteOffRequestSerializer(documents, many=True).data
        )
        total_amount = sum(document.amount for document in documents)
        verb = {"DRAFT": "created", "POST": "posted", "SUBMIT": "submitted"}[action]
        return success_response(
            f"{len(documents)} {kind.lower()} adjustment(s) {verb}.",
            data={
                "kind": kind,
                "action": action,
                "count": len(documents),
                "total_amount": total_amount,
                "items": payload,
            },
            status=201,
        )


# Support the writeoff rows workflow.
def _writeoff_rows(entity, *, limit=1000):
    """Normalised bad-debt write-off rows, from two disjoint sources.

    * POSTED write-offs come from the finance audit log (``INVOICE_WRITTEN_OFF``
      SUCCESS). This covers legacy bare-invoice write-offs *and* posted
      ``WriteOffRequest`` documents - posting one runs ``write_off_invoice``,
      which writes that log. Always reported "POSTED".
    * Non-posted ``WriteOffRequest`` documents (DRAFT / PENDING_APPROVAL /
      APPROVED) come from the table itself, carrying their real status and
      ``write_off_id`` so the UI can submit / post them. The audit log never
      captures these, so without this they are invisible on the screen.

    The two sources are disjoint (posted ⇒ audit log; non-posted ⇒ table), so no
    write-off is double-counted.
    """
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
            "customer_name": l.metadata.get("customer_name") or (inv.customer.name if inv else "-"),
            "reason": l.metadata.get("narration") or "Bad-debt write-off",
            "amount": int(l.metadata.get("amount") or 0), "amount_naira": format_naira(int(l.metadata.get("amount") or 0)),
            "status": "POSTED", "refund_id": None, "write_off_id": None,
        })

    # Non-posted write-off requests (drafts + awaiting approval). The audit log
    # only records POSTED write-offs, so these would otherwise never surface.
    for w in (WriteOffRequest.objects
              .filter(entity=entity).exclude(status=DocumentStatus.POSTED)
              .select_related("invoice", "invoice__customer")
              .order_by("-id")[:limit]):
        wo_date = w.write_off_date or w.created_at.date()
        rows.append({
            "key": f"WR{w.id}", "kind": "WRITEOFF", "reference": w.document_number,
            "date": wo_date.isoformat(),
            "customer_code": w.invoice.customer.code, "customer_name": w.invoice.customer.name,
            "reason": w.reason or w.narration or "Bad-debt write-off",
            "amount": w.amount, "amount_naira": format_naira(w.amount),
            "status": w.status, "refund_id": None, "write_off_id": w.id,
        })
    return rows


# Group endpoint behavior for A R Adjustment List View.
class ARAdjustmentListView(_FinanceBase):
    """GET /finance/ar-adjustments/ - unified customer refunds + bad-debt write-offs.

    Filters: ``?type=(refund|writeoff)`` and ``?search=``. The merged list is sorted
    by date and paginated; KPI totals (written-off YTD, pending refund count) ride
    in the response so they stay accurate across pages.

    docstring-name: Refunds & write-offs
    """

    rbac_permission = "finance.refund.view"

    # Handle GET requests for this endpoint.
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

        # KPI totals - from the full sets, independent of the type filter / page.
        year = timezone.now().year
        # "Written off YTD" is money actually written off → POSTED rows only
        # (writeoff_rows now also carries non-posted requests).
        written_off_ytd = sum(
            w["amount"] for w in writeoff_rows
            if w["status"] == "POSTED" and w["date"][:4] == str(year))
        # "Pending" spans both adjustment kinds awaiting posting/approval.
        pending = (
            Refund.objects.filter(entity=entity).exclude(status=DocumentStatus.POSTED).count()
            + WriteOffRequest.objects.filter(entity=entity).exclude(status=DocumentStatus.POSTED).count()
        )
        from .receivables import customer_refund_available_balances
        active_customer_ids = Customer.objects.filter(
            entity=entity, is_active=True).values_list("id", flat=True)
        refundable_credit = sum(
            customer_refund_available_balances(entity, active_customer_ids).values())

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
            "kpis": {
                "written_off_ytd": written_off_ytd,
                "pending": pending,
                "refundable_credit": refundable_credit,
            },
            "data": rows[start:start + page_size],
        })


# Group endpoint behavior for Invoice Pay View.
class InvoicePayView(_FinanceBase):
    """POST /invoices/<pk>/pay/ - record a customer receipt and settle this invoice.

    Body: ``{amount(kobo), payment_date, method?, deposit_account, reference?,
    narration?}``. Posts the receipt (Dr bank/cash, Cr AR) and allocates it to this
    invoice; any excess remains as unallocated credit on the customer.

    docstring-name: Record a payment
    """

    rbac_permission = "finance.payment.create"

    @transaction.atomic
    # Handle POST requests for this endpoint.
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


# Group endpoint behavior for Invoice Remind View.
class InvoiceRemindView(_FinanceBase):
    """POST /invoices/<pk>/remind/ - raise & send a dunning reminder for this invoice.

    docstring-name: Send an invoice reminder
    """

    rbac_permission = "finance.dunning.send"

    # Handle POST requests for this endpoint.
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
# Concessions - discounts / waivers / scholarships                            #
# --------------------------------------------------------------------------- #

# Group endpoint behavior for Concession List Create View.
class ConcessionListCreateView(_FinanceBase):
    """GET (list) / POST (create draft) concessions for an entity.

    docstring-name: Concessions
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.concession.create" if self.request.method == "POST" \
            else "finance.concession.view"

    # Handle GET requests for this endpoint.
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

    # Handle POST requests for this endpoint.
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


# Define Concession Action Base values.
class _ConcessionActionBase(_FinanceBase):
    # Support the concession workflow.
    def _concession(self, request, pk):
        entity = resolve_entity(request)
        concession = Concession.objects.filter(entity=entity, pk=pk).first()
        if concession is None:
            raise NotFound("Concession not found for this entity.")
        return entity, concession


# Group endpoint behavior for Concession Detail View.
class ConcessionDetailView(_ConcessionActionBase):
    """GET /finance/concessions/<id>/ - retrieve one concession (discount / waiver /
    scholarship) by id.

    docstring-name: Concessions
    """
    rbac_permission = "finance.concession.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, concession = self._concession(request, pk)
        return success_response(
            "Concession retrieved.", data=ConcessionSerializer(concession).data,
        )


# Group endpoint behavior for Concession Post View.
class ConcessionPostView(_ConcessionActionBase):
    """POST /finance/concessions/<id>/post/ - post a draft concession, writing the
    discount/waiver/scholarship off against the allowance account (Dr allowance / Cr AR)
    so it reduces the linked invoice's balance and the customer's outstanding.

    docstring-name: Post a concession
    """
    rbac_permission = "finance.concession.post"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from .approvals import approval_required
        from .installments import post_concession

        _, concession = self._concession(request, pk)
        # Same opt-in gate as refunds and write-offs: with a template published for
        # this concession's scope, the only route to the ledger is through approval.
        if approval_required(concession):
            raise ValidationError({
                "detail": "This concession is approval-gated; submit it for approval "
                          "instead of posting directly.",
            })
        post_concession(concession, actor_user=request.user)
        concession.refresh_from_db()
        return success_response(
            f"{concession.get_kind_display()} {concession.document_number} posted.",
            data=ConcessionSerializer(concession).data,
        )



# Group endpoint behavior for Concession Submit View.
class ConcessionSubmitView(_ConcessionActionBase):
    """POST /finance/concessions/<id>/submit/ - submit a draft concession for approval.

    A concession forgives revenue, so above the seeded threshold it needs a second
    person exactly as a refund does. The handler's ``validate_document`` runs the
    concession preflight now - posted invoice, not backdated before it, a positive
    amount within the outstanding balance - so a doomed waiver is refused before it
    reaches an approver's queue, and the GL is untouched until final approval.

    Only meaningful when a template exists for ``finance.concession`` at this
    concession's scope; the seeded ladder gates it at or above the configured
    threshold and lets smaller allowances post directly.

    docstring-name: Submit a concession for approval
    """
    rbac_permission = "finance.concession.submit"

    @transaction.atomic
    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from vs_workflow.services import release as release_svc
        from vs_workflow.services.submission import submit_for_approval

        _, concession = self._concession(request, pk)
        instance = submit_for_approval(concession, requested_by=request.user)
        concession.refresh_from_db()
        return success_response(
            f"{concession.get_kind_display()} {concession.document_number} "
            f"submitted for approval.",
            data=ConcessionSerializer(concession).data
            # Same contract as refunds, procurement and payouts: the client learns
            # here that nobody can approve this, and can offer to continue.
            | {"approval": release_svc.approval_block(instance)},
        )


class ConcessionVoidView(_ConcessionActionBase):
    """POST /finance/concessions/<id>/void/ - void a posted concession."""

    rbac_permission = "finance.concession.reverse"

    def post(self, request, pk):
        from .voids import void_concession

        _, concession = self._concession(request, pk)
        void_concession(
            concession, actor_user=request.user,
            date=_reversal_date(request.data),
        )
        concession.refresh_from_db()
        return success_response(
            f"{concession.get_kind_display()} {concession.document_number} voided.",
            data=ConcessionSerializer(concession).data,
        )


# Group endpoint behavior for Concession Summary View.
class ConcessionSummaryView(_FinanceBase):
    """GET /finance/concessions/summary/ - KPI totals (kobo) for the header cards.

    docstring-name: Concession summary
    """

    rbac_permission = "finance.concession.view"

    # Handle GET requests for this endpoint.
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

# Group endpoint behavior for Payment Plan List Create View.
class PaymentPlanListCreateView(_FinanceBase):
    """GET (list) / POST (create draft + build schedule) payment plans for an entity.

    docstring-name: Payment plans
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.paymentplan.create" if self.request.method == "POST" \
            else "finance.paymentplan.view"

    # Handle GET requests for this endpoint.
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
    # Handle POST requests for this endpoint.
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


# Define Payment Plan Action Base values.
class _PaymentPlanActionBase(_FinanceBase):
    # Support the plan workflow.
    def _plan(self, request, pk):
        entity = resolve_entity(request)
        plan = PaymentPlan.objects.filter(entity=entity, pk=pk).first()
        if plan is None:
            raise NotFound("Payment plan not found for this entity.")
        return entity, plan


# Group endpoint behavior for Payment Plan Detail View.
class PaymentPlanDetailView(_PaymentPlanActionBase):
    """GET /finance/payment-plans/<id>/ - retrieve one installment payment plan (by id),
    including its scheduled installments and progress.

    docstring-name: Payment plans
    """
    rbac_permission = "finance.paymentplan.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, plan = self._plan(request, pk)
        return success_response("Payment plan retrieved.", data=PaymentPlanSerializer(plan).data)


# Group endpoint behavior for Payment Plan Activate View.
class PaymentPlanActivateView(_PaymentPlanActionBase):
    """POST /finance/payment-plans/<id>/activate/ - move a draft plan into ACTIVE so its
    installment schedule becomes live and can be tracked against customer receipts.

    docstring-name: Activate a payment plan
    """
    rbac_permission = "finance.paymentplan.activate"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from .installments import activate_payment_plan

        _, plan = self._plan(request, pk)
        activate_payment_plan(plan, actor_user=request.user)
        plan.refresh_from_db()
        return success_response(
            f"Payment plan {plan.document_number} activated.",
            data=PaymentPlanSerializer(plan).data,
        )


# Group endpoint behavior for Payment Plan Refresh View.
class PaymentPlanRefreshView(_PaymentPlanActionBase):
    """POST /finance/payment-plans/<id>/refresh/ - recompute the plan's progress, marking
    installments paid and advancing plan status. Body may carry a ``settled_amount``
    (kobo) to apply against the schedule; omit it to just re-derive from what's settled.

    docstring-name: Refresh payment plan status
    """
    rbac_permission = "finance.paymentplan.activate"

    # Handle POST requests for this endpoint.
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


# Group endpoint behavior for Payment Plan Cancel View.
class PaymentPlanCancelView(_PaymentPlanActionBase):
    """POST /finance/payment-plans/<id>/cancel/ - cancel a plan, closing out its remaining
    installments so it no longer tracks against the customer's balance.

    docstring-name: Cancel a payment plan
    """
    rbac_permission = "finance.paymentplan.cancel"

    # Handle POST requests for this endpoint.
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

# Group endpoint behavior for Customer Statement View.
class CustomerStatementView(_FinanceBase):
    """A dated statement of account for one customer (``?customer=<code|id>``).

    Optional ``?start=`` / ``?end=`` ISO dates bound the period (``end`` defaults to
    today; an absent ``start`` runs from inception with a zero opening balance).
    Supports ``?export=csv|xlsx|pdf``. All money is reported in kobo + naira.

    docstring-name: Customer statement
    """

    rbac_permission = "finance.report.view"

    # Handle GET requests for this endpoint.
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
            title=f"Statement of Account - {stmt.customer_name}",
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
# Dunning - policies, stages and automated reminder notices                   #
# --------------------------------------------------------------------------- #

# Support the normalize channels workflow.
def _normalize_channels(raw):
    """Coerce a stage channel input (CSV string or list) into a normalised CSV of
    valid DunningChannel values, in enum order, deduped; defaults to EMAIL."""
    from .constants import DunningChannel

    parts = ([str(x).strip().upper() for x in raw] if isinstance(raw, (list, tuple))
             else [p.strip().upper() for p in str(raw or "").split(",")])
    chosen = [c for c in DunningChannel.values if c in parts]
    return ",".join(chosen) if chosen else DunningChannel.EMAIL


# Group endpoint behavior for Dunning Policy List Create View.
class DunningPolicyListCreateView(_FinanceBase):
    """GET (list) dunning policies, or POST to create one (optionally with stages).

    docstring-name: Dunning policies
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.dunning.manage" if self.request.method == "POST" \
            else "finance.dunning.view"

    # Handle GET requests for this endpoint.
    def get(self, request):
        entity = resolve_entity(request)
        qs = DunningPolicy.objects.filter(entity=entity).prefetch_related("stages")
        return success_response(
            "Dunning policies retrieved.",
            data=DunningPolicySerializer(qs.order_by("name"), many=True).data,
        )

    @transaction.atomic
    # Handle POST requests for this endpoint.
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


# Group endpoint behavior for Dunning Policy Detail View.
class DunningPolicyDetailView(_FinanceBase):
    """GET / PATCH one dunning policy (by id). PATCH updates name / active / default and,
    if ``stages`` is given, replaces the whole reminder ladder.

    docstring-name: Dunning policies
    """

    @property
    # Handle the rbac permission workflow.
    def rbac_permission(self):
        return "finance.dunning.manage" if self.request.method == "PATCH" \
            else "finance.dunning.view"

    # Support the policy workflow.
    def _policy(self, request, pk):
        policy = DunningPolicy.objects.filter(entity=resolve_entity(request), pk=pk).first()
        if policy is None:
            raise NotFound("Dunning policy not found for this entity.")
        return policy

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        return success_response(
            "Dunning policy retrieved.", data=DunningPolicySerializer(self._policy(request, pk)).data,
        )

    @transaction.atomic
    # Handle PATCH requests for this endpoint.
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


# Group endpoint behavior for Dunning Generate View.
class DunningGenerateView(_FinanceBase):
    """POST: run a dunning policy over the entity's overdue invoices, raising notices.

    docstring-name: Generate dunning notices
    """

    rbac_permission = "finance.dunning.generate"

    # Handle POST requests for this endpoint.
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


# Group endpoint behavior for Dunning Summary View.
class DunningSummaryView(_FinanceBase):
    """GET /finance/dunning/summary/ - open-receivable aging buckets for the header.

    ``due_soon`` is the next 7 days (not yet overdue); the rest are days past due.

    docstring-name: Dunning summary
    """

    rbac_permission = "finance.dunning.view"

    # Handle GET requests for this endpoint.
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


# Group endpoint behavior for Dunning Notice List Create View.
class DunningNoticeListCreateView(_FinanceBase):
    """GET dunning notices for an entity (filterable by status / customer / invoice).

    docstring-name: Dunning notices
    """

    rbac_permission = "finance.dunning.view"

    # Handle GET requests for this endpoint.
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


# Define Dunning Notice Action Base values.
class _DunningNoticeActionBase(_FinanceBase):
    # Support the notice workflow.
    def _notice(self, request, pk):
        entity = resolve_entity(request)
        notice = DunningNotice.objects.filter(entity=entity, pk=pk).first()
        if notice is None:
            raise NotFound("Dunning notice not found for this entity.")
        return entity, notice


# Group endpoint behavior for Dunning Notice Detail View.
class DunningNoticeDetailView(_DunningNoticeActionBase):
    """GET /finance/dunning-notices/<id>/ - retrieve one dunning (reminder) notice by id.

    docstring-name: Dunning notices
    """
    rbac_permission = "finance.dunning.view"

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        _, notice = self._notice(request, pk)
        return success_response(
            "Dunning notice retrieved.", data=DunningNoticeSerializer(notice).data,
        )


# Group endpoint behavior for Dunning Notice Send View.
class DunningNoticeSendView(_DunningNoticeActionBase):
    """POST /finance/dunning-notices/<id>/send/ - dispatch a pending notice over its
    stage's channels (in-app + email) and mark it SENT.

    docstring-name: Send a dunning notice
    """
    rbac_permission = "finance.dunning.send"

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from .dunning import mark_notice_sent

        _, notice = self._notice(request, pk)
        mark_notice_sent(notice, actor_user=request.user)
        notice.refresh_from_db()
        return success_response(
            f"Dunning notice {notice.document_number} marked sent.",
            data=DunningNoticeSerializer(notice).data,
        )


# Group endpoint behavior for Dunning Notice Cancel View.
class DunningNoticeCancelView(_DunningNoticeActionBase):
    """POST /finance/dunning-notices/<id>/cancel/ - cancel a notice before it goes out,
    recording an optional ``reason``.

    docstring-name: Cancel a dunning notice
    """
    rbac_permission = "finance.dunning.send"

    # Handle POST requests for this endpoint.
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
