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

**Where the datasets themselves live.** This module holds the *vocabulary* and the
registry; it declares no datasets of its own and imports no domain app. Each app
publishes its own in an ``export_datasets`` module, loaded from that app's
``AppConfig.ready()``. That is what keeps the Export Centre domain-neutral: adding a
school or health dataset never touches vs_exports, and vs_exports never grows a
``from vs_finance.models import ...``.
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


# Resolve the label map for a Django TextChoices class without importing it eagerly.
def choice_labels(dotted: str) -> dict:
    """``"vs_finance.constants.DocumentStatus"`` → ``{value: label}``.

    Also resolves classes nested inside another class, which is how several apps
    declare theirs (``"vs_user.models.User.Status"``): the longest importable prefix is
    imported, then the remaining segments are walked as attributes.

    Resolved at import of the declaring module, not per request, so a typo shows up on
    boot rather than halfway through somebody's export.
    """
    import importlib

    parts = dotted.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
        for attr in parts[split:]:
            obj = getattr(obj, attr)
        return {str(value): str(label) for value, label in obj.choices}
    raise ModuleNotFoundError(f"Cannot resolve choices from '{dotted}'.")


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
