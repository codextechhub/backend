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
    """Publish this tenant's own payout-approval route, carrying no steps. Idempotent.

    Empty is the point: who signs off on money leaving a tenant is the tenant's own
    answer, read from the organogram it builds, and a ladder invented at creation would
    be a guess at the people and at the amount that needs a second pair of eyes.
    Nothing is invented here, so no approver group is created either: a group exists to
    be named by a step, and there are no steps.

    The empty row is not the same as no row. It stands in front of the shared platform
    route, so a change to that shared row can never begin governing this tenant's
    cash-out. A batch submitted against it is refused as unconfigured rather than paid
    unseen, and goes out only when somebody confirms it in as many words, recorded
    against them.

    A tenant that wants the default checker and high-value ladder asks for it, through
    the seeding command, and that publishes the steps and the groups they name.

    Non-destructive by contract: a tenant that already has a route keeps exactly what is
    configured, steps included, which is what makes this safe to run again for the
    second entity in the same tenant.
    """
    from .approvals import ensure_tenant_approval_templates

    if entity.tenant_id is None:  # Platform-level books have no tenant to seed for.
        return
    ensure_tenant_approval_templates(entity.tenant, with_default_stages=False)
