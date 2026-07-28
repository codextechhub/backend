from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from vs_rbac.tests.helpers import codex_tenant, make_vision_user

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
