"""The field registry agrees with the code that guards fields today.

Field Access replaces per-serializer permission maps with a registry an
administrator can see. That only works if the registry names exactly what the
serializers guard: a guarded field left out of it is a field nobody can open,
and a declared field no serializer emits is a switch that changes nothing.
These tests lock the registry to the code in both directions, and pin the
behaviour of ``sync_field_registry``, which writes it to the database.
"""
import importlib
import pkgutil
from io import StringIO
from unittest import mock

from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import serializers as drf_serializers

from vs_exports.catalogue import all_datasets
from vs_rbac.field_registry import (
    FieldDeclaration,
    FieldSpec,
    all_declarations,
    get_declaration,
    register_fields,
)
from vs_rbac.fls import FieldSecurityMixin
from vs_rbac.models import (
    FieldDefinition,
    Permission,
    PermissionRegistryRevision,
    PermissionScope,
    RBACAuditLog,
)

from .helpers import make_permission

SYNC = "vs_rbac.management.commands.sync_field_registry.all_declarations"

#: Every resource with a registered field. Adding one is a deliberate change.
EXPECTED_RESOURCES = {
    ("finance", "bankaccount"),
    ("finance", "payrollrun"),
    ("finance", "salary"),
    ("import", "batches"),
    ("import", "jobs"),
    ("import", "templates"),
    ("payments", "payout"),
    ("payments", "virtual_account"),
    ("platform", "staff_profile"),
    ("platform", "team"),
    ("procurement", "vendor"),
    ("school", "students"),
}

#: Serializers that can write a registered name without carrying a rule for it,
#: each with the reason that is safe. Every entry must still match a real hit.
WRITE_PATH_ALLOWLIST = {
    ("vs_import_data.serializers.ImportBatchUploadSerializer", "file"):
        "The upload is the only way a file enters a batch, and no key guards writing one.",
    ("vs_import_data.serializers.ImportTemplateCreateSerializer", "validation_rules"):
        "Behind import.templates.create, a platform-only key; no key guards writing rules.",
    ("vs_import_data.serializers.ImportTemplateUpdateSerializer", "validation_rules"):
        "Behind import.templates.manage, the platform-only key that also reads the rules.",
    ("schools.vs_students.serializers.EnrolmentWriteSerializer", "enrolment_date"):
        "A new pupil's enrolment date is set by whoever enrols them; only changing it "
        "on an existing record needs school.students.manage (owner decision, "
        "enrolment and import alike).",
}


def _surface(path):
    module_path, name = path.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), name)


def _api_names(declaration):
    return {
        api_name
        for spec in declaration.fields
        for api_name in spec.resolved_api_names
    }


def _vendor_view_fields():
    from vs_procurement.views.vendors import _SENSITIVE_VENDOR_FIELDS

    return set(_SENSITIVE_VENDOR_FIELDS)


def _all_field_security_serializers():
    """Every production serializer that guards a field with the old mixin."""
    for app in django_apps.get_app_configs():
        name = f"{app.name}.serializers"
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise

    def walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)

    for cls in set(walk(FieldSecurityMixin)):
        parts = cls.__module__.split(".")
        if any(part == "tests" or part.startswith("test") for part in parts):
            continue
        if cls.read_permissions or cls.write_permissions:
            yield cls


class RegistryMatchesTheSerializersTests(SimpleTestCase):
    """What is declared reaches a client, and what is guarded is declared."""

    def test_the_registry_covers_the_expected_resources(self):
        declared = {(d.module, d.resource) for d in all_declarations()}
        self.assertEqual(declared, EXPECTED_RESOURCES)

    def test_every_declared_name_is_emitted_by_one_of_its_surfaces(self):
        for declaration in all_declarations():
            self.assertTrue(declaration.surfaces, declaration)
            emitted = set()
            for path in declaration.surfaces:
                emitted |= set(_surface(path)(context={}).fields)
            for spec in declaration.fields:
                for api_name in spec.resolved_api_names:
                    with self.subTest(key=declaration.key_for(spec), api_name=api_name):
                        self.assertIn(api_name, emitted)

    def test_every_guarded_name_on_a_surface_is_registered(self):
        """A guarded field the registry forgets would be closed to everybody."""
        for declaration in all_declarations():
            registered = _api_names(declaration)
            for path in declaration.surfaces:
                cls = _surface(path)
                guarded = set(cls.read_permissions) | set(cls.write_permissions)
                with self.subTest(surface=path):
                    self.assertEqual(guarded - registered, set())

    def test_the_vendor_views_hand_written_guard_is_registered(self):
        vendor = get_declaration("procurement", "vendor")
        self.assertEqual(_vendor_view_fields() - _api_names(vendor), set())

    def test_every_serializer_using_the_old_mixin_is_a_declared_surface(self):
        declared = {path for d in all_declarations() for path in d.surfaces}
        for cls in _all_field_security_serializers():
            path = f"{cls.__module__}.{cls.__qualname__}"
            with self.subTest(serializer=path):
                self.assertIn(path, declared)

    def test_sensitive_means_hidden_today(self):
        """Read-guarded fields are sensitive; write-only guarded ones are not."""
        for declaration in all_declarations():
            surfaces = [_surface(path) for path in declaration.surfaces]
            for spec in declaration.fields:
                hidden = any(
                    api_name in cls.read_permissions
                    for cls in surfaces
                    for api_name in spec.resolved_api_names
                )
                if (declaration.module, declaration.resource) == ("procurement", "vendor"):
                    hidden = hidden or spec.name in _vendor_view_fields()
                with self.subTest(key=declaration.key_for(spec)):
                    self.assertEqual(spec.sensitive, hidden)

    def test_every_export_link_names_a_registered_field_of_the_same_sensitivity(self):
        registry = {
            declaration.key_for(spec): spec
            for declaration in all_declarations()
            for spec in declaration.fields
        }
        linked = 0
        for dataset in all_datasets():
            for column in dataset.fields:
                if not column.access:
                    continue
                linked += 1
                with self.subTest(dataset=dataset.key, column=column.id):
                    self.assertIn(column.access, registry)
                    self.assertEqual(column.sensitive, registry[column.access].sensitive)
        self.assertGreater(linked, 0)


def _is_test_module(name):
    return any(part == "tests" or part.startswith("test") for part in name.split("."))


def _serializers_owned_by(app):
    """Every production serializer class defined inside *app*.

    Every module of the app is imported first, so a serializer declared beside
    a view is found as surely as one in ``serializers.py``. The app is found
    from a dotted surface path at run time, never imported by name, so this
    engine's tests import no domain app.
    """
    for info in pkgutil.walk_packages([app.path], prefix=f"{app.name}."):
        if _is_test_module(info.name) or {"migrations", "management"} & set(info.name.split(".")):
            continue
        importlib.import_module(info.name)

    def walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)

    for cls in set(walk(drf_serializers.Serializer)):
        module = cls.__module__
        if module.split(".")[: len(app.name.split("."))] != app.name.split("."):
            continue
        if not _is_test_module(module):
            yield cls


class EveryWritePathIsKnownTests(SimpleTestCase):
    """A second route into a registered field cannot arrive without its rule.

    A field's write rule binds only the serializer that declares it, so a
    second serializer writing the same field has no rule unless someone copies
    it. For each declaration, every serializer in the app owning its surfaces
    is scanned: a ``ModelSerializer`` when its model is a surface's model, a
    plain ``Serializer`` by field name. A non-read-only field named like a
    registered writable field must carry a key a surface already guards that
    name with. Where no surface guards the name, the serializer must at least
    be a declared surface.
    """

    def test_every_serializer_that_can_write_a_registered_field_is_known(self):
        seen = set()
        for declaration in all_declarations():
            writable = {
                api_name
                for spec in declaration.fields if spec.writable
                for api_name in spec.resolved_api_names
            }
            surfaces = [_surface(path) for path in declaration.surfaces]
            models = {
                getattr(getattr(cls, "Meta", None), "model", None) for cls in surfaces
            } - {None}
            rules = {}
            for cls in surfaces:
                for name, key in getattr(cls, "write_permissions", {}).items():
                    rules.setdefault(name, set()).add(key)
            owners = {
                django_apps.get_containing_app_config(path.rsplit(".", 1)[0])
                for path in declaration.surfaces
            }
            self.assertNotIn(None, owners, declaration.surfaces)

            for app in owners:
                for cls in _serializers_owned_by(app):
                    if issubclass(cls, drf_serializers.ModelSerializer) and getattr(
                        getattr(cls, "Meta", None), "model", None,
                    ) not in models:
                        continue
                    path = f"{cls.__module__}.{cls.__qualname__}"
                    guards = (
                        cls.write_permissions
                        if issubclass(cls, FieldSecurityMixin) else {}
                    )
                    for name, field in cls(context={}).fields.items():
                        if field.read_only or name not in writable:
                            continue
                        seen.add((path, name))
                        if (path, name) in WRITE_PATH_ALLOWLIST:
                            continue
                        with self.subTest(serializer=path, field=name):
                            if name in rules:
                                self.assertIn(
                                    guards.get(name), rules[name],
                                    f"{path} can write '{name}' without "
                                    f"{sorted(rules[name])}.",
                                )
                            else:
                                self.assertIn(
                                    path, declaration.surfaces,
                                    f"{path} can write '{name}' and is neither "
                                    f"guarded nor a declared surface.",
                                )
        self.assertEqual(set(WRITE_PATH_ALLOWLIST) - seen, set())


class RegistrationValidationTests(SimpleTestCase):
    def test_a_name_declared_twice_is_refused_and_nothing_is_stored(self):
        with self.assertRaises(ValueError):
            register_fields(
                "testfields", "duplicated", surfaces=(),
                fields=(
                    FieldSpec("serial", "Serial", scope=PermissionScope.TENANT),
                    FieldSpec("serial", "Serial again", scope=PermissionScope.TENANT),
                ),
            )
        self.assertIsNone(get_declaration("testfields", "duplicated"))

    def test_an_unclassified_scope_is_refused(self):
        with self.assertRaises(ValueError):
            register_fields(
                "testfields", "unscoped", surfaces=(),
                fields=(FieldSpec("serial", "Serial"),),
            )
        self.assertIsNone(get_declaration("testfields", "unscoped"))


class DefaultAccessTests(SimpleTestCase):
    def test_a_sensitive_field_starts_closed(self):
        field = FieldDefinition(sensitive=True, writable=True)
        self.assertEqual(field.default_access, {"read": False, "write": False})

    def test_a_normal_field_starts_open(self):
        field = FieldDefinition(sensitive=False, writable=True)
        self.assertEqual(field.default_access, {"read": True, "write": True})

    def test_a_field_nobody_can_write_never_starts_writable(self):
        field = FieldDefinition(sensitive=False, writable=False)
        self.assertEqual(field.default_access, {"read": True, "write": False})


class RegistryScopeMatchesTheGuardingKeyTests(TestCase):
    """Each field carries the scope of the permission key that guards it today."""

    @classmethod
    def setUpTestData(cls):
        for command in (
            "seed_actions",
            "seed_school_permissions",
            "seed_platform_permissions",
            "seed_import_permissions",
            "seed_finance_permissions",
            "seed_procurement_permissions",
            "seed_payments_permissions",
        ):
            call_command(command, verbosity=0, stdout=StringIO())
        call_command("sync_field_registry", stdout=StringIO())

    def _guards(self):
        """``(module, resource, api_name, permission_key)`` for every guard in the code."""
        for declaration in all_declarations():
            for path in declaration.surfaces:
                cls = _surface(path)
                for mapping in (cls.read_permissions, cls.write_permissions):
                    for api_name, key in mapping.items():
                        yield declaration.module, declaration.resource, api_name, key
        for api_name in _vendor_view_fields():
            yield "procurement", "vendor", api_name, "procurement.vendor.view_sensitive"

    def test_the_real_registry_syncs_against_the_seeded_tree(self):
        declared = sum(len(d.fields) for d in all_declarations())
        self.assertEqual(FieldDefinition.objects.filter(is_active=True).count(), declared)

    def test_every_field_scope_equals_its_guarding_keys_scope(self):
        scopes = dict(Permission.objects.values_list("key", "scope"))
        rows = list(FieldDefinition.objects.select_related("resource"))
        guarded = set()
        for module, resource, api_name, key in self._guards():
            matches = [
                row for row in rows
                if row.resource.module_id == module
                and row.resource.name == resource
                and api_name in row.api_names
            ]
            with self.subTest(api_name=f"{module}.{resource}.{api_name}", guard=key):
                self.assertEqual(len(matches), 1)
                self.assertIn(key, scopes, "the guarding key is not seeded")
                self.assertEqual(matches[0].scope, scopes[key])
                guarded.add(matches[0].key)
        self.assertEqual(guarded, {row.key for row in rows})


def _declaration(*fields, module="testfields", resource="gadget"):
    return FieldDeclaration(
        module=module, resource=resource, fields=tuple(fields), surfaces=(),
    )


def _spec(name="serial_number", label="Serial number", **kwargs):
    kwargs.setdefault("scope", PermissionScope.TENANT)
    return FieldSpec(name, label, **kwargs)


class SyncFieldRegistryTests(TestCase):
    """``sync_field_registry`` against declarations the test controls."""

    def setUp(self):
        make_permission("testfields.gadget.view")
        make_permission("testfields.gizmo.view")

    def _sync(self, declarations, *args):
        out = StringIO()
        with mock.patch(SYNC, return_value=list(declarations)):
            call_command("sync_field_registry", *args, stdout=out)
        return out.getvalue()

    def _audits(self):
        return RBACAuditLog.objects.filter(action_type="FIELD_REGISTRY_SYNCED")

    def test_it_creates_rows_keyed_by_module_resource_and_name(self):
        self._sync([_declaration(
            _spec(group="Identity", sensitive=True, writable=False, sort_order=5),
            _spec("owner", "Owner", api_names=("owner_id", "owner_name")),
        )])
        row = FieldDefinition.objects.get(key="testfields.gadget.serial_number")
        self.assertEqual(row.resource.name, "gadget")
        self.assertEqual(row.api_names, ["serial_number"])
        self.assertEqual(
            (row.group, row.sensitive, row.writable, row.sort_order, row.scope),
            ("Identity", True, False, 5, PermissionScope.TENANT),
        )
        owner = FieldDefinition.objects.get(key="testfields.gadget.owner")
        self.assertEqual(owner.api_names, ["owner_id", "owner_name"])
        audit = self._audits().get()
        self.assertEqual(
            audit.metadata["created"],
            ["testfields.gadget.owner", "testfields.gadget.serial_number"],
        )

    def test_a_second_run_writes_nothing_and_audits_nothing(self):
        declarations = [_declaration(_spec())]
        self._sync(declarations)
        audits = self._audits().count()
        with CaptureQueriesContext(connection) as queries:
            output = self._sync(declarations)
        writes = [
            q["sql"] for q in queries.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])
        self.assertEqual(self._audits().count(), audits)
        self.assertIn("unchanged", output)

    def test_a_changed_label_updates_the_row(self):
        self._sync([_declaration(_spec(label="Serial"))])
        self._sync([_declaration(_spec(label="Serial number"))])
        row = FieldDefinition.objects.get(key="testfields.gadget.serial_number")
        self.assertEqual(row.label, "Serial number")
        self.assertEqual(
            self._audits().order_by("-created_at").first().metadata["updated"],
            ["testfields.gadget.serial_number"],
        )

    def test_a_removed_declaration_is_deactivated_and_bumps_the_revision(self):
        self._sync([_declaration(_spec(), _spec("colour", "Colour"))])
        before = PermissionRegistryRevision.current()
        self._sync([_declaration(_spec())])
        row = FieldDefinition.objects.get(key="testfields.gadget.colour")
        self.assertFalse(row.is_active)
        self.assertEqual(PermissionRegistryRevision.current(), before + 1)
        latest = self._audits().order_by("-created_at").first()
        self.assertEqual(latest.metadata["deactivated"], ["testfields.gadget.colour"])

    def test_a_resource_that_does_not_exist_stops_the_whole_sync(self):
        self._sync([_declaration(_spec(label="Serial"))])
        with self.assertRaises(CommandError) as raised:
            self._sync([
                _declaration(_spec(label="Changed")),
                _declaration(_spec(), resource="missing_resource"),
            ])
        self.assertIn("testfields.missing_resource", str(raised.exception))
        row = FieldDefinition.objects.get(key="testfields.gadget.serial_number")
        self.assertEqual(row.label, "Serial")
        self.assertEqual(FieldDefinition.objects.count(), 1)

    def test_a_module_that_does_not_exist_stops_the_sync(self):
        with self.assertRaises(CommandError):
            self._sync([_declaration(_spec(), module="nomodule")])
        self.assertFalse(FieldDefinition.objects.exists())

    def test_a_blank_scope_is_refused(self):
        with self.assertRaises(CommandError):
            self._sync([_declaration(_spec(scope=""))])
        self.assertFalse(FieldDefinition.objects.exists())

    def test_check_fails_on_drift_and_passes_when_in_sync(self):
        declarations = [_declaration(_spec())]
        with self.assertRaises(CommandError):
            self._sync(declarations, "--check")
        self.assertFalse(FieldDefinition.objects.exists())

        self._sync(declarations)
        output = self._sync(declarations, "--check")
        self.assertIn("in sync", output)

        with self.assertRaises(CommandError) as raised:
            self._sync([_declaration(_spec(label="Renamed"))], "--check")
        self.assertIn("label", str(raised.exception))
        self.assertEqual(
            FieldDefinition.objects.get(key="testfields.gadget.serial_number").label,
            "Serial number",
        )
