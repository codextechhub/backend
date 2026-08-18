import json
import threading
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import School
from .serializers import SchoolCreateSerializer
from .views.school import SchoolDetailView
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_rbac.tests.helpers import make_branch, make_school, make_vision_user
from vs_tenants.models import Branch, Tenant


def _branch_payload(name="Main Campus", *, is_main=True, email="main@branch.test"):
    """The smallest branch a school can be created with."""
    return {
        "name": name,
        "_type": "Main" if is_main else "Annex",
        "state": "Lagos",
        "is_main": is_main,
        "primary_admin_data": {"full_name": f"{name} Head", "email": email},
    }


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
            "branches": [_branch_payload(email="head@serializer-school.test")],
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

        omitted = SchoolCreateSerializer(data={
            "name": "Defaults School",
            "branches": [_branch_payload(email="head@defaults-school.test")],
        })
        explicit = SchoolCreateSerializer(data={
            "name": "Explicit School",
            "ownership_type": "PRIVATE",
            "currency": "NGN",
            "branches": [_branch_payload(email="head@explicit-school.test")],
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
        """A bare tenant, not a school: every school has a branch.

        Code allocation has to work for a tenant whose first branch is being
        created, and a tenant with no product attached is the honest way to
        express that now that a branchless school is not a shape that exists.
        """
        empty = Tenant.objects.create(
            name="Empty Org", slug="empty-org", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )

        self.assertEqual(empty.branches.count(), 0)
        self.assertEqual(Branch.allocate_next_code(tenant_id=empty.pk), 1)


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

    def _make_tenant(self, historical, *, slug, name):
        """Create a tenant through the model as it stood at that migration.

        Never through the live ``Tenant``. Rewinding this app also unapplies
        the ``vs_tenants`` migrations that depend on it, so any column added to
        ``Tenant`` after that point does not exist in the database while these
        tests run, and the live model would try to insert it. That is not
        hypothetical: it is exactly what the onboarding expiry sweep's
        ``pending_since`` column did to all seven of these tests. The
        historical model is the shape the database actually has.

        For the same reason nothing below queries ``Branch`` by a tenant
        *instance*: a historical instance is a different class from the live
        one, so these tests filter on ``tenant_id``.
        """
        HistoricalTenant = historical.get_model("vs_tenants", "Tenant")
        return HistoricalTenant.objects.create(
            name=name,
            slug=slug,
            kind=Tenant.Kind.SCHOOL,
            status=Tenant.Status.ACTIVE,
        )


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
            tenant = self._make_tenant(historical, slug=slug, name=name)
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

        self.assertEqual(
            Branch.all_objects.filter(tenant_id=tenants["alpha"].pk).count(), 0,
        )
        self.assertEqual(
            Branch.all_objects.filter(tenant_id=tenants["beta"].pk).count(), 0,
        )

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
            Branch.all_objects.filter(tenant_id=tenants["alpha"].pk)
            .values_list("pk", "code")
        )
        self.assertEqual(codes[first.pk], 1, "the lowest pk keeps its code")
        self.assertEqual(sorted(codes.values()), [1, 2, 3])
        mains = list(
            Branch.all_objects.filter(tenant_id=tenants["alpha"].pk, is_main=True)
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
            tenant = self._make_tenant(historical, slug=slug, name=name)
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
                Branch.all_objects.filter(tenant_id=tenants["multi"].pk)
                .values_list("name", flat=True)
            ),
            ["HQ", "Lekki"],
        )
        self.assertEqual(Branch.all_objects.filter(tenant_id=tenants["solo"].pk).count(), 0)
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
                Branch.all_objects.filter(tenant_id=tenants["multi"].pk)
                .values_list("code", flat=True)
            ),
            [1, 2],
        )
        self.assertEqual(Branch.all_objects.filter(tenant_id=tenants["solo"].pk).count(), 0)
        # Still creatable, and the allocator still counts from the tenant.
        # Through ``save()``, not by calling ``allocate_next_code`` directly:
        # the allocator takes a ``select_for_update`` lock and a
        # TransactionTestCase runs in autocommit, so calling it by hand raises
        # TransactionManagementError. ``save()`` opens the atomic block itself,
        # which is also the path production uses.
        fresh = Branch.all_objects.create(
            tenant_id=tenants["multi"].pk, name="Ikoyi", _type="Sub",
        )
        self.assertEqual(fresh.code, 3)


class EverySchoolHasAtLeastOneBranchTests(TestCase):
    """The invariant the product settled and the code did not hold.

    ``SchoolCreateSerializer.branches`` was ``required=False, default=list``, so
    the live creation endpoint (and the bulk importer, which runs this same
    serializer) would happily mint a school with nowhere to put a user, a
    document or a student. Every branch rule in ``validate()`` sat behind an
    ``if branches:`` and therefore never ran for exactly the payload that needed
    them.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="branch-invariant@example.com", super_admin=True,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _post(self, payload, *, expect):
        response = self._client().post(
            reverse("school-create"), payload, format="json",
        )
        self.assertEqual(response.status_code, expect, response.data)
        return response

    # --- refused ----------------------------------------------------------

    def test_a_school_with_no_branches_at_all_is_refused(self):
        response = self._post(
            {"name": "Nowhere School", "slug": "nowhere-school"}, expect=400,
        )

        self.assertIn("branches", self._errors(response))
        self.assertFalse(School.objects.filter(slug="nowhere-school").exists())

    def test_an_empty_branch_list_is_refused(self):
        response = self._post(
            {"name": "Empty List", "slug": "empty-list", "branches": []},
            expect=400,
        )

        self.assertIn("branches", self._errors(response))
        self.assertFalse(School.objects.filter(slug="empty-list").exists())

    def test_the_refusal_is_a_validation_error_not_a_crash(self):
        """400 with a readable message, never a 500."""
        response = self._post({"name": "Readable", "slug": "readable"}, expect=400)

        self.assertIn("at least one branch", str(self._errors(response)["branches"]))

    def test_nothing_at_all_is_written_when_the_branch_is_missing(self):
        """The refusal happens in validation, before the first row is written."""
        before = (
            School.objects.count(), Tenant.objects.count(), Branch.all_objects.count(),
        )

        self._post({"name": "No Trace", "slug": "no-trace"}, expect=400)

        self.assertEqual(
            (School.objects.count(), Tenant.objects.count(), Branch.all_objects.count()),
            before,
        )

    def test_two_branches_still_need_exactly_one_main(self):
        response = self._post({
            "name": "Two Mains", "slug": "two-mains",
            "branches": [
                _branch_payload("A", is_main=True, email="a@two-mains.test"),
                _branch_payload("B", is_main=True, email="b@two-mains.test"),
            ],
        }, expect=400)

        self.assertIn("branches", self._errors(response))
        self.assertFalse(School.objects.filter(slug="two-mains").exists())

    def test_two_branches_with_no_main_are_refused(self):
        """Promotion is only safe when there is one branch to promote."""
        self._post({
            "name": "No Main", "slug": "no-main",
            "branches": [
                _branch_payload("A", is_main=False, email="a@no-main.test"),
                _branch_payload("B", is_main=False, email="b@no-main.test"),
            ],
        }, expect=400)

        self.assertFalse(School.objects.filter(slug="no-main").exists())

    # --- accepted ---------------------------------------------------------

    def test_one_branch_succeeds_and_that_branch_is_the_main_one(self):
        self._post({
            "name": "One Branch", "slug": "one-branch",
            "branches": [_branch_payload("Main Campus", email="head@one-branch.test")],
        }, expect=201)

        school = School.objects.get(slug="one-branch")
        self.assertEqual(school.branches.count(), 1)
        branch = school.main_branch
        self.assertIsNotNone(branch)
        self.assertEqual(branch.name, "Main Campus")
        self.assertTrue(branch.is_main)
        self.assertEqual(branch.code, 1)
        self.assertEqual(branch.tenant_id, school.tenant_id)

    def test_the_only_branch_is_promoted_to_main_when_the_flag_is_omitted(self):
        """A school's single site is its main site; saying so twice buys nothing."""
        self._post({
            "name": "Implied Main", "slug": "implied-main",
            "branches": [{
                "name": "Only Campus", "_type": "Main", "state": "Lagos",
                "primary_admin_data": {
                    "full_name": "Only Head", "email": "head@implied-main.test",
                },
            }],
        }, expect=201)

        school = School.objects.get(slug="implied-main")
        self.assertEqual(school.branches.count(), 1)
        self.assertTrue(school.main_branch.is_main)

    def test_a_multi_branch_school_keeps_the_shape_it_asked_for(self):
        """One branch proves nothing about a school with several."""
        self._post({
            "name": "Multi Branch", "slug": "multi-branch",
            "branches": [
                _branch_payload("HQ", is_main=True, email="hq@multi-branch.test"),
                _branch_payload("Lekki", is_main=False, email="lekki@multi-branch.test"),
                _branch_payload("Ikoyi", is_main=False, email="ikoyi@multi-branch.test"),
            ],
        }, expect=201)

        school = School.objects.get(slug="multi-branch")
        self.assertEqual(school.branches.count(), 3)
        self.assertEqual(school.main_branch.name, "HQ")
        self.assertEqual(
            sorted(school.branches.values_list("code", flat=True)), [1, 2, 3],
        )

    @staticmethod
    def _errors(response):
        """Field errors live at ``error.detail`` in the project envelope."""
        data = response.data
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return data["error"].get("detail", data)
        return data


class BulkImporterSuppliesAMainBranchTests(TestCase):
    """The second creation path: the bulk school importer.

    It runs the same serializer, so the rule above already binds it. What is
    pinned here is that an import row with no branch columns filled in still
    produces a school with a main branch rather than a rejected row.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="branch-importer@example.com", super_admin=True,
        )

    def test_a_row_with_no_branch_columns_still_gets_a_main_branch(self):
        from vs_import_data.services.import_executor import import_schools_row

        result = import_schools_row(
            import_batch=None,
            payload={
                "name": "Imported Academy",
                "slug": "imported-academy",
                "branch_admin_full_name": "Imported Head",
                "branch_admin_email": "head@imported-academy.test",
            },
            queued_by=self.vision_user,
        )

        school = result.instance
        self.assertEqual(school.slug, "imported-academy")
        self.assertEqual(school.branches.count(), 1)
        self.assertTrue(school.main_branch.is_main)
        # The default name the handler builds when the column is blank.
        self.assertEqual(school.main_branch.name, "Imported Academy - Main Campus")

    def test_a_row_names_its_branch_when_the_column_is_filled(self):
        from vs_import_data.services.import_executor import import_schools_row

        result = import_schools_row(
            import_batch=None,
            payload={
                "name": "Named Academy",
                "slug": "named-academy",
                "branch_name": "Yaba Campus",
                "branch_state": "Lagos",
                "branch_admin_full_name": "Yaba Head",
                "branch_admin_email": "head@named-academy.test",
            },
            queued_by=self.vision_user,
        )

        self.assertEqual(result.instance.main_branch.name, "Yaba Campus")


class SeedDataSuppliesABranchTests(TestCase):
    """The dev seed is a creation path too, and it must not model a shape the
    product has abolished."""

    def test_every_seeded_school_spec_has_exactly_one_main_branch(self):
        from core.management.commands.seed_dev_data import SCHOOLS

        self.assertTrue(SCHOOLS)
        for spec in SCHOOLS:
            branches = spec["branches"]
            self.assertTrue(branches, f"{spec['slug']} is seeded with no branch")
            mains = [b for b in branches if b[2]]
            self.assertEqual(
                len(mains), 1, f"{spec['slug']} must have exactly one main branch",
            )


class SchoolDetailMissingSlugTests(TestCase):
    """A school that does not exist answers 404, and says nothing else.

    The verification 7464999 never wrote. That commit removed a blanket
    ``except Exception`` from ``SchoolDetailView.retrieve`` which caught every
    failure - including the ``Http404`` that ``get_object`` raises for an
    unknown slug - and answered with ``f"DEBUG: {type(exc).__name__}: {exc}"``
    plus ``traceback.format_exc()``. Two defects in one: an absent school came
    back as a server fault rather than a 404, and the response was built to hand
    a Python stack trace, file paths and all, to whoever asked.

    The status code was never the whole defect, so these assert on the body.
    """

    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="detail-404@example.com", super_admin=True,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _get(self, slug):
        return self._client().get(reverse("school-detail", kwargs={"slug": slug}))

    def test_a_school_that_does_not_exist_answers_404(self):
        response = self._get("no-such-school")

        self.assertEqual(response.status_code, 404, response.data)

    def test_the_404_body_carries_no_trace_of_the_server(self):
        response = self._get("no-such-school")

        body = json.dumps(response.data)
        for leak in (
            "DEBUG:",
            "Traceback",
            "traceback",
            'File "',
            "site-packages",
            "/apps/",
            "Http404",
            "Exception",
        ):
            self.assertNotIn(leak, body, f"404 body leaked {leak!r}: {body}")

    def test_the_404_uses_the_platform_error_envelope(self):
        """The same shape any other 404 in this codebase produces, so a caller
        does not have to special-case this endpoint."""
        response = self._get("no-such-school")

        self.assertEqual(response.data["success"], False)
        self.assertIsInstance(response.data["message"], str)
        self.assertTrue(response.data["message"])
        self.assertEqual(response.data["error"]["code"], "REQUEST_ERROR")

    def test_an_existing_school_is_unaffected(self):
        school = make_school(slug="present-school", name="Present School")
        make_branch(school, name="Main Campus")

        response = self._get("present-school")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["slug"], "present-school")

    def test_an_unexpected_failure_still_leaks_nothing(self):
        """The other route out of this view.

        Removing the blanket ``except`` handed every non-404 failure to
        ``core.exceptions.custom_exception_handler``. That handler must answer
        an unforeseen error with a fixed sentence and log the trace server-side
        - if it echoed the exception instead, the traceback would simply have
        moved one layer out rather than gone away.
        """
        school = make_school(slug="present-or-not", name="Present Or Not")
        make_branch(school, name="Main Campus")
        # Patched on the view class, not on the module-level name: the view
        # bound ``serializer_class = SchoolDetailSerializer`` when it was
        # defined, so replacing the module attribute changes nothing and the
        # request simply succeeds - which is how this test first passed for the
        # wrong reason.
        with mock.patch.object(
            SchoolDetailView, "get_serializer",
            side_effect=RuntimeError("psql://user:hunter2@db.internal/cx"),
        ):
            with self.assertLogs("core.exceptions", level="ERROR"):
                response = self._get("present-or-not")

        self.assertEqual(response.status_code, 500)
        body = json.dumps(response.data)
        self.assertNotIn("hunter2", body)
        self.assertNotIn("RuntimeError", body)
        self.assertNotIn("Traceback", body)
        self.assertEqual(response.data["error"]["code"], "SERVER_ERROR")
