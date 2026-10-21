"""Starting a conversation: staff to staff, family to staff, pupil to staff.

Starting is find-or-create on the conversation's ``pair_key``, so two taps, two
devices or a retried request make one conversation, and a second person
starting "the same" conversation lands in the first. A simultaneous pair of
creates is settled by the unique constraint: the loser catches the
``IntegrityError`` and reads the winner's row.

Who may be addressed is decided by :mod:`.people` and is the same answer for a
person who is out of reach and for one who does not exist
(:class:`~schools.vs_messages.exceptions.RecipientUnavailable`), so a start
request cannot be used to learn who is on the staff of another branch.

A conversation can be started empty, but the other end only sees it in their
inbox once something has been said (``services.inbox``).
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

from ..constants import (
    SURFACE_PARENT,
    SURFACE_STAFF,
    SURFACE_STUDENT,
    ConversationKind,
    ParticipantRole,
)
from ..exceptions import ConversationReadOnly, RecipientUnavailable
from ..models import Conversation, Participant
from . import config, messages, people, visibility


def open_conversation(tenant, *, kind, pair_key, creator, participants, **fields):
    """Find the conversation with *pair_key*, or create it with *participants*.

    *participants* is a list of ``(user_id, role)``. Returns
    ``(conversation, created)``. A found conversation is brought up to date for
    a portal account that replaced an earlier one on the same record.
    """
    existing = Conversation.objects.filter(tenant=tenant, pair_key=pair_key).first()
    if existing is not None:
        _ensure_participants(existing, participants)
        return existing, False
    try:
        with transaction.atomic():
            conversation = Conversation.objects.create(
                tenant=tenant, kind=kind, pair_key=pair_key, created_by=creator, **fields,
            )
            Participant.objects.bulk_create([
                Participant(tenant=tenant, conversation=conversation, user_id=user_id, role=role)
                for user_id, role in participants
            ])
        return conversation, True
    except IntegrityError:
        existing = Conversation.all_objects.filter(tenant=tenant, pair_key=pair_key).first()
        if existing is None:
            raise
        return existing, False


def _ensure_participants(conversation, participants) -> None:
    """Open a participation for any account in *participants* that has none."""
    open_users = set(
        Participant.objects.filter(conversation=conversation, left_at__isnull=True)
        .values_list("user_id", flat=True)
    )
    missing = [(u, r) for u, r in participants if u not in open_users]
    if missing:
        Participant.objects.bulk_create([
            Participant(
                tenant_id=conversation.tenant_id, conversation=conversation,
                user_id=user_id, role=role,
            )
            for user_id, role in missing
        ])


def _first_message(tenant, user, conversation, surface, body, client_token):
    """Send *body* in a conversation just opened, as the caller who opened it."""
    if not (body or "").strip():
        return None
    access = visibility.resolve(tenant, user, conversation.pk, surface=surface)
    message, _ = messages.send_message(access, sender=user, body=body, client_token=client_token)
    return message


def _require_current_staff(tenant, actor) -> None:
    if not people.is_current_staff(tenant, actor):
        raise RecipientUnavailable()


def _writable(settings, kind) -> None:
    if kind == ConversationKind.FAMILY and not settings.families_enabled:
        raise ConversationReadOnly("Messaging with parents is switched off at this school.")
    if kind == ConversationKind.PUPIL and not settings.pupils_enabled:
        raise ConversationReadOnly("Messaging with pupils is switched off at this school.")


# ── Staff to staff ────────────────────────────────────────────────────────


def start_staff_direct(tenant, actor, *, recipient_id, body="", client_token=None):
    """Open, or find, the direct conversation between *actor* and *recipient_id*.

    Returns ``(conversation, message_or_None, created)``. The recipient must be
    staff the actor may address (:func:`people.staff_contacts`).
    """
    _require_current_staff(tenant, actor)
    try:
        recipient_id = int(recipient_id)
    except (TypeError, ValueError):
        raise RecipientUnavailable() from None
    if recipient_id not in people.contactable_staff_ids(tenant, actor, [recipient_id]):
        raise RecipientUnavailable()
    low, high = sorted((actor.pk, recipient_id))
    conversation, created = open_conversation(
        tenant, kind=ConversationKind.STAFF_DIRECT, pair_key=f"staff:{low}:{high}",
        creator=actor,
        participants=[(actor.pk, ParticipantRole.MEMBER), (recipient_id, ParticipantRole.MEMBER)],
    )
    message = _first_message(tenant, actor, conversation, SURFACE_STAFF, body, client_token)
    return conversation, message, created


# ── Staff to a family or a pupil ──────────────────────────────────────────


def start_family_from_staff(tenant, actor, *, kind, student_id, guardian_id=None, body="",
                            client_token=None):
    """Open, or find, the family or pupil conversation *actor* starts about a child.

    *kind* is ``FAMILY`` (a guardian is named) or ``PUPIL``. The child must be
    one the actor may reach (:func:`people.family_children`) and the person
    addressed must have a working account.
    """
    settings = config.load_settings(tenant)
    _require_current_staff(tenant, actor)
    _writable(settings, kind)
    try:
        student_id = int(student_id)
    except (TypeError, ValueError):
        raise RecipientUnavailable() from None
    child = people.family_children(tenant, actor, settings).filter(pk=student_id).first()
    if child is None:
        raise RecipientUnavailable()

    if kind == ConversationKind.PUPIL:
        pupil = child.user
        if pupil is None or not pupil.is_active:
            raise RecipientUnavailable()
        conversation, created = open_conversation(
            tenant, kind=kind, pair_key=f"pupil:{child.pk}:{actor.pk}", creator=actor,
            participants=[(pupil.pk, ParticipantRole.PUPIL), (actor.pk, ParticipantRole.STAFF)],
            student=child,
        )
    else:
        link = next((l for l in child.guardian_links.all() if l.guardian_id == _int(guardian_id)), None)
        account = link.guardian.user if link is not None else None
        if account is None or not account.is_active:
            raise RecipientUnavailable()
        conversation, created = open_conversation(
            tenant, kind=kind,
            pair_key=f"family:{link.guardian_id}:{actor.pk}:{child.pk}", creator=actor,
            participants=[(account.pk, ParticipantRole.GUARDIAN), (actor.pk, ParticipantRole.STAFF)],
            student=child, guardian=link.guardian,
        )
    message = _first_message(tenant, actor, conversation, SURFACE_STAFF, body, client_token)
    return conversation, message, created


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── A family or a pupil to staff ──────────────────────────────────────────


def start_from_guardian(tenant, user, *, student_id, staff_user_id, body="", client_token=None):
    """A guardian writes to a member of staff about one of their children.

    The child must be linked to the caller's guardian record and on the roll,
    and the staff member must be one the family may reach
    (:func:`people.staff_reachable_for_child`). Returns
    ``(conversation, message_or_None, created)``.
    """
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import Guardian, Student

    settings = config.load_settings(tenant)
    _writable(settings, ConversationKind.FAMILY)
    staff_user_id = _int(staff_user_id)
    guardian = Guardian.objects.filter(tenant=tenant, user_id=user.pk).first()
    child = (
        Student.objects.filter(
            tenant=tenant, pk=_int(student_id), status__in=PORTAL_STATUSES,
            guardian_links__guardian=guardian,
        ).first()
        if guardian is not None else None
    )
    if child is None or staff_user_id not in people.staff_reachable_for_child(tenant, child, settings):
        raise RecipientUnavailable()
    conversation, created = open_conversation(
        tenant, kind=ConversationKind.FAMILY,
        pair_key=f"family:{guardian.pk}:{staff_user_id}:{child.pk}", creator=user,
        participants=[(user.pk, ParticipantRole.GUARDIAN), (staff_user_id, ParticipantRole.STAFF)],
        student=child, guardian=guardian,
    )
    message = _first_message(tenant, user, conversation, SURFACE_PARENT, body, client_token)
    return conversation, message, created


def start_from_pupil(tenant, user, *, staff_user_id, body="", client_token=None):
    """A pupil writes to a member of staff who teaches them or is the school office."""
    from schools.vs_students.constants import PORTAL_STATUSES
    from schools.vs_students.models import Student

    settings = config.load_settings(tenant)
    _writable(settings, ConversationKind.PUPIL)
    staff_user_id = _int(staff_user_id)
    child = Student.objects.filter(
        tenant=tenant, user_id=user.pk, status__in=PORTAL_STATUSES,
    ).first()
    if child is None or staff_user_id not in people.staff_reachable_for_child(tenant, child, settings):
        raise RecipientUnavailable()
    conversation, created = open_conversation(
        tenant, kind=ConversationKind.PUPIL,
        pair_key=f"pupil:{child.pk}:{staff_user_id}", creator=user,
        participants=[(user.pk, ParticipantRole.PUPIL), (staff_user_id, ParticipantRole.STAFF)],
        student=child,
    )
    message = _first_message(tenant, user, conversation, SURFACE_STUDENT, body, client_token)
    return conversation, message, created
