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
    make_school_admin,
    make_vision_user,
)

from .models import (
    DatasetTypeChoices,
    FileFormatChoices,
    ImportBatch,
    ImportBatchStatusChoices,
    ImportTemplate,
    ImportTemplateColumn,
    TemplateColumnDataTypeChoices,
)
from .services.validation_service import validate_import_batch


class ImportValidationPublishGateTests(TestCase):
    def test_one_valid_row_does_not_hide_another_rows_validation_error(self):
        tenant = codex_tenant()
        user = make_vision_user(email="import-validation-gate@test.com", tenant=tenant)
        template = ImportTemplate.objects.create(
            code="publish-gate-test",
            name="Publish Gate",
            dataset_type=DatasetTypeChoices.CX_USERS,
            default_file_format=FileFormatChoices.CSV,
        )
        ImportTemplateColumn.objects.create(
            template=template,
            column_name="Name",
            target_field="name",
            data_type=TemplateColumnDataTypeChoices.STRING,
            is_required=True,
        )
        batch = ImportBatch.objects.create(
            tenant=tenant,
            uploaded_by=user,
            template=template,
            dataset_type=DatasetTypeChoices.CX_USERS,
            file=SimpleUploadedFile("gate.csv", b"Name\nValid\n\n"),
            file_format=FileFormatChoices.CSV,
            original_filename="gate.csv",
            total_rows=2,
            total_columns=1,
            uploaded_headers=["Name"],
            preview_rows=[{"Name": "Valid"}, {"Name": ""}],
        )

        result = validate_import_batch(batch)
        batch.refresh_from_db()

        self.assertEqual(result["summary"]["error_count"], 1)
        self.assertTrue(batch.has_critical_errors)
        self.assertFalse(batch.is_ready_for_import)
        self.assertEqual(batch.status, ImportBatchStatusChoices.VALIDATION_FAILED)


class ImportBatchCancellationTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="cancel-import-school")
        self.branch = make_branch(self.school)
        self.user = make_school_admin(
            self.branch,
            email="cancel-import@test.com",
        )
        role = make_role(self.school.tenant, name="Import uploader")
        make_role_permission(role, make_permission("import.batches.create"))
        make_assignment(self.school.tenant, self.user, role)
        self.client = TenantAPIClient(user=self.user)
        self.template = ImportTemplate.objects.create(
            code="cancel-import-template",
            name="Cancel Import",
            dataset_type=DatasetTypeChoices.CX_USERS,
            default_file_format=FileFormatChoices.CSV,
        )

    def make_batch(self, *, status=ImportBatchStatusChoices.READY_TO_IMPORT):
        return ImportBatch.objects.create(
            tenant=self.school.tenant,
            uploaded_by=self.user,
            template=self.template,
            dataset_type=DatasetTypeChoices.CX_USERS,
            file=SimpleUploadedFile("cancel.csv", b"Name\nOne\n"),
            file_format=FileFormatChoices.CSV,
            original_filename="cancel.csv",
            status=status,
            total_rows=1,
            total_columns=1,
            uploaded_headers=["Name"],
            preview_rows=[{"Name": "One"}],
            is_ready_for_import=status == ImportBatchStatusChoices.READY_TO_IMPORT,
        )

    def test_uploader_can_cancel_before_execution(self):
        batch = self.make_batch()

        response = self.client.post(f"/v1/import/batches/{batch.pk}/cancel/")

        self.assertEqual(response.status_code, 200, response.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatchStatusChoices.CANCELLED)
        self.assertFalse(batch.is_ready_for_import)

    def test_running_import_cannot_be_cancelled(self):
        batch = self.make_batch(status=ImportBatchStatusChoices.IMPORT_RUNNING)

        response = self.client.post(f"/v1/import/batches/{batch.pk}/cancel/")

        self.assertEqual(response.status_code, 400, response.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatchStatusChoices.IMPORT_RUNNING)

    def test_other_tenant_cannot_cancel_batch_by_id(self):
        batch = self.make_batch()
        other_school = make_school(slug="other-cancel-import-school")
        other_branch = make_branch(other_school)
        other_user = make_school_admin(
            other_branch,
            email="other-cancel-import@test.com",
        )
        role = make_role(other_school.tenant, name="Other import uploader")
        make_role_permission(role, make_permission("import.batches.create"))
        make_assignment(other_school.tenant, other_user, role)
        other_client = TenantAPIClient(user=other_user)

        response = other_client.post(f"/v1/import/batches/{batch.pk}/cancel/")

        self.assertEqual(response.status_code, 404, response.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatchStatusChoices.READY_TO_IMPORT)
