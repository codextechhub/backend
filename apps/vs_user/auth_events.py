"""The names of identity and account events.

Every sign-in, lockout, invitation, password and email change is recorded by
``vs_user.services.audit.log_auth_event`` as an ``AuditEvent`` in the identity
module, with one of these names in ``metadata['auth_event']``. That audit row is
the only record: there is no separate table of auth events. Readers (the staff
history, ``GET /v1/user/auth-events/``) query ``AuditEvent`` and use this list
for the names and their display labels.

``services.audit`` maps each name onto the ``AuditActionType`` the audit trail
files it under; a name added here needs an entry there too.
"""
from __future__ import annotations

from django.db import models


class AuthEvent(models.TextChoices):
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
    CARD_LOGIN_ROTATED       = 'CARD_LOGIN_ROTATED',       'Card Login Rotated'
