"""Reading other people's conversations as a school leader, read-only, on the record.

Three kinds are inspectable, each under a key of its own: a staff group, a
broadcast (the broadcast itself, never the private replies that came back to
its sender) and a pupil's conversation with staff. Staff-to-staff and
guardian-to-staff conversations are never inspectable, and no inspection route
resolves them.

An inspector joins nothing and moves no read watermark. Every opening of a
conversation's detail or its messages writes one ``CONVERSATION_INSPECTED``
audit event, whether or not the inspector also takes part in it.
"""
from __future__ import annotations

from django.db.models import Count, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Coalesce

from vs_audit.models import AuditActionType

from ..constants import (
    INSPECT_SURFACES,
    PERM_BROADCAST_INSPECT,
    PERM_GROUP_INSPECT,
    PERM_WELFARE_INSPECT,
    SURFACE_INSPECT_BROADCAST,
    SURFACE_INSPECT_GROUP,
    SURFACE_INSPECT_WELFARE,
    ConversationKind,
    ParticipantRole,
)
from ..exceptions import ConversationNotFound
from ..models import Message, Participant
from . import audit, visibility

#: The key that opens each inspection surface.
SURFACE_KEYS = {
    SURFACE_INSPECT_GROUP: PERM_GROUP_INSPECT,
    SURFACE_INSPECT_BROADCAST: PERM_BROADCAST_INSPECT,
    SURFACE_INSPECT_WELFARE: PERM_WELFARE_INSPECT,
}


def surfaces_held(tenant, user) -> list[str]:
    """The inspection surfaces *user* holds the key for."""
    from vs_rbac.evaluator import has_permission

    return [s for s, key in SURFACE_KEYS.items() if has_permission(user, key, tenant=tenant)]


def resolve_any(tenant, user, conversation_id) -> visibility.Access:
    """Resolve *conversation_id* on whichever inspection surface the caller holds the key for.

    A conversation of a kind the caller holds no key for answers exactly as one
    that does not exist.
    """
    for surface in surfaces_held(tenant, user):
        try:
            return visibility.resolve(tenant, user, conversation_id, surface=surface)
        except ConversationNotFound:
            continue
    raise ConversationNotFound()


def listing(tenant, user, surface):
    """Conversations of the surface's kind inside the caller's reach, annotated for a list row.

    Adds the member count, the number of messages, the child's class name and
    the last activity. No message text is loaded.
    """
    from schools.vs_students.models import ClassEnrolment

    members = (
        Participant.objects.filter(conversation_id=OuterRef("pk"), left_at__isnull=True)
        .order_by().values("conversation_id").annotate(c=Count("pk")).values("c")
    )
    said = (
        Message.objects.filter(conversation_id=OuterRef("pk"))
        .order_by().values("conversation_id").annotate(c=Count("pk")).values("c")
    )
    placement = ClassEnrolment.objects.filter(
        student_id=OuterRef("student_id"), is_active=True, session__status="ACTIVE",
    ).order_by("-pk")
    return (
        visibility.inspectable(tenant, user, surface)
        .annotate(
            member_total=Coalesce(Subquery(members), Value(0)),
            message_total=Coalesce(Subquery(said), Value(0)),
            child_class_name=Subquery(placement.values("school_class__name")[:1]),
        )
        .select_related("student__branch", "branch")
        .prefetch_related(Prefetch(
            "participants",
            queryset=Participant.objects.filter(
                left_at__isnull=True,
                role__in=[
                    ParticipantRole.OWNER, ParticipantRole.SENDER,
                    ParticipantRole.PUPIL, ParticipantRole.STAFF,
                ],
            ).select_related("user"),
            to_attr="leaders",
        ))
        .order_by("-last_message_at", "-pk")
    )


def search(qs, term, *, titles=True, people=True, children=True):
    """Narrow an oversight list by title, by who is in it, and by the child's name.

    Message text is never searched: a list row carries none, and an inspector
    reads a conversation by opening it, which is recorded.
    """
    term = (term or "").strip()
    if not term:
        return qs
    match = Q(pk__in=[])
    if titles:
        match |= Q(title__icontains=term)
    if people:
        match |= Q(Q(participants__left_at__isnull=True) & (
            Q(participants__user__first_name__icontains=term)
            | Q(participants__user__last_name__icontains=term)
        ))
    if children:
        match |= Q(kind=ConversationKind.PUPIL) & (
            Q(student__first_name__icontains=term) | Q(student__last_name__icontains=term)
        )
    return qs.filter(match).distinct()


def record_opening(access, actor, *, what: str) -> None:
    """Write the audit event for one opening of an inspected conversation.

    *what* is ``"detail"`` or ``"messages"``. The event names the inspector
    and the conversation and carries no message text.
    """
    conversation = access.conversation
    label = conversation.title
    if conversation.kind == ConversationKind.PUPIL and conversation.student_id:
        label = f"{conversation.student.full_name}'s conversation with staff"
    audit.record(
        conversation, AuditActionType.CONVERSATION_INSPECTED, actor,
        f"{actor.full_name} read {label or 'a conversation'} "
        f"({INSPECT_SURFACES[access.surface].lower()}, {what}).",
        label=label, metadata={"surface": access.surface, "what": what},
    )
