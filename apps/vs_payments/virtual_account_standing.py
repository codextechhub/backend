"""Noticing a dedicated account its provider no longer holds for its customer.

A dedicated account is stored once, when it is issued, and offered from then on:
on the pupil's Pay screen, the parent's, and every statement. Nothing at the
provider tells us when the number stops being ours. A live account can be
closed by the provider or its bank, and a test key's numbers come from a shared
pool that the provider hands on to other merchants' customers. Either way a
family paying into the number pays somebody else, and nobody here finds out.

:func:`check_virtual_accounts` asks each provider once for every account it
holds open for us, a page per hundred accounts, rather than once per account.
An account missing from that list, or listed against another customer, is then
asked about on its own, and only an account the provider confirms is closed,
gone or somebody else's is replaced
(:func:`vs_payments.services.replace_withdrawn_virtual_account`).

A run that would replace more than :data:`MAX_SHARE` of a provider's accounts
(and more than :data:`MIN_CAP`) replaces none of them and says so: that is a
provider answering wrongly, not a provider withdrawing a tenth of its numbers
overnight, and reissuing every family's number on its word would be the
larger harm.
"""
from __future__ import annotations

import logging

from .constants import VirtualAccountStatus
from .exceptions import ProviderError, ProviderNotConfiguredError
from .models import VirtualAccount
from .providers.registry import get_provider

logger = logging.getLogger("vs_payments.virtual_account_standing")

MAX_SHARE = 0.10
MIN_CAP = 5


def _listed_for_someone_else(va, client, owners) -> bool:
    """Whether the provider's listing leaves ``va`` off, or lists it against another customer."""
    if va.provider_reference not in owners:
        return True
    ours, _ = client.virtual_account_customer(va.raw)
    listed = owners[va.provider_reference]
    return bool(ours and listed and ours != listed)


def _confirmed_withdrawn(va, client) -> bool:
    """Whether the provider, asked about ``va`` alone, says it is closed, gone or another's."""
    standing = client.virtual_account_standing(provider_reference=va.provider_reference)
    if standing is None or not standing.active:
        return True
    ours, _ = client.virtual_account_customer(va.raw)
    return bool(ours and standing.customer_code and ours != standing.customer_code)


def _check_provider(provider_name, summary) -> None:
    from .services import replace_withdrawn_virtual_account

    try:
        client = get_provider(provider_name)
        owners = client.open_virtual_accounts()
    except (ProviderNotConfiguredError, NotImplementedError):
        return
    except ProviderError:
        logger.warning("Could not list %s virtual accounts.", provider_name, exc_info=True)
        summary["unanswered"].append(provider_name)
        return

    ours = list(
        VirtualAccount.objects.filter(provider=provider_name, status=VirtualAccountStatus.ACTIVE,
                                      customer__isnull=False)
        .exclude(provider_reference="").select_related("customer", "entity")
    )
    summary["checked"] += len(ours)
    suspects = [va for va in ours if _listed_for_someone_else(va, client, owners)]
    cap = max(MIN_CAP, int(len(ours) * MAX_SHARE))
    if len(suspects) > cap:
        logger.error("%s: %s of %s virtual accounts look withdrawn; none replaced.",
                     provider_name, len(suspects), len(ours))
        summary["refused"].append({"provider": provider_name, "suspects": len(suspects), "of": len(ours)})
        return

    for va in suspects:
        try:
            if not _confirmed_withdrawn(va, client):
                continue
        except ProviderError:
            logger.warning("Could not confirm virtual account %s.", va.pk, exc_info=True)
            continue
        new = replace_withdrawn_virtual_account(va)
        summary["withdrawn"].append(va.pk)
        if new is not None:
            summary["replaced"].append(new.pk)


def check_virtual_accounts() -> dict:
    """Replace every active dedicated account its provider no longer holds for its customer.

    Returns ids only: the accounts found withdrawn, their replacements, the
    providers that did not answer, and any run held back by the safety limit.
    """
    summary = {"checked": 0, "withdrawn": [], "replaced": [], "unanswered": [], "refused": []}
    providers = (
        VirtualAccount.objects.filter(status=VirtualAccountStatus.ACTIVE)
        .order_by().values_list("provider", flat=True).distinct()
    )
    for provider_name in sorted(set(providers)):
        _check_provider(provider_name, summary)
    return summary
