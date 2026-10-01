"""Telling a family that a pupil has been suspended.

The audience is the school's own setting, so the cases that matter are the
ones where a school's choice and the record it holds disagree: a main contact
with no way of being reached, a record with no main contact at all, a school
that has chosen to say nothing, and a school that has chosen nothing at all.
None of them may reach an adult the school did not choose, and none of them
may stop the suspension.

Brightfield runs Lekki and Ikeja, so the branch is named in its notices.
Sunrise runs one branch, so the branch recedes from its notices. Every test
calls ``transition`` rather than the endpoint: the audience rule, the split
between an account and an address, and the swallowed failure are all service
behaviour, and the endpoint's own contract is covered where the endpoints are.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from vs_notifications.constants import ChannelChoices
from vs_rbac.tests.helpers import make_staff_user

from ..constants import Relationship, StudentStatus, SuspensionNotice
from ..models import Guardian, StudentStatusLog
from ..services.status import transition
from ..services.suspension_notice import EVENT_KEY, read_audience
from .base import StudentsFixture


class _NoticeFixture(StudentsFixture):
    """A pupil per shape of family, and the seeded templates that render them.

    The templates are seeded rather than mocked, because a context key the
    template does not read renders as an empty space rather than raising: the
    only proof the two line up is reading the rendered body back.
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

        # Chiamaka's mother is her main contact and reads Vision, because she
        # also teaches at Lekki: one person, one account.
        cls.chiamaka = cls.pupil(
            first="Chiamaka", last="Nwosu", number="BFS/2025/0101",
        )
        cls.mother_account = make_staff_user(
            cls.lekki, email="ngozi.nwosu@brightfield.test",
            first_name="Ngozi", last_name="Nwosu",
        )
        cls.mother = cls.make_guardian(
            name="Mrs. Ngozi Nwosu", email="ngozi.nwosu@brightfield.test",
            phone="08035550101", user=cls.mother_account,
        )
        cls.father = cls.make_guardian(
            name="Mr. Chukwudi Nwosu", email="chukwudi.nwosu@example.ng",
            phone="08035550102",
        )
        cls.add_link(cls.chiamaka, cls.mother, primary=True,
                     relationship=Relationship.MOTHER)
        cls.add_link(cls.chiamaka, cls.father, primary=False,
                     relationship=Relationship.FATHER)

        # Tunde's main contact is a mother the school holds no address for and
        # who has no account. His uncle has both.
        cls.tunde = cls.pupil(
            first="Tunde", last="Bello", number="BFS/2025/0102",
        )
        cls.unreachable_mother = cls.make_guardian(
            name="Mrs. Aduke Bello", email="", phone="08035550103",
        )
        cls.uncle = cls.make_guardian(
            name="Mr. Segun Bello", email="segun.bello@example.ng",
            phone="08035550104",
        )
        cls.add_link(cls.tunde, cls.unreachable_mother, primary=True,
                     relationship=Relationship.MOTHER)
        cls.add_link(cls.tunde, cls.uncle, primary=False,
                     relationship=Relationship.OTHER)

    # ── fixture helpers ────────────────────────────────────────────────────

    @classmethod
    def pupil(cls, *, first, last, number, branch=None, tenant=None):
        from vs_config.clock import branch_today

        from ..constants import Gender
        from ..models import Student

        tenant = tenant or cls.tenant
        branch = branch or cls.lekki
        return Student.all_objects.create(
            tenant=tenant, branch=branch, first_name=first, last_name=last,
            student_number=number,
            date_of_birth=dt.date(2013, 4, 18), gender=Gender.FEMALE,
            status=StudentStatus.ACTIVE,
            enrolment_date=branch_today(tenant, branch),
        )

    @classmethod
    def make_guardian(cls, *, name, email, phone, user=None, tenant=None):
        return Guardian.all_objects.create(
            tenant=tenant or cls.tenant, full_name=name, email=email,
            phone=phone, user=user,
        )

    @classmethod
    def add_link(cls, student, guardian, *, primary, relationship):
        from ..models import StudentGuardian

        return StudentGuardian.all_objects.create(
            tenant=student.tenant, student=student, guardian=guardian,
            relationship=relationship, is_primary=primary,
        )

    # ── assertions ─────────────────────────────────────────────────────────

    def set_audience(self, value, *, tenant=None):
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        from ..constants import CFG_SUSPENSION_NOTICE

        set_value(
            definition=ConfigurationDefinition.objects.get(key=CFG_SUSPENSION_NOTICE),
            value=value, actor=self.admin, tenant=tenant or self.tenant,
        )

    def notices(self, student=None):
        """Every ``student.suspended`` record, narrowed to one pupil by name.

        The in-app subject names the pupil and its body does not, so both are
        matched: a notice is attributed to a pupil the way a reader attributes
        it, by the name printed on it.
        """
        from django.db.models import Q

        from vs_notifications.models import Notification

        rows = Notification.all_objects.filter(event_type__key=EVENT_KEY)
        if student is not None:
            name = f"{student.first_name} {student.last_name}"
            rows = rows.filter(Q(subject__contains=name) | Q(body__contains=name))
        return rows

    def addressed_to(self, student=None) -> set:
        """Every address and account the notice reached, however it reached it."""
        return {
            row.unregistered_email or row.recipient.email
            for row in self.notices(student)
        }

    def suspend(self, student, *, reason="Repeated absence without notice.",
                send_reason=False, return_date=None):
        return transition(
            student, StudentStatus.SUSPENDED, actor=self.admin, reason=reason,
            send_reason=send_reason, return_date=return_date,
        )


class AudienceTests(_NoticeFixture):
    """Who the school writes to, and who it never writes to instead."""

    def test_a_school_that_has_chosen_nothing_writes_to_the_main_contact(self):
        """The default is the narrowest audience that still tells the family.

        A school that has not opened the screen has not chosen silence, so the
        absence of a stored value may not read as NOBODY.
        """
        self.assertEqual(
            read_audience(self.tenant), SuspensionNotice.PRIMARY_GUARDIAN,
        )

        self.suspend(self.chiamaka)

        self.assertEqual(
            self.addressed_to(self.chiamaka), {self.mother.email},
        )

    def test_the_whole_record_is_written_to_where_the_school_says_so(self):
        self.set_audience(SuspensionNotice.ALL_GUARDIANS)

        self.suspend(self.chiamaka)

        self.assertEqual(
            self.addressed_to(self.chiamaka),
            {self.mother.email, self.father.email},
        )
        # Each adult is told once, not once per link and not once per guardian
        # row that happens to share an address.
        emails = self.notices(self.chiamaka).filter(channel=ChannelChoices.EMAIL)
        self.assertEqual(emails.count(), 2, list(emails.values_list("subject", flat=True)))

    def test_nobody_means_nothing_is_sent(self):
        self.set_audience(SuspensionNotice.NOBODY)

        self.suspend(self.chiamaka)

        self.assertEqual(self.notices(self.chiamaka).count(), 0)
        self.chiamaka.refresh_from_db()
        self.assertEqual(self.chiamaka.status, StudentStatus.SUSPENDED)

    def test_an_unreadable_stored_value_reads_as_the_main_contact(self):
        """A value stored by hand at the platform layer is not trusted.

        A bad value costs a school its own rule, never the family's notice,
        and never widens the audience it was storing.
        """
        from vs_config.models import ConfigurationDefinition, ConfigurationValue

        definition = ConfigurationDefinition.objects.get(
            key="students.suspension.notice",
        )
        ConfigurationValue.all_objects.create(
            definition=definition, tenant=self.tenant,
            scope_key=f"tenant:{self.tenant.pk}", value="EVERYONE",
        )

        self.assertEqual(
            read_audience(self.tenant), SuspensionNotice.PRIMARY_GUARDIAN,
        )


class UnreachableGuardianTests(_NoticeFixture):
    """Nobody is written to in the place of somebody who cannot be reached."""

    def test_a_main_contact_with_no_account_and_no_address_is_not_replaced(self):
        """Tunde's uncle is not told because his mother could not be.

        Telling the wrong household that a child is in trouble is worse than
        telling nobody and letting the school phone the mother as it would
        have anyway.
        """
        with self.assertLogs(
            "schools.vs_students.services.suspension_notice", level="WARNING",
        ) as logged:
            self.suspend(self.tunde)

        self.assertEqual(self.notices(self.tunde).count(), 0)
        self.assertIn(str(self.unreachable_mother.pk), "\n".join(logged.output))
        self.tunde.refresh_from_db()
        self.assertEqual(self.tunde.status, StudentStatus.SUSPENDED)

    def test_a_record_with_no_main_contact_tells_nobody(self):
        kelechi = self.pupil(
            first="Kelechi", last="Eze", number="BFS/2025/0103",
        )
        aunt = self.make_guardian(
            name="Mrs. Oby Eze", email="oby.eze@example.ng", phone="08035550105",
        )
        self.add_link(kelechi, aunt, primary=False, relationship=Relationship.AUNT)

        with self.assertLogs(
            "schools.vs_students.services.suspension_notice", level="WARNING",
        ):
            self.suspend(kelechi)

        self.assertEqual(self.notices(kelechi).count(), 0)

    def test_an_unreachable_guardian_does_not_silence_a_reachable_one(self):
        """Under ALL_GUARDIANS the record is written to as far as it can be."""
        self.set_audience(SuspensionNotice.ALL_GUARDIANS)

        with self.assertLogs(
            "schools.vs_students.services.suspension_notice", level="WARNING",
        ):
            self.suspend(self.tunde)

        self.assertEqual(self.addressed_to(self.tunde), {self.uncle.email})


class ChannelTests(_NoticeFixture):
    """An account reads it in Vision and by email; an address only by email."""

    def test_an_account_gets_both_channels_and_an_address_only_email(self):
        self.set_audience(SuspensionNotice.ALL_GUARDIANS)

        self.suspend(self.chiamaka)

        rows = self.notices(self.chiamaka)
        self.assertEqual(
            sorted(
                (row.channel, row.unregistered_email or row.recipient.email)
                for row in rows
            ),
            [
                (ChannelChoices.EMAIL, self.father.email),
                (ChannelChoices.EMAIL, self.mother.email),
                (ChannelChoices.IN_APP, self.mother.email),
            ],
        )

    def test_the_notice_names_the_pupil_and_withholds_the_reason_by_default(self):
        """A reason nobody weighed is a staff note, not a letter to a parent.

        The reason is written to the history either way. What is asserted here
        is that it reaches a family only where the person suspending the pupil
        chose to send it, and that the quiet answer is the one a caller gets
        without saying anything.
        """
        self.suspend(self.chiamaka, reason="Caught stealing. Third time.")

        email = self.notices(self.chiamaka).get(channel=ChannelChoices.EMAIL)
        self.assertIn("Chiamaka", email.subject)
        self.assertIn("Chiamaka Nwosu", email.body)
        self.assertNotIn("stealing", email.body.lower())
        self.assertNotIn("stealing", email.subject.lower())
        self.assertEqual(
            StudentStatusLog.all_objects.filter(
                student=self.chiamaka, to_status=StudentStatus.SUSPENDED,
            ).first().reason,
            "Caught stealing. Third time.",
        )

    def test_the_reason_reaches_the_family_where_the_suspender_sent_it(self):
        """"Fighting in the dining hall" is the line that saves a phone call."""
        self.suspend(
            self.chiamaka, reason="Fighting in the dining hall on Tuesday.",
            send_reason=True,
        )

        email = self.notices(self.chiamaka).get(channel=ChannelChoices.EMAIL)
        self.assertIn("Fighting in the dining hall on Tuesday.", email.body)
        in_app = self.notices(self.chiamaka).get(channel=ChannelChoices.IN_APP)
        self.assertIn("Fighting in the dining hall on Tuesday.", in_app.body)


class BranchTests(_NoticeFixture):
    """The branch is named where it changes meaning and absent where it does not."""

    def test_a_school_with_several_branches_names_the_branch(self):
        self.suspend(self.chiamaka)

        self.assertIn("Lekki", self.notices(self.chiamaka).get(channel=ChannelChoices.EMAIL).body)

    def test_a_school_with_one_branch_leaves_the_branch_out(self):
        solo_pupil = self.pupil(
            first="Amaka", last="Obi", number="SRA/2025/0001",
            branch=self.solo_branch, tenant=self.solo.tenant,
        )
        parent = self.make_guardian(
            name="Mr. Ikenna Obi", email="ikenna.obi@example.ng",
            phone="08035550106", tenant=self.solo.tenant,
        )
        self.add_link(solo_pupil, parent, primary=True,
                      relationship=Relationship.FATHER)

        transition(
            solo_pupil, StudentStatus.SUSPENDED, actor=self.solo_admin,
            reason="Repeated lateness.",
        )

        body = self.notices(solo_pupil).get(channel=ChannelChoices.EMAIL).body
        self.assertIn("Amaka Obi", body)
        self.assertNotIn("Main", body)
        self.assertNotIn("Branch", body)


class TransitionReachTests(_NoticeFixture):
    """Only a move into suspension writes to a family, and nothing blocks one."""

    def test_lifting_a_suspension_writes_to_nobody(self):
        self.suspend(self.chiamaka)
        before = self.notices(self.chiamaka).count()

        transition(
            self.chiamaka, StudentStatus.ACTIVE, actor=self.admin,
            reason="Suspension served.",
        )

        self.assertEqual(self.notices(self.chiamaka).count(), before)

    def test_a_withdrawal_writes_to_nobody(self):
        transition(
            self.chiamaka, StudentStatus.WITHDRAWN, actor=self.admin,
            reason="Family relocating.",
        )

        self.assertEqual(self.notices().count(), 0)

    def test_a_dispatcher_that_raises_leaves_the_pupil_suspended(self):
        """Suspending a pupil is the act; the message is a consequence of it.

        A school may not be stopped from suspending a pupil because a mail
        queue is down, so the failure is logged and the status change, its log
        row and its audit event all stand.
        """
        with mock.patch(
            "vs_notifications.services.dispatch.NotificationService.send",
            side_effect=RuntimeError("the queue is down"),
        ):
            with self.assertLogs(
                "schools.vs_students.services.suspension_notice", level="ERROR",
            ):
                self.suspend(self.chiamaka)

        self.chiamaka.refresh_from_db()
        self.assertEqual(self.chiamaka.status, StudentStatus.SUSPENDED)
        self.assertTrue(
            StudentStatusLog.all_objects.filter(
                student=self.chiamaka, to_status=StudentStatus.SUSPENDED,
            ).exists()
        )

    def test_one_school_s_choice_does_not_reach_another(self):
        """Sunrise choosing silence leaves Brightfield writing to its families."""
        self.set_audience(SuspensionNotice.NOBODY, tenant=self.solo.tenant)

        self.suspend(self.chiamaka)

        self.assertEqual(self.addressed_to(self.chiamaka), {self.mother.email})
