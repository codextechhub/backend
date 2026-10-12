"""Every place a person can be taken to from the search bar, and who finds people.

A *place* is a screen (an ``action``) or one setting on a screen (a
``setting``). Places are declared in code by the app that owns the screen, so
the search bar learns a new screen the moment its app ships one, and no
frontend list has to be kept in step by hand.

Discovery. The first call that reads the registry imports every installed
app's ``search_places`` module (:func:`django.utils.module_loading.autodiscover_modules`),
and each module registers what it owns at import time. This app imports
nothing of anybody else's: a school app registers through exactly the same
door as a finance one.

Four kinds of registration:

* :func:`register` adds fixed places. A key is a stable dotted id and may be
  registered once; a second registration of the same key raises
  :class:`DuplicatePlaceKey`, because two places answering to one key would let
  a frontend route to whichever happened to load last.
* :func:`register_contributor` adds a function called on every read, for places
  that live in the database (configuration definitions). It returns an iterable
  of :class:`Place`; its keys share the same uniqueness rule.
* :func:`register_people_provider` adds a function that finds people. See
  :data:`PeopleProvider` for the contract. Providers run in registration order,
  which follows ``INSTALLED_APPS``.
* :func:`register_condition` names a predicate a place may require through
  ``Place.condition``, for a screen that exists only sometimes: at a tenant
  with several branches, for a whole-tenant reader, under one custody mode.
  See :data:`Condition`. This app knows no condition by name; the app that
  owns the rule registers it, by calling the function its own endpoint asks.

A place carries no business data: a label, a description, where it routes and
who may open it. ``permission`` and ``capability`` decide visibility and are
never sent to a client.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import autodiscover_modules

#: A screen a person can open.
ACTION = "action"
#: One setting on a screen; ``anchor`` names the field.
SETTING = "setting"
KINDS = frozenset({ACTION, SETTING})

#: The module name every installed app may provide.
DISCOVERY_MODULE = "search_places"


class DuplicatePlaceKey(ImproperlyConfigured):
    """Two places were declared under one key."""


class DuplicateCondition(ImproperlyConfigured):
    """Two conditions were registered under one name."""


class UnknownCondition(ImproperlyConfigured):
    """A place names a condition nobody registered."""


@dataclass(frozen=True)
class Place:
    """One screen or setting the search bar can route to.

    ``key`` is unique and stable (``finance.settings.documents.default_invoice_due_days``).
    ``screen`` is the stable frontend screen key the hit routes to
    (``finance.settings.documents``), and ``anchor`` the field or section on it,
    empty for the screen itself. ``keywords`` are extra words a person might
    type that the label does not contain ("due date" for "Days until an invoice
    is due"). ``group`` is the heading a setting is listed under.

    ``permission`` is one permission key, a tuple meaning any of them, or None
    for any signed-in member of the tenant. It is the key the screen's own read
    endpoint requires, so the search bar never offers a screen that would then
    refuse its reader. ``capability`` is the ``vs_config`` capability the
    screen's module answers to, as :mod:`vs_rbac.capability_map` maps it, or
    None where the module is core.

    ``condition`` names a registered :data:`Condition` the place also needs,
    or None for a screen that always exists for whoever may open it. It is
    asked only of a place that has passed the permission and capability
    filter and matched the query, and is never sent to a client.
    """

    kind: str
    key: str
    label: str
    screen: str
    description: str = ""
    keywords: tuple[str, ...] = ()
    group: str = ""
    anchor: str = ""
    permission: str | tuple[str, ...] | None = None
    capability: str | None = None
    condition: str | None = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ImproperlyConfigured(f"Place {self.key!r} has unknown kind {self.kind!r}.")
        if not self.key or not self.label or not self.screen:
            raise ImproperlyConfigured("A place needs a key, a label and a screen.")
        if isinstance(self.keywords, str):
            object.__setattr__(self, "keywords", (self.keywords,))
        else:
            object.__setattr__(self, "keywords", tuple(self.keywords))
        if isinstance(self.permission, list):
            object.__setattr__(self, "permission", tuple(self.permission))

    @property
    def permission_keys(self) -> tuple[str, ...]:
        """The permission keys any one of which opens this place; empty when none is needed."""
        if self.permission is None:
            return ()
        if isinstance(self.permission, str):
            return (self.permission,)
        return self.permission


#: ``fn(*, request, tenant, user, query, limit) -> list[dict]``.
#:
#: Returns at most ``limit`` hits the owning module has already scoped to the
#: caller's tenant and branches and checked against its own read permission
#: (returning ``[]`` without it). ``request`` is passed so the module can apply
#: the same Field Access its own search endpoint applies. Each hit carries at
#: least ``type``, ``id``, ``label``, ``meta`` (short secondary text, may be
#: "") and ``screen``. A hit never carries an email address, a phone number or
#: a photograph.
PeopleProvider = Callable[..., list]

#: ``fn() -> Iterable[Place]``, called on every read of the registry.
Contributor = Callable[[], Iterable[Place]]

#: ``fn(*, request, tenant, branch, user) -> bool``.
#:
#: Whether the screens naming this condition exist for this caller right now.
#: ``tenant`` and ``branch`` are the request's resolved scope (``tenant`` may
#: be None for a platform caller with no tenant asserted). Asked at most once
#: per search, and only when a place the caller could otherwise open, and
#: that the query matched, names it, so a condition that reads the database
#: costs nothing on a search that needs no such place. It must mirror the rule the screen's own endpoint or
#: frontend applies, by calling the function that decides it.
Condition = Callable[..., bool]


@dataclass
class PlaceRegistry:
    """A set of places, contributors and people providers.

    The module-level functions below act on one shared instance whose discovery
    imports every app's ``search_places``. A test builds its own instance
    without discovery to exercise the rules in isolation.
    """

    discover: Callable[[], None] | None = None
    _places: dict[str, Place] = field(default_factory=dict)
    _contributors: list[Contributor] = field(default_factory=list)
    _providers: list[PeopleProvider] = field(default_factory=list)
    _conditions: dict[str, Condition] = field(default_factory=dict)
    _discovered: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def register(self, *places: Place) -> None:
        with self._lock:
            for place in places:
                if not isinstance(place, Place):
                    raise ImproperlyConfigured(f"{place!r} is not a Place.")
                if place.key in self._places:
                    raise DuplicatePlaceKey(f"Search place {place.key!r} is registered twice.")
                self._places[place.key] = place

    def register_contributor(self, fn: Contributor) -> Contributor:
        with self._lock:
            if fn not in self._contributors:
                self._contributors.append(fn)
        return fn

    def register_people_provider(self, fn: PeopleProvider) -> PeopleProvider:
        with self._lock:
            if fn not in self._providers:
                self._providers.append(fn)
        return fn

    def register_condition(self, name: str, fn: Condition) -> Condition:
        with self._lock:
            if not name:
                raise ImproperlyConfigured("A condition needs a name.")
            if self._conditions.get(name, fn) is not fn:
                raise DuplicateCondition(f"Search condition {name!r} is registered twice.")
            self._conditions[name] = fn
        return fn

    def condition(self, name: str) -> Condition:
        """The predicate registered as *name*; :class:`UnknownCondition` if none is."""
        self._ensure_discovered()
        try:
            return self._conditions[name]
        except KeyError:
            raise UnknownCondition(f"No search condition is registered as {name!r}.") from None

    def condition_names(self) -> set[str]:
        self._ensure_discovered()
        return set(self._conditions)

    def _ensure_discovered(self) -> None:
        if self._discovered or self.discover is None:
            return
        with self._lock:
            if not self._discovered:
                self.discover()
                self._discovered = True

    def places(self) -> list[Place]:
        """Every fixed place, then every contributed one; keys unique across both."""
        self._ensure_discovered()
        out = dict(self._places)
        for contributor in list(self._contributors):
            for place in contributor():
                if place.key in out:
                    raise DuplicatePlaceKey(
                        f"Search place {place.key!r} is contributed twice.",
                    )
                out[place.key] = place
        return list(out.values())

    def people_providers(self) -> list[PeopleProvider]:
        self._ensure_discovered()
        return list(self._providers)


def _autodiscover() -> None:
    autodiscover_modules(DISCOVERY_MODULE)


registry = PlaceRegistry(discover=_autodiscover)

register = registry.register
register_contributor = registry.register_contributor
register_people_provider = registry.register_people_provider
register_condition = registry.register_condition
places = registry.places
people_providers = registry.people_providers
condition = registry.condition
condition_names = registry.condition_names
