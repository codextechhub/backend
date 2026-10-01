"""A school's own staff rules: starting role, documents, self-service, hiring, leave.

Nine ``vs_config`` definitions, all school-scoped, read through
:func:`resolve_many` so a school's value, the platform's and the definition's
default are inherited exactly as every other setting is. The staff-number rule
is a separate part of Settings, Staff and lives in ``number_policy.py``,
because a branch may hold its own.

Each default is the behaviour a school has before it chooses:

* every new member of staff starts on the role keyed ``teacher``;
* no document is expected on a staff record;
* a person may change their middle name, date of birth, photo and phone on
  their own record, and nothing else;
* a new hire is invited as soon as they are added;
* no leave type has an allowance;
* leave counts Monday to Friday and skips the days the school is closed.

The last is the one default that is not the older behaviour, which counted
every calendar day. ``services/leave.py`` says why.

A value stored by hand at the platform layer can be anything the definition's
type allows, so every read is cleaned here rather than trusted: an unknown
document type, field or leave type is dropped, an allowance out of range reads
as no limit, an empty or nonsensical week reads as Monday to Friday, and a
field in the floor (:data:`~..constants.SELF_EDIT_FLOOR`) is never
self-editable whatever is stored. A bad stored value costs a school its own
rule, never a record.

Writes go through ``set_value``, which checks the definition's scope and
records ``config.value.updated`` in the audit trail. A value already in force
is not written again, so saving the screen unchanged leaves no audit rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from django.db import transaction

from ..constants import (
    CFG_HIRE_APPROVAL,
    CFG_LEAVE_ALLOWANCES,
    CFG_LEAVE_GROUPS,
    CFG_LEAVE_OVERRIDES,
    CFG_LEAVE_EXCLUDE_CLOSURES,
    CFG_LEAVE_WORKING_DAYS,
    CFG_REQUIRED_DOCUMENTS,
    CFG_SELF_EDITABLE,
    CFG_STARTING_ROLE,
    DEFAULT_SELF_EDITABLE,
    DEFAULT_STARTING_ROLE_KEY,
    DEFAULT_WORKING_DAYS,
    LEAVE_ALLOWANCE_MAX,
    SELF_EDIT_FLOOR,
    SELF_EDIT_FLOOR_LABELS,
    DocumentType,
    LeaveType,
)
from ..exceptions import StaffSettingNotRegistered

_RULE_KEYS = (
    CFG_STARTING_ROLE, CFG_REQUIRED_DOCUMENTS, CFG_SELF_EDITABLE, CFG_HIRE_APPROVAL,
    CFG_LEAVE_ALLOWANCES, CFG_LEAVE_GROUPS, CFG_LEAVE_OVERRIDES,
    CFG_LEAVE_WORKING_DAYS, CFG_LEAVE_EXCLUDE_CLOSURES,
)

_LEAVE_KEYS = (
    CFG_LEAVE_ALLOWANCES, CFG_LEAVE_GROUPS, CFG_LEAVE_OVERRIDES,
    CFG_LEAVE_WORKING_DAYS, CFG_LEAVE_EXCLUDE_CLOSURES,
)


def resolve_many(keys, *, tenant, branch=None):
    """``{key: (value, row)}`` for each active definition among *keys*.

    One query for the definitions and one per key through ``resolve_value``,
    so the inheritance order is vs_config's own. A key with no active
    definition is absent from the answer.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value

    definitions = ConfigurationDefinition.objects.filter(
        key__in=list(keys), is_active=True,
    )
    return {
        definition.key: resolve_value(definition, tenant=tenant, branch=branch)
        for definition in definitions
    }


def _values(tenant, keys) -> dict:
    if tenant is None:
        return {}
    return {key: value for key, (value, _) in resolve_many(keys, tenant=tenant).items()}


# ── The editable-field vocabulary ──────────────────────────────────────────

def _registered_fields():
    """The staff register's Field Access declarations, in their declared order."""
    from vs_rbac.field_registry import get_declaration

    declaration = get_declaration("school", "teachers")
    return declaration.fields if declaration is not None else ()


def _update_surface() -> frozenset:
    """The fields the record's PATCH accepts, which is all a person could send."""
    from ..serializers import StaffUpdateSerializer

    return frozenset(StaffUpdateSerializer.Meta.fields)


def self_editable_options() -> list[dict]:
    """The fields a school may let staff change about themselves.

    Registered, writable, carried by the record's PATCH, and outside the floor.
    """
    surface = _update_surface()
    return [
        {"value": spec.name, "label": spec.label}
        for spec in _registered_fields()
        if spec.writable and spec.name in surface and spec.name not in SELF_EDIT_FLOOR
    ]


def self_editable_locked() -> list[dict]:
    """The floor, labelled, for the screen to show as never self-editable."""
    labels = {spec.name: spec.label for spec in _registered_fields()}
    labels.update(SELF_EDIT_FLOOR_LABELS)
    return [
        {"value": name, "label": labels.get(name, name.replace("_", " ").capitalize())}
        for name in SELF_EDIT_FLOOR
    ]


# ── Cleaning a stored value ────────────────────────────────────────────────

def _role_key(value) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_STARTING_ROLE_KEY


def _documents(value) -> tuple:
    """The stored list, cleaned to known types, in the checklist's own order."""
    if not isinstance(value, (list, tuple)):
        return ()
    wanted = {str(v) for v in value}
    return tuple(v for v in DocumentType.values if v in wanted)


def _self_editable(value) -> tuple:
    """The stored list, cleaned to the options, in the declared order."""
    if not isinstance(value, (list, tuple)):
        return DEFAULT_SELF_EDITABLE
    wanted = {str(v) for v in value}
    return tuple(o["value"] for o in self_editable_options() if o["value"] in wanted)


def _allowance(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= LEAVE_ALLOWANCE_MAX else None


def _allowances(value) -> dict:
    """Every leave type, each an allowance in days or None for no limit."""
    stored = value if isinstance(value, dict) else {}
    return {code: _allowance(stored.get(code)) for code in LeaveType.values}


def _groups(value) -> tuple:
    """Valid, uniquely named leave groups from a stored configuration value."""
    if not isinstance(value, list):
        return ()
    groups, seen_ids, seen_names = [], set(), set()
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        try:
            group_id = str(UUID(str(item.get("id", ""))))
        except (ValueError, TypeError, AttributeError):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            continue
        name = name.strip()
        if group_id in seen_ids or name.casefold() in seen_names:
            continue
        groups.append({"id": group_id, "name": name})
        seen_ids.add(group_id)
        seen_names.add(name.casefold())
    return tuple(groups)


def _overrides(value, groups) -> tuple:
    """Only known leave types and groups, with one rule per scope and type."""
    if not isinstance(value, list):
        return ()
    group_ids = {group["id"] for group in groups}
    out, seen = [], set()
    for item in value[:500]:
        if not isinstance(item, dict):
            continue
        branch_id = item.get("branch_id")
        group_id = item.get("group_id") or None
        leave_type = item.get("leave_type")
        days = item.get("days")
        if branch_id is not None and (isinstance(branch_id, bool) or not isinstance(branch_id, int) or branch_id < 1):
            continue
        if group_id is not None and group_id not in group_ids:
            continue
        if branch_id is None and group_id is None:
            continue
        if leave_type not in LeaveType.values or (days is not None and _allowance(days) is None):
            continue
        key = (branch_id, group_id, leave_type)
        if key in seen:
            continue
        seen.add(key)
        out.append({"branch_id": branch_id, "group_id": group_id, "leave_type": leave_type, "days": days})
    return tuple(out)


def _working_days(value) -> tuple:
    if not isinstance(value, (list, tuple)):
        return DEFAULT_WORKING_DAYS
    days = sorted({
        v for v in value
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 7
    })
    return tuple(days) or DEFAULT_WORKING_DAYS


# ── The rules ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LeaveRules:
    """How a school counts and limits leave."""

    allowances: dict = field(default_factory=lambda: _allowances({}))
    groups: tuple = ()
    overrides: tuple = ()
    working_days: tuple = DEFAULT_WORKING_DAYS
    exclude_closures: bool = True

    def allowance_for(self, leave_type, staff=None):
        """Resolve a person's most specific allowance for one leave type.

        A combined branch and group rule wins, then the person's group, then
        their main posting's branch, then the school-wide default. A missing
        rule inherits; an explicit null rule removes a limit.
        """
        if staff is not None:
            branch_id = staff.branch_id
            group_id = staff.leave_group or None
            scopes = (
                (branch_id, group_id), (None, group_id), (branch_id, None),
            )
            for wanted_branch, wanted_group in scopes:
                if wanted_branch is None and wanted_group is None:
                    continue
                for row in self.overrides:
                    if (row["leave_type"] == leave_type
                            and row["branch_id"] == wanted_branch
                            and row["group_id"] == wanted_group):
                        return row["days"]
        return self.allowances.get(leave_type)

    def as_dict(self, tenant=None) -> dict:
        from vs_tenants.models import Branch

        return {
            "allowances": dict(self.allowances),
            "groups": list(self.groups),
            "overrides": list(self.overrides),
            "branch_options": list(
                Branch.objects.filter(tenant=tenant).order_by("name").values("id", "name")
            ) if tenant is not None else [],
            "leave_types": [
                {"value": value, "label": label} for value, label in LeaveType.choices
            ],
            "working_days": list(self.working_days),
            "exclude_closures": self.exclude_closures,
        }


@dataclass(frozen=True)
class StaffRules:
    starting_role: str = DEFAULT_STARTING_ROLE_KEY
    required_documents: tuple = ()
    self_editable_fields: tuple = DEFAULT_SELF_EDITABLE
    hire_requires_approval: bool = False
    leave: LeaveRules = field(default_factory=LeaveRules)

    def as_dict(self, tenant) -> dict:
        """The body of ``GET /v1/i/me/staff/rules/``.

        Every list of choices the screen offers travels with the values, so the
        screen carries no copy of any of them. ``starting_role_options`` is the
        school's active roles; a role the saver could not grant is still listed
        and is refused on save with the reason.
        """
        from vs_rbac.models import TenantRoleTemplate

        roles = TenantRoleTemplate.objects.filter(
            tenant=tenant, status="ACTIVE",
        ).order_by("name").values_list("key", "name")
        return {
            "starting_role": self.starting_role,
            "starting_role_options": [
                {"value": key, "label": name} for key, name in roles
            ],
            "required_documents": list(self.required_documents),
            "document_types": [
                {"value": value, "label": label} for value, label in DocumentType.choices
            ],
            "self_editable_fields": list(self.self_editable_fields),
            "self_editable_options": self_editable_options(),
            "self_editable_locked": self_editable_locked(),
            "hire_requires_approval": self.hire_requires_approval,
            "leave": self.leave.as_dict(tenant),
        }


def _leave_from(found: dict) -> LeaveRules:
    groups = _groups(found.get(CFG_LEAVE_GROUPS))
    return LeaveRules(
        allowances=_allowances(found.get(CFG_LEAVE_ALLOWANCES)),
        groups=groups,
        overrides=_overrides(found.get(CFG_LEAVE_OVERRIDES), groups),
        working_days=_working_days(found.get(CFG_LEAVE_WORKING_DAYS)),
        exclude_closures=found.get(CFG_LEAVE_EXCLUDE_CLOSURES, True) is not False,
    )


def read_staff_rules(tenant) -> StaffRules:
    """All nine rules in one read, with defaults when there is no tenant.

    Values resolve through the configuration service's scope hierarchy.
    """
    found = _values(tenant, _RULE_KEYS)
    return StaffRules(
        starting_role=_role_key(found.get(CFG_STARTING_ROLE)),
        required_documents=_documents(found.get(CFG_REQUIRED_DOCUMENTS)),
        self_editable_fields=_self_editable(found.get(CFG_SELF_EDITABLE)),
        hire_requires_approval=found.get(CFG_HIRE_APPROVAL) is True,
        leave=_leave_from(found),
    )


def directory_rules(tenant) -> tuple[str, tuple]:
    """The starting role's key and the required documents, in one read.

    What the staff directory's page reads beside its rows: three queries
    rather than four.
    """
    found = _values(tenant, (CFG_STARTING_ROLE, CFG_REQUIRED_DOCUMENTS))
    return (
        _role_key(found.get(CFG_STARTING_ROLE)),
        _documents(found.get(CFG_REQUIRED_DOCUMENTS)),
    )


def starting_role_key(tenant) -> str:
    return _role_key(_values(tenant, (CFG_STARTING_ROLE,)).get(CFG_STARTING_ROLE))


def required_documents(tenant) -> tuple:
    return _documents(_values(tenant, (CFG_REQUIRED_DOCUMENTS,)).get(CFG_REQUIRED_DOCUMENTS))


def self_editable_fields(tenant) -> frozenset:
    return frozenset(_self_editable(
        _values(tenant, (CFG_SELF_EDITABLE,)).get(CFG_SELF_EDITABLE, DEFAULT_SELF_EDITABLE),
    ))


def hire_requires_approval(tenant) -> bool:
    return _values(tenant, (CFG_HIRE_APPROVAL,)).get(CFG_HIRE_APPROVAL) is True


def leave_rules(tenant) -> LeaveRules:
    return _leave_from(_values(tenant, _LEAVE_KEYS))


@transaction.atomic
def write_staff_rules(
    tenant, actor, *, starting_role, required_documents, self_editable_fields,
    hire_requires_approval, leave, reason="",
) -> StaffRules:
    """Store the school's staff rules. The caller has already validated them.

    Refused with ``STAFF_SETTING_NOT_REGISTERED`` before anything is written
    when a definition is missing, so a half-saved set never exists. Allowances
    are stored for the types that have one; a type left out, or sent as null,
    has no limit. Turning hire approval on publishes the school's New staff
    approval ladder where it has none, so the first hire has somewhere to go.
    """
    from vs_config.models import ConfigurationDefinition
    from vs_config.services.resolution import resolve_value, set_value

    allowances = {
        code: days for code, days in _allowances(leave.get("allowances")).items()
        if days is not None
    }
    current = leave_rules(tenant)
    groups = _groups(leave.get("groups", list(current.groups)))
    overrides = _overrides(leave.get("overrides", list(current.overrides)), groups)
    wanted = {
        CFG_STARTING_ROLE: starting_role,
        CFG_REQUIRED_DOCUMENTS: list(_documents(required_documents)),
        CFG_SELF_EDITABLE: list(_self_editable(self_editable_fields)),
        CFG_HIRE_APPROVAL: bool(hire_requires_approval),
        CFG_LEAVE_ALLOWANCES: allowances,
        CFG_LEAVE_GROUPS: list(groups),
        CFG_LEAVE_OVERRIDES: list(overrides),
        CFG_LEAVE_WORKING_DAYS: list(_working_days(leave.get("working_days"))),
        CFG_LEAVE_EXCLUDE_CLOSURES: bool(leave.get("exclude_closures")),
    }
    definitions = {
        d.key: d for d in ConfigurationDefinition.objects.filter(
            key__in=list(wanted), is_active=True,
        )
    }
    for key in wanted:
        if key not in definitions:
            raise StaffSettingNotRegistered(key=key)

    why = (reason or "").strip() or "Staff rules set from Staff settings."
    for key, value in wanted.items():
        definition = definitions[key]
        current, _ = resolve_value(definition, tenant=tenant)
        if current == value:
            continue
        set_value(
            definition=definition, value=value, actor=actor, tenant=tenant,
            reason=why,
        )

    if hire_requires_approval:
        from ..approvals import ensure_hire_approval_template

        ensure_hire_approval_template(tenant, created_by=actor)
    return read_staff_rules(tenant)
