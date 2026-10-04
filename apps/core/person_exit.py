"""Resolve whether represented people have left their employment.

Each product registers the employment record that owns this fact. A tenant's
account status cannot answer it: a suspended account may belong to someone
still employed, and a departed person's name remains on historical records.
Callers pass the user ids already present in a response, so one lookup serves
the whole response without opening another tenant's employment records.
"""

from collections.abc import Callable, Iterable


_lookups: dict[str, Callable[[object, set[int]], set[int]]] = {}
_bulk_lookups: dict[
    str, Callable[[list[tuple[object, set[int]]]], set[int]]
] = {}


def register_exit_lookup(
    tenant_kind: str,
    lookup: Callable[[object, set[int]], set[int]],
    *,
    bulk_lookup: Callable[[list[tuple[object, set[int]]]], set[int]] | None = None,
) -> None:
    """Register the employment lookup for one kind of tenant."""
    _lookups[tenant_kind] = lookup
    if bulk_lookup is not None:
        _bulk_lookups[tenant_kind] = bulk_lookup
    else:
        _bulk_lookups.pop(tenant_kind, None)


def exited_user_ids(tenant, user_ids: Iterable[int]) -> set[int]:
    """Return departed user ids among the given ids in this tenant."""
    ids = {int(user_id) for user_id in user_ids if user_id is not None}
    if not ids or tenant is None:
        return set()
    lookup = _lookups.get(tenant.kind)
    return lookup(tenant, ids) if lookup else set()


def exited_states(tenant, user_ids: Iterable[int]) -> dict[int, bool]:
    """Give every represented user an explicit employment-exit flag."""
    ids = {int(user_id) for user_id in user_ids if user_id is not None}
    exited = exited_user_ids(tenant, ids)
    return {user_id: user_id in exited for user_id in ids}


def prime_exit_states(context: dict, user_ids: Iterable[int]) -> dict[int, bool]:
    """Cache one bulk lookup for the people already named in a response.

    A proxy action can name a platform operator and a tenant employee together.
    Grouping by each account's own tenant keeps those employment records separate.
    Unknown ids are not exposed in the result.
    """
    from collections import defaultdict

    from vs_user.models import User

    ids = {int(user_id) for user_id in user_ids if user_id is not None}
    cached = context.setdefault("person_exit_states", {})
    missing = ids - cached.keys()
    if not missing:
        return cached
    groups = defaultdict(set)
    tenants = {}
    for user in User.objects.filter(pk__in=missing).select_related("tenant"):
        groups[user.tenant_id].add(user.pk)
        tenants[user.tenant_id] = user.tenant
    cached.update({user_id: False for user_id in missing})
    groups_by_kind = defaultdict(list)
    for tenant_id, group in groups.items():
        tenant = tenants[tenant_id]
        groups_by_kind[tenant.kind].append((tenant, group))
    for tenant_kind, tenant_groups in groups_by_kind.items():
        bulk_lookup = _bulk_lookups.get(tenant_kind)
        if bulk_lookup is not None:
            exited = bulk_lookup(tenant_groups)
            cached.update({
                user_id: user_id in exited
                for _, group in tenant_groups for user_id in group
            })
            continue
        for tenant, group in tenant_groups:
            cached.update(exited_states(tenant, group))
    return cached


def person_is_exited(context: dict, user_id: int | None) -> bool | None:
    """Read a primed person's flag, or resolve a lone detail person once."""
    if user_id is None:
        return None
    return prime_exit_states(context, (user_id,)).get(int(user_id), False)


def mark_named_people(people: Iterable[dict | None]) -> None:
    """Add flags to an explicitly selected collection of person references."""
    people = [person for person in people if person is not None]
    states = prime_exit_states({}, (person["id"] for person in people))
    for person in people:
        person["is_exited"] = states.get(int(person["id"]), False)
