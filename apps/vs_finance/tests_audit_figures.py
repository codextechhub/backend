"""The finance trail says what happened and where; its figures belong to Field Access.

Every finance audit sentence names the act, the document, the people, the
branches and the accounts, and never an amount, a narration, an address or a
free-text reason. The figures travel in the entry's ``before``/``after`` under
the registered field names, which the trail filters per reader, and the central
platform trail copies none of it: it records who did what to which document, in
which branch, and when.

Corona runs Ikeja and Lekki. Each class below drives one area through the real
service or endpoint, reads the stored entry, and reads it again as a role that
may read the figure and a role that may not.
"""
from __future__ import annotations

import datetime
import itertools
from types import SimpleNamespace

from django.test import SimpleTestCase

from core.test_utils import TenantAPIClient
from vs_audit.models import AuditEvent
from vs_finance.audit import (
    ENTRY_MARKER, MIRROR_MARKER, MIRROR_METADATA_KEYS, entry_reason, entry_sentence,
    mirror_summary, record,
)
from vs_finance.constants import FinanceAuditAction
from vs_finance.models import FinanceAuditLog, FiscalPeriod
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_accruals import D, _AccrualFixture
from .tests_accruals_field_access import DEPOSIT, DI, PROVISION, _AccrualsFieldAccess
from .tests_bank_documents_field_access import TRF, TXN, _BankFieldAccess
from .tests_branch_scope import _FinanceBranchFixture
from .tests_dashboard_field_access import _CloseFixture
from .tests_inter_branch import TRANSFER_KEYS, _InterBranchFixture
from .tests_payers_field_access import PAY, _PayerFieldAccess
from .tests_payroll_split import _SplitFixture
from .tests_petty_cash_field_access import NAIRA, PC, _PettyCashFieldAccess
from .tests_petty_cash_returns import JAN_10

_seq = itertools.count(1)


def assert_no_figure(test, text, *amounts):
    """*text* carries no naira sign and none of *amounts* (kobo) in any common spelling."""
    test.assertNotIn("₦", text)
    for kobo in amounts:
        test.assertNotIn(f"{kobo / 100:,.2f}", text)
        test.assertNotIn(f"{kobo:,}", text)
        test.assertNotIn(str(kobo), text)


def audit_rows(test, user, books, action, **query):
    """The finance trail's rows for *action*, as *user* is served them."""
    extra = "".join(f"&{key}={value}" for key, value in query.items())
    response = TenantAPIClient(user=user).get(
        f"/v1/finance/audit-logs/?entity={books.code}&action={action}{extra}",
    )
    test.assertEqual(response.status_code, 200, getattr(response, "data", None))
    return response.data["data"]


def reader_role(grant, tenant, label, field_keys, *, hide=(), extra_keys=("finance.audit.view",),
                email_domain="figures.test", branch=None):
    """A user who reads the trail, with every field of ``field_keys`` open except ``hide``."""
    name = f"fig-{label}-{next(_seq)}"
    user = grant(
        _FinanceBranchFixture.user_for(tenant, f"{name}@{email_domain}"), *extra_keys,
        tenant=tenant, role_key=name, branch=branch,
    )
    role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
    set_field_access(role, *field_keys, read=True, write=True)
    if hide:
        set_field_access(role, *hide, read=False, write=False)
    return user


def _k(resource, *names):
    return [f"{resource}.{name}" for name in names]


# --------------------------------------------------------------------------- #
# The central trail                                                           #
# --------------------------------------------------------------------------- #

class CentralMirrorTests(_AccrualFixture):
    """The mirror records who did what to which document, and no value."""

    def test_the_mirrored_entry_carries_neither_the_sentence_nor_the_arguments(self):
        log = record(
            entity=self.books, action=FinanceAuditAction.INVOICE_POSTED,
            target_type="Invoice", target_id="7", document_number="INV-0007",
            message="Posted invoice for EZE (₦150,000.00) to ada@family.test: school fees",
            branch=self.ikeja, amount=15_000_000, recipients=["ada@family.test"],
            narration="school fees", reason="Because the bursar asked", error_code="E1",
        )

        event = AuditEvent.objects.get(entity_type="vs_finance.Invoice", entity_id="7")

        self.assertEqual(event.summary, mirror_summary("INVOICE_POSTED", "INV-0007"))
        self.assertEqual(event.summary, "Invoice posted: INV-0007")
        self.assertEqual(
            set(event.metadata),
            {"finance_action", "error_code", MIRROR_MARKER},
        )
        self.assertTrue(set(event.metadata) <= MIRROR_METADATA_KEYS | {MIRROR_MARKER})
        self.assertEqual(event.branch_id, self.ikeja.pk)
        self.assertEqual(event.entity_label, "INV-0007")
        self.assertEqual(log.metadata["amount"], 15_000_000)

    def test_a_value_a_caller_adds_later_cannot_travel(self):
        record(
            entity=self.books, action=FinanceAuditAction.REFUND_POSTED,
            target_type="Refund", target_id="9", message="Refunded EZE.",
            some_new_figure=123_456, payer_name="Mr Okafor",
        )

        event = AuditEvent.objects.get(entity_type="vs_finance.Refund", entity_id="9")

        self.assertNotIn("some_new_figure", event.metadata)
        self.assertNotIn("payer_name", event.metadata)
        self.assertNotIn("123456", str(event.__dict__))

    def test_a_refusal_is_a_sentence_made_of_its_error_code(self):
        from vs_finance.exceptions import PettyCashOverdrawError

        from vs_finance.audit import record_rejection

        record_rejection(
            entity=self.books, action=FinanceAuditAction.PETTY_CASH_VOUCHER_REJECTED,
            exc=PettyCashOverdrawError(fund_name="Front desk", requested=500_000, on_hand=100_000),
            target_type="PettyCashFund", target_id="3",
        )

        log = FinanceAuditLog.objects.get(target_type="PettyCashFund", target_id="3")
        event = AuditEvent.objects.get(entity_type="vs_finance.PettyCashFund", entity_id="3")

        self.assertEqual(log.message, "Refused: petty cash overdrawn.")
        assert_no_figure(self, log.message, 500_000)
        self.assertIn("₦5,000.00", log.metadata["refusal"])
        assert_no_figure(self, event.summary)
        self.assertNotIn("refusal", event.metadata)

    def test_an_entry_mirrored_before_the_rule_is_read_without_its_values(self):
        from vs_audit.serializers import AuditEventDetailSerializer
        from vs_audit.protected_words import (
            visible_metadata, visible_summary, visible_summary_expression,
        )
        from vs_audit.services import emit_audit_event

        old = emit_audit_event(
            module_key="FINANCE", action_type="FINANCIAL_TRANSACTION",
            entity_type="vs_finance.Invoice", entity_id="5", entity_label="INV-0005",
            tenant=self.tenant, summary="Posted invoice for EZE (₦5,000.00).",
            metadata={"finance_action": "INVOICE_POSTED", "total": 500_000,
                      "recipients": ["ada@family.test"]},
        )

        self.assertEqual(visible_summary(old, lambda key: True), "Invoice posted: INV-0005")
        self.assertEqual(
            visible_metadata(old, lambda key: True), {"finance_action": "INVOICE_POSTED"},
        )
        data = AuditEventDetailSerializer(old).data
        self.assertEqual(data["summary"], "Invoice posted: INV-0005")
        self.assertEqual(dict(data["metadata"]), {"finance_action": "INVOICE_POSTED"})
        expression = visible_summary_expression(lambda key: True)
        shown = AuditEvent.objects.filter(pk=old.pk).annotate(shown=expression).get()
        self.assertEqual(shown.shown, "Invoice posted: INV-0005")
        self.assertFalse(
            AuditEvent.objects.annotate(shown=expression).filter(shown__contains="5,000").exists(),
        )
        stored = AuditEvent.objects.get(pk=old.pk)
        self.assertEqual(stored.metadata["total"], 500_000)

    def test_the_platform_statutory_data_trail_keeps_its_own_values(self):
        from vs_audit.protected_words import visible_summary

        event = SimpleNamespace(
            module_key="FINANCE", action_type="UPDATE", entity_type="vs_finance.PayeBand",
            entity_label="Band 1", summary="Changed band.", metadata={
                "finance_action": "PAYROLL_STATUTORY_DATA_CHANGED"},
        )
        self.assertEqual(visible_summary(event, lambda key: True), "Changed band.")


class EntrySentenceTests(SimpleTestCase):
    """:func:`entry_sentence` and :func:`entry_reason`, judged on the row alone."""

    @staticmethod
    def row(action="INVOICE_POSTED", number="INV-0042", message="", **metadata):
        return SimpleNamespace(
            action=action, document_number=number, message=message, metadata=metadata,
        )

    def test_an_entry_written_now_keeps_its_own_sentence(self):
        row = self.row(message="Posted invoice INV-0042 for EZE.", **{ENTRY_MARKER: True})
        self.assertEqual(entry_sentence(row), "Posted invoice INV-0042 for EZE.")

    def test_an_older_entry_is_rebuilt_from_its_action_and_document(self):
        row = self.row(message="Posted invoice for EZE (₦150,000.00) to ada@family.test.")
        self.assertEqual(entry_sentence(row), "Invoice posted: INV-0042")
        self.assertEqual(entry_sentence(self.row(number="")), "Invoice posted")

    def test_an_older_entry_without_any_metadata_is_still_rebuilt(self):
        row = SimpleNamespace(
            action="REFUND_POSTED", document_number="", message="Refunded ₦5,000.", metadata=None,
        )
        self.assertEqual(entry_sentence(row), mirror_summary("REFUND_POSTED"))
        self.assertNotIn("₦", entry_sentence(row))

    def test_a_reason_is_the_metadata_reason_for_every_reader(self):
        row = self.row("RFQ_CANCELLED", reason="  Requirement withdrawn ")
        self.assertEqual(entry_reason(row), "Requirement withdrawn")
        self.assertEqual(entry_reason(self.row("RFQ_CANCELLED")), "")
        self.assertEqual(entry_reason(self.row("RFQ_CANCELLED", reason=["no"])), "")

    def test_a_pay_change_reason_needs_a_readable_pay_switch(self):
        row = self.row("SALARY_CHANGED", reason="Promoted to head of department")
        self.assertEqual(entry_reason(row), "")
        self.assertEqual(entry_reason(row, can_read=lambda key: False), "")
        self.assertEqual(
            entry_reason(row, can_read=lambda key: key == "finance.salary.paye_amount"),
            "Promoted to head of department",
        )

    def test_a_declined_request_reason_is_read_from_the_snapshot_the_reader_has(self):
        row = self.row("INTER_BRANCH_DECLINED")
        self.assertEqual(entry_reason(row), "")
        self.assertEqual(entry_reason(row, snapshots={"after": {}}), "")
        self.assertEqual(
            entry_reason(row, snapshots={"after": {"decline_reason": "No cash"}}), "No cash",
        )


class OlderEntryAndReasonTests(_InterBranchFixture):
    """Entries written before sentences were figure-free, and the reasons that left them.

    Corona's bursar reads the trail. An entry from before the rule says
    "Posted invoice for EZE (N150,000.00) to ada@family.test" in the table; no
    screen shows that, only the act and the document, and the table is
    untouched. A reason such as why a contract ended is served beside the
    sentence, and a reason that belongs to a Field Access field follows it.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields(IB, "finance.salary")
        cls.open = reader_role(cls.grant, cls.tenant, "old-open", cls.field_keys,
                               email_domain="corona.test")
        cls.no_pay = reader_role(
            cls.grant, cls.tenant, "old-no-pay", cls.field_keys, email_domain="corona.test",
            hide=_k("finance.salary", "gross_amount", "paye_amount", "pension_amount",
                    "net_amount", "components"),
        )
        cls.no_decline = reader_role(
            cls.grant, cls.tenant, "old-no-decline", cls.field_keys, email_domain="corona.test",
            hide=_k(IB, "decline_reason"),
        )
        cls.rival_reader = reader_role(
            cls.grant, cls.rival_tenant, "old-rival", cls.field_keys, email_domain="rival.test",
        )
        cls.old = FinanceAuditLog.objects.create(
            entity=cls.books, action="INVOICE_POSTED", target_type="Invoice", target_id="7",
            document_number="INV-0007", branch=cls.ikeja,
            message="Posted invoice for EZE (₦150,000.00) to ada@family.test: school fees",
            metadata={"total": 15_000_000},
        )
        cls.new = record(
            entity=cls.books, action=FinanceAuditAction.INVOICE_POSTED, target_type="Invoice",
            target_id="8", document_number="INV-0008", branch=cls.ikeja,
            message="Posted invoice INV-0008 for EZE.",
        )

    def entries(self, user, books=None, **query):
        extra = "".join(f"&{key}={value}" for key, value in query.items())
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/audit-logs/?entity={(books or self.books).code}{extra}",
        )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return {row["id"]: row for row in response.data["data"]}

    def test_the_list_and_the_detail_rebuild_an_older_sentence(self):
        shown = self.entries(self.open)

        self.assertEqual(shown[self.old.pk]["message"], "Invoice posted: INV-0007")
        self.assertEqual(shown[self.new.pk]["message"], "Posted invoice INV-0008 for EZE.")
        detail = TenantAPIClient(user=self.open).get(
            f"/v1/finance/audit-logs/{self.old.pk}/?entity={self.books.code}",
        )
        self.assertEqual(detail.data["data"]["message"], "Invoice posted: INV-0007")
        for text in (shown[self.old.pk]["message"], detail.data["data"]["message"]):
            assert_no_figure(self, text, 15_000_000)
            self.assertNotIn("ada@family.test", text)
            self.assertNotIn("school fees", text)

    def test_the_stored_sentence_is_untouched(self):
        self.entries(self.open)

        stored = FinanceAuditLog.objects.get(pk=self.old.pk)
        self.assertIn("₦150,000.00", stored.message)
        self.assertNotIn(ENTRY_MARKER, stored.metadata)
        self.assertTrue(FinanceAuditLog.objects.get(pk=self.new.pk).metadata[ENTRY_MARKER])

    def test_another_schools_reader_never_meets_these_entries(self):
        self.assertEqual(self.entries(self.rival_reader, self.rival_books), {})
        self.assertNotIn(self.old.pk, self.entries(self.rival_reader, self.rival_books))

    def test_a_reason_is_served_beside_the_sentence_and_not_inside_it(self):
        row = record(
            entity=self.books, action=FinanceAuditAction.PETTY_CASH_FUND_REOPENED,
            target_type="PettyCashFund", target_id="3", branch=self.lekki,
            message="Reopened petty cash fund 'Front desk'.", reason="Audit found a missing voucher",
        )

        shown = self.entries(self.open)[row.pk]

        self.assertEqual(shown["reason"], "Audit found a missing voucher")
        self.assertNotIn("voucher", shown["message"])
        self.assertEqual(self.entries(self.open)[self.new.pk]["reason"], "")
        self.assertEqual(
            self.entries(self.no_pay)[row.pk]["reason"], "Audit found a missing voucher",
        )

    def test_a_pay_change_reason_follows_the_pay_switches(self):
        row = record(
            entity=self.books, action=FinanceAuditAction.SALARY_CHANGED,
            target_type="EmployeeSalary", target_id="9", branch=self.ikeja,
            message="Changed Ada's pay terms.", reason="Promotion, N50,000 more",
            before={"gross_amount": 100}, after={"gross_amount": 150},
        )

        self.assertEqual(self.entries(self.open)[row.pk]["reason"], "Promotion, N50,000 more")
        hidden = self.entries(self.no_pay)[row.pk]
        self.assertEqual(hidden["reason"], "")
        self.assertEqual(hidden["after"], {})
        self.assertEqual(hidden["message"], "Changed Ada's pay terms.")

    def test_a_declined_request_reason_follows_the_decline_reason_switch(self):
        row = record(
            entity=self.books, action=FinanceAuditAction.INTER_BRANCH_DECLINED,
            target_type="InterBranchTransfer", target_id="4", branch=self.ikeja,
            message="Ikeja Branch declined Lekki Branch's request.",
            after={"decline_reason": "Diesel is already paid for"},
        )

        self.assertEqual(self.entries(self.open)[row.pk]["reason"], "Diesel is already paid for")
        hidden = self.entries(self.no_decline)[row.pk]
        self.assertEqual(hidden["reason"], "")
        self.assertNotIn("decline_reason", hidden["after"])


# --------------------------------------------------------------------------- #
# Receivables accruals                                                        #
# --------------------------------------------------------------------------- #

class AccrualAuditFigureTests(_AccrualsFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant, "fig-open")
        cls.no_amount = cls.staff(cls.tenant, "fig-no-amount", hide=[
            *_k(DI, "release_amount"), *_k(DEPOSIT, "amount"),
        ])
        cls.no_required = cls.staff(cls.tenant, "fig-no-required", hide=_k(
            PROVISION, "required_total", "required", "bands"))
        cls.solo_open = cls.staff(cls.solo_tenant, "fig-solo-open")

    def row(self, action):
        return FinanceAuditLog.objects.filter(action=action).order_by("-id").first()

    def test_a_release_names_the_month_and_keeps_the_amount_in_the_snapshot(self):
        from vs_finance.deferred_income import release_deferred_income

        self.bill(self.eze)
        release_deferred_income(self.books, up_to=D(2027, 1, 31))

        row = self.row("DEFERRED_INCOME_RELEASED")
        assert_no_figure(self, row.message, 37_500)
        self.assertIn("January 2027", row.message)
        self.assertEqual(row.after, {"amount": 37_500})
        open_row = audit_rows(self, self.open, self.books, "DEFERRED_INCOME_RELEASED")[0]
        hidden_row = audit_rows(self, self.no_amount, self.books, "DEFERRED_INCOME_RELEASED")[0]
        self.assertEqual(open_row["after"], {"amount": 37_500})
        self.assertEqual(hidden_row["after"], {})
        self.assertEqual(open_row["message"], hidden_row["message"])

    def test_undoing_a_release_names_no_amount(self):
        from vs_finance.deferred_income import release_deferred_income, reverse_deferred_release

        self.bill(self.eze)
        release_deferred_income(self.books, up_to=D(2027, 1, 31))
        reverse_deferred_release(self.books, self.period(2027, 1))

        row = self.row("DEFERRED_RELEASE_REVERSED")
        assert_no_figure(self, row.message, 37_500)
        self.assertEqual(row.before, {"amount": 37_500})
        self.assertEqual(
            audit_rows(self, self.no_amount, self.books, "DEFERRED_RELEASE_REVERSED")[0]["before"],
            {},
        )

    def test_forfeited_deposits_name_no_customer_amount(self):
        from vs_finance.customers import set_customer_active
        from vs_finance.deposits import forfeit_unclaimed_deposits
        from vs_finance.models import CustomerDeposit

        old = self.deposit_bill(self.bello)
        self.receipt(self.bello, 50_000, on=D(2026, 1, 12), invoice=old)
        set_customer_active(self.bello, False)
        CustomerDeposit.objects.filter(invoice=old).update(claim_opened_on=D(2020, 6, 30))
        forfeit_unclaimed_deposits(self.books, as_of=D(2026, 6, 30))

        row = self.row("DEPOSITS_FORFEITED")
        assert_no_figure(self, row.message, 50_000)
        self.assertEqual(row.after, {"amount": 50_000})
        self.assertEqual(
            audit_rows(self, self.no_amount, self.books, "DEPOSITS_FORFEITED")[0]["after"], {},
        )
        self.assertEqual(
            audit_rows(self, self.open, self.books, "DEPOSITS_FORFEITED")[0]["after"],
            {"amount": 50_000},
        )

    def test_a_released_deposit_names_the_customer_and_no_figure(self):
        from unittest import mock

        from vs_finance.customers import set_customer_active
        from vs_finance.deposits import release_deposits

        invoice = self.deposit_bill(self.bello)
        self.receipt(self.bello, 50_000, on=D(2026, 1, 12), invoice=invoice)
        with mock.patch("vs_finance.deposits.tenant_today", return_value=D(2026, 7, 20)):
            set_customer_active(self.bello, False)
        release_deposits(self.bello, note_date=D(2026, 7, 21))

        row = self.row("DEPOSIT_RELEASED")
        self.assertIn("BELLO", row.message)
        assert_no_figure(self, row.message, 50_000)
        self.assertEqual(row.metadata["amount"], 50_000)

    def test_a_provision_names_the_run_and_keeps_its_totals_in_the_snapshot(self):
        from vs_finance.provisions import post_provision, prepare_provision

        self.bill(self.eze, 1_000_000, on=D(2026, 1, 5), due=D(2026, 1, 10), service=None)
        provision = post_provision(prepare_provision(self.books, as_of=D(2026, 12, 31)))

        row = self.row("PROVISION_POSTED")
        self.assertIn(provision.document_number, row.message)
        assert_no_figure(self, row.message, 250_000, 1_000_000)
        self.assertEqual(row.after["required_total"], 250_000)
        self.assertTrue(row.after["bands"])
        hidden = audit_rows(self, self.no_required, self.books, "PROVISION_POSTED")[0]
        for name in ("required_total", "required", "bands"):
            self.assertNotIn(name, hidden["after"])
        self.assertIn("required_total", audit_rows(
            self, self.open, self.books, "PROVISION_POSTED")[0]["after"])

    def test_another_schools_reader_never_meets_these_entries(self):
        from vs_finance.deferred_income import release_deferred_income

        self.bill(self.eze)
        release_deferred_income(self.books, up_to=D(2027, 1, 31))

        self.assertEqual(audit_rows(
            self, self.solo_open, self.solo_books, "DEFERRED_INCOME_RELEASED"), [])


# --------------------------------------------------------------------------- #
# Bank transactions and transfers                                             #
# --------------------------------------------------------------------------- #


class BankAuditFigureTests(_BankFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        keys = [*cls.txn_keys, *cls.transfer_keys]
        cls.open = reader_role(cls.grant, cls.tenant, "open", keys)
        cls.no_values = reader_role(cls.grant, cls.tenant, "values", keys, hide=[
            *_k(TXN, "amount", "narration", "reference", "direction", "transaction_date"),
            *_k(TRF, "amount", "narration", "reference", "transfer_date"),
        ])
        cls.no_accounts = reader_role(cls.grant, cls.tenant, "accounts", keys, hide=[
            *_k(TXN, "bank_account", "counter_account"), *_k(TRF, "from_account", "to_account"),
        ])

    def test_a_posted_transaction_names_the_bank_not_the_narration_or_amount(self):
        row = FinanceAuditLog.objects.get(
            action="BANK_TRANSACTION_POSTED", target_id=str(self.okafor_posted_txn.pk),
        )
        self.assertEqual(row.message, f"Received money into {self.ikeja_bank.name}.")
        assert_no_figure(self, row.message, 7_000_000)
        self.assertNotIn("Owner capital", row.message)
        self.assertEqual(row.after["amount"], 7_000_000)
        self.assertEqual(row.after["narration"], "Owner capital, posted")

        full = audit_rows(self, self.open, self.books, "BANK_TRANSACTION_POSTED")[0]
        values = audit_rows(self, self.no_values, self.books, "BANK_TRANSACTION_POSTED")[0]
        accounts = audit_rows(self, self.no_accounts, self.books, "BANK_TRANSACTION_POSTED")[0]
        self.assertEqual(full["after"]["narration"], "Owner capital, posted")
        for name in ("amount", "narration", "reference", "direction", "transaction_date"):
            self.assertNotIn(name, values["after"])
        self.assertIn("bank_account_id", values["after"])
        self.assertNotIn("bank_account_id", accounts["after"])
        self.assertNotIn("counter_account_id", accounts["after"])
        self.assertIn("amount", accounts["after"])
        self.assertEqual(full["message"], values["message"])

    def test_a_posted_transfer_names_both_accounts_and_no_amount(self):
        row = FinanceAuditLog.objects.get(
            action="BANK_TRANSFER_POSTED", target_id=str(self.okafor_posted_transfer.pk),
        )
        self.assertIn(self.ikeja_zenith.name, row.message)
        self.assertIn(self.ikeja_bank.name, row.message)
        assert_no_figure(self, row.message, 1_000_000)
        self.assertEqual(row.after["amount"], 1_000_000)

        values = audit_rows(self, self.no_values, self.books, "BANK_TRANSFER_POSTED")[0]
        self.assertNotIn("amount", values["after"])
        self.assertNotIn("narration", values["after"])
        self.assertIn("from_account_id", values["after"])
        self.assertEqual(
            audit_rows(self, self.open, self.books, "BANK_TRANSFER_POSTED")[0]["after"]["amount"],
            1_000_000,
        )

    def test_the_central_copy_of_a_posting_carries_none_of_it(self):
        event = AuditEvent.objects.get(
            entity_type="vs_finance.BankTransaction",
            entity_id=str(self.okafor_posted_txn.pk),
        )
        self.assertNotIn("amount", event.metadata)
        self.assertNotIn("counter_account", event.metadata)
        assert_no_figure(self, event.summary, 7_000_000)


# --------------------------------------------------------------------------- #
# Petty cash returns                                                          #
# --------------------------------------------------------------------------- #


class PettyCashReturnAuditTests(_PettyCashFieldAccess):

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import PettyCashReturn
        from vs_finance.petty_cash import post_petty_cash_return

        super().setUpTestData()
        cls.the_return = PettyCashReturn.objects.create(
            entity=cls.books, branch=cls.lekki, fund=cls.lekki_fund, kind="REDUCE",
            bank_account=cls.lekki_bank, return_date=JAN_10, counted_amount=19_000 * NAIRA,
            book_balance=20_000 * NAIRA, amount=10_000 * NAIRA,
            previous_float_amount=20_000 * NAIRA, new_float_amount=10_000 * NAIRA,
            counted_by=cls.custodian, created_by=cls.custodian, narration="Cut Lekki's float",
            difference_reason="Two notes were torn",
        )
        post_petty_cash_return(cls.the_return, actor_user=cls.custodian)
        keys = cls.field_keys
        cls.open = reader_role(cls.grant, cls.tenant, "open", keys)
        cls.no_counts = reader_role(cls.grant, cls.tenant, "counts", keys, hide=_k(
            PC, "counted_amount", "book_balance", "difference", "amount", "new_float_amount"))

    def test_the_sentence_names_the_fund_and_no_count(self):
        row = FinanceAuditLog.objects.get(action="PETTY_CASH_RETURN_POSTED")
        self.assertIn(self.lekki_fund.name, row.message)
        assert_no_figure(self, row.message, 19_000 * NAIRA, 20_000 * NAIRA, 10_000 * NAIRA)
        self.assertEqual(row.after["counted_amount"], 19_000 * NAIRA)
        self.assertEqual(row.after["book_balance"], 20_000 * NAIRA)
        self.assertEqual(row.after["new_float_amount"], 10_000 * NAIRA)
        self.assertEqual(row.after["shortage"], 1_000 * NAIRA)

    def test_a_reader_without_the_count_figures_reads_none_of_them(self):
        full = audit_rows(self, self.open, self.books, "PETTY_CASH_RETURN_POSTED")[0]
        hidden = audit_rows(self, self.no_counts, self.books, "PETTY_CASH_RETURN_POSTED")[0]
        self.assertEqual(full["after"]["counted_amount"], 19_000 * NAIRA)
        for name in ("counted_amount", "book_balance", "shortage", "overage", "amount",
                     "new_float_amount"):
            self.assertNotIn(name, hidden["after"])
        self.assertIn("bank_account_id", hidden["after"])
        self.assertEqual(full["message"], hidden["message"])


# --------------------------------------------------------------------------- #
# Inter-branch transfers                                                      #
# --------------------------------------------------------------------------- #

IB = "finance.interbranch"


class InterBranchAuditTests(_InterBranchFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        keys = install_declared_fields(IB)
        grants = (*TRANSFER_KEYS, "finance.audit.view")
        cls.sender = reader_role(
            cls.grant, cls.tenant, "ib-open", keys, extra_keys=grants, email_domain="corona.test")
        cls.no_values = reader_role(
            cls.grant, cls.tenant, "ib-hidden", keys, extra_keys=grants,
            hide=_k(IB, "amount", "purpose"), email_domain="corona.test")

    def test_a_send_names_the_branches_and_keeps_amount_and_purpose_in_the_snapshot(self):
        sent = self.send(TenantAPIClient(user=self.sender), source=self.ikeja_bank,
                         to_branch=self.lekki)
        self.assertEqual(sent.status_code, 201, sent.data)

        rows = FinanceAuditLog.objects.filter(action="INTER_BRANCH_SENT")
        self.assertEqual(rows.count(), 2)
        for row in rows:
            self.assertEqual(row.message, "Ikeja Branch sent money to Lekki Branch.")
            self.assertNotIn("Diesel", row.message)
            self.assertEqual(row.after["amount"], 1_000_000_00)
            self.assertEqual(row.after["purpose"], "Diesel for the generators")

        full = audit_rows(self, self.sender, self.books, "INTER_BRANCH_SENT")
        hidden = audit_rows(self, self.no_values, self.books, "INTER_BRANCH_SENT")
        self.assertEqual(len(full), 2)
        self.assertEqual(full[0]["after"]["purpose"], "Diesel for the generators")
        for row in hidden:
            self.assertNotIn("amount", row["after"])
            self.assertNotIn("purpose", row["after"])
            self.assertEqual(row["message"], "Ikeja Branch sent money to Lekki Branch.")

    def test_each_branch_still_finds_its_own_entry(self):
        self.send(TenantAPIClient(user=self.sender), source=self.ikeja_bank,
                  to_branch=self.lekki)

        self.assertEqual(
            sorted(FinanceAuditLog.objects.filter(action="INTER_BRANCH_SENT")
                   .values_list("branch_id", flat=True)),
            sorted([self.ikeja.pk, self.lekki.pk]),
        )


# --------------------------------------------------------------------------- #
# Payments from a payer                                                       #
# --------------------------------------------------------------------------- #


class PayerPaymentAuditTests(_PayerFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        keys = cls.field_keys
        cls.open = reader_role(cls.grant, cls.tenant, "open", keys, email_domain="payer.test")
        cls.no_amounts = reader_role(
            cls.grant, cls.tenant, "amounts", keys, email_domain="payer.test",
            hide=_k(PAY, "amount", "shares", "bank_account"))

    def test_the_sentence_names_payer_branch_and_customer_count_only(self):
        document = self.pay(450_000_00)

        rows = FinanceAuditLog.objects.filter(action="PAYER_PAYMENT_POSTED")
        self.assertEqual(rows.count(), 2)
        ikeja = rows.get(branch=self.ikeja)
        self.assertIn("Mr Okafor", ikeja.message)
        self.assertIn(self.ikeja_bank.name, ikeja.message)
        assert_no_figure(self, ikeja.message, 450_000_00, 330_000_00, 120_000_00)
        self.assertEqual(ikeja.after["amount"], 330_000_00)
        self.assertEqual(ikeja.after["bank_account"], self.ikeja_bank.pk)
        self.assertEqual(
            {share["customer"] for share in ikeja.after["shares"]}, {"ADA", "EMEKA"},
        )
        self.assertNotIn("document", ikeja.after["shares"][0])
        self.assertEqual(document.shares.count(), 3)

    def test_a_reader_without_the_amounts_reads_neither_amount_nor_shares(self):
        self.pay(450_000_00)

        full = audit_rows(self, self.open, self.books, "PAYER_PAYMENT_POSTED")
        hidden = audit_rows(self, self.no_amounts, self.books, "PAYER_PAYMENT_POSTED")
        self.assertTrue(all("amount" in row["after"] for row in full))
        for row in hidden:
            for name in ("amount", "shares", "bank_account"):
                self.assertNotIn(name, row["after"])
            self.assertIn("Mr Okafor", row["message"])

    def test_a_link_between_payer_and_customer_keeps_its_names(self):
        self.assertTrue(FinanceAuditLog.objects.filter(
            action="PAYER_LINK_CHANGED", message__contains="Mr Okafor (OKAFOR) pays for Ada Okafor",
        ).exists())


# --------------------------------------------------------------------------- #
# Payroll                                                                     #
# --------------------------------------------------------------------------- #

PRUN = "finance.payrollrun"


class PayrollAuditTests(_SplitFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        keys = install_declared_fields(PRUN)
        cls.open = reader_role(cls.grant, cls.tenant, "pay-open", keys,
                               email_domain="corona.test")
        cls.no_pay = reader_role(
            cls.grant, cls.tenant, "pay-hidden", keys, email_domain="corona.test",
            hide=_k(PRUN, "gross_amount", "paye_amount", "pension_amount", "net_amount",
                    "components"))

    def test_a_run_names_the_branch_and_keeps_its_totals_in_the_snapshot(self):
        run = self.posted_run()
        self.act(run, "pay", {"bank_accounts": [self.ikeja_bank.pk, self.lekki_bank.pk]})

        accrued = FinanceAuditLog.objects.filter(action="PAYROLL_POSTED")
        self.assertEqual(
            sorted(row.message for row in accrued),
            ["Accrued Ikeja Branch's payroll.", "Accrued Lekki Branch's payroll."],
        )
        ikeja = accrued.get(branch=self.ikeja)
        self.assertEqual(ikeja.after["gross_amount"], 50_000)
        self.assertEqual(ikeja.after["net_amount"], 45_000)
        paid = FinanceAuditLog.objects.filter(action="PAYROLL_PAID").get(branch=self.ikeja)
        assert_no_figure(self, paid.message, 45_000)
        self.assertIn(self.ikeja_bank.name, paid.message)
        self.assertEqual(paid.after, {"net_amount": 45_000})

    def test_a_reader_without_the_pay_figures_reads_none_of_them(self):
        run = self.posted_run()
        self.act(run, "pay", {"bank_accounts": [self.ikeja_bank.pk, self.lekki_bank.pk]})

        for action in ("PAYROLL_POSTED", "PAYROLL_PAID"):
            full = audit_rows(self, self.open, self.books, action)
            hidden = audit_rows(self, self.no_pay, self.books, action)
            self.assertEqual(len(full), len(hidden))
            for row in full:
                self.assertIn("net_amount", row["after"])
            for row in hidden:
                self.assertEqual(row["after"], {})
                self.assertTrue(row["message"])


# --------------------------------------------------------------------------- #
# Closing a fiscal year                                                       #
# --------------------------------------------------------------------------- #


class YearCloseAuditTests(_CloseFixture):
    CLOSE_KEYS = (*_CloseFixture.CLOSE_KEYS, "finance.audit.view")

    def close(self, user, **body):
        FiscalPeriod.objects.filter(
            fiscal_year=self.year(self.books), is_closing=False,
        ).update(status="CLOSED")
        return TenantAPIClient(user=user).post(
            f"/v1/finance/fiscal-years/{self.year(self.books).pk}/close/"
            f"?entity={self.books.code}", body, format="json",
        )

    def test_the_sentence_carries_no_net_result_and_the_snapshot_does(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        closed = self.close(self.lagoon_staff("closer"), branch=self.ikeja.pk)
        self.assertEqual(closed.status_code, 200, closed.data)

        row = FinanceAuditLog.objects.get(action="FISCAL_YEAR_CLOSED")
        self.assertNotIn("profit", row.message)
        self.assertNotIn("loss", row.message)
        assert_no_figure(self, row.message)
        self.assertEqual(row.after, {"net_income": closed.data["data"]["net_income"]["kobo"]})

        full = audit_rows(self, self.lagoon_staff("reader"), self.books, "FISCAL_YEAR_CLOSED")
        hidden = audit_rows(
            self, self.lagoon_staff("blind", "net_income", resource="finance.fiscalyear"),
            self.books, "FISCAL_YEAR_CLOSED",
        )
        self.assertIn("net_income", full[0]["after"])
        self.assertEqual(hidden[0]["after"], {})
        self.assertEqual(full[0]["message"], hidden[0]["message"])
