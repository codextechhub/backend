import threading

from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from .models import School
from .serializers import SchoolCreateSerializer
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_rbac.tests.helpers import make_vision_user
from vs_tenants.models import Branch, Tenant


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


class BranchTenantOwnershipTests(TestCase):
    """Phase D: a branch is owned by a tenant, and there is no school link left.

    These were derivation tests while ``Branch.save()`` copied the tenant down
    from the school. The column is gone, so what has to hold now is that the
    school app still reaches its sites, that a caller must name the owner, and
    that no code path can reach a school from a branch.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Derivation School", slug="derivation-school")
        cls.other = School.objects.create(name="Other School", slug="other-school")

    def test_a_branch_is_created_against_a_tenant(self):
        branch = Branch.objects.create(
            tenant=self.school.tenant, name="Main", is_main=True,
        )

        self.assertEqual(branch.tenant_id, self.school.tenant_id)
        branch.refresh_from_db()
        self.assertEqual(branch.tenant_id, self.school.tenant_id)

    def test_a_branch_no_longer_carries_a_school_at_all(self):
        """The point of the phase: no field, no column, no reverse traversal."""
        field_names = {f.name for f in Branch._meta.get_fields()}
        self.assertNotIn("school", field_names)
        self.assertEqual(Branch._meta.app_label, "vs_tenants")
        self.assertEqual(Branch._meta.db_table, "vs_schools_branch")

    def test_a_tenant_with_no_school_can_still_own_a_branch(self):
        """A branch needs a tenant, not a product. This is the decoupling."""
        plain = Tenant.objects.create(
            name="Plain Org", slug="plain-org", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )

        branch = Branch.objects.create(tenant=plain, name="Depot", is_main=True)

        self.assertEqual(branch.code, 1)
        self.assertEqual(plain.branches.count(), 1)

    def test_the_school_still_reaches_its_sites(self):
        Branch.objects.create(tenant=self.school.tenant, name="Main", is_main=True)

        self.assertEqual(self.school.branches.count(), 1)
        self.assertEqual(self.school.tenant.branches.count(), 1)
        self.assertEqual(self.school.main_branch.name, "Main")

    def test_tenant_aware_manager_scopes_branches_by_tenant(self):
        from vs_tenants.context import set_current_tenant

        Branch.objects.create(tenant=self.school.tenant, name="Mine", is_main=True)
        Branch.objects.create(tenant=self.other.tenant, name="Theirs", is_main=True)

        set_current_tenant(self.school.tenant)
        try:
            self.assertEqual([b.name for b in Branch.objects.all()], ["Mine"])
        finally:
            set_current_tenant(None)

    def test_a_tenant_with_no_branches_is_untouched(self):
        empty = School.objects.create(name="Branchless", slug="branchless")

        self.assertEqual(empty.branches.count(), 0)
        self.assertIsNone(empty.main_branch)
        self.assertEqual(Branch.allocate_next_code(tenant_id=empty.tenant_id), 1)


class BranchUniquenessConstraintTests(TestCase):
    """R1: the uniqueness the docstring promised and Meta.constraints never had."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Unique School", slug="unique-school")
        cls.rival = School.objects.create(name="Rival School", slug="rival-school")

    def test_duplicate_code_for_one_tenant_is_rejected_by_the_database(self):
        Branch.objects.create(tenant=self.school.tenant, name="First", is_main=True)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Pin the code to bypass the allocator, the way the allocation
                # race would have done before the lock was fixed.
                Branch.all_objects.create(
                    tenant=self.school.tenant, name="Clash", code=1,
                )

    def test_the_same_code_is_free_for_a_different_tenant(self):
        mine = Branch.objects.create(tenant=self.school.tenant, name="Mine", is_main=True)
        theirs = Branch.objects.create(tenant=self.rival.tenant, name="Theirs", is_main=True)

        self.assertEqual(mine.code, 1)
        self.assertEqual(theirs.code, 1)

    def test_second_main_branch_is_rejected_as_a_field_error(self):
        Branch.objects.create(tenant=self.school.tenant, name="Main", is_main=True)

        with self.assertRaises(ValidationError) as caught:
            Branch.objects.create(tenant=self.school.tenant, name="Also main", is_main=True)

        self.assertIn("is_main", caught.exception.message_dict)

    def test_promoting_a_second_branch_to_main_is_rejected(self):
        Branch.objects.create(tenant=self.school.tenant, name="Main", is_main=True)
        spare = Branch.objects.create(tenant=self.school.tenant, name="Spare", is_main=False)

        spare.is_main = True
        with self.assertRaises(ValidationError):
            spare.save()

    def test_the_existing_main_branch_can_still_be_saved(self):
        main = Branch.objects.create(tenant=self.school.tenant, name="Main", is_main=True)

        main.name = "Main Campus"
        main.save()

        main.refresh_from_db()
        self.assertEqual(main.name, "Main Campus")

    def test_a_second_main_that_evades_the_model_guard_is_stopped_by_the_index(self):
        # The model guard is the friendly path; this proves the partial unique
        # index is really there, which is what makes it race-proof.
        Branch.objects.create(tenant=self.school.tenant, name="Main", is_main=True)
        spare = Branch.objects.create(tenant=self.school.tenant, name="Spare", is_main=False)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Branch.all_objects.filter(pk=spare.pk).update(is_main=True)

    def test_each_tenant_keeps_its_own_main_branch(self):
        Branch.objects.create(tenant=self.school.tenant, name="Main", is_main=True)
        Branch.objects.create(tenant=self.rival.tenant, name="Main", is_main=True)

        self.assertEqual(Branch.all_objects.filter(is_main=True).count(), 2)


class BranchCodeAllocationConcurrencyTests(TransactionTestCase):
    """The first-branch race: two creates against a tenant that has no branches.

    ``allocate_next_code`` used to lock ``filter(school=school)``, which selects
    no rows at all when the owner is empty, so both callers read max=0 and both
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
                        tenant_id=tenant_id, name="First", code=code,
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
                branch = Branch(tenant_id=tenant_id, name="Second")
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


class _MigrationHarness(TransactionTestCase):
    """Drive real migrations forward and back, then leave the database current.

    Subclasses set ``BEFORE`` and ``AFTER``. Shared by the two branch migration
    suites so that the "put every leaf back" rule below is stated once; getting
    it wrong is silent until an unrelated test hits a missing column.
    """

    serialized_rollback = True

    APP = "vs_schools"
    BEFORE = ""
    AFTER = ""

    def tearDown(self):
        # Always leave the database at the latest state for the rest of the run.
        #
        # Every LEAF, not just this app's: rewinding vs_schools 0003 also
        # unapplies the migrations that depend on it - vs_workflow 0007, which
        # adds WorkflowTemplate.is_active, and migrations in vs_finance and
        # vs_procurement. Migrating only this app forward left their columns
        # missing for the rest of the run, so the serialized-rollback restore
        # and every later test that touched those tables failed on a column
        # that does not exist.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        executor.loader.build_graph()
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


class BranchTenantMigrationTests(_MigrationHarness):
    """Phase B: the 0003 backfill, its de-duplication step, and its reverse."""

    BEFORE = "0002_alter_branchlifecycle_reason"
    AFTER = "0003_branch_tenant"

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
        # Read back through the historical model: the current ``Branch`` has
        # no ``school`` any more, and the whole point of the backfill was that
        # the two agreed at the moment the column still existed.
        MigratedBranch = self._historical_apps(self.AFTER).get_model(self.APP, "Branch")
        for branch in MigratedBranch.objects.select_related("school"):
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

        MigratedBranch = self._historical_apps(self.AFTER).get_model(self.APP, "Branch")
        branch = MigratedBranch.objects.select_related("school").get(name="B1")
        self.assertEqual(branch.tenant_id, branch.school.tenant_id)


class BranchMoveMigrationTests(_MigrationHarness):
    """Phase D: the school column goes, the model changes app, no row moves.

    ``0004_branch_drop_school`` is the only migration in the phase that emits
    SQL; the ten that follow it are state-only. What has to be proved is that
    the pair round-trips on a database that already has data in it, in both
    tenant shapes, and that nothing was silently dropped on the way.

    ``0003`` is the reverse target rather than ``0002`` because that is the
    boundary this phase owns: rewinding to it unapplies the state move, the
    eight retargets and the column drop, and puts ``school_id`` back.
    """

    BEFORE = "0003_branch_tenant"
    AFTER = "0005_move_branch_to_vs_tenants"

    def _seed(self):
        """Two shapes of tenant: one with several branches, one with none."""
        self._migrate(self.BEFORE)
        historical = self._historical_apps(self.BEFORE)
        OldSchool = historical.get_model(self.APP, "School")
        OldBranch = historical.get_model(self.APP, "Branch")

        tenants, schools = {}, {}
        for slug, name in (("multi", "Multi School"), ("solo", "Solo School")):
            tenant = Tenant.objects.create(
                name=name, slug=slug, kind=Tenant.Kind.SCHOOL, status=Tenant.Status.ACTIVE,
            )
            tenants[slug] = tenant
            schools[slug] = OldSchool.objects.create(
                name=name, slug=slug, code=f"SC-{slug}", tenant_id=tenant.pk,
                ownership_type="PRIVATE", term_structure="3_TERMS", currency="NGN",
            )

        OldBranch.objects.create(
            school=schools["multi"], tenant_id=tenants["multi"].pk,
            name="HQ", code=1, is_main=True, _type="Main",
        )
        OldBranch.objects.create(
            school=schools["multi"], tenant_id=tenants["multi"].pk,
            name="Lekki", code=2, is_main=False, _type="Sub",
        )
        # "solo" deliberately gets no branch at all.
        return tenants, schools, OldBranch

    def _branch_columns(self):
        with connection.cursor() as cursor:
            return {
                c.name
                for c in connection.introspection.get_table_description(
                    cursor, "vs_schools_branch",
                )
            }

    def _inbound_foreign_keys(self):
        """Every foreign key constraint pointing at the branch table."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.confrelid "
                "WHERE t.relname = %s AND c.contype = 'f'",
                ["vs_schools_branch"],
            )
            return cursor.fetchone()[0]

    def _constraints(self):
        with connection.cursor() as cursor:
            return connection.introspection.get_constraints(cursor, "vs_schools_branch")

    # Two methods, not six. Each one rewinds and replays the tail of a
    # 155-migration graph, which is the slowest thing in this suite by an order
    # of magnitude, so the assertions are grouped by the state they need rather
    # than one per behaviour.

    def test_forward_drops_the_school_and_re_keys_the_indexes(self):
        tenants, _, _ = self._seed()
        before_fks = self._inbound_foreign_keys()

        self._migrate(self.AFTER)

        columns = self._branch_columns()
        self.assertNotIn("school_id", columns)
        self.assertIn("tenant_id", columns)

        # Every row is still there, in both tenant shapes.
        self.assertEqual(
            sorted(
                Branch.all_objects.filter(tenant=tenants["multi"])
                .values_list("name", flat=True)
            ),
            ["HQ", "Lekki"],
        )
        self.assertEqual(Branch.all_objects.filter(tenant=tenants["solo"]).count(), 0)
        # No row moved and no constraint was rebuilt away: the table is the
        # same table, so every inbound foreign key is still there.
        self.assertEqual(self._inbound_foreign_keys(), before_fks)

        constraints = self._constraints()
        self.assertIn("vs_schools__tenant__6bef02_idx", constraints)
        self.assertIn("vs_schools__tenant__b47bb3_idx", constraints)
        self.assertIn("vs_schools__tenant__457ea7_idx", constraints)
        self.assertNotIn("vs_schools__school__38f3c1_idx", constraints)
        self.assertNotIn("vs_schools__school__e52510_idx", constraints)
        self.assertNotIn("vs_schools__school__b13fda_idx", constraints)
        # The uniqueness phase B added must survive the re-keying.
        self.assertIn("uq_branch_tenant_code", constraints)
        self.assertIn("uq_branch_one_main_per_tenant", constraints)

    def test_forward_reverse_forward_is_stable(self):
        tenants, schools, _ = self._seed()

        self._migrate(self.AFTER)
        self._migrate(self.BEFORE)

        # The reverse is not a no-op on data: it has to work out which school
        # each branch belonged to, from the tenant they now share.
        self.assertIn("school_id", self._branch_columns())
        OldBranch = self._historical_apps(self.BEFORE).get_model(self.APP, "Branch")
        self.assertEqual(
            {b.name: b.school_id for b in OldBranch.objects.all()},
            {"HQ": schools["multi"].pk, "Lekki": schools["multi"].pk},
        )
        for branch in OldBranch.objects.all():
            self.assertEqual(branch.tenant_id, tenants["multi"].pk)

        self._migrate(self.AFTER)

        self.assertNotIn("school_id", self._branch_columns())
        self.assertEqual(
            sorted(
                Branch.all_objects.filter(tenant=tenants["multi"])
                .values_list("code", flat=True)
            ),
            [1, 2],
        )
        self.assertEqual(Branch.all_objects.filter(tenant=tenants["solo"]).count(), 0)
        # Still creatable, and the allocator still counts from the tenant.
        # Through ``save()``, not by calling ``allocate_next_code`` directly:
        # the allocator takes a ``select_for_update`` lock and a
        # TransactionTestCase runs in autocommit, so calling it by hand raises
        # TransactionManagementError. ``save()`` opens the atomic block itself,
        # which is also the path production uses.
        fresh = Branch.all_objects.create(
            tenant=tenants["multi"], name="Ikoyi", _type="Sub",
        )
        self.assertEqual(fresh.code, 3)
