"""The backfill that gives a record to people who had an account and none.

Migration 0005 runs once, on data this repository will never see again, which is
why its rules are asserted here rather than checked by eye against one database.
It is exercised by calling the migration's own function with the live app
registry: every model it touches has the same columns before and after 0005, so
the historical registry would answer identically, and driving the migration
graph forward and back costs minutes per test for no extra evidence.

The two groups it exists for are a school's own administrators, who were
provisioned with an account and a grant and no record, and everybody a school
invited before this module existed, when the directory listed accounts.
"""
from __future__ import annotations

import datetime as dt
from importlib import import_module

from django.apps import apps as live_apps
from django.test import TestCase
from django.utils import timezone

from schools.vs_schools.models import ContactInfo, InviteStatus, SchoolPrimaryAdmin
from schools.vs_staff.models import StaffEmploymentEvent, StaffProfile
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_role,
    make_school,
    make_school_admin,
)

backfill = import_module(
    "schools.vs_staff.migrations.0005_staff_records_for_accounts_that_predate_them",
)


class BackfillTests(TestCase):
    """One school, and the several kinds of account a school's tenant holds."""

    def setUp(self):
        self.school = make_school(slug="st-monicas-backfill", name="St Monica's")
        self.tenant = self.school.tenant
        self.ikeja = make_branch(self.school, name="Ikeja")
        self.role = make_role(self.school, name="School Admin", key="school_admin")

    def _granted(self, email, branch=None, **kwargs):
        """An account this school has given a role to."""
        user = make_school_admin(branch, email=email, tenant=self.tenant, **kwargs)
        make_assignment(self.school, user, self.role, branch=None)
        return user

    def _run(self):
        backfill.write_missing_records(live_apps, None)

    # ── who gets one ───────────────────────────────────────────────────────

    def test_an_administrator_with_no_record_gets_one(self):
        user = self._granted("grace@monicas.test")

        self._run()

        self.assertTrue(StaffProfile.all_objects.filter(user=user).exists())

    def test_the_posting_is_the_account_s_own_branch(self):
        """A branch administrator is based at their branch; a registrar is not.

        Read off the account rather than guessed from the grant, because
        ``User.branch`` is what provisioning already wrote and a null there
        means across the whole school rather than missing.
        """
        based = self._granted("tunde@monicas.test", self.ikeja)
        school_wide = self._granted("grace@monicas.test")

        self._run()

        self.assertEqual(
            StaffProfile.all_objects.get(user=based).branch_id, self.ikeja.pk,
        )
        self.assertIsNone(StaffProfile.all_objects.get(user=school_wide).branch_id)

    def test_the_title_the_school_typed_is_carried_over(self):
        user = self._granted("grace@monicas.test")
        contact = ContactInfo.objects.create(
            full_name="Grace Okonkwo", email="grace@monicas.test",
        )
        SchoolPrimaryAdmin.objects.create(
            school=self.school, contact=contact, school_role="IT Head",
            invite_status=InviteStatus.SENT,
        )

        self._run()

        self.assertEqual(StaffProfile.all_objects.get(user=user).job_title, "IT Head")

    # ── and what their record says ─────────────────────────────────────────

    def test_somebody_signing_in_reads_active(self):
        user = self._granted("grace@monicas.test", status="ACTIVE")

        self._run()

        self.assertEqual(
            StaffProfile.all_objects.get(user=user).employment_status, "ACTIVE",
        )

    def test_somebody_who_never_accepted_reads_invited(self):
        user = self._granted("pending@monicas.test", status="PENDING")

        self._run()

        self.assertEqual(
            StaffProfile.all_objects.get(user=user).employment_status, "INVITED",
        )

    def test_the_history_starts_on_the_day_the_account_was_made(self):
        """Not the day the backfill runs, which tells a school nothing.

        The record and its first event both carry it, so the directory's
        newest-first order puts these people where they joined rather than all
        together at the top.
        """
        user = self._granted("grace@monicas.test", status="ACTIVE")
        joined = timezone.now() - dt.timedelta(days=400)
        type(user).objects.filter(pk=user.pk).update(created_at=joined)
        user.refresh_from_db()

        self._run()

        profile = StaffProfile.all_objects.get(user=user)
        event = StaffEmploymentEvent.all_objects.get(staff=profile)
        self.assertEqual(profile.created_at, user.created_at)
        self.assertEqual(event.from_status, "")
        self.assertEqual(event.to_status, "ACTIVE")
        self.assertEqual(event.effective_date, timezone.localtime(joined).date())

    # ── and who does not ───────────────────────────────────────────────────

    def test_an_account_holding_no_role_is_left_alone(self):
        """A grant is the school's own statement that somebody does a job here.

        Without that test the backfill would be "every account this tenant
        owns", which is the shape the directory narrowed away from.
        """
        stranger = make_school_admin(
            None, email="nobody@monicas.test", tenant=self.tenant,
        )

        self._run()

        self.assertFalse(StaffProfile.all_objects.filter(user=stranger).exists())

    def test_a_parked_or_refused_account_is_not_a_member_of_staff(self):
        drafted = self._granted("draft@monicas.test", status="DRAFT")
        refused = self._granted("refused@monicas.test", status="REJECTED")

        self._run()

        self.assertFalse(
            StaffProfile.all_objects.filter(user__in=[drafted, refused]).exists(),
        )

    def test_an_existing_record_is_not_touched_or_duplicated(self):
        """Including its status: a resignation must survive this running."""
        user = self._granted("gone@monicas.test", status="ACTIVE")
        StaffProfile.all_objects.create(
            tenant=self.tenant, user=user, employment_status="RESIGNED",
            job_title="Bursar",
        )

        self._run()

        rows = StaffProfile.all_objects.filter(user=user)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().employment_status, "RESIGNED")
        self.assertEqual(rows.get().job_title, "Bursar")

    def test_another_school_s_people_are_reached_too_and_kept_apart(self):
        """The backfill is platform-wide, and every record names its own owner.

        A tenant column copied from the wrong side here would be invisible until
        one school opened its staff list and read another school's people.
        """
        ours = self._granted("grace@monicas.test")
        other = make_school(slug="green-field-backfill", name="Green Field")
        other_role = make_role(other, name="School Admin", key="school_admin")
        theirs = make_school_admin(
            None, email="admin@green-field.test", tenant=other.tenant,
        )
        make_assignment(other, theirs, other_role, branch=None)

        self._run()

        self.assertEqual(
            StaffProfile.all_objects.get(user=ours).tenant_id, self.tenant.pk,
        )
        self.assertEqual(
            StaffProfile.all_objects.get(user=theirs).tenant_id, other.tenant.pk,
        )

    def test_running_it_twice_writes_nothing_the_second_time(self):
        self._granted("grace@monicas.test")

        self._run()
        before = StaffProfile.all_objects.count()
        self._run()

        self.assertEqual(StaffProfile.all_objects.count(), before)
