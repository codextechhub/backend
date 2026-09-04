"""Bulk import, the export dataset, documents and the admission-number policy.

FRD M11 v2.4 FR-012, FR-014, FR-015 and FR-019.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile

from schools.vs_students.constants import Relationship, StudentStatus
from schools.vs_students.imports import resolve_row, validate_students_import_batch
from schools.vs_students.models import Guardian, Student, StudentDocument

from .base import StudentsFixture


class _ImportFixture(StudentsFixture):
    """A batch shaped the way the engine hands one to a validator."""

    def batch(self, rows, *, tenant=None, branch=None):
        from vs_import_data.models import ImportBatch, ImportTemplate

        template = ImportTemplate.objects.get(code="students_v1")
        return ImportBatch.all_objects.create(
            tenant=tenant or self.tenant, branch=branch,
            template=template, dataset_type="students",
            preview_rows=rows, original_filename="roll.xlsx",
            uploaded_by=self.admin,
        )

    def row(self, **overrides):
        base = {
            "First Name": "Chiamaka", "Middle Name": "", "Last Name": "Nwosu",
            "Date of Birth": "2013-04-18", "Gender": "Female",
            "Admission No.": "", "Admission Date": "2025-09-08",
            # Brightfield has two branches, so every row must name one. A
            # spreadsheet carries the branch NAME, not an id: a school types
            # what it calls the place, and an id is not a thing it has.
            "Branch": self.lekki.name, "Class": "JSS1 A",
            "Guardian Name": "Mr. Chukwudi Nwosu",
            "Guardian Phone": "08035550101",
            "Guardian Email": "chukwudi@example.ng",
            "Guardian Relationship": "Father",
            "Home Address": "14 Admiralty Way", "Previous School": "",
        }
        base.update(overrides)
        return base


class ImportOwnershipTests(_ImportFixture):
    def test_students_is_a_dataset_a_school_may_import(self):
        from vs_import_data.datasets import may_import, platform_only

        self.assertFalse(platform_only("students"))
        self.assertTrue(may_import(self.admin, "students"))

    def test_the_module_import_key_reaches_the_wizard(self):
        """The engine's bridge was hard-coded to bank statements.

        Before the registry, a school administrator holding
        ``school.students.import`` was refused however the key was granted, and
        it read as a seeding problem rather than a hard-coded branch.
        """
        from vs_import_data.permissions import _DATASET_IMPORT_KEYS

        self.assertEqual(
            _DATASET_IMPORT_KEYS.get("students"), "school.students.import",
        )
        self.assertEqual(
            _DATASET_IMPORT_KEYS.get("bank_statements"),
            "finance.bankaccount.import",
        )

    def test_the_template_exists_with_all_fifteen_columns(self):
        from vs_import_data.models import ImportTemplate

        template = ImportTemplate.objects.get(code="students_v1")
        fields = set(template.columns.values_list("target_field", flat=True))
        self.assertIn("branch", fields)
        self.assertIn("guardian_email", fields)
        self.assertEqual(len(fields), 15)


class ImportValidationTests(_ImportFixture):
    def test_a_clean_file_reports_no_error(self):
        issues = validate_students_import_batch(self.batch([self.row()]))
        self.assertEqual([i for i in issues if i["severity"] == "error"], [])

    def test_a_class_the_school_does_not_have_is_a_hard_error(self):
        issues = validate_students_import_batch(
            self.batch([self.row(**{"Class": "JSS1 Z"})]),
        )
        errors = [i for i in issues if i["severity"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Academic Structure", errors[0]["message"])

    def test_a_missing_class_is_a_warning_and_the_student_still_imports(self):
        issues = validate_students_import_batch(
            self.batch([self.row(**{"Class": ""})]),
        )
        self.assertEqual([i for i in issues if i["severity"] == "error"], [])
        self.assertTrue(any(i["severity"] == "warning" for i in issues))

    def test_a_duplicate_against_an_existing_student_is_a_warning(self):
        """An import blocked by a real pair of siblings is one nobody can run."""
        self.student(first="Chiamaka", last="Nwosu")
        issues = validate_students_import_batch(self.batch([self.row()]))
        self.assertEqual([i for i in issues if i["severity"] == "error"], [])
        self.assertTrue(
            any("already on the roll" in i["message"] for i in issues),
        )

    def test_two_rows_with_the_same_admission_number_are_an_error(self):
        issues = validate_students_import_batch(self.batch([
            self.row(**{"Admission No.": "BFS/2025/0142"}),
            self.row(**{
                "Admission No.": "BFS/2025/0142", "First Name": "Somto",
            }),
        ]))
        errors = [i for i in issues if i["severity"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Row 1", errors[0]["message"])

    def test_any_number_of_rows_with_no_admission_number_pass(self):
        issues = validate_students_import_batch(self.batch([
            self.row(), self.row(**{"First Name": "Somto"}),
            self.row(**{"First Name": "Tobi"}),
        ]))
        self.assertEqual([i for i in issues if i["severity"] == "error"], [])

    def test_a_number_another_school_holds_imports_cleanly(self):
        """Per-school uniqueness, proven from the outside."""
        Student.all_objects.create(
            tenant=self.solo.tenant, branch=self.solo_branch,
            first_name="Theirs", last_name="Own",
            date_of_birth=self.row()["Date of Birth"], gender="FEMALE",
            student_number="BFS/2025/0142",
        )
        issues = validate_students_import_batch(
            self.batch([self.row(**{"Admission No.": "BFS/2025/0142"})]),
        )
        self.assertEqual([i for i in issues if i["severity"] == "error"], [])

    def test_a_branch_of_another_tenant_fails_like_one_that_does_not_exist(self):
        """One answer for all three, so the column cannot be used to enumerate."""
        theirs = validate_students_import_batch(
            self.batch([self.row(**{"Branch": self.solo_branch.name})]),
        )
        nowhere = validate_students_import_batch(
            self.batch([self.row(**{"Branch": "Nowhere At All"})]),
        )
        self.assertEqual(
            [i["message"] for i in theirs if i["severity"] == "error"][0].split("'")[-1],
            [i["message"] for i in nowhere if i["severity"] == "error"][0].split("'")[-1],
        )

    def test_a_class_at_another_branch_fails_before_anything_is_written(self):
        issues = validate_students_import_batch(self.batch([
            self.row(**{
                "Branch": self.lekki.name, "Class": self.ikeja_class.name,
            }),
        ]))
        errors = [i for i in issues if i["severity"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Ikeja", errors[0]["message"])
        self.assertEqual(Student.all_objects.count(), 0)

    def test_a_multi_branch_school_must_name_a_branch_on_every_row(self):
        issues = validate_students_import_batch(
            self.batch([self.row(**{"Branch": ""})]),
        )
        self.assertTrue(any(
            i["severity"] == "error" and "more than one branch" in i["message"]
            for i in issues
        ))


class ImportExecutionTests(_ImportFixture):
    def _execute(self, raw_row, *, branch=None):
        from vs_import_data.services.import_executor import (
            execute_dataset_handler, map_row_to_payload,
        )

        batch = self.batch([raw_row], branch=branch or self.lekki)
        payload = map_row_to_payload(batch, raw_row)
        return execute_dataset_handler(batch, payload, self.admin)

    def test_a_row_creates_a_student_a_guardian_and_a_placement(self):
        result = self._execute(self.row())
        student = result.instance
        self.assertEqual(student.status, StudentStatus.ACTIVE)
        self.assertEqual(student.branch, self.lekki)
        self.assertEqual(student.enrolments.filter(is_active=True).count(), 1)
        self.assertEqual(student.guardian_links.filter(is_primary=True).count(), 1)

    def test_three_rows_naming_one_guardian_email_create_one_guardian(self):
        for name in ("Chiamaka", "Somto", "Tobi"):
            self._execute(self.row(**{"First Name": name}))
        self.assertEqual(
            Guardian.all_objects.filter(tenant=self.tenant).count(), 1,
        )
        self.assertEqual(
            Student.all_objects.filter(tenant=self.tenant).count(), 3,
        )

    def test_a_row_with_no_class_lands_enrolled_and_unplaced(self):
        result = self._execute(self.row(**{"Class": ""}))
        self.assertEqual(result.instance.status, StudentStatus.ENROLLED)
        self.assertEqual(result.instance.enrolments.count(), 0)

    def test_an_unknown_relationship_is_imported_as_other(self):
        result = self._execute(
            self.row(**{"Guardian Relationship": "Family friend"}),
        )
        self.assertEqual(
            result.instance.guardian_links.first().relationship, "OTHER",
        )


class AdmissionPolicyTests(StudentsFixture):
    """A school's own rule, defaulting to the permissive behaviour."""

    def _set(self, **body):
        """Set the school's rule, refusing to continue if the write failed.

        Asserting the status here is what stops a policy test passing because
        the setting never landed.
        """
        response = self.put(self.admin, "student-admission-policy", body)
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def test_a_school_with_no_policy_may_enrol_without_a_number(self):
        response = self.post(self.admin, "student-list", self.enrolment_body())
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_required_policy_refuses_an_enrolment_with_no_number(self):
        self._set(
            required=True, pattern="",
            hint="Give the child their BFS number.",
        )
        response = self.post(self.admin, "student-list", self.enrolment_body())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.data["error"]["code"], "ADMISSION_NUMBER_REQUIRED",
        )
        self.assertIn("BFS number", response.data["message"])

    def test_a_pattern_refuses_a_number_that_does_not_match(self):
        self._set(
            required=True, pattern=r"BFS/\d{4}/\d{4}",
            hint="Use the BFS/YYYY/NNNN format.",
        )
        response = self.post(
            self.admin, "student-list",
            self.enrolment_body(student_number="CSS-24-0117"),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.data["error"]["code"], "ADMISSION_NUMBER_FORMAT",
        )
        self.assertEqual(response.data["message"], "Use the BFS/YYYY/NNNN format.")

    def test_the_refusal_quotes_the_hint_and_never_the_expression(self):
        self._set(
            required=True, pattern=r"BFS/\d{4}/\d{4}",
            hint="Use the BFS/YYYY/NNNN format.",
        )
        response = self.post(
            self.admin, "student-list",
            self.enrolment_body(student_number="nope"),
        )
        self.assertNotIn("\\d", response.data["message"])

    def test_the_pattern_is_anchored_so_it_cannot_match_a_substring(self):
        self._set(required=True, pattern=r"BFS/\d{4}/\d{4}", hint="hint")
        response = self.post(
            self.admin, "student-list",
            self.enrolment_body(student_number="BFS/2025/0142XYZ"),
        )
        self.assertEqual(response.status_code, 422)

    def test_a_matching_number_is_accepted(self):
        self._set(required=True, pattern=r"BFS/\d{4}/\d{4}", hint="hint")
        response = self.post(
            self.admin, "student-list",
            self.enrolment_body(student_number="BFS/2025/0142"),
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_an_uncompilable_pattern_is_refused_on_write(self):
        """Refused here rather than discovered at the next enrolment.

        Stored, it would look like a broken enrolment form rather than a
        setting somebody mistyped.
        """
        response = self.put(
            self.admin, "student-admission-policy",
            {"required": True, "pattern": "BFS/[", "hint": "x"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.data["error"]["code"], "INVALID_ADMISSION_PATTERN",
        )

    def test_two_schools_validate_against_their_own_rule(self):
        self._set(required=True, pattern=r"BFS/\d{4}/\d{4}", hint="BFS")
        # Sunrise has set nothing, so it keeps the permissive behaviour.
        from schools.vs_students.services.policy import read_policy

        self.assertTrue(read_policy(self.tenant).required)
        self.assertFalse(read_policy(self.solo.tenant).required)

    def test_a_number_stored_before_a_pattern_existed_stays_editable(self):
        """Validation happens on write only, never retrospectively."""
        row = self.student(number="OLD-001")
        self._set(required=True, pattern=r"BFS/\d{4}/\d{4}", hint="BFS")
        response = self.patch(
            self.admin, "student-detail", {"first_name": "Renamed"}, pk=row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        row.refresh_from_db()
        self.assertEqual(row.student_number, "OLD-001")


class DocumentTests(StudentsFixture):
    def setUp(self):
        self.row = self.student()

    def _upload(self, doc_type="BIRTH_CERTIFICATE", user=None):
        return self.client_for(user or self.admin).post(
            f"/v1/students/{self.row.pk}/documents/?tenant={self.tenant.slug}",
            {
                "document_type": doc_type,
                "file": SimpleUploadedFile("b.pdf", b"bytes", "application/pdf"),
            },
            format="multipart",
        )

    def test_the_checklist_shows_all_five_types_attached_or_not(self):
        response = self.get(self.admin, "student-documents", pk=self.row.pk)
        self.assertEqual(len(response.data["data"]), 5)
        self.assertTrue(all(not r["attached"] for r in response.data["data"]))
        # The birth certificate alone. The passport photograph used to be
        # prompted for too, and is not: a school photographs its intake on a
        # day it chooses, so marking every new child incomplete for a missing
        # picture teaches everybody to ignore the mark.
        required = {r["document_type"] for r in response.data["data"] if r["required"]}
        self.assertEqual(required, {"BIRTH_CERTIFICATE"})

    def test_attaching_records_the_file_and_returns_a_signed_url(self):
        response = self._upload()
        self.assertEqual(response.status_code, 201, response.data)
        row = next(
            r for r in response.data["data"]
            if r["document_type"] == "BIRTH_CERTIFICATE"
        )
        self.assertTrue(row["attached"])
        # Signed and user-bound, never a bare /media/ path: an unsigned path
        # inside its window is a bearer token.
        self.assertIn("t=", row["url"])

    def test_an_unknown_document_type_is_refused_per_field(self):
        response = self._upload(doc_type="SOMETHING_ELSE")
        self.assertEqual(response.status_code, 400)
        self.assertIn("document_type", response.data["error"]["detail"])

    def test_a_missing_required_document_never_blocks_an_enrolment(self):
        response = self.post(self.admin, "student-list", self.enrolment_body())
        self.assertEqual(response.status_code, 201, response.data)

    def test_removing_a_document_of_another_tenants_student_is_404(self):
        self._upload()
        doc = StudentDocument.all_objects.get(student=self.row)
        theirs = Student.all_objects.create(
            tenant=self.solo.tenant, branch=self.solo_branch,
            first_name="Theirs", last_name="Own",
            date_of_birth=self.row.date_of_birth, gender="FEMALE",
        )
        response = self.delete(
            self.admin, "student-document-detail",
            pk=theirs.pk, doc_id=doc.pk,
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(StudentDocument.all_objects.filter(pk=doc.pk).exists())


class ExportDatasetTests(StudentsFixture):
    def test_the_dataset_is_registered_and_tenant_fenced(self):
        from vs_exports.catalogue import get_dataset

        dataset = get_dataset("school.students")
        self.assertIsNotNone(dataset)
        self.assertEqual(dataset.permission, "school.students.export")

    def test_no_medical_field_can_be_exported_by_anyone(self):
        """A gate protects a screen; a file leaves the building."""
        from vs_exports.catalogue import get_dataset

        fields = {f.id for f in get_dataset("school.students").fields}
        for banned in (
            "blood_group", "allergies", "conditions",
            "emergency_contact_name", "emergency_contact_phone",
        ):
            self.assertNotIn(banned, fields)

    def test_a_childs_name_and_birthday_are_declared_sensitive(self):
        from vs_exports.catalogue import get_dataset

        sensitive = {
            f.id for f in get_dataset("school.students").fields if f.sensitive
        }
        for expected in ("first_name", "last_name", "date_of_birth", "address"):
            self.assertIn(expected, sensitive)


class ImportCatchesWhatTheFormCatchesTests(_ImportFixture):
    """Every fault the enrol screen refuses, the file is refused for too.

    A form and a file that disagree are two sets of rules, and the file is the
    one that writes hundreds of rows at a time. Each test below names the fault
    a school actually makes in a spreadsheet.
    """

    def _errors(self, **overrides):
        issues = validate_students_import_batch(self.batch([self.row(**overrides)]))
        return [i for i in issues if i["severity"] == "error"]

    def _warnings(self, rows):
        issues = validate_students_import_batch(self.batch(rows))
        return [i for i in issues if i["severity"] == "warning"]

    # ── the year, which is the digit a spreadsheet gets wrong ──────────────

    def test_a_birth_year_that_makes_the_child_an_adult_is_refused(self):
        """1998 typed for 2008 puts a 28-year-old on a school roll."""
        errors = self._errors(**{"Date of Birth": "1994-04-18"})
        self.assertEqual(len(errors), 1)
        self.assertIn("over 25", errors[0]["message"])

    def test_a_birth_year_that_makes_the_child_a_baby_is_refused(self):
        errors = self._errors(**{"Date of Birth": "2025-04-18"})
        self.assertEqual(len(errors), 1)
        self.assertIn("under 2", errors[0]["message"])

    def test_a_school_age_child_passes(self):
        self.assertEqual(self._errors(**{"Date of Birth": "2013-04-18"}), [])

    # ── the admission date, which no form can get wrong ────────────────────

    def test_an_admission_date_in_the_future_is_refused(self):
        errors = self._errors(**{"Admission Date": "2099-01-06"})
        self.assertEqual(len(errors), 1)
        self.assertIn("future", errors[0]["message"])

    def test_an_admission_date_before_the_child_was_born_is_refused(self):
        errors = self._errors(**{
            "Date of Birth": "2013-04-18", "Admission Date": "2010-09-08",
        })
        self.assertEqual(len(errors), 1)
        self.assertIn("before the student was born", errors[0]["message"])

    # ── values that would explode at the write ─────────────────────────────

    def test_a_value_too_long_for_its_column_is_refused_before_anything_is_written(self):
        """Otherwise it is a DataError with half the file already committed."""
        errors = self._errors(**{"First Name": "A" * 120})
        self.assertEqual(len(errors), 1)
        self.assertIn("limit is 100", errors[0]["message"])

    # ── the guardian columns, which decide which household a child joins ───

    def test_a_guardian_email_that_is_not_an_address_is_refused(self):
        """Guardians are matched on it, so a broken value splits a family."""
        errors = self._errors(**{"Guardian Email": "chukwudi at example"})
        self.assertEqual(len(errors), 1)
        self.assertIn("not an email address", errors[0]["message"])

    def test_a_phone_too_short_to_ring_is_refused(self):
        errors = self._errors(**{"Guardian Phone": "JSS1 A"})
        self.assertEqual(len(errors), 1)
        self.assertIn("could ring", errors[0]["message"])

    def test_two_rows_sharing_a_contact_under_different_names_are_warned_about(self):
        """The second name is discarded and both children join one household.

        Right for siblings, and wrong for a mistyped address, and the school is
        the only one who can tell which.
        """
        warnings = self._warnings([
            self.row(),
            self.row(**{
                "First Name": "Somto", "Guardian Name": "Mrs. Ada Eze",
            }),
        ])
        self.assertTrue(
            any("will not be recorded" in w["message"] for w in warnings),
            warnings,
        )

    def test_siblings_sharing_a_contact_under_one_name_are_not_warned_about(self):
        warnings = self._warnings([
            self.row(), self.row(**{"First Name": "Somto"}),
        ])
        self.assertFalse(
            any("will not be recorded" in w["message"] for w in warnings),
            warnings,
        )

    def test_a_contact_the_school_already_holds_under_another_name_is_warned_about(self):
        self.guardian(name="Mrs. Somebody Else", email="chukwudi@example.ng",
                      phone="08099998888")
        warnings = self._warnings([self.row()])
        self.assertTrue(
            any("Mrs. Somebody Else" in w["message"] for w in warnings), warnings,
        )

    # ── the fault a form cannot have ───────────────────────────────────────

    def test_a_file_that_overfills_a_class_warns_once_and_names_the_count(self):
        """Typing the 31st child into a class of 30 is refused on the spot.

        A file naming that class forty times passes every row's own checks,
        because no row is wrong on its own.
        """
        # row() names "JSS1 A", the school-wide class.
        self.shared_class.capacity = 2
        self.shared_class.save(update_fields=["capacity"])
        warnings = self._warnings([
            self.row(**{"First Name": "One", "Guardian Email": "a@example.ng",
                        "Guardian Phone": "08000000001"}),
            self.row(**{"First Name": "Two", "Guardian Email": "b@example.ng",
                        "Guardian Phone": "08000000002"}),
            self.row(**{"First Name": "Three", "Guardian Email": "c@example.ng",
                        "Guardian Phone": "08000000003"}),
        ])
        overfull = [w for w in warnings if "over its capacity" in w["message"]]
        self.assertEqual(len(overfull), 1, warnings)
        self.assertIn("holds 2", overfull[0]["message"])

    def test_a_file_inside_capacity_says_nothing(self):
        self.shared_class.capacity = 30
        self.shared_class.save(update_fields=["capacity"])
        warnings = self._warnings([self.row()])
        self.assertEqual(
            [w for w in warnings if "capacity" in w["message"]], [],
        )

    # ── a shifted column, which is one mistake and many symptoms ───────────

    def test_a_digit_in_a_name_is_warned_about(self):
        warnings = self._warnings([self.row(**{"First Name": "Chiamaka 3"})])
        self.assertTrue(
            any("columns line up" in w["message"] for w in warnings), warnings,
        )


class GuardianImportTests(_ImportFixture):
    """Households: the second parent nothing else can add in bulk.

    The tests that earn their place are the primary-contact ones. Linking a new
    primary DEMOTES the old one without a word, so a file can move who the
    school calls first and nobody would know - and no single row can see that
    happening.
    """

    def setUp(self):
        super().setUp()
        from schools.vs_students.services.guardians import link, upsert_guardian

        self.amaka = self.student(first="Amaka", last="Adeleke", number="BFS/1")
        self.mother, _ = upsert_guardian(
            self.tenant, full_name="Mrs. Tolu Adeleke", phone="08035550101",
            email="tolu@example.ng",
        )
        link(
            self.amaka, self.mother, relationship=Relationship.MOTHER,
            is_primary=True, actor=self.admin,
        )

    def grow(self, **overrides):
        base = {
            "Guardian Name": "Mr. Emeka Adeleke",
            "Guardian Phone": "08035550102",
            "Guardian Email": "emeka@example.ng",
            "Admission Number": "BFS/1",
            "Student First Name": "", "Student Last Name": "",
            "Student Date of Birth": "",
            "Relationship": "Father", "Primary Contact": "No",
            "Occupation": "", "Home Address": "",
        }
        base.update(overrides)
        return base

    def gbatch(self, rows):
        from vs_import_data.models import ImportBatch, ImportTemplate

        return ImportBatch.all_objects.create(
            tenant=self.tenant,
            template=ImportTemplate.objects.get(code="guardians_v1"),
            dataset_type="guardians", preview_rows=rows,
            original_filename="households.xlsx", uploaded_by=self.admin,
        )

    def gissues(self, rows, severity):
        from schools.vs_students.guardian_imports import (
            validate_guardians_import_batch,
        )

        return [
            i for i in validate_guardians_import_batch(self.gbatch(rows))
            if i["severity"] == severity
        ]

    def gerrors(self, rows):
        return self.gissues(rows, "error")

    def gwarnings(self, rows):
        return self.gissues(rows, "warning")

    # ── identifying the child ──────────────────────────────────────────────

    def test_a_clean_row_reports_no_error(self):
        self.assertEqual(self.gerrors([self.grow()]), [])

    def test_an_admission_number_no_student_holds_is_refused(self):
        errors = self.gerrors([self.grow(**{"Admission Number": "BFS/999"})])
        self.assertEqual(len(errors), 1)
        self.assertIn("BFS/999", errors[0]["message"])

    def test_a_child_named_ambiguously_is_refused_rather_than_guessed(self):
        """Two real children share a name and a birthday.

        Picking the first would attach a father to the wrong family in silence.
        """
        self.student(first="Amaka", last="Adeleke", dob=self.amaka.date_of_birth)
        errors = self.gerrors([self.grow(**{
            "Admission Number": "", "Student First Name": "Amaka",
            "Student Last Name": "Adeleke",
            "Student Date of Birth": str(self.amaka.date_of_birth),
        })])
        self.assertEqual(len(errors), 1)
        self.assertIn("admission number", errors[0]["message"])

    def test_naming_no_child_at_all_is_refused(self):
        errors = self.gerrors([self.grow(**{"Admission Number": ""})])
        self.assertEqual(len(errors), 1)
        self.assertIn("Say which child", errors[0]["message"])

    # ── the primary contact ────────────────────────────────────────────────

    def test_moving_the_primary_contact_is_warned_about_and_names_who_loses_it(self):
        """The change is silent otherwise: link() demotes without a word."""
        warnings = self.gwarnings([self.grow(**{"Primary Contact": "Yes"})])
        self.assertTrue(
            any("Mrs. Tolu Adeleke" in w["message"] and "moves it" in w["message"]
                for w in warnings),
            warnings,
        )

    def test_two_rows_claiming_primary_for_one_child_are_refused(self):
        """The second would silently win, so the file contradicts itself."""
        errors = self.gerrors([
            self.grow(**{"Primary Contact": "Yes"}),
            self.grow(**{
                "Guardian Name": "Mrs. Ada Eze", "Guardian Phone": "08035550103",
                "Guardian Email": "ada@example.ng", "Primary Contact": "Yes",
            }),
        ])
        self.assertEqual(len(errors), 1)
        self.assertIn("disagree", errors[0]["message"])

    def test_a_child_given_contacts_and_no_primary_is_warned_about(self):
        orphan = self.student(first="Ngozi", last="Umeh", number="BFS/2")
        warnings = self.gwarnings([self.grow(**{
            "Admission Number": "BFS/2", "Primary Contact": "No",
        })])
        self.assertTrue(
            any("no primary contact" in w["message"] for w in warnings),
            warnings,
        )
        self.assertTrue(orphan.pk)

    # ── the household ──────────────────────────────────────────────────────

    def test_the_primary_check_does_not_depend_on_row_order(self):
        """The primary named on row 1, a second contact on row 2.

        Read one row at a time, row 2 looks unprimaried: nothing is written
        yet, so the database still says None and the file would be warned about
        a child it has already settled.
        """
        orphan = self.student(first="Ngozi", last="Umeh", number="BFS/2")
        warnings = self.gwarnings([
            self.grow(**{
                "Admission Number": "BFS/2", "Primary Contact": "Yes",
                "Guardian Name": "Mr. A", "Guardian Phone": "08000000001",
                "Guardian Email": "a@example.ng",
            }),
            self.grow(**{
                "Admission Number": "BFS/2", "Primary Contact": "No",
                "Guardian Name": "Mrs. B", "Guardian Phone": "08000000002",
                "Guardian Email": "b@example.ng",
            }),
        ])
        self.assertEqual(
            [w for w in warnings if "no primary contact" in w["message"]], [],
            warnings,
        )
        self.assertTrue(orphan.pk)

    def test_a_pair_already_linked_is_skipped_not_linked_twice(self):
        warnings = self.gwarnings([self.grow(**{
            "Guardian Name": "Mrs. Tolu Adeleke",
            "Guardian Phone": "08035550101",
            "Guardian Email": "tolu@example.ng",
        })])
        self.assertTrue(
            any("already linked" in w["message"] for w in warnings), warnings,
        )

    def test_the_same_pair_twice_in_one_file_is_refused(self):
        errors = self.gerrors([self.grow(), self.grow()])
        self.assertEqual(len(errors), 1)
        self.assertIn("only be linked once", errors[0]["message"])

    def test_a_contact_the_school_holds_under_another_name_is_warned_about(self):
        warnings = self.gwarnings([self.grow(**{
            "Guardian Name": "Somebody Else",
            "Guardian Phone": "08035550101",
            "Guardian Email": "tolu@example.ng",
            "Admission Number": "BFS/1",
        })])
        self.assertTrue(
            any("Mrs. Tolu Adeleke" in w["message"] for w in warnings), warnings,
        )

    def test_a_phone_too_short_to_ring_is_refused(self):
        errors = self.gerrors([self.grow(**{"Guardian Phone": "123"})])
        self.assertEqual(len(errors), 1)
        self.assertIn("could ring", errors[0]["message"])

    # ── writing ────────────────────────────────────────────────────────────

    def test_it_creates_the_guardian_and_the_link(self):
        from schools.vs_students.guardian_imports import build_links, resolve_file

        resolved = resolve_file(
            [{
                "guardian_full_name": "Mr. Emeka Adeleke",
                "guardian_phone": "08035550102",
                "guardian_email": "emeka@example.ng",
                "student_number": "BFS/1",
                "student_first_name": "", "student_last_name": "",
                "student_date_of_birth": "",
                "relationship": "Father", "is_primary": "No",
                "occupation": "", "address": "",
            }],
            tenant=self.tenant,
        )
        counts = build_links(resolved, tenant=self.tenant, actor=self.admin)
        self.assertEqual(counts, {"guardians": 1, "links": 1})
        self.assertEqual(
            self.amaka.guardian_links.count(), 2,
        )
        # The mother keeps primary: the row did not claim it.
        self.assertEqual(
            self.amaka.guardian_links.get(is_primary=True).guardian,
            self.mother,
        )

    def test_the_template_exists_with_all_eleven_columns(self):
        from vs_import_data.models import ImportTemplate

        from schools.vs_students.guardian_imports import COLUMNS

        template = ImportTemplate.objects.get(code="guardians_v1")
        fields = set(template.columns.values_list("target_field", flat=True))
        self.assertEqual(fields, set(COLUMNS))

    def test_a_school_may_import_its_own_households(self):
        from vs_import_data.datasets import platform_only

        self.assertFalse(platform_only("guardians"))
