"""No gateway account or beneficiary detail reaches a caller who may read none.

Every declared surface of ``payments.virtual_account`` and ``payments.payout``
is rendered on a real record whose registered fields are filled in, as a caller
whose role has every field of that resource switched off, and the whole
payload is walked.

Two tenants: one with two branches and one with a single branch.
"""
from __future__ import annotations

from django.test import TestCase

from vs_finance.models import LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school

from .constants import PayoutStatus
from .models import PayoutInstruction, VirtualAccount

_SURFACE = "vs_payments.serializers."


class PaymentsDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({"payments.virtual_account", "payments.payout"})
    covers = frozenset({
        _SURFACE + "VirtualAccountSerializer",
        _SURFACE + "PayoutInstructionSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        multi = make_school(slug="deep-pay-multi", name="Corona Group").tenant
        make_branch(multi, name="Ikeja Branch")
        make_branch(multi, name="Lekki Branch", is_main=False)
        solo = make_school(slug="deep-pay-solo", name="Single Site").tenant
        make_branch(solo, name="Main Branch")
        cls.tenant = multi
        cls.records = [
            (multi, *cls._rows(multi, "DPYMULTI", "9900112233")),
            (solo, *cls._rows(solo, "DPYSOLO", "9900112244")),
        ]

    @classmethod
    def _rows(cls, tenant, code, number):
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        account = VirtualAccount.objects.create(
            entity=entity, provider="FAKE", customer=None, account_number=number,
            bank_name="Fake MFB", account_name="Corona Group Collections",
        )
        payout = PayoutInstruction.objects.create(
            entity=entity, provider="PAYSTACK", reference=f"{code}-PO-1",
            amount=50_000_00, currency=entity.base_currency,
            beneficiary_name="Ade Stationers Ltd",
            beneficiary_account_number="0123456789", beneficiary_bank_code="058",
            status=PayoutStatus.PROCESSING,
        )
        return account, payout

    def samples(self):
        samples = []
        for tenant, account, payout in self.records:
            samples += [
                Sample(_SURFACE + "VirtualAccountSerializer", account, tenant=tenant),
                Sample(_SURFACE + "PayoutInstructionSerializer", payout, tenant=tenant),
            ]
        return samples
