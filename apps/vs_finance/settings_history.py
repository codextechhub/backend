"""Recent changes to a settings screen, in the words the screen itself uses.

Every settings change is audited with ``before`` and ``after`` snapshots keyed by
the setting's field name and holding the stored value: ``term_collection_target_pct:
80``, ``default_receipt_allocation_strategy: "oldest"``, ``concession_second_person_threshold:
1000000``. Those keys and values are for the server. A bursar reading a settings
screen's history needs "Term collection target: 75% → 80%", "Receipt allocation
order: Largest balance first → Oldest due first" and "Concession total needing a
second person: ₦10,000.00 → ₦25,000.00".

So each settings family declares its fields here as :class:`SettingField` (a label
and how its value reads), and :func:`settings_history` returns the audit rows with
a ``changes`` list beside the raw snapshots:
``[{"field", "label", "before", "after"}]``, where ``before`` and ``after`` are the
display strings. ``field`` stays the machine key so a client may match a change to
its control; it is never meant to be shown. Money reads in naira, a rate in basis
points as a percentage, a choice by its label, a switch as Yes or No, a calendar
date in the tenant's date format (:func:`vs_config.display.format_date`), and an
account by its code and name.

A key a family does not declare (a setting since retired) reads as "Another
setting", and a stored choice no longer offered reads as "An option no longer
offered", so a stale entry never shows a raw key or code either.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    AccountMappingKey,
    FinanceAuditAction,
    PayeMethod,
    PayerPaymentSplit,
    PayerPaymentSurplus,
    RevenueRecognitionMethod,
)

#: The label for a key no family declares.
UNKNOWN_SETTING_LABEL = "Another setting"
#: How an empty value reads unless a field says otherwise.
NOT_SET = "Not set"
#: How a stored choice reads once it is no longer among the choices.
RETIRED_CHOICE = "An option no longer offered"

#: Countries whose tax tables a payroll may price PAYE from, by ISO code.
COUNTRY_NAMES = {"NG": "Nigeria", "GH": "Ghana", "KE": "Kenya", "ZA": "South Africa"}


@dataclass(frozen=True)
class SettingField:
    """One setting's label and how its stored value reads.

    ``kind`` is one of ``text``, ``bool``, ``days``, ``years``, ``count``,
    ``percent`` (a whole percentage), ``bps`` (basis points, read as a
    percentage), ``kobo`` (money, read in naira), ``choice`` (``choices`` is the
    ``TextChoices``), ``date``, ``country``, ``bank`` (a bank account summary),
    ``account`` (a ledger account code) or ``bands`` (doubtful-debt provision
    bands). ``unit`` names what a ``count`` counts. ``empty`` is how a blank
    value reads where "Not set" would mislead.
    """

    label: str
    kind: str = "text"
    choices: type | None = None
    unit: str = ""
    empty: str = NOT_SET


def _plural(number, word):
    return f"{number:,} {word}" if number == 1 else f"{number:,} {word}s"


def _percent_from_bps(bps):
    whole, part = divmod(int(bps), 100)
    return f"{whole}%" if not part else f"{whole}.{part:02d}".rstrip("0") + "%"


def _generic(value):
    """How a value of an undeclared setting reads: never a key, never raw JSON."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f"{value:,}"
    if isinstance(value, (dict, list)):
        return "Changed"
    text = str(value)
    if "_" in text or "." in text and " " not in text:
        return "Changed"
    return text


def describe_value(field, value, *, tenant=None, accounts=None) -> str:
    """``value`` of the setting ``field`` (a :class:`SettingField`, or None) in words."""
    if field is None:
        return NOT_SET if value in (None, "") else _generic(value)
    if value is None or value == "" or value == []:
        return field.empty
    kind = field.kind
    if kind == "bool":
        return "Yes" if value else "No"
    if kind == "days":
        return _plural(int(value), "day")
    if kind == "years":
        return _plural(int(value), "year")
    if kind == "count":
        return _plural(int(value), field.unit) if field.unit else f"{int(value):,}"
    if kind == "percent":
        return f"{int(value)}%"
    if kind == "bps":
        return _percent_from_bps(value)
    if kind == "kobo":
        from .money import format_naira

        return format_naira(int(value))
    if kind == "choice":
        try:
            return str(field.choices(value).label)
        except ValueError:
            return RETIRED_CHOICE
    if kind == "date":
        from vs_config.display import format_date

        return format_date(value, tenant) or field.empty
    if kind == "country":
        return COUNTRY_NAMES.get(str(value).upper(), str(value).upper())
    if kind == "bank":
        if not isinstance(value, dict):
            return field.empty
        name = value.get("name") or "A bank account"
        branch = value.get("branch_name")
        return f"{name} ({branch})" if branch else name
    if kind == "account":
        name = (accounts or {}).get(str(value))
        return f"{value} · {name}" if name else str(value)
    if kind == "bands":
        if not isinstance(value, list):
            return "Changed"
        parts = [
            f"over {_plural(int(band['over_days']), 'day')}: {_percent_from_bps(band['rate_bps'])}"
            for band in value if isinstance(band, dict)
            and {"over_days", "rate_bps"} <= set(band)
        ]
        text = ", ".join(parts)
        return text[:1].upper() + text[1:] if text else field.empty
    return str(value)


def describe_changes(fields, before, after, *, tenant=None, accounts=None) -> list[dict]:
    """Each setting a change touched, with its label and before and after in words.

    Settings appear in the family's declared order, then any undeclared key.
    """
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    touched = set(before) | set(after)
    ordered = [key for key in fields if key in touched]
    ordered += sorted(touched - set(fields))
    changes = []
    for key in ordered:
        field = fields.get(key)
        changes.append({
            "field": key,
            "label": field.label if field else UNKNOWN_SETTING_LABEL,
            "before": describe_value(field, before.get(key), tenant=tenant, accounts=accounts),
            "after": describe_value(field, after.get(key), tenant=tenant, accounts=accounts),
        })
    return changes


def settings_history(entity, action, fields, *, limit=10):
    """The last ``limit`` changes to one of the entity's settings, newest first.

    Each row is a :class:`~vs_finance.serializers.FinanceAuditLogSerializer` row
    with ``changes`` added (see the module docstring); ``before`` and ``after``
    stay as stored, for any client that reads them.

    Not narrowed to the caller's branches. The settings belong to the whole
    tenant, so every entry here carries no branch, and a branch-bound reader who
    may open the settings may see who changed them. The finance audit trail
    itself (:mod:`vs_finance.views_ops.audit`) is where an entry with no branch
    is shown to whole-school readers only.
    """
    from .models import Account, FinanceAuditLog
    from .serializers import SettingsHistorySerializer

    rows = list(
        FinanceAuditLog.objects.filter(entity=entity, action=action)
        .select_related("actor", "effective_user", "branch").order_by("-created_at", "-id")[:limit]
    )
    accounts = {}
    account_keys = {key for key, field in fields.items() if field.kind == "account"}
    if account_keys:
        codes = {
            str(snapshot[key]) for row in rows for snapshot in (row.before, row.after)
            if isinstance(snapshot, dict) for key in account_keys if snapshot.get(key)
        }
        accounts = dict(
            Account.objects.filter(entity=entity, code__in=codes).values_list("code", "name")
        )
    return SettingsHistorySerializer(
        rows, many=True,
        context={"fields": fields, "tenant": entity.tenant, "accounts": accounts},
    ).data


ACCOUNT_SETTING_FIELDS = {
    key: SettingField(label, "account") for key, label in AccountMappingKey.choices
}

DOCUMENT_SETTING_FIELDS = {
    "default_invoice_due_days": SettingField("Days until an invoice is due", "days"),
    "default_invoice_narration": SettingField("Default invoice narration", empty="Blank"),
    "auto_post_manual_invoices": SettingField("Post manual invoices immediately", "bool"),
    "allow_customer_opening_balances": SettingField("Allow customer opening balances", "bool"),
    "term_collection_target_pct": SettingField("Term collection target", "percent"),
    "auto_apply_customer_credit": SettingField(
        "Apply customer credit to new bills automatically", "bool"),
    "concession_second_person_threshold": SettingField(
        "Concession total needing a second person", "kobo"),
    "primary_collection_bank_account": SettingField(
        "Collection bank account", "bank", empty="Each branch's first active account"),
}


def _banking_fields():
    from .models import FinanceBankingSettings

    return {
        "default_bank_reconciliation_tolerance_days": SettingField(
            "Reconciliation date window", "days"),
        "default_group_reconciliation_matches": SettingField(
            "Allow grouped automatic matches", "bool"),
        "default_receipt_allocation_strategy": SettingField(
            "Receipt allocation order", "choice",
            choices=FinanceBankingSettings.ReceiptAllocationStrategy),
        "petty_cash_low_balance_threshold_bps": SettingField(
            "Petty cash low-balance alert", "bps"),
    }


def _calendar_fields():
    from .models import FinanceCalendarSettings

    return {
        "next_year_mode": SettingField(
            "Next fiscal year", "choice", choices=FinanceCalendarSettings.NextYearMode),
        "next_year_lead_days": SettingField("Days ahead to open or warn", "days"),
        "periods_close_in_order": SettingField("Close months in order", "bool"),
    }


RECEIVABLES_SETTING_FIELDS = {
    "revenue_recognition": SettingField(
        "How income billed in advance is released", "choice",
        choices=RevenueRecognitionMethod),
    "provision_bands": SettingField("Doubtful-debt provision bands", "bands"),
    "deposits_offset_unpaid_bills": SettingField(
        "Set a leaver's deposit against their unpaid bills", "bool"),
    "unclaimed_deposit_years": SettingField(
        "Years before an unclaimed deposit becomes income", "years"),
    "payer_payment_split": SettingField(
        "How a payer's payment is split", "choice", choices=PayerPaymentSplit),
    "payer_payment_surplus": SettingField(
        "Whose credit a payer's leftover payment becomes", "choice",
        choices=PayerPaymentSurplus),
}

PAYROLL_SETTING_FIELDS = {
    "paye_method": SettingField("Where PAYE comes from", "choice", choices=PayeMethod),
    "tax_country": SettingField("Tax table country", "country"),
    "employee_pension_enabled": SettingField("Withhold employee pension", "bool"),
    "employee_pension_rate_bps": SettingField("Employee pension rate", "bps"),
    "employer_pension_enabled": SettingField("Accrue employer pension", "bool"),
    "employer_pension_rate_bps": SettingField("Employer pension rate", "bps"),
    "nhf_enabled": SettingField("Withhold National Housing Fund", "bool"),
    "nhf_rate_bps": SettingField("National Housing Fund rate", "bps"),
    "nsitf_enabled": SettingField("Accrue NSITF", "bool"),
    "nsitf_rate_bps": SettingField("NSITF rate", "bps"),
    "itf_enabled": SettingField("Accrue ITF training levy", "bool"),
    "itf_rate_bps": SettingField("ITF training levy rate", "bps"),
    "payslip_in_app": SettingField("Show payslips in the app", "bool"),
    "payslip_email": SettingField("Email payslips", "bool"),
    "previous_pay_required": SettingField("Earlier pay required", "bool"),
    "payroll_moved_here_on": SettingField("Payroll moved here on", "date"),
}

RETENTION_SETTING_FIELDS = {
    "retention_years": SettingField(
        "Years financial records are kept", "years", empty="The statutory period"),
    "archive_min_age_years": SettingField("Years before records may be archived", "years"),
}


def finance_setting_fields(action):
    """The declared fields of the finance settings family audited under ``action``."""
    families = {
        FinanceAuditAction.FINANCE_SETTINGS_UPDATED: lambda: ACCOUNT_SETTING_FIELDS,
        FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED: lambda: DOCUMENT_SETTING_FIELDS,
        FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED: _banking_fields,
        FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED: _calendar_fields,
        FinanceAuditAction.FINANCE_RECEIVABLES_SETTINGS_UPDATED: lambda: RECEIVABLES_SETTING_FIELDS,
        FinanceAuditAction.PAYROLL_SETTINGS_UPDATED: lambda: PAYROLL_SETTING_FIELDS,
        FinanceAuditAction.RETENTION_SETTINGS_UPDATED: lambda: RETENTION_SETTING_FIELDS,
    }
    return families[action]()
