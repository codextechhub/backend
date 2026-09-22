"""No vendor contact or bank detail reaches a caller who may read none of them.

The declared surface of ``procurement.vendor`` is rendered on a real supplier
whose contact, tax and bank details are all filled in and who has a contact
person, as a caller whose role has every vendor field switched off. The whole
payload is walked, so the contact people nested under the vendor are held to
their own switch rather than escaping below it.

Two tenants: one with two branches and one with a single branch.
"""
from __future__ import annotations

from django.test import TestCase

from vs_finance.models import LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_procurement.models import Vendor, VendorContact
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school

_VENDOR = "vs_procurement.serializers.VendorSerializer"


class VendorDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({"procurement.vendor"})
    covers = frozenset({_VENDOR})

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        multi = make_school(slug="deep-proc-multi", name="Corona Group").tenant
        make_branch(multi, name="Ikeja Branch")
        make_branch(multi, name="Lekki Branch", is_main=False)
        solo = make_school(slug="deep-proc-solo", name="Single Site").tenant
        make_branch(solo, name="Main Branch")
        cls.tenant = multi
        cls.vendors = [(multi, cls._vendor(multi, "DPPMULTI")), (solo, cls._vendor(solo, "DPPSOLO"))]

    @classmethod
    def _vendor(cls, tenant, code):
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        vendor = Vendor.objects.create(
            entity=entity, code="ADE", name="Ade Stationers",
            email="accounts@ade.example", phone="08030000000",
            address="12 Broad Street, Lagos", tax_id="TIN-2233",
            bank_name="GTBank", bank_code="058",
            bank_account_number="0123456789", bank_account_name="Ade Stationers Ltd",
        )
        VendorContact.objects.create(
            vendor=vendor, name="Amina Bello", email="amina@ade.example",
            phone="08030000001", is_primary=True,
        )
        return vendor

    def samples(self):
        return [
            Sample(_VENDOR, Vendor.objects.prefetch_related("contacts").get(pk=vendor.pk),
                   tenant=tenant)
            for tenant, vendor in self.vendors
        ]
