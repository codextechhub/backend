"""What a dataset's own import key opens on a batch, and what it leaves shut.

``HasImportBatchRBACPermission`` lets a module's import key (``school.students.import``
on a students batch, ``finance.bankaccount.import`` on a bank statement) stand
in for the engine's keys, so the holder can take a file through the wizard
without broad ``import.*`` access. The stand-in covers the wizard and nothing
past it.

Rolling back takes every imported row off again, and deleting a batch erases
the record of what was loaded. Neither is a step of the wizard: a school
corrects an import by uploading a fixed file, and unwinding one is a support
action that needs the engine's own key for it.

The people:

- Ada Okoye, registrar at Bright Star School, holds ``school.students.import``
  and nothing from the engine.
- Obi Eze, CodeX support, holds ``import.rollbacks.run`` on Bright Star.
- Chika Nwosu at Greenfield School holds the same students key as Ada, on her
  own school.
"""
from __future__ import annotations

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from .constants import ImportPermission
from .models import (
    DatasetTypeChoices,
    FileFormatChoices,
    ImportBatch,
    ImportBatchStatusChoices,
    ImportJob,
    ImportJobStatusChoices,
    ImportTemplate,
    ImportValidationIssue,
    ValidationCodeChoices,
    ValidationSeverityChoices,
)

STUDENTS_IMPORT_KEY = "school.students.import"


def _grant(tenant, user, key, *, name):
    """Give *user* a school-wide role holding exactly *key* on *tenant*."""
    role = make_role(tenant, name=name)
    make_role_permission(role, make_permission(key))
    make_assignment(tenant, user, role)


class _DatasetKeyFixture(TestCase):
    """Bright Star's students batch, imported once, and the people around it."""

    def setUp(self):
        self.school = make_school(slug="bright-star-keys", name="Bright Star School")
        self.tenant = self.school.tenant

        self.ada = make_school_admin(
            None, email="ada.okoye@bright-star.test", tenant=self.tenant,
        )
        _grant(self.tenant, self.ada, STUDENTS_IMPORT_KEY, name="Registrar")
        self.ada_client = TenantAPIClient(user=self.ada)

        self.template = ImportTemplate.objects.create(
            code="dataset-key-students",
            name="Students",
            dataset_type=DatasetTypeChoices.STUDENTS,
            default_file_format=FileFormatChoices.CSV,
        )
        self.batch = self.make_batch(self.tenant, self.ada)
        self.job = ImportJob.objects.create(
            import_batch=self.batch,
            queued_by=self.ada,
            status=ImportJobStatusChoices.SUCCEEDED,
            total_rows=0,
            processed_rows=0,
            succeeded_rows=0,
        )

    def make_batch(self, tenant, uploader, **overrides):
        fields = {
            "tenant": tenant,
            "uploaded_by": uploader,
            "template": self.template,
            "dataset_type": DatasetTypeChoices.STUDENTS,
            "file": SimpleUploadedFile("roll.csv", b"First Name\nTunde\n"),
            "file_format": FileFormatChoices.CSV,
            "original_filename": "roll.csv",
            "status": ImportBatchStatusChoices.IMPORT_SUCCEEDED,
            "total_rows": 1,
            "total_columns": 1,
            "uploaded_headers": ["First Name"],
            "preview_rows": [{"First Name": "Tunde"}],
        }
        fields.update(overrides)
        return ImportBatch.objects.create(**fields)

    def batch_url(self, suffix="", batch=None):
        return f"/v1/import/batches/{(batch or self.batch).pk}/{suffix}"

    def rollback_url(self, batch=None, job=None):
        batch = batch or self.batch
        job = job or self.job
        return f"/v1/import/batches/{batch.pk}/jobs/{job.pk}/rollback/"


class DatasetKeyTakesAFileThroughTheWizardTests(_DatasetKeyFixture):
    """Ada, holding only the students key, runs her own file start to finish."""

    def setUp(self):
        super().setUp()
        self.fresh = self.make_batch(
            self.tenant, self.ada, status=ImportBatchStatusChoices.UPLOADED,
        )

    def test_she_can_validate_her_batch(self):
        response = self.ada_client.post(self.batch_url("validate/", self.fresh))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("summary", response.json()["data"])

    def test_she_can_start_the_import(self):
        """The executor is stubbed: what is under test is the gate, not the roll."""
        self.fresh.is_ready_for_import = True
        self.fresh.status = ImportBatchStatusChoices.READY_TO_IMPORT
        self.fresh.save(update_fields=["is_ready_for_import", "status"])
        job = ImportJob(
            import_batch=self.fresh, status=ImportJobStatusChoices.SUCCEEDED,
        )
        job.id = 4242

        with mock.patch(
            "vs_import_data.views.execute_import", return_value=job,
        ) as executor:
            response = self.ada_client.post(
                self.batch_url("start-import/", self.fresh),
                {"run_async": False},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        executor.assert_called_once()
        self.assertEqual(executor.call_args.kwargs["import_batch"], self.fresh)

    def test_she_can_read_the_batch_its_issues_and_its_jobs(self):
        for suffix in ("", "download/", "issues/", "issues/export/", "jobs/"):
            with self.subTest(endpoint=suffix or "detail"):
                response = self.ada_client.get(self.batch_url(suffix))
                self.assertEqual(response.status_code, 200)

        response = self.ada_client.get(self.batch_url(f"jobs/{self.job.pk}/"))
        self.assertEqual(response.status_code, 200, response.content)

    def test_she_can_abandon_her_own_upload(self):
        response = self.ada_client.post(self.batch_url("cancel/", self.fresh))

        self.assertEqual(response.status_code, 200, response.content)
        self.fresh.refresh_from_db()
        self.assertEqual(self.fresh.status, ImportBatchStatusChoices.CANCELLED)


class DatasetKeyStopsAtTheWizardTests(_DatasetKeyFixture):
    """Ada cannot unwind, erase or administer what she imported.

    Bright Star imports 400 children through Ada. If her students key reached
    the rollback endpoint, one call from her would take all 400 off the roll,
    guardians and class placements with them, with nobody at CodeX involved.
    """

    def test_rollback_is_refused(self):
        response = self.ada_client.post(
            self.rollback_url(), {"reason": "wrong file"}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.rollback_started_at)
        self.assertEqual(self.job.status, ImportJobStatusChoices.SUCCEEDED)

    def test_delete_is_refused(self):
        response = self.ada_client.delete(self.batch_url())

        self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(ImportBatch.all_objects.filter(pk=self.batch.pk).exists())

    def test_rewriting_the_batch_is_refused(self):
        response = self.ada_client.patch(
            self.batch_url(), {"notes": "edited"}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.content)

    def test_the_administrative_reads_are_refused(self):
        for suffix in (
            f"jobs/{self.job.pk}/rollbacks/",
            "audit-logs/",
            "notifications/",
        ):
            with self.subTest(endpoint=suffix):
                response = self.ada_client.get(self.batch_url(suffix))
                self.assertEqual(response.status_code, 403, response.content)

    def test_resolving_an_issue_in_place_is_refused(self):
        """A school fixes the file and uploads it again; the issue is not waved away."""
        issue = ImportValidationIssue.objects.create(
            import_batch=self.batch,
            severity=ValidationSeverityChoices.ERROR,
            code=ValidationCodeChoices.choices[0][0],
            message="Class not found.",
        )

        response = self.ada_client.patch(
            self.batch_url(f"issues/{issue.pk}/resolve/"), {}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        issue.refresh_from_db()
        self.assertFalse(issue.is_resolved)

    def test_the_refusal_is_the_same_for_a_batch_that_does_not_exist(self):
        """Refused actions answer before any lookup, so ids cannot be probed."""
        response = self.ada_client.delete("/v1/import/batches/9999999/")

        self.assertEqual(response.status_code, 403, response.content)


class EngineRollbackKeyTests(_DatasetKeyFixture):
    """Rolling back stays open to whoever holds the engine's key for it."""

    def test_the_rollback_key_rolls_back(self):
        obi = make_school_admin(
            None, email="obi.eze@codex.test", tenant=self.tenant,
        )
        _grant(self.tenant, obi, ImportPermission.ROLLBACK_RUN, name="Import support")

        response = TenantAPIClient(user=obi).post(
            self.rollback_url(), {"reason": "wrong file"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["was_successful"])

    def test_the_rollback_key_and_the_dataset_key_together_roll_back(self):
        """The engine key decides; holding the dataset key as well changes nothing."""
        _grant(self.tenant, self.ada, ImportPermission.ROLLBACK_RUN, name="Support desk")

        response = self.ada_client.post(
            self.rollback_url(), {"reason": "wrong file"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)


class DatasetKeyCrossTenantTests(_DatasetKeyFixture):
    """Chika holds the students key at Greenfield. Bright Star's batch is not hers."""

    def setUp(self):
        super().setUp()
        greenfield = make_school(slug="greenfield-keys", name="Greenfield School")
        self.chika = make_school_admin(
            None, email="chika.nwosu@greenfield.test", tenant=greenfield.tenant,
        )
        _grant(greenfield.tenant, self.chika, STUDENTS_IMPORT_KEY, name="Registrar")
        self.chika_client = TenantAPIClient(user=self.chika)

    def test_she_cannot_validate_or_read_another_schools_batch(self):
        for method, suffix in (("post", "validate/"), ("get", ""), ("get", "download/")):
            with self.subTest(endpoint=suffix or "detail"):
                response = getattr(self.chika_client, method)(self.batch_url(suffix))
                self.assertEqual(response.status_code, 403, response.content)

    def test_she_cannot_roll_back_another_schools_import(self):
        response = self.chika_client.post(
            self.rollback_url(), {"reason": "x"}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.rollback_started_at)

    def test_the_rollback_key_on_her_own_school_does_not_reach_bright_star(self):
        _grant(
            self.chika.tenant, self.chika, ImportPermission.ROLLBACK_RUN,
            name="Import support",
        )

        response = self.chika_client.post(
            self.rollback_url(), {"reason": "x"}, format="json",
        )

        self.assertEqual(response.status_code, 404, response.content)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.rollback_started_at)
