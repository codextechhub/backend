"""The pupil on the roll, as the branch backfill's fallback for a customer with no branch.

Corona runs Ikeja (its main branch) and Lekki; Greenfield runs one branch. A
Lekki pupil's unbranched invoice must land in Lekki, never in Ikeja because it
is the main branch, and a source id naming another school's pupil must place
nothing.
"""
from __future__ import annotations

import datetime

from schools.core.fal.branch_backfill import PUPIL_ON_THE_ROLL
from schools.core.fal.contracts import SOURCE_TYPE_STUDENT
from schools.core.fal.tests.base import FALFixture
from vs_finance.branch_derivation import plan_entity
from vs_finance.models import Account, Customer, Invoice, LedgerEntity


class PupilOnTheRollTests(FALFixture):
    def customer(self, student, *, branch=None):
        entity = LedgerEntity.objects.get(pk=self.corona_books.entity_ref)
        return Customer.objects.create(
            entity=entity, code=f"S{student.pk}", name="Parent", branch=branch,
            receivable_account=Account.objects.get(entity=entity, code="1200"),
            source_type=SOURCE_TYPE_STUDENT, source_id=str(student.pk),
        )

    def invoice(self, customer):
        row = Invoice.objects.create(
            entity=customer.entity, customer=customer, invoice_date=datetime.date(2026, 9, 10),
        )
        Invoice._base_manager.filter(pk=row.pk).update(branch=None)
        return row

    def assigned(self, row):
        plan = plan_entity(row.entity)
        target = next(p for p in plan.targets if p.target.model_label == "vs_finance.Invoice")
        return target.assign.get(row.pk), next((f for f in target.flags if f.pk == row.pk), None)

    def test_an_invoice_whose_customer_has_no_branch_takes_the_pupils_branch(self):
        invoice = self.invoice(self.customer(self.student(self.corona, self.lekki)))
        self.assertEqual(self.assigned(invoice)[0], (self.lekki.pk, PUPIL_ON_THE_ROLL))

    def test_the_customers_own_branch_comes_first(self):
        invoice = self.invoice(self.customer(self.student(self.corona, self.lekki), branch=self.ikeja))
        self.assertEqual(self.assigned(invoice)[0], (self.ikeja.pk, "the customer"))

    def test_another_schools_pupil_places_nothing(self):
        stranger = self.student(self.greenfield, self.greenfield_main)
        invoice = self.invoice(self.customer(stranger))
        assigned, flag = self.assigned(invoice)
        self.assertIsNone(assigned)
        self.assertEqual(flag.reason, "no branch on the customer")
