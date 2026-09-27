"""No uploaded row, parsed preview or validation rule reaches a caller who may read none.

Every declared surface of ``import.templates``, ``import.jobs`` and
``import.batches`` is rendered on a real template, batch, job and row result
whose registered fields are filled in, as a caller whose role has every field
of that resource switched off. The whole payload is walked, so a job's row
results and a batch's template, both nested, are held to their own switches.

Batches and jobs are rendered in a tenant with two branches, the batch posted
to one of them, and in one with a single branch. Templates are platform
configuration and are rendered on the platform tenant.

The routes that serve part of a batch without rendering its detail are held to
the same switches over HTTP: the file download to ``import.batches.file``, and
every place a validation issue quotes a cell of the upload to
``import.batches.preview_rows``.
"""
from __future__ import annotations

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.test_utils import TenantAPIClient

from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    platform_tenant,
    set_field_access,
)

from .constants import ImportPermission

from .models import (
    DatasetTypeChoices,
    FileFormatChoices,
    ImportBatch,
    ImportJob,
    ImportJobRowResult,
    ImportRowActionChoices,
    ImportTemplate,
    ImportValidationIssue,
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


class BatchRoutesFollowTheDetailsSwitchesTests(TestCase):
    """Bright Star's Ikeja office reads its import batches under a role's switches.

    Funmi's role opens the batch screens with ``import.batches.view`` and the
    issue screens with ``import.validations.view``. Whether she sees the file
    and the rows inside a batch is the role's switches' question, and every
    route that serves either must answer it as the batch detail does.
    """

    ROW = {"Name": "Ngozi Okafor", "Email": "ngozi@example.ng"}

    def setUp(self):
        school = make_school(slug="bright-star-fls", name="Bright Star School")
        self.tenant = school.tenant
        ikeja = make_branch(school, name="Ikeja Branch")
        make_branch(school, name="Lekki Branch", is_main=False)
        self.keys = install_declared_fields("import.batches")

        self.funmi = make_school_admin(None, email="funmi@bright-star.test", tenant=self.tenant)
        self.role = make_role(self.tenant, name="Import reader")
        for key in (ImportPermission.BATCH_VIEW, ImportPermission.VALIDATION_VIEW,
                    ImportPermission.BATCH_VALIDATE):
            make_role_permission(self.role, make_permission(key))
        make_assignment(self.tenant, self.funmi, self.role, branch=None)
        set_field_access(self.role, *self.keys, read=True, write=False)
        self.client = TenantAPIClient(user=self.funmi)

        template = ImportTemplate.objects.create(
            code="fls-routes-users", name="Users",
            dataset_type=DatasetTypeChoices.CX_USERS,
            default_file_format=FileFormatChoices.CSV,
        )
        self.batch = ImportBatch.objects.create(
            tenant=self.tenant, branch=ikeja, uploaded_by=self.funmi, template=template,
            dataset_type=DatasetTypeChoices.CX_USERS,
            file=SimpleUploadedFile("people.csv", b"Name,Email\nNgozi Okafor,ngozi@example.ng\n"),
            file_format=FileFormatChoices.CSV, original_filename="people.csv",
            total_rows=1, total_columns=2, uploaded_headers=["Name", "Email"],
            preview_rows=[self.ROW],
        )
        self.issue = ImportValidationIssue.objects.create(
            import_batch=self.batch, severity="error", code="invalid_format",
            message="Email is not a valid address.", row_number=2, column_name="Email",
            raw_value="ngozi@example.ng", normalized_value="ngozi@example.ng",
            metadata={"reported": "ngozi@example.ng"},
        )

    def switch_off(self, name):
        set_field_access(self.role, f"import.batches.{name}", read=False, write=False)

    def url(self, suffix=""):
        return f"/v1/import/batches/{self.batch.pk}/{suffix}"

    # -- the file ---------------------------------------------------------- #

    def test_the_file_downloads_while_the_switch_is_on(self):
        response = self.client.get(self.url("download/"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ngozi@example.ng", b"".join(response.streaming_content))

    def test_the_file_is_refused_while_the_switch_is_off(self):
        self.switch_off("file")

        response = self.client.get(self.url("download/"))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_read_denied")
        self.assertNotIn(b"ngozi@example.ng", response.content)

    def test_the_detail_and_the_download_agree(self):
        """The detail leaves the file out for exactly the reader the download refuses."""
        for readable in (True, False):
            set_field_access(self.role, "import.batches.file", read=readable, write=False)
            with self.subTest(readable=readable):
                detail = self.client.get(self.url())
                download = self.client.get(self.url("download/"))
                self.assertEqual(detail.status_code, 200)
                self.assertEqual("file" in detail.data["data"], readable)
                self.assertEqual(download.status_code, 200 if readable else 403)

    def test_the_refusal_says_nothing_about_whether_a_file_is_held(self):
        """Asked before the file is looked for, so an empty batch refuses alike."""
        self.switch_off("file")
        ImportBatch.objects.filter(pk=self.batch.pk).update(file="")

        response = self.client.get(self.url("download/"))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "field_read_denied")

    def test_the_finance_importers_owner_rule_opens_the_file_as_it_opens_the_detail(self):
        """The detail's own rule, reused: the statement importer reads what she uploaded."""
        self.switch_off("file")
        make_role_permission(self.role, make_permission("finance.bankaccount.import"))

        self.assertEqual(self.client.get(self.url("download/")).status_code, 200)
        self.assertIn("file", self.client.get(self.url()).data["data"])

    def test_the_rows_switch_does_not_decide_the_file(self):
        self.switch_off("preview_rows")

        self.assertEqual(self.client.get(self.url("download/")).status_code, 200)

    # -- the rows, quoted by validation issues ------------------------------ #

    def test_an_issue_quotes_its_cell_while_the_rows_switch_is_on(self):
        response = self.client.get(self.url(f"issues/{self.issue.pk}/"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["raw_value"], "ngozi@example.ng")

    def test_an_issue_leaves_its_cell_out_while_the_rows_switch_is_off(self):
        self.switch_off("preview_rows")

        response = self.client.get(self.url(f"issues/{self.issue.pk}/"))

        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        for name in ("raw_value", "normalized_value", "metadata"):
            self.assertNotIn(name, data)
        self.assertEqual(data["message"], "Email is not a valid address.")
        self.assertNotIn("ngozi@example.ng", str(data))

    def test_the_issues_export_quotes_cells_while_the_rows_switch_is_on(self):
        response = self.client.get(self.url("issues/export/"))

        self.assertEqual(response.status_code, 200)
        header = response.content.decode().splitlines()[0]
        self.assertIn("Raw Value", header)
        self.assertIn(b"ngozi@example.ng", response.content)

    def test_the_issues_export_drops_the_cell_column_while_the_rows_switch_is_off(self):
        self.switch_off("preview_rows")

        response = self.client.get(self.url("issues/export/"))

        self.assertEqual(response.status_code, 200)
        lines = response.content.decode().splitlines()
        self.assertNotIn("Raw Value", lines[0])
        self.assertIn("Email is not a valid address.", lines[1])
        self.assertNotIn(b"ngozi@example.ng", response.content)

    def test_a_validation_run_leaves_the_cells_out_while_the_rows_switch_is_off(self):
        self.switch_off("preview_rows")
        result = {"summary": {"errors": 1}, "issues": [{
            "severity": "error", "code": "invalid_format", "row_number": 2,
            "column_name": "Email", "message": "Email is not a valid address.",
            "raw_value": "ngozi@example.ng",
        }]}

        with mock.patch("vs_import_data.views.validate_import_batch", return_value=result):
            response = self.client.post(self.url("validate/"), {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        issue = response.data["data"]["issues"][0]
        self.assertNotIn("raw_value", issue)
        self.assertEqual(issue["message"], "Email is not a valid address.")

    def test_a_validation_run_quotes_the_cells_while_the_rows_switch_is_on(self):
        result = {"summary": {"errors": 1}, "issues": [{
            "severity": "error", "code": "invalid_format", "row_number": 2,
            "column_name": "Email", "message": "Email is not a valid address.",
            "raw_value": "ngozi@example.ng",
        }]}

        with mock.patch("vs_import_data.views.validate_import_batch", return_value=result):
            response = self.client.post(self.url("validate/"), {}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["issues"][0]["raw_value"], "ngozi@example.ng")
