"""Resolve a provider name to a configured provider instance.  # Central lookup for PSP adapters.

Callers ask for a provider by its :class:`~vs_payments.constants.PaymentProvider` value
(or fall back to ``settings.PAYMENTS_DEFAULT_PROVIDER``); the registry constructs the
right client from settings. Tests can inject a fake with :func:`register` (e.g. point
``"PAYSTACK"`` at a :class:`~vs_payments.providers.fake.FakeProvider`) so no live keys or
network are ever needed in the suite.  # Allow tests and local overrides to bypass real credentials.
"""
from __future__ import annotations

from django.conf import settings

from ..exceptions import ProviderNotConfiguredError
from .base import Provider

# Test/explicit overrides take precedence over settings-built instances.  # Keep injected fakes first.
_OVERRIDES: dict[str, Provider] = {}  # Map normalized provider names to prebuilt instances.


# Handle the register workflow.
def register(name: str, provider: Provider) -> None:
    """Force ``name`` (a PaymentProvider value) to resolve to ``provider``."""
    _OVERRIDES[name.upper()] = provider  # Store the override using a normalized key.


# Handle the unregister workflow.
def unregister(name: str | None = None) -> None:
    """Drop a single override, or all of them when ``name`` is None."""
    if name is None:  # Clear the entire override map when no provider name is supplied.
        _OVERRIDES.clear()  # Remove all test or manual overrides.
    else:  # Remove only the requested override.
        _OVERRIDES.pop(name.upper(), None)  # Ignore missing keys so cleanup is idempotent.


# Support the build workflow.
def _build(name: str) -> Provider:
    if name == "PAYSTACK":  # Build a Paystack adapter from configured credentials.
        from .paystack import PaystackProvider
        secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")  # Secret key required for API auth.
        if not secret:  # Fail fast when the provider is not configured.
            raise ProviderNotConfiguredError("Paystack secret key is not configured.")
        return PaystackProvider(  # Return a configured Paystack client.
            secret_key=secret,
            base_url=getattr(settings, "PAYSTACK_BASE_URL", "https://api.paystack.co"),  # Use the default Paystack API host unless overridden.
        )
    if name == "FAKE":  # Provide a no-network adapter for tests and demos.
        from .fake import FakeProvider
        return FakeProvider()  # Return the in-memory test provider.
    raise ProviderNotConfiguredError(f"Unknown payment provider '{name}'.")


# Handle the get provider workflow.
def get_provider(name: str | None = None) -> Provider:
    """Return a provider instance for ``name`` (defaults to the configured default)."""
    resolved = (name or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")).upper()  # Normalize the provider name.
    if resolved in _OVERRIDES:  # Respect explicit overrides before building a configured instance.
        return _OVERRIDES[resolved]  # Return the injected provider.
    return _build(resolved)  # Build the provider from settings when no override exists.
