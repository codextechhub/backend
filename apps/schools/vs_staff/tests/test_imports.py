"""Loading a school's staff from a spreadsheet.

FRD M12 v2.1, FR-016.

The case every test here is built around: a school downloads the template,
fills it in without changing a thing, and uploads it back. That file has to
validate clean, and for a while it did not - see
``ValidationReadsTheFileTests``.
"""
from __future__ import annotations

from vs_import_data.models import (
    DatasetTypeChoices,
    ImportBatch,
    ImportTemplate,
    ImportTemplateColumn,
    TemplateColumnDataTypeChoices,
    TemplateStatusChoices,
)

from schools.vs_staff.imports import COLUMNS, REQUIRED_COLUMNS, validate_rows

from .base import StaffFixture

#: The template's column headings, and the field each one feeds.
#:
#: They DIFFER on purpose, and the difference is the whole point of these
#: tests: a spreadsheet says "First Name" and the resolver reads `first_name`.
#: A fixture whose headings matched its target fields would pass whether or not
#: the translation happened, which is exactly how the defect survived.
HEADINGS = {
    "first_name": "First Name",
    "middle_name": "Middle Name",
    "last_name": "Last Name",
    "email": "Email",
    "phone": "Phone",
    "gender": "Gender",
    "staff_number": "Staff ID",
    "job_title": "Job Title",
    "employment_type": "Employment Type",
    "hire_date": "Hire Date",
    "branch": "Branch",
    "role": "Role",
}


class _ImportFixture(StaffFixture):
    """A staff template shaped like the seeded one, and a batch against it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.template = ImportTemplate.objects.create(
            code="staff_test_v1",
            name="Staff Import",
            dataset_type=DatasetTypeChoices.STAFF,
            status=TemplateStatusChoices.ACTIVE,
        )
        for order, field in enumerate(COLUMNS):
            ImportTemplateColumn.objects.create(
                template=cls.template,
                column_name=HEADINGS[field],
                target_field=field,
                display_name=HEADINGS[field],
                data_type=TemplateColumnDataTypeChoices.STRING,
                is_required=field in REQUIRED_COLUMNS,
                column_order=order,
            )

    def batch(self, *rows):
        """A batch carrying rows keyed the way an uploaded file keys them."""
        return ImportBatch.all_objects.create(
            tenant=self.tenant,
            uploaded_by=self.admin,
            template=self.template,
            dataset_type=DatasetTypeChoices.STAFF,
            original_filename="staff.csv",
            preview_rows=list(rows),
        )

    def row(self, **overrides):
        """One good row, written under the template's HEADINGS."""
        values = {
            "First Name": "Ifeoma",
            "Middle Name": "",
            "Last Name": "Anyanwu",
            "Email": "ifeoma.anyanwu@brightfield.test",
            "Phone": "0803 555 7001",
            "Gender": "FEMALE",
            "Staff ID": "BFS/IMP/001",
            "Job Title": "Teacher",
            "Employment Type": "Full-time",
            "Hire Date": "2023-09-04",
            "Branch": "",
            "Role": "teacher",
        }
        values.update(overrides)
        return values


class ValidationReadsTheFileTests(_ImportFixture):
    """The pass that decides whether anything may be written at all.

    An uploaded row is keyed by the file's HEADERS. The resolver reads target
    fields. The engine translates between them at execution time, and the
    validator has to do the same or the two passes read one row two ways.

    It did not. Every lookup missed, every required field read as empty, and a
    school uploading the template it had just downloaded was told to fix twelve
    errors in a perfect file - which, since errors block a batch, meant the
    staff import could never import anybody.
    """

    def test_a_clean_file_validates_with_nothing_to_fix(self):
        issues = validate_rows(self.batch(self.row()))
        self.assertEqual(issues, [], issues)

    def test_every_row_of_a_clean_file_passes_not_just_the_first(self):
        issues = validate_rows(
            self.batch(
                self.row(),
                self.row(**{
                    "First Name": "Sunday",
                    "Last Name": "Ekpo",
                    "Email": "sunday.ekpo@brightfield.test",
                    "Staff ID": "BFS/IMP/002",
                }),
            ),
        )
        self.assertEqual(issues, [], issues)

    def test_an_issue_names_the_column_the_way_the_file_does(self):
        """A reader is looking at a spreadsheet, not at a field name.

        "column: first_name" sends somebody hunting for a heading their file
        does not have.
        """
        issues = validate_rows(self.batch(self.row(**{"First Name": ""})))
        self.assertEqual([i["column_name"] for i in issues], ["First Name"])


class RowRefusalTests(_ImportFixture):
    def test_a_missing_required_field_is_an_error(self):
        issues = validate_rows(self.batch(self.row(**{"Email": ""})))
        self.assertEqual(
            [(i["code"], i["column_name"]) for i in issues],
            [("required", "Email")],
        )

    def test_a_role_this_school_does_not_have_is_refused(self):
        """A hard error, not a warning: there is no invite-now-decide-later.

        Somebody created without a role has an account that signs in and
        reaches nothing, and no screen would explain why.
        """
        issues = validate_rows(self.batch(self.row(**{"Role": "caretaker"})))
        self.assertTrue(issues)
        self.assertEqual(issues[0]["column_name"], "Role")
        self.assertEqual(issues[0]["severity"], "error")

    def test_the_same_address_twice_in_one_file_names_the_earlier_row(self):
        """Two rows that each pass alone would create one account, then fail."""
        issues = validate_rows(self.batch(self.row(), self.row()))
        duplicates = [i for i in issues if i["code"] == "duplicate_in_file"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["row_number"], 2)
        self.assertIn("row 1", duplicates[0]["message"])
        self.assertEqual(duplicates[0]["column_name"], "Email")

    def test_a_batch_with_no_template_is_not_an_exception(self):
        """Nothing to translate through, so nothing to say about the rows."""
        batch = self.batch(self.row())
        batch.template = None
        batch.save(update_fields=["template"])
        self.assertEqual(validate_rows(batch), [])
