"""The payments screens the search bar can route to.

Payments is the money-movement half of the finance product and answers to the
``finance`` capability (:mod:`vs_rbac.capability_map`). The platform's own
provider settings are platform-only and answer to no tenant capability.

The custody screen is offered to holders of ``payments.settings.view`` only,
as one screen: its three settings (the mode, the settlement interval and the
clearing window) have no labels in code to name a setting place by.
Its read endpoint also opens to ``payments.payout.view``, but a payout reader
is answered the custody mode alone, which the payouts screens already show,
so routing them to a settings screen they cannot read would be a dead end.

Payouts and payout batches exist only while the tenant's online payments are
held and paid out (custody mode ``HELD``): a tenant paid directly into its
branches' banks has nothing to pay out. Their places name the
``payments.held_custody`` condition, which asks
:func:`vs_payments.custody.custody_mode`, the function the custody screen and
the payout endpoints read the mode from.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register, register_condition

CAPABILITY = "finance"
REPORT_VIEW = "payments.report.view"
HELD_CUSTODY = "payments.held_custody"


def custody_is_held(*, request, tenant, branch, user) -> bool:
    """Whether *tenant*'s online payments are held and paid out today."""
    from .constants import CustodyMode
    from .custody import custody_mode

    return tenant is not None and custody_mode(tenant) == CustodyMode.HELD


register_condition(HELD_CUSTODY, custody_is_held)

register(
    Place(
        kind=ACTION, key="payments.settings.custody", label="Payment custody settings",
        description="Whether online payments go straight to your own account or are held and settled.",
        keywords=("online payments", "settlement", "payment settings"),
        screen="payments.settings.custody", permission="payments.settings.view",
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="payments.platform_provider_settings",
        label="Platform payment provider settings",
        description="How the platform's own merchant account at the payment provider behaves.",
        screen="payments.platform_provider_settings",
        permission="payments.platform_provider.view",
    ),
    Place(
        kind=ACTION, key="payments.collections", label="Collections",
        description="Online payments received.", keywords=("online payments",),
        screen="payments.collections", permission="payments.collection.view",
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="payments.payouts", label="Payouts",
        description="Held money paid out to the branch bank accounts.",
        screen="payments.payouts", permission="payments.payout.view",
        capability=CAPABILITY, condition=HELD_CUSTODY,
    ),
    Place(
        kind=ACTION, key="payments.payout_batches", label="Payout batches",
        description="Payouts grouped into one transfer run.",
        keywords=("payouts", "settlement"),
        screen="payments.payout_batches", permission="payments.payout.view",
        capability=CAPABILITY, condition=HELD_CUSTODY,
    ),
    Place(
        kind=ACTION, key="payments.settlement", label="Settlement reconciliation",
        description="Whether what the provider settled matches what was collected.",
        keywords=("settlement",),
        screen="payments.settlement", permission=REPORT_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="payments.transactions", label="Transactions log",
        description="Every online payment event, in order.",
        screen="payments.transactions", permission=REPORT_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="payments.provider_activity", label="Payment provider activity",
        description="Money moved at the payment provider.",
        keywords=("movements",),
        screen="payments.provider_activity", permission=REPORT_VIEW, capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="payments.needs_attention", label="Payments needing attention",
        description="Provider notices the platform could not apply on its own.",
        keywords=("webhooks", "failed"),
        screen="payments.needs_attention", permission="payments.webhook.view",
        capability=CAPABILITY,
    ),
    Place(
        kind=ACTION, key="payments.unattributed_webhooks", label="Provider webhooks",
        description="Provider notices that name no school.",
        keywords=("webhooks",),
        screen="payments.unattributed_webhooks",
        permission="payments.unattributed_webhook.view",
    ),
    Place(
        kind=ACTION, key="payments.virtual_accounts", label="Virtual accounts",
        description="Dedicated account numbers customers pay into.",
        screen="payments.virtual_accounts", permission="payments.virtual_account.view",
        capability=CAPABILITY,
    ),
)
