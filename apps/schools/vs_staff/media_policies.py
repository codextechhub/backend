"""Who may read a staff photograph, or a document held against a person.

There is no default policy: a file whose owning model has registered nothing is
not served at all, because the alternative is that adding a ``FileField``
silently publishes it to every account on the platform.

A staff document is the most sensitive payload this module serves and the one
most likely to be exposed by accident, because a file URL looks like a URL
rather than like a record. Two rules follow and both are asserted as tests.
**A person may always read their own**, whatever permissions they hold: a
teacher with no staff key at all can open the CV she uploaded. And **nobody
reads anybody else's without** ``school.teachers.view``, plus the branch
narrowing the directory itself applies, so a file can never be reachable by a
caller the profile is not.

One widening, for the photograph alone: a colleague who may read the org chart
may see the faces on it. See :func:`_may_read_photo`.
"""
from __future__ import annotations

from core.media import register_policy

from .constants import PERM_ORG_VIEW, PERM_VIEW
from .models import StaffDocument, StaffProfile


def _may_read_staff_file(request, staff) -> bool:
    from vs_rbac.permissions import has_permission

    from .services.scoping import is_self, scope_staff

    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if tenant is None or user is None:
        return False
    if staff.tenant_id != getattr(tenant, "pk", None):
        return False
    # Their own file, before any permission is consulted. A person who cannot
    # read their own record cannot read their own passport photograph either,
    # and that is not a boundary worth keeping.
    if is_self(user, staff):
        return True
    if not has_permission(user, PERM_VIEW, tenant=tenant):
        return False
    # The branch check, made against the same scoped queryset the directory
    # uses, so a photograph cannot be reachable by a caller the profile is not.
    return scope_staff(
        StaffProfile.objects.filter(tenant=tenant, pk=staff.pk), user, tenant,
    ).exists()


def _may_read_document(request, document) -> bool:
    return _may_read_staff_file(request, document.staff)


def _may_read_photo(request, staff) -> bool:
    """A photograph is also readable by anybody who reads the org chart.

    The chart shows every member of staff in the school to every colleague
    holding ``school.organogram.view``, photograph included, whatever branch
    either of them works in. Without this a Lekki teacher would see an Ikeja
    colleague's name on the chart beside a broken image. It widens the
    photograph alone: a document still needs the directory's rule above.
    """
    if _may_read_staff_file(request, staff):
        return True
    from vs_rbac.permissions import has_permission

    tenant = getattr(request, "tenant", None)
    user = getattr(request, "user", None)
    if tenant is None or user is None or staff.tenant_id != getattr(tenant, "pk", None):
        return False
    return has_permission(user, PERM_ORG_VIEW, tenant=tenant)


def register() -> None:
    register_policy(StaffProfile, _may_read_photo)
    register_policy(StaffDocument, _may_read_document)
