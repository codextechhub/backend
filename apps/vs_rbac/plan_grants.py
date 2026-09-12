"""Keeping a tenant's role grants inside the depth its plan reaches.

Entitlements decide what the product offers. Role grants are what a customer
handed its own people, and a reach that shrinks does not rewrite those on its
own. Left alone, Corona drops from Premium to Standard and its bursar still
holds every Advanced payroll key: the menu is gone, the picker no longer lists
them, and the keys are still there, refused at the door by the plan gate,
invisible to the administrator who would remove them, and back in force the
moment the school moves up again for an unrelated reason.

So the grants go with the depth, whichever way the reach shrank: a tier moved
down, an uplift withdrawn, a plan re-applied over grants that carried no depth
at all. What a customer may do and what its roles say it may do are kept one
fact.

Depth, and deliberately nothing else
    Only a band the tenant can no longer reach is taken back. A module that is
    closed rather than shallow leaves every grant standing, because an expired
    subscription, an operator's denial and an override switched off are all
    states meant to be reversed, and the gate already refuses every key behind
    them for as long as they last. A school three weeks late on its invoice is
    refused at the door and then pays; emptying its roles in the meantime would
    cost it a permission structure the payment cannot give back, and nobody
    would be able to say what had been in it.

An uplift that ends on a date
    A deal ends by the clock, and nothing here runs on the clock: this
    deployment has no scheduler at all, which ``core.checks`` warns about on
    every management command. A sweep promising to take these grants back on
    the night an uplift lapsed would be a promise nothing keeps, so none is
    claimed.

    The half that matters is immediate anyway. ``resolved_depth`` reads
    ``ends_at``, so from the second Corona's payroll uplift lapses the gate
    refuses those keys, the picker greys them and the menu drops them. Nobody
    can use a key after the deal that paid for it is over.

    The role rows outlive it, holding keys that do nothing, until the next time
    the school's reach is settled: withdrawing the uplift, a plan change in
    either direction, or ``manage.py apply_plans``. Each of those reconciles
    through here, and each is something a person does rather than something a
    schedule promises.

A role that cannot be settled is reported, never enforced
    Taking a key back re-saves the role through ``set_role_access``, which
    checks the role's permission dependencies. A key the school keeps may
    declare a dependency on one being taken away: Corona's bursar keeps a
    reporting key that requires the payroll key its new tier no longer
    reaches, and that check refuses the save.

    The refusal must not travel any further than the role it concerns. It
    answers a question about one role's internal wiring, and left to propagate
    it aborted a commercial decision: Corona's drop from Premium to Standard
    failed outright, so the school stayed on Premium in the product while its
    invoice said Standard.

    So each role is settled on its own. One that cannot be saved is left
    exactly as it was, whole rather than half written, and named twice: in the
    audit trail, and in the response the operator who made the change is
    already reading. Every other role in the tenant is settled regardless.

    The report reaches that response through ``unsettled_roles``, a list the
    caller owns and this fills. It travels beside the return value rather than
    inside it because the writes that settle a tenant's reach answer with the
    row they wrote, and that is their callers' contract; a role nobody could
    settle is a second fact about the same write rather than a replacement for
    the first. Every write that settles takes the list as an argument of the
    settlement itself, so a new one cannot perform a settlement without saying
    where its report goes.

    Nothing is taken away to force the save through. The kept key is inside
    the depth the school still pays for, and dropping it because something it
    depended on left would charge the school for a rule it never agreed to.

Nothing retries a reported role; it waits for a person
    The refusal is deterministic. It comes from the role's own dependency
    graph rather than from a busy database, so re-running the same
    reconciliation produces the same refusal until somebody edits the role.
    There is also nothing here to re-run it: this deployment has no scheduler,
    and a sweep promising to settle these later would be a promise nothing
    keeps.

    Waiting costs the school nothing in the meantime. The keys left behind sit
    outside the depth the plan reaches, so the gate refuses them at the door
    exactly as if they had gone, and the picker greys them. What is left is a
    stale row, not live access.

    Nor is it forgotten. The next settlement of this tenant's reach - a plan
    change in either direction, an uplift written or withdrawn, ``manage.py
    apply_plans`` - tries the role again and reports it again if it still
    cannot be saved.
"""
import logging
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError as ModelValidationError
from rest_framework.exceptions import ValidationError as RequestValidationError

from vs_audit.models import (
    AuditActionType,
    AuditModuleKey,
    AuditSeverity,
    AuditStatus,
)
from vs_config.models import Capability
from vs_config.services.capabilities import BulkCapabilityEvaluator
from vs_config.services.depth import depth_allows

from .audit import record_rbac_audit
from .models import TenantRolePermission, TenantRoleTemplate
from .plan_gate import capability_for_row, tenant_is_provisioned

logger = logging.getLogger("vs_rbac.plan_grants")

#: Recorded on every role access change made here, so an administrator reading
#: an audit trail can tell a revocation the platform made from one the school
#: made itself.
REVOCATION_SOURCE = "plan_downgrade"

#: Recorded instead when the role refused to save. Kept separate from
#: ``REVOCATION_SOURCE`` so "which roles did this plan change leave behind"
#: is a question the trail answers by filtering rather than by inference.
UNSETTLED_SOURCE = "plan_downgrade_blocked"


@dataclass(frozen=True)
class GrantReconciliation:
    """What one settlement of a tenant's role grants did, and what it could not.

    ``revoked`` maps a role key to the keys taken back from it. ``unsettled``
    names the roles left exactly as they were because their own rules refused
    the change, each entry carrying the role, the keys that should have gone
    and the refusal in words. Both are empty when there was nothing to do.

    The entries in ``unsettled`` are plain dictionaries because they are
    rendered straight into an API response and stored in audit metadata, and
    a shape that survives both without a serializer keeps the report one fact
    rather than two.
    """

    revoked: dict[str, list[str]] = field(default_factory=dict)
    unsettled: list[dict] = field(default_factory=list)


#: Used when a caller names no reason of its own. ``set_role_access`` refuses an
#: empty one, and rightly: a permission that vanished from somebody's account
#: with no record of why is the question this system exists to answer.
DEFAULT_REASON = "The plan no longer reaches these permissions."


def revoke_grants_beyond_the_tenants_depth(
    *, tenant, actor, reason="", unsettled_roles=None,
):
    """Take back every role grant the tenant's depth no longer covers.

    Idempotent, and safe after any write that settles what a tenant reaches:
    it asks what is out of reach now rather than what changed, so a caller does
    not have to work out which of several writes narrowed anything.

    Run it after the entitlement rows are written, never before or between
    them. It acts on the answer immediately, and a half-written tier answers
    wrongly in both directions: a module not yet rewritten still reads at the
    old depth, and one rewritten after the revocation has its keys taken on the
    strength of a depth it no longer has.

    Goes through ``set_role_access`` rather than deleting rows, so each
    revocation takes the role's lock, bumps its version and writes an audit
    entry naming what caused it.

    A tenant with no package grants at all is unprovisioned rather than
    unentitled, and nothing is taken from it, exactly as the gate refuses it
    nothing.

    Each role is settled on its own, and one whose own rules refuse the change
    is left exactly as it was and named in the report rather than allowed to
    abort the write that called this. ``set_role_access`` carries its own
    transaction, so a refusal arrives here with that role's savepoint already
    rolled back: the role is whole, the surrounding transaction is still
    usable, and the settlement carries on to the next role. The module
    docstring says why the refusal stops here and what becomes of a role that
    is reported.

    ``unsettled_roles``, when given, is a list this fills with those roles, so
    a caller answering to a person can name them in the response that person is
    reading. It is a parameter of the settlement rather than something read off
    the return value because the callers below this all answer with the row
    they wrote: taking the list here is what stops a write settling a tenant's
    roles with nowhere to report the ones it left behind.

    Returns a :class:`GrantReconciliation`.
    """
    from .services import set_role_access

    if tenant is None or not tenant_is_provisioned(tenant):
        return GrantReconciliation()

    evaluator = BulkCapabilityEvaluator(
        list(Capability.objects.all().prefetch_related("dependency_links")),
        tenant=tenant,
    )
    capabilities: dict[str, object] = {}
    revoked: dict[str, list[str]] = {}
    unsettled: list[dict] = []

    for role in TenantRoleTemplate.objects.filter(tenant=tenant):
        rows = list(
            TenantRolePermission.objects.filter(role=role).select_related(
                "permission",
                "permission__capability",
                "permission__capability__parent",
                "permission__resource",
            )
        )
        keep, denied, lost = [], [], []
        for row in rows:
            if not row.granted:
                denied.append(row.permission_id)
            elif _within_reach(row.permission, evaluator, capabilities):
                keep.append(row.permission_id)
            else:
                lost.append(row.permission_id)
        if not lost:
            continue

        try:
            set_role_access(
                role=role,
                actor=actor,
                reason=reason or DEFAULT_REASON,
                permission_keys=keep,
                # Named rather than omitted: a role whose grants are replaced
                # without an explicit deny set has its denies cleared, and a
                # key denied on purpose becoming merely ungranted hands it back
                # through any group the role carries.
                denied_permission_keys=denied,
                # Groups are left exactly as they are. A group is a named set
                # the customer composed, and emptying one over a depth change
                # would edit its own vocabulary rather than its access; the
                # keys a group carries meet the same gate when they resolve.
                allow_restricted=True,
                source=REVOCATION_SOURCE,
            )
        except (ModelValidationError, RequestValidationError) as exc:
            # The two validation layers only. A database or programming error
            # is a fault to surface, not a role to report.
            unsettled.append(_report_unsettled_role(
                role=role,
                actor=actor,
                reason=reason or DEFAULT_REASON,
                lost=sorted(lost),
                exc=exc,
            ))
            continue
        revoked[role.key] = sorted(lost)

    if revoked:
        logger.info(
            "Revoked grants beyond the depth %s reaches: %s", tenant.slug, revoked,
        )
    if unsettled:
        logger.warning(
            "Roles left standing beyond the depth %s reaches, because their own "
            "permission dependencies refused the change: %s",
            tenant.slug, [entry["role_key"] for entry in unsettled],
        )
    if unsettled_roles is not None:
        unsettled_roles.extend(unsettled)
    return GrantReconciliation(revoked=revoked, unsettled=unsettled)


def unsettled_roles_note(unsettled):
    """The sentence a write appends when it left a role standing beyond the depth.

    A write that completes while a role keeps grants the tenant can no longer
    reach is not a plain success, and somebody who reads only the message
    should not have to open the payload to discover that. The roles themselves
    travel in ``roles_needing_attention``, with the keys involved and the
    refusal in words.

    One sentence serves every surface that settles a tenant's reach, so the
    plan screen and the configuration screen cannot come to describe the same
    event differently. Empty when there is nothing to report, which is what
    lets a caller append it unconditionally.
    """
    if not unsettled:
        return ""
    count = len(unsettled)
    names = ", ".join(entry["role_name"] for entry in unsettled)
    return (
        f" {count} {'role' if count == 1 else 'roles'} kept permissions the new "
        f"depth does not reach and {'needs' if count == 1 else 'need'} "
        f"attention: {names}."
    )


def _report_unsettled_role(*, role, actor, reason, lost, exc):
    """Record one role the settlement could not save, and describe it.

    The audit row is written after ``set_role_access`` has rolled its own
    savepoint back, so it survives the refusal instead of vanishing with it.
    It carries :data:`UNSETTLED_SOURCE` rather than the source a completed
    revocation writes, and the keys that stayed behind, so an administrator
    reading the trail can see which role to open and what to settle inside it.

    Returns the entry handed back to the caller, which is the shape the plan
    endpoints render.
    """
    detail = _refusal_detail(exc)
    record_rbac_audit(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        actor_user=actor,
        entity_type="TenantRoleTemplate",
        entity_id=str(role.pk),
        entity_label=role.name,
        severity=AuditSeverity.WARNING,
        status=AuditStatus.FAILED,
        summary=(
            f"Role '{role.name}' still holds permissions the plan no longer "
            f"reaches, because its own permission dependencies refuse the change"
        ),
        metadata={
            "tenant_id": str(role.tenant_id),
            "reason": reason,
            "source": UNSETTLED_SOURCE,
            "role_key": role.key,
            "permission_keys": lost,
            "detail": detail,
        },
    )
    return {
        "role_key": role.key,
        "role_name": role.name,
        "permission_keys": lost,
        "detail": detail,
    }


def _refusal_detail(exc):
    """The refusal in words, from either validation layer that can raise it.

    ``set_role_access`` reaches two of them. The dependency check raises
    Django's ``ValidationError``, which carries ``messages``; the service's own
    guards raise the REST framework's, which carries ``detail``. Somebody
    reading the report should not have to know which one spoke.
    """
    messages = getattr(exc, "messages", None)
    if messages:
        return " ".join(str(message) for message in messages)
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return " ".join(
            str(item)
            for value in detail.values()
            for item in (value if isinstance(value, list) else [value])
        )
    if isinstance(detail, list):
        return " ".join(str(item) for item in detail)
    return str(detail or exc)


def _within_reach(permission, evaluator, capabilities):
    """Whether one granted key sits inside the depth the tenant reaches.

    True for everything that is not sold by depth. A key with no capability
    behind it is core for every customer, and a key answering to a module
    rather than to one of its bands is in or out with the module itself, which
    is a different question and one nobody buys their way out of.

    The evaluator is asked the two questions separately, because the gate
    collapses them into one refusal and only one of them may take a grant away:
    a module that is closed is a state to be reversed, while a band out of
    depth is what the customer is actually paying for.
    """
    capability = capability_for_row(permission, cache=capabilities)
    if capability is None or capability.parent_id is None:
        return True
    module_id = capability.parent_id
    # A band whose module is outside the evaluated catalogue cannot be answered
    # for, and an unanswerable key keeps its grant.
    if module_id not in evaluator.capabilities:
        return True
    if not evaluator.evaluate(module_id):
        return True
    return depth_allows(capability.depth, evaluator.resolved_depth(module_id))
