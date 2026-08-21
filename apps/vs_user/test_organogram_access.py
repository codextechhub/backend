"""Access contract for the employee-facing organogram."""

from django.test import TestCase
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_branch,
    make_school_admin,
    make_vision_user,
)
from vs_user.models import (
    MatrixReport,
    OrgNode,
    PlatformStaffProfile,
    Position,
    PositionAssignment,
)
from vs_user.views.organogram import PositionAssignmentViewSet


class OrganogramAccessTests(TestCase):
    def setUp(self):
        self.viewer = make_vision_user(email="ordinary.organogram@codex.test")
        self.manager = make_vision_user(
            email="manager.organogram@codex.test",
            first_name="Ada",
            last_name="Lovelace",
        )
        self.node = OrgNode.objects.create(
            name="Organogram Access", code="ORG-ACCESS", kind=OrgNode.Kind.DIVISION,
        )
        self.manager_position = Position.objects.create(
            title="Access Director", code="ACCESS-DIR", org_node=self.node,
        )
        self.viewer_position = Position.objects.create(
            title="Access Analyst", code="ACCESS-AN", org_node=self.node,
            reports_to=self.manager_position,
        )
        PositionAssignment.objects.create(
            user=self.manager, position=self.manager_position, is_primary=True,
        )
        PositionAssignment.objects.create(
            user=self.viewer, position=self.viewer_position,
            is_primary=True, is_acting=True,
        )
        MatrixReport.objects.create(
            position=self.viewer_position,
            reports_to=self.manager_position,
            relationship_label="Project",
        )
        self.profile = PlatformStaffProfile.objects.create(
            user=self.manager,
            employee_id="CX-ORG-1",
            job_title="Access Director",
            position=self.manager_position,
            personal_email="private@example.test",
            nok_name="Private Relative",
            bank_name="Private Bank",
        )
        self.viewer_profile = PlatformStaffProfile.objects.create(
            user=self.viewer,
            employee_id="CX-ORG-2",
            job_title="Access Analyst",
            position=self.viewer_position,
            personal_email="owner-private@example.test",
            nok_name="Owner Relative",
            bank_name="Owner Bank",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.viewer)

    def _grant(self, key):
        role = make_role(self.viewer.tenant, name=f"Role for {key}")
        make_role_permission(role, make_permission(key))
        make_assignment(self.viewer.tenant, self.viewer, role)
        # Permission evaluation memoises on the request user instance. This
        # test grants between two requests, so discard the earlier denied set.
        if hasattr(self.viewer, "_rbac_effective_perms"):
            del self.viewer._rbac_effective_perms

    def test_active_platform_employee_can_read_chart_without_rbac_permission(self):
        urls = (
            "/v1/user/organogram/nodes/?page_size=100",
            "/v1/user/organogram/positions/?page_size=100",
            "/v1/user/organogram/positions/tree/",
            "/v1/user/organogram/matrix-reports/?page_size=100",
            "/v1/user/platform-staff-profiles/?page_size=100",
            "/v1/user/organogram/assignments/current/",
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, response.content)

    def test_public_profile_list_contains_only_chart_safe_fields(self):
        response = self.client.get("/v1/user/platform-staff-profiles/?page_size=100")

        self.assertEqual(response.status_code, 200, response.content)
        profile = next(item for item in response.json()["data"] if item["id"] == self.profile.id)
        self.assertEqual(profile["employee_id"], "CX-ORG-1")
        self.assertNotIn("personal_email", profile)
        self.assertNotIn("nok_name", profile)
        self.assertNotIn("bank_name", profile)

    def test_staff_search_and_profile_still_require_authentication(self):
        self.client.force_authenticate(user=None)

        search = self.client.get("/v1/user/platform-staff-profiles/?search=Ada")
        detail = self.client.get(
            f"/v1/user/platform-staff-profiles/{self.profile.id}/",
        )

        self.assertEqual(search.status_code, 401, search.content)
        self.assertEqual(detail.status_code, 401, detail.content)

    def test_profile_search_matches_a_full_name_across_name_fields(self):
        response = self.client.get(
            "/v1/user/platform-staff-profiles/?search=Ada%20Lovelace&page_size=10",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [item["id"] for item in response.json()["data"]],
            [self.profile.id],
        )

    def test_current_assignments_exclude_history_fields(self):
        response = self.client.get("/v1/user/organogram/assignments/current/")

        self.assertEqual(response.status_code, 200, response.content)
        acting = next(
            item for item in response.json()["data"]
            if str(item["user"]["id"]) == str(self.viewer.id)
        )
        self.assertEqual(set(acting), {"user", "position", "is_acting"})
        self.assertTrue(acting["is_acting"])

    def test_colleague_profile_is_brief_without_hr_permission(self):
        response = self.client.get(
            f"/v1/user/platform-staff-profiles/{self.profile.id}/",
        )

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["profile_view"], "brief")
        self.assertEqual(data["user"]["full_name"], "Ada Lovelace")
        for private_field in (
            "date_of_birth", "personal_email", "residential_address",
            "nok_name", "bank_name", "account_name", "account_number",
            "date_joined", "date_exited", "_stripped_fields",
        ):
            self.assertNotIn(private_field, data)

    def test_owner_can_retrieve_their_full_profile_without_hr_permission(self):
        response = self.client.get(
            f"/v1/user/platform-staff-profiles/{self.viewer_profile.id}/",
        )

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["profile_view"], "full")
        self.assertEqual(data["personal_email"], "owner-private@example.test")
        self.assertEqual(data["nok_name"], "Owner Relative")
        self.assertEqual(data["bank_name"], "Owner Bank")

    def test_hr_permission_unlocks_full_profile_but_not_payroll(self):
        profile_url = f"/v1/user/platform-staff-profiles/{self.profile.id}/"
        assignments_url = "/v1/user/organogram/assignments/?page_size=100"

        self.assertEqual(self.client.get(assignments_url).status_code, 403)

        self._grant("platform.staff_profile.view")

        response = self.client.get(profile_url)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["profile_view"], "full")
        self.assertEqual(data["personal_email"], "private@example.test")
        self.assertEqual(data["nok_name"], "Private Relative")
        self.assertNotIn("bank_name", data)
        self.assertIn("bank_name", data["_stripped_fields"])

        self.assertEqual(self.client.get(assignments_url).status_code, 200)

    def test_summary_vacancies_remain_permissioned(self):
        url = "/v1/user/organogram/positions/vacancies/"
        self.assertEqual(self.client.get(url).status_code, 403)

        self._grant("platform.organogram.view")

        self.assertEqual(self.client.get(url).status_code, 200)

    def test_structure_writes_still_require_manage_permission(self):
        response = self.client.post(
            "/v1/user/organogram/positions/",
            {
                "title": "Unauthorised Seat",
                "code": "NO-WRITE",
                "org_node_id": self.node.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.content)

    def test_school_user_cannot_read_platform_organogram(self):
        school = make_school(slug="org-access-school", name="Org Access School")
        branch = make_branch(school)
        school_user = make_school_admin(branch, email="school.organogram@example.test")
        self.client.force_authenticate(user=school_user)

        response = self.client.get("/v1/user/organogram/positions/tree/")

        self.assertEqual(response.status_code, 403, response.content)

    def test_staff_search_and_profile_are_scoped_to_current_tenant(self):
        self.profile.profile_photo = "platform_staff/photos/ada.png"
        self.profile.save(update_fields=["profile_photo"])
        school = make_school(slug="profile-scope-school", name="Profile Scope School")
        branch = make_branch(school)
        school_user = make_school_admin(branch, email="profile.scope@example.test")
        self.client.force_authenticate(user=school_user)

        search = self.client.get(
            "/v1/user/platform-staff-profiles/?search=Ada&page_size=10",
        )
        detail = self.client.get(
            f"/v1/user/platform-staff-profiles/{self.profile.id}/",
        )
        photos = self.client.get("/v1/user/platform-staff-profiles/photos/")

        self.assertEqual(search.status_code, 200, search.content)
        self.assertEqual(search.json()["data"], [])
        self.assertEqual(detail.status_code, 404, detail.content)
        self.assertEqual(photos.status_code, 200, photos.content)
        self.assertEqual(photos.json()["data"], {})


class _StubRequest:
    """The three attributes ``get_queryset`` reads, and nothing else.

    The query arm has to be provable on its own, without the permission gate
    in front of it - that is the whole point of putting a second answer behind
    the first - so these tests reach the viewset directly rather than through a
    URL that would refuse them at the door.
    """

    def __init__(self, caller, tenant=None, **params):
        self.user = caller
        self.tenant = tenant if tenant is not None else caller.tenant
        self.query_params = params


class OrganogramTenantIsolationTests(TestCase):
    """The organogram is CX-internal, asserted twice over.

    ``PositionAssignmentViewSet`` used to serve
    ``PositionAssignment.objects.all()`` and honour a raw ``?user=`` on top of
    it, holding nothing back but the scope of the RBAC key. These tests hold
    the gate and the queryset to account separately.
    """

    def setUp(self):
        self.client = APIClient()

        # The CX chart.
        self.node = OrgNode.objects.create(
            name="Isolation Division", code="ISO-DV", kind=OrgNode.Kind.DIVISION,
        )
        self.cx_position = Position.objects.create(
            title="Isolation Director", code="ISO-DIR", org_node=self.node,
        )
        self.cx_staff = make_vision_user(
            email="cx.isolation@codex.test", first_name="Chidera", last_name="Okoro",
        )
        self.cx_assignment = PositionAssignment.objects.create(
            user=self.cx_staff, position=self.cx_position, is_primary=True,
        )

        # Bright Star: one branch, the common shape.
        self.bright_star = make_school(slug="bright-star-iso", name="Bright Star School")
        self.bright_star_main = make_branch(self.bright_star)
        self.amaka = make_school_admin(
            self.bright_star_main, email="amaka@brightstar.test",
        )

        # Greenfield: three branches, so nothing here can be true only of a
        # school that happens to have one site.
        self.greenfield = make_school(slug="greenfield-iso", name="Greenfield School")
        self.greenfield_main = make_branch(self.greenfield)
        self.greenfield_lekki = make_branch(
            self.greenfield, name="Lekki", is_main=False,
        )
        self.greenfield_ikeja = make_branch(
            self.greenfield, name="Ikeja", is_main=False,
        )
        self.tunde = make_school_admin(
            self.greenfield_ikeja, email="tunde@greenfield.test",
        )

        # Rows that the model forbids and that therefore cannot arrive through
        # the API: ``PositionAssignment.clean`` refuses a non-platform user, and
        # so does OrganogramService. They are written straight to the table on
        # purpose - without them the tenant clause would pass by having nothing
        # to exclude, which proves nothing about whether it is applied.
        self.bright_star_row = PositionAssignment.objects.create(
            user=self.amaka, position=self.cx_position, is_primary=True,
        )
        self.greenfield_row = PositionAssignment.objects.create(
            user=self.tunde, position=self.cx_position, is_primary=False,
        )

    # ── the queryset ─────────────────────────────────────────────────────────

    def _assignments_for(self, caller, **params):
        view = PositionAssignmentViewSet()
        view.request = _StubRequest(caller, **params)
        return list(view.get_queryset())

    def test_platform_caller_reads_the_whole_table(self):
        self.assertCountEqual(
            self._assignments_for(self.cx_staff),
            [self.cx_assignment, self.bright_star_row, self.greenfield_row],
        )

    def test_single_branch_school_sees_only_its_own_rows(self):
        self.assertEqual(
            self._assignments_for(self.amaka), [self.bright_star_row],
        )

    def test_multi_branch_school_sees_only_its_own_rows(self):
        # Tunde is posted to Ikeja, one of Greenfield's three sites. Branch is
        # not the dimension here - a seat in the chart is the tenant's, not a
        # site's - so he sees his tenant's row and neither of the others.
        self.assertEqual(
            self._assignments_for(self.tunde), [self.greenfield_row],
        )

    def test_user_filter_cannot_reach_outside_the_callers_tenant(self):
        # Amaka names Chidera's id. The filter used to be the first thing
        # applied to an unbounded table, so it selected his whole tenure
        # history; it now picks from rows she was already entitled to.
        self.assertEqual(
            self._assignments_for(self.amaka, user=str(self.cx_staff.id)), [],
        )
        self.assertEqual(
            self._assignments_for(self.tunde, user=str(self.amaka.id)), [],
        )

    def test_user_filter_still_narrows_for_a_platform_caller(self):
        self.assertEqual(
            self._assignments_for(self.cx_staff, user=str(self.cx_staff.id)),
            [self.cx_assignment],
        )

    def test_a_malformed_id_is_an_empty_page_not_a_server_error(self):
        # ``filter(user_id='account-lockouts')`` raises ValueError inside the
        # ORM, which DRF renders as a 500 - the defect 677b469 found on the
        # account routes. Nothing has that id, so the answer is no rows.
        self.assertEqual(
            self._assignments_for(self.cx_staff, user="account-lockouts"), [],
        )
        self.assertEqual(
            self._assignments_for(self.cx_staff, position="not-an-id"), [],
        )

    # ── the gate ─────────────────────────────────────────────────────────────

    def _escalate(self, user):
        """Give *user* the RBAC bypass, inside their own school tenant.

        This used to BE the escalation: ``is_vision_super_admin`` asked only for
        an ACTIVE assignment to a role keyed ``xvs_super_admin`` in the caller's
        own tenant, and ``HasRBACPermission`` returns True for such a caller
        before it looks at any key - so the scope guard added in ad41a03 was
        never consulted, because no permission row was ever read.

        That check now also requires the role's tenant to be PLATFORM-kind, so
        this grants nothing. It is kept because "holds the key by another route"
        is exactly the caller these tests must refuse, and a future change that
        reopens the bypass should fail here rather than pass quietly.
        """
        role = make_role(user.tenant, name="Superuser", key="xvs_super_admin")
        make_assignment(user.tenant, user, role)
        for cached in ("_is_xvs_super_admin", "_rbac_effective_perms"):
            if hasattr(user, cached):
                delattr(user, cached)
        return role

    def test_an_escalated_school_admin_is_still_refused_every_surface(self):
        self._escalate(self.amaka)
        self.client.force_authenticate(user=self.amaka)

        for url in (
            "/v1/user/organogram/assignments/?page_size=100",
            f"/v1/user/organogram/assignments/{self.cx_assignment.id}/",
            "/v1/user/organogram/assignments/current/",
            "/v1/user/organogram/positions/vacancies/",
            "/v1/user/organogram/positions/tree/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

        # The staff-profile list is the deliberate exception: it stays open to
        # any authenticated caller and answers the question with its queryset
        # instead, so she gets a page rather than a refusal - an empty one.
        profiles = self.client.get("/v1/user/platform-staff-profiles/")
        self.assertEqual(profiles.status_code, 200, profiles.content)
        self.assertEqual(profiles.json()["data"], [])

    def test_an_escalated_school_admin_cannot_write_the_cx_chart(self):
        self._escalate(self.tunde)
        self.client.force_authenticate(user=self.tunde)

        writes = (
            ("/v1/user/organogram/nodes/",
             {"name": "Forged", "code": "FORGED-DV", "kind": "DIVISION"}),
            ("/v1/user/organogram/positions/",
             {"title": "Forged Seat", "code": "FORGED", "org_node_id": self.node.id}),
            ("/v1/user/organogram/assignments/",
             {"user_id": self.cx_staff.id, "position_id": self.cx_position.id}),
            ("/v1/user/organogram/matrix-reports/",
             {"position_id": self.cx_position.id, "reports_to_id": self.cx_position.id}),
            ("/v1/user/platform-staff-profiles/",
             {"user_id": self.cx_staff.id, "employee_id": "CX-FORGED"}),
        )
        for url, payload in writes:
            with self.subTest(url=url):
                response = self.client.post(url, payload, format="json")
                self.assertEqual(response.status_code, 403, response.content)

        self.assertFalse(OrgNode.objects.filter(code="FORGED-DV").exists())
        self.assertFalse(Position.objects.filter(code="FORGED").exists())
        self.assertFalse(
            PlatformStaffProfile.objects.filter(employee_id="CX-FORGED").exists()
        )

    def test_a_school_role_named_xvs_super_admin_confers_nothing(self):
        """The escalation this file was written next to, now closed.

        ``is_vision_super_admin`` short-circuits ``HasRBACPermission`` before it
        looks at any key, so returning True from it bypasses every permission
        gate on the platform. It used to ask only whether the caller held a role
        keyed ``xvs_super_admin`` in their OWN tenant - and a role's key is
        derived from the name whoever created it typed. So a school admin with
        role-create could mint one and hand themselves the platform.

        The guard in ``vs_user.serializers`` refuses that key only when
        ``creating_platform_staff`` is true, so a school user created with a
        school role of that name was never caught by it.
        """
        from vs_rbac.permissions import is_vision_super_admin

        self.assertFalse(is_vision_super_admin(self.amaka))
        self._escalate(self.amaka)
        # Same role key, same tenant, ACTIVE - and it means nothing, because the
        # role does not live on the platform tenant.
        self.assertFalse(is_vision_super_admin(self.amaka))

    def test_a_platform_caller_keeps_the_console(self):
        # The other half of the contract: narrowing must not have cost CX its
        # own chart. The reader holds the read key and nothing else.
        role = make_role(self.cx_staff.tenant, name="Chart Reader")
        make_role_permission(role, make_permission("platform.staff_profile.view"))
        make_assignment(self.cx_staff.tenant, self.cx_staff, role)
        self.client.force_authenticate(user=self.cx_staff)

        listing = self.client.get("/v1/user/organogram/assignments/?page_size=100")
        detail = self.client.get(
            f"/v1/user/organogram/assignments/{self.bright_star_row.id}/",
        )

        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertCountEqual(
            [row["id"] for row in listing.json()["data"]],
            [self.cx_assignment.id, self.bright_star_row.id, self.greenfield_row.id],
        )
        self.assertEqual(detail.status_code, 200, detail.content)
