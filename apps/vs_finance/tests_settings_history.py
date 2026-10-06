"""A settings screen's history names each change in the screen's own words.

The history panel under every finance settings screen once printed the audited
field keys with their underscores swapped for spaces ("term collection target
pct") beside the stored values ("80", "oldest", "1000000"). These tests hold the
server to supplying a label and a display value for every setting it audits, so
the panel never has to make one up from a key.
"""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from core.test_utils import TenantAPIClient
from schools.vs_schools.models import School
from vs_finance.banking_settings import SETTING_FIELDS as BANKING_FIELDS
from vs_finance.calendar_settings import SETTING_FIELDS as CALENDAR_FIELDS
from vs_finance.constants import AccountMappingKey, FinanceAuditAction
from vs_finance.document_settings import SETTING_FIELDS as DOCUMENT_FIELDS
from vs_finance.models import Account, LedgerEntity
from vs_finance.payroll_settings import SETTING_FIELDS as PAYROLL_FIELDS
from vs_finance.receivables_policy import SETTING_FIELDS as RECEIVABLES_FIELDS
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_finance.settings_history import (
    RETIRED_CHOICE,
    UNKNOWN_SETTING_LABEL,
    SettingField,
    describe_changes,
    describe_value,
    finance_setting_fields,
)
from vs_finance.views_records import RecordRetentionSettingsView
from vs_tenants.models import Branch

#: A field key or a dotted path: ``term_collection_target_pct``, ``vs_finance.banking``.
CODE_SHAPED = re.compile(r"[A-Za-z]_[A-Za-z]|[a-z]\.[a-z]")


def assert_human_changes(test, changes):
    """Every label and value in ``changes`` reads as words, never as a key."""
    test.assertTrue(changes)
    for change in changes:
        test.assertEqual(set(change), {"field", "label", "before", "after"})
        for text in (change["label"], change["before"], change["after"]):
            test.assertIsNone(CODE_SHAPED.search(text), f"{change['field']}: {text}")


class EveryAuditedSettingHasALabelTests(SimpleTestCase):
    """A setting added to a family without a label would reach the panel unnamed."""

    FAMILIES = (
        (FinanceAuditAction.FINANCE_SETTINGS_UPDATED, tuple(AccountMappingKey.values)),
        (FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED, DOCUMENT_FIELDS),
        (FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED, BANKING_FIELDS),
        (FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED, CALENDAR_FIELDS),
        (FinanceAuditAction.FINANCE_RECEIVABLES_SETTINGS_UPDATED, RECEIVABLES_FIELDS),
        (FinanceAuditAction.PAYROLL_SETTINGS_UPDATED, PAYROLL_FIELDS),
        (FinanceAuditAction.RETENTION_SETTINGS_UPDATED, tuple(RecordRetentionSettingsView.FIELDS)),
    )

    def test_each_family_labels_every_field_it_audits_in_words(self):
        for action, audited in self.FAMILIES:
            fields = finance_setting_fields(action)
            with self.subTest(action=action):
                self.assertEqual(set(fields), set(audited))
                for key, field in fields.items():
                    self.assertIsNone(CODE_SHAPED.search(field.label), f"{key}: {field.label}")
                    if field.kind == "choice":
                        self.assertIsNotNone(field.choices, key)


class SettingValuesReadAsWordsTests(SimpleTestCase):
    def test_each_kind_of_value_reads_the_way_a_bursar_writes_it(self):
        cases = (
            (SettingField("x", "bool"), True, "Yes"),
            (SettingField("x", "bool"), False, "No"),
            (SettingField("x", "days"), 1, "1 day"),
            (SettingField("x", "days"), 30, "30 days"),
            (SettingField("x", "years"), 7, "7 years"),
            (SettingField("x", "count", unit="vendor"), 3, "3 vendors"),
            (SettingField("x", "percent"), 80, "80%"),
            (SettingField("x", "bps"), 750, "7.5%"),
            (SettingField("x", "bps"), 800, "8%"),
            (SettingField("x", "bps"), 25, "0.25%"),
            (SettingField("x", "kobo"), 2_500_000, "₦25,000.00"),
            (SettingField("x", "country"), "NG", "Nigeria"),
            (SettingField("x", "bank"), {"name": "Collections", "branch_name": "Ikeja Branch"},
             "Collections (Ikeja Branch)"),
            (SettingField("x", "bands"),
             [{"over_days": 30, "rate_bps": 1000}, {"over_days": 90, "rate_bps": 5000}],
             "Over 30 days: 10%, over 90 days: 50%"),
            (SettingField("x"), None, "Not set"),
            (SettingField("x", empty="Blank"), "", "Blank"),
        )
        for field, value, expected in cases:
            with self.subTest(kind=field.kind, value=value):
                self.assertEqual(describe_value(field, value), expected)

    def test_a_choice_reads_by_its_label_and_a_retired_one_never_by_its_code(self):
        fields = finance_setting_fields(FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED)
        field = fields["default_receipt_allocation_strategy"]
        self.assertEqual(describe_value(field, "largest"), "Largest balance first")
        self.assertEqual(describe_value(field, "MOST_OVERDUE_FIRST"), RETIRED_CHOICE)

    def test_an_account_reads_by_code_and_name(self):
        field = SettingField("Cash and bank", "account")
        self.assertEqual(
            describe_value(field, "1300", accounts={"1300": "Petty cash"}), "1300 · Petty cash",
        )

    def test_a_retired_setting_is_named_neutrally_and_listed_after_the_declared_ones(self):
        changes = describe_changes(
            {"term_collection_target_pct": SettingField("Term collection target", "percent")},
            {"legacy_mode": "FAST_TRACK", "term_collection_target_pct": 75},
            {"legacy_mode": "SLOW_TRACK", "term_collection_target_pct": 80},
        )
        self.assertEqual([c["label"] for c in changes], ["Term collection target", UNKNOWN_SETTING_LABEL])
        self.assertEqual((changes[0]["before"], changes[0]["after"]), ("75%", "80%"))
        self.assertEqual((changes[1]["before"], changes[1]["after"]), ("Changed", "Changed"))
        assert_human_changes(self, changes)


class SettingsHistoryAPITests(TestCase):
    """The history each finance settings endpoint returns carries the words."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed_currencies()
        cls.school = School.objects.create(
            name="History School", slug="history-school", code="HISSC", status="ACTIVE",
        )
        Branch.objects.create(
            tenant=cls.school.tenant, name="Main Branch", is_main=True, status="ACTIVE",
        )
        cls.entity = LedgerEntity.objects.create(
            name="History Books", code="HISBK", kind=LedgerEntity.Kind.TENANT,
            tenant=cls.school.tenant,
        )
        seed_chart_of_accounts(cls.entity)
        cls.user = get_user_model().objects.create_user(
            email="settings-history@test.com", password="pw", tenant=cls.school.tenant,
            status="ACTIVE", first_name="Ada", last_name="Bursar",
        )

    def setUp(self):
        self.client = TenantAPIClient(user=self.user)

    def _url(self, path):
        return f"/v1/finance/settings/{path}/?entity={self.entity.code}"

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_document_settings_history_labels_each_change_and_writes_its_values(self, _p):
        response = self.client.patch(self._url("documents"), {
            "term_collection_target_pct": 75,
            "concession_second_person_threshold": 2_500_000,
            "auto_apply_customer_credit": False,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        entry = response.data["data"]["history"][0]
        changes = {c["label"]: (c["before"], c["after"]) for c in entry["changes"]}
        self.assertEqual(changes["Term collection target"][1], "75%")
        self.assertEqual(changes["Concession total needing a second person"][1], "₦25,000.00")
        self.assertEqual(changes["Apply customer credit to new bills automatically"], ("Yes", "No"))
        assert_human_changes(self, entry["changes"])
        self.assertIn("term_collection_target_pct", entry["after"])

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_account_mapping_history_names_the_role_and_both_accounts(self, _p):
        replacement = Account.objects.get(entity=self.entity, code="1300")
        previous = Account.objects.get(entity=self.entity, code="1100")
        response = self.client.patch(
            self._url("account-mappings"), {"mappings": {"CASH_BANK": replacement.id}},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        [change] = response.data["data"]["history"][0]["changes"]
        self.assertEqual(change["label"], "Cash and bank")
        self.assertEqual(change["before"], f"1100 · {previous.name}")
        self.assertEqual(change["after"], f"1300 · {replacement.name}")

    @patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True)
    def test_banking_and_calendar_histories_read_choices_by_label(self, _p):
        self.client.patch(self._url("banking"), {
            "default_receipt_allocation_strategy": "largest",
            "petty_cash_low_balance_threshold_bps": 1250,
        }, format="json")
        banking = self.client.get(self._url("banking")).data["data"]["history"][0]["changes"]
        self.assertIn(("Receipt allocation order", "Largest balance first"),
                      [(c["label"], c["after"]) for c in banking])
        self.assertIn(("Petty cash low-balance alert", "12.5%"),
                      [(c["label"], c["after"]) for c in banking])
        assert_human_changes(self, banking)

        self.client.patch(self._url("calendar"), {"next_year_mode": "WARN_ONLY"}, format="json")
        [calendar] = self.client.get(self._url("calendar")).data["data"]["history"][0]["changes"]
        self.assertEqual(calendar["label"], "Next fiscal year")
        self.assertEqual(calendar["after"], "Warn finance staff only")

    def test_no_settings_change_yet_is_an_empty_list(self):
        with patch("vs_rbac.permissions.HasRBACPermission.has_permission", return_value=True):
            response = self.client.get(self._url("payroll"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["history"], [])
