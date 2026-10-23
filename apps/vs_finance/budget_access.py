"""Field Access decisions shared by every budget and plan restatement.

A planned amount appears as a line amount, a total, a heatmap cell and in
other modules' availability and dashboard summaries. These helpers resolve the
budget switches once per request and keep derived actual and variance figures
behind the statement and source figures they depend on.
"""
from rest_framework import serializers

from vs_rbac.field_enforcement import FieldAccessMixin


BUDGET_ALIASES = (
    "name", "budget_name", "fiscal_year", "status", "is_locked", "approved_at",
    "lines", "amount", "budget", "budgeted_total", "budget_total", "total_budget",
    "plan", "actual", "actual_ytd", "actual_total", "total_actual", "used",
    "variance", "total_variance", "consumed_pct", "pct", "pct_of_plan", "delta_pct",
)


class BudgetAccessSerializer(FieldAccessMixin, serializers.Serializer):
    """Resolve every budget response alias through its registered switch."""

    field_resource = "finance.budget"

    def get_fields(self):
        fields = super().get_fields()
        fields.update({name: serializers.BooleanField(read_only=True) for name in BUDGET_ALIASES})
        return fields


def readable_budget_fields(request) -> set[str]:
    """Return the budget aliases readable by this request, cached for a response."""
    cache_name = "_finance_budget_readable_fields"
    if not hasattr(request, cache_name):
        values = {name: True for name in BUDGET_ALIASES}
        setattr(request, cache_name, set(BudgetAccessSerializer(
            values, context={"request": request},
        ).data))
    return getattr(request, cache_name)


def actual_readable(request, account_types=()) -> bool:
    """An actual is readable only with its matching income statement section."""
    from .report_access import readable_report_sections

    if "actual" not in readable_budget_fields(request):
        return False
    sections = readable_report_sections(request)
    types = set(account_types)
    if not types:
        return {"income_revenue", "income_expense"} <= sections
    if "INCOME" in types and "income_revenue" not in sections:
        return False
    if "EXPENSE" in types and "income_expense" not in sections:
        return False
    return True


def budget_figures(request, row, *, account_types=()):
    """Remove unreadable plan, actual and derived figures from a flat payload."""
    fields = readable_budget_fields(request)
    data = dict(row)
    amount = "amount" in fields
    actual = actual_readable(request, account_types)
    variance = "variance" in fields and amount and actual
    for key in ("amount", "budget", "budgeted_total", "budget_total", "total_budget", "plan"):
        if not amount:
            data.pop(key, None)
    for key in ("actual", "actual_ytd", "actual_total", "total_actual", "used"):
        if not actual:
            data.pop(key, None)
    for key in ("variance", "total_variance", "consumed_pct", "pct", "pct_of_plan", "delta_pct"):
        if not variance:
            data.pop(key, None)
    return data


def filter_budget_cards(request, payload):
    """Filter overview and spending cards that restate budget fields."""
    from .report_access import readable_report_sections

    data = dict(payload)
    fields = readable_budget_fields(request)
    overview = data.get("revenue_vs_budget")
    if isinstance(overview, dict):
        card = dict(overview)
        if "name" not in fields:
            card.pop("budget_name", None)
        for key, account_type in (("revenue", "INCOME"), ("expense", "EXPENSE")):
            if isinstance(card.get(key), dict):
                card[key] = budget_figures(request, card[key], account_types=(account_type,))
        if isinstance(card.get("net"), dict):
            net = budget_figures(request, card["net"],
                                 account_types=("INCOME", "EXPENSE"))
            if "income_net" not in readable_report_sections(request):
                net.pop("actual", None)
                net.pop("delta_pct", None)
            card["net"] = net
        data["revenue_vs_budget"] = card
    lines_card = data.get("budget")
    if isinstance(lines_card, dict):
        card = dict(lines_card)
        if "name" not in fields:
            card.pop("budget_name", None)
        card["lines"] = [
            budget_figures(request, line,
                           account_types=("INCOME" if line.get("kind") == "income" else "EXPENSE",))
            for line in card.get("lines", [])
        ]
        data["budget"] = card
    spend = data.get("budgets")
    if isinstance(spend, dict):
        card = dict(spend)
        items = []
        for item in card.get("items", []):
            filtered = budget_figures(request, item, account_types=("EXPENSE",))
            if "name" not in fields:
                filtered.pop("name", None)
            if "status" not in fields:
                filtered.pop("approved", None)
            items.append(filtered)
        card["items"] = items
        data["budgets"] = card
    return data
