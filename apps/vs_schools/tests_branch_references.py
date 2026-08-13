"""The shared branch-reference resolver is a tenant boundary, so test it directly.

``vs_schools.services.references.find_branch_in_tenant`` is the single place
``vs_config``, ``vs_procurement`` and ``vs_user`` resolve a caller-supplied
branch id. Phase C changed the filter it applies from ``school__tenant`` to the
branch's own ``tenant`` column. Both express the same set of rows, but only one
of them is still a filter if it is written wrongly, so the boundary is asserted
here rather than only through the three callers.

Two shapes of tenant on purpose, per the multi-tenancy rule: one school with
several branches and one with none at all.
"""
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from vs_schools.models import Branch, School
from vs_schools.services.references import (
    BRANCH_NOT_FOUND,
    find_branch_in_tenant,
    resolve_branch_reference,
)


class FindBranchInTenantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branched = School.objects.create(name="Branched", slug="ref-branched")
        cls.hq = Branch.objects.create(school=cls.branched, name="HQ", is_main=True)
        cls.lekki = Branch.objects.create(school=cls.branched, name="Lekki")

        # A second tenant that owns a branch of its own: the row every check
        # below must refuse to hand to the first tenant.
        cls.rival = School.objects.create(name="Rival", slug="ref-rival")
        cls.rival_branch = Branch.objects.create(school=cls.rival, name="Rival Main", is_main=True)

        # A tenant with no branches at all - the dimension must simply recede.
        cls.branchless = School.objects.create(name="Branchless", slug="ref-branchless")

    # --- the boundary itself -------------------------------------------------

    def test_a_branch_in_the_tenant_resolves(self):
        self.assertEqual(find_branch_in_tenant(self.branched.tenant, self.lekki.pk), self.lekki)

    def test_a_branch_owned_by_another_tenant_never_resolves(self):
        """The rewrite must still deny. This is the assertion that catches a
        filter accidentally turned into a no-op: with the tenant condition
        dropped, the row exists and would be returned."""
        self.assertIsNone(find_branch_in_tenant(self.branched.tenant, self.rival_branch.pk))

    def test_a_foreign_branch_is_indistinguishable_from_an_absent_one(self):
        absent = find_branch_in_tenant(self.branched.tenant, self.rival_branch.pk + 10_000)
        foreign = find_branch_in_tenant(self.branched.tenant, self.rival_branch.pk)
        self.assertEqual(foreign, absent)

    def test_a_branchless_tenant_cannot_borrow_another_tenants_branch(self):
        self.assertIsNone(find_branch_in_tenant(self.branchless.tenant, self.hq.pk))
        self.assertIsNone(find_branch_in_tenant(self.branchless.tenant, self.rival_branch.pk))

    def test_the_reverse_direction_is_refused_too(self):
        # Not symmetric by accident: assert both ways so a rewrite that keyed
        # the filter off the wrong side of the comparison is caught.
        self.assertIsNone(find_branch_in_tenant(self.rival.tenant, self.hq.pk))

    # --- the non-boundary answers, unchanged by the rewrite ------------------

    def test_a_blank_or_absent_reference_is_none_not_an_error(self):
        for ref in (None, ""):
            with self.subTest(ref=ref):
                self.assertIsNone(find_branch_in_tenant(self.branched.tenant, ref))

    def test_no_tenant_resolves_nothing(self):
        self.assertIsNone(find_branch_in_tenant(None, self.hq.pk))

    def test_an_unusable_id_is_a_miss_not_a_database_error(self):
        for bad in ("not-an-id", "9" * 40, "3f1b2c4d-0000-4000-8000-000000000001", "-1"):
            with self.subTest(ref=bad):
                self.assertIsNone(find_branch_in_tenant(self.branched.tenant, bad))


class ResolveBranchReferenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Resolve", slug="ref-resolve")
        cls.branch = Branch.objects.create(school=cls.school, name="Main", is_main=True)
        cls.rival = School.objects.create(name="Resolve Rival", slug="ref-resolve-rival")
        cls.rival_branch = Branch.objects.create(school=cls.rival, name="Main", is_main=True)

    def test_a_foreign_branch_raises_the_same_error_an_unknown_one_does(self):
        with self.assertRaises(ValidationError) as foreign:
            resolve_branch_reference(self.school.tenant, self.rival_branch.pk)
        with self.assertRaises(ValidationError) as unknown:
            resolve_branch_reference(self.school.tenant, self.rival_branch.pk + 10_000)

        self.assertEqual(foreign.exception.detail, unknown.exception.detail)
        self.assertIn(BRANCH_NOT_FOUND, str(foreign.exception.detail["branch"]))

    def test_an_own_branch_resolves(self):
        self.assertEqual(resolve_branch_reference(self.school.tenant, self.branch.pk), self.branch)

    def test_a_blank_reference_means_no_branch_rather_than_an_error(self):
        self.assertIsNone(resolve_branch_reference(self.school.tenant, ""))
