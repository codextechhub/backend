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
"""
from __future__ import annotations


def ap_reconciled(entity, period):
    """Blocking: the AP sub-ledger must equal its control account.

    Returns ``None`` for an entity with no payables at all, so a school that has never
    bought anything does not carry a meaningless check on its close screen.
    """
    from vs_finance.close import ChecklistItem

    from .models import Vendor
    from .reports import reconcile_ap

    if not Vendor.objects.filter(entity=entity).exists():
        return None

    ap = reconcile_ap(entity)
    return ChecklistItem(
        name="ap_reconciled",
        passed=ap.is_reconciled,
        detail=(f"sub-ledger {ap.subledger_total} vs control {ap.control_total} kobo"),
    )


def grir_explained(entity, period):
    """Warning: the GR/IR clearing balance, surfaced so it cannot be closed unseen.

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

    balance = grir_balance(entity)
    return ChecklistItem(
        name="grir_explained",
        passed=balance == 0,
        blocking=False,
        detail=(
            "GR/IR nets to zero"
            if balance == 0
            else f"GR/IR clearing balance {balance} kobo (received not invoiced, "
                 f"or invoiced not received)"
        ),
    )


def register():
    """Contribute both checks to the finance close. Called from AppConfig.ready."""
    from vs_finance.close import register_close_check

    register_close_check(ap_reconciled)
    register_close_check(grir_explained)

