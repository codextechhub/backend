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

    def test_full_hr_profile_and_assignment_history_remain_permissioned(self):
        urls = (
            f"/v1/user/platform-staff-profiles/{self.profile.id}/",
            "/v1/user/organogram/assignments/?page_size=100",
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403, response.content)

        self._grant("platform.staff_profile.view")

        for url in urls:
            with self.subTest(granted_url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, response.content)

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
