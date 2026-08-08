"""The dataset catalogue - what may be exported, by whom, and how it reads.

The handoff calls the catalogue the dependency that decides the outcome: "a thin or
badly described catalogue makes every screen feel empty". It is declared **in code**
rather than as tenant-editable rows because every entry has to name a real ORM path -
a field label with no column behind it is a run-time failure waiting to happen. What
administrators control (which datasets a role may export, which fields are sensitive)
is expressed through RBAC keys, which *are* per tenant.

Three ideas:

:class:`Field`
    One exportable column. Carries the ORM path used to read it, the label a person
    sees, a ``kind`` that decides how the value renders in each
    :class:`~vs_exports.constants.ValuesMode`, plus ``locked`` (always present, cannot
    be deselected - the row's identity) and ``sensitive`` (needs an extra permission
    and is called out at review).

:class:`FilterDef`
    One filter the UI may offer, its operators, and whether the dataset refuses to run
    without it.

:class:`Dataset`
    A base queryset factory scoped to one :class:`~vs_finance.models.LedgerEntity`,
    the fields and filters above, supported formats, a row cap and a maximum date span.

Everything the API exposes about a dataset comes from here, so the UI never hardcodes
fields, formats or option sets - which is exactly what the spec requires.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field as dc_field
from decimal import Decimal

from django.db.models import Q

from .constants import DatasetScope, ExportFormat, ValuesMode


# --------------------------------------------------------------------------- #
# Value rendering                                                             #
# --------------------------------------------------------------------------- #
#: Field kinds. The kind - not the column type - decides how a cell renders, because
#: "for people" and "for another system" are two different renderings of one value.
KIND_TEXT = "text"
KIND_DATE = "date"
KIND_DATETIME = "datetime"
KIND_MONEY = "money"       # stored in kobo (integer), like the rest of the platform
KIND_NUMBER = "number"
KIND_CHOICE = "choice"     # stored as a code, displayed as its label


# Render one cell value for the requested values mode.
def render_value(kind: str, value, mode: str, *, choices: dict | None = None):
    """Turn a raw ORM value into the cell that goes in the file.

    ``people`` mode is what a finance user reads (``26 Jul 2026``, ``₦1,240,000.00``,
    ``Overdue``); ``system`` mode is what another system imports (``2026-07-26``,
    ``1240000.00``, ``OVERDUE``). Blank values are an em dash for people and an empty
    cell for systems, so an importer never has to strip decoration.
    """
    people = mode == ValuesMode.PEOPLE
    if value is None or value == "":
        return "-" if people else ""

    if kind == KIND_DATE:
        if isinstance(value, (datetime.date, datetime.datetime)):
            return value.strftime("%d %b %Y") if people else value.strftime("%Y-%m-%d")
        return str(value)

    if kind == KIND_DATETIME:
        if isinstance(value, datetime.datetime):
            return (
                value.strftime("%d %b %Y %H:%M") if people
                else value.strftime("%Y-%m-%dT%H:%M:%S")
            )
        return str(value)

    if kind == KIND_MONEY:
        # Money is integer kobo everywhere in this platform; never float it.
        kobo = int(value)
        naira = Decimal(kobo) / Decimal(100)
        return f"₦{naira:,.2f}" if people else f"{naira:.2f}"

    if kind == KIND_NUMBER:
        if isinstance(value, Decimal):
            return f"{value:,.2f}" if people else str(value)
        return f"{value:,}" if people and isinstance(value, int) else str(value)

    if kind == KIND_CHOICE:
        code = str(value)
        return (choices or {}).get(code, code) if people else code

    return str(value)


# --------------------------------------------------------------------------- #
# Catalogue primitives                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Field:
    """One exportable column.

    ``source`` is the ORM lookup path used with ``values_list`` - reading through
    ``values_list`` rather than model instances is what keeps a 500k-row export from
    turning into 500k queries, so every field must be expressible as a path.
    """

    id: str
    label: str
    group: str
    kind: str = KIND_TEXT
    source: str = ""             # defaults to ``id`` when omitted
    locked: bool = False         # always exported; cannot be deselected
    sensitive: bool = False      # needs exports.sensitive_field.export as well
    choices: dict = dc_field(default_factory=dict)
    description: str = ""

    @property
    def path(self) -> str:
        """ORM lookup path for this column."""
        return self.source or self.id

    # Serialise for the catalogue endpoint.
    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "group": self.group,
            "type": self.kind,
            "locked": self.locked,
            "sensitive": self.sensitive,
            "description": self.description,
        }


#: Filter kinds the compiler understands.
FILTER_DATE_RANGE = "date_range"
FILTER_CHOICE = "choice"        # "is any of" over a fixed value set
FILTER_TEXT = "text"            # case-insensitive contains
FILTER_BOOLEAN = "boolean"
FILTER_NUMBER_RANGE = "number_range"


@dataclass(frozen=True)
class FilterDef:
    """One filter the builder may offer on a dataset."""

    id: str
    label: str
    kind: str
    source: str = ""
    required: bool = False
    choices: dict = dc_field(default_factory=dict)
    description: str = ""
    #: Marks the filter the dataset's ``max_date_span_days`` is measured against.
    is_primary_date: bool = False

    @property
    def path(self) -> str:
        return self.source or self.id

    # Serialise for the catalogue endpoint.
    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "type": self.kind,
            "required": self.required,
            "choices": [{"value": k, "label": v} for k, v in self.choices.items()],
            "description": self.description,
            "is_primary_date": self.is_primary_date,
        }


@dataclass(frozen=True)
class ScopeContext:
    """The boundary one export reads inside - always a tenant, sometimes an entity.

    Passed to :attr:`Dataset.base` instead of a bare entity so a tenant-scoped dataset
    (audit events, users, configuration) is expressible without inventing a fake
    entity, and an entity-scoped one still cannot reach past its set of books.
    """

    tenant: object
    entity: object = None

    @property
    def entity_code(self) -> str:
        return getattr(self.entity, "code", "") or ""

    @property
    def label(self) -> str:
        """What the review step and the Filters sheet call this scope."""
        return self.entity_code or getattr(self.tenant, "name", "") or "your organisation"


@dataclass(frozen=True)
class Dataset:
    """One publishable dataset: a queryset, its columns, and its house rules."""

    key: str
    module: str
    name: str
    description: str
    #: ``(ScopeContext) -> QuerySet``. Scoping lives here, not in a filter, so no
    #: caller can edit it away and there is no path that reads past the boundary.
    base: callable
    fields: tuple
    filters: tuple = ()
    formats: tuple = (ExportFormat.XLSX, ExportFormat.CSV)
    row_cap: int = 200_000
    max_date_span_days: int | None = None
    #: RBAC key a caller must hold to export this dataset at all.
    permission: str = ""
    #: Default column selection offered when the builder starts empty.
    default_columns: tuple = ()
    #: Which boundary this dataset's rows live inside. Entity-scoped datasets refuse
    #: to run without one; tenant-scoped ones ignore it entirely.
    scope: str = DatasetScope.ENTITY

    @property
    def needs_entity(self) -> bool:
        return self.scope == DatasetScope.ENTITY

    # Look up one field by id.
    def field(self, field_id: str) -> Field | None:
        for f in self.fields:
            if f.id == field_id:
                return f
        return None

    # Look up one filter by id.
    def filter_def(self, filter_id: str) -> FilterDef | None:
        for f in self.filters:
            if f.id == filter_id:
                return f
        return None

    @property
    def locked_field_ids(self) -> tuple:
        """Fields that are always exported whatever the caller selected."""
        return tuple(f.id for f in self.fields if f.locked)

    @property
    def required_filter_ids(self) -> tuple:
        return tuple(f.id for f in self.filters if f.required)

    # Serialise for the catalogue endpoint.
    def describe(self, *, include_sensitive: bool = True) -> dict:
        """The catalogue shape the builder reads.

        ``include_sensitive=False`` hides restricted fields from a caller who may not
        export them, so the picker never offers a column that would be dropped at run
        time.
        """
        fields = [
            f for f in self.fields if include_sensitive or not f.sensitive
        ]
        return {
            "id": self.key,
            "module": self.module,
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "requires_entity": self.needs_entity,
            "fields": [f.describe() for f in fields],
            "field_count": len(fields),
            "default_columns": list(self.default_columns or self.locked_field_ids),
            "required_filters": list(self.required_filter_ids),
            "filters": [f.describe() for f in self.filters],
            "supported_formats": [str(f) for f in self.formats],
            "format_options": FORMAT_OPTION_SCHEMA,
            "max_date_span_days": self.max_date_span_days,
            "row_cap": self.row_cap,
        }


#: The allowed option set per format, discriminated by format rather than flattened
#: into one bag of nullable fields (spec, D10·1). The UI reads it from here.
FORMAT_OPTION_SCHEMA = {
    ExportFormat.CSV: {
        "delimiter": {"type": "choice", "values": [",", ";", "\t", "|"], "default": ","},
        "encoding": {"type": "choice", "values": ["utf-8", "utf-8-sig", "latin-1"], "default": "utf-8"},
        "header_row": {"type": "boolean", "default": True},
        "quote_all": {"type": "boolean", "default": False},
        "line_ending": {"type": "choice", "values": ["\r\n", "\n"], "default": "\r\n"},
    },
    ExportFormat.XLSX: {
        "sheet_name": {"type": "text", "max_length": 31, "default": "Export"},
        "freeze_header": {"type": "boolean", "default": True},
        "filters_sheet": {"type": "boolean", "default": True},
        "auto_width": {"type": "boolean", "default": True},
    },
}


# Merge caller options over the schema defaults for one format.
def default_format_options(fmt: str) -> dict:
    """Every option for ``fmt`` at its default - the starting point for a new export."""
    return {k: v["default"] for k, v in FORMAT_OPTION_SCHEMA.get(fmt, {}).items()}


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, Dataset] = {}


# Register one dataset in the catalogue.
def register(dataset: Dataset) -> Dataset:
    """Add a dataset to the catalogue (idempotent on key)."""
    _REGISTRY[dataset.key] = dataset
    return dataset


# Fetch one dataset by key.
def get_dataset(key: str) -> Dataset | None:
    return _REGISTRY.get(key)


# List every published dataset.
def all_datasets() -> list[Dataset]:
    return sorted(_REGISTRY.values(), key=lambda d: (d.module, d.name))


# List the modules that have at least one dataset.
def modules() -> list[str]:
    return sorted({d.module for d in _REGISTRY.values()})


# --------------------------------------------------------------------------- #
# Published datasets                                                          #
# --------------------------------------------------------------------------- #
# Finance ------------------------------------------------------------------- #
# Build the entity-scoped base queryset for customer invoices.
def _invoices(scope):
    from vs_finance.models import Invoice

    return Invoice.objects.filter(entity=scope.entity)


# Build the entity-scoped base queryset for invoice lines.
def _invoice_lines(scope):
    from vs_finance.models import InvoiceLine

    return InvoiceLine.objects.filter(invoice__entity=scope.entity)


# Build the entity-scoped base queryset for posted journal lines.
def _gl_postings(scope):
    from vs_finance.models import JournalLine

    return JournalLine.objects.filter(entry__entity=scope.entity)


# Build the entity-scoped base queryset for gateway collections.
def _collections(scope):
    from vs_payments.models import CollectionIntent

    return CollectionIntent.objects.filter(entity=scope.entity)


# Build the tenant-scoped base queryset for audit events.
def _audit_events(scope):
    from vs_audit.models import AuditEvent

    # Tenant-scoped: audit events belong to the organisation, not to a set of books.
    return AuditEvent.objects.filter(tenant=scope.tenant)


# Resolve the label map for a Django TextChoices class without importing it eagerly.
def _choice_labels(dotted: str) -> dict:
    """``"vs_finance.constants.DocumentStatus"`` → ``{value: label}``.

    Resolved lazily at import of this module (not at request time) so a typo shows up
    on boot rather than mid-export.
    """
    module_path, _, name = dotted.rpartition(".")
    import importlib

    choices = getattr(importlib.import_module(module_path), name)
    return {str(value): str(label) for value, label in choices.choices}


_DOC_STATUS = _choice_labels("vs_finance.constants.DocumentStatus")
_PAY_STATUS = _choice_labels("vs_finance.constants.InvoicePaymentStatus")
_COLLECTION_STATUS = _choice_labels("vs_payments.constants.CollectionStatus")
_PROVIDER = _choice_labels("vs_payments.constants.PaymentProvider")


CUSTOMER_INVOICES = register(Dataset(
    key="finance.customer_invoices",
    module="Finance",
    name="Customer invoices",
    description=(
        "One row per invoice raised, with customer, dates, status and totals. Updated "
        "continuously. Two customer fields are restricted and marked sensitive."
    ),
    base=_invoices,
    permission="finance.invoice.view",
    row_cap=200_000,
    max_date_span_days=None,
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
        Field("currency", "Currency", "Amounts", KIND_TEXT, source="currency__code"),
        Field("reference", "Reference", "Invoice", KIND_TEXT),
        Field("narration", "Narration", "Invoice", KIND_TEXT),
        Field("customer_tax_id", "Customer tax ID", "Customer", KIND_TEXT,
              source="customer__source_id", sensitive=True,
              description="Restricted: the customer's external tax reference."),
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
        FilterDef("customer", "Customer", FILTER_TEXT, source="customer__name"),
        FilterDef("total", "Total", FILTER_NUMBER_RANGE,
                  description="Amounts are in kobo."),
    ),
))


INVOICE_LINES = register(Dataset(
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


GL_POSTINGS = register(Dataset(
    key="finance.gl_postings",
    module="Finance",
    name="General ledger postings",
    description=(
        "Posted journal entries for the period, one row per line. Tuned for a "
        "month at a time; wider ranges run but take longer."
    ),
    base=_gl_postings,
    permission="finance.journal.view",
    row_cap=500_000,
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
    ),
    filters=(
        FilterDef("entry_date", "Entry date", FILTER_DATE_RANGE, required=True,
                  source="entry__date", is_primary_date=True),
        FilterDef("entry_status", "Entry status", FILTER_CHOICE, source="entry__status",
                  choices=_DOC_STATUS),
        FilterDef("account", "Account code", FILTER_TEXT, source="account__code"),
    ),
))


# Payments ------------------------------------------------------------------ #
SETTLEMENTS = register(Dataset(
    key="payments.collections",
    module="Payments",
    name="Gateway collections",
    description=(
        "Every collection attempt through a payment provider, with provider reference, "
        "status and the receipt it booked."
    ),
    base=_collections,
    permission="payments.collection.view",
    row_cap=200_000,
    default_columns=("reference", "created_at", "amount", "status", "provider"),
    fields=(
        Field("reference", "Our reference", "Collection", KIND_TEXT, locked=True),
        Field("provider_reference", "Provider reference", "Collection", KIND_TEXT),
        Field("provider", "Provider", "Collection", KIND_CHOICE, choices=_PROVIDER),
        Field("channel", "Channel", "Collection", KIND_TEXT),
        Field("status", "Status", "Collection", KIND_CHOICE, choices=_COLLECTION_STATUS),
        Field("amount", "Amount", "Amounts", KIND_MONEY),
        Field("currency", "Currency", "Amounts", KIND_TEXT, source="currency__code"),
        Field("customer_name", "Customer", "Payer", KIND_TEXT, source="customer__name"),
        Field("invoice_number", "Invoice", "Collection", KIND_TEXT,
              source="invoice__document_number"),
        Field("created_at", "Created", "Record", KIND_DATETIME),
        Field("confirmed_at", "Confirmed", "Record", KIND_DATETIME),
        Field("payer_email", "Payer email", "Payer", KIND_TEXT, sensitive=True,
              description="Restricted: personal contact data."),
        Field("payer_name", "Payer name", "Payer", KIND_TEXT, sensitive=True,
              description="Restricted: personal data supplied at checkout."),
    ),
    filters=(
        FilterDef("created_at", "Created", FILTER_DATE_RANGE, required=True,
                  is_primary_date=True),
        FilterDef("status", "Status", FILTER_CHOICE, choices=_COLLECTION_STATUS),
        FilterDef("provider", "Provider", FILTER_CHOICE, choices=_PROVIDER),
    ),
))


# Audit -------------------------------------------------------------------- #
_AUDIT_SEVERITY = _choice_labels("vs_audit.models.AuditSeverity")
_AUDIT_STATUS = _choice_labels("vs_audit.models.AuditStatus")
_AUDIT_MODULE = _choice_labels("vs_audit.models.AuditModuleKey")


AUDIT_EVENTS = register(Dataset(
    key="audit.events",
    module="Audit",
    name="Audit events",
    description=(
        "Every audited action across the platform, one row per event. Covers the whole "
        "organisation - audit is not kept per set of books."
    ),
    base=_audit_events,
    scope=DatasetScope.TENANT,
    # The precise key: platform.audit.view reads the console, .export takes the data
    # out of it. Already seeded as restricted by seed_platform_permissions.
    permission="platform.audit.export",
    row_cap=500_000,
    max_date_span_days=92,
    default_columns=("event_at", "module_key", "action_type", "actor_label", "summary"),
    fields=(
        Field("event_id", "Event", "Event", KIND_TEXT, source="id", locked=True),
        Field("event_at", "When", "Event", KIND_DATETIME),
        Field("module_key", "Module", "Event", KIND_CHOICE, choices=_AUDIT_MODULE),
        Field("action_type", "Action", "Event", KIND_TEXT),
        Field("severity", "Severity", "Event", KIND_CHOICE, choices=_AUDIT_SEVERITY),
        Field("status", "Outcome", "Event", KIND_CHOICE, choices=_AUDIT_STATUS),
        Field("summary", "Summary", "Event", KIND_TEXT),
        Field("actor_label", "Actor", "Actor", KIND_TEXT),
        Field("actor_email", "Actor email", "Actor", KIND_TEXT,
              source="actor_user__email", sensitive=True,
              description="Restricted: identifies the person behind the action."),
        Field("entity_type", "Object type", "Target", KIND_TEXT),
        Field("entity_label", "Object", "Target", KIND_TEXT),
    ),
    filters=(
        FilterDef("event_at", "When", FILTER_DATE_RANGE, required=True,
                  is_primary_date=True,
                  description="Required - an unbounded audit export is never what "
                              "anyone actually wants."),
        FilterDef("module_key", "Module", FILTER_CHOICE, choices=_AUDIT_MODULE),
        FilterDef("severity", "Severity", FILTER_CHOICE, choices=_AUDIT_SEVERITY),
        FilterDef("status", "Outcome", FILTER_CHOICE, choices=_AUDIT_STATUS),
    ),
))


# --------------------------------------------------------------------------- #
# Filter compilation                                                          #
# --------------------------------------------------------------------------- #
class FilterError(ValueError):
    """A saved filter can no longer be applied to its dataset."""

    def __init__(self, message: str, *, filter_id: str = ""):
        super().__init__(message)
        self.filter_id = filter_id


# Parse an ISO date, raising a filter error rather than a bare ValueError.
def _as_date(raw, filter_id: str, label: str):
    if isinstance(raw, datetime.date):
        return raw
    try:
        return datetime.date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise FilterError(
            f"“{label}” needs a date written as YYYY-MM-DD; it currently reads "
            f"“{raw}”.",
            filter_id=filter_id,
        )


# Compile one saved filter into a Q object.
def compile_filter(dataset: Dataset, spec: dict) -> Q:
    """Turn one stored filter dict into a ``Q``.

    Raises :class:`FilterError` when the filter refers to something the dataset no
    longer has - the withdrawn-filter failure the design calls out by name. The error
    message is the one a finance user reads, so it names the filter, not a column.
    """
    filter_id = str(spec.get("id") or "")
    fdef = dataset.filter_def(filter_id)
    if fdef is None:
        raise FilterError(
            f"This export filters on “{filter_id}”, which no longer exists on the "
            f"{dataset.name} dataset.",
            filter_id=filter_id,
        )

    path = fdef.path

    if fdef.kind == FILTER_DATE_RANGE:
        start, end = spec.get("start"), spec.get("end")
        q = Q()
        if start:
            q &= Q(**{f"{path}__gte": _as_date(start, filter_id, fdef.label)})
        if end:
            q &= Q(**{f"{path}__lte": _as_date(end, filter_id, fdef.label)})
        return q

    if fdef.kind == FILTER_CHOICE:
        values = spec.get("values") or []
        if not isinstance(values, list):
            raise FilterError(
                f"“{fdef.label}” expects a list of values.", filter_id=filter_id,
            )
        unknown = [v for v in values if fdef.choices and str(v) not in fdef.choices]
        if unknown:
            raise FilterError(
                f"“{fdef.label}” is set to {', '.join(map(str, unknown))}, which is no "
                f"longer a value on the {dataset.name} dataset.",
                filter_id=filter_id,
            )
        return Q(**{f"{path}__in": values}) if values else Q()

    if fdef.kind == FILTER_TEXT:
        value = spec.get("value")
        return Q(**{f"{path}__icontains": value}) if value else Q()

    if fdef.kind == FILTER_BOOLEAN:
        value = spec.get("value")
        return Q(**{path: bool(value)}) if value is not None else Q()

    if fdef.kind == FILTER_NUMBER_RANGE:
        q = Q()
        if spec.get("min") is not None:
            q &= Q(**{f"{path}__gte": spec["min"]})
        if spec.get("max") is not None:
            q &= Q(**{f"{path}__lte": spec["max"]})
        return q

    raise FilterError(
        f"“{fdef.label}” uses a filter type this version cannot apply.",
        filter_id=filter_id,
    )


# Describe a filter in the plain language the review step reads back.
def describe_filter(dataset: Dataset, spec: dict) -> str:
    """One sentence a person can check - "Invoice date is 1 Jul 2026 to 31 Jul 2026"."""
    fdef = dataset.filter_def(str(spec.get("id") or ""))
    if fdef is None:
        return f"{spec.get('id')} (no longer available)"
    if fdef.kind == FILTER_DATE_RANGE:
        start, end = spec.get("start") or "any", spec.get("end") or "any"
        return f"{fdef.label} is {start} to {end}"
    if fdef.kind == FILTER_CHOICE:
        values = [fdef.choices.get(str(v), str(v)) for v in (spec.get("values") or [])]
        return f"{fdef.label} is any of {', '.join(values)}" if values else f"{fdef.label} is any"
    if fdef.kind == FILTER_TEXT:
        return f"{fdef.label} contains “{spec.get('value')}”"
    if fdef.kind == FILTER_BOOLEAN:
        return f"{fdef.label} is {'yes' if spec.get('value') else 'no'}"
    if fdef.kind == FILTER_NUMBER_RANGE:
        return f"{fdef.label} is between {spec.get('min', 'any')} and {spec.get('max', 'any')}"
    return fdef.label
