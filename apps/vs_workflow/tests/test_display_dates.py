"""An approval card's dates are stored ISO and read in the tenant's format.

Harbour School writes dates DD/MM/YYYY on a 24-hour clock in Lagos. A card
snapshotted at submission holds "2026-09-29"; the approver reads "29/09/2026",
and reads whatever the school chooses next without the snapshot being
rewritten. Text that merely mentions a date is left alone.
"""
import datetime
from types import SimpleNamespace

from django.test import TestCase

from vs_config.clock import forget_tenant_zone
from vs_config.display import CLOCK_KEY, DATE_FORMAT_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_tenants.models import Tenant
from vs_workflow.presentation import (
    details_dates_for_reader,
    reword_date,
    summary_for_reader,
)
from vs_workflow.serializers import WorkflowInstanceDetailSerializer


class ApprovalCardDatesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = Tenant.objects.create(
            name="Harbour School", slug="harbour-approvals", kind=Tenant.Kind.ORGANIZATION,
        )
        set_value(
            definition=ConfigurationDefinition.objects.get(key=DATE_FORMAT_KEY),
            value="DD_MM_YYYY", actor=None, tenant=cls.school,
        )
        set_value(
            definition=ConfigurationDefinition.objects.get(key=CLOCK_KEY),
            value="H24", actor=None, tenant=cls.school,
        )

    def setUp(self):
        forget_tenant_zone(self.school)

    def test_each_kind_of_stored_value(self):
        instant = datetime.datetime(2026, 9, 29, 13, 30, tzinfo=datetime.timezone.utc)
        for stored, shown in (
            ("2026-09-29", "29/09/2026"),
            (str(instant), "29/09/2026, 14:30"),
            (instant.isoformat(), "29/09/2026, 14:30"),
            ("2026-09-01 to 2026-09-05", "01/09/2026 to 05/09/2026"),
            ("Term starts 2026-09-01", "Term starts 2026-09-01"),
            ("INV-12609291", "INV-12609291"),
            ("", ""),
        ):
            with self.subTest(stored=stored):
                self.assertEqual(reword_date(stored, self.school), shown)
        self.assertIsNone(reword_date(None, self.school))

    def test_summary_and_details_are_reworded_and_the_snapshot_is_not(self):
        summary = {
            "title": "JV-0042", "subtitle": "2026-09-29",
            "fields": [{"label": "Date", "value": "2026-09-29"}, {"label": "Amount", "value": "₦5,000.00"}],
        }
        details = {"schema_version": 1, "sections": [
            {"kind": "fields", "title": "Leave", "items": [
                {"label": "Dates", "value": "2026-10-05 to 2026-10-09"},
            ]},
            {"kind": "table", "title": "Lines", "columns": [{"key": "due", "label": "Due"}],
             "rows": [{"due": "2026-10-31"}]},
        ]}
        read = summary_for_reader(summary, self.school)
        self.assertEqual(read["subtitle"], "29/09/2026")
        self.assertEqual([f["value"] for f in read["fields"]], ["29/09/2026", "₦5,000.00"])
        self.assertEqual(summary["fields"][0]["value"], "2026-09-29")

        read = details_dates_for_reader(details, self.school)
        self.assertEqual(read["sections"][0]["items"][0]["value"], "05/10/2026 to 09/10/2026")
        self.assertEqual(read["sections"][1]["rows"][0]["due"], "31/10/2026")
        self.assertEqual(details["sections"][1]["rows"][0]["due"], "2026-10-31")

    def test_the_detail_serializer_reads_the_card_in_the_instance_tenants_format(self):
        instance = SimpleNamespace(
            pk=None, tenant=self.school, branch_id=None,
            document_summary={"title": "PO-1", "fields": [{"label": "Order date", "value": "2026-09-29"}]},
            document_type="unregistered.type", document=object(),
        )
        summary = WorkflowInstanceDetailSerializer().get_document_summary(instance)
        self.assertEqual(summary["fields"][0]["value"], "29/09/2026")
