"""Which people a caller sees, and how a branch reference is resolved.

**The read here is inclusive, and that is the whole difference from
vs_students.** A student is never shared: ``Student.branch`` is non-null, so
narrowing to the caller's own branches loses nothing. A posting *is* nullable,
and a null means "across the whole school" rather than missing data. Mrs.
Nwankwo the registrar is not at Lekki, she is at the school, and she appears in
every branch's roster; an exclusive narrowing would hide her from every branch
administrator at once.

A caller whose granted branches have all been withdrawn therefore sees the
school-wide people and nobody else, which is a real answer and is exactly
different both from seeing everybody and from seeing nobody. ``WHOLE_TENANT``
and an empty frozenset are not the same value and must not be collapsed.

One rule sits outside the narrowing entirely: **a person always reaches their
own record.** A teacher who holds no staff permission at all opens their own
profile, their own documents and their own leave, and nobody else's.

FRD M12 v2.1 sections 6.3 and 12.1.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.exceptions import NotFound

from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids


def scope_staff(queryset, user, tenant, *, field="branch", include_self=True):
    """Narrow *queryset* to the caller's branches, the school-wide rows and self.

    Inclusive on the null: a school-wide posting belongs to every branch, so a
    branch-bound caller must still see it.
    """
    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return queryset

    scope = Q(**{f"{field}__isnull": True})
    if visible:
        scope |= Q(**{f"{field}_id__in": tuple(sorted(visible))})
    if include_self and getattr(user, "pk", None):
        # Their own row, whatever the narrowing says. A person posted to a
        # branch their grants no longer reach is still themselves.
        scope |= Q(user_id=user.pk)
    return queryset.filter(scope)


def branch_dimension_applies(tenant) -> bool:
    """Whether this school has more than one branch.

    Where it has one the dimension recedes entirely: no posting field in the
    response, no posting filter on the directory, no roster screen, no chip.
    Absent, not greyed out. Nothing about the data changes with it, and the
    controls appear when a second branch opens without a row being rewritten.
    """
    from vs_tenants.models import Branch

    return Branch.all_objects.filter(tenant=tenant).count() > 1


def get_staff_or_404(tenant, user, pk, *, queryset=None, include_self=True):
    """One person, scoped. Another school's or another branch's answers 404.

    Never 403: a 403 confirms the row exists, and a staff id must not be usable
    to learn that somebody works at another school. The message says "no such
    person at this school" rather than "this user does not exist", because the
    same address may legitimately be an account somewhere else.
    """
    from ..models import StaffProfile

    qs = queryset if queryset is not None else StaffProfile.objects.all()
    row = scope_staff(
        qs.filter(tenant=tenant), user, tenant, include_self=include_self,
    ).filter(pk=pk).first()
    if row is None:
        raise NotFound("No such person at this school.")
    return row


def get_own_profile(tenant, user):
    """The caller's own staff record, or None if they have none.

    A school's first administrator is provisioned before this module exists and
    legitimately has no profile, so the absence is an ordinary answer rather
    than an error.
    """
    from ..models import StaffProfile

    if not getattr(user, "pk", None):
        return None
    return StaffProfile.objects.filter(tenant=tenant, user_id=user.pk).first()


def is_self(user, staff) -> bool:
    return bool(getattr(user, "pk", None)) and staff.user_id == user.pk
