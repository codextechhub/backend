"""Rendering a declared surface as a caller who may read none of its fields.

:class:`vs_rbac.field_enforcement.FieldAccessMixin` filters the keys of its
own payload and of every nested serializer that carries it. It cannot see
inside what a ``SerializerMethodField`` returns, or inside a dict a view merges
in by hand, so a registered field can travel below the top of a response while
the switch that hides it removes only the copy at the top. Reading the
serializer's code cannot prove the absence of that; rendering it can.

:class:`DeepPayloadChecks` renders every declared surface of a resource on a
real record, as a caller whose role has Read and Write off for every field of
that resource, and walks the whole payload at every depth. A key named like any
registered client name of the resource fails the test with its path and value,
unless the surface's case lists that path in :attr:`DeepPayloadChecks.allowlist`
with the reason it names something else.

Each app that owns a declaration provides one case, in a module named
``tests_field_access_deep_payload`` or ``tests.test_field_access_deep_payload``,
because only that app can build its records. The registry tests find every
case and fail when a declared surface is rendered by none of them, so a surface
declared tomorrow is rendered the day it is declared.

The walk is deliberately stricter than "the field's value appears": every
registered name is refused wherever it appears as a key, whatever its value,
because a copy that happens to differ today (a blank phone, a changed address)
is the same leak on the next record. Each allowlisted path must still match a
real hit, so an entry cannot outlive the payload it excused.
"""
from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module

from rest_framework.test import APIRequestFactory

from vs_rbac.field_evaluator import get_field_access
from vs_rbac.field_registry import get_declaration
from vs_user.models import User

from .helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    set_field_access,
)

_caller_counter = itertools.count(1)


@dataclass
class Sample:
    """One declared surface, the record it is rendered on, and its context.

    ``instance`` is what the view hands the serializer: a model instance, a
    queryset or list with ``many=True``, or, for a form serializer that no view
    renders from a model, a mapping of the record's real values in the shape
    the form carries. ``context`` holds whatever the view adds beside the
    request (``multi_branch``, sibling maps); the request is always the closed
    caller's. ``tenant`` is the tenant the record belongs to, where a case
    renders records of more than one; it defaults to the case's.
    """

    surface: str
    instance: object
    context: dict = field(default_factory=dict)
    many: bool = False
    tenant: object = None


def _surface_class(path: str):
    module_path, name = path.rsplit(".", 1)
    return getattr(import_module(module_path), name)


def walk(payload, path: str = ""):
    """Every ``(path, key, value)`` in *payload*, at every depth.

    A mapping key extends the path with ``.key``; a list element with ``[]``,
    so one allowlist entry covers every row of a list.
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, key, value
            yield from walk(value, here)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from walk(item, f"{path}[]")


def registered_names(resource: str) -> set[str]:
    """Every client name the declaration of *resource* registers."""
    module, _, name = resource.partition(".")
    declaration = get_declaration(module, name)
    return {
        api_name
        for spec in declaration.fields
        for api_name in spec.resolved_api_names
    }


def resource_of(surface: str) -> str:
    """The ``module.resource`` whose declaration lists *surface*."""
    from vs_rbac.field_registry import all_declarations

    for declaration in all_declarations():
        if surface in declaration.surfaces:
            return f"{declaration.module}.{declaration.resource}"
    raise AssertionError(f"{surface} is not a declared surface of any resource.")


def closed_request(tenant, resource: str):
    """A request from a member of *tenant* who may read no field of *resource*.

    The resource's real declarations are installed, and the caller holds one
    role with Read and Write off on every one of them, so a field open by
    default is closed as surely as a sensitive one. The caller is nobody the
    records are about, so no owner rule opens anything.
    """
    keys = install_declared_fields(resource)
    number = next(_caller_counter)
    role = make_role(tenant, name=f"Sees nothing {number}", key=f"deep_payload_{number}")
    set_field_access(role, *keys, read=False, write=False)
    user = User.objects.create_user(
        email=f"sees-nothing-{number}@deep-payload.test", password="testpass123",
        tenant=tenant, status="ACTIVE", first_name="Sees", last_name="Nothing",
    )
    make_assignment(tenant, user, role, branch=None)
    request = APIRequestFactory().get("/")
    request.user = User.objects.get(pk=user.pk)
    request.tenant = tenant
    return request, keys


class DeepPayloadChecks:
    """The check every app's deep payload case runs, over the samples it builds.

    Mixed into a ``TestCase`` by each owning app, never collected on its own.
    A subclass sets :attr:`covers` to the dotted paths of the surfaces it
    renders, implements :meth:`samples` and, where a surface's resource is
    held by a tenant other than ``self.tenant``, :meth:`tenant_for`.
    """

    #: The ``module.resource`` declarations this case answers for.
    resources: frozenset = frozenset()

    #: The declared surfaces this case renders. The registry tests require the
    #: cases between them to cover every declared surface.
    covers: frozenset = frozenset()

    #: ``(surface path, key path) -> reason`` for a key named like a registered
    #: field that belongs to something else. Matched on the exact path.
    allowlist: dict = {}

    def samples(self) -> list[Sample]:
        raise NotImplementedError

    def tenant_for(self, resource: str):
        """The tenant whose member renders *resource*; ``self.tenant`` by default."""
        return self.tenant

    def _render(self, sample: Sample, request):
        cls = _surface_class(sample.surface)
        context = {**sample.context, "request": request}
        return cls(sample.instance, many=sample.many, context=context).data

    def test_every_surface_is_rendered_by_this_case(self):
        rendered = {sample.surface for sample in self.samples()}
        self.assertEqual(rendered, set(self.covers))
        for sample in self.samples():
            with self.subTest(surface=sample.surface):
                self.assertIn(
                    resource_of(sample.surface), self.resources,
                    "A case renders only the resources it names.",
                )

    def test_no_registered_field_reaches_a_caller_who_may_read_none(self):
        hits_seen = set()
        requests = {}
        for sample in self.samples():
            resource = resource_of(sample.surface)
            names = registered_names(resource)
            tenant = sample.tenant or self.tenant_for(resource)
            if (tenant.pk, resource) not in requests:
                requests[(tenant.pk, resource)] = closed_request(tenant, resource)
            request, keys = requests[(tenant.pk, resource)]

            access = get_field_access(request.user, tenant=request.tenant)
            self.assertEqual(
                [key for key in keys if access.can_read(key)], [],
                f"The caller for {resource} can still read some of its fields, "
                f"so this render would prove nothing.",
            )

            open_payload = _surface_class(sample.surface)(
                sample.instance, many=sample.many, context=dict(sample.context),
            ).data
            carried = {key for _, key, _ in walk(open_payload) if key in names}
            with self.subTest(surface=sample.surface, check="sample carries the fields"):
                self.assertTrue(
                    carried,
                    f"{sample.surface} rendered for nobody carries none of "
                    f"{sorted(names)}, so the closed render below proves nothing.",
                )

            payload = self._render(sample, request)
            leaks = []
            for path, key, value in walk(payload):
                if key not in names:
                    continue
                hits_seen.add((sample.surface, path))
                if (sample.surface, path) in self.allowlist:
                    continue
                leaks.append(f"{path} = {value!r}")
            with self.subTest(surface=sample.surface):
                self.assertEqual(
                    leaks, [],
                    f"{sample.surface} sends fields of {resource} to a caller "
                    f"who may read none of them.",
                )
        self.assertEqual(set(self.allowlist) - hits_seen, set(),
                         "An allowlist entry matches nothing any more.")
