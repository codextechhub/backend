# services/password.py
# Password change and reset business logic.

from __future__ import annotations

from datetime import timedelta
import uuid

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from ..models import User, PasswordResetRequest, AuthEventLog, AccountLockout
from .audit import log_auth_event, blacklist_all_user_tokens, get_client_ip

# Self-service reset: 1 hour. Admin-triggered reset: 24 hours.
RESET_EXPIRY_SELF_HOURS  = 1
RESET_EXPIRY_ADMIN_HOURS = 24


class PasswordService:

    @staticmethod
    @transaction.atomic
    def change(user, new_password: str, request=None):
        """
        Changes the password for a logged-in user.
        Ends all active sessions — the user must log in again.
        """
        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            raise ValueError({"error_code": "PASSWORD_POLICY_VIOLATION", "messages": list(e.messages)})

        user.set_password(new_password)
        user.password_changed_at = timezone.now()
        user.save(update_fields=["password", "password_changed_at", "updated_at"])
        blacklist_all_user_tokens(user)

        log_auth_event(
            actor=user, subject=user, school=user.school,
            event=AuthEventLog.Event.PASSWORD_CHANGED, request=request,
        )

    @staticmethod
    def request_reset(email: str, request=None):
        """
        Self-service password reset request.
        Silently does nothing if the email is not found -- prevents enumeration.
        """
        user = User.objects.filter(email__iexact=email).first()

        if not user or user.status == User.Status.DEACTIVATED:
            return  # Do not reveal whether the account exists

        PasswordService._create_and_send_reset(user, origin="SELF", request=request)

    @staticmethod
    @transaction.atomic
    def admin_reset(target_user, requesting_user, request=None):
        """
        Admin triggers a password reset for another user.
        Creates a 24-hour token and emails it to the user.
        """
        PasswordService._create_and_send_reset(target_user, origin="ADMIN", request=request)

        log_auth_event(
            actor=requesting_user, subject=target_user,
            school=target_user.school,
            event=AuthEventLog.Event.PASSWORD_RESET_REQUESTED,
            request=request,
            metadata={"initiated_by": str(requesting_user.id), "origin": "ADMIN"},
        )

    @staticmethod
    @transaction.atomic
    def confirm_reset(user, new_password: str, request=None):
        """
        Confirms a password reset using the activation key.
        Ends all active sessions on success.
        """
        pr = PasswordResetRequest.objects.filter(
            user=user, used_at__isnull=True
        ).last()

        if not pr or pr.is_expired():
            raise ValueError({"error_code": "RESET_KEY_INVALID", "message": "Invalid or expired reset link."})

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            raise ValueError({"error_code": "PASSWORD_POLICY_VIOLATION", "messages": list(e.messages)})

        with transaction.atomic():
            user.set_password(new_password)
            user.password_changed_at = timezone.now()
            user.activation_key = uuid.uuid4()

            if user.status == User.Status.LOCKED:
                user.status = User.Status.ACTIVE
                lockout = AccountLockout.objects.select_for_update().filter(user=user).first()
                if lockout:
                    lockout.clear()
                    lockout.save(update_fields=["failure_count", "locked_until", "locked_reason", "updated_at"])

            user.save(update_fields=["password", "password_changed_at", "status", "updated_at", "activation_key"])
            pr.mark_used()
            pr.save(update_fields=["used_at", "updated_at"])
            blacklist_all_user_tokens(user)

        log_auth_event(
            actor=None, subject=user, school=user.school,
            event=AuthEventLog.Event.PASSWORD_RESET_COMPLETED,
            request=request,
            metadata={"origin": pr.requested_by},
        )

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _create_and_send_reset(user, origin: str, request=None):
        """
        Creates a PasswordResetRequest record and dispatches the reset email.
        """
        expiry_hours = RESET_EXPIRY_SELF_HOURS if origin == "SELF" else RESET_EXPIRY_ADMIN_HOURS

        PasswordResetRequest.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=expiry_hours),
            requested_by=origin,
            requested_ip=get_client_ip(request) if request else None,
            requested_user_agent=request.META.get("HTTP_USER_AGENT", "") if request else "",
        )
        from ..tasks import send_password_reset_email_task
        send_password_reset_email_task.delay(activation_key=str(user.activation_key), origin=origin, request=request)