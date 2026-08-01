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
