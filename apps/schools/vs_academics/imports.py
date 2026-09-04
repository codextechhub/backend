"""Building a school's academic structure from a spreadsheet.

**One row is one class, and the spine is derived from it.** A school does not
hold a normalised hierarchy; it holds a list of classes - JSS1 A, JSS1 B, JSS2
A - and the programme and year group are things it can name about each of them.
Asking for three files in dependency order would be asking the school to
normalise its own data before it can give it to us, which is the work the
import exists to remove.

So a row names its programme and its level, and both are created the first time
they appear. That is the whole point and also the risk, because a typo creates
a phantom year group rather than failing: "JSS1" and "JS1" in one file are two
levels, and the second holds one class for ever. The validator therefore treats
the file as a whole rather than row by row, and every check below that looks
across rows exists for that reason.

**Interpretation lives here, not in the engine**, and validation and execution
are two passes over the same file. Both call :func:`resolve_file`, so the two
cannot read a row differently.

Codes are not asked for. ``services.structure.generate_code`` builds them from
the name, which is the same helper the screens use, because a school thinks in
names and a code column is a column of values somebody has to invent.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

#: The template's columns, in the order a school reads them.
COLUMNS = (
    "programme",
    "level",
    "level_order",
    "promotes_to",
    "class_name",
    "arm",
    "capacity",
    "branch",
    "department",
)

#: What a school writes to say pupils leave after a level rather than moving up.
TERMINAL_WORDS = frozenset({
    "leaves school", "leaves", "leaver", "leavers", "none", "terminal",
    "graduates", "graduate", "end", "exit",
})

MAX_LENGTHS = {
    "programme": 100,
    "level": 60,
    "class_name": 60,
    "arm": 30,
    "department": 100,
}

#: A class of more than this is a typo, not a class. The largest real one a
#: Nigerian school runs is around sixty.
MAX_CAPACITY = 200


@dataclass
class RowIssue:
    code: str
    message: str
    field: str = ""
    value: str = ""
    severity: str = "error"


@dataclass
class ResolvedRow:
    """One class, and the programme and level it implies."""

    programme: str = ""
    level: str = ""
    level_order: int | None = None
    promotes_to: str = ""
    is_terminal: bool = False
    class_name: str = ""
    arm: str = ""
    capacity: int | None = None
    branch: object | None = None
    branch_named: str = ""
    department: str = ""
    issues: list = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def level_key(self) -> str:
        return self.level.casefold()

    @property
    def class_key(self) -> tuple:
        """What makes two rows the same class: the name, inside a level, at a
        branch. The same shape as the database constraint."""
        return (self.level_key, self.class_name.casefold(), self.branch_named.casefold())


def _text(payload: dict, key: str) -> str:
    raw = payload.get(key)
    return "" if raw is None else str(raw).strip()


def _as_int(raw: str):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def resolve_row(payload: dict, *, tenant, multi_branch):
    """Read one uploaded row. Everything a row can be wrong about on its own."""
    from vs_tenants.models import Branch

    row = ResolvedRow()
    row.programme = _text(payload, "programme")
    row.level = _text(payload, "level")
    row.class_name = _text(payload, "class_name")
    row.arm = _text(payload, "arm")
    row.department = _text(payload, "department")
    row.branch_named = _text(payload, "branch")

    for field, label in (
        ("programme", "A programme"),
        ("level", "A level"),
        ("class_name", "A class name"),
    ):
        if not getattr(row, field):
            row.issues.append(RowIssue(
                "required", f"{label} is required on every row.", field,
            ))

    for field, limit in MAX_LENGTHS.items():
        value = getattr(row, field, "") or ""
        if len(value) > limit:
            row.issues.append(RowIssue(
                "invalid_format",
                f"That is {len(value)} characters and the limit is {limit}. "
                f"A value this long is usually two columns that have shifted.",
                field, value[:40],
            ))

    raw_order = _text(payload, "level_order")
    if raw_order:
        row.level_order = _as_int(raw_order)
        if row.level_order is None or row.level_order < 1:
            row.issues.append(RowIssue(
                "invalid_format",
                f"'{raw_order}' is not a position. Use 1 for the first year "
                f"group of the programme, 2 for the next, and so on.",
                "level_order", raw_order,
            ))

    raw_capacity = _text(payload, "capacity")
    if raw_capacity:
        row.capacity = _as_int(raw_capacity)
        if row.capacity is None or row.capacity < 1:
            row.issues.append(RowIssue(
                "invalid_format",
                f"'{raw_capacity}' is not a number of seats. Leave it blank "
                f"for a class with no limit.",
                "capacity", raw_capacity,
            ))
        elif row.capacity > MAX_CAPACITY:
            row.issues.append(RowIssue(
                "business_rule",
                f"{row.capacity} seats in one class is almost certainly a "
                f"typed number. The limit this importer accepts is "
                f"{MAX_CAPACITY}.",
                "capacity", raw_capacity,
            ))

    raw_promotes = _text(payload, "promotes_to")
    if raw_promotes:
        if raw_promotes.casefold() in TERMINAL_WORDS:
            row.is_terminal = True
        else:
            row.promotes_to = raw_promotes

    if row.branch_named:
        found = Branch.all_objects.filter(
            tenant=tenant, name__iexact=row.branch_named,
        ).first()
        if found is None:
            # One answer for a branch that is unknown, malformed or another
            # tenant's, so the column cannot be used to enumerate.
            row.issues.append(RowIssue(
                "not_found",
                f"'{row.branch_named}' is not a branch of this school.",
                "branch", row.branch_named,
            ))
        else:
            row.branch = found

    # A class whose name does not begin with its level's is usually a row that
    # has slipped: "JSS2 A" sitting under level JSS1. A warning, because a
    # school may genuinely call its classes Red, Blue and Green.
    if (
        row.level and row.class_name
        and not row.class_name.casefold().startswith(row.level.casefold())
        and row.class_name.casefold() != row.level.casefold()
    ):
        row.issues.append(RowIssue(
            "business_rule",
            f"'{row.class_name}' does not look like a class of {row.level}. "
            f"Check that the row has not slipped.",
            "class_name", row.class_name, severity="warning",
        ))

    return row


@dataclass
class ResolvedFile:
    """The whole upload, because the faults that matter are between rows.

    A phantom level, a promotion chain that loops, two classes fighting for one
    name: none of these is visible in a single row, and every one of them is a
    file a school would otherwise upload and then have to unpick by hand.
    """

    rows: list = dc_field(default_factory=list)
    #: level key -> the programme it was first seen under, and the row number.
    level_programme: dict = dc_field(default_factory=dict)
    #: (programme key, order) -> row number, for the unique-order constraint.
    orders: dict = dc_field(default_factory=dict)
    #: level key -> row number where it was first named.
    level_rows: dict = dc_field(default_factory=dict)
    #: level key -> the spelling the school used, for messages.
    level_names: dict = dc_field(default_factory=dict)
    #: level key -> the level it promotes into, as written.
    promotions: dict = dc_field(default_factory=dict)
    issues: list = dc_field(default_factory=list)

    def add(self, row_number: int, issue: RowIssue):
        self.issues.append((row_number, issue))


def resolve_file(payloads, *, tenant, multi_branch, session):
    """Read every row, then everything that is only visible across rows."""
    out = ResolvedFile()
    classes_seen: dict[tuple, int] = {}

    for row_number, payload in enumerate(payloads, start=1):
        row = resolve_row(payload, tenant=tenant, multi_branch=multi_branch)
        out.rows.append((row_number, row))
        for issue in row.issues:
            out.add(row_number, issue)
        if not row.ok:
            continue

        # **A level under two programmes is a typo, not a structure.** A school
        # cannot have JSS1 in both Junior Secondary and Senior Secondary, and
        # the usual cause is one row whose programme cell is wrong. Left alone,
        # the import creates a second JSS1 nobody meant.
        seen = out.level_programme.get(row.level_key)
        if seen is None:
            out.level_programme[row.level_key] = (row.programme, row_number)
            out.level_rows[row.level_key] = row_number
            out.level_names[row.level_key] = row.level
            if row.promotes_to:
                out.promotions[row.level_key] = row.promotes_to
        elif seen[0].casefold() != row.programme.casefold():
            out.add(row_number, RowIssue(
                "business_rule",
                f"Row {seen[1]} puts {row.level} in '{seen[0]}' and this row "
                f"puts it in '{row.programme}'. A year group belongs to one "
                f"programme.",
                "programme", row.programme,
            ))
            continue

        if row.level_order is not None:
            key = (row.programme.casefold(), row.level_order)
            clash = out.orders.get(key)
            if clash is not None and out.level_rows.get(row.level_key) != clash:
                out.add(row_number, RowIssue(
                    "duplicate_record",
                    f"Row {clash} already gives position {row.level_order} in "
                    f"{row.programme}. Two year groups cannot share one.",
                    "level_order", str(row.level_order),
                ))
            else:
                out.orders[key] = out.level_rows.get(row.level_key, row_number)

        earlier = classes_seen.get(row.class_key)
        if earlier is not None:
            out.add(row_number, RowIssue(
                "duplicate_record",
                f"Row {earlier} already creates {row.class_name} in "
                f"{row.level}. Two classes cannot share one name.",
                "class_name", row.class_name,
            ))
            continue
        classes_seen[row.class_key] = row_number

    _check_promotions(out)
    _check_against_the_school(out, tenant=tenant, session=session)
    _check_unwired_levels(out)
    return out


def _check_promotions(out: ResolvedFile):
    """A promotion target that is not here, points at itself, or loops.

    **The loop is the one worth the code.** JSS1 promotes to JSS2, JSS2 back to
    JSS1, and end-of-year promotion moves a year group in a circle for ever.
    Nothing about either row is wrong on its own, and no screen would show it,
    because a screen only ever sees one level at a time.
    """
    known = set(out.level_programme)
    for level_key, target in out.promotions.items():
        row_number = out.level_rows[level_key]
        if target.casefold() not in known:
            out.add(row_number, RowIssue(
                "not_found",
                f"'{target}' is not a level in this file, so nothing can be "
                f"promoted into it. Write the year group's name exactly as it "
                f"appears in the Level column, or write 'Leaves school'.",
                "promotes_to", target,
            ))
            continue
        if target.casefold() == level_key:
            out.add(row_number, RowIssue(
                "business_rule",
                "A year group cannot promote into itself.",
                "promotes_to", target,
            ))

    # Walk each chain. A level already visited on this walk is a cycle.
    #
    # Reported once per cycle, not once per level in it: every member of a loop
    # is a valid starting point, so JSS1 and JSS2 pointing at each other would
    # otherwise produce the same finding twice and the school would be left
    # wondering whether there were two problems.
    reported: set[frozenset] = set()
    for start in list(out.promotions):
        seen, at = [start], start
        while at in out.promotions:
            nxt = out.promotions[at].casefold()
            if nxt not in out.level_programme:
                break
            if nxt in seen:
                cycle = seen[seen.index(nxt):]
                fingerprint = frozenset(cycle)
                if fingerprint in reported:
                    break
                reported.add(fingerprint)
                names = " to ".join(out.level_names[k] for k in cycle)
                out.add(out.level_rows[cycle[0]], RowIssue(
                    "business_rule",
                    f"These year groups promote in a circle: {names} and back "
                    f"again. Pupils would never leave the school.",
                    "promotes_to", out.promotions[cycle[0]],
                ))
                break
            seen.append(nxt)
            at = nxt


def _check_against_the_school(out: ResolvedFile, *, tenant, session):
    """What the school already holds, which the file cannot see."""
    from .models import Level, Program, SchoolClass

    if session is None:
        return

    for level_key, (programme, row_number) in out.level_programme.items():
        held = Level.all_objects.filter(
            tenant=tenant, session=session, name__iexact=level_key,
        ).select_related("program").first()
        if held is None:
            continue
        if held.program.name.casefold() != programme.casefold():
            out.add(row_number, RowIssue(
                "business_rule",
                f"This school already runs {held.name} under "
                f"'{held.program.name}' this year, and this file puts it under "
                f"'{programme}'.",
                "programme", programme,
            ))

    for row_number, row in out.rows:
        if not row.ok:
            continue
        held = SchoolClass.all_objects.filter(
            tenant=tenant, session=session, name__iexact=row.class_name,
            level__name__iexact=row.level,
        ).first()
        if held is not None:
            out.add(row_number, RowIssue(
                "duplicate_record",
                f"{row.class_name} already exists in {row.level} this year. "
                f"This row will be skipped rather than creating a second.",
                "class_name", row.class_name, severity="warning",
            ))


def _check_unwired_levels(out: ResolvedFile):
    """A level that neither promotes nor ends is the state that bites later.

    ``Level.next_level`` null means two different things, and the flag exists to
    tell them apart: pupils leave here, or nobody has wired this yet. A school
    that uploads a structure with the column blank has the second, and finds out
    at the end of the year when promotion refuses to run.
    """
    for level_key, (_programme, row_number) in out.level_programme.items():
        row = next(r for n, r in out.rows if n == row_number)
        if row.promotes_to or row.is_terminal:
            continue
        out.add(row_number, RowIssue(
            "business_rule",
            f"{row.level} says nothing about where its pupils go next, so "
            f"end-of-year promotion will not move them. Give the next year "
            f"group's name, or write 'Leaves school'.",
            "promotes_to", "", severity="warning",
        ))


def import_session(tenant):
    from .models import AcademicSession, SessionStatus

    return AcademicSession.objects.filter(
        tenant=tenant, status=SessionStatus.ACTIVE,
    ).first()


def _payload_of(raw_row: dict, columns) -> dict:
    return {c.target_field: raw_row.get(c.column_name) for c in columns}


def validate_structure_import_batch(import_batch) -> list[dict]:
    """Every fault in an uploaded structure, in the engine's issue shape."""
    from .services.scoping import branch_dimension_applies

    template = import_batch.template
    if template is None:
        return []

    tenant = import_batch.tenant
    session = import_session(tenant)
    columns = list(template.columns.all())
    header = {c.target_field: c.column_name for c in columns}
    rows = import_batch.preview_rows or []

    if session is None:
        return [{
            "severity": "error", "code": "business_rule",
            "message": (
                "This school has no year running, so there is nothing to build "
                "a structure inside. Open a session first."
            ),
            "row_number": 1, "column_name": header.get("level", "Level"),
            "raw_value": "",
        }]

    resolved = resolve_file(
        [_payload_of(raw, columns) for raw in rows],
        tenant=tenant, multi_branch=branch_dimension_applies(tenant),
        session=session,
    )
    return [
        {
            "severity": issue.severity,
            "code": issue.code,
            "message": issue.message,
            "row_number": row_number,
            "column_name": header.get(issue.field, issue.field),
            "raw_value": str(issue.value or ""),
        }
        for row_number, issue in resolved.issues
    ]


def build_structure(resolved: ResolvedFile, *, tenant, session, created_by):
    """Write the structure the file describes, in dependency order.

    Programmes and levels first, because a class cannot exist without them, and
    the promotion chain last, because a level cannot point at one that has not
    been created yet.

    Everything is get-or-create against what the school already holds, so a
    school that uploads the same file twice ends with one structure rather than
    two. That matters more here than anywhere else: a school correcting three
    rows re-uploads the whole file.
    """
    from django.db import transaction

    from .models import Department, Level, Program, SchoolClass
    from .services.structure import generate_code

    created = {"departments": 0, "programmes": 0, "levels": 0, "classes": 0}

    with transaction.atomic():
        departments = _ensure_departments(resolved, tenant, created)
        programmes = _ensure_programmes(resolved, tenant, departments, created)
        levels = _ensure_levels(resolved, tenant, session, programmes, created)
        _wire_promotions(resolved, levels)
        _ensure_classes(resolved, tenant, session, levels, created_by, created)

    return created


def _taken_codes(model, tenant, **extra):
    return {
        c.lower() for c in model.all_objects
        .filter(tenant=tenant, **extra).values_list("code", flat=True)
    }


def _ensure_departments(resolved, tenant, created):
    from .models import Department
    from .services.structure import generate_code

    out = {}
    taken = _taken_codes(Department, tenant)
    for _n, row in resolved.rows:
        if not row.ok or not row.department:
            continue
        key = row.department.casefold()
        if key in out:
            continue
        held = Department.all_objects.filter(
            tenant=tenant, name__iexact=row.department,
        ).first()
        if held is None:
            held = Department.all_objects.create(
                tenant=tenant, name=row.department,
                code=generate_code(row.department, taken),
            )
            taken.add(held.code.lower())
            created["departments"] += 1
        out[key] = held
    return out


def _ensure_programmes(resolved, tenant, departments, created):
    from .models import Program
    from .services.structure import generate_code

    out = {}
    taken = _taken_codes(Program, tenant)
    order = 0
    for _n, row in resolved.rows:
        if not row.ok:
            continue
        key = row.programme.casefold()
        if key in out:
            continue
        held = Program.all_objects.filter(
            tenant=tenant, name__iexact=row.programme,
        ).first()
        if held is None:
            order += 1
            held = Program.all_objects.create(
                tenant=tenant, name=row.programme,
                code=generate_code(row.programme, taken),
                # The order a school lists its programmes in IS the order they
                # run in: Nursery before Primary before Secondary. Nobody
                # writes a spreadsheet the other way round.
                order_index=order,
                department=departments.get(row.department.casefold()),
                branch=row.branch,
            )
            taken.add(held.code.lower())
            created["programmes"] += 1
        out[key] = held
    return out


def _ensure_levels(resolved, tenant, session, programmes, created):
    from .models import Level
    from .services.structure import generate_code

    out = {}
    taken = _taken_codes(Level, tenant, session=session)
    # Position within a programme, for the rows that gave none. First
    # appearance order, which is how a school writes them down.
    next_order: dict[str, int] = {}
    for _n, row in resolved.rows:
        if not row.ok or row.level_key in out:
            continue
        programme = programmes[row.programme.casefold()]
        held = Level.all_objects.filter(
            tenant=tenant, session=session, program=programme,
            name__iexact=row.level,
        ).first()
        if held is None:
            slot = next_order.get(row.programme.casefold(), 0) + 1
            order = row.level_order if row.level_order is not None else slot
            next_order[row.programme.casefold()] = max(slot, order)
            held = Level.all_objects.create(
                tenant=tenant, session=session, program=programme,
                name=row.level, code=generate_code(row.level, taken),
                order_index=order, is_terminal=row.is_terminal,
                branch=row.branch,
            )
            taken.add(held.code.lower())
            created["levels"] += 1
        out[row.level_key] = held
    return out


def _wire_promotions(resolved, levels):
    """Last, because a level cannot point at one that does not exist yet."""
    for level_key, target in resolved.promotions.items():
        level = levels.get(level_key)
        into = levels.get(target.casefold())
        if level is None or into is None or level.next_level_id == into.pk:
            continue
        level.next_level = into
        # The check constraint refuses both at once, and the file cannot mean
        # both: a level that names a successor is not a leaving point.
        level.is_terminal = False
        level.save(update_fields=["next_level", "is_terminal"])


def _ensure_classes(resolved, tenant, session, levels, created_by, created):
    from .models import SchoolClass
    from .services.structure import generate_code

    taken = _taken_codes(SchoolClass, tenant, session=session)
    for _n, row in resolved.rows:
        if not row.ok:
            continue
        level = levels[row.level_key]
        held = SchoolClass.all_objects.filter(
            tenant=tenant, session=session, level=level,
            name__iexact=row.class_name,
        ).first()
        if held is not None:
            continue
        code = generate_code(row.class_name, taken)
        SchoolClass.all_objects.create(
            tenant=tenant, session=session, level=level,
            name=row.class_name, code=code,
            arm=row.arm or _arm_of(row),
            capacity=row.capacity,
            branch=row.branch or level.branch,
            created_by=created_by,
        )
        # Held here rather than read back: the next row needs to know this code
        # is gone, and asking the database for a value we just chose is a query
        # that can also come back None.
        taken.add(code.lower())
        created["classes"] += 1


def _arm_of(row) -> str:
    """The tail of the class name, when the school did not name the arm.

    "JSS1 A" is arm A, which is what every screen shows beside it. Only when
    the name actually begins with the level's: a school calling its classes
    Red, Blue and Green has no arm to derive, and slicing one out of "Red"
    would produce nonsense.
    """
    if not row.class_name.casefold().startswith(row.level.casefold()):
        return ""
    return row.class_name[len(row.level):].strip()


def execute_structure_import(import_batch, queued_by):
    """Publish one validated structure as a single atomic job.

    **A whole-file import, not a row at a time**, and the engine already has
    that shape for bank statements. The reason is the same both times: what a
    row means depends on the rows around it. Half a structure is worse than
    none - a school left with three of its five year groups, no promotion chain
    and no way to tell which rows took would have to unpick it by hand before
    it could try again.

    So every row lands or none does, and a school that fixes three cells
    re-uploads the whole file onto what is already there without creating a
    second copy of anything.
    """
    from django.db import transaction
    from django.utils import timezone

    from vs_import_data.models import (
        ImportJobRowResult,
        ImportJobStatusChoices,
        ImportRowActionChoices,
    )
    from vs_import_data.services.import_executor import (
        finalize_import_job,
        start_import_job,
    )

    from .services.scoping import branch_dimension_applies

    job = start_import_job(import_batch=import_batch, queued_by=queued_by)
    try:
        with transaction.atomic():
            tenant = import_batch.tenant
            session = import_session(tenant)
            if session is None:
                raise ValueError(
                    "This school has no year running, so there is nothing to "
                    "build a structure inside."
                )

            template = import_batch.template
            columns = list(template.columns.all())
            raw_rows = import_batch.preview_rows or []

            # Re-read the file rather than trusting the validation pass: the
            # school's structure may have changed between validating and
            # confirming, and this is the pass that writes.
            resolved = resolve_file(
                [_payload_of(raw, columns) for raw in raw_rows],
                tenant=tenant, multi_branch=branch_dimension_applies(tenant),
                session=session,
            )
            blocking = [
                issue for _n, issue in resolved.issues
                if issue.severity == "error"
            ]
            if blocking:
                raise ValueError(
                    "The structure no longer validates. Re-upload the file and "
                    "validate it again before importing."
                )

            counts = build_structure(
                resolved, tenant=tenant, session=session, created_by=queued_by,
            )

            ImportJobRowResult.objects.bulk_create([
                ImportJobRowResult(
                    job=job,
                    row_number=row_number,
                    action=ImportRowActionChoices.CREATE,
                    target_model="SchoolClass",
                    target_object_pk="",
                    status_message=f"{row.class_name} in {row.level}.",
                    row_payload=raw_rows[row_number - 1],
                    normalized_payload={
                        "programme": row.programme, "level": row.level,
                        "class": row.class_name, "capacity": row.capacity,
                    },
                )
                for row_number, row in resolved.rows
            ])

            processed = len(resolved.rows)
            job.processed_rows = processed
            job.succeeded_rows = processed
            job.failed_rows = 0
            job.skipped_rows = 0
            job.progress_percent = 100
            job.save(update_fields=[
                "processed_rows", "succeeded_rows", "failed_rows",
                "skipped_rows", "progress_percent", "updated_at",
            ])
            finalize_import_job(
                import_batch=import_batch, job=job,
                processed_rows=processed, succeeded_rows=processed,
                failed_rows=0, skipped_rows=0,
            )
            job.execution_summary = {**job.execution_summary, **counts}
            job.save(update_fields=["execution_summary", "updated_at"])
        return job
    except Exception as exc:
        job.status = ImportJobStatusChoices.FAILED
        job.completed_at = timezone.now()
        job.last_error_code = "STRUCTURE_IMPORT_FAILED"
        job.last_error_message = str(exc)
        job.save(update_fields=[
            "status", "completed_at", "last_error_code", "last_error_message",
            "updated_at",
        ])
        raise
