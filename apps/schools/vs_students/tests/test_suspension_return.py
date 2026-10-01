"""A suspension with an end date brings the pupil back on that day by itself.

The sweep reads the whole platform, so the cases that matter are the ones where
a row looks due and is not: a pupil withdrawn since, a suspension whose date
has not arrived at the pupil's own branch, a suspension with no date at all,
and a day the job already swept.

Brightfield runs Lekki and Ikeja; Ikeja keeps Nairobi time, so at 21:30 UTC on
14 March it is still the 14th at Lekki and already the 15th at Ikeja. Sunrise
runs one branch and keeps the school's zone, which is the proof that a school
with nothing set sees its own day and not the server's.

The sweep is called as a function rather than through the endpoint because
there is no endpoint: it is a worker's job. One test calls the Celery task, to
prove the beat entry and the task name reach the service.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest import mock

from vs_audit.models import AuditActionType, AuditEvent
from vs_config.clock import TIME_ZONE_KEY, forget_tenant_zone, tenant_zone
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_notifications.models import Notification

from ..constants import Relationship, StudentStatus
from ..exceptions import SuspensionEndsBeforeItStarts
from ..models import Guardian, StudentGuardian, StudentStatusLog
from ..services.status import transition
from ..services.suspension_return import (
    due_suspensions,
    return_suspended_students,
)
from .base import StudentsFixture

#: 22:30 on 14 March in Lagos, 00:30 on 15 March in Nairobi.
INSTANT = dt.datetime(2026, 3, 14, 21, 30, tzinfo=dt.timezone.utc)
LAGOS_DAY = dt.date(2026, 3, 14)
NAIROBI_DAY = dt.date(2026, 3, 15)


@contextmanager
def at_instant(instant=INSTANT):
    """Freeze the platform's clock, leaving every school's zone its own.

    Patched at ``vs_config.clock.tenant_now``, the one place every "today" is
    read from, so a branch still reads that same instant in its own zone.
    """
    with mock.patch(
        "vs_config.clock.tenant_now",
        side_effect=lambda tenant: instant.astimezone(tenant_zone(tenant)),
    ):
        yield


class _ReturnFixture(StudentsFixture):
    """Pupils at both schools, and a family on one of them.

    The guardian exists so the "nothing is sent" assertion is worth making: a
    pupil with no reachable guardian would pass it without the rule being
    there at all.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_notifications.services.seed import (
            seed_notification_templates,
            seed_platform_settings,
        )

        seed_notification_templates()
        seed_platform_settings()

        cls.lekki_pupil = cls.pupil("Chiamaka", "Nwosu", "BFS/2026/0201", cls.lekki)
        cls.ikeja_pupil = cls.pupil("Tunde", "Bello", "BFS/2026/0202", cls.ikeja)
        cls.solo_pupil = cls.pupil(
            "Amaka", "Obi", "SRA/2026/0001", cls.solo_branch,
            tenant=cls.solo.tenant,
        )

        mother = Guardian.all_objects.create(
            tenant=cls.tenant, full_name="Mrs. Ngozi Nwosu",
            email="ngozi.nwosu@example.ng", phone="08035550201",
        )
        StudentGuardian.all_objects.create(
            tenant=cls.tenant, student=cls.lekki_pupil, guardian=mother,
            relationship=Relationship.MOTHER, is_primary=True,
        )

    @classmethod
    def pupil(cls, first, last, number, branch, *, tenant=None):
        from vs_config.clock import branch_today

        from ..constants import Gender
        from ..models import Student

        tenant = tenant or cls.tenant
        return Student.all_objects.create(
            tenant=tenant, branch=branch, first_name=first, last_name=last,
            student_number=number, date_of_birth=dt.date(2013, 4, 18),
            gender=Gender.FEMALE, status=StudentStatus.ACTIVE,
            enrolment_date=branch_today(tenant, branch),
        )

    # ── helpers ────────────────────────────────────────────────────────────

    def suspend(self, student, *, start, end=None, actor=None, reason="Fighting."):
        return transition(
            student, StudentStatus.SUSPENDED, actor=actor or self.admin,
            reason=reason, effective_date=start, return_date=end,
        )

    def live_log(self, student, status=StudentStatus.SUSPENDED):
        return (
            StudentStatusLog.all_objects.filter(student=student, to_status=status)
            .order_by("-changed_at", "-id").first()
        )

    def status_of(self, student):
        student.refresh_from_db()
        return student.status


class DueReturnTests(_ReturnFixture):
    """The day the suspension ends, the pupil is back, dated that day."""

    def test_a_suspension_that_has_ended_returns_the_pupil_on_its_end_date(self):
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )

        with at_instant():
            result = return_suspended_students()

        self.assertEqual(result, {"returned": 1, "skipped": 0, "failed": 0})
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.ACTIVE)
        row = self.live_log(self.lekki_pupil, StudentStatus.ACTIVE)
        self.assertEqual(row.from_status, StudentStatus.SUSPENDED)
        self.assertEqual(row.effective_date, dt.date(2026, 3, 12))
        # No human did this, so the history attributes it to nobody and the
        # profile's history tab renders it as "System".
        self.assertIsNone(row.changed_by)

    def test_the_return_is_audited(self):
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )

        with at_instant():
            return_suspended_students()

        event = (
            AuditEvent.objects.filter(
                tenant=self.tenant, entity_type="Student",
                entity_id=str(self.lekki_pupil.pk),
                action_type=AuditActionType.STUDENT_REACTIVATED,
            ).order_by("-id").first()
        )
        self.assertIsNotNone(event)
        self.assertIsNone(event.actor_user)
        self.assertEqual(event.metadata["from"], StudentStatus.SUSPENDED)
        self.assertEqual(event.metadata["to"], StudentStatus.ACTIVE)
        self.assertEqual(event.metadata["effective_date"], "2026-03-12")

    def test_a_return_days_late_is_still_dated_the_day_it_was_due(self):
        """A worker down from Thursday to Sunday may not rewrite the record.

        Tunde was suspended until the 12th. The job next runs on the 14th, and
        his record has to say he came back on the 12th, or his attendance, his
        fees and his file all inherit three days he was never suspended for.
        """
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 2), end=dt.date(2026, 3, 12),
        )

        with at_instant():
            return_suspended_students()

        self.assertEqual(
            self.live_log(self.lekki_pupil, StudentStatus.ACTIVE).effective_date,
            dt.date(2026, 3, 12),
        )

    def test_the_sweep_reads_the_platform_in_one_query(self):
        """One query finds every due row at every school, plus the clock's two.

        The sweep reads every school on the platform, so an N+1 here is an N+1
        in the number of schools. The branch's own day is applied in Python
        afterwards, and the zones are read once per school rather than once per
        row.
        """
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )
        self.suspend(
            self.ikeja_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )
        self.suspend(
            self.solo_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
            actor=self.solo_admin,
        )

        with at_instant():
            # Two schools have due rows, and each reads its own zones once.
            with self.assertNumQueries(5):
                due = due_suspensions()

        self.assertEqual(
            {row.student_id for row in due},
            {self.lekki_pupil.pk, self.ikeja_pupil.pk, self.solo_pupil.pk},
        )

    def test_the_celery_task_reaches_the_service(self):
        from ..tasks import return_ended_suspensions_task

        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )

        with at_instant():
            result = return_ended_suspensions_task()

        self.assertEqual(result["returned"], 1)
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.ACTIVE)


class NotYetDueTests(_ReturnFixture):
    """Nothing is returned early, and nothing without a date is returned at all."""

    def test_a_suspension_whose_day_has_not_come_is_left_alone(self):
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 20),
        )

        with at_instant():
            result = return_suspended_students()

        self.assertEqual(result["returned"], 0)
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.SUSPENDED)

    def test_a_suspension_with_no_end_date_is_never_returned_automatically(self):
        """A school suspending pending an investigation lifts it itself.

        The absence of a date is a decision, not a gap: nothing may guess a
        day the school declined to set.
        """
        self.suspend(self.lekki_pupil, start=dt.date(2026, 3, 2), end=None)

        with at_instant():
            result = return_suspended_students()

        self.assertEqual(result["returned"], 0)
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.SUSPENDED)
        self.assertIsNone(self.live_log(self.lekki_pupil).return_date)

    def test_the_end_date_itself_returns_the_pupil_and_the_day_before_does_not(self):
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=LAGOS_DAY,
        )

        day_before = INSTANT - dt.timedelta(days=1)
        with at_instant(day_before):
            self.assertEqual(return_suspended_students()["returned"], 0)
        with at_instant():
            self.assertEqual(return_suspended_students()["returned"], 1)


class WithdrawnPupilTests(_ReturnFixture):
    """The guard that matters most: nobody is put back on the roll by a sweep."""

    def test_a_pupil_withdrawn_since_the_suspension_is_not_resurrected(self):
        """Chiamaka was suspended until the 12th and withdrew on the 10th.

        Her suspension row still says the 12th. Reading it without asking
        where she is now would put a withdrawn child back on the roll, bill
        her again, and tell her family nothing.
        """
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )
        transition(
            self.lekki_pupil, StudentStatus.WITHDRAWN, actor=self.admin,
            reason="Family relocating.", effective_date=dt.date(2026, 3, 10),
        )

        with at_instant():
            result = return_suspended_students()

        self.assertEqual(result, {"returned": 0, "skipped": 0, "failed": 0})
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.WITHDRAWN)

    def test_a_pupil_whose_suspension_was_already_lifted_is_not_swept_twice(self):
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )
        transition(
            self.lekki_pupil, StudentStatus.ACTIVE, actor=self.admin,
            reason="Suspension served early.", effective_date=dt.date(2026, 3, 11),
        )

        with at_instant():
            self.assertEqual(return_suspended_students()["returned"], 0)

        self.assertEqual(
            self.live_log(self.lekki_pupil, StudentStatus.ACTIVE).effective_date,
            dt.date(2026, 3, 11),
        )

    def test_an_old_suspension_does_not_return_a_pupil_suspended_again(self):
        """Only the live suspension is read, not every one in the history."""
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 1), end=dt.date(2026, 3, 4),
        )
        transition(
            self.lekki_pupil, StudentStatus.ACTIVE, actor=self.admin,
            reason="Suspension served.", effective_date=dt.date(2026, 3, 4),
        )
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 10), end=dt.date(2026, 3, 25),
        )

        with at_instant():
            result = return_suspended_students()

        self.assertEqual(result["returned"], 0)
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.SUSPENDED)


class IdempotenceTests(_ReturnFixture):
    """A second run on the same day changes nothing."""

    def test_running_the_sweep_twice_writes_nothing_the_second_time(self):
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )

        with at_instant():
            self.assertEqual(return_suspended_students()["returned"], 1)
            rows = StudentStatusLog.all_objects.filter(
                student=self.lekki_pupil,
            ).count()
            events = AuditEvent.objects.filter(
                entity_type="Student", entity_id=str(self.lekki_pupil.pk),
            ).count()

            self.assertEqual(
                return_suspended_students(),
                {"returned": 0, "skipped": 0, "failed": 0},
            )

        self.assertEqual(
            StudentStatusLog.all_objects.filter(student=self.lekki_pupil).count(),
            rows,
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                entity_type="Student", entity_id=str(self.lekki_pupil.pk),
            ).count(),
            events,
        )


class SilenceTests(_ReturnFixture):
    """A pupil coming back writes to nobody."""

    def test_the_automatic_return_sends_no_notification_of_any_kind(self):
        """The school said when the pupil was back when it suspended them.

        Asserted across every event, not only the suspension's own, because
        the rule is that the return has no message rather than that one
        template is not used.
        """
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )
        before = Notification.all_objects.count()

        with at_instant():
            return_suspended_students()

        self.assertEqual(Notification.all_objects.count(), before)


class BranchDayTests(_ReturnFixture):
    """Each school and each branch is judged on its own calendar day."""

    def setUp(self):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value="Africa/Nairobi", actor=None, branch=self.ikeja,
        )
        forget_tenant_zone(self.tenant)

    def test_two_branches_a_day_apart_return_their_pupils_on_their_own_days(self):
        """At 21:30 UTC it is the 14th at Lekki and the 15th at Ikeja.

        Both suspensions end on the 15th. Tunde is back at Ikeja, where the
        15th has arrived; Chiamaka is not, because at Lekki it is still the
        14th and she is still suspended for one more day.
        """
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=NAIROBI_DAY,
        )
        self.suspend(
            self.ikeja_pupil, start=dt.date(2026, 3, 9), end=NAIROBI_DAY,
        )

        with at_instant():
            result = return_suspended_students()

        self.assertEqual(result["returned"], 1)
        self.assertEqual(self.status_of(self.ikeja_pupil), StudentStatus.ACTIVE)
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.SUSPENDED)

    def test_a_school_with_one_branch_reads_its_own_day_not_the_servers(self):
        """Sunrise keeps Lagos time and the platform runs in UTC.

        At 23:30 UTC on the 14th it is already the 15th at Sunrise, so a pupil
        due back on the 15th is back. A sweep judging by the server's day would
        leave them suspended through their own morning.
        """
        self.suspend(
            self.solo_pupil, start=dt.date(2026, 3, 9), end=NAIROBI_DAY,
            actor=self.solo_admin,
        )

        late = dt.datetime(2026, 3, 14, 23, 30, tzinfo=dt.timezone.utc)
        with at_instant(late):
            result = return_suspended_students()

        self.assertEqual(result["returned"], 1)
        self.assertEqual(self.status_of(self.solo_pupil), StudentStatus.ACTIVE)

    def test_one_schools_day_does_not_return_anothers_pupil(self):
        self.suspend(
            self.ikeja_pupil, start=dt.date(2026, 3, 9), end=NAIROBI_DAY,
        )
        self.suspend(
            self.solo_pupil, start=dt.date(2026, 3, 9), end=NAIROBI_DAY,
            actor=self.solo_admin,
        )

        with at_instant():
            return_suspended_students()

        self.assertEqual(self.status_of(self.ikeja_pupil), StudentStatus.ACTIVE)
        self.assertEqual(self.status_of(self.solo_pupil), StudentStatus.SUSPENDED)


class OddHistoryTests(_ReturnFixture):
    """A row the state machine cannot act on is left for a person to look at."""

    def test_a_suspension_from_a_status_off_the_roll_is_skipped_and_logged(self):
        """A pupil recorded as suspended from nowhere is not guessed at.

        The transition table only ever produces a suspension from ACTIVE, so a
        row saying otherwise is a data fault. Returning the pupil to whatever
        it says would be the sweep inventing a status change nobody made.
        """
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )
        StudentStatusLog.all_objects.filter(
            student=self.lekki_pupil, to_status=StudentStatus.SUSPENDED,
        ).update(from_status=StudentStatus.GRADUATED)

        with at_instant():
            with self.assertLogs(
                "schools.vs_students.services.suspension_return", level="WARNING",
            ) as logged:
                result = return_suspended_students()

        self.assertEqual(result, {"returned": 0, "skipped": 1, "failed": 0})
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.SUSPENDED)
        self.assertIn(str(self.lekki_pupil.pk), "\n".join(logged.output))

    def test_one_pupils_failure_does_not_cost_another_their_return(self):
        """A bad row at Brightfield may not strand Sunrise's pupil."""
        self.suspend(
            self.lekki_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
        )
        self.suspend(
            self.solo_pupil, start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 12),
            actor=self.solo_admin,
        )
        failing = [True]

        def once(*args, **kwargs):
            if failing:
                failing.pop()
                raise RuntimeError("the audit table is unreachable")
            return real(*args, **kwargs)

        real = transition
        with at_instant():
            with mock.patch(
                "schools.vs_students.services.status.transition", side_effect=once,
            ):
                with self.assertLogs(
                    "schools.vs_students.services.suspension_return", level="ERROR",
                ):
                    result = return_suspended_students()

        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["failed"], 1)


class EndDateValidationTests(_ReturnFixture):
    """A suspension has to end after it starts."""

    def test_an_end_date_before_the_start_is_refused(self):
        with self.assertRaises(SuspensionEndsBeforeItStarts):
            self.suspend(
                self.lekki_pupil,
                start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 8),
            )

        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.ACTIVE)
        self.assertFalse(
            StudentStatusLog.all_objects.filter(
                student=self.lekki_pupil, to_status=StudentStatus.SUSPENDED,
            ).exists()
        )

    def test_an_end_date_on_the_start_day_is_refused(self):
        """The shortest suspension starts on Monday and ends on Tuesday."""
        with self.assertRaises(SuspensionEndsBeforeItStarts):
            self.suspend(
                self.lekki_pupil,
                start=dt.date(2026, 3, 9), end=dt.date(2026, 3, 9),
            )

    def test_a_return_date_is_not_kept_on_a_move_that_is_not_a_suspension(self):
        """A field that means nothing for the move being recorded is dropped."""
        transition(
            self.lekki_pupil, StudentStatus.WITHDRAWN, actor=self.admin,
            reason="Family relocating.", return_date=dt.date(2099, 1, 1),
        )

        self.assertIsNone(
            self.live_log(self.lekki_pupil, StudentStatus.WITHDRAWN).return_date,
        )


class SuspendRouteTests(_ReturnFixture):
    """What the endpoint now accepts, and what the profile now reads back."""

    def test_a_suspension_needs_no_reason_and_a_withdrawal_still_does(self):
        response = self.post(
            self.admin, "student-suspend", {}, pk=self.ikeja_pupil.pk,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.status_of(self.ikeja_pupil), StudentStatus.SUSPENDED)
        self.assertEqual(self.live_log(self.ikeja_pupil).reason, "")

        refused = self.post(
            self.admin, "student-withdraw", {}, pk=self.lekki_pupil.pk,
        )
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(self.status_of(self.lekki_pupil), StudentStatus.ACTIVE)

    def test_the_route_stores_the_end_date_and_refuses_a_bad_one(self):
        response = self.post(
            self.admin, "student-suspend",
            {"effective_date": "2026-03-09", "return_date": "2026-03-12"},
            pk=self.lekki_pupil.pk,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.live_log(self.lekki_pupil).return_date, dt.date(2026, 3, 12),
        )

        refused = self.post(
            self.admin, "student-suspend",
            {"effective_date": "2026-03-09", "return_date": "2026-03-01"},
            pk=self.ikeja_pupil.pk,
        )
        self.assertEqual(refused.status_code, 422, refused.data)
        self.assertEqual(
            refused.data["error"]["code"], "SUSPENSION_ENDS_BEFORE_IT_STARTS",
        )

    def test_the_profile_says_when_the_pupil_is_expected_back(self):
        self.post(
            self.admin, "student-suspend",
            {"reason": "Fighting in the dining hall.", "send_reason": True,
             "effective_date": "2026-03-09", "return_date": "2026-03-12"},
            pk=self.lekki_pupil.pk,
        )

        with at_instant():
            response = self.get(self.admin, "student-detail", pk=self.lekki_pupil.pk)

        suspension = response.data["data"]["suspension"]
        self.assertEqual(suspension["effective_date"], dt.date(2026, 3, 9))
        self.assertEqual(suspension["return_date"], dt.date(2026, 3, 12))
        self.assertEqual(suspension["reason"], "Fighting in the dining hall.")
        # The 12th has passed at Lekki and nothing has swept yet, so the
        # register should already be showing a pupil who is expected in school.
        self.assertTrue(suspension["due_back"])

    def test_a_pupil_who_is_not_suspended_carries_no_suspension_block(self):
        response = self.get(self.admin, "student-detail", pk=self.lekki_pupil.pk)

        self.assertIsNone(response.data["data"]["suspension"])
