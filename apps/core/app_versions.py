"""Which versions of a store app a phone may run.

CodeX publishes store apps (the parent and student apps today) on Android and
iOS. A phone opens the app and asks, before anyone signs in, whether the version
it is running is still acceptable. Each app on each platform has two numbers,
the same for every tenant and set by CodeX in ``NATIVE_APP_VERSIONS``:

* ``minimum``: an older app is blocked until it updates;
* ``latest``: an older app is told, dismissibly, that a newer one exists.

A concrete case: version 1.3.0 of the parent app ships with a Pay button that
fails on some phones. CodeX raises the Android ``minimum`` to 1.3.1. A parent
still on 1.3.0 opens the app, is told to update, and cannot reach the broken
button; a parent on 1.4.0 never notices. Raising ``latest`` alone only nudges.

Versions are dotted numbers of one to three parts (``1``, ``1.2``, ``1.2.3``).
Missing parts are zero, so ``1.2`` equals ``1.2.0``, and parts compare as
numbers, so ``1.10`` is newer than ``1.9``. Anything else is invalid.

Everything here is a pure function of its arguments except
``configured_policy``, which reads the setting, so the rules are tested without
a database.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from django.conf import settings

PLATFORMS = ("android", "ios")

UPDATE_REQUIRED = "update_required"
UPDATE_AVAILABLE = "update_available"
CURRENT = "current"

_VERSION = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,2}")


def parse_version(text: Any) -> tuple[int, int, int]:
    """Return ``text`` as a three-part tuple, or raise ``ValueError``.

    Only a string of one to three dot-separated runs of ASCII digits is valid;
    whitespace, signs, suffixes such as ``-beta`` and a fourth part are not.
    """
    if not isinstance(text, str) or not _VERSION.fullmatch(text):
        raise ValueError(f"Not a dotted numeric version: {text!r}")
    parts = [int(part) for part in text.split(".")]
    parts += [0] * (3 - len(parts))
    return tuple(parts)  # type: ignore[return-value]


def compare_versions(left: str, right: str) -> int:
    """Return -1, 0 or 1 as ``left`` is older than, equal to, or newer than ``right``."""
    a, b = parse_version(left), parse_version(right)
    return (a > b) - (a < b)


def version_status(version: str | None, minimum: str | None, latest: str | None) -> str | None:
    """Classify a running version against a policy's two numbers.

    ``None`` when the caller did not say which version it runs. A policy with no
    ``minimum`` never blocks, and one with no ``latest`` never nudges. Raises
    ``ValueError`` when ``version`` is not a valid version.
    """
    if version is None:
        return None
    parse_version(version)
    if minimum and compare_versions(version, minimum) < 0:
        return UPDATE_REQUIRED
    if latest and compare_versions(version, latest) < 0:
        return UPDATE_AVAILABLE
    return CURRENT


def _usable(value: Any) -> str | None:
    """A configured version as a string, or ``None`` when absent or invalid."""
    try:
        parse_version(value)
    except ValueError:
        return None
    return value


def configured_policy(app: str, platform: str) -> dict[str, str | None] | None:
    """The policy for one app on one platform, or ``None`` when none is set.

    A value that is not a valid version is read as absent rather than raised,
    so a typo in the setting never turns a public route into a server error;
    ``configuration_problems`` is what reports it.
    """
    versions = getattr(settings, "NATIVE_APP_VERSIONS", None)
    if not isinstance(versions, Mapping):
        return None
    entry = versions.get(app)
    policy = entry.get(platform) if isinstance(entry, Mapping) else None
    if not isinstance(policy, Mapping):
        return None
    store_url = policy.get("store_url")
    return {
        "minimum": _usable(policy.get("minimum")),
        "latest": _usable(policy.get("latest")),
        "store_url": store_url if isinstance(store_url, str) and store_url else None,
    }


def configuration_problems(versions: Any, parse_error: str = "") -> list[str]:
    """Describe everything wrong with a ``NATIVE_APP_VERSIONS`` value.

    Empty when the value is sound. Covers a value that could not be parsed, a
    shape that is not app -> platform -> policy, a platform other than
    ``PLATFORMS``, an invalid version string, and a ``latest`` below ``minimum``.
    """
    if parse_error:
        return [f"The NATIVE_APP_VERSIONS environment value is not valid JSON ({parse_error})."]
    if not isinstance(versions, Mapping):
        return ["NATIVE_APP_VERSIONS must be a mapping of app id to platforms."]

    problems: list[str] = []
    for app, platforms in versions.items():
        if not isinstance(platforms, Mapping):
            problems.append(f"{app}: expected a mapping of platform to policy.")
            continue
        for platform, policy in platforms.items():
            where = f"{app} on {platform}"
            if platform not in PLATFORMS:
                problems.append(f"{where}: the platform must be one of {', '.join(PLATFORMS)}.")
                continue
            if not isinstance(policy, Mapping):
                problems.append(f"{where}: expected a policy with minimum, latest and store_url.")
                continue
            valid: dict[str, str] = {}
            for key in ("minimum", "latest"):
                if key not in policy:
                    continue
                try:
                    parse_version(policy[key])
                except ValueError:
                    problems.append(f"{where}: {key} {policy[key]!r} is not a valid version.")
                else:
                    valid[key] = policy[key]
            if len(valid) == 2 and compare_versions(valid["latest"], valid["minimum"]) < 0:
                problems.append(
                    f"{where}: latest {valid['latest']} is below minimum {valid['minimum']}."
                )
    return problems
