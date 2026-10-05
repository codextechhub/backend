"""A pupil's fee account moving with them when they change branch.

Tunde attends Ikeja at Corona. He owes N400,000 for a term that began ten days
ago and runs three months more, and holds N10,000 of credit at Ikeja. When he
moves to Lekki his account moves with him: Lekki collects the bill, takes over
the credit and the fees not yet earned, and owes Ikeja for the days Ikeja has
already taught him. The fixture here is shared with the students app's tests of
the move itself (``schools/vs_students/tests/test_branch_move.py``).
"""
from __future__ import annotations

import datetime

from vs_config.clock import branch_today

from schools.core.fal import registry
from schools.core.fal.adapters.django_finance import DjangoStudentCustomerAdapter
from schools.core.fal.exceptions import CrossTenantError
from schools.core.fal.testing import FakeStudentCustomer

from .base import FALFixture

TERM_FEE = 40_000_000
CREDIT = 1_000_000


def net(account, branch, counterparty=None):
    """Debits less credits on ``account`` in one branch's books, against one counterparty."""
    from vs_finance.branch_ledger import ledger_lines

    lines = ledger_lines().filter(account=account, entry__branch=branch)
    if counterparty is not None:
        lines = lines.filter(counterparty_branch=counterparty)
    return sum(line.debit - line.credit for line in lines)


class PupilMoveFixture(FALFixture):
    """Corona (Ikeja and Lekki) with Tunde billed at Ikeja; Greenfield runs one branch."""

    @classmethod
    def setUpTestData(cls):
        from schools.vs_academics.models import AcademicSession, Level, Program, SchoolClass
        from schools.vs_students.models import ClassEnrolment
        from vs_finance.models import Customer, Invoice, InvoiceLine, Payment
        from vs_finance.receivables import post_invoice, post_payment

        super().setUpTestData()
        cls.today = branch_today(cls.corona.tenant, cls.ikeja.pk)
        cls.year = AcademicSession.all_objects.create(
            tenant=cls.corona.tenant, name="2026/2027", status="ACTIVE",
            start_date=cls.today - datetime.timedelta(days=120),
            end_date=cls.today + datetime.timedelta(days=210),
        )
        program = Program.all_objects.create(
            tenant=cls.corona.tenant, name="Junior Secondary", code="JSS",
        )
        level = Level.all_objects.create(
            tenant=cls.corona.tenant, program=program, session=cls.year,
            name="JSS1", code="JSS1", order_index=1,
        )
        cls.ikeja_class = SchoolClass.all_objects.create(
            tenant=cls.corona.tenant, level=level, session=cls.year, name="JSS1 Ikeja",
            code="J1I", arm="A", capacity=30, branch=cls.ikeja,
        )
        cls.lekki_class = SchoolClass.all_objects.create(
            tenant=cls.corona.tenant, level=level, session=cls.year, name="JSS1 Lekki",
            code="J1L", arm="B", capacity=30, branch=cls.lekki,
        )
        cls.shared_class = SchoolClass.all_objects.create(
            tenant=cls.corona.tenant, level=level, session=cls.year, name="JSS1 Shared",
            code="J1S", arm="C", capacity=30, branch=None,
        )

        cls.tunde = cls.student(cls.corona, cls.ikeja)
        cls.placement = ClassEnrolment.all_objects.create(
            tenant=cls.corona.tenant, student=cls.tunde, school_class=cls.ikeja_class,
            session=cls.year, effective_date=cls.today - datetime.timedelta(days=60),
        )
        handle = DjangoStudentCustomerAdapter().ensure_customer(
            cls.tunde.pk, entity_ref=cls.corona_books.entity_ref,
        ).unwrap()
        cls.customer = Customer.objects.get(pk=handle.customer_ref)

        cls.bill = Invoice.objects.create(
            entity_id=cls.corona_books.entity_ref, customer=cls.customer, branch=cls.ikeja,
            invoice_date=cls.today, due_date=cls.today + datetime.timedelta(days=30),
        )
        InvoiceLine.objects.create(
            invoice=cls.bill, line_no=1, quantity=1, unit_price=TERM_FEE,
            revenue_account=cls.account(cls.corona_books.entity_ref, "4100"),
            service_start=cls.today - datetime.timedelta(days=10),
            service_end=cls.today + datetime.timedelta(days=90),
        )
        post_invoice(cls.bill)
        cls.bill.refresh_from_db()

        cls.credit = Payment.objects.create(
            entity_id=cls.corona_books.entity_ref, customer=cls.customer, branch=cls.ikeja,
            payment_date=cls.today, amount=CREDIT,
            deposit_account=cls.account(cls.corona_books.entity_ref, "1100"),
        )
        post_payment(cls.credit, auto_allocate=False)

        # A Greenfield pupil, at the only branch Greenfield has.
        cls.ada = cls.student(cls.greenfield, cls.greenfield_main, first="Ada")

    # ---- helpers -----------------------------------------------------------
    def ledger(self, code):
        return self.account(self.corona_books.entity_ref, code)

    def unearned(self, on=None):
        """The term's income not yet earned on ``on``, which moves with the bill."""
        from vs_finance.models import DeferredIncomeEntry

        return sum(entry.open_amount for entry in DeferredIncomeEntry.objects.filter(
            invoice=self.bill, status="PENDING", recognition_date__gt=on or self.today,
        ))

    def moves(self):
        from vs_finance.models import InterBranchTransfer

        return InterBranchTransfer.objects.filter(entity_id=self.corona_books.entity_ref)

    def move(self, move_ref="M1", *, dry_run=False, to=None, on=None):
        return DjangoStudentCustomerAdapter().move_account(
            self.tunde.pk, from_branch_ref=self.ikeja.pk,
            to_branch_ref=(to or self.lekki).pk, move_ref=move_ref, move_date=on,
            actor_ref=self.bursar.pk, reason="Family moved to Lekki", dry_run=dry_run,
        ).unwrap()


class MoveAccountPortTests(PupilMoveFixture):
    """The FAL's ``move_account`` over the real engine."""

    def test_the_account_and_its_balance_move_and_revenue_stays(self):
        unearned = self.unearned()
        ikeja_revenue = net(self.ledger("4100"), self.ikeja)

        (moved,) = self.move()

        self.customer.refresh_from_db()
        self.bill.refresh_from_db()
        self.assertEqual(self.customer.branch_id, self.lekki.pk)
        self.assertEqual(self.bill.branch_id, self.lekki.pk)
        self.assertEqual(self.bill.journal.branch_id, self.ikeja.pk)
        self.assertEqual(
            (moved.owed_amount, moved.credit_amount, moved.deferred_amount),
            (TERM_FEE, CREDIT, unearned),
        )
        self.assertEqual(moved.inter_branch_amount, TERM_FEE - CREDIT - unearned)
        self.assertEqual([b.number for b in moved.bills], [self.bill.document_number])
        self.assertTrue(moved.transfer_number)
        ib = self.ledger("1260")
        self.assertEqual(net(ib, self.ikeja, self.lekki), TERM_FEE - CREDIT - unearned)
        self.assertEqual(net(ib, self.lekki, self.ikeja), -(TERM_FEE - CREDIT - unearned))
        self.assertEqual(net(self.ledger("4100"), self.ikeja), ikeja_revenue)
        self.assertEqual(net(self.ledger("4100"), self.lekki), 0)
        self.assertTrue(self.moves().get().move_key.startswith("pupil-move:M1:"))

    def test_a_retry_of_the_same_move_moves_nothing_twice(self):
        from vs_finance.models import FinanceAuditLog

        first = self.move("M7")
        trail = FinanceAuditLog.objects.count()
        ib = net(self.ledger("1260"), self.ikeja)

        again = self.move("M7")

        self.assertEqual(again, first)
        self.assertEqual(self.moves().count(), 1)
        self.assertEqual(net(self.ledger("1260"), self.ikeja), ib)
        self.assertEqual(FinanceAuditLog.objects.count(), trail)

    def test_a_preview_carries_the_figures_and_moves_nothing(self):
        from vs_finance.branch_ledger import ledger_lines

        lines = ledger_lines().count()

        (preview,) = self.move(dry_run=True)

        self.assertIsNone(preview.transfer_ref)
        self.assertEqual(preview.transfer_number, "")
        self.assertEqual(preview.owed_amount, TERM_FEE)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.branch_id, self.ikeja.pk)
        self.assertFalse(self.moves().exists())
        self.assertEqual(ledger_lines().count(), lines)
        (real,) = self.move()
        self.assertEqual(
            (real.owed_amount, real.inter_branch_amount, real.bills),
            (preview.owed_amount, preview.inter_branch_amount, preview.bills),
        )

    def test_a_branch_of_another_school_is_refused(self):
        with self.assertRaises(CrossTenantError):
            self.move(to=self.greenfield_main)
        self.assertFalse(self.moves().exists())

    def test_a_pupil_never_billed_moves_no_account(self):
        amaka = self.student(self.corona, self.ikeja, first="Amaka")

        moved = DjangoStudentCustomerAdapter().move_account(
            amaka.pk, from_branch_ref=self.ikeja.pk, to_branch_ref=self.lekki.pk,
            move_ref="M3",
        ).unwrap()

        self.assertEqual(moved, ())
        self.assertFalse(self.moves().exists())


class FakeMoveAccountTests(FALFixture):
    """The fake records one move per reference, as the real port books one."""

    def test_the_fake_records_a_move_once(self):
        fake = FakeStudentCustomer()
        registry.set_student_customer(fake)

        for _ in range(2):
            registry.get_student_customer().move_account(
                7, from_branch_ref=1, to_branch_ref=2, move_ref="M1",
            )
        registry.get_student_customer().move_account(
            7, from_branch_ref=1, to_branch_ref=2, move_ref="M2", dry_run=True,
        )

        self.assertEqual(fake.moves, [("M1", 7, 1, 2)])
