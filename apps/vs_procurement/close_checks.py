"""Payables checks contributed to the finance period close.

A period close is the control that stops the past being rewritten, and it is only as
good as the checks it runs. Finance's own checklist can only cover finance-native
invariants: the trial balance, the AR sub-ledger against its control, and depreciation.
It cannot check payables without importing procurement, and the dependency runs the
other way.

So procurement contributes its two checks here and registers them from
``AppConfig.ready``, the same inversion the workflow handlers and the export datasets
already use. Before this, finance exposed an ``extra_checks`` argument for exactly this
purpose and nothing in the product ever passed it, so every close ran without them: a
period could be sealed over an AP sub-ledger that disagreed with its control account,
and the close would report success.

The two checks:

* **AP reconciles.** The sum of what the entity owes every vendor must equal the balance
  of the payable control account. Drift means a posting bypassed the sub-ledger, or the
  reverse, and it must be found before the period is sealed.
* **GR/IR is explained.** The clearing account nets to zero when everything received has
  been invoiced. A non-zero balance is not wrong in itself - goods received late in the
  month are legitimately unbilled - so this one is a *warning*, not a blocker. It exists
  to make the number impossible to close without seeing.

Each detail line names its amounts in naira for the person closing; the figures
themselves stay in kobo wherever they are stored.
"""
from __future__ import annotations

from vs_finance.money import format_naira


def ap_reconciled(entity, period, branch=None):
    """Blocking: the AP sub-ledger must equal its control account.

    Returns ``None`` for an entity with no payables at all, so a school that has never
    bought anything does not carry a meaningless check on its close screen.
    """
    from vs_finance.close import ChecklistItem

    from .models import Vendor
    from .reports import reconcile_ap

    if not Vendor.objects.filter(entity=entity).exists():
        return None

    scope = None
    if branch is not None:
        from vs_rbac.scoping import BranchScope
        scope = BranchScope(frozenset((getattr(branch, "pk", branch),)), include_shared=False)
    ap = reconcile_ap(entity, branch_scope=scope)
    return ChecklistItem(
        name="ap_reconciled", title="Payables agree with the ledger",
        passed=ap.is_reconciled,
        detail=(
            f"Suppliers' balances total {format_naira(ap.subledger_total)}; the payables "
            f"account in the ledger holds {format_naira(ap.control_total)}."
        ),
    )


def grir_explained(entity, period, branch=None):
    """Warning: goods received and not yet billed, surfaced so it cannot be closed unseen.

    The figure is the GR/IR clearing balance; a bursar reads it as goods received
    but not yet billed (or billed but not yet received), so that is what it says.

    Deliberately non-blocking. Goods received near the period end and not yet billed
    leave a legitimate balance here, so failing the close on it would make month-end
    impossible. What is not legitimate is closing without anybody having looked, which
    is what this check ends.
    """
    from vs_finance.close import ChecklistItem

    from .models import Vendor
    from .reports import grir_balance

    if not Vendor.objects.filter(entity=entity).exists():
        return None

    scope = None
    if branch is not None:
        from vs_rbac.scoping import BranchScope
        scope = BranchScope(frozenset((getattr(branch, "pk", branch),)), include_shared=False)
    balance = grir_balance(entity, branch_scope=scope)
    if balance == 0:
        detail = "Every delivery received has been billed, and every bill delivered."
    elif balance > 0:
        detail = f"{format_naira(balance)} of goods have been received and not yet billed."
    else:
        detail = f"{format_naira(-balance)} has been billed for goods not yet received."
    return ChecklistItem(
        name="grir_explained", title="Goods received but not yet billed",
        passed=balance == 0, blocking=False, detail=detail,
    )


def register():
    """Contribute both checks to the finance close. Called from AppConfig.ready."""
    from vs_finance.close import register_close_check

    register_close_check(ap_reconciled)
    register_close_check(grir_explained)


ap_reconciled.supports_branch = True
grir_explained.supports_branch = True
