# services/user.py
# Business logic for user creation, email changes, and status transitions.

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

from ..models import LoginSession, User, AuthEventLog
from .audit import log_auth_event, blacklist_all_user_tokens
from vs_rbac.models import SchoolRoleTemplate, SchoolUserRoleAssignment, PlatformRoleTemplate, PlatformUserRoleAssignment


class UserCreationService:

    @staticmethod
    @transaction.atomic
    def create_pending(validated_data: dict, requesting_user, request=None) -> User:
        """Creates the User record in PENDING_APPROVAL status and assigns the role.

        No invitation is created and no email is sent — the workflow engine
        drives the next step. Call finalize_invitation() on approval.
        """
        role_instance = validated_data.pop('role_instance', None)
        position_instance = validated_data.pop('position_instance', None)
        profile_prefill = validated_data.pop('profile_prefill', None) or {}

        user = User.objects.create_user(
            email=validated_data['email'].lower().strip(),
            password=None,
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            gender=validated_data['gender'],
            phone=validated_data.get('phone', ''),
            user_type=validated_data['user_type'],
            role=validated_data.get('role', ''),
            school=validated_data.get('school') if validated_data.get('school') else None,
            branch=validated_data.get('branch') if validated_data.get('branch') else None,
            invited_by=requesting_user,
            invited_by_name=getattr(requesting_user, 'full_name', '') or '',
            status=User.Status.PENDING_APPROVAL,
            is_active=False,
            is_staff=True if validated_data['user_type'] == "CX_STAFF" else False,
        )

        if isinstance(role_instance, PlatformRoleTemplate):
            PlatformUserRoleAssignment.objects.create(
                user=user, role=role_instance, assigned_by=requesting_user,
            )
        else:
            SchoolUserRoleAssignment.objects.create(
                user=user, role=role_instance, school=user.school, assigned_by=requesting_user,
            )

        if user.user_type == User.UserType.CX_STAFF:
            # If HR fields were supplied, create the profile now and prefill it.
            # InvitationService.create() later get_or_creates this same profile
            # (idempotent), so the only effect of doing it here is that the
            # captured-at-creation HR data is already present.
            if profile_prefill:
                from ..models import PlatformStaffProfile
                PlatformStaffProfile.objects.update_or_create(
                    user=user, defaults=profile_prefill,
                )

            # Slot the hire into their organogram seat, if one was supplied. This
            # writes the effective-dated primary PositionAssignment now; when the
            # profile already exists (prefill above) its position cache is synced
            # immediately, otherwise it is synced at invite time. The seat is
            # vacated again if the creation workflow is rejected (workflow_handlers).
            if position_instance is not None:
                from .organogram import OrganogramService
                OrganogramService.assign_position(
                    user=user, position=position_instance, assigned_by=requesting_user,
                )

        log_auth_event(
            actor=requesting_user, subject=user, school=user.school,
            event=AuthEventLog.Event.USER_CREATED, request=request,
        )

        return user

    @staticmethod
    @transaction.atomic
    def finalize_invitation(user: User, requested_by) -> None:
        """Sends the invitation after workflow approval.

        Transitions status from PENDING_APPROVAL → PENDING and dispatches
        the invitation email. Safe to call only once per user.
        """
        from .invitation import InvitationService
        from ..tasks import send_invitation_email_task

        user.status = User.Status.PENDING
        user.save(update_fields=["status", "updated_at"])

        InvitationService.create(user=user, invited_by=requested_by)
        try:
            send_invitation_email_task.delay(str(user.activation_key))
        except Exception:
            logger.error(
                'Failed to dispatch invitation email for user %s — email will need to be resent manually.',
                user.pk, exc_info=True,
            )


class EmailChangeService:

    @staticmethod
    @transaction.atomic
    def change_email(target_user, new_email: str, requesting_user, request=None) -> User:
        """
        Changes a user's email immediately.
        Ends all active sessions — the user must log in again with the new email.
        """
        new_email      = new_email.lower().strip()
        previous_email = target_user.email

        if new_email == target_user.email.lower():
            raise ValueError({'error_code': 'SAME_EMAIL', 'message': 'This is already your email address.'})

        # Global uniqueness check — email must be unique across the whole platform.
        if User.objects.filter(email__iexact=new_email).exclude(pk=target_user.pk).exists():
            raise ValueError({'error_code': 'DUPLICATE_EMAIL', 'message': 'This email is already in use.'})

        target_user.email = new_email
        target_user.save(update_fields=['email', 'updated_at'])

        # End all sessions — user logs in again with the new email.
        blacklist_all_user_tokens(target_user)

        LoginSession.objects.filter(user=target_user, is_active=True).update(
            is_active=False, ended_at=timezone.now(), end_reason='EMAIL_CHANGE',
        )

        log_auth_event(
            actor=requesting_user,
            subject=target_user,
            school=target_user.school,
            event=AuthEventLog.Event.EMAIL_CHANGED,
            request=request,
            metadata={'previous_email': previous_email, 'new_email': new_email},
        )

        return target_user


class  UserStatusService:
    """
    Manages all account status transitions.
    Every transition is atomic, logged, and ends active sessions where appropriate.
    """

    @staticmethod
    @transaction.atomic
    def suspend(target_user, requesting_user, request=None) -> User:
        if target_user.status not in (User.Status.ACTIVE, User.Status.LOCKED):
            raise ValueError({'error_code': 'INVALID_STATUS_TRANSITION', 'message': f'Cannot suspend a {target_user.status} account.'})

        target_user.status    = User.Status.SUSPENDED
        target_user.is_active = False
        target_user.save(update_fields=['status', 'is_active', 'updated_at'])
        blacklist_all_user_tokens(target_user)

        LoginSession.objects.filter(user=target_user, is_active=True).update(
            is_active=False, ended_at=timezone.now(), end_reason='SUSPENDED',
        )

        log_auth_event(
            actor=requesting_user, subject=target_user,
            school=target_user.school,
            event=AuthEventLog.Event.ACCOUNT_SUSPENDED, request=request,
        )
        return target_user

    @staticmethod
    @transaction.atomic
    def reactivate(target_user, requesting_user, request=None) -> User:
        if target_user.status not in (User.Status.SUSPENDED, User.Status.DEACTIVATED):
            raise ValueError({'error_code': 'INVALID_STATUS_TRANSITION', 'message': f'Cannot reactivate a {target_user.status} account.'})

        target_user.status    = User.Status.ACTIVE
        target_user.is_active = True
        target_user.save(update_fields=['status', 'is_active', 'updated_at'])

        log_auth_event(
            actor=requesting_user, subject=target_user,
            school=target_user.school,
            event=AuthEventLog.Event.ACCOUNT_REACTIVATED, request=request,
        )
        return target_user

    @staticmethod
    @transaction.atomic
    def deactivate(target_user, requesting_user, request=None) -> User:
        if requesting_user.pk == target_user.pk:
            raise ValueError({'error_code': 'CANNOT_DEACTIVATE_SELF', 'message': 'You cannot deactivate your own account.'})

        if target_user.status == User.Status.DEACTIVATED:
            raise ValueError({'error_code': 'INVALID_STATUS_TRANSITION', 'message': 'Account is already deactivated.'})

        target_user.status    = User.Status.DEACTIVATED
        target_user.is_active = False
        target_user.save(update_fields=['status', 'is_active', 'updated_at'])
        blacklist_all_user_tokens(target_user)

        log_auth_event(
            actor=requesting_user, subject=target_user,
            school=target_user.school,
            event=AuthEventLog.Event.ACCOUNT_DEACTIVATED, request=request,
        )
        return target_user

    @staticmethod
    @transaction.atomic
    def unlock(target_user, requesting_user, request=None) -> User:
        if target_user.status != User.Status.LOCKED:
            raise ValueError({'error_code': 'INVALID_STATUS_TRANSITION', 'message': 'Account is not locked.'})

        from ..models import AccountLockout
        lockout = AccountLockout.objects.filter(user=target_user).first()
        if lockout:
            lockout.clear()
            lockout.save(update_fields=['failure_count', 'locked_until', 'locked_reason', 'updated_at'])

        target_user.status    = User.Status.ACTIVE
        target_user.is_active = True
        target_user.save(update_fields=['status', 'is_active', 'updated_at'])

        log_auth_event(
            actor=requesting_user, subject=target_user,
            school=target_user.school,
            event=AuthEventLog.Event.ACCOUNT_UNLOCKED, request=request,
        )
        return target_user