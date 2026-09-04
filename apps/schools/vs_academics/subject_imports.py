"""Loading a school's subject list, and where each subject is taught.

**One row is one subject, and the levels come with it.** A school holds its
subjects as a list with a year-group range beside each - "Mathematics, JSS1 to
SSS3" - so the offerings are a column on the subject rather than a file of
their own. A separate offerings file would be two hundred rows of two ids and
nobody would fill it in.

A subject is CATALOGUE and is not rebuilt each year. Mathematics is
Mathematics; what changes by year is where it is taught, which is the offering,
and an offering points at a level that belongs to exactly one year. So this
import creates each subject once and attaches it to the levels of the year the
school is running now.

Interpretation lives here and the two passes share it, exactly as the structure
and student imports do.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .imports import RowIssue, _text, import_session

#: The template's columns, in the order a school reads them.
COLUMNS = (
    "subject",
    "levels",
    "kind",
    "department",
    "branch",
    "description",
)

#: What a school writes for a subject every pupil takes, and one they choose.
CORE_WORDS = frozenset({"core", "compulsory", "required", "yes", "y", "true"})
ELECTIVE_WORDS = frozenset({"elective", "optional", "no", "n", "false"})

MAX_LENGTHS = {"subject": 100, "department": 100}

#: Levels are separated by a semicolon, as the calendar template separates its
#: audience. A comma cannot be the separator: "Primary 4, 5 and 6" is a thing a
#: school writes, and splitting it would invent two levels called "5" and "6".
LEVEL_SEPARATOR = ";"


@dataclass
class ResolvedSubject:
    subject: str = ""
    kind_is_core: bool = True
    department: str = ""
    branch: object | None = None
    description: str = ""
    #: Level rows this subject will be offered at.
    levels: list = dc_field(default_factory=list)
    #: Names the file gave that matched nothing, kept for the message.
    unknown_levels: list = dc_field(default_factory=list)
    issues: list = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def key(self) -> str:
        return self.subject.casefold()


def resolve_row(payload: dict, *, tenant, session):
    """Read one uploaded subject and the levels it names."""
    from vs_tenants.models import Branch

    from .models import Level

    row = ResolvedSubject()
    row.subject = _text(payload, "subject")
    row.department = _text(payload, "department")
    row.description = _text(payload, "description")

    if not row.subject:
        row.issues.append(RowIssue(
            "required", "A subject name is required on every row.", "subject",
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

    raw_kind = _text(payload, "kind").casefold()
    if raw_kind and raw_kind not in CORE_WORDS and raw_kind not in ELECTIVE_WORDS:
        row.issues.append(RowIssue(
            "invalid_choice",
            f"'{raw_kind}' is not Core or Elective. It will be imported as "
            f"Core, which every pupil takes.",
            "kind", raw_kind, severity="warning",
        ))
    row.kind_is_core = raw_kind not in ELECTIVE_WORDS

    raw_branch = _text(payload, "branch")
    if raw_branch:
        found = Branch.all_objects.filter(
            tenant=tenant, name__iexact=raw_branch,
        ).first()
        if found is None:
            row.issues.append(RowIssue(
                "not_found",
                f"'{raw_branch}' is not a branch of this school.",
                "branch", raw_branch,
            ))
        else:
            row.branch = found

    _resolve_levels(row, payload, tenant=tenant, session=session)
    return row


def _resolve_levels(row, payload, *, tenant, session):
    """The year groups this subject is taught at, by name.

    A name the school does not run is an ERROR and not a skipped value. Silently
    dropping it would leave a subject that looks imported and is taught in one
    fewer year than the school believes, which nothing downstream would ever
    surface: the subject list shows what it has, not what was asked for.
    """
    from .models import Level

    raw = _text(payload, "levels")
    if not raw:
        return

    wanted = [n.strip() for n in raw.split(LEVEL_SEPARATOR) if n.strip()]
    if not wanted:
        return

    held = {
        level.name.casefold(): level
        for level in Level.all_objects.filter(tenant=tenant, session=session)
    }
    seen: set[str] = set()
    for name in wanted:
        key = name.casefold()
        if key in seen:
            row.issues.append(RowIssue(
                "duplicate_record",
                f"'{name}' is listed twice for this subject. It is offered "
                f"there once.",
                "levels", name, severity="warning",
            ))
            continue
        seen.add(key)
        level = held.get(key)
        if level is None:
            row.unknown_levels.append(name)
            continue
        row.levels.append(level)

    if row.unknown_levels:
        row.issues.append(RowIssue(
            "not_found",
            f"This school does not run {', '.join(row.unknown_levels)} this "
            f"year. Write the year group's name exactly as it appears in "
            f"Academic Structure, and separate several with a semicolon.",
            "levels", ", ".join(row.unknown_levels),
        ))


@dataclass
class ResolvedSubjectFile:
    rows: list = dc_field(default_factory=list)
    issues: list = dc_field(default_factory=list)

    def add(self, row_number: int, issue: RowIssue):
        self.issues.append((row_number, issue))


def resolve_file(payloads, *, tenant, session):
    """Every row, then what is only visible across the file."""
    out = ResolvedSubjectFile()
    seen: dict[str, int] = {}

    for row_number, payload in enumerate(payloads, start=1):
        row = resolve_row(payload, tenant=tenant, session=session)
        out.rows.append((row_number, row))
        for issue in row.issues:
            out.add(row_number, issue)
        if not row.ok:
            continue

        earlier = seen.get(row.key)
        if earlier is not None:
            out.add(row_number, RowIssue(
                "duplicate_record",
                f"Row {earlier} already creates {row.subject}. A subject is "
                f"catalogue and exists once; list all its year groups on one "
                f"row, separated by a semicolon.",
                "subject", row.subject,
            ))
            continue
        seen[row.key] = row_number

        if not row.levels:
            out.add(row_number, RowIssue(
                "business_rule",
                f"{row.subject} names no year group, so it will exist in the "
                f"catalogue and be taught nowhere.",
                "levels", "", severity="warning",
            ))

    _check_levels_with_nothing_to_teach(out, tenant=tenant, session=session)
    _check_against_the_school(out, tenant=tenant)
    return out


def _check_levels_with_nothing_to_teach(out, *, tenant, session):
    """A year group this file leaves with no subject at all.

    **The fault a form cannot have.** Adding subjects one at a time, nobody
    notices that SSS3 was never ticked; the subject screen shows what each
    SUBJECT covers, never what each year group is missing. A file is the first
    time the whole picture exists, so it is the first chance to say that a year
    group has an empty timetable.

    Only counts year groups the school actually runs, and only where the file
    is building the catalogue rather than adding one subject to it.
    """
    from .models import Level, SubjectOffering

    covered = {
        level.pk for _n, row in out.rows if row.ok for level in row.levels
    }
    already = set(
        SubjectOffering.objects.filter(
            tenant=tenant, level__session=session,
        ).values_list("level_id", flat=True)
    )
    bare = [
        level for level in Level.all_objects.filter(tenant=tenant, session=session)
        if level.pk not in covered and level.pk not in already
    ]
    if not bare or not out.rows:
        return
    out.add(1, RowIssue(
        "business_rule",
        f"{', '.join(level.name for level in bare)} would be left with no "
        f"subject at all. Pupils there would have an empty timetable.",
        "levels", "", severity="warning",
    ))


def _check_against_the_school(out, *, tenant):
    """A subject the school already holds. Its offerings are added to, not
    replaced, so re-uploading a corrected file never takes a year group away."""
    from .models import Subject

    for row_number, row in out.rows:
        if not row.ok:
            continue
        held = Subject.all_objects.filter(
            tenant=tenant, name__iexact=row.subject,
        ).first()
        if held is not None:
            out.add(row_number, RowIssue(
                "duplicate_record",
                f"{held.name} is already in this school's subject list. The "
                f"year groups on this row are added to it rather than "
                f"creating a second subject.",
                "subject", row.subject, severity="warning",
            ))


def build_subjects(resolved, *, tenant, session, created_by=None):
    """Write the subjects and their offerings, in one transaction."""
    from django.db import transaction

    from .models import Department, Subject, SubjectOffering
    from .services.structure import generate_code

    counts = {"subjects": 0, "offerings": 0, "departments": 0}

    with transaction.atomic():
        departments = {}
        dept_codes = {
            c.lower() for c in Department.all_objects
            .filter(tenant=tenant).values_list("code", flat=True)
        }
        subject_codes = {
            c.lower() for c in Subject.all_objects
            .filter(tenant=tenant).values_list("code", flat=True)
        }

        for _n, row in resolved.rows:
            if not row.ok:
                continue

            department = None
            if row.department:
                key = row.department.casefold()
                department = departments.get(key)
                if department is None:
                    department = Department.all_objects.filter(
                        tenant=tenant, name__iexact=row.department,
                    ).first()
                    if department is None:
                        department = Department.all_objects.create(
                            tenant=tenant, name=row.department,
                            code=generate_code(row.department, dept_codes),
                        )
                        dept_codes.add(department.code.lower())
                        counts["departments"] += 1
                    departments[key] = department

            subject = Subject.all_objects.filter(
                tenant=tenant, name__iexact=row.subject,
            ).first()
            if subject is None:
                subject = Subject.all_objects.create(
                    tenant=tenant, name=row.subject,
                    code=generate_code(row.subject, subject_codes),
                    is_core=row.kind_is_core, description=row.description,
                    department=department, branch=row.branch,
                )
                subject_codes.add(subject.code.lower())
                counts["subjects"] += 1

            for level in row.levels:
                # get_or_create, so a re-upload adds the year groups that are
                # new and leaves the rest alone rather than raising on the
                # (subject, level) constraint.
                _offering, made = SubjectOffering.objects.get_or_create(
                    tenant=tenant, subject=subject, level=level,
                )
                if made:
                    counts["offerings"] += 1

    return counts


def _payload_of(raw_row: dict, columns) -> dict:
    return {c.target_field: raw_row.get(c.column_name) for c in columns}


def validate_subjects_import_batch(import_batch) -> list[dict]:
    """Every fault in an uploaded subject list, in the engine's issue shape."""
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
                "This school has no year running, so there are no year groups "
                "to teach these subjects at. Open a session first."
            ),
            "row_number": 1, "column_name": header.get("levels", "Levels"),
            "raw_value": "",
        }]

    resolved = resolve_file(
        [_payload_of(raw, columns) for raw in rows],
        tenant=tenant, session=session,
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


def execute_subjects_import(import_batch, queued_by):
    """Publish one validated subject list as a single atomic job.

    Whole-file for the same reason the structure import is: a school left with
    half its subjects, and no way to tell which half, has to diff the file
    against the screen before it can try again.
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

    job = start_import_job(import_batch=import_batch, queued_by=queued_by)
    try:
        with transaction.atomic():
            tenant = import_batch.tenant
            session = import_session(tenant)
            if session is None:
                raise ValueError(
                    "This school has no year running, so there are no year "
                    "groups to teach these subjects at."
                )

            columns = list(import_batch.template.columns.all())
            raw_rows = import_batch.preview_rows or []
            resolved = resolve_file(
                [_payload_of(raw, columns) for raw in raw_rows],
                tenant=tenant, session=session,
            )
            blocking = [
                issue for _n, issue in resolved.issues
                if issue.severity == "error"
            ]
            if blocking:
                raise ValueError(
                    "The subject list no longer validates. Re-upload the file "
                    "and validate it again before importing."
                )

            counts = build_subjects(
                resolved, tenant=tenant, session=session, created_by=queued_by,
            )

            ImportJobRowResult.objects.bulk_create([
                ImportJobRowResult(
                    job=job,
                    row_number=row_number,
                    action=ImportRowActionChoices.CREATE,
                    target_model="Subject",
                    target_object_pk="",
                    status_message=(
                        f"{row.subject} at "
                        f"{len(row.levels)} year group(s)."
                    ),
                    row_payload=raw_rows[row_number - 1],
                    normalized_payload={
                        "subject": row.subject,
                        "levels": [level.name for level in row.levels],
                        "is_core": row.kind_is_core,
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
        job.last_error_code = "SUBJECTS_IMPORT_FAILED"
        job.last_error_message = str(exc)
        job.save(update_fields=[
            "status", "completed_at", "last_error_code", "last_error_message",
            "updated_at",
        ])
        raise
