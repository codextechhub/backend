"""The two Today screens: the administrator's hub and a teacher's own registers.

Monday 13 October 2025. JSS1 A (Eze, Lekki) and SSS2 Science (Ikeja) have taken
their registers; JSS1 B (Bello, Lekki) has not, and has been reminded; Primary 4
A has nobody placed; JSS1 Shared belongs to no branch. Sunrise is the school
with one branch.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from schools.vs_academics.models import SchoolClass
from schools.vs_calendar.models import CalendarEvent, CalendarEventAudience
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_branch,
    make_school,
    make_school_admin,
    set_field_access,
)

from ..constants import PERM_REGISTER_MARK_ANY, PERM_REGISTER_VIEW, NoticeState
from ..models import ClassRegister, RegisterReminder, StudentMark
from ..services.rates import rate_percent
from .base import (
    FOUNDERS_DAY,
    LAST_FRIDAY,
    LEKKI_OPEN_DAY,
    TODAY,
    AttendanceFixture,
    make_key_role,
)

SEND = "schools.vs_attendance.services.notices.send_notification"


class RateTests(SimpleTestCase):
    def test_present_over_present_absent_and_excused_to_one_decimal(self):
        self.assertEqual(rate_percent(3, 1, 1), 60.0)
        self.assertEqual(rate_percent(2, 1, 0), 66.7)
        self.assertEqual(rate_percent(1, 0, 2), 33.3)
        self.assertEqual(rate_percent(1, 0, 7), 12.5)

    def test_halves_round_up_not_to_even(self):
        self.assertEqual(rate_percent(1, 0, 15), 6.3)
        self.assertEqual(rate_percent(7, 0, 1), 87.5)

    def test_a_school_with_no_register_has_no_rate_rather_than_zero(self):
        self.assertIsNone(rate_percent(0, 0, 0))
        self.assertEqual(rate_percent(0, 2, 0), 0.0)


class _Hub(AttendanceFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.zainab = cls.make_student("Zainab", "Aliyu", cls.jss1b, "BFS/301")
        cls.emeka = cls.make_student("Emeka", "Obi", cls.jss1b, "BFS/302")
        cls.ngozi = cls.make_student("Ngozi", "Ibe", cls.sss2, "BFS/401", branch=cls.ikeja)
        cls.ada = cls.make_student("Ada", "Eze", cls.sss2, "BFS/402", branch=cls.ikeja)
        cls.reminder = RegisterReminder.objects.create(
            tenant=cls.tenant, school_class=cls.jss1b, date=TODAY, sent_by=cls.admin,
        )

    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def take_both(self):
        """JSS1 A: Chiamaka absent, Tunde late. SSS2: Ada excused, Ngozi present."""
        self.assertEqual(
            self.take(self.eze, self.jss1a, TODAY, chiamaka="A", tunde="L").status_code, 200,
        )
        self.assertEqual(
            self.take(self.ikeja_admin, self.sss2, TODAY, ada="E").status_code, 200,
        )

    def get(self, user=None, **params):
        response = self.today(user or self.admin, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)


class AdminHubTests(_Hub):
    def test_the_rate_and_the_counts_come_from_the_registers_taken(self):
        self.take_both()
        kpis = self.get()["kpis"]
        # Tunde (late) and Kelechi, Ngozi present; Chiamaka absent; Ada excused.
        self.assertEqual(kpis["rate"], 60.0)
        self.assertEqual(
            (kpis["absent"], kpis["late"], kpis["excused"], kpis["not_taken"]), (1, 1, 1, 1),
        )
        self.assertEqual(kpis["corrections_waiting"], 0)

    def test_a_suspended_child_is_counted_nowhere(self):
        self.take_both()
        body = self.get()
        self.assertNotIn("Amina Yusuf", [r["student"]["name"] for r in body["absent"]])
        self.assertEqual(sum(1 for _ in body["absent"]), 2)

    def test_the_classes_whose_register_is_due_and_not_taken_are_listed(self):
        self.take_both()
        [row] = self.get()["not_taken"]
        self.assertEqual(row, {
            "class_id": self.jss1b.pk, "class_name": "JSS1 B", "level_name": "JSS1",
            "class_teacher_name": "Sade Bello",
            "branch": {"id": self.lekki.pk, "name": "Lekki Branch"}, "reminded": True,
        })

    def test_a_class_nobody_is_placed_in_is_neither_taken_nor_missing(self):
        body = self.get()
        names = {r["class_name"] for r in body["not_taken"]}
        self.assertNotIn("Primary 4 A", names)
        self.assertEqual(names, {"JSS1 A", "JSS1 B", "SSS2 Science"})

    def test_a_class_not_reminded_reads_not_reminded(self):
        rows = {r["class_name"]: r for r in self.get()["not_taken"]}
        self.assertTrue(rows["JSS1 B"]["reminded"])
        self.assertFalse(rows["JSS1 A"]["reminded"])

    def test_absent_and_excused_children_are_listed_with_their_notice_state(self):
        self.take_both()
        StudentMark.objects.filter(student=self.ada).update(note="Clinic")
        rows = self.get()["absent"]
        by_name = {r["student"]["name"]: r for r in rows}
        self.assertEqual(set(by_name), {"Chiamaka Nwosu", "Ada Eze"})
        chiamaka = by_name["Chiamaka Nwosu"]
        self.assertEqual(
            (chiamaka["mark"], chiamaka["class_name"], chiamaka["notice_state"]),
            ("A", "JSS1 A", NoticeState.SENT),
        )
        self.assertIsNotNone(chiamaka["notice_sent_at"])
        self.assertEqual(chiamaka["student"]["student_number"], "BFS/001")
        ada = by_name["Ada Eze"]
        self.assertEqual((ada["mark"], ada["reason_label"], ada["note"]), ("E", "Sick", "Clinic"))

    def test_a_register_not_yet_submitted_adds_nothing_to_the_counts(self):
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        body = self.get()
        self.assertEqual(body["kpis"]["absent"], 0)
        self.assertEqual(body["absent"], [])
        self.assertIsNone(body["kpis"]["rate"])

    def test_the_hub_names_the_branch_dimension_and_the_classes_with_no_branch(self):
        body = self.get()
        self.assertTrue(body["branch_dimension"])
        self.assertEqual(body["classes_without_branch"], ["JSS1 Shared"])
        self.assertNotIn("JSS1 Shared", {r["class_name"] for r in body["not_taken"]})
        self.assertEqual(body["date"], "2025-10-13")

    def test_the_branch_filter_narrows_every_list(self):
        self.take_both()
        body = self.get(branch=self.ikeja.pk)
        self.assertEqual(body["not_taken"], [])
        self.assertEqual([r["student"]["name"] for r in body["absent"]], ["Ada Eze"])
        self.assertEqual(body["classes_without_branch"], [])
        self.assertEqual(body["kpis"]["rate"], 50.0)
        lekki = self.get(branch=self.lekki.pk)
        self.assertEqual([r["class_name"] for r in lekki["not_taken"]], ["JSS1 B"])
        self.assertEqual([r["student"]["name"] for r in lekki["absent"]], ["Chiamaka Nwosu"])

    def test_a_branch_admin_sees_only_their_branch_and_cannot_ask_for_another(self):
        self.take_both()
        body = self.get(self.ikeja_admin)
        self.assertEqual([r["student"]["name"] for r in body["absent"]], ["Ada Eze"])
        self.assertEqual(body["not_taken"], [])
        self.assertEqual(body["classes_without_branch"], [])
        self.assertEqual(self.today(self.ikeja_admin, branch=self.lekki.pk).status_code, 404)
        lekki = self.get(self.lekki_admin)
        self.assertEqual([r["class_name"] for r in lekki["not_taken"]], ["JSS1 B"])
        self.assertEqual(lekki["kpis"]["rate"], 66.7)

    def test_an_unknown_branch_and_another_schools_branch_are_not_found(self):
        self.assertEqual(self.today(self.admin, branch=999999).status_code, 404)
        self.assertEqual(self.today(self.admin, branch=self.solo_branch.pk).status_code, 404)

    def test_the_first_correction_waiting_is_shown_to_the_people_who_decide(self):
        self.take(self.eze, self.jss1a, LAST_FRIDAY, tunde="A")
        row = self.data(self.correct(
            self.eze, self.jss1a, self.tunde, "P", reason_code=self.reason_id(),
        ))
        body = self.get()
        self.assertEqual(body["kpis"]["corrections_waiting"], 1)
        self.assertEqual(body["pending_corrections_preview"]["id"], row["id"])
        self.assertEqual(body["pending_corrections_preview"]["student"]["name"], "Tunde Bello")
        self.assertEqual(self.get(self.ikeja_admin)["kpis"]["corrections_waiting"], 0)
        self.assertIsNone(self.get(self.ikeja_admin)["pending_corrections_preview"])

    def test_a_correction_you_asked_for_is_not_waiting_on_you(self):
        self.take(self.eze, self.jss1a, LAST_FRIDAY, tunde="A")
        self.correct(self.admin, self.jss1a, self.tunde, "P", reason_code=self.reason_id())
        mine = self.get(self.admin)
        self.assertEqual(mine["kpis"]["corrections_waiting"], 0)
        self.assertIsNone(mine["pending_corrections_preview"])
        self.assertEqual(self.get(self.lekki_admin)["kpis"]["corrections_waiting"], 1)

    def test_a_registrar_who_cannot_decide_sees_neither_the_count_nor_the_correction(self):
        role = make_key_role(
            self.tenant, "Registrar", "registrar",
            (PERM_REGISTER_VIEW, PERM_REGISTER_MARK_ANY),
        )
        registrar = make_school_admin(
            None, email="registrar@brightfield.test", tenant=self.tenant,
        )
        make_assignment(self.tenant, registrar, role, branch=None)
        self.take(self.eze, self.jss1a, LAST_FRIDAY, tunde="A")
        self.correct(self.eze, self.jss1a, self.tunde, "P", reason_code=self.reason_id())
        body = self.get(registrar)
        self.assertNotIn("corrections_waiting", body["kpis"])
        self.assertIsNone(body["pending_corrections_preview"])


class DueTests(_Hub):
    def test_a_school_wide_closure_closes_the_whole_hub(self):
        body = self.get(date=str(FOUNDERS_DAY))
        self.assertEqual(body["closed"], {"reason": "Founders' Day", "code": "CLOSED_BY_EVENT"})
        self.assertEqual((body["not_taken"], body["absent"]), ([], []))
        self.assertEqual(body["kpis"]["not_taken"], 0)
        self.assertIsNone(body["kpis"]["rate"])

    def test_a_weekend_closes_the_whole_hub(self):
        body = self.get(date="2025-10-11")
        self.assertEqual(body["closed"]["code"], "NOT_TEACHING_DAY")
        self.assertEqual(body["not_taken"], [])

    def test_a_branch_closure_takes_that_branchs_classes_out_of_the_missing_list(self):
        body = self.get(date=str(LEKKI_OPEN_DAY))
        self.assertIsNone(body["closed"])
        self.assertEqual([r["class_name"] for r in body["not_taken"]], ["SSS2 Science"])

    def test_the_day_is_closed_for_a_scope_that_is_wholly_closed(self):
        body = self.get(date=str(LEKKI_OPEN_DAY), branch=self.lekki.pk)
        self.assertEqual(body["closed"]["reason"], "Lekki Open Day")
        self.assertEqual(body["not_taken"], [])

    def test_a_closure_narrowed_to_a_level_closes_only_that_levels_classes(self):
        day = dt.date(2025, 10, 8)
        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.lekki, name="Speech Day",
            event_type="SCHOOL_EVENT", start_date=day, end_date=day, closes_school=True,
        )
        CalendarEventAudience.all_objects.create(
            tenant=self.tenant, event=event, level=self.pry4,
        )
        self.assertEqual(
            {r["class_name"] for r in self.get(date=str(day))["not_taken"]},
            {"JSS1 A", "JSS1 B", "SSS2 Science"},
        )
        jss1 = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, branch=self.lekki, name="JSS1 trip",
            event_type="SCHOOL_EVENT", start_date=day, end_date=day, closes_school=True,
        )
        CalendarEventAudience.all_objects.create(
            tenant=self.tenant, event=jss1, level=self.jss1,
        )
        self.assertEqual(
            [r["class_name"] for r in self.get(date=str(day))["not_taken"]],
            ["SSS2 Science"],
        )

    def test_a_past_day_with_no_register_is_missing(self):
        body = self.get(date=str(LAST_FRIDAY))
        self.assertEqual(len(body["not_taken"]), 3)

    def test_a_day_that_has_not_come_is_refused(self):
        response = self.today(self.admin, date="2025-10-14")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DATE_IN_FUTURE")
        self.assertEqual(self.today(self.admin, date="soon").status_code, 400)

    def test_a_school_with_no_classes_gets_an_empty_hub(self):
        school = make_school(slug="emptyfield", name="Emptyfield")
        make_branch(school, name="Main Branch", is_main=True)
        role = make_key_role(
            school.tenant, "School Admin", "school_admin",
            (PERM_REGISTER_VIEW, PERM_REGISTER_MARK_ANY),
        )
        admin = make_school_admin(None, email="a@emptyfield.test", tenant=school.tenant)
        make_assignment(school.tenant, admin, role, branch=None)
        body = self.get(admin)
        self.assertIsNone(body["closed"])
        self.assertEqual((body["not_taken"], body["absent"]), ([], []))
        self.assertEqual(body["classes_without_branch"], [])
        self.assertFalse(body["branch_dimension"])
        self.assertIsNone(body["kpis"]["rate"])


class SingleBranchTests(_Hub):
    def test_the_dimension_recedes_at_a_school_with_one_branch(self):
        body = self.get(self.solo_admin)
        self.assertFalse(body["branch_dimension"])
        [row] = body["not_taken"]
        self.assertEqual(row["class_name"], "Basic 3")
        self.assertIsNone(row["branch"])
        self.assertEqual(row["level_name"], "Basic 3")
        self.assertEqual(body["classes_without_branch"], [])

    def test_a_class_with_no_branch_is_the_only_branchs_there(self):
        self.take(self.solo_teacher, self.solo_class, TODAY)
        body = self.get(self.solo_admin)
        self.assertEqual(body["not_taken"], [])
        self.assertEqual(body["kpis"]["rate"], 100.0)

    def test_the_branch_filter_works_with_the_only_branch(self):
        body = self.get(self.solo_admin, branch=self.solo_branch.pk)
        self.assertEqual(len(body["not_taken"]), 1)


class HubSecurityTests(_Hub):
    def test_a_teacher_is_pointed_to_their_own_screen(self):
        response = self.today(self.eze)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.code(response), "ADMINISTRATORS_ONLY")
        self.assertIn("today/mine/", response.data["message"])

    def test_someone_with_no_attendance_key_is_refused(self):
        self.assertEqual(self.today(self.nobody).status_code, 403)
        self.assertEqual(self.today_mine(self.nobody).status_code, 403)

    def test_another_schools_classes_never_appear(self):
        body = self.get(self.solo_admin)
        self.assertEqual({r["class_name"] for r in body["not_taken"]}, {"Basic 3"})
        mine = self.get(self.admin)
        self.assertNotIn("Basic 3", {r["class_name"] for r in mine["not_taken"]})

    def test_a_hidden_count_leaves_the_rest_of_the_hub(self):
        self.take_both()
        install_declared_fields("attendance.register")
        set_field_access(self.admin_role, "attendance.register.counts", read=False, write=False)
        body = self.get()
        self.assertNotIn("kpis", body)
        self.assertIn("not_taken", body)
        set_field_access(self.admin_role, "attendance.register.status", read=False, write=False)
        self.assertNotIn("not_taken", self.get())

    def test_a_hidden_mark_note_and_reason_leave_the_absent_rows_bare(self):
        self.take_both()
        install_declared_fields("attendance.register")
        set_field_access(
            self.admin_role, "attendance.register.mark", "attendance.register.note",
            "attendance.register.reason_code", "attendance.register.notice_state",
            read=False, write=False,
        )
        for row in self.get()["absent"]:
            self.assertEqual(set(row), {"student", "class_name"})


class QueryCountTests(_Hub):
    def count(self, **params):
        with CaptureQueriesContext(connection) as queries:
            response = self.today(self.admin, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return len(queries), self.data(response)

    def add_ikeja_class(self, number, *, submitted):
        school_class = self.make_class(self.jss1, f"Extra {number}", f"EX{number}", self.ikeja, self.bello_staff)
        pupils = [
            self.make_student(f"Pupil{number}{n}", "Test", school_class, f"EX/{number}{n}", branch=self.ikeja)
            for n in range(3)
        ]
        if submitted:
            register = ClassRegister.objects.create(
                tenant=self.tenant, school_class=school_class, date=TODAY,
                branch=self.ikeja, session=self.year, term=self.first_term,
                submitted_at=dt.datetime(2025, 10, 13, 8, 0, tzinfo=dt.timezone.utc),
                submitted_by=self.bello,
            )
            for pupil, mark in zip(pupils, "PAE"):
                StudentMark.objects.create(
                    tenant=self.tenant, register=register, student=pupil, mark=mark,
                    reason_code_id=self.reason_id() if mark == "E" else None,
                    marked_by=self.bello, notice_state=NoticeState.NONE,
                )
        else:
            RegisterReminder.objects.create(
                tenant=self.tenant, school_class=school_class, date=TODAY, sent_by=self.admin,
            )

    def test_the_number_of_queries_does_not_grow_with_the_classes(self):
        self.assertEqual(
            self.take(self.ikeja_admin, self.sss2, TODAY, ada="E").status_code, 200,
        )
        self.count(branch=self.ikeja.pk)
        one_class, body = self.count(branch=self.ikeja.pk)
        self.assertEqual(len(body["absent"]), 1)
        for number in range(4):
            self.add_ikeja_class(number, submitted=number % 2 == 0)
        five_classes, body = self.count(branch=self.ikeja.pk)
        self.assertEqual(five_classes, one_class)
        self.assertEqual(len(body["absent"]), 5)
        self.assertEqual(len(body["not_taken"]), 2)

    def test_the_whole_school_hub_is_just_as_bounded(self):
        self.assertEqual(self.take(self.eze, self.jss1a, TODAY, tunde="A").status_code, 200)
        self.count()
        before, _ = self.count()
        for number in range(3):
            self.add_ikeja_class(number, submitted=number != 1)
        after, body = self.count()
        self.assertEqual(after, before)
        self.assertEqual(len(body["not_taken"]), 3)


class TeacherHubTests(_Hub):
    def test_a_teacher_sees_the_class_they_lead_and_where_its_register_stands(self):
        body = self.data(self.today_mine(self.eze))
        self.assertEqual(body["closed"], None)
        [card] = body["classes"]
        self.assertEqual(card["class_id"], self.jss1a.pk)
        self.assertEqual(card["class_name"], "JSS1 A")
        self.assertEqual(card["student_count"], 3)
        self.assertEqual(card["status"], "NOT_STARTED")
        self.assertIsNone(card["submitted_at"])
        self.assertIsNone(card["submitted_by_name"])

    def test_the_status_follows_the_register(self):
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.assertEqual(self.data(self.today_mine(self.eze))["classes"][0]["status"], "IN_PROGRESS")
        self.fill(self.eze, self.jss1a)
        self.assertEqual(self.data(self.today_mine(self.eze))["classes"][0]["status"], "READY")
        self.submit(self.eze, self.jss1a)
        [card] = self.data(self.today_mine(self.eze))["classes"]
        self.assertEqual(card["status"], "SUBMITTED")
        self.assertIsNotNone(card["submitted_at"])
        self.assertEqual(card["submitted_by_name"], "Chidi Eze")

    def test_a_teacher_sees_only_their_own_classes(self):
        [card] = self.data(self.today_mine(self.bello))["classes"]
        self.assertEqual((card["class_name"], card["student_count"]), ("JSS1 B", 2))

    def test_someone_who_leads_no_class_gets_an_empty_list(self):
        response = self.today_mine(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.data(response)["classes"], [])

    def test_a_closed_day_is_carried_on_the_screen_and_the_card(self):
        body = self.data(self.today_mine(self.eze, date=str(FOUNDERS_DAY)))
        self.assertEqual(body["closed"]["reason"], "Founders' Day")
        self.assertEqual(body["classes"][0]["status"], "CLOSED_DAY")
        self.assertEqual(body["classes"][0]["student_count"], 0)

    def test_another_schools_teacher_sees_only_their_own_school(self):
        [card] = self.data(self.today_mine(self.solo_teacher))["classes"]
        self.assertEqual(card["class_name"], "Basic 3")
        self.assertEqual(self.data(self.today_mine(self.eze))["classes"][0]["class_name"], "JSS1 A")

    def test_a_class_taken_out_of_service_is_not_listed(self):
        SchoolClass.all_objects.filter(pk=self.jss1a.pk).update(is_active=False)
        self.assertEqual(self.data(self.today_mine(self.eze))["classes"], [])

    def test_a_day_that_has_not_come_is_refused(self):
        self.assertEqual(self.today_mine(self.eze, date="2025-10-14").status_code, 422)

    def test_the_status_and_the_taker_have_their_own_switches(self):
        self.take(self.eze, self.jss1a, TODAY)
        install_declared_fields("attendance.register")
        set_field_access(
            self.teacher_role, "attendance.register.status", "attendance.register.taken_by",
            "attendance.register.counts", read=False, write=False,
        )
        [card] = self.data(self.today_mine(self.eze))["classes"]
        self.assertEqual(set(card), {"class_id", "class_name"})


class ClassTeacherSwitchTests(_Hub):
    """The class teacher's name on the hub follows the Class teacher switch."""

    def hide_class_teacher(self):
        install_declared_fields("academics.classes")
        set_field_access(
            self.admin_role, "academics.classes.class_teacher", read=False, write=False,
        )

    def test_a_reader_who_may_not_read_it_gets_the_row_without_the_name(self):
        self.take_both()
        self.hide_class_teacher()
        [row] = self.get()["not_taken"]
        self.assertNotIn("class_teacher_name", row)
        self.assertEqual(row["class_name"], "JSS1 B")

    def test_the_reminder_answer_leaves_it_out_too(self):
        self.hide_class_teacher()
        response = self.remind(self.admin, self.jss1a)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("class_teacher_name", self.data(response))

    def test_the_register_header_leaves_it_out(self):
        self.hide_class_teacher()
        response = self.read(self.admin, self.jss1a)
        self.assertEqual(response.status_code, 200, response.data)
        school_class = self.data(response)["school_class"]
        self.assertNotIn("class_teacher_name", school_class)
        self.assertEqual(school_class["name"], "JSS1 A")

    def test_the_name_is_there_with_the_switch_open(self):
        install_declared_fields("academics.classes")
        self.take_both()
        [row] = self.get()["not_taken"]
        self.assertEqual(row["class_teacher_name"], "Sade Bello")
