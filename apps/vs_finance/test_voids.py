from __future__ import annotations

import datetime
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from .constants import CreditNoteKind, DocumentStatus, InvoicePaymentStatus, PeriodStatus
from .credit_notes import allocate_credit_note, post_credit_note, post_refund
from .exceptions import BackdatedPostingError, PostingError
from .installments import activate_payment_plan, build_installments, post_concession
from .models import (
    Account,
    Concession,
    CreditNote,
    CreditNoteLine,
    Customer,
    FiscalPeriod,
    FiscalYear,
    Invoice,
    LedgerEntity,
    Payment,
    PaymentPlan,
    Refund,
)
from .posting import reverse_journal
from .receivables import (
    allocate_payment,
    customer_credit_balance,
    post_invoice,
    post_payment,
)
from .reports import reconcile_ar
from .tests import _ARFixtureMixin
from .voids import (
    void_concession,
    void_credit_note,
    void_invoice,
    void_payment,
    void_refund,
)


class ARDocumentVoidTests(_ARFixtureMixin, TestCase):
    def _credit_note(self, entity, customer, *, amount=30000, invoice=None,
                     date=datetime.date(2026, 1, 15), auto_allocate=False):
        note = CreditNote.objects.create(
            entity=entity, customer=customer, kind=CreditNoteKind.CREDIT,
            note_date=date, invoice=invoice, reason="Correction",
        )
        CreditNoteLine.objects.create(
            note=note,
            revenue_account=Account.objects.get(entity=entity, code="4900"),
            quantity=1, unit_price=amount, line_no=1,
        )
        post_credit_note(note, auto_allocate=auto_allocate)
        note.refresh_from_db()
        return note

    def test_void_payment_unwinds_invoice_and_gl(self):
        entity, _, customer, _ = self.build_ar()
        invoice = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(invoice)
        plan = PaymentPlan.objects.create(
            entity=entity, customer=customer, invoice=invoice,
            start_date=datetime.date(2026, 1, 10), frequency="MONTHLY",
            installment_count=2, total_amount=invoice.balance_due,
        )
        build_installments(plan)
        activate_payment_plan(plan)
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=50000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        plan.refresh_from_db()
        self.assertEqual(plan.plan_status, "COMPLETED")

        void_payment(payment)
        payment.refresh_from_db(); invoice.refresh_from_db(); plan.refresh_from_db()
        self.assertEqual(payment.status, DocumentStatus.REVERSED)
        self.assertEqual(payment.allocated_amount, 0)
        self.assertEqual(invoice.amount_paid, 0)
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.UNPAID)
        self.assertEqual(plan.plan_status, "ACTIVE")
        self.assertTrue(all(row.amount_settled == 0 for row in plan.installments.all()))
        self.assertTrue(reconcile_ar(entity).is_reconciled)
        with self.assertRaises(PostingError):
            void_payment(payment)

    def test_void_payment_reverses_later_allocation_journal(self):
        entity, _, customer, _ = self.build_ar()
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 5),
            amount=40000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        invoice = self.make_invoice(
            entity, customer, lines=[("4100", 1, 40000, None)],
            date=datetime.date(2026, 1, 10),
        )
        post_invoice(invoice)
        allocate_payment(payment)
        link = payment.allocation_journals.get()

        with self.assertRaises(BackdatedPostingError):
            void_payment(payment, date=datetime.date(2026, 1, 7))
        payment.refresh_from_db(); link.journal.refresh_from_db()
        self.assertEqual(payment.status, DocumentStatus.POSTED)
        self.assertEqual(link.journal.status, DocumentStatus.POSTED)

        void_payment(payment, date=datetime.date(2026, 1, 20))
        link.journal.refresh_from_db(); invoice.refresh_from_db()
        self.assertEqual(link.journal.status, DocumentStatus.REVERSED)
        self.assertEqual(invoice.amount_paid, 0)
        self.assertTrue(reconcile_ar(entity).is_reconciled)

    def test_void_credit_note_unwinds_later_allocation(self):
        entity, _, customer, _ = self.build_ar()
        note = self._credit_note(
            entity, customer, amount=50000, date=datetime.date(2026, 1, 5),
        )
        invoice = self.make_invoice(
            entity, customer, lines=[("4100", 1, 50000, None)],
            date=datetime.date(2026, 1, 10),
        )
        post_invoice(invoice)
        allocate_credit_note(note)
        link = note.allocation_journals.get()

        void_credit_note(note, date=datetime.date(2026, 1, 20))
        note.refresh_from_db(); invoice.refresh_from_db(); link.journal.refresh_from_db()
        self.assertEqual(note.status, DocumentStatus.REVERSED)
        self.assertEqual(note.allocated_amount, 0)
        self.assertEqual(invoice.amount_credited, 0)
        self.assertEqual(link.journal.status, DocumentStatus.REVERSED)
        self.assertTrue(reconcile_ar(entity).is_reconciled)
        with self.assertRaises(PostingError):
            void_credit_note(note)

    def test_void_credit_note_unwinds_allocation_embedded_in_original_journal(self):
        entity, _, customer, _ = self.build_ar()
        invoice = self.make_invoice(
            entity, customer, lines=[("4100", 1, 30000, None)],
            date=datetime.date(2026, 1, 5),
        )
        post_invoice(invoice)
        note = self._credit_note(
            entity, customer, amount=30000, invoice=invoice,
            date=datetime.date(2026, 1, 10), auto_allocate=True,
        )
        invoice.refresh_from_db()
        self.assertEqual(note.allocated_amount, 30000)
        self.assertEqual(invoice.amount_credited, 30000)
        self.assertFalse(note.allocation_journals.exists())
        self.assertEqual(
            sum(note.journal.lines.filter(account__code="1200").values_list("credit", flat=True)),
            30000,
        )

        void_credit_note(note)
        note.refresh_from_db(); invoice.refresh_from_db(); note.journal.refresh_from_db()
        self.assertEqual(note.status, DocumentStatus.REVERSED)
        self.assertEqual(note.allocated_amount, 0)
        self.assertEqual(invoice.amount_credited, 0)
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.UNPAID)
        self.assertEqual(note.journal.status, DocumentStatus.REVERSED)
        self.assertTrue(reconcile_ar(entity).is_reconciled)

    def test_incomplete_legacy_allocation_link_fails_closed(self):
        entity, _, customer, _ = self.build_ar()
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 5),
            amount=40000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        invoice = self.make_invoice(
            entity, customer, lines=[("4100", 1, 40000, None)],
            date=datetime.date(2026, 1, 10),
        )
        post_invoice(invoice)
        allocate_payment(payment)
        link = payment.allocation_journals.get()
        allocation_journal = link.journal
        link.delete()  # Simulate a legacy audit event that could not be backfilled.

        with self.assertRaisesMessage(PostingError, "Repair/backfill"):
            void_payment(payment)
        with self.assertRaises(PostingError) as caught:
            reverse_journal(allocation_journal)
        self.assertIn(payment.document_number, str(caught.exception))

    def test_void_refund_restores_source_lot_credit(self):
        entity, _, customer, _ = self.build_ar()
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 5),
            amount=30000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        refund = Refund.objects.create(
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 10),
            amount=30000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_refund(refund)
        self.assertEqual(customer_credit_balance(customer), 0)

        void_refund(refund)
        refund.refresh_from_db(); payment.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.REVERSED)
        self.assertEqual(payment.refunded_amount, 0)
        self.assertEqual(payment.credit_remaining, 30000)
        self.assertEqual(customer_credit_balance(customer), 30000)
        self.assertTrue(reconcile_ar(entity).is_reconciled)
        with self.assertRaises(PostingError):
            void_refund(refund)

    def test_void_refund_restores_credit_note_source_lot(self):
        entity, _, customer, _ = self.build_ar()
        note = self._credit_note(
            entity, customer, amount=30000, date=datetime.date(2026, 1, 5),
        )
        refund = Refund.objects.create(
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 10),
            amount=30000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_refund(refund)
        allocation = refund.allocations.get()
        note.refresh_from_db()
        self.assertEqual(allocation.note_id, note.pk)
        self.assertIsNone(allocation.payment_id)
        self.assertEqual(note.refunded_amount, 30000)
        self.assertEqual(note.credit_remaining, 0)

        void_refund(refund)
        refund.refresh_from_db(); note.refresh_from_db()
        self.assertEqual(refund.status, DocumentStatus.REVERSED)
        self.assertEqual(note.refunded_amount, 0)
        self.assertEqual(note.credit_remaining, 30000)
        self.assertEqual(customer_credit_balance(customer), 30000)
        self.assertTrue(reconcile_ar(entity).is_reconciled)

    def test_source_documents_require_posted_refund_voided_first(self):
        entity, _, customer, _ = self.build_ar()
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 5),
            amount=20000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        refund = Refund.objects.create(
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 10),
            amount=20000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_refund(refund)
        with self.assertRaisesMessage(PostingError, "void that refund first"):
            void_payment(payment)

    def test_void_concession_and_invoice(self):
        entity, _, customer, _ = self.build_ar()
        invoice = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(invoice)
        concession = Concession.objects.create(
            entity=entity, customer=customer, invoice=invoice,
            concession_date=datetime.date(2026, 1, 15), amount=10000,
        )
        post_concession(concession)
        void_concession(concession)
        invoice.refresh_from_db(); concession.refresh_from_db()
        self.assertEqual(invoice.amount_credited, 0)
        self.assertEqual(concession.status, DocumentStatus.REVERSED)
        self.assertTrue(reconcile_ar(entity).is_reconciled)
        with self.assertRaises(PostingError):
            void_concession(concession)

        void_invoice(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.REVERSED)
        self.assertTrue(reconcile_ar(entity).is_reconciled)
        with self.assertRaises(PostingError):
            void_invoice(invoice)

    def test_invoice_void_refuses_downstream_settlement(self):
        entity, _, customer, _ = self.build_ar()
        invoice = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(invoice)
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=10000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        with self.assertRaisesMessage(PostingError, "receipt settlements"):
            void_invoice(invoice)

    def test_raw_reversal_refuses_each_document_journal_but_manual_is_reversible(self):
        entity, period, customer, _ = self.build_ar()
        invoice = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(invoice)
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=10000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment, auto_allocate=False)
        note = self._credit_note(entity, customer, amount=5000)
        refund = Refund.objects.create(
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 20),
            amount=5000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_refund(refund)
        refund.refresh_from_db()
        concession = Concession.objects.create(
            entity=entity, customer=customer, invoice=invoice,
            concession_date=datetime.date(2026, 1, 20), amount=5000,
        )
        post_concession(concession)

        for document in (invoice, payment, note, refund, concession):
            with self.subTest(document=type(document).__name__):
                with self.assertRaises(PostingError) as caught:
                    reverse_journal(document.journal)
                self.assertIn(document.document_number, str(caught.exception))
                self.assertIn("/void/", str(caught.exception))

        manual = self.make_entry(entity, period, [("1100", 1000, 0), ("4100", 0, 1000)])
        from .posting import post_journal
        post_journal(manual)
        reversal = reverse_journal(manual)
        self.assertEqual(reversal.status, DocumentStatus.POSTED)

    def test_journal_detail_contract_routes_document_void_without_guessing(self):
        from .posting import post_journal
        from .serializers import JournalEntryDetailSerializer

        entity, period, customer, _ = self.build_ar()
        invoice = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(invoice)

        action = JournalEntryDetailSerializer(invoice.journal).data["reversal_action"]
        self.assertEqual(action, {
            "kind": "VOID_DOCUMENT",
            "document_type": "INVOICE",
            "document_id": invoice.pk,
            "document_number": invoice.document_number,
        })

        manual = self.make_entry(entity, period, [("1100", 1000, 0), ("4100", 0, 1000)])
        post_journal(manual)
        manual_action = JournalEntryDetailSerializer(manual).data["reversal_action"]
        self.assertEqual(manual_action, {"kind": "REVERSE_JOURNAL"})

    def test_void_date_cannot_precede_document_or_later_allocation(self):
        entity, _, customer, _ = self.build_ar()
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 10),
            amount=40000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        with self.assertRaises(BackdatedPostingError):
            void_payment(payment, date=datetime.date(2026, 1, 9))
        payment.refresh_from_db()
        self.assertEqual(payment.status, DocumentStatus.POSTED)

    def test_void_without_date_falls_forward_when_original_period_is_closed(self):
        entity, january, customer, _ = self.build_ar()
        invoice = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(invoice)
        august = FiscalPeriod.objects.create(
            entity=entity, fiscal_year=january.fiscal_year, period_no=8,
            name="Aug 2026", start_date=datetime.date(2026, 8, 1),
            end_date=datetime.date(2026, 8, 31), status=PeriodStatus.OPEN,
        )
        january.status = PeriodStatus.CLOSED
        january.save(update_fields=["status", "updated_at"])
        now = timezone.make_aware(datetime.datetime(2026, 8, 1, 12, 0))

        with mock.patch("vs_finance.posting.timezone.now", return_value=now):
            void_invoice(invoice)

        invoice.refresh_from_db()
        reversal = invoice.journal.reversed_by
        self.assertEqual(reversal.date, datetime.date(2026, 8, 1))
        self.assertEqual(reversal.period, august)

    def test_closed_period_fallback_never_backdates_future_document(self):
        entity, january, customer, _ = self.build_ar()
        december = FiscalPeriod.objects.create(
            entity=entity, fiscal_year=january.fiscal_year, period_no=12,
            name="Dec 2026", start_date=datetime.date(2026, 12, 1),
            end_date=datetime.date(2026, 12, 31), status=PeriodStatus.OPEN,
        )
        invoice = self.make_invoice(
            entity, customer, lines=[("4100", 1, 50000, None)],
            date=datetime.date(2026, 12, 10), due=datetime.date(2026, 12, 20),
        )
        post_invoice(invoice)
        december.status = PeriodStatus.CLOSED
        december.save(update_fields=["status", "updated_at"])
        now = timezone.make_aware(datetime.datetime(2026, 8, 1, 12, 0))

        with mock.patch("vs_finance.posting.timezone.now", return_value=now):
            with self.assertRaises(BackdatedPostingError):
                void_invoice(invoice)

        invoice.refresh_from_db(); invoice.journal.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.POSTED)
        self.assertEqual(invoice.journal.status, DocumentStatus.POSTED)
        self.assertFalse(hasattr(invoice.journal, "reversed_by"))

    def test_reconcile_excludes_2140_credit_and_includes_debit_note_ar(self):
        entity, _, customer, _ = self.build_ar()
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 5),
            amount=40000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment)
        rec = reconcile_ar(entity)
        self.assertTrue(rec.is_reconciled)
        self.assertEqual(rec.subledger_total, 0)
        self.assertEqual(rec.control_total, 0)

        note = CreditNote.objects.create(
            entity=entity, customer=customer, kind=CreditNoteKind.DEBIT,
            note_date=datetime.date(2026, 1, 10),
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=entity, code="4100"),
            quantity=1, unit_price=25000, line_no=1,
        )
        post_credit_note(note)
        rec = reconcile_ar(entity)
        self.assertTrue(rec.is_reconciled)
        self.assertEqual(rec.subledger_total, 25000)
        self.assertEqual(rec.control_total, 25000)


class ARDocumentVoidEndpointTests(_ARFixtureMixin, TestCase):
    VIEW_SPECS = (
        ("finance.invoice.reverse", "InvoiceVoidView", "invoices"),
        ("finance.payment.reverse", "PaymentVoidView", "payments"),
        ("finance.creditnote.reverse", "CreditNoteVoidView", "credit-notes"),
        ("finance.refund.reverse", "RefundVoidView", "refunds"),
        ("finance.concession.reverse", "ConcessionVoidView", "concessions"),
    )

    def _user(self, *keys, email):
        from django.contrib.auth import get_user_model
        from vs_rbac.tests.helpers import (
            make_assignment, make_permission, make_role, make_role_permission,
        )
        from vs_tenants.models import Tenant

        tenant = Tenant.objects.get(slug="codex")
        user = get_user_model().objects.create_user(
            email=email, password="x", user_type="CX_STAFF", status="ACTIVE",
            first_name="Void", last_name="Tester", tenant=tenant,
        )
        role = make_role(tenant, name=f"Role {email}")
        for key in keys:
            make_role_permission(role, make_permission(key))
        make_assignment(tenant, user, role)
        return user

    def _call(self, view_name, path, entity, pk, user, body=None):
        from . import views_ar

        request = APIRequestFactory().post(
            f"/v1/finance/{path}/{pk}/void/?entity={entity.code}",
            body or {}, format="json",
        )
        force_authenticate(request, user=user)
        request.tenant = user.tenant
        request.rbac_tenant = user.tenant
        response = getattr(views_ar, view_name).as_view()(request, pk=pk)
        response.render()
        return response

    def _posted_documents(self):
        entity, _, customer, _ = self.build_ar()
        bank = Account.objects.get(entity=entity, code="1100")

        invoice = self.make_invoice(entity, customer, lines=[("4100", 1, 50000, None)])
        post_invoice(invoice)
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=10000, deposit_account=bank,
        )
        post_payment(payment, auto_allocate=False)
        note = CreditNote.objects.create(
            entity=entity, customer=customer, kind=CreditNoteKind.CREDIT,
            note_date=datetime.date(2026, 1, 15),
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=entity, code="4900"),
            quantity=1, unit_price=5000, line_no=1,
        )
        post_credit_note(note, auto_allocate=False)
        refund = Refund.objects.create(
            entity=entity, customer=customer, refund_date=datetime.date(2026, 1, 20),
            amount=5000, deposit_account=bank,
        )
        post_refund(refund)
        concession = Concession.objects.create(
            entity=entity, customer=customer, invoice=invoice,
            concession_date=datetime.date(2026, 1, 20), amount=5000,
        )
        post_concession(concession)
        return entity, (invoice, payment, note, refund, concession)

    def test_each_endpoint_requires_its_reverse_permission(self):
        entity, documents = self._posted_documents()
        user = self._user(email="void-denied@test.com")
        for (_, view, path), document in zip(self.VIEW_SPECS, documents):
            with self.subTest(path=path):
                response = self._call(view, path, entity, document.pk, user)
                self.assertEqual(response.status_code, 403)

    def test_each_endpoint_hides_another_tenants_document(self):
        from vs_tenants.models import Tenant

        entity, documents = self._posted_documents()
        foreign_tenant = Tenant.objects.create(name="Foreign", slug="foreign-void")
        foreign_entity = LedgerEntity.objects.create(
            name="Foreign Books", code="FVOID", tenant=foreign_tenant,
        )
        user = self._user(
            *(spec[0] for spec in self.VIEW_SPECS), email="void-isolation@test.com",
        )
        for (_, view, path), document in zip(self.VIEW_SPECS, documents):
            with self.subTest(path=path):
                response = self._call(view, path, foreign_entity, document.pk, user)
                self.assertEqual(response.status_code, 404)

    def test_payment_endpoint_returns_existing_serializer_shape(self):
        entity, _, customer, _ = self.build_ar()
        payment = Payment.objects.create(
            entity=entity, customer=customer, payment_date=datetime.date(2026, 1, 15),
            amount=10000, deposit_account=Account.objects.get(entity=entity, code="1100"),
        )
        post_payment(payment, auto_allocate=False)
        user = self._user("finance.payment.reverse", email="void-payment@test.com")
        response = self._call(
            "PaymentVoidView", "payments", entity, payment.pk, user,
            {"reversal_date": "2026-01-20"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {"success", "message", "data"})
        self.assertTrue(response.data["success"])
        self.assertEqual(
            set(response.data["data"]),
            {
                "id", "document_number", "customer_id", "customer_code", "customer_name",
                "payment_date", "method", "amount", "amount_naira", "allocated_amount",
                "unallocated_amount", "refunded_amount", "credit_remaining",
                "allocation_status", "deposit_account_code", "deposit_account_name",
                "reference", "narration", "journal_id", "status",
            },
        )
        self.assertEqual(response.data["data"]["status"], DocumentStatus.REVERSED)
        self.assertEqual(response.data["data"]["id"], payment.pk)

    def test_raw_journal_endpoint_returns_actionable_refusal_envelope(self):
        from .views import JournalReverseView

        entity, documents = self._posted_documents()
        invoice = documents[0]
        user = self._user("finance.journal.reverse", email="raw-journal-refusal@test.com")
        request = APIRequestFactory().post(
            f"/v1/finance/journals/{invoice.journal_id}/reverse/?entity={entity.code}",
            {}, format="json",
        )
        force_authenticate(request, user=user)
        request.tenant = user.tenant
        request.rbac_tenant = user.tenant
        response = JournalReverseView.as_view()(request, id=invoice.journal_id)
        response.render()

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["error"]["code"], "POSTING_ERROR")
        self.assertIn(invoice.document_number, response.data["message"])
        self.assertIn(f"/finance/invoices/{invoice.pk}/void/", response.data["message"])


class VoidPermissionSeedTests(TestCase):
    def test_seed_registers_all_five_critical_reverse_permissions_idempotently(self):
        from django.core.management import call_command
        from vs_rbac.models import Permission

        call_command("seed_actions", verbosity=0)
        call_command("seed_finance_permissions", verbosity=0)
        call_command("seed_finance_permissions", verbosity=0)
        for resource in ("invoice", "payment", "creditnote", "refund", "concession"):
            permission = Permission.objects.get(key=f"finance.{resource}.reverse")
            self.assertEqual(permission.sensitivity_level, "CRITICAL")
            self.assertTrue(permission.is_restricted)
