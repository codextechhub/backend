"""Rolling back an import reverses what the row actually created, or says why not.

The defect these cover: ``reverse_target_record`` read ``target_object_pk`` and
assumed every one of them was a ``School`` id. Branch ids, user ids and school
ids are three independent ``BigAutoField`` sequences running through the same
integers, so rolling back a branches import deleted whichever schools happened
to share those numbers, left the branches untouched, and reported success.
"""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_vision_user,
)

from .models import (
    DatasetTypeChoices,
    FileFormatChoices,
    ImportBatch,
    ImportBatchStatusChoices,
    ImportJob,
    ImportJobRowResult,
    ImportJobStatusChoices,
    ImportRowActionChoices,
    ImportTemplate,
)
from .services.rollback_service import rollback_import_job


class RollbackTestCase(TestCase):
    """Builds a real batch, job and row results for a given dataset."""

    dataset_type = DatasetTypeChoices.BRANCHES

    def setUp(self):
        self.tenant = codex_tenant()
        self.operator = make_vision_user(email="rollback-operator@test.com")
        self.template = ImportTemplate.objects.create(
            code=f"rollback-{self.dataset_type}",
            name="Rollback Template",
            dataset_type=self.dataset_type,
            default_file_format=FileFormatChoices.CSV,
        )
        self.batch = ImportBatch.objects.create(
            tenant=self.tenant,
            uploaded_by=self.operator,
            template=self.template,
            dataset_type=self.dataset_type,
            file=SimpleUploadedFile("rollback.csv", b"Name\nOne\n"),
            file_format=FileFormatChoices.CSV,
            original_filename="rollback.csv",
            status=ImportBatchStatusChoices.IMPORT_SUCCEEDED,
            total_rows=1,
            total_columns=1,
        )
        self.job = ImportJob.objects.create(
            import_batch=self.batch,
            queued_by=self.operator,
            status=ImportJobStatusChoices.SUCCEEDED,
            total_rows=1,
            processed_rows=1,
            succeeded_rows=1,
        )

    def add_row(self, *, target_model, target_object_pk, payload, row_number=1):
        return ImportJobRowResult.objects.create(
            job=self.job,
            row_number=row_number,
            action=ImportRowActionChoices.CREATE,
            target_model=target_model,
            target_object_pk=str(target_object_pk),
            normalized_payload=payload,
        )

    def roll_back(self):
        return rollback_import_job(
            self.job, initiated_by=self.operator, reason="test",
        )


class BranchRollbackTests(RollbackTestCase):
    def test_branch_rollback_does_not_delete_the_school_with_the_same_id(self):
        """The reported bug, with the ids forced to collide.

        Greenfield's fourth campus is Branch id 4242. Bright Star School, on the
        platform since March, is School id 4242. Rolling back Greenfield's
        branches import used to delete Bright Star.
        """
        from vs_tenants.models import Branch
        from schools.vs_schools.models import School

        bright_star = make_school(
            pk=4242, slug="bright-star", name="Bright Star School",
        )
        greenfield = make_school(slug="greenfield", name="Greenfield College")
        make_branch(greenfield, name="Greenfield Main", is_main=True)
        campus = Branch.objects.create(
            pk=4242,
            tenant=greenfield.tenant,
            name="Greenfield Second Campus",
            is_main=False,
            status="ACTIVE",
        )
        self.assertEqual(bright_star.pk, campus.pk)

        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={
                "name": "Greenfield Second Campus",
                "school_slug": "greenfield",
            },
        )

        record = self.roll_back()

        self.assertFalse(Branch.all_objects.filter(pk=campus.pk).exists())
        self.assertTrue(School.objects.filter(pk=bright_star.pk).exists())
        self.assertTrue(record.was_successful)
        self.assertEqual(record.reverted_rows_count, 1)

    def test_a_branch_belonging_to_another_school_is_refused(self):
        """Ownership is checked against the school the row itself named."""
        from vs_tenants.models import Branch

        greenfield = make_school(slug="greenfield-own", name="Greenfield")
        make_branch(greenfield, name="Greenfield Main", is_main=True)
        stranger = make_school(slug="bright-star-own", name="Bright Star")
        make_branch(stranger, name="Bright Star Main", is_main=True)
        other_branch = Branch.objects.create(
            tenant=stranger.tenant,
            name="Annex",
            is_main=False,
            status="ACTIVE",
        )

        self.add_row(
            target_model="Branch",
            target_object_pk=other_branch.pk,
            payload={"name": "Annex", "school_slug": "greenfield-own"},
        )

        record = self.roll_back()

        self.assertTrue(Branch.all_objects.filter(pk=other_branch.pk).exists())
        self.assertFalse(record.was_successful)
        self.assertEqual(record.reverted_rows_count, 0)
        self.assertIn("different school", record.details["rows"][0]["message"])

    def test_a_renamed_branch_is_refused_because_the_id_is_not_an_identity(self):
        from vs_tenants.models import Branch

        greenfield = make_school(slug="greenfield-renamed", name="Greenfield")
        make_branch(greenfield, name="Greenfield Main", is_main=True)
        campus = Branch.objects.create(
            tenant=greenfield.tenant, name="Ikeja Campus", is_main=False,
            status="ACTIVE",
        )

        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Lekki Campus", "school_slug": "greenfield-renamed"},
        )

        record = self.roll_back()

        self.assertTrue(Branch.all_objects.filter(pk=campus.pk).exists())
        self.assertFalse(record.was_successful)

    def test_the_main_branch_is_never_deleted(self):
        from vs_tenants.models import Branch

        greenfield = make_school(slug="greenfield-main", name="Greenfield")
        main = make_branch(greenfield, name="Greenfield Main", is_main=True)

        self.add_row(
            target_model="Branch",
            target_object_pk=main.pk,
            payload={"name": "Greenfield Main", "school_slug": "greenfield-main"},
        )

        record = self.roll_back()

        self.assertTrue(Branch.all_objects.filter(pk=main.pk).exists())
        self.assertFalse(record.was_successful)
        self.assertIn("main branch", record.details["rows"][0]["message"])


class UnknownModelRollbackTests(RollbackTestCase):
    def test_an_unregistered_target_model_is_refused_not_guessed_at(self):
        """A dataset with no reverser must not fall through to deleting schools."""
        from schools.vs_schools.models import School

        school = make_school(pk=7, slug="untouched", name="Untouched School")

        self.add_row(
            target_model="Student",
            target_object_pk=7,
            payload={"name": "Tunde"},
        )

        record = self.roll_back()

        self.assertTrue(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(record.was_successful)
        self.assertEqual(record.reverted_rows_count, 0)
        self.assertIn("No rollback is defined", record.details["rows"][0]["message"])

    def test_a_blank_target_model_is_refused(self):
        from schools.vs_schools.models import School

        school = make_school(pk=8, slug="untouched-blank", name="Untouched")

        self.add_row(target_model="", target_object_pk=8, payload={})

        record = self.roll_back()

        self.assertTrue(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(record.was_successful)


class SchoolRollbackTests(RollbackTestCase):
    dataset_type = DatasetTypeChoices.SCHOOLS

    def test_an_untouched_school_is_removed_with_its_tenant(self):
        from schools.vs_schools.models import School
        from vs_tenants.models import Tenant

        school = make_school(slug="fresh-import", name="Fresh Import School")
        make_branch(school, name="Fresh Import Main", is_main=True)
        tenant_pk = school.tenant_id

        self.add_row(
            target_model="School",
            target_object_pk=school.pk,
            payload={"slug": "fresh-import", "name": "Fresh Import School"},
        )

        record = self.roll_back()

        self.assertTrue(record.was_successful, record.details)
        self.assertFalse(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(Tenant.objects.filter(pk=tenant_pk).exists())

    def test_a_school_whose_tenant_has_other_records_is_refused(self):
        """Bright Star imported on Monday, used on Tuesday, rolled back on Wednesday."""
        from schools.vs_schools.models import School
        from schools.vs_academics.models import AcademicSession

        school = make_school(slug="in-use", name="In Use School")
        make_branch(school, name="In Use Main", is_main=True)
        AcademicSession.objects.create(
            tenant=school.tenant,
            name="2026/2027",
            start_date="2026-09-01",
            end_date="2027-07-31",
        )

        self.add_row(
            target_model="School",
            target_object_pk=school.pk,
            payload={"slug": "in-use", "name": "In Use School"},
        )

        record = self.roll_back()

        self.assertTrue(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(record.was_successful)
        self.assertIn(
            "records the import did not create",
            record.details["rows"][0]["message"],
        )

    def test_a_school_whose_admin_has_signed_in_is_refused(self):
        from django.utils import timezone
        from schools.vs_schools.models import School

        school = make_school(slug="signed-in", name="Signed In School")
        make_branch(school, name="Signed In Main", is_main=True)
        admin = make_vision_user(
            email="head@signed-in.test",
            tenant=school.tenant,
            status="PENDING",
        )
        admin.last_login = timezone.now()
        admin.save(update_fields=["last_login"])

        self.add_row(
            target_model="School",
            target_object_pk=school.pk,
            payload={"slug": "signed-in", "name": "Signed In School"},
        )

        record = self.roll_back()

        self.assertTrue(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(record.was_successful)
        self.assertIn("has signed in", record.details["rows"][0]["message"])

    def test_a_school_with_a_second_branch_is_refused(self):
        from schools.vs_schools.models import School

        school = make_school(slug="two-branches", name="Two Branch School")
        make_branch(school, name="Main", is_main=True)
        make_branch(school, name="Annex", is_main=False)

        self.add_row(
            target_model="School",
            target_object_pk=school.pk,
            payload={"slug": "two-branches", "name": "Two Branch School"},
        )

        record = self.roll_back()

        self.assertTrue(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(record.was_successful)
        self.assertIn("branches", record.details["rows"][0]["message"])

    def test_a_slug_that_no_longer_matches_is_refused(self):
        from schools.vs_schools.models import School

        school = make_school(slug="renamed-since", name="Renamed School")
        make_branch(school, name="Main", is_main=True)

        self.add_row(
            target_model="School",
            target_object_pk=school.pk,
            payload={"slug": "as-imported", "name": "Renamed School"},
        )

        record = self.roll_back()

        self.assertTrue(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(record.was_successful)

    def test_a_pre_b23_slug_reference_is_refused_rather_than_guessed_at(self):
        """Rows written when the slug WAS the primary key can no longer be resolved."""
        from schools.vs_schools.models import School

        school = make_school(slug="old-style", name="Old Style School")
        make_branch(school, name="Main", is_main=True)

        self.add_row(
            target_model="School",
            target_object_pk="old-style",
            payload={"slug": "old-style", "name": "Old Style School"},
        )

        record = self.roll_back()

        self.assertTrue(School.objects.filter(pk=school.pk).exists())
        self.assertFalse(record.was_successful)
        self.assertIn("not a numeric primary key", record.details["rows"][0]["message"])


class CxUserRollbackTests(RollbackTestCase):
    dataset_type = DatasetTypeChoices.CX_USERS

    def test_a_user_still_awaiting_approval_is_deleted(self):
        from vs_user.models import User

        hire = make_vision_user(
            email="new.hire@codexng.com", status="PENDING_APPROVAL",
        )

        self.add_row(
            target_model="User",
            target_object_pk=hire.pk,
            payload={"email": "new.hire@codexng.com"},
        )

        record = self.roll_back()

        self.assertTrue(record.was_successful, record.details)
        self.assertFalse(User.objects.filter(pk=hire.pk).exists())

    def test_an_active_user_is_refused(self):
        from vs_user.models import User

        staff = make_vision_user(email="settled.staff@codexng.com", status="ACTIVE")

        self.add_row(
            target_model="User",
            target_object_pk=staff.pk,
            payload={"email": "settled.staff@codexng.com"},
        )

        record = self.roll_back()

        self.assertTrue(User.objects.filter(pk=staff.pk).exists())
        self.assertFalse(record.was_successful)

    def test_a_user_id_that_names_a_different_address_is_refused(self):
        from vs_user.models import User

        someone_else = make_vision_user(
            email="someone.else@codexng.com", status="PENDING_APPROVAL",
        )

        self.add_row(
            target_model="User",
            target_object_pk=someone_else.pk,
            payload={"email": "new.hire@codexng.com"},
        )

        record = self.roll_back()

        self.assertTrue(User.objects.filter(pk=someone_else.pk).exists())
        self.assertFalse(record.was_successful)


class RollbackReportingTests(RollbackTestCase):
    def test_a_partial_rollback_is_reported_as_partial_not_successful(self):
        from vs_tenants.models import Branch

        greenfield = make_school(slug="partial", name="Partial School")
        make_branch(greenfield, name="Main", is_main=True)
        campus = Branch.objects.create(
            tenant=greenfield.tenant, name="Reversible Campus", is_main=False,
            status="ACTIVE",
        )

        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Reversible Campus", "school_slug": "partial"},
            row_number=1,
        )
        self.add_row(
            target_model="Student",
            target_object_pk=99,
            payload={},
            row_number=2,
        )

        record = self.roll_back()
        self.job.refresh_from_db()
        self.batch.refresh_from_db()

        self.assertFalse(record.was_successful)
        self.assertEqual(record.reverted_rows_count, 1)
        self.assertEqual(record.details["refused_rows_count"], 1)
        self.assertEqual(
            self.job.status, ImportJobStatusChoices.PARTIALLY_ROLLED_BACK
        )
        self.assertEqual(
            self.batch.status, ImportBatchStatusChoices.PARTIALLY_ROLLED_BACK
        )

    def test_retrying_a_partial_rollback_skips_rows_already_reversed(self):
        from vs_tenants.models import Branch

        greenfield = make_school(slug="retry", name="Retry School")
        make_branch(greenfield, name="Main", is_main=True)
        campus = Branch.objects.create(
            tenant=greenfield.tenant, name="Retry Campus", is_main=False,
            status="ACTIVE",
        )

        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Retry Campus", "school_slug": "retry"},
            row_number=1,
        )
        self.add_row(
            target_model="Student", target_object_pk=99, payload={}, row_number=2,
        )

        self.roll_back()
        second = self.roll_back()

        rows = {row["row_number"]: row for row in second.details["rows"]}
        self.assertEqual(rows[1]["status"], "skipped")
        self.assertEqual(rows[2]["status"], "refused")
        self.assertEqual(second.reverted_rows_count, 0)

    def test_the_endpoint_reports_which_rows_were_not_reversed(self):
        from schools.vs_schools.models import School

        School.objects.filter(pk=31).delete()
        make_school(pk=31, slug="endpoint-untouched", name="Untouched")

        self.add_row(target_model="Student", target_object_pk=31, payload={})

        role = make_role(self.tenant, name="Import roller")
        make_role_permission(role, make_permission("import.rollbacks.run"))
        make_assignment(self.tenant, self.operator, role)
        client = TenantAPIClient(user=self.operator)

        response = client.post(
            f"/v1/import/batches/{self.batch.pk}/jobs/{self.job.pk}/rollback/",
            {"reason": "wrong file"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertFalse(data["was_successful"])
        self.assertEqual(data["reverted_rows_count"], 0)
        self.assertEqual(data["refused_rows_count"], 1)
        self.assertTrue(School.objects.filter(pk=31).exists())


class UpdatedRowRollbackTests(RollbackTestCase):
    def test_a_row_that_only_updated_a_record_is_not_deleted(self):
        """An update is not a creation, so rollback has nothing to delete."""
        from vs_tenants.models import Branch

        greenfield = make_school(slug="updated-row", name="Updated Row School")
        make_branch(greenfield, name="Main", is_main=True)
        campus = Branch.objects.create(
            tenant=greenfield.tenant, name="Existing Campus", is_main=False,
            status="ACTIVE",
        )

        row = self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Existing Campus", "school_slug": "updated-row"},
        )
        row.action = ImportRowActionChoices.UPDATE
        row.save(update_fields=["action"])

        record = self.roll_back()

        self.assertTrue(Branch.all_objects.filter(pk=campus.pk).exists())
        self.assertFalse(record.was_successful)
        self.assertIn("not a creation", record.details["rows"][0]["message"])


class OrphanedContactTests(RollbackTestCase):
    """A deleted admin link must not leave its contact card behind."""

    def _campus_with_admin(self, school, *, contact):
        from schools.vs_schools.models import BranchPrimaryAdmin
        from vs_tenants.models import Branch

        campus = Branch.objects.create(
            tenant=school.tenant, name="Second Campus", is_main=False,
            status="ACTIVE",
        )
        BranchPrimaryAdmin.objects.create(branch=campus, contact=contact)
        return campus

    def test_the_branch_admin_contact_card_goes_with_the_branch(self):
        from schools.vs_schools.models import ContactInfo

        school = make_school(slug="contact-sweep", name="Contact Sweep School")
        make_branch(school, name="Main", is_main=True)
        contact = ContactInfo.objects.create(
            full_name="Chidi Okonkwo", email="chidi@contact-sweep.test",
        )
        campus = self._campus_with_admin(school, contact=contact)

        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Second Campus", "school_slug": "contact-sweep"},
        )

        record = self.roll_back()

        self.assertTrue(record.was_successful, record.details)
        self.assertFalse(ContactInfo.objects.filter(pk=contact.pk).exists())

    def test_a_contact_still_used_by_the_school_admin_survives(self):
        from schools.vs_schools.models import ContactInfo, SchoolPrimaryAdmin

        school = make_school(slug="contact-shared", name="Shared Contact School")
        make_branch(school, name="Main", is_main=True)
        contact = ContactInfo.objects.create(
            full_name="Ada Okoye", email="ada@contact-shared.test",
        )
        SchoolPrimaryAdmin.objects.create(school=school, contact=contact)
        campus = self._campus_with_admin(school, contact=contact)

        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Second Campus", "school_slug": "contact-shared"},
        )

        record = self.roll_back()

        self.assertTrue(record.was_successful, record.details)
        self.assertTrue(ContactInfo.objects.filter(pk=contact.pk).exists())

    def test_a_school_rollback_takes_its_admin_contacts_with_it(self):
        from schools.vs_schools.models import ContactInfo, SchoolPrimaryAdmin

        school = make_school(slug="school-contacts", name="School Contacts")
        branch = make_branch(school, name="Main", is_main=True)
        school_contact = ContactInfo.objects.create(
            full_name="Bola Adeyemi", email="bola@school-contacts.test",
        )
        branch_contact = ContactInfo.objects.create(
            full_name="Ngozi Eze", email="ngozi@school-contacts.test",
        )
        SchoolPrimaryAdmin.objects.create(school=school, contact=school_contact)
        from schools.vs_schools.models import BranchPrimaryAdmin

        BranchPrimaryAdmin.objects.create(branch=branch, contact=branch_contact)

        self.add_row(
            target_model="School",
            target_object_pk=school.pk,
            payload={"slug": "school-contacts", "name": "School Contacts"},
        )

        record = self.roll_back()

        self.assertTrue(record.was_successful, record.details)
        self.assertFalse(
            ContactInfo.objects.filter(
                pk__in=[school_contact.pk, branch_contact.pk]
            ).exists()
        )


class QueuedRollbackTests(RollbackTestCase):
    """A rollback big enough to matter leaves the request."""

    def setUp(self):
        super().setUp()
        role = make_role(self.tenant, name="Import roller async")
        make_role_permission(role, make_permission("import.rollbacks.run"))
        make_assignment(self.tenant, self.operator, role)
        self.client = TenantAPIClient(user=self.operator)
        self.url = (
            f"/v1/import/batches/{self.batch.pk}/jobs/{self.job.pk}/rollback/"
        )

    def _campus(self, *, slug="queued", name="Queued Campus"):
        from vs_tenants.models import Branch

        school = make_school(slug=slug, name=f"{name} School")
        make_branch(school, name=f"{name} Main", is_main=True)
        return Branch.objects.create(
            tenant=school.tenant, name=name, is_main=False, status="ACTIVE",
        )

    def test_a_small_rollback_still_answers_inside_the_request(self):
        campus = self._campus()
        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Queued Campus", "school_slug": "queued"},
        )

        response = self.client.post(self.url, {"reason": "small"}, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertNotIn("queued", data)
        self.assertEqual(data["reverted_rows_count"], 1)
        self.assertEqual(len(data["rows"]), 1)

    def test_a_rollback_over_the_row_limit_is_queued(self):
        from unittest import mock
        from vs_tenants.models import Branch

        campus = self._campus(slug="over-limit", name="Over Limit Campus")
        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Over Limit Campus", "school_slug": "over-limit"},
        )

        with mock.patch("vs_import_data.constants.ROLLBACK_INLINE_ROW_LIMIT", 0):
            response = self.client.post(self.url, {"reason": "big"}, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["queued"])
        self.assertEqual(data["row_count"], 1)
        # Celery runs eagerly under test settings, so the queued work has
        # already happened by the time the response is read.
        self.assertFalse(Branch.all_objects.filter(pk=campus.pk).exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ImportJobStatusChoices.ROLLED_BACK)

    def test_a_caller_may_force_the_queued_path(self):
        from vs_tenants.models import Branch

        campus = self._campus(slug="forced", name="Forced Campus")
        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Forced Campus", "school_slug": "forced"},
        )

        response = self.client.post(
            self.url, {"reason": "force", "run_async": True}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["queued"])
        self.assertFalse(Branch.all_objects.filter(pk=campus.pk).exists())

    def test_a_caller_may_force_the_inline_path(self):
        from unittest import mock

        campus = self._campus(slug="forced-inline", name="Inline Campus")
        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "Inline Campus", "school_slug": "forced-inline"},
        )

        with mock.patch("vs_import_data.constants.ROLLBACK_INLINE_ROW_LIMIT", 0):
            response = self.client.post(
                self.url, {"reason": "inline", "run_async": False}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["reverted_rows_count"], 1)

    def test_a_second_rollback_is_refused_while_one_is_in_flight(self):
        from django.utils import timezone

        campus = self._campus(slug="in-flight", name="In Flight Campus")
        self.add_row(
            target_model="Branch",
            target_object_pk=campus.pk,
            payload={"name": "In Flight Campus", "school_slug": "in-flight"},
        )
        self.job.rollback_started_at = timezone.now()
        self.job.rollback_completed_at = None
        self.job.save(update_fields=["rollback_started_at", "rollback_completed_at"])

        response = self.client.post(self.url, {"reason": "again"}, format="json")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("already running", response.content.decode())
