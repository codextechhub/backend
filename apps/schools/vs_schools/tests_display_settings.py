"""``/v1/i/me/settings/display/``: time zones, date format and clock.

The same door as the security and payroll settings, so the tests lead the
same way: who is refused, whose value a write can reach, then what a school
admin can do. Two live schools, because a single-branch test proves nothing
about a multi-branch one (Bright Star runs Ikeja and Lekki, Green Field runs
one branch), and one pending school. Bright Star's Lekki administrator holds
both settings keys pinned to Lekki, which is how a school lets a branch keep
its own time zone.
"""
import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_config.clock import TIME_ZONE_KEY, branch_zone, forget_tenant_zone, tenant_zone
from vs_config.display import CLOCK_KEY, DATE_FORMAT_KEY
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

        lekki_role = make_role(cls.bright, name="Lekki Admin", key="lekki_admin")
        make_role_permission(lekki_role, cls.view_perm)
        make_role_permission(lekki_role, cls.update_perm)
        cls.lekki_admin = make_school_admin(cls.lekki, email="lekki@bright.example.com")
        make_assignment(cls.bright, cls.lekki_admin, lekki_role, branch=cls.lekki)

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

    def patch(self, user, body, branch=None):
        return self.client_for(user).patch(self.url(user, branch), body, format="json")

    def get(self, user, branch=None):
        return self.client_for(user).get(self.url(user, branch))

    def delete(self, user, branch=None):
        return self.client_for(user).delete(self.url(user, branch))

    @staticmethod
    def url(user, branch=None):
        suffix = f"&branch={branch.pk}" if branch is not None else ""
        return f"{DISPLAY_URL}?tenant={user.tenant.slug}{suffix}"

    def stored_zone(self, school):
        value, row = resolve_value(self.definition, tenant=school.tenant)
        return value, row

    def branch_zone_row(self, branch):
        """The branch's own row, or ``None`` when it follows the school."""
        _, row = resolve_value(self.definition, tenant=branch.tenant, branch=branch)
        return row if row is not None and row.scope_key == f"branch:{branch.pk}" else None

    def stored(self, key, school):
        definition = ConfigurationDefinition.objects.get(key=key)
        return resolve_value(definition, tenant=school.tenant)


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
        self.assertEqual(set(data), {
            "timezone", "source", "options", "date_format", "date_format_options",
            "clock", "clock_options", "branches",
        })
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


class DisplayFormatAndClockTests(_DisplaySettingsFixture):
    """The school's date format and clock: one each for the whole school."""

    def test_a_new_school_reads_the_defaults_and_is_offered_every_choice(self):
        moment = datetime.datetime(2026, 9, 29, 10, 0, tzinfo=ZoneInfo("Africa/Lagos"))
        with mock.patch("vs_config.clock.tenant_now", return_value=moment):
            data = self.get(self.bright_admin).data["data"]
        self.assertEqual((data["date_format"], data["clock"]), ("D_MMM_YYYY", "H12"))
        self.assertEqual(data["date_format_options"], [
            {"value": "D_MMM_YYYY", "label": "29 Sep 2026"},
            {"value": "DD_MM_YYYY", "label": "29/09/2026"},
            {"value": "YYYY_MM_DD", "label": "2026-09-29"},
        ])
        self.assertEqual(data["clock_options"], [
            {"value": "H12", "label": "12-hour (8:00 am, 2:30 pm)"},
            {"value": "H24", "label": "24-hour (08:00, 14:30)"},
        ])

    def test_the_format_alone_or_the_clock_alone_can_be_saved(self):
        response = self.patch(self.bright_admin, {"date_format": "YYYY_MM_DD"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["date_format"], "YYYY_MM_DD")
        self.assertEqual(response.data["data"]["clock"], "H12")
        response = self.patch(self.bright_admin, {"clock": "H24", "reason": "Our staff read 24-hour time"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["clock"], "H24")
        self.assertEqual(self.stored(DATE_FORMAT_KEY, self.bright)[0], "YYYY_MM_DD")
        self.assertIsNone(self.stored_zone(self.bright)[1])
        event = ConfigurationAuditEvent.all_objects.get(
            action="config.value.updated", reason="Our staff read 24-hour time",
        )
        self.assertEqual((event.before_data, event.after_data), ({"value": None}, {"value": "H24"}))

    def test_all_three_in_one_save_are_audited_one_value_each(self):
        response = self.patch(self.bright_admin, {
            "timezone": "Africa/Accra", "date_format": "DD_MM_YYYY", "clock": "H24",
            "reason": "Moving to Ghana",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            ConfigurationAuditEvent.all_objects.filter(
                tenant=self.bright.tenant, reason="Moving to Ghana",
            ).count(),
            3,
        )
        data = response.data["data"]
        self.assertEqual(
            (data["timezone"], data["date_format"], data["clock"]),
            ("Africa/Accra", "DD_MM_YYYY", "H24"),
        )

    def test_a_value_outside_the_choices_is_refused_on_its_field_and_nothing_is_written(self):
        cases = (
            ({"date_format": "MM_DD_YYYY"}, "date_format",
             "'MM_DD_YYYY' is not a date format this school can use. Choose "
             "D_MMM_YYYY, DD_MM_YYYY or YYYY_MM_DD."),
            ({"clock": "H13"}, "clock",
             "'H13' is not a clock this school can use. Choose H12 or H24."),
            ({"clock": "H24", "timezone": "Africa/Atlantis"}, "timezone",
             "'Africa/Atlantis' is not a recognised time zone. Use an IANA name "
             "such as Africa/Lagos."),
        )
        for body, field, sentence in cases:
            with self.subTest(body=body):
                response = self.patch(self.bright_admin, body)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["error"]["detail"][field], [sentence])
        self.assertIsNone(self.stored(CLOCK_KEY, self.bright)[1])
        self.assertIsNone(self.stored(DATE_FORMAT_KEY, self.bright)[1])

    def test_an_empty_save_is_refused(self):
        response = self.patch(self.bright_admin, {"reason": "Nothing to say"})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(
            response.data["error"]["detail"]["non_field_errors"],
            ["Send a time zone, a date format or a clock to change."],
        )

    def test_the_format_and_clock_reach_the_signed_in_tenant_block(self):
        from vs_tenants.context import tenant_context_block

        self.patch(self.bright_admin, {"date_format": "DD_MM_YYYY", "clock": "H24"})
        display = tenant_context_block(self.bright.tenant)["display"]
        self.assertEqual((display["date_format"], display["clock"]), ("DD_MM_YYYY", "H24"))
        self.assertEqual(
            tenant_context_block(self.green.tenant)["display"]["date_format"], "D_MMM_YYYY",
        )


class BranchZoneTests(_DisplaySettingsFixture):
    """A branch in another zone keeps its own; every other branch follows the school."""

    def test_the_school_body_lists_each_branch_and_where_its_zone_comes_from(self):
        self.patch(self.bright_admin, {"timezone": "Africa/Nairobi"}, branch=self.lekki)
        branches = self.get(self.bright_admin).data["data"]["branches"]
        self.assertEqual(branches, [
            {"id": self.ikeja.pk, "name": "Ikeja Branch", "timezone": "Africa/Lagos",
             "source": "school"},
            {"id": self.lekki.pk, "name": "Lekki Branch", "timezone": "Africa/Nairobi",
             "source": "branch"},
        ])

    def test_a_branch_bound_reader_is_listed_only_their_branch(self):
        branches = self.get(self.ikeja_admin).data["data"]["branches"]
        self.assertEqual([row["id"] for row in branches], [self.ikeja.pk])

    def test_a_single_branch_school_lists_no_branches_and_refuses_a_branch_zone(self):
        self.assertEqual(self.get(self.green_admin).data["data"]["branches"], [])
        response = self.patch(self.green_admin, {"timezone": "Africa/Nairobi"}, branch=self.green_main)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["detail"]["branch"], [
            "Green Field has one branch, so its time zone is the school's. Change "
            "the school's time zone instead.",
        ])
        self.assertIsNone(self.branch_zone_row(self.green_main))

    def test_a_branch_sets_its_own_zone_and_the_school_keeps_its_own(self):
        response = self.patch(
            self.lekki_admin, {"timezone": "Africa/Nairobi", "reason": "Lekki runs on Nairobi time"},
            branch=self.lekki,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["message"], "Lekki Branch keeps its own time zone.")
        self.assertEqual(response.data["data"], {
            "branch": {"id": self.lekki.pk, "name": "Lekki Branch"},
            "timezone": "Africa/Nairobi", "source": "branch",
            "options": response.data["data"]["options"],
        })
        self.assertIsNone(self.stored_zone(self.bright)[1])
        event = ConfigurationAuditEvent.all_objects.get(reason="Lekki runs on Nairobi time")
        self.assertEqual(event.branch_id, self.lekki.pk)
        forget_tenant_zone(self.bright.tenant)
        self.assertEqual(branch_zone(self.bright.tenant, self.lekki), ZoneInfo("Africa/Nairobi"))
        self.assertEqual(branch_zone(self.bright.tenant, self.ikeja), ZoneInfo("Africa/Lagos"))

    def test_a_branch_read_names_its_source(self):
        data = self.get(self.lekki_admin, branch=self.lekki).data["data"]
        self.assertEqual((data["timezone"], data["source"]), ("Africa/Lagos", "school"))
        self.patch(self.bright_admin, {"timezone": "Africa/Accra"})
        data = self.get(self.lekki_admin, branch=self.lekki).data["data"]
        self.assertEqual((data["timezone"], data["source"]), ("Africa/Accra", "school"))
        self.patch(self.lekki_admin, {"timezone": "Africa/Kigali"}, branch=self.lekki)
        data = self.get(self.lekki_admin, branch=self.lekki).data["data"]
        self.assertEqual((data["timezone"], data["source"]), ("Africa/Kigali", "branch"))
        self.assertEqual(data["options"][0]["value"], "Africa/Abidjan")

    def test_removing_a_branch_zone_follows_the_school_again(self):
        self.patch(self.lekki_admin, {"timezone": "Africa/Nairobi"}, branch=self.lekki)
        response = self.delete(self.lekki_admin, branch=self.lekki)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["message"], "Lekki Branch follows the school's time zone again.",
        )
        self.assertEqual(
            (response.data["data"]["timezone"], response.data["data"]["source"]),
            ("Africa/Lagos", "school"),
        )
        self.assertIsNone(self.branch_zone_row(self.lekki))
        self.assertTrue(ConfigurationAuditEvent.all_objects.filter(
            action="config.value.cleared", branch=self.lekki,
        ).exists())

    def test_a_delete_naming_no_branch_is_refused(self):
        response = self.delete(self.bright_admin)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["detail"]["branch"], [
            "Name the branch whose own time zone to remove. The school's time "
            "zone is changed, never removed.",
        ])

    def test_a_branch_save_refuses_a_format_or_clock_and_a_bad_zone(self):
        cases = (
            ({"timezone": "Africa/Nairobi", "date_format": "DD_MM_YYYY"}, "date_format",
             "The date format and the clock are the school's, not a branch's. Save "
             "them without choosing a branch."),
            ({"timezone": "Africa/Nairobi", "clock": "H24"}, "clock",
             "The date format and the clock are the school's, not a branch's. Save "
             "them without choosing a branch."),
            ({"timezone": "Nairobi"}, "timezone",
             "'Nairobi' is not a recognised time zone. Use an IANA name such as "
             "Africa/Lagos."),
        )
        for body, field, sentence in cases:
            with self.subTest(body=body):
                response = self.patch(self.lekki_admin, body, branch=self.lekki)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["error"]["detail"][field], [sentence])
        self.assertIsNone(self.branch_zone_row(self.lekki))

    def test_a_branch_bound_caller_may_not_touch_another_branch(self):
        for verb in ("get", "delete"):
            with self.subTest(verb=verb):
                response = getattr(self, verb)(self.lekki_admin, branch=self.ikeja)
                self.assertEqual(response.status_code, 404, response.data)
        response = self.patch(self.lekki_admin, {"timezone": "Africa/Nairobi"}, branch=self.ikeja)
        self.assertEqual(response.status_code, 404, response.data)
        self.assertIsNone(self.branch_zone_row(self.ikeja))

    def test_a_reader_without_the_update_key_cannot_set_a_branch_zone(self):
        response = self.patch(self.ikeja_admin, {"timezone": "Africa/Nairobi"}, branch=self.ikeja)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIsNone(self.branch_zone_row(self.ikeja))

    def test_another_schools_branch_is_a_404(self):
        for verb in ("get", "delete"):
            with self.subTest(verb=verb):
                response = getattr(self, verb)(self.bright_admin, branch=self.green_main)
                self.assertEqual(response.status_code, 404, response.data)
        response = self.patch(self.bright_admin, {"timezone": "Africa/Nairobi"}, branch=self.green_main)
        self.assertEqual(response.status_code, 404, response.data)
        self.assertIsNone(self.branch_zone_row(self.green_main))

    def test_a_pending_school_is_refused_a_branch_zone_too(self):
        response = self.patch(self.sunrise_admin, {"timezone": "Africa/Nairobi"}, branch=self.sunrise_main)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")
