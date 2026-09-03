"""The branch read endpoints, and the filter that scopes them.

``Branch`` carries no school, so every one of these views selects its rows with
``tenant__school_profile__slug=<slug>`` rather than ``school__slug=<slug>``.
The two express the same set of rows -
``School.tenant`` is a non-nullable OneToOneField - but a filter rewritten
wrongly does not raise, it stops narrowing, and every school would then see
every other school's sites. None of these endpoints had a test before, so the
rewrite would have been silent.

Two shapes of tenant, per the multi-tenancy rule: a school with several
branches and a school with none. Plus a tenant with branches and no school at
all, which is only possible because of this phase and which the payload has to
survive.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import make_branch, make_school, make_vision_user
from vs_tenants.models import Branch, BranchStatus, Tenant

from .serializers import BranchListSerializer


class BranchReadEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vision_user = make_vision_user(
            email="branch-endpoints@example.com", super_admin=True
        )

        cls.school = make_school(slug="ep-multi", name="Endpoint Multi")
        cls.hq = make_branch(cls.school, name="HQ", status=BranchStatus.ACTIVE)
        cls.lekki = make_branch(
            cls.school, name="Lekki", is_main=False, status=BranchStatus.PENDING,
        )

        cls.rival = make_school(slug="ep-rival", name="Endpoint Rival")
        cls.rival_branch = make_branch(
            cls.rival, name="Rival HQ", status=BranchStatus.ACTIVE,
        )

        cls.branchless = make_school(slug="ep-solo", name="Endpoint Solo")

    def _client(self):
        client = APIClient()
        client.force_authenticate(user=self.vision_user)
        return client

    def _rows(self, response):
        """The rows out of the paginated envelope.

        ``success_response`` coerces an empty list to ``{}``, so an empty page
        is not necessarily ``[]``; callers below assert emptiness rather than
        equality with a list.
        """
        data = response.data["data"]
        if isinstance(data, dict):
            return data.get("results", [])
        return data

    # --- the scoping filter ---------------------------------------------------

    def test_the_list_returns_only_this_schools_branches(self):
        response = self._client().get(
            reverse("branch-list", kwargs={"slug": self.school.slug})
        )

        self.assertEqual(response.status_code, 200, response.data)
        names = {row["name"] for row in self._rows(response)}
        self.assertEqual(names, {"HQ", "Lekki"})

    def test_another_schools_branch_never_appears(self):
        """The assertion that catches a filter turned into a no-op: the rival
        row exists, and would come back if the scoping were dropped."""
        response = self._client().get(
            reverse("branch-list", kwargs={"slug": self.school.slug})
        )

        ids = {row["id"] for row in self._rows(response)}
        self.assertNotIn(self.rival_branch.pk, ids)

    def test_a_school_with_no_branches_gets_an_empty_list(self):
        response = self._client().get(
            reverse("branch-list", kwargs={"slug": self.branchless.slug})
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self._rows(response))

    def test_the_search_still_matches_on_the_school(self):
        """``?q=`` searched ``school__name``/``school__slug``; it now goes
        through the tenant. Same field, one more hop."""
        response = self._client().get(
            reverse("branch-list", kwargs={"slug": self.school.slug}),
            {"q": "Endpoint Multi"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(self._rows(response)), 2)

    def test_the_search_does_not_reach_across_schools(self):
        response = self._client().get(
            reverse("branch-list", kwargs={"slug": self.school.slug}),
            {"q": "Endpoint Rival"},
        )

        self.assertFalse(self._rows(response))

    def test_status_filters_still_narrow(self):
        response = self._client().get(
            reverse("branch-list", kwargs={"slug": self.school.slug}),
            {"active": "true"},
        )

        names = {row["name"] for row in self._rows(response)}
        self.assertEqual(names, {"HQ"})

    # --- detail and stats -----------------------------------------------------

    def test_detail_resolves_the_branch_inside_its_own_school(self):
        response = self._client().get(
            reverse(
                "branch-detail",
                kwargs={"slug": self.school.slug, "code": self.hq.code},
            )
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["id"], self.hq.pk)
        self.assertEqual(response.data["data"]["school_slug"], self.school.slug)

    def test_detail_refuses_a_branch_code_belonging_to_another_school(self):
        """Codes are per tenant, so both schools have a branch 1. Without the
        scoping filter this would hand over the rival's row."""
        self.assertEqual(self.hq.code, self.rival_branch.code)

        response = self._client().get(
            reverse(
                "branch-detail",
                kwargs={"slug": self.branchless.slug, "code": self.rival_branch.code},
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_stats_count_only_this_schools_branches(self):
        response = self._client().get(
            reverse("branch-stats", kwargs={"slug": self.school.slug})
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["all"], 2)
        self.assertEqual(response.data["data"]["active"], 1)
        self.assertEqual(response.data["data"]["pending"], 1)

    def test_stats_for_a_school_with_no_branches_are_zero(self):
        response = self._client().get(
            reverse("branch-stats", kwargs={"slug": self.branchless.slug})
        )

        self.assertEqual(response.data["data"]["all"], 0)

    # --- the school_slug field ------------------------------------------------

    def test_the_list_payload_still_carries_the_school_slug(self):
        response = self._client().get(
            reverse("branch-list", kwargs={"slug": self.school.slug})
        )

        for row in self._rows(response):
            self.assertEqual(row["school_slug"], self.school.slug)

    def test_a_branch_whose_tenant_has_no_school_serializes_with_a_null_slug(self):
        """Only possible since the move, and the field must stay a key.

        The obvious spelling, ``source="tenant.school_profile.slug"``, raises
        on the missing reverse one-to-one, and DRF answers that by dropping the
        key from the payload rather than by nulling it.
        """
        plain = Tenant.objects.create(
            name="Plain Org", slug="ep-plain", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        depot = Branch.objects.create(tenant=plain, name="Depot", is_main=True)

        payload = BranchListSerializer(depot).data

        self.assertIn("school_slug", payload)
        self.assertIsNone(payload["school_slug"])
