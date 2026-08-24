"""Permission keys that are registered but gate nothing.

A key here is seeded, grantable, and checked by no view in the codebase. Ticking
it changes nothing at all, which makes it worse than a missing box: it tells a
school administrator she has granted something when she has not.

    Adaeze wants her deputy Ngozi approving journal adjustments at Brightfield.
    She opens the roles screen, finds "Approve journals", ticks it, saves, and
    tells Ngozi she is set up. Ngozi clicks Approve and is refused. The real
    gate asked whether Ngozi's ROLE KEY is ``finance-adjustment-approver`` -
    ``vs_workflow`` matches the stage's ``approver_role_key`` and never looks at
    a permission at all. Nothing on Adaeze's screen could have told her that.

So they are withheld from the catalogue the roles screen reads. Deliberately
NOT reclassified and NOT blocked from being granted: their scope is honest (a
school may hold them) and wiring the approve endpoints to check them as well as
the stage role is a reasonable future change. Hiding them stops the screen
lying today, and costs nothing if that change is made - the key comes off this
list and reappears in the picker.

**This list is checked, not trusted.** ``ScopeAuditCommandTests`` asserts that
no key here is reachable by any resolved route, so wiring one up fails the suite
with a note to remove it from here. A list like this rots the moment somebody
implements the feature and forgets, and that test is what stops it.
"""
from __future__ import annotations

#: key -> why it currently gates nothing.
UNENFORCED_KEYS: dict[str, str] = {
    # Approval is decided by ``vs_workflow``, which matches the approver's role
    # key against the stage's ``approver_role_key``. These ten permissions are
    # what a reader would expect to control it, and none of them is consulted.
    # The seeded roles that DO work are finance-adjustment-approver,
    # finance-senior-adjustment-approver, payout-approver, procurement-approver
    # and procurement-senior-approver.
    "finance.journal.approve":
        "Journal approval runs through the workflow stage's approver role.",
    "finance.journal.approve_high_value":
        "High-value journal approval runs through the workflow stage's approver role.",
    "finance.refund.approve":
        "Refund approval runs through the workflow stage's approver role.",
    "finance.refund.approve_high_value":
        "High-value refund approval runs through the workflow stage's approver role.",
    "finance.writeoff.approve":
        "Write-off approval runs through the workflow stage's approver role.",
    "finance.writeoff.approve_high_value":
        "High-value write-off approval runs through the workflow stage's approver role.",
    "payments.payout_batch.approve":
        "Payout approval runs through the workflow stage's approver role.",
    "payments.payout_batch.approve_high_value":
        "High-value payout approval runs through the workflow stage's approver role.",
    "procurement.approval.approve":
        "Procurement approval runs through the workflow stage's approver role.",
    "procurement.approval.approve_senior":
        "Senior procurement approval runs through the workflow stage's approver role.",

}


def is_unenforced(key: str) -> bool:
    """True when this key gates nothing, so no picker should offer it."""
    return key in UNENFORCED_KEYS
