"""No gateway account, beneficiary, terminal or payer detail reaches a caller who may read none.

Every declared surface of ``payments.virtual_account``, ``payments.payout``,
``payments.collection``, ``payments.refund``, ``payments.terminal``,
``payments.terminal_destination`` and ``payments.terminal_payment`` is rendered
on a real record whose registered fields are filled in, as a caller whose role
has every field of that resource switched off, and the whole payload is walked.
The terminal payment is rendered matched and carrying suggested customers, which
are the places a customer's name could travel below the top of the payload.

Two tenants: one with two branches and one with a single branch.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from vs_finance.models import Account, BankAccount, Customer, LedgerEntity, Refund
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school
from vs_tenants.models import Branch
from vs_user.models import User

from .constants import CollectionOrigin, PayoutStatus, TerminalPaymentStatus, TerminalStatus
from .held import platform_books
from .models import (
    CollectionIntent,
    PayoutInstruction,
    ProviderBalanceTopUp,
    ProviderRefund,
    TerminalPayment,
    VirtualAccount,
    VirtualTerminal,
)

_SURFACE = "vs_payments.serializers."


class PaymentsDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({
        "payments.virtual_account", "payments.payout",
        "payments.collection", "payments.refund",
        "payments.terminal", "payments.terminal_destination", "payments.terminal_payment",
        "payments.platform_topup",
    })
    covers = frozenset({
        _SURFACE + "VirtualAccountSerializer",
        _SURFACE + "PayoutInstructionSerializer",
        _SURFACE + "CollectionIntentSerializer",
        _SURFACE + "ProviderRefundSerializer",
        _SURFACE + "VirtualTerminalSerializer",
        _SURFACE + "TerminalDestinationSerializer",
        _SURFACE + "TerminalPaymentSerializer",
        _SURFACE + "ProviderBalanceTopUpSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        multi = make_school(slug="deep-pay-multi", name="Corona Group").tenant
        ikeja = make_branch(multi, name="Ikeja Branch")
        make_branch(multi, name="Lekki Branch", is_main=False)
        solo = make_school(slug="deep-pay-solo", name="Single Site").tenant
        main = make_branch(solo, name="Main Branch")
        cls.tenant = multi
        cls.platform_tenant, cls.topup = cls._platform_topup()
        cls.records = [
            (multi, *cls._rows(multi, ikeja, "DPYMULTI", "9900112233")),
            (solo, *cls._rows(solo, main, "DPYSOLO", "9900112244")),
        ]

    @classmethod
    def _platform_topup(cls):
        """A top-up in the platform's own books, filled in, with the person who recorded it."""
        platform = platform_books()
        branch = Branch.all_objects.filter(tenant=platform.tenant).order_by("-is_main", "pk").first()
        gl = Account.objects.create(
            entity=platform, code="1149", name="Platform Zenith", account_type="ASSET",
            is_postable=True)
        bank = BankAccount.objects.create(
            entity=platform, name="Platform Zenith", branch=branch, gl_account=gl,
            bank_name="Zenith", account_number="0123456789")
        recorder = User.objects.create_user(
            email="recorder@deep-payload.test", password="testpass123", tenant=platform.tenant,
            status="ACTIVE", first_name="Ngozi", last_name="Eze")
        topup = ProviderBalanceTopUp.objects.create(
            entity=platform, branch=branch, provider="PAYSTACK", amount=200_000_000,
            topped_up_on=datetime.date(2026, 1, 20), bank_account=bank, reference="DPY-TOPUP-1",
            note="Funds the platform's own payouts.", created_by=recorder)
        return platform.tenant, topup

    @classmethod
    def _rows(cls, tenant, branch, code, number):
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
        parent = Customer.objects.create(
            entity=entity, code=f"C{code}", name="Parent",
            receivable_account=Account.objects.get(entity=entity, code="1200"))
        collection = CollectionIntent.objects.create(
            entity=entity, provider="PAYSTACK", reference=f"{code}-COL-1", amount=50_000_00,
            currency=entity.base_currency, customer=parent, origin=CollectionOrigin.DESK,
            status="SUCCEEDED")
        refund = Refund.objects.create(
            entity=entity, customer=parent, method="ONLINE", amount=10_000_00,
            refund_date=datetime.date(2026, 1, 20))
        provider_refund = ProviderRefund.objects.create(
            entity=entity, refund=refund, collection=collection, provider="PAYSTACK",
            amount=10_000_00, status="FAILED", provider_status="failed",
            provider_reference="RF-77", transaction_reference=collection.reference,
            failure_reason="The payer's bank refused it.")

        terminal = VirtualTerminal.objects.create(
            tenant=tenant, branch=branch, provider="PAYSTACK", code=f"VT_{code}",
            name=f"{tenant.name} - {branch.name}", status=TerminalStatus.ACTIVE,
            last_error="An earlier attempt failed.",
            destinations=[{"name": "Head Teacher", "number": "2348055500001"}],
            split_code="SPL_X", split_subaccount="ACCT_X",
            provider_details={"paymentMethods": [{"type": "dedicated_account", "bank": "Wema"}]},
        )
        payer = Customer.objects.create(
            entity=entity, code=f"D{code}", name="Ada Okeye", billing_email="ada@example.com",
            branch=branch,
        )
        intent = CollectionIntent.objects.create(
            entity=entity, branch=branch, provider="PAYSTACK", channel="TERMINAL",
            reference=f"{code}-TRM-1", amount=150_000, fee=1_500, currency=entity.base_currency,
            payer_email="ada@example.com", payer_name="ADA OKEYE", status="SUCCEEDED",
            customer=payer,
        )
        terminal_payment = TerminalPayment.objects.create(
            tenant=tenant, entity=entity, branch=branch, terminal=terminal, collection=intent,
            status=TerminalPaymentStatus.MATCHED, match_method="AUTO", customer=payer,
            payer_email="ada@example.com", sender_name="ADA OKEYE", sender_bank="Zenith Bank",
            match_note="Matched.",
        )
        suggestions = [{"customer": payer.pk, "code": payer.code, "name": payer.name,
                        "open_balance": 100_000, "email_match": True, "reasons": ["email"]}]
        return (account, payout, collection, provider_refund, terminal, terminal_payment,
                suggestions)

    def samples(self):
        samples = []
        for (tenant, account, payout, collection, provider_refund, terminal, terminal_payment,
             suggestions) in self.records:
            samples += [
                Sample(_SURFACE + "VirtualAccountSerializer", account, tenant=tenant),
                Sample(_SURFACE + "PayoutInstructionSerializer", payout, tenant=tenant),
                Sample(_SURFACE + "CollectionIntentSerializer", collection, tenant=tenant),
                Sample(_SURFACE + "ProviderRefundSerializer", provider_refund, tenant=tenant),
                Sample(_SURFACE + "VirtualTerminalSerializer", terminal, tenant=tenant),
                Sample(_SURFACE + "TerminalDestinationSerializer", terminal.destinations,
                       tenant=tenant, many=True),
                Sample(_SURFACE + "TerminalPaymentSerializer", terminal_payment, tenant=tenant,
                       context={"suggestions": {terminal_payment.pk: suggestions}}),
            ]
        samples.append(Sample(_SURFACE + "ProviderBalanceTopUpSerializer", self.topup,
                              tenant=self.platform_tenant))
        return samples
