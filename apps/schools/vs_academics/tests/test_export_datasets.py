"""The Export Centre datasets behind the five Export buttons.

Two things are worth pinning: that every dataset is fenced to one school, and
that each is gated by the same key as the list it mirrors. An export that
answered a wider question than the screen would be a way around a permission
rather than a convenience.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase

from vs_exports.catalogue import get_dataset
from vs_rbac.tests.helpers import make_branch, make_school
from schools.vs_academics.models import (
    AcademicSession, Department, Level, Program, SchoolClass,
)

KEYS = (
    "academics.sessions", "academics.departments", "academics.programs",
    "academics.levels", "academics.classes", "academics.subjects",
)


class _Scope:
    """The minimum a dataset's ``base`` callable reads.

    ``user`` carries the person the export runs as, which is what lets a
    dataset narrow rows the way the screens do.
    """

    def __init__(self, tenant, user=None):
        self.tenant = tenant
        self.entity = None
        self.user = user


class DatasetRegistrationTests(TestCase):
    def test_every_dataset_is_registered(self):
        for key in KEYS:
            self.assertIsNotNone(get_dataset(key), f"{key} is not in the catalogue")

    def test_each_is_gated_by_the_key_of_the_list_it_mirrors(self):
        """An export must not be a way around a permission."""
        expected = {
            "academics.sessions": "academics.session.view",
            "academics.departments": "academics.structure.view",
            "academics.programs": "academics.structure.view",
            "academics.levels": "academics.structure.view",
            "academics.classes": "academics.classes.view",
            "academics.subjects": "academics.subject.view",
        }
        for key, permission in expected.items():
            self.assertEqual(get_dataset(key).permission, permission, key)

    def test_no_dataset_exposes_a_field_the_serializers_withhold(self):
        """No created_by, no raw metadata, no user identity anywhere."""
        banned = ("created_by", "metadata", "email", "password", "tenant_id")
        for key in KEYS:
            for field in get_dataset(key).fields:
                for bad in banned:
                    self.assertNotIn(
                        bad, field.path,
                        f"{key} exposes {field.path}",
                    )


class DatasetFencingTests(TestCase):
    """Every dataset here is tenant-fenced, unlike the platform register."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.other = make_school(slug="sunrise", name="Sunrise Academy")
        make_branch(cls.school, name="Lekki Campus", is_main=True)
        make_branch(cls.other, name="Main", is_main=True)

        for tenant, tag in ((cls.school.tenant, "BF"), (cls.other.tenant, "SR")):
            year = AcademicSession.all_objects.create(
                tenant=tenant, name="2099/2100",
                start_date=dt.date(2099, 9, 1), end_date=dt.date(2100, 7, 31),
                status="ACTIVE",
            )
            Department.all_objects.create(
                tenant=tenant, name=f"{tag} Sciences", code=f"{tag}SCI",
            )
            program = Program.all_objects.create(
                tenant=tenant, name=f"{tag} Junior", code=f"{tag}JSS",
            )
            level = Level.all_objects.create(
                tenant=tenant, session=year, program=program, name=f"{tag}JSS1",
                code=f"{tag}JSS1", order_index=1,
            )
            SchoolClass.all_objects.create(
                tenant=tenant, session=year, level=level, name=f"{tag}JSS1 A", code=f"{tag}JSS1-A",
            )

    def rows(self, key, tenant):
        return list(get_dataset(key).base(_Scope(tenant)))

    def test_no_dataset_returns_another_schools_rows(self):
        for key in ("academics.departments", "academics.programs",
                    "academics.levels", "academics.classes"):
            rows = self.rows(key, self.school.tenant)
            self.assertTrue(rows, f"{key} returned nothing to check")
            for row in rows:
                self.assertEqual(
                    row.tenant_id, self.school.tenant.id,
                    f"{key} leaked a row from another school",
                )

    def test_each_school_sees_only_its_own(self):
        mine = self.rows("academics.departments", self.school.tenant)
        theirs = self.rows("academics.departments", self.other.tenant)
        self.assertEqual([d.name for d in mine], ["BF Sciences"])
        self.assertEqual([d.name for d in theirs], ["SR Sciences"])

    def test_the_base_uses_all_objects_rather_than_the_ambient_tenant(self):
        """The explicit filter is the boundary, not request-local state.

        An export runs outside a request, so a base that relied on the ambient
        tenant contextvar would return everything or nothing depending on how
        it was invoked. Asserted by reading two tenants in one process.
        """
        self.assertEqual(len(self.rows("academics.classes", self.school.tenant)), 1)
        self.assertEqual(len(self.rows("academics.classes", self.other.tenant)), 1)


class DatasetBranchNarrowingTests(TestCase):
    """The catalogue reading: shared rows plus the caller's own.

    Inclusive, because a curriculum mostly is not one branch's. The exclusive
    reading here would hand a branch admin a nearly empty file whenever the
    school publishes at school level, which is the normal case - the defect
    vs_workflow already found and fixed once.

    These live here rather than in vs_exports because the engine may not import
    a schools app, and because this is where somebody changing the reading will
    look.
    """

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.models import PermissionScope
        from vs_rbac.tests.helpers import (
            make_assignment, make_permission, make_role, make_role_permission,
            make_school_admin,
        )

        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        role = make_role(cls.school, name="Branch Admin", key="branch_admin")
        make_role_permission(
            role,
            make_permission("academics.structure.view", scope=PermissionScope.TENANT),
        )
        cls.lekki_head = make_school_admin(
            None, email="head@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_head, role, branch=cls.lekki)
        cls.school_level = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.school_level, role, branch=None)

    def setUp(self):
        Department.all_objects.create(
            tenant=self.tenant, name="Sciences", code="SCI",
        )
        Department.all_objects.create(
            tenant=self.tenant, name="Lekki Extra", code="LEX", branch=self.lekki,
        )
        Department.all_objects.create(
            tenant=self.tenant, name="Ikeja Extra", code="IEX", branch=self.ikeja,
        )

    def names(self, key, user):
        return {
            row.name for row in get_dataset(key).base(
                _Scope(self.tenant, user=user),
            )
        }

    def test_a_branch_admin_exports_the_shared_rows_plus_their_own(self):
        self.assertEqual(
            self.names("academics.departments", self.lekki_head),
            {"Sciences", "Lekki Extra"},
        )

    def test_a_branch_admin_never_exports_another_branchs_rows(self):
        self.assertNotIn(
            "Ikeja Extra", self.names("academics.departments", self.lekki_head),
        )

    def test_a_school_level_caller_still_exports_the_whole_school(self):
        """Narrowing must not restrict the people it was never about."""
        self.assertEqual(
            self.names("academics.departments", self.school_level),
            {"Sciences", "Lekki Extra", "Ikeja Extra"},
        )

    def test_sessions_are_not_narrowed(self):
        """A year applies to a named SET of branches, not through a column.

        The helper does not fit a join table, so this is left rather than
        guessed at. Asserted so the omission is visible and deliberate.
        """
        from schools.vs_academics.models import AcademicSession

        AcademicSession.all_objects.create(
            tenant=self.tenant, name="2026/2027",
            start_date="2026-09-01", end_date="2027-07-31",
        )
        rows = list(get_dataset("academics.sessions").base(
            _Scope(self.tenant, user=self.lekki_head),
        ))
        self.assertEqual(len(rows), 1)

    def test_every_catalogue_dataset_narrows(self):
        """Enumerated, so a dataset added later is caught here."""
        import inspect

        from schools.vs_academics import export_datasets as ex

        for name in ("_departments", "_programs", "_levels", "_classes", "_subjects"):
            source = inspect.getsource(getattr(ex, name))
            self.assertIn(
                "narrow_to_caller_branches", source,
                f"{name} does not narrow by branch",
            )
