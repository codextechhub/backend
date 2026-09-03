"""All business logic for the invitation and activation flow.

InvitationService handles:
  - Creating a UserInvitation when a new user is created
  - Validating the invitation by user_id (not token)
  - Activating the account when the user submits their password
  - Resending an invitation (resets expiry, dispatches new email)
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
