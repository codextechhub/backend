"""Loading the households a school calls: parents, and which child each reaches.

**One row is one LINK, not one parent.** A guardian's relationship and whether
they are the primary contact belong to the pair, not to the person: Mrs Adeleke
is Mother to Amaka and Legal Guardian to the niece she also has at the school,
and a file keyed on the guardian could not say that. So a row names a guardian
and a child, and a parent with three children here appears three times.

This is the second guardian that nothing else can add in bulk. The student
import carries ONE guardian per child, which is the one the school called when
it enrolled them; every father, grandmother and legal guardian after that is
typed in by hand, one drawer at a time.

Guardians are matched on email first and phone second, the same rule
``upsert_guardian`` applies everywhere else, so a parent already at the school
is reused rather than duplicated and their existing name is kept.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .constants import Relationship
from .imports import RowIssue, _as_date, _digits, _text

#: The template's columns, in the order a school reads them.
COLUMNS = (
    "guardian_full_name",
    "guardian_phone",
    "guardian_email",
    "student_number",
    "student_first_name",
    "student_last_name",
    "student_date_of_birth",
    "relationship",
    "is_primary",
    "occupation",
    "address",
)

MAX_LENGTHS = {
    "guardian_full_name": 150,
    "guardian_phone": 32,
    "occupation": 100,
}

_YES = frozenset({"yes", "y", "true", "1", "primary"})

_RELATIONSHIPS: dict[str, str] = {}
for _code, _label in Relationship.choices:
    _RELATIONSHIPS[_code.lower()] = _code
    _RELATIONSHIPS[_label.lower()] = _code


@dataclass
class ResolvedLink:
    guardian_full_name: str = ""
    guardian_phone: str = ""
    guardian_email: str = ""
    occupation: str = ""
    address: str = ""
    relationship: str = Relationship.OTHER
    is_primary: bool = False
    #: The student this row reaches, once identified.
    student: object | None = None
    #: The guardian this school already holds on this contact.
    existing_guardian: object | None = None
    #: The link that already joins these two.
    existing_link: object | None = None
    #: Who currently holds primary contact for this student.
    current_primary: object | None = None
    issues: list = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def contact_key(self) -> str:
        return self.guardian_email.casefold() or _digits(self.guardian_phone)


def resolve_row(payload: dict, *, tenant):
    """Read one uploaded link. Everything a row can be wrong about alone."""
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.core.validators import validate_email

    row = ResolvedLink()
    row.guardian_full_name = _text(payload, "guardian_full_name")
    row.guardian_phone = _text(payload, "guardian_phone")
    row.guardian_email = _text(payload, "guardian_email")
    row.occupation = _text(payload, "occupation")
    row.address = _text(payload, "address")

    if not row.guardian_full_name:
        row.issues.append(RowIssue(
            "required", "A guardian's name is required on every row.",
            "guardian_full_name",
        ))
    if not row.guardian_phone:
        row.issues.append(RowIssue(
            "required",
            "A phone number the school can reach is required on every row.",
            "guardian_phone",
        ))
    elif len(_digits(row.guardian_phone)) < 7:
        row.issues.append(RowIssue(
            "invalid_format",
            f"'{row.guardian_phone}' is not a number the school could ring.",
            "guardian_phone", row.guardian_phone,
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

    # The address decides which household this child joins, so a broken one
    # does not make a bad row: it makes a second record for a parent the school
    # already holds, and splits the family.
    if row.guardian_email:
        try:
            validate_email(row.guardian_email)
        except DjangoValidationError:
            row.issues.append(RowIssue(
                "invalid_format",
                f"'{row.guardian_email}' is not an email address.",
                "guardian_email", row.guardian_email,
            ))

    raw_rel = _text(payload, "relationship")
    row.relationship = _RELATIONSHIPS.get(raw_rel.lower(), Relationship.OTHER)
    if raw_rel and raw_rel.lower() not in _RELATIONSHIPS:
        row.issues.append(RowIssue(
            "invalid_choice",
            f"'{raw_rel}' is not a relationship this school records. It will "
            f"be imported as Other.",
            "relationship", raw_rel, severity="warning",
        ))

    row.is_primary = _text(payload, "is_primary").casefold() in _YES

    _resolve_student(row, payload, tenant=tenant)
    _resolve_guardian(row, tenant=tenant)
    return row


def _resolve_student(row, payload, *, tenant):
    """Which child this row reaches.

    **The admission number is the only exact answer**, which is why the column
    is here even though many schools leave it blank. A name and a date of birth
    are not an identifier: the student import already warns that two real
    children share both, and if this resolver picked the first of two it would
    attach a father to the wrong family in silence. So an ambiguous match is
    refused and the message says what to write instead.
    """
    from .models import Student

    number = _text(payload, "student_number")
    first = _text(payload, "student_first_name")
    last = _text(payload, "student_last_name")
    raw_dob = _text(payload, "student_date_of_birth")

    if number:
        found = Student.objects.filter(
            tenant=tenant, student_number__iexact=number,
        ).first()
        if found is None:
            row.issues.append(RowIssue(
                "not_found",
                f"No student at this school holds admission number {number}.",
                "student_number", number,
            ))
            return
        row.student = found
        return

    if not (first and last):
        row.issues.append(RowIssue(
            "required",
            "Say which child this is: an admission number, or the child's "
            "first name, last name and date of birth.",
            "student_number",
        ))
        return

    candidates = Student.objects.filter(
        tenant=tenant, first_name__iexact=first, last_name__iexact=last,
    )
    dob = _as_date(raw_dob)
    if raw_dob and dob is None:
        row.issues.append(RowIssue(
            "invalid_format",
            f"'{raw_dob}' is not a date this importer can read. Use "
            f"YYYY-MM-DD.",
            "student_date_of_birth", raw_dob,
        ))
        return
    if dob is not None:
        candidates = candidates.filter(date_of_birth=dob)

    found = list(candidates[:3])
    if not found:
        row.issues.append(RowIssue(
            "not_found",
            f"No student at this school is called {first} {last}"
            + (f", born {raw_dob}." if raw_dob else "."),
            "student_first_name", first,
        ))
        return
    if len(found) > 1:
        row.issues.append(RowIssue(
            "business_rule",
            f"{len(found)} students are called {first} {last}"
            + (f" and born {raw_dob}" if raw_dob else "")
            + ". Give this row their admission number instead, so the right "
              "child is reached.",
            "student_first_name", first,
        ))
        return
    row.student = found[0]


def _resolve_guardian(row, *, tenant):
    """The guardian this contact already reaches, and the link if there is one."""
    from .models import StudentGuardian
    from .services.guardians import match_existing, primary_for

    if not (row.guardian_email or row.guardian_phone):
        return

    row.existing_guardian = match_existing(
        tenant, email=row.guardian_email, phone=row.guardian_phone,
    )
    if row.student is None:
        return

    row.current_primary = primary_for(row.student)
    if row.existing_guardian is not None:
        row.existing_link = StudentGuardian.objects.filter(
            student=row.student, guardian=row.existing_guardian,
        ).first()


@dataclass
class ResolvedGuardianFile:
    rows: list = dc_field(default_factory=list)
    issues: list = dc_field(default_factory=list)

    def add(self, row_number: int, issue: RowIssue):
        self.issues.append((row_number, issue))


def resolve_file(payloads, *, tenant):
    """Every row, then what only the whole file shows."""
    out = ResolvedGuardianFile()
    #: student pk -> the row that makes somebody their primary contact.
    primaries: dict[int, int] = {}
    #: contact -> (row number, the name that row gave).
    contacts: dict[str, tuple[int, str]] = {}
    #: (student pk, contact) -> row number, for a pair named twice.
    pairs: dict[tuple, int] = {}

    for row_number, payload in enumerate(payloads, start=1):
        row = resolve_row(payload, tenant=tenant)
        out.rows.append((row_number, row))
        for issue in row.issues:
            out.add(row_number, issue)
        if not row.ok:
            continue

        key = (row.student.pk, row.contact_key)
        earlier = pairs.get(key)
        if earlier is not None:
            out.add(row_number, RowIssue(
                "duplicate_record",
                f"Row {earlier} already links this guardian to "
                f"{row.student.first_name}. A pair can only be linked once.",
                "guardian_full_name", row.guardian_full_name,
            ))
            continue
        pairs[key] = row_number

        if row.existing_link is not None:
            out.add(row_number, RowIssue(
                "duplicate_record",
                f"{row.existing_guardian.full_name} is already linked to "
                f"{row.student.first_name}. This row will be skipped rather "
                f"than linking them twice.",
                "guardian_full_name", row.guardian_full_name,
                severity="warning",
            ))
            continue

        _check_contact_reused(out, row, row_number, contacts)
        _check_primary(out, row, row_number, primaries)

    _check_students_left_without_a_primary(out, tenant=tenant)
    return out


def _check_contact_reused(out, row, row_number, contacts):
    """The same contact under a different name, in the file or at the school.

    Guardians are matched on email then phone and the stored name wins, so this
    row's spelling is discarded and the child joins that household. Right for a
    real parent with three children here, and wrong for a mistyped address, and
    only the school can tell which.
    """
    if not row.contact_key:
        return
    earlier = contacts.get(row.contact_key)
    if earlier is not None:
        if earlier[1] != row.guardian_full_name:
            out.add(row_number, RowIssue(
                "duplicate_record",
                f"Row {earlier[0]} gives the same contact for "
                f"'{earlier[1]}'. Both children join that one guardian, and "
                f"'{row.guardian_full_name}' will not be recorded.",
                "guardian_full_name", row.guardian_full_name,
                severity="warning",
            ))
        return
    contacts[row.contact_key] = (row_number, row.guardian_full_name)

    held = row.existing_guardian
    if held is not None and held.full_name != row.guardian_full_name:
        out.add(row_number, RowIssue(
            "duplicate_record",
            f"{held.full_name} already uses that contact at this school, so "
            f"this child joins their household and "
            f"'{row.guardian_full_name}' will not be recorded.",
            "guardian_full_name", row.guardian_full_name, severity="warning",
        ))


def _check_primary(out, row, row_number, primaries):
    """Who ends up as the one contact the school calls.

    **Two things no single row can see.** Two rows both claiming primary for
    one child contradict each other, and the second would silently win, because
    linking a new primary demotes the old one without a word. And a row that
    claims primary for a child who already has one takes it from somebody -
    which is often exactly what a school means when it corrects a record, and
    is never something it should discover afterwards.
    """
    if not row.is_primary:
        return

    earlier = primaries.get(row.student.pk)
    if earlier is not None:
        out.add(row_number, RowIssue(
            "business_rule",
            f"Row {earlier} already makes somebody the primary contact for "
            f"{row.student.first_name}. A child has one, so these two rows "
            f"disagree.",
            "is_primary", "Yes",
        ))
        return
    primaries[row.student.pk] = row_number

    current = row.current_primary
    if current is not None and current.pk != getattr(
        row.existing_guardian, "pk", None,
    ):
        out.add(row_number, RowIssue(
            "business_rule",
            f"{row.student.first_name} already has {current.full_name} as "
            f"primary contact. This row moves it to "
            f"{row.guardian_full_name}.",
            "is_primary", "Yes", severity="warning",
        ))


def _check_students_left_without_a_primary(out, *, tenant):
    """A child this file gives contacts to and no primary contact.

    The module wants exactly one per student: it refuses an enrolment without
    one and refuses to unlink the last. A file that adds two grandparents and
    marks neither leaves the school with two numbers and no answer to "who do
    we call first".
    """
    from .services.guardians import primary_for

    # Two passes, because one is order-dependent and wrong. Reading the rows
    # once, a child whose primary is named on row 1 and whose second contact is
    # on row 5 looks unprimaried at row 5: nothing is written yet, so the
    # database still says None. Collect what the FILE settles first, then ask
    # the database only about the children it leaves open.
    marked = {
        row.student.pk for _n, row in out.rows
        if row.ok and row.student is not None and row.is_primary
    }

    wanted: dict[int, tuple] = {}
    for row_number, row in out.rows:
        if not row.ok or row.student is None:
            continue
        if row.student.pk in marked or row.student.pk in wanted:
            continue
        if primary_for(row.student) is None:
            wanted[row.student.pk] = (row_number, row.student)

    for row_number, student in wanted.values():
        out.add(row_number, RowIssue(
            "business_rule",
            f"{student.full_name} has no primary contact, and no row in this "
            f"file marks one. The school would have numbers for them and no "
            f"answer to who it calls first.",
            "is_primary", "", severity="warning",
        ))


def build_links(resolved, *, tenant, actor):
    """Create or reuse each guardian, and join them to their child."""
    from django.db import transaction

    from .services import guardians as guardian_service

    counts = {"guardians": 0, "links": 0}

    with transaction.atomic():
        for _n, row in resolved.rows:
            if not row.ok or row.existing_link is not None:
                continue
            guardian, made = guardian_service.upsert_guardian(
                tenant,
                full_name=row.guardian_full_name,
                phone=row.guardian_phone,
                email=row.guardian_email,
                occupation=row.occupation,
                address=row.address,
            )
            if made:
                counts["guardians"] += 1
            guardian_service.link(
                row.student, guardian,
                relationship=row.relationship,
                is_primary=row.is_primary,
                actor=actor,
            )
            counts["links"] += 1

    return counts


def _payload_of(raw_row: dict, columns) -> dict:
    return {c.target_field: raw_row.get(c.column_name) for c in columns}


def validate_guardians_import_batch(import_batch) -> list[dict]:
    """Every fault in an uploaded household list, in the engine's issue shape."""
    template = import_batch.template
    if template is None:
        return []

    columns = list(template.columns.all())
    header = {c.target_field: c.column_name for c in columns}
    resolved = resolve_file(
        [_payload_of(raw, columns) for raw in (import_batch.preview_rows or [])],
        tenant=import_batch.tenant,
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


def execute_guardians_import(import_batch, queued_by):
    """Publish one validated household list as a single atomic job.

    Whole-file, because the primary-contact rules are read across rows: half a
    file could leave a child with two guardians and no primary, or move a
    primary contact and then fail before the row that was meant to replace it.
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
            columns = list(import_batch.template.columns.all())
            raw_rows = import_batch.preview_rows or []
            resolved = resolve_file(
                [_payload_of(raw, columns) for raw in raw_rows], tenant=tenant,
            )
            blocking = [
                issue for _n, issue in resolved.issues
                if issue.severity == "error"
            ]
            if blocking:
                raise ValueError(
                    "The household list no longer validates. Re-upload the "
                    "file and validate it again before importing."
                )

            counts = build_links(resolved, tenant=tenant, actor=queued_by)

            ImportJobRowResult.objects.bulk_create([
                ImportJobRowResult(
                    job=job,
                    row_number=row_number,
                    action=(
                        ImportRowActionChoices.SKIP
                        if row.existing_link is not None
                        else ImportRowActionChoices.CREATE
                    ),
                    target_model="StudentGuardian",
                    target_object_pk="",
                    status_message=(
                        "Already linked."
                        if row.existing_link is not None
                        else f"{row.guardian_full_name} to "
                             f"{row.student.full_name if row.student else ''}."
                    ),
                    row_payload=raw_rows[row_number - 1],
                    normalized_payload={
                        "guardian": row.guardian_full_name,
                        "student": row.student.full_name if row.student else "",
                        "relationship": row.relationship,
                        "is_primary": row.is_primary,
                    },
                )
                for row_number, row in resolved.rows
            ])

            processed = len(resolved.rows)
            skipped = len([
                1 for _n, r in resolved.rows if r.existing_link is not None
            ])
            job.processed_rows = processed
            job.succeeded_rows = processed - skipped
            job.failed_rows = 0
            job.skipped_rows = skipped
            job.progress_percent = 100
            job.save(update_fields=[
                "processed_rows", "succeeded_rows", "failed_rows",
                "skipped_rows", "progress_percent", "updated_at",
            ])
            finalize_import_job(
                import_batch=import_batch, job=job,
                processed_rows=processed, succeeded_rows=processed - skipped,
                failed_rows=0, skipped_rows=skipped,
            )
            job.execution_summary = {**job.execution_summary, **counts}
            job.save(update_fields=["execution_summary", "updated_at"])
        return job
    except Exception as exc:
        job.status = ImportJobStatusChoices.FAILED
        job.completed_at = timezone.now()
        job.last_error_code = "GUARDIANS_IMPORT_FAILED"
        job.last_error_message = str(exc)
        job.save(update_fields=[
            "status", "completed_at", "last_error_code", "last_error_message",
            "updated_at",
        ])
        raise
