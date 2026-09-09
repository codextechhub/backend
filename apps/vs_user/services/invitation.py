"""All business logic for the invitation and activation flow.

InvitationService handles:
  - Creating a UserInvitation when a new user is created
  - Validating the invitation by user_id (not token)
  - Activating the account when the user submits their password
  - Resending an invitation (resets expiry, dispatches new email)
  - Re-sending invitations whose email reached nobody, on a beat schedule
"""
from __future__ import annotations

from datetime import timedelta
import logging

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from ..action_tokens import invitation_token_digest, issue_invitation_token
from ..models import User, UserInvitation, AuthEventLog, PlatformStaffProfile
from ..services.audit import log_auth_event
from ..tokens import CodeXRefreshToken

logger = logging.getLogger(__name__)

# An invitation whose email keeps failing is a bad address rather than a bad
# moment, and a mailbox that does not exist would otherwise be written to every
# quarter of an hour until the link expired.
_MAX_DELIVERY_ATTEMPTS = 5

# One sweep's worth. The window repeats, so a backlog drains over several runs
# instead of holding a worker for the whole of it.
_RETRY_BATCH_SIZE = 200


class InvitationService:

    # ── Create ────────────────────────────────────────────────────────────────

    @staticmethod
    def create(user: User, invited_by: User) -> tuple[UserInvitation, str]:
        """
        Creates a UserInvitation record for a newly created user.
        Called by UserCreationService immediately after the user row is saved.

        Uses get_or_create so it is safe to call multiple times -
        if a record already exists it is reset instead of duplicated.
        """
        token, token_hash = issue_invitation_token()
        with transaction.atomic():
            from vs_config.runtime_settings import get_security_value

            invitation = UserInvitation.objects.select_for_update().filter(user=user).first()
            if invitation:
                invitation.reset(token_hash=token_hash, invited_by=invited_by)
            else:
                invitation = UserInvitation.objects.create(
                    user=user,
                    invited_by=invited_by,
                    token_hash=token_hash,
                    expires_at=timezone.now() + timedelta(
                        days=get_security_value("invitation_expiry_days")
                        if user.tenant_id is None
                        else get_security_value(
                            "invitation_expiry_days", tenant=user.tenant, branch=user.branch,
                        )
                    ),
                    is_used=False,
                )
                
            if user.is_platform_user:
                profile, _ = PlatformStaffProfile.objects.get_or_create(user=user)
                # If a seat was assigned at creation time, settle the profile's
                # position cache (and thus department + line manager) now that
                # the profile exists.
                from .organogram import OrganogramService
                primary = OrganogramService.primary_position_for(user)
                if primary is not None and profile.position_id != primary.pk:
                    profile.position = primary
                    profile.save(update_fields=['position', 'updated_at'])

        return invitation, token

    # ── Validate ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_valid_invitation(token: str) -> UserInvitation:
        """
        Looks up the exact UserInvitation that owns the submitted token.
        This is called when the user lands on the activation screen
        at vision.codexng.com/invite/{user_id}/

        Raises ValueError with a user-facing message on any failure.
        """
        token_hash = invitation_token_digest(token)
        if token_hash is None:
            raise ValueError({
                'error_code': 'INVITATION_NOT_FOUND',
                'message':    'This invitation link is invalid.',
            })

        try:
            invitation = (
                UserInvitation.objects
                .select_related('user__tenant__school_profile')
                .get(token_hash=token_hash)
            )
        except UserInvitation.DoesNotExist:
            raise ValueError({
                'error_code': 'INVITATION_NOT_FOUND',
                'message':    'This invitation link is invalid.',
            })

        if invitation.is_used:
            raise ValueError({
                'error_code': 'INVITATION_ALREADY_USED',
                'message':    'This invitation link has already been used. Please log in.',
            })

        if invitation.is_expired:
            raise ValueError({
                'error_code': 'INVITATION_EXPIRED',
                'message':    'This invitation link has expired. Please contact your administrator.',
            })

        return invitation

    # ── Activate ──────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def activate(token: str, password: str, request=None) -> dict:
        """
        Activates a user account.

        Steps:
          1. Validate and lock the invitation identified by the token
          2. Validate the password against Django's password validators
          3. Set the password on the user
          4. Set is_active=True, status=ACTIVE
          5. Consume the invitation (is_used=True)
          6. Write audit log

        Returns a dict with a single 'message' key confirming the account is
        active. No tokens are issued here: the frontend must send the user
        through the normal login flow afterwards.
        """
        # 1. Resolve and lock the exact invitation row. This is deliberately
        # repeated here instead of calling the preview helper because only the
        # consuming path may serialize concurrent submissions.
        token_hash = invitation_token_digest(token)
        if token_hash is None:
            raise ValueError({
                'error_code': 'INVITATION_NOT_FOUND',
                'message': 'This invitation link is invalid.',
            })
        try:
            invitation = (
                UserInvitation.objects
                .select_for_update(of=("self",))
                .select_related('user__tenant__school_profile')
                .get(token_hash=token_hash)
            )
        except UserInvitation.DoesNotExist:
            raise ValueError({
                'error_code': 'INVITATION_NOT_FOUND',
                'message': 'This invitation link is invalid.',
            })

        if invitation.is_used:
            raise ValueError({
                'error_code': 'INVITATION_ALREADY_USED',
                'message': 'This invitation link has already been used. Please log in.',
            })
        if invitation.is_expired:
            raise ValueError({
                'error_code': 'INVITATION_EXPIRED',
                'message': 'This invitation link has expired. Please contact your administrator.',
            })

        user = User.objects.select_for_update().get(pk=invitation.user_id)

        # 1b. ...and validate the ACCOUNT, which the link's own validity says
        # nothing about. An invitation is issued when a hire is approved, and
        # the account can change underneath it before the link is clicked: a
        # withdrawn or cancelled workflow runs on_rejected and drives the same
        # user to REJECTED while their invitation email sits unread in an
        # inbox, still unused and still inside its expiry window. Without this,
        # clicking it set a password and wrote status=ACTIVE - the rejection
        # undone by the rejected person, through the front door.
        #
        # PENDING and nothing else: activation is the one transition this
        # method performs, and every other status either has not reached it
        # yet or is already past it.
        if user.status != User.Status.PENDING:
            raise ValueError({
                'error_code': 'INVITATION_NOT_ACTIONABLE',
                'message':    'This invitation link is no longer valid.',
            })

        # 2. Validate password strength
        try:
            validate_password(password, user=user)
        except DjangoValidationError as e:
            raise ValueError({
                'error_code': 'PASSWORD_POLICY_VIOLATION',
                'messages':   list(e.messages),
            })

        # 3 + 4. Set password and activate account
        user.set_password(password)
        user.password_changed_at = timezone.now()
        user.is_active           = True
        user.status              = User.Status.ACTIVE

        user.save(update_fields=[
            'password', 'password_changed_at',
            'is_active', 'status', 'updated_at',
        ])

        # 5. Consume the invitation - link is now dead
        invitation.consume()

        # 6. Audit log
        log_auth_event(
            actor=user,
            subject=user,
            tenant=user.tenant,
            event=AuthEventLog.Event.ACCOUNT_ACTIVATED,
            request=request,
        )

        # 7. Tell whoever is listening, inside this transaction.
        #
        # A domain module may hold a record whose own lifecycle starts here: a
        # school's staff record moves from Invited to Active when its owner
        # accepts, and there is no second act of an administrator declaring
        # somebody employed. This app must not import that module to say so -
        # it is an engine and knows nothing about schools - so it announces the
        # fact and the module that owns the consequence connects to it.
        #
        # Inside the transaction deliberately: a person must never be ACTIVE on
        # their login and Invited on their staff record.
        from ..signals import account_activated

        account_activated.send(sender=User, user=user)

        return {
            'message': 'Account activated. You can now log in.',
        }

    # ── Resend ────────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def resend(user: User, requested_by: User, request=None) -> UserInvitation:
        """
        Rotates the invitation token and dispatches a new invitation email.
        The previous URL dies immediately and the expiry is extended using the
        live platform security setting.

        Only valid for PENDING accounts. Caller must check status before
        calling this.
        """
        token, token_hash = issue_invitation_token()
        invitation = UserInvitation.objects.select_for_update().filter(user=user).first()
        if invitation is not None:
            invitation.reset(token_hash=token_hash, invited_by=requested_by)
        else:
            # No invitation record exists - create one fresh.
            invitation, token = InvitationService.create(
                user=user,
                invited_by=requested_by,
            )

        # Queued for after this transaction commits. A resend rotates the
        # token hash on the row, so a worker that reads it early sees the old
        # hash, decides the token it was handed is stale, and sends nothing -
        # while the admin is told the link was resent. Owner is the admin
        # doing the resend, not the invitee.
        from ..tasks import queue_invitation_email
        queue_invitation_email(
            invitation_id=invitation.pk,
            token=token,
            user=user,
            owner_id=str(requested_by.id) if requested_by else None,
            label=f"Invitation email to {user.email}",
        )

        log_auth_event(
            actor=requested_by,
            subject=user,
            tenant=user.tenant,
            event=AuthEventLog.Event.INVITATION_SENT,
            request=request,
        )

        return invitation

    # ── Recovery ──────────────────────────────────────────────────────────────

    @staticmethod
    def retry_failed_deliveries(
        *, max_attempts: int = _MAX_DELIVERY_ATTEMPTS, limit: int = _RETRY_BATCH_SIZE,
    ) -> dict:
        """Re-send the invitations whose email reached nobody.

        FAILED is the only status swept, and it is terminal on both routes that
        can produce it: the notification engine writes it after exhausting its
        own retries, and ``queue_invitation_email`` writes it when the broker
        refuses the hand-off outright. Neither can still be in flight, so
        nothing here can race a delivery that is about to succeed. PENDING is
        deliberately left alone for the same reason - it is what an invitation
        looks like while its worker has yet to run.

        A used or expired invitation is not re-sent: the first has been
        accepted and the second advertises a link that would be refused.

        Returns a count of what it did, which is what the beat task logs.
        """
        base = UserInvitation.objects.filter(
            email_status=UserInvitation.EmailStatus.FAILED,
            is_used=False,
            expires_at__gt=timezone.now(),
        )
        # Reported rather than retried: these need a human to look at the
        # address, and a silent cap is a backlog nobody knows about.
        exhausted = base.filter(email_attempts__gte=max_attempts).count()
        owed = list(
            base.filter(email_attempts__lt=max_attempts)
            .order_by('pk')
            .values_list('pk', flat=True)[:limit]
        )

        retried = skipped = 0
        for invitation_id in owed:
            if InvitationService._redispatch(invitation_id, max_attempts=max_attempts):
                retried += 1
            else:
                skipped += 1

        return {'retried': retried, 'skipped': skipped, 'exhausted': exhausted}

    @staticmethod
    def _redispatch(invitation_id: int, *, max_attempts: int) -> bool:
        """Rotate one invitation's token and queue its email again.

        Rotating is not a choice. Only the token's digest is stored, so the link
        that was going to be emailed cannot be recovered and a re-send has to
        carry a new one. It costs nothing: the row is only reachable here
        because its email reached nobody, so there is no live link being killed.

        The row is re-read under its own lock and re-checked against the
        conditions that selected it, because an administrator clicking Resend
        can reach the same invitation in the same moment.

        ``reset()`` is deliberately not used. It would zero the attempt count,
        so a mailbox that does not exist would be retried forever, and it would
        push the expiry out on every sweep, so a link would outlive the window
        it was issued with.

        One invitation that cannot be re-sent must not stop the rest of the
        sweep, so a failure here is logged and counted rather than raised.
        """
        from ..tasks import queue_invitation_email

        try:
            with transaction.atomic():
                invitation = (
                    UserInvitation.objects.select_for_update()
                    .select_related('user')
                    .filter(
                        pk=invitation_id,
                        email_status=UserInvitation.EmailStatus.FAILED,
                        is_used=False,
                        expires_at__gt=timezone.now(),
                        email_attempts__lt=max_attempts,
                    )
                    .first()
                )
                if invitation is None:
                    return False

                token, token_hash = issue_invitation_token()
                invitation.token_hash = token_hash
                invitation.email_status = UserInvitation.EmailStatus.PENDING
                invitation.save(update_fields=[
                    'token_hash', 'email_status', 'updated_at',
                ])

                queue_invitation_email(
                    invitation_id=invitation.pk,
                    token=token,
                    user=invitation.user,
                    # The queue row belongs to whoever asked for the invitation
                    # in the first place; a recovery sweep has no actor of its
                    # own and must not invent one.
                    owner_id=(
                        str(invitation.invited_by_id)
                        if invitation.invited_by_id else None
                    ),
                    label=f'Invitation email to {invitation.user.email}',
                )
            return True
        except Exception:
            logger.exception(
                'retry_failed_deliveries: invitation %s could not be re-sent',
                invitation_id,
            )
            return False
