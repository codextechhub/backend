"""What else gets set up when a tenant's books are created.

Creating a :class:`~vs_finance.models.LedgerEntity` already provisions a fully usable
set of books in one call: the currencies, a starter chart of accounts, and open fiscal
periods. The intent is that no operator has to remember a follow-up command.

Approval ladders were the exception, and it mattered. A payout batch or a procurement
document is only gated when a workflow template exists for its scope, so a tenant whose
ladders were never published had **no** maker-checker at all on its highest-risk
cash-out path. The seeds existed but only a management command and one endpoint called
them, so turning the gate on was an operator step that could simply be missed.

This registry closes that. A dependent app registers a provisioner from its
``AppConfig.ready`` and it runs inside the same transaction that creates the entity, so
the books and the controls over them arrive together or not at all. Finance never
imports procurement or payments; the dependency keeps running one way, exactly as it
does for the period-close checks and the workflow handlers.

**Failures are not swallowed.** A provisioner that raises rolls the entity creation
back. That is deliberate: an entity that exists without its approval ladders is the
open door this module was written to close, and a loud, retryable failure at
onboarding is much cheaper than a silent one discovered at the first payout run.
"""
from __future__ import annotations

#: Callables run against a newly created entity, in registration order.
_PROVISIONERS: list = []


def register_entity_provisioner(fn):
    """Register a callable run as ``fn(entity)`` when a ledger entity is created.

    Registration is idempotent, so a module imported twice does not provision twice.
    Provisioners must themselves be idempotent and non-destructive: several entities
    can share one tenant, so the second entity in a tenant will call them again and
    must find the earlier work and leave it alone.
    """
    if fn not in _PROVISIONERS:
        _PROVISIONERS.append(fn)
    return fn


def registered_entity_provisioners() -> list:
    """The registered provisioners, for tests and diagnostics."""
    return list(_PROVISIONERS)


def provision_entity(entity) -> None:
    """Run every registered provisioner for a newly created entity."""
    for fn in _PROVISIONERS:
        fn(entity)
