"""Tests for the Export Centre.

Ordered the way the pre-ship checklist asks: the security-critical cases first
(permission denied, cross-tenant isolation, sensitive-field handling, download
authorisation and its log), then the happy path, then every branch the design names -
omissions, failure codes, idempotency, the concurrency cap, cancellation and expiry.

Run against the local database::

    ../cx/bin/python manage.py test vs_exports --settings=apps.settings.local
"""
from __future__ import annotations

import datetime
from unittest import mock
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_finance.models import Account, Customer, Invoice, InvoiceLine, LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_tenants.models import Tenant

from vs_exports import analytics, engine, services
from vs_exports.catalogue import get_dataset
from vs_exports.constants import (
    AuditAction,
    DatasetScope,
    DeliveryState,
    DownloadOutcome,
    DownloadRefusal,
    ExportFormat,
    ExportPermission,
    FailureCode,
    OmissionCode,
    RunStatus,
    RunTrigger,
    ValuesMode,
)
from vs_exports.serializers import ExportRunDetailSerializer
from vs_exports.models import (
    ExportAnalyticsEvent,
    ExportDefinition,
    ExportDownload,
    ExportFile,
    ExportRun,
)

User = get_user_model()

DATASET = "finance.customer_invoices"
COLUMNS = ["document_number", "customer_name", "invoice_date", "total"]


# --------------------------------------------------------------------------- #
# Fixture                                                                     #
# --------------------------------------------------------------------------- #
class _ExportFixture:
    """A tenant with an entity, four posted-looking invoices, and three users.

    ``self.admin`` is a Vision super admin (bypasses RBAC, used for happy paths),
    ``self.analyst`` holds the ordinary export keys plus the finance read key but
    **not** ``exports.sensitive_field.export``, and ``self.outsider`` belongs to a
    different tenant entirely.
    """

    def build(self):
        call_command("seed_actions", verbosity=0)
        call_command("seed_exports_permissions", verbosity=0)
        call_command("seed_finance_permissions", verbosity=0)

        self.tenant = Tenant.objects.get(slug="codex")
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Lekki Books", code="LEKKI", kind=LedgerEntity.Kind.TENANT,
            tenant=self.tenant,
        )
        seed_chart_of_accounts(self.entity)
        self.customer = Customer.objects.create(
            entity=self.entity, code="CUST1", name="Northgate Logistics",
            billing_email="ap@northgate.example",
            receivable_account=Account.objects.get(entity=self.entity, code="1200"),
        )
        self.today = datetime.date.today()
        for offset, amount in enumerate((1_240_000_00, 318_500_00, 2_004_750_00, 96_200_00)):
            invoice = Invoice.objects.create(
                entity=self.entity, customer=self.customer,
                invoice_date=self.today - datetime.timedelta(days=offset),
                due_date=self.today + datetime.timedelta(days=30),
                total=amount, subtotal=amount,
            )
            InvoiceLine.objects.create(
                invoice=invoice,
                revenue_account=Account.objects.get(entity=self.entity, code="4100"),
                quantity=1, unit_price=amount, net_amount=amount, line_no=1,
            )

        self.admin = self._user("admin@test.com", role="xvs_super_admin")
        self.analyst = self._user("analyst@test.com", role="exports_analyst", keys=[
            ExportPermission.CATALOGUE_VIEW, ExportPermission.DEFINITION_VIEW,
            ExportPermission.DEFINITION_CREATE, ExportPermission.DEFINITION_UPDATE,
            ExportPermission.DEFINITION_DELETE, ExportPermission.DEFINITION_SHARE,
            ExportPermission.RUN_VIEW, ExportPermission.RUN_CREATE,
            ExportPermission.RUN_CANCEL, ExportPermission.FILE_DOWNLOAD,
            "finance.invoice.view",
        ])
        # No export keys at all - the 403 case.
        self.stranger = self._user("stranger@test.com", role="no_exports", keys=[])

        self.other_tenant = Tenant.objects.create(
            name="Other Org", slug="other-org", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        self.outsider = self._user(
            "outsider@test.com", role="xvs_super_admin", tenant=self.other_tenant,
        )

    def _user(self, email, *, role, keys=None, tenant=None):
        from vs_rbac.models import (
            Permission, TenantRolePermission, TenantRoleTemplate, TenantUserRoleAssignment,
        )

        tenant = tenant or Tenant.objects.get(slug="codex")
        user = User.objects.create_user(
            email=email, password="testpass123", user_type="CX_STAFF",
            status="ACTIVE", first_name=email.split("@")[0], last_name="Tester",
            tenant=tenant,
        )
        template, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=tenant, key=role,
            defaults={"name": role, "status": "ACTIVE"},
        )
        if keys:
            for permission in Permission.objects.filter(key__in=keys):
                TenantRolePermission.objects.get_or_create(
                    role=template, permission=permission, defaults={"granted": True},
                )
        TenantUserRoleAssignment.objects.create(
            tenant=tenant, user=user, role=template, assignment_status="ACTIVE",
        )
        return user

    # A saved, runnable definition owned by ``owner``.
    def make_definition(self, *, owner=None, columns=None, fmt=ExportFormat.CSV,
                        filters=None, name="Customer invoices"):
        return ExportDefinition.objects.create(
            tenant=self.tenant, entity=self.entity, dataset_key=DATASET, name=name,
            columns=columns if columns is not None else list(COLUMNS),
            filters=filters if filters is not None else [{
                "id": "invoice_date",
                "start": (self.today - datetime.timedelta(days=30)).isoformat(),
                "end": (self.today + datetime.timedelta(days=1)).isoformat(),
            }],
            format=fmt,
            format_options=services.clean_format_options(fmt, {}),
            values_mode=ValuesMode.SYSTEM,
            file_name_pattern="customer-invoices-{date}",
            owner=owner or self.admin,
        )


# --------------------------------------------------------------------------- #
# Security: permissions and tenant isolation                                  #
# --------------------------------------------------------------------------- #
class ExportPermissionTests(_ExportFixture, TestCase):
    """No export key means no Export Centre - on every route that changes anything."""

    def setUp(self):
        self.build()

    def test_catalogue_requires_permission(self):
        client = TenantAPIClient(user=self.stranger)
        self.assertEqual(client.get("/v1/exports/catalogue/").status_code, 403)

    def test_run_requires_permission(self):
        definition = self.make_definition()
        client = TenantAPIClient(user=self.stranger)
        response = client.post(f"/v1/exports/definitions/{definition.pk}/run/", {}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_activity_is_admin_only(self):
        """The analyst holds every ordinary key but not exports.activity.view."""
        client = TenantAPIClient(user=self.analyst)
        self.assertEqual(client.get("/v1/exports/activity/").status_code, 403)

    def test_sensitive_fields_hidden_without_the_key(self):
        """The picker never offers a column that would be dropped at run time."""
        response = TenantAPIClient(user=self.analyst).get(f"/v1/exports/catalogue/{DATASET}/")
        self.assertEqual(response.status_code, 200)
        ids = {f["id"] for f in response.json()["data"]["fields"]}
        self.assertNotIn("customer_email", ids)
        self.assertIn("customer_name", ids)

    def test_sensitive_fields_offered_to_a_holder(self):
        response = TenantAPIClient(user=self.admin).get(f"/v1/exports/catalogue/{DATASET}/")
        ids = {f["id"] for f in response.json()["data"]["fields"]}
        self.assertIn("customer_email", ids)


class ExportTenantIsolationTests(_ExportFixture, TestCase):
    """Changing a pk in the URL must never reach another tenant's export."""

    def setUp(self):
        self.build()

    def test_definition_from_another_tenant_is_not_found(self):
        definition = self.make_definition()
        client = TenantAPIClient(user=self.outsider, tenant_slug=self.other_tenant.slug)
        self.assertEqual(
            client.get(f"/v1/exports/definitions/{definition.pk}/").status_code, 404,
        )

    def test_run_from_another_tenant_is_not_found(self):
        definition = self.make_definition()
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        client = TenantAPIClient(user=self.outsider, tenant_slug=self.other_tenant.slug)
        self.assertEqual(client.get(f"/v1/exports/runs/{run.pk}/").status_code, 404)

    def test_shared_definition_cannot_be_edited_by_the_recipient(self):
        """Sharing grants sight, never edit rights."""
        definition = self.make_definition(owner=self.admin)
        definition.shares.create(user=self.analyst, shared_by=self.admin)
        client = TenantAPIClient(user=self.analyst)
        self.assertEqual(client.get(f"/v1/exports/definitions/{definition.pk}/").status_code, 200)
        response = client.patch(
            f"/v1/exports/definitions/{definition.pk}/", {"name": "Mine now"}, format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_unshared_definition_is_invisible(self):
        definition = self.make_definition(owner=self.admin)
        client = TenantAPIClient(user=self.analyst)
        self.assertEqual(client.get(f"/v1/exports/definitions/{definition.pk}/").status_code, 404)


# --------------------------------------------------------------------------- #
# Catalogue, preview and definition validation                                #
# --------------------------------------------------------------------------- #
class CatalogueAndPreviewTests(_ExportFixture, TestCase):
    def setUp(self):
        self.build()
        self.client = TenantAPIClient(user=self.admin)

    def test_catalogue_lists_modules_including_empty_ones(self):
        data = self.client.get("/v1/exports/catalogue/").json()["data"]
        names = {m["name"] for m in data["modules"]}
        self.assertIn("Finance", names)
        finance = next(m for m in data["modules"] if m["name"] == "Finance")
        self.assertTrue(finance["available"])
        self.assertIn(DATASET, {d["id"] for d in finance["datasets"]})

    def test_preview_returns_estimate_and_sample(self):
        payload = {
            "dataset_key": DATASET,
            "columns": COLUMNS,
            "filters": [{
                "id": "invoice_date",
                "start": (self.today - datetime.timedelta(days=30)).isoformat(),
                "end": (self.today + datetime.timedelta(days=1)).isoformat(),
            }],
            "format": ExportFormat.CSV,
            "values_mode": ValuesMode.PEOPLE,
        }
        response = self.client.post(
            f"/v1/exports/preview/?entity={self.entity.code}", payload, format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["matching_rows"], 4)
        self.assertEqual(data["estimate_confidence"], "exact")
        self.assertEqual(len(data["sample"]["rows"]), 4)
        # People mode renders money as naira, not raw kobo.
        self.assertIn("₦", data["sample"]["rows"][0][-1])
        self.assertIn("Customer invoices", data["reads_as"])

    def test_preview_reports_a_withdrawn_filter_as_a_validation_error(self):
        payload = {
            "dataset_key": DATASET,
            "columns": COLUMNS,
            "filters": [{"id": "settlement_status", "values": ["PENDING"]}],
        }
        response = self.client.post(
            f"/v1/exports/preview/?entity={self.entity.code}", payload, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_required_filter_blocks_a_non_draft(self):
        response = self.client.post(
            f"/v1/exports/definitions/?entity={self.entity.code}",
            {"name": "No filter", "dataset_key": DATASET, "columns": COLUMNS,
             "filters": [], "format": ExportFormat.CSV},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("filters", str(response.json()).lower())

    def test_missing_required_filter_is_allowed_on_a_draft(self):
        response = self.client.post(
            f"/v1/exports/definitions/?entity={self.entity.code}",
            {"name": "Draft", "dataset_key": DATASET, "columns": [], "filters": [],
             "format": ExportFormat.CSV, "is_draft": True},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ExportDefinition.objects.get(name="Draft").is_draft)

    def test_unknown_column_is_rejected(self):
        response = self.client.post(
            f"/v1/exports/definitions/?entity={self.entity.code}",
            {"name": "Bad", "dataset_key": DATASET, "columns": ["not_a_field"],
             "filters": [], "format": ExportFormat.CSV},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_format_options_are_discriminated_by_format(self):
        """Switching format must not leave a CSV delimiter on an Excel export."""
        response = self.client.post(
            f"/v1/exports/definitions/?entity={self.entity.code}",
            {"name": "Excel", "dataset_key": DATASET, "columns": COLUMNS,
             "filters": [{"id": "invoice_date", "start": self.today.isoformat(),
                          "end": self.today.isoformat()}],
             "format": ExportFormat.XLSX,
             "format_options": {"delimiter": ";", "sheet_name": "Invoices"}},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        options = response.json()["data"]["format_options"]
        self.assertNotIn("delimiter", options)
        self.assertEqual(options["sheet_name"], "Invoices")

    def test_owner_cannot_be_set_from_the_payload(self):
        """Mass assignment: owner, tenant and entity come from the request only."""
        response = self.client.post(
            f"/v1/exports/definitions/?entity={self.entity.code}",
            {"name": "Mine", "dataset_key": DATASET, "columns": COLUMNS,
             "filters": [{"id": "invoice_date", "start": self.today.isoformat(),
                          "end": self.today.isoformat()}],
             "format": ExportFormat.CSV, "owner": self.analyst.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ExportDefinition.objects.get(name="Mine").owner_id, self.admin.pk)

    def test_empty_definition_list_returns_a_list(self):
        """success_response coerces [] → {}; the paginated envelope must not."""
        response = self.client.get("/v1/exports/definitions/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], [])
        self.assertEqual(response.json()["pagination"]["totalItems"], 0)


# --------------------------------------------------------------------------- #
# Running                                                                     #
# --------------------------------------------------------------------------- #
class ExportRunTests(_ExportFixture, TestCase):
    """End to end: trigger a run, get a file, get an honest run record."""

    def setUp(self):
        self.build()
        self.client = TenantAPIClient(user=self.admin)

    def test_csv_run_produces_a_file_with_every_row(self):
        definition = self.make_definition()
        run, created = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()

        self.assertTrue(created)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.row_count, 4)

        file = run.file
        self.assertTrue(file.name.endswith(".csv"))
        self.assertEqual(file.row_count, 4)
        self.assertGreater(file.size_bytes, 0)
        self.assertTrue(file.is_downloadable)
        # 30-day availability, opened at completion.
        self.assertGreater(file.available_until, timezone.now() + datetime.timedelta(days=29))

        body = default_storage.open(file.storage_name, "rb").read().decode("utf-8")
        self.assertIn("Invoice number", body)
        self.assertIn("Northgate Logistics", body)
        # System values mode: plain decimal, no currency symbol.
        self.assertIn("1240000.00", body)
        self.assertNotIn("₦", body)

    def test_xlsx_run_produces_a_workbook(self):
        definition = self.make_definition(fmt=ExportFormat.XLSX)
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)
        body = default_storage.open(run.file.storage_name, "rb").read()
        self.assertEqual(body[:2], b"PK")            # a zip container, i.e. a real xlsx

    def test_frozen_config_survives_a_later_edit(self):
        """Editing an export changes future files only."""
        definition = self.make_definition()
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        definition.columns = ["document_number"]
        definition.name = "Renamed"
        definition.save(update_fields=["columns", "name"])

        run.refresh_from_db()
        self.assertEqual(run.frozen_config["columns"], COLUMNS)
        drift = services.config_drift(run)
        self.assertEqual({d["field"] for d in drift}, {"columns", "name"})

    def test_locked_column_is_always_included(self):
        definition = self.make_definition(columns=["customer_name"])
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.file.columns_produced[0], "document_number")

    def test_run_view_returns_failure_guidance(self):
        definition = self.make_definition(
            filters=[{"id": "invoice_date", "start": self.today.isoformat(),
                      "end": self.today.isoformat()}],
        )
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        response = self.client.get(f"/v1/exports/runs/{run.pk}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIsNone(data["failure"])
        self.assertEqual(data["configuration"]["scope"], "LEKKI")

    def test_idempotent_run_returns_the_same_row(self):
        definition = self.make_definition()
        first, created_first = services.trigger_run(
            definition=definition, actor=self.admin, client_key="abc-123",
        )
        second, created_second = services.trigger_run(
            definition=definition, actor=self.admin, client_key="abc-123",
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ExportRun.objects.count(), 1)

    def test_concurrency_cap_refuses_a_further_run(self):
        definition = self.make_definition()
        # Three runs stuck in flight - the cap is 3.
        for _ in range(3):
            ExportRun.objects.create(
                tenant=self.tenant, entity=self.entity, definition=definition,
                frozen_config=services.freeze(definition), requested_by=self.admin,
                status=RunStatus.RUNNING,
            )
        with self.assertRaises(services.ExportServiceError):
            services.trigger_run(definition=definition, actor=self.admin)

    def test_draft_cannot_run(self):
        definition = self.make_definition()
        definition.is_draft = True
        definition.save(update_fields=["is_draft"])
        with self.assertRaises(services.ExportServiceError):
            services.trigger_run(definition=definition, actor=self.admin)

    def test_queued_cancellation_is_immediate_and_keeps_no_file(self):
        definition = self.make_definition()
        run = ExportRun.objects.create(
            tenant=self.tenant, entity=self.entity, definition=definition,
            frozen_config=services.freeze(definition), requested_by=self.admin,
            status=RunStatus.QUEUED,
        )
        services.request_cancel(run, self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED)
        self.assertFalse(ExportFile.objects.filter(run=run).exists())

    def test_cancelled_run_is_not_executed(self):
        definition = self.make_definition()
        run = ExportRun.objects.create(
            tenant=self.tenant, entity=self.entity, definition=definition,
            frozen_config=services.freeze(definition), requested_by=self.admin,
            status=RunStatus.QUEUED, cancel_requested=True,
        )
        services.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.CANCELLED)
        self.assertFalse(ExportFile.objects.filter(run=run).exists())


class ExportOmissionAndFailureTests(_ExportFixture, TestCase):
    """The states the design cares most about: partly complete, and failed."""

    def setUp(self):
        self.build()

    def test_sensitive_column_is_omitted_not_silently_dropped(self):
        definition = self.make_definition(
            owner=self.analyst, columns=COLUMNS + ["customer_email"],
        )
        run, _ = services.trigger_run(definition=definition, actor=self.analyst)
        run.refresh_from_db()

        self.assertEqual(run.status, RunStatus.COMPLETED_WITH_OMISSIONS)
        self.assertEqual(run.row_count, 4)               # every row, fewer columns
        self.assertEqual(len(run.omissions), 1)
        omission = run.omissions[0]
        self.assertEqual(omission["code"], OmissionCode.FIELD_FORBIDDEN)
        self.assertEqual(omission["scope"], "columns")
        self.assertIn("Billing contact email", omission["items"])
        self.assertNotIn("customer_email", run.file.columns_produced)

    def test_sensitive_column_is_included_for_a_holder_and_audited(self):
        definition = self.make_definition(
            owner=self.admin, columns=COLUMNS + ["customer_email"],
        )
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertIn("customer_email", run.file.columns_produced)

        from vs_audit.models import AuditEvent

        self.assertTrue(AuditEvent.objects.filter(
            action_type=AuditAction.SENSITIVE_FIELD_INCLUDED, entity_id=str(run.pk),
        ).exists())

    def test_withdrawn_filter_fails_with_a_code_and_an_action(self):
        definition = self.make_definition(
            filters=[
                {"id": "invoice_date", "start": self.today.isoformat(),
                 "end": self.today.isoformat()},
                {"id": "settlement_status", "values": ["PENDING"]},
            ],
        )
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()

        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(run.failure_code, FailureCode.FILTER_INVALID)
        self.assertIn("settlement_status", run.failure_message)
        self.assertIn("replace the filter", run.failure_guidance)
        self.assertFalse(ExportFile.objects.filter(run=run).exists())

    def test_failure_response_never_leaks_a_traceback(self):
        definition = self.make_definition(
            filters=[{"id": "invoice_date", "start": self.today.isoformat(),
                      "end": self.today.isoformat()},
                     {"id": "settlement_status", "values": ["PENDING"]}],
        )
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        response = TenantAPIClient(user=self.admin).get(f"/v1/exports/runs/{run.pk}/")
        failure = response.json()["data"]["failure"]
        self.assertEqual(failure["code"], FailureCode.FILTER_INVALID)
        self.assertTrue(failure["recommended_action"])
        self.assertEqual(failure["reference"], run.reference)
        self.assertNotIn("Traceback", str(response.json()))

    def _wide_gl_definition(self):
        return ExportDefinition.objects.create(
            tenant=self.tenant, entity=self.entity, dataset_key="finance.gl_postings",
            name="GL", columns=["entry_number", "debit"],
            filters=[{"id": "entry_date", "start": "2026-01-01", "end": "2026-06-30"}],
            format=ExportFormat.CSV, owner=self.admin,
        )

    def test_a_wide_date_range_runs_and_only_warns(self):
        """The dataset's span is guidance about cost, not a ceiling.

        It used to fail the run outright, which refused an ordinary request - a
        finance user asking for a quarter of postings. The hard limit on what one
        export may produce is the ROW CAP, measured on the actual result rather
        than guessed at from the calendar.
        """
        run, _ = services.trigger_run(definition=self._wide_gl_definition(), actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertFalse(run.failure_code)

    def test_the_estimate_warns_before_the_wide_range_is_run(self):
        definition = self._wide_gl_definition()
        dataset = get_dataset("finance.gl_postings")
        from vs_exports.catalogue import ScopeContext

        figures = engine.estimate(
            self.admin, dataset, ScopeContext(tenant=self.tenant, entity=self.entity),
            {"columns": definition.columns, "filters": definition.filters, "format": "csv"},
            self.tenant,
        )
        codes = {w["code"] for w in figures["warnings"]}
        self.assertIn("WIDE_DATE_RANGE", codes)

    def test_a_required_date_filter_still_needs_both_ends(self):
        """Advisory width is not the same as an unbounded read."""
        definition = ExportDefinition.objects.create(
            tenant=self.tenant, entity=self.entity, dataset_key="finance.gl_postings",
            name="GL open ended", columns=["entry_number", "debit"],
            filters=[{"id": "entry_date", "start": "2026-01-01"}],
            format=ExportFormat.CSV, owner=self.admin,
        )
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(run.failure_code, FailureCode.REQUIRED_FILTER_MISSING)

    def test_row_cap_produces_a_partial_file_naming_what_was_left_out(self):
        definition = self.make_definition()
        # The engine caps at min(dataset.row_cap, DEFAULT_ROW_CAP); squeeze the
        # platform ceiling to 2 while 4 rows match.
        with patch("vs_exports.engine.DEFAULT_ROW_CAP", 2):
            run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED_WITH_OMISSIONS)
        self.assertEqual(run.row_count, 2)
        self.assertEqual(run.omissions[0]["code"], OmissionCode.ROW_CAP_HIT)

    def test_every_audit_action_is_a_registered_vocabulary_token(self):
        """vs_audit validates action_type on save and emit_audit_event swallows the
        error, so an unregistered token loses the event silently. Catch it here
        instead of discovering it during a compliance review."""
        from vs_audit.models import AuditActionType, AuditModuleKey
        from vs_exports.constants import MODULE_KEY

        registered = {c[0] for c in AuditActionType.choices}
        declared = {
            value for name, value in vars(AuditAction).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        self.assertEqual(declared - registered, set())
        self.assertIn(MODULE_KEY, {c[0] for c in AuditModuleKey.choices})

    def test_run_lifecycle_is_written_to_the_audit_trail(self):
        from vs_audit.models import AuditEvent

        definition = self.make_definition()
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        actions = set(AuditEvent.objects.filter(
            entity_type="ExportRun", entity_id=str(run.pk),
        ).values_list("action_type", flat=True))
        self.assertIn(AuditAction.RUN_STARTED, actions)
        self.assertIn(AuditAction.RUN_COMPLETED, actions)

    def test_quick_export_runs_without_a_saved_definition(self):
        client = TenantAPIClient(user=self.admin)
        response = client.post(
            f"/v1/exports/quick/?entity={self.entity.code}",
            {"dataset_key": DATASET, "columns": COLUMNS,
             "filters": [{"id": "invoice_date",
                          "start": (self.today - datetime.timedelta(days=30)).isoformat(),
                          "end": (self.today + datetime.timedelta(days=1)).isoformat()}],
             "format": ExportFormat.CSV, "values_mode": ValuesMode.SYSTEM,
             "name": "Invoices from the list screen"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

        run = ExportRun.objects.get(reference=response.json()["data"]["reference"])
        self.assertIsNone(run.definition_id)
        self.assertEqual(run.trigger, "QUICK")
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.row_count, 4)
        # A run with no definition has nothing to have drifted from.
        self.assertEqual(services.config_drift(run), [])

    def test_quick_export_is_private_to_the_person_who_ran_it(self):
        run, _ = services.trigger_quick_run(
            config={"dataset_key": DATASET, "columns": COLUMNS,
                    "filters": [{"id": "invoice_date",
                                 "start": self.today.isoformat(),
                                 "end": self.today.isoformat()}],
                    "format": ExportFormat.CSV, "values_mode": ValuesMode.SYSTEM},
            entity=self.entity, tenant=self.tenant, actor=self.admin,
        )
        run.refresh_from_db()
        response = TenantAPIClient(user=self.analyst).get(
            f"/v1/exports/files/{run.file.pk}/download/",
        )
        self.assertEqual(response.status_code, 404)

    def test_quick_export_needs_at_least_one_column(self):
        response = TenantAPIClient(user=self.admin).post(
            f"/v1/exports/quick/?entity={self.entity.code}",
            {"dataset_key": DATASET, "columns": [], "filters": []},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_quick_export_respects_the_dataset_gate(self):
        """The analyst holds run.create but not the payments dataset key."""
        response = TenantAPIClient(user=self.analyst).post(
            f"/v1/exports/quick/?entity={self.entity.code}",
            {"dataset_key": "payments.collections", "columns": ["reference"],
             "filters": [{"id": "created_at", "start": self.today.isoformat(),
                          "end": self.today.isoformat()}]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_retry_is_refused_on_a_completed_run(self):
        definition = self.make_definition()
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        with self.assertRaises(services.ExportServiceError):
            services.retry_run(run, self.admin)


# --------------------------------------------------------------------------- #
# Downloads and expiry                                                        #
# --------------------------------------------------------------------------- #
class ExportDownloadTests(_ExportFixture, TestCase):
    def setUp(self):
        self.build()
        self.definition = self.make_definition(owner=self.admin)
        run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        run.refresh_from_db()
        self.run = run
        self.file = run.file

    def test_owner_downloads_and_the_attempt_is_logged(self):
        client = TenantAPIClient(user=self.admin)
        response = client.get(f"/v1/exports/files/{self.file.pk}/download/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])

        self.file.refresh_from_db()
        self.assertEqual(self.file.download_count, 1)
        log = ExportDownload.objects.get(file=self.file)
        self.assertEqual(log.outcome, DownloadOutcome.ALLOWED)
        self.assertEqual(log.user_id, self.admin.pk)

    def test_expired_file_is_refused_and_the_refusal_is_logged(self):
        self.file.available_until = timezone.now() - datetime.timedelta(minutes=1)
        self.file.save(update_fields=["available_until"])

        response = TenantAPIClient(user=self.admin).get(
            f"/v1/exports/files/{self.file.pk}/download/",
        )
        self.assertEqual(response.status_code, 403)
        log = ExportDownload.objects.get(file=self.file)
        self.assertEqual(log.outcome, DownloadOutcome.REFUSED)
        self.assertEqual(log.refusal_reason, DownloadRefusal.EXPIRED)
        # The run itself is untouched: expiry is a property of the file.
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, RunStatus.COMPLETED)

    def test_unshared_user_cannot_download(self):
        response = TenantAPIClient(user=self.analyst).get(
            f"/v1/exports/files/{self.file.pk}/download/",
        )
        self.assertEqual(response.status_code, 404)     # not even visible

    def test_shared_user_can_download(self):
        self.definition.shares.create(user=self.analyst, shared_by=self.admin)
        response = TenantAPIClient(user=self.analyst).get(
            f"/v1/exports/files/{self.file.pk}/download/",
        )
        self.assertEqual(response.status_code, 200)

    def test_download_log_endpoint_shows_refusals_too(self):
        self.file.available_until = timezone.now() - datetime.timedelta(minutes=1)
        self.file.save(update_fields=["available_until"])
        client = TenantAPIClient(user=self.admin)
        client.get(f"/v1/exports/files/{self.file.pk}/download/")

        response = client.get(f"/v1/exports/files/{self.file.pk}/downloads/")
        self.assertEqual(response.status_code, 200)
        outcomes = {row["outcome"] for row in response.json()["data"]}
        self.assertIn(DownloadOutcome.REFUSED, outcomes)

    def test_expiry_sweeper_purges_bytes_but_keeps_history(self):
        self.file.available_until = timezone.now() - datetime.timedelta(days=1)
        self.file.save(update_fields=["available_until"])
        storage_name = self.file.storage_name

        purged = services.expire_files()
        self.assertEqual(purged, 1)

        self.file.refresh_from_db()
        self.assertIsNotNone(self.file.purged_at)
        self.assertFalse(default_storage.exists(storage_name))

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, RunStatus.COMPLETED)
        self.assertEqual(self.run.row_count, 4)

    def test_expiry_sweeper_is_idempotent(self):
        self.file.available_until = timezone.now() - datetime.timedelta(days=1)
        self.file.save(update_fields=["available_until"])
        services.expire_files()
        self.assertEqual(services.expire_files(), 0)




# --------------------------------------------------------------------------- #
# Queue position and delivery                                                 #
# --------------------------------------------------------------------------- #
class QueuePositionTests(_ExportFixture, TestCase):
    """A queued run must be able to explain the wait rather than go quiet."""

    def setUp(self):
        self.build()
        self.definition = self.make_definition()

    def _queued(self, **kwargs):
        return ExportRun.objects.create(
            tenant=self.tenant, entity=self.entity, definition=self.definition,
            frozen_config=services.freeze(self.definition), requested_by=self.admin,
            status=RunStatus.QUEUED, **kwargs
        )

    def test_position_counts_queued_runs_ahead(self):
        first, second, third = self._queued(), self._queued(), self._queued()
        self.assertEqual(services.queue_position(first), 1)
        self.assertEqual(services.queue_position(second), 2)
        self.assertEqual(services.queue_position(third), 3)

    def test_running_runs_occupy_a_place_in_the_queue(self):
        ExportRun.objects.create(
            tenant=self.tenant, entity=self.entity, definition=self.definition,
            frozen_config=services.freeze(self.definition), requested_by=self.admin,
            status=RunStatus.RUNNING,
        )
        queued = self._queued()
        self.assertEqual(services.queue_position(queued), 2)

    def test_a_started_run_has_no_position(self):
        run = self._queued()
        run.status = RunStatus.RUNNING
        run.save(update_fields=["status"])
        self.assertIsNone(services.queue_position(run))

    def test_position_is_scoped_to_the_tenant(self):
        """Another organisation's backlog must not inflate our queue position."""
        other_entity = LedgerEntity.objects.create(
            name="Other Books", code="OTHER", kind=LedgerEntity.Kind.TENANT,
            tenant=self.other_tenant,
        )
        other_definition = ExportDefinition.objects.create(
            tenant=self.other_tenant, entity=other_entity, dataset_key=DATASET,
            name="Theirs", columns=list(COLUMNS), filters=[],
            format=ExportFormat.CSV, owner=self.outsider,
        )
        for _ in range(3):
            ExportRun.objects.create(
                tenant=self.other_tenant, entity=other_entity,
                definition=other_definition,
                frozen_config=services.freeze(other_definition),
                requested_by=self.outsider, status=RunStatus.QUEUED,
            )
        self.assertEqual(services.queue_position(self._queued()), 1)

    def test_progress_block_exposes_the_position(self):
        run = self._queued()
        response = TenantAPIClient(user=self.admin).get(f"/v1/exports/runs/{run.pk}/")
        progress = response.json()["data"]["progress"]
        self.assertEqual(progress["queue_position"], 1)
        self.assertIsNone(progress["rows_total"])


class ExportDeliveryTests(_ExportFixture, TestCase):
    """A run can succeed while a delivery fails; the two are tracked separately."""

    def setUp(self):
        self.build()
        call_command("seed_notification_event_types", verbosity=0)
        call_command("seed_notification_templates", verbosity=0)
        self.definition = self.make_definition(owner=self.admin)
        self.definition.email_recipients = True
        self.definition.save(update_fields=["email_recipients"])
        self.definition.shares.create(user=self.analyst, shared_by=self.admin)

    def test_a_delivery_is_opened_per_recipient_and_sent_on_success(self):
        run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)

        delivery = run.deliveries.get()
        self.assertEqual(delivery.recipient_user_id, self.analyst.pk)
        self.assertEqual(delivery.state, DeliveryState.SENT)
        self.assertIsNotNone(delivery.attempted_at)

    def test_the_link_token_is_never_serialised(self):
        run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        response = TenantAPIClient(user=self.admin).get(f"/v1/exports/runs/{run.pk}/")
        body = response.json()
        self.assertEqual(len(body["data"]["deliveries"]), 1)
        self.assertNotIn(run.deliveries.get().token, str(body))

    def test_deliveries_are_skipped_when_no_rows_matched(self):
        """An empty file is worse than no email - the recipient cannot tell why."""
        self.definition.filters = [{
            "id": "invoice_date",
            "start": (self.today + datetime.timedelta(days=400)).isoformat(),
            "end": (self.today + datetime.timedelta(days=401)).isoformat(),
        }]
        self.definition.save(update_fields=["filters"])

        run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.row_count, 0)
        self.assertEqual(run.deliveries.get().state, DeliveryState.SKIPPED)

    def test_a_failed_run_skips_its_deliveries_rather_than_leaving_them_pending(self):
        self.definition.filters = [
            {"id": "invoice_date", "start": self.today.isoformat(),
             "end": self.today.isoformat()},
            {"id": "settlement_status", "values": ["PENDING"]},
        ]
        self.definition.save(update_fields=["filters"])

        run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.FAILED)
        delivery = run.deliveries.get()
        self.assertEqual(delivery.state, DeliveryState.SKIPPED)
        self.assertIn("No file", delivery.failure_reason)

    def test_one_failing_send_does_not_fail_the_run_or_the_others(self):
        self.definition.shares.create(user=self.stranger, shared_by=self.admin)
        first = {"seen": False}

        def _one_bad_send(*args, **kwargs):
            """Fail only the first share notification, leave the rest alone."""
            if kwargs.get("event_key") != "export.file_shared":
                return []
            if not first["seen"]:
                first["seen"] = True
                raise RuntimeError("smtp down")
            return []

        with patch("vs_notifications.notify.send_notification", side_effect=_one_bad_send):
            run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        run.refresh_from_db()

        self.assertEqual(run.status, RunStatus.COMPLETED)
        states = set(run.deliveries.values_list("state", flat=True))
        self.assertEqual(states, {DeliveryState.FAILED, DeliveryState.SENT})

    def test_no_deliveries_when_the_export_does_not_email(self):
        self.definition.email_recipients = False
        self.definition.save(update_fields=["email_recipients"])
        run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        self.assertEqual(run.deliveries.count(), 0)

    def test_revoking_a_link_leaves_the_file_alone(self):
        run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        run.refresh_from_db()
        delivery = run.deliveries.get()

        response = TenantAPIClient(user=self.admin).post(
            f"/v1/exports/deliveries/{delivery.pk}/revoke/", {}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, DeliveryState.REVOKED)
        self.assertTrue(run.file.is_downloadable)

    def test_the_download_link_points_at_the_authenticated_endpoint(self):
        run, _ = services.trigger_run(definition=self.definition, actor=self.admin)
        run.refresh_from_db()
        link = services.download_url(run.file)
        self.assertIn(f"/exports/files/{run.file.pk}", link)

    def test_every_notification_event_key_is_registered(self):
        """send_notification rejects unregistered keys, so a typo silences the
        notification exactly the way an unregistered audit action does."""
        from vs_notifications.models import NotificationEventType

        for key in ("export.file_shared", "export.run_failed", "export.run_completed"):
            self.assertTrue(
                NotificationEventType.objects.filter(key=key, is_active=True).exists(),
                f"{key} is not seeded",
            )


# --------------------------------------------------------------------------- #
# Dataset scope - entity-scoped versus tenant-scoped                          #
# --------------------------------------------------------------------------- #
class DatasetScopeTests(_ExportFixture, TestCase):
    """Scope is the dataset's declaration, not an assumption baked into the platform."""

    AUDIT_DATASET = "audit.events"

    def setUp(self):
        self.build()
        call_command("seed_platform_permissions", verbosity=0)
        self.client = TenantAPIClient(user=self.admin)

    def test_the_catalogue_declares_each_datasets_scope(self):
        finance = get_dataset(DATASET)
        audit_events = get_dataset(self.AUDIT_DATASET)
        self.assertEqual(finance.scope, DatasetScope.ENTITY)
        self.assertTrue(finance.needs_entity)
        self.assertEqual(audit_events.scope, DatasetScope.TENANT)
        self.assertFalse(audit_events.needs_entity)

    def test_catalogue_publishes_whether_an_entity_is_required(self):
        response = self.client.get(f"/v1/exports/catalogue/{self.AUDIT_DATASET}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["scope"], DatasetScope.TENANT)
        self.assertFalse(data["requires_entity"])

    def test_an_entity_scoped_dataset_still_demands_an_entity(self):
        """The finance guarantee is unchanged: no entity, no invoice export."""
        response = self.client.post(
            "/v1/exports/preview/",
            {"dataset_key": DATASET, "columns": COLUMNS, "filters": []},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("entity", str(response.json()).lower())

    def test_a_tenant_scoped_export_needs_no_entity_at_all(self):
        response = self.client.post(
            "/v1/exports/definitions/",
            {
                "name": "Audit trail", "dataset_key": self.AUDIT_DATASET,
                "columns": ["event_at", "action_type", "summary"],
                "filters": [{
                    "id": "event_at",
                    "start": (self.today - datetime.timedelta(days=7)).isoformat(),
                    "end": (self.today + datetime.timedelta(days=1)).isoformat(),
                }],
                "format": ExportFormat.CSV,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()["data"]
        self.assertIsNone(data["entity_code"])
        self.assertEqual(data["scope"]["type"], DatasetScope.TENANT)

        definition = ExportDefinition.objects.get(name="Audit trail")
        self.assertIsNone(definition.entity_id)

    def test_a_tenant_scoped_export_runs_and_produces_a_file(self):
        definition = ExportDefinition.objects.create(
            tenant=self.tenant, entity=None, dataset_key=self.AUDIT_DATASET,
            name="Audit trail", columns=["event_at", "action_type", "summary"],
            filters=[{
                "id": "event_at",
                "start": (self.today - datetime.timedelta(days=7)).isoformat(),
                "end": (self.today + datetime.timedelta(days=1)).isoformat(),
            }],
            format=ExportFormat.CSV, values_mode=ValuesMode.SYSTEM,
            file_name_pattern="audit-{entity}-{date}", owner=self.admin,
        )
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()

        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertIsNone(run.entity_id)
        # The {entity} token falls back to the tenant slug rather than rendering blank.
        self.assertIn("codex", run.file.name)

    def test_a_tenant_scoped_run_reads_only_its_own_tenants_rows(self):
        from vs_audit.services import emit_audit_event

        emit_audit_event(
            module_key="EXPORTS", action_type="EXPORT_REQUESTED",
            entity_type="Probe", entity_id="1", tenant=self.other_tenant,
            summary="Belongs to the other tenant",
        )
        definition = ExportDefinition.objects.create(
            tenant=self.tenant, entity=None, dataset_key=self.AUDIT_DATASET,
            name="Audit trail", columns=["event_at", "summary"],
            filters=[{
                "id": "event_at",
                "start": (self.today - datetime.timedelta(days=7)).isoformat(),
                "end": (self.today + datetime.timedelta(days=1)).isoformat(),
            }],
            format=ExportFormat.CSV, owner=self.admin,
        )
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        body = default_storage.open(run.file.storage_name, "rb").read().decode("utf-8")
        self.assertNotIn("Belongs to the other tenant", body)

    def test_audit_export_needs_the_audit_export_key(self):
        """The dataset's own permission gate, not the Export Centre's."""
        response = TenantAPIClient(user=self.analyst).get(
            f"/v1/exports/catalogue/{self.AUDIT_DATASET}/",
        )
        # Unknown and forbidden look identical, so the analyst cannot enumerate it.
        self.assertEqual(response.status_code, 404)


# --------------------------------------------------------------------------- #
# Analytics                                                                   #
# --------------------------------------------------------------------------- #
class AnalyticsSafetyTests(TestCase):
    """The privacy rule is enforced by code, not by convention."""

    def test_unknown_property_keys_are_dropped(self):
        clean = analytics.sanitise(
            analytics.Event.ESTIMATE_VIEWED,
            {"rows_bucket": "1k-10k", "customer_name": "Northgate Logistics"},
        )
        self.assertEqual(clean, {"rows_bucket": "1k-10k"})

    def test_non_scalar_values_are_dropped(self):
        clean = analytics.sanitise(
            analytics.Event.COLUMNS_CHANGED,
            {"count": 6, "from_default": {"rows": [["INV-1", "Acme"]]}},
        )
        self.assertEqual(clean, {"count": 6})

    def test_long_strings_are_truncated(self):
        clean = analytics.sanitise(analytics.Event.VALUES_MODE_CHOSEN, {"mode": "x" * 500})
        self.assertEqual(len(clean["mode"]), 64)

    def test_an_unknown_event_is_dropped_not_raised(self):
        self.assertEqual(analytics.sanitise("not.an.event", {"a": 1}), {})

    def test_buckets_never_reveal_the_exact_figure(self):
        self.assertEqual(analytics.bucket_rows(2_410), "1k-10k")
        self.assertEqual(analytics.bucket_rows(2_411), "1k-10k")
        self.assertEqual(analytics.bucket_bytes(1_300_000), "1-10MB")
        self.assertEqual(analytics.bucket_ms(90_000), "1m-1h")
        self.assertEqual(analytics.bucket_rows(None), "unknown")


class AnalyticsRecordingTests(_ExportFixture, TestCase):
    def setUp(self):
        self.build()
        self.client = TenantAPIClient(user=self.admin)

    def test_a_run_emits_run_triggered(self):
        definition = self.make_definition()
        services.trigger_run(definition=definition, actor=self.admin)
        event = ExportAnalyticsEvent.objects.get(name=analytics.Event.RUN_TRIGGERED)
        self.assertEqual(event.properties["from_definition"], True)
        self.assertEqual(event.tenant_id, self.tenant.pk)

    def test_a_quick_export_is_marked_as_not_from_a_definition(self):
        services.trigger_quick_run(
            config={
                "dataset_key": DATASET, "columns": COLUMNS,
                "filters": [{
                    "id": "invoice_date",
                    "start": (self.today - datetime.timedelta(days=30)).isoformat(),
                    "end": (self.today + datetime.timedelta(days=1)).isoformat(),
                }],
                "format": ExportFormat.CSV, "values_mode": ValuesMode.SYSTEM,
            },
            entity=self.entity, tenant=self.tenant, actor=self.admin,
        )
        triggered = ExportAnalyticsEvent.objects.get(name=analytics.Event.RUN_TRIGGERED)
        self.assertFalse(triggered.properties["from_definition"])
        self.assertTrue(
            ExportAnalyticsEvent.objects.filter(
                name=analytics.Event.QUICK_EXPORT_USED,
            ).exists()
        )

    def test_preview_records_only_bucketed_figures(self):
        self.client.post(
            f"/v1/exports/preview/?entity={self.entity.code}",
            {
                "dataset_key": DATASET, "columns": COLUMNS,
                "filters": [{
                    "id": "invoice_date",
                    "start": (self.today - datetime.timedelta(days=30)).isoformat(),
                    "end": (self.today + datetime.timedelta(days=1)).isoformat(),
                }],
            },
            format="json",
        )
        event = ExportAnalyticsEvent.objects.get(name=analytics.Event.ESTIMATE_VIEWED)
        # Four rows matched, but only the bucket is stored.
        self.assertEqual(event.properties["rows_bucket"], "1-100")
        self.assertNotIn("matching_rows", event.properties)

    def test_viewing_a_failure_records_the_reason_code(self):
        definition = self.make_definition(filters=[
            {"id": "invoice_date", "start": self.today.isoformat(),
             "end": self.today.isoformat()},
            {"id": "settlement_status", "values": ["PENDING"]},
        ])
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        self.client.get(f"/v1/exports/runs/{run.pk}/")
        event = ExportAnalyticsEvent.objects.get(name=analytics.Event.FAILURE_VIEWED)
        self.assertEqual(event.properties["reason_code"], FailureCode.FILTER_INVALID)

    def test_a_good_run_after_a_failure_records_the_resolution(self):
        definition = self.make_definition(filters=[
            {"id": "invoice_date", "start": self.today.isoformat(),
             "end": self.today.isoformat()},
            {"id": "settlement_status", "values": ["PENDING"]},
        ])
        failed, _ = services.trigger_run(definition=definition, actor=self.admin)
        failed.refresh_from_db()
        self.assertEqual(failed.status, RunStatus.FAILED)

        # The user fixes the export, then runs it again.
        definition.filters = [{
            "id": "invoice_date",
            "start": (self.today - datetime.timedelta(days=30)).isoformat(),
            "end": (self.today + datetime.timedelta(days=1)).isoformat(),
        }]
        definition.save(update_fields=["filters"])
        good, _ = services.trigger_run(definition=definition, actor=self.admin)
        good.refresh_from_db()
        self.assertEqual(good.status, RunStatus.COMPLETED)

        event = ExportAnalyticsEvent.objects.get(name=analytics.Event.FAILURE_RESOLVED)
        self.assertEqual(event.properties["path"], "edit_and_run")
        self.assertGreaterEqual(event.properties["ms_to_resolve"], 0)

    def test_a_second_success_does_not_re_record_an_old_failure(self):
        definition = self.make_definition()
        services.trigger_run(definition=definition, actor=self.admin)
        services.trigger_run(definition=definition, actor=self.admin)
        self.assertEqual(
            ExportAnalyticsEvent.objects.filter(
                name=analytics.Event.FAILURE_RESOLVED,
            ).count(),
            0,
        )

    def test_a_download_records_the_files_age(self):
        definition = self.make_definition()
        run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        self.client.get(f"/v1/exports/files/{run.file.pk}/download/")
        event = ExportAnalyticsEvent.objects.get(name=analytics.Event.FILE_DOWNLOADED)
        self.assertEqual(event.properties["age_days"], 0)


class AnalyticsEndpointTests(_ExportFixture, TestCase):
    def setUp(self):
        self.build()
        self.client = TenantAPIClient(user=self.admin)

    def test_the_client_may_report_builder_events(self):
        response = self.client.post("/v1/exports/analytics/", {
            "session_key": "sess-1",
            "events": [
                {"name": "builder.entered", "properties": {"entry": "wizard"}},
                {"name": "builder.step_viewed", "properties": {"step": 2, "ms_on_step": 4100}},
                {"name": "builder.abandoned", "properties": {"last_step": 3}},
            ],
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["accepted"], 3)
        self.assertEqual(
            ExportAnalyticsEvent.objects.filter(session_key="sess-1").count(), 3,
        )

    def test_the_client_may_not_report_a_server_owned_event(self):
        """run.triggered is the server's to state - accepting it from a browser would
        let anyone inflate the reuse metric."""
        response = self.client.post("/v1/exports/analytics/", {
            "events": [{"name": "run.triggered", "properties": {"trigger": "MANUAL"}}],
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_ingest_strips_properties_the_event_does_not_declare(self):
        self.client.post("/v1/exports/analytics/", {
            "events": [{
                "name": "columns.changed",
                "properties": {"count": 6, "customer": "Northgate Logistics"},
            }],
        }, format="json")
        event = ExportAnalyticsEvent.objects.get(name="columns.changed")
        self.assertEqual(event.properties, {"count": 6})

    def test_summary_is_admin_only(self):
        self.assertEqual(
            TenantAPIClient(user=self.analyst).get(
                "/v1/exports/analytics/summary/",
            ).status_code,
            403,
        )

    def test_summary_reports_the_four_metrics(self):
        definition = self.make_definition()
        services.trigger_run(definition=definition, actor=self.admin)
        services.trigger_quick_run(
            config={
                "dataset_key": DATASET, "columns": COLUMNS,
                "filters": [{
                    "id": "invoice_date",
                    "start": (self.today - datetime.timedelta(days=30)).isoformat(),
                    "end": (self.today + datetime.timedelta(days=1)).isoformat(),
                }],
                "format": ExportFormat.CSV, "values_mode": ValuesMode.SYSTEM,
            },
            entity=self.entity, tenant=self.tenant, actor=self.admin,
        )
        self.client.post("/v1/exports/analytics/", {
            "events": [{"name": "builder.abandoned", "properties": {"last_step": 2}}],
        }, format="json")

        data = self.client.get("/v1/exports/analytics/summary/").json()["data"]
        self.assertEqual(data["runs"], 2)
        # One from a saved definition, one quick - 50% reuse.
        self.assertEqual(data["reuse"]["from_saved_definition"], 1)
        self.assertEqual(data["reuse"]["share"], 0.5)
        self.assertEqual(data["unhappy_runs"]["count"], 0)
        self.assertEqual(
            data["builder"]["abandoned_by_step"], [{"step": 2, "count": 1}],
        )

    def test_summary_copes_with_no_activity_at_all(self):
        data = self.client.get("/v1/exports/analytics/summary/").json()["data"]
        self.assertEqual(data["runs"], 0)
        self.assertIsNone(data["reuse"]["share"])
        self.assertIsNone(data["failure_resolution"]["median_ms"])
        self.assertEqual(data["builder"]["abandoned_by_step"], [])

    def test_pruning_drops_only_events_past_retention(self):
        from vs_exports.tasks import prune_analytics_task

        fresh = analytics.record(
            analytics.Event.BUILDER_ENTERED, tenant=self.tenant, actor=self.admin,
            properties={"entry": "wizard"},
        )
        stale = analytics.record(
            analytics.Event.BUILDER_ENTERED, tenant=self.tenant, actor=self.admin,
            properties={"entry": "wizard"},
        )
        ExportAnalyticsEvent.objects.filter(pk=stale.pk).update(
            occurred_at=timezone.now() - datetime.timedelta(
                days=analytics.RETENTION_DAYS + 1,
            ),
        )
        result = prune_analytics_task()
        self.assertEqual(result["deleted"], 1)
        self.assertTrue(ExportAnalyticsEvent.objects.filter(pk=fresh.pk).exists())

    def test_analytics_never_breaks_the_request_that_emits_it(self):
        """Telemetry is best-effort by contract - the opposite of the audit trail."""
        with patch(
            "vs_exports.models.ExportAnalyticsEvent.objects.create",
            side_effect=RuntimeError("analytics store down"),
        ):
            definition = self.make_definition()
            run, _ = services.trigger_run(definition=definition, actor=self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.COMPLETED)


class RetryRuleTests(_ExportFixture, TestCase):
    """Only a transient fault is worth retrying.

    The rule was documented on ``retry_run`` from the start but never enforced:
    ``RETRYABLE_FAILURE_CODES`` was declared and used nowhere, and the run
    serializer reported ``retryable`` purely from "has a definition". A filter or
    permission failure therefore offered a Retry button that queued a run which
    failed identically - a second wait and a second notification for nothing.
    """

    def setUp(self):
        self.build()

    def _failed_run(self, code):
        definition = self.make_definition()
        run = ExportRun.objects.create(
            tenant=self.tenant,
            entity=definition.entity,
            definition=definition,
            frozen_config=services.freeze(definition),
            requested_by=self.admin,
            status=RunStatus.FAILED,
            failure_code=code,
            failure_message="It went wrong.",
        )
        return run

    def test_a_configuration_failure_is_not_retryable(self):
        for code in (
            FailureCode.FILTER_INVALID,
            FailureCode.REQUIRED_FILTER_MISSING,
            FailureCode.ROW_CAP_EXCEEDED,
            FailureCode.DATE_SPAN_EXCEEDED,
            FailureCode.DATASET_FORBIDDEN,
        ):
            run = self._failed_run(code)
            with self.subTest(code=code):
                data = ExportRunDetailSerializer(run).data
                self.assertFalse(data["failure"]["retryable"])
                with self.assertRaises(services.ExportServiceError):
                    services.retry_run(run, self.admin)

    def test_a_transient_failure_is_retryable(self):
        run = self._failed_run(FailureCode.INFRASTRUCTURE)
        data = ExportRunDetailSerializer(run).data
        self.assertTrue(data["failure"]["retryable"])

        with mock.patch("vs_exports.services.enqueue"):
            new_run = services.retry_run(run, self.admin)
        self.assertEqual(new_run.attempt, run.attempt + 1)
        self.assertEqual(new_run.trigger, RunTrigger.RETRY)

    def test_the_refusal_names_the_actual_next_step(self):
        run = self._failed_run(FailureCode.FILTER_INVALID)
        with self.assertRaises(services.ExportServiceError) as ctx:
            services.retry_run(run, self.admin)
        # The guidance for the code, not a generic "cannot retry".
        self.assertIn("replace the filter", str(ctx.exception))

    def test_a_quick_export_has_nothing_to_retry(self):
        run = ExportRun.objects.create(
            tenant=self.tenant,
            frozen_config={"name": "Quick export"},
            requested_by=self.admin,
            status=RunStatus.FAILED,
            failure_code=FailureCode.INFRASTRUCTURE,
        )
        self.assertFalse(ExportRunDetailSerializer(run).data["failure"]["retryable"])


class DriftReadabilityTests(_ExportFixture, TestCase):
    """The run detail says WHAT changed, without publishing the stored blob."""

    def setUp(self):
        self.build()

    def test_changes_are_sentences_not_ids(self):
        definition = self.make_definition()
        with mock.patch("vs_exports.services.enqueue"):
            run, _ = services.trigger_run(definition=definition, actor=self.admin)

        definition.name = "Renamed since the run"
        definition.columns = list(definition.columns)[:1]
        definition.save()

        drift = ExportRunDetailSerializer(run).data["drift"]
        self.assertGreaterEqual(drift["count"], 2)
        by_field = {c["field"]: c for c in drift["changes"]}

        self.assertEqual(by_field["name"]["now"], "Renamed since the run")
        # Columns render as labels a person recognises, never as field ids.
        columns = by_field["columns"]
        self.assertNotIn("_", columns["now"])
        self.assertTrue(all(isinstance(c["then"], str) for c in drift["changes"]))
