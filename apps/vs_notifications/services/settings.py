"""The single truth source for which channels fire for an event, and why.

Called by dispatch.py before creating Notification records, and by the settings
API to compute the effective matrix (bulk variant - one query for all event
types instead of one per event type).

There is NO fail-open behaviour: the principled fallback for a missing setting
row is the event type's default_enabled. Resolution layers, most specific wins:

    branch row → tenant row → platform row → default_enabled

The branch layer is consulted only when all three hold: a branch was named, the
event type is ``branch_scoped``, and the branch belongs to the tenant being
resolved. The last condition is what keeps a branch from crossing a tenant
boundary: a school's ticket escalated to platform staff resolves against the
platform tenant, and the school's branch has no say there.

The service layer must NOT depend on the thread-local tenant context (Celery
tasks have none), so it reads through `all_objects` and scopes explicitly.
"""
from django.db.models import Q

#: Provenance labels, as the settings API reports them in ``source``.
SOURCE_BRANCH = "branch"
SOURCE_TENANT = "tenant"
SOURCE_PLATFORM = "platform"
SOURCE_DEFAULT = "default"


def branch_applies(tenant, branch) -> bool:
    """Whether *branch* may speak for recipients owned by *tenant*.

    A branch belongs to exactly one tenant, and its choices apply only there.
    The platform layer (``tenant`` None) has no branches at all.
    """
    return (
        branch is not None
        and tenant is not None
        and getattr(branch, "tenant_id", None) == tenant.pk
    )


def settings_rows(event_types, tenant=None, branch=None) -> list:
    """Every setting row that can decide *event_types* for this scope, in one query.

    Returns dicts with event_type_id / channel / is_enabled / tenant_id /
    branch_id: the platform rows, *tenant*'s whole-tenant rows and, when the
    branch applies to the tenant, that branch's rows. Another branch's rows are
    never fetched, so they cannot leak into a resolve by accident.
    """
    from ..models import NotificationSetting

    scope = Q(tenant__isnull=True)
    if tenant is not None:
        scope |= Q(tenant=tenant, branch__isnull=True)
        if branch_applies(tenant, branch):
            scope |= Q(tenant=tenant, branch=branch)

    return list(
        NotificationSetting.all_objects.filter(
            scope, event_type__in=list(event_types),
        ).values("event_type_id", "channel", "is_enabled", "tenant_id", "branch_id")
    )


# Resolve value and provenance for many event types with one settings query.
def resolve_settings_bulk(event_types, tenant=None, branch=None, rows=None) -> dict:
    """
    Return {event_type_id: {channel: (is_enabled, source)}} for every event type
    given, covering every channel in each event type's supported_channels.

    Logic per event type, in order:
        1. is_active is False  → every supported channel is off, source "default".
           (Platform kill switch; wins over everything, including transactional.)
        2. is_transactional    → every supported channel is on, source "default".
           Transactional events (password resets, invites) bypass settings
           entirely - they always dispatch on their supported channels.
        3. Otherwise overlay the persisted NotificationSetting rows:
               branch row (branch_scoped events only) → tenant row
               → platform row → default_enabled.

    ``rows`` lets a caller that already holds the output of
    :func:`settings_rows` for the same scope pass it in, so the whole operation
    costs a single settings query. Rows from another tenant or another branch
    are ignored rather than trusted, so a caller cannot widen the scope by
    handing in the wrong rows.

    The IN_APP invariant: this function only READS persisted rows. "IN_APP cannot
    be disabled" is enforced where settings are WRITTEN, so a disabled IN_APP row
    should never exist, and a value is never silently overridden here.
    """
    event_types = list(event_types)
    use_branch = branch_applies(tenant, branch)
    tenant_id = getattr(tenant, "pk", None)
    branch_id = branch.pk if use_branch else None

    if rows is None:
        rows = settings_rows(event_types, tenant=tenant, branch=branch)

    # Split into the three layers so the most specific row can win per channel.
    branch_map = {}
    tenant_map = {}
    platform_map = {}
    for row in rows:
        key = (row["event_type_id"], row["channel"])
        row_branch_id = row.get("branch_id")
        if row["tenant_id"] is None:
            platform_map[key] = row["is_enabled"]
        elif tenant_id is None or row["tenant_id"] != tenant_id:
            continue
        elif row_branch_id is None:
            tenant_map[key] = row["is_enabled"]
        elif use_branch and row_branch_id == branch_id:
            branch_map[key] = row["is_enabled"]

    result = {}
    for et in event_types:
        supported = list(et.supported_channels)

        # Inactive event types suppress every channel, including transactional ones.
        if not et.is_active:
            result[et.id] = {ch: (False, SOURCE_DEFAULT) for ch in supported}
            continue

        # Transactional events bypass setting rows so must-send emails cannot be disabled.
        if et.is_transactional:
            result[et.id] = {ch: (True, SOURCE_DEFAULT) for ch in supported}
            continue

        consult_branch = use_branch and et.branch_scoped
        resolved = {}
        for channel in supported:
            key = (et.id, channel)
            if consult_branch and key in branch_map:
                resolved[channel] = (branch_map[key], SOURCE_BRANCH)
            elif key in tenant_map:
                resolved[channel] = (tenant_map[key], SOURCE_TENANT)
            elif key in platform_map:
                resolved[channel] = (platform_map[key], SOURCE_PLATFORM)
            else:
                resolved[channel] = (et.default_enabled, SOURCE_DEFAULT)
        result[et.id] = resolved

    return result


# Resolve channel settings for many event types with one settings query.
def resolve_channels_bulk(event_types, tenant=None, rows=None, school=None, branch=None) -> dict:
    """
    Return {event_type_id: {channel: is_enabled}} for every event type given.

    :func:`resolve_settings_bulk` without the provenance, for callers that only
    need to know what fires. ``school`` is read for its tenant when ``tenant``
    is not given.
    """
    tenant = tenant or getattr(school, "tenant", None)
    resolved = resolve_settings_bulk(event_types, tenant=tenant, branch=branch, rows=rows)
    return {
        et_id: {channel: value for channel, (value, _source) in channels.items()}
        for et_id, channels in resolved.items()
    }


# Resolve channel settings for one event type through the bulk implementation.
def resolve_channels(event_type, tenant=None, school=None, branch=None) -> dict[str, bool]:
    """
    Return {channel: is_enabled} for every channel in event_type.supported_channels.

    Single-event convenience wrapper - delegates to resolve_channels_bulk so
    the layering rules live in exactly one place.
    """
    return resolve_channels_bulk(
        [event_type], tenant=tenant, school=school, branch=branch,
    )[event_type.id]
