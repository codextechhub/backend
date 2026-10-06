"""Where a tenant's online money is held, and the rules each arrangement imposes.

A tenant holds its online money one of two ways (:class:`~vs_payments.constants.CustodyMode`):

* **Held**, where every payment settles to the platform's own provider balance, as
  it always has. Online payouts draw on that balance.
* **Direct**, where each branch's collection bank account has a provider
  subaccount and every checkout and dedicated virtual account names the
  subaccount of the branch the money belongs to, so the provider settles each
  payment straight to that branch's bank, the branch bearing the fee. Nothing
  waits in a provider balance, so there is nothing to pay suppliers from
  online: a direct tenant pays them from its bank and records the payment.

The arrangement binds every branch of the tenant and changes only at the start
of a month. A change is stored as pending with the first day of the next month.
A move to held is in force from that day, and :func:`custody_mode` answers it
without anything having to run at midnight. A move to direct waits for the daily
task (:func:`vs_payments.held.apply_custody_switch`), which first pays out
everything the platform holds for the tenant and takes effect only once nothing
is held. The setting also records the settlement interval: how often a held
tenant is to be paid what the platform holds for it.

A direct tenant's online payments are not refunded online either: the money is
returned from the branch's bank and the refund recorded
(:func:`assert_online_refund_allowed`).

Either way a confirmed payment is first booked to gateway clearing and moves to
a bank when its settlement is matched (:mod:`vs_payments.settlement`).
"""
from __future__ import annotations

from vs_finance.wording import counted

import datetime

from django.db import transaction
from rest_framework.exceptions import ValidationError

from . import audit
from .constants import (
    CLEARING_STALE_DAYS_RANGE,
    SETTLEMENT_INTERVAL_RANGE,
    CustodyMode,
    PaymentAuditAction,
)
from .exceptions import PaymentStateError


class SubaccountMissingError(PaymentStateError):
    """A direct-mode branch has no provider subaccount, so it cannot take online payments."""

    error_code = "COLLECTION_SUBACCOUNT_MISSING"
    default_message = "This branch cannot take online payments until its collection account is set up."


class OnlinePayoutsNotOfferedError(PaymentStateError):
    """A direct-mode tenant asked for an online payout, which it does not have."""

    error_code = "ONLINE_PAYOUTS_NOT_OFFERED"
    default_message = (
        "Online payouts are not available while online payments settle directly to "
        "each branch's bank. Pay the supplier from the bank and record the payment."
    )


class OnlineRefundsNotOfferedError(PaymentStateError):
    """A direct-mode tenant asked to refund an online payment online, which it does not do."""

    error_code = "ONLINE_REFUNDS_NOT_OFFERED"
    default_message = (
        "Online payments are not refunded online while they settle directly to each "
        "branch's bank. Refund the payer from the branch's bank and record the refund."
    )


def next_month_start(day: datetime.date) -> datetime.date:
    """The first day of the month after ``day``."""
    return (day.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)


def _today(tenant) -> datetime.date:
    from vs_config.clock import tenant_today

    return tenant_today(tenant)


def custody_row(tenant):
    """The tenant's stored settings, or an unsaved row holding the defaults."""
    from .models import PaymentCustodySettings

    if tenant is None:
        return PaymentCustodySettings()
    return (
        PaymentCustodySettings.objects.filter(tenant=tenant).first()
        or PaymentCustodySettings(tenant=tenant)
    )


def custody_mode(tenant, *, on=None, row=None) -> str:
    """The mode in force for ``tenant`` on ``on`` (the tenant's today by default).

    A pending move to held is in force from its month start. A pending move to
    direct is not in force until the daily task applies it, because it waits
    until the platform holds nothing for the tenant: until then its payments
    still settle to the platform's balance. Books with no tenant (the
    platform's own) and a tenant that never chose are ``HELD``.
    """
    if tenant is None:
        return CustodyMode.HELD
    row = row if row is not None else custody_row(tenant)
    if row.pk is None:
        return CustodyMode.HELD
    day = on or _today(tenant)
    if (row.pending_mode == CustodyMode.HELD and row.pending_from
            and row.pending_from <= day):
        return row.pending_mode
    return row.mode


def is_direct(entity, *, on=None) -> bool:
    """Whether ``entity``'s online payments settle directly to its branches' banks."""
    tenant = entity.tenant if entity.tenant_id else None
    return custody_mode(tenant, on=on) == CustodyMode.DIRECT


def assert_online_payouts_allowed(entity) -> None:
    """Refuse an online payout for a tenant whose payments settle directly (409)."""
    if is_direct(entity):
        raise OnlinePayoutsNotOfferedError()


def assert_online_refund_allowed(entity) -> None:
    """Refuse an online refund for a tenant whose payments settle directly (409).

    The money is in the branch's bank, not in a provider balance, and a refund
    through the provider would be paid out of the platform's own balance. The
    gate every path that would return money through the provider passes;
    recording a refund paid from the bank stays open.
    """
    if is_direct(entity):
        raise OnlineRefundsNotOfferedError()


def refund_guard(entity, method) -> None:
    """Finance's refund guard: a refund recorded as paid online is an online refund."""
    from vs_finance.constants import PaymentMethod

    if str(method or "").upper() == PaymentMethod.ONLINE:
        assert_online_refund_allowed(entity)


# --------------------------------------------------------------------------- #
# Each branch's collection account and its subaccount                         #
# --------------------------------------------------------------------------- #

def branch_collection_account(entity, branch_id):
    """The bank account a branch's online payments settle into, or None.

    It is the branch's flagged collection account, the one its documents print
    as "pay to"; at a tenant with one branch, a flagged account not yet given a
    branch is that branch's. An account of another branch is never the answer.
    """
    from vs_finance.models import BankAccount
    from vs_rbac.scoping import transaction_branch_match_q

    if branch_id is None:
        return None
    return (
        BankAccount.objects.filter(entity=entity, is_primary_collection=True)
        .filter(transaction_branch_match_q(entity.tenant_id, branch_id))
        .select_related("gl_account", "branch").order_by("branch_id", "id").first()
    )


def branch_subaccount(entity, branch_id, provider) -> str:
    """The subaccount code a direct-mode payment for ``branch_id`` must name.

    Refused with a 409 when the branch has no collection account, or its account
    has no subaccount at ``provider``: the payment would otherwise settle to the
    platform's balance while the tenant believes it goes to its own bank.
    """
    from vs_tenants.models import Branch

    bank = branch_collection_account(entity, branch_id)
    if bank is not None and bank.gateway_subaccount_code and (
            bank.gateway_subaccount_provider in ("", provider)):
        return bank.gateway_subaccount_code
    name = (Branch.all_objects.filter(pk=branch_id).values_list("name", flat=True).first()
            if branch_id else None) or "This branch"
    if bank is None:
        reason = f"{name} has no collection bank account"
    else:
        reason = f"{name}'s collection account {bank.name} is not set up with the payment provider"
    raise SubaccountMissingError(
        f"{reason}, so it cannot take online payments yet. A school-wide administrator "
        f"sets it up under payment settings.",
        branch=branch_id,
    )


def branches_without_subaccount(entity) -> list[str]:
    """Names of the tenant's branches that could not take a direct-mode payment today."""
    from vs_tenants.models import Branch

    if not entity.tenant_id:
        return []
    missing = []
    for branch in Branch.all_objects.filter(tenant_id=entity.tenant_id).order_by("name"):
        bank = branch_collection_account(entity, branch.pk)
        if bank is None or not bank.gateway_subaccount_code:
            missing.append(branch.name)
    return missing


# --------------------------------------------------------------------------- #
# Reading and changing the setting                                            #
# --------------------------------------------------------------------------- #

def serialize_custody(row, tenant) -> dict:
    """The setting as the settings screen shows it, with the mode in force today."""
    return {
        "mode": custody_mode(tenant, row=row),
        "stored_mode": row.mode,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "pending_mode": row.pending_mode or None,
        "pending_from": row.pending_from.isoformat() if row.pending_from else None,
        "pending_note": row.pending_note or None,
        "settlement_interval_days": row.settlement_interval_days,
        "clearing_stale_days": row.clearing_stale_days,
        "updated_at": row.updated_at.isoformat() if row.pk else None,
    }


def _whole_days(data, field, bounds):
    value = data[field]
    if isinstance(value, bool):
        raise ValidationError({field: "Enter a whole number of days."})
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a whole number of days."}) from exc
    low, high = bounds
    if not low <= value <= high:
        raise ValidationError({field: f"Use a value from {low} to {high} days."})
    return value


_FIELDS = ("mode", "settlement_interval_days", "clearing_stale_days")


@transaction.atomic
def update_custody_settings(*, entity, data, actor_user=None):
    """Apply a partial change to the tenant's custody setting and audit it.

    A new ``mode`` never takes effect at once. It is stored as pending from the
    first day of next month; asking for the mode already in force cancels a
    pending change instead, and asking again for the mode already pending keeps
    its date, so a move to direct that is waiting for held money to be paid out
    does not slip a month. Moving to direct needs every branch's collection
    account set up with the provider first, or the first day of the month would
    leave a branch unable to take a payment: Lekki's bursar would find every
    parent's checkout refused on 1 November.

    A pending move to held whose month has arrived is written as the stored mode
    before anything else, so the row always says what is in force. A pending
    move to direct is applied only by the daily task.
    """
    from .models import PaymentCustodySettings

    tenant = entity.tenant if entity.tenant_id else None
    if tenant is None:
        raise ValidationError({"settings": "These books do not belong to an organisation, so there is nothing to configure."})
    if not isinstance(data, dict) or not data:
        raise ValidationError({"settings": "Provide at least one setting."})
    unknown = sorted(set(data) - set(_FIELDS))
    if unknown:
        raise ValidationError({"settings": f"Unknown settings: {', '.join(unknown)}."})

    PaymentCustodySettings.objects.get_or_create(tenant=tenant)
    row = PaymentCustodySettings.objects.select_for_update().get(tenant=tenant)
    today = _today(tenant)
    if (row.pending_mode == CustodyMode.HELD and row.pending_from
            and row.pending_from <= today):
        row.mode, row.effective_from = row.pending_mode, row.pending_from
        row.pending_mode, row.pending_from, row.pending_note = "", None, ""
    before = serialize_custody(row, tenant)

    if "settlement_interval_days" in data:
        row.settlement_interval_days = _whole_days(
            data, "settlement_interval_days", SETTLEMENT_INTERVAL_RANGE)
    if "clearing_stale_days" in data:
        row.clearing_stale_days = _whole_days(data, "clearing_stale_days", CLEARING_STALE_DAYS_RANGE)
    if "mode" in data:
        mode = str(data["mode"] or "").strip().upper()
        if mode not in CustodyMode.values:
            raise ValidationError({"mode": f"Use one of: {', '.join(CustodyMode.values)}."})
        if mode == row.mode:  # Staying put cancels a change still waiting for its month.
            row.pending_mode, row.pending_from, row.pending_note = "", None, ""
        elif mode == row.pending_mode:  # Already on its way; keep its date.
            pass
        else:
            if mode == CustodyMode.DIRECT:
                missing = branches_without_subaccount(entity)
                if missing:
                    raise ValidationError({"mode": (
                        "Every branch needs its collection account set up with the payment "
                        f"provider before payments can settle directly. Not yet: {', '.join(missing)}."
                    )})
            row.pending_mode, row.pending_from = mode, next_month_start(today)

    row.updated_by = actor_user
    row.full_clean(exclude=["tenant"])
    row.save()
    after = serialize_custody(row, tenant)
    changed = {key: (before[key], after[key]) for key in after
               if key != "updated_at" and before[key] != after[key]}
    if changed:
        audit.record(
            action=PaymentAuditAction.CUSTODY_SETTINGS_UPDATED, entity=entity,
            actor_user=actor_user,
            message=f"Updated {counted(len(changed), 'payment custody setting')}.",
            metadata={"before": {k: v[0] for k, v in changed.items()},
                      "after": {k: v[1] for k, v in changed.items()}},
        )
    return row


# --------------------------------------------------------------------------- #
# Creating or refreshing a branch's subaccount                                #
# --------------------------------------------------------------------------- #

def save_collection_subaccount(*, entity, bank_account, settlement_bank_code,
                               business_name="", provider=None, actor_user=None):
    """Create, or point again, the provider subaccount behind a branch's collection account.

    Only a branch's collection account (the one its documents print as "pay to")
    takes one, because that is the account a direct payment for the branch is
    settled into. The account must be active, carry its account number and have
    a branch, or be at a tenant with one branch. ``settlement_bank_code`` is the
    bank's code at the provider (for example 058 for GTBank); the bank account
    record keeps a bank name, not a code, so the caller supplies it.

    An account that already has a subaccount at the same provider has it pointed
    at the account's current details, so its code, and every virtual account
    created against it, stays valid. The bank code is kept on the account too:
    a held tenant's settlement run transfers the branch's money there. The provider is called outside any
    transaction: it creates nothing that moves money, but a slow provider must
    not hold a row lock.
    """
    from vs_rbac.scoping import only_branch_id

    from .providers.registry import get_provider
    from .services import resolve_provider_name

    if bank_account.entity_id != entity.pk:
        raise ValidationError({"bank_account": "No such bank account in this entity."})
    if not bank_account.is_primary_collection or not bank_account.is_active:
        raise ValidationError({"bank_account": (
            "Only a branch's active collection account can be set up for online payments. "
            "Make it the branch's collection account first."
        )})
    if not str(bank_account.account_number or "").strip():
        raise ValidationError({"bank_account": "The bank account has no account number."})
    branch = bank_account.branch
    if branch is None and entity.tenant_id and only_branch_id(entity.tenant_id) is None:
        raise ValidationError({"bank_account": (
            f"{bank_account.name} has not been given a branch, so no branch's payments "
            f"can settle into it. Give it its branch first."
        )})
    code = str(settlement_bank_code or "").strip()
    if not code.isdigit() or len(code) > 10:
        raise ValidationError({"settlement_bank_code": "Enter the bank's code, digits only."})

    provider_name = resolve_provider_name(provider or bank_account.gateway_subaccount_provider or None)
    client = get_provider(provider_name)
    name = str(business_name or "").strip() or " - ".join(
        part for part in (entity.name, getattr(branch, "name", "")) if part)
    refresh = bool(bank_account.gateway_subaccount_code) and (
        bank_account.gateway_subaccount_provider in ("", provider_name))
    if refresh:
        result = client.update_subaccount(
            subaccount_code=bank_account.gateway_subaccount_code, business_name=name,
            settlement_bank_code=code, account_number=bank_account.account_number,
        )
    else:
        result = client.create_subaccount(
            business_name=name, settlement_bank_code=code,
            account_number=bank_account.account_number, percentage_charge=0,
        )
    if not result.subaccount_code:
        from .exceptions import ProviderError

        raise ProviderError("The payment provider returned no subaccount code.", provider=provider_name)

    bank_account.gateway_subaccount_code = result.subaccount_code
    bank_account.gateway_subaccount_provider = provider_name
    bank_account.settlement_bank_code = code
    bank_account.save(update_fields=[
        "gateway_subaccount_code", "gateway_subaccount_provider", "settlement_bank_code",
        "updated_at"])
    audit.record(
        action=PaymentAuditAction.SUBACCOUNT_SAVED, entity=entity, provider=provider_name,
        reference=result.subaccount_code, actor_user=actor_user,
        message=(f"{'Refreshed' if refresh else 'Created'} the collection subaccount for "
                 f"{bank_account.name}."),
        metadata={"bank_account_id": bank_account.pk, "branch_id": bank_account.branch_id,
                  "refreshed": refresh},
    )
    return bank_account
