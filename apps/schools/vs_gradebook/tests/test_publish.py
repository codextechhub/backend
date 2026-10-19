"""Previewing and publishing a class: the snapshot, the release and the guardian's email."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from core.test_utils import TenantAPIClient
from django.urls import reverse

from schools.vs_academics.models import AcademicSession, AcademicTerm
from schools.vs_attendance.services.term_summary import StudentTermCounts
from schools.vs_gradebook.constants import EmailState, SheetStatus, StoredStage
from schools.vs_gradebook.models import (
    ClassTerm,
    GradingScale,
    ResultHold,
    ScoreSheet,
    StudentTermResult,
)
from schools.vs_gradebook.services import classbook
from vs_audit.models import AuditEvent
from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification

from .class_base import SENT_COMMENT, ClassFixture

SEND = "schools.vs_gradebook.services.publishing.send_notification"
SUMMARY = "schools.vs_gradebook.services.publishing.class_term_summary"


def result_of(student):
    return StudentTermResult.objects.get(student=student)


def counts_for(*students):
    return SimpleNamespace(days_not_taken=0, students={
        student.pk: StudentTermCounts(opened=60, present=55, absent=5, late=2, excused=0, not_taken=0)
        for student in students
    })


class PreviewTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()

    def test_it_counts_what_publishing_would_release_email_and_leave_without_an_address(self):
        body = self.data(self.preview(self.principal))
        self.assertEqual(body["class"]["name"], "JSS1 A")
        self.assertEqual(
            (body["released"], body["emailed"], body["no_email"]), (4, 3, 1),
        )
        self.assertEqual(body["held"], [])

    def test_a_held_student_is_named_and_left_out_of_the_counts(self):
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.tunde,
            reason="Fees.", placed_by=self.bursar,
        )
        body = self.data(self.preview(self.principal))
        self.assertEqual((body["released"], body["emailed"], body["no_email"]), (3, 3, 0))
        [held] = body["held"]
        self.assertEqual((held["student"]["name"], held["held_by"]), ("Tunde Bello", "Bimpe Bursar"))
        self.assertNotIn("reason", held)

    def test_a_child_with_no_primary_guardian_counts_as_no_email(self):
        from schools.vs_students.models import StudentGuardian

        StudentGuardian.all_objects.filter(student=self.chiamaka).update(is_primary=False)
        body = self.data(self.preview(self.principal))
        self.assertEqual((body["emailed"], body["no_email"]), (2, 2))

    def test_it_writes_nothing(self):
        self.preview(self.principal)
        self.assertFalse(StudentTermResult.objects.filter(snapshot__isnull=False).exists())
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)

    def test_a_class_not_awaiting_approval_has_no_preview(self):
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=None)
        response = self.preview(self.principal)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "NOT_AWAITING")

    def test_only_the_publish_key_may_preview(self):
        for user in (self.eze, self.admin, self.bursar, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.preview(user).status_code, 403)

    def test_another_branch_or_schools_class_is_a_404(self):
        self.assertEqual(self.preview(self.ikeja_principal).status_code, 404)
        response = self.call(
            "get", self.solo_principal, "gradebook-publish-preview", class_id=self.jss1a.pk,
            term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)


class PublishTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()

    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        summary = mock.patch(SUMMARY, return_value=counts_for(
            self.chiamaka, self.tunde, self.kelechi, self.amina,
        ))
        summary.start()
        self.addCleanup(summary.stop)

    def test_publishing_answers_per_class_with_the_counts(self):
        response = self.publish(self.principal)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response), [{
            "class": {"id": self.jss1a.pk, "name": "JSS1 A"}, "published": True,
            "released": 4, "emailed": 3, "no_email": 1, "held": 0,
        }])
        self.assertEqual(self.message(response), "1 of 1 classes published.")

    def test_the_class_is_published_with_who_and_when(self):
        self.publish(self.principal)
        class_term = ClassTerm.objects.get(school_class=self.jss1a)
        self.assertEqual(class_term.stage, StoredStage.PUBLISHED)
        self.assertEqual(class_term.published_by, self.principal)
        self.assertIsNotNone(class_term.published_at)

    def test_every_student_on_the_roll_gets_a_snapshot_at_version_one(self):
        self.publish(self.principal)
        results = StudentTermResult.objects.all()
        self.assertEqual(results.count(), 4)
        for result in results:
            self.assertEqual(result.version, 1)
            self.assertIsNotNone(result.snapshot)
            self.assertEqual(result.snapshot["version"], 1)

    def test_a_snapshot_holds_everything_the_report_card_prints(self):
        self.publish(self.principal)
        snapshot = dict(result_of(self.tunde).snapshot)
        published_at = snapshot.pop("published_at")
        self.assertIsNotNone(dt.datetime.fromisoformat(published_at).tzinfo)
        components = [
            {"id": c.pk, "name": c.name, "short_name": c.short_name,
             "max_score": c.max_score, "is_exam": c.is_exam}
            for c in (self.ca1, self.ca2, self.exam)
        ]
        cells = [
            {"component": self.ca1.pk, "mark": 15, "marker": None},
            {"component": self.ca2.pk, "mark": 15, "marker": None},
            {"component": self.exam.pk, "mark": 45, "marker": None},
        ]
        self.assertEqual(snapshot, {
            "version": 1,
            "student": {"id": self.tunde.pk, "name": "Tunde Bello", "admission_number": "BFS/002"},
            "class": {"id": self.jss1a.pk, "name": "JSS1 A"},
            "class_size": 4,
            "term": {
                "id": self.first_term.pk, "name": "First Term",
                "start_date": "2025-09-08", "end_date": "2025-12-12",
            },
            "session": {"id": self.year.pk, "name": "2025/2026"},
            "branch": {"id": self.lekki.pk, "name": "Lekki Branch"},
            "scale_id": self.junior_scale.pk,
            "structure_id": self.junior_structure.pk,
            "components": components,
            "subjects": [
                {
                    "subject": {"id": self.french.pk, "name": "French", "teacher": None},
                    "cells": cells,
                    "total": 75, "obtainable": 100, "percentage": "75.00", "grade": "A",
                    "remark": "Excellent", "is_fail": False,
                    "class_average": "62.5", "highest": 75, "lowest": 50,
                },
                {
                    "subject": {"id": self.maths.pk, "name": "Mathematics", "teacher": "Chidi Eze"},
                    "cells": cells,
                    "total": 75, "obtainable": 100, "percentage": "75.00", "grade": "A",
                    "remark": "Excellent", "is_fail": False,
                    "class_average": "63.5", "highest": 79, "lowest": 40,
                },
            ],
            "total_obtained": 150, "total_obtainable": 200,
            "overall_average": "75.00",
            "attendance": {"opened": 60, "present": 55, "absent": 5},
            "teacher_comment": SENT_COMMENT,
            "class_teacher": "Chidi Eze",
            "principal_comment": "An excellent result. Keep it up.",
            "principal": "Pauline Principal",
            "grading_key": [
                {"min_score": 70, "max_score": 100, "grade": "A", "remark": "Excellent", "is_fail": False},
                {"min_score": 60, "max_score": 69, "grade": "B", "remark": "Very good", "is_fail": False},
                {"min_score": 50, "max_score": 59, "grade": "C", "remark": "Good", "is_fail": False},
                {"min_score": 40, "max_score": 49, "grade": "D", "remark": "Fair", "is_fail": False},
                {"min_score": 0, "max_score": 39, "grade": "F", "remark": "Fail", "is_fail": True},
            ],
            "next_term_begins": "2026-01-12",
            "options": {"show_class_stats": True, "rating_key": True},
            "class_average": "59.88",
            "position": 1, "roll_size": 4,
            "ratings": {},
            "traits": [
                "Punctuality", "Neatness", "Politeness", "Honesty", "Relationship with others",
                "Attentiveness", "Handwriting", "Sports", "Drawing and painting",
                "Handling of tools",
            ],
        })

    def test_a_student_who_does_not_take_an_optional_subject_has_no_row_for_it(self):
        self.publish(self.principal)
        snapshot = result_of(self.kelechi).snapshot
        self.assertEqual([s["subject"]["name"] for s in snapshot["subjects"]], ["Mathematics"])
        self.assertEqual((snapshot["total_obtained"], snapshot["total_obtainable"]), (40, 100))
        self.assertEqual(snapshot["overall_average"], "40.00")

    def test_abs_and_excused_are_kept_as_markers_and_the_excused_component_leaves_obtainable(self):
        ScoreSheet.objects.filter(subject=self.maths).update(status=SheetStatus.SUBMITTED)
        from schools.vs_gradebook.models import Score

        Score.objects.filter(student=self.tunde, component=self.ca1).update(mark=None, marker="EXC")
        Score.objects.filter(student=self.tunde, component=self.ca2).update(mark=None, marker="ABS")
        self.publish(self.principal)
        maths = next(s for s in result_of(self.tunde).snapshot["subjects"] if s["subject"]["name"] == "Mathematics")
        self.assertEqual(
            [(c["mark"], c["marker"]) for c in maths["cells"]],
            [(None, "EXC"), (None, "ABS"), (45, None)],
        )
        self.assertEqual((maths["total"], maths["obtainable"]), (45, 80))
        self.assertEqual(maths["percentage"], "56.25")

    def test_the_principals_own_comment_for_the_band_wins_over_the_template(self):
        self.write_principal_comments(self.principal, [{"band_min": 55, "text": "Keep climbing."}])
        self.publish(self.principal)
        self.assertEqual(result_of(self.chiamaka).snapshot["principal_comment"], "Keep climbing.")
        self.assertEqual(result_of(self.amina).snapshot["principal_comment"], "Keep climbing.")
        self.assertEqual(
            result_of(self.kelechi).snapshot["principal_comment"],
            "A fair result. More consistent work is needed.",
        )
        self.assertEqual(
            result_of(self.tunde).snapshot["principal_comment"], "An excellent result. Keep it up.",
        )

    def test_the_snapshot_keeps_what_was_published_when_the_scale_changes_later(self):
        self.publish(self.principal)
        GradingScale.objects.filter(pk=self.junior_scale.pk).update(name="Renamed")
        from schools.vs_gradebook.models import GradeBand

        GradeBand.objects.filter(scale=self.junior_scale, grade="A").update(grade="Z")
        snapshot = result_of(self.tunde).snapshot
        self.assertEqual(snapshot["subjects"][0]["grade"], "A")
        self.assertEqual(snapshot["scale_id"], self.junior_scale.pk)

    def test_the_ratings_the_class_teacher_gave_are_in_the_snapshot(self):
        StudentTermResult.objects.filter(student=self.tunde).update(ratings={"Sports": 4})
        self.publish(self.principal)
        self.assertEqual(result_of(self.tunde).snapshot["ratings"], {"Sports": 4})

    def test_a_student_with_no_graded_subject_has_no_average_position_or_principal_comment(self):
        from schools.vs_gradebook.models import Score

        Score.objects.filter(student=self.kelechi).update(mark=None, marker="EXC")
        self.publish(self.principal)
        snapshot = result_of(self.kelechi).snapshot
        self.assertIsNone(snapshot["overall_average"])
        self.assertIsNone(snapshot["position"])
        self.assertIsNone(snapshot["principal_comment"])

    def test_next_term_begins_is_the_next_term_of_the_year(self):
        self.publish(self.principal)
        self.assertEqual(result_of(self.tunde).snapshot["next_term_begins"], "2026-01-12")

    def test_the_audit_trail_records_the_publication(self):
        self.publish(self.principal)
        event = AuditEvent.objects.get(module_key="GRADEBOOK", summary__contains="published")
        self.assertIn("published JSS1 A, First Term: 4 released, 0 held", event.summary)

    def test_publishing_again_is_refused_and_changes_nothing(self):
        self.publish(self.principal)
        stamp = ClassTerm.objects.get(school_class=self.jss1a).published_at
        response = self.publish(self.principal)
        [row] = self.data(response)
        self.assertEqual((row["published"], row["code"]), (False, "NOT_AWAITING"))
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).published_at, stamp)

    def test_a_published_class_takes_no_more_scores_comments_or_ratings(self):
        self.publish(self.principal)
        self.assertEqual(
            self.compile_put(self.eze, self.tunde, {"teacher_comment": "Late."}).status_code, 409,
        )
        saved = self.save(self.eze, self.jss1a, self.maths, [self.cell(self.tunde, self.ca1, 1)])
        self.assertEqual(self.code(saved), "CLASS_LOCKED")


class ReleaseTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()

    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        summary = mock.patch(SUMMARY, return_value=counts_for())
        summary.start()
        self.addCleanup(summary.stop)

    def hold(self, student):
        return ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=student, reason="Fees.",
            placed_by=self.bursar,
        )

    def test_a_held_student_is_not_released_and_is_not_emailed(self):
        self.hold(self.chiamaka)
        response = self.publish(self.principal)
        [row] = self.data(response)
        self.assertEqual((row["released"], row["emailed"], row["no_email"], row["held"]), (3, 2, 1, 1))
        held = result_of(self.chiamaka)
        self.assertIsNone(held.released_at)
        self.assertEqual(held.email_state, EmailState.HELD)
        self.assertIsNone(held.emailed_at)
        self.assertIsNotNone(held.snapshot)
        recipients = [call.kwargs["context"]["student_first_name"] for call in self.send.call_args_list]
        self.assertNotIn("Chiamaka", recipients)

    def test_a_student_without_a_hold_is_released_with_the_time(self):
        self.publish(self.principal)
        released = result_of(self.tunde)
        self.assertIsNotNone(released.released_at)

    def test_a_guardian_with_an_address_is_emailed_and_the_child_recorded_sent(self):
        self.publish(self.principal)
        chiamaka = result_of(self.chiamaka)
        self.assertEqual(chiamaka.email_state, EmailState.SENT)
        self.assertIsNotNone(chiamaka.emailed_at)
        call = next(
            c for c in self.send.call_args_list
            if c.kwargs["context"]["student_first_name"] == "Chiamaka"
        )
        self.assertEqual(call.kwargs["event_key"], "gradebook.report_card_ready")
        self.assertEqual(call.kwargs["recipients"], [])
        [recipient] = call.kwargs["unregistered_recipients"]
        self.assertEqual(recipient.email, "ngozi.nwosu@example.ng")
        self.assertEqual(call.kwargs["tenant"], self.tenant)
        self.assertEqual(call.kwargs["branch"], self.lekki)

    def test_the_guardians_notice_names_the_child_for_the_parent_app(self):
        self.publish(self.principal)
        call = next(
            c for c in self.send.call_args_list
            if c.kwargs["event_key"] == "gradebook.report_card_ready"
            and c.kwargs["context"]["student_first_name"] == "Chiamaka"
        )
        self.assertEqual(call.kwargs["metadata"], {"student_id": self.chiamaka.pk})

    def test_a_guardian_with_no_email_is_recorded_as_such_and_nobody_is_written_to(self):
        self.publish(self.principal)
        tunde = result_of(self.tunde)
        self.assertEqual(tunde.email_state, EmailState.NO_EMAIL)
        self.assertIsNone(tunde.emailed_at)
        names = [c.kwargs["context"]["student_first_name"] for c in self.send.call_args_list]
        self.assertNotIn("Tunde", names)

    def test_a_child_with_no_primary_guardian_is_recorded_as_no_email(self):
        from schools.vs_students.models import StudentGuardian

        StudentGuardian.all_objects.filter(student=self.tunde).delete()
        self.publish(self.principal)
        self.assertEqual(result_of(self.tunde).email_state, EmailState.NO_EMAIL)

    def test_siblings_with_one_guardian_get_one_email_each(self):
        self.publish(self.principal)
        calls = [
            c for c in self.send.call_args_list
            if c.kwargs["recipients"] == [self.eze_mother_user]
        ]
        self.assertEqual(
            sorted(c.kwargs["context"]["student_first_name"] for c in calls), ["Amina", "Kelechi"],
        )
        self.assertEqual(self.send.call_count, 3)
        for call in calls:
            self.assertIsNone(call.kwargs["unregistered_recipients"])

    def test_the_context_has_exactly_what_the_template_reads_and_no_result(self):
        self.publish(self.principal)
        call = next(
            c for c in self.send.call_args_list
            if c.kwargs["context"]["student_first_name"] == "Chiamaka"
        )
        self.assertEqual(call.kwargs["context"], {
            "student_first_name": "Chiamaka", "term_name": "First Term",
            "session_name": "2025/2026", "tenant_name": "Brightfield Schools",
            "branch_name": "Lekki Branch", "tenant_phone": self.school.phone or "",
        })

    def test_nothing_is_sent_until_the_transaction_commits(self):
        ids = [self.jss1a.pk]
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.call(
                "post", self.principal, "gradebook-publish", {"classes": ids}, term=self.first_term,
            )
        self.assertEqual(self.send.call_count, 0)
        self.assertEqual(len(callbacks), 1)
        self.assertIsNone(result_of(self.chiamaka).email_state)
        for callback in callbacks:
            callback()
        self.assertEqual(result_of(self.chiamaka).email_state, EmailState.SENT)

    def test_a_failed_send_leaves_the_child_released_and_without_a_state(self):
        self.send.side_effect = RuntimeError("smtp down")
        response = self.publish(self.principal)
        self.assertEqual(response.status_code, 200)
        chiamaka = result_of(self.chiamaka)
        self.assertIsNotNone(chiamaka.released_at)
        self.assertIsNone(chiamaka.email_state)

    def test_a_notification_layer_that_carried_nothing_is_not_recorded_as_sent(self):
        self.send.return_value = []
        self.publish(self.principal)
        self.assertIsNone(result_of(self.chiamaka).email_state)

    def test_one_email_goes_for_each_child_with_an_address_and_no_more(self):
        self.publish(self.principal)
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(StudentTermResult.objects.count(), 4)


class RenderedEmailTests(ClassFixture):
    """The real template, rendered for the real recipients."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_notifications.services.seed import (
            seed_notification_templates,
            seed_platform_settings,
        )

        seed_notification_templates()
        seed_platform_settings()
        cls.drive_to_awaiting()

    def notices(self):
        return Notification.all_objects.filter(event_type__key="gradebook.report_card_ready")

    def test_the_email_says_the_card_is_ready_and_carries_nothing_from_the_result(self):
        self.school.phone = "0801 234 5678"
        self.school.save(update_fields=["phone"])
        self.publish(self.principal)
        email = self.notices().get(
            channel=ChannelChoices.EMAIL, unregistered_email="ngozi.nwosu@example.ng",
        )
        self.assertEqual(email.subject, "Chiamaka's report card is ready")
        self.assertIn(
            "Chiamaka's report card for First Term, 2025/2026 is ready to collect from the school.",
            email.body,
        )
        self.assertIn("You can reach the school on 0801 234 5678.", email.body)
        self.assertIn("Brightfield Schools, Lekki Branch", email.body)
        text = (email.subject + email.body).lower()
        for leak in ("grade", "score", "position", "average", "comment", "http", "attach"):
            self.assertNotIn(leak, text)
        for figure in ("64.5", "79", "50", "a1"):
            self.assertNotIn(figure, text.replace("2025/2026", "").replace("0801 234 5678", ""))

    def test_a_guardian_with_an_account_gets_it_in_the_app_as_well_once_for_each_child(self):
        self.publish(self.principal)
        mine = self.notices().filter(recipient=self.eze_mother_user)
        self.assertEqual(
            {(n.channel, n.subject) for n in mine},
            {
                (ChannelChoices.EMAIL, "Kelechi's report card is ready"),
                (ChannelChoices.IN_APP, "Kelechi's report card is ready"),
                (ChannelChoices.EMAIL, "Amina's report card is ready"),
                (ChannelChoices.IN_APP, "Amina's report card is ready"),
            },
        )

    def test_a_held_child_is_sent_nothing(self):
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.chiamaka, reason="Fees.",
            placed_by=self.bursar,
        )
        self.publish(self.principal)
        self.assertFalse(self.notices().filter(subject__startswith="Chiamaka").exists())


class PublishSeveralTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()
        cls.make_sheet(
            cls.sss1a, cls.maths, status=SheetStatus.SUBMITTED,
            rows={cls.zainab: (18, 15, 46), cls.femi: (5, 5, 10)},
        )

    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        summary = mock.patch(SUMMARY, return_value=counts_for())
        summary.start()
        self.addCleanup(summary.stop)

    def test_a_class_not_awaiting_is_refused_on_its_own_and_the_others_go_ahead(self):
        response = self.publish(self.principal, [self.sss1a, self.jss1a])
        self.assertEqual(response.status_code, 200, response.data)
        sss, jss = self.data(response)
        self.assertEqual((sss["class"]["name"], sss["published"], sss["code"]), ("SSS1 A", False, "NOT_AWAITING"))
        self.assertEqual(sss["message"], "Only a class that is awaiting approval can be published or returned.")
        self.assertEqual((sss["released"], sss["emailed"], sss["no_email"], sss["held"]), (0, 0, 0, 0))
        self.assertEqual((jss["class"]["name"], jss["published"]), ("JSS1 A", True))
        self.assertNotIn("code", jss)
        self.assertEqual(self.message(response), "1 of 2 classes published.")
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.PUBLISHED)
        self.assertIsNone(ClassTerm.objects.get(school_class=self.sss1a).stage)

    def test_two_awaiting_classes_publish_each_in_its_own_transaction(self):
        ClassTerm.objects.filter(school_class=self.sss1a).update(stage=StoredStage.AWAITING)
        StudentTermResult.objects.create(
            tenant=self.tenant, class_term=ClassTerm.objects.get(school_class=self.sss1a),
            student=self.zainab, teacher_comment="Good.",
        )
        StudentTermResult.objects.create(
            tenant=self.tenant, class_term=ClassTerm.objects.get(school_class=self.sss1a),
            student=self.femi, teacher_comment="Fine.",
        )
        response = self.publish(self.principal, [self.jss1a, self.sss1a])
        rows = self.data(response)
        self.assertEqual([r["published"] for r in rows], [True, True])
        self.assertEqual(self.message(response), "2 of 2 classes published.")

    def test_a_class_whose_sheet_is_no_longer_in_is_refused_and_unchanged(self):
        ScoreSheet.objects.filter(subject=self.french).update(status=SheetStatus.RETURNED)
        [row] = self.data(self.publish(self.principal))
        self.assertEqual((row["published"], row["code"]), (False, "SUBJECTS_MISSING"))
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)
        self.assertFalse(StudentTermResult.objects.filter(snapshot__isnull=False).exists())

    def test_a_class_whose_scale_has_gone_is_refused_and_nothing_is_half_published(self):
        GradingScale.objects.filter(pk=self.junior_scale.pk).update(status="RETIRED")
        [row] = self.data(self.publish(self.principal))
        self.assertEqual((row["published"], row["code"]), (False, "NO_SCALE"))
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)
        self.assertFalse(StudentTermResult.objects.filter(snapshot__isnull=False).exists())
        self.assertEqual(self.send.call_count, 0)

    def test_a_class_named_twice_is_published_once(self):
        response = self.publish(self.principal, [self.jss1a, self.jss1a])
        self.assertEqual(len(self.data(response)), 1)

    def test_only_the_publish_key_may_publish_and_nothing_is_changed_without_it(self):
        for user in (self.eze, self.admin, self.bursar, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.publish(user).status_code, 403)
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)

    def test_a_class_outside_the_callers_branch_fails_the_whole_request(self):
        response = self.publish(self.ikeja_principal, [self.jss1a])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)

    def test_one_invisible_class_among_visible_ones_publishes_none(self):
        response = self.publish(self.lekki_principal, [self.jss1a, self.jss1c])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)

    def test_another_schools_class_is_a_404(self):
        response = self.publish(self.principal, [self.solo_class])
        self.assertEqual(response.status_code, 404)
        response = self.publish(self.solo_principal, [self.jss1a], term=self.solo_term)
        self.assertEqual(response.status_code, 404)

    def test_the_body_must_name_classes(self):
        for body in ({}, {"classes": []}, {"classes": "1"}, {"classes": [0]}, {"classes": ["x"]}):
            with self.subTest(body=body):
                response = self.call(
                    "post", self.principal, "gradebook-publish", body, term=self.first_term,
                )
                self.assertEqual(response.status_code, 400)

    def test_too_many_classes_are_refused(self):
        response = self.call(
            "post", self.principal, "gradebook-publish", {"classes": list(range(1, 52))},
            term=self.first_term,
        )
        self.assertEqual(response.status_code, 400)


class SnapshotRulesTests(ClassFixture):
    """Ties, a school that does not rank and a school with one branch."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting(maths=cls.tied_maths(), french=cls.tied_french())
        cls.make_sheet(
            cls.solo_class, cls.solo_maths, status=SheetStatus.SUBMITTED,
            rows={cls.solo_pupil: (18, 15, 46), cls.solo_pupil_two: (10, 10, 20)},
        )
        client = TenantAPIClient(cls.solo_teacher)
        for pupil in (cls.solo_pupil, cls.solo_pupil_two):
            client.put(
                reverse("gradebook-compile", kwargs={"class_id": cls.solo_class.pk, "student_id": pupil.pk})
                + f"?term={cls.solo_term.pk}", {"teacher_comment": "Good."}, format="json",
            )
        client.post(
            reverse("gradebook-class-submit", kwargs={"class_id": cls.solo_class.pk})
            + f"?term={cls.solo_term.pk}", {}, format="json",
        )

    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        summary = mock.patch(SUMMARY, return_value=counts_for())
        summary.start()
        self.addCleanup(summary.stop)

    def test_tied_averages_share_a_position_and_the_next_is_skipped_on_the_cards(self):
        self.publish(self.principal)
        positions = {
            result.student.full_name: result.snapshot["position"]
            for result in StudentTermResult.objects.filter(tenant=self.tenant).select_related("student")
        }
        self.assertEqual(
            positions,
            {"Tunde Bello": 1, "Chiamaka Nwosu": 2, "Amina Yusuf": 2, "Kelechi Eze": 4},
        )

    def test_a_school_that_does_not_rank_or_rate_has_neither_in_the_snapshot(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.call(
                "post", self.solo_principal, "gradebook-publish", {"classes": [self.solo_class.pk]},
                term=self.solo_term,
            )
        self.assertEqual(response.status_code, 200, response.data)
        for result in StudentTermResult.objects.filter(tenant=self.solo):
            for key in ("position", "roll_size", "ratings", "traits"):
                self.assertNotIn(key, result.snapshot)
            self.assertIn("class_size", result.snapshot)
            self.assertIn("overall_average", result.snapshot)
            self.assertEqual(result.snapshot["branch"]["name"], "Main Branch")

    def test_at_a_school_with_one_branch_the_email_names_no_branch(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.call(
                "post", self.solo_principal, "gradebook-publish", {"classes": [self.solo_class.pk]},
                term=self.solo_term,
            )
        contexts = [c.kwargs["context"] for c in self.send.call_args_list]
        self.assertEqual(len(contexts), 2)
        for context in contexts:
            self.assertEqual(context["branch_name"], "")
            self.assertEqual(context["tenant_name"], "Sunrise Academy")

    def test_a_published_class_is_isolated_from_another_schools_principal(self):
        response = self.call(
            "post", self.solo_principal, "gradebook-publish", {"classes": [self.jss1a.pk]},
            term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)


class NextTermBeginsTests(ClassFixture):
    def test_it_is_the_next_term_of_the_same_year(self):
        self.assertEqual(
            classbook.next_term_start(self.tenant, self.first_term), dt.date(2026, 1, 12),
        )

    def test_it_is_null_when_the_school_has_nothing_after_the_last_term(self):
        self.assertIsNone(classbook.next_term_start(self.tenant, self.second_term))

    def test_after_the_last_term_it_is_the_first_term_of_the_next_year(self):
        later = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2026/2027", start_date=dt.date(2026, 9, 7),
            end_date=dt.date(2027, 7, 16), status="DRAFT",
        )
        AcademicTerm.all_objects.create(
            tenant=self.tenant, session=later, name="Second Term", order_index=2,
            start_date=dt.date(2027, 1, 11), end_date=dt.date(2027, 4, 2),
        )
        AcademicTerm.all_objects.create(
            tenant=self.tenant, session=later, name="First Term", order_index=1,
            start_date=dt.date(2026, 9, 7), end_date=dt.date(2026, 12, 11),
        )
        self.assertEqual(
            classbook.next_term_start(self.tenant, self.second_term), dt.date(2026, 9, 7),
        )

    def test_another_schools_terms_are_not_considered(self):
        self.assertIsNone(classbook.next_term_start(self.solo, self.solo_term))
