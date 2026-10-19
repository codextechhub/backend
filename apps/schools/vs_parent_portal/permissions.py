"""Who may use the parent portal: a parent with a guardian record, and nobody else.

A parent's account is an ordinary ``vs_user.User`` that holds no role and no
permission key. What it reaches is decided here, on every request, from the
link between the signed-in account and a guardian record
(``Guardian.user``) at the request's school, and from the guardian's links to
children on the roll. Nothing about the parent or the children is stamped into
the token, so unlinking a guardian, or a child leaving the roll, ends the reach
on the very next request.

Being a parent is having a guardian record, never a kind of account. A teacher
whose own child attends the school is admitted exactly as a parent-only account
is, and keeps every staff route she already had.

A caller with no guardian record gets 404, not 403: "there is nothing here for
you" is also the truth for a member of staff who opens a portal address. A
child that is not the caller's, and a child that does not exist, answer with
one and the same 404, so an address cannot be used to learn which children
exist.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission

from vs_rbac.permissions import IsAuthenticatedAndActive


class NotAParent(NotFound):
    """The portal's one refusal, carried with a code the client can branch on."""

    error_code = "NOT_A_PARENT"
    message = "There is no guardian record for this account at this school."
    http_status = 404
    default_detail = message


def linked_guardian(request):
    """The guardian record the signed-in account belongs to at this school, or None.

    Cached on the request, so a view and its permission ask once.
    """
    if hasattr(request, "_portal_guardian"):
        return request._portal_guardian
    from schools.vs_students.models import Guardian

    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    guardian = None
    if tenant is not None and getattr(user, "is_authenticated", False):
        guardian = (
            Guardian.objects.select_related("tenant")
            .filter(tenant=tenant, user=user)
            .first()
        )
    request._portal_guardian = guardian
    return guardian


def my_children(guardian) -> list:
    """The guardian's children who are on the roll, eldest first.

    Each is a ``Student`` of the guardian's school, linked through
    ``StudentGuardian``, whose status is one the portal serves
    (``PORTAL_STATUSES``). The branch, the school and the passport photograph
    are loaded with them, and each carries ``parent_relationship``, the
    guardian's relationship to that child as the school words it, so a list of
    any length costs the same number of queries.
    """
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import Student, StudentGuardian
    from schools.vs_students.services.documents import photo_prefetch

    children = list(
        Student.objects.filter(
            tenant=guardian.tenant_id, status__in=PORTAL_STATUSES,
            guardian_links__guardian=guardian,
        )
        .select_related("branch", "tenant")
        .prefetch_related(photo_prefetch())
        .order_by("date_of_birth", "pk")
    )
    labels = {
        link.student_id: link.relationship_label
        for link in StudentGuardian.objects.filter(
            guardian=guardian, student__in=[c.pk for c in children],
        )
    }
    for child in children:
        child.parent_relationship = labels.get(child.pk, "")
    return children


def children_of(request) -> list:
    """The signed-in parent's children, read once for the request."""
    if not hasattr(request, "_portal_children"):
        guardian = linked_guardian(request)
        request._portal_children = my_children(guardian) if guardian is not None else []
    return request._portal_children


def child_of(request, child_id):
    """One of the signed-in parent's children, or a 404 that says nothing more.

    Another family's child and an id that does not exist answer identically.
    """
    for child in children_of(request):
        if child.pk == child_id:
            return child
    raise NotFound("No such child for you.")


class IsLinkedGuardian(BasePermission):
    """Admit the guardian the signed-in account is linked to; 404 for anybody else."""

    def has_permission(self, request, view):
        if linked_guardian(request) is None:
            raise NotAParent()
        return True


#: What every portal view declares. Order matters: an anonymous or closed
#: account is refused as such before the link is looked for.
PORTAL_PERMISSIONS = [IsAuthenticatedAndActive, IsLinkedGuardian]
