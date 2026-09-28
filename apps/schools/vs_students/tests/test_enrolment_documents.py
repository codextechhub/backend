"""The documents a school requires before a child joins the roll, at enrolment and on import.

``applicants.documents.required_to_confirm`` holds a confirmation back until
the documents are on the applicant's record. The same list holds a direct
enrolment: the enrol form sends the documents in the same save, as
``multipart/form-data``, and an enrolment without them is refused with nothing
written. A spreadsheet carries no documents, so at a school with such a list
the student import brings every row in as an applicant instead.

Security first: the photograph's write switch, and one school's list never
reaching another. Brightfield has two branches and Sunrise one, so each rule
is seen at both shapes of school, and a school requiring nothing is held to
enrol and import exactly as before.
"""
from __future__ import annotations

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from schools.vs_academics.models import Level, Program, SchoolClass
from schools.vs_students.constants import DocumentType, StudentStatus
from schools.vs_students.imports import validate_students_import_batch
from schools.vs_students.models import Guardian, Student, StudentDocument
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .base import StudentsFixture
from .test_import_export import _ImportFixture

BIRTH = DocumentType.BIRTH_CERTIFICATE
TRANSFER = DocumentType.TRANSFER_CERTIFICATE


def pdf(name="doc.pdf", size=16):
    return SimpleUploadedFile(name, b"%" * size, content_type="application/pdf")


class _RequiresDocuments:
    """Sunrise gets a class of its own, and each school can require documents."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        program = Program.all_objects.create(
            tenant=cls.solo.tenant, name="Junior Secondary", code="JSS",
        )
        cls.solo_level = Level.all_objects.create(
            tenant=cls.solo.tenant, program=program, session=cls.solo_year,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.solo_class = SchoolClass.all_objects.create(
            tenant=cls.solo.tenant, level=cls.solo_level, session=cls.solo_year,
            name="JSS1 A", code="JSS1A", arm="A", capacity=30, branch=None,
        )

    def require(self, *documents, tenant=None, actor=None):
        from schools.vs_students.services.admission import write_admission_rules

        write_admission_rules(
            tenant or self.tenant, actor or self.admin,
            stages=[], required_documents_to_confirm=list(documents),
        )


class _EnrolFixture(_RequiresDocuments, StudentsFixture):

    def body(self, **overrides):
        return self.enrolment_body(first_name="Tunde", last_name="Bello", **overrides)

    def solo_body(self, **overrides):
        body = self.body(school_class=self.solo_class.pk, **overrides)
        body.pop("branch")
        return body

    def post_form(self, user, body, files=None, **fields):
        """The enrol form's multipart save: the JSON in ``payload``, then the files."""
        url = reverse("student-list")
        data = {**fields, **(files or {})}
        if body is not None:
            data["payload"] = json.dumps(body)
        return self.client_for(user).post(
            f"{url}?tenant={self._slug(user)}", data, format="multipart",
        )

    def refusal(self, response, status=400):
        self.assertEqual(response.status_code, status, response.data)
        return response.data["error"]["detail"]

    def assert_nothing_written(self, tenant=None):
        tenant = tenant or self.tenant
        self.assertFalse(Student.all_objects.filter(tenant=tenant).exists())
        self.assertFalse(Guardian.all_objects.filter(tenant=tenant).exists())
        self.assertFalse(StudentDocument.all_objects.filter(tenant=tenant).exists())

    def held(self, student_id):
        return set(
            StudentDocument.all_objects.filter(student_id=student_id)
            .values_list("document_type", flat=True),
        )


# ── security ────────────────────────────────────────────────────────────────

class EnrolmentDocumentSecurityTests(_EnrolFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students")
        role = make_role(cls.school, name="Clerk", key="clerk")
        for key in (
            "school.students.view", "school.students.create",
            "academics.classes.assign", "academics.classes.view",
        ):
            make_role_permission(role, cls.permissions[key])
        set_field_access(role, "school.students.photo", read=True, write=False)
        cls.clerk = make_school_admin(None, email="clerk@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.clerk, role, branch=None)

    def test_enrolling_with_documents_still_needs_the_enrol_keys(self):
        response = self.post_form(self.nobody, self.body(), {"document_BIRTH_CERTIFICATE": pdf()})
        self.assertEqual(response.status_code, 403)
        self.assert_nothing_written()

    def test_a_photograph_is_refused_without_its_write_switch(self):
        """The enrol form cannot attach what the documents route would refuse."""
        response = self.post_form(self.clerk, self.body(), {
            "document_PASSPORT_PHOTO": SimpleUploadedFile(
                "face.jpg", b"x" * 16, content_type="image/jpeg",
            ),
        })
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "field_write_denied")
        self.assert_nothing_written()

    def test_one_schools_list_does_not_hold_another_school(self):
        """Brightfield requires a birth certificate; Sunrise requires nothing."""
        self.require(BIRTH)
        response = self.post(self.solo_admin, "student-list", self.solo_body())
        self.assertEqual(response.status_code, 201, response.data)

    def test_the_other_way_round_too(self):
        self.require(BIRTH, tenant=self.solo.tenant, actor=self.solo_admin)
        at_sunrise = self.post(self.solo_admin, "student-list", self.solo_body())
        self.assertEqual(self.refusal(at_sunrise, 422)["missing"][0]["value"], BIRTH)
        at_brightfield = self.post(self.admin, "student-list", self.body())
        self.assertEqual(at_brightfield.status_code, 201, at_brightfield.data)


# ── direct enrolment ────────────────────────────────────────────────────────

class DirectEnrolmentDocumentTests(_EnrolFixture):

    def test_a_missing_document_refuses_the_enrolment_and_writes_nothing(self):
        self.require(BIRTH)
        response = self.post(self.admin, "student-list", self.body())
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "DOCUMENTS_MISSING")
        self.assertEqual(
            response.data["message"],
            "Tunde Bello cannot be enrolled until the birth certificate is attached.",
        )
        self.assertEqual(response.data["error"]["detail"], {"missing": [
            {"value": "BIRTH_CERTIFICATE", "label": "Birth certificate"},
        ]})
        self.assert_nothing_written()

    def test_the_refusal_names_every_missing_document(self):
        """A document sent is not listed; the ones still owed are."""
        self.require(BIRTH, TRANSFER, DocumentType.IMMUNISATION)
        response = self.post_form(
            self.admin, self.body(middle_name="Ade"), {"document_TRANSFER_CERTIFICATE": pdf()},
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["message"],
            "Tunde Ade Bello cannot be enrolled until the birth certificate and "
            "immunisation record are attached.",
        )
        self.assertEqual(
            [row["value"] for row in response.data["error"]["detail"]["missing"]],
            ["BIRTH_CERTIFICATE", "IMMUNISATION"],
        )
        self.assert_nothing_written()

    def test_the_enrolment_passes_and_attaches_the_file_sent_with_it(self):
        self.require(BIRTH)
        response = self.post_form(self.admin, self.body(), {
            "document_BIRTH_CERTIFICATE": pdf("tunde-birth.pdf"),
        })
        self.assertEqual(response.status_code, 201, response.data)
        student = Student.all_objects.get(pk=response.data["data"]["id"])
        self.assertEqual(student.status, StudentStatus.ACTIVE)
        self.assertEqual(student.enrolments.filter(is_active=True).count(), 1)
        doc = StudentDocument.all_objects.get(student=student)
        self.assertEqual((doc.document_type, doc.original_name), (BIRTH, "tunde-birth.pdf"))

    def test_the_same_at_a_one_branch_school(self):
        self.require(BIRTH, tenant=self.solo.tenant, actor=self.solo_admin)
        refused = self.post_form(self.solo_admin, self.solo_body())
        self.assertEqual(self.refusal(refused, 422)["missing"][0]["value"], BIRTH)
        self.assert_nothing_written(self.solo.tenant)
        enrolled = self.post_form(self.solo_admin, self.solo_body(), {
            "document_BIRTH_CERTIFICATE": pdf(),
        })
        self.assertEqual(enrolled.status_code, 201, enrolled.data)
        self.assertEqual(self.held(enrolled.data["data"]["id"]), {BIRTH})

    def test_unrequired_documents_are_attached_too(self):
        self.require(BIRTH)
        response = self.post_form(self.admin, self.body(), {
            "document_BIRTH_CERTIFICATE": pdf(),
            "document_REPORT_CARD": pdf("report.pdf"),
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.held(response.data["data"]["id"]), {BIRTH, DocumentType.REPORT_CARD})

    def test_saving_an_applicant_needs_no_document(self):
        self.require(BIRTH)
        response = self.post(self.admin, "student-list", self.body(
            as_applicant=True, applied_for=self.jss1.pk,
        ))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], StudentStatus.APPLICANT)

    def test_documents_sent_with_an_applicant_are_attached(self):
        self.require(BIRTH, TRANSFER)
        response = self.post_form(
            self.admin, self.body(as_applicant=True, applied_for=self.jss1.pk),
            {"document_BIRTH_CERTIFICATE": pdf()},
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.held(response.data["data"]["id"]), {BIRTH})

    def test_json_enrols_as_before_at_a_school_requiring_nothing(self):
        response = self.post(self.admin, "student-list", self.body())
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["status"], StudentStatus.ACTIVE)

    def test_multipart_works_at_a_school_requiring_nothing(self):
        response = self.post_form(self.admin, self.body(), {"document_REPORT_CARD": pdf()})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.held(response.data["data"]["id"]), {DocumentType.REPORT_CARD})


# ── the form's own refusals ─────────────────────────────────────────────────

class EnrolmentFormTests(_EnrolFixture):

    def test_an_unknown_document_field_is_refused_by_name(self):
        response = self.post_form(self.admin, self.body(), {"document_SCHOOL_REPORT": pdf()})
        detail = self.refusal(response)
        self.assertEqual(list(detail), ["document_SCHOOL_REPORT"])
        self.assertIn("BIRTH_CERTIFICATE", detail["document_SCHOOL_REPORT"][0])
        self.assert_nothing_written()

    def test_a_document_type_is_matched_exactly(self):
        detail = self.refusal(self.post_form(
            self.admin, self.body(), {"document_birth_certificate": pdf()},
        ))
        self.assertIn("document_birth_certificate", detail)

    def test_a_file_too_large_for_the_upload_route_is_refused_here(self):
        response = self.post_form(self.admin, self.body(), {
            "document_BIRTH_CERTIFICATE": pdf(size=6 * 1024 * 1024),
        })
        detail = self.refusal(response)
        self.assertEqual(
            detail["document_BIRTH_CERTIFICATE"],
            ["That file is 6MB. The limit is 5MB."],
        )
        self.assert_nothing_written()

    def test_a_photograph_must_be_an_image_as_on_the_upload_route(self):
        detail = self.refusal(self.post_form(
            self.admin, self.body(), {"document_PASSPORT_PHOTO": pdf()},
        ))
        self.assertIn("must be an image", detail["document_PASSPORT_PHOTO"][0])
        self.assert_nothing_written()

    def test_a_missing_or_malformed_payload_is_refused(self):
        missing = self.refusal(self.post_form(self.admin, None, {"document_REPORT_CARD": pdf()}))
        self.assertIn("payload", missing)
        malformed = self.refusal(self.post_form(self.admin, None, payload="{not json"))
        self.assertIn("payload", malformed)
        not_an_object = self.refusal(self.post_form(self.admin, None, payload="[1, 2]"))
        self.assertIn("payload", not_an_object)
        self.assert_nothing_written()

    def test_a_field_beside_the_payload_is_refused_rather_than_ignored(self):
        """``as_applicant`` sent flat would otherwise enrol a child meant to wait."""
        detail = self.refusal(self.post_form(self.admin, self.body(), as_applicant="true"))
        self.assertIn("as_applicant", detail)
        self.assert_nothing_written()

    def test_the_payload_is_validated_as_the_json_body_is(self):
        body = self.body()
        body.pop("school_class")
        detail = self.refusal(self.post_form(self.admin, body, {"document_REPORT_CARD": pdf()}))
        self.assertIn("school_class", detail)
        self.assert_nothing_written()


# ── the student import ──────────────────────────────────────────────────────

class ImportAtARequiringSchoolTests(_RequiresDocuments, _ImportFixture):

    WARNING = (
        "This school needs a birth certificate before enrolling, so this child "
        "is imported as an applicant and joins the roll once it is uploaded."
    )

    def execute(self, raw_row, *, tenant=None, branch=None, user=None):
        from vs_import_data.services.import_executor import (
            execute_dataset_handler, map_row_to_payload,
        )

        batch = self.batch([raw_row], tenant=tenant, branch=branch)
        return execute_dataset_handler(
            batch, map_row_to_payload(batch, raw_row), user or self.admin,
        )

    def warnings(self, issues):
        return [i["message"] for i in issues if i["severity"] == "warning"]

    def test_each_row_is_warned_once_that_it_comes_in_as_an_applicant(self):
        self.require(BIRTH)
        issues = validate_students_import_batch(self.batch([
            self.row(),
            self.row(**{"First Name": "Somto", "Class": ""}),
        ]))
        self.assertFalse([i for i in issues if i["severity"] == "error"], issues)
        by_row = {}
        for issue in issues:
            if issue["message"] == self.WARNING:
                by_row[issue["row_number"]] = by_row.get(issue["row_number"], 0) + 1
        self.assertEqual(by_row, {1: 1, 2: 1})
        # An applicant takes no seat, so the no-class warning does not apply.
        self.assertFalse(
            [m for m in self.warnings(issues) if m.startswith("No class given")], issues,
        )

    def test_the_warning_names_every_document(self):
        self.require(BIRTH, DocumentType.IMMUNISATION)
        issues = validate_students_import_batch(self.batch([self.row()]))
        self.assertIn(
            "This school needs a birth certificate and an immunisation record "
            "before enrolling, so this child is imported as an applicant and "
            "joins the roll once they are uploaded.",
            self.warnings(issues),
        )

    def test_a_row_becomes_an_applicant_for_its_classs_level_and_takes_no_seat(self):
        self.require(BIRTH)
        result = self.execute(self.row(), branch=self.lekki)
        student = result.instance
        self.assertEqual(student.status, StudentStatus.APPLICANT)
        self.assertEqual(student.applied_for, self.jss1)
        self.assertIsNotNone(student.applied_on)
        self.assertEqual(student.branch, self.lekki)
        self.assertEqual(student.enrolments.count(), 0)
        self.assertEqual(student.guardian_links.filter(is_primary=True).count(), 1)
        self.assertEqual(result.message, "Chiamaka Nwosu saved as an applicant.")

    def test_it_is_listed_under_the_year_of_that_level(self):
        self.require(BIRTH)
        self.execute(self.row(), branch=self.lekki)
        rows = self.get(self.admin, "student-list", {
            "status": "APPLICANT", "session": self.year.pk,
        }).data["data"]
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual([row["first_name"] for row in rows], ["Chiamaka"])

    def test_no_number_is_issued_and_a_typed_one_is_kept(self):
        from ..services.policy import write_policy

        write_policy(
            self.tenant, self.admin, required=True, pattern=r"BFS/\d{4}",
            hint="BFS/NNNN", auto_issue=True,
        )
        self.student(branch=self.lekki, first="Old", last="Pupil", number="BFS/0007")
        self.require(BIRTH)
        blank = self.execute(self.row(), branch=self.lekki).instance
        typed = self.execute(self.row(**{
            "First Name": "Tunde", "Admission No.": "BFS/0100",
        }), branch=self.lekki).instance
        self.assertEqual((blank.student_number, typed.student_number), ("", "BFS/0100"))

    def test_a_blank_number_is_not_refused_where_the_branch_requires_one(self):
        """Required and nothing to count on from: an applicant waits for confirmation."""
        from ..services.policy import write_policy

        write_policy(
            self.tenant, self.admin, required=True, pattern=r"BFS/\d{4}",
            hint="BFS/NNNN", auto_issue=False,
        )
        self.require(BIRTH)
        issues = validate_students_import_batch(self.batch([self.row()]))
        self.assertFalse([i for i in issues if i["severity"] == "error"], issues)

    def test_a_typed_number_is_still_checked(self):
        self.student(branch=self.lekki, first="Old", last="Pupil", number="BFS/0100")
        self.require(BIRTH)
        issues = validate_students_import_batch(self.batch([
            self.row(**{"Admission No.": "BFS/0100"}),
        ]))
        self.assertIn(
            "Admission No.", [i["column_name"] for i in issues if i["severity"] == "error"],
        )

    def test_rows_are_not_counted_against_a_full_class(self):
        """The Lekki class holds two; a file of three applicants takes no seat in it."""
        self.require(BIRTH)
        issues = validate_students_import_batch(self.batch([
            self.row(**{"First Name": name, "Class": "JSS1 B"})
            for name in ("Ada", "Bola", "Chidi")
        ]))
        self.assertFalse([m for m in self.warnings(issues) if "capacity" in m], issues)

    def test_a_one_branch_school_imports_applicants_the_same_way(self):
        self.require(BIRTH, tenant=self.solo.tenant, actor=self.solo_admin)
        row = self.row(**{"Branch": ""})
        issues = validate_students_import_batch(self.batch([row], tenant=self.solo.tenant))
        self.assertFalse([i for i in issues if i["severity"] == "error"], issues)
        self.assertIn(self.WARNING, self.warnings(issues))
        student = self.execute(row, tenant=self.solo.tenant, user=self.solo_admin).instance
        self.assertEqual(student.status, StudentStatus.APPLICANT)
        self.assertEqual(student.applied_for, self.solo_level)
        self.assertEqual(student.branch, self.solo_branch)

    def test_the_applicant_joins_the_roll_once_the_document_is_uploaded(self):
        self.require(BIRTH)
        student = self.execute(self.row(), branch=self.lekki).instance
        refused = self.post(self.admin, "student-confirm", {}, pk=student.pk)
        self.assertEqual(refused.data["error"]["code"], "DOCUMENTS_MISSING")
        StudentDocument.all_objects.create(
            tenant=self.tenant, student=student, document_type=BIRTH,
            file=pdf(), original_name="doc.pdf",
        )
        confirmed = self.post(self.admin, "student-confirm", {}, pk=student.pk)
        self.assertEqual(confirmed.status_code, 200, confirmed.data)


class ImportAtASchoolRequiringNothingTests(_RequiresDocuments, _ImportFixture):

    def test_a_row_is_enrolled_and_placed_as_before_with_no_applicant_warning(self):
        from vs_import_data.services.import_executor import (
            execute_dataset_handler, map_row_to_payload,
        )

        issues = validate_students_import_batch(self.batch([self.row()]))
        self.assertFalse(
            [i for i in issues if "imported as an applicant" in i["message"]], issues,
        )
        batch = self.batch([self.row()], branch=self.lekki)
        result = execute_dataset_handler(batch, map_row_to_payload(batch, self.row()), self.admin)
        self.assertEqual(result.instance.status, StudentStatus.ACTIVE)
        self.assertEqual(result.instance.enrolments.filter(is_active=True).count(), 1)
        self.assertEqual(result.message, "Chiamaka Nwosu enrolled.")

    def test_a_file_overfilling_a_class_is_still_warned_about(self):
        """The counterpart of the applicant rows that take no seat."""
        issues = validate_students_import_batch(self.batch([
            self.row(**{"First Name": name, "Class": "JSS1 B"})
            for name in ("Ada", "Bola", "Chidi")
        ]))
        self.assertTrue(
            [i for i in issues if i["severity"] == "warning" and "capacity" in i["message"]],
            issues,
        )

    def test_another_schools_list_does_not_turn_this_schools_rows_into_applicants(self):
        self.require(BIRTH, tenant=self.solo.tenant, actor=self.solo_admin)
        issues = validate_students_import_batch(self.batch([self.row()]))
        self.assertFalse(
            [i for i in issues if "imported as an applicant" in i["message"]], issues,
        )


class StudentsTemplateGuidanceTests(_ImportFixture):
    """The template says that a school requiring documents gets applicants."""

    SENTENCE = "every row comes in as an applicant instead"

    def migration(self):
        import importlib

        return importlib.import_module(
            "vs_import_data.migrations.0023_students_template_imports_applicants",
        )

    def instructions(self):
        from vs_import_data.models import ImportTemplate

        return ImportTemplate.objects.get(code="students_v1").instructions

    def test_the_guidance_carries_the_sentence_once(self):
        from django.apps import apps

        self.assertEqual(self.instructions().count(self.SENTENCE), 1)
        self.migration().forwards(apps, None)
        self.assertEqual(self.instructions().count(self.SENTENCE), 1)

    def test_going_back_takes_it_out_and_leaves_the_rest(self):
        from django.apps import apps

        before = self.instructions()
        self.migration().backwards(apps, None)
        after = self.instructions()
        self.assertNotIn(self.SENTENCE, after)
        self.assertIn("waits under Classes and transfers.", after)
        self.migration().forwards(apps, None)
        self.assertEqual(self.instructions(), before)


class ConfirmDocumentsDescriptionTests(StudentsFixture):
    """The description beside the setting says what the list holds."""

    def description(self):
        from vs_config.models import ConfigurationDefinition

        return ConfigurationDefinition.objects.get(
            key="applicants.documents.required_to_confirm",
        ).description

    def test_it_names_the_direct_enrolment_and_the_import(self):
        text = self.description()
        self.assertIn("a child enrolled directly", text)
        self.assertIn("the student import brings each row in as an applicant", text)
        self.assertNotIn("is not checked", text)

    def test_the_migration_is_reversible(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module(
            "schools.vs_students.migrations.0009_confirm_documents_hold_enrolment",
        )
        before = self.description()
        migration.backwards(apps, None)
        self.assertEqual(self.description(), migration.AS_WRITTEN)
        migration.forwards(apps, None)
        self.assertEqual(self.description(), before)
