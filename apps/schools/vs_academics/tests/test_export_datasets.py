"""The Export Centre datasets behind the five Export buttons.

Two things are worth pinning: that every dataset is fenced to one school, and
that each is gated by the same key as the list it mirrors. An export that
answered a wider question than the screen would be a way around a permission
rather than a convenience.
"""
from __future__ import annotations

from django.test import TestCase

from vs_exports.catalogue import get_dataset
from vs_rbac.tests.helpers import make_branch, make_school
from schools.vs_academics.models import Department, Level, Program, SchoolClass

KEYS = (
    "academics.sessions", "academics.departments", "academics.programs",
    "academics.levels", "academics.classes", "academics.subjects",
)


class _Scope:
    """The minimum a dataset's ``base`` callable reads."""

    def __init__(self, tenant):
        self.tenant = tenant
        self.entity = None


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
            Department.all_objects.create(
                tenant=tenant, name=f"{tag} Sciences", code=f"{tag}SCI",
            )
            program = Program.all_objects.create(
                tenant=tenant, name=f"{tag} Junior", code=f"{tag}JSS",
            )
            level = Level.all_objects.create(
                tenant=tenant, program=program, name=f"{tag}JSS1",
                code=f"{tag}JSS1", order_index=1,
            )
            SchoolClass.all_objects.create(
                tenant=tenant, level=level, name=f"{tag}JSS1 A", code=f"{tag}JSS1-A",
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
