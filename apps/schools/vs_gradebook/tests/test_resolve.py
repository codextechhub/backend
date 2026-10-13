"""What a class's results are made of: structure, scale, class-term, due sheets, roll and people."""
from __future__ import annotations

import datetime as dt

from schools.vs_academics.models import AcademicSession, AcademicTerm, ElectiveTaker
from schools.vs_staff.models import StaffProfile
from vs_rbac.tests.helpers import make_school

from ..constants import SetupStatus
from ..exceptions import ClassHasNoBranch, InvalidTerm, NoScale, NoStructure
from ..models import ClassTerm
from ..services import resolve
from .base import GradebookFixture, school_clock


class StructureInForceTests(GradebookFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.school_wide = cls.make_structure(cls.junior, name="School-wide")
        cls.ikeja_own = cls.make_structure(cls.junior, branch=cls.ikeja, name="Ikeja own")
        cls.draft = cls.make_structure(cls.senior, status=SetupStatus.DRAFT, name="Draft")

    def test_the_school_wide_structure_applies_where_a_branch_has_none_of_its_own(self):
        self.assertEqual(resolve.structure_for(self.tenant, self.jss1a).pk, self.school_wide.pk)

    def test_a_branch_specific_structure_wins_for_that_branchs_classes_only(self):
        self.assertEqual(resolve.structure_for(self.tenant, self.jss1c).pk, self.ikeja_own.pk)
        self.assertEqual(resolve.structure_for(self.tenant, self.jss1a).pk, self.school_wide.pk)

    def test_a_class_with_no_branch_at_a_school_with_several_uses_the_school_wide_one(self):
        self.assertEqual(resolve.structure_for(self.tenant, self.shared).pk, self.school_wide.pk)

    def test_the_components_come_with_it(self):
        structure = resolve.structure_for(self.tenant, self.jss1a)
        self.assertEqual([c.short_name for c in structure.components.all()], ["CA1", "CA2", "EXAM"])

    def test_a_draft_or_retired_structure_does_not_apply(self):
        with self.assertRaises(NoStructure) as raised:
            resolve.structure_for(self.tenant, self.sss1a)
        self.assertEqual(raised.exception.error_code, "NO_STRUCTURE")
        self.assertEqual(raised.exception.http_status, 409)
        self.assertEqual(
            raised.exception.message,
            "This class's programme has no assessment structure in use. "
            "Set one up in Assessment setup.",
        )
        self.make_structure(self.senior, status=SetupStatus.RETIRED, name="Old")
        with self.assertRaises(NoStructure):
            resolve.structure_for(self.tenant, self.sss1a)

    def test_another_schools_structure_never_applies(self):
        self.make_structure(self.solo_program, tenant=self.solo, name="Theirs")
        with self.assertRaises(NoStructure):
            resolve.structure_for(self.tenant, self.sss1a)

    def test_at_a_school_with_one_branch_a_branchless_class_belongs_to_that_branch(self):
        own = self.make_structure(self.solo_program, tenant=self.solo, branch=self.solo_branch, name="Own")
        self.make_structure(
            self.solo_program, tenant=self.solo, status=SetupStatus.DRAFT, name="Wide draft",
        )
        self.assertEqual(resolve.structure_for(self.solo, self.solo_class).pk, own.pk)

    def test_the_scale_follows_the_same_rule(self):
        wide = self.make_scale(self.junior, name="Wide")
        own = self.make_scale(self.junior, branch=self.ikeja, name="Ikeja")
        self.assertEqual(resolve.scale_for(self.tenant, self.jss1a).pk, wide.pk)
        self.assertEqual(resolve.scale_for(self.tenant, self.jss1c).pk, own.pk)
        self.assertEqual(
            [b.grade for b in resolve.scale_for(self.tenant, self.jss1a).bands.all()],
            ["A", "B", "C", "D", "F"],
        )
        with self.assertRaises(NoScale) as raised:
            resolve.scale_for(self.tenant, self.sss1a)
        self.assertEqual(raised.exception.error_code, "NO_SCALE")
        self.assertIn("no grading scale in use", raised.exception.message)


class ClassTermTests(GradebookFixture):
    def test_a_class_term_that_does_not_exist_reads_as_none(self):
        self.assertIsNone(resolve.class_term(self.tenant, self.jss1a, self.first_term))
        self.assertEqual(ClassTerm.objects.count(), 0)

    def test_creating_one_names_the_classs_branch(self):
        made = resolve.class_term(self.tenant, self.jss1c, self.first_term, create=True)
        self.assertEqual((made.school_class, made.term, made.branch), (self.jss1c, self.first_term, self.ikeja))
        self.assertIsNone(made.structure)
        self.assertIsNone(made.stage)
        again = resolve.class_term(self.tenant, self.jss1c, self.first_term, create=True)
        self.assertEqual(again.pk, made.pk)
        self.assertEqual(resolve.class_term(self.tenant, self.jss1c, self.first_term).pk, made.pk)

    def test_a_term_of_another_year_is_refused(self):
        old_year = AcademicSession.all_objects.create(
            tenant=self.tenant, name="2024/2025", start_date=dt.date(2024, 9, 9),
            end_date=dt.date(2025, 7, 18), status="ARCHIVED",
        )
        old_term = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=old_year, name="Old Term", order_index=1,
            start_date=dt.date(2024, 9, 9), end_date=dt.date(2024, 12, 13),
        )
        with self.assertRaises(InvalidTerm) as raised:
            resolve.class_term(self.tenant, self.jss1a, old_term, create=True)
        self.assertIn("Old Term is not a term of 2025/2026", raised.exception.message)
        self.assertIsNone(resolve.class_term(self.tenant, self.jss1a, old_term))

    def test_a_class_with_no_branch_at_a_school_with_several_cannot_have_one(self):
        with self.assertRaises(ClassHasNoBranch):
            resolve.class_term(self.tenant, self.shared, self.first_term, create=True)
        self.assertEqual(ClassTerm.objects.count(), 0)

    def test_at_a_school_with_one_branch_the_result_belongs_to_that_branch(self):
        made = resolve.class_term(self.solo, self.solo_class, self.solo_term, create=True)
        self.assertEqual(made.branch, self.solo_branch)


class DueSubjectsTests(GradebookFixture):
    def test_a_class_owes_a_sheet_for_each_core_subject_and_not_for_an_untaken_elective(self):
        subjects = resolve.due_subjects(self.tenant, self.jss1a, self.first_term)
        self.assertEqual([s.name for s in subjects], ["Mathematics"])

    def test_an_elective_is_due_once_somebody_takes_it(self):
        ElectiveTaker.all_objects.create(
            tenant=self.tenant, school_class=self.jss1a, term=self.first_term,
            subject=self.french, student=self.chiamaka,
        )
        subjects = resolve.due_subjects(self.tenant, self.jss1a, self.first_term)
        self.assertEqual([s.name for s in subjects], ["French", "Mathematics"])

    def test_takers_in_another_class_or_term_do_not_make_it_due(self):
        ElectiveTaker.all_objects.create(
            tenant=self.tenant, school_class=self.jss1b, term=self.first_term,
            subject=self.french, student=self.chiamaka,
        )
        ElectiveTaker.all_objects.create(
            tenant=self.tenant, school_class=self.jss1a, term=self.second_term,
            subject=self.french, student=self.chiamaka,
        )
        subjects = resolve.due_subjects(self.tenant, self.jss1a, self.first_term)
        self.assertEqual([s.name for s in subjects], ["Mathematics"])

    def test_a_subject_the_level_does_not_offer_is_not_due(self):
        subjects = resolve.due_subjects(self.tenant, self.sss1a, self.first_term)
        self.assertEqual([s.name for s in subjects], ["Mathematics"])

    def test_an_archived_subject_is_not_due(self):
        self.maths.is_active = False
        self.maths.save()
        self.assertEqual(resolve.due_subjects(self.tenant, self.jss1a, self.first_term), [])


class SheetRollTests(GradebookFixture):
    def names(self, roll):
        return [entry.student.first_name for entry in roll]

    def test_a_core_sheet_is_the_class_roll_with_the_suspended_child_on_it(self):
        roll = resolve.sheet_roll(self.tenant, self.jss1a, self.first_term, self.maths)
        self.assertEqual(
            sorted(self.names(roll)), ["Amina", "Chiamaka", "Kelechi", "Tunde"],
        )
        self.assertEqual({e.student.first_name for e in roll if e.suspended}, {"Amina"})

    def test_the_roll_is_read_as_at_today_while_the_term_runs(self):
        roll = resolve.sheet_roll(self.tenant, self.jss1a, self.first_term, self.maths)
        self.assertNotIn("Obinna", self.names(roll))
        with school_clock(dt.datetime(2025, 11, 20, 12, 0, tzinfo=dt.timezone.utc)):
            roll = resolve.sheet_roll(self.tenant, self.jss1a, self.first_term, self.maths)
        self.assertIn("Obinna", self.names(roll))

    def test_a_term_that_has_ended_reads_the_roll_as_it_ended(self):
        late = self.make_student(
            "Late", "Joiner", self.jss1a, "BFS/099", effective=dt.date(2026, 1, 5),
        )
        with school_clock(dt.datetime(2026, 1, 20, 12, 0, tzinfo=dt.timezone.utc)):
            first = resolve.sheet_roll(self.tenant, self.jss1a, self.first_term, self.maths)
            second = resolve.sheet_roll(self.tenant, self.jss1a, self.second_term, self.maths)
        self.assertNotIn(late.first_name, self.names(first))
        self.assertIn("Obinna", self.names(first))
        self.assertIn(late.first_name, self.names(second))

    def test_an_elective_sheet_is_the_takers_only(self):
        for student in (self.chiamaka, self.tunde):
            ElectiveTaker.all_objects.create(
                tenant=self.tenant, school_class=self.jss1a, term=self.first_term,
                subject=self.french, student=student,
            )
        roll = resolve.sheet_roll(self.tenant, self.jss1a, self.first_term, self.french)
        self.assertEqual(sorted(self.names(roll)), ["Chiamaka", "Tunde"])

    def test_a_taker_who_has_left_the_class_is_not_on_the_sheet(self):
        ElectiveTaker.all_objects.create(
            tenant=self.tenant, school_class=self.jss1a, term=self.first_term,
            subject=self.french, student=self.obinna,
        )
        roll = resolve.sheet_roll(self.tenant, self.jss1a, self.first_term, self.french)
        self.assertEqual(roll, [])

    def test_a_subject_the_level_does_not_offer_has_an_empty_roll(self):
        self.assertEqual(resolve.sheet_roll(self.tenant, self.sss1a, self.first_term, self.french), [])

    def test_the_class_branchs_today_is_the_school_clocks(self):
        self.assertEqual(resolve.school_today(self.tenant, self.jss1a), dt.date(2025, 10, 13))
        self.assertEqual(resolve.school_today(self.tenant, self.shared), dt.date(2025, 10, 13))


class PeopleTests(GradebookFixture):
    def test_the_main_teacher_is_the_lead_and_the_assistants_are_the_rest(self):
        self.assertEqual(resolve.main_teacher(self.jss1a, self.maths, self.year), self.eze_staff)
        self.assertEqual(resolve.assistants(self.jss1a, self.maths, self.year), [self.bello_staff])

    def test_a_subject_nobody_leads_has_no_main_teacher(self):
        self.assertIsNone(resolve.main_teacher(self.jss1b, self.maths, self.year))
        self.assertEqual(resolve.assistants(self.jss1b, self.maths, self.year), [])

    def test_assignments_are_per_class(self):
        self.assertIsNone(resolve.main_teacher(self.jss1c, self.maths, self.year))

    def test_the_head_of_a_subject_is_its_departments_head(self):
        self.assertIsNone(resolve.head_of_subject(self.maths))
        self.set_head(self.sciences, self.bello_staff)
        self.maths.refresh_from_db()
        self.assertEqual(resolve.head_of_subject(self.maths), self.bello_staff)

    def test_a_head_who_has_left_heads_nothing(self):
        self.set_head(self.sciences, self.bello_staff)
        StaffProfile.all_objects.filter(pk=self.bello_staff.pk).update(employment_status="RESIGNED")
        self.maths.refresh_from_db()
        self.assertIsNone(resolve.head_of_subject(self.maths))

    def test_a_subject_with_no_department_has_no_head(self):
        self.assertIsNone(resolve.head_of_subject(self.french))


class CurrentTermTests(GradebookFixture):
    def test_the_running_term(self):
        self.assertEqual(resolve.current_term(self.tenant, dt.date(2025, 10, 13)), self.first_term)
        self.assertEqual(resolve.current_term(self.tenant, dt.date(2026, 1, 20)), self.second_term)

    def test_between_terms_it_is_the_term_that_just_ended(self):
        self.assertEqual(resolve.current_term(self.tenant, dt.date(2025, 12, 20)), self.first_term)

    def test_before_the_year_begins_it_is_the_first_term(self):
        self.assertEqual(resolve.current_term(self.tenant, dt.date(2025, 8, 1)), self.first_term)

    def test_a_school_with_no_active_year_has_none(self):
        empty = make_school(slug="empty-school", name="Empty School").tenant
        self.assertIsNone(resolve.current_term(empty, dt.date(2025, 10, 13)))

    def test_another_schools_year_is_not_read(self):
        self.assertEqual(resolve.current_term(self.solo, dt.date(2025, 10, 13)), self.solo_term)
