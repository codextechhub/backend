"""A school's calendar and timetable settings: who reads them, who changes them, what they accept.

Security first: who may read and change the settings, the reach a writer
needs, and that one school's settings never reach another. Then the defaults,
the round trip and the audit trail, and every refusal.

Brightfield runs Lekki and Ikeja. Adaeze holds every calendar, timetable and
exam key for the whole school but not the settings key; Bisi is the
whole-school settings administrator; Kemi holds the same settings key pinned to
Lekki; Mr Eze teaches and holds no key at all. Sunrise is another school.
"""
from __future__ import annotations

from django.urls import reverse

from vs_config.models import ConfigurationAuditEvent
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)

from ..services.calendar_rules import calendar_rules_body, read_calendar_rules
from .base import _Base

EVENT_TYPES = [
    {"value": "HOLIDAY", "label": "Public holiday"},
    {"value": "MIDTERM_BREAK", "label": "Mid-term break"},
    {"value": "EXAM_PERIOD", "label": "Exam period"},
    {"value": "SCHOOL_EVENT", "label": "School event"},
    {"value": "PTA", "label": "PTA"},
    {"value": "SPORTS", "label": "Sports day"},
]

DUTY_OPTIONS = [
    {"value": "OFF", "label": "Off: anyone with the teacher role may take any lesson"},
    {
        "value": "WARN",
        "label": "Warn when the teacher has no teaching duty for the class and subject",
    },
    {
        "value": "REFUSE",
        "label": "Refuse a teacher with no teaching duty for the class and subject",
    },
]

#: Every value a school reads before it has saved anything.
DEFAULTS = {
    "teaching_days": [1, 2, 3, 4, 5],
    "week_starts_on": 1,
    "closes_school_by_type": {
        "HOLIDAY": True, "MIDTERM_BREAK": True, "EXAM_PERIOD": False,
        "SCHOOL_EVENT": False, "PTA": False, "SPORTS": False,
    },
    "room_required_to_publish": True,
    "teacher_duty_match": "OFF",
    "invigilator_roles": ["teacher"],
    "default_period_minutes": None,
}

#: What the settings screen sends: every editable setting, changed.
SAVED = {
    "teaching_days": [6, 1, 2, 3, 4, 5],
    "week_starts_on": 7,
    "closes_school_by_type": {
        "HOLIDAY": True, "MIDTERM_BREAK": False, "EXAM_PERIOD": False,
        "SCHOOL_EVENT": False, "PTA": False, "SPORTS": True,
    },
    "room_required_to_publish": False,
    "teacher_duty_match": "WARN",
    "invigilator_roles": ["teacher", "bursar"],
    "default_period_minutes": 40,
}


def values_of(body) -> dict:
    return {key: body[key] for key in DEFAULTS}


class _RulesFixture(_Base):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.eze = cls.make_teacher("eze@brightfield.test", "Chukwuemeka", "Eze")
        cls.bursar_role = make_role(cls.school, name="Bursar", key="bursar")

        settings_role = make_role(cls.school, name="Settings Admin", key="settings_admin")
        make_role_permission(
            settings_role,
            make_permission("school.settings.update", scope=PermissionScope.TENANT),
        )
        cls.bisi = make_school_admin(
            None, email="bisi@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.bisi, settings_role, branch=None)
        cls.kemi = make_school_admin(
            cls.lekki, email="kemi@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.kemi, settings_role, branch=cls.lekki)
        cls.sunrise_admin = make_school_admin(
            None, email="head@sunrise.test", tenant=cls.other.tenant,
        )

    def put_rules(self, user, body, tenant=None):
        return self.client_for(user).put(
            f"{reverse('calendar-rules')}?tenant={tenant or self.tenant.slug}",
            body, format="json",
        )

    def save(self, **overrides):
        response = self.put_rules(self.bisi, {**SAVED, **overrides})
        self.assertEqual(response.status_code, 200, response.data)
        return response


# ── security ────────────────────────────────────────────────────────────────

class CalendarRulesSecurityTests(_RulesFixture):

    def test_any_member_of_the_school_reads_them_with_no_key(self):
        """Mr Eze's own week is drawn on the school's days, so he needs no key."""
        response = self.get(self.eze, "calendar-rules")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(values_of(response.data["data"]), DEFAULTS)

    def test_a_member_of_another_school_cannot_read_this_schools(self):
        response = self.get(self.sunrise_admin, "calendar-rules")
        self.assertEqual(response.status_code, 404, response.data)

    def test_changing_them_needs_the_settings_key_not_a_timetable_key(self):
        """Adaeze holds every calendar, timetable and exam key and still may not."""
        for user in (self.admin, self.eze):
            response = self.put_rules(user, SAVED)
            self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(values_of(calendar_rules_body(self.tenant)), DEFAULTS)

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        """Kemi teaching Saturdays at Lekki would have Ikeja teaching them too."""
        before = ConfigurationAuditEvent.objects.count()
        response = self.put_rules(self.kemi, SAVED)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "SHARED_RECORD_READ_ONLY")
        self.assertEqual(
            response.data["message"],
            "Only a school-wide administrator can change the school's calendar "
            "and timetable settings.",
        )
        self.assertEqual(values_of(calendar_rules_body(self.tenant)), DEFAULTS)
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before)

    def test_a_school_wide_caller_changes_them(self):
        response = self.save()
        self.assertEqual(response.data["message"], "Calendar and timetable settings saved.")
        self.assertEqual(read_calendar_rules(self.tenant).teaching_days, (1, 2, 3, 4, 5, 6))

    def test_one_schools_settings_never_reach_another(self):
        self.save()
        self.assertEqual(
            values_of(calendar_rules_body(self.other.tenant)),
            {**DEFAULTS, "invigilator_roles": []},
        )
        self.assertEqual(
            read_calendar_rules(self.other.tenant).teaching_days, (1, 2, 3, 4, 5),
        )

    def test_the_request_cannot_name_another_school(self):
        response = self.put_rules(self.bisi, SAVED, tenant=self.other.tenant.slug)
        self.assertIn(response.status_code, (403, 404), response.data)
        self.assertEqual(
            read_calendar_rules(self.other.tenant).teaching_days, (1, 2, 3, 4, 5),
        )


# ── the settings endpoint ───────────────────────────────────────────────────

class CalendarRulesShapeTests(_RulesFixture):

    def test_a_school_that_has_set_nothing_reads_the_defaults_with_every_choice(self):
        data = self.get(self.admin, "calendar-rules").data["data"]
        self.assertEqual(values_of(data), DEFAULTS)
        self.assertEqual(data["event_types"], EVENT_TYPES)
        self.assertEqual(data["teacher_duty_match_options"], DUTY_OPTIONS)
        self.assertEqual(data["invigilator_role_options"], [
            {"value": "bursar", "label": "Bursar"},
            {"value": "school_admin", "label": "School Admin"},
            {"value": "settings_admin", "label": "Settings Admin"},
            {"value": "teacher", "label": "Teacher"},
        ])

    def test_the_half_term_label_is_in_the_schools_word(self):
        from schools.vs_schools.models import School

        School.objects.filter(pk=self.school.pk).update(term_structure="2_SEMESTERS")
        data = self.get(self.admin, "calendar-rules").data["data"]
        self.assertEqual(
            data["event_types"][1], {"value": "MIDTERM_BREAK", "label": "Mid-semester break"},
        )

    def test_a_save_round_trips_and_answers_with_the_settings(self):
        response = self.save()
        expected = {**SAVED, "teaching_days": [1, 2, 3, 4, 5, 6]}
        self.assertEqual(values_of(response.data["data"]), expected)
        again = self.get(self.eze, "calendar-rules").data["data"]
        self.assertEqual(values_of(again), expected)

    def test_every_write_is_audited_with_its_reason_and_an_unchanged_save_writes_nothing(self):
        before = ConfigurationAuditEvent.objects.count()
        self.save(reason="Saturday lessons from January.")
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before + 7)
        reasons = set(
            ConfigurationAuditEvent.objects.order_by().values_list("reason", flat=True)
            .filter(action="config.value.updated", tenant=self.tenant),
        )
        self.assertEqual(reasons, {"Saturday lessons from January."})
        self.save()
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before + 7)

    def test_saving_the_defaults_a_school_already_reads_writes_nothing(self):
        before = ConfigurationAuditEvent.objects.count()
        response = self.put_rules(self.bisi, DEFAULTS)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before)

    def test_a_default_period_set_back_to_none_clears_the_schools_value(self):
        self.save()
        cleared = ConfigurationAuditEvent.objects.filter(action="config.value.cleared")
        before = cleared.count()
        self.save(default_period_minutes=None)
        self.assertIsNone(read_calendar_rules(self.tenant).default_period_minutes)
        self.assertEqual(cleared.count(), before + 1)

    def test_a_role_listed_twice_is_saved_once(self):
        self.save(invigilator_roles=["teacher", "teacher", "bursar"])
        self.assertEqual(
            read_calendar_rules(self.tenant).invigilator_roles, ("teacher", "bursar"),
        )

    def test_a_stored_value_that_breaks_the_rules_reads_as_the_default(self):
        """A value typed at the platform layer costs a school its choice, never a screen."""
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        for key, value in (
            ("calendar.teaching_days", [0, 9]),
            ("calendar.closes_school_by_type", {"HOLIDAY": "yes"}),
            ("exams.invigilator_roles", ["", 3]),
        ):
            set_value(
                definition=ConfigurationDefinition.objects.get(key=key),
                value=value, actor=None, tenant=self.tenant,
            )
        self.assertEqual(values_of(calendar_rules_body(self.tenant)), DEFAULTS)


# ── refusals ────────────────────────────────────────────────────────────────

class CalendarRulesRefusalTests(_RulesFixture):

    def refused(self, **overrides):
        before = ConfigurationAuditEvent.objects.count()
        response = self.put_rules(self.bisi, {**SAVED, **overrides})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ConfigurationAuditEvent.objects.count(), before)
        self.assertEqual(values_of(calendar_rules_body(self.tenant)), DEFAULTS)
        return response.data["error"]["detail"]

    def test_every_setting_is_sent_every_time(self):
        response = self.put_rules(self.bisi, {})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["detail"], {
            "teaching_days": ["Choose the days the school teaches."],
            "week_starts_on": [
                "Say whether the school's week starts on Monday or Sunday.",
            ],
            "closes_school_by_type": [
                "Say, for each kind of calendar entry, whether it closes the school.",
            ],
            "room_required_to_publish": [
                "Say whether a lesson needs a room before its timetable can be "
                "published.",
            ],
            "teacher_duty_match": [
                "Say what happens to a teacher with no teaching duty for the lesson.",
            ],
            "invigilator_roles": ["List the roles whose holders may invigilate."],
            "default_period_minutes": [
                "Send the default length of a period in minutes, or null for no "
                "default.",
            ],
        })

    def test_a_school_teaches_at_least_one_real_weekday_each_listed_once(self):
        self.assertEqual(
            self.refused(teaching_days=[])["teaching_days"],
            ["Choose at least one day the school teaches."],
        )
        for bad in ([0], [8], ["1"], [True], [1.5]):
            self.assertEqual(
                self.refused(teaching_days=bad)["teaching_days"],
                ["Give each teaching day as a weekday number, 1 (Monday) to 7 (Sunday)."],
            )
        self.assertEqual(
            self.refused(teaching_days=[1, 2, 1])["teaching_days"],
            ["Monday is listed twice."],
        )

    def test_the_week_starts_on_monday_or_sunday(self):
        for bad in (3, 0, "Monday"):
            self.assertEqual(
                self.refused(week_starts_on=bad)["week_starts_on"],
                ["The school's week starts on Monday (1) or Sunday (7)."],
            )

    def test_every_kind_of_entry_is_answered_with_true_or_false(self):
        full = SAVED["closes_school_by_type"]
        self.assertEqual(
            self.refused(closes_school_by_type=["HOLIDAY"])["closes_school_by_type"],
            ["Say, for each kind of calendar entry, whether it closes the school."],
        )
        self.assertEqual(
            self.refused(closes_school_by_type={**full, "FUNERAL": True})[
                "closes_school_by_type"
            ],
            ["'FUNERAL' is not a kind of calendar entry."],
        )
        missing = {k: v for k, v in full.items() if k != "PTA"}
        self.assertEqual(
            self.refused(closes_school_by_type=missing)["closes_school_by_type"],
            ["Say whether an entry of type PTA closes the school."],
        )
        self.assertEqual(
            self.refused(closes_school_by_type={**full, "SPORTS": "yes"})[
                "closes_school_by_type"
            ],
            ["Say true or false for whether an entry of type Sports day closes the school."],
        )

    def test_the_room_rule_is_a_yes_or_no(self):
        self.assertEqual(
            self.refused(room_required_to_publish=None)["room_required_to_publish"],
            ["Say whether a lesson needs a room before its timetable can be published."],
        )

    def test_the_duty_match_is_off_warn_or_refuse(self):
        self.assertEqual(
            self.refused(teacher_duty_match="SOMETIMES")["teacher_duty_match"],
            ["Choose Off, Warn or Refuse for a teacher with no teaching duty for the lesson."],
        )

    def test_invigilators_come_from_at_least_one_of_the_schools_active_roles(self):
        self.assertEqual(
            self.refused(invigilator_roles=[])["invigilator_roles"],
            ["Choose at least one role whose holders may invigilate."],
        )
        self.assertEqual(
            self.refused(invigilator_roles=["teacher", "ghost"])["invigilator_roles"],
            ["'ghost' is not one of this school's active roles."],
        )

    def test_another_schools_role_is_not_one_of_this_schools(self):
        make_role(self.other, name="Sunrise Proctor", key="proctor")
        self.assertEqual(
            self.refused(invigilator_roles=["proctor"])["invigilator_roles"],
            ["'proctor' is not one of this school's active roles."],
        )

    def test_a_default_period_is_ten_to_two_hundred_and_forty_minutes(self):
        for bad in (5, 241, "long", True):
            self.assertEqual(
                self.refused(default_period_minutes=bad)["default_period_minutes"],
                ["A default period is 10 to 240 minutes long, or leave it empty for no default."],
            )
        self.save(default_period_minutes=10)
        self.save(default_period_minutes=240)
