"""``GET /v1/tenants/branches/`` - the branches the caller may name.

Why this endpoint exists at all: the only branch list the API offered was
``GET /v1/i/<slug>/branches/``, keyed by school slug and gated on
``platform.branches.view``. A school's own bursar holds neither, so the person
who assigns staff to sites could not read the sites, and the payroll roster had
to build its picker from branches that already had somebody on them - never the
new site nobody has been assigned to yet, which is exactly the one she is
filling.

So the assertions here are not "the right rows came back". They are **"the list
and the write path agree"**: every branch this endpoint offers must be one
``raised_branch`` would accept, and every branch it withholds must be one
``raised_branch`` would refuse. A picker that drifts from the write path offers
a choice the save then rejects, which reads to a bursar as the system losing her
work.
"""
from __future__ import annotations

from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_role,
    make_school,
    make_school_admin,
    make_vision_user,
)

from .models import Branch, BranchStatus

URL = "/v1/tenants/branches/"


class _Fixture:
    """One three-branch school and a rival.

    Three branches rather than two: with only two, a rule that quietly means
    "any branch except the one I asked about" still passes.
    """

    def setUp(self):
        super().setUp()
        self.school = make_school(slug="corona-group", name="Corona Group")
        self.tenant = self.school.tenant
        self.ikeja = make_branch(self.school, name="Ikeja")
        self.lekki = make_branch(self.school, name="Lekki", is_main=False)
        self.yaba = make_branch(self.school, name="Yaba", is_main=False)

        self.rival_school = make_school(slug="rival-group", name="Rival Group")
        self.rival_tenant = self.rival_school.tenant
        self.rival_branch = make_branch(self.rival_school, name="Ikeja")

    def names_for(self, user, expect=200):
        client = TenantAPIClient(user=user)
        response = client.get(URL)
        self.assertEqual(response.status_code, expect, response.data)
        return sorted(row["name"] for row in response.data["data"])

    def pin(self, user, branch, role_key):
        """Pin a caller to one branch through a real branch-scoped grant."""
        role = make_role(self.tenant, name=role_key)
        make_assignment(self.tenant, user, role, branch=branch)


class BranchOptionsTests(_Fixture, TestCase):

    def test_an_unauthenticated_caller_gets_nothing(self):
        response = TenantAPIClient().get(URL)

        self.assertIn(response.status_code, (401, 403), response.data)

    def test_a_whole_tenant_caller_sees_every_site(self):
        """No RBAC key is required. The list is a projection of grants already
        held, not a capability of its own."""
        bursar = make_school_admin(None, email="bursar@corona.test", tenant=self.tenant)

        self.assertEqual(self.names_for(bursar), ["Ikeja", "Lekki", "Yaba"])

    def test_the_main_branch_comes_first(self):
        """Ordering is part of the contract: a picker's default should be the
        site most people mean, and everything after it alphabetical."""
        bursar = make_school_admin(None, email="order@corona.test", tenant=self.tenant)
        client = TenantAPIClient(user=bursar)

        rows = client.get(URL).data["data"]

        self.assertEqual([r["name"] for r in rows], ["Ikeja", "Lekki", "Yaba"])
        self.assertTrue(rows[0]["is_main"])

    def test_a_pinned_caller_sees_only_her_own_branch(self):
        officer = make_school_admin(None, email="lekki@corona.test", tenant=self.tenant)
        self.pin(officer, self.lekki, "lekki-payroll")

        self.assertEqual(self.names_for(officer), ["Lekki"])

    def test_a_caller_pinned_to_two_branches_sees_both(self):
        """One person holding the same role at two sites is two assignments,
        which is the shape the ambiguous case actually arrives in."""
        officer = make_school_admin(None, email="both@corona.test", tenant=self.tenant)
        self.pin(officer, self.ikeja, "ikeja-payroll")
        self.pin(officer, self.yaba, "yaba-payroll")

        self.assertEqual(self.names_for(officer), ["Ikeja", "Yaba"])

    def test_a_home_posting_pins_a_caller_with_no_grants(self):
        """The fallback that predates branch grants: somebody's own branch still
        decides when nothing else speaks for them."""
        teacher = make_school_admin(
            self.yaba, email="posted@corona.test", tenant=self.tenant,
        )

        self.assertEqual(self.names_for(teacher), ["Yaba"])

    # ── the boundary ────────────────────────────────────────────────────────

    def test_another_tenants_branches_are_never_listed(self):
        """The rival has a branch called Ikeja too, so a leak would be legible
        rather than obvious."""
        bursar = make_school_admin(None, email="rival@rival.test", tenant=self.rival_tenant)

        self.assertEqual(self.names_for(bursar), ["Ikeja"])

    def test_asserting_a_tenant_that_is_not_yours_never_reaches_the_view(self):
        """Not even for platform staff.

        This route does not set ``platform_cross_tenant_param``, and neither do
        the finance views it was built for - a CodeX admin works on ledger
        entities inside its own tenant, never by asserting a school's slug. The
        refusal therefore lands in authentication, before any branch is read,
        and that is the boundary worth pinning: the view's own cross-tenant
        guard is a fail-closed backstop for a future view that opts in, exactly
        as ``raised_branch`` keeps its own unreachable one.
        """
        vision = make_vision_user(email="platform-branches@example.com", super_admin=True)
        officer = make_school_admin(None, email="cross@corona.test", tenant=self.tenant)
        self.pin(officer, self.ikeja, "cross-payroll")

        for who, label in ((vision, "platform staff"), (officer, "a pinned officer")):
            with self.subTest(caller=label):
                client = TenantAPIClient(user=who, tenant_slug=self.rival_school.slug)

                self.assertEqual(client.get(URL).status_code, 404)

    # ── liveness ────────────────────────────────────────────────────────────

    def test_a_closed_branch_is_not_offered(self):
        """``_grant_scope`` already drops out-of-service branches for a pinned
        caller. Listing them for a whole-tenant one would make the same shut
        site pickable by the bursar and invisible to the manager who stood in
        it."""
        self.yaba.status = BranchStatus.CLOSED
        self.yaba.save(update_fields=["status"])
        bursar = make_school_admin(None, email="closed@corona.test", tenant=self.tenant)

        self.assertEqual(self.names_for(bursar), ["Ikeja", "Lekki"])

    def test_a_suspended_branch_is_not_offered(self):
        self.lekki.status = BranchStatus.SUSPENDED
        self.lekki.save(update_fields=["status"])
        bursar = make_school_admin(None, email="susp@corona.test", tenant=self.tenant)

        self.assertNotIn("Lekki", self.names_for(bursar))

    def test_out_of_service_is_read_from_the_model_not_a_local_list(self):
        """Guards the constant, not the two statuses. A status added to
        ``OUT_OF_SERVICE_STATES`` later must stop being offered here in the same
        breath, without anybody remembering this file exists."""
        for status in Branch.OUT_OF_SERVICE_STATES:
            with self.subTest(status=status):
                self.ikeja.status = status
                self.ikeja.save(update_fields=["status"])
                bursar = make_school_admin(
                    None, email=f"oos-{status.lower()}@corona.test", tenant=self.tenant,
                )

                self.assertNotIn("Ikeja", self.names_for(bursar))

    # ── the shape ───────────────────────────────────────────────────────────

    def test_the_row_carries_what_a_picker_needs_and_no_more(self):
        """Narrower than the School Management list on purpose: a payroll screen
        must not become a second, unaudited window onto a school's estate."""
        bursar = make_school_admin(None, email="shape@corona.test", tenant=self.tenant)
        client = TenantAPIClient(user=bursar)

        row = client.get(URL).data["data"][0]

        self.assertEqual(
            set(row), {"id", "name", "code", "is_main", "status"},
        )
