"""Reading the staff registers: who is on which branch's register, and what each row says.

Brightfield has two branches and Sunrise has one, and the rules differ between
them: a person with no home branch is locked in a group of their own at
Brightfield and is simply that branch's at Sunrise. The register is read for
Monday 13 October 2025, with Founders' Day (a school-wide closure) on the 9th and
Lekki's Open Day on the 7th.
"""
from __future__ import annotations

import datetime as dt

from django.db import connection
from django.test.utils import CaptureQueriesContext

from schools.vs_calendar.models import CalendarEvent, CalendarEventAudience
from schools.vs_staff.constants import PERM_LEAVE_VIEW
from schools.vs_staff.models import StaffProfile
from vs_config.display import format_date
from vs_rbac.tests.helpers import make_branch

from ..constants import PERM_STAFF_MARK, PERM_STAFF_VIEW
from .base import FOUNDERS_DAY, LEKKI_OPEN_DAY, TODAY, YESTERDAY, make_key_role
from .staff_base import BOTH, StaffFixture

LEKKI_NAMES = ["Peter Achebe", "Funke Adeyemi", "Sade Bello", "Chidi Eze"]


class WhoIsOnTheRegisterTests(StaffFixture):
    def test_a_whole_school_reader_gets_a_group_for_every_branch_and_the_people_with_no_home(self):
        response = self.staff_day(self.registrar)

        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertTrue(body["branch_dimension"])
        self.assertIsNone(body["closed"])
        self.assertEqual({g["branch"]["id"] for g in body["groups"]}, {self.lekki.pk, self.ikeja.pk})
        self.assertEqual(body["no_home_branch"]["count"], 1)

    def test_a_branch_register_lists_its_home_staff_in_surname_order(self):
        rows = self.group(self.staff_day(self.registrar), self.lekki)["rows"]

        self.assertEqual([row["staff"]["name"] for row in rows], LEKKI_NAMES)
        self.assertNotIn("Emeka Nnamdi", [row["staff"]["name"] for row in rows])

    def test_a_row_carries_the_person_the_role_line_and_nothing_marked_yet(self):
        row = self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.funke.pk]

        self.assertEqual(row["staff"], {
            "id": self.funke.pk, "name": "Funke Adeyemi", "staff_number": "",
        })
        self.assertEqual(row["role"], "Bursar")
        self.assertIsNone(row["mark"])
        self.assertIsNone(row["arrived_at"])
        self.assertEqual(row["note"], "")
        self.assertFalse(row["locked"])
        self.assertIsNone(row["lock_reason"])
        self.assertIsNone(row["leave"])
        self.assertIsNone(row["exit"])

    def test_the_role_line_adds_the_classes_a_person_leads_this_year(self):
        lekki = self.staff_rows(self.staff_day(self.registrar), self.lekki)
        ikeja = self.staff_rows(self.staff_day(self.registrar), self.ikeja)

        self.assertEqual(lekki[self.eze_staff.pk]["role"], "Class teacher · JSS1 A")
        self.assertEqual(ikeja[self.emeka_n.pk]["role"], "Teacher · Class teacher · SSS2 Science")
        self.assertEqual(ikeja[self.tunde_b.pk]["role"], "Economics Teacher")

    def test_a_job_title_of_class_teacher_is_not_said_twice(self):
        StaffProfile.objects.filter(pk=self.emeka_n.pk).update(job_title="Class Teacher")
        ikeja = self.staff_rows(self.staff_day(self.registrar), self.ikeja)
        self.assertEqual(ikeja[self.emeka_n.pk]["role"], "Class teacher · SSS2 Science")

    def test_a_fresh_register_has_nothing_marked_and_nobody_submitted_it(self):
        group = self.group(self.staff_day(self.registrar), self.lekki)

        self.assertEqual(group["status"], "NOT_STARTED")
        self.assertEqual(group["counts"], {
            "total": 4, "present": 0, "late": 0, "absent": 0, "on_leave": 0, "unmarked": 4,
        })
        self.assertIsNone(group["submitted_at"])
        self.assertIsNone(group["submitted_by_name"])
        self.assertIsNone(group["edit_window_open"])

    def test_the_branch_parameter_narrows_the_day_to_one_visible_branch(self):
        body = self.data(self.staff_day(self.registrar, branch=self.ikeja.pk))

        self.assertEqual([g["branch"]["id"] for g in body["groups"]], [self.ikeja.pk])
        self.assertIsNone(body["no_home_branch"])

    def test_an_additional_posting_does_not_put_a_person_on_a_second_register(self):
        self.funke.additional_postings.add(self.ikeja)

        day = self.staff_day(self.registrar)

        self.assertIn(self.funke.pk, self.staff_rows(day, self.lekki))
        self.assertNotIn(self.funke.pk, self.staff_rows(day, self.ikeja))

    def test_a_person_does_not_appear_on_another_tenants_register(self):
        day = self.staff_day(self.registrar)

        names = [r["staff"]["name"] for g in self.data(day)["groups"] for r in g["rows"]]
        self.assertNotIn("Halima Danjuma", names)
        self.assertNotIn("Kola Ayanwale", names)


class NoHomeBranchTests(StaffFixture):
    def test_at_a_school_with_several_branches_they_are_locked_in_a_group_of_their_own(self):
        group = self.data(self.staff_day(self.registrar))["no_home_branch"]

        self.assertEqual(group["count"], 1)
        row = group["rows"][0]
        self.assertEqual(row["staff"]["name"], "Adaeze Nwankwo")
        self.assertTrue(row["locked"])
        self.assertEqual(row["lock_reason"], "NO_HOME_BRANCH")
        self.assertEqual(
            row["lock_note"],
            "Give Adaeze Nwankwo a home branch in Staff Management before marking them.",
        )
        self.assertIsNone(row["mark"])

    def test_they_are_on_no_branch_register(self):
        day = self.staff_day(self.registrar)

        for group in self.data(day)["groups"]:
            self.assertNotIn(self.adaeze.pk, [row["staff"]["id"] for row in group["rows"]])

    def test_a_branch_bound_reader_is_never_told_they_exist(self):
        for user in (self.lekki_hr, self.ikeja_hr):
            with self.subTest(user=user.email):
                body = self.data(self.staff_day(user))
                self.assertIsNone(body["no_home_branch"])
                names = [r["staff"]["name"] for g in body["groups"] for r in g["rows"]]
                self.assertNotIn("Adaeze Nwankwo", names)

    def test_naming_a_branch_leaves_the_group_out(self):
        body = self.data(self.staff_day(self.registrar, branch=self.lekki.pk))

        self.assertIsNone(body["no_home_branch"])

    def test_they_are_not_counted_in_any_branchs_totals(self):
        for group in self.data(self.staff_day(self.registrar))["groups"]:
            self.assertEqual(group["counts"]["total"] + group["counts"]["on_leave"], len(group["rows"]))

    def test_at_a_school_with_one_branch_they_are_that_branchs_and_markable(self):
        body = self.data(self.staff_day(self.solo_registrar))

        self.assertFalse(body["branch_dimension"])
        self.assertIsNone(body["no_home_branch"])
        (group,) = body["groups"]
        rows = {row["staff"]["name"]: row for row in group["rows"]}
        self.assertEqual(set(rows), {"Bola Ade", "Kola Ayanwale", "Halima Danjuma"})
        self.assertFalse(rows["Kola Ayanwale"]["locked"])
        self.assertIsNone(rows["Kola Ayanwale"]["lock_reason"])
        self.assertEqual(group["counts"]["total"], 3)

    def test_a_school_gaining_a_second_branch_locks_them_from_then_on(self):
        make_branch(self.solo_school, name="Second Branch", is_main=False)

        body = self.data(self.staff_day(self.solo_registrar))

        self.assertTrue(body["branch_dimension"])
        self.assertEqual(body["no_home_branch"]["rows"][0]["staff"]["name"], "Kola Ayanwale")


class OnLeaveTests(StaffFixture):
    def test_somebody_on_approved_leave_that_day_is_locked_with_the_leave_beside_them(self):
        leave = self.approve_leave(self.funke, self.days_ago(2), TODAY + dt.timedelta(days=10))

        group = self.group(self.staff_day(self.registrar), self.lekki)

        row = {r["staff"]["id"]: r for r in group["rows"]}[self.funke.pk]
        self.assertTrue(row["locked"])
        self.assertEqual(row["lock_reason"], "ON_LEAVE")
        self.assertEqual(row["leave"], {
            "id": leave.pk, "type": "MATERNITY", "type_label": "Maternity",
            "end_date": TODAY + dt.timedelta(days=10),
        })
        self.assertIsNone(row["mark"])
        self.assertEqual(group["counts"]["on_leave"], 1)
        self.assertEqual(group["counts"]["total"], 3)
        self.assertEqual(group["counts"]["unmarked"], 3)

    def test_the_lock_names_neither_the_kind_of_leave_nor_its_last_day(self):
        self.approve_leave(self.funke, self.days_ago(2), TODAY + dt.timedelta(days=10))

        row = self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.funke.pk]

        self.assertNotIn("Maternity", row["lock_note"])
        self.assertNotIn(str(TODAY.year), row["lock_note"])

    def test_leave_that_ended_yesterday_leaves_them_markable_today(self):
        self.approve_leave(self.funke, self.days_ago(5), YESTERDAY)

        row = self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.funke.pk]

        self.assertFalse(row["locked"])
        self.assertIsNone(row["leave"])

    def test_leave_that_starts_tomorrow_does_not_lock_today(self):
        self.approve_leave(self.funke, TODAY + dt.timedelta(days=1), TODAY + dt.timedelta(days=4))

        row = self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.funke.pk]

        self.assertFalse(row["locked"])

    def test_leave_not_yet_approved_or_not_granted_locks_nobody(self):
        for status in ("PENDING", "REJECTED", "CANCELLED"):
            with self.subTest(status=status):
                leave = self.approve_leave(self.funke, self.days_ago(1), TODAY + dt.timedelta(days=3))
                leave.status = status
                leave.save(update_fields=["status"])
                row = self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.funke.pk]
                self.assertFalse(row["locked"])
                leave.delete()

    def test_leave_is_read_for_the_day_asked_not_for_today(self):
        self.approve_leave(self.funke, self.days_ago(7), self.days_ago(4))

        earlier = self.staff_rows(self.staff_day(self.registrar, self.days_ago(5)), self.lekki)
        today = self.staff_rows(self.staff_day(self.registrar), self.lekki)

        self.assertTrue(earlier[self.funke.pk]["locked"])
        self.assertFalse(today[self.funke.pk]["locked"])

    def test_the_longest_overlapping_leave_is_the_one_shown(self):
        self.approve_leave(self.funke, self.days_ago(2), TODAY + dt.timedelta(days=2), "SICK")
        longer = self.approve_leave(self.funke, self.days_ago(1), TODAY + dt.timedelta(days=9))

        row = self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.funke.pk]

        self.assertEqual(row["leave"]["id"], longer.pk)


class LeaveDetailsReadTests(StaffFixture):
    """The leave object on a locked row follows ``school.leave.view``, not the staff register's keys."""

    def test_a_caller_without_leave_view_gets_the_locked_row_with_no_leave_key(self):
        self.approve_leave(self.funke, self.days_ago(2), TODAY + dt.timedelta(days=10))

        row = self.staff_rows(self.staff_day(self.viewer), self.lekki)[self.funke.pk]

        self.assertTrue(row["locked"])
        self.assertEqual(row["lock_reason"], "ON_LEAVE")
        self.assertIn("On approved leave", row["lock_note"])
        self.assertNotIn("leave", row)

    def test_a_caller_with_both_keys_gets_the_leave_id_type_and_end_date(self):
        leave = self.approve_leave(self.funke, self.days_ago(2), TODAY + dt.timedelta(days=10))
        reader = self.make_actor(
            None, "leave.reader@brightfield.test", "Lena", "Reed",
            make_key_role(self.tenant, "Staff and leave", "staff_and_leave", (PERM_STAFF_VIEW, PERM_LEAVE_VIEW)),
        )

        row = self.staff_rows(self.staff_day(reader), self.lekki)[self.funke.pk]

        self.assertEqual(row["leave"], {
            "id": leave.pk, "type": "MATERNITY", "type_label": "Maternity",
            "end_date": TODAY + dt.timedelta(days=10),
        })

    def test_a_row_not_on_leave_carries_a_null_leave_for_everybody(self):
        row = self.staff_rows(self.staff_day(self.viewer), self.lekki)[self.funke.pk]

        self.assertIn("leave", row)
        self.assertIsNone(row["leave"])

    def test_the_people_with_no_home_branch_follow_the_same_rule(self):
        self.approve_leave(self.adaeze, self.days_ago(2), TODAY + dt.timedelta(days=3))

        without = self.data(self.staff_day(self.viewer))["no_home_branch"]["rows"][0]
        with_key = self.data(self.staff_day(self.registrar))["no_home_branch"]["rows"][0]

        self.assertNotIn("leave", without)
        self.assertIn("id", with_key["leave"])

    def test_the_refusal_to_mark_over_leave_is_the_same_without_the_key(self):
        self.approve_leave(self.funke, self.days_ago(2), TODAY + dt.timedelta(days=10))
        marker_with_leave_view = self.make_actor(
            None, "marker.leave@brightfield.test", "Mia", "Lee",
            make_key_role(self.tenant, "Marker and leave", "marker_leave", (PERM_STAFF_MARK, PERM_LEAVE_VIEW)),
        )

        for user in (self.marker, marker_with_leave_view):
            with self.subTest(user=user.email):
                response = self.mark_staff(user, self.lekki, self.funke, "P")
                self.assertEqual(response.status_code, 422, response.data)
                self.assertEqual(self.code(response), "ON_LEAVE")

    def test_a_write_re_reads_the_group_with_the_same_leave_rule(self):
        self.approve_leave(self.funke, self.days_ago(2), TODAY + dt.timedelta(days=10))
        both_but_leave = self.make_actor(
            None, "nl@brightfield.test", "Nell", "Roe",
            make_key_role(self.tenant, "Staff only", "staff_only", BOTH),
        )

        for user, expects in ((both_but_leave, False), (self.registrar, True)):
            with self.subTest(user=user.email):
                response = self.mark_staff(user, self.lekki, self.peter, "P")
                self.assertEqual(response.status_code, 200, response.data)
                row = self.staff_rows(response, self.lekki)[self.funke.pk]
                self.assertEqual("leave" in row, expects)

    def test_the_check_costs_no_more_queries_for_more_people_on_leave(self):
        def measure(user):
            self.staff_day(user)  # warms caches that are per process, not per request
            with CaptureQueriesContext(connection) as queries:
                response = self.staff_day(user)
            self.assertEqual(response.status_code, 200, response.data)
            return len(queries)

        before = {user.email: measure(user) for user in (self.viewer, self.registrar)}
        for number in range(8):
            person = self.make_person(f"Away{number}", "Staffer", self.lekki)
            self.approve_leave(person, self.days_ago(1), TODAY + dt.timedelta(days=2))

        for user in (self.viewer, self.registrar):
            self.assertEqual(measure(user), before[user.email], user.email)


class EmploymentTests(StaffFixture):
    def lekki_ids(self, on_date=TODAY):
        return set(self.staff_rows(self.staff_day(self.registrar, on_date), self.lekki))

    def test_somebody_hired_after_the_date_is_not_listed(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(hire_date=TODAY + dt.timedelta(days=1))

        self.assertNotIn(self.funke.pk, self.lekki_ids())

    def test_somebody_hired_on_the_date_is_listed(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(hire_date=TODAY)

        self.assertIn(self.funke.pk, self.lekki_ids())

    def test_a_past_register_does_not_list_somebody_hired_since(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(hire_date=self.days_ago(1))

        self.assertNotIn(self.funke.pk, self.lekki_ids(self.days_ago(3)))
        self.assertIn(self.funke.pk, self.lekki_ids())

    def test_somebody_whose_last_day_was_before_the_date_is_not_listed(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(
            employment_status="RESIGNED", exit_date=YESTERDAY,
        )

        self.assertNotIn(self.funke.pk, self.lekki_ids())

    def test_somebody_whose_last_day_is_to_come_is_listed_with_a_note(self):
        last = TODAY + dt.timedelta(days=5)
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(
            employment_status="RESIGNED", exit_date=last,
        )

        row = self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.funke.pk]

        self.assertFalse(row["locked"])
        self.assertEqual(row["exit"]["label"], "Resigned")
        self.assertEqual(row["exit"]["last_working_day"], last)
        self.assertEqual(
            row["exit"]["text"],
            f"Resigned · last working day {format_date(last, self.tenant, branch=self.lekki)}",
        )

    def test_somebody_whose_last_day_is_the_date_itself_is_listed(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(
            employment_status="TERMINATED", exit_date=TODAY,
        )

        row = self.staff_rows(self.staff_day(self.registrar), self.lekki)[self.funke.pk]

        self.assertEqual(row["exit"]["label"], "Terminated")

    def test_a_past_register_still_lists_somebody_who_has_since_left(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(
            employment_status="RESIGNED", exit_date=YESTERDAY,
        )

        self.assertIn(self.funke.pk, self.lekki_ids(self.days_ago(3)))

    def test_a_hire_still_awaiting_approval_is_not_employed_yet(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(employment_status="PENDING_APPROVAL")

        self.assertNotIn(self.funke.pk, self.lekki_ids())

    def test_somebody_off_the_roll_with_no_last_day_cannot_be_placed_on_a_day(self):
        StaffProfile.all_objects.filter(pk=self.funke.pk).update(
            employment_status="RESIGNED", exit_date=None,
        )

        self.assertNotIn(self.funke.pk, self.lekki_ids())

    def test_somebody_invited_but_not_yet_signed_in_still_works_there(self):
        for status in ("INVITED", "AWAITING_GO_LIVE", "SUSPENDED"):
            with self.subTest(status=status):
                StaffProfile.all_objects.filter(pk=self.funke.pk).update(employment_status=status)
                self.assertIn(self.funke.pk, self.lekki_ids())


class ClosedDayTests(StaffFixture):
    def test_a_school_wide_closure_leaves_no_register_in_any_branch(self):
        body = self.data(self.staff_day(self.registrar, FOUNDERS_DAY))

        self.assertEqual(body["closed"]["reason"], "Founders' Day")
        self.assertEqual(body["closed"]["code"], "CLOSED_BY_EVENT")
        for group in body["groups"]:
            self.assertEqual(group["status"], "CLOSED_DAY")
            self.assertEqual(group["rows"], [])
        self.assertIsNone(body["no_home_branch"])

    def test_a_branch_closure_closes_that_branch_alone(self):
        body = self.data(self.staff_day(self.registrar, LEKKI_OPEN_DAY))

        groups = {g["branch"]["id"]: g for g in body["groups"]}
        self.assertEqual(groups[self.lekki.pk]["closed"]["reason"], "Lekki Open Day")
        self.assertIsNone(groups[self.ikeja.pk]["closed"])
        self.assertEqual(len(groups[self.ikeja.pk]["rows"]), 2)
        self.assertIsNone(body["closed"])

    def test_a_closure_limited_to_a_level_does_not_close_the_staff_day(self):
        day = dt.date(2025, 10, 8)
        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=None, name="JSS1 Excursion",
            event_type="SCHOOL_EVENT", start_date=day, end_date=day, closes_school=True,
        )
        CalendarEventAudience.all_objects.create(tenant=self.tenant, event=event, level=self.jss1)

        body = self.data(self.staff_day(self.registrar, day))

        self.assertIsNone(body["closed"])
        for group in body["groups"]:
            self.assertIsNone(group["closed"])
            self.assertTrue(group["rows"])

    def test_a_weekend_is_not_a_teaching_day(self):
        body = self.data(self.staff_day(self.registrar, YESTERDAY))

        self.assertEqual(body["closed"]["code"], "NOT_TEACHING_DAY")

    def test_staff_are_not_held_to_term_time(self):
        holiday = dt.date(2025, 9, 1)

        body = self.data(self.staff_day(self.registrar, holiday))

        self.assertIsNone(body["closed"])

    def test_a_date_that_has_not_come_is_refused(self):
        response = self.staff_day(self.registrar, TODAY + dt.timedelta(days=1))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DATE_IN_FUTURE")

    def test_a_date_that_is_not_a_date_is_refused(self):
        response = self.staff_day(self.registrar, "yesterday")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "INVALID_DATE")


class ScopeTests(StaffFixture):
    def test_a_branch_admin_reads_only_their_own_branch(self):
        lekki = self.data(self.staff_day(self.lekki_hr))
        ikeja = self.data(self.staff_day(self.ikeja_hr))

        self.assertEqual([g["branch"]["id"] for g in lekki["groups"]], [self.lekki.pk])
        self.assertEqual([g["branch"]["id"] for g in ikeja["groups"]], [self.ikeja.pk])

    def test_a_branch_admin_naming_another_branch_is_told_it_does_not_exist(self):
        response = self.staff_day(self.lekki_hr, branch=self.ikeja.pk)

        self.assertEqual(response.status_code, 404)

    def test_another_schools_branch_is_a_404_too(self):
        response = self.staff_day(self.registrar, branch=self.solo_branch.pk)

        self.assertEqual(response.status_code, 404)

    def test_a_branch_that_does_not_exist_is_a_404(self):
        response = self.staff_day(self.registrar, branch=999999)

        self.assertEqual(response.status_code, 404)


class QueryCostTests(StaffFixture):
    """The number of queries does not grow with the number of people or branches."""

    def measure(self, user=None):
        client_user = user or self.registrar
        self.staff_day(client_user)  # warms caches that are per process, not per request
        with CaptureQueriesContext(connection) as queries:
            response = self.staff_day(client_user)
        self.assertEqual(response.status_code, 200, response.data)
        return len(queries)

    def test_more_people_cost_no_more_queries(self):
        before = self.measure()
        for number in range(12):
            person = self.make_person(f"Extra{number}", "Staffer", self.lekki, title="Teacher")
            if number % 3 == 0:
                self.approve_leave(person, self.days_ago(1), TODAY + dt.timedelta(days=2))
        self.make_person("Orphan", "Staffer", None)

        self.assertEqual(self.measure(), before)

    def test_more_branches_cost_no_more_queries(self):
        before = self.measure()
        third = make_branch(self.school, name="Yaba Branch", is_main=False)
        for number in range(3):
            self.make_person(f"Yaba{number}", "Staffer", third)

        self.assertEqual(self.measure(), before)

    def test_a_submitted_register_costs_the_same_however_many_people_are_on_it(self):
        self.lekki_everyone_marked()
        self.assertEqual(self.submit_staff(self.registrar, self.lekki).status_code, 200)
        before = self.measure()
        for number in range(6):
            self.make_person(f"Late{number}", "Joiner", self.lekki)

        self.assertEqual(self.measure(), before)
