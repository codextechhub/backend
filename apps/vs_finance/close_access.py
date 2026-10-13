"""The month-end close and the year-end close as a caller's Field Access lets them read them.

Where a month stands in its close, how far its checklist has got and what each
check found are registered fields of ``finance.period``; a year's close state,
archive state and the result its close carried to equity are fields of
``finance.fiscalyear``. A field the caller may not read is absent from the
response. This module holds the surfaces that carry them on the close
endpoints, and the one question every other place that restates a check asks:
:func:`check_readable`.

A check names the figures it found ("customers' balances total ... the
ledger holds ..."), so each of finance's own checks has a switch of its own,
and every check another module contributes to the close is read under
``other_checks``. Three checks restate a figure another resource guards, and a
caller sees them only when they may read that figure too (:data:`ALSO_NEEDS`):
the deferred-income check names how many shares fall due and their total, the
sealed-figures check says whether the closed figures still match, and the
inter-branch check names what branches owe each other. Hiding the figure
therefore cannot be undone by reading it off the close checklist.

The same rule words a refusal. A close that is refused names the failed checks,
and :func:`redact_close_refusal` rewrites that sentence for a caller who may not
read some of them, so the refusal that explains why the month cannot close does
not carry what the checklist would have withheld.
"""
from __future__ import annotations

from rest_framework import serializers

from vs_rbac.field_enforcement import FieldAccessMixin, can_read

from .field_access import CLOSE_CHECKS, OTHER_CHECK_NAMES
from .models import BranchFiscalPeriod
from .serializers import FiscalPeriodSerializer, FiscalYearSerializer

PERIOD = "finance.period"
FISCAL_YEAR = "finance.fiscalyear"

#: Finance's own close checks, which each have a switch named after them.
OWN_CHECKS = frozenset(name for name, _label in CLOSE_CHECKS)

#: Further fields a caller must read before a check is shown, because the check's
#: detail restates them.
ALSO_NEEDS = {
    "deferred_income_released": ("finance.deferredincome.pending_balance",),
    "sealed_figures_unchanged": ("finance.seal.verdict",),
    "inter_branch_balanced": ("finance.interbranch.net_owed",),
}


def check_readable(request, name: str) -> bool:
    """Whether the request's caller may read the result of the close check *name*.

    A check finance does not declare is another module's and travels under
    ``other_checks``, so a check added to the close tomorrow is governed the
    day it arrives.
    """
    field = name if name in OWN_CHECKS else "other_checks"
    if not can_read(request, f"{PERIOD}.{field}"):
        return False
    return all(can_read(request, key) for key in ALSO_NEEDS.get(name, ()))


def readable_check_items(request, items) -> list:
    """The checklist item dicts (each with a ``name``) the caller may read, in order."""
    return [item for item in items if check_readable(request, item["name"])]


def redact_close_refusal(request, error):
    """Reword a "not ready to close" refusal to name only the checks the caller may read.

    The refusal carries the failed checks and the sentence that opens it
    (:class:`~vs_finance.exceptions.PeriodCloseError`). When the caller may read
    every one the error is left as it is; otherwise the message and the list of
    failed check names are rebuilt from the ones they may. A refusal that names
    no check at all keeps its opening sentence alone. Returns the same error.
    """
    from .close import failures_sentence

    items = getattr(error, "failure_items", None)
    if not items:
        return error
    shown = [item for item in items if check_readable(request, item.name)]
    if len(shown) == len(items):
        return error
    sentence = failures_sentence(shown)
    error.message = f"{error.lead} {sentence}." if shown else error.lead
    error.args = (error.message,)
    error.failures = [item.name for item in shown]
    error.extra = {**error.extra, "failures": error.failures}
    return error


def _result():
    """A check result: whatever the payload holds under the check's name, read-only."""
    return serializers.JSONField(read_only=True)


class CloseChecksSerializer(FieldAccessMixin, serializers.Serializer):
    """Check results keyed by check name, each shown only to a caller who may read it.

    The same decision :func:`check_readable` makes for a list of items, applied
    to a mapping: the switches remove the checks the caller has closed, and the
    checks that need another figure are removed when that figure is closed. A
    check with no declared field is another module's, governed by
    ``other_checks``.
    """

    field_resource = PERIOD

    trial_balance_balanced = _result()
    no_draft_journals = _result()
    ar_reconciled = _result()
    depreciation_posted = _result()
    earlier_periods_closed = _result()
    deferred_income_released = _result()
    inter_branch_balanced = _result()
    sealed_figures_unchanged = _result()
    depreciation_posted_for_year = _result()
    ap_reconciled = _result()
    grir_explained = _result()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        for name in list(data):
            if not check_readable(request, name):
                data.pop(name)
        for name, value in instance.items():
            if name not in self.fields and check_readable(request, name):
                data[name] = value
        return data


class CheckItemsField(serializers.Field):
    """The checklist's items, with every check the caller may not read removed."""

    def __init__(self, **kwargs):
        kwargs["read_only"] = True
        super().__init__(**kwargs)

    def to_representation(self, value):
        return readable_check_items(self.context.get("request"), value)


class CloseChecklistSerializer(FieldAccessMixin, serializers.Serializer):
    """A checklist as the close and its refusal show it: whether it passed, and its items."""

    field_resource = PERIOD

    passed = _result()
    items = CheckItemsField()


class PeriodChecklistPreviewSerializer(FieldAccessMixin, serializers.Serializer):
    """What closing a month would find: the month, its progress and its checks.

    ``done`` and ``total`` count the checks the caller may read, so a hidden
    check is not betrayed by a count that includes it.
    """

    field_resource = PERIOD

    period = FiscalPeriodSerializer(read_only=True)
    passed = _result()
    done = _result()
    total = _result()
    items = CheckItemsField()
    branch = _result()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        items = data.get("items", [])
        if "done" in data:
            data["done"] = sum(1 for item in items if item["passed"])
        if "total" in data:
            data["total"] = len(items)
        return data


class BranchPeriodStateSerializer(FieldAccessMixin, serializers.ModelSerializer):
    """Where one branch stands in a month after a branch-level close, reopen or lock."""

    field_resource = PERIOD

    class Meta:
        model = BranchFiscalPeriod
        fields = ["id", "branch", "status", "closed_at"]
        read_only_fields = fields


class PeriodCloseResultSerializer(FieldAccessMixin, serializers.Serializer):
    """The response to closing, reopening or locking a month.

    A school-wide action answers with the month (and the checklist a close ran);
    a branch action adds the branch's own state.
    """

    field_resource = PERIOD

    period = FiscalPeriodSerializer(read_only=True)
    branch_period = BranchPeriodStateSerializer(read_only=True)
    checklist = CloseChecklistSerializer(read_only=True)


class FiscalYearCloseResultSerializer(FieldAccessMixin, serializers.Serializer):
    """The response to closing or reopening a fiscal year."""

    field_resource = FISCAL_YEAR

    fiscal_year = FiscalYearSerializer(read_only=True)
    closing_journals = _result()
    closing_journal = _result()
    reversals = _result()
    net_income = _result()
    branch = _result()
