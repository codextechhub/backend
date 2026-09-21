"""Every route that writes a student's restricted fields applies its rule.

Blood group, allergies and conditions are Field Access switches on
``school.students``. A role whose Write switch does not reach them is refused,
whether the record is being created by enrolment or edited later. The
enrolment date is different: it is declared open on create, so a new pupil's
date is set by whoever enrols them, through the enrol form or the spreadsheet
import alike, and only changing it on an existing record asks the switch.

Each rule is proven against both shapes of school: Brightfield with two
branches and Sunrise with one. Creating a record, a blank medical value writes
nothing and passes, because the enrol form posts every input whether it was
touched or not. Editing one, a blank value erases what is stored, so it is
refused like any other.

A test database carries no Field Access registry, and an unregistered field is
open to everybody, so the fixture installs the student declarations first and
then sets each role's switches to what the conversion leaves it holding: the
medical fields closed for everybody but the nurse, and the enrolment date
readable by all three with Write off, which is what a role that did not hold
``school.students.manage`` converts to.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_academics.models import Level, Program, SchoolClass
from schools.vs_students.constants import Relationship
from schools.vs_students.imports import validate_students_import_batch
from schools.vs_students.models import (
    ClassEnrolment,
    Guardian,
    Student,
    StudentGuardian,
)
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .test_import_export import _ImportFixture

MEDICAL = ("blood_group", "allergies", "conditions")

MEDICAL_KEYS = tuple(f"school.students.{name}" for name in MEDICAL)
ENROLMENT_DATE_KEY = "school.students.enrolment_date"

#: What an admissions officer holds: enrol and seat a child, nothing more.
OFFICER_KEYS = (
    "school.students.view",
    "school.students.create",
    "academics.classes.assign",
    "academics.classes.view",
)


class _WritePathFixture(_ImportFixture):
    """Three roles per school: an officer, a nurse-registrar and a clerk.

    None of them may change an enrolment date once the record exists, which is
    what a role without ``school.students.manage`` converts to. Only the nurse
    reads and writes the medical fields.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students")
        tenant = cls.solo.tenant
        program = Program.all_objects.create(tenant=tenant, name="Primary", code="PRY")
        level = Level.all_objects.create(
            tenant=tenant, program=program, session=cls.solo_year,
            name="Primary 1", code="P1", order_index=1,
        )
        cls.solo_class = SchoolClass.all_objects.create(
            tenant=tenant, level=level, session=cls.solo_year,
            name="Primary 1 A", code="P1A", arm="A", capacity=30, branch=None,
        )
        cls.people = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            cls.people[label] = {
                "officer": cls._person(school, label, "officer", ()),
                "nurse": cls._person(school, label, "nurse", (), medical=True),
                "clerk": cls._person(
                    school, label, "clerk", ("school.students.update",),
                ),
            }

    @classmethod
    def _person(cls, school, label, name, extra_keys, *, medical=False):
        role = make_role(school, name=f"{name.title()} {label}", key=f"{name}_{label}")
        for key in OFFICER_KEYS + tuple(extra_keys):
            make_role_permission(role, cls.permissions[key])
        # Everybody reads when a pupil joined and nobody may rewrite it: the
        # state a role without school.students.manage converts to.
        set_field_access(role, ENROLMENT_DATE_KEY, read=True, write=False)
        if medical:
            set_field_access(role, *MEDICAL_KEYS, read=True, write=True)
        user = make_school_admin(
            None, email=f"{name}.{label}@write-paths.test", tenant=school.tenant,
        )
        make_assignment(school, user, role, branch=None)
        return user

    # ── the two shapes ─────────────────────────────────────────────────────

    def shapes(self):
        """``(label, tenant, body builder)`` for the two-branch and one-branch school."""
        return (
            ("multi", self.tenant, self.enrolment_body),
            ("single", self.solo.tenant, self.solo_body),
        )

    def solo_body(self, **overrides):
        """An enrolment at Sunrise, naming no branch because it has only one."""
        body = {
            "first_name": "Tunde", "last_name": "Bello",
            "date_of_birth": "2014-02-11", "gender": "MALE",
            "school_class": self.solo_class.pk,
            "guardians": [{
                "full_name": "Mrs. Kemi Bello", "phone": "08025550188",
                "email": "kemi.bello@example.ng",
                "relationship": Relationship.MOTHER, "is_primary": True,
            }],
        }
        body.update(overrides)
        return body

    def counts(self, tenant):
        return tuple(
            model.all_objects.filter(tenant=tenant).count()
            for model in (Student, ClassEnrolment, Guardian, StudentGuardian)
        )

    def refused_fields(self, response):
        """The field names a refusal carries errors under, in ``error.detail``."""
        detail = (response.data.get("error") or {}).get("detail")
        return set(detail) if isinstance(detail, dict) else set()

    def assertFieldWriteDenied(self, response):
        """A refused field write is a 403 that names the field, not a 400."""
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")


class EnrolmentMedicalFieldTests(_WritePathFixture):
    def test_medical_values_without_the_switch_are_refused_per_field_and_nothing_is_written(self):
        """An admissions officer cannot type a pupil's allergies the nurse relies on."""
        for label, tenant, body in self.shapes():
            with self.subTest(school=label):
                officer = self.people[label]["officer"]
                before = self.counts(tenant)
                response = self.post(officer, "student-list", body(
                    blood_group="O+", allergies="No known allergies",
                    conditions="None",
                ))
                self.assertFieldWriteDenied(response)
                self.assertTrue(
                    set(MEDICAL) <= self.refused_fields(response), response.data,
                )
                self.assertEqual(self.counts(tenant), before)

    def test_one_medical_value_without_the_switch_names_only_that_field(self):
        for label, tenant, body in self.shapes():
            with self.subTest(school=label):
                officer = self.people[label]["officer"]
                response = self.post(officer, "student-list", body(
                    allergies="Peanuts", blood_group="", conditions="",
                ))
                self.assertFieldWriteDenied(response)
                self.assertEqual(self.refused_fields(response), {"allergies"})

    def test_blank_or_absent_medical_values_without_the_switch_enrol_with_the_fields_empty(self):
        for label, tenant, body in self.shapes():
            officer = self.people[label]["officer"]
            for case, extra in (
                ("blank", {"blood_group": "", "allergies": "", "conditions": ""}),
                ("whitespace", {"blood_group": " ", "allergies": "  ", "conditions": ""}),
                ("absent", {}),
            ):
                with self.subTest(school=label, case=case):
                    response = self.post(officer, "student-list", body(
                        first_name=f"Blank{case.title()}", **extra,
                    ))
                    self.assertEqual(response.status_code, 201, response.data)
                    student = Student.all_objects.get(
                        tenant=tenant, first_name=f"Blank{case.title()}",
                    )
                    for field in MEDICAL:
                        self.assertEqual(getattr(student, field), "")

    def test_medical_values_with_the_switch_are_saved(self):
        for label, tenant, body in self.shapes():
            with self.subTest(school=label):
                nurse = self.people[label]["nurse"]
                response = self.post(nurse, "student-list", body(
                    first_name="Keyed", blood_group="AB+",
                    allergies="Penicillin", conditions="Asthma",
                ))
                self.assertEqual(response.status_code, 201, response.data)
                student = Student.all_objects.get(tenant=tenant, first_name="Keyed")
                self.assertEqual(
                    (student.blood_group, student.allergies, student.conditions),
                    ("AB+", "Penicillin", "Asthma"),
                )

    def test_a_nurse_without_the_date_switch_enrols_with_medical_values_and_a_chosen_date(self):
        """The medical switch is all enrolment asks for; the date is open on create."""
        for label, tenant, body in self.shapes():
            with self.subTest(school=label):
                nurse = self.people[label]["nurse"]
                response = self.post(nurse, "student-list", body(
                    first_name="Nursed", allergies="Latex",
                    enrolment_date="2025-09-08",
                ))
                self.assertEqual(response.status_code, 201, response.data)
                student = Student.all_objects.get(tenant=tenant, first_name="Nursed")
                self.assertEqual(
                    (student.allergies, student.enrolment_date),
                    ("Latex", dt.date(2025, 9, 8)),
                )


class EnrolmentDateTests(_WritePathFixture):
    """Set freely by whoever enrols; changed later only with the Write switch."""

    def test_an_officer_without_the_write_switch_enrols_with_a_chosen_date_and_it_is_saved(self):
        for label, tenant, body in self.shapes():
            with self.subTest(school=label):
                officer = self.people[label]["officer"]
                response = self.post(officer, "student-list", body(
                    first_name="Dated", enrolment_date="2025-09-08",
                ))
                self.assertEqual(response.status_code, 201, response.data)
                student = Student.all_objects.get(tenant=tenant, first_name="Dated")
                self.assertEqual(student.enrolment_date, dt.date(2025, 9, 8))

    def test_changing_that_date_later_without_the_write_switch_is_refused(self):
        for label, tenant, body in self.shapes():
            with self.subTest(school=label):
                officer = self.people[label]["officer"]
                clerk = self.people[label]["clerk"]
                response = self.post(officer, "student-list", body(
                    first_name="Moved", enrolment_date="2025-09-08",
                ))
                self.assertEqual(response.status_code, 201, response.data)
                student = Student.all_objects.get(tenant=tenant, first_name="Moved")

                response = self.patch(
                    clerk, "student-detail", {"enrolment_date": "2025-01-06"},
                    pk=student.pk,
                )
                self.assertFieldWriteDenied(response)
                self.assertEqual(self.refused_fields(response), {"enrolment_date"})
                student.refresh_from_db()
                self.assertEqual(student.enrolment_date, dt.date(2025, 9, 8))


class EditRouteIsUnchangedTests(_WritePathFixture):
    """Editing keeps refusing every present value, blank included."""

    def records(self):
        return (
            ("multi", self.student(allergies="Peanuts", enrolment_date=dt.date(2024, 9, 9))),
            ("single", self.student(
                tenant=self.solo.tenant, branch=self.solo_branch,
                allergies="Peanuts", enrolment_date=dt.date(2024, 9, 9),
            )),
        )

    def test_a_blank_medical_value_without_the_switch_is_still_refused_on_edit(self):
        """On an existing record a blank erases the nurse's entry."""
        for label, row in self.records():
            with self.subTest(school=label):
                clerk = self.people[label]["clerk"]
                response = self.patch(
                    clerk, "student-detail", {"allergies": ""}, pk=row.pk,
                )
                self.assertFieldWriteDenied(response)
                self.assertIn("allergies", self.refused_fields(response))
                row.refresh_from_db()
                self.assertEqual(row.allergies, "Peanuts")

    def test_an_enrolment_date_without_the_write_switch_is_refused_on_edit(self):
        for label, row in self.records():
            with self.subTest(school=label):
                clerk = self.people[label]["clerk"]
                response = self.patch(
                    clerk, "student-detail", {"enrolment_date": "2025-01-06"},
                    pk=row.pk,
                )
                self.assertFieldWriteDenied(response)
                row.refresh_from_db()
                self.assertEqual(row.enrolment_date, dt.date(2024, 9, 9))

    def test_an_edit_that_touches_no_restricted_field_still_saves(self):
        for label, row in self.records():
            with self.subTest(school=label):
                clerk = self.people[label]["clerk"]
                response = self.patch(
                    clerk, "student-detail", {"previous_school": "Hilltop"}, pk=row.pk,
                )
                self.assertEqual(response.status_code, 200, response.data)


class ImportEnrolmentDateTests(_WritePathFixture):
    """A spreadsheet's admission date is set by whoever loads the roll, like the form's."""

    def import_shapes(self):
        return (
            ("multi", self.tenant, self.lekki, {}),
            ("single", self.solo.tenant, self.solo_branch, {"Branch": "", "Class": ""}),
        )

    def batch_for(self, rows, *, tenant, branch, uploaded_by):
        from vs_import_data.models import ImportBatch, ImportTemplate

        return ImportBatch.all_objects.create(
            tenant=tenant, branch=branch,
            template=ImportTemplate.objects.get(code="students_v1"),
            dataset_type="students", preview_rows=rows,
            original_filename="roll.xlsx", uploaded_by=uploaded_by,
        )

    def test_an_uploader_without_the_write_switch_validates_a_file_with_an_admission_date(self):
        for label, tenant, branch, overrides in self.import_shapes():
            with self.subTest(school=label):
                officer = self.people[label]["officer"]
                issues = validate_students_import_batch(self.batch_for(
                    [self.row(**overrides)], tenant=tenant, branch=branch,
                    uploaded_by=officer,
                ))
                self.assertEqual(
                    [i for i in issues if i["severity"] == "error"], [],
                )

    def test_an_uploader_without_the_write_switch_executes_and_the_admission_date_is_saved(self):
        from vs_import_data.services.import_executor import (
            execute_dataset_handler, map_row_to_payload,
        )

        for label, tenant, branch, overrides in self.import_shapes():
            with self.subTest(school=label):
                officer = self.people[label]["officer"]
                raw_row = self.row(**{**overrides, "First Name": f"Dated{label.title()}"})
                batch = self.batch_for(
                    [raw_row], tenant=tenant, branch=branch, uploaded_by=officer,
                )
                result = execute_dataset_handler(
                    batch, map_row_to_payload(batch, raw_row), officer,
                )
                self.assertEqual(result.instance.enrolment_date, dt.date(2025, 9, 8))
