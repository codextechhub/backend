# =============================================================================
# vs_notifications / services / settings.py
#
# resolve_channels() / resolve_channels_bulk() — the single truth source for
# which channels should fire for a given (event_type, optional school).
#
# Called by dispatch.py before creating Notification records, and by the
# settings API to compute the effective matrix (bulk variant — one query for
# all event types instead of one per event type).
#
# There is NO fail-open behaviour: the principled fallback for a missing
# setting row is the event type's default_enabled. Resolution layers, most
# specific wins:  school row → platform row → default_enabled.
#
# The service layer must NOT depend on the thread-local tenant context (Celery
# tasks have none), so it reads through `all_objects` and scopes explicitly.
# =============================================================================


# Resolve channel settings for many event types with one settings query.
def resolve_channels_bulk(event_types, tenant=None, rows=None, school=None) -> dict:
    """
    Return {event_type_id: {channel: is_enabled}} for every event type given,
    covering every channel in each event type's supported_channels.

    Logic per event type, in order:
        1. is_active is False  → every supported channel is False.
           (Platform kill switch; wins over everything, including transactional.)
        2. is_transactional    → every supported channel is True.
           Transactional events (password resets, invites) bypass settings
           entirely — they always dispatch on their supported channels.
        3. Otherwise overlay the persisted NotificationSetting rows:
               school row wins → else platform row → else default_enabled.

    All rows for all event types are fetched in ONE query (all_objects: no
    thread-local dependence; the query covers this school's rows + the platform
    school=NULL rows).

    Note on the IN_APP invariant: this function only READS persisted rows. The
    "IN_APP cannot be disabled" rule is enforced where settings are WRITTEN
    (serializer/service), so a disabled IN_APP row should never exist. We do not
    silently override values here — keeping read and write consistent.

    Args:
        event_types:  Iterable of NotificationEventType instances.
        school:       A School instance, or None for a platform-scope resolve.
        rows:         Optional pre-fetched rows — an iterable of dicts with
                      event_type_id / channel / is_enabled / school_id keys,
                      already scoped to (school + platform). Callers that need
                      the raw rows for their own purposes (e.g. the settings
                      matrix provenance) can fetch once and pass them in so the
                      whole operation costs a single settings query.
    """
    tenant = tenant or getattr(school, "tenant", None)
    event_types = list(event_types)

    # ── Fetch every relevant row in one query (unless supplied) ────────────
    if rows is None:
        from ..models import NotificationSetting
        from django.db.models import Q

        scope = Q(school__isnull=True)
        if tenant is not None:
            scope |= Q(tenant=tenant)

        rows = NotificationSetting.all_objects.filter(
            scope,
            event_type__in=event_types,
        ).values("event_type_id", "channel", "is_enabled", "tenant_id")

    # Split into the two layers so the school row can win per channel.
    school_map = {}
    platform_map = {}
    for row in rows:
        key = (row["event_type_id"], row["channel"])
        if row["tenant_id"] is None:
            platform_map[key] = row["is_enabled"]
        else:
            school_map[key] = row["is_enabled"]

    # ── Resolve each event type against the shared maps ────────────────────
    result = {}
    for et in event_types:
        supported = list(et.supported_channels)

        # Inactive event types suppress every channel, including transactional ones.
        if not et.is_active:
            result[et.id] = {ch: False for ch in supported}
            continue

        # Transactional events bypass setting rows so must-send emails cannot be disabled.
        if et.is_transactional:
            result[et.id] = {ch: True for ch in supported}
            continue

        # 3. Overlay: school row → platform row → default_enabled
        resolved = {}
        for channel in supported:
            key = (et.id, channel)
            if key in school_map:
                resolved[channel] = school_map[key]
            elif key in platform_map:
                resolved[channel] = platform_map[key]
            else:
                resolved[channel] = et.default_enabled
        result[et.id] = resolved

    return result


# Resolve channel settings for one event type through the bulk implementation.
def resolve_channels(event_type, tenant=None, school=None) -> dict[str, bool]:
    """
    Return {channel: is_enabled} for every channel in event_type.supported_channels.

    Single-event convenience wrapper — delegates to resolve_channels_bulk so
    the layering rules live in exactly one place.

    Args:
        school:  A School instance, or None for a platform-scope resolve.
    """
    return resolve_channels_bulk([event_type], tenant=tenant, school=school)[event_type.id]
