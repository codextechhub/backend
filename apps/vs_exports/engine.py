"""The export engine - turn a configuration into rows, an estimate, or a file.

Three responsibilities, deliberately separated from the views and the queue:

**Authorisation is re-checked here, not trusted from the builder.** The spec draws the
line sharply: at build time permission checks are *advisory* (they shape the catalogue
so the UI can disable things with a reason); at run time they are *authoritative* and
re-evaluated as the owner. A field the owner has since lost is dropped and named in
:class:`Omission` - the run completes with omissions rather than failing, because a
file missing two columns is more use than no file, provided nothing is silent about it.

**Reading is done through ``values_list``.** Every catalogue field is an ORM path, so
one query streams the whole result set; nothing here materialises model instances, and
a 200k-row export costs one query rather than 200k.

**Failures carry a code.** :class:`ExportError` pairs a
:class:`~vs_exports.constants.FailureCode` with a user-safe sentence. The view and the
worker both render only the code, the sentence and the run reference - never a
traceback.
"""
from __future__ import annotations

import datetime
from itertools import islice

from django.db.models import Q

from .catalogue import (
    FilterError,
    compile_filter,
    describe_filter,
    get_dataset,
    render_value,
)
from .constants import (
    DEFAULT_ROW_CAP,
    EXACT_COUNT_LIMIT,
    ExportPermission,
    FailureCode,
    OmissionCode,
    PREVIEW_ROWS,
    ROW_WARNING_THRESHOLD,
    ValuesMode,
)


class ExportError(Exception):
    """A run cannot proceed. Carries the machine code and the user-safe message."""

    def __init__(self, code: str, message: str, *, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class Omission:
    """One structured reason part of a result was left out.

    The UI renders this list; it never infers omissions from prose, which is why the
    scope and the affected items are separate fields rather than a sentence.
    """

    def __init__(self, code: str, scope: str, detail: str, items=None):
        self.code = code
        self.scope = scope          # "columns" | "rows"
        self.detail = detail        # user-safe sentence
        self.items = list(items or [])

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "scope": self.scope,
            "detail": self.detail,
            "items": self.items,
        }


# --------------------------------------------------------------------------- #
# Authorisation                                                               #
# --------------------------------------------------------------------------- #
# Check one RBAC key for a user in a tenant.
def _holds(user, key: str, tenant) -> bool:
    from vs_rbac.evaluator import has_permission
    from vs_rbac.permissions import is_vision_super_admin

    if not key:
        return True
    if is_vision_super_admin(user):
        return True
    return has_permission(user, key, tenant=tenant)


# Decide whether a user may export a dataset at all.
def may_export_dataset(user, dataset, tenant) -> bool:
    """The dataset's own permission key - the coarse gate."""
    return _holds(user, dataset.permission, tenant)


# Decide whether a user may include restricted fields.
def may_export_sensitive(user, tenant) -> bool:
    """Sensitive fields need this key *in addition* to the dataset's key."""
    return _holds(user, ExportPermission.SENSITIVE_EXPORT, tenant)


def resolve_columns(user, dataset, column_ids, tenant):
    """Reduce requested columns to the ones this user may actually read.

    Returns ``(fields, omissions)``. Locked fields are always included and always
    first - they are the row's identity, and a file whose rows cannot be identified is
    not an export. Withdrawn fields and fields the caller has lost access to each
    produce their own :class:`Omission`, so the run detail can say *which* of the two
    happened rather than "something was left out".

    The same call serves both checkpoints: at build time the caller treats the result
    as advisory (it shapes the picker), at run time as authoritative (it shapes the
    file). Sharing one implementation is what stops the two from drifting apart.
    """
    allow_sensitive = may_export_sensitive(user, tenant)
    requested = list(dict.fromkeys(column_ids or []))          # de-dupe, keep order
    # Locked fields lead, then everything the caller asked for.
    ordered = list(dataset.locked_field_ids) + [
        c for c in requested if c not in dataset.locked_field_ids
    ]

    fields, withdrawn, forbidden = [], [], []
    for column_id in ordered:
        field = dataset.field(column_id)
        if field is None:
            withdrawn.append(column_id)
            continue
        if field.sensitive and not allow_sensitive:
            forbidden.append(field.label)
            continue
        fields.append(field)

    omissions = []
    if forbidden:
        omissions.append(Omission(
            OmissionCode.FIELD_FORBIDDEN, "columns",
            f"{_and_list(forbidden)} {'were' if len(forbidden) > 1 else 'was'} left out "
            f"because your access to {'them' if len(forbidden) > 1 else 'it'} was "
            f"removed. Every other row and column was exported in full.",
            forbidden,
        ))
    if withdrawn:
        omissions.append(Omission(
            OmissionCode.FIELD_WITHDRAWN, "columns",
            f"{_and_list(withdrawn)} no longer {'exist' if len(withdrawn) > 1 else 'exists'} "
            f"on the {dataset.name} dataset and {'were' if len(withdrawn) > 1 else 'was'} "
            f"left out.",
            withdrawn,
        ))
    return fields, omissions


# Join labels the way a sentence would.
def _and_list(items) -> str:
    items = [str(i) for i in items]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


# --------------------------------------------------------------------------- #
# Query building                                                              #
# --------------------------------------------------------------------------- #
# Validate that required filters are present.
def _check_required_filters(dataset, filters):
    present = {str(f.get("id")) for f in filters or []}
    missing = [
        dataset.filter_def(fid).label
        for fid in dataset.required_filter_ids
        if fid not in present
    ]
    if missing:
        raise ExportError(
            FailureCode.REQUIRED_FILTER_MISSING,
            f"The {dataset.name} dataset needs {_and_list(missing)} to be set before it "
            f"can run. Edit the export and set it.",
        )


# The dataset's primary date filter, and the span it asks for.
def _primary_date_span(dataset, filters):
    """``(primary, start, end, span_days)`` for the primary date filter.

    ``span_days`` is ``None`` when there is nothing to measure. Raises only for a
    date this app cannot parse, which is a genuine configuration fault rather
    than a judgement about size.
    """
    primary = next((f for f in dataset.filters if f.is_primary_date), None)
    if primary is None:
        return None, None, None, None
    spec = next((f for f in filters or [] if str(f.get("id")) == primary.id), None)
    if not spec:
        return primary, None, None, None
    start, end = spec.get("start"), spec.get("end")
    if not start or not end:
        return primary, start, end, None
    try:
        span = (
            datetime.date.fromisoformat(str(end)) - datetime.date.fromisoformat(str(start))
        ).days
    except ValueError:
        raise ExportError(
            FailureCode.FILTER_INVALID,
            f"“{primary.label}” needs dates written as YYYY-MM-DD.",
        )
    return primary, start, end, span


# A required date filter has to be BOUNDED to count as set.
def _check_date_bounds(dataset, filters):
    """Both ends of a required date range must be given.

    This is about the filter being genuinely set, not about how wide it is -
    a dataset that declares its date filter required means a bounded read, and
    "from the beginning of time" is not a range. How wide the range may be is
    :func:`date_span_warning`'s business, and it only warns.
    """
    primary, start, end, _ = _primary_date_span(dataset, filters)
    if primary is None or not primary.required:
        return
    spec = next((f for f in filters or [] if str(f.get("id")) == primary.id), None)
    if spec and (not start or not end):
        raise ExportError(
            FailureCode.REQUIRED_FILTER_MISSING,
            f"“{primary.label}” needs both a start and an end date.",
        )


# Advise on an unusually wide date range - never block on it.
def date_span_warning(dataset, filters):
    """``{code, message}`` when the range is wider than the dataset suggests.

    A warning, not a failure. The dataset's ``max_date_span_days`` is guidance
    about cost, and a finance user asking for a quarter or a year of postings is
    making a perfectly ordinary request. The hard ceiling on what one export may
    produce is the ROW CAP, which is measured on the actual result rather than
    guessed at from the calendar.
    """
    if not dataset.max_date_span_days:
        return None
    _, _, _, span = _primary_date_span(dataset, filters)
    if span is None or span <= dataset.max_date_span_days:
        return None
    return {
        "code": "WIDE_DATE_RANGE",
        "message": (
            f"This asks for {span:,} days of {dataset.name.lower()}; the dataset is "
            f"tuned for {dataset.max_date_span_days}. It will run, but expect it to "
            f"take longer and produce a larger file."
        ),
    }


def build_queryset(dataset, scope, filters, sort=None):
    """Scoped queryset with every saved filter and sort applied.

    The boundary comes from the dataset's own ``base`` factory rather than a filter, so
    it cannot be edited away by a caller and there is no code path that reads past it -
    another entity's books for an entity-scoped dataset, another tenant's rows for a
    tenant-scoped one.
    """
    _check_required_filters(dataset, filters)
    _check_date_bounds(dataset, filters)

    qs = dataset.base(scope)
    combined = Q()
    for spec in filters or []:
        try:
            combined &= compile_filter(dataset, spec)
        except FilterError as exc:
            raise ExportError(
                FailureCode.FILTER_INVALID, str(exc), detail=exc.filter_id,
            )
    qs = qs.filter(combined)

    order = []
    for item in sort or []:
        field = dataset.field(str(item.get("field", "")))
        if field is None:
            continue                                  # a withdrawn sort is not fatal
        prefix = "-" if str(item.get("direction", "asc")).lower() == "desc" else ""
        order.append(f"{prefix}{field.path}")
    # A stable tiebreak keeps paging and previews reproducible between runs.
    return qs.order_by(*order, "pk") if order else qs.order_by("pk")


# --------------------------------------------------------------------------- #
# Estimation and preview                                                      #
# --------------------------------------------------------------------------- #
#: Rough bytes per cell, per format. Deliberately coarse - the run records the truth.
_BYTES_PER_CELL = {"xlsx": 11, "csv": 9}


def estimate(user, dataset, scope, config, tenant):
    """Row count, byte size and warnings for the builder's estimate panel.

    Counting stops being exact above :data:`~vs_exports.constants.EXACT_COUNT_LIMIT`:
    beyond that the builder gets a bucketed figure and a lowered confidence rather than
    a spinner where a number should be, which is the honest fallback the handoff asks
    for.
    """
    fields, omissions = resolve_columns(user, dataset, config.get("columns"), tenant)
    qs = build_queryset(dataset, scope, config.get("filters"), config.get("sort"))

    # Count one row past the limit - a LIMITed COUNT, so a huge table costs the same
    # as a small one - which is enough to tell "exactly N" from "more than N".
    rows = qs.values("pk")[: EXACT_COUNT_LIMIT + 1].count()
    exact = rows <= EXACT_COUNT_LIMIT

    fmt = config.get("format") or "xlsx"
    bytes_est = rows * max(len(fields), 1) * _BYTES_PER_CELL.get(fmt, 10)

    row_cap = min(dataset.row_cap, DEFAULT_ROW_CAP)
    warnings = []
    if not exact:
        warnings.append({
            "code": "ESTIMATE_BUCKETED",
            "message": (
                f"More than {EXACT_COUNT_LIMIT:,} rows match. The exact count is "
                f"recorded on the run."
            ),
        })
    if rows > ROW_WARNING_THRESHOLD:
        warnings.append({
            "code": "LARGE_RESULT",
            "message": (
                f"This export produces more than {ROW_WARNING_THRESHOLD:,} rows. It will "
                f"run, but it will take a while and the file will be large."
            ),
        })
    if rows > row_cap:
        warnings.append({
            "code": "ROW_CAP_EXCEEDED",
            "message": (
                f"This is above the {row_cap:,}-row cap for {dataset.name}. Narrow the "
                f"filters - a shorter date range is usually enough."
            ),
        })
    if (wide := date_span_warning(dataset, config.get("filters"))) is not None:
        warnings.append(wide)
    for omission in omissions:
        warnings.append({"code": omission.code, "message": omission.detail})

    return {
        "matching_rows": rows if exact else None,
        "rows_bucket": None if exact else f"more than {EXACT_COUNT_LIMIT:,}",
        "estimated_bytes": bytes_est,
        "estimate_confidence": "exact" if exact else "bucketed",
        "columns": len(fields),
        "row_cap": row_cap,
        "warnings": warnings,
    }


def sample_rows(user, dataset, scope, config, tenant, *, limit=PREVIEW_ROWS):
    """First rows of the result, rendered exactly as they will appear in the file."""
    fields, _ = resolve_columns(user, dataset, config.get("columns"), tenant)
    qs = build_queryset(dataset, scope, config.get("filters"), config.get("sort"))
    mode = config.get("values_mode") or ValuesMode.PEOPLE
    paths = [f.path for f in fields]
    if not paths:
        return [f.label for f in fields], []
    raw = qs.values_list(*paths)[:limit]
    rows = [
        [
            render_value(field.kind, value, mode, choices=field.choices)
            for field, value in zip(fields, record)
        ]
        for record in raw
    ]
    return [f.label for f in fields], rows


# --------------------------------------------------------------------------- #
# Production                                                                  #
# --------------------------------------------------------------------------- #
#: Rows fetched per database round trip while writing.
CHUNK_SIZE = 2_000


def produce(user, config, scope, tenant, *, progress=None, is_cancelled=None):
    """Build the file bytes for one run.

    Returns ``(bytes, headers, field_ids, row_count, omissions)``.

    ``progress`` is called as ``progress(phase, rows_done, rows_total)`` so the run row
    can report determinate progress; ``is_cancelled`` is polled between chunks, which
    is what makes cancellation cooperative - the caller can stop us without leaving a
    half-written file, because nothing is stored until this function returns.
    """
    from .constants import RunPhase
    from .writers import write

    dataset = get_dataset(config.get("dataset_key"))
    if dataset is None:
        raise ExportError(
            FailureCode.DATASET_WITHDRAWN,
            "The dataset this export reads is no longer published. Pick a different "
            "dataset, or ask your administrators to publish it again.",
        )
    if not may_export_dataset(user, dataset, tenant):
        raise ExportError(
            FailureCode.DATASET_FORBIDDEN,
            f"Your access to the {dataset.name} dataset was removed, so this export "
            f"could not run. Request access, then run it again.",
        )

    fields, omissions = resolve_columns(user, dataset, config.get("columns"), tenant)
    if not fields:
        raise ExportError(
            FailureCode.NO_COLUMNS,
            "Every column this export selects is now unavailable to you, so there was "
            "nothing to put in the file.",
        )

    qs = build_queryset(dataset, scope, config.get("filters"), config.get("sort"))

    if progress:
        progress(RunPhase.COUNTING, 0, None)
    row_cap = min(dataset.row_cap, DEFAULT_ROW_CAP)
    total = qs.count()
    capped = total > row_cap
    if capped:
        omissions.append(Omission(
            OmissionCode.ROW_CAP_HIT, "rows",
            f"{total:,} rows matched but {dataset.name} caps an export at {row_cap:,}. "
            f"The first {row_cap:,} rows are in the file; narrow the filters to get the "
            f"rest.",
            [f"{total - row_cap:,} rows not exported"],
        ))
        total = row_cap

    mode = config.get("values_mode") or ValuesMode.PEOPLE
    headers = [f.label for f in fields]
    paths = [f.path for f in fields]

    if progress:
        progress(RunPhase.READING, 0, total)

    rows = []
    # islice rather than a sliced queryset so the cap and the streaming cursor stay
    # independent - the reader still fetches in chunks, it just stops early.
    source = islice(qs.values_list(*paths).iterator(chunk_size=CHUNK_SIZE), row_cap)
    for index, record in enumerate(source, start=1):
        rows.append([
            render_value(field.kind, value, mode, choices=field.choices)
            for field, value in zip(fields, record)
        ])
        if index % CHUNK_SIZE == 0:
            if is_cancelled and is_cancelled():
                raise Cancelled()
            if progress:
                progress(RunPhase.READING, index, total)

    if is_cancelled and is_cancelled():
        raise Cancelled()
    if progress:
        progress(RunPhase.BUILDING, len(rows), total)

    fmt = config.get("format") or "xlsx"
    try:
        body = write(
            fmt, headers, rows,
            options=config.get("format_options"),
            filters_summary=filters_summary(dataset, config, scope),
        )
    except ValueError as exc:
        raise ExportError(FailureCode.FILTER_INVALID, str(exc))

    return body, headers, [f.id for f in fields], len(rows), omissions


class Cancelled(Exception):
    """Raised inside :func:`produce` when a cancel was requested mid-run."""


# Describe what produced a file, for the workbook's Filters sheet and the run detail.
def filters_summary(dataset, config, scope):
    """``[(label, value)]`` - the provenance a reader needs six weeks later."""
    rows = [
        ("Dataset", dataset.name),
        ("Scope", scope.label),
        ("Values", "For people to read" if (config.get("values_mode") or ValuesMode.PEOPLE) == ValuesMode.PEOPLE
         else "For another system to import"),
    ]
    for spec in config.get("filters") or []:
        rows.append(("Filter", describe_filter(dataset, spec)))
    if not config.get("filters"):
        rows.append(("Filter", f"None - the whole dataset for {scope.label}"))
    return rows


# Build the plain sentence the review step reads back.
def plain_sentence(dataset, config, scope, *, rows=None) -> str:
    """One sentence a finance user can check without knowing the schema."""
    columns = len(config.get("columns") or [])
    fmt = "an Excel file" if (config.get("format") or "xlsx") == "xlsx" else "a CSV file"
    where = scope.label
    count = f"about {rows:,} rows" if rows is not None else "an unknown number of rows"
    filters = [describe_filter(dataset, s) for s in config.get("filters") or []]
    scope = f" where {'; '.join(filters)}" if filters else ""
    return (
        f"{dataset.name} in {where}{scope} - {columns} columns, {count} - as {fmt}. "
        f"Files stay available for 30 days."
    )
