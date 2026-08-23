"""The support-ticket context allowlist, and the registry that extends it.

A ticket may carry a little context about where the person was when they raised
it. What it may carry is an allowlist, and it is an allowlist rather than a free
dictionary for one reason: a support ticket is read by staff outside the
tenant, so anything a client can put in it is something a client can leak into
it. Four keys are declared here and validated by
:class:`vs_tickets.serializers.TicketContextSerializer`.

**Modules add their own keys by registering them, from their own app.** That is
the same shape the Export Centre uses for datasets (see
``schools/vs_schools/export_datasets.py``, registered from ``AppConfig.ready``)
and it exists for the same reason: this app is domain-neutral and must not
import ``apps/schools/`` to discover what a school's vocabulary is. Onboarding
knows what an onboarding task key is; this app only needs to know that the
value it receives is one of the ones onboarding published.

**Every registered field is a closed vocabulary.** Not a regex, not free text:
a fixed tuple of permitted values. That is what made carrying context
acceptable in the first place, and a registered key that accepted arbitrary
strings would quietly undo it for every module at once.
"""
from __future__ import annotations

import re

from django.core.exceptions import ImproperlyConfigured

#: The keys this app declares itself. Their validation lives in the serializer,
#: because each has a shape of its own rather than a list of values.
CORE_KEYS = frozenset({"guide_id", "route_pattern", "product_area", "app_version"})

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: key -> permitted values. Written once per key at startup, read per request.
_REGISTERED: dict[str, tuple[str, ...]] = {}


def register_context_choice_field(key: str, *, choices, description: str = "") -> None:
    """Publish one closed-vocabulary context key from the module that owns it.

    Call from ``AppConfig.ready()``. Idempotent for an identical re-registration
    (``ready()`` can run more than once in a test process); a second
    registration of the same key with *different* values is a programming error
    and raises, because two modules disagreeing about what a key means is not
    something to resolve silently at import time.

    Namespace the key with your module (``onboarding_task_key``), since the
    allowlist is flat and shared by every module in the platform.
    """
    if not _KEY_RE.match(key or ""):
        raise ImproperlyConfigured(
            f"Ticket context key {key!r} must be lowercase letters, digits and "
            f"underscores, 3 to 40 characters."
        )
    if key in CORE_KEYS:
        raise ImproperlyConfigured(
            f"Ticket context key {key!r} is one this app declares itself."
        )

    values = tuple(str(choice) for choice in choices)
    if not values:
        raise ImproperlyConfigured(
            f"Ticket context key {key!r} must publish at least one permitted value."
        )
    for value in values:
        if not _VALUE_RE.match(value):
            raise ImproperlyConfigured(
                f"Ticket context value {value!r} for {key!r} is not a plain "
                f"identifier. Registered fields carry closed vocabularies only."
            )

    existing = _REGISTERED.get(key)
    if existing is not None and existing != values:
        raise ImproperlyConfigured(
            f"Ticket context key {key!r} is already registered with different "
            f"values."
        )
    _REGISTERED[key] = values


def registered_choice_fields() -> dict[str, tuple[str, ...]]:
    """Every registered key and its permitted values."""
    return dict(_REGISTERED)


def allowed_keys() -> frozenset[str]:
    """Everything a ticket's context may carry, core and registered together."""
    return CORE_KEYS | frozenset(_REGISTERED)
