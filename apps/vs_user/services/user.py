# services/user.py
# Business logic for user creation, email changes, and status transitions.

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

logger = logging.getLogger(__name__)

from ..email_normalization import normalize_email
from ..auth_events import AuthEvent
from ..models import LoginSession, User
from .audit import log_auth_event, blacklist_all_user_tokens
from .email_availability import email_refusal
from vs_rbac.models import TenantUserRoleAssignment
from vs_tenants.models import Tenant


#: The caller did not say how far a role grant reaches, which is not the same
#: as saying it reaches everywhere. ``None`` in its place is a deliberate
#: whole-tenant grant; this leaves the answer to :func:`grant_reach`.
REACH_UNSTATED = object()


def grant_reach(*, user, role, requested=REACH_UNSTATED):
    """The branch a role grant written alongside a new account reaches.

    A grant made in the same act as a posting takes that posting, because a
    grant carrying no branch reaches every branch there is: a teacher hired at
    Ikeja and given Teacher without anybody naming a branch could read Lekki's
    records, Yaba's and every branch opened afterwards. Whole-tenant reach
    remains available and has to be asked for, by passing ``requested=None``.

    A role template that belongs to one branch answers for itself and outranks
    the posting, so the deputy based at Lekki who is made Branch Admin of Ikeja
    administers Ikeja rather than the site she sits at.
    """
    if role is not None and len(role.branch_ids) > 1:
        return None
    if requested is not REACH_UNSTATED:
        return requested
    if role is not None and role.branch_id:
        return role.branch
    return user.branch


class UserCreationService:

    @staticmethod
    def _enforce_role_grant_ceiling(role_instance, requesting_user) -> None:
        if role_instance is None:
            return
        from vs_rbac.validators import (
            missing_restricted_grant_authority,
            role_restricted_permission_keys,
        )

        missing = missing_restricted_grant_authority(
            requesting_user, role_restricted_permission_keys(role_instance),
        )
        if missing:
            raise PermissionDenied(
                "You cannot assign a role carrying restricted permissions "
                "outside your grant authority: "
                f"{', '.join(sorted(missing))}."
            )

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
                       status: str = User.Status.PENDING_APPROVAL,
                       role_branch=REACH_UNSTATED) -> User:
        """Creates the User record and assigns the role.

        ``status`` defaults to PENDING_APPROVAL (the workflow engine drives the
        next step; call finalize_invitation() on approval). Pass
        ``User.Status.DRAFT`` to park an incomplete hire: the role becomes
        optional (no assignment is written until a role is present) and the
        caller must NOT submit it to the workflow or invite it.

        ``role_branch`` says how far the grant reaches. Left alone it follows
        the posting the account is created with, which is the reach every
        caller wants and none of them had to ask for; pass ``None`` for a
        deliberate whole-tenant grant. See :func:`grant_reach`.
        """
        role_instance = validated_data.pop('role_instance', None)
        position_instance = validated_data.pop('position_instance', None)
        profile_prefill = validated_data.pop('profile_prefill', None) or {}
        UserCreationService._enforce_role_grant_ceiling(
            role_instance, requesting_user,
        )

        target_tenant = validated_data.get('tenant') or requesting_user.tenant
        creating_platform_staff = (
            getattr(target_tenant, 'kind', None) == Tenant.Kind.PLATFORM
        )

        if (
            creating_platform_staff
            and status != User.Status.DRAFT
            and position_instance is None
        ):
            raise ValueError({
                'error_code': 'POSITION_REQUIRED',
                'message': 'A position must be assigned to platform staff.',
            })

        if position_instance is not None:
            # Position is authoritative even for callers that bypass the API
            # serializer and invoke the creation service directly.
            profile_prefill['job_title'] = position_instance.title

        user = User.objects.create_user(
            email=validated_data['email'].lower().strip(),
            password=None,
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            gender=validated_data['gender'],
            phone=validated_data.get('phone', ''),
            tenant=target_tenant,
            role=validated_data.get('role', ''),
            branch=validated_data.get('branch') if validated_data.get('branch') else None,
            invited_by=requesting_user,
            invited_by_name=getattr(requesting_user, 'full_name', '') or '',
            status=status,
            is_active=False,
            is_staff=creating_platform_staff,
        )

        # role_instance is a native TenantRoleTemplate resolved by the serializer
        # within the target tenant. Drafts may not have one yet - the assignment
        # is written when the draft is submitted (see submit_draft).
        if role_instance is not None:
            TenantUserRoleAssignment.objects.create(
                tenant=user.tenant,
                branch=grant_reach(
                    user=user, role=role_instance, requested=role_branch,
                ),
                user=user,
                role=role_instance,
                assigned_by=requesting_user,
            )

        if creating_platform_staff:
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
            profile, _ = PlatformStaffProfile.objects.update_or_create(
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
                # update_or_create may leave this same profile instance cached
                # on `user`; keep the immediate create response consistent with
                # the assignment that OrganogramService just persisted.
                profile.position = position_instance
                profile.job_title = position_instance.title

        log_auth_event(
            actor=requesting_user, subject=user, tenant=user.tenant,
            event=AuthEvent.USER_CREATED, request=request,
        )

        return user

    @staticmethod
    @transaction.atomic
    def submit_draft(user: User, requesting_user, request=None, role_instance=None,
                     role_branch=REACH_UNSTATED) -> User:
        """Promote a DRAFT hire into the normal approval flow (PENDING_APPROVAL).

        A role must be assigned first: either the draft already carries one, or
        ``role_instance`` is supplied here to assign it now. The caller submits
        the returned user to the workflow (mirrors the single-create path).

        A grant written here reaches exactly as far as one written at creation
        does, for the same reason: the draft carries the posting already, and a
        hire parked for a week must not come out of it with wider access than
        the same hire entered in one sitting. See :func:`grant_reach`.
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
            UserCreationService._enforce_role_grant_ceiling(
                role_instance, requesting_user,
            )
            TenantUserRoleAssignment.objects.create(
                tenant=user.tenant,
                branch=grant_reach(
                    user=user, role=role_instance, requested=role_branch,
                ),
                user=user, role=role_instance, assigned_by=requesting_user,
            )
            user.role = role_instance.name

        user.status = User.Status.PENDING_APPROVAL
        user.save(update_fields=['status', 'role', 'updated_at'])

        log_auth_event(
            actor=requesting_user, subject=user, tenant=user.tenant,
            event=AuthEvent.USER_CREATED, request=request,
        )
        return user

    @staticmethod
    @transaction.atomic
    def finalize_invitation(user: User, requested_by, send_email: bool = True) -> None:
        """Makes the account invitable, and by default writes to the invitee.

        Transitions status from PENDING_APPROVAL to PENDING and creates the
        ``UserInvitation`` row. Safe to call only once per user.

        ``send_email=False`` parks the invitation instead of dispatching it: the
        account still reaches PENDING and still gets a real invitation row,
        which sits at ``EmailStatus.PENDING`` until somebody asks for it to go
        out. That is what the existing resend path already acts on, so a parked
        person is chased later through the same service that chases anybody
        else, rather than through a second notion of "not yet invited".

        The switch is a parameter rather than a sibling function because only
        the last of the three steps differs. A sibling would restate the status
        transition and the invitation create, which would make two places
        answerable for what "invited" means, and the two would drift the first
        time either changed. The default sends, so every caller that does not
        mention it behaves as it always has.
        """
        from .invitation import InvitationService
        from ..tasks import queue_invitation_email

        user.status = User.Status.PENDING
        user.save(update_fields=["status", "updated_at"])

        invitation, token = InvitationService.create(
            user=user, invited_by=requested_by,
        )
        if not send_email:
            return
        # Queued for after this transaction commits: the worker is given the
        # invitation's id, and it must not be able to look for it before the
        # commit publishes the row. See ``queue_invitation_email``.
        queue_invitation_email(
            invitation_id=invitation.pk,
            token=token,
            user=user,
            owner_id=str(requested_by.id) if requested_by else None,
            label=f"Invitation email to {user.email}",
        )


class EmailChangeService:

    @staticmethod
    @transaction.atomic
    def change_email(target_user, new_email: str, requesting_user, request=None, note: str = "") -> User:
        """
        Changes a user's email immediately.
        Ends all active sessions - the user must log in again with the new email.

        An account still waiting to be activated has its invitation reissued to
        the new address. The usual reason for the change is a mistyped
        invitation, and the link already sent to the wrong inbox would otherwise
        stay live: whoever holds it could set the password and walk in as the
        new member of staff. Reissuing rotates the token, so that link dies here
        and the right person receives a working one.

        ``note`` is the administrator's reason, recorded with the event.
        """
        new_email      = normalize_email(new_email)
        previous_email = target_user.email

        if new_email == normalize_email(target_user.email):
            raise ValueError({'error_code': 'SAME_EMAIL', 'message': 'This is already your email address.'})

        # Uniqueness is PER TENANT, so the question is asked of the tenant that
        # owns this account, not of the platform. Unscoped, a Bright Star
        # parent could not be corrected to ada@gmail.com because Greenfield
        # already had an account on it - and the refusal told Bright Star's
        # admin that somebody, somewhere, holds the address.
        refusal = email_refusal(
            new_email, tenant=target_user.tenant_id, exclude_pk=target_user.pk,
        )
        if refusal:
            raise ValueError({'error_code': 'DUPLICATE_EMAIL', 'message': refusal})

        target_user.email = new_email
        target_user.save(update_fields=['email', 'updated_at'])

        # End all sessions - user logs in again with the new email.
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
            event=AuthEvent.EMAIL_CHANGED,
            request=request,
            metadata={
                'previous_email': previous_email, 'new_email': new_email,
                **({'note': note.strip()} if note and note.strip() else {}),
            },
        )

        if target_user.status == User.Status.PENDING:
            from .invitation import InvitationService

            invitation = getattr(target_user, 'invitation', None)
            if invitation is None or not invitation.is_used:
                InvitationService.resend(target_user, requesting_user, request=request)

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

        # all_objects - see EmailChangeService: cross-tenant target sessions.
        LoginSession.all_objects.filter(user=target_user, is_active=True).update(
            is_active=False, ended_at=timezone.now(), end_reason='SUSPENDED',
        )

        log_auth_event(
            actor=requesting_user, subject=target_user,
            tenant=target_user.tenant,
            event=AuthEvent.ACCOUNT_SUSPENDED, request=request,
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
            event=AuthEvent.ACCOUNT_REACTIVATED, request=request,
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
            event=AuthEvent.ACCOUNT_DEACTIVATED, request=request,
        )
        return target_user

    @staticmethod
    @transaction.atomic
    def unlock(target_user, requesting_user, request=None, metadata=None) -> User:
        """Release a brute-force lockout early, and touch nothing else.

        The one implementation, called by the platform endpoint, the school's
        own account action and the lockout console alike, so "what does
        unlocking do" has a single answer.

        It reads and writes :class:`AccountLockout` and never ``User.status``.
        That separation is the point of the two actions: unlocking ends a timed
        security condition, and reactivating ends an administrative suspension.
        Setting ACTIVE here would merge them, and a suspended account that had
        also been guessed at would be reinstated by an administrator who thought
        they were clearing a lockout.

        Refused only when there is nothing to clear. A counter left standing by
        a window that has already closed still counts as something: the holder's
        next single mistake would otherwise reach the threshold again, and
        clearing it is a real act rather than a no-op dressed as one.
        """
        from ..models import AccountLockout

        lockout = (
            AccountLockout.objects.select_for_update().filter(user=target_user).first()
        )
        if lockout is None or not lockout.has_state():
            raise ValueError({'error_code': 'INVALID_STATUS_TRANSITION', 'message': 'Account is not locked.'})

        lockout.clear()
        lockout.save(update_fields=['failure_count', 'locked_until', 'locked_reason', 'updated_at'])

        log_auth_event(
            actor=requesting_user, subject=target_user,
            tenant=target_user.tenant,
            event=AuthEvent.ACCOUNT_UNLOCKED, request=request,
            metadata=metadata or {},
        )
        return target_user
