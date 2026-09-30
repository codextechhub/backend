"""The finance audit trail, read branch by branch.

Corona pays Ada at Ikeja (50,000 gross, 45,000 net), Bola at Lekki (80,000
gross, 72,000 net) and Chidi at Yaba (60,000 gross, 54,000 net) in one central
January run, booked one journal per branch. Ngozi keeps Lekki's books, pinned to
Lekki, and holds the audit-view key. They read the trail the way they read every
other finance screen: Lekki's entries and nothing else. The run's accrual is
recorded once per branch share, so they read "Accrued Lekki Branch's payroll:
gross 80000, net 72000" and never the school's 190,000 or anybody else's pay.
An entry written before entries carried a branch is shown to whole-school
readers only, because nothing on it says whose it is.

Mr Bello, the whole-school bursar, reads everything, the per-branch entries of
the run included: the school's total is the sum of its shares. A reader at a
rival school reads none of it.
"""
from __future__ import annotations

import datetime
import io

from django.core.management import call_command
from django.db import DatabaseError, transaction

from vs_finance.audit import record
from vs_finance.constants import FinanceAuditAction
from vs_finance.models import Account, FinanceAuditLog, JournalEntry, JournalLine, PayrollRun

from .tests_payroll_split import _SplitFixture

JAN_10 = datetime.date(2026, 1, 10)
JAN_31 = datetime.date(2026, 1, 31)

#: Everything of Ikeja's and Yaba's pay, and every whole-school total, as it would print.
NOT_LEKKIS = (
    "Ada Obi", "Chidi Eze", "Ikeja", "Yaba", "50000", "45000", "60000", "54000",
    "190000", "171000", "130000", "117000",
)


class _AuditFixture(_SplitFixture):
    """Corona's three branches, their central run, and a trail to read it through."""

    KEYS = _SplitFixture.KEYS + ("finance.audit.view",)

    def setUp(self):
        super().setUp()
        self.chidi = self.salary(self.books, "Chidi Eze", self.yaba, gross=60_000, paye=4_000, pension=2_000)
        self.yaba_bank = self.bank(self.books, "Yaba Operations", self.yaba, "73")
        self.ngozi = self.client_for(self.tenant, "audit-lekki@corona.test", branch=self.lekki)

    # -- rows ----------------------------------------------------------------- #

    def entry_about(self, document, action=FinanceAuditAction.INVOICE_POSTED, *, actor=None,
                    message=""):
        return record(
            entity=document.entity, action=action, actor_user=actor, target=document,
            message=message or f"{action} on {type(document).__name__} {document.pk}",
        )

    def legacy_entry(self, message="Accrued payroll: gross 190000, net 171000 kobo."):
        """An entry as written before entries carried a branch."""
        return FinanceAuditLog.objects.create(
            entity=self.books, action=FinanceAuditAction.PAYROLL_POSTED,
            target_type="PayrollRun", target_id="999999", message=message,
        )

    def paid_run(self):
        """January's run, posted and paid from Ikeja's and Lekki's accounts; Yaba still owed."""
        run = self.posted_run()
        paid = self.act(run, "pay", {"bank_accounts": [self.ikeja_bank.pk, self.lekki_bank.pk]})
        self.assertEqual(paid.status_code, 200, paid.data)
        return run

    # -- reading -------------------------------------------------------------- #

    def trail(self, client, books=None, **params):
        books = books or self.books
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return client.get(
            f"/v1/finance/audit-logs/?entity={books.code}&page_size=100"
            + (f"&{query}" if query else ""),
        )

    def rows(self, client, books=None, **params):
        response = self.trail(client, books, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def ids(self, client, books=None, **params):
        return {row["id"] for row in self.rows(client, books, **params)}

    def count(self, client, **params):
        response = self.trail(client, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["pagination"]["totalItems"]

    def facets(self, client):
        response = client.get(f"/v1/finance/audit-logs/facets/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def one(self, client, entry, books=None):
        books = books or self.books
        return client.get(f"/v1/finance/audit-logs/{entry.pk}/?entity={books.code}")

    def assert_nothing_of_theirs(self, payload):
        text = str(payload)
        for leaked in NOT_LEKKIS:
            self.assertNotIn(leaked, text)


class ALekkiReaderReadsLekkiTests(_AuditFixture):
    """Ngozi's trail is Lekki's, and only Lekki's."""

    def test_they_see_lekkis_entries_and_none_of_ikejas(self):
        lekki_entry = self.entry_about(self.invoice(self.books, self.customer(self.books, "L1", self.lekki), self.lekki))
        ikeja_entry = self.entry_about(self.invoice(self.books, self.customer(self.books, "I1", self.ikeja), self.ikeja))

        seen = self.ids(self.ngozi)

        self.assertIn(lekki_entry.pk, seen)
        self.assertNotIn(ikeja_entry.pk, seen)

    def test_they_see_their_branchs_disbursement_and_not_ikejas(self):
        self.paid_run()

        paid = self.rows(self.ngozi, action=FinanceAuditAction.PAYROLL_PAID)

        self.assertEqual(len(paid), 1, paid)
        self.assertIn("Lekki Branch", paid[0]["message"])
        self.assertIn("72000", paid[0]["message"])
        self.assert_nothing_of_theirs(self.rows(self.ngozi))

    def test_they_never_see_the_whole_schools_salary_totals(self):
        self.posted_run()

        accrued = self.rows(self.ngozi, action=FinanceAuditAction.PAYROLL_POSTED)

        self.assertEqual([row["message"] for row in accrued],
                         ["Accrued Lekki Branch's payroll: gross 80000, net 72000 kobo."])
        self.assert_nothing_of_theirs(self.rows(self.ngozi))

    def test_an_entry_written_before_entries_had_a_branch_is_not_theirs(self):
        old = self.legacy_entry()

        self.assertNotIn(old.pk, self.ids(self.ngozi))
        self.assertEqual(self.count(self.ngozi), 0)

    def test_another_branchs_entry_by_id_is_not_found(self):
        lekki_entry = self.entry_about(self.invoice(self.books, self.customer(self.books, "L1", self.lekki), self.lekki))
        ikeja_entry = self.entry_about(self.invoice(self.books, self.customer(self.books, "I1", self.ikeja), self.ikeja))
        old = self.legacy_entry()

        self.assertEqual(self.one(self.ngozi, lekki_entry).status_code, 200)
        self.assertEqual(self.one(self.ngozi, ikeja_entry).status_code, 404)
        self.assertEqual(self.one(self.ngozi, old).status_code, 404)

    def test_filters_and_counts_say_nothing_of_other_branches(self):
        """An Ikeja-only clerk, action and document type do not surface in any form."""
        clerk = self.user_for(self.tenant, "ikeja-clerk@corona.test")
        ikeja_invoice = self.invoice(self.books, self.customer(self.books, "I1", self.ikeja), self.ikeja)
        self.entry_about(ikeja_invoice, FinanceAuditAction.INVOICE_WRITTEN_OFF, actor=clerk)
        self.entry_about(self.ikeja_bank, FinanceAuditAction.BANK_RECONCILED, actor=clerk)
        self.entry_about(self.invoice(self.books, self.customer(self.books, "L1", self.lekki), self.lekki))
        self.legacy_entry()

        self.assertEqual(self.count(self.ngozi), 1)
        self.assertEqual(self.count(self.ngozi, action=FinanceAuditAction.INVOICE_WRITTEN_OFF), 0)
        self.assertEqual(self.count(self.ngozi, action=FinanceAuditAction.PAYROLL_POSTED), 0)
        self.assertEqual(self.count(self.ngozi, target_type="BankAccount"), 0)
        self.assertEqual(self.count(self.ngozi, actor=clerk.pk), 0)

        facets = self.facets(self.ngozi)
        self.assertNotIn("ikeja-clerk@corona.test", str(facets["actors"]))
        self.assertNotIn(FinanceAuditAction.INVOICE_WRITTEN_OFF, [a["value"] for a in facets["actions"]])
        self.assertNotIn(FinanceAuditAction.PAYROLL_POSTED, [a["value"] for a in facets["actions"]])
        self.assertEqual(facets["target_types"], ["Invoice"])


class AWholeSchoolReaderReadsEverythingTests(_AuditFixture):
    """Mr Bello's trail is the school's, per-branch entries and old ones alike."""

    def test_they_see_every_branchs_entries_and_the_old_ones(self):
        self.paid_run()
        old = self.legacy_entry()

        rows = self.rows(self.bello)

        self.assertIn(old.pk, {row["id"] for row in rows})
        accrued = [row for row in rows if row["action"] == FinanceAuditAction.PAYROLL_POSTED
                   and row["id"] != old.pk]
        self.assertEqual({row["branch_name"] for row in accrued},
                         {"Ikeja Branch", "Lekki Branch", "Yaba Branch"})
        paid = [row for row in rows if row["action"] == FinanceAuditAction.PAYROLL_PAID]
        self.assertEqual({row["branch_name"] for row in paid}, {"Ikeja Branch", "Lekki Branch"})

    def test_the_schools_total_is_the_sum_of_its_shares(self):
        run = self.posted_run()

        shares = FinanceAuditLog.objects.filter(
            entity=self.books, action=FinanceAuditAction.PAYROLL_POSTED,
            target_type="PayrollRun", target_id=str(run.pk),
        )

        self.assertEqual(sum(e.metadata["gross"] for e in shares), 190_000)
        self.assertEqual(sum(e.metadata["net"] for e in shares), 171_000)

    def test_they_open_any_branchs_entry_by_id(self):
        ikeja_entry = self.entry_about(self.invoice(self.books, self.customer(self.books, "I1", self.ikeja), self.ikeja))
        old = self.legacy_entry()

        for entry in (ikeja_entry, old):
            with self.subTest(entry=entry.pk):
                response = self.one(self.bello, entry)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["data"]["id"], entry.pk)


class AnotherTenantReadsNothingTests(_AuditFixture):
    """The rival school's reader is confined to the rival school's books."""

    def setUp(self):
        super().setUp()
        self.rival = self.client_for(self.rival_tenant, "audit-rival@rival.test")
        rival_invoice = self.invoice(
            self.rival_books, self.customer(self.rival_books, "R1", self.rival_branch), self.rival_branch,
        )
        self.rival_entry = self.entry_about(rival_invoice)
        self.corona_entry = self.entry_about(
            self.invoice(self.books, self.customer(self.books, "L1", self.lekki), self.lekki),
        )

    def test_their_trail_is_their_own_books(self):
        self.assertEqual(self.ids(self.rival, self.rival_books), {self.rival_entry.pk})

    def test_naming_corona_books_or_an_entry_reaches_nothing(self):
        self.assertIn(self.trail(self.rival, self.books).status_code, (403, 404))
        self.assertEqual(self.one(self.rival, self.corona_entry, self.rival_books).status_code, 404)
        self.assertIn(self.one(self.rival, self.corona_entry).status_code, (403, 404))


class EachEntryNamesItsDocumentsBranchTests(_AuditFixture):
    """The branch comes from the document the entry is about, never from who acted."""

    def test_an_entry_takes_the_branch_of_its_document_not_of_the_actor(self):
        lekki_person = self.user_for(self.tenant, "lekki-hand@corona.test")
        ikeja_invoice = self.invoice(self.books, self.customer(self.books, "I1", self.ikeja), self.ikeja)

        entry = self.entry_about(ikeja_invoice, actor=lekki_person)

        self.assertEqual(entry.branch_id, self.ikeja.pk)

    def test_an_entry_about_something_with_no_branch_has_none(self):
        entry = record(
            entity=self.books, action=FinanceAuditAction.FINANCE_SETTINGS_UPDATED,
            target_type="FinanceAccountSettings", target_id=str(self.books.pk),
        )

        self.assertIsNone(entry.branch_id)

    def test_a_central_run_is_accrued_once_per_branch_share(self):
        run = self.posted_run()

        accrued = {
            e.branch_id: e for e in FinanceAuditLog.objects.filter(
                action=FinanceAuditAction.PAYROLL_POSTED, target_id=str(run.pk))
        }

        self.assertEqual(set(accrued), {self.ikeja.pk, self.lekki.pk, self.yaba.pk})
        lekki = accrued[self.lekki.pk]
        self.assertEqual((lekki.metadata["gross"], lekki.metadata["net"]), (80_000, 72_000))
        self.assertEqual(lekki.metadata["journal_id"],
                         run.branch_shares.get(branch=self.lekki).journal_id)
        self.assertNotIn("journal_ids", lekki.metadata)

    def test_a_central_run_booked_to_one_branch_is_that_branchs(self):
        self.ada.delete()
        self.chidi.delete()
        run = self.posted_run()
        self.assertIsNone(run.branch_id)

        (accrued,) = FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.PAYROLL_POSTED, target_id=str(run.pk))

        self.assertEqual(accrued.branch_id, self.lekki.pk)

    def test_voiding_a_central_run_is_recorded_per_branch_share(self):
        run = self.posted_run()
        cancelled = self.act(run, "cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.data)

        voided = FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.PAYROLL_CANCELLED, target_id=str(run.pk))

        self.assertEqual({e.branch_id for e in voided}, {self.ikeja.pk, self.lekki.pk, self.yaba.pk})
        lekki = voided.get(branch=self.lekki)
        self.assertEqual(lekki.metadata["journal_id"],
                         run.branch_shares.get(branch=self.lekki).journal_id)

    def test_a_refused_post_of_a_central_run_is_whole_school(self):
        run = PayrollRun.objects.get(pk=self.generate().data["data"]["id"])
        run.lines.all().delete()

        self.act(run, "post")

        (refused,) = FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.PAYROLL_POST_REJECTED, target_id=str(run.pk))
        self.assertIsNone(refused.branch_id)


class ADepreciationRunTests(_AuditFixture):
    """A run over every branch's assets is recorded once per branch it posted to."""

    def test_each_branch_reads_its_own_part_of_the_run(self):
        from vs_finance.assets import acquire_asset, run_period_depreciation
        from vs_finance.models import FiscalPeriod, FiscalYear, FixedAsset

        FiscalPeriod.objects.create(
            entity=self.books, fiscal_year=FiscalYear.objects.get(entity=self.books), period_no=2,
            name="Feb 2026", start_date=datetime.date(2026, 2, 1), end_date=datetime.date(2026, 2, 28),
        )
        for branch, bank, cost in ((self.ikeja, self.ikeja_bank, 1_100_000),
                                   (self.lekki, self.lekki_bank, 2_200_000)):
            asset = FixedAsset.objects.create(
                entity=self.books, branch=branch, name=f"Rack {branch.name}",
                acquisition_date=datetime.date(2026, 1, 1), cost=cost, salvage_value=0,
                useful_life_months=11,
            )
            acquire_asset(asset, bank_account=bank)

        run_period_depreciation(self.books, up_to_date=datetime.date(2026, 2, 28))

        runs = {
            e.branch_id: e for e in FinanceAuditLog.objects.filter(
                action=FinanceAuditAction.DEPRECIATION_POSTED, target_type="LedgerEntity")
        }
        self.assertEqual(set(runs), {self.ikeja.pk, self.lekki.pk})
        self.assertEqual(runs[self.lekki.pk].metadata["total"], 200_000)
        self.assertEqual(runs[self.ikeja.pk].metadata["total"], 100_000)
        lekki_rows = self.rows(self.ngozi, action=FinanceAuditAction.DEPRECIATION_POSTED)
        self.assertEqual([row["id"] for row in lekki_rows], [runs[self.lekki.pk].pk])
        self.assertNotIn("300000", str(lekki_rows))


class TaxReturnSharesTests(_AuditFixture):
    """The tenant's one VAT return is recorded share by share, as it is booked."""

    def post(self, branch, date, pairs, source):
        from vs_finance.posting import post_journal, resolve_period

        entry = JournalEntry.objects.create(
            entity=self.books, branch=branch, date=date,
            period=resolve_period(self.books, date), narration="test", source=source,
        )
        for line_no, (code, debit, credit) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=self.books, code=code),
                debit=debit, credit=credit, line_no=line_no,
            )
        post_journal(entry)

    def january_vat(self):
        """Ikeja owes 60,000 of output VAT; Lekki 40,000 less 10,000 of input."""
        from vs_finance.models import TaxObligation
        from vs_finance.tax_filing import prepare_filing

        self.post(self.ikeja, JAN_10, [("1100", 460_000, 0), ("4100", 0, 400_000), ("2200", 0, 60_000)], "SALES")
        self.post(self.lekki, JAN_10, [("1100", 306_667, 0), ("4100", 0, 266_667), ("2200", 0, 40_000)], "SALES")
        self.post(self.lekki, JAN_10, [("5300", 133_333, 0), ("1300", 10_000, 0), ("1100", 0, 143_333)],
                  "PURCHASE")
        return prepare_filing(
            TaxObligation.objects.get(entity=self.books, code="VAT"),
            period_start=datetime.date(2026, 1, 1), period_end=JAN_31,
        )

    def entries(self, filing, action):
        return {
            e.branch_id: e for e in FinanceAuditLog.objects.filter(
                action=action, target_type="TaxFiling", target_id=str(filing.pk))
        }

    def test_each_step_of_the_return_is_recorded_per_share(self):
        from vs_finance.tax_filing import file_filing, pay_filing

        filing = self.january_vat()
        file_filing(filing, filed_date=JAN_31)
        pay_filing(filing, bank_account=self.lekki_bank, pay_date=JAN_31)

        prepared = self.entries(filing, FinanceAuditAction.TAX_FILING_PREPARED)
        filed = self.entries(filing, FinanceAuditAction.TAX_FILING_FILED)
        paid = self.entries(filing, FinanceAuditAction.TAX_FILING_PAID)
        self.assertEqual(set(prepared), {self.ikeja.pk, self.lekki.pk})
        self.assertEqual(set(filed), {self.ikeja.pk, self.lekki.pk})
        self.assertEqual(set(paid), {self.lekki.pk})
        self.assertEqual(prepared[self.lekki.pk].metadata["total"], 30_000)
        self.assertEqual(sum(e.metadata["total"] for e in prepared.values()), 90_000)
        self.assertEqual(paid[self.lekki.pk].metadata["amount"], 30_000)

    def test_the_lekki_reader_reads_lekkis_share_of_the_return_only(self):
        from vs_finance.tax_filing import file_filing, pay_filing

        filing = self.january_vat()
        file_filing(filing, filed_date=JAN_31)
        pay_filing(filing, bank_account=self.ikeja_bank, pay_date=JAN_31)

        rows = [row for row in self.rows(self.ngozi) if row["target_type"] == "TaxFiling"]

        self.assertEqual(sorted(row["action"] for row in rows),
                         sorted([FinanceAuditAction.TAX_FILING_FILED, FinanceAuditAction.TAX_FILING_PREPARED]))
        text = str(rows)
        for leaked in ("60000", "90000", "Ikeja"):
            self.assertNotIn(leaked, text)

    def test_reversing_a_remittance_is_the_remittances_branchs(self):
        from vs_finance.tax_filing import file_filing, pay_filing, reverse_remittance

        filing = self.january_vat()
        file_filing(filing, filed_date=JAN_31)
        pay_filing(filing, bank_account=self.lekki_bank, pay_date=JAN_31)

        reverse_remittance(filing.remittances.get(), reason="The transfer bounced.")

        self.assertEqual(set(self.entries(filing, FinanceAuditAction.TAX_REMITTANCE_REVERSED)),
                         {self.lekki.pk})

    def test_unfiling_is_recorded_per_share(self):
        from vs_finance.tax_filing import file_filing, unfile_filing

        filing = self.january_vat()
        file_filing(filing, filed_date=JAN_31)

        unfile_filing(filing)

        self.assertEqual(set(self.entries(filing, FinanceAuditAction.TAX_FILING_UNFILED)),
                         {self.ikeja.pk, self.lekki.pk})


class OldEntriesAreBackfilledTests(_AuditFixture):
    """The branch backfill gives an old entry its document's branch, and nothing else."""

    def old_entry_about(self, target_type, target_id, action=FinanceAuditAction.INVOICE_POSTED, **metadata):
        return FinanceAuditLog.objects.create(
            entity=self.books, action=action, target_type=target_type,
            target_id=str(target_id), message="old", metadata=metadata,
        )

    def backfill(self):
        call_command("branch_backfill", "--tenant", self.tenant.slug, "--apply", stdout=io.StringIO())

    def test_an_old_entry_takes_its_documents_branch(self):
        invoice = self.invoice(self.books, self.customer(self.books, "L1", self.lekki), self.lekki)
        old = self.old_entry_about("Invoice", invoice.pk)

        self.backfill()

        old.refresh_from_db()
        self.assertEqual(old.branch_id, self.lekki.pk)
        self.assertEqual((old.message, old.target_id), ("old", str(invoice.pk)))
        self.assertIn(old.pk, self.ids(self.ngozi))

    def test_an_old_bank_statement_entry_takes_its_accounts_branch(self):
        old = self.old_entry_about(
            "BankStatementLine", 123, FinanceAuditAction.BANK_STATEMENT_CORRECTED,
            bank_account_id=self.lekki_bank.pk,
        )

        self.backfill()

        old.refresh_from_db()
        self.assertEqual(old.branch_id, self.lekki.pk)

    def test_an_old_platform_copy_takes_its_documents_branch(self):
        """The platform trail's copies follow the finance entries they copy."""
        from vs_audit.services import emit_audit_event

        invoice = self.invoice(self.books, self.customer(self.books, "L1", self.lekki), self.lekki)
        central = self.posted_run()

        def copy(entity_type, entity_id, module_key="FINANCE"):
            return emit_audit_event(
                module_key=module_key, action_type="FINANCIAL_TRANSACTION", entity_type=entity_type,
                entity_id=str(entity_id), tenant=self.tenant, summary="old",
            )

        about_invoice = copy("vs_finance.Invoice", invoice.pk)
        about_run = copy("vs_finance.PayrollRun", central.pk)
        about_statement = copy("vs_finance.BankStatementLine", 5)
        sign_in = copy("User", 1, module_key="IDENTITY")

        self.backfill()

        for event in (about_invoice, about_run, about_statement, sign_in):
            event.refresh_from_db()
        self.assertEqual(about_invoice.branch_id, self.lekki.pk)
        self.assertIsNone(about_run.branch_id)
        self.assertIsNone(about_statement.branch_id)
        self.assertIsNone(sign_in.branch_id)

    def test_an_old_emailed_invoice_entry_takes_the_invoices_branch(self):
        from vs_finance.constants import (
            FinanceDeliveryDocument, FinanceDeliverySource, FinanceDeliveryStatus,
        )
        from vs_finance.document_email import _audit
        from vs_finance.models import FinanceDocumentDelivery

        customer = self.customer(self.books, "L1", self.lekki)
        invoice = self.invoice(self.books, customer, self.lekki)
        delivery = FinanceDocumentDelivery.objects.create(
            entity=self.books, customer=customer, document_type=FinanceDeliveryDocument.INVOICE,
            document_id=str(invoice.pk), source=FinanceDeliverySource.MANUAL,
            status=FinanceDeliveryStatus.SENT,
        )
        fresh = _audit(delivery, FinanceAuditAction.DOCUMENT_EMAIL_SENT, "Sent.")
        old = self.old_entry_about("InvoiceDelivery", delivery.pk, FinanceAuditAction.DOCUMENT_EMAIL_SENT)

        self.backfill()

        old.refresh_from_db()
        self.assertEqual((fresh.branch_id, old.branch_id), (self.lekki.pk, self.lekki.pk))

    def test_entries_about_whole_school_documents_stay_unbranched_and_unflagged(self):
        from vs_finance.branch_derivation import plan_entity

        central = self.posted_run()
        about_run = self.old_entry_about("PayrollRun", central.pk, FinanceAuditAction.PAYROLL_POSTED)
        about_settings = self.old_entry_about(
            "FinanceAccountSettings", self.books.pk, FinanceAuditAction.FINANCE_SETTINGS_UPDATED,
        )

        plan = plan_entity(self.books)
        (audit_plan,) = [p for p in plan.targets if p.target.model_label == "vs_finance.FinanceAuditLog"]

        self.assertNotIn(about_run.pk, audit_plan.assign)
        self.assertNotIn(about_settings.pk, audit_plan.assign)
        self.assertEqual(audit_plan.flags, [])
        self.backfill()
        for row in (about_run, about_settings):
            row.refresh_from_db()
            self.assertIsNone(row.branch_id)

    def test_an_old_entry_about_an_unbranched_document_is_flagged(self):
        invoice = self.invoice(self.books, self.customer(self.books, "N1", None), None)
        old = self.old_entry_about("Invoice", invoice.pk)

        from vs_finance.branch_derivation import plan_entity

        plan = plan_entity(self.books)
        (audit_plan,) = [p for p in plan.targets if p.target.model_label == "vs_finance.FinanceAuditLog"]
        self.assertEqual([f.pk for f in audit_plan.flags], [old.pk])

    def test_the_table_still_refuses_every_other_change(self):
        """Only a blank branch may be filled; nothing else on an entry moves."""
        entry = self.entry_about(self.invoice(self.books, self.customer(self.books, "L1", self.lekki), self.lekki))
        old = self.legacy_entry()

        for label, rows, change in (
            ("message", FinanceAuditLog.objects.filter(pk=entry.pk), {"message": "rewritten"}),
            ("branch moved", FinanceAuditLog.objects.filter(pk=entry.pk), {"branch": self.ikeja}),
            ("branch cleared", FinanceAuditLog.objects.filter(pk=entry.pk), {"branch": None}),
            ("fill and rewrite", FinanceAuditLog.objects.filter(pk=old.pk),
             {"branch": self.lekki, "message": "rewritten"}),
        ):
            with self.subTest(label), self.assertRaises(DatabaseError), transaction.atomic():
                rows.update(**change)
        with self.assertRaises(DatabaseError), transaction.atomic():
            FinanceAuditLog.objects.filter(pk=old.pk).delete()


class WriteOffListReachTests(_AuditFixture):
    """A branch reader's own write-offs are not pushed out by other branches' newer ones."""

    def test_an_older_lekki_write_off_survives_newer_ikeja_ones(self):
        from vs_finance.constants import FinanceAuditStatus
        from vs_finance.views_ar import _writeoff_rows
        from vs_rbac.scoping import BranchScope

        lekki_invoice = self.invoice(self.books, self.customer(self.books, "L1", self.lekki), self.lekki)
        ikeja_customer = self.customer(self.books, "I1", self.ikeja)
        lekki_entry = self.entry_about(lekki_invoice, FinanceAuditAction.INVOICE_WRITTEN_OFF)
        for _ in range(3):
            self.entry_about(self.invoice(self.books, ikeja_customer, self.ikeja),
                             FinanceAuditAction.INVOICE_WRITTEN_OFF)
        self.assertEqual(FinanceAuditLog.objects.filter(status=FinanceAuditStatus.SUCCESS).count(), 4)

        rows = _writeoff_rows(
            self.books, limit=2, scope=BranchScope(frozenset({self.lekki.pk}), include_shared=False),
        )

        self.assertEqual([row["key"] for row in rows], [f"W{lekki_entry.pk}"])
