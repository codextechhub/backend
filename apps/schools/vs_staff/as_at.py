"""A staff profile as it stood at the end of an earlier day.

Every read behind the profile accepts ``?as_at=YYYY-MM-DD`` and answers in the
same shape as the live read. The record, the account, the postings, the role
grants, the permission exceptions, the qualifications, the documents, the
teaching assignments and the leave requests are each rebuilt from
:mod:`vs_history` and rendered through the live serializers, so Field Access
applies to the past exactly as it does to the present.

What a past view shows differently
----------------------------------

* Anything worked out from "today" (tenure, whether somebody is on leave,
  whether an approved absence reads Completed) is worked out from the chosen
  day instead.
* An account reads its stored status. A sign-in lockout is a running window
  rather than a recorded state, so none is laid over a past day.
* A branch, class, subject or role is named as it is named today.
* A document or photograph replaced since is shown without a link, because its
  file was retired when it was replaced.
* The invitation details are the invitation as it stands, which records when
  it was sent rather than changing afterwards.

Which person a caller may open is decided by the live record, as for every
other read, so a past view never widens what a caller sees.
"""
from __future__ import annotations

from vs_history.as_at import (
    AsAt,
    history_starts,
    instance_at,
    instances_at,
    require_history,
)
from vs_history.registry import install_related, spec_for

from .history import STAFF
from .models import LeaveRequest, StaffDocument, StaffProfile, StaffQualification, TeachingAssignment

USER = "vs_user.user"


def staff_history_starts(staff_pk):
    return history_starts(spec_for(StaffProfile), staff_pk)


def _live_file_names(model, rows, attr="file") -> dict:
    ids = [row.pk for row in rows]
    if not ids:
        return {}
    return dict(model.all_objects.filter(pk__in=ids).values_list("pk", attr))


def grants_at(user_pk, as_at: AsAt) -> list:
    """The person's role grants as they stood, with their roles loaded."""
    from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

    rows = instances_at(spec_for(TenantUserRoleAssignment), USER, user_pk, as_at)
    roles = TenantRoleTemplate._base_manager.prefetch_related(
        "additional_branches",
    ).in_bulk({row.role_id for row in rows if row.role_id})
    for row in rows:
        if row.role_id in roles:
            row.role = roles[row.role_id]
    rows.sort(key=lambda row: row.assigned_at, reverse=True)
    return [row for row in rows if row.role_id in roles]


def overrides_at(user_pk, as_at: AsAt) -> list:
    """The person's permission exceptions as they stood, newest first."""
    from vs_rbac.models import Permission, UserPermissionOverride

    rows = instances_at(spec_for(UserPermissionOverride), USER, user_pk, as_at)
    permissions = Permission.objects.in_bulk({row.permission_id for row in rows})
    for row in rows:
        row.permission = permissions.get(row.permission_id)
    rows = [row for row in rows if row.permission is not None]
    rows.sort(key=lambda row: row.permission.key)
    return rows


def account_at(user, as_at: AsAt):
    """The account as it stood, carrying its grants then and no running lockout."""
    from vs_user.models import User

    past = instance_at(spec_for(User), user.pk, as_at)
    if past is None:
        return None
    for name in ("password", "last_login", "last_login_at", "password_changed_at"):
        setattr(past, name, getattr(user, name, None))
    past.tenant_id = user.tenant_id
    # Present and empty, so ``account_state`` reads the stored status.
    past._state.fields_cache["lockout"] = None
    install_related(past, "tenant_role_assignments", grants_at(user.pk, as_at))
    return past


def children_at(staff_pk, as_at: AsAt) -> dict:
    """Every list hanging off the profile, as it stood."""
    return {
        "qualifications": instances_at(spec_for(StaffQualification), STAFF, staff_pk, as_at),
        "documents": instances_at(spec_for(StaffDocument), STAFF, staff_pk, as_at),
        "teaching_assignments": instances_at(spec_for(TeachingAssignment), STAFF, staff_pk, as_at),
        "leave_requests": instances_at(spec_for(LeaveRequest), STAFF, staff_pk, as_at),
    }


def running_leave(leave_rows, as_at: AsAt):
    """The approved leave covering the chosen day, if any."""
    from .constants import LeaveStatus

    return next(
        (
            row for row in leave_rows
            if row.status == LeaveStatus.APPROVED and row.start_date <= as_at.date <= row.end_date
        ),
        None,
    )


def session_on(tenant, as_at: AsAt):
    """The school year the chosen day falls in, if any."""
    from schools.vs_academics.models import AcademicSession

    return (
        AcademicSession.all_objects.filter(
            tenant=tenant, start_date__lte=as_at.date, end_date__gte=as_at.date,
        ).first()
    )


def staff_at(staff, as_at: AsAt):
    """*staff* rebuilt as at *as_at*, ready for the profile's serializer.

    Returns ``(record, children, meta)``. Raises
    :class:`vs_history.as_at.HistoryNotKept` when the history starts later.
    """
    from vs_tenants.models import Branch

    spec = spec_for(StaffProfile)
    name = " ".join(p for p in (staff.user.first_name, staff.user.last_name) if p)
    starts = require_history(spec, staff.pk, as_at, noun=f"{name}'s record")
    version = instance_at(spec, staff.pk, as_at)
    record = version
    record.updated_at = version._history_version.recorded_at
    record.user = account_at(staff.user, as_at) or staff.user
    postings = version._history_version.data.get("additional_postings", [])
    install_related(
        record, "additional_postings",
        list(Branch.all_objects.filter(pk__in=postings).order_by("name")),
    )
    children = children_at(staff.pk, as_at)
    for related_name, rows in children.items():
        install_related(record, related_name, rows)

    leave = running_leave(children["leave_requests"], as_at)
    record.is_on_leave = leave is not None
    record.on_leave_until = leave.end_date if leave else None
    session = session_on(staff.tenant, as_at)
    record.teaching_load = sum(
        1 for row in children["teaching_assignments"]
        if session is not None and row.session_id == session.pk
    )

    photo_retired = bool(record.photo.name) and record.photo.name != (staff.photo.name or "")
    if photo_retired:
        record.photo = None
    meta = {
        "date": as_at.date.isoformat(),
        "history_starts": starts.isoformat(),
        "photo_retired": photo_retired,
    }
    return record, children, meta


def days_taken_at(leave_rows) -> list[dict]:
    """Days taken per leave type, counted from the approved requests then held.

    The same answer :func:`services.leave.days_taken` gives for the live rows.
    """
    from .constants import LeaveStatus

    totals: dict[str, int] = {}
    for row in leave_rows:
        if row.status == LeaveStatus.APPROVED:
            totals[row.leave_type] = totals.get(row.leave_type, 0) + (row.days or 0)
    return [{"leave_type": kind, "days": days} for kind, days in sorted(totals.items())]


def documents_with_retired(rows) -> tuple[list, set]:
    """The documents, and the ids of those whose file was replaced since."""
    live = _live_file_names(StaffDocument, rows)
    retired = {row.pk for row in rows if live.get(row.pk) != row.file.name}
    return rows, retired
