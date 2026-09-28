"""Guardians: matching, linking, and the one-primary rule.

The single most load-bearing thing here is that **a guardian is a person, not a
login**. The record is tenant-scoped and carries no branch, so one row serves
siblings at two branches of one school; the *account*, where there is one, is an
ordinary ``vs_user.User`` of this tenant which may already hold a staff role.

The matching rule is what makes siblings work. Adding a guardian whose email or
phone already exists in this school links the existing row and never creates a
second - without it, adding the second sibling silently splits the family in
two and it stays split. A school that matches on email only
(``guardians.matching``) never joins two families on a shared phone.

The school's other guardian rules are enforced here too, at the one place
every write path passes through: how many guardians a child needs, and whether
a new guardian needs an email (``services/guardian_rules.py``).

FRD M11 v2.4 FR-005 and FR-021.
"""
from __future__ import annotations

from core.search import search as core_search
from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import ON_ROLL, GuardianMatching
from ..exceptions import GuardianRequired, PrimaryGuardianRequired
from ..models import Guardian, StudentGuardian
from .guardian_rules import read_guardian_rules

#: The refusal for a new guardian with no email at a school that asks for one.
EMAIL_REQUIRED_MESSAGE = "A guardian email is required at this school."


def match_existing(tenant, *, email="", phone="", matching=None):
    """The guardian row this person already has at this school, if any.

    Email first and case-insensitively, because that is the identifier a
    parent's account would be issued to and the one the unique constraint
    covers. Phone second, unless the school matches on email only: a school
    whose families share landlines would otherwise merge two households on a
    number. *matching* is the school's mode, read from its settings when not
    given; an import passes the one it read for the whole file.
    """
    if matching is None:
        matching = read_guardian_rules(tenant).matching
    email = (email or "").strip()
    phone = (phone or "").strip()
    if email:
        found = Guardian.objects.filter(tenant=tenant, email__iexact=email).first()
        if found is not None:
            return found
    if phone and matching != GuardianMatching.EMAIL_ONLY:
        return Guardian.objects.filter(tenant=tenant, phone=phone).first()
    return None


def resolve_user(tenant, email):
    """The User this guardian's email already belongs to at this school.

    Never another tenant's User: the same real address at two schools is two
    accounts with no connection between them, which is the settled decision and
    not a limitation. And never a second account for somebody who already has
    one here, which is why a teacher with a child at the school is linked to
    her own User rather than given a new one.
    """
    from vs_user.models import User

    email = (email or "").strip()
    if not email:
        return None
    user = User.objects.filter(tenant=tenant, email__iexact=email).first()
    if user is None:
        return None
    # One guardian row per User per tenant. If somebody already holds it, this
    # guardian is not that person and gets no account link.
    taken = Guardian.objects.filter(tenant=tenant, user=user).exists()
    return None if taken else user


@transaction.atomic
def upsert_guardian(tenant, *, phone, full_name="", first_name="", middle_name="",
                    last_name="", email="", occupation="", address="", rules=None):
    """The guardian row for this person, created only if they are new here.

    The name is given in parts. A caller holding only a one-line name passes
    ``full_name``, and :meth:`Guardian.save` splits it and flags the split for
    a person to confirm.

    The school's rules decide how the person is matched and whether a new row
    needs an email; a guardian already held is returned whatever their email.
    *rules* is the school's ``GuardianRules``, read when not given. A new row
    with no email at a school that asks for one is refused on ``email``: the
    write paths refuse it earlier with their own field, so reaching this is a
    caller that did not check.
    """
    rules = rules or read_guardian_rules(tenant)
    existing = match_existing(
        tenant, email=email, phone=phone, matching=rules.matching,
    )
    if existing is not None:
        # Deliberately does not overwrite the stored name from the new
        # spelling. A school that typed "Mrs P. Okafor" for the second child
        # has not renamed the guardian it already holds.
        return existing, False

    if rules.email_required and not (email or "").strip():
        raise ValidationError({"email": [EMAIL_REQUIRED_MESSAGE]})

    return Guardian.objects.create(
        tenant=tenant, full_name=(full_name or "").strip(),
        first_name=(first_name or "").strip(), middle_name=(middle_name or "").strip(),
        last_name=(last_name or "").strip(),
        phone=(phone or "").strip(), email=(email or "").strip(),
        occupation=(occupation or "").strip(), address=(address or "").strip(),
        user=resolve_user(tenant, email),
    ), True


@transaction.atomic
def link(student, guardian, *, relationship, is_primary, actor, relationship_detail=""):
    """Join a guardian to a student. A pair can only be linked once.

    *relationship_detail* is the school's own relationship, stored beside
    OTHER (``GuardianRules.resolve_relationship`` gives both).
    """
    if StudentGuardian.objects.filter(student=student, guardian=guardian).exists():
        raise ValidationError({
            "guardian": (
                f"{guardian.full_name} is already linked to "
                f"{student.first_name}. A pair can only be linked once."
            ),
        })

    if is_primary:
        _demote_others(student)

    row = StudentGuardian.objects.create(
        tenant=student.tenant, student=student, guardian=guardian,
        relationship=relationship, relationship_detail=relationship_detail or "",
        is_primary=bool(is_primary),
    )
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.STUDENT_GUARDIAN_LINKED,
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=student.tenant, actor_user=actor,
        summary=(
            f"Linked {guardian.full_name} to {student.full_name}"
            + (" as primary contact." if is_primary else ".")
        ),
        metadata={
            "guardian": guardian.pk, "relationship": relationship,
            "relationship_label": row.relationship_label,
        },
    )
    return row


def _demote_others(student):
    """Clear the existing primary before setting a new one.

    The partial unique constraint would otherwise refuse the insert, and the
    caller's intent - "this one is primary now" - is unambiguous.
    """
    StudentGuardian.objects.filter(student=student, is_primary=True).update(
        is_primary=False,
    )


@transaction.atomic
def set_primary(student, guardian, *, actor):
    _demote_others(student)
    updated = StudentGuardian.objects.filter(
        student=student, guardian=guardian,
    ).update(is_primary=True)
    if not updated:
        raise ValidationError({"guardian": "That guardian is not linked to this student."})
    return updated


@transaction.atomic
def unlink(student, guardian, *, actor, promote=None):
    """Remove a link, refusing to leave a student on the roll with too few.

    Too few is fewer than the school's minimum (``guardians.min_per_student``),
    which is one until a school asks for more. A child who is not on the roll
    can be left with none, and a child already below a minimum set after they
    were enrolled cannot be taken further below it.

    Unlinking the primary is allowed when another guardian is promoted in the
    same call. Where the school did not say which, and exactly one other
    guardian remains, that one is promoted rather than the school being sent
    back to choose between one option.
    """
    row = StudentGuardian.objects.filter(
        student=student, guardian=guardian,
    ).select_related("guardian").first()
    if row is None:
        raise ValidationError({"guardian": "That guardian is not linked to this student."})

    remaining = list(
        StudentGuardian.objects.filter(student=student).exclude(pk=row.pk),
    )
    if student.status in ON_ROLL:
        minimum = read_guardian_rules(student.tenant).min_per_student
        if not remaining:
            raise GuardianRequired(
                f"{guardian.full_name} is {student.first_name}'s only guardian. "
                f"Link another before removing this one.",
            )
        if len(remaining) < minimum:
            raise GuardianRequired(
                f"This school asks for {minimum} guardians for every child, "
                f"and {student.first_name} has {len(remaining) + 1}. Link "
                f"another before removing this one.",
            )

    was_primary = row.is_primary
    row.delete()

    if was_primary and remaining:
        target = None
        if promote is not None:
            target = next((r for r in remaining if r.guardian_id == promote.pk), None)
            if target is None:
                raise ValidationError({
                    "promote": "That guardian is not linked to this student.",
                })
        elif len(remaining) == 1:
            target = remaining[0]
        if target is None:
            raise PrimaryGuardianRequired(
                f"{guardian.full_name} was the primary contact. Say which of "
                f"the others takes over.",
            )
        StudentGuardian.objects.filter(pk=target.pk).update(is_primary=True)

    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.STUDENT_GUARDIAN_UNLINKED,
        entity_type="Student", entity_id=str(student.pk),
        entity_label=student.full_name,
        tenant=student.tenant, actor_user=actor,
        summary=f"Unlinked {guardian.full_name} from {student.full_name}.",
        metadata={"guardian": guardian.pk, "was_primary": was_primary},
    )


def assert_guardian_set(rows, *, tenant):
    """Exactly one primary, and at least the school's minimum. Checked before any write.

    ``rows`` is the validated guardian payload from an enrolment, which is an
    applicant saved as much as a child enrolled. No guardian at all is
    ``GUARDIAN_REQUIRED``, as it is at every school; some, but fewer than a
    minimum above one, is refused on ``guardians`` so the form shows it where
    the rows are.
    """
    minimum = read_guardian_rules(tenant).min_per_student
    if not rows:
        raise GuardianRequired(
            f"This school asks for {minimum} guardians for every child."
            if minimum > 1 else "",
        )
    if len(rows) < minimum:
        raise ValidationError({
            "guardians": [f"This school asks for {minimum} guardians for every child."],
        })
    primaries = [r for r in rows if r.get("is_primary")]
    if len(primaries) != 1:
        raise PrimaryGuardianRequired(
            "Mark exactly one guardian as the primary contact."
            if len(primaries) > 1
            else "Mark one guardian as the primary contact.",
        )


def wards_queryset(guardian, user, tenant):
    """The children this guardian stands for, narrowed to the caller's branches."""
    from ..models import Student
    from .scoping import scope_students

    return scope_students(
        Student.objects.filter(tenant=tenant, guardian_links__guardian=guardian),
        user, tenant,
    ).distinct()


def primary_for(student):
    link_row = (
        StudentGuardian.objects.filter(student=student, is_primary=True)
        .select_related("guardian").first()
    )
    return link_row.guardian if link_row else None


def guardian_directory(tenant, user, *, search="", include_unlinked=False,
                       branch=None, session=None, name_review=False):
    """The guardian list, with ward counts. Branch narrowing is on the wards.

    Guardian carries no branch, so the row itself is never narrowed. What is
    narrowed is which children appear against it, which is why a branch-bound
    caller sees a guardian with one of their two children beside them.

    *branch* applies that same narrowing on request rather than by the caller's
    own binding, so a school-wide administrator reading the directory under a
    branch lens sees the guardians of that branch's children - and not the
    parents of a site she is not looking at.

    *name_review* keeps only the guardians whose name was split from one line
    and not yet confirmed, the list an administrator works through to confirm
    them.
    """
    from .scoping import scope_students
    from ..models import Student

    qs = Guardian.objects.filter(tenant=tenant)
    if name_review:
        qs = qs.filter(name_needs_review=True)
    # Loosely and ranked, like every other directory, so "ada okeye" typed the
    # other way round, or a surname and a first initial, still finds the row.
    qs = core_search(qs, search, fields=GUARDIAN_SEARCH_FIELDS)
    if not include_unlinked:
        visible_students = scope_students(
            Student.objects.filter(tenant=tenant), user, tenant,
        )
        if branch is not None:
            visible_students = visible_students.filter(branch=branch)
        if session is not None:
            # Same narrowing as the branch, on the other axis: the guardians of
            # the children who were on THAT year's roll. A guardian carries no
            # year any more than they carry a branch.
            visible_students = visible_students.filter(
                enrolments__session=session,
            )
        qs = qs.filter(student_links__student__in=visible_students).distinct()
    # Ordered, because this is paginated. Postgres gives no stable order to an
    # unordered query, so page 2 could repeat a guardian from page 1 and drop
    # another entirely - and the reader has no way to tell. Name then pk: the
    # name is what the list is read by, the pk breaks ties between the several
    # households that share a surname.
    # Rank first where a search ran, so a three-letter coincidence cannot sort
    # alphabetically above the guardian whose name was typed in full. The
    # annotation is absent when nothing was typed, and the plain order stands.
    if (search or "").strip():
        return qs.order_by("search_blob_rank", "full_name", "pk")
    return qs.order_by("full_name", "pk")


#: What a typed query is matched against, joined into one string.
#:
#: The phone number is in here because "who do we call about this family" is
#: often asked from a missed call rather than from a name.
GUARDIAN_SEARCH_FIELDS = ("full_name", "phone", "email")


#: The guardian's own details, as opposed to their link to any one student.
GUARDIAN_FIELDS = (
    "first_name", "middle_name", "last_name", "phone", "email", "occupation", "address",
)

#: The parts of a guardian's name. Sending any of them confirms the name.
GUARDIAN_NAME_FIELDS = ("first_name", "middle_name", "last_name")


@transaction.atomic
def update_guardian(guardian, *, actor, **fields):
    """Correct a guardian's own details.

    **This had no route at all until now**, which meant a phone number typed
    wrongly at enrolment was permanent: the create path is the only place that
    ever wrote these columns, and linking an EXISTING guardian passes their id
    and drops every other field. A registrar's only workaround was to create a
    second record for the same parent, which splits the household and breaks the
    sibling link the Guardians screen exists to show.

    Only changed fields are written, so an unchanged save is not an audit entry
    saying somebody edited a record they did not. The one exception is a name
    awaiting review: sending its parts unchanged is a person confirming the
    split, which clears the flag and is recorded as such.

    Two couplings the caller must not have to know about:

    * **Email is unique per school**, so moving one onto an address another
      guardian already holds is refused by name rather than surfacing as an
      IntegrityError.
    * **The portal account is resolved FROM the email**, so changing the email
      re-resolves it. Leaving it would point a corrected guardian at the account
      belonging to the address they no longer use.

    At a school that requires a guardian email, blanking one is refused. A
    guardian who never had one is not, because nothing is being blanked.
    """
    changed = {}
    for key in GUARDIAN_FIELDS:
        if key not in fields:
            continue
        value = (fields[key] or "").strip()
        if value != getattr(guardian, key):
            changed[key] = value

    if guardian.name_needs_review and any(key in fields for key in GUARDIAN_NAME_FIELDS):
        changed["name_needs_review"] = False

    if not changed:
        return guardian, []

    if "email" in changed and not changed["email"]:
        if read_guardian_rules(guardian.tenant).email_required:
            raise ValidationError({"email": [EMAIL_REQUIRED_MESSAGE]})

    email = changed.get("email")
    if email:
        clash = (
            Guardian.objects.filter(tenant=guardian.tenant, email__iexact=email)
            .exclude(pk=guardian.pk)
            .first()
        )
        if clash is not None:
            raise ValidationError({
                "email": (
                    f"{clash.full_name} already uses that address at this "
                    f"school. Two guardians cannot share one."
                ),
            })

    for key, value in changed.items():
        setattr(guardian, key, value)
    if "email" in changed:
        guardian.user = resolve_user(guardian.tenant, changed["email"])
    guardian.save(update_fields=[*changed, "user", "updated_at"])

    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.UPDATE,
        entity_type="Guardian", entity_id=str(guardian.pk),
        entity_label=guardian.full_name,
        tenant=guardian.tenant, actor_user=actor,
        summary=(
            f"{guardian.full_name}'s name confirmed."
            if set(changed) == {"name_needs_review"}
            else f"{guardian.full_name}'s details updated: "
                 f"{', '.join(sorted(k for k in changed if k != 'name_needs_review'))}."
        ),
        metadata={"fields": sorted(changed)},
    )
    return guardian, sorted(changed)


@transaction.atomic
def set_photo(guardian, *, upload, actor):
    """Give a guardian a face, or replace the one they have.

    ``core.binding`` does the rest on save: it points the stored bytes at this
    row, and retires whatever this field pointed at before - so a replaced
    photograph does not keep its own live url after the school believes it has
    been replaced.

    Optional, always. A school holds guardians it has never met, and nothing in
    the module gates on this being set.
    """
    guardian.photo = upload
    guardian.save(update_fields=["photo", "updated_at"])
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.UPDATE,
        entity_type="Guardian", entity_id=str(guardian.pk),
        entity_label=guardian.full_name,
        tenant=guardian.tenant, actor_user=actor,
        summary=f"{guardian.full_name}'s photograph updated.",
        metadata={"field": "photo"},
    )
    return guardian


@transaction.atomic
def clear_photo(guardian, *, actor):
    """Take the photograph away. Audited separately: it is the direction that
    loses evidence, and the bytes do not come back."""
    guardian.photo.delete(save=False)
    guardian.photo = ""
    guardian.save(update_fields=["photo", "updated_at"])
    emit_audit_event(
        module_key=AuditModuleKey.STUDENT,
        action_type=AuditActionType.UPDATE,
        entity_type="Guardian", entity_id=str(guardian.pk),
        entity_label=guardian.full_name,
        tenant=guardian.tenant, actor_user=actor,
        summary=f"{guardian.full_name}'s photograph removed.",
        metadata={"field": "photo", "removed": True},
    )
    return guardian
