"""Finance's own contribution to provisioning a tenant's books.

Finance published no approval ladders at all. Refunds and write-offs carried a submit
endpoint and a workflow handler, but with no template ever published
``approval_required`` answered False, so both posted directly: the gate was built and
never switched on. Concessions and credit notes had no gate at build time either.

Registering here means the adjustment ladders arrive with the books, the same way the
procurement and payout ladders now do.
"""
from __future__ import annotations


def provision_adjustment_approvals(entity):
    """Publish this tenant's adjustment-approval ladders. Idempotent per tenant.

    Non-destructive: a document type that already has a tenant-scoped template keeps
    whatever an administrator configured, so a second entity in the same tenant finds
    the earlier work and leaves it alone.

    Seeded blocked, not seeded open: the approving roles are created with nobody
    appointed, so the first refund, write-off, or above-threshold concession parks and
    names the role to fill rather than posting itself.
    """
    from .approvals import ensure_tenant_approval_templates

    if entity.tenant_id is None:  # Platform-level books have no tenant to seed for.
        return
    ensure_tenant_approval_templates(entity.tenant)
