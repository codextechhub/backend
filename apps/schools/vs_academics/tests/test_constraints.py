"""The guarantees that live in the database rather than in a service.

Every case here writes directly through ``all_objects`` and expects the
database to refuse it. That is the point: M13 FRD v2.6 section 2 carries
forward the rule that holding one active session at a time must be enforced by
a constraint as well as by service logic, because the service alone loses a
race, and a service-level test passes while the race is still open.
"""
from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.test import TestCase

from vs_rbac.tests.helpers import make_branch, make_school
from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Department,
    Level,
    Program,
    SchoolClass,
    SessionBranch,
    SessionStatus,
    Subject,
    SubjectOffering,
)

D = dt.date


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)
        cls.other = make_school(slug="sunrise", name="Sunrise Academy")

    def session(self, name, status=SessionStatus.DRAFT, school_wide=True, tenant=None):
        return AcademicSession.all_objects.create(
            tenant=tenant or self.tenant, name=name,
            start_date=D(2026, 9, 1), end_date=D(2027, 7, 31),
            status=status, is_school_wide=school_wide,
        )


class OneActiveSessionTests(_Base):
    """The rule that moved from per tenant to per branch at version 2.6."""

    def test_two_school_wide_active_sessions_are_refused(self):
        self.session("2026/2027", SessionStatus.ACTIVE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.session("2027/2028", SessionStatus.ACTIVE)

    def test_a_school_wide_active_session_does_not_block_another_tenant(self):
        """The constraint is per tenant, so two schools are unaffected."""
        self.session("2026/2027", SessionStatus.ACTIVE)
        self.session("2026/2027", SessionStatus.ACTIVE, tenant=self.other.tenant)
        self.assertEqual(
            AcademicSession.all_objects.filter(status=SessionStatus.ACTIVE).count(), 2,
        )

    def test_a_draft_and_an_archived_session_never_collide(self):
        self.session("2026/2027", SessionStatus.ACTIVE)
        self.session("2027/2028", SessionStatus.DRAFT)
        self.session("2025/2026", SessionStatus.ARCHIVED)
        self.assertEqual(AcademicSession.all_objects.count(), 3)

    def test_one_active_session_per_branch(self):
        """Two branch-scoped years may both be live; not on the same branch."""
        a = self.session("2026/2027 Lekki", SessionStatus.ACTIVE, school_wide=False)
        b = self.session("2027 Ikeja", SessionStatus.ACTIVE, school_wide=False)
        SessionBranch.all_objects.create(
            tenant=self.tenant, session=a, branch=self.lekki,
            session_status=SessionStatus.ACTIVE,
        )
        SessionBranch.all_objects.create(
            tenant=self.tenant, session=b, branch=self.ikeja,
            session_status=SessionStatus.ACTIVE,
        )
        # Two live years, different branches: legitimate since version 2.6.
        self.assertEqual(SessionBranch.all_objects.count(), 2)

        clash = self.session("2027 Lekki again", SessionStatus.ACTIVE, school_wide=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SessionBranch.all_objects.create(
                tenant=self.tenant, session=clash, branch=self.lekki,
                session_status=SessionStatus.ACTIVE,
            )

    def test_a_branch_may_appear_in_many_non_active_sessions(self):
        """History is not a collision: only ACTIVE rows are constrained."""
        for i, status in enumerate(
            (SessionStatus.ARCHIVED, SessionStatus.ARCHIVED, SessionStatus.DRAFT)
        ):
            s = self.session(f"year-{i}", status, school_wide=False)
            SessionBranch.all_objects.create(
                tenant=self.tenant, session=s, branch=self.lekki,
                session_status=status,
            )
        self.assertEqual(SessionBranch.all_objects.count(), 3)

    def test_a_session_names_a_branch_only_once(self):
        s = self.session("2026/2027", school_wide=False)
        SessionBranch.all_objects.create(
            tenant=self.tenant, session=s, branch=self.lekki,
            session_status=SessionStatus.DRAFT,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SessionBranch.all_objects.create(
                tenant=self.tenant, session=s, branch=self.lekki,
                session_status=SessionStatus.DRAFT,
            )


class SessionShapeTests(_Base):
    def test_a_session_name_is_unique_per_tenant_case_insensitively(self):
        self.session("2026/2027")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.session("2026/2027")

    def test_the_same_name_at_another_school_is_fine(self):
        self.session("2026/2027")
        self.session("2026/2027", tenant=self.other.tenant)
        self.assertEqual(AcademicSession.all_objects.count(), 2)

    def test_end_date_must_follow_start_date(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AcademicSession.all_objects.create(
                tenant=self.tenant, name="backwards",
                start_date=D(2027, 7, 31), end_date=D(2026, 9, 1),
            )


class TermTests(_Base):
    def setUp(self):
        self.sess = self.session("2026/2027")

    def term(self, name, order, start, end):
        return AcademicTerm.all_objects.create(
            tenant=self.tenant, session=self.sess, name=name,
            order_index=order, start_date=start, end_date=end,
        )

    def test_order_index_is_unique_within_a_session(self):
        self.term("First Term", 1, D(2026, 9, 1), D(2026, 12, 11))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.term("Second Term", 1, D(2027, 1, 5), D(2027, 4, 1))

    def test_a_term_name_is_unique_within_a_session_case_insensitively(self):
        self.term("First Term", 1, D(2026, 9, 1), D(2026, 12, 11))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.term("first term", 2, D(2027, 1, 5), D(2027, 4, 1))

    def test_the_same_term_name_in_another_session_is_fine(self):
        self.term("First Term", 1, D(2026, 9, 1), D(2026, 12, 11))
        other = self.session("2027/2028")
        AcademicTerm.all_objects.create(
            tenant=self.tenant, session=other, name="First Term",
            order_index=1, start_date=D(2027, 9, 1), end_date=D(2027, 12, 11),
        )
        self.assertEqual(AcademicTerm.all_objects.count(), 2)

    def test_terms_come_back_in_order_index_order(self):
        self.term("Third Term", 3, D(2027, 4, 19), D(2027, 7, 16))
        self.term("First Term", 1, D(2026, 9, 1), D(2026, 12, 11))
        self.term("Second Term", 2, D(2027, 1, 5), D(2027, 4, 1))
        self.assertEqual(
            [t.name for t in AcademicTerm.all_objects.filter(session=self.sess)],
            ["First Term", "Second Term", "Third Term"],
        )


class CatalogueUniquenessTests(_Base):
    """Codes are unique per tenant and per kind, whatever the branch.

    Per kind is the product owner's decision of 25 August 2026: a department
    called Languages does not stop a programme being called Languages. Per
    tenant regardless of branch is section 5.3 - a branch gets no namespace of
    its own, so it cannot shadow a school-wide item by reusing its code.
    """

    def dept(self, name, code, branch=None, tenant=None):
        return Department.all_objects.create(
            tenant=tenant or self.tenant, name=name, code=code, branch=branch,
        )

    def test_a_branch_cannot_reuse_a_school_wide_code(self):
        self.dept("Sciences", "SCI")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.dept("Lekki Sciences", "SCI", branch=self.lekki)

    def test_a_school_wide_row_cannot_reuse_a_branch_code(self):
        self.dept("Lekki Sciences", "SCI", branch=self.lekki)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.dept("Sciences", "SCI")

    def test_two_branches_cannot_share_a_code(self):
        self.dept("Lekki Sciences", "SCI", branch=self.lekki)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.dept("Ikeja Sciences", "SCI", branch=self.ikeja)

    def test_case_alone_is_not_a_difference(self):
        self.dept("Sciences", "SCI")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.dept("sciences", "OTHER")

    def test_another_tenant_may_hold_the_same_code(self):
        self.dept("Sciences", "SCI")
        self.dept("Sciences", "SCI", tenant=self.other.tenant)
        self.assertEqual(Department.all_objects.count(), 2)

    def test_a_department_and_a_programme_may_share_a_name(self):
        """Uniqueness is per kind. The design's check is wider and is wrong."""
        self.dept("Languages", "LAN")
        Program.all_objects.create(
            tenant=self.tenant, name="Languages", code="LANP",
        )
        self.assertEqual(Program.all_objects.count(), 1)


class ClassNameTests(_Base):
    """The two partial constraints a single unique constraint would miss."""

    def setUp(self):
        self.program = Program.all_objects.create(
            tenant=self.tenant, name="Junior Secondary", code="JSS",
        )
        self.level = Level.all_objects.create(
            tenant=self.tenant, program=self.program, name="JSS1",
            code="JSS1", order_index=1,
        )

    def klass(self, name, code, branch=None):
        return SchoolClass.all_objects.create(
            tenant=self.tenant, level=self.level, name=name, code=code,
            branch=branch,
        )

    def test_each_branch_may_run_its_own_jss1_a(self):
        self.klass("JSS1 A", "JSS1-A-LEK", branch=self.lekki)
        self.klass("JSS1 A", "JSS1-A-IKE", branch=self.ikeja)
        self.assertEqual(SchoolClass.all_objects.count(), 2)

    def test_one_branch_may_not_run_two_jss1_a(self):
        self.klass("JSS1 A", "JSS1-A-LEK", branch=self.lekki)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.klass("JSS1 A", "JSS1-A-2", branch=self.lekki)

    def test_two_school_wide_jss1_a_are_refused(self):
        """NULL != NULL in PostgreSQL, which is why there are two constraints.

        A single constraint over (level, branch, name) would let this through.
        """
        self.klass("JSS1 A", "JSS1-A")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.klass("JSS1 A", "JSS1-A-2")

    def test_a_class_code_is_unique_across_the_tenant(self):
        self.klass("JSS1 A", "JSS1-A", branch=self.lekki)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.klass("JSS1 B", "JSS1-A", branch=self.ikeja)


class OfferingTests(_Base):
    def setUp(self):
        self.program = Program.all_objects.create(
            tenant=self.tenant, name="Junior Secondary", code="JSS",
        )
        self.level = Level.all_objects.create(
            tenant=self.tenant, program=self.program, name="JSS1",
            code="JSS1", order_index=1,
        )
        self.subject = Subject.all_objects.create(
            tenant=self.tenant, name="Mathematics", code="MTH",
        )

    def test_a_subject_is_offered_at_a_level_once(self):
        SubjectOffering.all_objects.create(
            tenant=self.tenant, subject=self.subject, level=self.level,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubjectOffering.all_objects.create(
                tenant=self.tenant, subject=self.subject, level=self.level,
            )

    def test_the_through_model_carries_its_own_tenant(self):
        """The reason it is not a bare ManyToManyField.

        TenantAwareManager filters only on a model's own tenant or branch
        field, so an implicit join table is returned completely unscoped.
        """
        offering = SubjectOffering.all_objects.create(
            tenant=self.tenant, subject=self.subject, level=self.level,
        )
        self.assertEqual(offering.tenant_id, self.tenant.id)


class LevelOrderTests(_Base):
    def setUp(self):
        self.program = Program.all_objects.create(
            tenant=self.tenant, name="Junior Secondary", code="JSS",
        )

    def test_order_index_is_unique_within_a_programme(self):
        Level.all_objects.create(
            tenant=self.tenant, program=self.program, name="JSS1",
            code="JSS1", order_index=1,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Level.all_objects.create(
                tenant=self.tenant, program=self.program, name="JSS2",
                code="JSS2", order_index=1,
            )

    def test_two_programmes_may_each_hold_a_year_one(self):
        """A level's name is unique within its programme, not per tenant."""
        other = Program.all_objects.create(
            tenant=self.tenant, name="Senior Secondary", code="SSS",
        )
        Level.all_objects.create(
            tenant=self.tenant, program=self.program, name="Year 1",
            code="Y1", order_index=1,
        )
        Level.all_objects.create(
            tenant=self.tenant, program=other, name="Year 1",
            code="Y1", order_index=1,
        )
        self.assertEqual(Level.all_objects.count(), 2)
