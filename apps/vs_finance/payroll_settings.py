"""Typed payroll policy for one ledger entity: statutory switches, rates and payslip delivery.

The same shape as the other finance settings screens: every field has a default
on :class:`~vs_finance.models.FinancePayrollSettings`, a partial update validates
each value, and an effective change is audited with its before and after. Only
a caller who covers the whole tenant may write (the view's
:class:`~vs_finance.views_settings.WholeTenantSettingsMixin`), because the policy
binds every branch that pays staff from these books.
"""
from __future__ import annotations

from vs_finance.wording import counted

import datetime

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .audit import record
from .constants import FinanceAuditAction, PayeMethod
from .models import FinancePayrollSettings
from .payroll_statutory import payroll_settings

#: Switches, by name.
BOOLEAN_FIELDS = (
    "employee_pension_enabled", "employer_pension_enabled", "nhf_enabled",
    "nsitf_enabled", "itf_enabled", "payslip_in_app", "payslip_email",
    "previous_pay_required",
)

#: Rates in basis points, by name.
RATE_FIELDS = (
    "employee_pension_rate_bps", "employer_pension_rate_bps", "nhf_rate_bps",
    "nsitf_rate_bps", "itf_rate_bps",
)

#: Dates, by name, written and read as ``YYYY-MM-DD`` or null.
DATE_FIELDS = ("payroll_moved_here_on",)

SETTING_FIELDS = ("paye_method", "tax_country") + BOOLEAN_FIELDS + RATE_FIELDS + DATE_FIELDS


def resolve_finance_payroll_settings(entity):
    """Return stored settings or an unsaved object carrying the defaults."""
    return payroll_settings(entity)


def serialize_finance_payroll_settings(settings):
    data = {field: getattr(settings, field) for field in SETTING_FIELDS}
    for field in DATE_FIELDS:
        data[field] = data[field].isoformat() if data[field] else None
    data["paye_method_label"] = PayeMethod(settings.paye_method).label
    data["updated_at"] = settings.updated_at.isoformat() if settings.pk else None
    data["updated_by"] = settings.updated_by.email if settings.pk and settings.updated_by else None
    return data


def _validated_values(data):
    if not isinstance(data, dict) or not data:
        raise ValidationError({"settings": "Provide at least one payroll setting."})
    unknown = sorted(set(data) - set(SETTING_FIELDS))
    if unknown:
        raise ValidationError({"settings": f"Unknown settings: {', '.join(unknown)}."})

    values = {}
    if "paye_method" in data:
        value = str(data["paye_method"] or "").upper()
        if value not in PayeMethod.values:
            raise ValidationError({"paye_method": "Choose COMPUTED or SUPPLIED."})
        values["paye_method"] = value
    if "tax_country" in data:
        from .models import PayeTaxTable

        value = str(data["tax_country"] or "").upper()
        if not PayeTaxTable.objects.filter(country=value).exists():
            raise ValidationError({"tax_country": "There are no tax tables for that country."})
        values["tax_country"] = value
    for field in BOOLEAN_FIELDS:
        if field in data:
            if not isinstance(data[field], bool):
                raise ValidationError({field: "Use true or false."})
            values[field] = data[field]
    for field in RATE_FIELDS:
        if field in data:
            value = data[field]
            if isinstance(value, bool):
                raise ValidationError({field: "Enter a whole number of basis points."})
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError({field: "Enter a whole number of basis points."}) from exc
            if not 0 <= value <= 10000:
                raise ValidationError({field: "Use a value from 0 to 10,000 basis points."})
            values[field] = value
    for field in DATE_FIELDS:
        if field in data:
            raw = data[field]
            if raw in (None, ""):
                values[field] = None
                continue
            try:
                values[field] = datetime.date.fromisoformat(str(raw))
            except ValueError as exc:
                raise ValidationError({field: "Use a date as YYYY-MM-DD, or null."}) from exc
    return values


@transaction.atomic
def update_finance_payroll_settings(*, entity, data, actor_user):
    """Apply a partial payroll-policy update and audit effective changes.

    A change takes effect on the next run generated; runs already raised keep
    the figures they were worked out with.
    """
    values = _validated_values(data)
    current = FinancePayrollSettings.objects.select_for_update().filter(entity=entity).first()
    settings = current or FinancePayrollSettings(entity=entity)
    before_all = serialize_finance_payroll_settings(settings)
    changed = False
    for field, value in values.items():
        if getattr(settings, field) != value:
            setattr(settings, field, value)
            changed = True
    if changed:
        settings.updated_by = actor_user
        settings.full_clean()
        settings.save()
    after_all = serialize_finance_payroll_settings(settings)
    changed_fields = [f for f in values if before_all[f] != after_all[f]]
    if changed_fields:
        record(
            entity=entity, action=FinanceAuditAction.PAYROLL_SETTINGS_UPDATED,
            actor_user=actor_user, target=settings,
            target_type="FinancePayrollSettings", target_id=str(settings.pk or entity.pk),
            message=f"Updated {counted(len(changed_fields), 'payroll setting')}.",
            before={f: before_all[f] for f in changed_fields},
            after={f: after_all[f] for f in changed_fields},
        )
    return settings
