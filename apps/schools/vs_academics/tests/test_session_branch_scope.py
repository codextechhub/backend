"""FR-013: one active year per branch, and a branch that may break away.

The rule that replaced uq_academic_session_one_active at FRD v2.6. Two halves
are tested separately on purpose: the database constraints are proven by
writing straight through ``all_objects``, because a service-level test passes
while a race is still open, and the narrowing is proven through the service,
because no constraint can express it.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase

from vs_audit.models import AuditEvent
from vs_rbac.tests.helpers import make_branch, make_school
from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    SessionBranch,
    SessionStatus,
)
from schools.vs_academics.services.sessions import (
    activate_session,
    archive_session,
    covered_branch_ids,
    set_branches,
)

D = dt.date


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

    def session(self, name, start=D(2026, 9, 1), end=D(2027, 7, 31), branches=()):
        s = AcademicSession.all_objects.create(
            tenant=self.tenant, name=name, start_date=start, end_date=end,
        )
        set_branches(s, self.tenant, list(branches))
        return s

    def covered(self, session):
        return covered_branch_ids(session, self.tenant)


class SchoolWideMeansEverywhereTests(_Base):
    def test_a_session_naming_no_branches_covers_every_branch(self):
        s = self.session("2026/2027")
        self.assertTrue(s.is_school_wide)
        self.assertEqual(self.covered(s), {self.lekki.id, self.ikeja.id})

    def test_a_branch_opened_later_is_inside_the_running_year(self):
        """The reason the set is computed rather than materialised.

        Brightfield never splits its calendar, so 2026/2027 names no branches.
        Yaba opens in February and is inside the running year from the day it
        exists, with nobody having to remember to add it.
        """
        s = self.session("2026/2027")
        activate_session(s, self.tenant)
        yaba = make_branch(self.school, name="Yaba Campus", is_main=False)
        self.assertIn(yaba.id, self.covered(s))

    def test_naming_branches_makes_it_not_school_wide(self):
        s = self.session("2026/2027 Lekki", branches=[self.lekki])
        s.refresh_from_db()
        self.assertFalse(s.is_school_wide)
        self.assertEqual(self.covered(s), {self.lekki.id})


class BreakawayTests(_Base):
    def test_a_branch_breaks_away_and_the_others_carry_on(self):
        """The case the product owner chose over refusing the activation."""
        school_wide = self.session("2026/2027")
        activate_session(school_wide, self.tenant)

        lekki_year = self.session("2027 Lekki", branches=[self.lekki])
        displaced = activate_session(lekki_year, self.tenant)

        school_wide.refresh_from_db()
        lekki_year.refresh_from_db()
        self.assertEqual(lekki_year.status, SessionStatus.ACTIVE)
        # Ikeja never notices: its year is still live and still its own.
        self.assertEqual(school_wide.status, SessionStatus.ACTIVE)
        self.assertEqual(self.covered(school_wide), {self.ikeja.id})
        self.assertFalse(school_wide.is_school_wide)
        self.assertEqual([s.pk for s in displaced], [school_wide.pk])

    def test_narrowing_to_nothing_archives_the_incumbent(self):
        """Both branches leave, so the year it covered is over."""
        school_wide = self.session("2026/2027")
        activate_session(school_wide, self.tenant)
        both = self.session("2027 split", branches=[self.lekki, self.ikeja])
        activate_session(both, self.tenant)

        school_wide.refresh_from_db()
        self.assertEqual(school_wide.status, SessionStatus.ARCHIVED)
        self.assertIsNotNone(school_wide.archived_at)

    def test_a_school_wide_activation_reclaims_every_branch(self):
        """The other direction: bringing everyone onto one calendar."""
        lekki_year = self.session("2027 Lekki", branches=[self.lekki])
        ikeja_year = self.session("2027 Ikeja", branches=[self.ikeja])
        activate_session(lekki_year, self.tenant)
        activate_session(ikeja_year, self.tenant)

        together = self.session("2028/2029", start=D(2028, 9, 1), end=D(2029, 7, 31))
        activate_session(together, self.tenant)

        lekki_year.refresh_from_db()
        ikeja_year.refresh_from_db()
        self.assertEqual(lekki_year.status, SessionStatus.ARCHIVED)
        self.assertEqual(ikeja_year.status, SessionStatus.ARCHIVED)

    def test_two_branch_years_coexist(self):
        lekki_year = self.session("2027 Lekki", branches=[self.lekki])
        ikeja_year = self.session("2027 Ikeja", branches=[self.ikeja])
        activate_session(lekki_year, self.tenant)
        displaced = activate_session(ikeja_year, self.tenant)

        lekki_year.refresh_from_db()
        self.assertEqual(lekki_year.status, SessionStatus.ACTIVE)
        self.assertEqual(displaced, [])

    def test_narrowing_writes_an_audit_event_naming_what_moved(self):
        school_wide = self.session("2026/2027")
        activate_session(school_wide, self.tenant)
        activate_session(self.session("2027 Lekki", branches=[self.lekki]), self.tenant)

        event = AuditEvent.objects.filter(
            action_type="ACADEMIC_SESSION_NARROWED",
            entity_id=str(school_wide.pk),
        ).first()
        self.assertIsNotNone(
            event, "a year silently changing shape must leave a trail",
        )
        self.assertEqual(event.metadata["lost"], [self.lekki.id])
        self.assertEqual(event.metadata["kept"], [self.ikeja.id])

    def test_the_link_rows_track_the_session_status(self):
        """The denormalised column is what the constraint reads.

        If it drifts from the session's own status the guard silently stops
        guarding, so this asserts the two never disagree.
        """
        s = self.session("2027 Lekki", branches=[self.lekki])
        activate_session(s, self.tenant)
        self.assertEqual(
            set(SessionBranch.all_objects.filter(session=s)
                .values_list("session_status", flat=True)),
            {SessionStatus.ACTIVE},
        )
        archive_session(s, self.tenant)
        self.assertEqual(
            set(SessionBranch.all_objects.filter(session=s)
                .values_list("session_status", flat=True)),
            {SessionStatus.ARCHIVED},
        )


class ReactivationTests(_Base):
    def term(self, session, name, order, start, end):
        return AcademicTerm.all_objects.create(
            tenant=self.tenant, session=session, name=name, order_index=order,
            start_date=start, end_date=end,
        )

    def test_an_archived_year_comes_back_with_its_terms(self):
        """The case that fails if activation does not un-archive terms.

        Archiving a year archives its terms, and activation refuses a year
        holding an archived term - so without this every archived year would be
        refused, on every school, and the route would be dead on arrival.
        """
        s = self.session("2025/2026", start=D(2025, 9, 1), end=D(2026, 7, 31))
        self.term(s, "First Term", 1, D(2025, 9, 1), D(2025, 12, 11))
        self.term(s, "Second Term", 2, D(2026, 1, 5), D(2026, 4, 1))
        self.term(s, "Third Term", 3, D(2026, 4, 19), D(2026, 7, 16))
        archive_session(s, self.tenant)
        self.assertEqual(
            AcademicTerm.all_objects.filter(
                session=s, archived_at__isnull=False).count(), 3,
        )

        activate_session(s, self.tenant)

        s.refresh_from_db()
        self.assertEqual(s.status, SessionStatus.ACTIVE)
        self.assertIsNone(s.archived_at)
        self.assertEqual(
            AcademicTerm.all_objects.filter(
                session=s, archived_at__isnull=False).count(), 0,
        )

    def test_activating_the_active_session_is_a_no_op(self):
        s = self.session("2026/2027")
        activate_session(s, self.tenant)
        s.refresh_from_db()
        stamped = s.activated_at

        self.assertEqual(activate_session(s, self.tenant), [])
        s.refresh_from_db()
        self.assertEqual(s.activated_at, stamped)

    def test_a_year_with_no_terms_still_activates(self):
        """Whether an empty year may be activated is the calendar module's
        question, not this rule's, and the archived-term refusal must not answer
        it by accident."""
        s = self.session("2026/2027")
        activate_session(s, self.tenant)
        s.refresh_from_db()
        self.assertEqual(s.status, SessionStatus.ACTIVE)
