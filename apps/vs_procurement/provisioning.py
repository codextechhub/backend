"""Procurement's contribution to provisioning a tenant's books.

Two things arrive with a tenant's chart of accounts: the spend-approval ladders, and
somewhere to put stock.

Spend approval is opt-in by template: a requisition, purchase order, vendor invoice or
vendor payment is only gated when a workflow template exists for its scope. A tenant
whose ladders were never published therefore had no maker-checker on its spend at all,
and publishing them was a management command somebody had to remember.

Registering here, against finance's entity provisioning, makes the gate arrive with the
books. Finance never imports this module; the app registers it from ``ready()``.
"""
from __future__ import annotations


def provision_approval_ladders(entity):
    """Publish this tenant's procurement approval ladders. Idempotent per tenant.

    Non-destructive by contract: a tenant that already has ladders keeps exactly what
    an administrator configured, which is what makes this safe to run again for the
    second entity in the same tenant.

    Seeded blocked, not seeded open: the stages never auto-skip and the approving roles
    are created with nobody appointed, so the first document submitted parks and names
    the role to fill rather than approving itself.
    """
    from .approvals import ensure_tenant_approval_templates

    if entity.tenant_id is None:  # Platform-level books have no tenant to seed for.
        return
    ensure_tenant_approval_templates(entity.tenant)


def provision_default_stock_location(entity):
    """Give the entity a default stock location. Idempotent.

    Stock now lives at a location, so an entity with none has nowhere to receive into
    and every movement would be refused. Migration 0028 gives existing entities their
    ``MAIN``; this is the same guarantee for every entity created afterwards, so
    "an entity always has somewhere to put stock" is an invariant rather than something
    each caller has to remember to arrange.

    A school that later runs two campuses adds its second location and moves the
    opening balances across deliberately, where the transfer is visible.
    """
    from .models import StockLocation

    if StockLocation.objects.filter(entity=entity).exists():
        return
    StockLocation.objects.create(
        entity=entity, code="MAIN", name="Main store",
        description="Created with this entity's books.",
        is_default=True, is_active=True,
    )
