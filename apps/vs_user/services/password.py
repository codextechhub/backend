# services/password.py
# Password change and reset business logic.

from __future__ import annotations

from datetime import timedelta
import logging

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from ..action_tokens import issue_password_reset_token, password_reset_token_digest
from ..models import User, PasswordResetRequest, AuthEventLog, AccountLockout
from .audit import log_auth_event, blacklist_all_user_tokens, get_client_ip
from .sign_in_scope import resolve_sign_in_account

logger = logging.getLogger(__name__)

#: Refusal raised by every route that would put a usable password on an account
#: whose status may not hold one. One payload, so the four call sites below
#: cannot drift into four different answers the way their status checks did.
PASSWORD_NOT_PERMITTED = {
    "error_code": "ACCOUNT_NOT_ELIGIBLE",
    "message": "This account cannot be given a password in its current state.",
}


def _require_password_eligible(user):
    """Refuse a password write to an account that may not hold one.

    The single gate for this service. It reads ``User.may_hold_password``, the
    same property ``LoginService`` reads the sign-in half of, so "may this
    account be given a working password" has one answer for the admin reset,
    the self-service request, the reset confirmation and the logged-in change.

    Before this, each of those four asked separately and got a different
    answer: the admin reset asked nothing at all, ``request_reset`` refused
    only DEACTIVATED, and ``confirm_reset`` refused nothing and merely declined
    to PROMOTE anything that was not LOCKED or PENDING - which left a rejected
    hire holding a brand-new working password under an unchanged REJECTED
    status.
    """
    if not user.may_hold_password:
        raise ValueError(PASSWORD_NOT_PERMITTED)


class PasswordService:

    @staticmethod
    @transaction.atomic
    def change(user, new_password: str, request=None):
        """
        Changes the password for a logged-in user.
        Ends all active sessions - the user must log in again.
        """
        # Cannot fire today - only an ACTIVE account can hold the session that
        # reaches this method - and it is here so that stays true by assertion
        # rather than by the reader tracing back to who the caller is.
        _require_password_eligible(user)

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            raise ValueError({"error_code": "PASSWORD_POLICY_VIOLATION", "messages": list(e.messages)})

        user.set_password(new_password)
        user.password_changed_at = timezone.now()
        user.save(update_fields=["password", "password_changed_at", "updated_at"])
        blacklist_all_user_tokens(user)

        log_auth_event(
            actor=user, subject=user, tenant=user.tenant,
            event=AuthEventLog.Event.PASSWORD_CHANGED, request=request,
        )

    @staticmethod
    def request_reset(email: str, tenant: str | None = None, request=None):
        """
        Self-service password reset request.
        Silently does nothing if the email is not found -- prevents enumeration.

        ``tenant`` is the asserted tenant slug, which the frontend reads off the
        subdomain the request was made from. It is optional and governed by the
        same switch as sign-in (``sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN``).

        Scoping matters here for the same reason it matters at sign-in, and the
        consequence is worse: this endpoint sends a link that CHANGES a
        password. An unscoped ``.first()`` on an address held at two customers
        would let a reset asked for at Greenfield rewrite the Bright Star
        account instead - silently, and looking correct in every log. The
        refusal stays silent because a reset request must never say whether the
        address exists, here or anywhere else on the platform.
        """
        user, _resolved, scope_failure = resolve_sign_in_account(
            email=email, tenant=tenant,
        )

        # The status test is the shared one now. It used to name DEACTIVATED
        # alone, which meant a request against a REJECTED hire sent them a live
        # reset link. The refusal stays SILENT rather than raising: this
        # endpoint must answer identically whether or not the address exists,
        # so an ineligible account has to look like an unknown one.
        if scope_failure or not user or not user.may_hold_password:
            return  # Do not reveal whether the account exists

        PasswordService._create_and_send_reset(
            user, origin="SELF", sender_name="CodeX System", actor=user,
        )

    @staticmethod
    @transaction.atomic
    def admin_reset(target_user, requesting_user, request=None):
        """
        Admin triggers a password reset for another user.
        Uses the configured admin-reset window and emails it to the user.

        The eligibility check lives HERE and not in ``AdminPasswordResetView``
        because the view is not the only door: the service is the choke point
        every admin-initiated reset passes through, and a check in the view
        would have to be copied to the next caller that appears. This one is
        loud (a raised refusal the view turns into an error response) whereas
        ``request_reset``'s is silent, and the difference is correct: the admin
        is authenticated, already holds ``platform.team.update`` over this
        account, and can see its status on the screen they clicked from, so
        telling them why is not a disclosure. An anonymous reset requester is
        told nothing either way.
        """
        _require_password_eligible(target_user)

        sender_name = requesting_user.full_name if requesting_user else "CodeX System"
        PasswordService._create_and_send_reset(
            target_user, origin="ADMIN", sender_name=sender_name, actor=requesting_user,
        )

        log_auth_event(
            actor=requesting_user, subject=target_user,
            tenant=target_user.tenant,
            event=AuthEventLog.Event.PASSWORD_RESET_REQUESTED,
            request=request,
            metadata={"initiated_by": str(requesting_user.id), "origin": "ADMIN"},
        )

    @staticmethod
    def valid_reset_for_token(token: str) -> PasswordResetRequest | None:
        """Return the one live request that owns ``token``, without consuming it."""
        token_hash = password_reset_token_digest(token)
        if token_hash is None:
            return None
        reset_request = (
            PasswordResetRequest.objects
            .select_related("user")
            .filter(token_hash=token_hash, used_at__isnull=True)
            .first()
        )
        if reset_request is None or reset_request.is_expired():
            return None
        return reset_request

    @staticmethod
    @transaction.atomic
    def confirm_reset(token: str, new_password: str, request=None):
        """
        Confirms the exact password-reset request that owns the submitted token.
        Ends all active sessions on success.

        Checked again here, not only where the link was issued. A reset row can
        outlive the state it was created in - an account suspended, deactivated
        or rejected in the hours between the email going out and the link being
        clicked - and this is the moment the password actually lands. Refusing
        at issue time only would leave a live link that reinstates a credential
        on an account that has since been closed.
        """
        token_hash = password_reset_token_digest(token)
        if token_hash is None:
            raise ValueError({"error_code": "RESET_KEY_INVALID", "message": "Invalid or expired reset link."})

        # Read only the identifiers first so every path takes locks in the same
        # order as issuance: user, then reset row. The locked lookup below
        # repeats every validity predicate, so a revoke or newer request that
        # wins this race cannot be consumed from a stale read.
        reset_identity = (
            PasswordResetRequest.objects
            .filter(token_hash=token_hash)
            .values("pk", "user_id")
            .first()
        )
        if reset_identity is None:
            raise ValueError({"error_code": "RESET_KEY_INVALID", "message": "Invalid or expired reset link."})

        user = User.objects.select_for_update().get(pk=reset_identity["user_id"])
        try:
            pr = PasswordResetRequest.objects.select_for_update().get(
                pk=reset_identity["pk"],
                token_hash=token_hash,
                used_at__isnull=True,
            )
        except PasswordResetRequest.DoesNotExist:
            raise ValueError({"error_code": "RESET_KEY_INVALID", "message": "Invalid or expired reset link."})

        if pr.is_expired():
            raise ValueError({"error_code": "RESET_KEY_INVALID", "message": "Invalid or expired reset link."})

        _require_password_eligible(user)

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            raise ValueError({"error_code": "PASSWORD_POLICY_VIOLATION", "messages": list(e.messages)})

        user.set_password(new_password)
        user.password_changed_at = timezone.now()

        # ``is_active`` is not set by hand any more. It is derived from
        # ``status`` in ``User._sync_is_active`` and forcing it True here
        # was how a parked DRAFT - the one status that derivation used to
        # skip - came out of a reset with a flag that made its session
        # valid to SimpleJWT. It stays in update_fields below because the
        # derivation still writes it.
        #
        # This promotion is now total over the statuses that can reach
        # here: ``_require_password_eligible`` admits exactly ACTIVE,
        # PENDING, LOCKED and SUSPENDED. LOCKED and PENDING become ACTIVE
        # (the reset IS the unlock, and the activation); ACTIVE is already
        # there; SUSPENDED deliberately stays suspended, because a new
        # password is not a reinstatement. There is no longer any status
        # that lands here, keeps its own value and walks away with a
        # working credential - which is what REJECTED used to do.
        if user.status in (User.Status.LOCKED, User.Status.PENDING):
            if user.status == User.Status.LOCKED:
                lockout = AccountLockout.objects.select_for_update().filter(user=user).first()
                if lockout:
                    lockout.clear()
                    lockout.save(update_fields=["failure_count", "locked_until", "locked_reason", "updated_at"])
            user.status = User.Status.ACTIVE

        user.save(update_fields=["password", "password_changed_at", "status", "is_active", "updated_at"])
        pr.mark_used()
        pr.save(update_fields=["used_at", "updated_at"])
        blacklist_all_user_tokens(user)

        log_auth_event(
            actor=None, subject=user, tenant=user.tenant,
            event=AuthEventLog.Event.PASSWORD_RESET_COMPLETED,
            request=request,
            metadata={"origin": pr.requested_by},
        )

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def _create_and_send_reset(user, origin: str, sender_name: str = "CodeX System", request=None, actor=None):
        """
        Creates a PasswordResetRequest record and dispatches the reset email.

        ``actor`` owns the resulting queue row and gets the completion
        notification - the requesting admin for ADMIN origin, the user
        themselves for SELF. It is never the target user on an admin reset.
        """
        from vs_config.runtime_settings import get_security_value

        # The user row is the stable lock shared by issuance and consumption.
        # It makes superseding an old request atomic even when two callers ask
        # for a reset at the same moment.
        user = (
            User.objects
            .select_for_update(of=("self",))
            .select_related("tenant", "branch")
            .get(pk=user.pk)
        )

        expiry_hours = get_security_value(
            "self_reset_expiry_hours" if origin == "SELF" else "admin_reset_expiry_hours",
            tenant=user.tenant,
            branch=user.branch,
        )

        # Expire any existing unused reset so the unique constraint doesn't block a new request
        PasswordResetRequest.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())

        token, token_hash = issue_password_reset_token()
        reset_request = PasswordResetRequest.objects.create(
            user=user,
            token_hash=token_hash,
            expires_at=timezone.now() + timedelta(hours=expiry_hours),
            requested_by=origin,
            requested_ip=get_client_ip(request) if request else None,
            requested_user_agent=request.META.get("HTTP_USER_AGENT", "") if request else "",
        )
        # Queued for after this transaction commits, never from inside it: the
        # worker only has the row's id, and it can outrun the commit that makes
        # the row readable. See ``queue_password_reset_email``.
        from ..tasks import queue_password_reset_email
        queue_password_reset_email(
            reset_request_id=reset_request.pk,
            token=token,
            origin=origin,
            sender_name=sender_name,
            owner_id=str(actor.id) if actor else None,
        )
