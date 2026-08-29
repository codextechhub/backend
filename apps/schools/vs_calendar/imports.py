"""Reading a school's calendar out of a spreadsheet.

This is the first dataset in the platform a school may import for itself, and
``vs_import_data/datasets.py`` carries the argument for why the calendar is the
right one to be first. What lives here is the other half: what a row *means*.

**One resolver, two callers.** Validation and execution are separate passes over
the same file, run minutes apart by different code paths, and the classic import
bug is that they disagree - validation reads "Primary 4" one way, the executor
reads it another, and a file that passed with a green tick imports something
nobody asked for. So neither of them interprets a row. Both call
``resolve_row``, and the executor writes only what the validator already read.

**Names, not ids.** A school filling this in has a spreadsheet, not an API
console: it knows the branch is called Ikeja Branch and the level is called
JSS1. Every reference here is therefore resolved by name, case-insensitively,
inside the school's own tenant and its own year.

**A name that resolves to nothing is a refusal, never a default.** The
temptation with ``applies_to`` is to shrug at an unrecognised name and import
the event for everybody, because an event for everybody is the common case and
looks harmless. It is not harmless. Lekki Branch uploads its calendar with a
Speech Day narrowed to ``Primary 4``, and the level is actually recorded as
``Primary 4 (Lekki)``. Shrugging turns one afternoon off for the primary school
into a school-wide closure: JSS1's three classes lose a teaching day they
actually taught, their term's teaching-day count is wrong for the rest of the
year, and nobody finds out until somebody queries the attendance figures in
March. Refusing the row costs the school one correction while it still has the
file open.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field as dc_field

from .models import CalendarEvent, CalendarEventAudience, EventType

#: Column headers, as target_field names, that this dataset reads. The template
#: in ``seed_import.py`` is the other end of this list; the two are checked
#: against each other by ``test_template_columns_match_the_handler``.
COLUMNS = (
    "name",
    "event_type",
    "start_date",
    "end_date",
    "branch",
    "closes_school",
    "description",
    "applies_to",
)

#: What a school may write in the Event Type column. Both the stored code and
#: the label the product shows are accepted, because a school that downloaded
#: the template sees the label and a school that read the API sees the code, and
#: refusing either of them would be pedantry.
_EVENT_TYPES: dict[str, str] = {}
for _code, _label in EventType.choices:
    _EVENT_TYPES[_code.lower()] = _code
    _EVENT_TYPES[_label.lower()] = _code

EVENT_TYPE_LABELS = [label for _, label in EventType.choices]

_TRUE = {"yes", "y", "true", "t", "1"}
_FALSE = {"no", "n", "false", "f", "0", ""}


# ── what a row resolves to ───────────────────────────────────────────────────

@dataclass
class RowIssue:
    """One thing wrong with one row, in the shape the import engine records."""

    code: str
    message: str
    #: The target_field it is about, translated to the school's own column
    #: header before it is shown.
    field: str = ""
    value: str = ""
    severity: str = "error"


@dataclass
class ResolvedRow:
    name: str = ""
    event_type: str = ""
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    closes_school: bool = False
    description: str = ""
    branch: object | None = None
    levels: list = dc_field(default_factory=list)
    classes: list = dc_field(default_factory=list)
    #: An existing event this row would repeat, if there is one.
    duplicate: object | None = None
    issues: list[RowIssue] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def key(self) -> tuple:
        """What makes two rows in one file the same event."""
        return (self.name.casefold(), self.start_date)


# ── helpers ──────────────────────────────────────────────────────────────────

def _text(payload: dict, key: str) -> str:
    raw = payload.get(key)
    if raw is None:
        return ""
    return str(raw).strip()


def _as_date(raw: str):
    """A date, or None when the cell is not one.

    Accepts what a spreadsheet actually produces as well as what the template
    asks for: Excel hands back ``2026-11-09 00:00:00`` for a cell formatted as a
    date, and refusing that would refuse a correctly filled file.
    """
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _fmt(day) -> str:
    """A date the way the product writes it: 12 Sep 2025."""
    return f"{day.day} {day:%b %Y}"


# ── the resolver ─────────────────────────────────────────────────────────────

def resolve_row(payload: dict, *, tenant, session, batch_branch, multi_branch):
    """Read one row into the event it describes, and everything wrong with it.

    Never raises for bad data. A row is a report, not an exception: the engine
    shows a school every fault in its file at once, and a resolver that stopped
    at the first one would make it upload the same file six times.
    """
    row = ResolvedRow()

    # ── name ──
    row.name = _text(payload, "name")
    if not row.name:
        row.issues.append(RowIssue(
            "required_value_missing", "Every event needs a name.", "name",
        ))
    elif len(row.name) > 120:
        row.issues.append(RowIssue(
            "invalid_format",
            f"This name is {len(row.name)} characters. The longest a calendar "
            f"entry can be is 120.",
            "name", row.name[:40],
        ))

    # ── type ──
    raw_type = _text(payload, "event_type")
    row.event_type = _EVENT_TYPES.get(raw_type.lower(), "")
    if not raw_type:
        row.issues.append(RowIssue(
            "required_value_missing",
            f"Say what kind of entry this is: {', '.join(EVENT_TYPE_LABELS)}.",
            "event_type",
        ))
    elif not row.event_type:
        row.issues.append(RowIssue(
            "invalid_choice",
            f"'{raw_type}' is not a kind of calendar entry. Use one of: "
            f"{', '.join(EVENT_TYPE_LABELS)}.",
            "event_type", raw_type,
        ))

    # ── dates ──
    row.start_date = _as_date(payload.get("start_date"))
    row.end_date = _as_date(payload.get("end_date"))
    for value, key, label in (
        (row.start_date, "start_date", "start date"),
        (row.end_date, "end_date", "end date"),
    ):
        if value is None:
            raw = _text(payload, key)
            row.issues.append(RowIssue(
                "required_value_missing" if not raw else "invalid_format",
                f"Every event needs a {label}, written as YYYY-MM-DD."
                if not raw else
                f"'{raw}' is not a date. Write it as YYYY-MM-DD, for example "
                f"2026-11-09.",
                key, raw,
            ))

    if row.start_date and row.end_date:
        if row.end_date < row.start_date:
            row.issues.append(RowIssue(
                "business_rule",
                f"This event ends on {_fmt(row.end_date)}, before it starts on "
                f"{_fmt(row.start_date)}. A one-day event repeats the same date "
                f"in both columns.",
                "end_date", str(row.end_date),
            ))
        elif session is not None and (
            row.start_date < session.start_date or row.end_date > session.end_date
        ):
            # Refused rather than warned, and this is the one date rule that
            # refuses. A date outside every TERM is a real entry on a real
            # calendar (the December break is exactly that); a date outside the
            # YEAR belongs to a year this file is not importing into.
            row.issues.append(RowIssue(
                "business_rule",
                f"This is outside {session.name} "
                f"({_fmt(session.start_date)} to {_fmt(session.end_date)}), so "
                f"it belongs to a different school year.",
                "start_date", str(row.start_date),
            ))

    # ── closes the school ──
    raw_closes = _text(payload, "closes_school").lower()
    if raw_closes in _TRUE:
        row.closes_school = True
    elif raw_closes not in _FALSE:
        row.issues.append(RowIssue(
            "invalid_choice",
            f"'{raw_closes}' is not yes or no. Write Yes if the school is shut "
            f"on these days, or leave it blank.",
            "closes_school", raw_closes,
        ))

    row.description = _text(payload, "description")

    # ── branch ──
    row.branch = _resolve_branch(row, payload, tenant=tenant,
                                 batch_branch=batch_branch,
                                 multi_branch=multi_branch)

    # ── audience ──
    _resolve_audience(row, payload, tenant=tenant, session=session)

    # ── an event the school already has ──
    if row.ok and session is not None:
        row.duplicate = CalendarEvent.objects.filter(
            tenant=tenant, session=session, branch=row.branch,
            name__iexact=row.name, start_date=row.start_date,
        ).first()

    return row


def _resolve_branch(row, payload, *, tenant, batch_branch, multi_branch):
    """Which branch the event belongs to, or None for the whole school."""
    from vs_tenants.models import Branch

    raw = _text(payload, "branch")

    if batch_branch is not None:
        # The upload was made for one branch, so the file cannot post entries
        # into another. Silence here would let a Lekki upload write Ikeja's
        # calendar, which is the same hole the dataset rules exist to close.
        if raw and raw.casefold() != (batch_branch.name or "").casefold():
            row.issues.append(RowIssue(
                "business_rule",
                f"This upload is for {batch_branch.name}, so it cannot put an "
                f"entry on {raw}'s calendar. Leave the column blank or write "
                f"{batch_branch.name}.",
                "branch", raw,
            ))
        return batch_branch

    if not raw:
        return None

    if not multi_branch:
        # Refused rather than ignored, deliberately. A single-branch school
        # writing a branch name has misunderstood the column, and importing the
        # event school-wide anyway would look identical to it working.
        row.issues.append(RowIssue(
            "business_rule",
            "This school runs one branch, so every calendar entry is for the "
            "whole school. Leave the branch column blank.",
            "branch", raw,
        ))
        return None

    found = Branch.all_objects.filter(
        tenant=tenant, name__iexact=raw,
    ).first()
    if found is None:
        row.issues.append(RowIssue(
            "cross_reference_missing",
            f"There is no branch called '{raw}' at this school. Leave the "
            f"column blank for an entry that covers every branch.",
            "branch", raw,
        ))
    return found


def _resolve_audience(row, payload, *, tenant, session):
    """The levels and classes named in ``applies_to``.

    Blank means everybody in the event's branch scope, which is the common case
    and the reason the column is optional.
    """
    from schools.vs_academics.models import Level, SchoolClass

    raw = _text(payload, "applies_to")
    if not raw or session is None:
        return

    seen: set[str] = set()
    for part in raw.split(";"):
        name = part.strip()
        if not name:
            continue
        folded = name.casefold()
        if folded in seen:
            # "JSS1; JSS1" is a school typing twice, not a conflict.
            continue
        seen.add(folded)

        # Level first, then class, because a level covers every class under it
        # and is what a school means by "the whole of JSS1".
        level = Level.objects.filter(
            tenant=tenant, session=session, name__iexact=name,
        ).first()
        if level is not None:
            if _in_scope(row, level, name):
                row.levels.append(level)
            continue

        klass = SchoolClass.objects.filter(
            tenant=tenant, session=session, name__iexact=name,
        ).first()
        if klass is not None:
            if _in_scope(row, klass, name):
                row.classes.append(klass)
            continue

        row.issues.append(RowIssue(
            "cross_reference_missing",
            f"'{name}' is not a level or a class in this school year. Check "
            f"the spelling, or leave the column blank for an entry that covers "
            f"everybody.",
            "applies_to", name,
        ))


def _in_scope(row, target, label) -> bool:
    """A branch event may only narrow to things at that branch.

    Same rule the form applies, for the same reason: narrowing a Lekki event to
    an Ikeja class produces an entry nobody can explain, showing on Ikeja's
    calendar because of the class and not on it because of the branch.
    """
    if row.branch is None:
        return True
    if target.branch_id is not None and target.branch_id != row.branch.pk:
        row.issues.append(RowIssue(
            "business_rule",
            f"{label} is not at {row.branch.name}, so this entry cannot be "
            f"narrowed to it.",
            "applies_to", label,
        ))
        return False
    return True


# ── the session a file lands in ──────────────────────────────────────────────

def import_session(tenant):
    """The school year an uploaded calendar belongs to.

    The same answer the screens give when nothing names a year: the ACTIVE one,
    else the most recent. A file carries no year column on purpose. A school
    uploading its calendar is setting up the year it is about to run, and a year
    column would let it post a holiday into a year that has already been
    archived and reported on.
    """
    from schools.vs_academics.models import AcademicSession, SessionStatus

    return (
        AcademicSession.objects.filter(
            tenant=tenant, status=SessionStatus.ACTIVE,
        ).first()
        or AcademicSession.objects.filter(tenant=tenant)
        .order_by("-start_date").first()
    )


# ── writing one row ──────────────────────────────────────────────────────────

def create_event_from_row(row: ResolvedRow, *, tenant, session, created_by):
    """Write the event this row describes, with its audience."""
    event = CalendarEvent.objects.create(
        tenant=tenant, session=session, branch=row.branch,
        name=row.name, event_type=row.event_type,
        start_date=row.start_date, end_date=row.end_date,
        closes_school=row.closes_school, description=row.description,
        created_by=created_by,
    )
    audience = [
        CalendarEventAudience(tenant=tenant, event=event, level=level)
        for level in row.levels
    ] + [
        CalendarEventAudience(tenant=tenant, event=event, school_class=klass)
        for klass in row.classes
    ]
    if audience:
        CalendarEventAudience.objects.bulk_create(audience)
    return event


# ── validating a whole file ──────────────────────────────────────────────────

def _payload_of(row: dict, columns) -> dict:
    """One uploaded row, keyed the way the handler reads it.

    The engine's own ``map_row_to_payload`` does this at execution time. The
    validator has to do the same translation, so the two passes are looking at
    the same row and not at the same file read two ways.
    """
    return {c.target_field: row.get(c.column_name) for c in columns}


def validate_calendar_events_import_batch(import_batch) -> list[dict]:
    """Every fault in an uploaded calendar, in the engine's issue shape.

    Returns errors, which block the import, and warnings, which do not. The
    split follows the module's standing rule that **a clash is a warning and not
    a refusal**: an entry that overlaps another is usually a mistake and
    occasionally two branches' arrangements being recorded, and no server can
    tell which. What is refused is what cannot be written at all.
    """
    template = import_batch.template
    if template is None:
        return []

    tenant = import_batch.tenant
    session = import_session(tenant)
    if session is None:
        return [{
            "severity": "error",
            "code": "business_rule",
            "message": (
                "This school has no academic year yet, and every calendar entry "
                "belongs to one. Create the year first, then upload this file."
            ),
            "row_number": None,
            "column_name": "",
            "raw_value": "",
        }]

    from schools.vs_academics.services.years import assert_year_is_writable

    from .services.scoping import branch_dimension_applies

    try:
        assert_year_is_writable(session)
    except Exception as exc:  # noqa: BLE001 - reported as a row-less issue
        # The year is archived. Refused here rather than at execution, so the
        # school is told before it fills in three hundred rows.
        return [{
            "severity": "error",
            "code": "business_rule",
            "message": str(getattr(exc, "detail", exc)),
            "row_number": None,
            "column_name": "",
            "raw_value": "",
        }]

    columns = list(template.columns.all())
    header = {c.target_field: c.column_name for c in columns}
    multi_branch = branch_dimension_applies(tenant)
    rows = import_batch.preview_rows or []

    issues: list[dict] = []
    first_seen: dict[tuple, int] = {}

    def record(row_number, issue: RowIssue):
        issues.append({
            "severity": issue.severity,
            "code": issue.code,
            "message": issue.message,
            "row_number": row_number,
            "column_name": header.get(issue.field, issue.field),
            "raw_value": str(issue.value or ""),
        })

    for row_number, raw_row in enumerate(rows, start=1):
        resolved = resolve_row(
            _payload_of(raw_row, columns),
            tenant=tenant, session=session,
            batch_branch=import_batch.branch, multi_branch=multi_branch,
        )
        for issue in resolved.issues:
            record(row_number, issue)

        if not resolved.ok:
            continue

        # Two rows naming the same event on the same day. Refused rather than
        # imported twice: a calendar with Founder's Day on it twice is a
        # calendar somebody has to clean up by hand.
        earlier = first_seen.get(resolved.key)
        if earlier is not None:
            record(row_number, RowIssue(
                "duplicate_record",
                f"Row {earlier} already has '{resolved.name}' starting on "
                f"{_fmt(resolved.start_date)}.",
                "name", resolved.name,
            ))
            continue
        first_seen[resolved.key] = row_number

        if resolved.duplicate is not None:
            record(row_number, RowIssue(
                "duplicate_record",
                f"'{resolved.name}' is already on this calendar starting "
                f"{_fmt(resolved.start_date)}. This row will be skipped.",
                "name", resolved.name, severity="warning",
            ))
            continue

        for issue in _calendar_warnings(resolved, tenant=tenant, session=session):
            record(row_number, issue)

    return issues


def _calendar_warnings(row: ResolvedRow, *, tenant, session) -> list[RowIssue]:
    """What is odd about an entry that will still be imported.

    The same two things the events API warns about on a single write, so a
    school gets the same answer whether it types an entry or uploads it.
    """
    from .services.calendar import term_of

    out: list[RowIssue] = []
    if term_of(session, row.start_date) is None:
        out.append(RowIssue(
            "business_rule",
            f"This falls outside every term in {session.name}. It will show on "
            f"the calendar and be flagged in the events list.",
            "start_date", str(row.start_date), severity="warning",
        ))

    overlap = CalendarEvent.objects.filter(
        tenant=tenant, session=session, event_type=row.event_type,
        branch=row.branch,
        start_date__lte=row.end_date, end_date__gte=row.start_date,
    ).first()
    if overlap is not None:
        out.append(RowIssue(
            "business_rule",
            f"This overlaps {overlap.name}, which is already on the calendar "
            f"with the same kind and scope.",
            "start_date", str(row.start_date), severity="warning",
        ))
    return out
