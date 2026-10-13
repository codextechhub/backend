"""Every branch has a payment terminal at the provider, opened with the branch and routed to the right money.

Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch. Creating Corona's
Surulere branch opens "Corona Group - Surulere Branch" at the provider once the
branch commits; a call the provider refuses leaves a failed row the sweep opens
again; a branch closed for good has its terminal switched off, and one that comes
back gets a new terminal. When Corona takes payments directly, each terminal is
routed through a split that sends every payment to its branch's subaccount, and
back to the platform's balance when Corona switches to held. The backfill command
opens terminals for branches that predate all of this, and running it again opens
none. The platform's own tenant keeps no terminals.
"""
from __future__ import annotations

import datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from vs_finance.models import BankAccount
from vs_tenants.models import Branch, BranchStatus

from . import custody, held, terminals
from .constants import CustodyMode, PaymentAuditAction, TerminalStatus
from .models import PaymentCustodySettings, PaymentEvent, VirtualTerminal
from .providers.paystack import PaystackProvider
from .tests_custody_held import _HeldFixture


class PaystackTerminalWireTests(SimpleTestCase):
    """What the adapter sends for a terminal and reads from a terminal payment.

    The paths, fields and event shape are those of Paystack's Virtual Terminal and
    Split API references (see the adapter's module docstring); this pins what the
    adapter does with them.
    """

    PATCH_TARGET = "vs_payments.providers.paystack.request_json"

    def setUp(self):
        self.provider = PaystackProvider(secret_key="sk_test_x")

    def sent(self, call):
        method, url = call.args[:2]
        return method, url.split("api.paystack.co", 1)[1], call.kwargs.get("body")

    def test_a_terminal_is_opened_with_a_name_destinations_and_currency(self):
        ok = {"status": True, "data": {
            "id": 7, "code": "VT_ABC123", "name": "Corona Group - Lekki Branch", "active": True,
            "paymentMethods": [{"type": "dedicated_account"}],
            "destinations": [{"target": "2348000000000"}]}}
        with patch(self.PATCH_TARGET, return_value=ok) as call:
            result = self.provider.create_terminal(
                name="Corona Group - Lekki Branch",
                destinations=[{"target": "2348000000000", "name": "Payments"}],
                metadata={"branch_id": 4})
        method, path, body = self.sent(call.call_args)
        self.assertEqual((method, path), ("POST", "/virtual_terminal"))
        self.assertEqual(body["name"], "Corona Group - Lekki Branch")
        self.assertEqual(body["destinations"], [{"target": "2348000000000", "name": "Payments"}])
        self.assertEqual(body["currency"], "NGN")
        self.assertEqual(body["metadata"], '{"branch_id": 4}')  # Stringified JSON.
        self.assertEqual((result.code, result.active), ("VT_ABC123", True))
        self.assertIn("paymentMethods", result.details)

    def test_a_terminal_is_fetched_and_deactivated_by_code(self):
        ok = {"status": True, "data": {"code": "VT_ABC123", "name": "x", "active": True}}
        with patch(self.PATCH_TARGET, return_value=ok) as call:
            self.provider.fetch_terminal(code="VT_ABC123")
        self.assertEqual(self.sent(call.call_args)[:2], ("GET", "/virtual_terminal/VT_ABC123"))
        with patch(self.PATCH_TARGET, return_value={"status": True, "message": "inactive"}) as call:
            self.provider.deactivate_terminal(code="VT_ABC123")
        self.assertEqual(self.sent(call.call_args)[:2], ("PUT", "/virtual_terminal/VT_ABC123/deactivate"))

    def test_a_split_sends_everything_to_the_subaccount_which_bears_the_fee(self):
        ok = {"status": True, "data": {"split_code": "SPL_xyz", "id": 3}}
        with patch(self.PATCH_TARGET, return_value=ok) as call:
            result = self.provider.create_split(name="Lekki settlement", subaccount_code="ACCT_LEK")
        method, path, body = self.sent(call.call_args)
        self.assertEqual((method, path), ("POST", "/split"))
        self.assertEqual(body["type"], "percentage")
        self.assertEqual(body["subaccounts"], [{"subaccount": "ACCT_LEK", "share": 100}])
        self.assertEqual((body["bearer_type"], body["bearer_subaccount"]), ("subaccount", "ACCT_LEK"))
        self.assertEqual(result.split_code, "SPL_xyz")

    def test_a_split_is_attached_with_put_and_removed_with_delete_naming_the_split(self):
        ok = {"status": True, "data": {}}
        with patch(self.PATCH_TARGET, return_value=ok) as call:
            self.provider.attach_terminal_split(code="VT_ABC123", split_code="SPL_xyz")
        self.assertEqual(self.sent(call.call_args),
                         ("PUT", "/virtual_terminal/VT_ABC123/split_code", {"split_code": "SPL_xyz"}))
        with patch(self.PATCH_TARGET, return_value=ok) as call:
            self.provider.detach_terminal_split(code="VT_ABC123", split_code="SPL_xyz")
        self.assertEqual(self.sent(call.call_args),
                         ("DELETE", "/virtual_terminal/VT_ABC123/split_code", {"split_code": "SPL_xyz"}))

    def _charge(self, **data):
        payload = {"event": "charge.success", "data": {
            "id": 99, "reference": "TREF1", "amount": 50_000, "currency": "NGN", **data}}
        return self.provider.parse_webhook(payload=payload, raw_body=b"", headers={})

    def test_a_charge_on_a_terminal_names_the_terminal_the_payer_and_the_sender(self):
        parsed = self._charge(
            metadata={"virtual_terminal": {"code": "VT_ABC123"}},
            customer={"email": " Ada@Example.com "},
            authorization={"sender_name": "ADA OKEYE", "sender_bank": "Zenith Bank"})
        self.assertEqual(
            (parsed.terminal_code, parsed.payer_email, parsed.sender_name, parsed.sender_bank),
            ("VT_ABC123", "Ada@Example.com", "ADA OKEYE", "Zenith Bank"))

    def test_metadata_sent_as_a_json_string_is_read_and_a_card_holder_is_the_sender(self):
        parsed = self._charge(
            metadata='{"virtual_terminal": {"code": "VT_STR"}}',
            authorization={"account_name": "T Bello"})
        self.assertEqual((parsed.terminal_code, parsed.sender_name), ("VT_STR", "T Bello"))

    def test_an_ordinary_charge_carries_no_terminal(self):
        parsed = self._charge(metadata={"narration": "fees"}, customer={"email": "a@b.co"})
        self.assertEqual((parsed.terminal_code, parsed.payer_email, parsed.sender_name), ("", "", ""))


class _TerminalFixture(_HeldFixture):
    """Corona's three branches and Single Site's one, each with a terminal row waiting to be opened."""

    #: Each branch's own phone, which becomes its terminal's first notification number.
    PHONES = {"ikeja": "08010000001", "lekki": "08010000002", "yaba": "08010000003",
              "solo_main": "08010000004", "rival_branch": "08010000005"}

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for attr, phone in cls.PHONES.items():
            branch = getattr(cls, attr)
            Branch.all_objects.filter(pk=branch.pk).update(phone=phone)
            branch.refresh_from_db()
        cls.terminal_rows = {
            branch.pk: terminals.ensure_terminal(branch)
            for branch in (cls.ikeja, cls.lekki, cls.solo_main)
        }

    def open(self, branch):
        """Open the branch's terminal at the (fake) provider and return it."""
        return terminals.provision_terminal(self.terminal_rows[branch.pk].pk)


class TerminalRowTests(_TerminalFixture):
    def test_each_branch_has_one_row_named_for_the_tenant_and_the_branch(self):
        self.assertEqual(
            sorted(VirtualTerminal.objects.filter(tenant=self.tenant).values_list("name", "status")),
            [("Corona Group - Ikeja Branch", "PENDING"), ("Corona Group - Lekki Branch", "PENDING")])
        self.assertEqual(
            list(VirtualTerminal.objects.filter(tenant=self.solo_tenant).values_list("name", flat=True)),
            ["Single Site - Main Branch"])

    def test_asking_again_returns_the_same_row(self):
        self.assertEqual(terminals.ensure_terminal(self.lekki).pk, self.terminal_rows[self.lekki.pk].pk)
        self.assertEqual(VirtualTerminal.objects.filter(branch=self.lekki).count(), 1)

    def test_the_platforms_own_tenant_keeps_none(self):
        self.assertIsNone(terminals.ensure_terminal(self.lagos))
        self.assertFalse(VirtualTerminal.objects.filter(branch=self.lagos).exists())

    def test_opening_a_terminal_records_its_code_and_what_the_provider_reported(self):
        terminal = self.open(self.lekki)
        self.assertEqual((terminal.status, terminal.code), (TerminalStatus.ACTIVE, "VT_FAKE0001"))
        self.assertEqual(self.fake.terminals["VT_FAKE0001"]["name"], "Corona Group - Lekki Branch")
        self.assertIn("paymentMethods", terminal.provider_details)
        self.assertEqual(
            PaymentEvent.objects.filter(action=PaymentAuditAction.TERMINAL_CREATED).count(), 1)

    def test_the_branchs_own_phone_is_the_first_destination_in_international_form(self):
        row = self.terminal_rows[self.lekki.pk]
        self.assertEqual(row.destinations, [{"name": "Lekki Branch", "number": "2348010000002"}])
        self.open(self.lekki)
        self.assertEqual(self.fake.terminals["VT_FAKE0001"]["destinations"],
                         [{"target": "2348010000002", "name": "Lekki Branch"}])

    def test_a_terminal_already_open_is_not_opened_twice(self):
        self.open(self.lekki)
        self.open(self.lekki)
        self.assertEqual(len(self.fake.terminals), 1)

    def test_a_row_leased_to_another_worker_is_left_alone(self):
        claim = VirtualTerminal.objects.filter(pk=self.terminal_rows[self.lekki.pk].pk)
        claim.update(claimed_at=timezone.now())
        self.open(self.lekki)
        self.assertEqual(self.fake.terminals, {})
        claim.update(claimed_at=timezone.now() - datetime.timedelta(hours=1))
        self.assertEqual(self.open(self.lekki).status, TerminalStatus.ACTIVE)

    def test_a_refusal_leaves_a_failed_row_with_the_reason_and_the_sweep_opens_it_later(self):
        self.fake.terminal_error = "destinations is required"
        failed = self.open(self.lekki)
        self.assertEqual((failed.status, failed.code), (TerminalStatus.FAILED, ""))
        self.assertIn("destinations is required", failed.last_error)
        self.assertEqual(failed.attempts, 1)
        self.assertIsNone(failed.claimed_at)
        self.assertEqual(PaymentEvent.objects.get(
            action=PaymentAuditAction.TERMINAL_FAILED).succeeded, False)

        # Too soon: the backoff has not passed.
        self.assertEqual(terminals.sweep()["opened"], 0)
        self.fake.terminal_error = ""
        later = timezone.now() + datetime.timedelta(minutes=30)
        summary = terminals.sweep(now=later)
        self.assertEqual(summary["opened"], 3)  # Lekki retried; Ikeja and Main never started.
        self.assertEqual(VirtualTerminal.objects.get(pk=failed.pk).status, TerminalStatus.ACTIVE)

    def test_the_retry_wait_grows_with_each_failure_and_is_capped(self):
        self.fake.terminal_error = "down"
        for _ in range(4):
            VirtualTerminal.objects.filter(pk=self.terminal_rows[self.lekki.pk].pk).update(claimed_at=None)
            self.open(self.lekki)
        row = VirtualTerminal.objects.get(pk=self.terminal_rows[self.lekki.pk].pk)
        self.assertEqual(row.attempts, 4)
        self.assertFalse(terminals._retry_due(row, row.updated_at + datetime.timedelta(minutes=39)))
        self.assertTrue(terminals._retry_due(row, row.updated_at + datetime.timedelta(minutes=41)))
        row.attempts = 20
        self.assertTrue(terminals._retry_due(row, row.updated_at + terminals.MAX_BACKOFF))

    def test_an_unconfigured_provider_is_a_failed_row_not_an_exception(self):
        from .providers import registry

        registry.unregister("PAYSTACK")
        with override_settings(PAYSTACK_SECRET_KEY=""):
            failed = self.open(self.lekki)
        self.assertEqual(failed.status, TerminalStatus.FAILED)
        self.assertIn("not configured", failed.last_error)


class BranchLifecycleTests(_TerminalFixture):
    """Creating a branch opens its terminal; taking it out of service for good switches it off.

    The setting is switched on per test, after the class fixture is built, so the
    fixture's own branches are created as every other test's are: with no terminal.
    """

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(PAYMENTS_TERMINALS_AUTO_CREATE=True))

    def new_branch(self, tenant, name, *, is_main=False, phone="08031112222"):
        with self.captureOnCommitCallbacks(execute=True):
            return Branch.objects.create(tenant=tenant, name=name, is_main=is_main,
                                         status=BranchStatus.ACTIVE, phone=phone)

    def test_a_branch_without_a_phone_waits_for_a_number_and_is_opened_when_it_has_one(self):
        branch = self.new_branch(self.tenant, "Surulere Branch", phone="")
        row = VirtualTerminal.objects.get(branch=branch)
        self.assertEqual((row.status, row.code, row.destinations),
                         (TerminalStatus.NEEDS_NUMBER, "", []))
        self.assertEqual(row.last_error, "Add the branch's phone number to open its terminal.")
        self.assertEqual(self.fake.terminals, {})  # The provider was not asked.

        with self.captureOnCommitCallbacks(execute=True):
            branch.phone = "0803 111 2222"
            branch.save(update_fields=["phone", "updated_at"])
        row.refresh_from_db()
        self.assertEqual((row.status, row.last_error), (TerminalStatus.ACTIVE, ""))
        self.assertEqual(row.destinations, [{"name": "Surulere Branch", "number": "2348031112222"}])
        self.assertEqual(self.fake.terminals[row.code]["destinations"],
                         [{"target": "2348031112222", "name": "Surulere Branch"}])

    def test_saving_a_branch_with_no_field_list_also_wakes_a_waiting_terminal(self):
        branch = self.new_branch(self.tenant, "Surulere Branch", phone="")
        with self.captureOnCommitCallbacks(execute=True):
            branch.phone = "+234 803 111 2222"
            branch.save()
        self.assertEqual(VirtualTerminal.objects.get(branch=branch).status, TerminalStatus.ACTIVE)

    def test_a_phone_that_is_not_a_number_keeps_the_terminal_waiting(self):
        branch = self.new_branch(self.tenant, "Surulere Branch", phone="12345")
        self.assertEqual(VirtualTerminal.objects.get(branch=branch).status, TerminalStatus.NEEDS_NUMBER)

    def test_the_sweep_wakes_a_waiting_terminal_whose_branch_was_given_a_phone_quietly(self):
        branch = self.new_branch(self.tenant, "Surulere Branch", phone="")
        Branch.all_objects.filter(pk=branch.pk).update(phone="08031112222")
        summary = terminals.sweep(now=timezone.now() + datetime.timedelta(hours=1))
        self.assertGreaterEqual(summary["opened"], 1)
        self.assertEqual(VirtualTerminal.objects.get(branch=branch).status, TerminalStatus.ACTIVE)

    def test_the_sweep_counts_but_does_not_call_the_provider_for_a_branch_with_no_phone(self):
        branch = self.new_branch(self.tenant, "Surulere Branch", phone="")
        before = len(self.fake.terminals)
        summary = terminals.sweep(now=timezone.now() + datetime.timedelta(hours=1))
        self.assertEqual(summary["needs_number"], 1)
        self.assertEqual(len(self.fake.terminals) - before, 3)  # The fixture's three pending rows only.
        self.assertEqual(VirtualTerminal.objects.get(branch=branch).status, TerminalStatus.NEEDS_NUMBER)

    def test_creating_a_branch_opens_its_terminal_once_the_branch_commits(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            branch = Branch.objects.create(tenant=self.tenant, name="Surulere Branch",
                                           is_main=False, status=BranchStatus.ACTIVE,
                                           phone="08031112222")
        row = VirtualTerminal.objects.get(branch=branch)
        self.assertEqual((row.status, row.name), (TerminalStatus.PENDING, "Corona Group - Surulere Branch"))
        self.assertEqual(self.fake.terminals, {})  # Nothing is called until the commit.
        for callback in callbacks:
            callback()
        row.refresh_from_db()
        self.assertEqual(row.status, TerminalStatus.ACTIVE)
        self.assertEqual(self.fake.terminals[row.code]["name"], "Corona Group - Surulere Branch")

    def test_a_single_branch_tenants_first_branch_gets_one_too(self):
        from vs_rbac.tests.helpers import make_school

        school = make_school(slug="brand-new", name="Brand New School", status="ACTIVE")
        branch = self.new_branch(school.tenant, "Main Branch", is_main=True)
        self.assertEqual(VirtualTerminal.objects.get(branch=branch).status, TerminalStatus.ACTIVE)

    def test_the_platforms_branches_get_none(self):
        branch = self.new_branch(self.platform.tenant, "Abuja")
        self.assertFalse(VirtualTerminal.objects.filter(branch=branch).exists())

    @override_settings(PAYMENTS_TERMINALS_AUTO_CREATE=False)
    def test_a_deployment_that_switches_it_off_opens_none(self):
        branch = self.new_branch(self.tenant, "Surulere Branch")
        self.assertFalse(VirtualTerminal.objects.filter(branch=branch).exists())

    def test_renaming_a_branch_that_has_no_terminal_does_not_give_it_one(self):
        self.lekki.name = "Lekki Phase 1"
        self.lekki.save()
        self.yaba.name = "Yaba Annex"
        self.yaba.save()
        self.assertFalse(VirtualTerminal.objects.filter(branch=self.yaba).exists())

    def test_a_branch_that_is_deactivated_has_its_terminal_switched_off(self):
        branch = self.new_branch(self.tenant, "Surulere Branch")
        code = VirtualTerminal.objects.get(branch=branch).code
        with self.captureOnCommitCallbacks(execute=True):
            branch.mark_inactive(actor_id="1", reason="Moved")
        row = VirtualTerminal.objects.get(branch=branch)
        self.assertEqual(row.status, TerminalStatus.DEACTIVATED)
        self.assertFalse(self.fake.terminals[code]["active"])
        self.assertIsNotNone(row.deactivated_at)

    def test_a_closed_branch_too_and_a_suspended_one_keeps_its_terminal(self):
        suspended = self.new_branch(self.tenant, "Surulere Branch")
        closed = self.new_branch(self.tenant, "Ajah Branch")
        with self.captureOnCommitCallbacks(execute=True):
            suspended.suspend(actor_id="1", reason="Audit")
            closed.transition(to_state=BranchStatus.CLOSED, actor_id="1")
        self.assertEqual(VirtualTerminal.objects.get(branch=suspended).status, TerminalStatus.ACTIVE)
        self.assertEqual(VirtualTerminal.objects.get(branch=closed).status, TerminalStatus.DEACTIVATED)

    def test_a_branch_that_returns_gets_a_new_terminal_and_keeps_the_old_one_deactivated(self):
        branch = self.new_branch(self.tenant, "Surulere Branch")
        first = VirtualTerminal.objects.get(branch=branch)
        with self.captureOnCommitCallbacks(execute=True):
            branch.mark_inactive(actor_id="1", reason="Moved")
        with self.captureOnCommitCallbacks(execute=True):
            branch.reactivate(actor_id="1")
        rows = list(VirtualTerminal.objects.filter(branch=branch).order_by("pk"))
        self.assertEqual([r.status for r in rows], [TerminalStatus.DEACTIVATED, TerminalStatus.ACTIVE])
        self.assertNotEqual(rows[0].code, rows[1].code)
        self.assertEqual(rows[0].pk, first.pk)

    def test_the_sweep_switches_off_the_terminal_of_a_branch_that_left_if_the_task_was_lost(self):
        terminal = self.open(self.lekki)
        Branch.all_objects.filter(pk=self.lekki.pk).update(status=BranchStatus.CLOSED)
        self.assertEqual(terminals.sweep()["deactivated"], 1)
        terminal.refresh_from_db()
        self.assertEqual(terminal.status, TerminalStatus.DEACTIVATED)
        self.assertEqual(terminals.sweep()["deactivated"], 0)  # Idempotent.

    def test_a_provider_refusal_to_deactivate_leaves_it_for_the_sweep(self):
        terminal = self.open(self.lekki)
        Branch.all_objects.filter(pk=self.lekki.pk).update(status=BranchStatus.INACTIVE)
        self.fake.terminal_error = "cannot deactivate"
        self.assertEqual(terminals.sweep()["deactivated"], 0)
        terminal.refresh_from_db()
        self.assertEqual(terminal.status, TerminalStatus.ACTIVE)
        self.fake.terminal_error = ""
        self.assertEqual(terminals.sweep()["deactivated"], 1)


class BackfillTests(_TerminalFixture):
    def run_command(self, *args):
        out = StringIO()
        call_command("backfill_virtual_terminals", *args, stdout=out)
        return out.getvalue()

    def test_it_opens_a_terminal_for_every_in_service_branch_without_one_and_only_those(self):
        output = self.run_command()
        # Ikeja, Lekki and Single Site already had a row; Yaba and the rival's branch did not.
        self.assertIn("created: 2", output)
        self.assertIn("already present: 3", output)
        self.assertEqual(
            sorted(VirtualTerminal.objects.exclude(tenant=self.rival_tenant)
                   .values_list("branch__name", "status")),
            [("Ikeja Branch", "ACTIVE"), ("Lekki Branch", "ACTIVE"), ("Main Branch", "ACTIVE"),
             ("Yaba Branch", "ACTIVE")])
        self.assertFalse(VirtualTerminal.objects.filter(branch=self.lagos).exists())

    def test_running_it_again_opens_none(self):
        self.run_command()
        count, opened = VirtualTerminal.objects.count(), len(self.fake.terminals)
        again = self.run_command()
        self.assertIn("created: 0", again)
        self.assertIn("opened at the provider: 0", again)
        self.assertEqual((VirtualTerminal.objects.count(), len(self.fake.terminals)), (count, opened))

    def test_a_dry_run_writes_nothing_and_calls_nothing(self):
        before = VirtualTerminal.objects.count()
        output = self.run_command("--dry-run")
        self.assertIn("would create: 2", output)  # Yaba and the rival's one branch.
        self.assertEqual(VirtualTerminal.objects.count(), before)
        self.assertEqual(self.fake.terminals, {})

    def test_one_tenant_only(self):
        self.run_command("--tenant", self.solo_tenant.slug)
        self.assertEqual(len(self.fake.terminals), 1)
        self.assertFalse(VirtualTerminal.objects.filter(branch=self.yaba).exists())

    def test_rows_only_leaves_opening_to_the_sweep(self):
        self.run_command("--rows-only")
        self.assertEqual(self.fake.terminals, {})
        self.assertTrue(VirtualTerminal.objects.filter(branch=self.yaba).exists())
        self.assertEqual(terminals.sweep(now=timezone.now() + datetime.timedelta(hours=1))["opened"], 5)

    def test_a_branch_with_no_phone_gets_a_waiting_row_and_is_counted(self):
        Branch.all_objects.filter(pk=self.yaba.pk).update(phone="")
        output = self.run_command()
        self.assertIn("waiting for a branch phone number: 1", output)
        self.assertEqual(VirtualTerminal.objects.get(branch=self.yaba).status,
                         TerminalStatus.NEEDS_NUMBER)
        self.assertEqual(len(self.fake.terminals), 4)  # Ikeja, Lekki, Main and the rival's; never Yaba's.
        again = self.run_command()
        self.assertIn("created: 0", again)
        self.assertIn("waiting for a branch phone number: 1", again)

    def test_a_branch_out_of_service_is_skipped(self):
        Branch.all_objects.filter(pk=self.yaba.pk).update(status=BranchStatus.CLOSED)
        self.run_command()
        self.assertFalse(VirtualTerminal.objects.filter(branch=self.yaba).exists())

    def test_a_row_that_failed_before_is_opened_by_the_next_run(self):
        self.fake.terminal_error = "down"
        self.run_command()
        self.assertEqual(set(VirtualTerminal.objects.exclude(tenant=self.rival_tenant)
                             .values_list("status", flat=True)), {TerminalStatus.FAILED})
        self.fake.terminal_error = ""
        VirtualTerminal.objects.update(claimed_at=None)
        self.run_command()
        self.assertEqual(set(VirtualTerminal.objects.exclude(tenant=self.rival_tenant)
                             .values_list("status", flat=True)), {TerminalStatus.ACTIVE})


class RoutingTests(_TerminalFixture):
    """Direct tenants' terminals send their money to the branch's subaccount; held tenants' do not."""

    def setUp(self):
        super().setUp()
        self.lekki_terminal = self.open(self.lekki)
        self.solo_terminal = self.open(self.solo_main)

    def test_a_held_terminal_has_no_split(self):
        self.assertEqual((self.lekki_terminal.split_code, self.lekki_terminal.split_subaccount), ("", ""))
        self.assertEqual(self.fake.terminal_splits, {})
        self.assertFalse(terminals.sync_terminal_routing(self.lekki_terminal))

    def test_switching_to_direct_attaches_a_split_to_the_branchs_subaccount(self):
        self.settle_directly()
        self.assertEqual(terminals.sync_tenant_terminals(self.tenant), {"changed": 1, "failed": 0})
        self.lekki_terminal.refresh_from_db()
        self.assertEqual(self.lekki_terminal.split_subaccount, "ACCT_LEK")
        split = self.lekki_terminal.split_code
        self.assertEqual(self.fake.splits[split], "ACCT_LEK")
        self.assertEqual(self.fake.terminal_splits[self.lekki_terminal.code], split)
        self.assertEqual(PaymentEvent.objects.filter(
            action=PaymentAuditAction.TERMINAL_ROUTING_CHANGED).count(), 1)

    def test_a_one_branch_tenant_routes_its_terminal_the_same_way(self):
        self.settle_directly()
        terminals.sync_tenant_terminals(self.solo_tenant)
        self.solo_terminal.refresh_from_db()
        self.assertEqual(self.solo_terminal.split_subaccount, "ACCT_SOLO")

    def test_syncing_again_changes_nothing(self):
        self.settle_directly()
        terminals.sync_tenant_terminals(self.tenant)
        self.assertEqual(terminals.sync_tenant_terminals(self.tenant), {"changed": 0, "failed": 0})
        self.assertEqual(len(self.fake.splits), 1)

    def test_switching_back_to_held_removes_the_split(self):
        self.settle_directly()
        terminals.sync_tenant_terminals(self.tenant)
        PaymentCustodySettings.objects.filter(tenant=self.tenant).update(mode=CustodyMode.HELD)
        terminals.sync_tenant_terminals(self.tenant)
        self.lekki_terminal.refresh_from_db()
        self.assertEqual((self.lekki_terminal.split_code, self.lekki_terminal.split_subaccount), ("", ""))
        self.assertEqual(self.fake.terminal_splits, {})

    def test_a_branch_whose_subaccount_changed_gets_a_new_split(self):
        self.settle_directly()
        terminals.sync_tenant_terminals(self.tenant)
        first = VirtualTerminal.objects.get(pk=self.lekki_terminal.pk).split_code
        BankAccount.objects.filter(pk=self.lekki_bank.pk).update(gateway_subaccount_code="ACCT_LEK2")
        terminals.sync_tenant_terminals(self.tenant)
        row = VirtualTerminal.objects.get(pk=self.lekki_terminal.pk)
        self.assertEqual(row.split_subaccount, "ACCT_LEK2")
        self.assertNotEqual(row.split_code, first)
        self.assertEqual(self.fake.terminal_splits[row.code], row.split_code)

    def test_a_direct_branch_without_a_subaccount_stays_on_the_platforms_balance(self):
        PaymentCustodySettings.objects.update_or_create(
            tenant=self.tenant, defaults={"mode": CustodyMode.DIRECT})
        self.assertEqual(terminals.desired_subaccount(self.lekki_terminal), "")
        self.assertFalse(terminals.sync_terminal_routing(self.lekki_terminal))

    def test_saving_a_branchs_subaccount_routes_its_terminal(self):
        PaymentCustodySettings.objects.update_or_create(
            tenant=self.tenant, defaults={"mode": CustodyMode.DIRECT})
        custody.save_collection_subaccount(
            entity=self.books, bank_account=self.lekki_bank, settlement_bank_code="057")
        row = VirtualTerminal.objects.get(pk=self.lekki_terminal.pk)
        self.assertTrue(row.split_code)
        self.assertEqual(self.fake.splits[row.split_code],
                         BankAccount.objects.get(pk=self.lekki_bank.pk).gateway_subaccount_code)

    def test_the_daily_repair_catches_up_a_route_that_did_not_attach(self):
        self.settle_directly()
        self.fake.terminal_error = "split service down"
        self.assertEqual(terminals.sync_tenant_terminals(self.tenant)["failed"], 1)
        self.assertEqual(VirtualTerminal.objects.get(pk=self.lekki_terminal.pk).split_code, "")
        self.fake.terminal_error = ""
        self.assertEqual(terminals.repair_routes()["rerouted"], 2)  # Corona's Lekki and Single Site.
        self.assertEqual(terminals.repair_routes()["rerouted"], 0)

    def test_applying_a_custody_switch_routes_the_tenants_terminals(self):
        self.settle_directly()
        PaymentCustodySettings.objects.filter(tenant=self.tenant).update(
            mode=CustodyMode.HELD, pending_mode=CustodyMode.DIRECT,
            pending_from=datetime.date.today() - datetime.timedelta(days=1))
        row = PaymentCustodySettings.objects.get(tenant=self.tenant)
        with patch.object(held, "outstanding_reasons", return_value=[]), \
                patch.object(held, "run_settlements", return_value={}), \
                patch.object(custody, "branches_without_subaccount", return_value=[]):
            result = held.apply_custody_switch(row)
        self.assertEqual(result["result"], "switched")
        self.assertEqual(result["terminals"], {"changed": 1, "failed": 0})
        self.assertTrue(VirtualTerminal.objects.get(pk=self.lekki_terminal.pk).split_code)
