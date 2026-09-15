"""Who may read and write each registered field.

Field Access sits beside permissions: a permission decides whether a person
may open a record at all, and this decides which registered fields of it they
may see and change. The answer is a :class:`FieldAccessMap`, built once per
request per tenant and consulted by key.

For field ``f`` and user ``u``::

    role_read  = any over u's active roles r: row(r, f).can_read  if row else default read
    role_write = any over u's active roles r: row(r, f).can_write if row else default write
                 (no roles at all: the defaults)

    read  = (role_read or ALLOW READ or ALLOW WRITE) and not DENY READ
    write = (role_write or ALLOW WRITE) and f.writable and not DENY WRITE and not DENY READ
    write = write and read

The most generous role wins, and a personal DENY beats a role and a personal
ALLOW, the same order :func:`vs_rbac.evaluator.get_effective_permissions`
applies to keys. Roles are the ones :func:`vs_rbac.evaluator._active_role_ids`
returns, so a role whose branch is out of service stops counting here exactly
as it stops conferring permissions.

Fixed rules applied before the formula:

* the Vision super admin reads and writes everything;
* a field a non-platform tenant may not hold (not ``TENANT`` scope) is never
  readable or writable there;
* a key that is not a registered, active field is always readable and
  writable, so Field Access stays opt-in field by field.

Branch is accepted for symmetry with the permission evaluator. Enforcement
passes :data:`vs_rbac.evaluator.ANY_BRANCH`: every role a person holds counts
for every record they can open, and which records they can open is branch
visibility's question, not this module's.

The module is not ``field_access.py`` because that name belongs to each
domain app's field declarations.
"""
from __future__ import annotations

from types import MappingProxyType

from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from .evaluator import ANY_BRANCH, _active_role_ids, _normalize_tenant
from .models import (
    FieldDefinition,
    PermissionRegistryRevision,
    PermissionScope,
    TenantRoleTemplate,
    UserFieldAccessOverride,
    tenant_is_platform,
)

_CACHE_ATTR = "_rbac_field_access"


class FieldAccessMap:
    """An immutable answer to "may this person read or write this field?".

    Holds a state for every registered, active field it was built over. A key
    it does not hold (an unregistered or inactive field) reads and writes
    True. A map built with ``open_all`` answers True for every key.
    """

    __slots__ = ("_states", "_open_all")

    def __init__(self, states=None, *, open_all: bool = False):
        object.__setattr__(
            self, "_states",
            MappingProxyType({key: (bool(r), bool(w)) for key, (r, w) in (states or {}).items()}),
        )
        object.__setattr__(self, "_open_all", bool(open_all))

    def __setattr__(self, name, value):
        raise AttributeError("FieldAccessMap is immutable.")

    def __delattr__(self, name):
        raise AttributeError("FieldAccessMap is immutable.")

    def __contains__(self, key) -> bool:
        return key in self._states

    def __len__(self) -> int:
        return len(self._states)

    def keys(self):
        return self._states.keys()

    def _pair(self, key) -> tuple[bool, bool]:
        if self._open_all:
            return True, True
        return self._states.get(key, (True, True))

    def can_read(self, key) -> bool:
        return self._pair(key)[0]

    def can_write(self, key) -> bool:
        return self._pair(key)[1]

    def state(self, key) -> dict:
        read, write = self._pair(key)
        return {"read": read, "write": write}


def _is_evaluable(user, tenant) -> bool:
    """The identity checks :func:`get_effective_permissions` applies, said once."""
    if not user or not getattr(user, "is_authenticated", False) or tenant is None:
        return False
    return getattr(user, "tenant_id", None) == tenant.pk


def _closed_map() -> FieldAccessMap:
    """Every active field closed, for an identity that cannot be evaluated.

    Unregistered keys stay open, as they do for everybody: they are not
    governed by Field Access at all.
    """
    keys = FieldDefinition.objects.filter(is_active=True).values_list("key", flat=True)
    return FieldAccessMap({key: (False, False) for key in keys})


def _role_switches(user, tenant, branch):
    """``(role_count, {field_key: (rows, any_read, any_write)})`` in one query.

    The active roles are LEFT JOINed to their switch rows, so a role with no
    rows still appears once, which is how the number of roles is known without
    a second query. ``rows`` is the number of those roles that carry a row for
    the field; a role without one falls back to the field's default.
    """
    role_ids = set()
    per_field: dict[str, list] = {}
    rows = TenantRoleTemplate.objects.filter(
        pk__in=_active_role_ids(user, tenant, branch),
    ).values_list(
        "pk", "field_access__field_id", "field_access__can_read", "field_access__can_write",
    )
    for role_id, field_key, can_read, can_write in rows:
        role_ids.add(role_id)
        if field_key is None:
            continue
        entry = per_field.setdefault(field_key, [0, False, False])
        entry[0] += 1
        entry[1] = entry[1] or bool(can_read)
        entry[2] = entry[2] or bool(can_write)
    return len(role_ids), per_field


def _evaluate(user, tenant, branch, *, with_overrides: bool) -> dict:
    """Per active field, ``(read, write)`` for *user* in *tenant*.

    Two queries: the active fields, with the user's in-force exceptions folded
    in as two correlated subqueries (the unique constraint allows one mode per
    field and access), and :func:`_role_switches`.
    """
    fields = FieldDefinition.objects.filter(is_active=True)
    columns = ["key", "sensitive", "writable", "scope"]
    if with_overrides:
        in_force = UserFieldAccessOverride.objects.filter(
            tenant=tenant, user=user, field_id=OuterRef("key"),
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        fields = fields.annotate(
            read_mode=Subquery(
                in_force.filter(access=UserFieldAccessOverride.Access.READ).values("mode")[:1]
            ),
            write_mode=Subquery(
                in_force.filter(access=UserFieldAccessOverride.Access.WRITE).values("mode")[:1]
            ),
        )
        columns += ["read_mode", "write_mode"]

    field_rows = list(fields.values_list(*columns))
    role_count, per_field = _role_switches(user, tenant, branch)
    platform = tenant_is_platform(tenant)
    allow, deny = UserFieldAccessOverride.Mode.ALLOW, UserFieldAccessOverride.Mode.DENY

    states = {}
    for row in field_rows:
        key, sensitive, writable, scope = row[:4]
        read_mode, write_mode = row[4:6] if with_overrides else (None, None)
        if not platform and scope != PermissionScope.TENANT:
            states[key] = (False, False)
            continue

        rows, any_read, any_write = per_field.get(key, (0, False, False))
        default_applies = role_count == 0 or rows < role_count
        role_read = any_read or (default_applies and not sensitive)
        role_write = any_write or (default_applies and not sensitive and writable)

        read = (role_read or read_mode == allow or write_mode == allow) and read_mode != deny
        write = (
            (role_write or write_mode == allow)
            and writable
            and write_mode != deny
            and read_mode != deny
        )
        states[key] = (read, write and read)
    return states


def get_field_access(user, tenant=None, branch=ANY_BRANCH) -> FieldAccessMap:
    """What *user* may read and write in *tenant* right now.

    Memoised on the user instance beside ``_rbac_effective_perms``, keyed by
    ``(tenant.pk, branch key)`` and checked against
    :class:`PermissionRegistryRevision`, so a field deactivated mid-request
    stops counting. A switch or exception change needs no invalidation: the
    memo lives as long as the request's user object.

    A cold call costs three queries, whatever the number of roles, fields and
    exceptions: the registry revision, the fields with exceptions, and the
    roles with switches. On the platform tenant the super admin check adds one
    unless the permission gate has already memoised it.

    An identity that cannot be evaluated (anonymous, no tenant, or a tenant
    that is not the user's own) gets every active field closed.
    """
    tenant = _normalize_tenant(user, tenant=tenant)
    if not _is_evaluable(user, tenant):
        return _closed_map()

    cache_key = (
        tenant.pk,
        branch if branch is ANY_BRANCH else getattr(branch, "pk", None),
    )
    revision = PermissionRegistryRevision.current()
    cache = getattr(user, _CACHE_ATTR, None)
    if cache is not None and cache_key in cache:
        cached_revision, cached_map = cache[cache_key]
        if cached_revision == revision:
            return cached_map

    from .permissions import is_vision_super_admin

    if tenant_is_platform(tenant) and is_vision_super_admin(user):
        result = FieldAccessMap(open_all=True)
    else:
        result = FieldAccessMap(_evaluate(user, tenant, branch, with_overrides=True))

    if cache is None:
        cache = {}
        setattr(user, _CACHE_ATTR, cache)
    cache[cache_key] = (revision, result)
    return result


def get_role_field_access(user, tenant=None) -> FieldAccessMap:
    """What *user*'s roles alone say, with no personal exception applied.

    For showing an exception beside the state it departs from ("the role hides
    this, allowed for this person"). Never use it for authorisation: it ignores
    exceptions and the super admin bypass, and it is not memoised.
    """
    tenant = _normalize_tenant(user, tenant=tenant)
    if not _is_evaluable(user, tenant):
        return _closed_map()
    return FieldAccessMap(_evaluate(user, tenant, ANY_BRANCH, with_overrides=False))
