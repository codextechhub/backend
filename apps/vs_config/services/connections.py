"""Bounded, read-only checks for deployment-owned integration connections."""

from django.core.cache import cache
from django.core.mail import get_connection


COOLDOWN_SECONDS = 30


class ConnectionTestCoolingDown(Exception):
    pass


class IntegrationConnection:
    """Small audit target for a deployment-owned connection test."""

    def __init__(self, connection):
        self.pk = connection


def _claim_test_slot(*, actor_id, connection):
    key = f"config-connection-test:{actor_id}:{connection}"
    if not cache.add(key, True, timeout=COOLDOWN_SECONDS):
        raise ConnectionTestCoolingDown


def test_email_connection(*, actor_id):
    """Open and close the configured email backend without sending a message."""
    _claim_test_slot(actor_id=actor_id, connection="email")
    connection = get_connection(fail_silently=False)
    try:
        connection.open()
    finally:
        connection.close()


def test_payment_connection(*, actor_id):
    """Run the provider's read-only credential check without creating money movement."""
    _claim_test_slot(actor_id=actor_id, connection="payments")
    from vs_payments.providers.registry import get_provider

    get_provider().healthcheck()
