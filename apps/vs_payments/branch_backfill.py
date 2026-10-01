"""The payment gateway's records, as the branch backfill derives them.

Every gateway record carries a branch, set when it is created: a collection its
:func:`vs_payments.services.collection_branch_id`, a virtual account its
customer's or its deposit bank's, a payout instruction its source bank
account's and a payout batch its lines' one branch. Rows written before those
columns existed are blank, and the backfill fills them from the same facts, in
the same order, and records each change.

An online payment for an invoice belongs to the invoice's branch, so a
collection reads its invoice first. A payout reads the supplier payment it
settles, named by a loose id because payments does not import procurement,
then the bank account the money leaves from. A batch reads its payouts, and only
when they all agree: its own source account is not consulted, because a batch is
reached by its branch and its detail lists every line, so a batch whose lines
left Ikeja's and Lekki's banks, given Ikeja because it names Ikeja's account,
would show Lekki's payouts to Ikeja's clerk. Such a batch is left for an
administrator.

A held settlement's journal (Dr bank, Dr bank charges, Cr gateway clearing in the
tenant's books) belongs to the settlement and carries its branch, and a
chargeback's entry in the tenant's books belongs to its held movement and carries
the movement's branch. A held movement's platform journal is not registered: it is
the platform's own entry, always raised in the platform's branch, never the client
branch the movement is for.
"""
from __future__ import annotations

from django.apps import apps

from vs_finance.branch_derivation import (
    JournalOwner, Target, agreeing, bank_behind, customer, register_journal_owner,
    register_target, via,
)

VIRTUAL_ACCOUNT = "vs_payments.VirtualAccount"
PAYOUT_INSTRUCTION = "vs_payments.PayoutInstruction"
VENDOR_PAYMENT = "vs_procurement.VendorPayment"

_vendor_payment = (
    (via("the supplier payment", "vendor_payment_id", VENDOR_PAYMENT),)
    if apps.is_installed("vs_procurement") else ()
)

_TARGETS = (
    Target(
        "vs_payments.CollectionIntent",
        (
            via("the invoice", "invoice", "vs_finance.Invoice"),
            via("the payment", "payment", "vs_finance.Payment"),
            customer(),
            bank_behind("the deposit bank account", "deposit_account"),
        ),
        order=950,
    ),
    Target(
        VIRTUAL_ACCOUNT,
        (customer(), bank_behind("the deposit bank account", "deposit_account")),
        order=950,
    ),
    Target(
        PAYOUT_INSTRUCTION,
        (*_vendor_payment, bank_behind("the source bank account", "source_account")),
        order=950,
    ),
    Target(
        "vs_payments.PayoutBatch",
        (agreeing("the batch's payouts", PAYOUT_INSTRUCTION, "batch", ("id", PAYOUT_INSTRUCTION)),),
        order=960,
    ),
)

for _target in _TARGETS:
    register_target(_target)

register_journal_owner(JournalOwner("vs_payments.HeldSettlement", "settlement_journal"))
register_journal_owner(JournalOwner("vs_payments.HeldMovement", "tenant_journal"))
