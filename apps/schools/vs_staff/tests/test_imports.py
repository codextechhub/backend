"""Loading a school's staff from a spreadsheet.

FRD M12 v2.1, FR-016.

The case every test here is built around: a school downloads the template,
fills it in without changing a thing, and uploads it back. That file has to
validate clean, and for a while it did not - see
``ValidationReadsTheFileTests``.
"""
from __future__ import annotations

from unittest import mock

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
    "send_invitation": "Send Invitation",
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
            "Send Invitation": "Yes",
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


class AnImportedGrantFollowsItsRowTests(_ImportFixture):
    """What the branch column decides, beyond where the person is based.

    A row writes the account, the invitation and the grant through the same
    service a single add uses, so the reach of an imported grant is the reach
    of an added one. It was not: a file of forty teachers uploaded by
    Brightfield's Ikeja office produced forty people posted to Ikeja and
    granted across the school, each able to read Lekki's records. Nobody filled
    anything in wrongly; the column that says where they work said nothing
    about what they may reach.
    """

    def create_from(self, **overrides):
        """One person, written the way the executor writes an imported row.

        Keyed by target field rather than by the file's headings: this is the
        payload the engine hands the handler after it has translated.
        """
        from schools.vs_staff.imports import create_staff_from_row, resolve_row

        payload = {
            "first_name": "Ifeoma",
            "last_name": "Anyanwu",
            "email": "ifeoma.anyanwu@brightfield.test",
            "role": "teacher",
            "branch": "",
        }
        payload.update(overrides)
        row = resolve_row(payload, tenant=self.tenant, multi_branch=True)
        self.assertTrue(row.ok, row.issues)
        return create_staff_from_row(row, tenant=self.tenant, created_by=self.admin)

    def grant_of(self, profile):
        from vs_rbac.models import TenantUserRoleAssignment

        return TenantUserRoleAssignment.objects.get(
            user=profile.user,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        )

    def test_a_row_naming_a_branch_is_granted_at_that_branch(self):
        profile = self.create_from(branch="Ikeja")

        self.assertEqual(profile.branch_id, self.ikeja.pk)
        self.assertEqual(self.grant_of(profile).branch_id, self.ikeja.pk)

    def test_a_row_leaving_the_branch_blank_is_granted_across_the_school(self):
        """Blank is a real posting, and the grant carries the same meaning.

        A registrar imported with no branch works across the whole school, which
        is exactly what a grant with no branch confers.
        """
        profile = self.create_from()

        self.assertIsNone(profile.branch_id)
        self.assertIsNone(self.grant_of(profile).branch_id)


class SendInvitationColumnTests(_ImportFixture):
    """Which people in the file are written to, and what a held-back row leaves.

    Without the column a file of eighty staff put eighty live activation links
    into eighty inboxes, and a school loading its payroll in July could not hold
    any of them back until September. The column is how a school says who to
    write to now; a blank one means everybody, so a school that has never seen
    it gets the behaviour it already had.
    """

    def create_from(self, **overrides):
        """One person, written the way the executor writes an imported row.

        Keyed by target field rather than by the file's headings: this is the
        payload the engine hands the handler after it has translated.
        """
        from schools.vs_staff.imports import create_staff_from_row, resolve_row

        payload = {
            "first_name": "Ifeoma",
            "last_name": "Anyanwu",
            "email": "ifeoma.anyanwu@brightfield.test",
            "role": "teacher",
            "branch": "",
        }
        payload.update(overrides)
        row = resolve_row(payload, tenant=self.tenant, multi_branch=True)
        self.assertTrue(row.ok, row.issues)
        return row, create_staff_from_row(
            row, tenant=self.tenant, created_by=self.admin,
        )

    def write(self, **overrides):
        """Write one row, and report whether an activation email was queued.

        The email is queued on commit, so the callbacks have to be run for the
        dispatch to happen at all. Asserting on the invitation row alone would
        pass whether or not anybody was actually written to, which is the exact
        thing being decided here.
        """
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                row, profile = self.create_from(**overrides)
        return profile, delay.called

    def test_a_row_saying_yes_is_emailed_its_link(self):
        _profile, emailed = self.write(send_invitation="Yes")

        self.assertTrue(emailed)

    def test_a_row_saying_no_is_not_emailed(self):
        _profile, emailed = self.write(send_invitation="No")

        self.assertFalse(emailed)

    def test_a_blank_cell_invites_because_a_school_may_not_know_the_column(self):
        _profile, emailed = self.write(send_invitation="")

        self.assertTrue(emailed)

    def test_a_column_the_school_left_out_entirely_still_invites(self):
        """An older file has no such key at all, not merely an empty one."""
        _profile, emailed = self.write()

        self.assertTrue(emailed)

    def test_the_spellings_a_school_actually_types_are_understood(self):
        for answer in ("no", "N", "false", "0"):
            with self.subTest(answer=answer):
                from schools.vs_staff.imports import resolve_row

                row = resolve_row(
                    {"first_name": "A", "last_name": "B", "email": f"{answer}@x.test",
                     "role": "teacher", "send_invitation": answer},
                    tenant=self.tenant,
                )
                self.assertFalse(row.send_invitation)

    def test_an_answer_that_is_neither_warns_and_invites(self):
        """A warning rather than an error, matching the employment type column.

        Refusing the row would block the batch over one cell, and the safe
        fallback is the behaviour the column replaced: the person is invited.
        """
        issues = validate_rows(self.batch(self.row(**{"Send Invitation": "Maybe"})))

        self.assertEqual(
            [(i["code"], i["column_name"], i["severity"]) for i in issues],
            [("unknown_send_invitation", "Send Invitation", "warning")],
        )

        _profile, emailed = self.write(send_invitation="Maybe")
        self.assertTrue(emailed)

    def test_a_held_back_row_leaves_a_real_invitation_waiting_to_be_sent(self):
        """Parked, not skipped. The account is invitable the moment somebody asks.

        The invitation row is what the existing resend path acts on, so a school
        chases a held-back person through the same service it chases anybody
        else with, rather than through a second notion of "not yet invited".
        """
        from vs_user.models import User, UserInvitation

        profile, emailed = self.write(send_invitation="No")

        self.assertFalse(emailed)
        profile.user.refresh_from_db()
        self.assertEqual(profile.user.status, User.Status.PENDING)
        invitation = UserInvitation.objects.get(user=profile.user)
        self.assertEqual(
            invitation.email_status, UserInvitation.EmailStatus.PENDING,
        )
        self.assertFalse(invitation.is_used)
        self.assertIsNone(invitation.email_sent_at)

    def test_a_clean_file_carrying_the_column_still_validates_with_nothing_to_fix(self):
        self.assertEqual(validate_rows(self.batch(self.row())), [])


class TheSingleAddStillSendsTests(_ImportFixture):
    """The default is what every caller that does not mention it gets.

    ``finalize_invitation`` grew the switch the import needed, and the single
    add, the seeding command and the platform hiring flow all call the same
    method. A default that had changed would silently stop sending for all
    three.
    """

    def test_finalize_invitation_sends_when_nobody_says_otherwise(self):
        from vs_user.services.user import UserCreationService

        person = self.make_staff(
            "default@brightfield.test", "Default", "Sender", branch=self.lekki,
        )
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                UserCreationService.finalize_invitation(
                    user=person.user, requested_by=self.admin,
                )

        self.assertTrue(delay.called)


class TheEngineRunsTheWholeFileTests(_ImportFixture):
    """One batch, driven through the executor rather than through the resolver.

    Every other test here calls the row function directly, which proves the
    interpretation and nothing about the path a real upload takes: the template
    translation, the per-row savepoints, the row results a school reads
    afterwards and the message on each one.
    """

    def ready_batch(self, *rows):
        batch = self.batch(*rows)
        batch.is_ready_for_import = True
        batch.total_rows = len(rows)
        batch.save(update_fields=["is_ready_for_import", "total_rows"])
        return batch

    def run_import(self, batch):
        from vs_import_data.services.import_executor import execute_import

        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                job = execute_import(batch, self.admin)
        return job, delay

    def test_a_file_invites_one_person_and_holds_the_other_back(self):
        from schools.vs_staff.models import StaffProfile
        from vs_import_data.models import ImportJobRowResult

        job, delay = self.run_import(self.ready_batch(
            self.row(),
            self.row(**{
                "First Name": "Sunday",
                "Last Name": "Ekpo",
                "Email": "sunday.ekpo@brightfield.test",
                "Staff ID": "BFS/IMP/002",
                "Send Invitation": "No",
            }),
        ))

        self.assertEqual(job.succeeded_rows, 2)
        self.assertEqual(job.failed_rows, 0)
        # Both people exist; only one of them was written to.
        self.assertEqual(delay.call_count, 1)
        for email in (
            "ifeoma.anyanwu@brightfield.test", "sunday.ekpo@brightfield.test",
        ):
            self.assertTrue(
                StaffProfile.all_objects.filter(user__email=email).exists(), email,
            )

        messages = {
            result.row_number: result.status_message
            for result in ImportJobRowResult.objects.filter(job=job)
        }
        self.assertIn("invited", messages[1])
        self.assertIn("No invitation email was sent", messages[2])

    def test_the_result_line_does_not_claim_a_held_back_person_was_invited(self):
        """The message is what a school reads to know what its upload did.

        One wording for both would tell a school it had emailed people it had
        deliberately held back, and the row result is the only record of it.
        """
        from vs_import_data.models import ImportJobRowResult

        job, _delay = self.run_import(self.ready_batch(
            self.row(**{"Send Invitation": "No"}),
        ))

        message = ImportJobRowResult.objects.get(job=job, row_number=1).status_message
        self.assertNotIn("invited.", message)
        self.assertIn("Ifeoma Anyanwu added.", message)


class TheStaffImportKeyTests(_ImportFixture):
    """The dataset's own key, and the engine's bridge to it."""

    def test_the_module_import_key_reaches_the_wizard(self):
        """Registered, or the engine falls back to the generic import key.

        A module that registers nothing is refused however its own key is
        granted, and that failure reads as a seeding problem rather than as a
        dataset nobody told the engine about.
        """
        from schools.vs_staff.constants import PERM_IMPORT
        from vs_import_data.permissions import _DATASET_IMPORT_KEYS

        self.assertEqual(PERM_IMPORT, "school.staff.import")
        self.assertEqual(_DATASET_IMPORT_KEYS.get("staff"), PERM_IMPORT)

    def test_staff_is_a_dataset_a_school_may_import(self):
        from vs_import_data.datasets import may_import, platform_only

        self.assertFalse(platform_only("staff"))
        self.assertTrue(may_import(self.admin, "staff"))


class HeldBackPeopleAreVisibleOnTheStaffListTests(_ImportFixture):
    """The screen that offers to invite somebody later has to be able to find them.

    A held-back person reads identically to an invited one on every other field
    the row carries: both accounts are PENDING, both can be resent, and both
    have an invitation dated today. Without the email status the school has no
    way to tell which of the eighty people it just loaded were actually written
    to, which is the whole point of holding any of them back.
    """

    def import_person(self, email, *, send):
        from schools.vs_staff.imports import create_staff_from_row, resolve_row

        row = resolve_row(
            {
                "first_name": "Ifeoma", "last_name": "Anyanwu", "email": email,
                "role": "teacher", "send_invitation": send,
            },
            tenant=self.tenant,
        )
        self.assertTrue(row.ok, row.issues)
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                return create_staff_from_row(
                    row, tenant=self.tenant, created_by=self.admin,
                )

    def rows_by_email(self):
        response = self.get(self.admin, "staff-list")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["email"]: row for row in response.data["data"]}

    def test_the_list_says_which_people_were_actually_emailed(self):
        from vs_user.models import UserInvitation

        self.import_person("invited@brightfield.test", send="Yes")
        self.import_person("parked@brightfield.test", send="No")

        rows = self.rows_by_email()
        self.assertEqual(
            rows["parked@brightfield.test"]["invitation_email_status"],
            UserInvitation.EmailStatus.PENDING,
        )
        # Both are chaseable, which is exactly why the status is needed beside it.
        self.assertTrue(rows["parked@brightfield.test"]["can_resend"])
        self.assertTrue(rows["invited@brightfield.test"]["can_resend"])

    def test_somebody_with_no_invitation_at_all_reports_nothing(self):
        """Null is "there is no invitation", not "one is waiting to go out"."""
        rows = self.rows_by_email()

        self.assertIsNone(rows["eze@brightfield.test"]["invitation_email_status"])

    def test_the_status_costs_no_extra_query_per_person(self):
        """Read from the prefetch the resend flag beside it already needs.

        A page of fifty that asked each row for its own invitation would be
        fifty extra queries on the busiest screen this module serves.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.import_person("parked@brightfield.test", send="No")

        with CaptureQueriesContext(connection) as first:
            self.rows_by_email()

        self.import_person("second@brightfield.test", send="No")
        self.import_person("third@brightfield.test", send="No")

        with CaptureQueriesContext(connection) as second:
            self.rows_by_email()

        self.assertEqual(len(first.captured_queries), len(second.captured_queries))
