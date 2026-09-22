"""Enforcement: what a caller's Field Access does to a response and a write.

The mechanism is tested on serializers and fields this module invents, not on a
domain app's: what is being pinned is the rule, and a rule proved against
vendors would have to be proved again against pupils. The switches themselves
are the evaluator's subject (``test_field_evaluator``); here they are simply
set, and what is tested is what a surface then does with them.
"""
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import serializers
from rest_framework.test import APIRequestFactory

from core.exceptions import custom_exception_handler
from vs_rbac.field_enforcement import (
    SYSTEM_SURFACES,
    FieldAccessMixin,
    FieldWriteDenied,
    assert_system_surface,
    assert_writable,
    can_read,
    field_access_payload,
    system_context,
    visible,
)
from vs_rbac.field_registry import FieldDeclaration, FieldSpec
from vs_rbac.models import FieldDefinition, PermissionScope, RoleFieldAccess
from vs_user.models import User

from .helpers import (
    make_assignment,
    make_branch,
    make_field_definition,
    make_permission,
    make_role,
    make_school,
    make_staff_user,
)

#: Records the test serializers would have written, so a refused write can be
#: shown to have written nothing.
SAVED: list = []


def _fresh(user):
    """Re-read *user* so no answer memoised on the instance is reused."""
    return User.objects.get(pk=user.pk)


class _VendorSerializer(FieldAccessMixin, serializers.Serializer):
    """A vendor as a detail response: it tells a form what to grey."""

    field_resource = "fenf.vendor"
    field_access_detail = True

    name = serializers.CharField(required=False)
    phone = serializers.CharField(required=False, allow_blank=True)
    bank_account_number = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)

    def create(self, validated_data):
        SAVED.append(validated_data)
        return SimpleNamespace(**validated_data)

    def update(self, instance, validated_data):
        SAVED.append(validated_data)
        return instance


class _VendorRowSerializer(_VendorSerializer):
    """The same vendor as a row of a list."""

    field_access_detail = False


class _VendorAliasSerializer(_VendorSerializer):
    """A serializer whose own name for the phone differs from the registry's."""

    field_aliases = {"contact_phone": "phone"}

    contact_phone = serializers.CharField(required=False, allow_blank=True)


class _VendorCreateSerializer(_VendorSerializer):
    """The Add form, which carries the date the vendor was opened."""

    opened_on = serializers.CharField(required=False, allow_blank=True)


def _is_own_record(obj, user) -> bool:
    return getattr(obj, "user_id", None) == user.pk


class _StaffSerializer(FieldAccessMixin, serializers.Serializer):
    """A staff record, which its own subject always reads and writes."""

    field_resource = "fenf.staff"
    field_access_detail = True
    owner_rule = staticmethod(_is_own_record)

    bank_name = serializers.CharField(required=False, allow_blank=True)


class _InvoiceSerializer(FieldAccessMixin, serializers.Serializer):
    """An invoice carrying a vendor, each filtered as its own resource."""

    field_resource = "fenf.invoice"
    field_access_detail = True

    reference = serializers.CharField(required=False)
    secret_margin = serializers.CharField(required=False, allow_blank=True)
    vendor = _VendorSerializer()


class _Enforcement(TestCase):
    """One school, one person, and a resource with a field of every kind.

    The person holds one role. ``name`` has no switch row, so it keeps the
    default of an ordinary field (read and write). ``phone`` and ``opened_on``
    are readable and not writable. ``bank_account_number`` is sensitive with no
    row, so it is closed by default. ``note`` is not registered at all.
    """

    def setUp(self):
        SAVED.clear()
        self.school = make_school(slug="fenf-bright-star", name="Bright Star School")
        self.branch = make_branch(self.school, name="Ikeja Branch")
        self.tenant = self.school.tenant
        self.name = make_field_definition("fenf.vendor.name", "Name")
        self.phone = make_field_definition("fenf.vendor.phone", "Phone")
        self.bank = make_field_definition(
            "fenf.vendor.bank_account_number", "Bank account number", sensitive=True,
        )
        self.opened_on = make_field_definition(
            "fenf.vendor.opened_on", "Opened on", open_on_create=True,
        )
        self.user = make_staff_user(self.branch, email="fenf-user@test.com")
        self.role = make_role(self.tenant, name="Storekeeper", key="fenf_storekeeper")
        make_assignment(self.tenant, self.user, self.role, branch=None)
        self._switch(self.phone, read=True, write=False)
        self._switch(self.opened_on, read=True, write=False)

    def _switch(self, field, *, read, write):
        return RoleFieldAccess.objects.create(
            role=self.role, field=field, can_read=read, can_write=write,
        )

    def _request(self, user=None):
        """A request from *user*, read fresh so no earlier answer is reused."""
        request = APIRequestFactory().get("/")
        request.user = User.objects.get(pk=(user or self.user).pk)
        request.tenant = self.tenant
        return request

    def _context(self, user=None):
        return {"request": self._request(user)}

    def _vendor(self, **overrides):
        values = {
            "name": "Ade Stationers",
            "phone": "08030000000",
            "bank_account_number": "0123456789",
            "note": "Pays on time.",
        }
        values.update(overrides)
        return SimpleNamespace(**values)


class ReadTests(_Enforcement):
    def test_a_hidden_field_is_absent_and_a_readable_one_is_present(self):
        data = _VendorSerializer(self._vendor(), context=self._context()).data
        self.assertNotIn("bank_account_number", data)
        self.assertEqual(data["phone"], "08030000000")
        self.assertEqual(data["name"], "Ade Stationers")

    def test_nothing_names_the_fields_that_were_dropped(self):
        data = _VendorSerializer(self._vendor(), context=self._context()).data
        self.assertNotIn("_stripped_fields", data)
        self.assertNotIn("bank_account_number", data["_read_only_fields"])

    def test_a_detail_response_names_only_present_and_unwritable_fields(self):
        data = _VendorSerializer(self._vendor(), context=self._context()).data
        self.assertEqual(data["_read_only_fields"], ["phone"])

    def test_an_unregistered_field_is_never_touched(self):
        data = _VendorSerializer(self._vendor(), context=self._context()).data
        self.assertEqual(data["note"], "Pays on time.")

    def test_a_list_omits_the_read_only_names(self):
        context = self._context()
        row = _VendorRowSerializer(self._vendor(), context=context).data
        self.assertNotIn("_read_only_fields", row)
        self.assertNotIn("bank_account_number", row)

        rows = _VendorSerializer(
            [self._vendor(), self._vendor()], many=True, context=context,
        ).data
        self.assertEqual([r for r in rows if "_read_only_fields" in r], [])

    def test_a_serializers_own_name_is_translated_to_the_registry_name(self):
        vendor = self._vendor()
        vendor.contact_phone = "08031111111"
        data = _VendorAliasSerializer(vendor, context=self._context()).data
        self.assertEqual(sorted(data["_read_only_fields"]), ["contact_phone", "phone"])


class WriteTests(_Enforcement):
    def _refusal(self, body, instance=None, serializer=_VendorSerializer):
        with self.assertRaises(FieldWriteDenied) as caught:
            serializer(instance, data=body, context=self._context()).is_valid()
        return caught.exception

    def test_an_unwritable_field_is_refused_and_nothing_is_saved(self):
        exc = self._refusal(
            {"name": "Ade Stationers Ltd", "phone": "08039999999"},
            instance=self._vendor(),
        )
        self.assertEqual(exc.fields, ["phone"])
        self.assertEqual(SAVED, [])

    def test_every_refused_field_is_named_at_once(self):
        exc = self._refusal({
            "phone": "08039999999",
            "bank_account_number": "9999999999",
            "name": "Ade Stationers Ltd",
        })
        self.assertEqual(exc.fields, ["bank_account_number", "phone"])

    def test_a_hidden_field_is_refused_exactly_as_a_read_only_one(self):
        hidden = self._refusal({"bank_account_number": "9999999999"})
        read_only = self._refusal({"phone": "08039999999"})
        self.assertEqual(
            list(hidden.extra.values()), list(read_only.extra.values()),
        )

    def test_the_refusal_comes_out_in_the_shared_envelope(self):
        exc = self._refusal({"phone": "08039999999"})
        response = custom_exception_handler(exc, {})
        self.assertEqual(response.status_code, 403)
        self.assertIs(response.data["success"], False)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assertEqual(
            response.data["error"]["detail"],
            {"phone": ["You do not have permission to change this field."]},
        )
        self.assertIn("phone", response.data["message"])

    def test_a_writable_field_alone_passes(self):
        serializer = _VendorSerializer(data={"name": "Ade Stationers Ltd"},
                                       context=self._context())
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {"name": "Ade Stationers Ltd"})


class EchoedValueTests(_Enforcement):
    def test_an_unchanged_value_is_dropped_on_update(self):
        serializer = _VendorSerializer(
            self._vendor(), data={"name": "Ade Stationers", "phone": "08030000000"},
            context=self._context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("phone", serializer.validated_data)

    def test_a_changed_value_is_refused_on_update(self):
        with self.assertRaises(FieldWriteDenied):
            _VendorSerializer(
                self._vendor(), data={"phone": "08039999999"},
                context=self._context(),
            ).is_valid()

    def test_an_empty_value_is_dropped_on_create(self):
        serializer = _VendorSerializer(
            data={"name": "Ade Stationers", "phone": ""}, context=self._context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("phone", serializer.validated_data)

    def test_a_filled_value_is_refused_on_create(self):
        with self.assertRaises(FieldWriteDenied):
            _VendorSerializer(
                data={"phone": "08039999999"}, context=self._context(),
            ).is_valid()

    def test_a_field_open_on_create_is_accepted_while_the_record_is_created(self):
        serializer = _VendorCreateSerializer(
            data={"name": "Ade Stationers", "opened_on": "2026-09-16"},
            context=self._context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["opened_on"], "2026-09-16")

    def test_a_field_open_on_create_is_refused_on_a_later_change(self):
        vendor = self._vendor(opened_on="2026-09-16")
        with self.assertRaises(FieldWriteDenied) as caught:
            _VendorCreateSerializer(
                vendor, data={"opened_on": "2026-01-01"}, context=self._context(),
            ).is_valid()
        self.assertEqual(caught.exception.fields, ["opened_on"])


class OwnerRuleTests(_Enforcement):
    """A person's own record, which their roles never close to them."""

    def setUp(self):
        super().setUp()
        self.bank_name = make_field_definition(
            "fenf.staff.bank_name", "Bank name", sensitive=True,
        )
        self.own = SimpleNamespace(user_id=self.user.pk, bank_name="First Bank")
        self.other = SimpleNamespace(user_id=self.user.pk + 1000, bank_name="GTBank")

    def test_the_owner_reads_their_own_closed_field(self):
        data = _StaffSerializer(self.own, context=self._context()).data
        self.assertEqual(data["bank_name"], "First Bank")
        self.assertEqual(data["_read_only_fields"], [])

    def test_somebody_elses_record_stays_closed(self):
        data = _StaffSerializer(self.other, context=self._context()).data
        self.assertNotIn("bank_name", data)

    def test_the_owner_writes_their_own_closed_field(self):
        serializer = _StaffSerializer(
            self.own, data={"bank_name": "GTBank"}, context=self._context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["bank_name"], "GTBank")

    def test_writing_somebody_elses_record_is_refused(self):
        with self.assertRaises(FieldWriteDenied):
            _StaffSerializer(
                self.other, data={"bank_name": "GTBank"}, context=self._context(),
            ).is_valid()


class NestedSerializerTests(_Enforcement):
    def setUp(self):
        super().setUp()
        make_field_definition("fenf.invoice.reference", "Reference")
        make_field_definition(
            "fenf.invoice.secret_margin", "Margin", sensitive=True,
        )

    def test_a_nested_serializer_is_filtered_as_its_own_resource(self):
        invoice = SimpleNamespace(
            reference="INV-001", secret_margin="12%", vendor=self._vendor(),
        )
        data = _InvoiceSerializer(invoice, context=self._context()).data
        self.assertEqual(data["reference"], "INV-001")
        self.assertNotIn("secret_margin", data)
        self.assertNotIn("bank_account_number", data["vendor"])
        self.assertEqual(data["vendor"]["phone"], "08030000000")


class ContextTests(_Enforcement):
    """The two renders that pass everything, and nothing else."""

    def test_no_request_context_passes_every_field(self):
        data = _VendorSerializer(self._vendor()).data
        self.assertEqual(data["bank_account_number"], "0123456789")
        self.assertNotIn("_read_only_fields", data)

        serializer = _VendorSerializer(data={"phone": "08039999999"})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_a_declared_system_surface_passes_every_field(self):
        surface = "vs_rbac.tests.test_field_enforcement.invoice_document"
        with mock.patch.dict(
            SYSTEM_SURFACES, {surface: "Addressed to the payer, not to a person here."},
        ):
            context = system_context(surface)
            context["request"] = self._request()
            data = _VendorSerializer(self._vendor(), context=context).data
        self.assertEqual(data["bank_account_number"], "0123456789")

    def test_an_undeclared_system_surface_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            system_context("vs_rbac.tests.test_field_enforcement.undeclared")


class RawSurfaceTests(_Enforcement):
    """The helpers for a view that builds its own response or applies its own body."""

    def test_visible_drops_the_keys_the_caller_cannot_read(self):
        row = {
            "name": "Ade Stationers",
            "phone": "08030000000",
            "bank_account_number": "0123456789",
            "note": "Pays on time.",
        }
        shown = visible(self._request(), "fenf.vendor", row)
        self.assertEqual(
            shown,
            {"name": "Ade Stationers", "phone": "08030000000", "note": "Pays on time."},
        )
        self.assertIn("bank_account_number", row)

    def test_assert_writable_refuses_the_keys_the_caller_cannot_write(self):
        with self.assertRaises(FieldWriteDenied) as caught:
            assert_writable(
                self._request(), "fenf.vendor",
                {"name": "Ade Stationers Ltd", "phone": "08039999999"},
            )
        self.assertEqual(caught.exception.fields, ["phone"])

    def test_assert_writable_passes_a_body_of_writable_keys(self):
        self.assertIsNone(
            assert_writable(
                self._request(), "fenf.vendor",
                {"name": "Ade Stationers Ltd", "note": "Pays on time."},
            )
        )

    def test_assert_writable_allows_an_open_on_create_field_while_creating(self):
        body = {"opened_on": "2026-09-16"}
        self.assertIsNone(
            assert_writable(self._request(), "fenf.vendor", body, creating=True)
        )
        with self.assertRaises(FieldWriteDenied):
            assert_writable(self._request(), "fenf.vendor", body)


class CostTests(_Enforcement):
    """Two hundred rows cost what one row costs: the answer is evaluated once."""

    def _queries(self, count):
        context = self._context()
        rows = [self._vendor() for _ in range(count)]
        with CaptureQueriesContext(connection) as captured:
            data = _VendorSerializer(rows, many=True, context=context).data
            self.assertEqual(len(data), count)
        return len(captured.captured_queries)

    def test_serializing_many_rows_evaluates_once(self):
        # The first render settles anything read once per process.
        self._queries(1)
        one = self._queries(1)
        many = self._queries(200)
        self.assertEqual(one, many)
        self.assertLessEqual(many, 5, "Field Access should cost a handful of queries.")


class SyncOpenOnCreateTests(TestCase):
    """``sync_field_registry`` carries the flag, and settles after one run."""

    SYNC = "vs_rbac.management.commands.sync_field_registry.all_declarations"

    def setUp(self):
        make_permission("fenf.gadget.view")
        self.declarations = [FieldDeclaration(
            module="fenf", resource="gadget", surfaces=(),
            fields=(
                FieldSpec("serial_number", "Serial number", scope=PermissionScope.TENANT),
                FieldSpec("issued_on", "Issued on", scope=PermissionScope.TENANT,
                          open_on_create=True),
            ),
        )]

    def _sync(self, *args):
        out = StringIO()
        with mock.patch(self.SYNC, return_value=self.declarations):
            call_command("sync_field_registry", *args, stdout=out)
        return out.getvalue()

    def test_the_flag_is_written_per_field(self):
        self._sync()
        self.assertTrue(
            FieldDefinition.objects.get(key="fenf.gadget.issued_on").open_on_create,
        )
        self.assertFalse(
            FieldDefinition.objects.get(key="fenf.gadget.serial_number").open_on_create,
        )

    def test_a_second_run_finds_nothing_to_change(self):
        self._sync()
        self.assertIn("unchanged", self._sync())
        self.assertIn("in sync", self._sync("--check"))


class TheMapAUserIsHandedTests(_Enforcement):
    """What ``/me`` and the login response carry, and what it leaves out.

    The map exists for the screens with no record to ask: an Add form and a
    list of columns. It is built from roles alone, so a record's own
    ``_read_only_fields`` wins wherever the two could differ.
    """

    def test_it_names_the_hidden_and_the_read_only_and_nothing_else(self):
        payload = field_access_payload(self.user, self.tenant)
        self.assertEqual(payload, {
            "fenf.vendor": {
                "hidden": ["bank_account_number"],
                "read_only": ["opened_on", "phone"],
                "open_on_create": ["opened_on"],
            },
        })

    def test_a_resource_with_nothing_to_say_is_absent(self):
        """Absent means full, so a client never has to tell one from the other."""
        make_field_definition("fenf.crate.weight", "Weight")
        payload = field_access_payload(self.user, self.tenant)
        self.assertNotIn("fenf.crate", payload)

    def test_a_user_with_nothing_restricted_gets_an_empty_map(self):
        for field in (self.phone, self.opened_on, self.bank):
            RoleFieldAccess.objects.update_or_create(
                role=self.role, field=field,
                defaults={"can_read": True, "can_write": True},
            )
        self.assertEqual(field_access_payload(_fresh(self.user), self.tenant), {})

    def test_it_agrees_with_the_evaluator_field_by_field(self):
        from vs_rbac.field_evaluator import get_field_access

        access = get_field_access(_fresh(self.user), tenant=self.tenant)
        payload = field_access_payload(_fresh(self.user), self.tenant)
        for field in FieldDefinition.objects.filter(is_active=True):
            resource = payload.get(
                f"{field.resource.module_id}.{field.resource.name}", {},
            )
            with self.subTest(field=field.key):
                self.assertEqual(
                    field.name in resource.get("hidden", []),
                    not access.can_read(field.key),
                )
                self.assertEqual(
                    field.name in resource.get("read_only", []),
                    access.can_read(field.key) and not access.can_write(field.key),
                )

    def test_a_field_the_school_may_not_hold_is_left_out_rather_than_hidden(self):
        """A school has no screen a CodeX payroll account could appear on.

        Listing it as hidden would tell Bright Star's administrator the shape
        of a record they will never meet, which is precisely what the access
        catalogue refuses to do for the same field.
        """
        make_field_definition(
            "fenf.cx_payroll.account_number", "Account number",
            sensitive=True, scope=PermissionScope.PLATFORM,
        )
        payload = field_access_payload(_fresh(self.user), self.tenant)
        self.assertNotIn("fenf.cx_payroll", payload)

    def test_it_carries_every_client_name_of_one_field(self):
        """A field reaching clients under two names is restricted under both."""
        alias = make_field_definition(
            "fenf.vendor.invited_by", "Invited by", sensitive=True,
            api_names=["invited_by_id", "invited_by_name"],
        )
        payload = field_access_payload(_fresh(self.user), self.tenant)
        self.assertEqual(
            payload["fenf.vendor"]["hidden"],
            ["bank_account_number", "invited_by_id", "invited_by_name"],
        )
        self.assertTrue(alias.sensitive)


class HiddenButOpenOnCreateTests(_Enforcement):
    """A field the caller cannot read, but may set while creating the record.

    Bright Star's storekeeper has the date a vendor was opened switched off
    entirely: she never sees it on an existing vendor. The date is declared
    open on create, so she still types it when she adds Ade Stationers, and
    only correcting it later needs the switch.
    """

    def setUp(self):
        super().setUp()
        RoleFieldAccess.objects.filter(role=self.role, field=self.opened_on).update(
            can_read=False, can_write=False,
        )

    def test_the_map_lists_it_as_hidden_and_as_open_on_create(self):
        payload = field_access_payload(_fresh(self.user), self.tenant)
        self.assertEqual(payload, {
            "fenf.vendor": {
                "hidden": ["bank_account_number", "opened_on"],
                "read_only": ["phone"],
                "open_on_create": ["opened_on"],
            },
        })

    def test_every_open_on_create_name_is_also_hidden_or_read_only(self):
        """The Add form's list never stands in for what an existing record shows."""
        for entry in field_access_payload(_fresh(self.user), self.tenant).values():
            with self.subTest(entry=entry):
                self.assertEqual(
                    set(entry["open_on_create"]) - set(entry["hidden"]) - set(entry["read_only"]),
                    set(),
                )

    def test_the_serializer_accepts_it_while_creating(self):
        serializer = _VendorCreateSerializer(
            data={"name": "Ade Stationers", "opened_on": "2026-09-16"},
            context=self._context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.assertEqual(SAVED, [{"name": "Ade Stationers", "opened_on": "2026-09-16"}])

    def test_the_serializer_refuses_it_on_an_existing_record(self):
        with self.assertRaises(FieldWriteDenied) as caught:
            _VendorCreateSerializer(
                self._vendor(opened_on="2026-09-16"),
                data={"opened_on": "2026-09-16"}, context=self._context(),
            ).is_valid()
        self.assertEqual(caught.exception.fields, ["opened_on"])
        self.assertEqual(SAVED, [])

    def test_the_serializer_leaves_it_out_of_the_record_it_renders(self):
        data = _VendorCreateSerializer(
            self._vendor(opened_on="2026-09-16"), context=self._context(),
        ).data
        self.assertNotIn("opened_on", data)

    def test_a_raw_create_path_accepts_it_and_a_raw_update_refuses_it(self):
        body = {"opened_on": "2026-09-16"}
        self.assertIsNone(
            assert_writable(self._request(), "fenf.vendor", body, creating=True)
        )
        with self.assertRaises(FieldWriteDenied):
            assert_writable(self._request(), "fenf.vendor", body)


class SystemSurfaceTests(TestCase):
    """A render that runs for nobody says so, once, in one list."""

    def test_an_undeclared_surface_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            assert_system_surface("vs_finance.documents._not_declared")

    def test_every_declared_surface_names_real_code_and_gives_a_reason(self):
        for surface, reason in SYSTEM_SURFACES.items():
            with self.subTest(surface=surface):
                module_path, name = surface.rsplit(".", 1)
                module = import_module(module_path)
                self.assertTrue(hasattr(module, name))
                self.assertGreater(len(reason.split()), 10, "Name the reason.")

    def test_the_invoice_pay_to_block_is_the_declared_exception(self):
        """The one render addressed to somebody outside the school.

        An invoice shows the school's own collection account to the parent who
        has to pay into it. Filtering it by whichever bursar pressed Send would
        produce a different document each time, and one with no account number
        could not be paid at all.
        """
        self.assertIn("vs_finance.documents._issuer_block", SYSTEM_SURFACES)
        assert_system_surface("vs_finance.documents._issuer_block")


class ReadingOneKeyTests(_Enforcement):
    """``can_read`` for a block a view includes or omits whole."""

    def test_it_answers_for_a_registered_key(self):
        request = self._request()
        self.assertFalse(can_read(request, "fenf.vendor.bank_account_number"))
        self.assertTrue(can_read(request, "fenf.vendor.phone"))

    def test_an_unregistered_key_reads_true(self):
        self.assertTrue(can_read(self._request(), "fenf.vendor.nothing_like_it"))

    def test_no_request_reads_true(self):
        self.assertTrue(can_read(None, "fenf.vendor.bank_account_number"))
