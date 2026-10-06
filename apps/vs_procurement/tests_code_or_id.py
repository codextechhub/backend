"""Procurement reads a cost centre and a tax code the way finance does.

The pickers send a cost centre's code, and a school may number its cost centres
("100 Administration", "200 Academics"). A digit string is therefore a code
first; only a JSON number is an id. Each test builds the collision on purpose:
a decoy coded with the target's id.
"""
from __future__ import annotations

from django.test import TestCase

from vs_finance.models import CostCenter, LedgerEntity, TaxCode
from vs_procurement.views.base import _resolve_cost_center, _resolve_tax


class CodeOrIdResolverTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.entity = LedgerEntity.objects.create(
            name="Corona Books", code="CORONA", kind=LedgerEntity.Kind.TENANT,
        )

    def test_a_numbered_cost_centre_is_found_by_its_code(self):
        target = CostCenter.objects.create(entity=self.entity, code="ADMIN", name="Admin")
        numbered = CostCenter.objects.create(
            entity=self.entity, code=str(target.pk), name="Numbered centre",
        )

        self.assertEqual(_resolve_cost_center(self.entity, str(target.pk)), numbered)
        self.assertEqual(_resolve_cost_center(self.entity, target.pk), target)

    def test_a_tax_code_id_is_not_read_as_another_codes_code(self):
        target = TaxCode.objects.create(entity=self.entity, code="VAT", name="VAT", rate_bps=750)
        decoy = TaxCode.objects.create(
            entity=self.entity, code=str(target.pk), name="Decoy", rate_bps=0,
        )

        self.assertEqual(_resolve_tax(self.entity, target.pk), target)
        self.assertEqual(_resolve_tax(self.entity, str(target.pk)), decoy)
