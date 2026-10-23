"""Read decisions for financial statement sections and their exports.

Each statement section is one registered field. Views use the same set of
readable sections to build JSON and downloaded tables, so a hidden column or
section cannot reappear in another format.
"""
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from vs_rbac.field_enforcement import FieldAccessMixin


REPORT_SECTIONS = (
    "trial_balance", "income_revenue", "income_expense", "income_net",
    "income_budget", "income_prior_year", "balance_assets",
    "balance_liabilities", "balance_equity", "balance_check",
    "cashflow_cash", "cashflow_activities", "equity_changes",
    "analytics_slice", "aging_customers", "aging_totals", "ar_reconciliation",
)


class ReportSectionsSerializer(FieldAccessMixin, serializers.Serializer):
    """Apply the registered switches to the set of statement sections."""

    field_resource = "finance.report"

    def get_fields(self):
        fields = super().get_fields()
        fields.update({name: serializers.BooleanField(read_only=True) for name in REPORT_SECTIONS})
        return fields


def readable_report_sections(request) -> set[str]:
    """Return this request's readable statement sections, cached for a response."""
    cache_name = "_finance_report_readable_sections"
    if not hasattr(request, cache_name):
        values = {name: True for name in REPORT_SECTIONS}
        setattr(request, cache_name, set(ReportSectionsSerializer(
            values, context={"request": request},
        ).data))
    return getattr(request, cache_name)


def require_report_sections(request, *names):
    """Refuse a report that has no safe partial form for missing sections."""
    if not set(names) <= readable_report_sections(request):
        raise PermissionDenied("This report requires access to every included statement section.")


def income_sections(request) -> set[str]:
    """Apply source dependencies to the income statement's visible sections."""
    from .budget_access import readable_budget_fields

    sections = readable_report_sections(request)
    allowed = set(sections)
    if not {"income_revenue", "income_expense"} <= sections:
        allowed.discard("income_net")
    if "amount" not in readable_budget_fields(request):
        allowed.discard("income_budget")
    return allowed


def balance_sections(request) -> set[str]:
    """The balance check needs all three parts of the position statement."""
    allowed = set(readable_report_sections(request))
    if not {"balance_assets", "balance_liabilities", "balance_equity"} <= allowed:
        allowed.discard("balance_check")
    return allowed
