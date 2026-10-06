"""The procurement settings history names each change in the screen's own words.

See :mod:`vs_finance.tests_settings_history`; procurement declares its own
labels beside its fields (:data:`vs_procurement.settings.HISTORY_FIELDS`).
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from core.test_utils import TenantAPIClient
from schools.vs_schools.models import School
from vs_finance.models import LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_finance.tests_settings_history import CODE_SHAPED, assert_human_changes
from vs_procurement.settings import HISTORY_FIELDS, SETTING_FIELDS
from vs_tenants.models import Branch


class EveryProcurementSettingHasALabelTests(SimpleTestCase):
    def test_every_audited_field_has_a_label_in_words(self):
        self.assertEqual(set(HISTORY_FIELDS), set(SETTING_FIELDS))
        for key, field in HISTORY_FIELDS.items():
            self.assertIsNone(CODE_SHAPED.search(field.label), f"{key}: {field.label}")


class ProcurementSettingsHistoryAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed_currencies()
        cls.school = School.objects.create(
            name="Proc History School", slug="proc-history-school", code="PRHSC", status="ACTIVE",
        )
        Branch.objects.create(tenant=cls.school.tenant, name="Main Branch", is_main=True, status="ACTIVE")
        cls.entity = LedgerEntity.objects.create(
            name="Proc History Books", code="PRHBK", kind=LedgerEntity.Kind.TENANT,
            tenant=cls.school.tenant,
        )
        seed_chart_of_accounts(cls.entity)
        cls.user = get_user_model().objects.create_user(
            email="proc-history@test.com", password="pw", tenant=cls.school.tenant,
            status="ACTIVE", first_name="Proc", last_name="History",
        )

    def setUp(self):
        self.client = TenantAPIClient(user=self.user)
        self.url = f"/v1/procurement/settings/?entity={self.entity.code}"

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_history_labels_terms_tolerances_and_counts(self, _permission):
        response = self.client.patch(self.url, {
            "default_payment_terms": "NET_60",
            "price_tolerance_bps": 250,
            "minimum_rfq_invited_vendors": 4,
            "vendor_purchase_kyc_requirement": "VERIFIED_ONLY",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        changes = response.data["data"]["history"][0]["changes"]
        after = {c["label"]: c["after"] for c in changes}
        self.assertEqual(after["Default payment terms"], "Net 60 days")
        self.assertEqual(after["Price tolerance"], "2.5%")
        self.assertEqual(after["Fewest vendors invited to quote"], "4 vendors")
        self.assertEqual(after["Vendor checks needed before buying"], "Verified only")
        assert_human_changes(self, changes)
