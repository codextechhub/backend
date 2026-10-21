"""Staff groups: making one, naming it, and who is in it.

A group holds staff only. Its creator becomes an owner, and only an owner may
rename it, add people, remove people or make another owner; anyone may leave.

**What a member can read.** A person added to a busy group reads only what is
said after they join: their participation starts at the group's newest message
(``after_message_id``). Removing a member closes their period and ends all
access, including to what was said while they were in; adding them again opens
a new period that starts afresh.

**Reach.** A group may belong to one branch or to the whole school. A
school-wide group needs a whole-school reach to create, and its members are the
staff the creator can address; a branch group narrows them to staff posted at
that branch or school-wide. The owner's reach, not the group's, decides whom
they may add later.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditActionType

from ..constants import RECIPIENTS_MAX, TITLE_MAX, ConversationKind, ParticipantRole
from ..exceptions import (
    BranchOutOfReach,
    ConversationNotFound,
    GroupNeedsOwner,
    NotAMember,
    NotGroupOwner,
    RecipientsInvalid,
    RecipientUnavailable,
    TitleInvalid,
)
from ..models import Conversation, Message, Participant
from . import audit, messages, people, visibility
from ..constants import SURFACE_STAFF


def clean_title(title, *, what: str = "group") -> str:
    """A group's name or a broadcast's title trimmed, refused when empty or too long."""
    text = (title or "").strip()
    if not text:
        raise TitleInvalid(f"Give the {what} a {'title' if what == 'broadcast' else 'name'}.")
    if len(text) > TITLE_MAX:
        raise TitleInvalid(f"Keep the {'title' if what == 'broadcast' else 'name'} to {TITLE_MAX} characters.")
    return text


def clean_ids(ids, *, allow_empty: bool) -> list:
    """Distinct integer ids in the order given; an unreadable id is refused."""
    if not isinstance(ids, (list, tuple)):
        raise RecipientsInvalid()
    out = []
    for raw in ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise RecipientUnavailable() from None
        if value not in out:
            out.append(value)
    if len(out) > RECIPIENTS_MAX:
        raise RecipientsInvalid(f"Choose at most {RECIPIENTS_MAX:,} people.")
    if not out and not allow_empty:
        raise RecipientsInvalid()
    return out


def branch_for(tenant, actor, branch_id):
    """The branch a new group or broadcast belongs to, checked against *actor*'s reach.

    ``None`` means school-wide and needs a whole-school reach. A branch that is
    not the school's and a branch outside the reach are refused alike.
    """
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
    from vs_tenants.models import Branch

    reach = visible_branch_ids(actor, tenant)
    if branch_id in (None, ""):
        if reach is not WHOLE_TENANT:
            raise BranchOutOfReach(
                "A group or broadcast for the whole school needs a reach over every branch."
            )
        return None
    try:
        branch = Branch.all_objects.filter(tenant=tenant, pk=int(branch_id)).first()
    except (TypeError, ValueError):
        branch = None
    if branch is None or (reach is not WHOLE_TENANT and branch.pk not in reach):
        raise BranchOutOfReach()
    return branch


def existing_by_token(tenant, sender, token, kind):
    """The conversation of *kind* a retried create already made, if any."""
    if not token:
        return None
    message = (
        Message.objects.filter(
            tenant=tenant, sender=sender, client_token=token, conversation__kind=kind,
        )
        .select_related("conversation")
        .first()
    )
    return message.conversation if message else None


def _require_owner(access) -> None:
    if (
        access.conversation.kind != ConversationKind.GROUP
        or access.participant is None
        or access.participant.role != ParticipantRole.OWNER
    ):
        raise NotGroupOwner()


def group_access(tenant, user, group_id):
    """Resolve *group_id* on the staff surface, refusing anything that is not a group."""
    access = visibility.resolve(tenant, user, group_id, surface=SURFACE_STAFF)
    if access.conversation.kind != ConversationKind.GROUP:
        raise ConversationNotFound()
    return access


@transaction.atomic
def create_group(tenant, actor, *, name, branch_id=None, member_ids=(), body="", client_token=None):
    """Make a group with *actor* as owner and *member_ids* as members.

    Returns ``(conversation, created)``. A retried create carrying the token of
    a first message already stored returns the group it made. Every member must
    be staff the actor may address, in the group's branch.
    """
    if not people.is_current_staff(tenant, actor):
        raise RecipientUnavailable()
    found = existing_by_token(tenant, actor, client_token, ConversationKind.GROUP)
    if found is not None:
        return found, False
    title = clean_title(name)
    branch = branch_for(tenant, actor, branch_id)
    requested = [i for i in clean_ids(member_ids, allow_empty=True) if i != actor.pk]
    allowed = people.contactable_staff_ids(
        tenant, actor, requested, narrow_branch_id=branch.pk if branch else None,
    )
    if set(requested) - allowed:
        raise RecipientUnavailable()
    group = Conversation.objects.create(
        tenant=tenant, kind=ConversationKind.GROUP, title=title, branch=branch, created_by=actor,
    )
    Participant.objects.bulk_create(
        [Participant(tenant=tenant, conversation=group, user=actor, role=ParticipantRole.OWNER)]
        + [
            Participant(tenant=tenant, conversation=group, user_id=i, role=ParticipantRole.MEMBER)
            for i in requested
        ]
    )
    audit.record(
        group, AuditActionType.MESSAGE_GROUP_CREATED, actor,
        f'{actor.full_name} created the group "{title}" with {len(requested) + 1} people.',
        label=title, metadata={"members": len(requested) + 1},
    )
    if (body or "").strip():
        messages.post_message(group, actor, body, client_token=client_token)
    return group, True


@transaction.atomic
def rename_group(access, actor, name):
    """An owner renames the group."""
    _require_owner(access)
    group = Conversation.all_objects.select_for_update().get(pk=access.conversation.pk)
    title = clean_title(name)
    before = group.title
    if title != before:
        group.title = title
        group.save(update_fields=["title"])
        audit.record(
            group, AuditActionType.MESSAGE_GROUP_RENAMED, actor,
            f'{actor.full_name} renamed the group "{before}" to "{title}".',
            label=title,
        )
    return group


@transaction.atomic
def add_members(access, actor, member_ids):
    """An owner adds staff. Each new member reads only what is said from now on."""
    _require_owner(access)
    group = Conversation.all_objects.select_for_update().get(pk=access.conversation.pk)
    requested = clean_ids(member_ids, allow_empty=False)
    allowed = people.contactable_staff_ids(
        group.tenant, actor, requested, narrow_branch_id=group.branch_id,
    )
    if set(requested) - allowed:
        raise RecipientUnavailable()
    present = set(
        Participant.objects.filter(conversation=group, left_at__isnull=True)
        .values_list("user_id", flat=True)
    )
    fresh = [i for i in requested if i not in present]
    Participant.objects.bulk_create([
        Participant(
            tenant_id=group.tenant_id, conversation=group, user_id=i,
            role=ParticipantRole.MEMBER, after_message_id=group.last_message_id or 0,
        )
        for i in fresh
    ])
    if fresh:
        audit.record(
            group, AuditActionType.MESSAGE_GROUP_MEMBERS_CHANGED, actor,
            f'{actor.full_name} added {len(fresh)} people to the group "{group.title}".',
            label=group.title, metadata={"added": fresh},
        )
    return group


@transaction.atomic
def remove_member(access, actor, user_id):
    """An owner removes a member, or a member leaves.

    The last owner cannot go while other members remain: someone else must be
    made an owner first. Returns the closed participation.
    """
    group = Conversation.all_objects.select_for_update().get(pk=access.conversation.pk)
    if group.kind != ConversationKind.GROUP:
        raise ConversationNotFound()
    leaving = user_id == actor.pk
    if not leaving:
        _require_owner(access)
    target = Participant.objects.filter(
        conversation=group, user_id=user_id, left_at__isnull=True,
    ).first()
    if target is None:
        raise NotAMember()
    if target.role == ParticipantRole.OWNER:
        open_rows = Participant.objects.filter(conversation=group, left_at__isnull=True)
        owners = open_rows.filter(role=ParticipantRole.OWNER).count()
        if owners <= 1 and open_rows.count() > 1:
            raise GroupNeedsOwner()
    target.left_at = timezone.now()
    target.removed_by = None if leaving else actor
    target.save(update_fields=["left_at", "removed_by"])
    verb = "left" if leaving else "removed a member from"
    audit.record(
        group, AuditActionType.MESSAGE_GROUP_MEMBERS_CHANGED, actor,
        f'{actor.full_name} {verb} the group "{group.title}".',
        label=group.title, metadata={"removed": [user_id], "left": leaving},
    )
    return target


@transaction.atomic
def make_owner(access, actor, user_id):
    """An owner makes a current member an owner too."""
    _require_owner(access)
    group = access.conversation
    target = Participant.objects.select_for_update().filter(
        conversation=group, user_id=user_id, left_at__isnull=True,
    ).first()
    if target is None:
        raise NotAMember()
    if target.role != ParticipantRole.OWNER:
        target.role = ParticipantRole.OWNER
        target.save(update_fields=["role"])
        audit.record(
            group, AuditActionType.MESSAGE_GROUP_MEMBERS_CHANGED, actor,
            f'{actor.full_name} made a member an owner of the group "{group.title}".',
            label=group.title, metadata={"owner": user_id},
        )
    return target
