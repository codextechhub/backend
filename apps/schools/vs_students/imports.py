"""Loading a school's existing roll from a spreadsheet.

**Interpretation lives here, not in the engine**, and validation and execution
are two passes over the same file. The way an import goes wrong quietly is the
two passes reading a row differently, so both call :func:`resolve_row` and the
handler writes only what that resolver read.

The engine takes its tenant from the batch and the template carries no school
column, so there is no way for a row to name a different school. That is the
same rule the calendar importer follows and the reason the schools, branches
and cx_users handlers are no guide: those three act on CodeX's own records.

FRD M11 v2.4 FR-012.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field as dc_field

from .constants import Gender, Relationship, StudentStatus

#: The template's columns, in the order a school reads them.
#:
#: ``branch`` and ``guardian_email`` are carried even though the design's own
#: template screen lists twelve columns and omits both. The design also has a
#: branch switcher, so it is not a single-branch product; and rule 4 below
#: matches guardians on an email it would otherwise never receive, which would
#: quietly split every family whose children are imported in one file.
COLUMNS = (
    "first_name",
    "middle_name",
    "last_name",
    "date_of_birth",
    "gender",
    "student_number",
    "admission_date",
    "branch",
    "class",
    "guardian_full_name",
    "guardian_phone",
    "guardian_email",
    "guardian_relationship",
    "address",
    "previous_school",
)

REQUIRED_COLUMNS = frozenset({
    "first_name", "last_name", "date_of_birth", "gender",
    "guardian_full_name", "guardian_phone",
})

_GENDERS: dict[str, str] = {}
for _code, _label in Gender.choices:
    _GENDERS[_code.lower()] = _code
    _GENDERS[_label.lower()] = _code

_RELATIONSHIPS: dict[str, str] = {}
for _code, _label in Relationship.choices:
    _RELATIONSHIPS[_code.lower()] = _code
    _RELATIONSHIPS[_label.lower()] = _code


@dataclass
class RowIssue:
    code: str
    message: str
    field: str = ""
    value: str = ""
    severity: str = "error"


@dataclass
class ResolvedRow:
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    date_of_birth: dt.date | None = None
    gender: str = ""
    student_number: str = ""
    admission_date: dt.date | None = None
    address: str = ""
    previous_school: str = ""
    branch: object | None = None
    school_class: object | None = None
    guardian_full_name: str = ""
    guardian_phone: str = ""
    guardian_email: str = ""
    guardian_relationship: str = Relationship.OTHER
    #: An existing student this row looks like. A warning, never an error: two
    #: real siblings can share a surname and a birthday is not a fingerprint.
    duplicate: object | None = None
    issues: list = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def key(self) -> tuple:
        """What makes two rows in one file the same child."""
        return (
            self.first_name.casefold(), self.last_name.casefold(),
            self.date_of_birth,
        )


def _text(payload: dict, key: str) -> str:
    raw = payload.get(key)
    return "" if raw is None else str(raw).strip()


#: The longest each column may be before the database refuses it.
#:
#: Checked HERE rather than left to the write, because a column that overflows
#: raises a DataError halfway through the file - after earlier rows are already
#: committed - and the school is left with a partial roll and a traceback. The
#: numbers come from the models and a test holds them to it.
MAX_LENGTHS = {
    "first_name": 100,
    "middle_name": 100,
    "last_name": 100,
    "student_number": 32,
    "previous_school": 200,
    "guardian_full_name": 150,
    "guardian_phone": 32,
}

#: Under 2 or over 25 is a typed year, not a pupil. The same bounds the enrol
#: form applies, so a file and a form refuse the same child for the same reason.
MIN_AGE_YEARS = 2
MAX_AGE_YEARS = 25


def _digits(raw: str) -> str:
    return "".join(c for c in raw if c.isdigit())


def _as_date(raw: str):
    """The formats a school's spreadsheet actually produces.

    ISO first because the template asks for it, then the two orderings a
    Nigerian school types by hand. An ambiguous value is read as day-first,
    which is what the template's own hint says and what the design's warning
    tells the school it did.
    """
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def resolve_row(payload: dict, *, tenant, session, batch_branch, multi_branch, policy=None):
    """Read one uploaded row into the thing the handler will write.

    Called by both passes. Everything it can refuse, it refuses here, so a
    school is told before the first row is written rather than after some of
    them are.
    """
    from .services.policy import assert_number_allowed, read_policy

    row = ResolvedRow()
    policy = policy or read_policy(tenant)

    row.first_name = _text(payload, "first_name")
    row.middle_name = _text(payload, "middle_name")
    row.last_name = _text(payload, "last_name")
    row.address = _text(payload, "address")
    row.previous_school = _text(payload, "previous_school")

    if not row.first_name:
        row.issues.append(RowIssue("required", "A first name is required.", "first_name"))
    if not row.last_name:
        row.issues.append(RowIssue("required", "A last name is required.", "last_name"))

    raw_dob = _text(payload, "date_of_birth")
    row.date_of_birth = _as_date(raw_dob)
    if not raw_dob:
        row.issues.append(
            RowIssue("required", "A date of birth is required.", "date_of_birth"),
        )
    elif row.date_of_birth is None:
        row.issues.append(RowIssue(
            "invalid_format",
            f"'{raw_dob}' is not a date this importer can read. Use YYYY-MM-DD.",
            "date_of_birth", raw_dob,
        ))
    elif row.date_of_birth > dt.date.today():
        row.issues.append(RowIssue(
            "business_rule", "That date of birth is in the future.",
            "date_of_birth", raw_dob,
        ))
    else:
        # The year is the digit a spreadsheet gets wrong: 1998 for 2008 puts a
        # 28-year-old on a school roll and nothing downstream would question it.
        years = dt.date.today().year - row.date_of_birth.year
        if years < MIN_AGE_YEARS:
            row.issues.append(RowIssue(
                "business_rule",
                f"That would make the student under {MIN_AGE_YEARS} years old. "
                f"Check the year.",
                "date_of_birth", raw_dob,
            ))
        elif years > MAX_AGE_YEARS:
            row.issues.append(RowIssue(
                "business_rule",
                f"That would make the student over {MAX_AGE_YEARS}. Check the year.",
                "date_of_birth", raw_dob,
            ))

    raw_gender = _text(payload, "gender")
    row.gender = _GENDERS.get(raw_gender.lower(), "")
    if not raw_gender:
        row.issues.append(RowIssue("required", "A gender is required.", "gender"))
    elif not row.gender:
        row.issues.append(RowIssue(
            "invalid_choice", f"'{raw_gender}' is not Female or Male.",
            "gender", raw_gender,
        ))

    raw_admitted = _text(payload, "admission_date")
    if raw_admitted:
        row.admission_date = _as_date(raw_admitted)
        if row.admission_date is None:
            row.issues.append(RowIssue(
                "invalid_format",
                f"'{raw_admitted}' is not a date this importer can read.",
                "admission_date", raw_admitted,
            ))
        elif row.admission_date > dt.date.today():
            # The enrol form defaults this to today and never offers a future
            # date, so this is a fault only a file can carry.
            row.issues.append(RowIssue(
                "business_rule", "That admission date is in the future.",
                "admission_date", raw_admitted,
            ))
        elif (
            row.date_of_birth is not None
            and row.admission_date < row.date_of_birth
        ):
            row.issues.append(RowIssue(
                "business_rule",
                "That admission date is before the student was born. One of "
                "the two dates is wrong.",
                "admission_date", raw_admitted,
            ))

    _resolve_number(row, payload, tenant=tenant, policy=policy)
    _resolve_branch(row, payload, tenant=tenant, batch_branch=batch_branch,
                    multi_branch=multi_branch)
    _resolve_class(row, payload, tenant=tenant, session=session)
    _resolve_guardian(row, payload)
    _resolve_duplicate(row, tenant=tenant)
    _check_lengths(row)
    _check_looks_like_a_name(row)
    return row


def _check_lengths(row):
    """Refuse what the column cannot hold, before anything is written."""
    for field, limit in MAX_LENGTHS.items():
        value = getattr(row, field, "") or ""
        if len(value) > limit:
            row.issues.append(RowIssue(
                "invalid_format",
                f"That is {len(value)} characters and the limit is {limit}. "
                f"A value this long is usually two columns that have shifted.",
                field, value[:40],
            ))


def _check_looks_like_a_name(row):
    """A digit in a name is a shifted column, not a name.

    A warning rather than an error: the importer does not get to decide that
    nobody is called "Ade 2". But a whole file whose names carry digits is a
    file whose columns are one to the left, and saying so on row 1 saves the
    school from finding out on row 300.
    """
    for field in ("first_name", "last_name", "guardian_full_name"):
        value = getattr(row, field, "") or ""
        if any(c.isdigit() for c in value):
            row.issues.append(RowIssue(
                "invalid_format",
                f"'{value}' has a digit in it. Check that the columns line up "
                f"with the headings.",
                field, value, severity="warning",
            ))


def _resolve_number(row, payload, *, tenant, policy):
    from .exceptions import StudentsError
    from .models import Student
    from .services.policy import assert_number_allowed

    raw = _text(payload, "student_number")
    row.student_number = raw
    if not raw:
        if policy.required:
            row.issues.append(RowIssue(
                "required",
                policy.hint
                or "This school requires an admission number for every student.",
                "student_number",
            ))
        return
    try:
        assert_number_allowed(tenant, raw, policy=policy)
    except StudentsError as exc:
        row.issues.append(RowIssue(
            "invalid_format", exc.message, "student_number", raw,
        ))
        return
    clash = Student.objects.filter(
        tenant=tenant, student_number__iexact=raw,
    ).first()
    if clash is not None:
        row.issues.append(RowIssue(
            "duplicate_record",
            f"{clash.full_name} already holds {raw} at this school.",
            "student_number", raw,
        ))


def _resolve_branch(row, payload, *, tenant, batch_branch, multi_branch):
    """A student always has a branch. There is no shared row to fall back to."""
    from vs_tenants.models import Branch

    raw = _text(payload, "branch")
    if raw:
        found = Branch.all_objects.filter(tenant=tenant, name__iexact=raw).first()
        if found is None:
            # One answer for a branch that is unknown, malformed or another
            # tenant's, so the column cannot be used to enumerate.
            row.issues.append(RowIssue(
                "not_found",
                f"'{raw}' is not a branch of this school.", "branch", raw,
            ))
            return
        row.branch = found
        return

    if batch_branch is not None:
        row.branch = batch_branch
        return
    if not multi_branch:
        # One branch, so the column is noise and the only branch is the answer.
        row.branch = Branch.all_objects.filter(tenant=tenant).first()
        if row.branch is None:
            row.issues.append(RowIssue(
                "business_rule",
                "This school has no branch to enrol students into.", "branch",
            ))
        return
    row.issues.append(RowIssue(
        "required",
        "This school has more than one branch, so every row needs one.",
        "branch",
    ))


def _resolve_class(row, payload, *, tenant, session):
    """A class the school does not have is a hard error, not a warning.

    A row with no class at all is fine: the student is created ENROLLED and
    unplaced, because a school importing its history often does not know the
    class yet.
    """
    from schools.vs_academics.models import SchoolClass

    raw = _text(payload, "class")
    if not raw:
        return
    if session is None:
        row.issues.append(RowIssue(
            "business_rule",
            "This school has no active session, so no class can be given.",
            "class", raw,
        ))
        return
    found = SchoolClass.objects.filter(
        tenant=tenant, session=session, is_active=True, name__iexact=raw,
    ).select_related("branch", "level").first()
    if found is None:
        row.issues.append(RowIssue(
            "not_found",
            f"'{raw}' does not exist in Academic Structure for this session.",
            "class", raw,
        ))
        return
    # Section 6.3, enforced at the same point as every other rule rather than
    # discovered at execution when half the file is already written.
    if (
        found.branch_id is not None
        and row.branch is not None
        and found.branch_id != row.branch.pk
    ):
        row.issues.append(RowIssue(
            "business_rule",
            f"{found.name} belongs to {found.branch.name}, and this student is "
            f"at {row.branch.name}.",
            "class", raw,
        ))
        return
    row.school_class = found


def _resolve_guardian(row, payload):
    row.guardian_full_name = _text(payload, "guardian_full_name")
    row.guardian_phone = _text(payload, "guardian_phone")
    row.guardian_email = _text(payload, "guardian_email")
    raw_rel = _text(payload, "guardian_relationship")
    row.guardian_relationship = _RELATIONSHIPS.get(
        raw_rel.lower(), Relationship.OTHER,
    )
    if not row.guardian_full_name:
        row.issues.append(RowIssue(
            "required", "Every student needs a guardian's name.",
            "guardian_full_name",
        ))
    if not row.guardian_phone:
        row.issues.append(RowIssue(
            "required", "Every guardian needs a phone number the school can reach.",
            "guardian_phone",
        ))
    elif len(_digits(row.guardian_phone)) < 7:
        # Not a format rule. A value with fewer than seven digits cannot be a
        # number anybody can ring, and the usual cause is a column that has
        # shifted so a class name or a date has landed here.
        row.issues.append(RowIssue(
            "invalid_format",
            f"'{row.guardian_phone}' is not a number the school could ring.",
            "guardian_phone", row.guardian_phone,
        ))

    # **A malformed address is not a cosmetic fault.** Guardians are matched on
    # email, so this column decides which household a child joins, and it is
    # also the address a parent's login would be issued to. A value that is not
    # an address matches nothing, creates a second record for a parent the
    # school already holds, and splits the family.
    if row.guardian_email:
        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email

        try:
            validate_email(row.guardian_email)
        except ValidationError:
            row.issues.append(RowIssue(
                "invalid_format",
                f"'{row.guardian_email}' is not an email address.",
                "guardian_email", row.guardian_email,
            ))

    if raw_rel and raw_rel.lower() not in _RELATIONSHIPS:
        row.issues.append(RowIssue(
            "invalid_choice",
            f"'{raw_rel}' is not a relationship this school records. It will "
            f"be imported as Other.",
            "guardian_relationship", raw_rel, severity="warning",
        ))


def _resolve_duplicate(row, *, tenant):
    """A warning, never an error.

    An import blocked by a real pair of siblings is an import a school cannot
    run, and two children genuinely do share a name and a birthday.
    """
    from .models import Student

    if not (row.first_name and row.last_name and row.date_of_birth):
        return
    row.duplicate = Student.objects.filter(
        tenant=tenant, date_of_birth=row.date_of_birth,
        first_name__iexact=row.first_name, last_name__iexact=row.last_name,
    ).first()


def import_session(tenant):
    from schools.vs_academics.models import AcademicSession, SessionStatus

    return AcademicSession.objects.filter(
        tenant=tenant, status=SessionStatus.ACTIVE,
    ).first()


def create_student_from_row(row: ResolvedRow, *, tenant, session, created_by):
    """Write the student this row describes, through the module's own services.

    Not a bespoke create: the same guardian matching, the same state machine
    and the same placement rules an enrolment uses, so no validation exists in
    two places and an imported student is indistinguishable from a typed one.
    """
    from django.utils import timezone

    from .models import Student
    from .services import guardians as guardian_service
    from .services.placement import place
    from .services.status import transition

    student = Student.objects.create(
        tenant=tenant, branch=row.branch,
        student_number=row.student_number,
        first_name=row.first_name, middle_name=row.middle_name,
        last_name=row.last_name, date_of_birth=row.date_of_birth,
        gender=row.gender, address=row.address,
        previous_school=row.previous_school,
        status=StudentStatus.APPLICANT,
        enrolment_date=row.admission_date or timezone.localdate(),
        created_by=created_by,
    )
    guardian, _ = guardian_service.upsert_guardian(
        tenant, full_name=row.guardian_full_name, phone=row.guardian_phone,
        email=row.guardian_email, address=row.address,
    )
    guardian_service.link(
        student, guardian, relationship=row.guardian_relationship,
        is_primary=True, actor=created_by,
    )
    transition(
        student, StudentStatus.ENROLLED, actor=created_by, system=True,
        reason="Imported from a spreadsheet.",
        effective_date=student.enrolment_date,
    )
    if row.school_class is not None and session is not None:
        # No year passed: place() takes the school's running year and checks
        # the class against it. import_session resolved the same ACTIVE year,
        # so this drops a duplicate rather than changing what is written.
        place(
            student, row.school_class, actor=created_by,
            effective_date=student.enrolment_date, allow_over_capacity=True,
        )
    return student


def _payload_of(raw_row: dict, columns) -> dict:
    """One uploaded row, keyed the way the handler reads it.

    The engine does this at execution time with ``map_row_to_payload``; the
    validator has to do the same translation, so both passes look at the same
    row rather than at the same file read two ways.
    """
    return {c.target_field: raw_row.get(c.column_name) for c in columns}


def validate_students_import_batch(import_batch) -> list[dict]:
    """Every fault in an uploaded roll, in the engine's issue shape.

    Errors block the import; warnings do not. The split follows one rule: what
    is refused is what cannot be written at all, and everything a person might
    legitimately have meant is a warning that names what will happen.
    """
    from .services.policy import read_policy
    from .services.scoping import branch_dimension_applies

    template = import_batch.template
    if template is None:
        return []

    tenant = import_batch.tenant
    session = import_session(tenant)
    columns = list(template.columns.all())
    header = {c.target_field: c.column_name for c in columns}
    multi_branch = branch_dimension_applies(tenant)
    policy = read_policy(tenant)
    rows = import_batch.preview_rows or []

    issues: list[dict] = []
    first_seen: dict[tuple, int] = {}
    numbers_seen: dict[str, int] = {}
    #: Guardian contact -> (row number, the name that row gave). Guardians are
    #: matched on these two columns, so a repeat under a different name is a
    #: household about to be merged.
    contacts_seen: dict[str, tuple[int, str]] = {}
    #: Class -> rows in this file that want a seat in it. The cumulative check
    #: below is the one a manual enrolment cannot reach: typing the thirty-first
    #: child into a class of thirty is refused on the spot, and a file simply
    #: writes all forty.
    wanted_seats: dict[int, list[int]] = {}
    capacity_reported: set[int] = set()

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
            policy=policy,
        )
        for issue in resolved.issues:
            record(row_number, issue)
        if not resolved.ok:
            continue

        # Two rows in one file carrying the same admission number. An error,
        # not a warning: the constraint would refuse the second one anyway, and
        # a school would rather be told which two rows clash than watch half a
        # file import.
        if resolved.student_number:
            key = resolved.student_number.casefold()
            earlier = numbers_seen.get(key)
            if earlier is not None:
                record(row_number, RowIssue(
                    "duplicate_record",
                    f"Row {earlier} already uses {resolved.student_number}.",
                    "student_number", resolved.student_number,
                ))
                continue
            numbers_seen[key] = row_number

        earlier = first_seen.get(resolved.key)
        if earlier is not None:
            record(row_number, RowIssue(
                "duplicate_record",
                f"Row {earlier} has the same name and date of birth. If these "
                f"are twins, give them different rows in a separate file.",
                "first_name", resolved.first_name, severity="warning",
            ))
        else:
            first_seen[resolved.key] = row_number

        if resolved.duplicate is not None:
            record(row_number, RowIssue(
                "duplicate_record",
                f"{resolved.duplicate.full_name} is already on the roll with "
                f"this name and date of birth. This row will still be imported.",
                "first_name", resolved.first_name, severity="warning",
            ))

        # A guardian this file has already named, or the school already holds,
        # under a DIFFERENT name. upsert_guardian matches on email then phone
        # and keeps the name it already has, so the child joins that household
        # and the name in this row is discarded. Usually right - it is how
        # siblings find each other - and occasionally a typo attaching a child
        # to a stranger, which nothing downstream would ever question.
        for field, value in (
            ("guardian_email", resolved.guardian_email.casefold()),
            ("guardian_phone", _digits(resolved.guardian_phone)),
        ):
            if not value:
                continue
            earlier = contacts_seen.get(value)
            if earlier is not None and earlier[1] != resolved.guardian_full_name:
                record(row_number, RowIssue(
                    "duplicate_record",
                    f"Row {earlier[0]} gives the same contact for "
                    f"'{earlier[1]}'. Both children will be attached to that "
                    f"one guardian, and '{resolved.guardian_full_name}' will "
                    f"not be recorded.",
                    field, getattr(resolved, field), severity="warning",
                ))
            elif earlier is None:
                contacts_seen[value] = (row_number, resolved.guardian_full_name)
                held = _guardian_holding(tenant, field, getattr(resolved, field))
                if held is not None and held.full_name != resolved.guardian_full_name:
                    record(row_number, RowIssue(
                        "duplicate_record",
                        f"{held.full_name} already uses that contact at this "
                        f"school, so this student joins their household and "
                        f"'{resolved.guardian_full_name}' will not be recorded.",
                        field, getattr(resolved, field), severity="warning",
                    ))

        if resolved.school_class is None:
            record(row_number, RowIssue(
                "business_rule",
                "No class given, so this student will be enrolled without one "
                "and will appear under Classes and transfers.",
                "class", severity="warning",
            ))
        else:
            wanted = wanted_seats.setdefault(resolved.school_class.pk, [])
            wanted.append(row_number)
            _check_capacity(
                record, resolved.school_class, session, wanted,
                row_number, capacity_reported,
            )

    return issues


def _guardian_holding(tenant, field: str, value: str):
    """The guardian this school already holds on that email or phone."""
    from .models import Guardian

    if not value:
        return None
    if field == "guardian_email":
        return Guardian.objects.filter(tenant=tenant, email__iexact=value).first()
    return Guardian.objects.filter(tenant=tenant, phone=value).first()


def _check_capacity(record, school_class, session, wanted, row_number, reported):
    """Warn once when a file asks a class for more seats than it has.

    **This is the fault a form cannot have.** Enrolling by hand, the thirty-
    first child into a class of thirty is refused on the spot with the seat
    count on screen. A file naming that class forty times passes every row's
    own checks, because no row is wrong on its own, and the school finds out
    when the register will not fit.

    A warning rather than an error, and reported ONCE per class on the row that
    tips it over: the import writes over capacity deliberately, because a school
    loading a roll it already has is describing what is true rather than asking
    permission. What it must not do is stay quiet about it.
    """
    from .services.placement import capacity_state

    if school_class.pk in reported or session is None:
        return
    used, cap, _ = capacity_state(school_class, session, adding=0)
    if cap is None or used + len(wanted) <= cap:
        return
    reported.add(school_class.pk)
    record(row_number, RowIssue(
        "business_rule",
        f"{school_class.name} holds {cap} and already has {used}. This file "
        f"puts {len(wanted)} more in it, so the class will be over its "
        f"capacity. The students will still be imported.",
        "class", school_class.name, severity="warning",
    ))
