"""Write the code-declared field registry to ``FieldDefinition`` rows.

Apps declare their restrictable fields in ``field_access.py`` (see
:mod:`vs_rbac.field_registry`); this command makes the database match.

    python manage.py sync_field_registry
    python manage.py sync_field_registry --check

It runs inside the ``seed_all_permissions`` chain after every module seed,
because a field sits under a ``PermissionResource`` those seeds register. A
declaration naming a module or resource that does not exist stops the command
before anything is written, so a typo in a slug cannot leave half a registry.

Rows are never deleted. A row no declaration names any more is deactivated,
which bumps :class:`PermissionRegistryRevision` the same way a deactivated
permission does. Any change writes one ``FIELD_REGISTRY_SYNCED`` RBAC audit row
listing the keys created, updated and deactivated. A run that finds nothing to
change writes nothing, so it is safe on every deploy.

``--check`` makes no writes and exits non-zero, listing each difference, when
the database does not match the code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vs_rbac.field_registry import all_declarations, validate_declaration

SYNCED_ACTION = "FIELD_REGISTRY_SYNCED"

#: The stored columns compared against a declaration.
_COMPARED = (
    "resource_id", "name", "api_names", "label", "group", "description",
    "sensitive", "writable", "scope", "sort_order", "is_active",
)


@dataclass
class _Plan:
    """What a sync would do, worked out before anything is written."""

    create: dict = field(default_factory=dict)
    update: dict = field(default_factory=dict)
    deactivate: list = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.create or self.update or self.deactivate)

    def describe(self) -> list[str]:
        lines = [f"  + {key} (declared, not in the database)" for key in sorted(self.create)]
        lines += [
            f"  ~ {key} (differs in: {', '.join(changed)})"
            for key, (_, _, changed) in sorted(self.update.items())
        ]
        lines += [
            f"  - {row.key} (no longer declared, still active)"
            for row in sorted(self.deactivate, key=lambda row: row.key)
        ]
        return lines


class Command(BaseCommand):
    help = "Write the code-declared field registry to FieldDefinition rows (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Make no writes; exit non-zero listing differences if the database does not match the code.",
        )

    def handle(self, *args, **options):
        if options["check"]:
            plan = self._plan(all_declarations())
            if not plan.has_changes:
                self.stdout.write("  Field registry is in sync with the code.")
                return
            lines = plan.describe()
            for line in lines:
                self.stdout.write(line)
            raise CommandError(
                f"The field registry does not match the code ({len(lines)} "
                f"difference(s)):\n" + "\n".join(lines)
            )

        with transaction.atomic():
            plan = self._plan(all_declarations(), lock=True)
            self._apply(plan)

        if not plan.has_changes:
            self.stdout.write("  Field registry unchanged.")
            return
        self.stdout.write(
            f"  Field registry synced: {len(plan.create)} created, "
            f"{len(plan.update)} updated, {len(plan.deactivate)} deactivated."
        )

    def _plan(self, declarations, *, lock=False) -> _Plan:
        """Compare *declarations* with the stored rows.

        Raises ``CommandError`` for an invalid declaration or a module or
        resource slug the permission registry does not have.
        """
        from vs_rbac.models import FieldDefinition, PermissionModule, PermissionResource

        for declaration in declarations:
            try:
                validate_declaration(declaration)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        module_names = {declaration.module for declaration in declarations}
        known_modules = set(
            PermissionModule.objects.filter(name__in=module_names)
            .values_list("name", flat=True)
        )
        missing_modules = sorted(module_names - known_modules)
        if missing_modules:
            raise CommandError(
                "The field registry names permission module(s) that do not exist: "
                f"{', '.join(missing_modules)}. Run the permission seeds first."
            )

        resources = {
            (resource.module_id, resource.name): resource
            for resource in PermissionResource.objects.filter(module_id__in=module_names)
        }
        missing_resources = sorted(
            f"{declaration.module}.{declaration.resource}"
            for declaration in declarations
            if (declaration.module, declaration.resource) not in resources
        )
        if missing_resources:
            raise CommandError(
                "The field registry names permission resource(s) that do not exist: "
                f"{', '.join(missing_resources)}. Run the permission seeds first."
            )

        desired: dict[str, dict] = {}
        for declaration in declarations:
            resource = resources[(declaration.module, declaration.resource)]
            for spec in declaration.fields:
                desired[declaration.key_for(spec)] = {
                    "resource": resource,
                    "resource_id": resource.pk,
                    "name": spec.name,
                    "api_names": list(spec.resolved_api_names),
                    "label": spec.label,
                    "group": spec.group,
                    "description": spec.description,
                    "sensitive": spec.sensitive,
                    "writable": spec.writable,
                    "scope": spec.scope,
                    "sort_order": spec.sort_order,
                    "is_active": True,
                }

        rows = FieldDefinition.objects.all()
        if lock:
            rows = rows.select_for_update()
        existing = {row.key: row for row in rows}

        plan = _Plan()
        for key, attrs in desired.items():
            row = existing.get(key)
            if row is None:
                plan.create[key] = attrs
                continue
            changed = [
                column for column in _COMPARED
                if getattr(row, column) != attrs[column]
            ]
            if changed:
                plan.update[key] = (row, attrs, changed)
        plan.deactivate = [
            row for key, row in existing.items()
            if key not in desired and row.is_active
        ]
        return plan

    def _apply(self, plan: _Plan) -> None:
        from vs_rbac.audit import record_rbac_audit
        from vs_rbac.models import FieldDefinition, PermissionRegistryRevision

        if not plan.has_changes:
            return

        for attrs in plan.create.values():
            values = {k: v for k, v in attrs.items() if k != "resource_id"}
            FieldDefinition(**values).save()

        for row, attrs, _ in plan.update.values():
            for column, value in attrs.items():
                if column != "resource_id":
                    setattr(row, column, value)
            row.save()

        for row in plan.deactivate:
            row.is_active = False
            row.save(update_fields=["is_active", "updated_at"])

        if plan.deactivate:
            PermissionRegistryRevision.bump()

        created = sorted(plan.create)
        updated = sorted(plan.update)
        deactivated = sorted(row.key for row in plan.deactivate)
        record_rbac_audit(
            action_type=SYNCED_ACTION,
            entity_type="FieldDefinition",
            entity_id="field_registry",
            entity_label="Field registry",
            severity="WARNING" if deactivated else "INFO",
            summary=(
                f"Field registry synced: {len(created)} created, "
                f"{len(updated)} updated, {len(deactivated)} deactivated"
            ),
            metadata={
                "created": created,
                "updated": updated,
                "deactivated": deactivated,
            },
        )
