"""Telling a child's family about an absence.

Chiamaka's mother has an address, Tunde's mother has none, and Kelechi's mother
has an account. The notification layer is patched in most tests so each asserts
who would be written to; one class seeds the real templates and reads the
rendered message back, because a context key a template does not read renders as
an empty space rather than raising.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification

from ..constants import EVENT_ABSENCE_NOTICE, EVENT_CORRECTION_NOTICE, NoticeState
from ..models import ClassRegister, StudentMark
from .base import TODAY, AttendanceFixture, set_setting

SEND = "schools.vs_attendance.services.notices.send_notification"


def mark_of(student) -> StudentMark:
    return StudentMark.objects.get(student=student)


class _Notices(AttendanceFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(SEND, return_value=["notification-id"])
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def take(self, **marks):
        """Mark JSS1 A's three pupils as the class teacher, then submit."""
        pupils = {"tunde": self.tunde, "kelechi": self.kelechi, "chiamaka": self.chiamaka}
        for name, pupil in pupils.items():
            kind = marks.get(name, "P")
            body = {"arrived_at": "08:30"} if kind == "L" else {}
            self.assertEqual(
                self.mark(self.eze, self.jss1a, pupil, kind, **body).status_code, 200,
            )
        response = self.submit(self.eze, self.jss1a)
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def events(self):
        return [call.kwargs["event_key"] for call in self.send.call_args_list]


class AbsenceNoticeTests(_Notices):
    def test_an_absent_child_with_an_address_is_emailed_once(self):
        self.take(chiamaka="A")
        self.assertEqual(self.events(), [EVENT_ABSENCE_NOTICE])
        call = self.send.call_args.kwargs
        self.assertEqual(call["recipients"], [])
        [recipient] = call["unregistered_recipients"]
        self.assertEqual(recipient.email, "ngozi.nwosu@example.ng")
        self.assertEqual(call["tenant"], self.tenant)
        self.assertEqual(call["branch"], self.lekki)
        self.assertEqual(call["context"]["student_first_name"], "Chiamaka")
        self.assertEqual(call["context"]["branch_name"], "Lekki Branch")
        self.assertTrue(call["context"]["is_today"])
        self.assertEqual(call["context"]["date"], "13 Oct 2025")
        stored = mark_of(self.chiamaka)
        self.assertEqual(stored.notice_state, NoticeState.SENT)
        self.assertIsNotNone(stored.notice_sent_at)

    def test_a_guardian_with_an_account_is_written_to_through_it(self):
        self.take(kelechi="A")
        call = self.send.call_args.kwargs
        self.assertEqual(call["recipients"], [self.eze_parent_user])
        self.assertIsNone(call["unregistered_recipients"])
        self.assertEqual(mark_of(self.kelechi).notice_state, NoticeState.SENT)

    def test_a_guardian_with_no_email_is_recorded_as_no_email_and_nobody_else_is_told(self):
        self.take(tunde="A")
        self.assertEqual(self.send.call_count, 0)
        self.assertEqual(mark_of(self.tunde).notice_state, NoticeState.NO_EMAIL)

    def test_a_child_with_no_primary_guardian_reads_no_email(self):
        from schools.vs_students.models import StudentGuardian

        StudentGuardian.all_objects.filter(student=self.chiamaka).update(is_primary=False)
        self.take(chiamaka="A")
        self.assertEqual(self.send.call_count, 0)
        self.assertEqual(mark_of(self.chiamaka).notice_state, NoticeState.NO_EMAIL)

    def test_each_absent_child_is_its_own_notice(self):
        self.take(chiamaka="A", tunde="A", kelechi="A")
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(
            {mark_of(p).notice_state for p in (self.chiamaka, self.kelechi)},
            {NoticeState.SENT},
        )
        self.assertEqual(mark_of(self.tunde).notice_state, NoticeState.NO_EMAIL)

    def test_late_and_excused_send_nothing(self):
        from ..models import ReasonCode
        from ..services.reasons import ensure_default_reason_codes

        ensure_default_reason_codes(self.tenant)
        sick = ReasonCode.objects.get(tenant=self.tenant, name="Sick")
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.mark(self.eze, self.jss1a, self.kelechi, "L", arrived_at="08:30")
        self.mark(self.eze, self.jss1a, self.chiamaka, "E", reason_code=sick.pk)
        self.assertEqual(self.submit(self.eze, self.jss1a).status_code, 200)
        self.assertEqual(self.send.call_count, 0)
        for pupil in (self.chiamaka, self.kelechi):
            self.assertEqual(mark_of(pupil).notice_state, NoticeState.NONE)

    def test_a_school_with_notices_off_sends_nothing_and_records_why(self):
        set_setting("attendance.guardian_notices", False, self.tenant, self.admin)
        self.take(chiamaka="A", tunde="A")
        self.assertEqual(self.send.call_count, 0)
        for pupil in (self.chiamaka, self.tunde):
            self.assertEqual(mark_of(pupil).notice_state, NoticeState.NOTICES_OFF)

    def test_a_school_that_has_chosen_nothing_has_notices_on(self):
        self.take(chiamaka="A")
        self.assertEqual(self.send.call_count, 1)

    def test_a_child_already_noticed_that_day_in_another_register_is_not_noticed_again(self):
        other = ClassRegister.objects.create(
            tenant=self.tenant, school_class=self.jss1b, date=TODAY, branch=self.lekki,
            session=self.year, term=self.first_term,
        )
        earlier = dt.datetime(2025, 10, 13, 8, 5, tzinfo=dt.timezone.utc)
        StudentMark.objects.create(
            tenant=self.tenant, register=other, student=self.chiamaka, mark="A",
            notice_state=NoticeState.SENT, notice_sent_at=earlier,
        )
        self.take(chiamaka="A")
        self.assertEqual(self.send.call_count, 0)
        stored = StudentMark.objects.get(register__school_class=self.jss1a, student=self.chiamaka)
        self.assertEqual(stored.notice_state, NoticeState.SENT)
        self.assertEqual(stored.notice_sent_at, earlier)

    def test_a_notice_for_an_earlier_day_names_its_date_instead_of_today(self):
        self.mark(self.eze, self.jss1a, self.tunde, "P", on_date=dt.date(2025, 10, 10))
        self.mark(self.eze, self.jss1a, self.kelechi, "P", on_date=dt.date(2025, 10, 10))
        self.mark(self.eze, self.jss1a, self.chiamaka, "A", on_date=dt.date(2025, 10, 10))
        self.submit(self.eze, self.jss1a, dt.date(2025, 10, 10))
        context = self.send.call_args.kwargs["context"]
        self.assertFalse(context["is_today"])
        self.assertEqual(context["date"], "10 Oct 2025")

    def test_a_failure_to_send_does_not_undo_the_submitted_register(self):
        self.send.side_effect = RuntimeError("mail queue is down")
        with self.assertLogs("schools.vs_attendance.services.notices", level="ERROR"):
            response = self.take(chiamaka="A")
        self.assertEqual(self.data(response)["status"], "SUBMITTED")
        self.assertEqual(mark_of(self.chiamaka).notice_state, NoticeState.NONE)

    def test_a_notice_the_notification_layer_sends_nowhere_reads_notices_off(self):
        """The school switched the channel off in its notification settings."""
        self.send.return_value = []
        self.take(chiamaka="A")
        self.assertEqual(mark_of(self.chiamaka).notice_state, NoticeState.NOTICES_OFF)

    def test_nothing_is_sent_when_the_register_is_refused(self):
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        response = self.submit(self.eze, self.jss1a)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.send.call_count, 0)


class PreviewTests(_Notices):
    def preview(self):
        return self.data(self.read(self.eze, self.jss1a))["submit_preview"]

    def test_the_preview_counts_who_will_be_emailed_and_who_has_no_address(self):
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.mark(self.eze, self.jss1a, self.tunde, "A")
        self.mark(self.eze, self.jss1a, self.kelechi, "P")
        self.assertEqual(
            self.preview(), {"notices_on": True, "will_email": 1, "no_email": 1},
        )

    def test_the_preview_says_notices_are_off(self):
        set_setting("attendance.guardian_notices", False, self.tenant, self.admin)
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.assertEqual(
            self.preview(), {"notices_on": False, "will_email": 0, "no_email": 0},
        )

    def test_a_child_already_noticed_today_is_not_in_the_preview(self):
        other = ClassRegister.objects.create(
            tenant=self.tenant, school_class=self.jss1b, date=TODAY, branch=self.lekki,
            session=self.year, term=self.first_term,
        )
        StudentMark.objects.create(
            tenant=self.tenant, register=other, student=self.chiamaka, mark="A",
            notice_state=NoticeState.SENT,
            notice_sent_at=dt.datetime(2025, 10, 13, 8, 5, tzinfo=dt.timezone.utc),
        )
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.assertEqual(self.preview()["will_email"], 0)


class SameDayChangeTests(_Notices):
    def test_the_correction_names_the_new_mark_and_goes_to_the_same_guardian(self):
        self.take(chiamaka="A")
        self.send.reset_mock()
        self.mark(self.eze, self.jss1a, self.chiamaka, "P")
        call = self.send.call_args.kwargs
        self.assertEqual(call["event_key"], EVENT_CORRECTION_NOTICE)
        self.assertEqual(call["context"]["mark_label"], "Present")
        self.assertEqual(call["unregistered_recipients"][0].email, "ngozi.nwosu@example.ng")
        # The record keeps that the family was told.
        self.assertEqual(mark_of(self.chiamaka).notice_state, NoticeState.SENT)

    def test_a_change_before_submission_emails_nobody(self):
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.mark(self.eze, self.jss1a, self.chiamaka, "P")
        self.assertEqual(self.send.call_count, 0)

    def test_changing_an_absence_nobody_was_told_of_sends_no_correction(self):
        self.take(tunde="A")
        self.send.reset_mock()
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.assertEqual(self.send.call_count, 0)
        self.assertEqual(mark_of(self.tunde).notice_state, NoticeState.NONE)

    def test_marking_a_child_absent_after_submission_notices_the_guardian(self):
        self.take()
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.assertEqual(self.events(), [EVENT_ABSENCE_NOTICE])
        self.assertEqual(mark_of(self.chiamaka).notice_state, NoticeState.SENT)

    def test_flipping_to_absent_and_back_does_not_email_the_absence_twice(self):
        self.take(chiamaka="A")
        self.mark(self.eze, self.jss1a, self.chiamaka, "P")
        self.send.reset_mock()
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.assertEqual(self.send.call_count, 0)


class RenderedNoticeTests(AttendanceFixture):
    """The real templates, rendered for the real recipients."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_notifications.services.seed import (
            seed_notification_templates,
            seed_platform_settings,
        )

        seed_notification_templates()
        seed_platform_settings()

    def notices(self, event):
        return Notification.all_objects.filter(event_type__key=event)

    def test_the_guardian_reads_the_absence_in_the_design_s_words(self):
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.mark(self.eze, self.jss1a, self.kelechi, "P")
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.submit(self.eze, self.jss1a)

        email = self.notices(EVENT_ABSENCE_NOTICE).get(channel=ChannelChoices.EMAIL)
        self.assertEqual(email.unregistered_email, "ngozi.nwosu@example.ng")
        self.assertEqual(email.subject, "Chiamaka was marked absent today")
        self.assertIn("Dear Parent or Guardian,", email.body)
        self.assertIn("Chiamaka was marked absent at school today, 13 Oct 2025.", email.body)
        self.assertIn("If this is a mistake, please contact the school.", email.body)
        self.assertIn("Brightfield Schools, Lekki Branch", email.body)

    def test_a_guardian_with_an_account_gets_it_in_the_app_as_well(self):
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.mark(self.eze, self.jss1a, self.chiamaka, "P")
        self.mark(self.eze, self.jss1a, self.kelechi, "A")
        self.submit(self.eze, self.jss1a)

        rows = self.notices(EVENT_ABSENCE_NOTICE)
        self.assertEqual(
            {row.channel for row in rows},
            {ChannelChoices.EMAIL, ChannelChoices.IN_APP},
        )
        in_app = rows.get(channel=ChannelChoices.IN_APP)
        self.assertEqual(in_app.recipient, self.eze_parent_user)
        self.assertEqual(in_app.subject, "Kelechi was marked absent today")

    def test_the_branch_recedes_at_a_school_with_one(self):
        from core.test_utils import TenantAPIClient

        client = TenantAPIClient(self.solo_teacher)
        base = f"/v1/attendance/registers/{self.solo_class.pk}/{TODAY}"
        from schools.vs_students.models import Guardian, StudentGuardian

        guardian = Guardian.all_objects.create(
            tenant=self.solo, full_name="Mr. Okoro", email="okoro@example.ng", phone="0803",
        )
        StudentGuardian.all_objects.create(
            tenant=self.solo, student=self.solo_pupil, guardian=guardian,
            relationship="FATHER", is_primary=True,
        )
        client.put(
            f"{base}/marks/{self.solo_pupil.pk}/", {"mark": "A"}, format="json",
        )
        with self.captureOnCommitCallbacks(execute=True):
            client.post(f"{base}/submit/")

        email = self.notices(EVENT_ABSENCE_NOTICE).get(channel=ChannelChoices.EMAIL)
        self.assertIn("Sunrise Academy via", email.body)
        self.assertNotIn("Main Branch", email.body)

    def test_the_correction_says_what_the_mark_was_corrected_to(self):
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        self.mark(self.eze, self.jss1a, self.kelechi, "P")
        self.mark(self.eze, self.jss1a, self.chiamaka, "A")
        self.submit(self.eze, self.jss1a)
        self.mark(self.eze, self.jss1a, self.chiamaka, "L", arrived_at="10:15")

        email = self.notices(EVENT_CORRECTION_NOTICE).get(channel=ChannelChoices.EMAIL)
        self.assertEqual(email.subject, "Attendance corrected for Chiamaka")
        self.assertIn("corrected to Late", email.body)
