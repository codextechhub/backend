"""Loading a school's existing staff from a spreadsheet.

**Interpretation lives here, not in the engine**, and validation and execution
are two passes over the same file. The way an import goes wrong quietly is the
two passes reading a row differently, so both call :func:`resolve_row` and the
handler writes only what that resolver read.

The engine takes its tenant from the batch and the template carries no school
column, so there is no way for a row to name a different school. The role column
is a role key **inside this school**, resolved against this tenant's own
catalogue, so a platform role key is not resolvable and a row cannot make
somebody a CodeX hire.

A row creates the account, the invitation, the grant and the staff record
through the same service a single add uses. A second creation path would be a
second set of rules about who may be created where, and the two would drift.

FRD M12 v2.1 FR-016.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field as dc_field

from .constants import EmploymentType

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
    "role",
)

REQUIRED_COLUMNS = ("first_name", "last_name", "email", "role")

_EMPLOYMENT_TYPES = {label.lower(): code for code, label in EmploymentType.choices}
_EMPLOYMENT_TYPES.update({code.lower(): code for code, _ in EmploymentType.choices})
# The words a school actually types, which are not the enum's.
_EMPLOYMENT_TYPES.update({
    "full time": EmploymentType.FULL_TIME,
    "fulltime": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "parttime": EmploymentType.PART_TIME,
})


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


def resolve_row(payload: dict, *, tenant, batch_branch=None, multi_branch=False):
    """Read one row into the values a create would use, with its own reasons.

    Every refusal is a row issue rather than an exception, so a bad row is
    skipped with a reason and the other two hundred still import. The engine's
    own behaviour is that critical issues block the batch and warnings allow it
    after confirmation, and nothing here changes that.
    """
    from vs_rbac.models import TenantRoleTemplate
    from vs_tenants.models import Branch
    from vs_user.email_normalization import normalize_email

    from .models import StaffProfile

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

    if row.staff_number and StaffProfile.objects.filter(
        tenant=tenant, staff_number=row.staff_number,
    ).exists():
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

    # A role is required and there is no invite-now-decide-later. A row naming
    # a role this school does not have is a hard error rather than a warning:
    # importing somebody with no role would create an account that can sign in
    # and reach nothing.
    raw_role = _text(payload, "role")
    if not raw_role:
        row.issues.append(RowIssue(
            code="required", field="role", message="A role is needed for every row.",
        ))
    else:
        role = TenantRoleTemplate.objects.filter(
            tenant=tenant, status="ACTIVE",
        ).filter(key=raw_role).first()
        if role is None:
            role = TenantRoleTemplate.objects.filter(
                tenant=tenant, status="ACTIVE", name__iexact=raw_role,
            ).first()
        if role is None:
            row.issues.append(RowIssue(
                code="unknown_role", field="role", value=raw_role,
                message=(
                    f"'{raw_role}' is not a role at this school. Build it in "
                    f"access control first, then import this row."
                ),
            ))
        else:
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

    return row


def create_staff_from_row(row: ResolvedRow, *, tenant, created_by, request=None):
    """Write one imported person, through the same services a single add uses.

    Not a bespoke create: the account, the invitation and the grant come from
    ``UserCreationService`` exactly as FR-001's endpoint gets them, and the
    profile comes from the same service the endpoint calls afterwards.
    """
    from vs_user.serializers import UserCreateSerializer
    from vs_user.services.user import UserCreationService

    from .services import creation

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
    user = UserCreationService.create_pending(
        account.validated_data, created_by, request=request,
    )
    # Same reason the single add does it: PENDING_APPROVAL is the platform
    # hiring workflow's state, and a school approves nobody.
    UserCreationService.finalize_invitation(user=user, requested_by=created_by)
    return creation.create_profile(
        tenant=tenant, user=user, actor=created_by,
        staff_number=row.staff_number, job_title=row.job_title,
        employment_type=row.employment_type, hire_date=row.hire_date,
        branch=row.branch, middle_name=row.middle_name,
    )


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
    issues = []

    for number, raw_row in enumerate(import_batch.preview_rows or [], start=1):
        row = resolve_row(
            _payload_of(raw_row, columns), tenant=tenant,
            batch_branch=import_batch.branch, multi_branch=multi,
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
    return issues
