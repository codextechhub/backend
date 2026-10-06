"""A ledger entity's kind reads in words.

The finance settings and setup screens printed an entity's kind as its stored
code lower-cased ("tenant entity"). The entity payload carries ``kind_label``,
the kind as the model names it, so a screen shows that instead.
"""
from django.test import TestCase

from vs_finance.models import LedgerEntity
from vs_finance.seed import seed_currencies
from vs_finance.serializers import LedgerEntitySerializer


class LedgerEntityKindLabelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        seed_currencies()
        cls.entity = LedgerEntity.objects.create(
            name="Lagoon View Product", code="LVPRD", kind=LedgerEntity.Kind.PRODUCT,
        )

    def test_the_kind_has_its_label_beside_its_code(self):
        data = LedgerEntitySerializer(self.entity).data
        self.assertEqual(data["kind"], "PRODUCT")
        self.assertEqual(data["kind_label"], "Product")
