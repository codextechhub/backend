"""Resolve a provider name to a configured provider instance.

Callers ask for a provider by its :class:`~vs_payments.constants.PaymentProvider` value
(or fall back to ``settings.PAYMENTS_DEFAULT_PROVIDER``); the registry constructs the
right client from settings. Tests can inject a fake with :func:`register` (e.g. point
``"PAYSTACK"`` at a :class:`~vs_payments.providers.fake.FakeProvider`) so no live keys or
network are ever needed in the suite.
"""
from __future__ import annotations

from django.conf import settings

from ..exceptions import ProviderNotConfiguredError
from .base import Provider

# Test/explicit overrides take precedence over settings-built instances.
_OVERRIDES: dict[str, Provider] = {}


def register(name: str, provider: Provider) -> None:
    """Force ``name`` (a PaymentProvider value) to resolve to ``provider``."""
    _OVERRIDES[name.upper()] = provider


def unregister(name: str | None = None) -> None:
    """Drop a single override, or all of them when ``name`` is None."""
    if name is None:
        _OVERRIDES.clear()
    else:
        _OVERRIDES.pop(name.upper(), None)


def _build(name: str) -> Provider:
    if name == "PAYSTACK":
        from .paystack import PaystackProvider
        secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
        if not secret:
            raise ProviderNotConfiguredError("Paystack secret key is not configured.")
        return PaystackProvider(
            secret_key=secret,
            base_url=getattr(settings, "PAYSTACK_BASE_URL", "https://api.paystack.co"),
        )
    if name == "OPAY":
        from .opay import OPayProvider
        merchant_id = getattr(settings, "OPAY_MERCHANT_ID", "")
        secret = getattr(settings, "OPAY_SECRET_KEY", "")
        if not (merchant_id and secret):
            raise ProviderNotConfiguredError("OPay merchant id / secret key is not configured.")
        return OPayProvider(
            merchant_id=merchant_id,
            secret_key=secret,
            public_key=getattr(settings, "OPAY_PUBLIC_KEY", ""),
            base_url=getattr(settings, "OPAY_BASE_URL", "https://api.opaycheckout.com"),
            create_path=getattr(settings, "OPAY_CREATE_PATH", ""),
            status_path=getattr(settings, "OPAY_STATUS_PATH", ""),
            transfer_path=getattr(settings, "OPAY_TRANSFER_PATH", ""),
            transfer_status_path=getattr(settings, "OPAY_TRANSFER_STATUS_PATH", ""),
        )
    if name == "FAKE":
        from .fake import FakeProvider
        return FakeProvider()
    raise ProviderNotConfiguredError(f"Unknown payment provider '{name}'.")


def get_provider(name: str | None = None) -> Provider:
    """Return a provider instance for ``name`` (defaults to the configured default)."""
    resolved = (name or getattr(settings, "PAYMENTS_DEFAULT_PROVIDER", "PAYSTACK")).upper()
    if resolved in _OVERRIDES:
        return _OVERRIDES[resolved]
    return _build(resolved)
