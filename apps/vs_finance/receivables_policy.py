"""The entity's receivables policy: revenue recognition, provision bands and deposits.

Read through :func:`resolve_receivables_policy`, which answers the model defaults
for books that have never saved one. Written through
:func:`update_receivables_policy`, audited field by field; the settings view
refuses the write to a caller whose reach is not the whole tenant.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .audit import record
from .constants import FinanceAuditAction, RevenueRecognitionMethod
from .models import FinanceReceivablesPolicy

SETTING_FIELDS = (
    "revenue_recognition",
    "provision_bands",
    "deposits_offset_unpaid_bills",
    "unclaimed_deposit_years",
)

#: Most bands a policy may hold; far beyond any real ageing ladder.
MAX_PROVISION_BANDS = 10


def resolve_receivables_policy(entity):
    """Return the stored policy, or an unsaved one carrying the defaults."""
    policy = FinanceReceivablesPolicy.objects.filter(entity=entity).first()
    return policy or FinanceReceivablesPolicy(entity=entity)


def provision_bands(policy) -> list[tuple[int, int]]:
    """The policy's bands as ``[(over_days, rate_bps)]``, lowest threshold first."""
    return sorted(
        (int(band["over_days"]), int(band["rate_bps"])) for band in (policy.provision_bands or [])
    )


def serialize_receivables_policy(policy):
    return {
        "revenue_recognition": policy.revenue_recognition,
        "revenue_recognition_label": RevenueRecognitionMethod(policy.revenue_recognition).label,
        "revenue_recognition_options": [
            {"value": value, "label": label}
            for value, label in RevenueRecognitionMethod.choices
        ],
        "provision_bands": [
            {"over_days": days, "rate_bps": rate} for days, rate in provision_bands(policy)
        ],
        "deposits_offset_unpaid_bills": policy.deposits_offset_unpaid_bills,
        "unclaimed_deposit_years": policy.unclaimed_deposit_years,
        "updated_at": policy.updated_at.isoformat() if policy.pk else None,
        "updated_by": policy.updated_by.email if policy.pk and policy.updated_by else None,
    }


def _whole_number(value, field, *, low, high, unit):
    try:
        if isinstance(value, bool):
            raise TypeError
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({field: f"Enter a whole number of {unit}."}) from exc
    if number < low or number > high:
        raise ValidationError({field: f"Use a value from {low} to {high:,} {unit}."})
    return number


def _validated_bands(raw):
    """Bands as stored: ascending thresholds, rates that never fall as a debt ages.

    A rate that fell with age would provide less for a two-year-old debt than for a
    one-year-old one, which no provision policy means.
    """
    field = "provision_bands"
    if not isinstance(raw, list) or not raw:
        raise ValidationError({field: "Give at least one band of over_days and rate_bps."})
    if len(raw) > MAX_PROVISION_BANDS:
        raise ValidationError({field: f"Use at most {MAX_PROVISION_BANDS} bands."})
    bands = []
    for band in raw:
        if not isinstance(band, dict) or set(band) != {"over_days", "rate_bps"}:
            raise ValidationError({field: "Each band needs over_days and rate_bps, and nothing else."})
        bands.append((
            _whole_number(band["over_days"], field, low=0, high=36_500, unit="days"),
            _whole_number(band["rate_bps"], field, low=0, high=10_000, unit="basis points"),
        ))
    bands.sort()
    days = [d for d, _rate in bands]
    if len(set(days)) != len(days):
        raise ValidationError({field: "Two bands cannot start at the same age."})
    rates = [rate for _d, rate in bands]
    if rates != sorted(rates):
        raise ValidationError({field: "A band's rate cannot be lower than a younger band's."})
    return [{"over_days": d, "rate_bps": rate} for d, rate in bands]


def _validated_values(data):
    if not isinstance(data, dict) or not data:
        raise ValidationError({"settings": "Provide at least one receivables setting."})
    unknown = sorted(set(data) - set(SETTING_FIELDS))
    if unknown:
        raise ValidationError({"settings": f"Unknown settings: {', '.join(unknown)}."})

    values = {}
    if "revenue_recognition" in data:
        value = str(data["revenue_recognition"] or "").upper()
        if value not in RevenueRecognitionMethod.values:
            raise ValidationError({"revenue_recognition": "Select a supported recognition method."})
        values["revenue_recognition"] = value
    if "provision_bands" in data:
        values["provision_bands"] = _validated_bands(data["provision_bands"])
    if "deposits_offset_unpaid_bills" in data:
        if not isinstance(data["deposits_offset_unpaid_bills"], bool):
            raise ValidationError({"deposits_offset_unpaid_bills": "Use true or false."})
        values["deposits_offset_unpaid_bills"] = data["deposits_offset_unpaid_bills"]
    if "unclaimed_deposit_years" in data:
        values["unclaimed_deposit_years"] = _whole_number(
            data["unclaimed_deposit_years"], "unclaimed_deposit_years", low=1, high=50,
            unit="years",
        )
    return values


@transaction.atomic
def update_receivables_policy(*, entity, data, actor_user):
    """Apply a partial policy update and audit the fields that changed."""
    values = _validated_values(data)
    current = FinanceReceivablesPolicy.objects.select_for_update().filter(entity=entity).first()
    policy = current or FinanceReceivablesPolicy(entity=entity)
    before_all = serialize_receivables_policy(policy)
    changed = False
    for field, value in values.items():
        if getattr(policy, field) != value:
            setattr(policy, field, value)
            changed = True
    if changed:
        policy.updated_by = actor_user
        policy.full_clean()
        policy.save()
    after_all = serialize_receivables_policy(policy)
    changed_before = {f: before_all[f] for f in values if before_all[f] != after_all[f]}
    changed_after = {f: after_all[f] for f in values if before_all[f] != after_all[f]}
    if changed_after:
        record(
            entity=entity,
            action=FinanceAuditAction.FINANCE_RECEIVABLES_SETTINGS_UPDATED,
            actor_user=actor_user,
            target=policy,
            target_type="FinanceReceivablesPolicy",
            target_id=str(policy.pk),
            message=f"Updated {len(changed_after)} finance receivables setting(s).",
            before=changed_before,
            after=changed_after,
        )
    return policy
