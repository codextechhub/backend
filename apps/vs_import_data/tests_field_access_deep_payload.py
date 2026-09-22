"""No uploaded row, parsed preview or validation rule reaches a caller who may read none.

Every declared surface of ``import.templates``, ``import.jobs`` and
``import.batches`` is rendered on a real template, batch, job and row result
whose registered fields are filled in, as a caller whose role has every field
of that resource switched off. The whole payload is walked, so a job's row
results and a batch's template, both nested, are held to their own switches.

Batches and jobs are rendered in a tenant with two branches, the batch posted
to one of them, and in one with a single branch. Templates are platform
configuration and are rendered on the platform tenant.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin, platform_tenant

from .models import (
    DatasetTypeChoices,
    FileFormatChoices,
    ImportBatch,
    ImportJob,
    ImportJobRowResult,
    ImportRowActionChoices,
    ImportTemplate,
)

_SURFACE = "vs_import_data.serializers."


class ImportDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({"import.templates", "import.jobs", "import.batches"})
    covers = frozenset({
        _SURFACE + "ImportTemplateDetailSerializer",
        _SURFACE + "ImportBatchDetailSerializer",
        _SURFACE + "ImportJobDetailSerializer",
        _SURFACE + "ImportJobRowResultSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        cls.platform = platform_tenant()
        cls.template = ImportTemplate.objects.create(
            code="deep-payload-users", name="Users",
            dataset_type=DatasetTypeChoices.CX_USERS,
            default_file_format=FileFormatChoices.CSV,
            validation_rules={"allow_duplicate_emails": False},
        )
        multi = make_school(slug="deep-import-multi", name="Corona Group").tenant
        ikeja = make_branch(multi, name="Ikeja Branch")
        make_branch(multi, name="Lekki Branch", is_main=False)
        solo = make_school(slug="deep-import-solo", name="Single Site").tenant
        make_branch(solo, name="Main Branch")
        cls.tenant = multi
        cls.records = [
            (multi, *cls._rows(multi, ikeja, "multi")),
            (solo, *cls._rows(solo, None, "solo")),
        ]

    @classmethod
    def _rows(cls, tenant, branch, slug):
        uploader = make_school_admin(
            None, email=f"uploader-{slug}@deep-import.test", tenant=tenant,
        )
        row = {"Name": "Ngozi Okafor", "Email": "ngozi@example.ng"}
        batch = ImportBatch.objects.create(
            tenant=tenant, branch=branch, uploaded_by=uploader, template=cls.template,
            dataset_type=DatasetTypeChoices.CX_USERS,
            file=SimpleUploadedFile("people.csv", b"Name,Email\nNgozi Okafor,ngozi@example.ng\n"),
            file_format=FileFormatChoices.CSV, original_filename="people.csv",
            total_rows=1, total_columns=2, uploaded_headers=["Name", "Email"],
            preview_rows=[row],
        )
        job = ImportJob.objects.create(
            import_batch=batch, queued_by=uploader, total_rows=1,
            last_error_code="ROW_FAILED", last_error_message="Row 2 could not be saved.",
            execution_summary={"created": 1, "rows": [row]},
        )
        result = ImportJobRowResult.objects.create(
            job=job, row_number=2, action=ImportRowActionChoices.CREATE,
            error_details={"email": "Already taken."}, row_payload=row,
            normalized_payload={"name": "Ngozi Okafor", "email": "ngozi@example.ng"},
        )
        return batch, job, result

    def samples(self):
        samples = [Sample(
            _SURFACE + "ImportTemplateDetailSerializer",
            ImportTemplate.objects.get(pk=self.template.pk), tenant=self.platform,
        )]
        for tenant, batch, job, result in self.records:
            samples += [
                Sample(_SURFACE + "ImportBatchDetailSerializer",
                       ImportBatch.objects.get(pk=batch.pk), tenant=tenant),
                Sample(_SURFACE + "ImportJobDetailSerializer",
                       ImportJob.objects.get(pk=job.pk), tenant=tenant),
                Sample(_SURFACE + "ImportJobRowResultSerializer",
                       ImportJobRowResult.objects.get(pk=result.pk), tenant=tenant),
            ]
        return samples
