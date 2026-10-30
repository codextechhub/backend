"""Sign-in records past their period, for :mod:`core.pruning`.

A sign-in leaves several rows behind: the attempt (the email typed, the IP and
browser), the session, and the one-time links and codes it used. They exist
for security and for looking into misuse, and the privacy policy keeps them a
year. Nothing here can still sign anybody in: the longest session lives 90
days and every link or code expires within a day, so a row a year old is only
history.

The sign-in events written to the audit trail are not touched here; see
:mod:`core.pruning`.
"""
from __future__ import annotations

from core.pruning import delete_in_batches


def prune_sign_in_records(cutoff, *, dry_run: bool = False) -> dict:
    from vs_notifications.models import NativePushDevice, PushSubscription

    from .models import (
        AccountClosureLink, AuthAttempt, LoginSession, PasswordResetRequest,
        TwoStepChallenge,
    )

    def older(model, **filters):
        return delete_in_batches(model._base_manager.filter(**filters), dry_run=dry_run)

    return {
        "sign_in_attempts": older(AuthAttempt, created_at__lt=cutoff),
        "sessions": older(LoginSession, last_seen_at__lt=cutoff),
        "two_step_challenges": older(TwoStepChallenge, expires_at__lt=cutoff),
        "password_reset_links": older(PasswordResetRequest, expires_at__lt=cutoff),
        "account_closure_links": older(AccountClosureLink, expires_at__lt=cutoff),
        "web_push_browsers": older(PushSubscription, last_seen_at__lt=cutoff),
        "push_phones": older(NativePushDevice, last_seen_at__lt=cutoff),
    }


def prune_expired_refresh_tokens(cutoff, *, dry_run: bool = False) -> dict:
    """What ``flushexpiredtokens`` does: an expired refresh token, and its
    blacklist row with it, answers nothing and only grows the table."""
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    return {
        "refresh_tokens": delete_in_batches(
            OutstandingToken.objects.filter(expires_at__lt=cutoff), dry_run=dry_run,
        ),
    }
