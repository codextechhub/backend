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


def caller_manages(user, tenant, staff, *, visible=None, resolved=False) -> bool:
    """Whether *user* may change *staff*'s record, not merely read it.

    A whole-tenant caller manages everybody they can see. A branch-bound caller
    manages only people whose every posting sits inside their own branches.
    School-wide people, and people also posted to a branch the caller does not
    cover, stay visible to them but read-only: the registrar at the school is
    relied on by every branch, and a correction made from Ikeja reaches Lekki's
    screens without Lekki's administrator knowing.

    A person always manages their own record, subject to the self-edit rules
    each endpoint already applies.

    ``visible`` and ``resolved`` let a list pass the caller's scope once rather
    than resolving it per row.
    """
    from vs_rbac.scoping import caller_may_change

    if getattr(user, "pk", None) and staff.user_id == user.pk:
        return True
    if not resolved:
        return caller_may_change(user, tenant, staff.posting_branch_ids)
    return caller_may_change(user, tenant, staff.posting_branch_ids, visible=visible)


def assert_manages(user, tenant, staff) -> None:
    """Refuse a write to a record the caller may read but not change."""
    from ..exceptions import SharedRecordReadOnly

    if not caller_manages(user, tenant, staff):
        raise SharedRecordReadOnly()


def guard_postings(user, tenant, branches, *, default_when_unset=False):
    """The postings a caller may write, from the branches they asked for.

    Returns the list to write. A whole-tenant caller gets back exactly what they
    asked for, including an empty list for school-wide. A branch-bound caller:

    * naming only branches they cover gets those branches;
    * naming a branch outside their set is refused;
    * naming none (school-wide) is refused, because a school-wide posting puts
      the person on every branch's roster, which is not theirs to decide. On a
      create, ``default_when_unset`` files the person under the caller's own
      branch instead when they cover exactly one, which is what "everything a
      branch administrator does is registered to their branch" means.
    """
    from ..exceptions import BranchOutsideReach

    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return list(branches)
    if not branches:
        if default_when_unset and len(visible) == 1:
            from vs_tenants.models import Branch

            sole = Branch.all_objects.filter(
                tenant=tenant, pk=next(iter(visible)),
            ).first()
            if sole is not None:
                return [sole]
        raise BranchOutsideReach(
            "Only a school-wide administrator can post somebody school-wide. "
            "Choose one of your branches."
        )
    outside = [branch.name for branch in branches if branch.pk not in visible]
    if outside:
        raise BranchOutsideReach(
            f"{', '.join(outside)} {'is' if len(outside) == 1 else 'are'} not "
            f"one of your branches, so you cannot post staff there."
        )
    return list(branches)


def viewer_sees_branches(user, tenant) -> bool:
    """Whether the posting dimension is shown to this viewer at all.

    True only where the school runs more than one branch AND the viewer works in
    more than one of them. A branch administrator at Ikeja sees Ikeja's people
    and the school-wide ones; which of the two a row is changes nothing they can
    do there, so no Posted to column, field or filter is drawn for them.
    """
    if not branch_dimension_applies(tenant):
        return False
    visible = visible_branch_ids(user, tenant)
    return visible is WHOLE_TENANT or len(visible) > 1


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
