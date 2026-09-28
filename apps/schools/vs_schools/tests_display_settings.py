"""``/v1/i/me/settings/display/``: the school's time zone.

The same door as the security and payroll settings, so the tests lead the
same way: who is refused, whose value a write can reach, then what a school
admin can do. Two live schools, because a single-branch test proves nothing
about a multi-branch one (Bright Star runs Ikeja and Lekki, Green Field runs
one branch), and one pending school.
"""
from zoneinfo import ZoneInfo

from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_config.clock import TIME_ZONE_KEY, tenant_zone
from vs_config.models import ConfigurationAuditEvent, ConfigurationDefinition
from vs_config.services.resolution import resolve_value, set_value
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
    make_vision_user,
)

from .models import SchoolStatus

DISPLAY_URL = "/v1/i/me/settings/display/"


class _DisplaySettingsFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.definition = ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY)
        cls.view_perm = make_permission("school.settings.view")
        cls.update_perm = make_permission("school.settings.update")
        cls.profile_perm = make_permission("school.profile.view")

        cls.bright = make_school(slug="bright-star", name="Bright Star")
        cls.ikeja = make_branch(cls.bright, name="Ikeja Branch")
        cls.lekki = make_branch(cls.bright, name="Lekki Branch", is_main=False)
        cls.bright_admin = cls._school_admin(cls.bright, cls.ikeja, "admin@bright.example.com")

        branch_role = make_role(cls.bright, name="Branch Admin", key="branch_admin")
        make_role_permission(branch_role, cls.view_perm)
        cls.ikeja_admin = make_school_admin(cls.ikeja, email="ikeja@bright.example.com")
        make_assignment(cls.bright, cls.ikeja_admin, branch_role, branch=cls.ikeja)

        teacher_role = make_role(cls.bright, name="Teacher", key="teacher")
        make_role_permission(teacher_role, cls.profile_perm)
        cls.teacher = make_staff_user(cls.ikeja, email="teacher@bright.example.com")
        make_assignment(cls.bright, cls.teacher, teacher_role, branch=cls.ikeja)

        cls.green = make_school(slug="green-field", name="Green Field")
        cls.green_main = make_branch(cls.green, name="Main Branch")
        cls.green_admin = cls._school_admin(cls.green, cls.green_main, "admin@green.example.com")

        cls.sunrise = make_school(slug="sunrise", name="Sunrise", status=SchoolStatus.PENDING)
        cls.sunrise_main = make_branch(cls.sunrise, name="Main Branch")
        cls.sunrise_admin = cls._school_admin(
            cls.sunrise, cls.sunrise_main, "admin@sunrise.example.com",
        )

    @classmethod
    def _school_admin(cls, school, branch, email):
        role = make_role(school, name="School Admin", key="school_admin")
        make_role_permission(role, cls.view_perm)
        make_role_permission(role, cls.update_perm)
        user = make_school_admin(branch, email=email)
        make_assignment(school, user, role, branch=None)
        return user

    def client_for(self, user, tenant_slug=None):
        return TenantAPIClient(user=user, tenant_slug=tenant_slug)

    def patch(self, user, body):
        return self.client_for(user).patch(
            f"{DISPLAY_URL}?tenant={user.tenant.slug}", body, format="json",
        )

    def stored_zone(self, school):
        value, row = resolve_value(self.definition, tenant=school.tenant)
        return value, row


class DisplaySettingsAccessTests(_DisplaySettingsFixture):
    """Who may reach the time zone, and whose value a write can touch."""

    def test_a_teacher_is_refused_both_verbs(self):
        self.assertEqual(self.client_for(self.teacher).get(DISPLAY_URL).status_code, 403)
        refused = self.patch(self.teacher, {"timezone": "Africa/Nairobi"})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIsNone(self.stored_zone(self.bright)[1])

    def test_a_branch_admin_may_read_but_not_write(self):
        read = self.client_for(self.ikeja_admin).get(DISPLAY_URL)
        self.assertEqual(read.status_code, 200, read.data)
        refused = self.patch(self.ikeja_admin, {"timezone": "Africa/Nairobi"})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIsNone(self.stored_zone(self.bright)[1])

    def test_one_school_saving_never_moves_another_schools_zone(self):
        response = self.patch(self.bright_admin, {"timezone": "Africa/Nairobi"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored_zone(self.bright)[0], "Africa/Nairobi")
        self.assertEqual(self.stored_zone(self.green), ("Africa/Lagos", None))
        green = self.client_for(self.green_admin).get(DISPLAY_URL).data["data"]
        self.assertEqual((green["timezone"], green["source"]), ("Africa/Lagos", "default"))

    def test_asserting_another_schools_tenant_is_a_404(self):
        client = self.client_for(self.bright_admin, tenant_slug=self.green.tenant.slug)
        self.assertEqual(client.get(DISPLAY_URL).status_code, 404)
        refused = client.patch(
            f"{DISPLAY_URL}?tenant={self.green.tenant.slug}",
            {"timezone": "Africa/Nairobi"}, format="json",
        )
        self.assertEqual(refused.status_code, 404, refused.data)
        self.assertIsNone(self.stored_zone(self.green)[1])

    def test_a_school_that_is_not_live_is_refused(self):
        response = self.client_for(self.sunrise_admin).get(DISPLAY_URL)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")
        refused = self.patch(self.sunrise_admin, {"timezone": "Africa/Nairobi"})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["error"]["code"], "TENANT_NOT_LIVE")

    def test_a_platform_caller_acting_as_itself_cannot_move_the_platform_zone(self):
        operator = make_vision_user(email="operator@codex.example.com", super_admin=True)
        response = self.patch(operator, {"timezone": "Europe/London"})
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(tenant_zone(None), ZoneInfo("Africa/Lagos"))


class DisplaySettingsBehaviourTests(_DisplaySettingsFixture):
    """What a school admin can do with the setting."""

    def test_a_new_school_reads_lagos_from_the_default(self):
        response = self.client_for(self.bright_admin).get(DISPLAY_URL)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(set(data), {"timezone", "source", "options"})
        self.assertEqual(data["timezone"], "Africa/Lagos")
        self.assertEqual(data["source"], "default")
        self.assertIn(
            {"value": "Africa/Lagos", "label": "Lagos (West Africa Time, UTC+1)"},
            data["options"],
        )
        values = [option["value"] for option in data["options"]]
        for zone in ("Africa/Accra", "Africa/Nairobi", "Africa/Johannesburg", "Europe/London"):
            self.assertIn(zone, values)

    def test_a_save_answers_with_the_refreshed_body_and_is_audited(self):
        response = self.patch(self.bright_admin, {
            "timezone": "Africa/Nairobi", "reason": "Our school is in Kenya",
        })
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual((data["timezone"], data["source"]), ("Africa/Nairobi", "school"))

        event = ConfigurationAuditEvent.all_objects.get(
            action="config.value.updated", reason="Our school is in Kenya",
        )
        self.assertEqual(event.tenant_id, self.bright.tenant.pk)
        self.assertEqual(event.actor, self.bright_admin)
        self.assertEqual(event.before_data, {"value": None})
        self.assertEqual(event.after_data, {"value": "Africa/Nairobi"})
        self.assertEqual(tenant_zone(self.bright.tenant), ZoneInfo("Africa/Nairobi"))

    def test_a_name_that_is_not_a_zone_is_refused_on_the_field(self):
        for bad in ("Lagos", "Africa/Atlantis", "UTC+1", ""):
            with self.subTest(value=bad):
                response = self.patch(self.bright_admin, {"timezone": bad})
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("timezone", response.data["error"]["detail"])
        self.assertIsNone(self.stored_zone(self.bright)[1])
        self.assertFalse(
            ConfigurationAuditEvent.all_objects.filter(
                tenant=self.bright.tenant, action="config.value.updated",
            ).exists()
        )

    def test_any_real_zone_is_accepted_and_offered_back_first(self):
        response = self.patch(self.bright_admin, {"timezone": "America/New_York"})
        self.assertEqual(response.status_code, 200, response.data)
        options = response.data["data"]["options"]
        self.assertEqual(options[0], {"value": "America/New_York", "label": "America/New York"})

    def test_a_platform_value_reads_as_platform(self):
        set_value(definition=self.definition, value="Africa/Accra", actor=None)
        data = self.client_for(self.green_admin).get(DISPLAY_URL).data["data"]
        self.assertEqual((data["timezone"], data["source"]), ("Africa/Accra", "platform"))

    def test_a_single_branch_school_sets_its_own_zone(self):
        response = self.patch(self.green_admin, {"timezone": "Africa/Johannesburg"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(tenant_zone(self.green.tenant), ZoneInfo("Africa/Johannesburg"))
        self.assertIsNone(self.stored_zone(self.bright)[1])
