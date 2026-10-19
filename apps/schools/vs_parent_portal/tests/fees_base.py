"""Brightfield's books, on top of the Nwosu, Yusuf and Ade families.

Mrs. Nwosu's three pupils each owe a ₦3,000.00 tuition invoice; Chiamaka and
Tobi also owe a ₦500.00 levy, which is below the school's smallest part payment
(the default ₦1,000.00) and so can only be paid whole. Mrs. Yusuf's Zainab owes
a tuition invoice of her own. Sunrise (one branch) has books of no kind: Bola
Ade has never been billed.

The invoices are raised through the fee-term bridge and the receipts through the
finance service, as the pupil portal's tests do, so every figure here is read
back through the same finance abstraction layer a real request uses.
"""
from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.core.fal import registry as fal_registry
from schools.core.fal.adapters.django_finance import (
    DjangoEntityResolverAdapter,
    DjangoFeeTermBridgeAdapter,
    DjangoFinanceReadAdapter,
)
from schools.core.fal.contracts import FinanceResult, Unavailable
from vs_payments.providers import registry as provider_registry
from vs_payments.providers.fake import FakeProvider

from .base import ParentFixture


class BooksHelpers:
    """What the school's finance staff would have done: a fee structure, a bill, a receipt.

    The same constructions the FAL's own fixture makes, kept here because that
    fixture builds two schools of its own and names one helper ``account``,
    which this module's fixtures already use for a user.
    """

    @classmethod
    def ledger_account(cls, entity_ref, code):
        from vs_finance.models import Account

        return Account.objects.get(entity_id=entity_ref, code=code)

    @classmethod
    def fee_structure(cls, books, *, code, amount):
        from vs_finance.models import FeeItem, FeeStructure

        structure = FeeStructure.objects.create(
            entity_id=books.entity_ref, code=code, name=code.title(), branch=None,
        )
        FeeItem.objects.create(
            structure=structure, line_no=1, description=code.title(),
            revenue_account=cls.ledger_account(books.entity_ref, "4100"), amount=amount,
        )
        return structure

    @classmethod
    def term_around_today(cls, school):
        """A school year and its first term, both running on the school's today.

        The invoices raised here are dated today, and a bill outside its term is
        refused, so the term is spanned around today rather than pinned to dates.
        """
        from schools.vs_academics.models import AcademicSession, AcademicTerm
        from vs_config.clock import tenant_today

        today = tenant_today(school.tenant)
        session = AcademicSession.all_objects.create(
            tenant=school.tenant, name="Billing year",
            start_date=today - dt.timedelta(days=120), end_date=today + dt.timedelta(days=210),
        )
        term = AcademicTerm.all_objects.create(
            tenant=school.tenant, session=session, name="Billing term", order_index=1,
            start_date=today - dt.timedelta(days=30), end_date=today + dt.timedelta(days=75),
        )
        return session, term

    @classmethod
    def student_customer(cls, books, student_ref, *, branch):
        from schools.core.fal.adapters.django_finance import DjangoStudentCustomerAdapter

        return DjangoStudentCustomerAdapter().ensure_customer(
            student_ref, entity_ref=books.entity_ref, name=f"Parent of {student_ref}",
            branch_ref=branch.pk,
        ).unwrap()

    @classmethod
    def pay(cls, books, customer_ref, amount, *, invoice, when=None):
        """A real posted receipt against *invoice*, through the finance service."""
        from vs_finance.models import Customer, Payment
        from vs_finance.receivables import post_payment

        customer = Customer.objects.get(pk=customer_ref)
        payment = Payment.objects.create(
            entity_id=books.entity_ref, customer=customer,
            payment_date=when or invoice.invoice_date, branch_id=customer.branch_id,
            amount=amount, deposit_account=cls.ledger_account(books.entity_ref, "1100"),
        )
        post_payment(payment, allocations=[(invoice, amount)], auto_allocate=False)
        payment.refresh_from_db()
        return payment


class UnreadableChildReader(DjangoFinanceReadAdapter):
    """The real finance reader, except that it cannot read the named children's books."""

    def __init__(self, *student_refs):
        self.unreadable = {str(ref) for ref in student_refs}

    def fee_status(self, student_ref):
        if str(student_ref) in self.unreadable:
            return FinanceResult.unavailable(Unavailable.BACKEND_UNAVAILABLE)
        return super().fee_status(student_ref)

    def invoices_for(self, student_ref, include_history=True):
        if str(student_ref) in self.unreadable:
            return FinanceResult.unavailable(Unavailable.BACKEND_UNAVAILABLE)
        return super().invoices_for(student_ref, include_history)


class ParentBooksFixture(BooksHelpers, ParentFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Invoice

        cls.books = DjangoEntityResolverAdapter().provision_entity(
            cls.school.pk, code="BRIGHTFIELD", name="Brightfield Schools",
        ).unwrap()
        bridge = DjangoFeeTermBridgeAdapter()
        session, term = cls.term_around_today(cls.school)
        tuition = cls.fee_structure(cls.books, code="TUITION", amount=300_000)
        levy = cls.fee_structure(cls.books, code="LEVY", amount=50_000)
        for structure in (tuition, levy):
            bridge.link_term(structure.pk, session.pk, term.pk)

        for pupil in (cls.chiamaka, cls.tobi, cls.folu, cls.zainab):
            cls.student_customer(cls.books, str(pupil.pk), branch=pupil.branch)
        bridge.generate_cohort_invoices(
            tuition.pk, tuple(str(p.pk) for p in (cls.chiamaka, cls.tobi, cls.folu, cls.zainab)),
        )
        bridge.generate_cohort_invoices(
            levy.pk, tuple(str(p.pk) for p in (cls.chiamaka, cls.tobi)),
        )

        by_pupil = {}
        for row in Invoice.objects.filter(
            customer__source_id__in=[str(p.pk) for p in (cls.chiamaka, cls.tobi, cls.folu, cls.zainab)],
        ).order_by("pk"):
            by_pupil.setdefault(row.customer.source_id, []).append(row)
        cls.chiamaka_tuition, cls.chiamaka_levy = by_pupil[str(cls.chiamaka.pk)]
        cls.tobi_tuition, cls.tobi_levy = by_pupil[str(cls.tobi.pk)]
        (cls.folu_tuition,) = by_pupil[str(cls.folu.pk)]
        (cls.zainab_tuition,) = by_pupil[str(cls.zainab.pk)]

    def setUp(self):
        super().setUp()
        self.fake = FakeProvider(secret="test-secret")
        provider_registry.register("PAYSTACK", self.fake)
        provider_registry.register("FAKE", self.fake)
        self.addCleanup(provider_registry.unregister)
        fal_registry.reset()
        self.addCleanup(fal_registry.reset)

    # ── requests ─────────────────────────────────────────────────────────

    def as_parent(self, user=None):
        return TenantAPIClient(user or self.nwosu_user)

    def read(self, name, user=None, params=None, **kwargs):
        return self.as_parent(user).get(reverse(name, kwargs=kwargs or None), params or {})

    def pay_invoice(self, invoice, amount, user=None):
        url = reverse("parent-portal-pay", kwargs={"invoice_ref": invoice.pk})
        return self.as_parent(user).post(url, {"amount": amount}, format="json")

    # ── school choices ───────────────────────────────────────────────────

    def set_config(self, key, value):
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        call_command("seed_config_catalogue", verbosity=0)
        definition = ConfigurationDefinition.objects.get(key=key)
        set_value(definition=definition, value=value, actor=self.admin, tenant=self.tenant)

    def due(self, invoice, days):
        """Move an invoice's due date to *days* from today, as a school's terms would."""
        from vs_config.clock import tenant_today
        from vs_finance.models import Invoice

        Invoice.objects.filter(pk=invoice.pk).update(
            due_date=tenant_today(self.tenant) + dt.timedelta(days=days),
        )
