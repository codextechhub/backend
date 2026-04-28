# services/invitation.py
# All business logic for the invitation and activation flow.
#
# InvitationService handles:
#   - Creating a UserInvitation when a new user is created
#   - Validating the invitation by user_id (not token)
#   - Activating the account when the user submits their password
#   - Resending an invitation (resets expiry, dispatches new email)

from __future__ import annotations

from datetime import timedelta
import uuid

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from ..models import User, UserInvitation, AuthEventLog
from ..services.audit import log_auth_event
from ..tokens import CodeXRefreshToken


INVITATION_EXPIRY_DAYS = 7


class InvitationService:

    # ── Create ────────────────────────────────────────────────────────────────

    @staticmethod
    def create(user: User, invited_by: User) -> UserInvitation:
        """
        Creates a UserInvitation record for a newly created user.
        Called by UserCreationService immediately after the user row is saved.

        Uses get_or_create so it is safe to call multiple times —
        if a record already exists it is reset instead of duplicated.
        """
        with transaction.atomic():
            invitation = UserInvitation.objects.select_for_update().filter(user=user).first()
            if invitation:
                invitation.reset(invited_by=invited_by)
            else:
                invitation = UserInvitation.objects.create(
                    user=user,
                    invited_by=invited_by,
                    expires_at=timezone.now() + timedelta(days=INVITATION_EXPIRY_DAYS),
                    is_used=False,
                )
        return invitation

    # ── Validate ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_valid_invitation(activation_key: str) -> UserInvitation:
        """
        Looks up a UserInvitation by the user's UUID.
        This is called when the user lands on the activation screen
        at vision.codexng.com/invite/{user_id}/

        Raises ValueError with a user-facing message on any failure.
        """
        try:
            user = User.objects.get(activation_key=activation_key)
            invitation = (
                UserInvitation.objects
                .select_related('user__school')
                .get(user_id=user.id)
            )
        except UserInvitation.DoesNotExist:
            raise ValueError({
                'error_code': 'INVITATION_NOT_FOUND',
                'message':    'This invitation link is invalid.',
            })
        except User.DoesNotExist:
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
    def activate(activation_key: str, password: str, request=None) -> dict:
        """
        Activates a user account.

        Steps:
          1. Validate the invitation by activation_key
          2. Validate the password against Django's password validators
          3. Set the password on the user
          4. Set is_active=True, status=ACTIVE
          5. Consume the invitation (is_used=True)
          6. Issue JWT tokens so the user is logged in immediately
          7. Write audit log

        Returns a dict with access token, refresh token, and user data
        so the frontend can log the user in without a separate login call.
        """
        # 1. Validate invitation
        invitation = InvitationService.get_valid_invitation(activation_key)
        user = invitation.user

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
        user.activation_key       = uuid.uuid4() # Invalidate the activation key immediately
        
        user.save(update_fields=[
            'password', 'password_changed_at',
            'is_active', 'status', 'updated_at',
            'activation_key',
        ])

        # 5. Consume the invitation — link is now dead
        invitation.consume()

        # 7. Audit log
        log_auth_event(
            actor=user,
            subject=user,
            school=user.school,
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
        Resets the invitation and dispatches a new invitation email.
        The URL stays the same — vision.codexng.com/invite/{user.id}/ —
        but the expiry is extended to 7 days from now.

        Only valid for PENDING accounts. Caller must check status before
        calling this.
        """
        try:
            invitation = UserInvitation.objects.get(user=user)
            invitation.reset(invited_by=requested_by)
        except UserInvitation.DoesNotExist:
            # No invitation record exists — create one fresh.
            invitation = InvitationService.create(
                user=user,
                invited_by=requested_by,
            )

        # Dispatch email asynchronously
        from ..tasks import send_invitation_email_task
        send_invitation_email_task(str(user.activation_key))

        log_auth_event(
            actor=requested_by,
            subject=user,
            school=user.school,
            event=AuthEventLog.Event.INVITATION_SENT,
            request=request,
        )

        return invitation