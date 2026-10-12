"""The configuration screens and settings the search bar can route to.

Configuration definitions live in the database, so they are contributed on
every read rather than registered once.

Three kinds of setting are offered here, every one of them to the platform's
own staff only, because every ``config.*`` key is platform-scoped:

* each setting on a dedicated platform screen (security, integrations, the
  platform's profile and defaults for new schools), anchored at the field name
  that screen's endpoint uses, under the key that screen requires;
* every other active definition, on the generic settings screen
  (``config.settings``), anchored at its key. That includes a definition only
  the platform may set: the generic screen sets it at platform scope.
* nothing that another app gives a screen of its own
  (:data:`vs_config.runtime_settings.DEDICATED_SCREEN_KEYS`), because the
  generic value endpoints refuse it, and routing somebody there would send
  them to a field that cannot be saved.

A school reaches the settings that are its to change through its own settings
screens, which register themselves from ``apps/schools/`` through
:func:`definition_setting_places`, so each setting is labelled and described
in its definition's own words wherever it is offered.

The rows are read once and kept for :data:`CACHE_SECONDS`, and dropped whenever
a definition is saved or deleted in this process. :func:`clear_cache` drops
them on demand.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from django.db.models.signals import post_delete, post_save

from vs_search.registry import ACTION, SETTING, Place, register, register_contributor

from .constants import ConfigPermissions
from .labels import setting_group_label
from .models import ConfigurationDefinition
from .platform_settings import ONBOARDING_FIELDS, PROFILE_FIELDS
from .runtime_settings import (
    DEDICATED_SCREEN_KEYS,
    INTEGRATION_FIELDS,
    PROTECTED_SETTING_KEYS,
    SECURITY_FIELDS,
)

#: How long a read of the definitions is reused.
CACHE_SECONDS = 60

SETTINGS_SCREEN = "config.settings"
PLATFORM_SETTINGS_SCREEN = "config.platform_settings"
SECURITY_SETTINGS_SCREEN = "config.security_settings"
INTEGRATION_SETTINGS_SCREEN = "config.integration_settings"

#: (screen, group, {field on the screen: definition key}, permission) for each
#: dedicated platform settings screen. The platform settings body nests its
#: fields under ``profile`` and ``onboarding``, so their anchors are dotted.
PLATFORM_SCREENS = (
    (PLATFORM_SETTINGS_SCREEN, "Platform settings",
     {**{f"profile.{name}": key for name, key in PROFILE_FIELDS.items()},
      **{f"onboarding.{name}": key for name, key in ONBOARDING_FIELDS.items()}},
     ConfigPermissions.VALUE_VIEW),
    (SECURITY_SETTINGS_SCREEN, "Security settings", dict(SECURITY_FIELDS),
     ConfigPermissions.SECURITY_VIEW),
    (INTEGRATION_SETTINGS_SCREEN, "Integration settings", dict(INTEGRATION_FIELDS),
     ConfigPermissions.INTEGRATION_VIEW),
)

#: Definitions offered on a screen of their own rather than the generic one.
ON_OWN_SCREEN = frozenset(
    key for _screen, _group, fields, _permission in PLATFORM_SCREENS for key in fields.values()
) | PROTECTED_SETTING_KEYS | DEDICATED_SCREEN_KEYS

_lock = threading.Lock()
_cache: dict = {"at": None, "rows": {}}


@dataclass(frozen=True)
class DefinitionWords:
    """What a definition is called and what it does, and where it may be set."""

    label: str
    description: str
    allowed_scopes: frozenset


def clear_cache(**_kwargs) -> None:
    """Forget the cached definitions, so the next read queries them again."""
    with _lock:
        _cache["at"] = None
        _cache["rows"] = {}


def active_definitions() -> dict[str, DefinitionWords]:
    """Every active definition's words, by key, read at most once per :data:`CACHE_SECONDS`.

    Shared with the apps whose own settings screens show definitions, so they
    label those settings in the definition's words without a query of their own.
    """
    now = time.monotonic()
    with _lock:
        if _cache["at"] is not None and now - _cache["at"] < CACHE_SECONDS:
            return _cache["rows"]
    rows = {
        key: DefinitionWords(label, description or "", frozenset(scopes or ()))
        for key, label, description, scopes in ConfigurationDefinition.objects.filter(
            is_active=True,
        ).values_list("key", "label", "description", "allowed_scopes")
    }
    with _lock:
        _cache["at"] = now
        _cache["rows"] = rows
    return rows


def definition_place(key, words: DefinitionWords) -> Place:
    """The generic settings screen's place for one configuration definition."""
    return Place(
        kind=SETTING,
        key=f"config.settings.{key}",
        label=words.label,
        description=words.description,
        group=setting_group_label(key),
        screen=SETTINGS_SCREEN,
        anchor=key,
        permission=ConfigPermissions.VALUE_VIEW,
    )


def definition_setting_places(
    screen, group, fields, *, permission, capability=None, keywords=(),
) -> list[Place]:
    """One setting place per field of a screen whose fields are configuration definitions.

    *fields* maps the field name the screen's endpoint uses (the anchor) to the
    definition key behind it. Each place is labelled and described in the
    definition's own words, with its catalogue group as a keyword, so renaming
    a definition renames its search hit. A definition that is missing or
    archived is not offered, which is also when the screen stops serving it.
    """
    definitions = active_definitions()
    out = []
    for field_name, definition_key in fields.items():
        words = definitions.get(definition_key)
        if words is None:
            continue
        out.append(Place(
            kind=SETTING,
            key=f"{screen}.{field_name}",
            label=words.label,
            description=words.description,
            keywords=(setting_group_label(definition_key), *keywords),
            group=group,
            screen=screen,
            anchor=field_name,
            permission=permission,
            capability=capability,
        ))
    return out


def platform_setting_places() -> list[Place]:
    """Every setting on the generic and dedicated platform settings screens."""
    out = [
        definition_place(key, words)
        for key, words in active_definitions().items()
        if key not in ON_OWN_SCREEN
    ]
    for screen, group, fields, permission in PLATFORM_SCREENS:
        out += definition_setting_places(screen, group, fields, permission=permission)
    return out


register_contributor(platform_setting_places)
post_save.connect(clear_cache, sender=ConfigurationDefinition,
                  dispatch_uid="vs_config.search_places.clear_cache.save")
post_delete.connect(clear_cache, sender=ConfigurationDefinition,
                    dispatch_uid="vs_config.search_places.clear_cache.delete")

register(
    Place(
        kind=ACTION, key="config.settings", label="Configuration settings",
        description="Every setting a school or branch may override, with its effective value.",
        keywords=("configuration",),
        screen=SETTINGS_SCREEN, permission=ConfigPermissions.VALUE_VIEW,
    ),
    Place(
        kind=ACTION, key="config.platform_settings", label="Platform settings",
        description="The platform's own profile and defaults for new schools.",
        screen="config.platform_settings", permission=ConfigPermissions.VALUE_VIEW,
    ),
    Place(
        kind=ACTION, key="config.security_settings", label="Security settings",
        description="Sign-in lockout, reset link lifetimes and invitation expiry.",
        keywords=("password", "lockout", "login"),
        screen="config.security_settings", permission=ConfigPermissions.SECURITY_VIEW,
    ),
    Place(
        kind=ACTION, key="config.integration_settings", label="Integration settings",
        description="The email sender and delivery retries.",
        keywords=("email",),
        screen="config.integration_settings", permission=ConfigPermissions.INTEGRATION_VIEW,
    ),
    Place(
        kind=ACTION, key="config.definitions", label="Setting definitions",
        description="The catalogue of every setting, its type, default and where it may be set.",
        keywords=("configuration",),
        screen="config.definitions", permission=ConfigPermissions.DEFINITION_VIEW,
    ),
    Place(
        kind=ACTION, key="config.entitlements", label="Plan entitlements",
        description="Which modules and levels each school's plan grants.",
        keywords=("plan", "package", "modules"),
        screen="config.entitlements", permission=ConfigPermissions.ENTITLEMENT_VIEW,
    ),
    Place(
        kind=ACTION, key="config.overrides", label="Feature overrides",
        description="Modules forced on or off for one school, whatever its plan says.",
        keywords=("plan", "modules"),
        screen="config.overrides", permission=ConfigPermissions.OVERRIDE_VIEW,
    ),
    Place(
        kind=ACTION, key="config.audit", label="Configuration audit trail",
        description="Every change to a setting, plan or feature, and who made it.",
        keywords=("history", "changes"),
        screen="config.audit", permission=ConfigPermissions.AUDIT_VIEW,
    ),
    Place(
        kind=ACTION, key="config.capabilities", label="Capabilities",
        description="Which modules and features are switched on.",
        keywords=("modules", "features", "plan"),
        screen="config.capabilities", permission=ConfigPermissions.CAPABILITY_VIEW,
    ),
)
