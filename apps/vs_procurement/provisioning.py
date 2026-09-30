"""Procurement's contribution to provisioning a tenant's books.

Two things arrive with a tenant's chart of accounts: the tenant's own spend-approval
routes, and somewhere to put stock.

Spend approval is opt-in by template: a requisition, purchase order, vendor invoice or
vendor payment resolves the template published for its scope, through the engine's
branch to tenant to platform cascade. A tenant holding no template of its own resolves
to the shared platform row, which means one shared row would decide how every tenant's
spend is approved.

Registering here, against finance's entity provisioning, is what gives each tenant its
own route from the moment its books exist. Finance never imports this module; the app
registers it from ``ready()``.
"""
from __future__ import annotations


def provision_approval_ladders(entity):
    """Publish this tenant's own approval routes, carrying no steps. Idempotent.

    One route per approvable document type, scoped to the tenant and published empty.
    Empty is the point: who approves a school's spend is the school's own answer, read
    from the organogram it builds, and a ladder invented at creation would be a guess
    at the people and the amounts. Nothing is invented here, so no approver group is
    created either: a group exists to be named by a step, and there are no steps.

    The empty row is not the same as no row. It stands in front of the shared platform
    route, so a change to that shared row can never begin governing this tenant's
    spend, which is the whole reason a tenant-scoped template exists. A document
    submitted against it is refused as unconfigured rather than approved unseen, and
    goes out only when somebody confirms it in as many words, recorded against them.

    A tenant that wants the default threshold-gated ladder asks for it through the
    seeding command, and that publishes the steps and the groups they name.

    Non-destructive by contract: a tenant that already has a route for a document type
    keeps exactly what is configured, steps included, which is what makes this safe to
    run again for the second entity in the same tenant.
    """
    from .approvals import ensure_tenant_approval_templates

    if entity.tenant_id is None:  # Platform-level books have no tenant to seed for.
        return
    ensure_tenant_approval_templates(entity.tenant, with_default_stages=False)


def provision_default_stock_location(entity):
    """Give the entity a default stock location. Idempotent.

    Stock now lives at a location, so an entity with none has nowhere to receive into
    and every movement would be refused. Migration 0028 gives existing entities their
    ``MAIN``; this is the same guarantee for every entity created afterwards, so
    "an entity always has somewhere to put stock" is an invariant rather than something
    each caller has to remember to arrange.

    A store belongs to one branch, so this one is filed under the tenant's main
    branch, which a school's books are created after. Books of a tenant with no
    branch at all (the platform's own) have no branch to file it under. A school
    that later runs two branches adds its second location and moves the opening
    balances across deliberately, where the transfer is visible.
    """
    from vs_rbac.scoping import only_branch_id
    from vs_tenants.models import Branch

    from .models import StockLocation

    if StockLocation.objects.filter(entity=entity).exists():
        return
    main = Branch.all_objects.filter(tenant_id=entity.tenant_id, is_main=True).first()
    StockLocation.objects.create(
        entity=entity, code="MAIN", name="Main store",
        description="Created with this entity's books.",
        branch_id=main.pk if main is not None else only_branch_id(entity.tenant_id),
        is_default=True, is_active=True,
    )
