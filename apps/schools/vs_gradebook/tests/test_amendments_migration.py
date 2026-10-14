"""Migration ``0002``: every published result gets its snapshot as version 1.

The data step runs on the schema the gradebook had before amendments, with
results written through the models as they stood then. A result that was
published has its snapshot written to ``ResultVersion`` as version 1; one that
never was has nothing written; running the step again adds nothing; and the
migration can be reversed and run again.
"""
from __future__ import annotations

import datetime as dt
from importlib import import_module

from django.test import tag

from core.migration_testing import RewoundSchemaTestCase
from schools.vs_academics.models import (
    AcademicSession,
    AcademicTerm,
    Level,
    Program,
    SchoolClass,
)
from schools.vs_students.constants import Gender, StudentStatus
from schools.vs_students.models import Student
from vs_rbac.tests.helpers import make_branch, make_school

AFTER = "0002_amendments_and_result_versions"


@tag("slow")
class BackfillTests(RewoundSchemaTestCase):
    """``vs_gradebook`` is rewound to its first migration once for the class."""

    APP = "vs_gradebook"
    BEFORE = "0001_initial"

    def seed(self):
        """A class with three students: two published, one whose result was never published."""
        school = make_school(slug="rewound", name="Rewound School")
        tenant, branch = school.tenant, make_branch(school, name="Main", is_main=True)
        year = AcademicSession.all_objects.create(
            tenant=tenant, name="2025/2026", start_date=dt.date(2025, 9, 8),
            end_date=dt.date(2026, 7, 17), status="ACTIVE",
        )
        term = AcademicTerm.all_objects.create(
            tenant=tenant, session=year, name="First Term", order_index=1,
            start_date=dt.date(2025, 9, 8), end_date=dt.date(2025, 12, 12),
        )
        program = Program.all_objects.create(tenant=tenant, name="Junior", code="JSS", order_index=1)
        level = Level.all_objects.create(
            tenant=tenant, session=year, program=program, name="JSS1", code="JSS1", order_index=1,
        )
        school_class = SchoolClass.all_objects.create(
            tenant=tenant, session=year, level=level, name="JSS1 A", code="JSS1A", branch=branch,
        )
        students = [
            Student.all_objects.create(
                tenant=tenant, branch=branch, first_name=name, last_name="Pupil",
                student_number=f"RW/00{n}", date_of_birth=dt.date(2013, 4, 18),
                gender=Gender.FEMALE, status=StudentStatus.ACTIVE,
                enrolment_date=dt.date(2025, 9, 8),
            )
            for n, name in enumerate(("Ada", "Bola", "Chidi"), start=1)
        ]

        ClassTerm = self.historical.get_model("vs_gradebook", "ClassTerm")
        Result = self.historical.get_model("vs_gradebook", "StudentTermResult")
        class_term = ClassTerm.objects.create(
            tenant_id=tenant.pk, school_class_id=school_class.pk, term_id=term.pk,
            branch_id=branch.pk, stage="PUBLISHED",
        )
        results = []
        for student, snapshot in zip(students, ({"overall_average": "75.00"}, {"overall_average": "64.50"}, None)):
            results.append(Result.objects.create(
                tenant_id=tenant.pk, class_term_id=class_term.pk, student_id=student.pk,
                snapshot=snapshot, version=1 if snapshot else 0,
            ))
        return results

    def versions(self):
        apps = self.historical_apps(AFTER)
        return apps.get_model("vs_gradebook", "ResultVersion")._default_manager.order_by("result_id")

    def test_every_published_result_gets_its_snapshot_as_version_one(self):
        published_a, published_b, unpublished = self.seed()
        self.migrate_to(AFTER)
        rows = list(self.versions())
        self.assertEqual([r.result_id for r in rows], [published_a.pk, published_b.pk])
        self.assertEqual([r.version for r in rows], [1, 1])
        self.assertEqual(
            [r.snapshot for r in rows],
            [{"overall_average": "75.00"}, {"overall_average": "64.50"}],
        )
        self.assertEqual([r.tenant_id for r in rows], [published_a.tenant_id] * 2)
        self.assertTrue(all(r.amendment_id is None for r in rows))
        self.assertNotIn(unpublished.pk, [r.result_id for r in rows])

    def test_a_result_that_already_has_a_version_one_is_not_written_twice(self):
        published_a, _, _ = self.seed()
        self.migrate_to(AFTER)
        step = import_module(f"schools.vs_gradebook.migrations.{AFTER}").write_version_one
        step(self.historical_apps(AFTER), None)
        self.assertEqual(self.versions().filter(result_id=published_a.pk).count(), 1)
        self.assertEqual(self.versions().count(), 2)

    def test_the_migration_reverses_and_runs_again(self):
        self.seed()
        self.migrate_to(AFTER)
        self.assertEqual(self.versions().count(), 2)
        self.migrate_to(self.BEFORE)
        self.migrate_to(AFTER)
        self.assertEqual(self.versions().count(), 2)

    def test_a_schema_with_no_published_result_gets_no_version_rows(self):
        self.migrate_to(AFTER)
        self.assertEqual(self.versions().count(), 0)
