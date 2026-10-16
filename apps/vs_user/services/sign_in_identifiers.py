"""Identifiers a tenant issues to its own people, and where to reach them.

This app is an engine. It knows accounts, invitations and sessions, and nothing
about the records a product keeps about the people behind them. A product that
issues its own identifier (a staff number, an admission number) and wants it
accepted wherever an email address is, registers two small callables here from
its ``AppConfig.ready()``, and this app never imports the product.

Identifier resolvers
    ``resolver(*, tenant_id, identifier) -> iterable of User``. Given a tenant
    and the text typed into the sign-in box, return the accounts that identifier
    names inside that tenant. Every registered resolver runs for every
    identifier that is not an email address, and the sign-in proceeds only when
    exactly one distinct account comes back across all of them. A staff number
    and an admission number that happen to collide inside one tenant therefore
    sign nobody in, rather than letting whichever resolver ran first win.

Delivery-address providers
    ``provider(user) -> iterable of DeliveryAddress``. Given an account, return
    the extra addresses a link meant for that account should also reach: a
    pupil's invitation and password reset go to the guardian who can act on
    them as well as to the pupil. The account's own address is never repeated
    here, and an account with no address of its own is reachable only through
    what its providers return.

Identifier labels
    ``provider(user) -> str | None``. The identifier an account signs in with
    when it has no email address (a pupil's admission number), so an
    activation or reset page can say "Sign in with BFS/2025/0143" rather than
    show an empty email box.

Every registry is keyed by a name, so registering twice (a test that
reloads an app) replaces rather than duplicates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable

from ..email_normalization import normalize_email
from ..models import User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryAddress:
    """One extra address to write to, with the name to greet it by."""

    email: str
    name: str = ""


IdentifierResolver = Callable[..., Iterable[User]]
DeliveryAddressProvider = Callable[[User], Iterable[DeliveryAddress]]

_resolvers: dict[str, IdentifierResolver] = {}
_address_providers: dict[str, DeliveryAddressProvider] = {}
_label_providers: dict[str, Callable[[User], "str | None"]] = {}


def register_identifier_resolver(name: str, resolver: IdentifierResolver) -> None:
    """Make *resolver* answer for the identifiers its product issues."""
    _resolvers[name] = resolver


def register_delivery_address_provider(
    name: str, provider: DeliveryAddressProvider,
) -> None:
    """Make *provider* contribute extra addresses for the accounts it knows."""
    _address_providers[name] = provider


def resolve_identifier_users(*, tenant_id: int, identifier: str) -> list[User]:
    """The distinct accounts *identifier* names inside *tenant_id*, at most two.

    Two is enough to know the identifier is ambiguous, and it keeps a hostile
    or careless identifier from loading more than that. A resolver that raises
    is logged and treated as having matched nothing: an unreadable registry
    entry must not turn the sign-in page into an error, and the sign-in
    answers as it would for an unknown identifier either way.
    """
    found: dict[int, User] = {}
    for name, resolver in list(_resolvers.items()):
        try:
            for user in resolver(tenant_id=tenant_id, identifier=identifier):
                found.setdefault(user.pk, user)
        except Exception:
            logger.exception("Identifier resolver %r failed.", name)
        if len(found) > 1:
            break
    return list(found.values())[:2]


def extra_delivery_addresses(user: User) -> list[DeliveryAddress]:
    """Addresses beyond the account's own that a link for *user* should reach.

    Folded to the stored form, deduplicated, and stripped of the account's own
    address so one inbox never receives the same link twice.
    """
    own = normalize_email(user.email)
    seen = {own} if own else set()
    out: list[DeliveryAddress] = []
    for name, provider in list(_address_providers.items()):
        try:
            for address in provider(user):
                email = normalize_email(address.email)
                if not email or email in seen:
                    continue
                seen.add(email)
                out.append(DeliveryAddress(email=email, name=address.name))
        except Exception:
            logger.exception("Delivery address provider %r failed.", name)
    return out


def register_identifier_label_provider(name: str, provider) -> None:
    """Make *provider* name the identifier its product issued to an account."""
    _label_providers[name] = provider


def sign_in_label(user: User) -> str:
    """What *user* signs in with: their email, else the identifier issued to them.

    Empty when the account has neither, which only an account still being set
    up can be.
    """
    if user.email:
        return user.email
    for name, provider in list(_label_providers.items()):
        try:
            label = provider(user)
        except Exception:
            logger.exception("Identifier label provider %r failed.", name)
            continue
        if label:
            return str(label)
    return ""
