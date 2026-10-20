"""Where an account's own app is, when it is not the school app it signed in to.

A browser sign-in on the school's own sign-in page answers ``home_app`` with
the path of the app the account belongs in (``{"path": "/parent/"}``), so the
page can send the person there instead of into the staff app, where they hold
no role. The activation and password-reset previews answer it too, so a person
setting their password is sent to that app's own sign-in afterwards.

A product registers a provider (:func:`register_home_app`) that answers a path
for the accounts it knows and None for the rest; the first that answers wins.
The parent portal answers ``/parent/`` for a parent with no staff role, so a
teacher who is also a parent keeps the staff app. A pupil's app is not named
here: a pupil is offered a device session, which sign-in already answers as
``device_app``. A provider that raises is logged and treated as not answering,
so a broken product hook never breaks a sign-in.

An account can also hold another app beside the one it signs in to: a teacher
whose own child attends keeps the staff app and is a parent in the parent app
as well. A product registers an *other app* provider
(:func:`register_other_app`) that answers a path for such accounts, and the
staff app's sign-in and ``me/`` list them as ``other_apps``, each
``{"key", "path"}``, so the staff app can offer the way in.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

HomeAppProvider = Callable[[object], Optional[str]]

_providers: dict[str, HomeAppProvider] = {}


def register_home_app(name: str, provider: HomeAppProvider) -> None:
    """Make *provider* name the app the accounts it knows belong in."""
    _providers[name] = provider


def home_app_path(user) -> Optional[str]:
    """The path of *user*'s own app on their tenant's address, or None."""
    if user is None or not getattr(user, "pk", None):
        return None
    for name, provider in list(_providers.items()):
        try:
            path = provider(user)
        except Exception:  # noqa: BLE001 - a product hook never breaks sign-in
            logger.exception("Home app provider %r failed.", name)
            continue
        if path:
            return path
    return None


def home_app_payload(user) -> Optional[dict]:
    """``{"path": ...}`` for *user*'s own app, as sign-in and the link previews answer it, or None."""
    path = home_app_path(user)
    return {"path": path} if path else None


_other_providers: dict[str, HomeAppProvider] = {}


def register_other_app(key: str, provider: HomeAppProvider) -> None:
    """Make *provider* name an app, under *key*, that the accounts it knows can also open."""
    _other_providers[key] = provider


def other_apps_payload(user) -> list[dict]:
    """``[{"key", "path"}]`` for each other app *user* can open, in registration order.

    A provider that raises is logged and left out, as a home app provider is.
    """
    if user is None or not getattr(user, "pk", None):
        return []
    found = []
    for key, provider in list(_other_providers.items()):
        try:
            path = provider(user)
        except Exception:  # noqa: BLE001 - a product hook never breaks sign-in
            logger.exception("Other app provider %r failed.", key)
            continue
        if path:
            found.append({"key": key, "path": path})
    return found
