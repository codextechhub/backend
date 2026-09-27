"""Resolve a provider name to a configured provider instance.

Callers ask for a provider by its :class:`~vs_payments.constants.PaymentProvider` value
(or fall back to ``settings.PAYMENTS_DEFAULT_PROVIDER``); the registry constructs the
right client from settings. Tests can inject a fake with :func:`register` (e.g. point
``"PAYSTACK"`` at a :class:`~vs_payments.providers.fake.FakeProvider`) so no live keys or
network are ever needed in the suite.

**The Fake provider exists only where a settings module switches it on.** It moves no
money, so an event it "confirms" is a claim nobody can check, and its webhook secret is
a development value. ``PAYMENTS_FAKE_PROVIDER_ENABLED`` is False in ``base.py`` and True
only in the development and test settings modules. With it off, ``FAKE`` is treated
exactly like a name that was never registered: :func:`available_providers` omits it,
:func:`is_available` refuses it and :func:`get_provider` raises for it, even when a test
override is registered under that name. Every caller (creating a collection, a virtual
account or a payout batch, the public webhook receiver, the confirm and replay paths)
resolves through here, so this is the one gate that keeps a Fake payment from being
started, confirmed or booked on a deployment that has not asked for it.
"""
from __future__ import annotations

from django.conf import settings

from ..constants import PaymentProvider
from ..exceptions import ProviderNotConfiguredError
from .base import Provider

# Test/explicit overrides take precedence over settings-built instances.
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


def fake_provider_enabled() -> bool:
    """True when this deployment's settings switch the in-memory Fake provider on."""
    return bool(getattr(settings, "PAYMENTS_FAKE_PROVIDER_ENABLED", False))


def available_providers() -> tuple[str, ...]:
    """The provider names this deployment offers, in :class:`PaymentProvider` order.

    This is the list any provider picker or validation should use. It says which
    providers exist here, not which are configured: Paystack is listed whether or not
    its keys are set, and :func:`get_provider` reports missing keys when it is used.
    """
    return tuple(
        name for name in PaymentProvider.values
        if name != PaymentProvider.FAKE or fake_provider_enabled()
    )


def is_available(name: str | None) -> bool:
    """True when ``name`` (any case) is a provider this deployment offers."""
    return bool(name) and str(name).strip().upper() in available_providers()


# Build a provider client from settings.
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
    if name == PaymentProvider.FAKE:  # No-network adapter; get_provider has checked it is enabled.
        from .fake import FakeProvider
        secret = getattr(settings, "PAYMENTS_FAKE_WEBHOOK_SECRET", "")
        if not secret:  # An empty HMAC key would let anyone sign an event.
            raise ProviderNotConfiguredError("Fake provider webhook secret is not configured.")
        return FakeProvider(secret=secret)
    raise ProviderNotConfiguredError(f"Unknown payment provider '{name}'.")


# Handle the get provider workflow.
def get_provider(name: str | None = None) -> Provider:
    """Return a provider instance for ``name`` (defaults to the configured default).

    Raises :class:`ProviderNotConfiguredError` for a name this deployment does not
    offer (see :func:`available_providers`), before any override is consulted.
    """
    resolved = str(name or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")).strip().upper()
    if not is_available(resolved):  # A disabled provider is refused exactly like an unknown one.
        raise ProviderNotConfiguredError(f"Unknown payment provider '{resolved}'.")
    if resolved in _OVERRIDES:  # Respect explicit overrides before building a configured instance.
        return _OVERRIDES[resolved]  # Return the injected provider.
    return _build(resolved)  # Build the provider from settings when no override exists.
