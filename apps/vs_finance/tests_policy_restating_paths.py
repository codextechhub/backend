"""Payloads that restate a receivables policy value honour its settings switch.

The receivables policy registers the doubtful-debt age bands
(``finance.settings.provision_bands``) and the way a payment from a payer is
split by default (``finance.settings.payer_payment_split``). A provision run
keeps the bands it was worked out with, and the payment screen is offered the
books' default split. Each is read here as a role that may open the screen but
may not read the setting, beside a role that may.
"""
from __future__ import annotations

from datetime import date as D

from vs_finance.provisions import prepare_provision

from .tests_accruals_field_access import SETTINGS, _AccrualsFieldAccess
from .tests_payers_field_access import _PayerFieldAccess


class ProvisionPolicyBandsAccessTests(_AccrualsFieldAccess):
    """A provision run's policy snapshot and age bands are the policy's bands seen through a run."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant, "bands-open")
        cls.no_bands = cls.staff(
            cls.tenant, "bands-hidden", hide=[f"{SETTINGS}.provision_bands"])
        cls.lekki_no_bands = cls.staff(
            cls.tenant, "bands-lekki-hidden", branch=cls.lekki,
            hide=[f"{SETTINGS}.provision_bands"])
        cls.no_line_bands = cls.staff(
            cls.tenant, "bands-line-hidden", hide=["finance.provision.bands"])
        cls.solo_no_bands = cls.staff(
            cls.solo_tenant, "bands-solo-hidden", hide=[f"{SETTINGS}.provision_bands"])
        cls.solo_open = cls.staff(cls.solo_tenant, "bands-solo-open")

    def run_of(self, books=None):
        books = books or self.books
        for customer in (self.eze, self.bello) if books == self.books else (self.ada,):
            self.bill(customer, 1_000_000, on=D(2026, 1, 5), due=D(2026, 1, 10), service=None)
        return prepare_provision(books, as_of=D(2026, 12, 31), narration="Year end")

    def detail(self, user, run, books=None):
        response = self.api(user).get(self.url(f"provisions/{run.pk}/", books))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def listed(self, user, books=None):
        response = self.api(user).get(self.url("provisions/", books))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"][0]

    def test_an_open_role_reads_the_snapshot_and_each_lines_bands(self):
        run = self.run_of()

        data = self.detail(self.open, run)

        self.assertTrue(data["policy_snapshot"])
        self.assertTrue(all("rate_bps" in band for band in data["policy_snapshot"]))
        self.assertTrue(all(line["bands"] for line in data["lines"]))

    def test_a_hidden_provision_bands_setting_leaves_the_detail_without_either(self):
        run = self.run_of()

        data = self.detail(self.no_bands, run)

        self.assertNotIn("policy_snapshot", data)
        self.assertEqual(len(data["lines"]), 2)
        for line in data["lines"]:
            self.assertNotIn("bands", line)
            self.assertIn("required", line)
        self.assertIn("required_total", data)

    def test_the_list_follows_the_same_switch(self):
        self.run_of()

        row = self.listed(self.no_bands)

        self.assertNotIn("policy_snapshot", row)
        for line in row["lines"]:
            self.assertNotIn("bands", line)

    def test_a_branch_reader_who_may_not_read_the_setting_gets_their_part_without_bands(self):
        run = self.run_of()

        data = self.detail(self.lekki_no_bands, run)

        self.assertTrue(data["partial_view"])
        self.assertNotIn("policy_snapshot", data)
        self.assertEqual([line["branch_id"] for line in data["lines"]], [self.lekki.pk])
        self.assertNotIn("bands", data["lines"][0])

    def test_hiding_the_runs_own_bands_field_leaves_the_policy_snapshot(self):
        run = self.run_of()

        data = self.detail(self.no_line_bands, run)

        self.assertIn("policy_snapshot", data)
        for line in data["lines"]:
            self.assertNotIn("bands", line)

    def test_a_single_branch_school_follows_the_same_switch(self):
        run = self.run_of(self.solo_books)

        hidden = self.detail(self.solo_no_bands, run, self.solo_books)
        shown = self.detail(self.solo_open, run, self.solo_books)

        self.assertNotIn("policy_snapshot", hidden)
        self.assertTrue(shown["policy_snapshot"])

    def test_another_schools_role_never_reads_these_books(self):
        run = self.run_of()
        response = self.api(self.solo_open).get(self.url(f"provisions/{run.pk}/"))

        self.assertIn(response.status_code, (403, 404))


class PayerPaymentSplitChoicesAccessTests(_PayerFieldAccess):
    """The payment screen is always offered the options, and the default only to a reader of the setting."""

    @classmethod
    def setUpTestData(cls):
        from vs_rbac.tests.helpers import install_declared_fields

        super().setUpTestData()
        install_declared_fields(SETTINGS)
        cls.open_recorder = cls.staff(cls.tenant)
        cls.no_default = cls.staff(cls.tenant, hide=[f"{SETTINGS}.payer_payment_split"])
        cls.solo_no_default = cls.staff(
            cls.solo_tenant, hide=[f"{SETTINGS}.payer_payment_split"])
        cls.solo_open = cls.staff(cls.solo_tenant)

    def choices(self, user, books=None):
        response = self.api(user).get(self.url("payer-payments/preview/", books))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_an_open_recorder_is_offered_the_options_and_the_default(self):
        data = self.choices(self.open_recorder)

        self.assertEqual(data["split_default"], "OLDEST_FIRST")
        self.assertTrue(data["split_default_label"])
        self.assertEqual(
            [option["value"] for option in data["split_options"]],
            ["OLDEST_FIRST", "PROPORTIONAL", "AS_ENTERED"])

    def test_a_hidden_setting_keeps_the_options_and_drops_the_default(self):
        data = self.choices(self.no_default)

        self.assertNotIn("split_default", data)
        self.assertNotIn("split_default_label", data)
        self.assertEqual(len(data["split_options"]), 3)

    def test_a_single_branch_school_follows_the_same_switch(self):
        hidden = self.choices(self.solo_no_default, self.solo_books)
        shown = self.choices(self.solo_open, self.solo_books)

        self.assertNotIn("split_default", hidden)
        self.assertIn("split_default", shown)

    def test_one_schools_switch_does_not_reach_another_school(self):
        self.assertIn("split_default", self.choices(self.solo_open, self.solo_books))
        self.assertIn("split_default", self.choices(self.open_recorder))
