"""The results hub and the teacher's hub."""
from __future__ import annotations

import datetime as dt

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_calendar.models import (
    CalendarEvent,
    EventType,
    Exam,
    ExamSlot,
    PublishState,
)
from schools.vs_gradebook.constants import SheetStatus, StoredStage
from schools.vs_gradebook.models import ClassTerm, ResultHold, SheetReminder
from schools.vs_staff.constants import TeachingPart
from schools.vs_staff.models import StaffProfile, TeachingAssignment
from vs_rbac.tests.helpers import make_branch

from .sheet_base import SheetFixture

FULL = (10, 10, 30)
JSS1A_ROLL = ("chiamaka", "tunde", "kelechi", "amina")


class HubFixture(SheetFixture):
    def hub(self, user, **query):
        return TenantAPIClient(user).get(reverse("gradebook-hub"), query)

    def mine(self, user, **query):
        return TenantAPIClient(user).get(reverse("gradebook-hub-mine"), query)

    def fill_jss1a(self, **kwargs):
        rows = {getattr(self, name): FULL for name in JSS1A_ROLL}
        return self.make_sheet(self.jss1a, self.maths, rows=rows, **kwargs)

    @staticmethod
    def late_for(body, class_name, subject_name):
        return next(
            row for row in body["late"]
            if row["class"]["name"] == class_name and row["subject"]["name"] == subject_name
        )



class AdminHubAccessTests(HubFixture):
    def test_the_hub_key_is_required(self):
        for user in (self.nobody, self.viewer, self.eze, self.enterer):
            with self.subTest(user=user.email):
                self.assertEqual(self.hub(user).status_code, 403)

    def test_a_branch_administrator_sees_only_their_branch(self):
        body = self.data(self.hub(self.lekki_admin))
        names = {c["name"] for c in body["classes"]}
        self.assertEqual(names, {"JSS1 A", "JSS1 B", "SSS1 A"})
        self.assertEqual(self.hub(self.lekki_admin, branch=self.ikeja.pk).status_code, 404)
        ikeja = self.data(self.hub(self.ikeja_admin))
        self.assertEqual({c["name"] for c in ikeja["classes"]}, {"JSS1 C"})

    def test_another_schools_branch_and_term_are_a_404(self):
        self.assertEqual(self.hub(self.admin, branch=self.solo_branch.pk).status_code, 404)
        self.assertEqual(self.hub(self.admin, term=self.solo_term.pk).status_code, 404)
        self.assertEqual(self.hub(self.solo_admin, term=self.first_term.pk).status_code, 404)

    def test_a_whole_school_reader_is_told_of_a_class_with_no_branch(self):
        body = self.data(self.hub(self.admin))
        self.assertEqual(body["classes_without_branch"], [{"id": self.shared.pk, "name": "JSS1 Shared"}])
        self.assertNotIn("JSS1 Shared", {c["name"] for c in body["classes"]})
        self.assertEqual(self.data(self.hub(self.lekki_admin))["classes_without_branch"], [])
        self.assertEqual(
            self.data(self.hub(self.admin, branch=self.lekki.pk))["classes_without_branch"], [],
        )


class AdminHubShapeTests(HubFixture):
    def test_the_term_and_its_dates(self):
        body = self.data(self.hub(self.admin))
        self.assertEqual(
            body["term"], {"id": self.first_term.pk, "name": "First Term", "session": "2025/2026"},
        )
        self.assertEqual(body["term_ends"], "2025-12-12")
        self.assertIsNone(body["results_day"])

    def test_the_planned_results_day_is_shown(self):
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Results day",
            event_type=EventType.RESULTS_DAY, start_date=dt.date(2025, 12, 16),
            end_date=dt.date(2025, 12, 16),
        )
        # The day falls after the term ends, so it belongs to no term yet.
        self.assertIsNone(self.data(self.hub(self.admin))["results_day"])
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Results day",
            event_type=EventType.RESULTS_DAY, start_date=dt.date(2025, 12, 11),
            end_date=dt.date(2025, 12, 11),
        )
        self.assertEqual(self.data(self.hub(self.admin))["results_day"], "2025-12-11")
        CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Ikeja results day", branch=self.ikeja,
            event_type=EventType.RESULTS_DAY, start_date=dt.date(2025, 12, 9),
            end_date=dt.date(2025, 12, 9),
        )
        self.assertEqual(self.data(self.hub(self.admin))["results_day"], "2025-12-11")
        self.assertEqual(
            self.data(self.hub(self.admin, branch=self.ikeja.pk))["results_day"], "2025-12-09",
        )
        # A reader whose only branch is Ikeja is told Ikeja's day.
        self.assertEqual(self.data(self.hub(self.ikeja_admin))["results_day"], "2025-12-09")

    def test_an_empty_branch_has_empty_lists_and_zero_counts(self):
        epe = make_branch(self.school, name="Epe Branch", is_main=False)
        body = self.data(self.hub(self.admin, branch=epe.pk))
        self.assertEqual(body["classes"], [])
        self.assertEqual(body["late"], [])
        self.assertEqual(
            body["kpis"],
            {"sheets_in": 0, "sheets_due": 0, "classes_compiled": 0, "awaiting_approval": 0,
             "published": 0, "held": 0},
        )

    def test_each_class_with_its_programme_teacher_and_counts(self):
        self.fill_jss1a(status=SheetStatus.SUBMITTED)
        body = self.data(self.hub(self.admin))
        row = next(c for c in body["classes"] if c["name"] == "JSS1 A")
        self.assertEqual(row["branch"], {"id": self.lekki.pk, "name": "Lekki Branch"})
        self.assertEqual(row["program"], {"id": self.junior.pk, "name": "Junior Secondary"})
        self.assertEqual(row["class_teacher"], {"id": self.eze_staff.pk, "name": "Chidi Eze"})
        self.assertEqual((row["subjects_in"], row["subjects_due"]), (1, 2))
        self.assertEqual(row["stage"], "ENTERING")
        self.assertFalse(row["returned"])

    def test_a_class_with_nobody_on_its_roll_owes_nothing(self):
        row = next(c for c in self.data(self.hub(self.admin))["classes"] if c["name"] == "JSS1 B")
        self.assertEqual((row["subjects_in"], row["subjects_due"], row["stage"]), (0, 0, "ENTERING"))
        self.assertEqual(row["class_teacher"]["name"], "Sade Bello")

    def test_an_optional_subject_is_due_only_where_someone_takes_it(self):
        body = self.data(self.hub(self.admin))
        subjects = {(r["class"]["name"], r["subject"]["name"]) for r in body["late"]}
        self.assertIn(("JSS1 A", "French"), subjects)
        self.assertNotIn(("JSS1 B", "French"), subjects)
        self.assertNotIn(("JSS1 C", "French"), subjects)

    def test_stages_follow_the_sheets_and_the_stored_decisions(self):
        self.fill_jss1a(status=SheetStatus.SUBMITTED)
        self.make_sheet(
            self.jss1a, self.french, status=SheetStatus.SUBMITTED,
            rows={self.chiamaka: FULL, self.tunde: FULL},
        )
        stages = lambda: {  # noqa: E731
            c["name"]: c["stage"] for c in self.data(self.hub(self.admin))["classes"]
        }
        self.assertEqual(stages()["JSS1 A"], "COMPILING")
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=StoredStage.AWAITING)
        self.assertEqual(stages()["JSS1 A"], "AWAITING")
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=StoredStage.PUBLISHED)
        self.assertEqual(stages()["JSS1 A"], "PUBLISHED")

    def test_a_class_returned_by_the_principal_says_so(self):
        self.fill_jss1a()
        ClassTerm.objects.filter(school_class=self.jss1a).update(returned_note="Fix the comments.")
        row = next(c for c in self.data(self.hub(self.admin))["classes"] if c["name"] == "JSS1 A")
        self.assertTrue(row["returned"])

    def test_the_counts(self):
        self.fill_jss1a(status=SheetStatus.SUBMITTED)
        self.make_sheet(
            self.jss1a, self.french, status=SheetStatus.APPROVED, needs_moderation=True,
            rows={self.chiamaka: FULL, self.tunde: FULL},
        )
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=StoredStage.AWAITING)
        self.make_sheet(self.jss1c, self.maths, status=SheetStatus.SUBMITTED, rows={
            self.sade: FULL, self.ife: FULL,
        })
        ClassTerm.objects.filter(school_class=self.jss1c).update(stage=StoredStage.PUBLISHED)
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.tunde, reason="Fees.",
        )
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.kelechi, reason="Fees.",
            lifted_at=dt.datetime(2025, 10, 1, tzinfo=dt.timezone.utc),
        )
        ResultHold.objects.create(
            tenant=self.tenant, term=self.second_term, student=self.amina, reason="Fees.",
        )
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.obinna, reason="Fees.",
        )
        kpis = self.data(self.hub(self.admin))["kpis"]
        self.assertEqual(kpis, {
            "sheets_in": 3, "sheets_due": 4, "classes_compiled": 2, "awaiting_approval": 1,
            "published": 1, "held": 1,
        })

    def test_holds_of_classes_the_reader_cannot_see_are_not_counted(self):
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.sade, reason="Fees.",
        )
        self.assertEqual(self.data(self.hub(self.admin))["kpis"]["held"], 1)
        self.assertEqual(self.data(self.hub(self.lekki_admin))["kpis"]["held"], 0)

    def test_the_named_term_is_the_one_counted(self):
        self.fill_jss1a(status=SheetStatus.SUBMITTED)
        first = self.data(self.hub(self.admin, term=self.first_term.pk))["kpis"]["sheets_in"]
        second = self.data(self.hub(self.admin, term=self.second_term.pk))["kpis"]["sheets_in"]
        self.assertEqual((first, second), (1, 0))


class LateSheetTests(HubFixture):
    def test_an_entered_count_is_the_default_reason(self):
        self.make_sheet(self.jss1a, self.maths, rows={
            self.chiamaka: FULL, self.tunde: FULL, self.kelechi: (10, None, 30),
        })
        row = self.late_for(self.data(self.hub(self.admin)), "JSS1 A", "Mathematics")
        self.assertEqual(row["reason_kind"], "ENTERED")
        self.assertEqual(row["reason"], "2 of 4 entered")
        self.assertEqual((row["entered"], row["roll"]), (2, 4))
        self.assertEqual(row["status"], "IN_PROGRESS")
        self.assertEqual(row["main_teacher"], {"id": self.eze_staff.pk, "name": "Chidi Eze"})
        self.assertFalse(row["reminded_today"])
        self.assertIsNone(row["staff_link"])

    def test_a_sheet_nobody_started_reads_nothing_entered(self):
        row = self.late_for(self.data(self.hub(self.admin)), "JSS1 A", "Mathematics")
        self.assertEqual((row["status"], row["reason"]), ("NOT_STARTED", "0 of 4 entered"))

    def test_a_sheet_that_is_in_is_not_late(self):
        self.fill_jss1a(status=SheetStatus.SUBMITTED)
        subjects = [(r["class"]["name"], r["subject"]["name"]) for r in self.data(self.hub(self.admin))["late"]]
        self.assertNotIn(("JSS1 A", "Mathematics"), subjects)

    def test_a_returned_sheet_names_who_returned_it(self):
        self.fill_jss1a(status=SheetStatus.RETURNED, returned_by=self.okeke, returned_note="Check.")
        row = self.late_for(self.data(self.hub(self.admin)), "JSS1 A", "Mathematics")
        self.assertEqual(row["reason_kind"], "RETURNED")
        self.assertEqual(row["reason"], "Returned by Ify Okeke")

    def test_a_sheet_waiting_for_its_head_says_so(self):
        self.moderate_seniors()
        self.make_sheet(
            self.sss1a, self.maths, status=SheetStatus.SUBMITTED, needs_moderation=True,
            rows={self.zainab: FULL, self.femi: FULL},
        )
        row = self.late_for(self.data(self.hub(self.admin)), "SSS1 A", "Mathematics")
        self.assertEqual(row["reason_kind"], "AWAITING_MODERATION")
        self.assertEqual(row["reason"], "Waiting for Ify Okeke to moderate it")

    def test_a_subject_with_no_main_teacher_says_so(self):
        row = self.late_for(self.data(self.hub(self.admin)), "JSS1 C", "Mathematics")
        self.assertEqual(row["reason_kind"], "NO_MAIN_TEACHER")
        self.assertEqual(row["reason"], "No main teacher assigned")
        self.assertIsNone(row["main_teacher"])

    def test_a_teacher_with_a_last_working_day_says_so(self):
        StaffProfile.all_objects.filter(pk=self.eze_staff.pk).update(exit_date=dt.date(2025, 11, 28))
        row = self.late_for(self.data(self.hub(self.admin)), "JSS1 A", "Mathematics")
        self.assertEqual(row["reason_kind"], "LAST_WORKING_DAY")
        self.assertEqual(row["reason"], "Last working day 28 Nov 2025")

    def test_the_order_is_returned_then_no_teacher_then_last_day_then_the_entered_count(self):
        StaffProfile.all_objects.filter(pk=self.eze_staff.pk).update(exit_date=dt.date(2025, 11, 28))
        self.fill_jss1a(status=SheetStatus.RETURNED, returned_by=self.okeke)
        row = self.late_for(self.data(self.hub(self.admin)), "JSS1 A", "Mathematics")
        self.assertEqual(row["reason_kind"], "RETURNED")

    def test_a_teacher_who_cannot_sign_in_is_named_only_to_a_reader_who_may_unlock_accounts(self):
        type(self.eze).objects.filter(pk=self.eze.pk).update(status="LOCKED")
        for user in (self.admin, self.lekki_admin, self.hub_reader):
            with self.subTest(user=user.email):
                row = self.late_for(self.data(self.hub(user)), "JSS1 A", "Mathematics")
                self.assertEqual(row["reason_kind"], "ENTERED")
                self.assertEqual(row["reason"], "0 of 4 entered")
                self.assertIsNone(row["staff_link"])
        row = self.late_for(self.data(self.hub(self.registrar)), "JSS1 A", "Mathematics")
        self.assertEqual(row["reason_kind"], "CANNOT_SIGN_IN")
        self.assertEqual(row["reason"], "Chidi Eze cannot sign in")
        self.assertEqual(row["staff_link"], self.eze_staff.pk)

    def test_a_reminder_sent_today_reads_reminded_and_yesterdays_does_not(self):
        sheet = self.make_sheet(self.jss1a, self.maths)
        SheetReminder.objects.create(
            tenant=self.tenant, sheet=sheet, on_date=dt.date(2025, 10, 12), sent_by=self.admin,
        )
        row = self.late_for(self.data(self.hub(self.admin)), "JSS1 A", "Mathematics")
        self.assertFalse(row["reminded_today"])
        SheetReminder.objects.create(
            tenant=self.tenant, sheet=sheet, on_date=dt.date(2025, 10, 13), sent_by=self.admin,
        )
        row = self.late_for(self.data(self.hub(self.admin)), "JSS1 A", "Mathematics")
        self.assertTrue(row["reminded_today"])

    def test_late_sheets_are_listed_by_class_then_subject(self):
        order = [(r["class"]["name"], r["subject"]["name"]) for r in self.data(self.hub(self.admin))["late"]]
        self.assertEqual(
            order,
            [("JSS1 A", "French"), ("JSS1 A", "Mathematics"), ("JSS1 C", "Mathematics"),
             ("SSS1 A", "Mathematics")],
        )

    def test_no_late_sheets_is_an_empty_list(self):
        epe = make_branch(self.school, name="Epe Branch", is_main=False)
        self.assertEqual(self.data(self.hub(self.admin, branch=epe.pk))["late"], [])


class BranchShapeTests(HubFixture):
    def test_a_school_with_several_branches_names_the_branch_on_each_row(self):
        body = self.data(self.hub(self.admin))
        self.assertTrue(body["branch_dimension"])
        self.assertEqual({c["branch"]["name"] for c in body["classes"]}, {"Lekki Branch", "Ikeja Branch"})

    def test_a_school_with_one_branch_carries_no_branch(self):
        body = self.data(self.hub(self.solo_admin, term=self.solo_term.pk))
        self.assertFalse(body["branch_dimension"])
        self.assertEqual(len(body["classes"]), 1)
        self.assertIsNone(body["classes"][0]["branch"])
        self.assertEqual(body["classes"][0]["name"], "Basic 3")
        late = body["late"][0]
        self.assertEqual((late["subject"]["name"], late["reason"]), ("Mathematics", "0 of 2 entered"))
        self.assertEqual(body["classes_without_branch"], [])


class QueryCountTests(HubFixture):
    def add_classes(self, count, start):
        for number in range(start, start + count):
            school_class = self.make_class(self.jss1, f"JSS1 X{number}", f"X{number}", self.lekki, None)
            pupils = [
                self.make_student("Pat", f"Okoro{number}{n}", school_class, f"BFS/9{number}{n}")
                for n in range(2)
            ]
            TeachingAssignment.all_objects.create(
                tenant=self.tenant, staff=self.eze_staff, school_class=school_class,
                subject=self.maths, session=self.year, part=TeachingPart.LEAD,
            )
            self.make_sheet(
                school_class, self.maths, rows={pupils[0]: FULL, pupils[1]: (10, None, 30)},
            )

    def measure(self, call):
        call()  # warm anything cached for the process
        with CaptureQueriesContext(connection) as queries:
            call()
        return len(queries)

    def test_the_hub_costs_the_same_however_many_classes_there_are(self):
        self.add_classes(1, 1)
        few = self.measure(lambda: self.assertEqual(self.hub(self.admin).status_code, 200))
        self.add_classes(4, 2)
        many = self.measure(lambda: self.assertEqual(self.hub(self.admin).status_code, 200))
        self.assertEqual(few, many)

    def test_the_moderation_queue_costs_the_same_however_many_sheets_there_are(self):
        self.moderate_seniors()

        def add(count, start):
            for number in range(start, start + count):
                school_class = self.make_class(self.sss1, f"SSS1 X{number}", f"SX{number}", self.lekki, None)
                pupil = self.make_student("Sam", f"Obi{number}", school_class, f"BFS/8{number}")
                self.make_sheet(
                    school_class, self.maths, status=SheetStatus.SUBMITTED, needs_moderation=True,
                    rows={pupil: FULL},
                )

        def queue():
            return TenantAPIClient(self.okeke).get(reverse("gradebook-moderation"))

        add(1, 1)
        few = self.measure(lambda: self.assertEqual(queue().status_code, 200))
        add(3, 2)
        many = self.measure(lambda: self.assertEqual(queue().status_code, 200))
        self.assertEqual(few, many)


class TeacherHubTests(HubFixture):
    def test_the_key_is_required(self):
        for user in (self.nobody, self.hub_reader):
            with self.subTest(user=user.email):
                self.assertEqual(self.mine(user).status_code, 403)

    def test_another_schools_term_is_a_404(self):
        self.assertEqual(self.mine(self.eze, term=self.solo_term.pk).status_code, 404)

    def test_the_main_teachers_sheets_with_entered_of_roll(self):
        self.make_sheet(self.jss1a, self.maths, rows={self.tunde: FULL, self.kelechi: FULL})
        body = self.data(self.mine(self.eze))
        self.assertEqual(body["term"]["name"], "First Term")
        [sheet] = body["sheets"]
        self.assertEqual(sheet["class"], {"id": self.jss1a.pk, "name": "JSS1 A"})
        self.assertEqual(sheet["subject"]["name"], "Mathematics")
        self.assertEqual((sheet["entered"], sheet["roll"]), (2, 4))
        self.assertEqual((sheet["status"], sheet["cta"]), ("IN_PROGRESS", "CONTINUE"))
        self.assertIsNone(sheet["exam_paper"])

    def test_the_call_to_action_follows_the_state(self):
        def cta():
            return self.data(self.mine(self.eze))["sheets"][0]["cta"]

        self.assertEqual(cta(), "ENTER")
        sheet = self.make_sheet(self.jss1a, self.maths, rows={self.tunde: FULL})
        self.assertEqual(cta(), "CONTINUE")
        for status, expected in (
            (SheetStatus.RETURNED, "REVISE"), (SheetStatus.SUBMITTED, "VIEW"),
            (SheetStatus.APPROVED, "VIEW"),
        ):
            type(sheet).objects.filter(pk=sheet.pk).update(status=status)
            self.assertEqual(cta(), expected, status)

    def test_a_sheet_that_is_in_reads_every_student_entered(self):
        self.fill_jss1a(status=SheetStatus.SUBMITTED)
        sheet = self.data(self.mine(self.eze))["sheets"][0]
        self.assertEqual((sheet["entered"], sheet["roll"]), (4, 4))

    def test_the_examination_paper_is_named(self):
        event = CalendarEvent.all_objects.create(
            tenant=self.tenant, session=self.year, name="Exams", event_type=EventType.EXAM_PERIOD,
            start_date=dt.date(2025, 12, 1), end_date=dt.date(2025, 12, 10),
        )
        exam = Exam.all_objects.create(
            tenant=self.tenant, calendar_event=event, name="First term",
            status=PublishState.PUBLISHED,
        )
        ExamSlot.all_objects.create(
            tenant=self.tenant, exam=exam, school_class=self.jss1a, subject=self.maths,
            exam_date=dt.date(2025, 12, 4), sitting="MORNING",
        )
        paper = self.data(self.mine(self.eze))["sheets"][0]["exam_paper"]
        self.assertEqual(paper["date"], "2025-12-04")
        self.assertEqual(paper["sitting"], "MORNING")

    def test_only_sheets_the_caller_leads_are_listed_not_those_they_assist(self):
        self.assertEqual(self.data(self.mine(self.bello))["sheets"], [])
        self.assertEqual(self.data(self.mine(self.musa)), {
            "term": {"id": self.first_term.pk, "name": "First Term", "session": "2025/2026"},
            "sheets": [], "duties": [],
        })

    def test_an_optional_subject_nobody_takes_is_not_listed(self):
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, staff=self.musa_staff, school_class=self.jss1b,
            subject=self.french, session=self.year, part=TeachingPart.LEAD,
        )
        self.assertEqual(self.data(self.mine(self.musa))["sheets"], [])

    def test_the_class_teacher_has_a_duty_for_each_class_they_lead(self):
        self.fill_jss1a(status=SheetStatus.SUBMITTED)
        body = self.data(self.mine(self.eze))
        [duty] = body["duties"]
        self.assertEqual(duty["kind"], "CLASS_TEACHER")
        self.assertEqual(duty["class"], {"id": self.jss1a.pk, "name": "JSS1 A"})
        self.assertEqual((duty["subjects_in"], duty["subjects_due"]), (1, 2))
        self.assertEqual(duty["stage"], "ENTERING")
        self.assertIsNone(duty["department"])
        self.assertIsNone(duty["waiting"])

    def test_the_head_of_a_department_has_a_duty_with_the_count_waiting(self):
        self.moderate_seniors()
        self.make_sheet(
            self.sss1a, self.maths, status=SheetStatus.SUBMITTED, needs_moderation=True,
            rows={self.zainab: FULL},
        )
        self.make_sheet(
            self.jss1a, self.maths, status=SheetStatus.APPROVED, needs_moderation=True,
            rows={self.tunde: FULL},
        )
        body = self.data(self.mine(self.okeke))
        [duty] = body["duties"]
        self.assertEqual(duty["kind"], "HEAD_OF_DEPARTMENT")
        self.assertEqual(duty["department"], {"id": self.sciences.pk, "name": "Sciences"})
        self.assertEqual(duty["waiting"], 1)
        self.assertIsNone(duty["class"])

    def test_a_head_of_a_department_with_nothing_moderated_has_no_duty(self):
        self.set_head(self.sciences, self.okeke_staff)
        self.assertEqual(self.data(self.mine(self.okeke))["duties"], [])

    def test_a_single_branch_teacher_sees_their_own_sheet_and_class(self):
        body = self.data(self.mine(self.solo_teacher, term=self.solo_term.pk))
        self.assertEqual([s["subject"]["name"] for s in body["sheets"]], ["Mathematics"])
        self.assertEqual([d["kind"] for d in body["duties"]], ["CLASS_TEACHER"])
