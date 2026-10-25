"""Closing your own account: who may, how, and who is told.

Mrs Okafor uses a phone app to follow her son's school life. She opens
Settings, taps Delete my account and confirms. Her sign-in closes on the spot:
every phone and browser she was signed in on is signed out, her password stops
working and her push notifications stop. Nothing the school keeps about her
or her son changes, because her name, phone and email on his record are the
school's to keep. The school's administrator is told, and can reopen the
account later if she changes her mind.

Tunde, her son, has his own sign-in for the pupil app. When he taps the same
button nothing closes. His request goes to Meridian Academy, his sign-in keeps
working, and he sees that the request is with the school until an
administrator closes the account or declines the request.

**Which of the two an account gets is the product's decision, not this
engine's.** A product registers a policy from its ``AppConfig.ready()``:
``provider(user) -> ClosurePolicy | None``. The policy names the ``mode``
(``close`` at once, or ``request`` for the tenant to decide), the one branch
the person's record belongs to (or none, when it spans several) and the word
the product uses for the account. An account no provider answers for, such as
a member of staff or a platform account, cannot delete itself here and is
refused with a plain pointer to its administrator.

**Deleting an account is closing it, never erasing records.** The account is
deactivated, its password made unusable, every session and refresh credential
ended, every push device forgotten (``account_closed`` is the signal a module
holding devices listens to) and the closure recorded. The same steps run
whoever asks and whoever decides, so "close" has one meaning.

**Proof by email for the web page.** A person without the app asks on a web
page, naming their email or sign-in identifier and the tenant. If an eligible
account exists a one-time link is emailed to the addresses the account is
reachable at; following it applies the same rule as the app. The request
answers identically whether or not the account exists.

**Review.** A request waiting for the tenant is read and decided by holders of
``PERM_VIEW`` and ``PERM_UPDATE``, narrowed to their branches like any
transaction. Those holders are also told when a request arrives and when an
account closes at once, in words that follow each reader's Field Access: a
reader who may not see who asked is told "Someone".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

from django.db import transaction
from django.utils import timezone

from ..action_tokens import account_closure_token_digest, issue_account_closure_token
from ..auth_events import AuthEvent
from ..models import AccountClosureLink, AccountClosureRequest, User
from ..signals import account_closed
from .audit import blacklist_all_user_tokens, get_client_ip, log_auth_event
from .sign_in_identifiers import extra_delivery_addresses
from .sign_in_scope import resolve_identifier_account, resolve_sign_in_account

logger = logging.getLogger(__name__)

MODE_CLOSE = "close"
MODE_REQUEST = "request"

#: The keys that read and decide closure requests.
PERM_VIEW = "school.account_closures.view"
PERM_UPDATE = "school.account_closures.update"

#: The Field Access key that decides whether a reviewer may read who asked.
PERSON_FIELD = "school.account_closures.person"

EVENT_REQUESTED = "user.deletion_requested"
EVENT_CLOSED = "user.account_deleted"
EVENT_LINK = "user.account_closure_link"

#: How long an emailed confirmation link lives.
LINK_LIFETIME = timedelta(hours=24)

#: What a reader who may not see who asked is told.
UNNAMED_PERSON = "Someone"

ORIGIN_SELF = "SELF_SERVICE"
ORIGIN_REVIEW = "TENANT_REVIEW"


@dataclass(frozen=True)
class ClosurePolicy:
    """How an account may be closed by its own holder.

    ``mode`` is ``close`` (the account closes when the person confirms) or
    ``request`` (the confirmation becomes a request the tenant decides).
    ``branch_id`` is the one branch the record belongs to, or None when the
    person spans several or none. ``kind`` is the product's word for the
    account ("parent", "pupil"), shown to reviewers.
    """

    mode: str
    branch_id: Optional[int] = None
    kind: str = ""


PolicyProvider = Callable[[User], Optional[ClosurePolicy]]

_providers: dict[str, PolicyProvider] = {}


class ClosureNotOffered(Exception):
    """The account is not one a product lets its holder close."""


class ClosureNotPending(Exception):
    """The request has already been decided."""


class ClosureLinkInvalid(Exception):
    """The emailed link is unknown, used, expired or no longer applies."""


@dataclass(frozen=True)
class ClosureOutcome:
    """What a confirmed request led to: ``closed`` or ``requested``, and its record."""

    status: str
    record: AccountClosureRequest


CLOSED = "closed"
REQUESTED = "requested"


def register_account_closure_policy(name: str, provider: PolicyProvider) -> None:
    """Make *provider* decide closure for the accounts it knows."""
    _providers[name] = provider


def closure_policy(user) -> Optional[ClosurePolicy]:
    """The policy the first answering provider gives *user*, or None.

    A provider that raises is logged and treated as not answering, so a broken
    product hook refuses a closure rather than breaking the screen that asks.
    """
    if user is None or not user.pk:
        return None
    for name, provider in list(_providers.items()):
        try:
            policy = provider(user)
        except Exception:  # noqa: BLE001 - a product hook never breaks the caller
            logger.exception("Account closure policy %r failed.", name)
            continue
        if policy is not None:
            return policy
    return None


def closure_state(user) -> dict:
    """What the signed-in person may do: ``{eligible, mode, pending}``."""
    policy = closure_policy(user)
    if policy is None or not user.may_sign_in:
        return {"eligible": False, "mode": None, "pending": None}
    pending = AccountClosureRequest.all_objects.filter(
        user=user, status=AccountClosureRequest.Status.PENDING,
    ).first()
    return {
        "eligible": True,
        "mode": policy.mode,
        "pending": {"requested_at": pending.requested_at} if pending else None,
    }


@transaction.atomic
def close_account(user, *, actor, request=None, origin: str = ORIGIN_SELF) -> User:
    """Close *user*'s sign-in account, whoever asked.

    Works when *actor* is the person themselves, which
    ``UserStatusService.deactivate`` refuses. The account is deactivated and
    given an unusable password, every session ends (device sessions and
    impersonations included) and ``account_closed`` lets other modules forget
    what they hold for the person. Records a tenant keeps about the person are
    not touched. Safe to repeat: a second run changes nothing it has not
    already changed.
    """
    locked = User.objects.select_for_update().get(pk=user.pk)
    locked.status = User.Status.DEACTIVATED
    locked.is_active = False
    locked.set_unusable_password()
    locked.save(update_fields=["status", "is_active", "password", "updated_at"])
    blacklist_all_user_tokens(locked, reason="ACCOUNT_CLOSED")
    account_closed.send(sender=User, user=locked, actor=actor)
    log_auth_event(
        actor=actor, subject=locked, tenant=locked.tenant,
        event=AuthEvent.ACCOUNT_DEACTIVATED, request=request,
        metadata={"origin": origin},
    )
    return locked


@transaction.atomic
def request_closure(user, *, channel: str, request=None) -> ClosureOutcome:
    """Apply the product's rule to a confirmed wish to delete an account.

    ``close`` accounts close now and the closure is recorded with the person
    as the decider. ``request`` accounts get a pending request, or the one they
    already have: asking twice never makes two. Raises ``ClosureNotOffered``
    for an account no policy answers for or one that cannot sign in.
    """
    policy = closure_policy(user)
    if policy is None:
        raise ClosureNotOffered()
    person = User.objects.select_for_update().get(pk=user.pk)
    if not person.may_sign_in:
        raise ClosureNotOffered()

    Status = AccountClosureRequest.Status
    if policy.mode == MODE_CLOSE:
        record = AccountClosureRequest.all_objects.create(
            tenant_id=person.tenant_id, user=person, branch_id=policy.branch_id,
            kind=policy.kind, channel=channel, status=Status.CLOSED,
            decided_at=timezone.now(), decided_by=person,
        )
        close_account(person, actor=person, request=request, origin=ORIGIN_SELF)
        _queue_reviewer_alert(record, EVENT_CLOSED)
        return ClosureOutcome(CLOSED, record)

    pending = AccountClosureRequest.all_objects.filter(
        user=person, status=Status.PENDING,
    ).first()
    if pending is not None:
        return ClosureOutcome(REQUESTED, pending)
    record = AccountClosureRequest.all_objects.create(
        tenant_id=person.tenant_id, user=person, branch_id=policy.branch_id,
        kind=policy.kind, channel=channel, status=Status.PENDING,
    )
    _queue_reviewer_alert(record, EVENT_REQUESTED)
    return ClosureOutcome(REQUESTED, record)


@transaction.atomic
def decide(record, *, actor, approve: bool, request=None) -> AccountClosureRequest:
    """Close the account behind a pending request, or decline the request.

    Raises ``ClosureNotPending`` when somebody has already decided it, and
    ``ClosureNotOffered`` when approving an account that no policy answers for
    any more (a pupil's account that has since become a member of staff's), so
    a stale request can never close a working account.
    """
    Status = AccountClosureRequest.Status
    locked = (
        AccountClosureRequest.all_objects.select_for_update()
        .select_related("user").get(pk=record.pk)
    )
    if locked.status != Status.PENDING:
        raise ClosureNotPending()
    if approve:
        if closure_policy(locked.user) is None:
            raise ClosureNotOffered()
        close_account(locked.user, actor=actor, request=request, origin=ORIGIN_REVIEW)
    locked.status = Status.CLOSED if approve else Status.DECLINED
    locked.decided_at = timezone.now()
    locked.decided_by = actor
    locked.save(update_fields=["status", "decided_at", "decided_by", "updated_at"])
    return locked


# ── The emailed link ──────────────────────────────────────────────────────────

def request_link(*, email: str = "", identifier: str = "", tenant: str | None = None,
                 request=None) -> None:
    """Email a confirmation link to an eligible, active account, and say nothing either way.

    The account is found inside the asserted tenant exactly as a password reset
    finds it (:mod:`vs_user.services.sign_in_scope`), by email or by an
    identifier the tenant issued. An unknown account, one in another tenant, a
    closed one, one no policy answers for and one with nowhere to send a link
    all return quietly, so the answer never reveals whether an account exists.
    The link goes to the addresses the account is reachable at, never to what
    was typed.
    """
    if identifier:
        user, _resolved, scope_failure = resolve_identifier_account(
            identifier=identifier, tenant=tenant,
        )
    else:
        user, _resolved, scope_failure = resolve_sign_in_account(
            email=email, tenant=tenant,
        )
    if scope_failure or user is None or not user.may_sign_in:
        return
    if closure_policy(user) is None:
        return
    if not user.email and not extra_delivery_addresses(user):
        return
    _create_and_send_link(user, request=request)


@transaction.atomic
def _create_and_send_link(user, *, request=None) -> None:
    """Retire any earlier link, store the new digest and queue the email after commit."""
    person = User.objects.select_for_update(of=("self",)).get(pk=user.pk)
    AccountClosureLink.objects.filter(user=person, used_at__isnull=True).update(
        used_at=timezone.now(),
    )
    token, token_hash = issue_account_closure_token()
    link = AccountClosureLink.objects.create(
        user=person,
        token_hash=token_hash,
        expires_at=timezone.now() + LINK_LIFETIME,
        requested_ip=get_client_ip(request) if request else None,
        requested_user_agent=request.META.get("HTTP_USER_AGENT", "") if request else "",
    )
    from ..tasks import queue_account_closure_email

    queue_account_closure_email(link_id=link.pk, token=token)


def confirm_link(token: str, *, request=None) -> ClosureOutcome:
    """Follow an emailed link: the link is spent and the product's rule applied.

    Raises ``ClosureLinkInvalid`` for a link that is unknown, already used,
    expired, or belongs to an account that can no longer be closed this way.
    Nothing is spent on a refusal.
    """
    digest = account_closure_token_digest(token or "")
    if digest is None:
        raise ClosureLinkInvalid()
    identity = (
        AccountClosureLink.objects.filter(token_hash=digest)
        .values("pk", "user_id").first()
    )
    if identity is None:
        raise ClosureLinkInvalid()

    try:
        with transaction.atomic():
            person = User.objects.select_for_update().get(pk=identity["user_id"])
            link = AccountClosureLink.objects.select_for_update().filter(
                pk=identity["pk"], token_hash=digest, used_at__isnull=True,
            ).first()
            if link is None or link.is_expired():
                raise ClosureLinkInvalid()
            link.used_at = timezone.now()
            link.save(update_fields=["used_at", "updated_at"])
            return request_closure(
                person, channel=AccountClosureRequest.Channel.WEB, request=request,
            )
    except ClosureNotOffered:
        raise ClosureLinkInvalid() from None


# ── Telling the reviewers ─────────────────────────────────────────────────────

def _queue_reviewer_alert(record, event_key: str) -> None:
    """Tell the tenant's reviewers once the closing transaction has committed."""
    record_id = record.pk
    transaction.on_commit(lambda: _alert_reviewers(record_id, event_key))


def reviewers_for(record) -> list:
    """The people who hold ``PERM_VIEW`` over the record's branch, other than its owner.

    The record's branch is judged as the work's scope, so a request carrying no
    branch reaches whole-tenant holders and one at a branch reaches that
    branch's holders and the whole-tenant ones: the same people who can read it
    in the review list.
    """
    from vs_rbac.evaluator import resolve_users_with_permission

    holders = resolve_users_with_permission(record.tenant, record.branch, PERM_VIEW)
    return [user for user in holders if user.pk != record.user_id]


def _alert_reviewers(record_id: int, event_key: str) -> None:
    """Send the alert, wording it for each reviewer's Field Access. Never raises."""
    try:
        from vs_notifications.notify import send_notification
        from vs_rbac.field_evaluator import get_field_access

        from ..tasks import _tenant_display_name

        record = (
            AccountClosureRequest.all_objects
            .select_related("user", "tenant", "branch").get(pk=record_id)
        )
        recipients = reviewers_for(record)
        if not recipients:
            logger.warning(
                "%s: nobody holds %s for closure request %s.",
                event_key, PERM_VIEW, record_id,
            )
            return
        named, unnamed = [], []
        for reviewer in recipients:
            access = get_field_access(reviewer, record.tenant)
            (named if access.can_read(PERSON_FIELD) else unnamed).append(reviewer)

        base = {
            "kind": record.kind,
            "tenant_name": _tenant_display_name(record.user),
        }
        for label, users in ((record.user.full_name or UNNAMED_PERSON, named),
                             (UNNAMED_PERSON, unnamed)):
            if not users:
                continue
            send_notification(
                event_key,
                context={**base, "person_label": label},
                recipients=users,
                tenant=record.tenant,
                branch=record.branch,
                metadata={"closure_request_id": record.pk},
            )
    except Exception:  # noqa: BLE001 - an alert never undoes a closure
        logger.exception("Could not alert reviewers of closure request %s.", record_id)
