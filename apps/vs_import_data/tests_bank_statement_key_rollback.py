"""The bank-statement import key rolls back its own statements, and nothing else.

Finance refuses to edit a bulk-imported statement line by line and tells the
bursar to roll it back and import it again. So ``finance.bankaccount.import``
declares ``import.rollbacks.run`` as an extra engine key for the
``bank_statements`` dataset: on a statement batch it reaches the rollback, and
on any other dataset's batch it reaches nothing past the wizard.

The people:

- Ngozi Adeyemi, bursar, holds ``finance.bankaccount.import`` and nothing from
  the import engine. She has loaded March's statement into the April account.
- Emeka Obi, bursar at Greenfield School, holds the same key on his own school.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from core.test_utils import TenantAPIClient
from vs_finance.constants import BankLineStatus
from vs_finance.models import BankStatement, BankStatementLine
from vs_finance.tests import _Phase4FixtureMixin
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)

from . import permissions as import_permissions
from .constants import ImportPermission
from .models import (
    DatasetTypeChoices,
    FileFormatChoices,
    ImportBatch,
    ImportBatchStatusChoices,
    ImportJob,
    ImportJobStatusChoices,
    ImportTemplate,
)

BANK_IMPORT_KEY = "finance.bankaccount.import"
STUDENTS_IMPORT_KEY = "school.students.import"

HEADERS = (
    "Transaction Date,Description,Reference,Money In,Money Out,"
    "Transaction ID,Balance\n"
)
ROWS = (
    "2026-03-02,Customer receipt,RCPT-1,500.00,,TX-1,1500.00\n"
    "2026-03-03,Bank fee,FEE-1,,50.00,TX-2,1450.00\n"
)


def _grant(tenant, user, key, *, name):
    """Give *user* a tenant-wide role holding exactly *key* on *tenant*."""
    role = make_role(tenant, name=name)
    make_role_permission(role, make_permission(key))
    make_assignment(tenant, user, role)


class _PublishedStatementFixture(_Phase4FixtureMixin, TestCase):
    """Ngozi's March statement, published into the April account through the wizard."""

    def setUp(self):
        call_command(
            "seed_import",
            dataset_type=DatasetTypeChoices.BANK_STATEMENTS,
            verbosity=0,
        )
        self.tenant = codex_tenant()
        self.entity, _, _ = self.build_books()
        self.bank = self.make_bank(self.entity)

        self.ngozi = make_vision_user(
            email="ngozi.adeyemi@books.test", tenant=self.tenant,
        )
        _grant(self.tenant, self.ngozi, BANK_IMPORT_KEY, name="Bursar")
        self.ngozi_client = TenantAPIClient(user=self.ngozi)

        self.batch_id, self.job_id = self.publish_statement()

    def publish_statement(self):
        upload = self.ngozi_client.post(
            (
                f"/v1/finance/bank-accounts/{self.bank.pk}/statement-imports/"
                f"?entity={self.entity.code}"
            ),
            {
                "file": SimpleUploadedFile(
                    "march.csv", (HEADERS + ROWS).encode(), content_type="text/csv",
                ),
                "statement_date": "2026-03-31",
                "period_label": "March 2026",
                "opening_balance": "1000.00",
                "closing_balance": "1450.00",
            },
            format="multipart",
        )
        self.assertEqual(upload.status_code, 201, upload.content)
        batch_id = upload.json()["data"]["id"]

        publish = self.ngozi_client.post(
            f"/v1/import/batches/{batch_id}/start-import/",
            {"run_async": False},
            format="json",
        )
        self.assertEqual(publish.status_code, 200, publish.content)
        return batch_id, publish.json()["data"]["job_id"]

    def rollback_url(self, batch_id=None, job_id=None):
        return (
            f"/v1/import/batches/{batch_id or self.batch_id}"
            f"/jobs/{job_id or self.job_id}/rollback/"
        )

    def make_students_batch(self):
        """A students roll on the same tenant, imported by somebody else."""
        template = ImportTemplate.objects.create(
            code="bank-key-students",
            name="Students",
            dataset_type=DatasetTypeChoices.STUDENTS,
            default_file_format=FileFormatChoices.CSV,
        )
        batch = ImportBatch.objects.create(
            tenant=self.tenant,
            uploaded_by=self.ngozi,
            template=template,
            dataset_type=DatasetTypeChoices.STUDENTS,
            file=SimpleUploadedFile("roll.csv", b"First Name\nTunde\n"),
            file_format=FileFormatChoices.CSV,
            original_filename="roll.csv",
            status=ImportBatchStatusChoices.IMPORT_SUCCEEDED,
            total_rows=1,
            total_columns=1,
            uploaded_headers=["First Name"],
            preview_rows=[{"First Name": "Tunde"}],
        )
        job = ImportJob.objects.create(
            import_batch=batch,
            queued_by=self.ngozi,
            status=ImportJobStatusChoices.SUCCEEDED,
            total_rows=0,
            processed_rows=0,
            succeeded_rows=0,
        )
        return batch, job


class BankKeyRollsBackItsStatementTests(_PublishedStatementFixture):
    """Ngozi takes the misfiled statement off again, so she can import it where it belongs."""

    def test_she_can_roll_back_the_statement(self):
        response = self.ngozi_client.post(
            self.rollback_url(), {"reason": "March loaded into April"}, format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["reverted_rows_count"], 2)
        self.assertFalse(BankStatement.objects.exists())
        self.assertFalse(BankStatementLine.objects.exists())

    def test_a_statement_with_an_acted_on_line_still_refuses(self):
        """The key opens the rollback; finance's own guard still decides it."""
        BankStatementLine.objects.filter(
            pk=BankStatementLine.objects.order_by("id").values("pk")[:1],
        ).update(status=BankLineStatus.IGNORED)

        response = self.ngozi_client.post(
            self.rollback_url(), {"reason": "too late"}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(BankStatementLine.objects.count(), 2)


class BankKeyStopsAtTheRollbackTests(_PublishedStatementFixture):
    """The rollback is the one thing past the wizard the bank key reaches."""

    def test_delete_is_refused(self):
        response = self.ngozi_client.delete(f"/v1/import/batches/{self.batch_id}/")

        self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(ImportBatch.all_objects.filter(pk=self.batch_id).exists())
        self.assertEqual(BankStatementLine.objects.count(), 2)

    def test_the_administrative_reads_are_refused(self):
        for suffix in (
            f"jobs/{self.job_id}/rollbacks/",
            "audit-logs/",
            "notifications/",
        ):
            with self.subTest(endpoint=suffix):
                response = self.ngozi_client.get(
                    f"/v1/import/batches/{self.batch_id}/{suffix}",
                )
                self.assertEqual(response.status_code, 403, response.content)

    def test_she_cannot_roll_back_a_students_batch(self):
        batch, job = self.make_students_batch()

        response = self.ngozi_client.post(
            self.rollback_url(batch.pk, job.pk), {"reason": "x"}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        job.refresh_from_db()
        self.assertIsNone(job.rollback_started_at)
        self.assertEqual(job.status, ImportJobStatusChoices.SUCCEEDED)

    def test_holding_the_students_key_as_well_does_not_roll_back_students(self):
        """The rollback belongs to the bank dataset, not to whoever holds two keys."""
        _grant(self.tenant, self.ngozi, STUDENTS_IMPORT_KEY, name="Registrar")
        batch, job = self.make_students_batch()

        response = self.ngozi_client.post(
            self.rollback_url(batch.pk, job.pk), {"reason": "x"}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        job.refresh_from_db()
        self.assertIsNone(job.rollback_started_at)


class BankKeyCrossTenantTests(_PublishedStatementFixture):
    """Emeka holds the bank key at Greenfield. Ngozi's statement is not his."""

    def test_he_cannot_roll_back_another_tenants_statement(self):
        greenfield = make_school(slug="greenfield-bank-key", name="Greenfield School")
        emeka = make_school_admin(
            None, email="emeka.obi@greenfield.test", tenant=greenfield.tenant,
        )
        _grant(greenfield.tenant, emeka, BANK_IMPORT_KEY, name="Bursar")

        response = TenantAPIClient(user=emeka).post(
            self.rollback_url(), {"reason": "x"}, format="json",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(BankStatementLine.objects.count(), 2)


class RegisterDatasetImportKeyTests(SimpleTestCase):
    """What a registration declares is what its key covers, and a re-registration replaces it."""

    DATASET = "registration_probe"

    def tearDown(self):
        import_permissions._DATASET_IMPORT_KEYS.pop(self.DATASET, None)
        import_permissions._DATASET_EXTRA_ENGINE_KEYS.pop(self.DATASET, None)

    def test_a_plain_registration_covers_the_wizard_alone(self):
        import_permissions.register_dataset_import_key(self.DATASET, "probe.import")

        self.assertEqual(
            import_permissions._stand_in_keys(self.DATASET),
            import_permissions._WIZARD_KEYS,
        )

    def test_declared_extra_keys_are_added_and_a_re_registration_drops_them(self):
        import_permissions.register_dataset_import_key(
            self.DATASET, "probe.import",
            extra_engine_keys=[ImportPermission.ROLLBACK_RUN],
        )
        self.assertIn(
            ImportPermission.ROLLBACK_RUN,
            import_permissions._stand_in_keys(self.DATASET),
        )

        import_permissions.register_dataset_import_key(self.DATASET, "probe.import")
        self.assertNotIn(
            ImportPermission.ROLLBACK_RUN,
            import_permissions._stand_in_keys(self.DATASET),
        )

    def test_school_datasets_declare_nothing_past_the_wizard(self):
        for dataset in ("students", "guardians", "staff", "academic_structure", "subjects"):
            with self.subTest(dataset=dataset):
                self.assertEqual(
                    import_permissions._stand_in_keys(dataset),
                    import_permissions._WIZARD_KEYS,
                )

    def test_bank_statements_declare_the_rollback_and_nothing_else(self):
        self.assertEqual(
            import_permissions._stand_in_keys("bank_statements")
            - import_permissions._WIZARD_KEYS,
            frozenset({ImportPermission.ROLLBACK_RUN}),
        )
