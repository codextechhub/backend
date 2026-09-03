"""
schools.core.fal.registry
=========================

The dependency-injection seam. Consumers never instantiate an adapter directly;
they ask the registry for a port. The concrete implementation is resolved from
Django settings, so production wires the Django adapters while tests swap in the
in-memory fakes (``testing``) with a single override call.

Adapter-swap story
------------------
Each port resolves from a settings key with a default dotted path. To swap an
adapter you change *one* settings value; no consumer import changes. In a test
you call ``set_*`` with a fake and ``reset()`` afterwards. One class may back
several ports, or you may split them across modules; the registry does not care,
it only imports dotted paths.

Settings keys::

    FAL_ENTITY_RESOLVER
    FAL_FEE_TERM_BRIDGE
    FAL_STUDENT_CUSTOMER
    FAL_FINANCE_RBAC
    FAL_FINANCE_READER
    FAL_GUARDIAN_LINK
    FAL_PARENT_PAYMENT
    FAL_PROCUREMENT_READER
    FAL_PROCUREMENT_ACTIONS

**Defaults are derived from this package's own location**, not hard-coded, so
they follow the package if it ever moves again.

Note: there is deliberately **no** ``FAL_PAYMENT_PORT`` key.
``PaymentPort``/``apply_payment`` is deferred to v1.2 (decision 2026-07-04);
settlement stays inside ``vs_payments``.

``FAL_GUARDIAN_LINK`` resolves the guardian-to-student link from the student
roll, which is what opens the parent portal's payment bridge. A deployment
without the student module points it at ``DenyAllGuardianLinkAdapter`` so the
bridge fails closed instead of failing to import.

Usage at a call site::

    from schools.core.fal import get_finance_reader
    reader = get_finance_reader()
    result = reader.fee_status(student.id)

Usage in a test::

    from schools.core.fal import registry
    from schools.core.fal.testing import FakeFinanceReader
    registry.set_finance_reader(FakeFinanceReader(outstanding=120000))
    ...
    registry.reset()   # restore settings-based resolution
"""

from __future__ import annotations

from .exceptions import FALNotConfiguredError
from .ports import (
    EntityResolverPort,
    FeeTermBridgePort,
    FinanceRbacPort,
    FinanceReadPort,
    GuardianLinkPort,
    ParentPaymentBridgePort,
    ProcurementActionPort,
    ProcurementReadPort,
    StudentCustomerPort,
)

#: Dotted path of the production adapter module, derived from wherever this
#: package actually lives. Moving the package moves the defaults with it.
_ADAPTER_MODULE = f"{__package__}.adapters.django_finance"

# Default dotted paths used when a setting is absent.
_DEFAULTS = {
    "FAL_ENTITY_RESOLVER": f"{_ADAPTER_MODULE}.DjangoEntityResolverAdapter",
    "FAL_FEE_TERM_BRIDGE": f"{_ADAPTER_MODULE}.DjangoFeeTermBridgeAdapter",
    "FAL_STUDENT_CUSTOMER": f"{_ADAPTER_MODULE}.DjangoStudentCustomerAdapter",
    "FAL_FINANCE_RBAC": f"{_ADAPTER_MODULE}.DjangoFinanceRbacAdapter",
    "FAL_FINANCE_READER": f"{_ADAPTER_MODULE}.DjangoFinanceReadAdapter",
    "FAL_GUARDIAN_LINK": f"{_ADAPTER_MODULE}.DjangoGuardianLinkAdapter",
    "FAL_PARENT_PAYMENT": f"{_ADAPTER_MODULE}.DjangoParentPaymentBridgeAdapter",
    "FAL_PROCUREMENT_READER": f"{_ADAPTER_MODULE}.DjangoProcurementReadAdapter",
    "FAL_PROCUREMENT_ACTIONS": f"{_ADAPTER_MODULE}.DjangoProcurementActionAdapter",
}

# Process-wide singletons; populated lazily and cached.
_cache: dict[str, object] = {}


def _load(setting_name: str):
    """Import and instantiate the class named by ``setting_name``.

    Imports are done lazily and locally so this module stays importable without
    Django (the contract, ports and testing layers never need a settings
    module).
    """
    try:
        from django.conf import settings
        from django.core.exceptions import ImproperlyConfigured
        from django.utils.module_loading import import_string
    except Exception as exc:  # pragma: no cover - only in a non-Django context
        raise FALNotConfiguredError(
            "schools.core.fal.registry requires Django to resolve adapters. "
            "In tests, inject a fake via registry.set_* instead."
        ) from exc

    # Django being *installed* is not the same as Django being *configured*:
    # touching `settings` without DJANGO_SETTINGS_MODULE raises
    # ImproperlyConfigured, which a consumer catching FALNotConfiguredError would
    # not see. Translate it, so the promise this module's docstring makes ("ask
    # the registry, get a typed error") holds in both cases.
    try:
        dotted = getattr(settings, setting_name, _DEFAULTS[setting_name])
    except ImproperlyConfigured as exc:
        raise FALNotConfiguredError(
            "Django settings are not configured, so schools.core.fal.registry "
            "cannot resolve adapters. In tests, inject a fake via registry.set_*."
        ) from exc

    try:
        cls = import_string(dotted)
    except ImportError as exc:
        raise FALNotConfiguredError(f"Could not import {setting_name}={dotted!r}") from exc
    return cls()


def _get(setting_name: str):
    if setting_name not in _cache:
        _cache[setting_name] = _load(setting_name)
    return _cache[setting_name]


# --------------------------------------------------------------------------- #
# Accessors
# --------------------------------------------------------------------------- #
def get_entity_resolver() -> EntityResolverPort:
    return _get("FAL_ENTITY_RESOLVER")  # type: ignore[return-value]


def get_fee_term_bridge() -> FeeTermBridgePort:
    return _get("FAL_FEE_TERM_BRIDGE")  # type: ignore[return-value]


def get_student_customer() -> StudentCustomerPort:
    return _get("FAL_STUDENT_CUSTOMER")  # type: ignore[return-value]


def get_finance_rbac() -> FinanceRbacPort:
    return _get("FAL_FINANCE_RBAC")  # type: ignore[return-value]


def get_finance_reader() -> FinanceReadPort:
    return _get("FAL_FINANCE_READER")  # type: ignore[return-value]


def get_guardian_link() -> GuardianLinkPort:
    return _get("FAL_GUARDIAN_LINK")  # type: ignore[return-value]


def get_parent_payment_bridge() -> ParentPaymentBridgePort:
    return _get("FAL_PARENT_PAYMENT")  # type: ignore[return-value]


def get_procurement_reader() -> ProcurementReadPort:
    return _get("FAL_PROCUREMENT_READER")  # type: ignore[return-value]


def get_procurement_actions() -> ProcurementActionPort:
    return _get("FAL_PROCUREMENT_ACTIONS")  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Test overrides
# --------------------------------------------------------------------------- #
def set_entity_resolver(port: EntityResolverPort) -> None:
    _cache["FAL_ENTITY_RESOLVER"] = port


def set_fee_term_bridge(port: FeeTermBridgePort) -> None:
    _cache["FAL_FEE_TERM_BRIDGE"] = port


def set_student_customer(port: StudentCustomerPort) -> None:
    _cache["FAL_STUDENT_CUSTOMER"] = port


def set_finance_rbac(port: FinanceRbacPort) -> None:
    _cache["FAL_FINANCE_RBAC"] = port


def set_finance_reader(port: FinanceReadPort) -> None:
    _cache["FAL_FINANCE_READER"] = port


def set_guardian_link(port: GuardianLinkPort) -> None:
    _cache["FAL_GUARDIAN_LINK"] = port


def set_parent_payment_bridge(port: ParentPaymentBridgePort) -> None:
    _cache["FAL_PARENT_PAYMENT"] = port


def set_procurement_reader(port: ProcurementReadPort) -> None:
    _cache["FAL_PROCUREMENT_READER"] = port


def set_procurement_actions(port: ProcurementActionPort) -> None:
    _cache["FAL_PROCUREMENT_ACTIONS"] = port


def reset() -> None:
    """Clear cached ports so the next access re-resolves from settings."""
    _cache.clear()
