"""Who may read a finance file through ``/media/``.

Registered from :meth:`VsFinanceConfig.ready`, so ``core`` never imports finance
to find out - the same one-way seam the export datasets use.
"""
from __future__ import annotations

from core.media import register_policy
from vs_rbac.evaluator import has_permission

from .models import ExpenseClaimLine


def _may_read_expense_receipt(request, line) -> bool:
    """A receipt is readable by the person claiming it, or by finance.

    Two distinct people, and the difference is the point. Mrs. Adeyemi photographs
    a taxi receipt onto her own claim and must be able to see it again while the
    claim is still a draft, before anyone in finance has looked at it. Everyone
    else needs ``finance.expenseclaim.view`` in the tenant, scoped to the branch
    the claim was filed for, which is the same verb the claim's own detail
    endpoint demands - so the receipt cannot be read by someone who would be
    refused the claim it belongs to.
    """
    claim = line.claim
    user = request.user
    if claim.claimant_id and str(claim.claimant_id) == str(getattr(user, "pk", "")):
        return True
    return has_permission(
        user, "finance.expenseclaim.view",
        tenant=getattr(claim.entity, "tenant", None), branch=claim.branch,
    )


def register() -> None:
    register_policy(ExpenseClaimLine, _may_read_expense_receipt)
