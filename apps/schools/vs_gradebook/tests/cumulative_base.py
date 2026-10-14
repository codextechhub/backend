"""Brightfield with a three-term school year, driven term by term to published.

Extends :class:`~.amend_base.AmendmentFixture` (so a test can also correct a
published result) with a third term of 2025/2026, making the third term the
cumulative one, and with :meth:`CumulativeFixture.publish_term`, which brings
JSS1 A to published for any term through the real services.

**The marks** are three terms of Mathematics (everybody) and French (Chiamaka
and Tunde), each mark a whole percentage because a subject is 100 marks:

==========  =====  =====  =====  ==========
Mathematics  T1     T2     T3    annual
==========  =====  =====  =====  ==========
Tunde        75     50     70     65.00
Chiamaka     79    100     91     90.00
Kelechi      40     75     50     55.00
Amina        60     60     60     60.00
==========  =====  =====  =====  ==========

French is Chiamaka 50, 90, 80 (73.33) and Tunde 75, 30, 60 (55.00). The overall
averages are Tunde 75, 40, 65; Chiamaka 64.50, 95, 85.50; Kelechi 40, 75, 50;
Amina 60 in each. The annual overall averages are Chiamaka 81.67, Tunde 60.00,
Amina 60.00 and Kelechi 55.00, so the annual positions are 1st, 2nd, 2nd and 4th.

Each term's roll is JSS1 A's on the school clock's day (Tunde, Kelechi, Chiamaka
and Amina), the elective takers of French are set for every term, and the
attendance summary and the notification layer are mocked as in the card tests.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from core.test_utils import TenantAPIClient
from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    ElectiveTaker,
    Level,
    SchoolClass,
)
from schools.vs_students.constants import EnrolmentOutcome
from schools.vs_students.models import ClassEnrolment

from ..constants import SheetStatus
from ..models import ClassTerm, StudentTermResult
from .amend_base import AmendmentFixture
from .base import school_clock
from .card_base import SUMMARY, counts_for


class CumulativeFixture(AmendmentFixture):
    #: Whether 2025/2026 has a third term. Without one the second term is the last.
    three_terms = True

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        terms = [cls.second_term]
        if cls.three_terms:
            cls.third_term = AcademicTerm.all_objects.create(
                tenant=cls.tenant, session=cls.year, name="Third Term", order_index=3,
                start_date=dt.date(2026, 4, 20), end_date=dt.date(2026, 7, 17),
            )
            terms.append(cls.third_term)
        for term in terms:
            for pupil in (cls.chiamaka, cls.tunde):
                ElectiveTaker.objects.create(
                    tenant=cls.tenant, school_class=cls.jss1a, term=term,
                    subject=cls.french, student=pupil,
                )
        cls.next_year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2026/2027",
            start_date=dt.date(2026, 9, 7), end_date=dt.date(2027, 7, 16), status="DRAFT",
        )
        cls.next_jss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.next_year, program=cls.junior,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.next_jss2 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.next_year, program=cls.junior,
            name="JSS2", code="JSS2", order_index=2,
        )
        cls.next_jss1a = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.next_year, level=cls.next_jss1,
            name="JSS1 A", code="JSS1A", branch=cls.lekki,
        )
        cls.next_jss2a = SchoolClass.all_objects.create(
            tenant=cls.tenant, session=cls.next_year, level=cls.next_jss2,
            name="JSS2 A", code="JSS2A", branch=cls.lekki,
        )

    @property
    def last_term(self):
        return self.third_term if self.three_terms else self.second_term

    @classmethod
    def record_outcome(cls, student, outcome, school_class=None):
        """What Student Management writes when the year's run decides for *student*.

        The placement in 2025/2026 ends with *outcome*; a pupil promoted or
        repeating is placed in *school_class* of 2026/2027, as the run places
        them, and a graduate is placed nowhere.
        """
        ClassEnrolment.all_objects.filter(
            student=student, session=cls.year, is_active=True,
        ).update(outcome=outcome, is_active=False, ended_at=timezone.now())
        if school_class is not None:
            ClassEnrolment.all_objects.create(
                tenant=cls.tenant, student=student, school_class=school_class,
                session=school_class.session, is_active=True,
                effective_date=dt.date(2026, 9, 7), outcome=EnrolmentOutcome.CURRENT,
            )

    # ── the three terms' marks ────────────────────────────────────────────

    @classmethod
    def term_marks(cls, number):
        """``(maths, french)`` rows of term *number* (1, 2 or 3)."""
        return {
            1: (cls.default_maths(), cls.default_french()),
            2: (
                {
                    cls.tunde: (10, 10, 30), cls.chiamaka: (20, 20, 60),
                    cls.kelechi: (15, 15, 45), cls.amina: (20, 20, 20),
                },
                {cls.chiamaka: (20, 20, 50), cls.tunde: (5, 5, 20)},
            ),
            3: (
                {
                    cls.tunde: (15, 15, 40), cls.chiamaka: (18, 18, 55),
                    cls.kelechi: (10, 10, 30), cls.amina: (15, 15, 30),
                },
                {cls.chiamaka: (20, 20, 40), cls.tunde: (10, 10, 40)},
            ),
        }[number]

    @classmethod
    def publish_term(cls, term, number, *, school_class=None, maths=None, french=None):
        """The class's sheets in, comments written, submitted and published for *term*.

        *number* picks the marks of :meth:`term_marks`; *maths* and *french*
        replace them. Returns the class term.
        """
        school_class = school_class or cls.jss1a
        default_maths, default_french = cls.term_marks(number)
        cls.make_sheet(
            school_class, cls.maths, status=SheetStatus.SUBMITTED, term=term,
            rows=maths or default_maths,
        )
        cls.make_sheet(
            school_class, cls.french, status=SheetStatus.SUBMITTED, term=term,
            rows=french or default_french,
        )
        cls.comment_all(term=term)
        everyone = (cls.chiamaka, cls.tunde, cls.kelechi, cls.amina)
        query = f"?term={term.pk}"
        with school_clock(), mock.patch(SUMMARY, return_value=counts_for(*everyone)):
            submitted = TenantAPIClient(cls.eze).post(
                reverse("gradebook-class-submit", kwargs={"class_id": school_class.pk}) + query,
                {}, format="json",
            )
            assert submitted.status_code == 200, submitted.data
            response = TenantAPIClient(cls.principal).post(
                reverse("gradebook-publish") + query, {"classes": [school_class.pk]},
                format="json",
            )
        assert response.status_code == 200, response.data
        return ClassTerm.objects.get(school_class=school_class, term=term)

    @classmethod
    def publish_year(cls):
        """Every term of the year published, in order."""
        terms = (cls.first_term, cls.second_term) + ((cls.third_term,) if cls.three_terms else ())
        for number, term in enumerate(terms, start=1):
            cls.publish_term(term, number)

    @classmethod
    def publish_sunrise_term(cls, term, marks):
        """Sunrise's Basic 3 published for *term* with the two pupils' *marks* ``(first, second)``."""
        cls.make_sheet(
            cls.solo_class, cls.solo_maths, status=SheetStatus.SUBMITTED, term=term,
            rows={cls.solo_pupil: marks[0], cls.solo_pupil_two: marks[1]},
        )
        client = TenantAPIClient(cls.solo_teacher)
        query = f"?term={term.pk}"
        with school_clock(), mock.patch(
            SUMMARY, return_value=counts_for(cls.solo_pupil, cls.solo_pupil_two),
        ):
            for pupil in (cls.solo_pupil, cls.solo_pupil_two):
                response = client.put(
                    reverse(
                        "gradebook-compile",
                        kwargs={"class_id": cls.solo_class.pk, "student_id": pupil.pk},
                    ) + query, {"teacher_comment": "Good."}, format="json",
                )
                assert response.status_code == 200, response.data
            response = client.post(
                reverse("gradebook-class-submit", kwargs={"class_id": cls.solo_class.pk}) + query,
                {}, format="json",
            )
            assert response.status_code == 200, response.data
            response = TenantAPIClient(cls.solo_principal).post(
                reverse("gradebook-publish") + query, {"classes": [cls.solo_class.pk]},
                format="json",
            )
        assert response.status_code == 200, response.data

    # ── reading ───────────────────────────────────────────────────────────

    def last_card(self, student, user=None):
        """The report card of the year's last term."""
        response = self.report_card(user or self.releaser, student, term=self.last_term)
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)

    def stored(self, student, term=None):
        """The stored snapshot of *student*'s result in JSS1 A for *term* (the last by default)."""
        return StudentTermResult.objects.get(
            student=student, class_term__school_class=self.jss1a,
            class_term__term=term or self.last_term,
        ).snapshot
