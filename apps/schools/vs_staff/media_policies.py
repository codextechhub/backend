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
caller the profile is not. A document is held to the key its own tab is read
under, ``school.staff_records.view``, not the directory's: a role that lists
staff but may not read their records cannot open a CV or a passport scan by its
link either.

Two widenings, each following the profile a reader can already open. The
photograph is on the contact card, which everybody working at the school reads,
and a colleague who may read the org chart sees the faces on it: see
:func:`_may_read_photo`. A document is readable by a reader the school's profile
policy shows qualifications and documents to, such as a line manager: see
:func:`_may_read_document`. Neither widens past the person the relationship
covers (``services/visibility.py``).
"""
from __future__ import annotations

from core.media import register_policy

from .constants import PERM_ORG_VIEW, PERM_RECORDS_VIEW, PERM_VIEW
from .models import StaffDocument, StaffProfile


def _may_read_staff_file(request, staff, key=PERM_VIEW) -> bool:
    """The person themselves, or a holder of *key* whose branches reach them."""
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
    if not has_permission(user, key, tenant=tenant):
        return False
    # The branch check, made against the same scoped queryset the directory
    # uses, so a photograph cannot be reachable by a caller the profile is not.
    return scope_staff(
        StaffProfile.objects.filter(tenant=tenant, pk=staff.pk), user, tenant,
    ).exists()


def _granted_by_relationship(request, staff, group) -> bool:
    """Whether the school's profile policy shows *group* of *staff* to this reader."""
    from .services.visibility import profile_access

    tenant = getattr(request, "tenant", None)
    if tenant is None or staff.tenant_id != getattr(tenant, "pk", None):
        return False
    return group in profile_access(request, staff, tenant).granted


def _may_read_document(request, document) -> bool:
    """The Documents tab's rule, or the records the reader's relationship is shown."""
    from .services.visibility import GROUP_RECORDS

    return _may_read_staff_file(
        request, document.staff, PERM_RECORDS_VIEW,
    ) or _granted_by_relationship(
        request, document.staff, GROUP_RECORDS,
    )


def _may_read_photo(request, staff) -> bool:
    """A photograph is also readable by anybody who reads the org chart.

    The chart shows every member of staff in the school to every colleague
    holding ``school.organogram.view``, photograph included, whatever branch
    either of them works in. Without this a Lekki teacher would see an Ikeja
    colleague's name on the chart beside a broken image. It is readable too by
    anybody the person's contact card is shown to, which is every colleague at
    the school.
    """
    from .services.visibility import GROUP_CONTACT

    if _may_read_staff_file(request, staff):
        return True
    if _granted_by_relationship(request, staff, GROUP_CONTACT):
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
