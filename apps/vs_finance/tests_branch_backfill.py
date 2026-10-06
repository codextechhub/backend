"""The branch backfill: every derivation, both shapes of school, and the write rules.

Built on :class:`_FinanceBranchFixture`: a three-branch school (Ikeja is its
main branch, then Lekki and Yaba), a one-branch school and a rival tenant. Rows
are created and then stripped of their branch with a queryset update, the state
a pre-rule row is in, so no save-time default can put a branch back.

The main branch is Ikeja throughout, so every multi-branch test that expects a
Lekki or Yaba answer, or a blank one, also proves Ikeja was not guessed.
"""
from __future__ import annotations

import datetime
import io

from django.contrib.auth import get_user_model
from django.core.management import call_command

from vs_audit.models import AuditEvent
from vs_finance.branch_derivation import (
    ONLY_BRANCH,
    apply_plan,
    journal_owners,
    plan_entity,
    targets,
)
from vs_finance.models import (
    Account,
    BankAccount,
    Budget,
    Concession,
    CreditNote,
    DepreciationSchedule,
    DunningNotice,
    ExpenseClaim,
    FiscalYear,
    JournalEntry,
    JournalLine,
    Payment,
    PaymentAllocation,
    PaymentPlan,
    PettyCashFund,
    PettyCashVoucher,
    Refund,
    RefundAllocation,
    WriteOffRequest,
)
from vs_finance.tests_branch_scope import _FinanceBranchFixture

DAY = datetime.date(2026, 1, 12)


def blank(row):
    """Strip ``row``'s branch the way a row written before the rule has none."""
    type(row)._base_manager.filter(pk=row.pk).update(branch=None)
    row.refresh_from_db()
    return row


class _BackfillFixture(_FinanceBranchFixture):
    """Row builders on top of the branch fixture."""

    _tag = 40

    def account(self, entity, code):
        return Account.objects.get(entity=entity, code=code)

    def bank(self, entity, branch):
        _BackfillFixture._tag += 1
        name = f"Bank {_BackfillFixture._tag}"
        gl = Account.objects.create(
            entity=entity, code=f"11{_BackfillFixture._tag}", name=f"Cash {_BackfillFixture._tag}",
            account_type=self.account(entity, "1100").account_type, is_postable=True,
        )
        return BankAccount.objects.create(entity=entity, name=name, branch=branch, gl_account=gl)

    def payment(self, entity, customer, branch=None, deposit=None):
        return blank(Payment.objects.create(
            entity=entity, customer=customer, branch=branch, payment_date=DAY,
            deposit_account=deposit,
        ))

    def journal(self, entity, *, reverses=None, lines=()):
        entry = JournalEntry.objects.create(entity=entity, date=DAY, reverses=reverses)
        for no, account in enumerate(lines, start=1):
            JournalLine.objects.create(entry=entry, account=account, debit=100, credit=0, line_no=no)
        return blank(entry)

    def person(self, email, branch):
        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=self.tenant, branch=branch,
            status="ACTIVE", first_name="Ada", last_name="Obi",
        )

    def plan(self, entity=None):
        return plan_entity(entity or self.books)

    def assigned(self, plan, row):
        label = f"{type(row)._meta.app_label}.{type(row).__name__}"
        for target_plan in plan.targets:
            if target_plan.target.model_label == label:
                return target_plan.assign.get(row.pk)
        raise AssertionError(f"{label} is not a backfill target")

    def flag(self, plan, row):
        label = f"{type(row)._meta.app_label}.{type(row).__name__}"
        for target_plan in plan.targets:
            if target_plan.target.model_label == label:
                return next((f for f in target_plan.flags if f.pk == row.pk), None)
        raise AssertionError(f"{label} is not a backfill target")


class ReceivablesDerivationTests(_BackfillFixture):
    """Invoices, payments, adjustments and refunds."""

    def test_an_invoice_takes_its_customers_branch(self):
        invoice = blank(self.invoice(self.books, self.customer(self.books, "C1", self.lekki), self.lekki))
        self.assertEqual(self.assigned(self.plan(), invoice), (self.lekki.pk, "the customer"))

    def test_an_invoice_whose_customer_has_no_branch_is_flagged_not_filed_under_main(self):
        invoice = blank(self.invoice(self.books, self.customer(self.books, "C1", None), None))
        plan = self.plan()
        self.assertIsNone(self.assigned(plan, invoice))
        self.assertEqual(self.flag(plan, invoice).reason, "no branch on the customer")

    def test_a_branch_of_another_tenant_is_no_answer(self):
        customer = self.customer(self.books, "C1", None)
        type(customer)._base_manager.filter(pk=customer.pk).update(branch=self.rival_branch)
        invoice = blank(self.invoice(self.books, customer, None))
        self.assertIsNone(self.assigned(self.plan(), invoice))

    def test_a_payment_takes_its_allocated_invoices_branch_when_they_agree(self):
        customer = self.customer(self.books, "C1", self.yaba)
        payment = self.payment(self.books, customer)
        for _ in range(2):
            PaymentAllocation.objects.create(payment=payment, invoice=self.invoice(self.books, customer, self.lekki), amount=1)
        self.assertEqual(self.assigned(self.plan(), payment), (self.lekki.pk, "the allocated invoices"))

    def test_a_payment_whose_invoices_disagree_falls_through_to_the_bank_then_the_customer(self):
        customer = self.customer(self.books, "C1", self.yaba)
        by_customer = self.payment(self.books, customer)
        by_bank = self.payment(self.books, customer, deposit=self.bank(self.books, self.lekki).gl_account)
        for payment in (by_customer, by_bank):
            PaymentAllocation.objects.create(payment=payment, invoice=self.invoice(self.books, customer, self.ikeja), amount=1)
            PaymentAllocation.objects.create(payment=payment, invoice=self.invoice(self.books, customer, self.lekki), amount=1)
        plan = self.plan()
        self.assertEqual(self.assigned(plan, by_bank), (self.lekki.pk, "the deposit bank account"))
        self.assertEqual(self.assigned(plan, by_customer), (self.yaba.pk, "the customer"))

    def test_a_payment_with_nothing_else_names_the_disagreement(self):
        customer = self.customer(self.books, "C1", None)
        payment = self.payment(self.books, customer)
        PaymentAllocation.objects.create(payment=payment, invoice=self.invoice(self.books, customer, self.ikeja), amount=1)
        PaymentAllocation.objects.create(payment=payment, invoice=self.invoice(self.books, customer, self.lekki), amount=1)
        flag = self.flag(self.plan(), payment)
        self.assertEqual(flag.reason, "the allocated invoices disagree: Ikeja Branch, Lekki Branch")

    def test_a_payment_reads_a_branch_its_invoice_is_only_planned_to_get(self):
        customer = self.customer(self.books, "C1", self.lekki)
        invoice = blank(self.invoice(self.books, customer, None))
        payment = self.payment(self.books, customer)
        PaymentAllocation.objects.create(payment=payment, invoice=invoice, amount=1)
        self.assertEqual(self.assigned(self.plan(), payment), (self.lekki.pk, "the allocated invoices"))

    def test_invoice_adjustments_take_the_invoices_branch(self):
        customer = self.customer(self.books, "C1", self.yaba)
        invoice = self.invoice(self.books, customer, self.lekki)
        rows = [
            blank(CreditNote.objects.create(entity=self.books, customer=customer, invoice=invoice, note_date=DAY)),
            blank(WriteOffRequest.objects.create(entity=self.books, invoice=invoice)),
            blank(Concession.objects.create(entity=self.books, customer=customer, invoice=invoice, concession_date=DAY)),
            blank(PaymentPlan.objects.create(entity=self.books, customer=customer, invoice=invoice, start_date=DAY)),
            blank(DunningNotice.objects.create(entity=self.books, customer=customer, invoice=invoice, level=1, notice_date=DAY)),
        ]
        plan = self.plan()
        for row in rows:
            with self.subTest(type(row).__name__):
                self.assertEqual(self.assigned(plan, row), (self.lekki.pk, "the invoice"))

    def test_an_adjustment_on_an_unbranched_invoice_falls_back_to_the_customer(self):
        customer = self.customer(self.books, "C1", self.yaba)
        other = self.customer(self.books, "C2", None)
        invoice = blank(self.invoice(self.books, other, None))
        note = blank(CreditNote.objects.create(entity=self.books, customer=customer, invoice=invoice, note_date=DAY))
        write_off = blank(WriteOffRequest.objects.create(entity=self.books, invoice=invoice))
        plan = self.plan()
        self.assertEqual(self.assigned(plan, note), (self.yaba.pk, "the customer"))
        self.assertEqual(self.flag(plan, write_off).reason, "no branch on the invoice or the customer")

    def test_a_refund_reads_its_source_payment_then_its_bank_then_its_customer(self):
        customer = self.customer(self.books, "C1", self.yaba)
        source = self.payment(self.books, customer)
        Payment._base_manager.filter(pk=source.pk).update(branch=self.lekki)
        by_source = blank(Refund.objects.create(entity=self.books, customer=customer, refund_date=DAY))
        RefundAllocation.objects.create(refund=by_source, payment=source, amount=1)
        by_bank = blank(Refund.objects.create(
            entity=self.books, customer=customer, refund_date=DAY, bank_account=self.bank(self.books, self.ikeja),
        ))
        by_customer = blank(Refund.objects.create(entity=self.books, customer=customer, refund_date=DAY))
        plan = self.plan()
        self.assertEqual(self.assigned(plan, by_source), (self.lekki.pk, "the refunded notes and payments"))
        self.assertEqual(self.assigned(plan, by_bank), (self.ikeja.pk, "the bank account"))
        self.assertEqual(self.assigned(plan, by_customer), (self.yaba.pk, "the customer"))


class OperationsDerivationTests(_BackfillFixture):
    """Petty cash, expense claims, fixed assets, and the models no fact places."""

    def fund(self, custodian, code):
        gl = Account.objects.create(
            entity=self.books, code=code, name=f"Float {code}",
            account_type=self.account(self.books, "1110").account_type, is_postable=True,
        )
        return blank(PettyCashFund.objects.create(entity=self.books, gl_account=gl, name=code, custodian=custodian))

    def test_a_petty_cash_fund_takes_its_custodians_branch_and_its_vouchers_follow(self):
        fund = self.fund(self.person("custodian@x.test", self.lekki), "1111")
        voucher = blank(PettyCashVoucher.objects.create(entity=self.books, fund=fund, voucher_date=DAY))
        plan = self.plan()
        self.assertEqual(self.assigned(plan, fund), (self.lekki.pk, "the custodian"))
        self.assertEqual(self.assigned(plan, voucher), (self.lekki.pk, "the fund"))

    def test_a_fund_without_a_placed_custodian_reads_its_vouchers(self):
        fund = self.fund(self.person("custodian@x.test", None), "1112")
        PettyCashVoucher.objects.create(entity=self.books, fund=fund, voucher_date=DAY, branch=self.yaba)
        self.assertEqual(self.assigned(self.plan(), fund), (self.yaba.pk, "the fund's vouchers"))

    def test_an_expense_claim_takes_the_claimants_branch(self):
        claim = blank(ExpenseClaim.objects.create(
            entity=self.books, claim_date=DAY, claimant=self.person("claimant@x.test", self.yaba),
        ))
        self.assertEqual(self.assigned(self.plan(), claim), (self.yaba.pk, "the claimant"))

    def test_a_fixed_asset_takes_its_funding_bank_accounts_branch(self):
        bank = self.bank(self.books, self.lekki)
        asset = blank(self.fixed_asset(self.books, "Bus", None))
        entry = self.journal(self.books, lines=[self.account(self.books, "1500"), bank.gl_account])
        type(asset)._base_manager.filter(pk=asset.pk).update(acquisition_journal=entry)
        self.assertEqual(self.assigned(self.plan(), asset), (self.lekki.pk, "the funding bank account"))

    def test_models_no_fact_places_are_flagged_at_a_multi_branch_school(self):
        budget = blank(Budget.objects.create(
            entity=self.books, name="Plan", fiscal_year=FiscalYear.objects.get(entity=self.books),
        ))
        shared = blank(self.bank(self.books, None))
        plan = self.plan()
        self.assertIsNone(self.assigned(plan, budget))
        self.assertIn("every budget belongs to a branch", self.flag(plan, budget).reason)
        self.assertIn("one record per branch", self.flag(plan, shared).reason)


class WholeTenantDocumentTests(_BackfillFixture):
    """A central payroll run and the tenant's tax return name no branch by design."""

    def test_a_central_run_and_the_tenants_return_are_neither_flagged_nor_placed(self):
        from vs_finance.models import PayrollRun, TaxFiling, TaxObligation

        run = PayrollRun.objects.create(entity=self.books, pay_date=DAY)
        filing = TaxFiling.objects.create(
            entity=self.books, obligation=TaxObligation.objects.filter(entity=self.books).first(),
            period_start=DAY, period_end=DAY,
        )
        plan = self.plan()
        for row in (run, filing):
            with self.subTest(row=type(row).__name__):
                self.assertIsNone(self.assigned(plan, row))
                self.assertIsNone(self.flag(plan, row))

    def test_a_central_run_is_left_alone_at_a_one_branch_school_too(self):
        from vs_finance.models import PayrollRun

        run = PayrollRun.objects.create(entity=self.solo_books, pay_date=DAY)
        self.assertIsNone(self.assigned(self.plan(self.solo_books), run))

    def test_a_central_runs_branch_journals_read_their_share(self):
        """Lekki's accrual of the central run is Lekki's, found through its share."""
        from vs_finance.models import PayrollRun, PayrollRunBranch

        run = PayrollRun.objects.create(entity=self.books, pay_date=DAY)
        accrual = self.journal(self.books)
        PayrollRunBranch.objects.create(run=run, branch=self.lekki, journal=accrual)
        self.assertEqual(
            self.assigned(self.plan(), accrual), (self.lekki.pk, "the document that raised it"),
        )


class JournalDerivationTests(_BackfillFixture):
    """A journal reads the document that raised it, the entry it reverses, then its banks."""

    def test_a_journal_takes_the_branch_of_the_document_that_raised_it(self):
        entry = self.journal(self.books)
        invoice = self.invoice(self.books, self.customer(self.books, "C1", None), self.lekki)
        type(invoice)._base_manager.filter(pk=invoice.pk).update(journal=entry)
        self.assertEqual(self.assigned(self.plan(), entry), (self.lekki.pk, "the document that raised it"))

    def test_a_journal_reads_a_document_only_planned_to_get_its_branch(self):
        entry = self.journal(self.books)
        invoice = blank(self.invoice(self.books, self.customer(self.books, "C1", self.yaba), None))
        type(invoice)._base_manager.filter(pk=invoice.pk).update(journal=entry)
        self.assertEqual(self.assigned(self.plan(), entry), (self.yaba.pk, "the document that raised it"))

    def test_a_depreciation_journal_reads_its_asset(self):
        entry = self.journal(self.books)
        asset = self.fixed_asset(self.books, "Bus", self.lekki)
        DepreciationSchedule.objects.create(asset=asset, seq=1, depreciation_date=DAY, journal=entry)
        self.assertEqual(self.assigned(self.plan(), entry), (self.lekki.pk, "the document that raised it"))

    def test_a_reversal_takes_the_branch_of_the_entry_it_reverses(self):
        original = self.journal(self.books)
        JournalEntry._base_manager.filter(pk=original.pk).update(branch=self.yaba)
        reversal = self.journal(self.books, reverses=original)
        self.assertEqual(self.assigned(self.plan(), reversal), (self.yaba.pk, "the entry it reverses"))

    def test_a_manual_journal_takes_the_branch_its_bank_lines_agree_on(self):
        bank = self.bank(self.books, self.lekki)
        agreed = self.journal(self.books, lines=[bank.gl_account, self.account(self.books, "4000")])
        split = self.journal(self.books, lines=[bank.gl_account, self.bank(self.books, self.ikeja).gl_account])
        plan = self.plan()
        self.assertEqual(self.assigned(plan, agreed), (self.lekki.pk, "the bank accounts on its lines"))
        self.assertIsNone(self.assigned(plan, split))
        self.assertEqual(
            self.flag(plan, split).reason, "the bank accounts on its lines disagree: Ikeja Branch, Lekki Branch",
        )

    def test_every_foreign_key_to_a_journal_is_a_registered_owner(self):
        """A journal-raising model left unregistered would leave its journals blank.

        ``related_objects`` are the reverse sides, so a foreign key to a journal
        reads as one-to-many from here.
        """
        registered = {(o.model_label, o.journal_field) for o in journal_owners()}
        not_owners = {
            ("vs_finance.JournalEntry", "reverses"),
            ("vs_finance.JournalLine", "entry"),
            # Points at the settlement journal its bank statement line owns and raised.
            ("vs_payments.CollectionIntent", "settlement_entry"),
            # The platform's own entry, always in the platform's branch rather than
            # the client branch the movement is for.
            ("vs_payments.HeldMovement", "platform_journal"),
            # Points at the credit note or concession journal that gave
            # income back; that document owns it, and the transfer's own journal is a leg's.
            ("vs_finance.InterBranchTransfer", "adjustment_entry"),
        }
        checked = 0
        for field in JournalEntry._meta.related_objects:
            if not field.one_to_many and not field.one_to_one:
                continue
            checked += 1
            key = (field.related_model._meta.label, field.field.name)
            if key in not_owners:
                continue
            with self.subTest(key):
                self.assertIn(key, registered)
        self.assertGreater(checked, len(not_owners))

    def test_every_branched_finance_document_is_a_target(self):
        """A document model with a branch column and no target would never be backfilled."""
        from django.apps import apps

        from vs_finance.models.core import FinanceDocument

        covered = {t.model_label for t in targets()}
        for model in apps.get_models():
            if issubclass(model, FinanceDocument) and model._meta.app_label in {"vs_finance", "vs_procurement"}:
                with self.subTest(model._meta.label):
                    self.assertIn(model._meta.label, covered)


class SchoolShapeTests(_BackfillFixture):
    """One branch files what is left under it; several never guess."""

    def test_a_one_branch_school_files_the_unresolved_under_its_only_branch(self):
        invoice = blank(self.invoice(self.solo_books, self.customer(self.solo_books, "S1", None), None))
        budget = blank(Budget.objects.create(
            entity=self.solo_books, name="Plan", fiscal_year=FiscalYear.objects.get(entity=self.solo_books),
        ))
        plan = self.plan(self.solo_books)
        self.assertEqual(self.assigned(plan, invoice), (self.solo_main.pk, ONLY_BRANCH))
        self.assertEqual(self.assigned(plan, budget), (self.solo_main.pk, ONLY_BRANCH))
        apply_plan(plan)
        invoice.refresh_from_db()
        self.assertEqual(invoice.branch_id, self.solo_main.pk)

    def test_a_multi_branch_school_leaves_the_unresolved_blank_rather_than_use_main(self):
        self.assertTrue(self.ikeja.is_main)
        invoice = blank(self.invoice(self.books, self.customer(self.books, "C1", None), None))
        call_command("branch_backfill", "--tenant", self.tenant.slug, "--apply", stdout=io.StringIO())
        invoice.refresh_from_db()
        self.assertIsNone(invoice.branch_id)

    def test_a_tenant_that_owns_no_branch_is_reported_as_a_data_error_and_the_run_goes_on(self):
        """Every tenant must own a branch, so one that owns none is reported as a data error.

        Bare Books owns no branch and has an unbranched invoice. A run over every
        tenant says so in those words, leaves the invoice untouched, and still
        files Single Site's unbranched invoice under its only branch.
        """
        from vs_tenants.models import Tenant

        bare = Tenant.objects.create(name="Bare Books", slug="bare-books", kind="ORGANIZATION", status="ACTIVE")
        books = self.build_books("BAREBOOKS", bare)
        invoice = blank(self.invoice(books, self.customer(books, "B1", None), None))
        solo = blank(self.invoice(self.solo_books, self.customer(self.solo_books, "S2", None), None))
        plan = self.plan(books)
        self.assertTrue(plan.owns_no_branch)
        self.assertTrue(next(p for p in plan.targets if p.target.model_label == "vs_finance.Invoice").blocked)

        audit = io.StringIO()
        call_command("branch_audit", stdout=audit)
        self.assertIn("owns no branch, which every tenant must; this is a data error",
                      audit.getvalue())
        self.assertIn("Data errors", audit.getvalue())
        self.assertIn("Bare Books [bare-books] books BAREBOOKS", audit.getvalue())
        self.assertNotIn("BLOCKED", audit.getvalue())
        self.assertNotIn("Prerequisite", audit.getvalue())

        backfill = io.StringIO()
        call_command("branch_backfill", "--apply", stdout=backfill)
        self.assertIn("this is a data error", backfill.getvalue())
        invoice.refresh_from_db()
        solo.refresh_from_db()
        self.assertIsNone(invoice.branch_id)
        self.assertEqual(solo.branch_id, self.solo_main.pk)


class WriteRuleTests(_BackfillFixture):
    """Dry run, idempotence, branched rows, the audit trail and the flagged list."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.lekki_customer = cls.customer(cls.books, "C1", cls.lekki)
        cls.invoice_row = blank(cls.invoice(cls.books, cls.lekki_customer, None))

    def test_a_dry_run_writes_nothing(self):
        events = AuditEvent.objects.count()
        out = io.StringIO()
        call_command("branch_backfill", "--tenant", self.tenant.slug, stdout=out)
        self.invoice_row.refresh_from_db()
        self.assertIsNone(self.invoice_row.branch_id)
        self.assertEqual(AuditEvent.objects.count(), events)
        self.assertIn("Dry run: would write a branch on 1 row.", out.getvalue())

    def test_apply_writes_and_audits_each_change(self):
        call_command("branch_backfill", "--tenant", self.tenant.slug, "--apply", stdout=io.StringIO())
        self.invoice_row.refresh_from_db()
        self.assertEqual(self.invoice_row.branch_id, self.lekki.pk)
        event = AuditEvent.objects.get(entity_type="vs_finance.Invoice", entity_id=str(self.invoice_row.pk))
        self.assertEqual(event.diff_data, {"branch_id": {"before": None, "after": self.lekki.pk}})
        self.assertEqual(event.metadata["derived_from"], "the customer")
        self.assertEqual(event.tenant_id, self.tenant.pk)

    def test_a_second_run_changes_nothing(self):
        call_command("branch_backfill", "--tenant", self.tenant.slug, "--apply", stdout=io.StringIO())
        events = AuditEvent.objects.count()
        out = io.StringIO()
        call_command("branch_backfill", "--tenant", self.tenant.slug, "--apply", stdout=out)
        self.assertEqual(AuditEvent.objects.count(), events)
        self.assertIn("Wrote a branch on 0 rows;", out.getvalue())
        self.assertFalse(any(p.assign for p in self.plan().targets))

    def test_a_row_that_already_has_a_branch_is_never_touched(self):
        branched = self.invoice(self.books, self.lekki_customer, self.yaba)
        call_command("branch_backfill", "--tenant", self.tenant.slug, "--apply", stdout=io.StringIO())
        branched.refresh_from_db()
        self.assertEqual(branched.branch_id, self.yaba.pk)
        self.assertFalse(AuditEvent.objects.filter(entity_id=str(branched.pk), entity_type="vs_finance.Invoice").exists())

    def test_a_row_that_gains_a_branch_after_planning_is_skipped(self):
        plan = self.plan()
        type(self.invoice_row)._base_manager.filter(pk=self.invoice_row.pk).update(branch=self.yaba)
        result = apply_plan(plan)
        self.invoice_row.refresh_from_db()
        self.assertEqual(self.invoice_row.branch_id, self.yaba.pk)
        self.assertEqual(result.written["vs_finance.Invoice"], 0)
        self.assertEqual(result.skipped["vs_finance.Invoice"], 1)

    def test_the_audit_prints_counts_and_the_flagged_list(self):
        stranded = blank(self.invoice(self.books, self.customer(self.books, "C2", None), None))
        out = io.StringIO()
        call_command("branch_audit", "--tenant", self.tenant.slug, stdout=out)
        text = out.getvalue()
        self.assertIn("3 branches", text)
        self.assertRegex(text, r"vs_finance\.Invoice\s+2\s+1\s+0\s+1")
        self.assertIn(f"vs_finance.Invoice #{stranded.pk} {stranded.document_number}: no branch on the customer", text)
        self.invoice_row.refresh_from_db()
        self.assertIsNone(self.invoice_row.branch_id)

    def test_other_tenants_are_left_alone(self):
        rival = blank(self.invoice(self.rival_books, self.customer(self.rival_books, "R1", self.rival_branch), None))
        call_command("branch_backfill", "--tenant", self.tenant.slug, "--apply", stdout=io.StringIO())
        rival.refresh_from_db()
        self.assertIsNone(rival.branch_id)

    def test_small_batches_write_every_row(self):
        second = blank(self.invoice(self.books, self.lekki_customer, None))
        result = apply_plan(self.plan(), batch_size=1)
        self.assertEqual(result.written["vs_finance.Invoice"], 2)
        second.refresh_from_db()
        self.assertEqual(second.branch_id, self.lekki.pk)


class GateTests(_BackfillFixture):
    """The year close and the tax return, counted the way those checks count them."""

    def posted_sale(self, entity, *, owner_branch=None):
        """A posted fee sale with output VAT and no branch, optionally raised by an invoice."""
        from vs_finance.constants import DocumentStatus
        from vs_finance.models import FiscalPeriod
        from vs_finance.seed import seed_tax_obligations

        seed_tax_obligations(entity)
        entry = JournalEntry.objects.create(
            entity=entity, date=DAY, period=FiscalPeriod.objects.get(entity=entity, period_no=1),
        )
        JournalLine.objects.create(entry=entry, account=self.account(entity, "1200"), debit=1075, credit=0, line_no=1)
        JournalLine.objects.create(entry=entry, account=self.account(entity, "4100"), debit=0, credit=1000, line_no=2)
        JournalLine.objects.create(entry=entry, account=self.account(entity, "2200"), debit=0, credit=75, line_no=3)
        JournalEntry._base_manager.filter(pk=entry.pk).update(branch=None, status=DocumentStatus.POSTED)
        if owner_branch is not None:
            invoice = self.invoice(entity, self.customer(entity, f"G{entry.pk}", None), owner_branch)
            type(invoice)._base_manager.filter(pk=invoice.pk).update(journal=entry)
        return entry

    def gate(self, plan, name):
        from vs_finance.branch_derivation import gates

        return next((g for g in gates(plan) if g.name == name), None)

    def test_the_year_close_and_vat_gates_count_what_the_backfill_resolves(self):
        self.posted_sale(self.books, owner_branch=self.lekki)
        self.posted_sale(self.books)
        plan = self.plan()
        close = self.gate(plan, "year close 2026")
        vat = self.gate(plan, "VAT return")
        self.assertEqual((close.count, close.resolved), (2, 1))
        self.assertEqual((vat.count, vat.resolved), (2, 1))
        out = io.StringIO()
        call_command("branch_audit", "--tenant", self.tenant.slug, stdout=out)
        self.assertIn("year close 2026: 2 unbranched entries; the backfill resolves 1, 1 needs an administrator", out.getvalue())

    def test_the_gates_clear_once_the_backfill_has_run(self):
        self.posted_sale(self.books, owner_branch=self.lekki)
        apply_plan(self.plan())
        plan = self.plan()
        self.assertIsNone(self.gate(plan, "year close 2026"))
        self.assertIsNone(self.gate(plan, "VAT return"))

    def test_a_one_branch_school_has_no_gates(self):
        from vs_finance.branch_derivation import gates

        self.posted_sale(self.solo_books)
        self.assertEqual(gates(self.plan(self.solo_books)), [])
