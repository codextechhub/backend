"""The principal's queue, returning a class, and the principal's comments."""
from __future__ import annotations

from unittest import mock

from schools.vs_gradebook.constants import SheetStatus, StoredStage
from schools.vs_gradebook.models import ClassTerm, PrincipalComment, ResultHold, ScoreSheet
from vs_audit.models import AuditEvent

from .class_base import ClassFixture

SEND_CLASS = "schools.vs_gradebook.services.approvals.send_notification"
SEND_SHEET = "schools.vs_gradebook.services.moderation.send_notification"


class ApprovalsQueueTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()
        # SSS1 A is still entering; it has no business on the queue.
        cls.make_sheet(cls.sss1a, cls.maths, status=SheetStatus.IN_PROGRESS, rows={cls.zainab: (1, 1, 1)})

    def test_it_lists_a_class_awaiting_approval_and_not_one_still_entering(self):
        body = self.data(self.approvals(self.principal))
        self.assertTrue(body["branch_dimension"])
        [row] = body["classes"]
        self.assertEqual(row["class"], {"id": self.jss1a.pk, "name": "JSS1 A"})
        self.assertEqual(row["branch"], {"id": self.lekki.pk, "name": "Lekki Branch"})
        self.assertEqual(row["class_teacher"]["name"], "Chidi Eze")
        self.assertEqual((row["subjects_in"], row["subjects_due"]), (2, 2))
        self.assertEqual(row["stage"], "AWAITING")
        self.assertTrue(row["can_decide"])
        self.assertEqual(row["held"], [])
        self.assertIsNotNone(row["submitted_at"])

    def test_a_published_class_is_listed_and_can_no_longer_be_decided(self):
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=StoredStage.PUBLISHED)
        [row] = self.data(self.approvals(self.principal))["classes"]
        self.assertEqual((row["stage"], row["can_decide"]), ("PUBLISHED", False))

    def test_the_held_students_are_named_with_who_held_them_and_never_why(self):
        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.tunde,
            reason="Fees outstanding since September.", placed_by=self.bursar,
        )
        [row] = self.data(self.approvals(self.principal))["classes"]
        [held] = row["held"]
        self.assertEqual(held["student"]["name"], "Tunde Bello")
        self.assertEqual(held["held_by"], "Bimpe Bursar")
        self.assertIsNotNone(held["placed_at"])
        self.assertNotIn("reason", held)
        self.assertNotIn("Fees outstanding", str(row))

    def test_a_lifted_hold_is_not_listed(self):
        import datetime as dt

        ResultHold.objects.create(
            tenant=self.tenant, term=self.first_term, student=self.tunde, reason="x",
            placed_by=self.bursar, lifted_at=dt.datetime(2025, 10, 10, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(self.data(self.approvals(self.principal))["classes"][0]["held"], [])

    def test_an_empty_queue_is_an_empty_list_not_an_object(self):
        body = self.data(self.approvals(self.ikeja_principal))
        self.assertEqual(body["classes"], [])

    def test_a_branch_bound_principal_sees_only_their_branch_and_a_branch_filter_narrows(self):
        self.assertEqual(len(self.data(self.approvals(self.lekki_principal))["classes"]), 1)
        self.assertEqual(
            self.data(self.approvals(self.principal, branch=self.ikeja.pk))["classes"], [],
        )
        self.assertEqual(
            len(self.data(self.approvals(self.principal, branch=self.lekki.pk))["classes"]), 1,
        )

    def test_a_branch_the_caller_cannot_see_or_another_schools_branch_is_a_404(self):
        self.assertEqual(self.approvals(self.ikeja_principal, branch=self.lekki.pk).status_code, 404)
        self.assertEqual(self.approvals(self.principal, branch=self.solo_branch.pk).status_code, 404)

    def test_only_a_holder_of_the_publish_key_may_read_it(self):
        for user in (self.eze, self.admin, self.bursar, self.hub_reader, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.approvals(user).status_code, 403)

    def test_a_term_of_another_school_is_a_404(self):
        self.assertEqual(self.approvals(self.principal, term=self.solo_term.pk).status_code, 404)


class SingleBranchApprovalsTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.make_sheet(
            cls.solo_class, cls.solo_maths, status=SheetStatus.SUBMITTED,
            rows={cls.solo_pupil: (18, 15, 46), cls.solo_pupil_two: (10, 10, 20)},
        )
        ClassTerm.objects.filter(school_class=cls.solo_class).update(stage=StoredStage.AWAITING)

    def test_the_branch_recedes_at_a_school_with_one(self):
        from core.test_utils import TenantAPIClient
        from django.urls import reverse

        response = TenantAPIClient(self.solo_principal).get(
            f"{reverse('gradebook-approvals')}?term={self.solo_term.pk}",
        )
        body = self.data(response)
        self.assertFalse(body["branch_dimension"])
        [row] = body["classes"]
        self.assertIsNone(row["branch"])
        self.assertEqual(row["class"]["name"], "Basic 3")


class ReturnTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()

    def setUp(self):
        super().setUp()
        for target, name in ((SEND_CLASS, "send_class"), (SEND_SHEET, "send_sheet")):
            patcher = mock.patch(target, return_value=["notification-id"])
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_the_class_goes_back_to_its_class_teacher_with_the_note(self):
        response = self.return_class(self.principal, "Please recheck the comments.")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response), {"returned": "CLASS_TEACHER", "stage": "COMPILING"})
        class_term = ClassTerm.objects.get(school_class=self.jss1a)
        self.assertIsNone(class_term.stage)
        self.assertEqual(class_term.returned_by, self.principal)
        self.assertEqual(class_term.returned_note, "Please recheck the comments.")
        self.assertIsNotNone(class_term.returned_at)

    def test_the_class_teacher_is_told_in_the_app(self):
        self.return_class(self.principal, "Please recheck the comments.")
        call = self.send_class.call_args.kwargs
        self.assertEqual(call["event_key"], "gradebook.class_returned")
        self.assertEqual(call["recipients"], [self.eze])
        self.assertEqual(call["tenant"], self.tenant)
        self.assertEqual(call["branch"], self.lekki)
        self.assertEqual(call["context"]["class_name"], "JSS1 A")
        self.assertEqual(call["context"]["term_name"], "First Term")
        self.assertEqual(call["context"]["note"], "Please recheck the comments.")
        self.assertEqual(call["context"]["returned_by_name"], "Pauline Principal")
        self.assertEqual(self.send_sheet.call_count, 0)

    def test_the_broadsheet_then_shows_the_return_and_the_class_can_be_submitted_again(self):
        self.return_class(self.principal, "Please recheck the comments.")
        body = self.data(self.broadsheet(self.eze))
        self.assertEqual(body["stage"], "COMPILING")
        self.assertEqual(body["returned"]["note"], "Please recheck the comments.")
        self.assertEqual(body["returned"]["by"], "Pauline Principal")
        self.assertTrue(body["can_submit"])
        self.assertEqual(self.submit_class(self.eze).status_code, 200)
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).returned_note, "")

    def test_a_note_is_required(self):
        for note in ("", "   "):
            with self.subTest(note=note):
                response = self.return_class(self.principal, note)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.code(response), "NOTE_REQUIRED")
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)

    def test_only_a_class_awaiting_approval_can_be_returned(self):
        for stage in (None, StoredStage.PUBLISHED):
            ClassTerm.objects.filter(school_class=self.jss1a).update(stage=stage)
            with self.subTest(stage=stage):
                response = self.return_class(self.principal)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.code(response), "NOT_AWAITING")
        self.assertEqual(self.send_class.call_count, 0)

    def test_one_subject_goes_back_to_its_main_teacher_and_the_class_is_cleared(self):
        response = self.return_class(self.principal, "Mathematics looks wrong.", subject=self.maths.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response), {"returned": "SUBJECT", "stage": "ENTERING"})
        sheet = ScoreSheet.objects.get(subject=self.maths)
        self.assertEqual(sheet.status, SheetStatus.RETURNED)
        self.assertEqual((sheet.returned_by, sheet.returned_note), (self.principal, "Mathematics looks wrong."))
        self.assertEqual(ScoreSheet.objects.get(subject=self.french).status, SheetStatus.SUBMITTED)
        class_term = ClassTerm.objects.get(school_class=self.jss1a)
        self.assertIsNone(class_term.stage)
        self.assertEqual(class_term.returned_note, "")

    def test_the_main_teacher_of_that_sheet_is_told_and_the_class_teacher_is_not(self):
        self.return_class(self.principal, "Mathematics looks wrong.", subject=self.maths.pk)
        call = self.send_sheet.call_args.kwargs
        self.assertEqual(call["event_key"], "gradebook.sheet_returned")
        self.assertEqual(call["recipients"], [self.eze])
        self.assertEqual(call["context"]["subject_name"], "Mathematics")
        self.assertEqual(call["context"]["note"], "Mathematics looks wrong.")
        self.assertEqual(self.send_class.call_count, 0)

    def test_the_returned_sheet_reopens_for_its_teacher_and_the_class_no_longer_counts_it_in(self):
        self.return_class(self.principal, "Mathematics looks wrong.", subject=self.maths.pk)
        body = self.data(self.sheet(self.eze, self.jss1a, self.maths))
        self.assertEqual(body["status"], "RETURNED")
        self.assertEqual(body["returned"]["note"], "Mathematics looks wrong.")
        self.assertTrue(body["can_enter"])
        broadsheet = self.data(self.broadsheet(self.eze))
        self.assertEqual(broadsheet["stage"], "ENTERING")
        self.assertEqual([g["subject"]["name"] for g in broadsheet["gaps"]], ["Mathematics"])

    def test_a_subject_the_class_does_not_have_is_a_404_and_nothing_changes(self):
        from schools.vs_academics.models import Subject

        other = Subject.all_objects.create(tenant=self.tenant, name="Latin", code="LAT", is_core=True)
        response = self.return_class(self.principal, "No.", subject=other.pk)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)

    def test_a_subject_return_still_needs_a_note(self):
        response = self.return_class(self.principal, "", subject=self.maths.pk)
        self.assertEqual(self.code(response), "NOTE_REQUIRED")
        self.assertEqual(ScoreSheet.objects.get(subject=self.maths).status, SheetStatus.SUBMITTED)

    def test_only_the_publish_key_returns_a_class(self):
        for user in (self.eze, self.admin, self.bursar, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.return_class(user).status_code, 403)

    def test_a_class_in_another_branch_or_school_is_a_404(self):
        self.assertEqual(self.return_class(self.ikeja_principal).status_code, 404)
        response = self.call(
            "post", self.solo_principal, "gradebook-class-return", {"note": "x"},
            class_id=self.jss1a.pk, term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ClassTerm.objects.get(school_class=self.jss1a).stage, StoredStage.AWAITING)

    def test_a_class_with_no_class_teacher_is_returned_without_a_notice(self):
        from schools.vs_academics.models import SchoolClass

        SchoolClass.all_objects.filter(pk=self.jss1a.pk).update(class_teacher=None)
        self.assertEqual(self.return_class(self.principal).status_code, 200)
        self.assertEqual(self.send_class.call_count, 0)

    def test_a_class_teacher_who_cannot_sign_in_is_not_written_to(self):
        type(self.eze).objects.filter(pk=self.eze.pk).update(status="LOCKED")
        self.assertEqual(self.return_class(self.principal).status_code, 200)
        self.assertEqual(self.send_class.call_count, 0)

    def test_the_return_is_audited_for_the_class_and_for_the_subject(self):
        self.return_class(self.principal, "Please recheck.")
        event = AuditEvent.objects.get(
            module_key="GRADEBOOK", summary__contains="to its class teacher",
        )
        self.assertIn("returned JSS1 A, First Term to its class teacher", event.summary)
        self.submit_class(self.eze)
        self.return_class(self.principal, "Maths again.", subject=self.maths.pk)
        newest = AuditEvent.objects.get(
            module_key="GRADEBOOK", summary__contains="to its teacher",
        )
        self.assertIn("returned Mathematics, JSS1 A, First Term to its teacher", newest.summary)


class PrincipalCommentTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()

    def test_it_lists_every_template_band_with_the_class_override_if_any(self):
        body = self.data(self.principal_comments(self.principal))
        self.assertEqual(body["class"]["name"], "JSS1 A")
        self.assertFalse(body["locked"])
        self.assertEqual([b["band_min"] for b in body["bands"]], [70, 55, 40, 0])
        self.assertEqual(body["bands"][0]["template"], "An excellent result. Keep it up.")
        self.assertTrue(all(b["override"] is None for b in body["bands"]))

    def test_a_save_writes_the_comment_for_the_bands_named_and_the_rest_keep_the_template(self):
        response = self.write_principal_comments(self.principal, [
            {"band_min": 70, "text": "Outstanding, Tunde."},
            {"band_min": 40, "text": "Work harder."},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        bands = {b["band_min"]: b for b in self.data(response)["bands"]}
        self.assertEqual(bands[70]["override"]["text"], "Outstanding, Tunde.")
        self.assertEqual(bands[70]["override"]["written_by"], "Pauline Principal")
        self.assertIsNotNone(bands[70]["override"]["written_at"])
        self.assertIsNone(bands[55]["override"])
        self.assertEqual(PrincipalComment.objects.count(), 2)

    def test_a_second_save_revises_a_band_rather_than_adding_one(self):
        self.write_principal_comments(self.principal, [{"band_min": 70, "text": "First."}])
        self.write_principal_comments(self.principal, [{"band_min": 70, "text": "Second."}])
        [comment] = PrincipalComment.objects.all()
        self.assertEqual(comment.text, "Second.")

    def test_a_band_the_templates_do_not_list_is_refused_and_nothing_is_written(self):
        response = self.write_principal_comments(self.principal, [
            {"band_min": 70, "text": "Fine."}, {"band_min": 33, "text": "Odd."},
        ])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "UNKNOWN_BAND")
        self.assertFalse(PrincipalComment.objects.exists())

    def test_a_blank_text_is_refused(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                response = self.write_principal_comments(self.principal, [{"band_min": 70, "text": text}])
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.code(response), "COMMENT_REQUIRED")
        self.assertFalse(PrincipalComment.objects.exists())

    def test_a_band_sent_twice_or_a_missing_list_is_a_validation_error(self):
        twice = self.write_principal_comments(self.principal, [
            {"band_min": 70, "text": "A."}, {"band_min": 70, "text": "B."},
        ])
        self.assertEqual(twice.status_code, 400)
        empty = self.write_principal_comments(self.principal, [])
        self.assertEqual(empty.status_code, 400)

    def test_comments_are_final_once_the_class_is_published(self):
        self.write_principal_comments(self.principal, [{"band_min": 70, "text": "Before."}])
        ClassTerm.objects.filter(school_class=self.jss1a).update(stage=StoredStage.PUBLISHED)
        response = self.write_principal_comments(self.principal, [{"band_min": 70, "text": "After."}])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "CLASS_LOCKED")
        self.assertEqual(PrincipalComment.objects.get().text, "Before.")
        body = self.data(self.principal_comments(self.principal))
        self.assertTrue(body["locked"])
        self.assertEqual(body["bands"][0]["override"]["text"], "Before.")

    def test_a_class_with_no_term_row_yet_can_still_have_comments_written(self):
        response = self.write_principal_comments(
            self.lekki_principal, [{"band_min": 0, "text": "Early."}], school_class=self.sss1a,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_only_the_publish_key_reads_or_writes_them(self):
        for user in (self.eze, self.admin, self.bursar, self.nobody):
            with self.subTest(user=user.email):
                self.assertEqual(self.principal_comments(user).status_code, 403)
                self.assertEqual(
                    self.write_principal_comments(user, [{"band_min": 70, "text": "x"}]).status_code,
                    403,
                )
        self.assertFalse(PrincipalComment.objects.exists())

    def test_another_branchs_class_and_another_schools_class_are_404s(self):
        self.assertEqual(self.principal_comments(self.ikeja_principal).status_code, 404)
        self.assertEqual(
            self.write_principal_comments(self.ikeja_principal, [{"band_min": 70, "text": "x"}]).status_code,
            404,
        )
        response = self.call(
            "get", self.solo_principal, "gradebook-principal-comments", class_id=self.jss1a.pk,
            term=self.solo_term,
        )
        self.assertEqual(response.status_code, 404)

    def test_a_save_is_audited(self):
        self.write_principal_comments(self.principal, [{"band_min": 70, "text": "Fine."}])
        event = AuditEvent.objects.filter(
            module_key="GRADEBOOK", summary__contains="principal's comments",
        ).get()
        self.assertIn("JSS1 A, First Term", event.summary)
