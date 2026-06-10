from __future__ import annotations

import copy
import hashlib
from datetime import timedelta

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import RegexValidator
from django.db import IntegrityError, transaction
from django.utils import timezone

from rest_framework import serializers
from rest_framework_simplejwt.serializers import (
    TokenRefreshSerializer as JWTTokenRefreshSerializer,
)

from vs_rbac.models import SchoolRoleTemplate, PlatformRoleTemplate
from vs_rbac.fls import FieldSecurityMixin
from vs_schools.models import School, Branch
from .models import (
    User,
    UserInvitation,
    LoginSession,
    AuthAttempt,
    AccountLockout,
    PasswordResetRequest,
    AuthEventLog,
    PlatformStaffProfile,
    OrgNode,
    Position,
    PositionAssignment,
    MatrixReport,
)


# =============================================================================
# Helpers -- UNCHANGED FROM ORIGINAL
# =============================================================================

class SchoolSlimSerializer(serializers.ModelSerializer):
    class Meta:
        model = School
        fields = ('id', 'name', 'slug')


class UserInlineSerializer(serializers.ModelSerializer):
    """Minimal nested user representation for related objects (sessions, lockouts, etc.)."""
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ('id', 'email', 'first_name', 'last_name', 'full_name')
        read_only_fields = fields


def _raise_password_error(exc: DjangoValidationError):
    if hasattr(exc, 'messages'):
        raise serializers.ValidationError({'password': exc.messages})
    raise serializers.ValidationError({'password': ['Invalid password.']})


# =============================================================================
# User serializers
# =============================================================================

class UserReadSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    full_name        = serializers.SerializerMethodField()
    school_name = serializers.CharField(source='school.name', read_only=True, default=None)
    branch_name      = serializers.CharField(source='branch.name', read_only=True, default=None)
    invited_by_name  = serializers.SerializerMethodField()

    # Security-sensitive fields: only platform staff with team-management
    # access should see account security metadata for other users.
    read_permissions = {
        'password_changed_at': 'platform.team.view',
        'last_login_at':       'platform.team.view',
        'invited_by_id':       'platform.team.view',
        'invited_by_name':     'platform.team.view',
    }

    class Meta:
        model  = User
        fields = (
            'id',
            'uid',
            'email',
            'first_name',
            'last_name',
            'full_name',
            'gender',
            'phone',
            'user_type',
            'role',
            'status',
            'school_id',
            'school_name',
            'branch_id',
            'branch_name',
            'invited_by_id',
            'invited_by_name',
            'password_changed_at',
            'last_login_at',
            'created_at',
            'updated_at',
        )
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        return obj.full_name

    def get_invited_by_name(self, obj) -> str | None:
        if obj.invited_by:
            return obj.invited_by.full_name
        return None


class UserListSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    full_name    = serializers.SerializerMethodField()
    branch_name  = serializers.CharField(source='branch.name', read_only=True, default=None)
    invited_by_name         = serializers.SerializerMethodField()
    invitation_email_status = serializers.SerializerMethodField()
    invitation_expires_at   = serializers.SerializerMethodField()

    read_permissions = {
        'invited_by_name':       'platform.team.view',
        'invitation_email_status': 'platform.team.view',
        'invitation_expires_at':   'platform.team.view',
    }

    class Meta:
        model  = User
        fields = (
            'id', 'uid', 'email', 'full_name', 'gender', 'user_type', 'role',
            'status', 'branch_id', 'branch_name', 'invited_by_name', 'created_at',
            'invitation_email_status', 'invitation_expires_at',
        )
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        return obj.full_name

    def get_invited_by_name(self, obj) -> str | None:
        if obj.invited_by:
            return obj.invited_by.full_name
        return None

    def get_invitation_email_status(self, obj) -> str | None:
        inv = getattr(obj, 'invitation', None)
        return inv.email_status if inv else None

    def get_invitation_expires_at(self, obj) -> str | None:
        inv = getattr(obj, 'invitation', None)
        return inv.expires_at.isoformat() if inv and inv.expires_at else None


class UserCreateSerializer(serializers.Serializer):
    first_name  = serializers.CharField(max_length=100)
    last_name   = serializers.CharField(max_length=100)
    email       = serializers.EmailField()
    gender      = serializers.ChoiceField(choices=User.Gender.choices, required=False, allow_blank=True, default='')
    user_type   = serializers.ChoiceField(choices=User.UserType.choices, required=False, default=None, allow_null=True)
    phone       = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default='',
        validators=[RegexValidator(r'^\+?[0-9 ()\-]{7,22}$', message='Enter a valid phone number.')],
    )
    # school and branch passed as UUIDs; resolved to objects in validate()
    school      = serializers.UUIDField(required=False, allow_null=True, default=None)
    branch      = serializers.UUIDField(required=False, allow_null=True, default=None)
    role        = serializers.CharField(
        max_length=120,
        required=True,
        error_messages={
            'required': 'A role must be assigned to the user.',
            'blank':    'A role must be assigned to the user.',
            'null':     'A role must be assigned to the user.',
        },
    )
    # Optional organogram seat to slot a CX hire into. Accepts a Position PK or
    # code. Resolved here and materialised into a real (effective-dated) primary
    # PositionAssignment by UserCreationService.create_pending.
    position    = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    job_title       = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')
    employee_id     = serializers.CharField(max_length=32, required=False, allow_blank=True, allow_null=True, default=None)
    employment_type = serializers.ChoiceField(
        choices=PlatformStaffProfile.EmploymentType.choices,
        required=False, allow_blank=True, default='',
    )
    date_joined     = serializers.DateField(required=False, allow_null=True, default=None)

    def validate_email(self, value):
        # Enforce email uniqueness here to provide a clear error message, rather than relying on DB constraint which raises IntegrityError.
        if User.objects.filter(email__iexact=value.lower().strip()).exists():
            raise serializers.ValidationError({'email': 'A user with this email already exists.'})

        return value.lower().strip()

    def validate(self, attrs):
        user_type = attrs.get('user_type')

        if not user_type:
            if self.context['request'].user.user_type == User.UserType.CX_STAFF:
                user_type = User.UserType.CX_STAFF
            else:                
                user_type = User.UserType.SCHOOL_ADMIN
                
        attrs['user_type'] = user_type

        # Resolve school UUID to instance
        school_id = attrs.pop('school', None)
        branch_id      = attrs.pop('branch', None)

        if school_id:
            try:
                attrs['school'] = School.objects.get(id=school_id)
            except School.DoesNotExist:
                raise serializers.ValidationError({'school': 'School not found.'})
        else:
            attrs['school'] = None

        if branch_id:
            try:
                attrs['branch'] = Branch.objects.get(id=branch_id)
            except Branch.DoesNotExist:
                raise serializers.ValidationError({'branch': 'Branch not found.'})
        else:
            attrs['branch'] = None

        # Vision Staff must not have school or branch
        if user_type == User.UserType.CX_STAFF:
            if attrs['school'] or attrs['branch']:
                raise serializers.ValidationError(
                    {'user_type': 'Vision Staff accounts cannot be assigned to a school or branch.'}
                )
        else:
            # All other user types must have a school
            if not attrs['school']:
                raise serializers.ValidationError(
                    {'school': 'This user type must be assigned to a school.'}
                )
            # Branch-level users must have a branch
            if user_type not in (User.UserType.SCHOOL_ADMIN,) and not attrs['branch']:
                raise serializers.ValidationError(
                    {'branch': f'User type {user_type} must be assigned to a branch.'}
                )

        # Branch must belong to the school
        if attrs.get('branch') and attrs.get('school'):
            if attrs['branch'].school_id != attrs['school'].id:
                raise serializers.ValidationError(
                    {'branch': 'The selected branch does not belong to the selected school.'}
                )
            
        role_id = attrs['role']
        if user_type == User.UserType.CX_STAFF:
            try:
                role = PlatformRoleTemplate.objects.get(id=role_id)
            except PlatformRoleTemplate.DoesNotExist:
                raise serializers.ValidationError(
                    {'role': f'Platform role with id "{role_id}" not found.'}
                )
            if role_id == 'xvs_super_admin':
                from vs_rbac.models import PlatformUserRoleAssignment
                if PlatformUserRoleAssignment.objects.filter(role_id='xvs_super_admin').exists():
                    raise serializers.ValidationError(
                        {'role': 'A Vision Super Admin already exists. Only one is allowed.'}
                    )
        else:
            school = attrs.get('school')
            if not school:
                raise serializers.ValidationError(
                    {'role': 'Role can only be assigned if a school is specified.'}
                )
            try:
                role = SchoolRoleTemplate.objects.get(id=role_id, school=school)
            except SchoolRoleTemplate.DoesNotExist:
                raise serializers.ValidationError(
                    {'role': f'Role with id "{role_id}" not found in the specified school.'}
                )

        attrs['role'] = role.name
        attrs['role_instance'] = role

        # Resolve the optional organogram seat (PK or code). CX staff only.
        position_ref = attrs.pop('position', None)
        position_instance = None
        if position_ref:
            if user_type != User.UserType.CX_STAFF:
                raise serializers.ValidationError(
                    {'position': 'Only platform (CX) staff can be assigned an organogram position.'}
                )
            qs = Position.objects.filter(is_active=True)
            position_ref = str(position_ref).strip()
            pos = (
                qs.filter(pk=position_ref).first() if position_ref.isdigit()
                else qs.filter(code__iexact=position_ref).first()
            )
            if pos is None:
                raise serializers.ValidationError(
                    {'position': f'Active position "{position_ref}" not found.'}
                )
            position_instance = pos
        attrs['position_instance'] = position_instance

        job_title   = (attrs.pop('job_title', '') or '').strip()
        employee_id = (attrs.pop('employee_id', None) or '').strip()
        emp_type    = attrs.pop('employment_type', '') or ''
        date_joined = attrs.pop('date_joined', None)

        profile_prefill = {}
        if job_title:
            profile_prefill['job_title'] = job_title
        if employee_id:
            profile_prefill['employee_id'] = employee_id
        if emp_type:
            profile_prefill['employment_type'] = emp_type
        if date_joined:
            profile_prefill['date_joined'] = date_joined

        if profile_prefill:
            if user_type != User.UserType.CX_STAFF:
                raise serializers.ValidationError(
                    {'job_title': 'Staff profile fields can only be set for platform (CX) staff.'}
                )
            if employee_id and PlatformStaffProfile.objects.filter(employee_id=employee_id).exists():
                raise serializers.ValidationError(
                    {'employee_id': 'This employee ID is already in use.'}
                )
        attrs['profile_prefill'] = profile_prefill

        return attrs


class UserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model  = User
        fields = ('first_name', 'last_name', 'phone', 'gender')
        # role and user_type are intentionally excluded — changes go through
        # SchoolRoleChangeRequest / PlatformRoleChangeRequest workflows only.
        # Email changes go through the separate /email/change/ endpoint.


class EmailChangeSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.lower().strip()


# =============================================================================
# Activation serializer
# =============================================================================
class ActivationSerializer(serializers.Serializer):
    password         = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError({'confirm_password': 'Passwords do not match.'})
        try:
            validate_password(attrs['password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': exc.messages})
        return attrs


class ActivationPreviewSerializer(serializers.ModelSerializer):
    full_name        = serializers.SerializerMethodField()

    class Meta:
        model  = User
        fields = ('email', 'first_name', 'last_name', 'full_name')
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        return obj.full_name


# =============================================================================
# Login / Token serializers 
# =============================================================================

class LoginRequestSerializer(serializers.Serializer):
    email            = serializers.EmailField()
    password         = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        email = attrs.get('email', '').strip()
        if not email:
            raise serializers.ValidationError({'email': 'Email is required.'})
        attrs['email'] = email.lower()
        return attrs


class TokenRefreshSerializer(JWTTokenRefreshSerializer):
    """
    Thin wrapper around SimpleJWT's TokenRefreshSerializer.

    SimpleJWT's serializer is the one that actually validates the refresh
    token, generates a new access token, and rotates the refresh token when
    ROTATE_REFRESH_TOKENS=True. The previous custom no-op CharField version
    caused TokenRefreshView to raise KeyError('access') and return HTTP 500
    on every refresh request.
    """
    pass


class TokenRevokeSerializer(serializers.Serializer):
    jti        = serializers.CharField()
    token_type = serializers.ChoiceField(choices=[('access', 'access'), ('refresh', 'refresh')])
    reason     = serializers.CharField(required=False, allow_blank=True)


# =============================================================================
# Password serializers
# =============================================================================

class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    password         = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = self.context['request'].user
        if not user.check_password(attrs['current_password']):
            raise serializers.ValidationError({'current_password': 'Current password is incorrect.'})
        try:
            validate_password(attrs['password'], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': exc.messages})
        if attrs['password'] == attrs['current_password']:
            raise serializers.ValidationError({'password': 'New password must differ from current password.'})
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError({'confirm_password': 'Passwords do not match.'})
        return attrs


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        return value.lower().strip()


class PasswordResetPreviewSerializer(serializers.Serializer):
    email       = serializers.EmailField(read_only=True)
    full_name   = serializers.CharField(read_only=True)

    class Meta:
        fields = ('email', 'full_name')

class PasswordResetConfirmSerializer(serializers.Serializer):
    password         = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        if attrs['password'] != attrs['confirm_password']:
            raise serializers.ValidationError({'confirm_password': 'Passwords do not match.'})
        try:
            validate_password(attrs['password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': exc.messages})
        return attrs


# =============================================================================
# UserInvitation serializers
# =============================================================================

class UserInvitationReadSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source='user.email', read_only=True)

    class Meta:
        model  = UserInvitation
        fields = (
            'id', 'user', 'user_email', 'invited_by',
            'expires_at', 'is_used', 'created_at',
        )
        read_only_fields = fields


# =============================================================================
# Session, Lockout, Attempt, AuthEvent serializers
# =============================================================================

class LoginSessionReadSerializer(serializers.ModelSerializer):
    user   = UserInlineSerializer(read_only=True)
    school = SchoolSlimSerializer(read_only=True)

    class Meta:
        model  = LoginSession
        fields = (
            'id', 'user', 'school', 'ip_address', 'user_agent',
            'device_label', 'last_seen_at', 'is_active', 'ended_at',
            'end_reason', 'created_at',
        )
        read_only_fields = fields


class ForceLogoutSerializer(serializers.Serializer):
    user_id    = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False, default=None)
    session_id = serializers.PrimaryKeyRelatedField(queryset=LoginSession.objects.all(), required=False, default=None)
    reason     = serializers.CharField()

    def validate(self, attrs):
        if not attrs.get('user_id') and not attrs.get('session_id'):
            raise serializers.ValidationError('Provide either user_id or session_id.')
        return attrs


class AuthAttemptReadSerializer(serializers.ModelSerializer):
    user   = UserInlineSerializer(read_only=True)
    school = SchoolSlimSerializer(read_only=True)

    class Meta:
        model  = AuthAttempt
        fields = (
            'id', 'email_entered', 'user', 'school',
            'ip_address', 'user_agent', 'result', 'failure_code', 'metadata', 'created_at',
        )
        read_only_fields = fields


class AccountLockoutReadSerializer(serializers.ModelSerializer):
    user          = UserInlineSerializer(read_only=True)
    is_locked_now = serializers.SerializerMethodField()

    class Meta:
        model  = AccountLockout
        fields = (
            'user', 'locked_until', 'locked_reason', 'failure_count',
            'last_failure_at', 'last_failure_ip', 'is_locked_now',
            'created_at', 'updated_at',
        )
        read_only_fields = fields

    def get_is_locked_now(self, obj) -> bool:
        return obj.is_locked_now()


class UnlockAccountSerializer(serializers.Serializer):
    user_id              = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), source='user')
    reason               = serializers.CharField(required=False, allow_blank=True)
    force_password_reset = serializers.BooleanField(default=False)


class AuthEventLogReadSerializer(serializers.ModelSerializer):
    class Meta:
        model  = AuthEventLog
        fields = (
            'id', 'actor', 'subject', 'school', 'event',
            'ip_address', 'user_agent', 'metadata', 'created_at',
        )
        read_only_fields = fields


class PasswordResetAdminSerializer(serializers.ModelSerializer):
    user = UserInlineSerializer(read_only=True)

    class Meta:
        model  = PasswordResetRequest
        fields = (
            'id', 'user', 'requested_by', 'requested_ip',
            'expires_at', 'used_at', 'created_at',
        )
        read_only_fields = fields


class MyPasswordResetSerializer(serializers.ModelSerializer):
    """Self-service view — omits the user field since it is always the requester."""

    class Meta:
        model  = PasswordResetRequest
        fields = ('id', 'requested_by', 'requested_ip', 'expires_at', 'used_at', 'created_at')
        read_only_fields = fields


# =============================================================================
# PlatformStaffProfile
# =============================================================================

# Payroll/bank fields are gated by these keys via FLS. Kept as module-level
# constants so the viewset can reuse them when deciding self-service exposure.
STAFF_PAYROLL_READ_PERM  = 'platform.staff_payroll.view'
STAFF_PAYROLL_WRITE_PERM = 'platform.staff_payroll.manage'
STAFF_PAYROLL_FIELDS     = ('bank_name', 'account_name', 'account_number')


class OrgNodeInlineSerializer(serializers.ModelSerializer):
    """Minimal nested org-node representation for related objects."""

    class Meta:
        model = OrgNode
        fields = ('id', 'name', 'code', 'kind')


class PositionInlineSerializer(serializers.ModelSerializer):
    """Minimal nested position representation for related objects."""

    org_node = OrgNodeInlineSerializer(read_only=True)

    class Meta:
        model = Position
        fields = ('id', 'title', 'code', 'org_node')


class PlatformStaffProfileListSerializer(serializers.ModelSerializer):
    """Slim representation for list endpoints — no sensitive payroll data."""

    user = UserInlineSerializer(read_only=True)
    position = PositionInlineSerializer(read_only=True)
    org_node = OrgNodeInlineSerializer(read_only=True)
    department = OrgNodeInlineSerializer(read_only=True)
    division = OrgNodeInlineSerializer(read_only=True)
    is_active_employee = serializers.BooleanField(read_only=True)

    class Meta:
        model = PlatformStaffProfile
        fields = (
            'id', 'user', 'employee_id', 'job_title', 'position', 'org_node', 'department', 'division',
            'employment_type', 'employment_status', 'is_active_employee',
            'created_at', 'updated_at',
        )
        read_only_fields = fields


class PlatformStaffProfileSerializer(FieldSecurityMixin, serializers.ModelSerializer):
    """
    Full CX-staff profile. Bank/payroll fields are stripped on read and
    rejected on write unless the caller holds the matching payroll permission
    (see FieldSecurityMixin in vs_rbac.fls).
    """

    read_permissions  = {f: STAFF_PAYROLL_READ_PERM  for f in STAFF_PAYROLL_FIELDS}
    write_permissions = {f: STAFF_PAYROLL_WRITE_PERM for f in STAFF_PAYROLL_FIELDS}

    # ── FLS owner exception ───────────────────────────────────────────────────
    # A staff member can always read and write their own payroll fields,
    # regardless of whether they hold the platform.staff_payroll.* permissions.

    def _request_user_owns(self, obj) -> bool:
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        return bool(
            obj is not None and user is not None
            and getattr(obj, 'user_id', None) == getattr(user, 'id', None)
        )

    def to_representation(self, instance):
        self._fls_is_owner = self._request_user_owns(instance)
        return super().to_representation(instance)

    def _can_read(self, field, user_perms):
        if getattr(self, '_fls_is_owner', False) and field in STAFF_PAYROLL_FIELDS:
            return True
        return super()._can_read(field, user_perms)

    def _can_write(self, field, user_perms):
        if field in STAFF_PAYROLL_FIELDS and self._request_user_owns(self.instance):
            return True
        return super()._can_write(field, user_perms)

    user            = UserInlineSerializer(read_only=True)
    user_id         = serializers.PrimaryKeyRelatedField(
        source='user', write_only=True, required=False,
        queryset=User.objects.filter(user_type=User.UserType.CX_STAFF),
    )
    # The primary seat is the single source everything settles off. Assign it
    # via position_id (or, preferably, through OrganogramService so the
    # effective-dated PositionAssignment history is written too).
    position        = PositionInlineSerializer(read_only=True)
    position_id     = serializers.PrimaryKeyRelatedField(
        source='position', write_only=True, required=False, allow_null=True,
        queryset=Position.objects.all(),
    )
    # org_node (the exact seat's node — could be a Team), department (the
    # DEPARTMENT-tier ancestor), division (the
    # DIVISION-tier ancestor), and line manager are all DERIVED from the
    # primary position — read only.
    org_node             = OrgNodeInlineSerializer(read_only=True)
    department           = OrgNodeInlineSerializer(read_only=True)
    division             = OrgNodeInlineSerializer(read_only=True)
    current_line_manager = UserInlineSerializer(read_only=True)
    is_active_employee   = serializers.BooleanField(read_only=True)

    class Meta:
        model = PlatformStaffProfile
        fields = (
            'id', 'user', 'user_id',
            'date_of_birth', 'marital_status', 'nationality', 'state_of_origin',
            'profile_photo', 'bio',
            'personal_email', 'alternate_phone', 'residential_address',
            'city', 'state',
            'nok_name', 'nok_relationship', 'nok_phone', 'nok_address',
            'employee_id', 'job_title', 'position', 'position_id', 'org_node', 'department',
            'division', 'employment_type', 'employment_status', 'date_joined', 'date_exited',
            'current_line_manager',
            'bank_name', 'account_name', 'account_number',
            'is_active_employee', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        # On create the target user must be supplied; on update it is fixed.
        if self.instance is None and 'user' not in attrs:
            raise serializers.ValidationError({'user_id': 'This field is required.'})

        # Run model-level validation (CX-only, self-manager guard) before save.
        target = copy.copy(self.instance) if self.instance is not None else PlatformStaffProfile()
        for field, value in attrs.items():
            setattr(target, field, value)
        try:
            target.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, 'message_dict') else exc.messages
            )
        return attrs


# =============================================================================
# Organogram — OrgNode / Position / PositionAssignment / MatrixReport
# =============================================================================

class OrgNodeSerializer(serializers.ModelSerializer):
    """Full org-node serializer with tier (kind), parent + derived head."""

    parent      = OrgNodeInlineSerializer(read_only=True)
    parent_id   = serializers.PrimaryKeyRelatedField(
        source='parent', write_only=True, required=False, allow_null=True,
        queryset=OrgNode.objects.all(),
    )
    head_position    = PositionInlineSerializer(read_only=True)
    head_position_id = serializers.PrimaryKeyRelatedField(
        source='head_position', write_only=True, required=False, allow_null=True,
        queryset=Position.objects.all(),
    )
    head    = UserInlineSerializer(read_only=True)
    children_count = serializers.SerializerMethodField()

    class Meta:
        model = OrgNode
        fields = (
            'id', 'name', 'code', 'kind',
            'parent', 'parent_id',
            'head_position', 'head_position_id', 'head',
            'description', 'is_active', 'children_count',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def get_children_count(self, obj) -> int:
        return obj.children.count()

    def validate(self, attrs):
        target = copy.copy(self.instance) if self.instance is not None else OrgNode()
        for field, value in attrs.items():
            setattr(target, field, value)
        try:
            target.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, 'message_dict') else exc.messages
            )
        return attrs


class PositionSerializer(serializers.ModelSerializer):
    """Full position serializer with org node + reports_to + occupancy."""

    org_node      = OrgNodeInlineSerializer(read_only=True)
    org_node_id   = serializers.PrimaryKeyRelatedField(
        source='org_node', write_only=True,
        queryset=OrgNode.objects.all(),
    )
    reports_to      = PositionInlineSerializer(read_only=True)
    reports_to_id   = serializers.PrimaryKeyRelatedField(
        source='reports_to', write_only=True, required=False, allow_null=True,
        queryset=Position.objects.all(),
    )
    default_role    = serializers.PrimaryKeyRelatedField(
        required=False, allow_null=True,
        queryset=PlatformRoleTemplate.objects.all(),
    )
    current_holders = UserInlineSerializer(many=True, read_only=True)
    is_vacant       = serializers.BooleanField(read_only=True)
    open_seats      = serializers.IntegerField(read_only=True)

    class Meta:
        model = Position
        fields = (
            'id', 'title', 'code',
            'org_node', 'org_node_id',
            'reports_to', 'reports_to_id',
            'default_role', 'headcount', 'is_active',
            'current_holders', 'is_vacant', 'open_seats',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        target = copy.copy(self.instance) if self.instance is not None else Position()
        for field, value in attrs.items():
            setattr(target, field, value)
        try:
            target.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, 'message_dict') else exc.messages
            )
        return attrs


class PositionAssignmentSerializer(serializers.ModelSerializer):
    """
    Read/write serializer for effective-dated position assignments.
    Writes go through OrganogramService for primary-seat handling, so this is
    mostly used for representation and validation.
    """

    user        = UserInlineSerializer(read_only=True)
    user_id     = serializers.PrimaryKeyRelatedField(
        source='user', write_only=True,
        queryset=User.objects.filter(user_type=User.UserType.CX_STAFF),
    )
    position    = PositionInlineSerializer(read_only=True)
    position_id = serializers.PrimaryKeyRelatedField(
        source='position', write_only=True,
        queryset=Position.objects.all(),
    )
    is_current  = serializers.BooleanField(read_only=True)

    class Meta:
        model = PositionAssignment
        fields = (
            'id', 'user', 'user_id', 'position', 'position_id',
            'is_primary', 'is_acting', 'start_date', 'end_date',
            'is_current', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')


class MatrixReportSerializer(serializers.ModelSerializer):
    """Dotted-line reporting between two positions."""

    position       = PositionInlineSerializer(read_only=True)
    position_id    = serializers.PrimaryKeyRelatedField(
        source='position', write_only=True,
        queryset=Position.objects.all(),
    )
    reports_to     = PositionInlineSerializer(read_only=True)
    reports_to_id  = serializers.PrimaryKeyRelatedField(
        source='reports_to', write_only=True,
        queryset=Position.objects.all(),
    )

    class Meta:
        model = MatrixReport
        fields = (
            'id', 'position', 'position_id',
            'reports_to', 'reports_to_id', 'relationship_label',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        target = copy.copy(self.instance) if self.instance is not None else MatrixReport()
        for field, value in attrs.items():
            setattr(target, field, value)
        try:
            target.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, 'message_dict') else exc.messages
            )
        return attrs


class OrgTreeNodeSerializer(serializers.Serializer):
    """
    Recursive read-only serializer for the position tree returned by
    OrganogramService.build_tree(). Each node is a Position plus its holders
    and nested direct reports.
    """

    id            = serializers.IntegerField()
    title         = serializers.CharField()
    code          = serializers.CharField()
    org_node      = OrgNodeInlineSerializer()
    holders       = UserInlineSerializer(many=True)
    is_vacant     = serializers.BooleanField()
    direct_reports = serializers.SerializerMethodField()

    def get_direct_reports(self, obj):
        children = obj.get('direct_reports', [])
        return OrgTreeNodeSerializer(children, many=True, context=self.context).data


