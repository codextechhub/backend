# models.py
# All models for the vs_users module in one flat file.
#
# Contents (in order):
#   TimeStampedModel      - shared abstract base
#   User + UserManager    - platform-wide custom user model (AUTH_USER_MODEL)
#   UserInvitation        - invitation expiry/usage gate (UUID-based, no token)
#   LoginSession          - application-level session tracker
#   AuthAttempt           - every login attempt, success or failure
#   AccountLockout        - per-user brute-force lockout state
#   PasswordResetRequest  - hashed reset token store
#   AuthEventLog          - append-only audit event log

from __future__ import annotations

import hashlib
from datetime import timedelta

import uuid
from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q, Max
from django.utils import timezone

from vs_schools.models import School, Branch


# =============================================================================
# TimeStampedModel
# =============================================================================

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

# =============================================================================
# UserManager + User
# =============================================================================

class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email: str, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email).strip()
        user  = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            # No password on creation.
            # The user sets their own password during activation
            # via the invitation link — see models/invitation.py.
            user.set_unusable_password()
        user.full_clean()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if not extra_fields.get('is_staff'):
            raise ValueError('Superuser must have is_staff=True.')
        if not extra_fields.get('is_superuser'):
            raise ValueError('Superuser must have is_superuser=True.')
        return self._create_user(email, password, **extra_fields)


# ─────────────────────────────────────────────────────────────────────────────
# User model
# ─────────────────────────────────────────────────────────────────────────────

class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """
    Every person who logs into any part of CodeX Vision — Vision staff,
    school admins, teachers, students, parents — is a record here.
    """

    workflow_document_type = "PLATFORM_USER_CREATION"

    # ── Choices ──────────────────────────────────────────────────────────────

    class UserType(models.TextChoices):
        CX_STAFF          = 'CX_STAFF',      'CX Staff'
        SCHOOL_ADMIN      = 'SCHOOL_ADMIN',  'School Admin'
        BRANCH_ADMIN      = 'BRANCH_ADMIN',  'Branch Admin'
        STAFF             = 'STAFF',         'Staff'
        STUDENT           = 'STUDENT',       'Student'
        PARENT            = 'PARENT',        'Parent/Guardian'

    class Status(models.TextChoices):
        PENDING_APPROVAL = 'PENDING_APPROVAL', 'Pending Approval'
        PENDING          = 'PENDING',          'Pending Activation'
        ACTIVE           = 'ACTIVE',           'Active'
        SUSPENDED        = 'SUSPENDED',        'Suspended'
        LOCKED           = 'LOCKED',           'Locked (security)'
        DEACTIVATED      = 'DEACTIVATED',      'Deactivated'
        REJECTED         = 'REJECTED',         'Creation Rejected'

    class Gender(models.TextChoices):
        MALE    = 'MALE',   'Male'
        FEMALE  = 'FEMALE', 'Female'

    # ── Tenant scoping ────────────────────────────────────────────────────────

    school = models.ForeignKey(
        School, on_delete=models.PROTECT,
        related_name='users', null=True, blank=True,
        help_text='NULL only for Vision Staff.',
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT,
        related_name='users', null=True, blank=True,
        help_text='NULL for Vision Staff and School Admins.',
    )

    # ── Identity ──────────────────────────────────────────────────────────────

    email      = models.EmailField(max_length=254, unique=True)
    first_name = models.CharField(max_length=100)
    last_name  = models.CharField(max_length=100)
    gender     = models.CharField(max_length=20, choices=Gender.choices, blank=True, default='')
    phone      = models.CharField(max_length=32, blank=True, null=True, default='')

    # Auto-assigned on create; unique within school for school users,
    # unique across all Vision Staff for VISION_STAFF. Starts at 10.
    uid = models.PositiveIntegerField(null=True, blank=True, editable=False)

    # ──User type and status ───────────────────────────────────────────────────────

    user_type = models.CharField(max_length=32, choices=UserType.choices)
    role      = models.CharField(max_length=120, blank=True, default='')  # Denormalized display name; actual grants live in SchoolUserRoleAssignment.
    status    = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    # ── Django auth flags ─────────────────────────────────────────────────────

    is_staff = models.BooleanField(default=False)

    # False until the user completes activation via the invitation link.
    # Set to True by InvitationService.activate().
    is_active = models.BooleanField(default=False)
    # activation_token is a UUID used to validate the invitation link.
    activation_key = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)

    # ── Audit timestamps ──────────────────────────────────────────────────────

    password_changed_at = models.DateTimeField(null=True, blank=True)
    last_login_at       = models.DateTimeField(null=True, blank=True)

    # Tracks which admin created this user — useful for audit and support.
    invited_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='invited_users',
    )
    # Denormalized snapshot of the inviter's name at invite time.
    # Survives deletion of the inviting admin's account.
    invited_by_name = models.CharField(max_length=200, blank=True, default='')

    # ── Auth config ───────────────────────────────────────────────────────────

    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = []
    objects = UserManager()

    # ── Meta ──────────────────────────────────────────────────────────────────

    class Meta:
        db_table = 'vs_users_user'
        constraints = [
            # Vision Staff must not be bound to any school or branch.
            models.CheckConstraint(
                condition=(
                    Q(user_type='CX_STAFF', school__isnull=True, branch__isnull=True)
                    | ~Q(user_type='CX_STAFF')
                ),
                name='ck_vision_staff_no_school',
            ),
            # All non-Vision Staff must have an school.
            models.CheckConstraint(
                condition=(
                    Q(user_type='CX_STAFF')
                    | Q(school__isnull=False)
                ),
                name='ck_school_bound_users',
            ),
            # Branch-level user types must have a branch.
            models.CheckConstraint(
                condition=(
                    Q(user_type__in=['CX_STAFF', 'SCHOOL_ADMIN'])
                    | Q(branch__isnull=False)
                ),
                name='ck_branch_required_for_branch_level_users',
            ),
            # uid is unique within each school for school-scoped users.
            models.UniqueConstraint(
                fields=['school', 'uid'],
                condition=Q(school__isnull=False),
                name='unique_uid_per_school',
            ),
            # uid is unique across all Vision Staff.
            models.UniqueConstraint(
                fields=['uid'],
                condition=Q(user_type='CX_STAFF'),
                name='unique_uid_vision_staff',
            ),
        ]
        indexes = [
            models.Index(fields=['school', 'user_type', 'status']),
            models.Index(fields=['school', 'branch']),
            models.Index(fields=['email', 'status']),
        ]
        ordering = ['-created_at']

    # ── Validation ────────────────────────────────────────────────────────────

    def clean(self):
        super().clean()
        if self.user_type != self.UserType.CX_STAFF:
            if not self.school_id:
                raise ValidationError('Non-Vision Staff must be assigned to an school.')
            if self.user_type not in (self.UserType.SCHOOL_ADMIN,) and not self.branch_id:
                raise ValidationError(f'{self.user_type} must be assigned to a branch.')
        if self.user_type == self.UserType.CX_STAFF:
            if self.school_id or self.branch_id:
                raise ValidationError('Vision Staff must not be assigned to an school or branch.')

    def save(self, *args, **kwargs):
        if self.uid is None:
            with transaction.atomic():
                if self.user_type == self.UserType.CX_STAFF:
                    max_uid = (
                        User.objects.select_for_update()
                        .filter(user_type=self.UserType.CX_STAFF)
                        .aggregate(m=Max('uid'))['m']
                    )
                else:
                    max_uid = (
                        User.objects.select_for_update()
                        .filter(school_id=self.school_id)
                        .aggregate(m=Max('uid'))['m']
                    )
                self.uid = (max_uid or 9) + 1
                self._sync_is_active()
                update_fields = kwargs.get('update_fields')
                if update_fields is not None and 'status' in update_fields and 'is_active' not in update_fields:
                    kwargs['update_fields'] = list(update_fields) + ['is_active']
                super().save(*args, **kwargs)
                return

        self._sync_is_active()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'status' in update_fields and 'is_active' not in update_fields:
            kwargs['update_fields'] = list(update_fields) + ['is_active']
        super().save(*args, **kwargs)

    def _sync_is_active(self):
        if self.status in (self.Status.SUSPENDED, self.Status.DEACTIVATED,
                           self.Status.PENDING, self.Status.PENDING_APPROVAL,
                           self.Status.REJECTED):
            self.is_active = False
        elif self.status == self.Status.ACTIVE:
            self.is_active = True
        # LOCKED: is_active left unchanged — blocked at RBAC layer, not Django auth

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def full_name(self) -> str:
        return f'{self.first_name} {self.last_name}'.strip()

    @property
    def is_locked(self) -> bool:
        return self.status == self.Status.LOCKED

    @property
    def is_suspended(self) -> bool:
        return self.status == self.Status.SUSPENDED

    @property
    def is_vision_staff(self) -> bool:
        return self.user_type == self.UserType.CX_STAFF

    def mark_password_change(self):
        self.password_changed_at = timezone.now()

    def __str__(self):
        return f'{self.email} ({self.user_type})'

# =============================================================================
# UserInvitation
# =============================================================================

INVITATION_EXPIRY_DAYS = 7


class UserInvitation(TimeStampedModel):
    """
    One record per user. Tracks whether the invitation link is still
    valid (unused and within the 7-day window).

    OneToOneField enforces one active invitation per user at all times.
    On resend, the existing record is reset rather than a new one created.
    """

    class EmailStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SENT    = 'SENT',    'Sent'
        FAILED  = 'FAILED',  'Failed'

    # OneToOne — a user can only have one invitation record at a time.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='invitation',
    )

    # The admin who created or last resent this invitation.
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='sent_invitations',
    )

    # 7 days from creation. Reset to 7 days from now on resend.
    expires_at = models.DateTimeField()

    # Flipped to True on successful activation. Once True the activation link is dead.
    is_used = models.BooleanField(default=False)

    # ── Email delivery tracking ───────────────────────────────────────────────
    email_status    = models.CharField(
        max_length=10,
        choices=EmailStatus.choices,
        default=EmailStatus.PENDING,
    )
    email_sent_at   = models.DateTimeField(null=True, blank=True)
    email_last_error = models.TextField(blank=True, default='')
    email_attempts  = models.PositiveSmallIntegerField(default=0)

    # ── State helpers ─────────────────────────────────────────────────────────

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """True only if the link has not been used and has not expired."""
        return not self.is_used and not self.is_expired

    # ── Actions ───────────────────────────────────────────────────────────────

    def consume(self):
        """
        Mark as used after successful activation.
        After this, visiting vision.codexng.com/invite/{user.id}/
        will show an error — account is already active.
        """
        self.is_used = True
        self.save(update_fields=['is_used'])

    def reset(self, invited_by=None):
        """
        Reset for a resend. Gives the user a fresh 7-day window from now.
        Updates invited_by if a new admin triggered the resend.
        """
        self.is_used         = False
        self.expires_at      = timezone.now() + timedelta(days=INVITATION_EXPIRY_DAYS)
        self.email_status    = self.EmailStatus.PENDING
        self.email_sent_at   = None
        self.email_last_error = ''
        self.email_attempts  = 0
        if invited_by:
            self.invited_by = invited_by
        self.save(update_fields=[
            'is_used', 'expires_at', 'invited_by_id',
            'email_status', 'email_sent_at', 'email_last_error', 'email_attempts',
        ])

    def __str__(self) -> str:
        return f'Invitation<user={self.user_id} used={self.is_used}>'

# =============================================================================
# LoginSession
# =============================================================================

class LoginSession(TimeStampedModel):
    """
    One record per login. Tracks the refresh token JTI so sessions can
    be linked to SimpleJWT's blacklist for forced logout operations.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sessions',
    )

    # Copied from user context at login time for fast filtering
    # without joining back to the User table on every query.
    school = models.ForeignKey(
        School, on_delete=models.PROTECT,
        related_name='login_sessions', null=True, blank=True,
    )

    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    user_agent   = models.TextField(blank=True, default='')
    device_label = models.CharField(max_length=128, blank=True, default='')
    last_seen_at = models.DateTimeField(default=timezone.now)

    # JTI of the refresh token — links this session to the SimpleJWT blacklist.
    refresh_jti = models.CharField(max_length=64, blank=True, default='', db_index=True)

    is_active  = models.BooleanField(default=True)
    ended_at   = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(
        max_length=64, blank=True, default='',
        help_text='LOGOUT / FORCE_LOGOUT / EXPIRED',
    )
    class Meta:
        ordering = ['-last_seen_at']
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['is_active', 'last_seen_at']),
        ]

    def end(self, reason: str = 'LOGOUT'):
        self.is_active  = False
        self.ended_at   = timezone.now()
        self.end_reason = reason

    def __str__(self) -> str:
        state = 'active' if self.is_active else 'ended'
        return f'Session<{self.user_id}:{state}>'

# =============================================================================
# AuthAttempt / AccountLockout
# =============================================================================

class AuthAttempt(TimeStampedModel):

    class Result(models.TextChoices):
        SUCCESS = 'SUCCESS', 'Success'
        FAIL    = 'FAIL',    'Fail'
        BLOCKED = 'BLOCKED', 'Blocked (policy)'

    email_entered = models.EmailField(max_length=254)

    # Null if the email was not found — do not reveal its existence.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_attempts',
    )
    school = models.ForeignKey(
        School, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_attempts',
    )

    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    user_agent   = models.TextField(blank=True, default='')
    result       = models.CharField(max_length=16, choices=Result.choices)
    failure_code = models.CharField(
        max_length=64, blank=True, default='',
        help_text='e.g. INVALID_CREDENTIALS / SCHOOL_MISMATCH / LOCKED',
    )
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f'AuthAttempt<{self.email_entered}:{self.result}>'

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email_entered']),
            models.Index(fields=['created_at']),
            models.Index(fields=['user', 'result']),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# AccountLockout
# One row per user — keeps lockout state separate from the User model
# so the User model stays lean and lockout queries stay simple.
# ─────────────────────────────────────────────────────────────────────────────

class AccountLockout(TimeStampedModel):

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='lockout',
    )
    locked_until    = models.DateTimeField(null=True, blank=True)
    locked_reason   = models.CharField(max_length=128, blank=True, default='')
    failure_count   = models.PositiveIntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure_ip = models.GenericIPAddressField(null=True, blank=True)

    def is_locked_now(self) -> bool:
        return self.locked_until is not None and timezone.now() < self.locked_until

    def register_failure(self, ip=None, lock_threshold: int = 5, lock_minutes: int = 15):
        now = timezone.now()
        self.failure_count  += 1
        self.last_failure_at = now
        if ip:
            self.last_failure_ip = ip
        if self.failure_count >= lock_threshold:
            self.locked_until  = now + timezone.timedelta(minutes=lock_minutes)
            self.locked_reason = 'BRUTE_FORCE_THRESHOLD'

    def clear(self):
        self.failure_count = 0
        self.locked_until  = None
        self.locked_reason = ''

    def __str__(self) -> str:
        state = 'locked' if self.is_locked_now() else 'ok'
        return f'Lockout<{self.user_id}:{state}>'


# =============================================================================
# PasswordResetRequest
# =============================================================================

class PasswordResetRequest(TimeStampedModel):
    """
    Tracks password reset tokens. Only the SHA-256 hash is stored —
    never the raw token. This limits exposure if the database is compromised.

    requested_by distinguishes self-service (1 hr expiry) from
    admin-triggered resets (24 hr expiry).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='password_resets',
    )

    expires_at = models.DateTimeField()
    used_at    = models.DateTimeField(null=True, blank=True)

    # Tracks origin for expiry logic and audit context.
    # SELF = self-service (1 hour), ADMIN = admin-triggered (24 hours).
    requested_by = models.CharField(
        max_length=10,
        choices=[('SELF', 'Self-Service'), ('ADMIN', 'Admin-Initiated')],
        default='SELF',
    )

    requested_ip         = models.GenericIPAddressField(null=True, blank=True)
    requested_user_agent = models.TextField(blank=True, default='')

    # ── State helpers ─────────────────────────────────────────────────────────

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and not self.is_expired()

    def mark_used(self):
        self.used_at = timezone.now()

    def __str__(self) -> str:
        state = 'used' if self.used_at else 'active'
        return f'PasswordResetRequest<{self.user_id}:{state}>'

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                condition=Q(used_at__isnull=True),
                name='one_active_reset_per_user',
            ),
        ]

# =============================================================================
# AuthEventLog
# =============================================================================

class AuthEventLog(TimeStampedModel):

    class Event(models.TextChoices):
        USER_CREATED             = 'USER_CREATED',             'User Created'
        INVITATION_SENT          = 'INVITATION_SENT',          'Invitation Sent'
        ACCOUNT_ACTIVATED        = 'ACCOUNT_ACTIVATED',        'Account Activated'
        LOGIN_SUCCESS            = 'LOGIN_SUCCESS',            'Login Success'
        LOGIN_FAILURE            = 'LOGIN_FAILURE',            'Login Failure'
        TOKEN_REVOKED            = 'TOKEN_REVOKED',            'Token Revoked'
        FORCE_LOGOUT             = 'FORCE_LOGOUT',             'Force Logout'
        ACCOUNT_LOCKED           = 'ACCOUNT_LOCKED',           'Account Locked'
        ACCOUNT_UNLOCKED         = 'ACCOUNT_UNLOCKED',         'Account Unlocked'
        ACCOUNT_SUSPENDED        = 'ACCOUNT_SUSPENDED',        'Account Suspended'
        ACCOUNT_REACTIVATED      = 'ACCOUNT_REACTIVATED',      'Account Reactivated'
        ACCOUNT_DEACTIVATED      = 'ACCOUNT_DEACTIVATED',      'Account Deactivated'
        PASSWORD_RESET_REQUESTED = 'PASSWORD_RESET_REQUESTED', 'Password Reset Requested'
        PASSWORD_RESET_COMPLETED = 'PASSWORD_RESET_COMPLETED', 'Password Reset Completed'
        PASSWORD_CHANGED         = 'PASSWORD_CHANGED',         'Password Changed'
        EMAIL_CHANGED            = 'EMAIL_CHANGED',            'Email Changed'

    # Who performed the action — could be the user themselves or an admin.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_events_as_actor',
    )

    # Who the action was performed on.
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_events_as_subject',
    )

    school = models.ForeignKey(
        School, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='auth_events',
    )

    event      = models.CharField(max_length=40, choices=Event.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    metadata   = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'AuthEvent<{self.event}>'


# =============================================================================
# PlatformStaffProfile
# =============================================================================

class PlatformStaffProfile(TimeStampedModel):
    """
    Extended personal / HR profile for CX Staff (User.UserType.CX_STAFF).
    One row per platform staff member. Kept separate from User so the auth
    model stays lean — same pattern as AccountLockout / LoginSession.

    CX-only by design. School-side staff profiles will live in the future
    `staff` app and are intentionally out of scope here.
    """

    class MaritalStatus(models.TextChoices):
        SINGLE   = 'SINGLE',   'Single'
        MARRIED  = 'MARRIED',  'Married'
        DIVORCED = 'DIVORCED', 'Divorced'
        WIDOWED  = 'WIDOWED',  'Widowed'

    class EmploymentType(models.TextChoices):
        FULL_TIME = 'FULL_TIME', 'Full-time'
        PART_TIME = 'PART_TIME', 'Part-time'
        CONTRACT  = 'CONTRACT',  'Contract'
        INTERN    = 'INTERN',    'Intern'

    class EmploymentStatus(models.TextChoices):
        ACTIVE    = 'ACTIVE',    'Active'
        ON_LEAVE  = 'ON_LEAVE',  'On Leave'
        SUSPENDED = 'SUSPENDED', 'Suspended'
        EXITED    = 'EXITED',    'Exited'

    # ── Link ──────────────────────────────────────────────────────────────────
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='platform_staff_profile',
    )

    # ── Personal ──────────────────────────────────────────────────────────────
    date_of_birth   = models.DateField(null=True, blank=True)
    marital_status  = models.CharField(
        max_length=16, choices=MaritalStatus.choices, blank=True, default='',
    )
    nationality     = models.CharField(max_length=80, blank=True, default='')
    state_of_origin = models.CharField(max_length=80, blank=True, default='')
    profile_photo   = models.ImageField(
        upload_to='platform_staff/photos/', null=True, blank=True,
    )
    bio             = models.TextField(blank=True, default='')

    # ── Contact (personal — work email/phone live on User) ────────────────────
    personal_email      = models.EmailField(max_length=254, blank=True, default='')
    alternate_phone     = models.CharField(max_length=32, blank=True, default='')
    residential_address = models.TextField(blank=True, default='')
    city                = models.CharField(max_length=80, blank=True, default='')
    state               = models.CharField(max_length=80, blank=True, default='')

    # ── Next of kin ───────────────────────────────────────────────────────────
    nok_name         = models.CharField(max_length=200, blank=True, default='')
    nok_relationship = models.CharField(max_length=80,  blank=True, default='')
    nok_phone        = models.CharField(max_length=32,  blank=True, default='')
    nok_address      = models.TextField(blank=True, default='')

    # ── Employment ────────────────────────────────────────────────────────────
    # Human-facing staff number, distinct from User.uid.
    employee_id       = models.CharField(max_length=32, null=True, blank=True, unique=True)
    job_title         = models.CharField(max_length=120, blank=True, default='')
    position          = models.ForeignKey(
        'Position',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='staff_profiles',
    )
    employment_type   = models.CharField(
        max_length=16, choices=EmploymentType.choices, blank=True, default='',
    )
    employment_status = models.CharField(
        max_length=16, choices=EmploymentStatus.choices,
        default=EmploymentStatus.ACTIVE,
    )
    date_joined       = models.DateField(null=True, blank=True)
    date_exited       = models.DateField(null=True, blank=True)

    # ── Payroll (sensitive — gated behind FLS at the serializer layer) ────────
    bank_name      = models.CharField(max_length=120, blank=True, default='')
    account_name   = models.CharField(max_length=200, blank=True, default='')
    account_number = models.CharField(max_length=20,  blank=True, default='')

    class Meta:
        db_table = 'vs_users_platform_staff_profile'
        verbose_name = 'Platform Staff Profile'
        indexes = [
            models.Index(fields=['position', 'employment_status']),
            models.Index(fields=['employee_id']),
        ]

    def clean(self):
        super().clean()
        # Profile is valid only for CX Staff. user_type lives on the User
        # table, so this is enforced here rather than via a DB CheckConstraint.
        if self.user_id and self.user.user_type != User.UserType.CX_STAFF:
            raise ValidationError('PlatformStaffProfile can only be attached to CX Staff users.')

    @property
    def is_active_employee(self) -> bool:
        return self.employment_status == self.EmploymentStatus.ACTIVE

    @property
    def org_node(self):
        """The exact org node the person sits in (could be a Team). Or None."""
        return self.position.org_node if self.position_id else None

    @property
    def department(self):
        """
        The DEPARTMENT-tier node the person belongs to, derived by walking up
        from their seat's org node (a Team resolves to its parent Department).
        Returns an OrgNode of kind DEPARTMENT, or None if they sit directly on a
        Division (no department tier).
        """
        node = self.org_node
        return node.nearest_of_kind(OrgNode.Kind.DEPARTMENT) if node else None

    @property
    def division(self):
        """
        The DIVISION-tier node the person belongs to, derived by walking up the
        org tree from their seat (Team → Department → Division). Returns an
        OrgNode of kind DIVISION, or None if there is no division ancestor.
        """
        node = self.org_node
        return node.nearest_of_kind(OrgNode.Kind.DIVISION) if node else None

    @property
    def primary_assignment(self):
        """The user's current primary PositionAssignment (carries dates/acting), or None."""
        return (
            self.user.position_assignments
            .filter(is_primary=True, end_date__isnull=True)
            .select_related('position', 'position__org_node')
            .first()
        )

    @property
    def current_line_manager(self):
        """
        Derives the user's line manager from their cached primary position's
        reports_to seat. Returns a User (the holder of the manager position),
        or None if there is no position, no parent seat, or it is vacant.
        """
        if not self.position_id or self.position.reports_to_id is None:
            return None
        return self.position.reports_to.current_holder

    def __str__(self) -> str:
        return f'PlatformStaffProfile<{self.user_id}:{self.job_title or "staff"}>'


# =============================================================================
# Organogram — OrgNode / Position / PositionAssignment / MatrixReport
# =============================================================================
#
# Position-based organisational chart for CX (platform) staff only.
#
#   OrgNode             - hierarchical tree of org units (self-referential),
#                         tiered as DIVISION → DEPARTMENT → TEAM
#   Position            - a seat in the org (title), belonging to an org node,
#                         reporting to another position (solid line)
#   PositionAssignment  - effective-dated link of a user to a position
#                         (FULL HISTORY: end_date IS NULL == current)
#   MatrixReport        - dotted-line / secondary reporting between positions
#
# CX-only by design: no school / branch fields. School org charts will live in
# the future `staff` app.

class OrgNode(TimeStampedModel):
    """
    A node in the CX org tree, tiered DIVISION → DEPARTMENT → TEAM.

    The hierarchy is strictly enforced in clean(): a Division is top-level, a
    Department must sit under a Division, and a Team must sit under a Department.
    """

    class Kind(models.TextChoices):
        DIVISION   = 'DIVISION',   'Division'
        DEPARTMENT = 'DEPARTMENT', 'Department'
        TEAM       = 'TEAM',       'Team'

    # Which parent tier each kind must sit under (None == must be top-level).
    _REQUIRED_PARENT_KIND = {
        Kind.DIVISION:   None,
        Kind.DEPARTMENT: Kind.DIVISION,
        Kind.TEAM:       Kind.DEPARTMENT,
    }

    name        = models.CharField(max_length=150)
    code        = models.CharField(max_length=40, unique=True)
    kind        = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.DEPARTMENT,
    )
    parent      = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='children',
    )
    # The position whose holder heads this node (e.g. "Head of Engineering").
    # Nullable so a node can exist before its head seat is defined.
    head_position = models.ForeignKey(
        'Position',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='heads_node',
    )
    description = models.TextField(blank=True, default='')
    is_active   = models.BooleanField(default=True)

    class Meta:
        db_table = 'vs_users_org_node'
        verbose_name = 'Org Node'
        ordering = ['name']
        indexes = [
            models.Index(fields=['parent', 'is_active']),
            models.Index(fields=['kind']),
            models.Index(fields=['code']),
        ]

    def clean(self):
        super().clean()
        if self.parent_id and self.parent_id == self.pk:
            raise ValidationError('An org node cannot be its own parent.')
        # Guard against cycles in the parent chain.
        ancestor = self.parent
        while ancestor is not None:
            if ancestor.pk == self.pk:
                raise ValidationError('Org node parent chain cannot contain a cycle.')
            ancestor = ancestor.parent

        # Enforce the DIVISION → DEPARTMENT → TEAM tiering.
        required_parent = self._REQUIRED_PARENT_KIND.get(self.kind)
        if required_parent is None:
            if self.parent_id is not None:
                raise ValidationError(
                    {'parent': f'A {self.get_kind_display()} is top-level and cannot have a parent.'}
                )
        else:
            if self.parent_id is None:
                raise ValidationError(
                    {'parent': f'A {self.get_kind_display()} must sit under a {OrgNode.Kind(required_parent).label}.'}
                )
            if self.parent.kind != required_parent:
                raise ValidationError(
                    {'parent': f'A {self.get_kind_display()} must sit under a '
                               f'{OrgNode.Kind(required_parent).label}, not a {self.parent.get_kind_display()}.'}
                )

    @property
    def head(self):
        """The User currently heading this node, or None."""
        if self.head_position_id is None:
            return None
        return self.head_position.current_holder

    def ancestors(self):
        """Yields nodes from immediate parent up to the root."""
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def nearest_of_kind(self, kind):
        """
        Returns self (or the nearest ancestor) whose kind matches `kind`,
        walking up the tree. None if no node of that kind exists in the chain.
        """
        node = self
        while node is not None:
            if node.kind == kind:
                return node
            node = node.parent
        return None

    def __str__(self) -> str:
        return f'OrgNode<{self.kind}:{self.code}>'


class Position(TimeStampedModel):
    """
    A seat in the organogram (e.g. "Backend Engineer"). People are assigned to
    positions via PositionAssignment; the solid reporting line is position→position
    through `reports_to`.
    """

    title        = models.CharField(max_length=150)
    code         = models.CharField(max_length=40, unique=True)
    # The org node this seat belongs to — may be a Division, Department, or Team.
    org_node     = models.ForeignKey(
        OrgNode,
        on_delete=models.PROTECT,
        related_name='positions',
    )
    # Solid-line manager seat. Null for the top of the tree (e.g. CEO).
    reports_to   = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='direct_reports',
    )
    # Optional default RBAC role granted when someone is assigned this seat.
    default_role = models.ForeignKey(
        'vs_rbac.PlatformRoleTemplate',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='default_for_positions',
    )
    # Number of people the seat may hold simultaneously (1 == single-incumbent).
    headcount    = models.PositiveSmallIntegerField(default=1)
    is_active    = models.BooleanField(default=True)

    class Meta:
        db_table = 'vs_users_position'
        verbose_name = 'Position'
        ordering = ['title']
        indexes = [
            models.Index(fields=['org_node', 'is_active'], name='pos_orgnode_active_idx'),
            models.Index(fields=['reports_to']),
            models.Index(fields=['code']),
        ]

    def clean(self):
        super().clean()
        if self.reports_to_id and self.reports_to_id == self.pk:
            raise ValidationError('A position cannot report to itself.')
        # Guard against cycles in the reporting chain.
        node = self.reports_to
        while node is not None:
            if node.pk == self.pk:
                raise ValidationError('Position reporting chain cannot contain a cycle.')
            node = node.reports_to

    # NOTE: "current holder" means an OPEN assignment held by an ACTIVE user.
    # A seat reserved for a hire who is still pending approval/activation
    # (user.is_active == False) is intentionally NOT counted as occupied, so
    # such a hire never shows up as a holder and never blocks headcount until
    # they actually activate.
    @property
    def current_assignments(self):
        """Open assignments to this seat held by active users."""
        return (
            self.assignments
            .filter(end_date__isnull=True, user__is_active=True)
            .select_related('user')
        )

    @property
    def current_holder(self):
        """
        The single primary current holder of this seat, or None.
        For multi-incumbent seats this returns the primary holder.
        """
        assignment = (
            self.assignments
            .filter(end_date__isnull=True, is_primary=True, user__is_active=True)
            .select_related('user')
            .first()
        )
        if assignment is None:
            assignment = (
                self.assignments
                .filter(end_date__isnull=True, user__is_active=True)
                .select_related('user')
                .first()
            )
        return assignment.user if assignment else None

    @property
    def current_holders(self):
        """All active Users currently holding this seat (multi-incumbent aware)."""
        return [a.user for a in self.current_assignments]

    @property
    def is_vacant(self) -> bool:
        return not self.assignments.filter(
            end_date__isnull=True, user__is_active=True,
        ).exists()

    @property
    def open_seats(self) -> int:
        filled = self.assignments.filter(
            end_date__isnull=True, user__is_active=True,
        ).count()
        return max(self.headcount - filled, 0)

    def __str__(self) -> str:
        return f'Position<{self.code}>'


class PositionAssignment(TimeStampedModel):
    """
    Effective-dated assignment of a user to a position. FULL HISTORY:
    a row with end_date IS NULL is the user's current tenure in that seat;
    closing a tenure is setting end_date. Past rows are retained.

    "One current primary assignment per user" cannot be a conditional unique
    constraint on MariaDB, so it is enforced in OrganogramService / clean().
    """

    user       = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='position_assignments',
    )
    position   = models.ForeignKey(
        Position,
        on_delete=models.PROTECT,
        related_name='assignments',
    )
    # The primary seat drives department + line-manager derivation. A user may
    # hold secondary (non-primary) seats simultaneously.
    is_primary = models.BooleanField(default=True)
    # Acting / interim cover (e.g. covering a vacant manager seat).
    is_acting  = models.BooleanField(default=False)
    start_date = models.DateField(default=timezone.localdate)
    end_date   = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'vs_users_position_assignment'
        verbose_name = 'Position Assignment'
        ordering = ['-start_date']
        indexes = [
            models.Index(fields=['user', 'end_date']),
            models.Index(fields=['position', 'end_date']),
            models.Index(fields=['is_primary', 'end_date']),
        ]

    @property
    def is_current(self) -> bool:
        return self.end_date is None

    def clean(self):
        super().clean()
        if self.user_id and self.user.user_type != User.UserType.CX_STAFF:
            raise ValidationError('Only CX Staff can be assigned to a position.')
        if self.end_date and self.end_date < self.start_date:
            raise ValidationError('end_date cannot be before start_date.')
        # One current primary assignment per user (MariaDB-safe: enforced here).
        if self.is_primary and self.end_date is None and self.user_id:
            clash = (
                PositionAssignment.objects
                .filter(user_id=self.user_id, is_primary=True, end_date__isnull=True)
                .exclude(pk=self.pk)
            )
            if clash.exists():
                raise ValidationError(
                    'This user already has a current primary position. '
                    'Close it before assigning a new primary position.'
                )

    def __str__(self) -> str:
        state = 'current' if self.is_current else f'ended {self.end_date}'
        return f'PositionAssignment<{self.user_id}@{self.position_id}:{state}>'


class MatrixReport(TimeStampedModel):
    """
    Dotted-line (matrix / secondary) reporting between two positions. Distinct
    from the solid line carried by Position.reports_to. Used for cross-functional
    or project reporting that the approval engine can optionally honour.
    """

    position    = models.ForeignKey(
        Position,
        on_delete=models.CASCADE,
        related_name='matrix_reports',
    )
    reports_to  = models.ForeignKey(
        Position,
        on_delete=models.CASCADE,
        related_name='matrix_directs',
    )
    relationship_label = models.CharField(max_length=120, blank=True, default='')

    class Meta:
        db_table = 'vs_users_matrix_report'
        verbose_name = 'Matrix Report'
        unique_together = [('position', 'reports_to')]
        indexes = [
            models.Index(fields=['position']),
            models.Index(fields=['reports_to']),
        ]

    def clean(self):
        super().clean()
        if self.position_id and self.position_id == self.reports_to_id:
            raise ValidationError('A position cannot have a matrix line to itself.')

    def __str__(self) -> str:
        return f'MatrixReport<{self.position_id}⇢{self.reports_to_id}>'