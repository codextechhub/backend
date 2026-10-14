"""Finance's own contribution to provisioning a tenant's books.

A refund, a write-off, a concession, a credit note and an expense claim are each
approval-gated only when a workflow template resolves for them. Registering here means
the routes those documents need arrive with the chart of accounts, the same way the
procurement and payout routes do, rather than waiting on a command somebody has to
remember.

What arrives is the route and not the ladder. Who approves a tenant's money is the
tenant's own answer, read from the organogram it builds.
"""
from __future__ import annotations


def provision_adjustment_approvals(entity):
    """Publish this tenant's own finance approval routes, carrying no steps. Idempotent.

    One route per adjustment document type and one for expense claims, scoped to the
    tenant and published empty. Empty is the point: a ladder invented at creation would
    be a guess at who approves a refund and at the amount above which a waiver needs a
    second pair of eyes. Nothing is invented here, so no approver group is created
    either: a group exists to be named by a step, and there are no steps.

    The empty row is not the same as no row, and for these documents that difference is
    the whole gate. With no row at all, ``approval_required`` answers False and a refund
    posts straight to the ledger with nobody told. With the empty row, the post is
    approval-undecided rather than approval-free: it is refused until somebody confirms
    it in as many words, and the confirmation is recorded against them.

    A tenant that wants the default threshold-gated ladders asks for it, through the
    seeding command, and that publishes the steps and the groups they name.

    Non-destructive by contract: a document type that already has a tenant-scoped route
    keeps exactly what is configured, steps included, which is what makes this safe to
    run again for the second entity in the same tenant.
    """
    from .approvals import (
        ensure_tenant_approval_templates,
        ensure_tenant_expense_claim_template,
    )

    if entity.tenant_id is None:  # Platform-level books have no tenant to seed for.
        return
    ensure_tenant_approval_templates(entity.tenant, with_default_stages=False)
    ensure_tenant_expense_claim_template(entity.tenant, with_default_stages=False)


def provision_payroll_approval(entity):
    """Give this tenant its payroll approval route. Idempotent.

    Unlike the adjustment routes, which arrive empty because who approves a
    refund is the tenant's answer, payroll arrives with its one approval stage:
    a payroll run is never posted or paid unreviewed, so there is no
    "approval-undecided" state to leave it in. The stage's group starts with the
    roles that hold ``finance.payrollrun.post``
    (:func:`vs_finance.approvals.ensure_tenant_payroll_approval`); at provisioning
    a new tenant usually has none yet, and the route is completed from its roles
    when its first run is submitted.
    """
    from .approvals import ensure_tenant_payroll_approval

    if entity.tenant_id is None:  # Platform-level books have no tenant to seed for.
        return
    ensure_tenant_payroll_approval(entity.tenant)
