"""The payment gateway's records, as the branch backfill would derive them.

None of these models carries a branch column, so each is registered with
``has_branch_column=False``: the audit reports how many of their rows would
derive a branch and how many would need an administrator, and the backfill
writes nothing to them. That report is the evidence a later schema change needs.

An online payment for an invoice belongs to the invoice's branch, so a
collection reads its invoice first. A payout reads the supplier payment it
settles, named by a loose id because payments does not import procurement,
then the bank account the money leaves from.
"""
from __future__ import annotations

from django.apps import apps

from vs_finance.branch_derivation import Target, agreeing, bank_behind, customer, register_target, via

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
        order=950, has_branch_column=False,
    ),
    Target(
        VIRTUAL_ACCOUNT,
        (customer(), bank_behind("the deposit bank account", "deposit_account")),
        order=950, has_branch_column=False,
    ),
    Target(
        PAYOUT_INSTRUCTION,
        (*_vendor_payment, bank_behind("the source bank account", "source_account")),
        order=950, has_branch_column=False,
    ),
    Target(
        "vs_payments.PayoutBatch",
        (
            agreeing("the batch's payouts", PAYOUT_INSTRUCTION, "batch", ("id", PAYOUT_INSTRUCTION)),
            bank_behind("the source bank account", "source_account"),
        ),
        order=960, has_branch_column=False,
    ),
)

for _target in _TARGETS:
    register_target(_target)
