"""Who reads the reason a pupil's standing changed.

The reason is free text a member of staff writes about a child, so it is a
registered field of ``school.students`` and sensitive: a role reads it only
where the school has turned the Read switch on. It reaches a client in two
places, and both are held to the same switch - the ``suspension`` block of a
profile, where the key is left out rather than sent empty, and the rows of the
status history.

Everything else about the suspension answers either way. When a pupil is
expected back is the fact a register needs, and withholding it would stop a
class teacher knowing whether to mark a child absent.

What the pupil's family is told is a separate decision, taken once per
suspension by the person suspending the pupil (``send_reason``). The two do not
constrain each other in either direction, which is the pair of cases
:class:`ReasonOnScreenAndInTheNoticeTests` pins.

Both shapes of school: Brightfield runs two branches and Sunrise runs one. A
switch is held by a role, and a role belongs to one school, so Sunrise reads
nothing from a switch Brightfield turned on.
"""
from __future__ import annotations

import datetime as dt

from vs_config.clock import branch_today
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from ..constants import Gender, StudentStatus
from ..field_access import STATUS_REASON_FIELD
from ..models import Student, StudentStatusLog
from .base import StudentsFixture

#: What each school's member of staff wrote, different per school so a reader
#: cannot pass a test by reading the other school's record.
BRIGHTFIELD_REASON = "Fighting in the dining hall on Tuesday."
SUNRISE_REASON = "Repeated absence without notice."


class _ReasonFixture(StudentsFixture):
    """A reader with the switch off and one with it on, at each school.

    The readers hold ``school.students.view`` and nothing else, because the
    question is what a switch does to somebody who may open the record at all.
    The one with the switch off holds no row for the field, which is what a
    role looks like the day the field is declared: sensitive means the default
    does not apply, so no row is Read off.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students")

        cls.teacher = cls.reader(
            cls.school, "Class teacher", "teacher", "teacher@brightfield.test",
            switch_on=False,
        )
        cls.counsellor = cls.reader(
            cls.school, "Counsellor", "counsellor", "counsellor@brightfield.test",
            switch_on=True,
        )
        cls.solo_teacher = cls.reader(
            cls.solo, "Class teacher", "teacher", "teacher@sunrise.test",
            switch_on=False,
        )
        cls.solo_counsellor = cls.reader(
            cls.solo, "Counsellor", "counsellor", "counsellor@sunrise.test",
            switch_on=True,
        )

        cls.pupil = cls.suspended(
            cls.tenant, cls.lekki, "Chiamaka", "Nwosu", BRIGHTFIELD_REASON,
        )
        cls.solo_pupil = cls.suspended(
            cls.solo.tenant, cls.solo_branch, "Amaka", "Obi", SUNRISE_REASON,
        )

        #: ``(school, reader who may not read it, reader who may, pupil, words)``
        cls.cases = (
            ("Brightfield", cls.teacher, cls.counsellor, cls.pupil,
             BRIGHTFIELD_REASON),
            ("Sunrise", cls.solo_teacher, cls.solo_counsellor, cls.solo_pupil,
             SUNRISE_REASON),
        )

    @classmethod
    def reader(cls, school, name, key, email, *, switch_on):
        role = make_role(school, name=name, key=f"{key}_{school.slug}")
        make_role_permission(role, cls.permissions["school.students.view"])
        if switch_on:
            set_field_access(role, STATUS_REASON_FIELD, read=True, write=False)
        user = make_school_admin(None, email=email, tenant=school.tenant)
        make_assignment(school, user, role, branch=None)
        return user

    @classmethod
    def suspended(cls, tenant, branch, first, last, reason, *, return_date=None):
        """A pupil serving a suspension, with the history row behind it.

        Written directly rather than through ``transition``, because these
        tests are about reading the record and the service's own behaviour is
        covered where the service is.
        """
        pupil = Student.all_objects.create(
            tenant=tenant, branch=branch, first_name=first, last_name=last,
            date_of_birth=dt.date(2013, 4, 18), gender=Gender.FEMALE,
            status=StudentStatus.SUSPENDED,
            enrolment_date=branch_today(tenant, branch),
        )
        StudentStatusLog.all_objects.create(
            tenant=tenant, student=pupil,
            from_status=StudentStatus.ACTIVE, to_status=StudentStatus.SUSPENDED,
            reason=reason, effective_date=dt.date(2026, 3, 9),
            return_date=return_date or dt.date(2026, 3, 12),
        )
        return pupil

    # ── reads ──────────────────────────────────────────────────────────────

    def profile(self, reader, pupil):
        response = self.get(reader, "student-detail", pk=pupil.pk)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def history(self, reader, pupil):
        response = self.get(reader, "student-status-history", pk=pupil.pk)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        return rows["results"] if isinstance(rows, dict) else rows


class StatusReasonSwitchTests(_ReasonFixture):
    """The switch decides the reason, and decides nothing else."""

    def test_a_reader_without_the_switch_gets_no_reason_in_either_place(self):
        for school, closed, _, pupil, words in self.cases:
            with self.subTest(school=school):
                suspension = self.profile(closed, pupil)["suspension"]
                self.assertNotIn("reason", suspension)

                rows = self.history(closed, pupil)
                self.assertEqual(len(rows), 1)
                self.assertNotIn("reason", rows[0])
                self.assertNotIn(words, str(rows[0]))

    def test_a_reader_with_the_switch_gets_the_reason_in_both_places(self):
        for school, _, open_to, pupil, words in self.cases:
            with self.subTest(school=school):
                self.assertEqual(
                    self.profile(open_to, pupil)["suspension"]["reason"], words,
                )
                self.assertEqual(self.history(open_to, pupil)[0]["reason"], words)

    def test_the_rest_of_the_suspension_answers_without_the_reason(self):
        """A register still has to know whether to expect the child.

        Withholding the words may not cost a class teacher the dates, or the
        pupil is marked absent on the day the school told them to come back.
        """
        for school, closed, open_to, pupil, _ in self.cases:
            with self.subTest(school=school):
                withheld = self.profile(closed, pupil)["suspension"]
                full = self.profile(open_to, pupil)["suspension"]
                self.assertEqual(withheld["effective_date"], dt.date(2026, 3, 9))
                self.assertEqual(withheld["return_date"], dt.date(2026, 3, 12))
                self.assertEqual(withheld["due_back"], full["due_back"])
                self.assertEqual(
                    set(full) - set(withheld), {"reason"},
                )

    def test_the_history_keeps_every_other_column_for_a_closed_reader(self):
        row = self.history(self.teacher, self.pupil)[0]
        self.assertEqual(row["to_status"], StudentStatus.SUSPENDED)
        # A history row is a serializer's render, so its dates are strings,
        # where the profile's hand-built block carries date objects.
        self.assertEqual(str(row["effective_date"]), "2026-03-09")
        self.assertEqual(str(row["return_date"]), "2026-03-12")
        self.assertIn("to_label", row)

    def test_one_schools_switch_does_not_open_another_schools_record(self):
        """A switch belongs to a role, and a role belongs to one school.

        Brightfield's counsellor role has the reason open. Sunrise's own class
        teacher must still read nothing, or a school would be inheriting a
        decision another school's administrator made.
        """
        self.assertEqual(
            self.profile(self.counsellor, self.pupil)["suspension"]["reason"],
            BRIGHTFIELD_REASON,
        )
        self.assertNotIn(
            "reason", self.profile(self.solo_teacher, self.solo_pupil)["suspension"],
        )


class ReasonOnScreenAndInTheNoticeTests(_ReasonFixture):
    """``send_reason`` and the Read switch answer different questions.

    One is what this pupil's family is told about this suspension; the other is
    which of the school's roles read the record afterwards. Neither may move
    the other, so both directions are pinned here.
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

        cls.mother = cls.guardian_for(
            cls.tenant, "Mrs. Ngozi Nwosu", "ngozi.nwosu@example.ng",
        )

    @classmethod
    def guardian_for(cls, tenant, name, email):
        from ..constants import Relationship
        from ..models import Guardian, StudentGuardian

        guardian = Guardian.all_objects.create(
            tenant=tenant, full_name=name, email=email, phone="08035550101",
        )
        StudentGuardian.all_objects.create(
            tenant=tenant, student=cls.pupil, guardian=guardian,
            relationship=Relationship.MOTHER, is_primary=True,
        )
        return guardian

    def suspend(self, pupil, *, reason, send_reason):
        from ..services.status import transition

        pupil.status = StudentStatus.ACTIVE
        pupil.save(update_fields=["status"])
        return transition(
            pupil, StudentStatus.SUSPENDED, actor=self.admin, reason=reason,
            send_reason=send_reason,
        )

    def notice_bodies(self):
        from vs_notifications.models import Notification

        from ..services.suspension_notice import EVENT_KEY

        return " ".join(
            Notification.all_objects.filter(event_type__key=EVENT_KEY)
            .values_list("body", flat=True)
        )

    def test_a_family_may_be_told_a_reason_a_teacher_may_not_read(self):
        """Brightfield suspends Chiamaka and tells her mother exactly why.

        Her class teacher's role does not have the field open, so the words
        reach the mother and not the screen the teacher opens.
        """
        words = "Fighting in the dining hall on Tuesday."

        self.suspend(self.pupil, reason=words, send_reason=True)

        self.assertIn(words, self.notice_bodies())
        self.assertNotIn(
            "reason", self.profile(self.teacher, self.pupil)["suspension"],
        )

    def test_a_reason_withheld_from_the_family_still_reaches_an_open_role(self):
        """The reverse: the school keeps the words off the family's notice.

        The counsellor's role has the field open, so the record still carries
        them for the people who have to act on it.
        """
        words = "Under investigation after a complaint from another parent."

        self.suspend(self.pupil, reason=words, send_reason=False)

        self.assertNotIn(words, self.notice_bodies())
        self.assertEqual(
            self.profile(self.counsellor, self.pupil)["suspension"]["reason"], words,
        )
