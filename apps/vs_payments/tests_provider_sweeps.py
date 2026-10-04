"""The daily held-ledger check allows for Paystack sweeping the platform's balance to its bank.

Corona (Ikeja, Lekki and Yaba) and Single Site (one branch) hold their online
money with the platform. Mrs Adeyemi pays Lekki N1,800 and Paystack keeps N20,
so the platform holds N1,780 for Lekki; Single Site's parent pays N400 less N6,
so it holds N394 for Single Site. With the platform setting "Paystack balance
swept automatically" off, the check compares Paystack's balance with the books
as it always has and never asks for settlements. Turned on, the check reads
Paystack's settlements, counts each successful one of the main balance exactly
once, and takes it off the books' figure, so Paystack settling N1,780 to the
platform's Zenith account overnight is not a mismatch the next morning. A real
mismatch still raises the incident, a settlement in another currency is refused
loudly, and only platform staff holding the setting's own update permission can
change it, with an audit entry.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase
from django.utils import timezone

from core.test_utils import TenantAPIClient
from vs_config.clock import tenant_today
from vs_config.models import ConfigurationAuditEvent
from vs_finance.models import Account, BankAccount

from . import held_reconciliation, settlement
from .models import HeldReconciliation, ProviderSweep
from .providers import paystack as paystack_module
from .providers import registry
from .providers.base import SettlementRecord
from .providers.paystack import PaystackProvider
from .tests_custody_held import _HeldFixture

UTC = datetime.timezone.utc


def _record(settlement_id, amount, at, *, status="SETTLED", currency="NGN", subaccount=""):
    return SettlementRecord(
        settlement_id=str(settlement_id), status=status, currency=currency, amount=amount,
        settled_at=at, subaccount=subaccount, raw={"id": settlement_id, "status": status},
    )


class _SweepFixture(_HeldFixture):
    """The held fixture with the operators' notice faked and the clock read once."""

    def setUp(self):
        super().setUp()
        notify = patch.object(held_reconciliation, "_notify_operators", return_value=1)
        self.notify = notify.start()
        self.addCleanup(notify.stop)
        self.today = tenant_today(self.platform.tenant)
        self.now = timezone.now()

    def swept(self, on=True):
        """The platform setting, as the check reads it."""
        return patch.object(held_reconciliation, "balance_swept", return_value=on)

    def hold_lekki_and_solo(self):
        """N1,780 held for Lekki and N394 for Single Site: N2,174 in all."""
        self.paid(self.books, self.adeyemi, 180_000, fee=2_000)
        self.paid(self.solo_books, self.solo_parent, 40_000, fee=600)

    def incidents(self, key=held_reconciliation.FAULT_KEY):
        from vs_health.models import Incident

        return Incident.objects.filter(fault_key=key).exclude(status=Incident.Status.RESOLVED)


class SweepSettingOffTests(_SweepFixture):
    """Off, the check is the comparison it always was."""

    def test_off_never_asks_for_settlements_and_compares_the_balance_as_reported(self):
        self.hold_lekki_and_solo()
        self.fake.settlements = [_record("S1", 178_000, self.now)]
        self.fake.balances["NGN"] = 217_400
        with self.swept(False), patch.object(self.fake, "list_settlements") as listing:
            row = held_reconciliation.reconcile_held_ledger()
        listing.assert_not_called()
        self.assertEqual(
            (row.balance_swept, row.swept_total, row.own_swept_settled, row.books_balance,
             row.difference, row.agrees),
            (False, 0, 0, 217_400, 0, True))
        self.assertFalse(ProviderSweep.objects.exists())

    def test_off_a_sweep_paystack_made_is_a_mismatch(self):
        self.hold_lekki_and_solo()
        self.fake.balances["NGN"] = 39_400  # Paystack swept Lekki's N1,780 anyway.
        with self.swept(False):
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.difference, row.agrees), (-178_000, False))
        self.assertTrue(self.incidents().exists())


class SweepSettingOnTests(_SweepFixture):
    """On, each settlement of the main balance is counted once and allowed for."""

    def test_one_sweep_is_counted_and_the_check_agrees(self):
        self.hold_lekki_and_solo()
        self.fake.settlements = [_record("S1", 178_000, self.now - datetime.timedelta(hours=8))]
        self.fake.balances["NGN"] = 39_400
        with self.swept():
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual(
            (row.balance_swept, row.provider_account, row.held_total, row.swept_total,
             row.books_balance, row.difference, row.agrees),
            (True, 217_400, 217_400, 178_000, 39_400, 0, True))
        sweep = ProviderSweep.objects.get()
        self.assertEqual((sweep.settlement_id, sweep.amount, sweep.recorded_on),
                         ("S1", 178_000, self.today))
        self.assertFalse(self.incidents().exists())

    def test_two_sweeps_across_the_run_boundary_each_count_once(self):
        """Lekki's sweep lands at 23:30 Lagos on day one, Single Site's at 00:30 on day two.

        The second is still day one on Paystack's UTC clock, so a window cut on
        Lagos days alone would miss it; the overlap reads it, and the unique key
        stops the first being counted again.
        """
        self.hold_lekki_and_solo()
        day_one, day_two = self.today - datetime.timedelta(days=1), self.today
        late_day_one = datetime.datetime.combine(day_one, datetime.time(22, 30), tzinfo=UTC)
        early_day_two = datetime.datetime.combine(day_one, datetime.time(23, 30), tzinfo=UTC)
        self.fake.settlements = [_record("S-LEKKI", 178_000, late_day_one)]
        self.fake.balances["NGN"] = 39_400
        with self.swept():
            first = held_reconciliation.reconcile_held_ledger(today=day_one)
        self.assertEqual((first.swept_total, first.agrees), (178_000, True))

        self.fake.settlements.append(_record("S-SOLO", 39_400, early_day_two))
        self.fake.balances["NGN"] = 0
        with self.swept():
            second = held_reconciliation.reconcile_held_ledger(today=day_two)
        self.assertEqual((second.swept_total, second.books_balance, second.agrees),
                         (217_400, 0, True))
        self.assertEqual(sorted(ProviderSweep.objects.values_list("settlement_id", "recorded_on")),
                         [("S-LEKKI", day_one), ("S-SOLO", day_two)])
        self.assertEqual(HeldReconciliation.objects.count(), 2)

    def test_a_repeated_run_counts_nothing_twice(self):
        self.hold_lekki_and_solo()
        self.fake.settlements = [_record("S1", 178_000, self.now)]
        self.fake.balances["NGN"] = 39_400
        with self.swept():
            for _ in range(3):
                row = held_reconciliation.reconcile_held_ledger()
            tomorrow = held_reconciliation.reconcile_held_ledger(
                today=self.today + datetime.timedelta(days=1))
        self.assertEqual(ProviderSweep.objects.count(), 1)
        self.assertEqual((row.swept_total, row.agrees), (178_000, True))
        self.assertEqual((tomorrow.swept_total, tomorrow.agrees), (178_000, True))

    def test_failed_pending_and_subaccount_settlements_are_not_counted(self):
        self.hold_lekki_and_solo()
        self.fake.settlements = [
            _record("S-OK", 178_000, self.now),
            _record("S-FAILED", 39_400, self.now, status="FAILED"),
            _record("S-ODD", 39_400, self.now, status="UNKNOWN"),
            # A direct-mode branch's settlement never touched the platform's balance.
            _record("S-SUB", 50_000, self.now, subaccount="ACCT_LEK"),
        ]
        self.fake.balances["NGN"] = 39_400
        with self.swept():
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual(list(ProviderSweep.objects.values_list("settlement_id", flat=True)),
                         ["S-OK"])
        self.assertEqual((row.swept_total, row.agrees), (178_000, True))

    def test_a_settlement_still_being_paid_that_explains_the_difference_is_not_raised(self):
        self.hold_lekki_and_solo()
        self.fake.settlements = [_record("S-PENDING", 178_000, self.now, status="PENDING")]
        self.fake.balances["NGN"] = 39_400  # Paystack already took it from the balance.
        with self.swept():
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.difference, row.agrees, row.swept_total), (None, False, 0))
        self.assertIn("still being paid", row.error)
        self.assertFalse(ProviderSweep.objects.exists())
        self.assertFalse(self.incidents().exists())

        self.fake.settlements = [_record("S-PENDING", 178_000, self.now)]  # Now settled.
        with self.swept():
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.swept_total, row.agrees), (178_000, True))

    def test_a_real_mismatch_still_raises_the_incident(self):
        self.hold_lekki_and_solo()
        self.fake.settlements = [_record("S1", 178_000, self.now)]
        self.fake.balances["NGN"] = 39_350  # N0.50 nobody booked, on top of the sweep.
        with self.swept():
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.swept_total, row.difference, row.agrees), (178_000, -50, False))
        incident = self.incidents().get()
        self.assertEqual(row.incident_code, incident.code)
        self.assertIn("swept to the platform's bank", incident.summary)
        self.assertEqual(self.notify.call_count, 1)

    def test_a_settlement_in_another_currency_is_refused_loudly(self):
        self.hold_lekki_and_solo()
        self.fake.settlements = [_record("S-NGN", 178_000, self.now),
                                 _record("S-USD", 9_000, self.now, currency="USD")]
        self.fake.balances["NGN"] = 39_400
        with self.swept(), self.assertLogs("vs_payments.held_reconciliation", "ERROR"):
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.provider_balance, row.difference, row.agrees), (None, None, False))
        self.assertIn("USD", row.error)
        self.assertFalse(ProviderSweep.objects.exists())  # Nothing in that read is counted.
        self.assertFalse(self.incidents().exists())
        refused = self.incidents(held_reconciliation.SWEEP_FAULT_KEY).get()
        self.assertIn("USD", refused.summary)

        self.fake.settlements = self.fake.settlements[:1]  # Paystack's record is put right.
        with self.swept():
            row = held_reconciliation.reconcile_held_ledger()
        self.assertTrue(row.agrees)
        self.assertFalse(self.incidents(held_reconciliation.SWEEP_FAULT_KEY).exists())

    def test_a_sweep_landing_while_the_balance_is_read_is_read_again(self):
        self.hold_lekki_and_solo()
        reads = []

        def balance(currency="NGN"):
            reads.append(currency)
            if len(reads) == 1:  # Read just before the sweep lands.
                self.fake.settlements.append(_record("S1", 178_000, timezone.now()))
                return 217_400
            return 39_400

        with self.swept(), patch.object(self.fake, "available_balance", side_effect=balance):
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual(len(reads), 2)
        self.assertEqual((row.provider_balance, row.swept_total, row.agrees), (39_400, 178_000, True))

    def test_a_balance_that_never_holds_still_is_recorded_not_raised(self):
        self.hold_lekki_and_solo()
        landed = iter(range(1, 10))

        def balance(currency="NGN"):
            self.fake.settlements.append(_record(f"S{next(landed)}", 100, timezone.now()))
            return 217_400

        with self.swept(), patch.object(self.fake, "available_balance", side_effect=balance):
            row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.provider_balance, row.difference), (None, None))
        self.assertIn("kept settling", row.error)
        self.assertFalse(self.incidents().exists())

    def test_the_platforms_own_takings_settled_after_a_sweep_are_not_taken_off_twice(self):
        """The platform's own N50 subscription payment is swept, then finance matches it.

        It leaves transit when matched, and the sweep that carried it was already
        taken off, so the check adds it back rather than reading N49 short.
        """
        codex_customer = self.customer(self.platform, "CCODEX", self.lagos)
        own = self.paid(self.platform, codex_customer, 5_000, fee=100)
        self.assertFalse(own.held_by_platform)
        gl = Account.objects.create(entity=self.platform, code="1139", name="CodeX Zenith",
                                    account_type="ASSET", is_postable=True)
        codex_bank = BankAccount.objects.create(
            entity=self.platform, name="CodeX Zenith", branch=self.lagos, gl_account=gl,
            bank_name="Zenith", account_number="0011390000", is_primary_collection=True)
        self.fake.settlements = [_record("S-OWN", 4_900, self.now)]
        self.fake.balances["NGN"] = 0
        with self.swept():
            before = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((before.own_in_transit, before.swept_total, before.agrees),
                         (4_900, 4_900, True))

        settlement.settle_collections(self.line(codex_bank, 4_900), [own.pk])
        with self.swept():
            after = held_reconciliation.reconcile_held_ledger()
        self.assertEqual(
            (after.own_in_transit, after.swept_total, after.own_swept_settled, after.agrees),
            (0, 4_900, 4_900, True))


class PaystackSettlementListingTests(_SweepFixture):
    """The Paystack adapter reads every page of the main balance's settlements, through the faked HTTP call."""

    def use_paystack(self):
        """The real adapter from here on; payments are taken through the fake first."""
        registry.register("PAYSTACK", PaystackProvider(secret_key="sk_test_sweeps"))

    def test_a_paginated_list_is_read_to_the_end_and_counted_once(self):
        self.hold_lekki_and_solo()
        self.use_paystack()
        stamp = self.now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        pages = {
            1: [{"id": 11, "status": "success", "currency": "NGN", "effective_amount": 100_000,
                 "total_amount": 100_500, "settlement_date": stamp, "subaccount": None},
                {"id": 12, "status": "failed", "currency": "NGN", "effective_amount": 5_000,
                 "settlement_date": stamp}],
            2: [{"id": 13, "status": "success", "currency": "NGN", "total_amount": 78_000,
                 "settlement_date": stamp},
                {"id": 14, "status": "success", "currency": "NGN", "effective_amount": 9_999,
                 "settlement_date": stamp, "subaccount": {"subaccount_code": "ACCT_LEK"}}],
        }
        urls = []

        def request_json(method, url, **kwargs):
            urls.append(url)
            if url.endswith("/balance"):
                return {"status": True, "data": [{"currency": "NGN", "balance": 39_400}]}
            page = int(url.split("page=")[1].split("&")[0])
            return {"status": True, "data": pages[page],
                    "meta": {"total": 4, "perPage": 2, "page": page, "pageCount": 2}}

        with self.swept(), patch.object(PaystackProvider, "SETTLEMENT_PAGE_SIZE", 2), \
                patch.object(paystack_module, "request_json", side_effect=request_json):
            row = held_reconciliation.reconcile_held_ledger(provider="PAYSTACK")
            again = held_reconciliation.reconcile_held_ledger(provider="PAYSTACK")

        listing = [url for url in urls if "/settlement?" in url]
        self.assertTrue(all("subaccount=none" in url and "perPage=2" in url for url in listing))
        self.assertIn("page=2", listing[1])
        self.assertEqual(sorted(ProviderSweep.objects.values_list("settlement_id", "amount")),
                         [("11", 100_000), ("13", 78_000)])
        self.assertEqual((row.swept_total, row.agrees), (178_000, True))
        self.assertEqual((again.swept_total, again.agrees), (178_000, True))

    def test_a_listing_past_the_page_cap_is_refused_not_half_read(self):
        self.use_paystack()

        def request_json(method, url, **kwargs):
            if url.endswith("/balance"):
                return {"status": True, "data": [{"currency": "NGN", "balance": 0}]}
            return {"status": True, "data": [], "meta": {"pageCount": 500}}

        with self.swept(), patch.object(PaystackProvider, "SETTLEMENT_MAX_PAGES", 3), \
                patch.object(paystack_module, "request_json", side_effect=request_json):
            row = held_reconciliation.reconcile_held_ledger(provider="PAYSTACK")
        self.assertEqual((row.provider_balance, row.difference), (None, None))
        self.assertIn("more than 3 pages", row.error)


class PaystackSettlementRecordTests(SimpleTestCase):
    """One Paystack settlement row, translated."""

    def test_status_amount_instant_and_subaccount_are_read(self):
        record = paystack_module._settlement_record({
            "id": 7, "status": "Success", "currency": "ngn", "effective_amount": "17800",
            "total_amount": 18000, "settlement_date": "2026-10-03T22:30:00.000Z",
            "subaccount": {"subaccount_code": "ACCT_X"},
        })
        self.assertEqual(
            (record.settlement_id, record.status, record.currency, record.amount, record.subaccount),
            ("7", "SETTLED", "NGN", 17_800, "ACCT_X"))
        self.assertEqual(record.settled_at, datetime.datetime(2026, 10, 3, 22, 30, tzinfo=UTC))

    def test_an_unknown_status_and_a_missing_amount_are_not_guessed(self):
        record = paystack_module._settlement_record({"id": 8, "status": "reversed"})
        self.assertEqual((record.status, record.amount, record.currency), ("UNKNOWN", None, ""))
        self.assertEqual(paystack_module._settlement_record({"status": "processing"}).status,
                         "PENDING")


class ProviderSettingsApiTests(_SweepFixture):
    """Only platform staff holding the setting's own update key change it, and it is audited."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_config_catalogue", verbosity=0)
        codex = cls.platform.tenant
        cls.operator = cls.grant(
            cls.user_for(codex, "provider-ops@codex.test"),
            "payments.platform_provider.view", "payments.platform_provider.update",
            "payments.platform_settlement.view", tenant=codex, role_key="provider-ops")
        cls.reader = cls.grant(
            cls.user_for(codex, "provider-reader@codex.test"),
            "payments.platform_provider.view", "config.value.update",
            tenant=codex, role_key="provider-reader")
        cls.bursar = cls.grant(
            cls.user_for(cls.tenant, "provider@corona.test"),
            "payments.platform_provider.view", "payments.platform_provider.update",
            "payments.platform_settlement.view", tenant=cls.tenant, role_key="provider-corona")

    URL = "/v1/payments/platform/provider-settings/"

    def test_an_operator_turns_it_on_the_change_is_audited_and_the_check_reads_it(self):
        client = TenantAPIClient(user=self.operator)
        response = client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["data"]["balance_swept"], response.data["data"]["source"]),
                         (False, "default"))

        response = client.patch(self.URL, {"balance_swept": True,
                                           "reason": "Paystack turned on daily settlement."},
                                format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["data"]["balance_swept"], response.data["data"]["source"]),
                         (True, "platform"))
        event = ConfigurationAuditEvent.objects.get(
            action="config.value.updated", reason="Paystack turned on daily settlement.")
        self.assertEqual((event.actor_id, event.tenant_id, event.before_data, event.after_data),
                         (self.operator.pk, None, {"value": None}, {"value": True}))

        self.hold_lekki_and_solo()
        self.fake.settlements = [_record("S1", 178_000, self.now)]
        self.fake.balances["NGN"] = 39_400
        row = held_reconciliation.reconcile_held_ledger()
        self.assertEqual((row.balance_swept, row.agrees), (True, True))

        sweeps = client.get("/v1/payments/platform/provider-sweeps/")
        self.assertEqual(sweeps.status_code, 200, sweeps.data)
        self.assertEqual([(s["settlement_id"], s["amount"]) for s in sweeps.data["data"]],
                         [("S1", 178_000)])
        self.assertNotIn("raw", sweeps.data["data"][0])

    def test_a_reason_and_a_true_or_false_value_are_required(self):
        client = TenantAPIClient(user=self.operator)
        self.assertEqual(client.patch(self.URL, {"balance_swept": True}, format="json")
                         .status_code, 400)
        self.assertEqual(client.patch(self.URL, {"balance_swept": "yes", "reason": "x"},
                                      format="json").status_code, 400)
        self.assertFalse(held_reconciliation.balance_swept())

    def test_codex_staff_without_the_update_key_cannot_change_it_by_either_route(self):
        client = TenantAPIClient(user=self.reader)
        self.assertEqual(client.get(self.URL).status_code, 200)
        self.assertEqual(client.patch(self.URL, {"balance_swept": True, "reason": "x"},
                                      format="json").status_code, 403)
        generic = client.post("/v1/config/values/", {
            "key": held_reconciliation.SWEPT_KEY, "value": True, "reason": "x"}, format="json")
        self.assertEqual(generic.status_code, 400, generic.data)
        self.assertIn("dedicated update permission", str(generic.data))
        self.assertFalse(held_reconciliation.balance_swept())

    def test_a_school_caller_is_refused_even_holding_the_keys(self):
        client = TenantAPIClient(user=self.bursar)
        self.assertEqual(client.get(self.URL).status_code, 403)
        self.assertEqual(client.patch(self.URL, {"balance_swept": True, "reason": "x"},
                                      format="json").status_code, 403)
        self.assertEqual(client.get("/v1/payments/platform/provider-sweeps/").status_code, 403)
        self.assertFalse(held_reconciliation.balance_swept())
