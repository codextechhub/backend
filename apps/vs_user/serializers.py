from __future__ import annotations

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
)


# =============================================================================
# Helpers -- UNCHANGED FROM ORIGINAL
# =============================================================================

class SchoolSlimSerializer(serializers.ModelSerializer):
    class Meta:
        model = School
        fields = ('id', 'name', 'slug')


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

    # Security-sensitive fields: only platform staff with user-management
    # access should see account security metadata for other users.
    read_permissions = {
        'password_changed_at': 'platform.users.view',
        'last_login_at':       'platform.users.view',
        'invited_by_id':       'platform.users.view',
        'invited_by_name':     'platform.users.view',
    }

    class Meta:
        model  = User
        fields = (
            'id',
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
        'invited_by_name':       'platform.users.view',
        'invitation_email_status': 'platform.users.view',
        'invitation_expires_at':   'platform.users.view',
    }

    class Meta:
        model  = User
        fields = (
            'id', 'email', 'full_name', 'gender', 'user_type', 'role',
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

    def validate_email(self, value):
        # Enforce email uniqueness here to provide a clear error message, rather than relying on DB constraint which raises IntegrityError.
        if User.objects.filter(email__iexact=value.lower().strip()).exists():
            raise serializers.ValidationError({'email': 'A user with this email already exists.'})

        return value.lower().strip()

    def validate(self, attrs):
        user_type = attrs.get('user_type')

        if not user_type:
            if self.context['request'].user.user_type == User.UserType.VISION_STAFF:
                user_type = User.UserType.VISION_STAFF
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
        if user_type == User.UserType.VISION_STAFF:
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
        if user_type == User.UserType.VISION_STAFF:
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
    class Meta:
        model  = AuthAttempt
        fields = (
            'id', 'email_entered', 'user', 'school',
            'ip_address', 'result', 'failure_code', 'metadata', 'created_at',
        )
        read_only_fields = fields


class AccountLockoutReadSerializer(serializers.ModelSerializer):
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


