from __future__ import annotations

import logging
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.forms.models import model_to_dict

logger = logging.getLogger("vs_audit")

# ---------------------------------------------------------------------------
# Summary templates - used when the caller doesn't supply a summary string.
# Keys match AuditActionType values.
# Placeholders: {actor} {entity} {entity_type}
# ---------------------------------------------------------------------------
_SUMMARY_TEMPLATES: dict[str, str] = {
    # Generic CRUD
    "CREATE":   "{actor} created {entity_type} {entity}",
    "UPDATE":   "{actor} updated {entity_type} {entity}",
    "DELETE":   "{actor} deleted {entity_type} {entity}",

    # Identity / auth
    "USER_CREATED":             "{actor} created user account for {entity}",
    "USER_INVITED":             "{actor} sent an invitation to {entity}",
    "ACCOUNT_ACTIVATED":        "{entity} activated their account",
    "LOGIN_SUCCESS":            "{entity} logged in successfully",
    "LOGIN_FAILED":             "Failed login attempt for {entity}",
    "TOKEN_REVOKED":            "Session token revoked for {entity}",
    "FORCE_LOGOUT":             "{entity} was forcefully logged out",
    "ACCOUNT_LOCKED":           "{entity}'s account was locked",
    "ACCOUNT_UNLOCKED":         "{entity}'s account was unlocked by {actor}",
    "ACCOUNT_SUSPENDED":        "{entity}'s account was suspended by {actor}",
    "ACCOUNT_REACTIVATED":      "{entity}'s account was reactivated by {actor}",
    "ACCOUNT_DEACTIVATED":      "{entity}'s account was deactivated by {actor}",
    "PASSWORD_RESET_REQUESTED": "{entity} requested a password reset",
    "PASSWORD_RESET":           "Password reset completed for {entity}",
    "PASSWORD_CHANGED":         "{entity} changed their password",
    "EMAIL_CHANGED":            "{entity}'s email address was changed",

    # Data import
    "DATA_FILE_UPLOADED":        "{actor} uploaded a data file",
    "DATA_IMPORT_STARTED":       "{actor} started a data import ({entity})",
    "DATA_IMPORT_ROW_PROCESSED": "Import row processed: {entity}",
    "DATA_IMPORT_COMPLETED":     "Data import completed: {entity}",
    "DATA_IMPORT_FAILED":        "Data import failed: {entity}",
    "DATA_IMPORT_ROLLED_BACK":   "Data import rolled back: {entity}",

    # RBAC
    "ROLE_ASSIGNED":       "{actor} assigned a role to {entity}",
    "ROLE_CHANGED":        "{actor} changed role for {entity}",
    "PERMISSION_CHANGED":  "{actor} changed permissions for {entity}",

    # Proxy lifecycle and request fallbacks. Lifecycle callers normally supply
    # a richer summary; these templates keep manually emitted events readable.
    "IMPERSONATION_STARTED": "{actor} started a proxy session as {entity}",
    "IMPERSONATION_ENDED":   "{actor} ended the proxy session as {entity}",
    "PROXY_CHANGE":          "{actor} made a change while proxied as {entity}",
    "PROXY_ACTION_FAILED":   "{actor}'s action failed while proxied as {entity}",

    # Other
    "CONFIG_CHANGED":          "{actor} changed system configuration: {entity}",
    "FINANCIAL_TRANSACTION":   "Financial transaction recorded for {entity}",
    "PROCUREMENT_ACTION":      "Procurement action for {entity}",
    "EXPORT_REQUESTED":        "{actor} requested an audit log export",
    "EXPORT_COMPLETED":        "Audit log export completed",
    "EXPORT_FAILED":           "Audit log export failed",
    "CUSTOM":                  "{actor} performed an action on {entity}",
}


def _build_summary(action_type: str, actor_user, entity_label: str, entity_type: str) -> str:
    """Generate a readable one-sentence summary from available context."""
    template = _SUMMARY_TEMPLATES.get(action_type, "{actor} performed {action_type} on {entity}")

    actor = "System"
    if actor_user is not None:
        actor = (
            getattr(actor_user, "full_name", None)
            or getattr(actor_user, "get_full_name", lambda: "")()
            or getattr(actor_user, "email", None)
            or "Unknown user"
        )

    entity = entity_label or entity_type or "unknown"
    entity_type_label = entity_type or "record"

    return template.format(
        actor=actor,
        entity=entity,
        entity_type=entity_type_label,
        action_type=action_type,
    )


def resolve_event_tenant(tenant):
    """Return the tenant an event belongs to, inheriting the request's when sound.

    ``AuditEvent.tenant`` is nullable and most emitters never passed one, so the
    column was empty on all but the school and branch rows. That is worse than a
    missing feature: an investigator who narrows the Event Explorer to Bright
    Star sees the two events that happened to carry a tenant, and concludes
    nothing else did. The filter is only worth having if the column is populated,
    and the column is only trustworthy if it is populated *correctly*.

    Three rules, in order.

    **An explicit tenant always wins.** A caller that names one knows something
    this function cannot: ``SchoolCreateSerializer`` writes Bright Star's tenant
    onto the creation event while the Codex staffer who pressed the button is
    asserting ``?tenant=codex``. Only ``None`` - "I did not say" - is filled in
    here.

    **A business tenant in the ambient context is inherited.** ``?tenant=`` is
    mandatory on every authenticated request and ``TenantJWTAuthentication``
    refuses any slug but the caller's own unless the caller is platform staff on
    a view that opts in. So for a SCHOOL or ORGANIZATION tenant the ambient value
    is the boundary the whole request is authorised inside - the one
    ``TenantAwareManager`` scopes its querysets to - and the event cannot be
    about somebody else without a separate authorisation bug having let the
    request reach another tenant's rows in the first place. That is what makes
    inheriting it safe, and it is why the PLATFORM case below is different in
    kind rather than merely riskier.

    **The PLATFORM tenant is not inherited; the event stays null.** Asserting
    ``?tenant=codex`` means "I am acting as Codex", and says nothing whatever
    about whose data is being touched - a Codex staffer creates Bright Star while
    asserting ``codex``, and every role assignment and onboarding row written
    during that request belongs to Bright Star, not to Codex. Stamping ``codex``
    on them would be worse than leaving them empty: the Bright Star filter would
    still miss them *and* the Codex filter would show somebody else's school.
    Null is the honest answer, and it is the answer the platform already gives
    elsewhere - ``vs_config.services.scopes.resolve_request_scope`` collapses a
    PLATFORM assertion to ``tenant=None`` and calls that the platform layer.

    Nothing is inherited outside a request at all. A Celery task, a management
    command and the login endpoint (which is unauthenticated, so authentication
    never ran) all see an empty context, and ``TenantContextCleanupMiddleware``
    clears it at both ends of every request so a stale value cannot survive into
    the next one. Emitters on those paths that know their tenant pass it
    explicitly - ``log_auth_event`` and ``create_import_audit_log`` do - because
    there is nothing here for them to inherit.
    """
    if tenant is not None:
        return tenant

    from vs_tenants.context import get_current_tenant
    from vs_tenants.models import Tenant

    ambient = get_current_tenant()
    if ambient is None or getattr(ambient, "kind", None) == Tenant.Kind.PLATFORM:
        return None
    return ambient


def emit_audit_event(
    *,
    module_key: str,
    action_type: str,
    entity_type: str,
    entity_id: str,
    actor_user=None,
    effective_user=None,
    tenant=None,
    impersonation_session=None,
    entity_label: str = "",
    severity: str = "INFO",
    status: str = "SUCCESS",
    summary: str = "",
    before_data: dict | None = None,
    diff_data: dict | None = None,
    metadata: dict | None = None,
):
    """
    Central helper: creates an AuditEvent + upserts EntityAuditTrail.

    - actor_user: pass a User instance; if None the event is attributed to SYSTEM.
    - tenant: the customer the event belongs to. Left out, it is inherited from
      the request in flight when that is sound - see :func:`resolve_event_tenant`
      for exactly when it is not. Passing one always wins; passing ``None`` means
      "I did not say", not "definitely nobody", because the pass-through helpers
      in vs_exports and vs_tenants forward their own ``tenant=None`` default and
      must not be able to erase the context that way.
    - summary: auto-generated from action_type + entity context when not provided.
    - Never raises - audit failures must never block business logic.
    - Returns the created AuditEvent, or None on failure.

    The writes below run in their own savepoint, which is what makes the
    promise above true for the callers that matter. Most of them emit from
    inside their own ``transaction.atomic`` block - the school and branch
    serializers, the onboarding effects - and a *database* error here (not a
    Python one) marks the whole enclosing transaction for rollback. Catching
    the exception was not enough: the caller carried on, and its own legitimate
    write was then refused at commit with TransactionManagementError. So an
    audit failure used to be able to destroy the business change it was only
    supposed to describe. Rolling back to a savepoint confines the damage to
    the audit row, which is the stated contract.
    """
    from .models import AuditEvent, AuditActorType, EntityAuditTrail

    try:
        with transaction.atomic():
            from vs_tenants.context import resolve_audit_identity
            actor_user, effective_user, impersonation_session = resolve_audit_identity(
                actor_user, effective_user, impersonation_session,
            )
            actor_type = AuditActorType.USER if actor_user is not None else AuditActorType.SYSTEM
            tenant = resolve_event_tenant(tenant)

            resolved_summary = summary or _build_summary(action_type, actor_user, entity_label, entity_type)

            event = AuditEvent.objects.create(
                module_key=module_key,
                action_type=action_type,
                actor_type=actor_type,
                actor_user=actor_user if actor_type == AuditActorType.USER else None,
                effective_user=effective_user,
                tenant=tenant,
                impersonation_session=impersonation_session,
                entity_type=entity_type,
                entity_id=str(entity_id),
                entity_label=entity_label or "",
                severity=severity,
                status=status,
                summary=resolved_summary,
                before_data=before_data or {},
                diff_data=diff_data or {},
                metadata=metadata or {},
            )

            # The proxy middleware uses this request-local marker to avoid adding a
            # vague fallback event when the feature already recorded the real
            # business action.
            from vs_tenants.context import mark_audit_event_emitted
            mark_audit_event_emitted()

            trail, _ = EntityAuditTrail.objects.get_or_create(
                entity_type=entity_type,
                entity_id=str(entity_id),
                defaults={"entity_label": entity_label or ""},
            )
            # The label was previously written once and never revisited, which
            # was survivable only while entity_id was itself readable. It is
            # not: a trail keyed on a primary key shows an opaque number, and
            # the label is the only human handle on the row. Left frozen, the
            # trail list would still be offering "Bright Star" long after the
            # school became "Bright Star Academy". Costs no extra query -
            # register_event's save carries the field.
            if entity_label and trail.entity_label != entity_label:
                trail.entity_label = entity_label
            trail.register_event(event)

        return event

    except Exception as exc:
        logger.error("emit_audit_event failed [%s/%s entity=%s:%s]: %s", module_key, action_type, entity_type, entity_id, exc)
        return None


class AuditDiffService:
    """
    Helper service for building:
    - before_data
    - after_data
    - diff

    The goal is to produce JSON-safe audit snapshots.
    """

    @staticmethod
    def _json_safe_value(value):
        """
        Convert Python/Django values into JSON-safe values.
        """
        if isinstance(value, (datetime, date)):
            return value.isoformat()

        if isinstance(value, Decimal):
            return str(value)

        if isinstance(value, UUID):
            return str(value)

        if isinstance(value, list):
            return [AuditDiffService._json_safe_value(v) for v in value]

        if isinstance(value, dict):
            return {
                str(k): AuditDiffService._json_safe_value(v)
                for k, v in value.items()
            }

        return value

    @staticmethod
    def to_json_safe_dict(data: dict) -> dict:
        """
        Make a dictionary fully JSON-safe.
        """
        return {
            str(key): AuditDiffService._json_safe_value(value)
            for key, value in data.items()
        }

    @staticmethod
    def model_instance_to_dict(instance, *, include_fields=None, exclude_fields=None) -> dict:
        """
        Convert a Django model instance into a clean dictionary.

        Args:
            instance: Django model instance
            include_fields: optional iterable of allowed fields
            exclude_fields: optional iterable of fields to skip

        Returns:
            JSON-safe dict
        """
        if instance is None:
            return {}

        data = model_to_dict(instance)

        if include_fields:
            data = {k: v for k, v in data.items() if k in include_fields}

        if exclude_fields:
            data = {k: v for k, v in data.items() if k not in exclude_fields}

        return AuditDiffService.to_json_safe_dict(data)

    @staticmethod
    def build_after_data_from_update(
        before_data: dict,
        updates: dict,
    ) -> dict:
        """
        Build after_data by applying an update payload to before_data.

        Useful when you have:
        - old object snapshot
        - validated_data from serializer

        instead of a fully saved new instance.
        """
        merged = deepcopy(before_data)
        for key, value in updates.items():
            merged[key] = AuditDiffService._json_safe_value(value)
        return merged

    @staticmethod
    def diff_dicts(before_data: dict, after_data: dict) -> dict:
        """
        Compare two dictionaries and return only changed fields.

        Returns shape:
        {
            "field_name": {
                "before": old_value,
                "after": new_value
            }
        }
        """
        before_data = before_data or {}
        after_data = after_data or {}

        all_keys = sorted(set(before_data.keys()) | set(after_data.keys()))
        diff = {}

        for key in all_keys:
            before_value = before_data.get(key)
            after_value = after_data.get(key)

            if before_value != after_value:
                diff[key] = {
                    "before": before_value,
                    "after": after_value,
                }

        return diff

    @staticmethod
    def build_audit_snapshot(
        *,
        before_data: dict | None = None,
        after_data: dict | None = None,
    ) -> dict:
        """
        Build the full audit snapshot structure.

        Returns:
        {
            "before_data": {...},
            "after_data": {...},
            "diff": {...}
        }
        """
        before_data = AuditDiffService.to_json_safe_dict(before_data or {})
        after_data = AuditDiffService.to_json_safe_dict(after_data or {})
        diff = AuditDiffService.diff_dicts(before_data, after_data)

        return {
            "before_data": before_data,
            "after_data": after_data,
            "diff": diff,
        }

    @staticmethod
    def from_instances(
        *,
        before_instance=None,
        after_instance=None,
        include_fields=None,
        exclude_fields=None,
    ) -> dict:
        """
        Build audit snapshot from two model instances.
        """
        before_data = AuditDiffService.model_instance_to_dict(
            before_instance,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )
        after_data = AuditDiffService.model_instance_to_dict(
            after_instance,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )

        return AuditDiffService.build_audit_snapshot(
            before_data=before_data,
            after_data=after_data,
        )

    @staticmethod
    def from_instance_and_updates(
        *,
        instance,
        updates: dict,
        include_fields=None,
        exclude_fields=None,
    ) -> dict:
        """
        Build audit snapshot from:
        - existing instance
        - update payload

        Good for serializer update flows.
        """
        before_data = AuditDiffService.model_instance_to_dict(
            instance,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )
        safe_updates = AuditDiffService.to_json_safe_dict(updates or {})
        after_data = AuditDiffService.build_after_data_from_update(
            before_data=before_data,
            updates=safe_updates,
        )

        return AuditDiffService.build_audit_snapshot(
            before_data=before_data,
            after_data=after_data,
        )
