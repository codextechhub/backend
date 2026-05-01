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
from django.db import models
from django.db.models import Q
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

    # ── Choices ──────────────────────────────────────────────────────────────

    class UserType(models.TextChoices):
        VISION_STAFF      = 'VISION_STAFF',      'Vision Staff'
        SCHOOL_ADMIN      = 'SCHOOL_ADMIN',      'School Admin'
        BRANCH_ADMIN      = 'BRANCH_ADMIN',      'Branch Admin'
        STAFF             = 'STAFF',             'Staff'
        STUDENT           = 'STUDENT',           'Student'
        PARENT            = 'PARENT',            'Parent/Guardian'

    class Status(models.TextChoices):
        PENDING     = 'PENDING',     'Pending Activation'
        ACTIVE      = 'ACTIVE',      'Active'
        SUSPENDED   = 'SUSPENDED',   'Suspended'
        LOCKED      = 'LOCKED',      'Locked (security)'
        DEACTIVATED = 'DEACTIVATED', 'Deactivated'

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

    # ──User type and status ───────────────────────────────────────────────────────

    user_type = models.CharField(max_length=32, choices=UserType.choices)
    role      = models.CharField(max_length=120, blank=True, default='')  # Denormalized display name; actual grants live in UserRoleAssignment.
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
                    Q(user_type='VISION_STAFF', school__isnull=True, branch__isnull=True)
                    | ~Q(user_type='VISION_STAFF')
                ),
                name='ck_vision_staff_no_school',
            ),
            # All non-Vision Staff must have an school.
            models.CheckConstraint(
                condition=(
                    Q(user_type='VISION_STAFF')
                    | Q(school__isnull=False)
                ),
                name='ck_school_bound_users',
            ),
            # Branch-level user types must have a branch.
            models.CheckConstraint(
                condition=(
                    Q(user_type__in=['VISION_STAFF', 'SCHOOL_ADMIN'])
                    | Q(branch__isnull=False)
                ),
                name='ck_branch_required_for_branch_level_users',
            ),
        ]
        indexes = [
            models.Index(fields=['school', 'user_type', 'status']),
            models.Index(fields=['school', 'branch']),
            models.Index(fields=['email', 'status']),
        ]
        ordering = ['-updated_at']

    # ── Validation ────────────────────────────────────────────────────────────

    def clean(self):
        super().clean()
        if self.user_type != self.UserType.VISION_STAFF:
            if not self.school_id:
                raise ValidationError('Non-Vision Staff must be assigned to an school.')
            if self.user_type not in (self.UserType.SCHOOL_ADMIN,) and not self.branch_id:
                raise ValidationError(f'{self.user_type} must be assigned to a branch.')
        if self.user_type == self.UserType.VISION_STAFF:
            if self.school_id or self.branch_id:
                raise ValidationError('Vision Staff must not be assigned to an school or branch.')

    def save(self, *args, **kwargs):
        if self.status in (self.Status.SUSPENDED, self.Status.DEACTIVATED, self.Status.PENDING):
            self.is_active = False
        elif self.status == self.Status.ACTIVE:
            self.is_active = True
        # LOCKED: is_active left unchanged — blocked at RBAC layer, not Django auth
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'status' in update_fields and 'is_active' not in update_fields:
            kwargs['update_fields'] = list(update_fields) + ['is_active']
        super().save(*args, **kwargs)

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
        return self.user_type == self.UserType.VISION_STAFF

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