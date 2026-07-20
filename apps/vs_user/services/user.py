# services/user.py
# Business logic for user creation, email changes, and status transitions.

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

from ..models import LoginSession, User, AuthEventLog
from .audit import log_auth_event, blacklist_all_user_tokens
from vs_rbac.models import TenantUserRoleAssignment


class UserCreationService:

    @staticmethod
    def _next_employee_id(tenant) -> str:
        """Return the next CX-N staff ID while serialising concurrent hires."""
        from django.db.models import IntegerField, Max
        from django.db.models.functions import Cast, Substr
        from vs_tenants.models import Tenant
        from ..models import PlatformStaffProfile

        # Lock one stable row so two concurrent creates cannot both choose the
        # same suffix. The profile's unique constraint remains the final guard.
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        highest = (
            PlatformStaffProfile.objects
            .filter(employee_id__regex=r"^CX-[0-9]+$")
            .annotate(sequence=Cast(Substr("employee_id", 4), IntegerField()))
            .aggregate(highest=Max("sequence"))["highest"]
            or 0
        )
        return f"CX-{highest + 1}"

    @staticmethod
    @transaction.atomic
    def create_pending(validated_data: dict, requesting_user, request=None,
                       status: str = User.Status.PENDING_APPROVAL) -> User:
        """Creates the User record and assigns the role.

        ``status`` defaults to PENDING_APPROVAL (the workflow engine drives the
        next step; call finalize_invitation() on approval). Pass
        ``User.Status.DRAFT`` to park an incomplete hire: the role becomes
        optional (no assignment is written until a role is present) and the
        caller must NOT submit it to the workflow or invite it.
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
            tenant=(validated_data.get('tenant') or requesting_user.tenant),
            user_type=validated_data['user_type'],
            role=validated_data.get('role', ''),
            branch=validated_data.get('branch') if validated_data.get('branch') else None,
            invited_by=requesting_user,
            invited_by_name=getattr(requesting_user, 'full_name', '') or '',
            status=status,
            is_active=False,
            is_staff=True if validated_data['user_type'] == "CX_STAFF" else False,
        )

        # role_instance is a native TenantRoleTemplate resolved by the serializer
        # within the target tenant. Drafts may not have one yet — the assignment
        # is written when the draft is submitted (see submit_draft).
        if role_instance is not None:
            TenantUserRoleAssignment.objects.create(
                tenant=user.tenant,
                branch=user.branch if role_instance.branch_id else None,
                user=user,
                role=role_instance,
                assigned_by=requesting_user,
            )

        if user.user_type == User.UserType.CX_STAFF:
            from ..models import PlatformStaffProfile

            # Every CX hire must have a staff profile and employee ID before
            # entering approval. Preserve an explicitly supplied ID; otherwise
            # allocate the next CX-N value under the tenant lock above.
            profile_prefill["employee_id"] = (
                profile_prefill.get("employee_id")
                or UserCreationService._next_employee_id(user.tenant)
            )

            # Create the profile now and prefill any supplied HR fields.
            # InvitationService.create() later get_or_creates this same profile
            # (idempotent), so the only effect of doing it here is that the
            # captured-at-creation HR data is already present.
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
            actor=requesting_user, subject=user, tenant=user.tenant,
            event=AuthEventLog.Event.USER_CREATED, request=request,
        )

        return user

    @staticmethod
    @transaction.atomic
    def submit_draft(user: User, requesting_user, request=None, role_instance=None) -> User:
        """Promote a DRAFT hire into the normal approval flow (PENDING_APPROVAL).

        A role must be assigned first: either the draft already carries one, or
        ``role_instance`` is supplied here to assign it now. The caller submits
        the returned user to the workflow (mirrors the single-create path).
        """
        if user.status != User.Status.DRAFT:
            raise ValueError({'error_code': 'NOT_A_DRAFT',
                              'message': 'Only draft accounts can be submitted.'})
        if not (user.first_name and user.last_name and user.email):
            raise ValueError({'error_code': 'INCOMPLETE_DRAFT',
                              'message': 'First name, last name and email are required before submitting.'})

        assignment = TenantUserRoleAssignment.objects.filter(
            user=user, assignment_status='ACTIVE',
        ).first()
        if assignment is None:
            if role_instance is None:
                raise ValueError({'error_code': 'ROLE_REQUIRED',
                                  'message': 'A role must be assigned before this draft can be submitted.'})
            TenantUserRoleAssignment.objects.create(
                tenant=user.tenant,
                branch=user.branch if role_instance.branch_id else None,
                user=user, role=role_instance, assigned_by=requesting_user,
            )
            user.role = role_instance.name

        user.status = User.Status.PENDING_APPROVAL
        user.save(update_fields=['status', 'role', 'updated_at'])

        log_auth_event(
            actor=requesting_user, subject=user, tenant=user.tenant,
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
            send_invitation_email_task.delay(
                str(user.activation_key),
                _job_owner_id=str(user.id),
                _job_tenant_id=user.tenant_id,
                _job_label=f"Invitation email to {user.email}",
                _job_kind="email",
            )
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
        # all_objects: the RBAC-authorized target may live outside the ambient
        # tenant (platform actor acting on a school user); every session ends.
        blacklist_all_user_tokens(target_user)

        LoginSession.all_objects.filter(user=target_user, is_active=True).update(
            is_active=False, ended_at=timezone.now(), end_reason='EMAIL_CHANGE',
        )

        log_auth_event(
            actor=requesting_user,
            subject=target_user,
            tenant=target_user.tenant,
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

        # all_objects — see EmailChangeService: cross-tenant target sessions.
        LoginSession.all_objects.filter(user=target_user, is_active=True).update(
            is_active=False, ended_at=timezone.now(), end_reason='SUSPENDED',
        )

        log_auth_event(
            actor=requesting_user, subject=target_user,
            tenant=target_user.tenant,
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
            tenant=target_user.tenant,
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
            tenant=target_user.tenant,
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
            tenant=target_user.tenant,
            event=AuthEventLog.Event.ACCOUNT_UNLOCKED, request=request,
        )
        return target_user
