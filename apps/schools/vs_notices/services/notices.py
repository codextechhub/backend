"""Posting a notice, and deciding who sees it.

**Who may post where.** A holder of ``school.notices.post`` posts to any
audience inside their branch reach: a whole-school administrator to anybody, a
branch administrator to their branches and the classes in them. The whole
school and a level span every branch, so only a whole-school reach may name
them. A holder of ``school.class_notices.post`` alone (a teacher) posts to the
classes they teach or lead this year, and corrects or withdraws only notices
they wrote.

**Who sees what.** A pupil sees a published notice when an audience row names
them: the whole school, their branch, or their level or class this year. A
member of staff sees a notice when an audience row lies inside their reach,
and always sees their own drafts. A parent sees the union of what each of
their children is sent.

**Publishing tells the pupils, and their parents.** Each pupil in the audience
with a working portal account is notified in-app and by email, after the
publication commits (``tasks.dispatch_published``). Unless the school has
turned off ``notices.tell_parents`` (on by default), each parent with an active
account is then told the same way, once, naming every one of their children the
notice reaches; a guardian with no account is skipped, and a member of staff
who is also a parent is told as a parent. Read counts are taken against every
pupil on the roll in the audience, account or not, so a class where half the
pupils have no account reads "12 of 32", which is the truth a teacher needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    AUDIENCE_MAX,
    BODY_MAX,
    PERM_POST,
    PERM_POST_CLASS,
    TITLE_MAX,
    AudienceKind,
    NoticeStatus,
)
from ..exceptions import (
    AudienceOutOfReach,
    AudienceRequired,
    NoticeNotEditable,
    NoticeNotPublishable,
    NoticeNotWithdrawable,
    NoticesError,
)
from ..models import Notice, NoticeAudience, NoticeRead


# ── Reach ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reach:
    """Where one person may post.

    ``whole`` is a whole-school reach. ``branch_ids`` are the branches a
    branch-bound poster covers. ``class_ids`` are the classes a teacher may
    post to. ``class_only`` is a teacher without the broader key, who may
    change only what they wrote.
    """

    whole: bool = False
    branch_ids: frozenset = field(default_factory=frozenset)
    class_ids: frozenset = field(default_factory=frozenset)
    class_only: bool = False

    @property
    def can_post(self) -> bool:
        return self.whole or bool(self.branch_ids) or bool(self.class_ids)


def taught_class_ids(tenant, user) -> frozenset:
    """The classes *user* teaches a subject to, or leads, in the active year."""
    from schools.vs_academics.models import SchoolClass, SessionStatus
    from schools.vs_staff.models import TeachingAssignment

    active = Q(session__status=SessionStatus.ACTIVE, is_active=True, tenant=tenant)
    taught = TeachingAssignment.objects.filter(
        tenant=tenant, staff__user=user, session__status=SessionStatus.ACTIVE,
    ).values("school_class_id")
    return frozenset(
        SchoolClass.objects.filter(active)
        .filter(Q(pk__in=taught) | Q(class_teacher__user=user))
        .values_list("pk", flat=True)
    )


def reach_for(tenant, user) -> Reach:
    """Where *user* may post a notice at *tenant*."""
    from vs_rbac.evaluator import has_permission
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    if has_permission(user, PERM_POST, tenant=tenant):
        visible = visible_branch_ids(user, tenant)
        if visible is WHOLE_TENANT:
            return Reach(whole=True)
        return Reach(branch_ids=frozenset(visible or ()))
    if has_permission(user, PERM_POST_CLASS, tenant=tenant):
        return Reach(class_ids=taught_class_ids(tenant, user), class_only=True)
    return Reach()


def _row_in_reach(reach: Reach, row) -> bool:
    if reach.whole:
        return True
    if row.kind == AudienceKind.CLASS:
        if row.school_class_id in reach.class_ids:
            return True
        branch_id = row.school_class.branch_id
        return branch_id is not None and branch_id in reach.branch_ids
    if row.kind == AudienceKind.BRANCH:
        return row.branch_id in reach.branch_ids
    # The whole school and a level span every branch.
    return False


def may_manage(reach: Reach, notice, user) -> bool:
    """Whether *user* may correct, publish or withdraw *notice*."""
    if reach.class_only and notice.created_by_id != user.pk:
        return False
    # Reads the prefetched rows a list already loaded, so a page of notices
    # asks once rather than once per notice.
    rows = list(notice.audiences.all())
    if not rows:
        return notice.created_by_id == user.pk and reach.can_post
    return all(_row_in_reach(reach, row) for row in rows)


# ── Audience ──────────────────────────────────────────────────────────────


def resolve_audience(tenant, entries) -> list[NoticeAudience]:
    """Turn ``[{"kind": "CLASS", "id": 5}, ...]`` into unsaved audience rows.

    Branches, levels and classes are looked up inside *tenant* only; levels and
    classes must belong to the active year. An id that does not resolve is
    refused as out of reach, never as "not found", so a guessed id says
    nothing about another school.
    """
    from schools.vs_academics.models import Level, SchoolClass, SessionStatus
    from vs_tenants.models import Branch

    if not entries:
        return []
    if len(entries) > AUDIENCE_MAX:
        raise NoticesError(
            f"A notice can go to at most {AUDIENCE_MAX} groups at once. Choose "
            f"a level or a branch instead of each of its classes.",
        )
    rows, seen = [], set()
    for entry in entries:
        kind = str(entry.get("kind", "")).upper()
        target = entry.get("id")
        if (kind, target) in seen:
            continue
        seen.add((kind, target))
        if kind == AudienceKind.SCHOOL:
            rows.append(NoticeAudience(tenant=tenant, kind=kind))
            continue
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise AudienceOutOfReach() from None
        if kind == AudienceKind.BRANCH:
            obj = Branch.all_objects.filter(tenant=tenant, pk=target).first()
            rows.append(NoticeAudience(tenant=tenant, kind=kind, branch=obj))
        elif kind == AudienceKind.LEVEL:
            obj = Level.all_objects.filter(
                tenant=tenant, pk=target, session__status=SessionStatus.ACTIVE,
            ).first()
            rows.append(NoticeAudience(tenant=tenant, kind=kind, level=obj))
        elif kind == AudienceKind.CLASS:
            obj = SchoolClass.all_objects.filter(
                tenant=tenant, pk=target, is_active=True,
                session__status=SessionStatus.ACTIVE,
            ).first()
            rows.append(NoticeAudience(tenant=tenant, kind=kind, school_class=obj))
        else:
            raise NoticesError(f"{kind or 'That'} is not a kind of audience.")
        if obj is None:
            raise AudienceOutOfReach()
    return rows


def assert_in_reach(reach: Reach, rows) -> None:
    if not all(_row_in_reach(reach, row) for row in rows):
        raise AudienceOutOfReach()


def audience_label(notice) -> str:
    """The audience in words: "School-wide", or the groups it names."""
    rows = list(notice.audiences.all())
    if any(row.kind == AudienceKind.SCHOOL for row in rows):
        return "School-wide"
    names = []
    for row in rows:
        if row.kind == AudienceKind.BRANCH:
            names.append(row.branch.name)
        elif row.kind == AudienceKind.LEVEL:
            names.append(row.level.name)
        elif row.kind == AudienceKind.CLASS:
            names.append(row.school_class.name)
    return ", ".join(names)


def audience_payload(notice) -> list[dict]:
    """The audience as rows a client can show and send back."""
    out = []
    for row in notice.audiences.all():
        if row.kind == AudienceKind.SCHOOL:
            out.append({"kind": row.kind, "id": None, "name": "Whole school"})
        elif row.kind == AudienceKind.BRANCH:
            out.append({"kind": row.kind, "id": row.branch_id, "name": row.branch.name})
        elif row.kind == AudienceKind.LEVEL:
            out.append({"kind": row.kind, "id": row.level_id, "name": row.level.name})
        else:
            out.append({
                "kind": row.kind, "id": row.school_class_id, "name": row.school_class.name,
            })
    return out


def audience_options(tenant, user) -> dict:
    """What the caller may choose from when addressing a notice.

    The whole school and levels only for a whole-school reach; branches only
    where the school has more than one, since one branch is the whole school.
    """
    from schools.vs_academics.models import Level, SchoolClass, SessionStatus
    from schools.vs_students.services.scoping import branch_dimension_applies
    from vs_tenants.models import Branch

    reach = reach_for(tenant, user)
    classes = SchoolClass.objects.filter(
        tenant=tenant, is_active=True, session__status=SessionStatus.ACTIVE,
    ).select_related("branch").order_by("name")
    out = {"school": reach.whole, "branches": [], "levels": [], "classes": []}
    if reach.whole:
        if branch_dimension_applies(tenant):
            out["branches"] = [
                {"id": b.pk, "name": b.name}
                for b in Branch.all_objects.filter(tenant=tenant).order_by("name")
            ]
        out["levels"] = [
            {"id": lv.pk, "name": lv.name}
            for lv in Level.objects.filter(
                tenant=tenant, session__status=SessionStatus.ACTIVE,
            ).order_by("order_index", "name")
        ]
    elif reach.branch_ids and branch_dimension_applies(tenant):
        out["branches"] = [
            {"id": b.pk, "name": b.name}
            for b in Branch.all_objects.filter(tenant=tenant, pk__in=reach.branch_ids)
            .order_by("name")
        ]
    if not reach.whole:
        classes = classes.filter(
            Q(pk__in=reach.class_ids) | Q(branch_id__in=reach.branch_ids),
        )
    out["classes"] = [{"id": c.pk, "name": c.name} for c in classes]
    return out


# ── Who sees what ─────────────────────────────────────────────────────────


def staff_notices(tenant, user):
    """The notices a member of staff sees: inside their reach, and their own drafts."""
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    qs = Notice.objects.filter(tenant=tenant)
    visible = visible_branch_ids(user, tenant)
    if visible is WHOLE_TENANT:
        return qs
    branch_ids = tuple(visible or ())
    in_reach = NoticeAudience.objects.filter(tenant=tenant).filter(
        Q(kind__in=(AudienceKind.SCHOOL, AudienceKind.LEVEL))
        | Q(kind=AudienceKind.BRANCH, branch_id__in=branch_ids)
        | Q(kind=AudienceKind.CLASS, school_class__branch__isnull=True)
        | Q(kind=AudienceKind.CLASS, school_class__branch_id__in=branch_ids)
    ).values("notice_id")
    return qs.filter(Q(pk__in=in_reach) | Q(created_by=user)).distinct()


def pupil_enrolment(student):
    """The pupil's active placement in the active year, or None."""
    return (
        student.enrolments.filter(is_active=True, session__status="ACTIVE")
        .select_related("school_class__level")
        .first()
    )


def _addressed_q(branch_id, enrolment) -> Q:
    """The audience rows that name one pupil: school, branch, level and class.

    *enrolment* is the pupil's placement in the active year, or None for a
    pupil not placed in a class, who is still reached by the school and their
    branch.
    """
    match = Q(kind=AudienceKind.SCHOOL) | Q(kind=AudienceKind.BRANCH, branch_id=branch_id)
    if enrolment is not None:
        match |= Q(kind=AudienceKind.CLASS, school_class_id=enrolment.school_class_id)
        if enrolment.school_class.level_id:
            match |= Q(kind=AudienceKind.LEVEL, level_id=enrolment.school_class.level_id)
    return match


def _published_for(tenant_id, match: Q):
    addressed = NoticeAudience.objects.filter(tenant=tenant_id).filter(match).values("notice_id")
    return (
        Notice.objects.filter(
            tenant=tenant_id, status=NoticeStatus.PUBLISHED, pk__in=addressed,
        )
        .prefetch_related("audiences__branch", "audiences__level", "audiences__school_class")
        .order_by("-published_at", "-pk")
    )


def pupil_notices(student):
    """The published notices addressed to *student*, newest first."""
    match = _addressed_q(student.branch_id, pupil_enrolment(student))
    return _published_for(student.tenant, match)


def pupil_placements(students) -> dict:
    """Each pupil's active-year placement, keyed by pupil id, in one query.

    A pupil with no placement in the active year maps to None.
    """
    from schools.vs_students.models import ClassEnrolment

    placed = {
        row.student_id: row
        for row in ClassEnrolment.objects.filter(
            student__in=[s.pk for s in students], is_active=True, session__status="ACTIVE",
        ).select_related("school_class__level")
    }
    return {s.pk: placed.get(s.pk) for s in students}


def guardian_notices(students, *, placements=None):
    """The published notices addressed to any of *students*, newest first, each once.

    The union of what each child is sent, matched exactly as :func:`pupil_notices`
    matches one pupil: the school, the child's branch, and the child's level or
    class this year. A notice that reaches two siblings appears once. *placements*
    is the result of :func:`pupil_placements`, passed by a caller that already
    holds it. All of *students* belong to one school.
    """
    students = list(students)
    if not students:
        return Notice.objects.none()
    placements = placements if placements is not None else pupil_placements(students)
    match = Q(pk__in=[])
    for student in students:
        match |= _addressed_q(student.branch_id, placements.get(student.pk))
    return _published_for(students[0].tenant_id, match)


def children_addressed(notice, students, placements) -> list:
    """The pupils among *students* that *notice* is addressed to, in their given order.

    Judged from the notice's audience rows, which :func:`guardian_notices`
    already loads, by the same rule it queries with.
    """
    rows = list(notice.audiences.all())
    out = []
    for student in students:
        enrolment = placements.get(student.pk)
        for row in rows:
            if (
                row.kind == AudienceKind.SCHOOL
                or (row.kind == AudienceKind.BRANCH and row.branch_id == student.branch_id)
                or (
                    enrolment is not None
                    and (
                        (row.kind == AudienceKind.CLASS
                         and row.school_class_id == enrolment.school_class_id)
                        or (row.kind == AudienceKind.LEVEL and enrolment.school_class.level_id
                            and row.level_id == enrolment.school_class.level_id)
                    )
                )
            ):
                out.append(student)
                break
    return out


def audience_pupils(notice):
    """Every pupil on the roll the notice is addressed to."""
    from schools.vs_students.constants import ON_ROLL
    from schools.vs_students.models import Student

    rows = list(notice.audiences.all())
    pupils = Student.objects.filter(tenant=notice.tenant_id, status__in=ON_ROLL)
    if any(row.kind == AudienceKind.SCHOOL for row in rows):
        return pupils
    match = Q(pk__in=[])
    placed = dict(enrolments__is_active=True, enrolments__session__status="ACTIVE")
    for row in rows:
        if row.kind == AudienceKind.BRANCH:
            match |= Q(branch_id=row.branch_id)
        elif row.kind == AudienceKind.LEVEL:
            match |= Q(enrolments__school_class__level_id=row.level_id, **placed)
        elif row.kind == AudienceKind.CLASS:
            match |= Q(enrolments__school_class_id=row.school_class_id, **placed)
    return pupils.filter(match).distinct()


def read_counts(notice) -> dict:
    """How many pupils in the audience have opened the notice, of how many."""
    pupils = audience_pupils(notice)
    readers = NoticeRead.objects.filter(notice=notice).values("user_id")
    return {
        "audience": pupils.count(),
        "read": pupils.filter(user_id__in=readers).count(),
    }


# ── Writes ────────────────────────────────────────────────────────────────


def _clean_text(title, body):
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        raise NoticesError("Give the notice a title.")
    if len(title) > TITLE_MAX:
        raise NoticesError(f"Keep the title to {TITLE_MAX} characters.")
    if not body:
        raise NoticesError("Write the notice before saving it.")
    if len(body) > BODY_MAX:
        raise NoticesError(f"Keep the notice to {BODY_MAX} characters.")
    return title, body


def _audit(notice, action, actor, summary):
    emit_audit_event(
        module_key=AuditModuleKey.NOTICES,
        action_type=action,
        entity_type="Notice", entity_id=str(notice.pk),
        entity_label=notice.title,
        tenant=notice.tenant, actor_user=actor,
        summary=summary,
        metadata={"status": notice.status, "audience": audience_label(notice)},
    )


@transaction.atomic
def create(tenant, *, actor, title, body, audience, publish_now=False):
    """Save a draft, and publish it straight away when *publish_now*."""
    reach = reach_for(tenant, actor)
    if not reach.can_post:
        raise AudienceOutOfReach()
    title, body = _clean_text(title, body)
    rows = resolve_audience(tenant, audience)
    assert_in_reach(reach, rows)
    notice = Notice.objects.create(tenant=tenant, title=title, body=body, created_by=actor)
    for row in rows:
        row.notice = notice
    NoticeAudience.objects.bulk_create(rows)
    _audit(notice, AuditActionType.CREATE, actor, f'Notice "{title}" was drafted.')
    if publish_now:
        publish(notice, actor=actor)
    return notice


@transaction.atomic
def update(notice, *, actor, title=None, body=None, audience=None):
    """Correct a notice.

    A draft may change anything. A published notice may change its words,
    which stamps it as edited, but not its audience: the pupils already told
    about it would keep a notice nobody now means them to read, and pupils
    newly in it would never be told.
    """
    notice = Notice.objects.select_for_update().get(pk=notice.pk)
    if notice.status == NoticeStatus.WITHDRAWN:
        raise NoticeNotEditable()
    reach = reach_for(notice.tenant, actor)
    if not may_manage(reach, notice, actor):
        raise AudienceOutOfReach()
    new_title, new_body = _clean_text(
        notice.title if title is None else title,
        notice.body if body is None else body,
    )
    changed = new_title != notice.title or new_body != notice.body
    notice.title, notice.body = new_title, new_body
    if audience is not None:
        if notice.status != NoticeStatus.DRAFT:
            raise NoticeNotEditable(
                "Who a published notice is for cannot change. Withdraw it and "
                "post a new one to a different audience.",
            )
        rows = resolve_audience(notice.tenant, audience)
        assert_in_reach(reach, rows)
        notice.audiences.all().delete()
        for row in rows:
            row.notice = notice
        NoticeAudience.objects.bulk_create(rows)
        changed = True
    if changed and notice.status == NoticeStatus.PUBLISHED:
        notice.edited_at = timezone.now()
    notice.save()
    if changed:
        _audit(notice, AuditActionType.UPDATE, actor, f'Notice "{notice.title}" was corrected.')
    return notice


@transaction.atomic
def publish(notice, *, actor):
    """Publish a draft and tell its pupils once the publication commits."""
    from ..tasks import queue_published

    notice = Notice.objects.select_for_update().get(pk=notice.pk)
    if notice.status != NoticeStatus.DRAFT:
        raise NoticeNotPublishable()
    reach = reach_for(notice.tenant, actor)
    if not may_manage(reach, notice, actor):
        raise AudienceOutOfReach()
    if not notice.audiences.exists():
        raise AudienceRequired()
    notice.status = NoticeStatus.PUBLISHED
    notice.published_at = timezone.now()
    notice.published_by = actor
    notice.save(update_fields=["status", "published_at", "published_by", "updated_at"])
    _audit(
        notice, AuditActionType.NOTICE_PUBLISHED, actor,
        f'Notice "{notice.title}" was published to {audience_label(notice)}.',
    )
    queue_published(notice.pk)
    return notice


@transaction.atomic
def withdraw(notice, *, actor):
    """Take a published notice out of every pupil's feed."""
    notice = Notice.objects.select_for_update().get(pk=notice.pk)
    if notice.status != NoticeStatus.PUBLISHED:
        raise NoticeNotWithdrawable()
    reach = reach_for(notice.tenant, actor)
    if not may_manage(reach, notice, actor):
        raise AudienceOutOfReach()
    notice.status = NoticeStatus.WITHDRAWN
    notice.withdrawn_at = timezone.now()
    notice.withdrawn_by = actor
    notice.save(update_fields=["status", "withdrawn_at", "withdrawn_by", "updated_at"])
    _audit(notice, AuditActionType.NOTICE_WITHDRAWN, actor, f'Notice "{notice.title}" was withdrawn.')
    return notice


def mark_read(notice, user) -> None:
    """Record that *user* opened *notice*, and clear its bell entry."""
    from vs_notifications.services.acknowledge import acknowledge_record
    from vs_notifications.services.routing import RecordFamily

    NoticeRead.objects.get_or_create(
        tenant_id=notice.tenant_id, notice=notice, user=user,
    )
    acknowledge_record(user, family=RecordFamily.NOTICE, value=notice.pk)


def mark_all_read(notices, user) -> int:
    """Record every one of *notices* as opened by *user*; returns how many were new."""
    from vs_notifications.services.acknowledge import acknowledge_record
    from vs_notifications.services.routing import RecordFamily

    notices = list(notices)
    already = set(
        NoticeRead.objects.filter(user=user, notice__in=notices).values_list("notice_id", flat=True)
    )
    fresh = [n for n in notices if n.pk not in already]
    NoticeRead.objects.bulk_create(
        [NoticeRead(tenant_id=n.tenant_id, notice=n, user=user) for n in fresh],
        ignore_conflicts=True,
    )
    for notice in fresh:
        acknowledge_record(user, family=RecordFamily.NOTICE, value=notice.pk)
    return len(fresh)
