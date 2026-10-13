"""Taking a student out of an optional subject must not orphan their scores."""
from __future__ import annotations

from schools.vs_academics.exceptions import ElectiveRemovalRefused
from schools.vs_academics.models import ElectiveTaker
from schools.vs_academics.services import electives

from ..models import Score, ScoreSheet
from ..services.electives import refuse_removal_with_scores
from .base import TODAY, GradebookFixture


class RemovalGuardTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for student in (cls.chiamaka, cls.tunde):
            ElectiveTaker.all_objects.create(
                tenant=cls.tenant, school_class=cls.jss1a, term=cls.first_term,
                subject=cls.french, student=student,
            )
        cls.structure = cls.make_structure(cls.junior)
        cls.component = cls.structure.components.first()
        cls.class_term = cls.make_class_term(cls.jss1a, structure=cls.structure)
        cls.sheet = ScoreSheet.all_objects.create(
            tenant=cls.tenant, class_term=cls.class_term, subject=cls.french,
        )

    def score(self, student, *, mark=None, marker=None, sheet=None):
        return Score.all_objects.create(
            tenant=self.tenant, sheet=sheet or self.sheet, student=student,
            component=self.component, mark=mark, marker=marker,
        )

    def remove(self, student, takes=False):
        return electives.apply_changes(
            self.tenant, self.jss1a, self.first_term,
            [{"student": student.pk, "subject": self.french.pk, "takes": takes}],
            actor=self.admin, today=TODAY,
        )

    def test_the_guard_is_registered_with_academic_structure(self):
        self.assertIn(refuse_removal_with_scores, electives.REMOVAL_GUARDS)

    def test_a_student_with_a_mark_cannot_be_taken_off_naming_them(self):
        self.score(self.chiamaka, mark=14)
        with self.assertRaises(ElectiveRemovalRefused) as raised:
            self.remove(self.chiamaka)
        self.assertEqual(
            raised.exception.message,
            "Chiamaka Nwosu already has scores in French for First Term, so French cannot "
            "be taken off them. Clear their scores on the JSS1 A French sheet first, or "
            "leave the subject on.",
        )
        self.assertTrue(ElectiveTaker.objects.filter(student=self.chiamaka, subject=self.french).exists())

    def test_a_marker_counts_as_a_score(self):
        self.score(self.chiamaka, marker="ABS")
        with self.assertRaises(ElectiveRemovalRefused):
            self.remove(self.chiamaka)
        self.score(self.tunde, marker="EXC")
        with self.assertRaises(ElectiveRemovalRefused):
            self.remove(self.tunde)

    def test_a_blank_cell_does_not_stop_the_removal(self):
        self.score(self.chiamaka)
        result = self.remove(self.chiamaka)
        self.assertEqual(result["removed"], [(self.chiamaka.pk, self.french.pk)])

    def test_a_student_with_no_scores_is_taken_off_even_when_a_classmate_has_some(self):
        self.score(self.chiamaka, mark=14)
        result = self.remove(self.tunde)
        self.assertEqual(result["removed"], [(self.tunde.pk, self.french.pk)])
        self.assertTrue(ElectiveTaker.objects.filter(student=self.chiamaka, subject=self.french).exists())

    def test_scores_in_another_term_or_class_or_subject_do_not_stop_it(self):
        second = self.make_class_term(self.jss1a, self.second_term, structure=self.structure)
        other_sheet = ScoreSheet.all_objects.create(
            tenant=self.tenant, class_term=second, subject=self.french,
        )
        self.score(self.chiamaka, mark=10, sheet=other_sheet)
        maths_sheet = ScoreSheet.all_objects.create(
            tenant=self.tenant, class_term=self.class_term, subject=self.maths,
        )
        self.score(self.chiamaka, mark=10, sheet=maths_sheet)
        result = self.remove(self.chiamaka)
        self.assertEqual(result["removed"], [(self.chiamaka.pk, self.french.pk)])

    def test_adding_a_student_is_never_guarded(self):
        result = self.remove(self.kelechi, takes=True)
        self.assertEqual(result["added"], [(self.kelechi.pk, self.french.pk)])

    def test_the_guard_answers_none_when_nothing_is_in_the_way(self):
        self.assertIsNone(
            refuse_removal_with_scores(
                self.tenant, self.jss1a, self.first_term, self.french, self.chiamaka.pk,
            )
        )
