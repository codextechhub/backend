"""Which students a caller sees, and which branch a student they write belongs to.

**The read here is exclusive, and that is the whole difference from
vs_academics.** A catalogue's shared rows are most of it, so narrowing a level
list to the caller's own branches would empty the screen; academics is
inclusive for that reason. A student is never shared: ``Student.branch`` is
non-null, so there is no school-wide student to add back in, and a caller
pinned to Ikeja must see Ikeja's children and no others.

The consequence of that difference is the one worth stating: a caller whose
granted branches have all been withdrawn sees **no students**, which is a real
answer, not an error, and is exactly different from seeing every student.
``WHOLE_TENANT`` and an empty frozenset are not the same value and must not be
collapsed.

FRD M11 v2.4 sections 6.2 and 6.3.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
from vs_tenants.references import resolve_branch_reference

from ..exceptions import BranchScopeConflict


def scope_students(queryset, user, tenant, field="branch"):
    """Narrow *queryset* to the caller's own branches. Exclusive: no null term."""
    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return queryset
    if not visible:
        return queryset.none()
    return queryset.filter(**{f"{field}_id__in": tuple(sorted(visible))})


def scope_classes(queryset, user, tenant):
    """Narrow a *class* queryset - inclusive, because a class may be shared.

    A school-wide class has a null branch and belongs to every branch, so a
    branch-bound caller must still see it. This is M13's rule and this module
    reads through it rather than reimplementing it.
    """
    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return queryset
    return queryset.filter(
        Q(branch__isnull=True) | Q(branch_id__in=tuple(sorted(visible))),
    )


#: The caller did not mention a branch at all - distinct from naming none.
UNSET = object()


def branch_for_write(user, tenant, requested=UNSET, *, field="branch"):
    """The branch a student this caller creates belongs to.

    There is no null answer. A student always has a branch, so a caller who
    cannot supply one is refused rather than given a shared row: "the whole
    school" is not a place a child attends.
    """
    from vs_tenants.models import Branch

    branch = (
        resolve_branch_reference(tenant, requested, field)
        if requested not in (UNSET, None, "")
        else None
    )
    visible = visible_branch_ids(user, tenant)

    if branch is not None:
        if visible is not WHOLE_TENANT and branch.id not in (visible or ()):
            raise ValidationError({field: "You cannot enrol a student in that branch."})
        return branch

    if visible is WHOLE_TENANT:
        owned = list(Branch.all_objects.filter(tenant=tenant)[:2])
        if len(owned) == 1:
            # The single-branch case: the dimension recedes, so the screen
            # never asked and the only branch is the answer.
            return owned[0]
        raise ValidationError({
            field: "This school has more than one branch, so say which one.",
        })

    if not visible:
        raise PermissionDenied(
            "Your access to every branch has been withdrawn, so you cannot "
            "enrol anyone. Ask a school administrator to restore it.",
        )
    if len(visible) == 1:
        return Branch.all_objects.filter(
            tenant=tenant, pk=next(iter(visible)),
        ).first()
    raise ValidationError({
        field: "You work in more than one branch, so say which one this student joins.",
    })


def assert_class_reachable(student_branch, school_class):
    """A child may join a school-wide class, or one at their own branch.

    Deliberately a *different* refusal from the 404 a class the caller cannot
    see gets. A class they cannot see does not exist as far as they are
    concerned; a class they can see but this child may not join is a rule they
    are entitled to be told about. There is no override.
    """
    if school_class.branch_id is None:
        return
    if school_class.branch_id == student_branch.id:
        return
    raise BranchScopeConflict(
        f"{school_class.name} belongs to {school_class.branch.name}, and this "
        f"student is at {student_branch.name}. Move the student's branch or "
        f"pick a class at {student_branch.name}.",
        student_branch=student_branch.name,
        class_branch=school_class.branch.name,
    )


def branch_dimension_applies(tenant) -> bool:
    """Whether this school has more than one branch.

    Where it has one the dimension recedes entirely: no branch field in the
    response, no branch filter on a list, no chip. Absent, not greyed out.
    Nothing about the data changes with it, and the controls appear when a
    second branch opens without a row being rewritten.
    """
    from vs_tenants.models import Branch

    return Branch.all_objects.filter(tenant=tenant).count() > 1


def get_student_or_404(tenant, user, pk, *, queryset=None):
    """One student, scoped. Another tenant's or another branch's answers 404.

    Never 403: a 403 confirms the row exists, and a student id must not be
    usable to learn that a child exists at another school.
    """
    from ..models import Student

    qs = queryset if queryset is not None else Student.objects.all()
    row = scope_students(qs.filter(tenant=tenant), user, tenant).filter(pk=pk).first()
    if row is None:
        raise NotFound("No such student at this school.")
    return row


def get_guardian_or_404(tenant, pk, *, queryset=None):
    """One guardian, scoped to the tenant.

    Guardian carries no branch, so there is no branch narrowing here - the
    narrowing is on the *wards* shown against them. This is the route whose
    object is reachable from more than one student, so the tenant check cannot
    be inherited from a student in the URL and is made explicitly.
    """
    from ..models import Guardian

    qs = queryset if queryset is not None else Guardian.objects.all()
    row = qs.filter(tenant=tenant, pk=pk).first()
    if row is None:
        raise NotFound("No such guardian at this school.")
    return row
