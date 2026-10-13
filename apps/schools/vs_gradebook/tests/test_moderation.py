"""Moderation: the queue, and the head's decision."""
from __future__ import annotations

from unittest import mock

from vs_audit.models import AuditEvent

from schools.vs_gradebook.constants import SheetStatus
from schools.vs_gradebook.models import Score, ScoreSheet

from .sheet_base import SheetFixture

SEND = "schools.vs_gradebook.services.moderation.send_notification"


class ModerationFixture(SheetFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.moderate_seniors()
        cls.sss1b = cls.make_class(cls.sss1, "SSS1 B", "SSS1B", cls.lekki, None)
        cls.sss1c = cls.make_class(cls.sss1, "SSS1 C", "SSS1C", cls.ikeja, None)
        cls.yemi = cls.make_student("Yemi", "Cole", cls.sss1b, "BFS/020")
        cls.dayo = cls.make_student("Dayo", "King", cls.sss1b, "BFS/021")
        cls.tosin = cls.make_student("Tosin", "Ade", cls.sss1c, "BFS/022", branch=cls.ikeja)

    def submitted(self, school_class=None, rows=None, **kwargs):
        school_class = school_class or self.sss1a
        rows = rows or {self.zainab: (80, 70, 90), self.femi: (40, 30, 20)}
        kwargs.setdefault("status", SheetStatus.SUBMITTED)
        kwargs.setdefault("needs_moderation", True)
        return self.make_sheet(school_class, self.maths, rows=rows, **kwargs)

    def queue(self, user, **query):
        from django.urls import reverse
        from core.test_utils import TenantAPIClient

        return TenantAPIClient(user).get(reverse("gradebook-moderation"), query)


class QueueTests(ModerationFixture):
    def test_the_head_sees_the_moderated_sheets_of_their_department(self):
        self.submitted()
        body = self.data(self.queue(self.okeke))
        self.assertEqual(len(body["sheets"]), 1)
        row = body["sheets"][0]
        self.assertEqual(row["class"]["name"], "SSS1 A")
        self.assertEqual(row["subject"]["name"], "Mathematics")
        self.assertEqual(row["main_teacher"]["name"], "Kunle Adewale")
        self.assertEqual(row["status"], "SUBMITTED")
        self.assertTrue(row["can_decide"])

    def test_the_figures_are_the_mean_percentage_extreme_totals_and_pass_rate(self):
        self.submitted(rows={
            self.zainab: (20, 20, 40),    # 80
            self.femi: (10, 5, 10),       # 25, a fail
        })
        row = self.data(self.queue(self.okeke))["sheets"][0]
        self.assertEqual(row["class_average"], "52.50")
        self.assertEqual((row["highest"], row["lowest"]), (80, 25))
        self.assertEqual(row["pass_rate"], 50)
        self.assertEqual((row["marked"], row["roll"]), (2, 2))

    def test_marked_counts_complete_rows_of_the_roll(self):
        self.submitted(rows={self.zainab: (20, 20, 40), self.femi: (10, None, 10)})
        row = self.data(self.queue(self.okeke))["sheets"][0]
        self.assertEqual((row["marked"], row["roll"]), (1, 2))
        self.assertEqual(row["highest"], 80)

    def test_compare_names_the_other_classes_of_the_programme_with_that_subjects_sheet(self):
        self.submitted()
        self.make_sheet(self.sss1b, self.maths, status=SheetStatus.IN_PROGRESS)
        self.make_sheet(self.jss1a, self.maths)
        row = self.data(self.queue(self.okeke))["sheets"][0]
        self.assertEqual(row["compare"], ["SSS1 B"])

    def test_a_hub_reader_sees_every_moderated_sheet_and_decides_none(self):
        self.submitted()
        self.submitted(self.sss1b, rows={self.yemi: (10, 10, 10), self.dayo: (20, 20, 20)})
        rows = self.data(self.queue(self.hub_reader))["sheets"]
        self.assertEqual({r["class"]["name"] for r in rows}, {"SSS1 A", "SSS1 B"})
        self.assertEqual({r["can_decide"] for r in rows}, {False})

    def test_a_hub_reader_who_is_also_the_head_decides(self):
        self.set_head(self.sciences, self.okeke_staff)
        self.submitted()
        rows = self.data(self.queue(self.okeke))["sheets"]
        self.assertTrue(rows[0]["can_decide"])

    def test_a_sheet_already_decided_cannot_be_decided_again(self):
        self.submitted(status=SheetStatus.APPROVED)
        row = self.data(self.queue(self.okeke))["sheets"][0]
        self.assertEqual(row["status"], "APPROVED")
        self.assertFalse(row["can_decide"])

    def test_a_teacher_who_heads_nothing_has_an_empty_queue(self):
        self.submitted()
        body = self.data(self.queue(self.musa))
        self.assertEqual(body["sheets"], [])

    def test_a_sheet_that_needs_no_moderation_is_not_queued(self):
        self.submitted(needs_moderation=False)
        self.assertEqual(self.data(self.queue(self.okeke))["sheets"], [])

    def test_only_the_named_term_is_listed(self):
        self.submitted()
        self.assertEqual(
            self.data(self.queue(self.okeke, term=self.second_term.pk))["sheets"], [],
        )
        self.assertEqual(len(self.data(self.queue(self.okeke, term=self.first_term.pk))["sheets"]), 1)

    def test_a_branch_bound_reader_sees_only_their_branch_and_a_branch_filter_narrows(self):
        self.submitted()
        self.submitted(self.sss1c, rows={self.tosin: (10, 10, 10)}, branch=self.ikeja)
        names = lambda body: {r["class"]["name"] for r in body["sheets"]}  # noqa: E731
        self.assertEqual(names(self.data(self.queue(self.hub_reader))), {"SSS1 A", "SSS1 C"})
        lekki = self.make_user(
            "lekki-hub@brightfield.test", "Lekki", "Hub", self.hub_role, branch=self.lekki,
        )
        self.assertEqual(names(self.data(self.queue(lekki))), {"SSS1 A"})
        self.assertEqual(self.queue(lekki, branch=self.ikeja.pk).status_code, 404)
        self.assertEqual(
            names(self.data(self.queue(self.hub_reader, branch=self.ikeja.pk))), {"SSS1 C"},
        )

    def test_the_branch_is_named_at_a_school_with_several_and_the_dimension_is_flagged(self):
        self.submitted()
        body = self.data(self.queue(self.okeke))
        self.assertTrue(body["branch_dimension"])
        self.assertEqual(body["sheets"][0]["branch"]["name"], "Lekki Branch")

    def test_no_key_is_a_403_and_another_schools_branch_a_404(self):
        self.assertEqual(self.queue(self.nobody).status_code, 403)
        self.assertEqual(self.queue(self.viewer).status_code, 403)
        self.assertEqual(self.queue(self.admin, branch=self.solo_branch.pk).status_code, 404)
        self.assertEqual(self.queue(self.admin, term=self.solo_term.pk).status_code, 404)


class DecisionTests(ModerationFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_head_approves_and_the_sheet_is_in(self):
        self.submitted()
        response = self.approve(self.okeke, self.sss1a, self.maths)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["status"], "APPROVED")
        sheet = ScoreSheet.objects.get()
        self.assertEqual(sheet.moderated_by, self.okeke)
        self.assertIsNotNone(sheet.moderated_at)
        self.assertEqual(self.send.call_count, 0)

    def test_the_decision_is_audited(self):
        self.submitted()
        self.approve(self.okeke, self.sss1a, self.maths)
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="ScoreSheet")
        self.assertIn("approved Mathematics, SSS1 A, First Term", event.summary)

    def test_a_teacher_who_does_not_head_the_department_cannot_decide(self):
        self.submitted()
        for user in (self.musa, self.adewale, self.eze):
            with self.subTest(user=user.email):
                self.assertEqual(self.approve(user, self.sss1a, self.maths).status_code, 403)
                self.assertEqual(self.send_back(user, self.sss1a, self.maths).status_code, 403)
        self.assertEqual(ScoreSheet.objects.get().status, SheetStatus.SUBMITTED)

    def test_the_main_teacher_who_reads_the_sheet_is_told_they_are_not_the_head(self):
        self.submitted()
        response = self.approve(self.adewale, self.sss1a, self.maths)
        self.assertEqual(self.code(response), "NOT_HEAD_OF_DEPARTMENT")

    def test_a_hub_reader_without_the_approve_key_is_refused(self):
        self.submitted()
        self.assertEqual(self.approve(self.hub_reader, self.sss1a, self.maths).status_code, 403)
        self.assertEqual(self.approve(self.admin, self.sss1a, self.maths).status_code, 403)

    def test_only_a_submitted_sheet_waiting_for_moderation_can_be_decided(self):
        for status in (SheetStatus.IN_PROGRESS, SheetStatus.APPROVED, SheetStatus.RETURNED):
            with self.subTest(status=status):
                Score.objects.all().delete()
                ScoreSheet.objects.all().delete()
                self.submitted(status=status)
                response = self.approve(self.okeke, self.sss1a, self.maths)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.code(response), "NOT_SUBMITTED")
                self.assertEqual(self.send_back(self.okeke, self.sss1a, self.maths).status_code, 409)

    def test_a_sheet_nobody_started_cannot_be_decided(self):
        self.assertEqual(self.code(self.approve(self.okeke, self.sss1a, self.maths)), "NOT_SUBMITTED")

    def test_a_submitted_sheet_that_needed_no_moderation_cannot_be_decided(self):
        self.submitted(needs_moderation=False)
        self.assertEqual(self.code(self.approve(self.okeke, self.sss1a, self.maths)), "NOT_SUBMITTED")

    def test_returning_needs_a_note(self):
        self.submitted()
        for note in ("", "   ", None):
            with self.subTest(note=note):
                response = self.send_back(self.okeke, self.sss1a, self.maths, note=note)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.code(response), "NOTE_REQUIRED")
        response = self.call(
            "post", self.okeke, "gradebook-sheet-return", {},
            class_id=self.sss1a.pk, subject_id=self.maths.pk,
        )
        self.assertEqual(self.code(response), "NOTE_REQUIRED")
        self.assertEqual(ScoreSheet.objects.get().status, SheetStatus.SUBMITTED)

    def test_a_return_reopens_the_sheet_and_tells_the_main_teacher(self):
        self.submitted()
        response = self.send_back(self.okeke, self.sss1a, self.maths, "Recheck the exam column.")
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["status"], "RETURNED")
        self.assertEqual(body["returned"]["by"], "Ify Okeke")
        self.assertEqual(body["returned"]["note"], "Recheck the exam column.")
        sheet = ScoreSheet.objects.get()
        self.assertEqual((sheet.returned_by, sheet.returned_note), (self.okeke, "Recheck the exam column."))

        call = self.send.call_args.kwargs
        self.assertEqual(call["event_key"], "gradebook.sheet_returned")
        self.assertEqual(call["recipients"], [self.adewale])
        self.assertEqual(call["branch"], self.lekki)
        self.assertEqual(call["context"]["note"], "Recheck the exam column.")
        self.assertEqual(call["context"]["returned_by_name"], "Ify Okeke")
        self.assertEqual(call["context"]["class_name"], "SSS1 A")

    def test_the_teacher_corrects_and_resubmits_and_the_note_is_cleared(self):
        self.submitted()
        self.send_back(self.okeke, self.sss1a, self.maths)
        corrected = self.save(
            self.adewale, self.sss1a, self.maths,
            [{"student": self.femi.pk, "component": self.sexam.pk, "mark": 55}],
        )
        self.assertEqual(corrected.status_code, 200, corrected.data)
        self.assertEqual(self.data(corrected)["status"], "RETURNED")
        again = self.submit(self.adewale, self.sss1a, self.maths)
        self.assertEqual(self.data(again)["next"], "MODERATION")
        self.assertIsNone(self.data(again)["sheet"]["returned"])
        self.assertEqual(ScoreSheet.objects.get().status, SheetStatus.SUBMITTED)

    def test_a_return_is_audited(self):
        self.submitted()
        self.send_back(self.okeke, self.sss1a, self.maths)
        event = AuditEvent.objects.get(module_key="GRADEBOOK", entity_type="ScoreSheet")
        self.assertIn("returned Mathematics, SSS1 A, First Term", event.summary)

    def test_a_return_with_no_main_teacher_still_returns_and_tells_nobody(self):
        self.submitted(self.sss1b, rows={self.yemi: (10, 10, 10), self.dayo: (20, 20, 20)})
        response = self.send_back(self.okeke, self.sss1b, self.maths)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.send.call_count, 0)

    def test_another_schools_sheet_and_one_outside_the_heads_branch_are_a_404(self):
        self.submitted()
        self.assertEqual(self.approve(self.solo_teacher, self.sss1a, self.maths).status_code, 404)
        ikeja_head = self.make_teacher("ikeja-head@brightfield.test", "Ike", "Head", self.ikeja)
        self.set_head(self.sciences, self.head_staff)
        self.assertEqual(self.approve(ikeja_head, self.sss1a, self.maths).status_code, 404)

    def test_a_single_branch_school_with_no_moderation_has_nothing_to_decide(self):
        response = self.call(
            "post", self.solo_teacher, "gradebook-sheet-approve", term=self.solo_term,
            class_id=self.solo_class.pk, subject_id=self.solo_maths.pk,
        )
        self.assertEqual(response.status_code, 403)
