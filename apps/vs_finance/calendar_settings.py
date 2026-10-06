"""Typed fiscal-calendar policy for one ledger entity: the rollover and the close order."""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .audit import record
from .constants import FinanceAuditAction
from .models import FinanceCalendarSettings


SETTING_FIELDS = (
    "next_year_mode",
    "next_year_lead_days",
    "periods_close_in_order",
)

#: The lead window an entity may choose, in days.
MIN_LEAD_DAYS = 7
MAX_LEAD_DAYS = 180


def resolve_finance_calendar_settings(entity):
    """Return stored settings or an unsaved object carrying safe defaults."""
    settings = FinanceCalendarSettings.objects.filter(entity=entity).first()
    return settings or FinanceCalendarSettings(entity=entity)


def serialize_finance_calendar_settings(settings):
    return {
        "next_year_mode": settings.next_year_mode,
        "next_year_mode_label": (
            FinanceCalendarSettings.NextYearMode(settings.next_year_mode).label
        ),
        "next_year_lead_days": settings.next_year_lead_days,
        "periods_close_in_order": settings.periods_close_in_order,
        "updated_at": settings.updated_at.isoformat() if settings.pk else None,
        "updated_by": settings.updated_by.email if settings.pk and settings.updated_by else None,
    }


def _validated_values(data):
    if not isinstance(data, dict) or not data:
        raise ValidationError({"settings": "Provide at least one calendar setting."})
    unknown = sorted(set(data) - set(SETTING_FIELDS))
    if unknown:
        raise ValidationError({"settings": f"Unknown settings: {', '.join(unknown)}."})

    values = {}
    field = "next_year_mode"
    if field in data:
        value = str(data[field] or "").upper()
        if value not in FinanceCalendarSettings.NextYearMode.values:
            raise ValidationError({field: "Choose AUTO_OPEN or WARN_ONLY."})
        values[field] = value

    field = "next_year_lead_days"
    if field in data:
        value = data[field]
        if isinstance(value, bool):
            raise ValidationError({field: "Enter a whole number of days."})
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError({field: "Enter a whole number of days."}) from exc
        if not MIN_LEAD_DAYS <= value <= MAX_LEAD_DAYS:
            raise ValidationError({
                field: f"Use a value from {MIN_LEAD_DAYS} to {MAX_LEAD_DAYS} days.",
            })
        values[field] = value

    field = "periods_close_in_order"
    if field in data:
        if not isinstance(data[field], bool):
            raise ValidationError({field: "Use true or false."})
        values[field] = data[field]
    return values


@transaction.atomic
def update_finance_calendar_settings(*, entity, data, actor_user):
    """Apply a partial calendar-policy update and audit effective changes."""
    values = _validated_values(data)
    current = FinanceCalendarSettings.objects.select_for_update().filter(entity=entity).first()
    settings = current or FinanceCalendarSettings(entity=entity)
    before_all = serialize_finance_calendar_settings(settings)
    changed = False
    for field, value in values.items():
        if getattr(settings, field) != value:
            setattr(settings, field, value)
            changed = True
    if changed:
        settings.updated_by = actor_user
        settings.full_clean()
        settings.save()
    after_all = serialize_finance_calendar_settings(settings)
    changed_before = {
        field: before_all[field] for field in values
        if before_all[field] != after_all[field]
    }
    changed_after = {
        field: after_all[field] for field in values
        if before_all[field] != after_all[field]
    }
    if changed_after:
        record(
            entity=entity,
            action=FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED,
            actor_user=actor_user,
            target=settings if settings.pk else None,
            target_type="FinanceCalendarSettings",
            target_id=str(settings.pk or entity.pk),
            message=f"Updated {len(changed_after)} finance calendar setting(s).",
            before=changed_before,
            after=changed_after,
        )
    return settings
