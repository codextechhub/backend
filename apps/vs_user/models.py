from __future__ import annotations
import uuid
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

# Institution lives in Module 1 (Institution Management)
from vs_institutions.models import Institution

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        
# -----------------------------------------------------------------------------
# User / Account
# -----------------------------------------------------------------------------

class UserAccountManager(BaseUserManager):
    use_in_migrations = True
    
    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email).strip()
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.full_clean()
        user.save(using=self._db)
        return user
    
    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)
    
    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        
        return self._create_user(email, password, **extra_fields)
    
class UserAccount(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """
    Module 3 core identity object.

    - Institution-aware: institution is REQUIRED for institution users; NULL for Vision staff.
    - Enforces: forced password change after temp password login (FR-IDA-002).
    """

    class UserType(models.TextChoices):
        VISION_STAFF = "VISION_STAFF", "Vision Staff"
        INSTITUTION_ADMIN = "INSTITUTION_ADMIN", "Institution Admin"
        STAFF = "STAFF", "Staff"
        STUDENT = "STUDENT", "Student"
        PARENT = "PARENT", "Parent/Guardian"

    class Status(models.TextChoices):
        INVITED = "INVITED", "Invited / Pending Activation"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        LOCKED = "LOCKED", "Locked (security)"
        DELETED = "DELETED", "Deleted (soft)"

    # Institution binding (tenant context)
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
        help_text="NULL only for Vision staff accounts.",
    )

    email = models.EmailField(max_length=254, unique=True)  # enforce case-insensitive unique via constraint
    user_type = models.CharField(max_length=32, choices=UserType.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INVITED)

    # Django admin / permissions flags
    is_staff = models.BooleanField(default=False)  # can access Django admin
    is_active = models.BooleanField(default=True)  # auth backend flag (keep true; use status for business rules)

    # Password lifecycle (FR-IDA-002)
    must_change_password = models.BooleanField(
        default=False,
        help_text="If True, user must change password before accessing protected features.",
    )
    password_changed_at = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    # Convenience: for customer support / notifications
    phone = models.CharField(max_length=32, default="", unique=False, null=True, blank=True,)
    full_name = models.CharField(max_length=160, blank=True, default="")
    
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserAccountManager()
    
    class Meta:
        constraints = [
            # Case-insensitive unique email per institution (institution-bound users)
            models.UniqueConstraint(
                Lower("email"),
                "institution",
                name="uq_user_email_lower_per_institution",
                condition=Q(institution__isnull=False),
            ),
            # Case-insensitive unique email among Vision staff (institution NULL)
            models.UniqueConstraint(
                Lower("email"),
                name="uq_vision_staff_email_lower",
                condition=Q(user_type="VISION_STAFF"),
            ),
            # Enforce institution binding rules
            models.CheckConstraint(
                check=(
                    Q(user_type="VISION_STAFF", institution__isnull=True)
                    | ~Q(user_type="VISION_STAFF")
                ),
                name="ck_user_institution_binding",
            ),
        ]
    
    def clean(self):
        super().clean()
        
        if self.user_type != self.UserType.VISION_STAFF and self.institution_id is None:
            raise ValidationError("Non-Vision staff users must be associated with an institution.")
        if self.user_type == self.UserType.VISION_STAFF and self.institution_id is not None:
            raise ValidationError("Vision staff users cannot be associated with an institution.")
        
    def mark_password_change(self):
        self.must_change_password = False
        self.password_changed_at = timezone.now()
        
    @property
    def is_locked(self) -> bool:
        return self.status == self.Status.LOCKED
    
    @property
    def is_suspended(self) -> bool:
        return self.status == self.Status.SUSPENDED
    
    def __str__(self):
        return f"{self.email} ({self.user_type})"

# class UserProfile(TimeStampedModel):
#     """
#     Optional: keep profile separate so identity remains lean.
#     """
#     user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
#     avatar_asset_ref = models.CharField(max_length=255, blank=True, default="")
#     date_of_birth = models.DateField(null=True, blank=True)
#     address = models.TextField(blank=True, default="")
#     metadata = models.JSONField(default=dict, blank=True)

#     def __str__(self) -> str:
#         return f"Profile<{self.user_id}>"
    
# -----------------------------------------------------------------------------
# Temporary password issuance (FR-IDA-002)
# -----------------------------------------------------------------------------

class TemporaryPasswordIssue(TimeStampedModel):
    """
    Tracks that a system-generated temporary password was issued WITHOUT storing the plaintext.

    Delivery channels:
    - USER_EMAIL (Option A)
    - SUPERVISOR_EMAIL (Option B)
    """

    class Channel(models.TextChoices):
        USER_EMAIL = "USER_EMAIL", "User Email"
        SUPERVISOR_EMAIL = "SUPERVISOR_EMAIL", "Supervisor Email"
        
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="temp_password_issues")
    channel = models.CharField(max_length=24, choices=Channel.choices)
    delivered_to = models.EmailField(max_length=254)
    expires_at = models.DateTimeField()
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_status = models.CharField(
        max_length=16,
        default="PENDING",
        help_text="PENDING / SENT / FAILED",
    )
    failure_reason = models.CharField(max_length=255, blank=True, default="")

    # store a verifier hash so you can prove “this temp password existed” without storing it
    verifier_hash = models.CharField(max_length=64, help_text="SHA-256 of (user_id + temp_password + secret pepper).")

    @staticmethod
    def generate_temp_password(length: int = 12) -> str:
        # simple + strong enough for a temp secret (still enforce your password policy separately)
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%&*"
        return "".join(secrets.choice(alphabet) for _ in range(length))
    
            
    @staticmethod
    def build_verifier(user_id: int, temp_password: str) -> str:
        pepper = getattr(settings, "TEMP_PASSWORD_PEPPER", settings.SECRET_KEY)
        raw = f"{user_id}:{temp_password}:{pepper}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def __str__(self) -> str:
        return f"TempPasswordIssue<{self.user_id}:{self.delivery_status}>"
    
# -----------------------------------------------------------------------------
# Authentication / sessions / token revocation
# -----------------------------------------------------------------------------
class LoginSession(TimeStampedModel):
    """
    Server-side session tracker for JWT usage.
    Keep it simple: we track 'active sessions' and optionally the refresh token jti.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sessions")
    institution = models.ForeignKey(
        Institution,
        on_delete=models.PROTECT,
        related_name="login_sessions",
        null=True,
        blank=True,
        help_text="Copied from user/institution-context for fast filtering.",
    )

    # Observability fields
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    device_label = models.CharField(max_length=128, blank=True, default="")
    last_seen_at = models.DateTimeField(default=timezone.now)

    # Token linkage (for refresh rotation + forced logout)
    refresh_jti = models.CharField(max_length=64, blank=True, default="", db_index=True)

    is_active = models.BooleanField(default=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=64, blank=True, default="")  # LOGOUT / FORCE_LOGOUT / EXPIRED

    def end(self, reason: str = "LOGOUT"):
        self.is_active = False
        self.ended_at = timezone.now()
        self.end_reason = reason

    def __str__(self) -> str:
        return f"Session<{self.user_id}:{'active' if self.is_active else 'ended'}>"
    
class RevokedToken(TimeStampedModel):
    """
    Minimal token revocation store.
    For SimpleJWT: store JTI values and check on each authenticated request.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="revoked_tokens")
    jti = models.CharField(max_length=64, unique=True, db_index=True)
    token_type = models.CharField(max_length=16, default="refresh")  # access/refresh (policy dependent)
    expires_at = models.DateTimeField(null=True, blank=True)

    session = models.ForeignKey(
        LoginSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revocations",
    )

    reason = models.CharField(max_length=128, blank=True, default="")  # logout/force_logout/incident_response
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tokens_revoked_by_me",
        help_text="Actor for admin/force actions.",
    )

    def is_expired(self) -> bool:
        return self.expires_at is not None and timezone.now() >= self.expires_at

    def __str__(self) -> str:
        return f"RevokedToken<{self.user_id}:{self.token_type}:{self.jti[:8]}...>"

# -----------------------------------------------------------------------------
# Brute force / lockouts / password reset
# -----------------------------------------------------------------------------
class AuthAttempt(TimeStampedModel):
    """
    Records authentication attempts (success/fail) for throttling + investigation.
    Avoid user enumeration: user may be NULL if email doesn't exist.
    """

    class Result(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        FAIL = "FAIL", "Fail"
        BLOCKED = "BLOCKED", "Blocked (policy)"
        
     # What the client attempted
    email_entered = models.EmailField(max_length=254)
    institution_context = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="Subdomain/slug (or other explicit context) used at login time.",
    )

    # What we resolved (if any)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auth_attempts",
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auth_attempts",
    )

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")

    result = models.CharField(max_length=16, choices=Result.choices)
    failure_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Examples: INVALID_CREDENTIALS / INSTITUTION_MISMATCH / LOCKED / SUSPENDED / RATE_LIMITED",
    )

    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"AuthAttempt<{self.email_entered}:{self.result}>"


class AccountLockout(TimeStampedModel):
    """
    One row per user: tracks lockout state. (Keeps logic predictable.)
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="lockout")
    locked_until = models.DateTimeField(null=True, blank=True)
    locked_reason = models.CharField(max_length=128, blank=True, default="")
    failure_count = models.PositiveIntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure_ip = models.GenericIPAddressField(null=True, blank=True)

    def is_locked_now(self) -> bool:
        return self.locked_until is not None and timezone.now() < self.locked_until

    def register_failure(self, ip: str | None = None, lock_threshold: int = 5, lock_minutes: int = 15):
        now = timezone.now()
        self.failure_count += 1
        self.last_failure_at = now
        if ip:
            self.last_failure_ip = ip

        if self.failure_count >= lock_threshold:
            self.locked_until = now + timedelta(minutes=lock_minutes)
            self.locked_reason = "BRUTE_FORCE_THRESHOLD"

    def clear(self):
        self.failure_count = 0
        self.locked_until = None
        self.locked_reason = ""

    def __str__(self) -> str:
        return f"Lockout<{self.user_id}:{'locked' if self.is_locked_now() else 'ok'}>"
    

class PasswordResetRequest(TimeStampedModel):
    """
    Secure password reset: store only a hash of the reset token (never plaintext).
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="password_resets")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    requested_user_agent = models.TextField(blank=True, default="")

    def mark_used(self):
        self.used_at = timezone.now()

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def __str__(self) -> str:
        return f"PasswordResetRequest<{self.user_id}:{'used' if self.used_at else 'active'}>"
    

class SuspiciousLoginEvent(TimeStampedModel):
    """
    Optional: a higher-level security signal (geo/IP anomaly detection, etc.).
    """

    class EventType(models.TextChoices):
        INSTITUTION_MISMATCH = "INSTITUTION_MISMATCH", "Institution Mismatch"
        IMPOSSIBLE_TRAVEL = "IMPOSSIBLE_TRAVEL", "Impossible Travel"
        NEW_DEVICE = "NEW_DEVICE", "New Device"
        HIGH_FAILURE_RATE = "HIGH_FAILURE_RATE", "High Failure Rate"
        RISK_ENGINE_BLOCK = "RISK_ENGINE_BLOCK", "Risk Engine Block"

    class Decision(models.TextChoices):
        ALLOW = "ALLOW", "Allow"
        CHALLENGE = "CHALLENGE", "Challenge"
        BLOCK = "BLOCK", "Block"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suspicious_events",
    )
    email_entered = models.EmailField(max_length=254, blank=True, default="")
    institution_context = models.CharField(max_length=120, blank=True, default="")

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")

    event_type = models.CharField(max_length=32, choices=EventType.choices)
    risk_score = models.PositiveIntegerField(default=0)
    decision = models.CharField(max_length=16, choices=Decision.choices, default=Decision.BLOCK)

    details = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Suspicious<{self.event_type}:{self.decision}>"


class AuthEventLog(TimeStampedModel):
    """
    Module-local event log for auth/account events (FR-IDA-013).
    Canonical audit storage can still live in the Audit module; this is a practical operational log.
    """

    class Event(models.TextChoices):
        USER_CREATED = "USER_CREATED", "User Created"
        TEMP_PASSWORD_ISSUED = "TEMP_PASSWORD_ISSUED", "Temporary Password Issued"
        LOGIN_SUCCESS = "LOGIN_SUCCESS", "Login Success"
        LOGIN_FAILURE = "LOGIN_FAILURE", "Login Failure"
        TOKEN_REVOKED = "TOKEN_REVOKED", "Token Revoked"
        FORCE_LOGOUT = "FORCE_LOGOUT", "Force Logout"
        ACCOUNT_LOCKED = "ACCOUNT_LOCKED", "Account Locked"
        ACCOUNT_UNLOCKED = "ACCOUNT_UNLOCKED", "Account Unlocked"
        PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED", "Password Reset Requested"
        PASSWORD_RESET_COMPLETED = "PASSWORD_RESET_COMPLETED", "Password Reset Completed"
        PASSWORD_CHANGED = "PASSWORD_CHANGED", "Password Changed"

    # “actor” can be the user themselves, or an admin performing a force action
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auth_events_as_actor",
    )
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auth_events_as_subject",
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auth_events",
    )

    event = models.CharField(max_length=40, choices=Event.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"AuthEvent<{self.event}>"


# -----------------------------------------------------------------------------
# Constraints / Indexes
# -----------------------------------------------------------------------------

# Enforce case-insensitive unique email per institution 
# UserAccount._meta.constraints.extend([
#     models.UniqueConstraint(
#         fields=["institution"],
#         expressions=[Lower("email")], 
#         name="uq_user_email_lower_per_institution"
#     ),
#     models.CheckConstraint(
#         check=(
#             Q(user_type=UserAccount.UserType.VISION_STAFF, institution__isnull=True)
#             | ~Q(user_type=UserAccount.UserType.VISION_STAFF)
#         ),
#         name="ck_user_institution_binding",
#     ),
# ])

# LoginSession._meta.indexes.extend([
#     models.Index(fields=["user", "is_active"]),
#     models.Index(fields=["institution", "is_active"]),
# ])

# AuthAttempt._meta.indexes.extend([
#     models.Index(fields=["email_entered", "created_at"]),
#     models.Index(fields=["ip_address", "created_at"]),
#     models.Index(fields=["result", "created_at"]),
# ])