"""Finance datasets published to the Export Centre.

Registered from :meth:`vs_finance.apps.VsFinanceConfig.ready`, so the Export Centre
never imports this app and this app never imports the Export Centre's views. All three
are :data:`~vs_exports.constants.DatasetScope.ENTITY`-scoped: a finance row belongs to
a set of books, and one file spanning two entities would be an accounting error.

Money columns are integer kobo in the database and are rendered by
:func:`vs_exports.catalogue.render_value` - never formatted here.
"""
from __future__ import annotations

from vs_exports.catalogue import (
    FILTER_BOOLEAN,
    FILTER_CHOICE,
    FILTER_DATE_RANGE,
    FILTER_NUMBER_RANGE,
    FILTER_SEARCH,
    FILTER_TEXT,
    KIND_CHOICE,
    KIND_DATE,
    KIND_DATETIME,
    KIND_MONEY,
    KIND_NUMBER,
    KIND_TEXT,
    Dataset,
    Field,
    FilterDef,
    choice_labels,
    register,
)


# Build the entity-scoped base queryset for customer invoices.
def _invoices(scope):
    from .models import Invoice

    return Invoice.objects.filter(entity=scope.entity)


# Build the entity-scoped base queryset for invoice lines.
def _invoice_lines(scope):
    from .models import InvoiceLine

    return InvoiceLine.objects.filter(invoice__entity=scope.entity)


# Build the entity-scoped base queryset for journal lines.
def _gl_postings(scope):
    from .models import JournalLine

    return JournalLine.objects.filter(entry__entity=scope.entity)


# Build the entity-scoped base queryset for customer receipts.
def _payments(scope):
    from .models import Payment

    return Payment.objects.filter(entity=scope.entity)


# Build the entity-scoped base queryset for staff expense claims.
def _expense_claims(scope):
    from .models import ExpenseClaim

    return ExpenseClaim.objects.filter(entity=scope.entity)


# Build the entity-scoped base queryset for the AR customer master.
def _customers(scope):
    from .models import Customer

    return Customer.objects.filter(entity=scope.entity)


_DOC_STATUS = choice_labels("vs_finance.constants.DocumentStatus")
_PAY_STATUS = choice_labels("vs_finance.constants.InvoicePaymentStatus")
_JOURNAL_SOURCE = choice_labels("vs_finance.constants.JournalSource")


# Register every finance dataset. Called once from AppConfig.ready().
def register_datasets():
    register(Dataset(
        key="finance.customer_invoices",
        module="Finance",
        name="Customer invoices",
        description=(
            "One row per invoice raised, with customer, dates, status and totals. "
            "Updated continuously. Three customer fields are restricted."
        ),
        base=_invoices,
        permission="finance.invoice.view",
        row_cap=200_000,
        default_columns=(
            "document_number", "customer_name", "invoice_date", "due_date",
            "status", "total",
        ),
        fields=(
            Field("document_number", "Invoice number", "Invoice", KIND_TEXT, locked=True,
                  description="The invoice's document number - the row's identity."),
            Field("customer_name", "Customer", "Invoice", KIND_TEXT, source="customer__name"),
            Field("customer_code", "Customer code", "Invoice", KIND_TEXT, source="customer__code"),
            Field("invoice_date", "Invoice date", "Invoice", KIND_DATE),
            Field("due_date", "Due date", "Invoice", KIND_DATE),
            Field("status", "Status", "Invoice", KIND_CHOICE, choices=_DOC_STATUS),
            Field("payment_status", "Payment status", "Invoice", KIND_CHOICE, choices=_PAY_STATUS),
            Field("subtotal", "Subtotal", "Amounts", KIND_MONEY),
            Field("tax_total", "Tax", "Amounts", KIND_MONEY),
            Field("total", "Total", "Amounts", KIND_MONEY),
            Field("amount_paid", "Amount paid", "Amounts", KIND_MONEY),
            Field("amount_credited", "Amount credited", "Amounts", KIND_MONEY),
            Field("currency", "Currency", "Amounts", KIND_TEXT, source="currency__code"),
            Field("reference", "Reference", "Invoice", KIND_TEXT),
            Field("narration", "Narration", "Invoice", KIND_TEXT),
            Field("customer_tax_id", "Customer tax ID", "Customer", KIND_TEXT,
                  source="customer__source_id", sensitive=True,
                  description="Restricted: the customer's external reference."),
            Field("customer_email", "Billing contact email", "Customer", KIND_TEXT,
                  source="customer__billing_email", sensitive=True,
                  description="Restricted: personal contact data."),
            Field("customer_phone", "Billing phone", "Customer", KIND_TEXT,
                  source="customer__billing_phone", sensitive=True,
                  description="Restricted: personal contact data."),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("invoice_date", "Invoice date", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True,
                      description="Required so an export can never mean 'every invoice ever'."),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_DOC_STATUS),
            FilterDef("payment_status", "Payment status", FILTER_CHOICE, choices=_PAY_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("document_number", "Invoice number"),
                ("customer__name", "Customer"),
                ("customer__code", "Customer code"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("due_date", "Due date", FILTER_DATE_RANGE,
                      description="Used by the invoice screen's Overdue tab."),
            FilterDef("customer", "Customer", FILTER_TEXT, source="customer__name"),
            FilterDef("customer_code", "Customer code", FILTER_TEXT,
                      source="customer__code"),
            FilterDef("total", "Total", FILTER_NUMBER_RANGE,
                      description="Amounts are in kobo."),
        ),
    ))

    register(Dataset(
        key="finance.invoice_lines",
        module="Finance",
        name="Invoice lines",
        description=(
            "One row per line on an invoice. Use this when you need item-level detail; "
            "expect roughly four times as many rows."
        ),
        base=_invoice_lines,
        permission="finance.invoice.view",
        row_cap=500_000,
        default_columns=("invoice_number", "description", "quantity", "net_amount", "tax_amount"),
        fields=(
            Field("invoice_number", "Invoice number", "Invoice", KIND_TEXT,
                  source="invoice__document_number", locked=True),
            Field("invoice_date", "Invoice date", "Invoice", KIND_DATE, source="invoice__invoice_date"),
            Field("customer_name", "Customer", "Invoice", KIND_TEXT, source="invoice__customer__name"),
            Field("description", "Description", "Line", KIND_TEXT),
            Field("quantity", "Quantity", "Line", KIND_NUMBER),
            Field("unit_price", "Unit price", "Line", KIND_MONEY),
            Field("net_amount", "Net amount", "Line", KIND_MONEY),
            Field("tax_amount", "Tax amount", "Line", KIND_MONEY),
            Field("revenue_account", "Revenue account", "Line", KIND_TEXT, source="revenue_account__code"),
            Field("tax_code", "Tax code", "Line", KIND_TEXT, source="tax_code__code"),
        ),
        filters=(
            FilterDef("invoice_date", "Invoice date", FILTER_DATE_RANGE, required=True,
                      source="invoice__invoice_date", is_primary_date=True),
            FilterDef("status", "Invoice status", FILTER_CHOICE, source="invoice__status",
                      choices=_DOC_STATUS),
        ),
    ))

    register(Dataset(
        key="finance.gl_postings",
        module="Finance",
        name="General ledger postings",
        description=(
            "Journal entries for the period, one row per line. The raw material for a "
            "trial balance or an auditor's sample."
        ),
        base=_gl_postings,
        permission="finance.journal.view",
        row_cap=500_000,
        # Advisory, not a hard stop: a wider range warns in the estimate rather
        # than failing the run, and the row cap is the real ceiling.
        max_date_span_days=31,
        default_columns=("entry_number", "entry_date", "account_code", "debit", "credit"),
        fields=(
            Field("entry_number", "Entry number", "Journal", KIND_TEXT,
                  source="entry__document_number", locked=True),
            Field("entry_date", "Entry date", "Journal", KIND_DATE, source="entry__date"),
            Field("entry_status", "Entry status", "Journal", KIND_CHOICE,
                  source="entry__status", choices=_DOC_STATUS),
            Field("line_no", "Line", "Line", KIND_NUMBER),
            Field("account_code", "Account code", "Line", KIND_TEXT, source="account__code"),
            Field("account_name", "Account", "Line", KIND_TEXT, source="account__name"),
            Field("debit", "Debit", "Amounts", KIND_MONEY),
            Field("credit", "Credit", "Amounts", KIND_MONEY),
            Field("description", "Description", "Line", KIND_TEXT),
            Field("cost_center", "Cost centre", "Analysis", KIND_TEXT, source="cost_center__code"),
            Field("entry_source", "Source", "Journal", KIND_CHOICE, source="entry__source",
                  choices=_JOURNAL_SOURCE,
                  description="What raised the entry - manual, or a posting module."),
        ),
        filters=(
            FilterDef("entry_date", "Entry date", FILTER_DATE_RANGE, required=True,
                      source="entry__date", is_primary_date=True),
            FilterDef("entry_status", "Entry status", FILTER_CHOICE, source="entry__status",
                      choices=_DOC_STATUS),
            # The ledger screen filters by source, so the export has to be able to
            # as well: without it every quick export off that screen would be
            # wider than the table and say so, which is honest but not useful.
            FilterDef("entry_source", "Source", FILTER_CHOICE, source="entry__source",
                      choices=_JOURNAL_SOURCE),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("entry__document_number", "Entry number"),
                ("entry__narration", "Narration"),
                ("entry__reference", "Reference"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("account", "Account code", FILTER_TEXT, source="account__code"),
        ),
    ))

    register(Dataset(
        key="finance.customer_receipts",
        module="Finance",
        name="Customer receipts",
        description=(
            "Money received from customers, one row per receipt, with the bank account "
            "it landed in. The cash-in side of the invoices dataset."
        ),
        base=_payments,
        permission="finance.payment.view",
        row_cap=200_000,
        default_columns=("document_number", "customer_name", "payment_date", "amount"),
        fields=(
            Field("document_number", "Receipt number", "Receipt", KIND_TEXT, locked=True),
            Field("customer_name", "Customer", "Receipt", KIND_TEXT, source="customer__name"),
            Field("payment_date", "Received on", "Receipt", KIND_DATE),
            Field("amount", "Amount", "Amounts", KIND_MONEY),
            Field("allocated_amount", "Allocated", "Amounts", KIND_MONEY,
                  description="Cash already matched to invoices."),
            Field("refunded_amount", "Refunded", "Amounts", KIND_MONEY),
            Field("status", "Status", "Receipt", KIND_CHOICE, choices=_DOC_STATUS),
            Field("method", "Method", "Receipt", KIND_TEXT),
            Field("reference", "Reference", "Receipt", KIND_TEXT),
            Field("deposit_account", "Deposit account", "Receipt", KIND_TEXT,
                  source="deposit_account__code"),
            Field("narration", "Narration", "Receipt", KIND_TEXT),
        ),
        filters=(
            FilterDef("payment_date", "Received on", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_DOC_STATUS),
            FilterDef("customer", "Customer", FILTER_TEXT, source="customer__name"),
            FilterDef("method", "Method", FILTER_TEXT),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("document_number", "Receipt number"),
                ("customer__name", "Customer"),
                ("reference", "Reference"),
            ), description="Matches any one of these, the way the search box does."),
        ),
    ))

    register(Dataset(
        key="finance.expense_claims",
        module="Finance",
        name="Expense claims",
        description=(
            "Staff reimbursement claims, one row per claim, with what has been paid "
            "back so far. The answer to 'what do we still owe our own people'."
        ),
        base=_expense_claims,
        permission="finance.expenseclaim.view",
        row_cap=200_000,
        default_columns=("document_number", "claimant_name", "claim_date", "total", "status"),
        fields=(
            Field("document_number", "Claim number", "Claim", KIND_TEXT, locked=True),
            Field("claimant_name", "Claimant", "Claim", KIND_TEXT),
            Field("claim_date", "Claim date", "Claim", KIND_DATE),
            Field("title", "Purpose", "Claim", KIND_TEXT),
            Field("narration", "Narration", "Claim", KIND_TEXT),
            Field("status", "Status", "Claim", KIND_CHOICE, choices=_DOC_STATUS),
            Field("payment_status", "Reimbursement", "Claim", KIND_CHOICE,
                  choices=_PAY_STATUS),
            Field("subtotal", "Net", "Amounts", KIND_MONEY),
            Field("tax_total", "Recoverable tax", "Amounts", KIND_MONEY),
            Field("total", "Total", "Amounts", KIND_MONEY),
            Field("amount_paid", "Reimbursed", "Amounts", KIND_MONEY),
            Field("currency", "Currency", "Amounts", KIND_TEXT, source="currency__code"),
            Field("reimbursement_account", "Liability account", "Claim", KIND_TEXT,
                  source="reimbursement_account__code"),
            Field("journal_number", "Journal", "Record", KIND_TEXT,
                  source="journal__document_number"),
            Field("claimant_email", "Claimant email", "Claim", KIND_TEXT,
                  source="claimant__email", sensitive=True,
                  description="Restricted: personal contact data."),
            Field("created_at", "Created", "Record", KIND_DATETIME),
        ),
        filters=(
            FilterDef("claim_date", "Claim date", FILTER_DATE_RANGE, required=True,
                      is_primary_date=True,
                      description="Required so an export can never mean 'every claim ever'."),
            FilterDef("status", "Status", FILTER_CHOICE, choices=_DOC_STATUS),
            FilterDef("payment_status", "Reimbursement", FILTER_CHOICE, choices=_PAY_STATUS),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("document_number", "Claim number"),
                ("claimant_name", "Claimant"),
                ("title", "Purpose"),
            ), description="Matches any one of these, the way the search box does."),
            FilterDef("total", "Total", FILTER_NUMBER_RANGE,
                      description="Amounts are in kobo."),
        ),
    ))

    register(Dataset(
        key="finance.customers",
        module="Finance",
        name="Customer master",
        description=(
            "The AR customer list with codes, contacts and control accounts. Master "
            "data, so no date filter is required."
        ),
        base=_customers,
        permission="finance.customer.view",
        row_cap=100_000,
        default_columns=("code", "name", "is_active"),
        fields=(
            Field("code", "Customer code", "Customer", KIND_TEXT, locked=True),
            Field("name", "Name", "Customer", KIND_TEXT),
            Field("is_active", "Active", "Customer", KIND_TEXT),
            Field("receivable_account", "Receivable account", "Customer", KIND_TEXT,
                  source="receivable_account__code"),
            Field("opening_balance", "Opening balance", "Amounts", KIND_MONEY),
            Field("created_at", "Created", "Record", KIND_DATETIME),
            Field("billing_email", "Billing email", "Contact", KIND_TEXT, sensitive=True,
                  description="Restricted: personal contact data."),
            Field("billing_phone", "Billing phone", "Contact", KIND_TEXT, sensitive=True,
                  description="Restricted: personal contact data."),
            Field("billing_address", "Billing address", "Contact", KIND_TEXT, sensitive=True,
                  description="Restricted: personal contact data."),
        ),
        filters=(
            FilterDef("created_at", "Created", FILTER_DATE_RANGE, is_primary_date=True),
            FilterDef("search", "Search", FILTER_SEARCH, searches=(
                ("code", "Customer code"), ("name", "Name"),
            ), description="Matches either one, the way the search box does."),
            FilterDef("name", "Name", FILTER_TEXT),
            FilterDef("is_active", "Active", FILTER_BOOLEAN),
        ),
    ))


# --------------------------------------------------------------------------- #
# Screen bindings                                                             #
# --------------------------------------------------------------------------- #
# Translate the invoice list screen's filters into export filters.
def _translate_invoices(params):
    """``/v1/finance/invoices/`` → filter specs for ``finance.customer_invoices``.

    The screen's ``bucket`` tabs are derived rather than stored, so each one is
    rebuilt here from the columns that actually back it. ``search`` spans three
    columns with an OR and has no single-filter equivalent, so it is reported as
    unmapped rather than dropped - dropping it would hand back every invoice.
    """
    import datetime

    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    today = datetime.date.today()

    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    if value := params.get("payment_status"):
        filters.append({"id": "payment_status", "values": [value]})
    if value := params.get("customer"):
        # The screen accepts a code or a numeric id; only the code is a filter here.
        if str(value).isdigit():
            unmapped.append(Unmapped(
                "customer", value,
                "The screen filtered by an internal customer id. Pick the customer "
                "again in the builder to carry it over.",
            ))
        else:
            filters.append({"id": "customer_code", "value": str(value).upper()})
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})

    bucket = (params.get("bucket") or "").lower()
    if bucket == "draft":
        filters.append({"id": "status", "values": ["DRAFT"]})
    elif bucket in ("paid", "overdue", "partial", "open"):
        filters.append({"id": "status", "values": ["POSTED"]})
        if bucket == "paid":
            filters.append({"id": "payment_status", "values": ["PAID"]})
        elif bucket == "partial":
            filters.append({"id": "payment_status", "values": ["PARTIAL"]})
        elif bucket == "open":
            filters.append({"id": "payment_status", "values": ["UNPAID", "PARTIAL"]})
        else:  # overdue: posted, not settled, and past its due date
            filters.append({"id": "payment_status", "values": ["UNPAID", "PARTIAL"]})
            filters.append({
                "id": "due_date",
                "end": (today - datetime.timedelta(days=1)).isoformat(),
            })
    elif bucket:
        unmapped.append(Unmapped("bucket", bucket, "This tab has no export equivalent."))

    return filters, unmapped


# Translate the customer list screen's filters into export filters.
def _translate_customers(params):
    from vs_exports.catalogue import Unmapped

    filters, unmapped = [], []
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    if (value := params.get("is_active")) is not None:
        filters.append({"id": "is_active", "value": str(value).lower() == "true"})

    # The screen's status tabs are not one column. ACTIVE and INACTIVE are the
    # stored flag, but OVERDUE and CREDIT are computed from the customer's live
    # AR balance, which no stored field carries - so they are reported rather
    # than quietly ignored, and the drawer says the file is wider.
    status = params.get("status")
    if status in ("ACTIVE", "INACTIVE"):
        filters.append({"id": "is_active", "value": status == "ACTIVE"})
    elif status in ("OVERDUE", "CREDIT"):
        unmapped.append(Unmapped(
            "status", status,
            "Overdue and In-credit are worked out from each customer's live balance, "
            "not stored on the customer, so the export cannot filter on them. Export "
            "Invoices instead if you need the overdue set.",
        ))
    elif status:
        unmapped.append(Unmapped(
            "status", status,
            "This status is not one the customer export recognises.",
        ))
    return filters, unmapped


# Translate the general-ledger screen's filters into export filters.
#
# One caveat this translator cannot fix, and the drawer states instead: the screen
# lists one row per ENTRY, the dataset produces one row per LINE. The filters match
# exactly; the row counts will not, and that is the dataset doing its job (a trial
# balance needs the lines).
def _translate_gl_postings(params):
    filters, unmapped = [], []
    if value := params.get("status"):
        filters.append({"id": "entry_status", "values": [value]})
    if value := params.get("source"):
        filters.append({"id": "entry_source", "values": [value]})
    # The screen sends its date window as two flat params; the export wants one
    # range object. Either end on its own is still a real narrowing (the range
    # compiles to gte/lte independently), so a half-open window is carried rather
    # than dropped - dropping it would widen the file in silence.
    window = {}
    if value := params.get("date_from"):
        window["start"] = value
    if value := params.get("date_to"):
        window["end"] = value
    if window:
        filters.append({"id": "entry_date", **window})
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    if value := params.get("account"):
        filters.append({"id": "account", "value": value})
    return filters, unmapped


# Translate the receipts and allocation screen's filters into export filters.
def _translate_receipts(params):
    filters, unmapped = [], []
    if value := params.get("status"):
        filters.append({"id": "status", "values": [value]})
    if value := params.get("method"):
        filters.append({"id": "method", "value": value})
    if value := params.get("search"):
        filters.append({"id": "search", "value": value})
    if value := params.get("customer"):
        filters.append({"id": "customer", "value": value})
    return filters, unmapped


# Translate the expense-claims screen's filters into export filters.
#
# The screen's status chip is a DISPLAY status: it collapses (status ×
# payment_status) into four words a person actually uses. The list view expands
# it server-side, and so does this - the same four branches, so a quick export
# and the table agree. "Approved" is the interesting one: it means posted but
# NOT fully reimbursed, which an "is any of" filter expresses as the payment
# states other than PAID.
def _translate_expense_claims(params):
    from vs_exports.catalogue import Unmapped

    from .constants import DocumentStatus, InvoicePaymentStatus

    filters, unmapped = [], []
    disp = params.get("display_status") or params.get("status")
    if disp == "DRAFT":
        filters.append({"id": "status", "values": [str(DocumentStatus.DRAFT)]})
    elif disp == "REJECTED":
        filters.append({"id": "status", "values": [str(DocumentStatus.CANCELLED)]})
    elif disp == "PAID":
        filters.append({"id": "status", "values": [str(DocumentStatus.POSTED)]})
        filters.append({"id": "payment_status", "values": [str(InvoicePaymentStatus.PAID)]})
    elif disp == "APPROVED":
        filters.append({"id": "status", "values": [str(DocumentStatus.POSTED)]})
        filters.append({"id": "payment_status", "values": [
            str(s) for s in InvoicePaymentStatus.values if s != InvoicePaymentStatus.PAID
        ]})
    elif disp:
        unmapped.append(Unmapped(
            "display_status", disp,
            "This status is not one the expense-claim export recognises, so the file "
            "is not limited by it.",
        ))
    for key in ("q", "search"):
        if value := params.get(key):
            filters.append({"id": "search", "value": value})
            break
    return filters, unmapped


# Register the finance screens. Called once from AppConfig.ready().
def register_screens():
    from vs_exports.catalogue import ScreenBinding, register_screen

    register_screen(ScreenBinding(
        key="finance.invoices",
        handles=(
            "status", "payment_status", "bucket", "search", "customer",
        ),
        label="Finance - Invoices",
        dataset_key="finance.customer_invoices",
        translate=_translate_invoices,
    ))
    register_screen(ScreenBinding(
        key="finance.customers",
        handles=(
            "search", "is_active", "status",
        ),
        label="Finance - Customers",
        dataset_key="finance.customers",
        translate=_translate_customers,
    ))
    register_screen(ScreenBinding(
        key="finance.gl_postings",
        handles=(
            "status", "source", "date_from", "date_to", "search", "account",
        ),
        label="Finance - General ledger",
        dataset_key="finance.gl_postings",
        translate=_translate_gl_postings,
        # The ledger's own default view is the current period, and postings are
        # the highest-cardinality table in the platform. 31 days matches the
        # dataset's advisory span rather than the app-wide 365.
        default_window_days=31,
    ))
    register_screen(ScreenBinding(
        key="finance.expense_claims",
        handles=(
            "display_status", "status", "q", "search",
        ),
        label="Finance - Expense claims",
        dataset_key="finance.expense_claims",
        translate=_translate_expense_claims,
    ))
    register_screen(ScreenBinding(
        key="finance.receipts",
        handles=(
            "search", "status", "method", "customer",
        ),
        label="Finance - Receipts",
        dataset_key="finance.customer_receipts",
        translate=_translate_receipts,
    ))
