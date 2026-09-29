"""Loading a school's existing staff from a spreadsheet.

**Interpretation lives here, not in the engine**, and validation and execution
are two passes over the same file. The way an import goes wrong quietly is the
two passes reading a row differently, so both call :func:`resolve_row` and the
handler writes only what that resolver read.

The engine takes its tenant from the batch and the template carries no school
column, so there is no way for a row to name a different school.

**There is no role column.** Everybody imported starts on the school's starting
role (Settings, Staff), exactly as a person added on the Add form does, whoever
uploads the file: adding people and deciding what they may reach are two jobs,
and the key that imports staff is not the key that assigns roles. A file that
still carries a Role column is not refused; the column is ignored and the
validation says so once (:func:`role_column_note`).

**The same holds while a school is onboarding.** The Add form then grants
School Admin or Branch Admin, picked person by person, because the people it
adds before go-live are the school's first administrators and nobody is there
to review a wider grant. A file has nowhere to make that choice, and onboarding
itself requires a fully imported staff list before data setup can finish, so
the import grants the starting role there too: the baseline every member of
staff gets at a live school without review. The two administrator roles stay
the Add form's. Nobody imported during setup is emailed until the school goes
live (``services/setup_invitations.py``).

A row creates the account, the invitation, the grant and the staff record
through the same service a single add uses. A second creation path would be a
second set of rules about who may be created where, and the two would drift.

FRD M12 v2.1 FR-016.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field as dc_field

from .constants import EmploymentStatus, EmploymentType
from .services.numbers import staff_number_taken

#: The template's columns, in the order a school reads them.
#:
#: ``branch`` is carried even at a single-branch school, where every row leaves
#: it blank and blank means across the whole school. A column that appeared only
#: for some schools would make one template two.
COLUMNS = (
    "first_name",
    "middle_name",
    "last_name",
    "email",
    "phone",
    "gender",
    "staff_number",
    "job_title",
    "employment_type",
    "hire_date",
    "branch",
    "send_invitation",
)

REQUIRED_COLUMNS = ("first_name", "last_name", "email")

#: Headers a school's older file may still carry for the role it no longer sets.
_ROLE_HEADERS = frozenset({"role", "role key", "role_key"})

_RESOLVE = object()

_EMPLOYMENT_TYPES = {label.lower(): code for code, label in EmploymentType.choices}
_EMPLOYMENT_TYPES.update({code.lower(): code for code, _ in EmploymentType.choices})
# The words a school actually types, which are not the enum's.
_EMPLOYMENT_TYPES.update({
    "full time": EmploymentType.FULL_TIME,
    "fulltime": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "parttime": EmploymentType.PART_TIME,
})

#: The words a school types in the Send Invitation column.
#:
#: Yes and No are what the template asks for; the rest are what people write
#: instead. A blank cell is not here because it never reaches this lookup: an
#: empty column means the school has not thought about the question, and the
#: answer to that is today's behaviour rather than a refusal.
_SEND_INVITATION = {
    "yes": True,
    "y": True,
    "true": True,
    "1": True,
    "no": False,
    "n": False,
    "false": False,
    "0": False,
}


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
    email: str = ""
    phone: str = ""
    gender: str = ""
    staff_number: str = ""
    job_title: str = ""
    employment_type: str = ""
    hire_date: dt.date | None = None
    branch: object | None = None
    role: object | None = None
    #: Whether the activation email goes out as part of writing this person.
    #:
    #: True by default, which is what a blank column means: a school that has
    #: never seen the column gets the behaviour it already had. False parks the
    #: invitation instead, leaving a real unsent one for the school to send
    #: when it is ready.
    send_invitation: bool = True
    issues: list = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def key(self) -> str:
        """What makes two rows in one file the same person.

        The email, and only the email. Two members of staff may share a name;
        one address is one account, because that is what the database enforces.
        """
        return self.email.casefold()


def _text(payload: dict, key: str) -> str:
    raw = payload.get(key)
    return "" if raw is None else str(raw).strip()


def _date(value: str):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def starting_role_for_import(tenant, actor=None):
    """``(role, refusal)``: the role every imported person starts on, or why none.

    The Add form's own rule (``services.roles.starting_role``), so the two
    cannot disagree: the school's starting role, refused by its name when it is
    no longer active, and refused when it carries restricted permissions
    *actor* does not hold, because an import is not a way round the grant
    ceiling. The same at a school still onboarding; the module docstring says
    why.
    """
    from rest_framework.exceptions import APIException, ValidationError

    from .services.roles import starting_role

    try:
        return starting_role(tenant, actor=actor), ""
    except ValidationError as exc:
        detail = exc.detail.get("role") if isinstance(exc.detail, dict) else exc.detail
        return None, str(detail[0] if isinstance(detail, list) else detail)
    except APIException as exc:
        return None, str(exc.detail)


def role_column_note(headers, role) -> dict | None:
    """One file-level warning where the file still carries a Role column.

    Not a refusal: a school re-uploading last term's file should not be sent to
    delete a column. The column is never read, because the template does not
    map it, and this says what happens instead.
    """
    if role is None:
        return None
    found = [h for h in headers or () if str(h).strip().casefold() in _ROLE_HEADERS]
    if not found:
        return None
    return {
        "row_number": None,
        "column_name": found[0],
        "value": "",
        "code": "role_column_ignored",
        "message": (
            f"Roles are not imported. Everybody in this file starts as "
            f"{role.name}; give other roles from Roles & Permissions."
        ),
        "severity": "warning",
    }


def resolve_row(payload: dict, *, tenant, batch_branch=None, multi_branch=False,
                actor=None, role=_RESOLVE):
    """Read one row into the values a create would use, with its own reasons.

    Every refusal is a row issue rather than an exception, so a bad row is
    skipped with a reason and the other two hundred still import. The engine's
    own behaviour is that critical issues block the batch and warnings allow it
    after confirmation, and nothing here changes that.

    The role is the school's starting role, from
    :func:`starting_role_for_import` for *actor*: the uploader when the file is
    checked, whoever runs it when it is written. :func:`validate_rows` resolves
    it once for the file and passes it as *role*, reporting a refusal once
    rather than on every row; a row resolved on its own resolves it here and
    carries the refusal as its own issue.
    """
    from vs_tenants.models import Branch
    from vs_user.email_normalization import normalize_email

    row = ResolvedRow(
        first_name=_text(payload, "first_name"),
        middle_name=_text(payload, "middle_name"),
        last_name=_text(payload, "last_name"),
        phone=_text(payload, "phone"),
        gender=_text(payload, "gender").upper(),
        staff_number=_text(payload, "staff_number"),
        job_title=_text(payload, "job_title"),
    )

    for field in ("first_name", "last_name"):
        if not getattr(row, field):
            row.issues.append(RowIssue(
                code="required", field=field,
                message=f"{field.replace('_', ' ').capitalize()} is needed.",
            ))

    raw_email = _text(payload, "email")
    if not raw_email:
        row.issues.append(RowIssue(
            code="required", field="email", message="An email address is needed.",
        ))
    else:
        row.email = normalize_email(raw_email)
        from vs_user.models import User

        if User.objects.filter(tenant=tenant, email__iexact=row.email).exists():
            row.issues.append(RowIssue(
                code="duplicate_email", field="email", value=row.email,
                message=(
                    f"{row.email} is already an account at this school. It may "
                    f"be an account at another school, which is fine and is not "
                    f"this row's problem."
                ),
            ))

    if staff_number_taken(tenant, row.staff_number):
        row.issues.append(RowIssue(
            code="duplicate_staff_number", field="staff_number",
            value=row.staff_number,
            message=f"Somebody at this school already has staff ID {row.staff_number}.",
        ))

    raw_type = _text(payload, "employment_type")
    if raw_type:
        code = _EMPLOYMENT_TYPES.get(raw_type.lower())
        if code is None:
            row.issues.append(RowIssue(
                code="unknown_employment_type", field="employment_type",
                value=raw_type, severity="warning",
                message=(
                    f"'{raw_type}' is not an employment type, so it is left "
                    f"blank. Use Full-time, Part-time, Contract or Volunteer."
                ),
            ))
        else:
            row.employment_type = code

    raw_send = _text(payload, "send_invitation")
    if raw_send:
        decided = _SEND_INVITATION.get(raw_send.lower())
        if decided is None:
            row.issues.append(RowIssue(
                code="unknown_send_invitation", field="send_invitation",
                value=raw_send, severity="warning",
                message=(
                    f"'{raw_send}' is not Yes or No, so this person is "
                    f"invited. Write No to create the account without "
                    f"emailing them yet."
                ),
            ))
        else:
            row.send_invitation = decided

    raw_hire = _text(payload, "hire_date")
    if raw_hire:
        parsed = _date(raw_hire)
        if parsed is None:
            row.issues.append(RowIssue(
                code="bad_date", field="hire_date", value=raw_hire,
                message=f"'{raw_hire}' is not a date. Use YYYY-MM-DD.",
            ))
        else:
            row.hire_date = parsed

    if role is _RESOLVE:
        role, refusal = starting_role_for_import(tenant, actor)
        if refusal:
            row.issues.append(RowIssue(code="starting_role", field="", message=refusal))
    row.role = role

    raw_branch = _text(payload, "branch")
    if raw_branch:
        branch = Branch.all_objects.filter(tenant=tenant).filter(
            name__iexact=raw_branch,
        ).first()
        if branch is None and raw_branch.isdigit():
            branch = Branch.all_objects.filter(
                tenant=tenant, pk=int(raw_branch),
            ).first()
        if branch is None:
            row.issues.append(RowIssue(
                code="unknown_branch", field="branch", value=raw_branch,
                message=f"'{raw_branch}' is not a branch of this school.",
            ))
        elif branch.status not in Branch.IN_SERVICE_STATES:
            row.issues.append(RowIssue(
                code="branch_not_in_service", field="branch", value=raw_branch,
                message=f"{branch.name} is not in service, so nobody can be posted to it.",
            ))
        else:
            row.branch = branch
    elif batch_branch is not None:
        row.branch = batch_branch
    # Blank at a multi-branch school is not an error: across the whole school is
    # a real posting, and a registrar genuinely has it.

    _check_staff_number(row, tenant)
    return row


def _check_staff_number(row, tenant):
    """The staff-number rule for the row's posting, as row issues.

    A blank the rule requires passes where the rule issues numbers and has a
    series to continue, because the write issues one; uniqueness is checked
    above, against the school and against the rest of the file.
    """
    from .services.number_policy import compile_pattern, read_policy, suggest_number

    policy = read_policy(tenant, row.branch)
    if not row.staff_number:
        if policy.required and not (
            policy.auto_issue and suggest_number(tenant, policy=policy, branch=row.branch)
        ):
            row.issues.append(RowIssue(
                code="staff_number_required", field="staff_number",
                message=(
                    policy.hint
                    or "This school requires a staff number for every member of staff."
                ),
            ))
        return
    compiled = compile_pattern(policy.pattern)
    if compiled is not None and not compiled.match(row.staff_number):
        row.issues.append(RowIssue(
            code="staff_number_format", field="staff_number", value=row.staff_number,
            message=policy.hint or "That staff number is not in this school's format.",
        ))


def create_staff_from_row(row: ResolvedRow, *, tenant, created_by, request=None):
    """Write one imported person, through the same services a single add uses.

    Not a bespoke create: the account, the invitation and the grant come from
    ``UserCreationService`` exactly as FR-001's endpoint gets them, and the
    profile comes from the same service the endpoint calls afterwards.

    ``row.send_invitation`` decides only whether the email goes out. A parked
    row still reaches PENDING and still gets a real invitation, so the school
    can send it later from the same resend path that chases anybody else; the
    difference is visible on the staff list as the invitation's email status.

    The school's staff rules apply as they do to a single add: a blank staff
    number is issued where the rule issues numbers, and where the school
    approves each hire the person is written Awaiting approval and submitted
    to the ladder, their invitation created (and emailed, if the row said so)
    only when the hire is approved.

    At a school still onboarding nobody is invited yet: the record reads
    Invited at go-live, and the invitation goes out when the school goes live.
    """
    from vs_user.serializers import UserCreateSerializer
    from vs_user.services.user import UserCreationService

    from .services import creation, hire, number_policy

    # The serializer reads the ACTOR off the request to decide the owning
    # tenant, and an import runs from a queue with no request in flight. The
    # stand-in is the same one import_cx_users_row uses, for the same reason: a
    # None here is a key that exists and an object with no .user behind it.
    from types import SimpleNamespace

    actor_request = request or SimpleNamespace(user=created_by, tenant=tenant)
    account = UserCreateSerializer(
        data={
            "first_name": row.first_name,
            "last_name": row.last_name,
            "email": row.email,
            "phone": row.phone,
            "gender": row.gender,
            "role": row.role.key,
            "branch": str(row.branch.pk) if row.branch else None,
        },
        context={"request": actor_request},
    )
    account.is_valid(raise_exception=True)
    from vs_tenants.models import Tenant

    staff_number = number_policy.settle_number(tenant, row.staff_number, branch=row.branch)
    setup = getattr(tenant, "status", None) == Tenant.Status.PENDING
    held = hire.needs_approval(tenant)
    user = UserCreationService.create_pending(
        account.validated_data, created_by, request=request,
    )
    # Left at PENDING_APPROVAL where the school is not live yet or approves each hire.
    if not (setup or held):
        UserCreationService.finalize_invitation(
            user=user, requested_by=created_by, send_email=row.send_invitation,
        )
    status = (
        EmploymentStatus.AWAITING_GO_LIVE if setup
        else EmploymentStatus.PENDING_APPROVAL if held else None
    )
    profile = creation.create_profile(
        tenant=tenant, user=user, actor=created_by,
        staff_number=staff_number, job_title=row.job_title,
        employment_type=row.employment_type, hire_date=row.hire_date,
        branch=row.branch, middle_name=row.middle_name,
        employment_status=status, invite_on_approval=row.send_invitation,
    )
    if held:
        hire.submit(profile, actor=created_by)
    return profile


def _payload_of(raw_row: dict, columns) -> dict:
    """One uploaded row, keyed the way :func:`resolve_row` reads it.

    **An uploaded row is keyed by the file's HEADERS**, not by the target
    fields: ``{"First Name": "Ifeoma", "Email": "..."}``. The engine translates
    at execution time with ``map_row_to_payload``, and the validator has to do
    the same translation or the two passes look at the same row two different
    ways.

    Without it every lookup misses and every row reports its required fields as
    empty, so a school downloading this template, filling it in and uploading it
    back is told to fix twelve errors in a perfect file - and since errors block
    a batch, nothing can ever be imported at all.
    """
    return {c.target_field: raw_row.get(c.column_name) for c in columns}


def validate_rows(import_batch) -> list[dict]:
    """The validation pass, reading every row through :func:`resolve_row`.

    Also catches what a per-row resolver cannot: the same address twice in one
    file. Two rows that each pass on their own would create one account and then
    fail, so the second is refused here with the first named.

    Two issues belong to the file rather than to a row, and carry no row
    number: a starting role the uploader cannot give, which is an error, and a
    Role column the file still carries, which is a warning that the column is
    ignored.
    """
    from schools.vs_staff.services.scoping import branch_dimension_applies

    template = import_batch.template
    if template is None:
        return []

    tenant = import_batch.tenant
    multi = branch_dimension_applies(tenant)
    columns = list(template.columns.all())
    # Back the other way, for the issue's own label. A reader looking at their
    # spreadsheet is looking for "First Name", not `first_name`.
    header = {c.target_field: c.column_name for c in columns}
    seen: dict[str, int] = {}
    seen_numbers: dict[str, int] = {}
    issues = []

    role, refusal = starting_role_for_import(tenant, import_batch.uploaded_by)
    if refusal:
        issues.append({
            "row_number": None, "column_name": "", "value": "",
            "code": "starting_role", "message": refusal, "severity": "error",
        })
    note = role_column_note(import_batch.uploaded_headers, role)
    if note is not None:
        issues.append(note)

    for number, raw_row in enumerate(import_batch.preview_rows or [], start=1):
        row = resolve_row(
            _payload_of(raw_row, columns), tenant=tenant,
            batch_branch=import_batch.branch, multi_branch=multi,
            actor=import_batch.uploaded_by, role=role,
        )
        for issue in row.issues:
            issues.append({
                "row_number": number,
                "column_name": header.get(issue.field, issue.field),
                "value": issue.value,
                "code": issue.code,
                "message": issue.message,
                "severity": issue.severity,
            })
        if row.email:
            if row.email in seen:
                issues.append({
                    "row_number": number,
                    "column_name": header.get("email", "email"),
                    "value": row.email,
                    "code": "duplicate_in_file",
                    "message": (
                        f"{row.email} is also on row {seen[row.email]}. One "
                        f"address is one account."
                    ),
                    "severity": "error",
                })
            else:
                seen[row.email] = number
        if row.staff_number:
            key = row.staff_number.strip().lower()
            if key in seen_numbers:
                issues.append({
                    "row_number": number,
                    "column_name": header.get("staff_number", "staff_number"),
                    "value": row.staff_number,
                    "code": "duplicate_in_file",
                    "message": (
                        f"Staff ID {row.staff_number} is also on row "
                        f"{seen_numbers[key]}. One ID is one person."
                    ),
                    "severity": "error",
                })
            else:
                seen_numbers[key] = number
    return issues
