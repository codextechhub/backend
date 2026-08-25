"""Row narrowing per caller: the half of the boundary the engine was missing.

The Export Centre already narrowed *columns* per person - ``resolve_columns``
runs at build time to shape the picker and again at run time to shape the file.
It did not narrow *rows*, so a branch-pinned caller holding an export key could
export sites whose screens answer 404 for them. These pin the fix, and pin it
at the engine rather than in one module, because the gap was never a schools
problem: every branch-scoped dataset in the platform had it.
"""
from __future__ import annotations

from django.test import TestCase

from vs_exports.catalogue import ScopeContext, narrow_to_caller_branches
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        cls.role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(
            cls.role,
            make_permission("academics.structure.view", scope=PermissionScope.TENANT),
        )
        cls.school_level = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.school_level, cls.role, branch=None)
        cls.lekki_head = make_school_admin(
            None, email="head@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_head, cls.role, branch=cls.lekki)

    def setUp(self):
        from schools.vs_academics.models import Department

        self.shared = Department.all_objects.create(
            tenant=self.tenant, name="Sciences", code="SCI",
        )
        self.lekki_dept = Department.all_objects.create(
            tenant=self.tenant, name="Lekki Extra", code="LEX", branch=self.lekki,
        )
        self.ikeja_dept = Department.all_objects.create(
            tenant=self.tenant, name="Ikeja Extra", code="IEX", branch=self.ikeja,
        )

    def names(self, user, **kwargs):
        from schools.vs_academics.models import Department

        qs = narrow_to_caller_branches(
            Department.all_objects.filter(tenant=self.tenant),
            ScopeContext(tenant=self.tenant, user=user),
            **kwargs,
        )
        return {d.name for d in qs}


class NarrowingTests(_Base):
    def test_a_branch_caller_gets_the_shared_rows_plus_their_own(self):
        """And never another branch's, which is the whole point."""
        self.assertEqual(
            self.names(self.lekki_head), {"Sciences", "Lekki Extra"},
        )

    def test_a_school_level_caller_gets_everything(self):
        self.assertEqual(
            self.names(self.school_level),
            {"Sciences", "Lekki Extra", "Ikeja Extra"},
        )

    def test_the_exclusive_reading_drops_the_shared_rows(self):
        """What a document dataset wants, and a catalogue must not have.

        vs_procurement reads a null branch this way on its own screens, because
        an entity-wide purchase is not one site's to read. Offering both here is
        what stops a second copy of this rule being written over there.
        """
        self.assertEqual(
            self.names(self.lekki_head, inclusive=False), {"Lekki Extra"},
        )

    def test_no_caller_means_no_narrowing_rather_than_no_rows(self):
        """A missing person is not a person with no branches.

        Conflating them would silently empty a system-triggered estimate, which
        would read as "this school has nothing" rather than as a bug.
        """
        self.assertEqual(
            self.names(None), {"Sciences", "Lekki Extra", "Ikeja Extra"},
        )


class ScopeContextTests(_Base):
    def test_a_run_carries_the_person_it_executes_as(self):
        """Not whoever happened to click, when the two differ.

        A definition's owner is who a run reads as, and the engine already
        refuses to start if they are no longer active. Branch narrowing has to
        follow the same person or the file would contain rows its owner could
        not have asked for.
        """
        from vs_exports.models import ExportRun

        field_names = {f.name for f in ExportRun._meta.get_fields()}
        self.assertIn("requested_by", field_names)
        self.assertIn("definition", field_names)

    def test_scope_context_defaults_user_to_none(self):
        """So every existing construction site keeps working untouched."""
        scope = ScopeContext(tenant=self.tenant)
        self.assertIsNone(scope.user)

    def test_a_dataset_that_does_not_opt_in_is_unaffected(self):
        """Audit events and configuration have no branch and must not gain one."""
        from schools.vs_academics.models import AcademicSession

        AcademicSession.all_objects.create(
            tenant=self.tenant, name="2026/2027",
            start_date="2026-09-01", end_date="2027-07-31",
        )
        from vs_exports.catalogue import get_dataset

        rows = list(get_dataset("academics.sessions").base(
            ScopeContext(tenant=self.tenant, user=self.lekki_head),
        ))
        self.assertEqual(len(rows), 1)


class DatasetIntegrationTests(_Base):
    """The five academics catalogue datasets, through their real base callables."""

    def rows(self, key, user):
        from vs_exports.catalogue import get_dataset

        return list(get_dataset(key).base(
            ScopeContext(tenant=self.tenant, user=user),
        ))

    def test_the_departments_export_narrows(self):
        mine = {d.name for d in self.rows("academics.departments", self.lekki_head)}
        self.assertEqual(mine, {"Sciences", "Lekki Extra"})
        self.assertNotIn("Ikeja Extra", mine)

    def test_the_classes_export_narrows(self):
        from schools.vs_academics.models import Level, Program, SchoolClass

        program = Program.all_objects.create(
            tenant=self.tenant, name="Junior Secondary", code="JSS",
        )
        level = Level.all_objects.create(
            tenant=self.tenant, program=program, name="JSS1", code="JSS1",
            order_index=1,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, level=level, name="JSS1 A", code="JSS1-A",
            branch=self.lekki,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, level=level, name="JSS1 B", code="JSS1-B",
            branch=self.ikeja,
        )
        SchoolClass.all_objects.create(
            tenant=self.tenant, level=level, name="JSS1 C", code="JSS1-C",
        )
        mine = {c.name for c in self.rows("academics.classes", self.lekki_head)}
        self.assertEqual(mine, {"JSS1 A", "JSS1 C"})

    def test_a_school_level_caller_still_exports_the_whole_school(self):
        """Narrowing must not become a restriction on the people who need it least."""
        everything = {
            d.name for d in self.rows("academics.departments", self.school_level)
        }
        self.assertEqual(
            everything, {"Sciences", "Lekki Extra", "Ikeja Extra"},
        )
