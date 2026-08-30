"""Fixtures for the FAL's own tests.

**Two schools, two tenants, always.** A single-tenant fixture cannot fail a
cross-tenant test, so every isolation assertion in this package is written
against Corona and Greenfield and would pass vacuously without both. The second
school is not decoration; it is the assertion.

**Two shapes of school, too.** Corona runs two branches, Greenfield runs one.
Branch defaulting, branch refusal and school-wide records all behave differently
between the two, and the single-branch case is the common one in production.
"""

from __future__ import annotations

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from schools.core.fal import registry
from schools.vs_schools.models import School
from vs_tenants.models import Branch

User = get_user_model()


class FALFixture(TestCase):
    """Two schools with real books, users, and a way to grant a permission."""

    @classmethod
    def setUpTestData(cls):
        from schools.core.fal.adapters.django_finance import (
            DjangoEntityResolverAdapter,
        )

        cls.corona = School.objects.create(
            slug="corona-secondary", name="Corona Secondary", code="COR-1",
            status="ACTIVE",
        )
        cls.greenfield = School.objects.create(
            slug="greenfield", name="Greenfield School", code="GRN-1", status="ACTIVE",
        )
        # Corona runs two sites; Greenfield runs one. Both shapes are exercised.
        cls.ikeja = Branch.objects.create(
            tenant=cls.corona.tenant, name="Ikeja", is_main=True, status="ACTIVE",
        )
        cls.lekki = Branch.objects.create(
            tenant=cls.corona.tenant, name="Lekki", is_main=False, status="ACTIVE",
        )
        cls.greenfield_main = Branch.objects.create(
            tenant=cls.greenfield.tenant, name="Main", is_main=True, status="ACTIVE",
        )

        resolver = DjangoEntityResolverAdapter()
        cls.corona_books = resolver.provision_entity(
            cls.corona.pk, code="CORONA", name="Corona Secondary",
        ).unwrap()
        cls.greenfield_books = resolver.provision_entity(
            cls.greenfield.pk, code="GREENFIELD", name="Greenfield School",
        ).unwrap()

        cls.bursar = cls.user_for(cls.corona, "bursar@corona.test")
        cls.lekki_bursar = cls.user_for(
            cls.corona, "lekki@corona.test", branch=cls.lekki,
        )
        cls.greenfield_bursar = cls.user_for(cls.greenfield, "bursar@greenfield.test")

    def setUp(self):
        super().setUp()
        # The registry caches port instances process-wide, so a test that injects
        # a fake would otherwise leak it into the next one.
        registry.reset()
        self.addCleanup(registry.reset)

    # ----- fixtures -------------------------------------------------------- #
    @classmethod
    def user_for(cls, school, email, *, branch=None, **kwargs):
        return User.objects.create_user(
            email=email, password="testpass123", tenant=school.tenant,
            branch=branch, status="ACTIVE", first_name="Test", last_name="User",
            **kwargs,
        )

    @classmethod
    def grant(cls, user, key, *, branch=None):
        """Give ``user`` ``key`` in their own tenant, through a real role.

        Built out of the canonical RBAC rows rather than a stub, because the
        thing under test is whether the FAL asks the evaluator the right
        question, and a stubbed evaluator could not answer it wrongly.
        """
        from vs_rbac.tests.helpers import (
            make_assignment, make_permission, make_role, make_role_permission,
        )

        permission = make_permission(key)
        role = make_role(user.tenant, name=f"Role for {key}")
        make_role_permission(role, permission)
        make_assignment(user.tenant, user, role, branch=branch)
        return role

    @classmethod
    def account(cls, entity_ref, code):
        from vs_finance.models import Account

        return Account.objects.get(entity_id=entity_ref, code=code)

    @classmethod
    def fee_structure(cls, books, *, code="TUITION", amount=300_000, branch=None):
        from vs_finance.models import FeeItem, FeeStructure

        structure = FeeStructure.objects.create(
            entity_id=books.entity_ref, code=code, name="Tuition", branch=branch,
        )
        FeeItem.objects.create(
            structure=structure, line_no=1, description="Tuition",
            revenue_account=cls.account(books.entity_ref, "4100"), amount=amount,
        )
        return structure

    @classmethod
    def session_and_term(cls, school, *, name="2026/2027"):
        from schools.vs_academics.models import AcademicSession, AcademicTerm

        session = AcademicSession.all_objects.create(
            tenant=school.tenant, name=name,
            start_date=datetime.date(2026, 9, 1), end_date=datetime.date(2027, 7, 31),
        )
        term = AcademicTerm.all_objects.create(
            tenant=school.tenant, session=session, name="First Term", order_index=1,
            start_date=datetime.date(2026, 9, 1), end_date=datetime.date(2026, 12, 15),
        )
        return session, term

    @classmethod
    def student_customer(cls, books, student_ref, *, name=None, branch=None):
        from schools.core.fal.adapters.django_finance import (
            DjangoStudentCustomerAdapter,
        )

        return DjangoStudentCustomerAdapter().ensure_customer(
            student_ref, entity_ref=books.entity_ref,
            name=name or f"Parent of {student_ref}",
            branch_ref=branch.pk if branch else None,
        ).unwrap()

    @classmethod
    def student(cls, school, branch, *, first="Tunde", last="Adeyemi"):
        """A real child on a real roll.

        Module 11 landed, so the FAL's student references are the roll's own
        primary keys and the tests use them rather than the opaque strings that
        stood in while no roll existed.
        """
        import datetime as _dt

        from schools.vs_students.constants import Gender, StudentStatus
        from schools.vs_students.models import Student

        return Student.all_objects.create(
            tenant=school.tenant, branch=branch,
            first_name=first, last_name=last,
            date_of_birth=_dt.date(2014, 3, 1), gender=Gender.MALE,
            status=StudentStatus.ACTIVE,
        )

    @classmethod
    def guardian_of(cls, school, student, *, full_name="Mrs Adeyemi",
                    relationship=None, is_primary=True):
        """A guardian, and the link that is the parent portal's whole authority."""
        from schools.vs_students.constants import Relationship
        from schools.vs_students.models import Guardian, StudentGuardian

        guardian = Guardian.all_objects.create(
            tenant=school.tenant, full_name=full_name, phone="+2348000000000",
        )
        StudentGuardian.all_objects.create(
            tenant=school.tenant, student=student, guardian=guardian,
            relationship=relationship or Relationship.MOTHER,
            is_primary=is_primary,
        )
        return guardian

    @classmethod
    def pay(cls, books, customer_ref, amount, *, invoice=None,
            when=datetime.date(2026, 10, 1)):
        """A real posted receipt, through the finance service, not a hand-built row.

        The branch is inherited from the customer, which is the engine's own rule:
        a receipt continues the family's chain, so the money lands in the branch
        that raised the debt (``vs_finance/views_ar.py``).
        """
        from vs_finance.models import Customer, Payment
        from vs_finance.receivables import post_payment

        customer = Customer.objects.get(pk=customer_ref)
        payment = Payment.objects.create(
            entity_id=books.entity_ref, customer=customer, payment_date=when,
            branch_id=customer.branch_id, amount=amount,
            deposit_account=cls.account(books.entity_ref, "1100"),
        )
        post_payment(
            payment,
            allocations=[(invoice, amount)] if invoice is not None else None,
            auto_allocate=invoice is None,
        )
        payment.refresh_from_db()
        return payment
