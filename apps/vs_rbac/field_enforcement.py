"""Enforcing Field Access on the surfaces that carry registered fields.

:mod:`vs_rbac.field_evaluator` decides who may read and write each registered
field; this module is the only place that acts on the answer. Every surface
goes through one of four doors, so a rule is never stated twice and never
forgotten in one place while being kept in another:

* :class:`FieldAccessMixin` for DRF serializers;
* :func:`visible` for a response a view builds by hand;
* :func:`assert_writable` for a body a view applies without a serializer;
* :func:`can_read` for a block a view includes or omits as a whole, where the
  block is not a key of a row and so has no name :func:`visible` could match.

What enforcement means
----------------------

A field the caller cannot read is **absent** from the payload, with no
placeholder and no list of the names that were dropped: a caller learns
nothing from a response about fields they may not see. A field they can read
but not change is present, and a detail response names it in
``_read_only_fields`` so a form can grey it rather than offer a save that will
be refused.

A submitted field the caller cannot write is refused with
:class:`FieldWriteDenied` (403), naming every offending field at once, and
nothing is saved. A hidden field and a read-only one are refused in the same
words, so a refusal tells a caller nothing they did not already know.

Three submissions are dropped rather than refused, because none of them
changes anything:

* on update, a value equal to the one already stored, sent back by a form that
  echoes the whole record. Only a caller who may read the field can echo it;
  for one who may not, an equal value is refused like any other, because
  "equal is accepted, different is refused" would otherwise answer the
  question the hidden field exists to keep unanswered;
* on create, an empty or null value, which a form posts for every input its
  user never touched;
* on create, any value of a field declared ``open_on_create``, which belongs
  to the act of creating the record rather than to editing it, and whose write
  switch therefore governs only later corrections.

Who the caller is
-----------------

The caller is the request's user, evaluated in the request's tenant with every
role they hold counting (``ANY_BRANCH``), which is what keeps the create form's
``/me`` map and a record's ``_read_only_fields`` in agreement. The answer is
memoised on the request, so serializing two hundred rows evaluates once.

The escape hatch, kept narrow and named: a render with **no request** in its
context passes everything, which is how management commands, fixtures and
payload construction outside a request behave today. A render that runs inside
a request but legitimately acts for nobody declares itself in
:data:`SYSTEM_SURFACES` and passes :func:`system_context`, so every such render
is listed in one place with the reason it is there.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from inspect import getattr_static

from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import APIException
from rest_framework.fields import SkipField
from rest_framework.relations import PKOnlyObject
from rest_framework.serializers import ListSerializer

from .evaluator import ANY_BRANCH
from .field_evaluator import FieldAccessMap, get_field_access

#: The context key a system-context render sets, through :func:`system_context`.
SYSTEM_CONTEXT_KEY = "field_access_system_surface"

#: What a caller is told about a field they may not write. One sentence for a
#: hidden field and a read-only one alike.
WRITE_DENIED_MESSAGE = "You do not have permission to change this field."

#: Renders that run for nobody, and why each one may.
#:
#: A render inside a request is normally filtered as the person making it. The
#: exception is a document addressed to an outside party: an invoice shows the
#: issuing tenant's own bank account to the payer, and filtering it by whichever
#: member of staff pressed Send would produce a different document each time.
#: Such a render calls :func:`system_context` with its dotted path, and that
#: path is listed here with the reason, so "what is rendered unfiltered" is one
#: list rather than a search through the code.
SYSTEM_SURFACES: dict[str, str] = {
    "vs_finance.documents._issuer_block": (
        "The pay-to block of an invoice, receipt or statement. It shows the "
        "issuing tenant's own collection account to the customer who has to "
        "pay into it, so the document is addressed to the payer and not to "
        "the member of staff who pressed Send. Filtering it by that person "
        "would produce a different document each time, and an invoice with no "
        "account number cannot be paid."
    ),
}

_MAP_ATTR = "_rbac_field_access_map"
_ENTRIES_ATTR = "_rbac_field_access_entries"


class FieldWriteDenied(APIException):
    """A caller submitted fields their roles do not let them write.

    403 rather than 400: the body is well formed and the values may be
    perfectly valid; what is missing is the right to set them. The refusal
    names every offending field in one answer, so a caller correcting a form
    is not sent round the loop once per field.

    ``error_code``, ``message`` and ``extra`` are the names
    :func:`core.exceptions.custom_exception_handler` reads, so the refusal
    comes out in the same envelope as every other error: ``message`` is the
    sentence, and ``error.detail`` carries one message per field.
    """

    status_code = 403
    error_code = "field_write_denied"
    http_status = 403
    default_detail = WRITE_DENIED_MESSAGE

    def __init__(self, fields):
        self.fields = sorted(set(fields))
        self.extra = {name: [WRITE_DENIED_MESSAGE] for name in self.fields}
        self.message = (
            "You do not have permission to change: " + ", ".join(self.fields) + "."
        )
        super().__init__(self.extra)


def assert_system_surface(surface: str) -> None:
    """Raise unless *surface* is a declared render that may act for nobody.

    The declaration a render without a serializer makes. A block built by hand
    from registered fields calls this where it builds them, so that the render
    appears in :data:`SYSTEM_SURFACES` with its reason and cannot be added
    quietly: an undeclared path raises the moment the render runs.
    """
    if surface not in SYSTEM_SURFACES:
        raise ImproperlyConfigured(
            f"'{surface}' is not a declared system surface. A render that runs "
            f"for nobody belongs in vs_rbac.field_enforcement.SYSTEM_SURFACES "
            f"with the reason it may."
        )


def system_context(surface: str) -> dict:
    """The serializer context for a render that acts for nobody.

    *surface* is the render's dotted path, and it must be listed in
    :data:`SYSTEM_SURFACES` with its reason.
    """
    assert_system_surface(surface)
    return {SYSTEM_CONTEXT_KEY: surface}


@dataclass(frozen=True)
class _Entry:
    """One registered field, under the name a client sends and receives it by."""

    key: str
    open_on_create: bool


def _split_resource(resource: str) -> tuple[str, str]:
    parts = (resource or "").split(".")
    if len(parts) != 2 or not all(parts):
        raise ImproperlyConfigured(
            f"'{resource}' is not a field resource. Name it "
            f"'<module>.<resource>', as the registry does."
        )
    return parts[0], parts[1]


def _load_entries(resource: str) -> dict[str, _Entry]:
    """Every active field of *resource*, keyed by each name a client uses.

    A field reaching clients under several names (``invited_by`` as
    ``invited_by_id`` and ``invited_by_name``) appears once per name, so one
    switch covers all of them.
    """
    from .models import FieldDefinition

    module, name = _split_resource(resource)
    rows = FieldDefinition.objects.filter(
        resource__module_id=module, resource__name=name, is_active=True,
    ).values_list("key", "name", "api_names", "open_on_create")
    entries: dict[str, _Entry] = {}
    for key, field_name, api_names, open_on_create in rows:
        for api_name in api_names or [field_name]:
            entries[api_name] = _Entry(key=key, open_on_create=open_on_create)
    return entries


def _entries_for(request, resource: str) -> dict[str, _Entry]:
    """The resource's fields, read once per request however many rows are served."""
    if request is None:
        return _load_entries(resource)
    cache = getattr(request, _ENTRIES_ATTR, None)
    if cache is None:
        cache = {}
        setattr(request, _ENTRIES_ATTR, cache)
    if resource not in cache:
        cache[resource] = _load_entries(resource)
    return cache[resource]


def _access_for(request) -> FieldAccessMap:
    """What the request's caller may read and write, evaluated once per request.

    The tenant is the request's, falling back to the user's own, and every
    role the user holds counts (``ANY_BRANCH``): which records they may open
    at all is branch visibility's question, not this one's.
    """
    user = getattr(request, "user", None)
    tenant = getattr(request, "tenant", None) or getattr(user, "tenant", None)
    tenant_id = getattr(tenant, "pk", None)
    cached = getattr(request, _MAP_ATTR, None)
    if cached is not None and cached[0] == tenant_id:
        return cached[1]
    access = get_field_access(user, tenant=tenant, branch=ANY_BRANCH)
    setattr(request, _MAP_ATTR, (tenant_id, access))
    return access


def _is_empty(value) -> bool:
    """Whether *value* can only leave a field empty.

    ``None`` and a string that is blank once trimmed. A form posts every input
    it carries, filled or not, and the ones its user never touched are not
    writes worth refusing when the record is being created.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def visible(request, resource: str, row: Mapping) -> dict:
    """*row* with every key the caller may not read removed.

    For a view that builds its response by hand instead of through a
    serializer. Keys that are not registered fields of *resource* pass
    untouched, exactly as they do in a serializer.

    A view rendering many rows calls this per row: the caller's access and the
    resource's fields are both read once per request, so the cost stays flat.

    ``request=None`` is the escape hatch the module docstring names: a render
    that acts for nobody keeps every key.
    """
    data = dict(row)
    if request is None:
        return data
    access = _access_for(request)
    entries = _entries_for(request, resource)
    for name in list(data):
        entry = entries.get(name)
        if entry is not None and not access.can_read(entry.key):
            data.pop(name)
    return data


def can_read(request, key: str) -> bool:
    """Whether the request's caller may read the registered field *key*.

    For a block a view includes or omits whole, rather than a key of a row:
    the contact people attached to a quotation request are a list under a name
    of their own, and the beneficiary of a money movement travels under the
    feed's column names rather than the payout's. Neither has a name
    :func:`visible` could match, so the caller names the registry key.

    *key* is the full ``module.resource.name``. A key that is not a registered,
    active field reads True, exactly as an unregistered name does everywhere
    else. ``request=None`` is the escape hatch the module docstring names.
    """
    if request is None:
        return True
    return _access_for(request).can_read(key)


def field_access_payload(user, tenant) -> dict:
    """What *user* may not read or change, keyed by ``module.resource``.

    The map the logged-in user receives with ``/me`` and with the login
    response, for the screens that have no record to ask: a create form and a
    list of columns. Each resource carries the client-facing names that are
    ``hidden``, the ones that are ``read_only``, and, under
    ``open_on_create``, the read-only ones an Add form may still offer because
    the write switch governs them only once the record exists.

    Only resources with something to say appear, and an absent resource or an
    absent name means full access, so a client never has to tell "nothing is
    restricted" from "this resource was not evaluated". A user with nothing
    restricted anywhere receives ``{}``.

    A field the tenant may not hold at all is left out rather than listed as
    hidden, the way the access catalogue leaves it out: a school has no screen
    a CodeX staff member's payroll account could appear on, so naming it here
    would tell every school administrator the shape of a record they will
    never meet.

    On an existing record the response's ``_read_only_fields`` wins, because it
    carries the owner rules this map cannot know: a member of staff reads and
    writes their own payroll bank details whatever their roles say, and the map
    is built from roles alone.
    """
    from .models import FieldDefinition, PermissionScope, tenant_is_platform

    access = get_field_access(user, tenant=tenant, branch=ANY_BRANCH)
    rows = FieldDefinition.objects.filter(is_active=True)
    if not tenant_is_platform(tenant):
        rows = rows.filter(scope=PermissionScope.TENANT)
    rows = rows.values_list(
        "resource__module_id", "resource__name", "key", "name", "api_names",
        "open_on_create",
    )
    payload: dict[str, dict] = {}
    for module, resource_name, key, field_name, api_names, open_on_create in rows:
        readable, writable = access.can_read(key), access.can_write(key)
        if readable and writable:
            continue
        resource = f"{module}.{resource_name}"
        entry = payload.setdefault(
            resource, {"hidden": [], "read_only": [], "open_on_create": []},
        )
        names = api_names or [field_name]
        if not readable:
            entry["hidden"].extend(names)
            continue
        entry["read_only"].extend(names)
        if open_on_create:
            entry["open_on_create"].extend(names)
    for entry in payload.values():
        for names in entry.values():
            names.sort()
    return payload


def assert_writable(request, resource: str, body: Mapping, *, creating: bool = False) -> None:
    """Raise :class:`FieldWriteDenied` for keys of *body* the caller may not write.

    For a view that applies ``request.data`` without a serializer. Stricter
    than :class:`FieldAccessMixin` in one way, deliberately: a serializer can
    drop a harmless submission before it writes anything, while a raw view
    applies the body as it stands, so here an empty value for a field the
    caller cannot write is refused rather than dropped. Pass ``creating=True``
    on a create path, where a field declared ``open_on_create`` is allowed.

    ``request=None`` is the escape hatch the module docstring names: a write
    made for nobody is refused nothing.
    """
    if request is None:
        return
    access = _access_for(request)
    entries = _entries_for(request, resource)
    denied = []
    for name in body:
        entry = entries.get(name)
        if entry is None or access.can_write(entry.key):
            continue
        if creating and entry.open_on_create:
            continue
        denied.append(name)
    if denied:
        raise FieldWriteDenied(denied)


class FieldAccessMixin:
    """Applies the caller's Field Access to one DRF serializer.

    Declare the resource whose fields this serializer carries, and, where a
    detail response should tell a form what to grey, that it is a detail
    serializer::

        class VendorSerializer(FieldAccessMixin, serializers.ModelSerializer):
            field_resource = "procurement.vendor"
            field_access_detail = True

    ``field_aliases`` maps a serializer's own name to the registry name where
    the two differ (``{"contact_phone": "phone"}``); without an entry the
    serializer's name is looked up as it stands. ``owner_rule`` is a
    ``staticmethod(obj, user) -> bool`` naming the person a record is about,
    who reads and writes their own row whatever the switches say: a member of
    staff always sees their own payroll bank details.

    ``_read_only_fields`` is emitted only by a serializer that declares
    ``field_access_detail = True``, and never for a row inside a list, so a
    detail serializer reused with ``many=True`` stays a list. It names the
    fields present in that payload the caller cannot write, and is an empty
    list when there are none, so a client never has to tell "nothing is
    restricted" from "this serializer does not say".

    Nested serializers need nothing: DRF gives a child the root's context, so
    a nested one carrying this mixin is filtered as its own resource.

    A serializer rendered with no request in its context passes everything, as
    does one given :func:`system_context`.
    """

    field_resource: str = ""
    field_aliases: dict[str, str] = {}
    field_access_detail: bool = False
    owner_rule = None

    def _field_access(self) -> FieldAccessMap | None:
        """The caller's access, or ``None`` when this render passes everything."""
        context = self.context
        if context.get(SYSTEM_CONTEXT_KEY):
            return None
        request = context.get("request")
        if request is None:
            return None
        return _access_for(request)

    def _field_entries(self) -> dict[str, _Entry]:
        if not self.field_resource:
            raise ImproperlyConfigured(
                f"{type(self).__name__} uses FieldAccessMixin without naming a "
                f"field_resource."
            )
        return _entries_for(self.context.get("request"), self.field_resource)

    def _entry_for(self, name: str, entries: dict[str, _Entry]) -> _Entry | None:
        return entries.get(self.field_aliases.get(name, name))

    def _is_owner(self, instance) -> bool:
        """Whether *instance* is the record the caller is the subject of."""
        if instance is None:
            return False
        rule = getattr_static(self, "owner_rule", None)
        if rule is None:
            return False
        if isinstance(rule, staticmethod):
            rule = rule.__func__
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        return bool(rule(instance, user))

    def _emits_read_only_fields(self) -> bool:
        return self.field_access_detail and not isinstance(self.parent, ListSerializer)

    def _stored_value(self, name: str):
        """What the stored record shows for *name*, in the shape a client sends.

        Raises :class:`rest_framework.fields.SkipField` where the serializer
        cannot produce one, which the caller treats as "not an echo".
        """
        field = self.fields.get(name)
        if field is None or self.instance is None:
            raise SkipField()
        attribute = field.get_attribute(self.instance)
        check = attribute.pk if isinstance(attribute, PKOnlyObject) else attribute
        return None if check is None else field.to_representation(attribute)

    def _is_echo(self, name: str, value) -> bool:
        """Whether *value* is what the record already holds for *name*."""
        try:
            stored = self._stored_value(name)
        except (SkipField, AttributeError, TypeError, ValueError):
            return False
        if value == stored:
            return True
        if value is None or stored is None:
            return False
        return str(value) == str(stored)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        access = self._field_access()
        if access is None or not isinstance(data, Mapping):
            return data

        read_only: list[str] = []
        if not self._is_owner(instance):
            entries = self._field_entries()
            for name in list(data):
                entry = self._entry_for(name, entries)
                if entry is None:
                    continue
                if not access.can_read(entry.key):
                    data.pop(name)
                elif not access.can_write(entry.key):
                    read_only.append(name)

        if self._emits_read_only_fields():
            data["_read_only_fields"] = read_only
        return data

    def to_internal_value(self, data):
        access = self._field_access()
        if access is None or not isinstance(data, Mapping):
            return super().to_internal_value(data)
        if self._is_owner(self.instance):
            return super().to_internal_value(data)

        entries = self._field_entries()
        creating = self.instance is None
        denied: list[str] = []
        dropped: list[str] = []
        for name in list(data):
            entry = self._entry_for(name, entries)
            if entry is None or access.can_write(entry.key):
                continue
            if creating:
                if entry.open_on_create:
                    continue
                if _is_empty(data[name]):
                    dropped.append(name)
                    continue
            elif access.can_read(entry.key) and self._is_echo(name, data[name]):
                dropped.append(name)
                continue
            denied.append(name)

        if denied:
            raise FieldWriteDenied(denied)
        if dropped:
            data = {name: value for name, value in data.items() if name not in dropped}
        return super().to_internal_value(data)
