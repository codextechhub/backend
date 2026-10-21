"""What an inbox page needs, loaded in a constant number of queries.

:func:`prepare` takes a visibility queryset (``staff_conversations`` and its
portal siblings) and adds everything a row shows as annotations and
prefetches: the unread count, the member count, the child's class, the
guardian's relationship and the last message with its sender. A page of five
conversations and a page of fifty cost the same.

:func:`search` builds the ``?search=`` filter from the parts the caller is
allowed to read. A part the caller may not read matches nothing, so a hidden
message body cannot be probed by searching for words in it, nor a hidden name
by searching for the name.
"""
from __future__ import annotations

from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from ..constants import (
    KIND_FILTERS,
    PAIR_KINDS,
    SURFACE_STAFF,
    ConversationKind,
    ParticipantRole,
)
from ..models import Message, Participant
from . import visibility


def prepare(qs, user, *, surface=SURFACE_STAFF):
    """Annotate and prefetch *qs* for rendering as inbox rows."""
    from schools.vs_students.models import ClassEnrolment, StudentGuardian

    qs = visibility.annotate_unread(qs, user)
    members = (
        Participant.objects.filter(conversation_id=OuterRef("pk"), left_at__isnull=True)
        .order_by().values("conversation_id").annotate(c=Count("pk")).values("c")
    )
    placement = ClassEnrolment.objects.filter(
        student_id=OuterRef("student_id"), is_active=True, session__status="ACTIVE",
    ).order_by("-pk")
    link = StudentGuardian.objects.filter(
        student_id=OuterRef("student_id"), guardian_id=OuterRef("guardian_id"),
    )
    qs = qs.annotate(
        member_total=Coalesce(Subquery(members), Value(0)),
        child_class_name=Subquery(placement.values("school_class__name")[:1]),
        relationship_code=Subquery(link.values("relationship")[:1]),
        relationship_detail=Subquery(link.values("relationship_detail")[:1]),
        activity=Case(
            When(last_message_id__gt=F("my_after"), then=F("last_message_at")),
            default=Coalesce(F("my_joined"), F("created_at")),
        ),
    )
    # Only a pair's two ends are loaded: a group's or a broadcast's members
    # are counted, never listed, on an inbox row.
    ends = Participant.objects.filter(
        left_at__isnull=True, conversation__kind__in=PAIR_KINDS,
    ).select_related("user", "user__staff_profile")
    return (
        qs.select_related("last_message__sender", "student", "guardian", "branch")
        .prefetch_related(Prefetch("participants", queryset=ends, to_attr="open_ends"))
        .order_by("-activity", "-pk")
    )


def listed_for(qs, user):
    """Hide a pair's conversation from its other end until something is said in it.

    The person who started it always sees it; so does anybody once a message
    they may read exists. Groups and broadcasts are shown as soon as the
    person is in them.
    """
    return qs.filter(
        ~Q(kind__in=PAIR_KINDS) | Q(last_message_id__gt=F("my_after")) | Q(created_by_id=user.pk)
    )


def by_kind(qs, name):
    """Narrow to one inbox tab: ``direct``, ``family``, ``group`` or ``broadcast``."""
    kinds = KIND_FILTERS.get((name or "").lower())
    return qs.filter(kind__in=kinds) if kinds else qs


def search(qs, term, *, user, surface=SURFACE_STAFF, titles=True, people=True,
           children=True, body=True):
    """Narrow *qs* to conversations matching *term* in the parts the caller may read.

    *titles* is the name of a group or broadcast, *people* the names of the
    other end, *children* the child's name and *body* the text of messages the
    caller may read.
    """
    term = (term or "").strip()
    if not term:
        return qs
    match = Q(pk__in=[])
    if titles:
        match |= Q(kind__in=(ConversationKind.GROUP, ConversationKind.BROADCAST),
                   title__icontains=term)
    if people:
        match |= _people_q(term, user, surface)
    if children:
        match |= Q(kind__in=(ConversationKind.FAMILY, ConversationKind.PUPIL)) & (
            Q(student__first_name__icontains=term) | Q(student__last_name__icontains=term)
            | Q(student__middle_name__icontains=term)
        )
    if body:
        match |= Q(Exists(
            Message.objects.filter(conversation_id=OuterRef("pk"), body__icontains=term)
            .filter(id__gt=OuterRef("my_after"))
        ))
    return qs.filter(match)


def _named(prefix, term):
    return Q(**{f"{prefix}user__first_name__icontains": term}) | Q(
        **{f"{prefix}user__last_name__icontains": term}
    )


def _people_q(term, user, surface) -> Q:
    """Conversations whose other end's name matches *term*."""
    if surface == SURFACE_STAFF:
        other = Exists(
            Participant.objects.filter(conversation_id=OuterRef("pk"), left_at__isnull=True)
            .exclude(user_id=user.pk).filter(_named("", term))
        )
        by_pair = Q(kind__in=(ConversationKind.STAFF_DIRECT, ConversationKind.BROADCAST_REPLY)) & other
        family = Q(kind=ConversationKind.FAMILY, guardian__full_name__icontains=term)
        pupil = Q(kind=ConversationKind.PUPIL) & (
            Q(student__first_name__icontains=term) | Q(student__last_name__icontains=term)
        )
        return by_pair | family | pupil
    staff = Exists(
        Participant.objects.filter(
            conversation_id=OuterRef("pk"), role=ParticipantRole.STAFF, left_at__isnull=True,
        ).filter(_named("", term))
    )
    return Q(staff)


def counterpart(conversation, user, *, surface=SURFACE_STAFF, labels=None, with_child=True):
    """The other end of *conversation* as ``{"user_id", "name", "label"}``, or None.

    ``labels`` maps a staff user id to the label a family reads under their
    name; without one the staff member's job title is used. ``with_child``
    is False when the caller may not read the child, so the guardian's label
    does not name them. A group and a broadcast have no single other end.
    """
    kind = conversation.kind
    if kind in (ConversationKind.GROUP, ConversationKind.BROADCAST):
        return None
    ends = getattr(conversation, "open_ends", [])
    if surface == SURFACE_STAFF:
        if kind == ConversationKind.FAMILY:
            return _guardian_end(conversation, ends, with_child)
        if kind == ConversationKind.PUPIL:
            return _pupil_end(conversation, ends)
        other = next((p for p in ends if p.user_id != user.pk), None)
        return _staff_end(other, labels) if other else None
    staff = next((p for p in ends if p.role == ParticipantRole.STAFF), None)
    return _staff_end(staff, labels) if staff else None


def person_name(user) -> str:
    """An account's name as a person reads it."""
    return (user.full_name or user.email) if user is not None else ""


def _staff_end(participant, labels):
    user = participant.user
    profile = getattr(user, "staff_profile", None)
    label = (labels or {}).get(user.pk) or ((profile.job_title or "").strip() if profile else "") or "Staff"
    return {"user_id": user.pk, "name": person_name(user), "label": label}


def _guardian_end(conversation, ends, with_child):
    guardian = conversation.guardian
    guardian_end = next((p for p in ends if p.role == ParticipantRole.GUARDIAN), None)
    label = relationship_label(conversation)
    if with_child:
        label = f"{label} of {conversation.student.full_name}"
    return {
        "user_id": guardian_end.user_id if guardian_end else getattr(guardian, "user_id", None),
        "name": guardian.full_name,
        "label": label,
    }


def _pupil_end(conversation, ends):
    pupil_end = next((p for p in ends if p.role == ParticipantRole.PUPIL), None)
    return {
        "user_id": pupil_end.user_id if pupil_end else getattr(conversation.student, "user_id", None),
        "name": conversation.student.full_name,
        "label": "Pupil",
    }


def relationship_label(conversation) -> str:
    """The guardian's relationship to the child ("Mother"), as the school words it."""
    from schools.vs_students.constants import Relationship

    detail = getattr(conversation, "relationship_detail", "") or ""
    code = getattr(conversation, "relationship_code", "") or ""
    if detail:
        return detail
    return Relationship(code).label if code in Relationship.values else "Guardian"
