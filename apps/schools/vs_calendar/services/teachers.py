"""Who may be put on a timetable, and how this module decides.

**This replaces FRD v3.0.1 section 4.8 entirely, and the reason is not a
disagreement.** That section defines a teacher as a ``vs_user.User`` "whose
``user_type`` is STAFF", quotes the field's help text in full, and argues at
length that a domain read of it does not violate the instruction that it must
never drive authorization. The column does not exist. ``vs_user`` migration
``0008_drop_admin_user_types`` retired the SCHOOL_ADMIN and BRANCH_ADMIN
personas and ``0009_drop_user_type`` dropped the field, and
``vs_user/models.py`` now reads, where the choices used to be: "There is
deliberately no ``UserType``."

**A teacher is therefore a role grant, not a persona.** Specifically: a user of
this tenant, ACTIVE, holding an ACTIVE ``TenantUserRoleAssignment`` to a
``TenantRoleTemplate`` whose ``key`` is ``teacher``. That is the same anchor
``seed_school_permissions`` uses for its own backfill, so the two cannot drift.

Three things the FRD parks as open questions this answers, which is why the
replacement is an improvement rather than a workaround:

* Its section 3.5 says a STAFF filter "omits exactly the people a Nigerian
  private school is most likely to have teaching alongside an administrative
  title: the principal, the vice-principal and the heads of department", and
  parks it as decision 17. A role grant is additive, so the principal who takes
  SSS3 Further Maths on Wednesdays holds the teacher role beside her admin one
  and appears.
* Its section 3.5 says ``User.branch`` is one foreign key and cannot record
  that a person teaches at two branches, and parks it as decision 20.
  ``TenantUserRoleAssignment`` can: the same role at two branches is two active
  rows, and its unique constraints are split precisely so both are storable.
* Its FR-013 argues the teacher picker must be tenant-wide *because*
  ``User.branch`` names only one. That argument is gone and the conclusion
  survives for a better reason: the picker is tenant-wide because the clash
  query is, and ``services.clashes`` is what makes it safe.

**What this still gets wrong, stated rather than hidden.** A teacher who has not
been given a login does not appear, and neither does one whose role assignment
nobody made. Both are real at a Nigerian secondary school. This is good enough
to build a timetable on and it is not a staffing register; M12 owns that, and
when it lands this module changes one function.

**What it must never grow.** No specialism, no availability, no qualification,
no maximum load, no suggested-teacher ranking, and no workload figure carrying a
threshold. Nothing in the platform records any of them, so every one would be
the API inventing a check the server cannot make - and an administrator would
trust it. A teacher is serialised as an id and a display name, never as an email
address, which matters more here than anywhere: a class timetable is the most
widely read document a school produces.
"""
from __future__ import annotations

from django.db.models import Q

#: The prebuilt role key that means "this person stands in front of a class".
#: ``core.management.commands.seed_school_permissions.ROLE_TEACHER`` is the same
#: string, and its backfill scans tenant role templates for exactly this key.
TEACHER_ROLE_KEY = "teacher"


def teaching_user_ids(tenant) -> set:
    """The ids of every person who may be put on this school's timetable."""
    from vs_rbac.models import TenantUserRoleAssignment

    return set(
        TenantUserRoleAssignment.objects.filter(
            tenant=tenant,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            role__key=TEACHER_ROLE_KEY,
        )
        .values_list("user_id", flat=True),
    )


def teaching_users(tenant):
    """Every teacher of this school, as a queryset ordered by display name.

    Deliberately **not** narrowed by branch. Narrowing it would look like
    tightening security and would break the case the design exists to show:
    Mr Eze teaches Physics at Lekki on Monday to Wednesday and at Ikeja on
    Thursday and Friday, and a picker filtered by the room's branch would make
    him unschedulable at the second one. What makes the wide picker safe is that
    the clash query is wide too.

    Whether this caller may write this timetable at all is a different question,
    answered before the picker is ever rendered, by ``academics.timetable.*``.
    """
    from vs_user.models import User

    return (
        User.objects.filter(
            tenant=tenant,
            status=User.Status.ACTIVE,
            id__in=teaching_user_ids(tenant),
        )
        .order_by("first_name", "last_name", "id")
    )


def assert_is_teacher(tenant, user):
    """Refuse a person who is not a teacher at this school.

    ``None`` passes: a slot may be saved without a teacher while a grid is
    being built, and the publish gate is what refuses the gap.
    """
    from ..exceptions import NotATeachingUser
    from vs_user.models import User

    if user is None:
        return None
    if getattr(user, "tenant_id", None) != tenant.id:
        # Another tenant's user is not "not a teacher", it is not visible at
        # all - but the caller must not be able to tell those apart, or the
        # endpoint becomes a way to probe another school's user ids.
        raise NotATeachingUser()
    if user.status != User.Status.ACTIVE:
        raise NotATeachingUser(
            f"{display_name(user)}'s account is not active, so they cannot be "
            f"put on a timetable.",
        )
    if user.id not in teaching_user_ids(tenant):
        raise NotATeachingUser()
    return user


def display_name(user) -> str:
    """What a timetable calls a person. Never an email address."""
    if user is None:
        return ""
    full = (getattr(user, "full_name", "") or "").strip()
    return full or f"User {user.pk}"
