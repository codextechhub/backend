"""The code registry of fields an administrator may restrict, per resource.

Which fields appear under Field Access is a developer's decision, not an
administrator's: a field with no serializer behind it would be a switch that
changes nothing. So each app declares its own fields in a ``field_access.py``
module and calls :func:`register_fields` from its ``AppConfig.ready()``, the
same one-way seam the Export Centre uses. This module names no domain and
imports no app; a school, health or finance field arrives here only because
the app that owns it registered it.

The declarations are held in process memory. ``manage.py sync_field_registry``
writes them to :class:`vs_rbac.models.FieldDefinition`, which is what the
catalogue endpoints read.

A declaration is validated when it is registered, so a duplicate name or an
unclassified scope fails the process at start-up rather than surfacing later
as a switch that means two things.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class FieldSpec:
    """One field as an app declares it.

    ``api_names`` lists the names the field travels under in responses and
    request bodies when that is not just ``name``. ``scope`` is the scope of
    the permission key that guards the field, ``TENANT`` or ``PLATFORM``.
    """

    name: str
    label: str
    group: str = ""
    description: str = ""
    sensitive: bool = False
    writable: bool = True
    scope: str = ""
    sort_order: int = 0
    api_names: tuple[str, ...] = ()

    @property
    def resolved_api_names(self) -> tuple[str, ...]:
        """The client-facing names, defaulting to the registry name alone."""
        return tuple(self.api_names) or (self.name,)


@dataclass(frozen=True)
class FieldDeclaration:
    """Every field one app declares under one ``module.resource``.

    ``surfaces`` are dotted import paths of the serializer classes that emit
    these fields, so the registry tests can prove each declared name reaches a
    client and each guarded name is declared.
    """

    module: str
    resource: str
    fields: tuple[FieldSpec, ...]
    surfaces: tuple[str, ...]

    def key_for(self, spec: FieldSpec) -> str:
        return f"{self.module}.{self.resource}.{spec.name}"


_REGISTRY: dict[tuple[str, str], FieldDeclaration] = {}


def validate_declaration(declaration: FieldDeclaration) -> None:
    """Raise ``ValueError`` naming the first problem in *declaration*.

    Checks slugs, non-blank labels, unique registry names and unique client
    names inside the resource, and a scope that is one of
    :class:`vs_rbac.models.PermissionScope`.
    """
    from .models import PermissionScope

    where = f"{declaration.module}.{declaration.resource}"
    for part in (declaration.module, declaration.resource):
        if not _NAME_RE.match(part or ""):
            raise ValueError(f"Field registry: '{where}' is not a valid module.resource slug.")
    names: set[str] = set()
    api_names: set[str] = set()
    for spec in declaration.fields:
        key = declaration.key_for(spec)
        if not _NAME_RE.match(spec.name or ""):
            raise ValueError(f"Field registry: '{key}' does not have a valid field name.")
        if not (spec.label or "").strip():
            raise ValueError(f"Field registry: '{key}' has no label.")
        if spec.name in names:
            raise ValueError(f"Field registry: '{key}' is declared twice.")
        names.add(spec.name)
        for api_name in spec.resolved_api_names:
            if api_name in api_names:
                raise ValueError(
                    f"Field registry: client name '{api_name}' is claimed by two "
                    f"fields under '{where}'."
                )
            api_names.add(api_name)
        if spec.scope not in PermissionScope.values:
            raise ValueError(
                f"Field registry: '{key}' has scope '{spec.scope}'. Declare the "
                f"scope of the permission key that guards it: "
                f"{', '.join(PermissionScope.values)}."
            )


def register_fields(
    module: str,
    resource: str,
    *,
    fields: tuple[FieldSpec, ...],
    surfaces: tuple[str, ...],
) -> FieldDeclaration:
    """Declare the restrictable fields of ``module.resource``.

    Idempotent on ``(module, resource)``: registering again replaces the
    earlier declaration, so a reloaded app never doubles its fields. The
    declaration is validated before it is stored, so a refused one leaves the
    registry exactly as it was.
    """
    declaration = FieldDeclaration(
        module=module,
        resource=resource,
        fields=tuple(fields),
        surfaces=tuple(surfaces),
    )
    validate_declaration(declaration)
    _REGISTRY[(module, resource)] = declaration
    return declaration


def get_declaration(module: str, resource: str) -> FieldDeclaration | None:
    return _REGISTRY.get((module, resource))


def all_declarations() -> list[FieldDeclaration]:
    """Every registered declaration, ordered by module then resource."""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]
