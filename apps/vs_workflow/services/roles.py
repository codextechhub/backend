"""Provisioning the roles that ROLE-sourced approval stages name.

A ROLE stage resolves its approvers through a role *key*, and publishing a
tenant-scoped template refuses a key that names no role in that tenant
(:func:`vs_workflow.services.templates._resolve_role`). That refusal is right for
a human editing a template - a typo should not publish - but it makes seeding a
brand-new tenant impossible: the tenant has no roles yet, so the very first seed
fails.

The fix is not to relax the check but to make provisioning create what it
depends on. :func:`ensure_approver_role` creates the role and nothing else: it
assigns nobody. An unheld role still resolves to nobody, so a seeded ladder
still parks its first document rather than approving it, which is exactly the
"seeded blocked, not seeded open" contract the seed commands promise. Approval
authority is only ever granted by a person.
"""
from __future__ import annotations


def role_display_name(key: str) -> str:
    """A readable name for a role key: ``payout-approver`` -> ``Payout Approver``."""
    return key.replace("-", " ").replace("_", " ").title()


def ensure_approver_role(tenant, key: str, *, description: str = ""):
    """Return the tenant's active role for ``key``, creating it if absent.

    Returns ``(role, created)``. Creating the role grants nobody anything; see
    the module docstring for why that is the point rather than a shortfall.
    """
    from vs_rbac.models import TenantRoleTemplate

    if tenant is None:
        raise ValueError("A tenant is required to ensure an approver role.")
    if not key:
        raise ValueError("A role key is required to ensure an approver role.")

    # Keys are unique per tenant regardless of status, so a deactivated role of the
    # same key is still the tenant's answer for that key. It is returned untouched:
    # reactivating what an administrator switched off would be provisioning
    # overruling a person, and publishing will refuse it loudly instead.
    existing = TenantRoleTemplate.objects.filter(tenant=tenant, key=key).first()
    if existing is not None:
        return existing, False

    # Names are unique per tenant too; fall back to the key when the readable name
    # is already taken by a role with a different key.
    name = role_display_name(key)
    if TenantRoleTemplate.objects.filter(tenant=tenant, name=name).exists():
        name = key[:80]

    return TenantRoleTemplate.objects.create(
        tenant=tenant,
        key=key,
        name=name,
        description=description or (
            "Approval role required by a workflow stage. Nobody holds it until "
            "an administrator assigns someone."
        ),
        status=TenantRoleTemplate.Status.ACTIVE,
    ), True
