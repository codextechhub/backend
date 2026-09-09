"""The price list, seeded onto the permission rows the gate reads.

``Permission.capability`` is what turns the plan gate from a mechanism into a
product decision. These tests run the real seed chain - the capability
catalogue, every permission seeder, then the banding - and check the result the
way the gate will read it.

Three things are worth more than the rest, and they are the ones a mistake here
would quietly break:

* a key that resolves to no band stays NULL, because absence means core and a
  wrong band takes a working route away from a paying school;
* the nine never-band keys never acquire one, because a school must not be able
  to buy its way into a child's medical record;
* every band a key points at is a *band*, never a module, because only a band
  carries the depth that is being sold.
"""
from django.core.management import call_command
from django.test import TestCase

from vs_config.models import Capability

from ..models import Permission
from ..permission_bands import (
    NEVER_BAND,
    band_for,
    capability_key_for,
)


class _Seeded(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_actions", verbosity=0)
        call_command("seed_config_catalogue", verbosity=0)
        call_command("seed_school_permissions", verbosity=0)
        call_command("seed_finance_permissions", verbosity=0)
        call_command("seed_procurement_permissions", verbosity=0)
        call_command("seed_payments_permissions", verbosity=0)
        call_command("seed_exports_permissions", verbosity=0)
        call_command("seed_config_permissions", verbosity=0)
        call_command("seed_import_permissions", verbosity=0)
        call_command("seed_ticket_permissions", verbosity=0)
        call_command("seed_workflow_permissions", verbosity=0)
        call_command("seed_permission_bands", verbosity=0)


class BandedPermissionsTests(_Seeded):
    """What the gate will read off the permission rows."""

    def test_every_banded_key_points_at_a_band_not_a_module(self):
        # A module key would say only that the school holds Finance, which
        # every school does. The depth is the whole of what is sold.
        banded = Permission.objects.filter(
            capability__isnull=False
        ).select_related("capability")
        self.assertGreater(banded.count(), 200)
        for permission in banded:
            with self.subTest(key=permission.key):
                self.assertIsNotNone(
                    permission.capability.parent_id,
                    f"{permission.key} points at the module "
                    f"{permission.capability.key} rather than one of its bands",
                )

    def test_a_core_key_of_a_sold_module_points_at_its_core_band(self):
        invoice = Permission.objects.get(key="finance.invoice.create")
        self.assertEqual(invoice.capability.key, "finance_core")

    def test_a_plus_key_points_at_the_plus_band(self):
        generate = Permission.objects.get(key="finance.feestructure.generate")
        self.assertEqual(generate.capability.key, "finance_plus")

    def test_an_advanced_key_points_at_the_advanced_band(self):
        payroll = Permission.objects.get(key="finance.payrollrun.create")
        self.assertEqual(payroll.capability.key, "finance_advanced")

    def test_one_action_can_sit_apart_from_its_resource(self):
        # Opening a period is everyday bookkeeping; closing it is the tail.
        self.assertEqual(
            Permission.objects.get(key="finance.period.create").capability.key,
            "finance_core",
        )
        self.assertEqual(
            Permission.objects.get(key="finance.period.close").capability.key,
            "finance_advanced",
        )

    def test_bulk_import_is_one_feature_wherever_it_appears(self):
        # A bursar who imported students in January is not refused a vendor
        # list in March, so both answer to the same platform-wide band.
        students = Permission.objects.get(key="school.students.import")
        batches = Permission.objects.get(key="import.batches.create")
        self.assertEqual(students.capability.key, "bulk_import")
        self.assertEqual(batches.capability.key, "bulk_import")

    def test_bulk_import_is_on_every_plan(self):
        """Loading a roll from a file is how a school arrives, not a upsell.

        A new school on the shallowest plan has four hundred students in a
        spreadsheet and no other way in, and the step sits on its onboarding
        checklist. Priced above them, the cheapest plan became the hardest one
        to start on. Pinned here so moving it back up is a deliberate act.
        """
        from vs_config.models import Capability

        band = Capability.objects.get(key="bulk_import")
        self.assertEqual(band.depth, Capability.Depth.CORE)

    def test_data_export_is_on_every_plan(self):
        """A school's records are its own, whatever it pays.

        The mirror of bulk import: getting data in and getting it out are the
        same promise read in two directions, and a school that cannot export
        is a school that cannot leave. Pinned so moving it up is deliberate.
        """
        from vs_config.models import Capability

        band = Capability.objects.get(key="data_export")
        self.assertEqual(band.depth, Capability.Depth.CORE)

    def test_a_verb_that_only_looks_like_the_export_product_is_left_alone(self):
        # ``config.audit.export`` ends in the same word and is a different
        # thing: reading your own configuration history is not data export.
        config_export = Permission.objects.get(key="config.audit.export")
        self.assertIsNone(config_export.capability_id)

    def test_the_split_keys_landed_at_their_own_depths(self):
        pairs = {
            "academics.exam.view": "calendar_advanced",
            "academics.timetable.view": "calendar_plus",
            "school.staff_records.view": "teachers_plus",
            "school.teachers.view": "teachers_core",
            "school.students.promote": "students_plus",
            "school.students.manage": "students_core",
            "procurement.analytics.view": "procurement_advanced",
            "procurement.report.view": "procurement_plus",
        }
        for key, expected in pairs.items():
            with self.subTest(key=key):
                self.assertEqual(
                    Permission.objects.get(key=key).capability.key, expected,
                )


class KeysThatMustStayUnbandedTests(_Seeded):
    """Absence is a decision, and two kinds of key depend on it."""

    def test_the_never_band_keys_carry_no_capability(self):
        for module, resource, action in NEVER_BAND:
            key = f"{module}.{resource}.{action}"
            with self.subTest(key=key):
                permission = Permission.objects.filter(key=key).first()
                if permission is None:
                    continue
                self.assertIsNone(
                    permission.capability_id,
                    f"{key} is on the never-band list and was banded anyway",
                )

    def test_a_platform_key_is_never_banded(self):
        for permission in Permission.objects.filter(module_id="platform"):
            with self.subTest(key=permission.key):
                self.assertIsNone(permission.capability_id)

    def test_the_shared_services_stay_core_for_everybody(self):
        # A school that cannot raise a support ticket or approve a requisition
        # does not have a working product at any price.
        for key in (
            "tickets.ticket.view",
            "workflow.template.view",
            "school.branches.view",
            "academics.session.view",
        ):
            with self.subTest(key=key):
                permission = Permission.objects.filter(key=key).first()
                if permission is None:
                    continue
                self.assertIsNone(permission.capability_id)


class ResolverTests(TestCase):
    """The mapping itself, without the database."""

    def test_a_never_band_key_resolves_to_no_capability(self):
        self.assertIsNone(
            capability_key_for("school", "students", "view_sensitive")
        )

    def test_an_unknown_key_is_core(self):
        self.assertIsNone(band_for("nosuch", "thing", "view"))
        self.assertIsNone(capability_key_for("nosuch", "thing", "view"))

    def test_no_band_names_a_depth_the_catalogue_does_not_seed(self):
        # The catalogue seeds ``<module>_core|plus|advanced``. A typo here
        # would silently leave keys unbanded rather than failing.
        suffixes = {"core", "plus", "advanced"}
        for (module, resource), band in list(
            __import__("vs_rbac.permission_bands", fromlist=["x"]).RESOURCE_BANDS.items()
        ):
            key = capability_key_for(module, resource, "view")
            if key and "_" in key and key.rsplit("_", 1)[1] in suffixes:
                self.assertIn(key.rsplit("_", 1)[1], suffixes)


class SeederIsRepeatableTests(_Seeded):
    """Running it twice changes nothing, and a withdrawn band is cleared."""

    def test_a_second_run_writes_nothing_new(self):
        before = dict(
            Permission.objects.values_list("key", "capability_id")
        )
        call_command("seed_permission_bands", verbosity=0)
        after = dict(Permission.objects.values_list("key", "capability_id"))
        self.assertEqual(before, after)

    def test_a_key_taken_off_the_price_list_is_cleared(self):
        # Moving a line out of the price list has to take effect on a re-run,
        # or the product goes on selling something nobody meant to sell.
        core = Capability.objects.get(key="finance_core")
        permission = Permission.objects.get(key="tickets.ticket.view")
        Permission.objects.filter(pk=permission.pk).update(capability=core)

        call_command("seed_permission_bands", verbosity=0)
        permission.refresh_from_db()
        self.assertIsNone(permission.capability_id)
