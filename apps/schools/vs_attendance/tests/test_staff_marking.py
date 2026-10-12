"""Taking a staff register: who may, what is refused, and the life of a day.

Security first (the permission keys, branch reach and another school), then the
marks, the people who cannot be marked, the lifecycle (fill in, submit, change
within the edit window, frozen after it), and the promise that a staff register
tells nobody anything.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from django.core import mail
from django.db import IntegrityError, transaction

from vs_audit.models import AuditEvent
from vs_notifications.models import Notification
from vs_user.models import AccountLockout

from ..constants import CFG_EDIT_WINDOW_DAYS
from ..models import StaffDay, StaffMark, StaffMarkChange
from .base import FOUNDERS_DAY, LAST_FRIDAY, TODAY, set_setting
from .staff_base import StaffFixture


class WhoMayWriteTests(StaffFixture):
    def test_somebody_who_may_only_view_cannot_write_anything(self):
        for response in (
            self.mark_staff(self.viewer, self.lekki, self.funke, "P"),
            self.fill_staff(self.viewer, self.lekki),
            self.submit_staff(self.viewer, self.lekki),
        ):
            self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(StaffMark.objects.count(), 0)

    def test_somebody_who_may_only_mark_cannot_read(self):
        self.assertEqual(self.staff_day(self.marker).status_code, 403)
        self.assertEqual(self.mark_staff(self.marker, self.lekki, self.funke, "P").status_code, 200)

    def test_somebody_with_no_attendance_key_is_refused_everything(self):
        for response in (
            self.staff_day(self.nobody),
            self.mark_staff(self.nobody, self.lekki, self.funke, "P"),
            self.fill_staff(self.nobody, self.lekki),
            self.submit_staff(self.nobody, self.lekki),
        ):
            self.assertEqual(response.status_code, 403, response.data)

    def test_a_teacher_holding_the_class_register_keys_cannot_touch_the_staff_register(self):
        self.assertEqual(self.staff_day(self.eze).status_code, 403)
        self.assertEqual(self.mark_staff(self.eze, self.lekki, self.funke, "P").status_code, 403)

    def test_a_branch_admin_cannot_write_to_another_branchs_register(self):
        for response in (
            self.mark_staff(self.lekki_hr, self.ikeja, self.tunde_b, "P"),
            self.fill_staff(self.lekki_hr, self.ikeja),
            self.submit_staff(self.lekki_hr, self.ikeja),
        ):
            self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(StaffMark.objects.count(), 0)

    def test_a_branch_admin_marks_their_own_branch(self):
        response = self.mark_staff(self.lekki_hr, self.lekki, self.funke, "A")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored_mark(self.funke).mark, "A")

    def test_somebody_on_another_branchs_staff_is_not_on_this_register(self):
        response = self.mark_staff(self.registrar, self.lekki, self.emeka_n, "P")

        self.assertEqual(response.status_code, 404)

    def test_another_schools_branch_and_staff_answer_404(self):
        for response in (
            self.mark_staff(self.registrar, self.solo_branch, self.halima, "P"),
            self.fill_staff(self.registrar, self.solo_branch),
            self.submit_staff(self.registrar, self.solo_branch),
            self.mark_staff(self.registrar, self.lekki, self.halima, "P"),
            self.mark_staff(self.solo_registrar, self.lekki, self.funke, "P"),
            self.mark_staff(self.solo_registrar, self.solo_branch, self.funke, "P"),
        ):
            self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(StaffMark.objects.count(), 0)

    def test_a_person_who_does_not_exist_is_a_404(self):
        response = self.mark_staff(self.registrar, self.lekki, SimpleNamespace(pk=999999), "P")

        self.assertEqual(response.status_code, 404)


class NoHomeBranchWriteTests(StaffFixture):
    def test_a_whole_school_caller_is_refused_with_the_fix_named(self):
        response = self.mark_staff(self.registrar, self.lekki, self.adaeze, "P")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "NO_HOME_BRANCH")
        self.assertEqual(
            response.data["message"],
            "Give Adaeze Nwankwo a home branch in Staff Management before marking them.",
        )
        self.assertEqual(StaffMark.objects.count(), 0)

    def test_the_refusal_is_the_same_whichever_branch_the_write_names(self):
        for branch in (self.lekki, self.ikeja):
            with self.subTest(branch=branch.name):
                response = self.mark_staff(self.registrar, branch, self.adaeze, "A")
                self.assertEqual(self.code(response), "NO_HOME_BRANCH")

    def test_a_branch_admin_cannot_see_them_to_be_refused(self):
        response = self.mark_staff(self.lekki_hr, self.lekki, self.adaeze, "P")

        self.assertEqual(response.status_code, 404)

    def test_fill_present_and_submit_leave_them_alone(self):
        self.lekki_everyone_marked()
        self.fill_staff(self.registrar, self.ikeja)

        self.assertFalse(StaffMark.objects.filter(staff=self.adaeze).exists())
        self.assertEqual(self.submit_staff(self.registrar, self.lekki).status_code, 200)

    def test_they_become_markable_the_moment_they_are_given_a_home_branch(self):
        self.adaeze.branch = self.lekki
        self.adaeze.save(update_fields=["branch"])

        response = self.mark_staff(self.registrar, self.lekki, self.adaeze, "P")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(self.staff_day(self.registrar))["no_home_branch"]["count"], 0)

    def test_at_a_school_with_one_branch_the_rule_never_bites(self):
        response = self.mark_staff(self.solo_registrar, self.solo_branch, self.kola, "L", arrived_at="09:05")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored_mark(self.kola).staff_day.branch, self.solo_branch)
        row = self.staff_rows(response, self.solo_branch)[self.kola.pk]
        self.assertEqual((row["mark"], row["arrived_at"], row["locked"]), ("L", "09:05", False))


class MarkTests(StaffFixture):
    def test_a_mark_is_saved_and_the_register_comes_back(self):
        response = self.mark_staff(self.registrar, self.lekki, self.funke, "P")

        self.assertEqual(response.status_code, 200, response.data)
        group = self.group(response, self.lekki)
        self.assertEqual(group["branch"]["id"], self.lekki.pk)
        self.assertEqual(group["status"], "IN_PROGRESS")
        self.assertEqual(group["counts"]["present"], 1)
        self.assertEqual(group["counts"]["unmarked"], 3)
        self.assertEqual(self.staff_rows(response, self.lekki)[self.funke.pk]["mark"], "P")

    def test_a_late_mark_needs_the_time_the_person_arrived(self):
        refused = self.mark_staff(self.registrar, self.lekki, self.peter, "L")
        saved = self.mark_staff(self.registrar, self.lekki, self.peter, "L", arrived_at="08:25:40")

        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self.code(refused), "ARRIVAL_TIME_REQUIRED")
        row = self.staff_rows(saved, self.lekki)[self.peter.pk]
        self.assertEqual((row["mark"], row["arrived_at"]), ("L", "08:25"))
        self.assertEqual(self.stored_mark(self.peter).arrived_at, dt.time(8, 25))

    def test_a_late_mark_counts_as_present_and_as_late(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "L", arrived_at="08:25")
        response = self.mark_staff(self.registrar, self.lekki, self.funke, "A")

        counts = self.group(response, self.lekki)["counts"]
        self.assertEqual((counts["present"], counts["late"], counts["absent"]), (1, 1, 1))

    def test_a_different_mark_drops_the_arrival_time(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "L", arrived_at="08:25")
        self.mark_staff(self.registrar, self.lekki, self.peter, "P")

        self.assertIsNone(self.stored_mark(self.peter).arrived_at)

    def test_excused_is_not_a_staff_mark(self):
        response = self.mark_staff(self.registrar, self.lekki, self.peter, "E")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(StaffMark.objects.count(), 0)

    def test_the_note_is_kept_unless_a_new_one_is_sent(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "A", note="Phoned in sick")
        self.mark_staff(self.registrar, self.lekki, self.peter, "P")
        kept = self.stored_mark(self.peter).note
        self.mark_staff(self.registrar, self.lekki, self.peter, "P", note="")

        self.assertEqual(kept, "Phoned in sick")
        self.assertEqual(self.stored_mark(self.peter).note, "")

    def test_saving_the_same_row_again_writes_nothing(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "P")
        first = self.stored_mark(self.peter)

        self.mark_staff(self.registrar, self.lekki, self.peter, "P")

        self.assertEqual(self.stored_mark(self.peter).marked_at, first.marked_at)
        self.assertEqual(StaffMark.objects.count(), 1)

    def test_somebody_on_approved_leave_cannot_be_marked_over(self):
        self.approve_leave(self.funke, self.days_ago(1), TODAY + dt.timedelta(days=3))

        response = self.mark_staff(self.registrar, self.lekki, self.funke, "A")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "ON_LEAVE")
        self.assertEqual(StaffMark.objects.count(), 0)

    def test_leave_that_ended_yesterday_can_be_marked_today(self):
        self.approve_leave(self.funke, self.days_ago(4), self.days_ago(1))

        response = self.mark_staff(self.registrar, self.lekki, self.funke, "P")

        self.assertEqual(response.status_code, 200, response.data)

    def test_a_mark_taken_before_leave_was_approved_is_hidden_and_uncounted(self):
        self.mark_staff(self.registrar, self.lekki, self.funke, "A")
        self.approve_leave(self.funke, self.days_ago(1), TODAY + dt.timedelta(days=3))

        group = self.group(self.staff_day(self.registrar), self.lekki)

        row = {r["staff"]["id"]: r for r in group["rows"]}[self.funke.pk]
        self.assertIsNone(row["mark"])
        self.assertEqual(group["counts"]["absent"], 0)
        self.assertEqual(group["counts"]["on_leave"], 1)

    def test_a_closed_day_has_no_register_to_mark(self):
        response = self.mark_staff(self.registrar, self.lekki, self.funke, "P", on_date=FOUNDERS_DAY)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "DAY_CLOSED_NO_REGISTER")

    def test_a_branch_closure_closes_only_its_own_register(self):
        day = dt.date(2025, 10, 7)

        lekki = self.mark_staff(self.registrar, self.lekki, self.funke, "P", on_date=day)
        ikeja = self.mark_staff(self.registrar, self.ikeja, self.tunde_b, "P", on_date=day)

        self.assertEqual(self.code(lekki), "DAY_CLOSED_NO_REGISTER")
        self.assertEqual(ikeja.status_code, 200, ikeja.data)

    def test_a_day_that_has_not_come_cannot_be_marked(self):
        tomorrow = TODAY + dt.timedelta(days=1)

        for response in (
            self.mark_staff(self.registrar, self.lekki, self.funke, "P", on_date=tomorrow),
            self.fill_staff(self.registrar, self.lekki, on_date=tomorrow),
            self.submit_staff(self.registrar, self.lekki, on_date=tomorrow),
        ):
            self.assertEqual(self.code(response), "DATE_IN_FUTURE")

    def test_a_past_day_never_taken_can_still_be_taken(self):
        response = self.mark_staff(self.registrar, self.lekki, self.funke, "A", on_date=LAST_FRIDAY)

        self.assertEqual(response.status_code, 200, response.data)

    def test_one_mark_per_person_per_day_is_a_rule_of_the_database(self):
        self.mark_staff(self.registrar, self.lekki, self.funke, "P")
        other = StaffDay.objects.create(tenant=self.tenant, branch=self.ikeja, date=TODAY)

        with self.assertRaises(IntegrityError), transaction.atomic():
            StaffMark.objects.create(
                tenant=self.tenant, staff_day=other, staff=self.funke, date=TODAY, mark="A",
            )

    def test_a_person_who_changes_branch_keeps_their_one_mark_and_it_moves_with_them(self):
        self.mark_staff(self.registrar, self.lekki, self.funke, "P")
        self.funke.branch = self.ikeja
        self.funke.save(update_fields=["branch"])

        shown = self.staff_rows(self.staff_day(self.registrar), self.ikeja)[self.funke.pk]
        moved = self.mark_staff(self.registrar, self.ikeja, self.funke, "A")

        self.assertEqual(shown["mark"], "P")
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual(StaffMark.objects.filter(staff=self.funke, date=TODAY).count(), 1)
        self.assertEqual(self.stored_mark(self.funke).staff_day.branch, self.ikeja)


class FillAndSubmitTests(StaffFixture):
    def test_fill_present_marks_the_unmarked_and_submits_nothing(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "L", arrived_at="08:40")
        self.approve_leave(self.funke, self.days_ago(1), TODAY + dt.timedelta(days=3))

        response = self.fill_staff(self.registrar, self.lekki)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.data(response)["filled"], 2)
        rows = self.staff_rows(response, self.lekki)
        self.assertEqual(rows[self.peter.pk]["mark"], "L")
        self.assertEqual(rows[self.eze_staff.pk]["mark"], "P")
        self.assertEqual(rows[self.bello_staff.pk]["mark"], "P")
        self.assertIsNone(rows[self.funke.pk]["mark"])
        self.assertEqual(self.group(response, self.lekki)["status"], "READY")
        self.assertIsNone(self.group(response, self.lekki)["submitted_at"])

    def test_fill_present_with_nothing_left_creates_nothing(self):
        self.lekki_everyone_marked()

        response = self.fill_staff(self.registrar, self.lekki)

        self.assertEqual(self.data(response)["filled"], 0)
        self.assertEqual(response.data["message"], "Nobody was left to mark.")

    def test_fill_present_does_not_create_a_register_when_there_is_nothing_to_fill(self):
        for person in (self.emeka_n, self.tunde_b):
            self.approve_leave(person, self.days_ago(1), TODAY + dt.timedelta(days=3))

        self.fill_staff(self.registrar, self.ikeja)

        self.assertFalse(StaffDay.objects.exists())

    def test_submit_is_refused_while_a_row_is_unmarked(self):
        self.mark_staff(self.registrar, self.lekki, self.peter, "P")

        response = self.submit_staff(self.registrar, self.lekki)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "UNMARKED_ROWS")
        self.assertIn("Still 3 unmarked", response.data["message"])

    def test_submit_records_who_and_when_and_leaves_the_other_branch_alone(self):
        self.lekki_everyone_marked(adeyemi="A", achebe="L")

        response = self.submit_staff(self.lekki_hr, self.lekki)

        self.assertEqual(response.status_code, 200, response.data)
        group = self.group(response, self.lekki)
        self.assertEqual(group["status"], "SUBMITTED")
        self.assertEqual(group["submitted_by_name"], "Lola Ade")
        self.assertIsNotNone(group["submitted_at"])
        self.assertTrue(group["edit_window_open"])
        ikeja = self.group(self.staff_day(self.registrar), self.ikeja)
        self.assertEqual(ikeja["status"], "NOT_STARTED")

    def test_a_register_cannot_be_submitted_twice(self):
        self.lekki_everyone_marked()
        self.submit_staff(self.registrar, self.lekki)

        response = self.submit_staff(self.registrar, self.lekki)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.code(response), "ALREADY_SUBMITTED")

    def test_people_on_leave_do_not_hold_a_submit_up(self):
        for person in (self.peter, self.funke, self.bello_staff, self.eze_staff):
            self.approve_leave(person, self.days_ago(1), TODAY + dt.timedelta(days=3))

        response = self.submit_staff(self.registrar, self.lekki)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.group(response, self.lekki)["counts"]["on_leave"], 4)

    def test_a_branch_with_nobody_on_its_register_has_nothing_to_submit(self):
        empty = self.make_branch_without_staff()

        response = self.submit_staff(self.registrar, empty)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "EMPTY_STAFF_REGISTER")

    def make_branch_without_staff(self):
        from vs_rbac.tests.helpers import make_branch

        return make_branch(self.school, name="Yaba Branch", is_main=False)

    def test_submitting_a_register_counts_it_in_the_audit_trail(self):
        self.lekki_everyone_marked(adeyemi="A")
        self.submit_staff(self.registrar, self.lekki)

        event = AuditEvent.objects.get(tenant=self.tenant, entity_type="StaffDay")

        self.assertEqual(event.module_key, "ATTENDANCE")
        self.assertIn("3 present, 0 late, 1 absent", event.summary)


class ChangingASubmittedRegisterTests(StaffFixture):
    def submitted(self, on_date=TODAY):
        """Everyone at Lekki marked Present on *on_date*, and the register submitted."""
        for person in (self.peter, self.funke, self.bello_staff, self.eze_staff):
            response = self.mark_staff(self.registrar, self.lekki, person, "P", on_date=on_date)
            self.assertEqual(response.status_code, 200, response.data)
        response = self.submit_staff(self.registrar, self.lekki, on_date)
        self.assertEqual(response.status_code, 200, response.data)

    def test_anybody_who_may_mark_can_change_a_row_the_same_day_and_it_is_kept_as_history(self):
        self.submitted()

        response = self.mark_staff(self.lekki_hr, self.lekki, self.funke, "A", note="Left at noon")

        self.assertEqual(response.status_code, 200, response.data)
        change = StaffMarkChange.objects.get()
        self.assertEqual((change.from_mark, change.to_mark), ("P", "A"))
        self.assertEqual(change.changed_by, self.lekki_hr)
        event = AuditEvent.objects.filter(
            tenant=self.tenant, entity_type="StaffDay", summary__contains="changed from",
        ).get()
        self.assertIn("Funke Adeyemi changed from Present to Absent", event.summary)

    def test_changing_nothing_records_no_history(self):
        self.submitted()

        self.mark_staff(self.registrar, self.lekki, self.funke, "P")

        self.assertEqual(StaffMarkChange.objects.count(), 0)

    def test_a_draft_change_is_not_history(self):
        self.mark_staff(self.registrar, self.lekki, self.funke, "P")
        self.mark_staff(self.registrar, self.lekki, self.funke, "A")

        self.assertEqual(StaffMarkChange.objects.count(), 0)

    def test_somebody_who_joins_after_the_submit_can_be_filled_in_and_it_is_kept_as_history(self):
        self.submitted()
        joiner = self.make_person("Late", "Joiner", self.lekki)

        response = self.fill_staff(self.registrar, self.lekki)

        self.assertEqual(self.data(response)["filled"], 1)
        change = StaffMarkChange.objects.get()
        self.assertEqual((change.mark.staff, change.from_mark, change.to_mark), (joiner, "", "P"))

    def test_the_day_after_it_closes_a_submitted_mark_is_frozen(self):
        self.submitted(LAST_FRIDAY)
        self.make_person("Late", "Joiner", self.lekki)

        for response in (
            self.mark_staff(self.registrar, self.lekki, self.funke, "A", on_date=LAST_FRIDAY),
            self.fill_staff(self.registrar, self.lekki, on_date=LAST_FRIDAY),
        ):
            self.assertEqual(response.status_code, 422)
            self.assertEqual(self.code(response), "STAFF_DAY_CLOSED")
        self.assertEqual(StaffMark.objects.get(staff=self.funke, date=LAST_FRIDAY).mark, "P")
        self.assertEqual(StaffMarkChange.objects.count(), 0)

    def test_the_school_can_give_the_window_more_days(self):
        set_setting(CFG_EDIT_WINDOW_DAYS, 3, self.tenant, self.admin)
        self.submitted(LAST_FRIDAY)

        response = self.mark_staff(self.registrar, self.lekki, self.funke, "A", on_date=LAST_FRIDAY)

        self.assertEqual(response.status_code, 200, response.data)
        group = self.group(self.staff_day(self.registrar, LAST_FRIDAY), self.lekki)
        self.assertTrue(group["edit_window_open"])

    def test_the_window_shuts_after_the_days_it_was_given(self):
        set_setting(CFG_EDIT_WINDOW_DAYS, 2, self.tenant, self.admin)
        self.submitted(LAST_FRIDAY)

        response = self.mark_staff(self.registrar, self.lekki, self.funke, "A", on_date=LAST_FRIDAY)

        self.assertEqual(self.code(response), "STAFF_DAY_CLOSED")
        group = self.group(self.staff_day(self.registrar, LAST_FRIDAY), self.lekki)
        self.assertFalse(group["edit_window_open"])

    def test_the_refusal_says_the_mark_stays_as_it_was(self):
        self.submitted(LAST_FRIDAY)

        response = self.mark_staff(self.registrar, self.lekki, self.funke, "A", on_date=LAST_FRIDAY)

        self.assertEqual(
            response.data["message"],
            "This day is closed. Staff marks cannot be changed after the day closes.",
        )


class NobodyIsToldTests(StaffFixture):
    def test_a_whole_day_of_absences_sends_no_notification_and_no_email(self):
        notifications = Notification.objects.count()
        for person in (self.peter, self.funke, self.bello_staff, self.eze_staff):
            with self.captureOnCommitCallbacks(execute=True):
                self.mark_staff(self.registrar, self.lekki, person, "A")
        with self.captureOnCommitCallbacks(execute=True):
            self.submit_staff(self.registrar, self.lekki)
        with self.captureOnCommitCallbacks(execute=True):
            self.mark_staff(self.registrar, self.lekki, self.funke, "P")

        self.assertEqual(Notification.objects.count(), notifications)
        self.assertEqual(mail.outbox, [])

    def test_the_register_never_serves_sign_in_state(self):
        AccountLockout.objects.create(
            user=self.funke.user, failure_count=3,
            locked_until=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc),
            locked_reason="Three failed sign-ins",
        )
        self.assertTrue(self.funke.user.is_locked)

        response = self.staff_day(self.registrar)
        rows = self.staff_rows(response, self.lekki)

        locked_out, other = rows[self.funke.pk], rows[self.peter.pk]
        self.assertEqual(set(locked_out), set(other))
        self.assertFalse(locked_out["locked"])
        text = repr(self.data(response)).lower()
        for word in ("lockout", "failed sign", "sign-in", "locked_until", "failure"):
            self.assertNotIn(word, text)
