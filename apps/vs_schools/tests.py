import threading

from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from .models import Branch, School
from .serializers import SchoolCreateSerializer
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_rbac.tests.helpers import make_vision_user
from vs_tenants.models import Tenant


class SchoolCodeAllocationTests(TestCase):
    def test_model_allocates_code_when_omitted(self):
        school = School.objects.create(name="Generated School", slug="generated-school")

        self.assertTrue(school.code.startswith(f"SC-{school.tenant_id}"))

    def test_create_serializer_validates_without_code(self):
        serializer = SchoolCreateSerializer(data={
            "name": "Serializer School",
            "ownership_type": "PRIVATE",
            "address": "1 Test Road",
            "term_structure": "3_TERMS",
            "currency": "NGN",
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_create_serializer_uses_platform_defaults_only_for_omitted_fields(self):
        actor = make_vision_user(email="onboarding-defaults@example.com")
        set_value(
            definition=ConfigurationDefinition.objects.get(
                key="platform.onboarding.default_ownership_type"
            ),
            value="NGO",
            actor=actor,
        )
        set_value(
            definition=ConfigurationDefinition.objects.get(
                key="platform.onboarding.default_currency"
            ),
            value="USD",
            actor=actor,
        )

        omitted = SchoolCreateSerializer(data={"name": "Defaults School"})
        explicit = SchoolCreateSerializer(data={
            "name": "Explicit School",
            "ownership_type": "PRIVATE",
            "currency": "NGN",
        })

        self.assertTrue(omitted.is_valid(), omitted.errors)
        self.assertEqual(omitted.validated_data["ownership_type"], "NGO")
        self.assertEqual(omitted.validated_data["currency"], "USD")
        self.assertTrue(explicit.is_valid(), explicit.errors)
        self.assertEqual(explicit.validated_data["ownership_type"], "PRIVATE")
        self.assertEqual(explicit.validated_data["currency"], "NGN")


class BranchTenantDerivationTests(TestCase):
    """Phase B: a branch owns its tenant directly and can never be without one."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Derivation School", slug="derivation-school")
        cls.other = School.objects.create(name="Other School", slug="other-school")

    def test_new_branch_derives_its_tenant_from_the_school(self):
        branch = Branch.objects.create(school=self.school, name="Main", is_main=True)

        self.assertEqual(branch.tenant_id, self.school.tenant_id)
        branch.refresh_from_db()
        self.assertEqual(branch.tenant_id, self.school.tenant_id)

    def test_an_explicit_tenant_is_left_alone(self):
        # Nothing supplies tenant today; assert the derivation only fills a gap
        # so an explicit value (a future FAL caller) is not silently replaced.
        branch = Branch(school=self.school, name="Explicit", tenant=self.school.tenant)
        branch.save()

        self.assertEqual(branch.tenant_id, self.school.tenant_id)

    def test_branch_is_reachable_from_the_tenant_without_touching_the_school(self):
        Branch.objects.create(school=self.school, name="Main", is_main=True)

        self.assertEqual(self.school.tenant.branches.count(), 1)

    def test_tenant_aware_manager_now_scopes_branches_by_tenant(self):
        from vs_tenants.context import set_current_tenant

        Branch.objects.create(school=self.school, name="Mine", is_main=True)
        Branch.objects.create(school=self.other, name="Theirs", is_main=True)

        set_current_tenant(self.school.tenant)
        try:
            self.assertEqual([b.name for b in Branch.objects.all()], ["Mine"])
        finally:
            set_current_tenant(None)

    def test_a_tenant_with_no_branches_is_untouched(self):
        empty = School.objects.create(name="Branchless", slug="branchless")

        self.assertEqual(empty.tenant.branches.count(), 0)
        self.assertEqual(Branch.allocate_next_code(tenant_id=empty.tenant_id), 1)


class BranchUniquenessConstraintTests(TestCase):
    """R1: the uniqueness the docstring promised and Meta.constraints never had."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Unique School", slug="unique-school")
        cls.rival = School.objects.create(name="Rival School", slug="rival-school")

    def test_duplicate_code_for_one_tenant_is_rejected_by_the_database(self):
        Branch.objects.create(school=self.school, name="First", is_main=True)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Pin the code to bypass the allocator, the way the allocation
                # race would have done before the lock was fixed.
                Branch.all_objects.create(
                    school=self.school, tenant=self.school.tenant, name="Clash", code=1,
                )

    def test_the_same_code_is_free_for_a_different_tenant(self):
        mine = Branch.objects.create(school=self.school, name="Mine", is_main=True)
        theirs = Branch.objects.create(school=self.rival, name="Theirs", is_main=True)

        self.assertEqual(mine.code, 1)
        self.assertEqual(theirs.code, 1)

    def test_second_main_branch_is_rejected_as_a_field_error(self):
        Branch.objects.create(school=self.school, name="Main", is_main=True)

        with self.assertRaises(ValidationError) as caught:
            Branch.objects.create(school=self.school, name="Also main", is_main=True)

        self.assertIn("is_main", caught.exception.message_dict)

    def test_promoting_a_second_branch_to_main_is_rejected(self):
        Branch.objects.create(school=self.school, name="Main", is_main=True)
        spare = Branch.objects.create(school=self.school, name="Spare", is_main=False)

        spare.is_main = True
        with self.assertRaises(ValidationError):
            spare.save()

    def test_the_existing_main_branch_can_still_be_saved(self):
        main = Branch.objects.create(school=self.school, name="Main", is_main=True)

        main.name = "Main Campus"
        main.save()

        main.refresh_from_db()
        self.assertEqual(main.name, "Main Campus")

    def test_a_second_main_that_evades_the_model_guard_is_stopped_by_the_index(self):
        # The model guard is the friendly path; this proves the partial unique
        # index is really there, which is what makes it race-proof.
        Branch.objects.create(school=self.school, name="Main", is_main=True)
        spare = Branch.objects.create(school=self.school, name="Spare", is_main=False)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.all_objects.filter(pk=spare.pk).update(is_main=True)

    def test_each_tenant_keeps_its_own_main_branch(self):
        Branch.objects.create(school=self.school, name="Main", is_main=True)
        Branch.objects.create(school=self.rival, name="Main", is_main=True)

        self.assertEqual(Branch.all_objects.filter(is_main=True).count(), 2)


class BranchCodeAllocationConcurrencyTests(TransactionTestCase):
    """The first-branch race: two creates against a tenant that has no branches.

    ``allocate_next_code`` used to lock ``filter(school=school)``, which selects
    no rows at all when the school is empty, so both callers read max=0 and both
    wrote code 1. It now locks the tenant row, which always exists.
    """

    # The codex platform tenant is migration-seeded and TransactionTestCase
    # flushes between methods, so restore the serialized seed data.
    serialized_rollback = True

    def test_two_concurrent_first_branch_creates_get_different_codes(self):
        school = School.objects.create(name="Race School", slug="race-school")
        tenant_id = school.tenant_id

        holder_locked = threading.Event()
        release_holder = threading.Event()
        second_attempting = threading.Event()
        second_done = threading.Event()
        outcomes = {}

        def holder():
            """Take the allocation lock and sit on it inside an open transaction."""
            close_old_connections()
            try:
                with transaction.atomic():
                    code = Branch.allocate_next_code(tenant_id=tenant_id)
                    holder_locked.set()
                    if not release_holder.wait(5):
                        raise TimeoutError("branch allocation race was never released")
                    Branch.all_objects.create(
                        school=school, tenant_id=tenant_id, name="First", code=code,
                    )
                outcomes["holder"] = code
            except Exception as exc:  # surfaced by the assertions below
                outcomes["holder_error"] = exc
            finally:
                close_old_connections()

        def contender():
            """Allocate through the ordinary create path while the lock is held."""
            close_old_connections()
            try:
                second_attempting.set()
                branch = Branch(school=school, name="Second")
                branch.save()
                outcomes["contender"] = branch.code
            except Exception as exc:
                outcomes["contender_error"] = exc
            finally:
                close_old_connections()
                second_done.set()

        first = threading.Thread(target=holder, daemon=True)
        second = threading.Thread(target=contender, daemon=True)

        first.start()
        try:
            self.assertTrue(holder_locked.wait(5))
            second.start()
            self.assertTrue(second_attempting.wait(5))
            # The negative control: with the old zero-row lock the contender
            # would have read max=0 and finished here with code 1.
            self.assertFalse(second_done.wait(0.5))
        finally:
            release_holder.set()
        first.join(10)
        second.join(10)

        self.assertNotIn("holder_error", outcomes)
        self.assertNotIn("contender_error", outcomes)
        self.assertEqual(
            sorted([outcomes["holder"], outcomes["contender"]]), [1, 2],
        )
        self.assertEqual(
            sorted(Branch.all_objects.filter(tenant_id=tenant_id)
                   .values_list("code", flat=True)),
            [1, 2],
        )


class BranchTenantMigrationTests(TransactionTestCase):
    """The 0002 backfill, its de-duplication step, and its reverse."""

    serialized_rollback = True

    APP = "vs_schools"
    BEFORE = "0002_alter_branchlifecycle_reason"
    AFTER = "0003_branch_tenant"

    def tearDown(self):
        # Always leave the database at the latest state for the rest of the run.
        self._migrate(self.AFTER)
        super().tearDown()

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(self.APP, target)])
        executor.loader.build_graph()
        return executor

    def _historical_apps(self, target):
        """One state registry, so every historical model shares an identity."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        return executor.loader.project_state((self.APP, target)).apps

    def _seed_pre_migration_rows(self):
        """Build schools and branches in the 0001 state, where Branch has no tenant."""
        self._migrate(self.BEFORE)
        historical = self._historical_apps(self.BEFORE)
        OldSchool = historical.get_model(self.APP, "School")
        OldBranch = historical.get_model(self.APP, "Branch")

        tenants = {}
        schools = {}
        for slug, name in (("alpha", "Alpha School"), ("beta", "Beta School")):
            tenant = Tenant.objects.create(
                name=name, slug=slug, kind=Tenant.Kind.SCHOOL, status=Tenant.Status.ACTIVE,
            )
            tenants[slug] = tenant
            schools[slug] = OldSchool.objects.create(
                name=name, slug=slug, code=f"SC-{slug}", tenant_id=tenant.pk,
                ownership_type="PRIVATE", term_structure="3_TERMS", currency="NGN",
            )
        return tenants, schools, OldBranch

    def test_backfill_gives_every_branch_its_school_tenant_and_leaves_none_null(self):
        tenants, schools, OldBranch = self._seed_pre_migration_rows()
        OldBranch.objects.create(school=schools["alpha"], name="A1", code=1, _type="Main")
        OldBranch.objects.create(school=schools["alpha"], name="A2", code=2, _type="Sub")
        OldBranch.objects.create(school=schools["beta"], name="B1", code=1, _type="Main")

        self._migrate(self.AFTER)

        self.assertEqual(Branch.all_objects.filter(tenant__isnull=True).count(), 0)
        self.assertEqual(
            set(Branch.all_objects.filter(name__startswith="A")
                .values_list("tenant_id", flat=True)),
            {tenants["alpha"].pk},
        )
        self.assertEqual(
            Branch.all_objects.get(name="B1").tenant_id, tenants["beta"].pk,
        )
        for branch in Branch.all_objects.all():
            self.assertEqual(branch.tenant_id, branch.school.tenant_id)

    def test_a_tenant_with_no_branches_survives_the_migration(self):
        tenants, _, _ = self._seed_pre_migration_rows()

        self._migrate(self.AFTER)

        self.assertEqual(tenants["alpha"].branches.count(), 0)
        self.assertEqual(tenants["beta"].branches.count(), 0)

    def test_dirty_data_is_de_duplicated_so_the_constraints_can_be_added(self):
        tenants, schools, OldBranch = self._seed_pre_migration_rows()
        # Exactly what the unlocked allocator produced: two branches, code 1.
        first = OldBranch.objects.create(
            school=schools["alpha"], name="Dup A", code=1, is_main=True, _type="Main",
        )
        second = OldBranch.objects.create(
            school=schools["alpha"], name="Dup B", code=1, is_main=True, _type="Main",
        )
        third = OldBranch.objects.create(
            school=schools["alpha"], name="Dup C", code=1, is_main=False, _type="Sub",
        )

        self._migrate(self.AFTER)

        codes = dict(
            Branch.all_objects.filter(tenant=tenants["alpha"]).values_list("pk", "code")
        )
        self.assertEqual(codes[first.pk], 1, "the lowest pk keeps its code")
        self.assertEqual(sorted(codes.values()), [1, 2, 3])
        mains = list(
            Branch.all_objects.filter(tenant=tenants["alpha"], is_main=True)
            .values_list("pk", flat=True)
        )
        self.assertEqual(mains, [first.pk], "only the lowest pk stays main")
        self.assertTrue(Branch.all_objects.filter(pk=second.pk, is_main=False).exists())
        self.assertTrue(Branch.all_objects.filter(pk=third.pk, is_main=False).exists())

    def test_reverse_drops_the_column_and_both_constraints(self):
        _, schools, OldBranch = self._seed_pre_migration_rows()
        OldBranch.objects.create(school=schools["alpha"], name="A1", code=1, _type="Main")
        self._migrate(self.AFTER)

        self._migrate(self.BEFORE)

        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(
                cursor, "vs_schools_branch",
            )
            constraints = connection.introspection.get_constraints(
                cursor, "vs_schools_branch",
            )
        self.assertNotIn("tenant_id", [c.name for c in columns])
        self.assertNotIn("uq_branch_tenant_code", constraints)
        self.assertNotIn("uq_branch_one_main_per_tenant", constraints)
        # The rows themselves survive: the reverse is a no-op on data.
        self.assertEqual(OldBranch.objects.count(), 1)

    def test_forward_reverse_forward_is_stable(self):
        _, schools, OldBranch = self._seed_pre_migration_rows()
        OldBranch.objects.create(school=schools["beta"], name="B1", code=1, _type="Main")

        self._migrate(self.AFTER)
        self._migrate(self.BEFORE)
        self._migrate(self.AFTER)

        branch = Branch.all_objects.get(name="B1")
        self.assertEqual(branch.tenant_id, branch.school.tenant_id)
