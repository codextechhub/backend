"""Who may use the pupil portal: the pupil a record is linked to, and nobody else.

A pupil's account holds no role and no permission key. What it reaches is
decided here, on every request, from the link between the signed-in account and
a pupil record (``Student.user``): the record must belong to the request's
school and the pupil must be on the roll. Nothing about the pupil is stamped
into the token, so taking access away, or the pupil leaving, ends their reach
on the very next request.

A caller who is not a linked pupil gets 404, not 403. The portal names no
pupil in its routes, so a 404 says only "there is nothing here for you", which
is also the truth for a member of staff who opens a portal address.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission

from vs_rbac.permissions import IsAuthenticatedAndActive


class NotAStudent(NotFound):
    """The portal's one refusal, carried with a code the client can branch on."""

    error_code = "NOT_A_STUDENT"
    message = "There is no pupil record for this account at this school."
    http_status = 404
    default_detail = message


def linked_student(request):
    """The pupil record the signed-in account belongs to, or None.

    Cached on the request, so a view and its permission ask once.
    """
    if hasattr(request, "_portal_student"):
        return request._portal_student
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import Student

    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    student = None
    if tenant is not None and getattr(user, "is_authenticated", False):
        student = (
            Student.objects.select_related("branch", "tenant")
            .filter(tenant=tenant, user=user, status__in=PORTAL_STATUSES)
            .first()
        )
    request._portal_student = student
    return student


class IsLinkedStudent(BasePermission):
    """Admit the pupil the signed-in account is linked to; 404 for anybody else."""

    def has_permission(self, request, view):
        if linked_student(request) is None:
            raise NotAStudent()
        return True


#: What every portal view declares. Order matters: an anonymous or closed
#: account is refused as such before the link is looked for.
PORTAL_PERMISSIONS = [IsAuthenticatedAndActive, IsLinkedStudent]
