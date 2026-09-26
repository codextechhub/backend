"""The field registry agrees with the code that enforces it.

Field Access replaced per-serializer permission maps with a registry an
administrator can see. That only works if the registry names exactly what the
code enforces: a field the conversion turns into a switch with no declaration
behind it is a field nobody can open, and a declared field no serializer emits
is a switch that changes nothing.

What each field used to be guarded by is no longer readable off a serializer,
so these tests read it from :mod:`vs_rbac.field_conversion`, the written
record of which key decided which field before the switches took over. The
conversion is the reason a field's ``sensitive`` flag has to match it exactly:
sensitive means closed by default, so a field that nothing used to hide must
not be sensitive, and one that a key did hide must be.

They lock the registry to the code in both directions, and pin the behaviour
of ``sync_field_registry``, which writes it to the database.
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
from vs_rbac.field_conversion import READ, WRITE, CONVERSIONS
from vs_rbac.field_enforcement import FieldAccessMixin
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
    ("school", "guardians"),
    ("school", "students"),
    ("school", "teachers"),
}

#: Resources whose fields are declared open, and the key that guards the
#: endpoints those fields reach clients through.
#:
#: Everybody who may open the record reads them, so the resource's own view key
#: is what decides their scope, while a switch narrows it per role. A guardian
#: is read with the student key because the guardian endpoints carry it.
ENDPOINT_GUARDED_RESOURCES = {
    ("school", "guardians"): "school.students.view",
    ("school", "teachers"): "school.teachers.view",
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
    ("schools.vs_students.serializers.EnrolmentWriteSerializer", "phone"):
        "The pupil's own number on the enrol form, not a guardian's; the guardians on "
        "that form arrive through the nested GuardianWriteSerializer, a declared "
        "surface of school.guardians.",
    ("schools.vs_students.serializers.EnrolmentWriteSerializer", "email"):
        "The pupil's own address on the enrol form, not a guardian's; see the phone "
        "entry above.",
    ("schools.vs_students.serializers.EnrolmentWriteSerializer", "address"):
        "The pupil's own address on the enrol form, not a guardian's; see the phone "
        "entry above.",
    ("schools.vs_staff.serializers.EmailChangeSerializer", "email"):
        "Behind school.administrators.update on an endpoint of its own, the key that "
        "changes an account's sign-in address; the staff edit form cannot.",
    **{
        ("schools.vs_students.serializers.EnrolmentWriteSerializer", name):
            "The pupil's own name on the enrol form, a school.students field; the "
            "guardians' names arrive through the nested GuardianWriteSerializer."
        for name in ("first_name", "middle_name", "last_name")
    },
    **{
        (f"schools.vs_students.serializers.{form}", name):
            "The guardian's own detail, a school.guardians field on a declared "
            "surface of that resource; a pupil's field of the same name is not here."
        for form in ("GuardianUpdateSerializer", "GuardianWriteSerializer")
        for name in ("first_name", "middle_name", "last_name", "phone", "email", "address")
    },
    ("schools.vs_students.serializers.ConfirmSerializer", "student_number"):
        "Issuing the admission number when an applicant is admitted, which is part of "
        "creating the pupil on the roll; student_number is open on create.",
    ("vs_user.serializers.UserUpdateSerializer", "first_name"):
        "Asks platform.staff_profile's Write switch in validate() for a CX staff "
        "member's account; a school's own accounts are not governed by that switch.",
    ("vs_user.serializers.UserUpdateSerializer", "last_name"):
        "See the first_name entry above.",
    **{
        ("vs_user.serializers.UserCreateSerializer", name):
            "Creating an account, where every value is set once; the profile's switches "
            "govern later corrections on the profile and user edit routes."
        for name in (
            "first_name", "last_name", "job_title", "employee_id", "employment_type",
            "date_joined", "date_of_birth", "marital_status", "nationality",
            "state_of_origin",
        )
    },
}


#: Declared surfaces that enforce nothing themselves, and why that is right.
#:
#: A surface normally carries :class:`FieldAccessMixin`, because a declared
#: field that no surface enforces is a switch an administrator can turn off
#: while every screen keeps showing the value. The one exception is a
#: serializer that carries the fields only by nesting the serializer that does
#: enforce them.
NESTING_ONLY_SURFACES = {
    "schools.vs_students.serializers.GuardianLinkSerializer":
        "It carries a guardian's details only by nesting GuardianSerializer, "
        "which DRF hands the root context and which is filtered as its own "
        "resource. The link's own fields are the relationship and the primary "
        "flag, and neither is registered.",
}


def _converted(access):
    """Every registry key a permission key gated *access* on before conversion.

    :data:`vs_rbac.field_conversion.CONVERSIONS` is the written record of what
    each key decided in the code it replaced, checked field by field when it
    was written. It is what these tests compare the registry against now that
    no serializer carries a key map: a field that was read-guarded must still
    be declared sensitive, and one the conversion never touched must not be.
    """
    return {
        key
        for conversion in CONVERSIONS
        if access in conversion.gates
        for key in conversion.fields
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


def _all_enforcing_serializers():
    """Every production serializer that enforces Field Access."""
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

    for cls in set(walk(FieldAccessMixin)):
        parts = cls.__module__.split(".")
        if any(part == "tests" or part.startswith("test") for part in parts):
            continue
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

    def test_every_converted_field_is_registered(self):
        """A converted field the registry forgot would be closed to everybody.

        The conversion writes a switch per field named in ``CONVERSIONS``. A
        name there with no declaration behind it would produce a switch on a
        field nothing enforces, and a role that lost the key would simply lose
        the data.
        """
        registered = {
            declaration.key_for(spec)
            for declaration in all_declarations()
            for spec in declaration.fields
        }
        converted = _converted(READ) | _converted(WRITE)
        self.assertEqual(converted - registered, set())

    def test_the_vendor_views_hand_written_guard_is_registered(self):
        vendor = get_declaration("procurement", "vendor")
        self.assertEqual(_vendor_view_fields() - _api_names(vendor), set())

    def test_every_enforcing_serializer_is_a_declared_surface(self):
        """A surface enforcing a resource is one the declaration names.

        Both directions matter. A serializer enforcing ``school.students``
        that the declaration does not list is a surface nobody checked the
        names of, and a declaration listing a serializer that enforces some
        other resource has been pointed at the wrong code.
        """
        by_path = {
            path: (d.module, d.resource)
            for d in all_declarations() for path in d.surfaces
        }
        for cls in _all_enforcing_serializers():
            path = f"{cls.__module__}.{cls.__qualname__}"
            with self.subTest(serializer=path):
                self.assertIn(path, by_path)
                module, resource = by_path[path]
                self.assertEqual(cls.field_resource, f"{module}.{resource}")

    def test_every_declared_surface_enforces_the_resource_that_names_it(self):
        """A switch that no surface reads would change nothing on any screen.

        This is the direction that catches the gap rather than the mistake: a
        resource can be declared, synced, shown in the catalogue and switched
        off by an administrator, and every screen keep printing the value,
        because nobody put the mixin on the serializer.
        """
        for declaration in all_declarations():
            resource = f"{declaration.module}.{declaration.resource}"
            for path in declaration.surfaces:
                with self.subTest(surface=path):
                    if path in NESTING_ONLY_SURFACES:
                        continue
                    cls = _surface(path)
                    self.assertTrue(
                        issubclass(cls, FieldAccessMixin),
                        f"{path} is a declared surface of {resource} and "
                        f"enforces nothing.",
                    )
                    self.assertEqual(cls.field_resource, resource)

    def test_every_alias_names_a_registered_field(self):
        """An alias pointing at nothing would silently enforce nothing."""
        declared = {
            (d.module, d.resource): _api_names(d) for d in all_declarations()
        }
        for cls in _all_enforcing_serializers():
            module, _, resource = cls.field_resource.partition(".")
            for own_name, registry_name in (cls.field_aliases or {}).items():
                with self.subTest(serializer=cls.__qualname__, alias=own_name):
                    self.assertIn(registry_name, declared[(module, resource)])

    def test_sensitive_means_the_old_key_hid_it(self):
        """Read-guarded fields are sensitive; write-only guarded ones are not.

        The default a field starts from is the whole of what release day does
        to a role that holds no switch row: a sensitive field starts closed,
        so declaring one sensitive that nothing hid would take it away from
        everybody, and declaring one open that a key hid would hand it to
        everybody.
        """
        hidden_before = _converted(READ)
        for declaration in all_declarations():
            for spec in declaration.fields:
                key = declaration.key_for(spec)
                with self.subTest(key=key):
                    self.assertEqual(spec.sensitive, key in hidden_before)

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
    """A second route into a registered field cannot arrive unenforced.

    Enforcement binds only the serializer that declares the resource, so a
    second serializer writing the same field enforces nothing unless somebody
    declares it too. For each declaration, every serializer in the app owning
    its surfaces is scanned: a ``ModelSerializer`` when its model is a
    surface's model, a plain ``Serializer`` by field name. A non-read-only
    field named like a registered writable field must belong to a declared
    surface, or sit on the allowlist with a one-line reason.

    This is the check that finds the shape the enrolment form had: a medical
    value accepted on the way in by a serializer nobody had given the rule to,
    while the edit form refused the same value.
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
                    for name, field in cls(context={}).fields.items():
                        if field.read_only or name not in writable:
                            continue
                        seen.add((path, name))
                        if (path, name) in WRITE_PATH_ALLOWLIST:
                            continue
                        with self.subTest(serializer=path, field=name):
                            self.assertIn(
                                path, declaration.surfaces,
                                f"{path} can write '{name}' and is not a "
                                f"declared surface of "
                                f"{declaration.module}.{declaration.resource}.",
                            )
        self.assertEqual(set(WRITE_PATH_ALLOWLIST) - seen, set())


#: Where an app keeps its deep payload case, relative to the app package.
DEEP_PAYLOAD_MODULES = ("tests_field_access_deep_payload", "tests.test_field_access_deep_payload")


def _deep_payload_cases():
    """Every deep payload case an installed app provides.

    Found by importing each app's case module by convention and walking the
    subclasses of :class:`vs_rbac.tests.deep_payload.DeepPayloadChecks`, so
    this engine's tests name no domain app. The classes are never bound into
    this module, so the runner collects them where they are defined and not
    a second time here.
    """
    from .deep_payload import DeepPayloadChecks

    for app in django_apps.get_app_configs():
        for suffix in DEEP_PAYLOAD_MODULES:
            name = f"{app.name}.{suffix}"
            try:
                importlib.import_module(name)
            except ModuleNotFoundError as exc:
                if exc.name not in (name, name.rsplit(".", 1)[0]):
                    raise

    def walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)

    return [cls for cls in walk(DeepPayloadChecks) if cls.covers]


class EveryDeclaredSurfaceIsRenderedDeepTests(SimpleTestCase):
    """Every declared surface is rendered by some app's deep payload case.

    The deep payload check renders a surface as a caller who may read none of
    its resource's fields and fails on a registered name at any depth, which
    is the only way to catch a field travelling inside a method field or a
    merged dict. It proves nothing about a surface nobody renders, so a
    surface declared without a case fails here.
    """

    def test_the_cases_cover_every_declared_surface_and_nothing_else(self):
        declared = {
            path: f"{d.module}.{d.resource}"
            for d in all_declarations() for path in d.surfaces
        }
        covered = {}
        for case in _deep_payload_cases():
            for path in case.covers:
                with self.subTest(case=case.__qualname__, surface=path):
                    self.assertIn(path, declared, "A case renders an undeclared surface.")
                    self.assertIn(declared.get(path), case.resources)
                    self.assertNotIn(path, covered, "Two cases render the same surface.")
                covered[path] = case
        self.assertEqual(set(declared) - set(covered), set())


#: Serializers that emit a registered name without enforcing it, each with the
#: reason that is safe. Every entry must still match a real hit.
READ_PATH_ALLOWLIST = {
    ("vs_user.serializers.UserInlineSerializer", "first_name"):
        "A person named beside something else (a line manager, an actor on a log); "
        "the name switch covers the CX staff profile, not every mention of a person.",
    ("vs_user.serializers.UserInlineSerializer", "last_name"):
        "See the first_name entry above.",
    ("vs_user.serializers.UserReadSerializer", "first_name"):
        "The account as Team management reads it, a surface of platform.team; the "
        "name switch covers the HR profile.",
    ("vs_user.serializers.UserReadSerializer", "last_name"):
        "See the first_name entry above.",
    ("vs_user.serializers.UserUpdateSerializer", "first_name"):
        "The response to the edit that the same serializer checked the switch for.",
    ("vs_user.serializers.UserUpdateSerializer", "last_name"):
        "See the first_name entry above.",
    ("vs_user.serializers.ActivationPreviewSerializer", "first_name"):
        "The invitee's own name, shown to them on their own activation page.",
    ("vs_user.serializers.ActivationPreviewSerializer", "last_name"):
        "See the first_name entry above.",
    ("vs_import_data.serializers.ImportTemplateCreateSerializer", "validation_rules"):
        "The response to creating a template, behind import.templates.create, a "
        "platform-only key; it returns the rules the caller has just written.",
    ("vs_import_data.serializers.ImportTemplateUpdateSerializer", "validation_rules"):
        "The response to correcting a template, behind import.templates.manage, the "
        "platform-only key whose holders write the rules on the same endpoint.",
}


class EveryReadPathIsKnownTests(SimpleTestCase):
    """A second serializer cannot send a registered field unfiltered.

    The read-side twin of :class:`EveryWritePathIsKnownTests`. Enforcement
    binds only the serializers that carry the mixin, so a list row or a lighter
    shape of the same model that emits a registered name hands it to every
    caller whatever the switch says: a school hiding when a pupil joined would
    still print the date down the directory. Every serializer in the app that
    owns a declaration, on a surface's model, that emits a registered name must
    be a declared surface or sit on the allowlist with a one-line reason.
    """

    def test_every_serializer_that_emits_a_registered_field_is_known(self):
        seen = set()
        for declaration in all_declarations():
            names = _api_names(declaration)
            surfaces = [_surface(path) for path in declaration.surfaces]
            models = {
                getattr(getattr(cls, "Meta", None), "model", None) for cls in surfaces
            } - {None}
            owners = {
                django_apps.get_containing_app_config(path.rsplit(".", 1)[0])
                for path in declaration.surfaces
            }
            for app in owners:
                for cls in _serializers_owned_by(app):
                    if getattr(getattr(cls, "Meta", None), "model", None) not in models:
                        continue
                    path = f"{cls.__module__}.{cls.__qualname__}"
                    if path in declaration.surfaces:
                        continue
                    for name, field in cls(context={}).fields.items():
                        if field.write_only or name not in names:
                            continue
                        seen.add((path, name))
                        if (path, name) in READ_PATH_ALLOWLIST:
                            continue
                        with self.subTest(serializer=path, field=name):
                            self.fail(
                                f"{path} sends '{name}' and is not a declared "
                                f"surface of {declaration.module}.{declaration.resource}."
                            )
        self.assertEqual(set(READ_PATH_ALLOWLIST) - seen, set())


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
    """Each field is reachable by a key of its own scope.

    A field is not holdable on its own: it reaches a client through endpoints,
    and those endpoints ask for keys registered on the field's own resource.
    So a field's scope has to be one some key on that resource carries. A
    ``PLATFORM`` field under a resource only tenants hold is a switch a school
    is offered on the Field Access screen and can never see the effect of; a
    ``TENANT`` field under a resource only CodeX holds is the reverse.

    A resource may carry both scopes, and ``import.templates`` does: reading
    the template list is a school's, editing the validation rules behind it is
    CodeX's, and the rules are a ``PLATFORM`` field for that reason. Matching
    the scope against the whole resource rather than one key is what keeps such
    a resource expressible.
    """

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
        """``(field_key, {scope: [keys]})`` for every registered field.

        The keys registered on the field's own resource, grouped by the scope
        each carries. A resource that has fields and no keys of its own falls
        back to :data:`ENDPOINT_GUARDED_RESOURCES`, which names the key whose
        endpoints reach them.
        """
        scopes = dict(Permission.objects.values_list("key", "scope"))
        by_resource: dict[tuple[str, str], list[str]] = {}
        for key, module, resource in Permission.objects.values_list(
            "key", "resource__module_id", "resource__name",
        ):
            by_resource.setdefault((module, resource), []).append(key)

        for declaration in all_declarations():
            pair = (declaration.module, declaration.resource)
            keys = by_resource.get(pair) or [ENDPOINT_GUARDED_RESOURCES[pair]]
            reachable: dict[str, list[str]] = {}
            for key in sorted(keys):
                reachable.setdefault(scopes[key], []).append(key)
            for spec in declaration.fields:
                yield declaration.key_for(spec), reachable

    def test_the_real_registry_syncs_against_the_seeded_tree(self):
        declared = sum(len(d.fields) for d in all_declarations())
        self.assertEqual(FieldDefinition.objects.filter(is_active=True).count(), declared)

    def test_a_fields_only_resource_carries_its_fields_and_no_permission(self):
        """The seed mints the resource; the sync fills it; no key is created."""
        rows = FieldDefinition.objects.filter(
            resource__module_id="school", resource__name="guardians", is_active=True,
        )
        self.assertEqual(
            {row.name for row in rows},
            {spec.name for spec in get_declaration("school", "guardians").fields},
        )
        self.assertFalse(
            Permission.objects.filter(
                resource__module_id="school", resource__name="guardians",
            ).exists(),
        )

    def test_a_field_nothing_withholds_today_starts_open(self):
        """Release day changes nobody's access (design decision D9).

        Guardian contact details and staff personal details are readable by
        everybody who may open those records. Declared sensitive, they would be
        hidden from every role the moment enforcement ships, which is the one
        outcome the conversion is not allowed to produce. A field nothing can
        write (a staff member's exit date, set only by the status change that
        ends employment) starts with Write off, which takes nothing from anybody.
        """
        for resource in ("guardians", "teachers"):
            for row in FieldDefinition.objects.filter(
                resource__module_id="school", resource__name=resource, is_active=True,
            ):
                with self.subTest(field=row.key):
                    self.assertFalse(row.sensitive)
                    self.assertEqual(
                        row.default_access, {"read": True, "write": row.writable},
                    )

    def test_every_field_scope_equals_its_guarding_keys_scope(self):
        rows = {row.key: row for row in FieldDefinition.objects.select_related("resource")}
        guarded = set()
        for field_key, reachable in self._guards():
            with self.subTest(field=field_key):
                self.assertIn(field_key, rows)
                self.assertIn(
                    rows[field_key].scope, reachable,
                    f"{field_key} is {rows[field_key].scope} and no key on its "
                    f"resource is: {reachable}",
                )
                guarded.add(field_key)
        self.assertEqual(guarded, set(rows))


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
