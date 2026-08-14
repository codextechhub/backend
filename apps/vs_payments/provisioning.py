"""Payments' contribution to provisioning a tenant's books.

A payout batch pays many beneficiaries at once and is the highest-risk cash-out path in
the product. The maker-checker over it is opt-in by template, so a tenant with no
``payments.payout_batch`` template did not have a jammed door on that path, it had an
open one: a single person could send a whole batch to the bank unreviewed.

Publishing the ladder was a management command somebody had to remember. Registering it
here, against finance's entity provisioning, means the gate arrives with the books.
Finance never imports this module; the app registers it from ``ready()``.
"""
from __future__ import annotations


def provision_payout_approval(entity):
    """Publish this tenant's payout-approval ladder. Idempotent per tenant.

    Non-destructive: a tenant that already has a ladder keeps whatever an administrator
    configured, so the second entity in a tenant finds the earlier work and leaves it
    alone.

    Seeded blocked, not seeded open: the single stage never auto-skips and the approving
    role is created with nobody appointed, so the first batch submitted parks and says
    which role to fill rather than paying itself out.
    """
    from .approvals import ensure_tenant_approval_templates

    if entity.tenant_id is None:  # Platform-level books have no tenant to seed for.
        return
    ensure_tenant_approval_templates(entity.tenant)
