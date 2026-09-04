"""Building a school's structure from one file.

The tests that matter here are the ones about the file as a WHOLE. A row that
is wrong on its own is the easy half; what makes this import worth writing is
that the expensive faults are between rows, and nothing that reads one row at
a time - a form, a serializer, a screen - can see any of them.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase

from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin
from schools.vs_academics.imports import (
    build_structure,
    import_session,
    resolve_file,
    validate_structure_import_batch,
)
from schools.vs_academics.models import (
    AcademicSession,
    Level,
    Program,
    SchoolClass,
)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.year = AcademicSession.all_objects.create(
            tenant=cls.tenant, name="2099/2100",
            start_date=dt.date(2099, 9, 1), end_date=dt.date(2100, 7, 31),
            status="ACTIVE",
        )
        cls.lekki = make_branch(cls.school, name="Lekki", is_main=True)
        cls.admin = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )

    def row(self, **overrides):
        base = {
            "programme": "Junior Secondary", "level": "JSS1",
            "level_order": "1", "promotes_to": "JSS2",
            "class_name": "JSS1 A", "arm": "", "capacity": "30",
            "branch": "", "department": "",
        }
        base.update(overrides)
        return base

    def resolve(self, rows):
        return resolve_file(
            rows, tenant=self.tenant, multi_branch=False,
            session=import_session(self.tenant),
        )

    def errors(self, rows):
        return [i for _n, i in self.resolve(rows).issues if i.severity == "error"]

    def warnings(self, rows):
        return [i for _n, i in self.resolve(rows).issues if i.severity == "warning"]

    def two_years(self):
        """The smallest complete structure: JSS1 promotes into JSS2, which ends."""
        return [
            self.row(),
            self.row(**{"class_name": "JSS1 B"}),
            self.row(**{
                "level": "JSS2", "level_order": "2",
                "promotes_to": "Leaves school", "class_name": "JSS2 A",
            }),
        ]


class WholeFileFaultsTests(_Base):
    """What no single row is wrong about."""

    def test_a_year_group_under_two_programmes_is_refused(self):
        """One mistyped programme cell would otherwise create a second JSS1."""
        errors = self.errors([
            self.row(**{"promotes_to": "Leaves school"}),
            self.row(**{
                "programme": "Senior Secondary", "class_name": "JSS1 B",
                "promotes_to": "Leaves school",
            }),
        ])
        self.assertEqual(len(errors), 1, [e.message for e in errors])
        self.assertIn("belongs to one programme", errors[0].message)
        self.assertIn("Row 1", errors[0].message)

    def test_a_promotion_chain_that_loops_is_refused(self):
        """JSS1 to JSS2 and back again moves a year group round for ever.

        Neither row is wrong on its own, and no screen can show it, because a
        screen sees one year group at a time.
        """
        errors = self.errors([
            self.row(),
            self.row(**{
                "level": "JSS2", "level_order": "2", "promotes_to": "JSS1",
                "class_name": "JSS2 A",
            }),
        ])
        self.assertTrue(
            any("in a circle" in e.message for e in errors), errors,
        )

    def test_a_longer_loop_is_found_and_reported_once(self):
        """JSS1 to JSS2 to JSS3 and back. Every member is a valid start point,
        so a naive walk reports the same loop three times."""
        errors = self.errors([
            self.row(),
            self.row(**{
                "level": "JSS2", "level_order": "2", "promotes_to": "JSS3",
                "class_name": "JSS2 A",
            }),
            self.row(**{
                "level": "JSS3", "level_order": "3", "promotes_to": "JSS1",
                "class_name": "JSS3 A",
            }),
        ])
        loops = [e for e in errors if "in a circle" in e.message]
        self.assertEqual(len(loops), 1, [e.message for e in errors])
        self.assertIn("JSS1", loops[0].message)
        self.assertIn("JSS3", loops[0].message)

    def test_a_year_group_promoting_into_itself_is_refused(self):
        errors = self.errors([self.row(**{"promotes_to": "JSS1"})])
        self.assertTrue(
            any("into itself" in e.message for e in errors), errors,
        )

    def test_promoting_into_a_year_group_that_is_not_in_the_file_is_refused(self):
        errors = self.errors([self.row(**{"promotes_to": "JSS9"})])
        self.assertEqual(len(errors), 1)
        self.assertIn("not a level in this file", errors[0].message)

    def test_two_rows_creating_the_same_class_are_refused(self):
        same = self.row(**{"promotes_to": "Leaves school"})
        errors = self.errors([same, dict(same)])
        self.assertEqual(len(errors), 1, [e.message for e in errors])
        self.assertIn("cannot share one name", errors[0].message)

    def test_two_year_groups_given_the_same_position_are_refused(self):
        errors = self.errors([
            self.row(),
            self.row(**{
                "level": "JSS2", "level_order": "1", "class_name": "JSS2 A",
                "promotes_to": "Leaves school",
            }),
        ])
        self.assertTrue(
            any("cannot share one" in e.message for e in errors), errors,
        )

    def test_a_complete_structure_reports_no_error(self):
        self.assertEqual(self.errors(self.two_years()), [])


class UnwiredLevelTests(_Base):
    """The state that looks complete until the end of the year."""

    def test_a_level_that_says_nothing_about_promotion_is_warned_about(self):
        warnings = self.warnings([self.row(**{"promotes_to": ""})])
        self.assertTrue(
            any("will not move them" in w.message for w in warnings), warnings,
        )

    def test_a_level_that_says_pupils_leave_is_not_warned_about(self):
        warnings = self.warnings([self.row(**{"promotes_to": "Leaves school"})])
        self.assertEqual(
            [w for w in warnings if "will not move them" in w.message], [],
        )


class RowFaultsTests(_Base):
    def test_a_missing_programme_level_or_class_is_refused(self):
        errors = self.errors([self.row(**{"programme": "", "level": ""})])
        self.assertEqual(len(errors), 2)

    def test_a_capacity_that_is_not_a_number_is_refused(self):
        errors = self.errors([self.row(**{"promotes_to": "Leaves school", "capacity": "thirty"})])
        self.assertEqual(len(errors), 1)
        self.assertIn("not a number of seats", errors[0].message)

    def test_an_impossible_capacity_is_refused(self):
        errors = self.errors([self.row(**{"promotes_to": "Leaves school", "capacity": "3000"})])
        self.assertEqual(len(errors), 1)
        self.assertIn("typed number", errors[0].message)

    def test_a_branch_this_school_does_not_have_is_refused(self):
        errors = self.errors([self.row(**{"promotes_to": "Leaves school", "branch": "Nowhere"})])
        self.assertEqual(len(errors), 1)
        self.assertIn("not a branch of this school", errors[0].message)

    def test_a_class_that_does_not_look_like_its_level_is_warned_about(self):
        warnings = self.warnings([self.row(**{"class_name": "SSS3 A"})])
        self.assertTrue(
            any("has not slipped" in w.message for w in warnings), warnings,
        )


class BuildTests(_Base):
    """What the file actually writes."""

    def build(self, rows):
        resolved = self.resolve(rows)
        self.assertEqual(
            [i for _n, i in resolved.issues if i.severity == "error"], [],
        )
        return build_structure(
            resolved, tenant=self.tenant, session=self.year,
            created_by=self.admin,
        )

    def test_one_file_builds_the_programme_the_levels_and_the_classes(self):
        counts = self.build(self.two_years())
        self.assertEqual(counts["programmes"], 1)
        self.assertEqual(counts["levels"], 2)
        self.assertEqual(counts["classes"], 3)
        self.assertEqual(
            SchoolClass.all_objects.filter(tenant=self.tenant).count(), 3,
        )

    def test_the_promotion_chain_is_wired(self):
        """The column that earns the file: without it promotion moves nobody."""
        self.build(self.two_years())
        jss1 = Level.all_objects.get(tenant=self.tenant, name="JSS1")
        jss2 = Level.all_objects.get(tenant=self.tenant, name="JSS2")
        self.assertEqual(jss1.next_level_id, jss2.pk)
        self.assertFalse(jss1.is_terminal)
        self.assertIsNone(jss2.next_level_id)
        self.assertTrue(jss2.is_terminal)

    def test_codes_are_generated_so_the_school_never_invents_one(self):
        self.build(self.two_years())
        self.assertTrue(
            all(Level.all_objects.filter(tenant=self.tenant)
                .values_list("code", flat=True)),
        )
        self.assertTrue(
            all(SchoolClass.all_objects.filter(tenant=self.tenant)
                .values_list("code", flat=True)),
        )

    def test_the_arm_is_taken_from_the_class_name_when_blank(self):
        self.build(self.two_years())
        self.assertEqual(
            SchoolClass.all_objects.get(tenant=self.tenant, name="JSS1 A").arm,
            "A",
        )

    def test_capacity_lands_because_it_is_what_refuses_an_overfull_class(self):
        self.build(self.two_years())
        self.assertEqual(
            SchoolClass.all_objects.get(
                tenant=self.tenant, name="JSS1 A",
            ).capacity,
            30,
        )

    def test_re_uploading_a_corrected_file_builds_no_second_copy(self):
        """A school fixing three cells re-uploads the whole file."""
        self.build(self.two_years())
        counts = self.build(self.two_years())
        self.assertEqual(counts, {
            "departments": 0, "programmes": 0, "levels": 0, "classes": 0,
        })
        self.assertEqual(
            Program.all_objects.filter(tenant=self.tenant).count(), 1,
        )
        self.assertEqual(
            SchoolClass.all_objects.filter(tenant=self.tenant).count(), 3,
        )

    def test_a_class_the_school_already_has_is_warned_about_not_duplicated(self):
        self.build(self.two_years())
        warnings = self.warnings(self.two_years())
        self.assertTrue(
            any("already exists" in w.message for w in warnings), warnings,
        )

    def test_a_level_the_school_runs_under_another_programme_is_refused(self):
        self.build(self.two_years())
        errors = self.errors([
            self.row(**{"programme": "Senior Secondary", "class_name": "JSS1 C"}),
        ])
        self.assertTrue(
            any("already runs" in e.message for e in errors), errors,
        )


class OwnershipTests(_Base):
    def test_a_school_may_import_its_own_structure(self):
        from vs_import_data.datasets import platform_only

        self.assertFalse(platform_only("academic_structure"))

    def test_the_module_import_key_reaches_the_wizard(self):
        from vs_import_data.permissions import _DATASET_IMPORT_KEYS

        self.assertEqual(
            _DATASET_IMPORT_KEYS.get("academic_structure"),
            "academics.structure.import",
        )

    def test_the_template_exists_with_all_nine_columns(self):
        from vs_import_data.models import ImportTemplate

        from schools.vs_academics.imports import COLUMNS

        template = ImportTemplate.objects.get(code="academic_structure_v1")
        fields = set(template.columns.values_list("target_field", flat=True))
        self.assertEqual(fields, set(COLUMNS))

    def test_a_school_with_no_running_year_is_told_so_rather_than_failing(self):
        from vs_import_data.models import ImportBatch, ImportTemplate

        self.year.status = "ARCHIVED"
        self.year.save(update_fields=["status"])
        batch = ImportBatch.all_objects.create(
            tenant=self.tenant,
            template=ImportTemplate.objects.get(code="academic_structure_v1"),
            dataset_type="academic_structure",
            preview_rows=[{}], original_filename="structure.xlsx",
            uploaded_by=self.admin,
        )
        issues = validate_structure_import_batch(batch)
        self.assertEqual(len(issues), 1)
        self.assertIn("no year running", issues[0]["message"])


class SubjectImportTests(_Base):
    """The subject list, and where each subject is taught.

    The check worth the code is the last one: a year group this file leaves
    with no subject at all. Adding subjects one at a time, nobody notices that
    SSS3 was never ticked, because the subject screen shows what each SUBJECT
    covers and never what each year group is missing.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from schools.vs_academics.models import Level, Program

        cls.programme = Program.all_objects.create(
            tenant=cls.tenant, name="Junior Secondary", code="JS", order_index=1,
        )
        cls.jss1 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=cls.programme,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.jss2 = Level.all_objects.create(
            tenant=cls.tenant, session=cls.year, program=cls.programme,
            name="JSS2", code="JSS2", order_index=2,
        )

    def srow(self, **overrides):
        base = {
            "subject": "Mathematics", "levels": "JSS1; JSS2",
            "kind": "Core", "department": "", "branch": "", "description": "",
        }
        base.update(overrides)
        return base

    def sresolve(self, rows):
        from schools.vs_academics.subject_imports import resolve_file

        return resolve_file(rows, tenant=self.tenant, session=self.year)

    def serrors(self, rows):
        return [i for _n, i in self.sresolve(rows).issues if i.severity == "error"]

    def swarnings(self, rows):
        return [i for _n, i in self.sresolve(rows).issues if i.severity == "warning"]

    def full(self):
        """Both year groups covered, so the empty-timetable check stays quiet."""
        return [self.srow(), self.srow(**{"subject": "English"})]

    def test_a_clean_file_reports_no_error(self):
        self.assertEqual(self.serrors(self.full()), [])

    def test_a_year_group_the_school_does_not_run_is_refused_not_skipped(self):
        """Skipping it would teach the subject in one fewer year, silently."""
        errors = self.serrors([self.srow(**{"levels": "JSS1; JSS9"})])
        self.assertEqual(len(errors), 1)
        self.assertIn("JSS9", errors[0].message)

    def test_the_same_subject_on_two_rows_is_refused(self):
        errors = self.serrors([self.srow(), self.srow()])
        self.assertEqual(len(errors), 1)
        self.assertIn("exists once", errors[0].message)

    def test_a_subject_naming_no_year_group_is_warned_about(self):
        warnings = self.swarnings([self.srow(**{"levels": ""})])
        self.assertTrue(
            any("taught nowhere" in w.message for w in warnings), warnings,
        )

    def test_a_year_group_left_with_no_subject_is_warned_about(self):
        """The fault a form cannot have: no screen shows an empty year group."""
        warnings = self.swarnings([self.srow(**{"levels": "JSS1"})])
        self.assertTrue(
            any("empty timetable" in w.message and "JSS2" in w.message
                for w in warnings),
            warnings,
        )

    def test_a_file_that_covers_every_year_group_says_nothing(self):
        warnings = self.swarnings(self.full())
        self.assertEqual(
            [w for w in warnings if "empty timetable" in w.message], [],
        )

    def test_a_year_group_listed_twice_on_one_row_is_warned_about(self):
        warnings = self.swarnings([
            self.srow(**{"levels": "JSS1; JSS1; JSS2"}),
        ])
        self.assertTrue(
            any("listed twice" in w.message for w in warnings), warnings,
        )

    def test_an_unrecognised_kind_is_imported_as_core_with_a_warning(self):
        warnings = self.swarnings([self.srow(**{"kind": "sometimes"})])
        self.assertTrue(
            any("imported as Core" in w.message for w in warnings), warnings,
        )

    def test_building_creates_the_subject_and_its_offerings(self):
        from schools.vs_academics.models import Subject, SubjectOffering
        from schools.vs_academics.subject_imports import build_subjects

        counts = build_subjects(
            self.sresolve(self.full()), tenant=self.tenant, session=self.year,
        )
        self.assertEqual(counts["subjects"], 2)
        self.assertEqual(counts["offerings"], 4)
        self.assertEqual(
            Subject.all_objects.filter(tenant=self.tenant).count(), 2,
        )
        self.assertEqual(
            SubjectOffering.objects.filter(tenant=self.tenant).count(), 4,
        )

    def test_re_uploading_adds_no_second_subject_and_no_second_offering(self):
        from schools.vs_academics.subject_imports import build_subjects

        build_subjects(self.sresolve(self.full()), tenant=self.tenant, session=self.year)
        counts = build_subjects(
            self.sresolve(self.full()), tenant=self.tenant, session=self.year,
        )
        self.assertEqual(counts, {"subjects": 0, "offerings": 0, "departments": 0})

    def test_a_subject_the_school_already_holds_is_warned_about(self):
        from schools.vs_academics.subject_imports import build_subjects

        build_subjects(self.sresolve(self.full()), tenant=self.tenant, session=self.year)
        warnings = self.swarnings(self.full())
        self.assertTrue(
            any("already in this school's subject list" in w.message
                for w in warnings),
            warnings,
        )

    def test_a_department_named_on_a_row_is_created_once(self):
        from schools.vs_academics.models import Department
        from schools.vs_academics.subject_imports import build_subjects

        counts = build_subjects(
            self.sresolve([
                self.srow(**{"department": "Sciences"}),
                self.srow(**{"subject": "Physics", "department": "Sciences"}),
            ]),
            tenant=self.tenant, session=self.year,
        )
        self.assertEqual(counts["departments"], 1)
        self.assertEqual(
            Department.all_objects.filter(tenant=self.tenant).count(), 1,
        )

    def test_a_school_may_import_its_own_subjects(self):
        from vs_import_data.datasets import platform_only

        self.assertFalse(platform_only("subjects"))

    def test_the_template_exists_with_all_six_columns(self):
        from vs_import_data.models import ImportTemplate

        from schools.vs_academics.subject_imports import COLUMNS

        template = ImportTemplate.objects.get(code="subjects_v1")
        fields = set(template.columns.values_list("target_field", flat=True))
        self.assertEqual(fields, set(COLUMNS))
