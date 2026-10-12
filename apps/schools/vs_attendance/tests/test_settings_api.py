"""The settings screen: what it reads, what it refuses, and who may change it.

The four settings are the school's, shared by every branch, so a branch
administrator who holds the key can read them and cannot change them.
"""
from __future__ import annotations

import datetime as dt

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import AcademicSession, Level, Program
from vs_config.models import ConfigurationValue
from vs_rbac.tests.helpers import make_assignment, make_school_admin

from ..constants import PERM_REGISTER_VIEW, PERM_SETTINGS_UPDATE
from ..services.rules import AttendanceRules, read_attendance_rules
from .base import AttendanceFixture, make_key_role

URL = "attendance-settings"


class _Settings(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        role = make_key_role(
            cls.tenant, "Configurer", "configurer", (PERM_REGISTER_VIEW, PERM_SETTINGS_UPDATE),
        )
        cls.configurer = make_school_admin(
            None, email="config@brightfield.test", tenant=cls.tenant,
            first_name="Chioma", last_name="Dike",
        )
        make_assignment(cls.tenant, cls.configurer, role, branch=None)
        cls.branch_configurer = make_school_admin(
            cls.ikeja, email="config@ikeja.test", first_name="Ibe", last_name="Ugo",
        )
        make_assignment(cls.tenant, cls.branch_configurer, role, branch=cls.ikeja)
        solo_role = make_key_role(
            cls.solo, "Configurer", "configurer", (PERM_REGISTER_VIEW, PERM_SETTINGS_UPDATE),
        )
        cls.solo_configurer = make_school_admin(
            None, email="config@sunrise.test", tenant=cls.solo,
            first_name="Seun", last_name="Ola",
        )
        make_assignment(cls.solo, cls.solo_configurer, solo_role, branch=None)

    def get(self, user=None):
        response = TenantAPIClient(user or self.configurer).get(reverse(URL))
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def patch(self, body, user=None):
        return TenantAPIClient(user or self.configurer).patch(reverse(URL), body, format="json")

    def stored(self, tenant=None):
        return ConfigurationValue.objects.filter(
            definition__key__startswith="attendance.", tenant=tenant or self.tenant,
        ).count()


class ReadTests(_Settings):
    def test_a_school_that_has_chosen_nothing_reads_the_defaults(self):
        body = self.get()
        self.assertEqual(
            {k: body[k] for k in (
                "guardian_notices", "chronic_threshold", "lesson_marking_levels",
                "edit_window_days",
            )},
            {
                "guardian_notices": True, "chronic_threshold": 90,
                "lesson_marking_levels": [], "edit_window_days": 0,
            },
        )

    def test_the_levels_of_the_active_year_travel_with_the_switch_on_each(self):
        self.assertEqual(self.patch({"lesson_marking_levels": ["JSS1"]}).status_code, 200)
        levels = self.get()["levels"]
        self.assertEqual(
            [(lv["code"], lv["name"], lv["program_name"], lv["lesson_marking"]) for lv in levels],
            [
                ("JSS1", "JSS1", "Junior Secondary", True),
                ("PRY4", "Primary 4", "Junior Secondary", False),
            ],
        )

    def test_a_code_carried_by_several_branches_is_listed_once(self):
        other = Program.all_objects.create(tenant=self.tenant, name="Ikeja Junior", code="IKJ")
        Level.all_objects.create(
            tenant=self.tenant, session=self.year, program=other, branch=self.ikeja,
            name="JSS1 Ikeja", code="jss1", order_index=1,
        )
        codes = [lv["code"].lower() for lv in self.get()["levels"]]
        self.assertEqual(codes.count("jss1"), 1)

    def test_the_screen_says_whose_settings_these_are(self):
        self.assertEqual(self.get()["scope_label"], {
            "school_name": "Brightfield Schools", "session_name": "2025/2026",
            "term_name": "First Term",
        })

    def test_any_holder_of_the_register_key_may_read_them(self):
        self.assertEqual(self.get(self.eze)["chronic_threshold"], 90)

    def test_someone_with_no_attendance_key_is_refused(self):
        self.assertEqual(TenantAPIClient(self.nobody).get(reverse(URL)).status_code, 403)

    def test_another_schools_settings_are_not_this_ones(self):
        self.assertEqual(self.patch({"edit_window_days": 4}).status_code, 200)
        self.assertEqual(self.get(self.solo_configurer)["edit_window_days"], 0)
        self.assertEqual(self.get(self.solo_configurer)["scope_label"]["school_name"], "Sunrise Academy")


class WriteTests(_Settings):
    def test_a_change_to_one_setting_leaves_the_others(self):
        response = self.patch({"edit_window_days": 3})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["edit_window_days"], 3)
        self.assertEqual(self.data(response)["chronic_threshold"], 90)
        self.assertEqual(read_attendance_rules(self.tenant).edit_window_days, 3)

    def test_all_four_can_change_together_and_come_back_in_the_answer(self):
        response = self.patch({
            "guardian_notices": False, "chronic_threshold": 85,
            "lesson_marking_levels": ["PRY4"], "edit_window_days": 14,
        })
        self.assertEqual(response.status_code, 200, response.data)
        rules = read_attendance_rules(self.tenant)
        self.assertEqual(
            (rules.guardian_notices, rules.chronic_threshold, rules.lesson_marking_levels,
             rules.edit_window_days),
            (False, 85, ("PRY4",), 14),
        )
        body = self.data(response)
        self.assertEqual(
            [lv["lesson_marking"] for lv in body["levels"]], [False, True],
        )

    def test_the_bounds_are_refused_and_nothing_is_written(self):
        for body in (
            {"chronic_threshold": 49}, {"chronic_threshold": 101},
            {"chronic_threshold": "high"}, {"edit_window_days": -1},
            {"edit_window_days": 15}, {"edit_window_days": "soon"},
            {"guardian_notices": "perhaps"}, {"lesson_marking_levels": "JSS1"},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status_code, 400)
        self.assertEqual(self.stored(), 0)

    def test_the_edges_of_the_bounds_are_accepted(self):
        for body in (
            {"chronic_threshold": 50}, {"chronic_threshold": 100},
            {"edit_window_days": 0}, {"edit_window_days": 14},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.patch(body).status_code, 200)

    def test_an_unknown_level_code_is_refused_by_name_and_nothing_else_is_saved(self):
        response = self.patch({
            "edit_window_days": 5, "lesson_marking_levels": ["JSS1", "ZZ9", "Q7"],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "UNKNOWN_LEVEL_CODE")
        self.assertIn("ZZ9", response.data["message"])
        self.assertIn("Q7", response.data["message"])
        self.assertNotIn("JSS1", response.data["message"])
        self.assertEqual(response.data["error"]["detail"]["field"], "lesson_marking_levels")
        self.assertEqual(self.stored(), 0)

    def test_a_code_is_saved_in_the_spelling_the_school_uses(self):
        response = self.patch({"lesson_marking_levels": ["jss1", " pry4 ", "JSS1"]})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["lesson_marking_levels"], ["JSS1", "PRY4"])

    def test_a_level_of_another_year_is_accepted_though_this_year_does_not_list_it(self):
        old = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2024/2025", status="ARCHIVED",
            start_date=dt.date(2024, 9, 9), end_date=dt.date(2025, 7, 18),
        )
        program = Program.all_objects.get(tenant=self.tenant, code="JSS")
        Level.all_objects.create(
            tenant=self.tenant, session=old, program=program, name="SSS3", code="SSS3",
            order_index=9,
        )
        self.assertNotIn("SSS3", [lv["code"] for lv in self.get()["levels"]])
        self.assertEqual(self.patch({"lesson_marking_levels": ["SSS3"]}).status_code, 200)

    def test_another_schools_level_code_is_unknown_here(self):
        response = self.patch({"lesson_marking_levels": ["BAS3"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "UNKNOWN_LEVEL_CODE")

    def test_the_change_binds_every_branch_and_no_other_school(self):
        self.assertEqual(self.patch({"chronic_threshold": 80}).status_code, 200)
        # Read by an Ikeja administrator and by Lekki's, from the same school's value.
        self.assertEqual(self.get(self.branch_configurer)["chronic_threshold"], 80)
        self.assertEqual(self.get(self.lekki_admin)["chronic_threshold"], 80)
        self.assertEqual(read_attendance_rules(self.solo), AttendanceRules())

    def test_saving_what_is_already_in_force_writes_nothing(self):
        self.assertEqual(
            self.patch({"chronic_threshold": 90, "guardian_notices": True}).status_code, 200,
        )
        self.assertEqual(self.patch({}).status_code, 200)
        self.assertEqual(self.stored(), 0)
        self.assertEqual(self.patch({"chronic_threshold": 91}).status_code, 200)
        self.assertEqual(self.stored(), 1)


class SecurityTests(_Settings):
    def test_changing_them_needs_the_settings_key(self):
        for user in (self.eze, self.admin, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.patch({"edit_window_days": 2}, user).status_code, 403)
        self.assertEqual(self.stored(), 0)

    def test_a_branch_administrator_holding_the_key_reads_but_cannot_change(self):
        self.assertEqual(self.get(self.branch_configurer)["edit_window_days"], 0)
        response = self.patch({"edit_window_days": 2}, self.branch_configurer)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "SHARED_RECORD_READ_ONLY")
        self.assertEqual(self.stored(), 0)

    def test_one_schools_administrator_cannot_change_anothers(self):
        self.assertEqual(self.patch({"edit_window_days": 9}, self.solo_configurer).status_code, 200)
        self.assertEqual(read_attendance_rules(self.tenant).edit_window_days, 0)
        self.assertEqual(read_attendance_rules(self.solo).edit_window_days, 9)
