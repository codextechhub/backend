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


def reserved_role_keys() -> set[str]:
    """Every role key an approval stage will resolve approvers through.

    Derived from the published workflow templates rather than written down, so
    it cannot fall out of step with them: a stage added tomorrow naming
    ``board-signatory`` reserves that key the moment it is published, with no
    list for anyone to remember to update.

    **Why anything is reserved at all.** A ROLE stage nominates its approvers by
    matching a role *key* inside the requesting tenant, and a tenant role's key
    is slugified from the name whoever created it typed. So "Payout Approver",
    typed into the roles screen by anyone holding role-create, produces the key
    ``payout-approver`` - which is the key the seeded payout ladder resolves. The
    holder is then on the frozen approver list for every payout batch the school
    raises, having been granted no payments permission at all. The ten
    ``*.approve`` permissions that look like they govern this are listed in
    ``vs_rbac.unenforced`` precisely because nothing reads them.

    Two doors follow from this set, and both are needed:

    * :func:`vs_rbac.serializers.tenant._unique_tenant_role_key` refuses to mint
      a new role on one of these keys;
    * :func:`vs_workflow.services.approvers._users_for_role_key` resolves only
      roles flagged ``is_system_role``, so a look-alike that predates the refusal
      confers nothing.

    Reading all four sources because a stage can name its role four ways, and a
    key reserved through only three of them is not reserved.
    """
    from ..models import (
        WorkflowStage,
        WorkflowStageApproverOverride,
        WorkflowStageDynamicRule,
    )

    keys: set[str] = set()
    for model, fields in (
        (WorkflowStage, ("approver_role_key", "approver_role__key")),
        (WorkflowStageApproverOverride, ("approver_role_key",)),
        (WorkflowStageDynamicRule, ("role_key", "role__key")),
    ):
        for row in model.objects.values_list(*fields):
            keys.update(value for value in row if value)
    return keys


def ensure_approver_role(tenant, key: str, *, description: str = ""):
    """Return the tenant's active role for ``key``, creating it if absent.

    Returns ``(role, created)``. Creating the role grants nobody anything; see
    the module docstring for why that is the point rather than a shortfall.

    The role is created with ``is_system_role=True``, and that flag is the whole
    of what separates it from a role a tenant administrator typed the same name
    into. :func:`vs_workflow.services.approvers._users_for_role_key` resolves
    only flagged roles, so provisioning an approver role is the single way a key
    comes to confer approval authority.
    """
    from vs_rbac.models import TenantRoleTemplate

    if tenant is None:
        raise ValueError("A tenant is required to ensure an approver role.")
    if not key:
        raise ValueError("A role key is required to ensure an approver role.")

    # Keys are unique per tenant regardless of status, so a deactivated role of the
    # same key is still the tenant's answer for that key. Its status is returned
    # untouched: reactivating what an administrator switched off would be
    # provisioning overruling a person, and publishing will refuse it loudly
    # instead.
    #
    # ``is_system_role`` is the exception, and is set even on a role found rather
    # than created. Provisioning naming this key is exactly the assertion the flag
    # records, and without this an existing row - seeded before the flag meant
    # anything, or created by an administrator to fill a coverage gap the seeds
    # left - would keep resolving nobody for ever. The flag is not a permission
    # and grants no one anything; who holds the role is still a person's decision.
    existing = TenantRoleTemplate.objects.filter(tenant=tenant, key=key).first()
    if existing is not None:
        if not existing.is_system_role:
            existing.is_system_role = True
            existing.save(update_fields=["is_system_role", "updated_at"])
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
        is_system_role=True,
    ), True
