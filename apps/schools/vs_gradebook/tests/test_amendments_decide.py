"""Deciding a correction: what approving changes, what it leaves alone, and rejecting.

The promise under test is that only the amended student's card moves. Every
classmate's snapshot and version are compared byte for byte before and after, the
old version of the amended card is found in ``ResultVersion``, the sheet's score
row shows the corrected mark, and the student's position, totals and class
figures follow the rules of the amendment (against the classmates' published
averages and totals). Then the family: a released result's guardian is written to
once, after commit, with nothing from the result; a held one is not.
"""
from __future__ import annotations

import json

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_gradebook.constants import AmendmentStatus, EmailState
from schools.vs_gradebook.exceptions import NotPending
from schools.vs_gradebook.models import (
    Amendment,
    GradeBand,
    ResultHold,
    ResultVersion,
    Score,
    ScoreSheet,
    StudentTermResult,
)
from schools.vs_gradebook.services import amendments
from vs_audit.models import AuditEvent
from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification
from vs_notifications.services.seed import seed_notification_templates, seed_platform_settings

from .amend_base import AmendmentFixture


def frozen(value):
    """*value* as the exact JSON text it is stored as, so equality is byte equality."""
    return json.dumps(value, sort_keys=True)


class PublicationKeepsEveryVersionTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def test_publishing_wrote_version_one_of_every_snapshot_beside_the_result(self):
        self.assertEqual(ResultVersion.objects.count(), 4)
        for result in StudentTermResult.objects.all():
            version = ResultVersion.objects.get(result=result)
            self.assertEqual(version.version, 1)
            self.assertIsNone(version.amendment)
            self.assertEqual(frozen(version.snapshot), frozen(result.snapshot))
            self.assertEqual(result.version, 1)


class ApproveTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def approved(self, student=None, **body):
        """Raise a correction of the examination to 50 for *student* and approve it; returns its id."""
        found = self.raised(student or self.kelechi)
        response = self.approve_amendment(self.approver, found, **body)
        self.assertEqual(response.status_code, 200, response.data)
        return found

    # ── only the amended card moves ───────────────────────────────────────

    def test_only_the_amended_students_snapshot_and_version_change(self):
        before = {k: (v, frozen(s)) for k, (v, s) in self.snapshots().items()}
        self.approved()
        after = {k: (v, frozen(s)) for k, (v, s) in self.snapshots().items()}
        for student in (self.chiamaka, self.tunde, self.amina):
            with self.subTest(student=student.first_name):
                self.assertEqual(after[student.pk], before[student.pk])
                self.assertEqual(after[student.pk][0], 1)
        self.assertNotEqual(after[self.kelechi.pk][1], before[self.kelechi.pk][1])
        self.assertEqual(after[self.kelechi.pk][0], 2)

    def test_no_classmate_gains_a_version_row(self):
        self.approved()
        for student in (self.chiamaka, self.tunde, self.amina):
            self.assertEqual(
                list(ResultVersion.objects.filter(result__student=student).values_list("version", flat=True)),
                [1],
            )

    def test_the_version_it_replaces_is_kept_and_the_new_one_names_its_amendment(self):
        original = self.snapshots()[self.kelechi.pk][1]
        found = self.approved()
        old, new = ResultVersion.objects.filter(result__student=self.kelechi).order_by("version")
        self.assertEqual((old.version, new.version), (1, 2))
        self.assertEqual(frozen(old.snapshot), frozen(original))
        self.assertIsNone(old.amendment)
        self.assertEqual(new.amendment_id, found)
        result = self.result_of(self.kelechi)
        self.assertEqual(frozen(new.snapshot), frozen(result.snapshot))
        self.assertEqual((result.version, result.snapshot["version"]), (2, 2))
        self.assertEqual(result.snapshot["published_at"], original["published_at"])
        self.assertIn("amended_at", result.snapshot)

    def test_a_result_with_no_version_row_gets_the_one_it_replaces_written_first(self):
        original = self.snapshots()[self.kelechi.pk][1]
        ResultVersion.objects.filter(result__student=self.kelechi).delete()
        self.approved()
        old, new = ResultVersion.objects.filter(result__student=self.kelechi).order_by("version")
        self.assertEqual((old.version, new.version), (1, 2))
        self.assertEqual(frozen(old.snapshot), frozen(original))

    def test_the_sheets_score_row_shows_the_corrected_mark_and_who_asked_for_it(self):
        self.approved()
        sheet = ScoreSheet.objects.get(class_term__school_class=self.jss1a, subject=self.maths)
        exam = Score.objects.get(sheet=sheet, student=self.kelechi, component=self.exam)
        self.assertEqual((exam.mark, exam.marker), (50, None))
        self.assertEqual(exam.amended_by, self.amender)
        self.assertIsNotNone(exam.amended_at)
        ca1 = Score.objects.get(sheet=sheet, student=self.kelechi, component=self.ca1)
        self.assertEqual(ca1.mark, 10)
        self.assertIsNone(ca1.amended_by)
        # Nobody else's marks moved.
        self.assertEqual(Score.objects.filter(amended_by__isnull=False).count(), 1)

    def test_the_sheet_stays_locked_and_reads_the_corrected_mark(self):
        self.approved()
        sheet = ScoreSheet.objects.get(class_term__school_class=self.jss1a, subject=self.maths)
        self.assertEqual(sheet.status, "SUBMITTED")
        body = self.data(self.sheet(self.admin, self.jss1a, self.maths, term=self.first_term))
        row = self.row_for(body, self.kelechi)
        self.assertEqual([c["mark"] for c in row["cells"]], [10, 10, 50])
        self.assertEqual(row["total"], 70)

    # ── the rebuilt card ──────────────────────────────────────────────────

    def test_the_amended_subject_the_totals_and_the_average_are_worked_out_again(self):
        self.approved()
        snapshot = self.result_of(self.kelechi).snapshot
        maths = self.subject_of(snapshot, "Mathematics")
        self.assertEqual(
            (maths["total"], maths["obtainable"], maths["percentage"], maths["grade"],
             maths["remark"], maths["is_fail"]),
            (70, 100, "70.00", "A", "Excellent", False),
        )
        self.assertEqual(
            [(c["mark"], c["marker"]) for c in maths["cells"]], [(10, None), (10, None), (50, None)],
        )
        self.assertEqual((snapshot["total_obtained"], snapshot["total_obtainable"]), (70, 100))
        self.assertEqual(snapshot["overall_average"], "70.00")

    def test_the_position_is_worked_out_against_the_classmates_published_averages(self):
        self.approved()
        snapshot = self.result_of(self.kelechi).snapshot
        # Tunde 75 and Chiamaka 64.5 were published; 70 falls between them.
        self.assertEqual((snapshot["position"], snapshot["roll_size"]), (2, 4))

    def test_a_corrected_average_may_share_a_place_with_a_classmate_without_moving_them(self):
        found = self.raised(self.kelechi, cells=self.cells(None, None, 40))
        self.approve_amendment(self.approver, found)
        kelechi, amina = self.result_of(self.kelechi), self.result_of(self.amina)
        self.assertEqual(kelechi.snapshot["overall_average"], "60.00")
        self.assertEqual(amina.snapshot["overall_average"], "60.00")
        self.assertEqual(kelechi.snapshot["position"], 3)
        self.assertEqual(amina.snapshot["position"], 3)
        self.assertEqual(amina.version, 1)

    def test_the_class_figures_of_the_amended_subject_are_for_this_card_only(self):
        self.approved()
        kelechi = self.subject_of(self.result_of(self.kelechi).snapshot, "Mathematics")
        # The classmates were published on 79, 75 and 60; the new total is 70.
        self.assertEqual(
            (kelechi["class_average"], kelechi["highest"], kelechi["lowest"]), ("71.0", 79, 60),
        )
        tunde = self.subject_of(self.result_of(self.tunde).snapshot, "Mathematics")
        self.assertEqual(
            (tunde["class_average"], tunde["highest"], tunde["lowest"]), ("63.5", 79, 40),
        )

    def test_a_card_never_shows_a_total_above_its_own_highest(self):
        found = self.raised(self.kelechi, cells=self.cells(None, None, 60))
        self.approve_amendment(self.approver, found)
        maths = next(
            s for s in self.card(self.kelechi)["subjects"] if s["name"] == "Mathematics"
        )
        self.assertEqual(maths["total"], 80)
        self.assertGreaterEqual(maths["highest"], maths["total"])
        self.assertEqual(maths["highest"], 80)
        tunde = next(s for s in self.card(self.tunde)["subjects"] if s["name"] == "Mathematics")
        self.assertEqual(tunde["highest"], 79)

    def test_the_other_subjects_and_the_rest_of_the_card_are_carried_over(self):
        before = self.snapshots()[self.tunde.pk][1]
        found = self.raised(self.tunde, cells=self.cells(None, None, 50))
        self.approve_amendment(self.approver, found)
        after = self.result_of(self.tunde).snapshot
        self.assertEqual(
            frozen(self.subject_of(after, "French")), frozen(self.subject_of(before, "French")),
        )
        for key in ("attendance", "teacher_comment", "class_teacher", "ratings", "traits",
                    "grading_key", "next_term_begins", "options", "scale_id", "components"):
            with self.subTest(key=key):
                self.assertEqual(frozen(after[key]), frozen(before[key]))

    def test_a_published_scale_edited_since_does_not_regrade_the_corrected_card(self):
        GradeBand.objects.filter(min_score=70, grade="A").update(min_score=90)
        self.approved()
        maths = self.subject_of(self.result_of(self.kelechi).snapshot, "Mathematics")
        self.assertEqual((maths["total"], maths["grade"]), (70, "A"))

    def test_the_principal_comment_follows_the_band_the_new_average_falls_in(self):
        self.approved()
        self.assertEqual(
            self.result_of(self.kelechi).snapshot["principal_comment"],
            "An excellent result. Keep it up.",
        )

    def test_the_principal_comment_stands_when_the_correction_stays_in_its_band(self):
        was = self.result_of(self.kelechi).snapshot["principal_comment"]
        found = self.raised(self.kelechi, cells=self.cells(None, None, 25))
        self.approve_amendment(self.approver, found)
        snapshot = self.result_of(self.kelechi).snapshot
        self.assertEqual(snapshot["overall_average"], "45.00")
        self.assertEqual(snapshot["principal_comment"], was)

    def test_the_report_card_the_class_print_and_the_results_tab_report_the_new_version(self):
        self.approved()
        self.assertEqual(self.card(self.kelechi)["version"], 2)
        self.assertEqual(self.card(self.tunde)["version"], 1)
        self.assertEqual(self.card(self.kelechi)["subjects"][0]["total"], 70)
        tab = self.data(self.results_tab(self.result_reader, self.kelechi))
        self.assertEqual(tab["terms"][0]["version"], 2)
        self.assertEqual(self.data(self.results_tab(self.result_reader, self.tunde))["terms"][0]["version"], 1)
        cards = self.data(self.class_cards(self.releaser))["cards"]
        self.assertEqual(sorted(card["version"] for card in cards), [1, 1, 2])

    def test_a_second_correction_builds_on_the_first_and_keeps_both_versions(self):
        self.approved()
        again = self.raised(self.kelechi, cells=self.cells(None, None, 55))
        self.assertEqual(self.approve_amendment(self.approver, again).status_code, 200)
        versions = list(
            ResultVersion.objects.filter(result__student=self.kelechi)
            .order_by("version").values_list("version", flat=True)
        )
        self.assertEqual(versions, [1, 2, 3])
        second = Amendment.objects.get(pk=again)
        self.assertEqual(second.before[2]["mark"], 50)
        self.assertEqual(self.result_of(self.kelechi).version, 3)

    # ── the answer and the audit trail ────────────────────────────────────

    def test_the_answer_is_the_amendment_as_decided(self):
        found = self.raised(self.kelechi)
        response = self.approve_amendment(self.approver, found, note="Checked the script.")
        body = self.data(response)
        self.assertEqual(body["status"], "APPROVED")
        self.assertEqual(body["decided_by"], "Apollo Approve")
        self.assertEqual(body["decision_note"], "Checked the script.")
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["after"]["total"], 70)

    def test_a_note_is_optional_when_approving(self):
        found = self.raised(self.kelechi)
        self.assertEqual(self.data(self.approve_amendment(self.approver, found))["decision_note"], "")

    def test_the_audit_trail_records_who_approved_and_the_version(self):
        self.approved(note="Private.")
        event = AuditEvent.objects.get(
            module_key="GRADEBOOK", entity_type="Amendment", summary__contains="approved",
        )
        self.assertIn("Apollo Approve approved the correction to Mathematics", event.summary)
        self.assertIn("version 2", event.summary)
        self.assertNotIn("Private", event.summary + str(event.metadata))

    # ── a second decision ─────────────────────────────────────────────────

    def test_an_amendment_is_decided_once(self):
        found = self.approved()
        stamp = Amendment.objects.get(pk=found).decided_at
        for response in (
            self.approve_amendment(self.approver, found),
            self.reject_amendment(self.approver, found, note="Too late."),
        ):
            self.assertEqual((response.status_code, self.code(response)), (409, "NOT_PENDING"))
        again = Amendment.objects.get(pk=found)
        self.assertEqual((again.status, again.decided_at), (AmendmentStatus.APPROVED, stamp))
        self.assertEqual(self.result_of(self.kelechi).version, 2)
        self.assertEqual(ResultVersion.objects.filter(result__student=self.kelechi).count(), 2)

    def test_a_rejected_amendment_cannot_then_be_approved(self):
        found = self.raised(self.kelechi)
        self.reject_amendment(self.approver, found, note="No.")
        response = self.approve_amendment(self.approver, found)
        self.assertEqual((response.status_code, self.code(response)), (409, "NOT_PENDING"))
        self.assertEqual(self.result_of(self.kelechi).version, 1)

    def test_an_approval_that_lost_the_race_finds_the_amendment_decided(self):
        """The status read before the lock is not trusted: the row is read again under it."""
        found = self.raised(self.kelechi)
        stale = amendments.open_for_decision(self.tenant, self.approver, found)
        self.assertEqual(stale.status, AmendmentStatus.PENDING)
        self.approve_amendment(self.lekki_approver, found)
        with self.assertRaises(NotPending):
            amendments.approve(self.tenant, self.approver, stale, "")
        self.assertEqual(self.result_of(self.kelechi).version, 2)

    def test_the_students_result_row_is_locked_while_approving(self):
        found = self.raised(self.kelechi)
        with CaptureQueriesContext(connection) as captured:
            self.approve_amendment(self.approver, found)
        locking = [
            q["sql"] for q in captured
            if "FOR UPDATE" in q["sql"] and "vs_gradebook_studenttermresult" in q["sql"]
        ]
        self.assertTrue(locking)


class RejectTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def test_rejecting_changes_nothing_and_keeps_the_note(self):
        found = self.raised(self.kelechi)
        snapshots = {k: (v, frozen(s)) for k, (v, s) in self.snapshots().items()}
        scores = list(Score.objects.order_by("pk").values_list("pk", "mark", "marker", "amended_by"))
        versions = ResultVersion.objects.count()

        response = self.reject_amendment(self.approver, found, note="The script was right.")
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["status"], "REJECTED")
        self.assertEqual(body["decision_note"], "The script was right.")
        self.assertEqual(body["decided_by"], "Apollo Approve")
        self.assertIsNone(body["version"])

        stored = Amendment.objects.get(pk=found)
        self.assertEqual(stored.status, AmendmentStatus.REJECTED)
        self.assertEqual(stored.decided_by, self.approver)
        self.assertIsNotNone(stored.decided_at)
        self.assertEqual(
            {k: (v, frozen(s)) for k, (v, s) in self.snapshots().items()}, snapshots,
        )
        self.assertEqual(
            list(Score.objects.order_by("pk").values_list("pk", "mark", "marker", "amended_by")), scores,
        )
        self.assertEqual(ResultVersion.objects.count(), versions)

    def test_a_note_is_required_to_reject(self):
        found = self.raised(self.kelechi)
        for body in ({}, {"note": ""}, {"note": "   "}):
            with self.subTest(body=body):
                response = self.reject_amendment(self.approver, found, **body)
                self.assertEqual(
                    (response.status_code, self.code(response)), (422, "NOTE_REQUIRED"),
                )
                self.assertEqual(response.data["error"]["detail"]["field"], "note")
        self.assertEqual(Amendment.objects.get(pk=found).status, AmendmentStatus.PENDING)

    def test_the_registered_name_of_the_note_is_accepted_too(self):
        found = self.raised(self.kelechi)
        response = self.reject_amendment(self.approver, found, decision_note="Not warranted.")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Amendment.objects.get(pk=found).decision_note, "Not warranted.")

    def test_rejecting_tells_nobody(self):
        found = self.raised(self.chiamaka)
        self.reject_amendment(self.approver, found, note="No.")
        self.send.assert_not_called()

    def test_the_audit_trail_records_who_rejected_and_not_why(self):
        found = self.raised(self.kelechi)
        self.reject_amendment(self.approver, found, note="Private.")
        event = AuditEvent.objects.get(
            module_key="GRADEBOOK", entity_type="Amendment", summary__contains="rejected",
        )
        self.assertIn("Apollo Approve rejected the correction to Mathematics", event.summary)
        self.assertNotIn("Private", event.summary + str(event.metadata))


class GuardianNoticeTests(AmendmentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.published()

    def updated_calls(self):
        return [
            c for c in self.send.call_args_list
            if c.kwargs["event_key"] == "gradebook.report_card_updated"
        ]

    def test_a_released_students_guardian_with_an_address_is_written_to_once(self):
        found = self.raised(self.chiamaka)
        self.approve_amendment(self.approver, found)
        [call] = self.updated_calls()
        self.assertEqual(call.kwargs["recipients"], [])
        [recipient] = call.kwargs["unregistered_recipients"]
        self.assertEqual(recipient.email, "ngozi.nwosu@example.ng")
        self.assertEqual(call.kwargs["tenant"], self.tenant)
        self.assertEqual(call.kwargs["branch"], self.lekki)

    def test_the_context_is_the_ready_context_and_carries_nothing_from_the_result(self):
        found = self.raised(self.chiamaka)
        self.approve_amendment(self.approver, found)
        [call] = self.updated_calls()
        self.assertEqual(call.kwargs["context"], {
            "student_first_name": "Chiamaka", "term_name": "First Term",
            "session_name": "2025/2026", "tenant_name": "Brightfield Schools",
            "branch_name": "Lekki Branch", "tenant_phone": self.school.phone or "",
        })

    def test_a_guardian_with_an_account_is_written_to_through_it(self):
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found)
        [call] = self.updated_calls()
        self.assertEqual(call.kwargs["recipients"], [self.eze_mother_user])
        self.assertIsNone(call.kwargs["unregistered_recipients"])

    def test_a_guardian_with_no_address_is_not_written_to(self):
        found = self.raised(self.tunde)
        self.approve_amendment(self.approver, found)
        self.assertEqual(self.updated_calls(), [])

    def test_a_held_students_guardian_is_not_written_to_and_the_hold_is_untouched(self):
        found = self.raised(self.amina)
        self.assertEqual(self.approve_amendment(self.approver, found).status_code, 200)
        self.assertEqual(self.updated_calls(), [])
        result = self.result_of(self.amina)
        self.assertEqual((result.version, result.email_state), (2, EmailState.HELD))
        self.assertIsNone(result.released_at)

    def test_lifting_the_hold_afterwards_releases_the_corrected_card_with_the_ready_email(self):
        found = self.raised(self.amina)
        self.approve_amendment(self.approver, found)
        hold = ResultHold.objects.get(student=self.amina)
        self.assertEqual(self.lift(self.bursar, hold.pk).status_code, 200)
        events = [c.kwargs["event_key"] for c in self.send.call_args_list]
        self.assertEqual(events, ["gradebook.report_card_ready"])
        self.assertEqual(self.card(self.amina)["version"], 2)

    def test_nothing_is_sent_until_the_transaction_commits(self):
        found = self.raised(self.chiamaka)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            TenantAPIClient(self.approver).post(
                reverse("gradebook-amendment-approve", kwargs={"pk": found}), {}, format="json",
            )
        self.assertEqual(self.updated_calls(), [])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(self.updated_calls()), 1)

    def test_a_failed_send_leaves_the_correction_approved(self):
        self.send.side_effect = RuntimeError("smtp down")
        found = self.raised(self.chiamaka)
        response = self.approve_amendment(self.approver, found)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Amendment.objects.get(pk=found).status, AmendmentStatus.APPROVED)
        self.assertEqual(self.result_of(self.chiamaka).version, 2)

    def test_the_readys_email_record_is_left_alone(self):
        before = self.result_of(self.chiamaka)
        found = self.raised(self.chiamaka)
        self.approve_amendment(self.approver, found)
        after = self.result_of(self.chiamaka)
        self.assertEqual(
            (after.email_state, after.emailed_at, after.released_at),
            (before.email_state, before.emailed_at, before.released_at),
        )

    def test_raising_tells_nobody(self):
        self.raised(self.chiamaka)
        self.send.assert_not_called()


class RenderedUpdatedEmailTests(AmendmentFixture):
    """The real template, rendered for the real recipient."""

    real_notifications = True

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed_notification_templates()
        seed_platform_settings()
        cls.published()

    def notices(self):
        return Notification.all_objects.filter(event_type__key="gradebook.report_card_updated")

    def test_the_email_says_an_updated_card_is_ready_and_carries_nothing_from_the_result(self):
        self.school.phone = "0801 234 5678"
        self.school.save(update_fields=["phone"])
        found = self.raised(self.chiamaka, cells=self.cells(None, None, 50))
        self.assertEqual(self.approve_amendment(self.approver, found).status_code, 200)
        email = self.notices().get(
            channel=ChannelChoices.EMAIL, unregistered_email="ngozi.nwosu@example.ng",
        )
        self.assertEqual(email.subject, "Chiamaka's updated report card is ready")
        self.assertIn(
            "An updated report card for Chiamaka (First Term, 2025/2026) is ready to collect "
            "from the school.", email.body,
        )
        self.assertIn("You can reach the school on 0801 234 5678.", email.body)
        text = (email.subject + email.body).lower()
        for leak in ("grade", "score", "position", "average", "comment", "http", "attach", "mathematics"):
            self.assertNotIn(leak, text)
        stripped = text.replace("2025/2026", "").replace("0801 234 5678", "")
        for figure in ("50", "83", "79", "64.5", "66.5"):
            self.assertNotIn(figure, stripped)

    def test_a_guardian_with_an_account_gets_it_in_the_app_as_well(self):
        found = self.raised(self.kelechi)
        self.approve_amendment(self.approver, found)
        mine = self.notices().filter(recipient=self.eze_mother_user)
        self.assertEqual(
            {(n.channel, n.subject) for n in mine},
            {
                (ChannelChoices.EMAIL, "Kelechi's updated report card is ready"),
                (ChannelChoices.IN_APP, "Kelechi's updated report card is ready"),
            },
        )

    def test_a_held_child_is_sent_nothing(self):
        found = self.raised(self.amina)
        self.approve_amendment(self.approver, found)
        self.assertFalse(self.notices().exists())
