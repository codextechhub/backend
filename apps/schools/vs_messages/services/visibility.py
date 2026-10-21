"""The only gate to a conversation: who may read it, write in it, and be told of it.

Nothing about a person's access is stamped into a token or stored on a
participant row. Every function here re-derives it from the links that matter
now, so the next request after an account is deactivated, a member of staff
leaves the roll, a guardian is unlinked from a child or a child leaves the
school, no longer finds the conversation.

**Who is current.**

* *Staff*: an active account of the school with a ``StaffProfile`` on the roll,
  holding an open participation whose role is a staff role.
* *A guardian in a family conversation*: ``Guardian.user`` is the caller, the
  ``StudentGuardian`` link between that guardian and the child still exists,
  and the child's status is one the portal serves.
* *A pupil in a pupil conversation*: ``Student.user`` is the caller and the
  status is one the portal serves.
* *A primary guardian reading a pupil conversation*: a ``StudentGuardian`` with
  ``is_primary`` links the caller's guardian record to the child. Recomputed on
  every request, so changing the primary guardian moves the view at once.
* *An inspector*: any active account holding the inspection key for the kind
  (checked by the route), with the conversation's branch inside their reach.

Every list is a queryset built from ``Exists`` and ``FilteredRelation``, never
a loop over participants, so a school of any size costs the same queries.

:func:`resolve` answers one conversation for one surface, or raises the
module's single ``ConversationNotFound``: a conversation at another school,
one that does not exist and one the caller may not reach are indistinguishable.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    FilteredRelation,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from ..constants import (
    INSPECT_SURFACES,
    STAFF_ROLES,
    SURFACE_PARENT,
    SURFACE_STAFF,
    SURFACE_STUDENT,
    ConversationKind,
    ParticipantRole,
    ReplyMode,
)
from ..exceptions import ConversationNotFound
from ..models import Conversation, Message, Participant
from .config import LazySettings, MessagingSettings
from .people import current_staff_user_ids

#: How a caller stands in a conversation.
MODE_PARTICIPANT = "participant"
MODE_INSPECTOR = "inspector"
MODE_WATCHING = "watching"


@dataclass(frozen=True)
class Access:
    """What one caller may do in one conversation, decided now.

    ``after_message_id`` is the newest message the caller may NOT read: they
    read ``id > after_message_id``. ``can_send`` and ``read_only`` are opposites
    carried together so a client never infers one from the other, and
    ``reason`` says in words why a read-only conversation is read-only.
    """

    conversation: Conversation
    mode: str
    participant: Participant | None
    after_message_id: int
    can_send: bool
    read_only: bool
    reason: str
    surface: str

    @property
    def inspecting(self) -> bool:
        return self.mode == MODE_INSPECTOR


@dataclass(frozen=True)
class CallerKeys:
    """The two staff keys that decide whether a staff caller may write."""

    send: bool = False
    family_send: bool = False


def caller_keys(tenant, user) -> CallerKeys:
    from vs_rbac.evaluator import has_permission

    from ..constants import PERM_FAMILY_SEND, PERM_SEND

    return CallerKeys(
        send=has_permission(user, PERM_SEND, tenant=tenant),
        family_send=has_permission(user, PERM_FAMILY_SEND, tenant=tenant),
    )


# ── Querysets ─────────────────────────────────────────────────────────────


def _with_participation(qs, user, *, required: bool):
    """Join the caller's open participation as ``me`` and expose its columns.

    ``required`` keeps only conversations the caller takes part in; without it
    the columns come back null for the ones they do not, and are read as zero.
    """
    qs = qs.annotate(me=FilteredRelation(
        "participants",
        condition=Q(participants__user_id=user.pk, participants__left_at__isnull=True),
    ))
    if required:
        qs = qs.filter(me__isnull=False)
    return qs.annotate(
        my_role=F("me__role"),
        my_after=Coalesce(F("me__after_message_id"), Value(0)),
        my_last_read=Coalesce(F("me__last_read_message_id"), Value(0)),
        my_joined=F("me__joined_at"),
    )


def _staff_side_current(tenant):
    """Whether the conversation's staff participant is current staff (a family or pupil conversation)."""
    return Exists(
        Participant.objects.filter(
            conversation=OuterRef("pk"), role=ParticipantRole.STAFF, left_at__isnull=True,
            user_id__in=current_staff_user_ids(tenant),
        )
    )


def _family_side_current(tenant):
    """Whether the family or pupil end of the conversation can still be reached."""
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import StudentGuardian

    linked = Exists(StudentGuardian.objects.filter(
        student_id=OuterRef("student_id"), guardian_id=OuterRef("guardian_id"),
        guardian__user__is_active=True,
    ))
    pupil = Q(student__user__isnull=False, student__user__is_active=True)
    return Case(
        When(Q(kind=ConversationKind.FAMILY, student__status__in=PORTAL_STATUSES) & linked,
             then=Value(True)),
        When(Q(kind=ConversationKind.PUPIL, student__status__in=PORTAL_STATUSES) & pupil,
             then=Value(True)),
        default=Value(False),
    )


def staff_conversations(tenant, user):
    """Conversations the signed-in member of staff takes part in, now.

    Empty unless *user* is current staff. Guardian and pupil participations
    belong to the portal surfaces and never appear here, so a teacher who is
    also a parent sees her own child's conversations in the parent app only.
    Annotated with ``my_role``, ``my_after``, ``my_last_read`` and ``my_joined``
    (the caller's participation), ``staff_ok`` and ``family_ok``.
    """
    qs = Conversation.objects.filter(tenant=tenant).filter(
        Exists(current_staff_user_ids(tenant).filter(user_id=user.pk))
    )
    qs = _with_participation(qs, user, required=True).filter(me__role__in=STAFF_ROLES)
    return qs.annotate(staff_ok=_staff_side_current(tenant), family_ok=_family_side_current(tenant))


def guardian_conversations(tenant, user, *, child=None):
    """What a guardian reads: own family conversations and, read-only, their children's pupil ones.

    A pupil conversation appears only for a child whose primary guardian the
    caller is (``watching`` is true on those rows). *child* narrows to one
    child. Annotated like :func:`staff_conversations` plus ``watching``.
    """
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import StudentGuardian

    links = StudentGuardian.objects.filter(tenant=tenant, student_id=OuterRef("student_id"))
    own = Q(kind=ConversationKind.FAMILY, guardian__user_id=user.pk) & Exists(
        links.filter(guardian_id=OuterRef("guardian_id"))
    )
    watching = Q(kind=ConversationKind.PUPIL) & Exists(
        links.filter(is_primary=True, guardian__user_id=user.pk)
    )
    qs = (
        Conversation.objects.filter(tenant=tenant, student__status__in=PORTAL_STATUSES)
        .filter(own | watching)
    )
    if child is not None:
        qs = qs.filter(student_id=getattr(child, "pk", child))
    qs = _with_participation(qs, user, required=False)
    return qs.annotate(
        staff_ok=_staff_side_current(tenant),
        watching=Case(When(kind=ConversationKind.PUPIL, then=Value(True)), default=Value(False)),
    )


def pupil_conversations(tenant, user):
    """The pupil conversations of the pupil whose account is *user*."""
    from schools.vs_students.constants import PORTAL_STATUSES

    qs = Conversation.objects.filter(
        tenant=tenant, kind=ConversationKind.PUPIL, student__user_id=user.pk,
        student__status__in=PORTAL_STATUSES,
    )
    qs = _with_participation(qs, user, required=False)
    return qs.annotate(staff_ok=_staff_side_current(tenant))


def inspectable(tenant, user, surface):
    """Conversations of one inspected kind whose branch is inside *user*'s reach.

    A group or broadcast with no branch (school-wide) needs a whole-school
    reach; a pupil conversation is judged by the child's current branch.
    """
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    qs = Conversation.objects.filter(tenant=tenant, kind=INSPECT_SURFACES[surface])
    reach = visible_branch_ids(user, tenant)
    if reach is WHOLE_TENANT:
        return qs
    branches = tuple(reach or ())
    if INSPECT_SURFACES[surface] == ConversationKind.PUPIL:
        return qs.filter(student__branch_id__in=branches)
    return qs.filter(branch_id__in=branches)


def annotate_unread(qs, user):
    """Add ``unread``: messages from others newer than the caller's watermark and join point."""
    newer = (
        Message.objects.filter(conversation_id=OuterRef("pk"))
        .filter(id__gt=OuterRef("my_after"))
        .filter(id__gt=OuterRef("my_last_read"))
        .exclude(sender_id=user.pk)
        .order_by()
        .values("conversation_id")
        .annotate(c=Count("pk"))
        .values("c")
    )
    return qs.annotate(unread=Coalesce(Subquery(newer), Value(0)))


# ── Writing ───────────────────────────────────────────────────────────────


def send_state(conversation, *, surface, role, staff_ok=True, family_ok=True,
               watching=False, keys: CallerKeys | None = None,
               settings: MessagingSettings | None = None, student_name: str = ""):
    """``(can_send, reason)`` for one caller in one conversation.

    The one place the write rules are stated, shared by :func:`resolve` and by
    every list row, so a row's ``can_send`` and the 409 a send would get
    cannot disagree. The reasons are sentences the person can be shown.
    """
    kind = conversation.kind
    settings = settings or MessagingSettings()
    if watching:
        name = f"{student_name}'s conversation" if student_name else "This conversation"
        return False, f"{name}. You can read it but not reply."
    if kind == ConversationKind.BROADCAST and role == ParticipantRole.RECIPIENT:
        if conversation.reply_mode == ReplyMode.PRIVATE_REPLIES:
            return False, "Reply to the sender privately instead of writing here."
        return False, "This broadcast does not take replies."
    if kind in (ConversationKind.FAMILY, ConversationKind.PUPIL):
        if not staff_ok:
            return False, "No longer at the school."
        if surface == SURFACE_STAFF and not family_ok:
            return False, "This family can no longer be reached here."
        if kind == ConversationKind.FAMILY and not settings.families_enabled:
            return False, "Messaging with parents is switched off at this school."
        if kind == ConversationKind.PUPIL and not settings.pupils_enabled:
            return False, "Messaging with pupils is switched off at this school."
    if surface == SURFACE_STAFF:
        keys = keys or CallerKeys()
        needed = keys.family_send if kind in (ConversationKind.FAMILY, ConversationKind.PUPIL) else keys.send
        if not needed:
            return False, "You do not have permission to write in this conversation."
    return True, ""


def _access(conversation, *, mode, surface, participant, after, can_send, reason):
    return Access(
        conversation=conversation, mode=mode, participant=participant,
        after_message_id=after, can_send=can_send, read_only=not can_send,
        reason=reason, surface=surface,
    )


def _participation(conversation, user, role=None):
    """The caller's open participation, made if a portal account has none yet."""
    row = Participant.objects.filter(
        conversation=conversation, user_id=user.pk, left_at__isnull=True,
    ).first()
    if row is None and role is not None:
        row = Participant.objects.create(
            tenant_id=conversation.tenant_id, conversation=conversation, user=user, role=role,
        )
    return row


def resolve(tenant, user, conversation_id, *, surface=SURFACE_STAFF, settings=None, keys=None) -> Access:
    """The caller's standing in conversation *conversation_id* on *surface*.

    Raises :class:`ConversationNotFound` for anything the caller may not
    reach, whatever the reason, with the same answer every time.
    """
    settings = settings or LazySettings(tenant)
    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        raise ConversationNotFound() from None

    if surface == SURFACE_STAFF:
        row = (
            staff_conversations(tenant, user).filter(pk=conversation_id)
            .select_related("student").first()
        )
        if row is None:
            raise ConversationNotFound()
        can_send, reason = send_state(
            row, surface=surface, role=row.my_role, staff_ok=row.staff_ok,
            family_ok=row.family_ok, keys=keys or caller_keys(tenant, user), settings=settings,
        )
        participant = _participation(row, user)
        return _access(row, mode=MODE_PARTICIPANT, surface=surface, participant=participant,
                       after=row.my_after, can_send=can_send, reason=reason)

    if surface == SURFACE_PARENT:
        row = (
            guardian_conversations(tenant, user).filter(pk=conversation_id)
            .select_related("student").first()
        )
        if row is None:
            raise ConversationNotFound()
        if row.watching:
            can_send, reason = send_state(
                row, surface=surface, role=None, watching=True,
                student_name=row.student.first_name,
            )
            return _access(row, mode=MODE_WATCHING, surface=surface, participant=None,
                           after=0, can_send=False, reason=reason)
        can_send, reason = send_state(
            row, surface=surface, role=ParticipantRole.GUARDIAN, staff_ok=row.staff_ok,
            settings=settings,
        )
        participant = _participation(row, user, ParticipantRole.GUARDIAN)
        return _access(row, mode=MODE_PARTICIPANT, surface=surface, participant=participant,
                       after=participant.after_message_id, can_send=can_send, reason=reason)

    if surface == SURFACE_STUDENT:
        row = pupil_conversations(tenant, user).filter(pk=conversation_id).first()
        if row is None:
            raise ConversationNotFound()
        can_send, reason = send_state(
            row, surface=surface, role=ParticipantRole.PUPIL, staff_ok=row.staff_ok,
            settings=settings,
        )
        participant = _participation(row, user, ParticipantRole.PUPIL)
        return _access(row, mode=MODE_PARTICIPANT, surface=surface, participant=participant,
                       after=participant.after_message_id, can_send=can_send, reason=reason)

    if surface in INSPECT_SURFACES:
        row = inspectable(tenant, user, surface).filter(pk=conversation_id).first()
        if row is None:
            raise ConversationNotFound()
        return _access(row, mode=MODE_INSPECTOR, surface=surface, participant=None, after=0,
                       can_send=False, reason="Inspection is read-only.")

    raise ConversationNotFound()


def visible_messages(access: Access):
    """The messages *access* may read, oldest first.

    A participant reads ``id > after_message_id``; an inspector and a primary
    guardian read every message of the conversation resolved. A broadcast
    inspection therefore sees the broadcast's own messages only: replies live
    in their own conversations, which no inspection surface resolves.
    """
    return Message.objects.filter(
        conversation_id=access.conversation.pk, id__gt=access.after_message_id,
    )


# ── Counts ────────────────────────────────────────────────────────────────


def unread_counts(tenant, user, *, surface=SURFACE_STAFF) -> dict:
    """Unread messages for the caller in one aggregate query.

    Counts messages in the caller's open participations that are newer than
    both the point they joined and their read watermark, sent by somebody else.
    A staff caller gets a total and a count per kind group (``direct``,
    ``family``, ``group``, ``broadcast``); a parent or pupil gets a total over
    their own conversations (a primary guardian's read-only view is never
    counted).
    """
    if surface == SURFACE_STAFF:
        visible = staff_conversations(tenant, user)
    elif surface == SURFACE_PARENT:
        visible = guardian_conversations(tenant, user).filter(kind=ConversationKind.FAMILY)
    elif surface == SURFACE_STUDENT:
        visible = pupil_conversations(tenant, user)
    else:
        raise ConversationNotFound()

    newer = (
        Q(conversation__messages__id__gt=F("after_message_id"))
        & Q(conversation__messages__id__gt=F("last_read_message_id"))
        & ~Q(conversation__messages__sender_id=user.pk)
    )
    rows = (
        Participant.objects.filter(
            user_id=user.pk, left_at__isnull=True, conversation_id__in=visible.values("pk"),
        )
        .values("conversation__kind")
        .annotate(n=Count("conversation__messages", filter=newer))
        .order_by()
    )
    by_kind = {row["conversation__kind"]: row["n"] for row in rows}
    if surface != SURFACE_STAFF:
        return {"total": sum(by_kind.values())}
    out = {
        "direct": by_kind.get(ConversationKind.STAFF_DIRECT, 0),
        "family": by_kind.get(ConversationKind.FAMILY, 0) + by_kind.get(ConversationKind.PUPIL, 0),
        "group": by_kind.get(ConversationKind.GROUP, 0),
        "broadcast": (by_kind.get(ConversationKind.BROADCAST, 0)
                      + by_kind.get(ConversationKind.BROADCAST_REPLY, 0)),
    }
    out["total"] = sum(out.values())
    return out


def conversation_unread(access: Access, user) -> int:
    """Unread messages in one conversation for the caller (zero when inspecting or watching)."""
    if access.participant is None:
        return 0
    participant = Participant.objects.get(pk=access.participant.pk)
    return Message.objects.filter(
        conversation_id=access.conversation.pk,
        id__gt=max(participant.after_message_id, participant.last_read_message_id),
    ).exclude(sender_id=user.pk).count()


# ── Alerts ────────────────────────────────────────────────────────────────


def _family_side(conversation) -> bool:
    """Whether the family or pupil end of *conversation* can be reached now."""
    return (
        Conversation.objects.filter(pk=conversation.pk)
        .annotate(ok=_family_side_current(conversation.tenant))
        .values_list("ok", flat=True)
        .first()
    ) is True


def alert_recipients(message) -> list[Participant]:
    """The participants to alert about *message*, in one pass over the participants.

    Open participants other than the sender who pass the current-access rules
    now, and who had nothing unread in the conversation before this message,
    so one alert covers a run of messages until the conversation is read. A
    primary guardian reading a pupil conversation is never a participant and
    is never alerted. A person who joined after the message, or has already
    read it by the time a late worker runs, is not alerted to it.
    """
    conversation = message.conversation
    tenant = conversation.tenant
    prior = (
        Message.objects.filter(conversation_id=conversation.pk, id__lt=message.pk)
        .filter(id__gt=OuterRef("after_message_id"))
        .filter(id__gt=OuterRef("last_read_message_id"))
        .exclude(sender_id=OuterRef("user_id"))
    )
    candidates = (
        Participant.objects.filter(conversation_id=conversation.pk, left_at__isnull=True)
        .filter(after_message_id__lt=message.pk, last_read_message_id__lt=message.pk)
        .exclude(user_id=message.sender_id)
        .filter(user__is_active=True, user__tenant_id=conversation.tenant_id)
        .annotate(had_unread=Exists(prior))
        .filter(had_unread=False)
        .select_related("user")
    )
    current = Q(role__in=STAFF_ROLES, user_id__in=current_staff_user_ids(tenant))
    if conversation.kind == ConversationKind.FAMILY and _family_side(conversation):
        current |= Q(role=ParticipantRole.GUARDIAN, user_id=conversation.guardian.user_id)
    elif conversation.kind == ConversationKind.PUPIL and _family_side(conversation):
        current |= Q(role=ParticipantRole.PUPIL, user_id=conversation.student.user_id)
    return list(candidates.filter(current))
