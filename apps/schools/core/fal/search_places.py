"""The fee billing settings screen the search bar can route to.

Read with ``school.fees.view``. The ``school.fees`` resource is core
(:mod:`vs_rbac.capability_map`), so the place names no capability.
"""
from __future__ import annotations

from vs_search.registry import ACTION, Place, register

register(
    Place(
        kind=ACTION, key="school_finance.fee_due_policy", label="When fee bills fall due",
        description="The rule that dates every fee bill the school raises.",
        keywords=("due date", "fees", "fee bills"),
        screen="school_finance.fee_due_policy", permission="school.fees.view",
    ),
)
