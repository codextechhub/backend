"""Who heads an academic department.

A department's head is a member of staff on the school's roll, named on the
department. Other modules ask two questions, through :func:`head_of` (who heads
this department) and :func:`departments_headed_by` (which departments does this
signed-in person head), and never read the column themselves.

**A head who has left no longer heads anything.** The column is not cleared when
a member of staff leaves the roll, because the department did not decide
anything. Both functions therefore answer as if the column were empty for a
head who is off the roll, so a gradebook never routes a sheet to somebody who has
gone.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from schools.vs_staff.constants import OFF_ROLL_STATUSES
from schools.vs_staff.models import StaffProfile

from ..exceptions import HeadRefused
from ..models import Department


def head_of(department):
    """The staff record heading *department*, or None when there is no head or they have left."""
    head = department.head
    if head is None or not head.is_on_roll:
        return None
    return head


def departments_headed_by(user, tenant):
    """The active departments of *tenant* that *user* heads, as a queryset by name."""
    return (
        Department.objects.filter(
            tenant=tenant, is_active=True, head__user=user, head__tenant=tenant,
        )
        .exclude(head__employment_status__in=OFF_ROLL_STATUSES)
        .order_by("name")
    )


def resolve_head(tenant, staff_id):
    """The staff record a caller named as head, refused unless they can hold the post.

    A record of another school answers exactly as one that does not exist
    (404), so the refusal never confirms that an id belongs to someone else. A
    record that has left the roll is refused in words.
    """
    staff = (
        StaffProfile.objects.filter(tenant=tenant, pk=staff_id)
        .select_related("user").first()
    )
    if staff is None:
        raise NotFound("No such member of staff at this school.")
    if not staff.is_on_roll:
        raise HeadRefused(
            f"{display_name(staff)} has left the school's staff roll, so cannot head a "
            f"department. Name somebody who still works here.",
        )
    return staff


def display_name(staff) -> str:
    """A member of staff's first and last name as the account holds them."""
    user = staff.user
    return " ".join(part for part in (user.first_name, user.last_name) if part).strip()
