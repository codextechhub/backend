"""Resolve a provider name to a configured provider instance.  # Central lookup for PSP adapters.

Callers ask for a provider by its :class:`~vs_payments.constants.PaymentProvider` value
(or fall back to ``settings.PAYMENTS_DEFAULT_PROVIDER``); the registry constructs the
right client from settings. Tests can inject a fake with :func:`register` (e.g. point
``"PAYSTACK"`` at a :class:`~vs_payments.providers.fake.FakeProvider`) so no live keys or
network are ever needed in the suite.  # Allow tests and local overrides to bypass real credentials.
"""
from __future__ import annotations  # Import project symbols used by this module.

from django.conf import settings  # Read provider credentials and URLs from Django settings.

from ..exceptions import ProviderNotConfiguredError  # Raised when a provider cannot be built.
from .base import Provider  # Shared provider contract.

# Test/explicit overrides take precedence over settings-built instances.  # Keep injected fakes first.
_OVERRIDES: dict[str, Provider] = {}  # Map normalized provider names to prebuilt instances.


def register(name: str, provider: Provider) -> None:  # Define the callable used by this module.
    """Force ``name`` (a PaymentProvider value) to resolve to ``provider``."""
    _OVERRIDES[name.upper()] = provider  # Store the override using a normalized key.


def unregister(name: str | None = None) -> None:  # Define the callable used by this module.
    """Drop a single override, or all of them when ``name`` is None."""
    if name is None:  # Clear the entire override map when no provider name is supplied.
        _OVERRIDES.clear()  # Remove all test or manual overrides.
    else:  # Remove only the requested override.
        _OVERRIDES.pop(name.upper(), None)  # Ignore missing keys so cleanup is idempotent.


def _build(name: str) -> Provider:  # Define the callable used by this module.
    if name == "PAYSTACK":  # Build a Paystack adapter from configured credentials.
        from .paystack import PaystackProvider  # Import lazily to avoid circular imports.
        secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")  # Secret key required for API auth.
        if not secret:  # Fail fast when the provider is not configured.
            raise ProviderNotConfiguredError("Paystack secret key is not configured.")
        return PaystackProvider(  # Return a configured Paystack client.
            secret_key=secret,  # Continue the structured value.
            base_url=getattr(settings, "PAYSTACK_BASE_URL", "https://api.paystack.co"),  # Use the default Paystack API host unless overridden.
        )  # Close the grouped expression.
    if name == "OPAY":  # Build an OPay adapter from configured credentials.
        from .opay import OPayProvider  # Import lazily to keep module load cheap.
        merchant_id = getattr(settings, "OPAY_MERCHANT_ID", "")  # Required merchant identifier.
        secret = getattr(settings, "OPAY_SECRET_KEY", "")  # Required signing secret.
        if not (merchant_id and secret):  # Both values are needed before the adapter can work.
            raise ProviderNotConfiguredError("OPay merchant id / secret key is not configured.")
        return OPayProvider(  # Return a configured OPay client.
            merchant_id=merchant_id,  # Continue the structured value.
            secret_key=secret,  # Continue the structured value.
            public_key=getattr(settings, "OPAY_PUBLIC_KEY", ""),  # Public key is optional depending on the flow.
            base_url=getattr(settings, "OPAY_BASE_URL", "https://api.opaycheckout.com"),  # Default OPay API host.
            create_path=getattr(settings, "OPAY_CREATE_PATH", ""),  # Custom path overrides when deployed behind proxies.
            status_path=getattr(settings, "OPAY_STATUS_PATH", ""),
            transfer_path=getattr(settings, "OPAY_TRANSFER_PATH", ""),
            transfer_status_path=getattr(settings, "OPAY_TRANSFER_STATUS_PATH", ""),
        )  # Close the grouped expression.
    if name == "FAKE":  # Provide a no-network adapter for tests and demos.
        from .fake import FakeProvider  # Import lazily so fake-only tests don't need provider extras.
        return FakeProvider()  # Return the in-memory test provider.
    raise ProviderNotConfiguredError(f"Unknown payment provider '{name}'.")  # Reject unsupported provider names.


def get_provider(name: str | None = None) -> Provider:  # Define the callable used by this module.
    """Return a provider instance for ``name`` (defaults to the configured default)."""
    resolved = (name or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")).upper()  # Normalize the provider name.
    if resolved in _OVERRIDES:  # Respect explicit overrides before building a configured instance.
        return _OVERRIDES[resolved]  # Return the injected provider.
    return _build(resolved)  # Build the provider from settings when no override exists.
