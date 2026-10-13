"""A spreadsheet of staff applies the runner's Field Access.

Adding a member of staff on the Add form is judged field by field against the
registered ``school.teachers`` switches. An uploaded file reaches the same
records without that form, so each non-blank cell is asked the same question:
a field the runner may not write is refused unless it is declared open on
create (the name and the sign-in address), and a blank cell is dropped.

Brightfield has two branches and Sunrise one, and a switch belongs to a role
and a role to one school, so nothing one school closes reaches the other.
"""
from __future__ import annotations

from unittest import mock

from schools.vs_staff.imports import validate_rows
from schools.vs_staff.models import StaffProfile
from vs_import_data.models import (
    ImportBatch,
    ImportJobRowResult,
    ImportRowActionChoices,
)
from vs_import_data.services.import_executor import (
    execute_dataset_handler,
    execute_import,
    map_row_to_payload,
)
from vs_rbac.field_enforcement import FieldWriteDenied
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)
from vs_user.models import User

from .base import ALL_KEYS
from .test_imports import _ImportFixture

PHONE = "school.teachers.phone"
HIRE_DATE = "school.teachers.hire_date"
JOB_TITLE = "school.teachers.job_title"
STAFF_NUMBER = "school.teachers.staff_number"


class _Fixture(_ImportFixture):
    """Six runners per school, each closed on a different set of fields."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields("school.teachers")
        closures = {
            "open": {},
            "phone_ro": {"read_only": (PHONE,)},
            "job_blind": {"closed": (JOB_TITLE,)},
            "number_ro": {"read_only": (STAFF_NUMBER,)},
            "two_ro": {"read_only": (PHONE, HIRE_DATE)},
            "create_only": {"read_only": tuple(cls.field_keys)},
        }
        cls.runners = {
            label: {
                name: cls.runner(school, label, name, **closed)
                for name, closed in closures.items()
            }
            for label, school in (("multi", cls.school), ("single", cls.solo))
        }
        cls.shapes = {
            "multi": (cls.tenant, cls.lekki, "ifeoma@brightfield.test"),
            "single": (cls.solo.tenant, cls.solo_branch, "ifeoma@sunrise.test"),
        }

    @classmethod
    def runner(cls, school, label, name, *, closed=(), read_only=()):
        role = make_role(school, name=f"{name} {label}", key=f"imp_{name}_{label}")
        for key in ALL_KEYS:
            make_role_permission(role, cls.permissions[key])
        if closed:
            set_field_access(role, *closed, read=False, write=False)
        if read_only:
            set_field_access(role, *read_only, read=True, write=False)
        user = make_school_admin(
            None, email=f"{name}.{label}@staff-import-fa.test", tenant=school.tenant,
        )
        make_assignment(school, user, role, branch=None)
        return user

    def cells(self, label, **overrides):
        """One good row for the school of *label*, written under the template's headings."""
        _, _, email = self.shapes[label]
        values = {"Email": email}
        values.update(overrides)
        return self.row(**values)

    def batch_for(self, rows, label, who):
        tenant, branch, _ = self.shapes[label]
        return ImportBatch.all_objects.create(
            tenant=tenant, branch=branch, template=self.template,
            dataset_type="staff", preview_rows=list(rows),
            original_filename="staff.csv", uploaded_by=self.runners[label][who],
        )

    def import_row(self, label, who, **cells):
        raw_row = self.cells(label, **cells)
        batch = self.batch_for([raw_row], label, who)
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            return execute_dataset_handler(
                batch, map_row_to_payload(batch, raw_row), self.runners[label][who],
            )

    def preview(self, label, who, **cells):
        batch = self.batch_for([self.cells(label, **cells)], label, who)
        return validate_rows(batch, actor=self.runners[label][who])

    @staticmethod
    def refusals(issues):
        return [i for i in issues if i.get("metadata", {}).get("field_write_denied")]

    def counts_of(self, tenant):
        return (
            User.objects.filter(tenant=tenant).count(),
            StaffProfile.all_objects.filter(tenant=tenant).count(),
        )


class StaffWriteSwitchTests(_Fixture):
    def test_a_field_the_runner_may_not_write_is_refused_and_nothing_is_written(self):
        for label, (tenant, _, _) in self.shapes.items():
            with self.subTest(school=label):
                before = self.counts_of(tenant)
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(label, "phone_ro")
                self.assertEqual(caught.exception.fields, ["phone"])
                self.assertEqual(
                    caught.exception.message,
                    "You do not have permission to change Phone.",
                )
                self.assertEqual(self.counts_of(tenant), before)

    def test_a_field_the_runner_cannot_even_read_is_refused_the_same_way(self):
        for label, (tenant, _, _) in self.shapes.items():
            with self.subTest(school=label):
                before = self.counts_of(tenant)
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(label, "job_blind")
                self.assertEqual(caught.exception.fields, ["job_title"])
                self.assertEqual(self.counts_of(tenant), before)

    def test_every_refused_field_of_a_row_is_named_at_once(self):
        for label in self.shapes:
            with self.subTest(school=label):
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(label, "two_ro")
                self.assertEqual(caught.exception.fields, ["hire_date", "phone"])
                self.assertIn("Hire date", caught.exception.message)
                self.assertIn("Phone", caught.exception.message)

    def test_the_staff_number_is_judged_like_any_other_cell(self):
        for label in self.shapes:
            with self.subTest(school=label):
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(label, "number_ro")
                self.assertEqual(caught.exception.fields, ["staff_number"])

    def test_a_blank_cell_is_dropped_not_refused(self):
        for label, (tenant, _, _) in self.shapes.items():
            with self.subTest(school=label):
                result = self.import_row(
                    label, "phone_ro", Phone="", **{"Hire Date": ""},
                )
                self.assertEqual(result.action, ImportRowActionChoices.CREATE)
                self.assertTrue(
                    StaffProfile.all_objects.filter(
                        tenant=tenant, user__email=self.shapes[label][2],
                    ).exists()
                )

    def test_the_name_and_the_sign_in_address_are_open_on_create(self):
        """A runner with Write off on every field still loads a file that sets only those."""
        blank = {
            "Middle Name": "", "Phone": "", "Gender": "", "Staff ID": "",
            "Job Title": "", "Employment Type": "", "Hire Date": "",
        }
        for label in self.shapes:
            with self.subTest(school=label):
                result = self.import_row(label, "create_only", **blank)
                self.assertEqual(result.action, ImportRowActionChoices.CREATE)

    def test_a_runner_with_write_off_everywhere_is_refused_only_the_cells_that_are_not_open(self):
        for label in self.shapes:
            with self.subTest(school=label):
                with self.assertRaises(FieldWriteDenied) as caught:
                    self.import_row(label, "create_only")
                self.assertEqual(
                    caught.exception.fields,
                    ["employment_type", "gender", "hire_date", "job_title", "phone",
                     "staff_number"],
                )

    def test_columns_that_are_not_registered_details_are_never_refused(self):
        """The branch and the invitation choice are not switched fields of the person."""
        blank = {
            "Middle Name": "", "Phone": "", "Gender": "", "Staff ID": "",
            "Job Title": "", "Employment Type": "", "Hire Date": "",
        }
        for label in self.shapes:
            with self.subTest(school=label):
                branch = self.shapes[label][1].name
                result = self.import_row(
                    label, "create_only", Branch=branch, **{"Send Invitation": "No"},
                    **blank,
                )
                self.assertEqual(result.action, ImportRowActionChoices.CREATE)

    def test_a_runner_who_may_write_everything_loads_the_whole_row(self):
        for label, (tenant, _, _) in self.shapes.items():
            with self.subTest(school=label):
                before = self.counts_of(tenant)
                result = self.import_row(label, "open")
                self.assertEqual(result.action, ImportRowActionChoices.CREATE)
                self.assertEqual(self.counts_of(tenant)[1], before[1] + 1)

    def test_a_switch_closed_at_one_school_does_not_reach_the_other(self):
        before = self.counts_of(self.tenant)
        with self.assertRaises(FieldWriteDenied):
            self.import_row("multi", "phone_ro")
        result = self.import_row("single", "open")
        self.assertEqual(result.action, ImportRowActionChoices.CREATE)
        self.assertEqual(self.counts_of(self.tenant), before)
        self.assertFalse(
            User.objects.filter(
                tenant=self.tenant, email=self.shapes["multi"][2],
            ).exists()
        )


class StaffPreviewTests(_Fixture):
    def test_the_preview_reports_the_same_refusal_against_its_row_and_column(self):
        for label in self.shapes:
            with self.subTest(school=label):
                found = self.refusals(self.preview(label, "phone_ro"))
                self.assertEqual(len(found), 1, found)
                issue = found[0]
                self.assertEqual(issue["severity"], "error")
                self.assertEqual(issue["code"], "business_rule")
                self.assertEqual(issue["row_number"], 1)
                self.assertEqual(issue["column_name"], "Phone")
                self.assertEqual(issue["metadata"], {"field_write_denied": ["phone"]})
                self.assertEqual(
                    issue["message"], "You do not have permission to change Phone.",
                )

    def test_the_preview_and_execution_agree_for_every_runner(self):
        for label in self.shapes:
            for who in self.runners[label]:
                with self.subTest(school=label, runner=who):
                    previewed = sorted(
                        field
                        for issue in self.refusals(self.preview(label, who))
                        for field in issue["metadata"]["field_write_denied"]
                    )
                    try:
                        self.import_row(label, who)
                        executed = []
                    except FieldWriteDenied as refusal:
                        executed = refusal.fields
                    self.assertEqual(previewed, executed)

    def test_a_blank_cell_is_not_reported(self):
        self.assertEqual(
            self.refusals(self.preview("multi", "phone_ro", Phone="")), [],
        )

    def test_a_preview_made_for_nobody_refuses_nothing(self):
        batch = self.batch_for([self.cells("multi")], "multi", "phone_ro")
        self.assertEqual(self.refusals(validate_rows(batch)), [])

    def test_a_refusal_in_the_file_does_not_hide_the_files_other_faults(self):
        """A bad date on the same row is still reported beside the refused phone."""
        issues = self.preview("multi", "phone_ro", **{"Hire Date": "not a date"})
        self.assertEqual(len(self.refusals(issues)), 1)
        self.assertIn("bad_date", {i["code"] for i in issues})


class StaffEngineTests(_Fixture):
    def test_the_engine_fails_the_refused_row_and_still_loads_the_others(self):
        for label, (tenant, _, email) in self.shapes.items():
            with self.subTest(school=label):
                second = f"sunday.{label}@staff-import-fa.test"
                batch = self.batch_for(
                    [
                        self.cells(label),
                        self.cells(
                            label, Email=second, Phone="", **{"Staff ID": "BFS/IMP/900"},
                        ),
                    ],
                    label, "phone_ro",
                )
                batch.is_ready_for_import = True
                batch.total_rows = 2
                batch.save(update_fields=["is_ready_for_import", "total_rows"])

                with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
                    with self.captureOnCommitCallbacks(execute=True):
                        job = execute_import(batch, self.runners[label]["phone_ro"])

                self.assertEqual(job.failed_rows, 1)
                self.assertEqual(job.succeeded_rows, 1)
                refused = ImportJobRowResult.objects.get(job=job, row_number=1)
                self.assertEqual(refused.action, ImportRowActionChoices.FAILED)
                self.assertEqual(
                    refused.status_message,
                    "You do not have permission to change Phone.",
                )
                self.assertEqual(refused.error_details, {"field_write_denied": ["phone"]})
                self.assertFalse(User.objects.filter(tenant=tenant, email=email).exists())
                self.assertTrue(User.objects.filter(tenant=tenant, email=second).exists())
