"""The permission keys that guarded fields, as Field Access switches.

Field visibility started as a handful of permission keys checked inside
serializers. Field Access replaced them with per-role Read and Write switches.
This module is the single place that says which key stood for which field and
which access, so nothing has to derive it again at a call site, and the two
data migrations, the verification command and the tests all read the same
table.

It outlives the keys themselves. Migration 0025 converts grants into switches
and migration 0026 deletes the keys that guarded nothing else, so from then on
the entries here describe what a database held before it was migrated, which is
what the verification command needs in order to prove that release day changed
nobody's access. ``survives`` records which keys stayed, because they also
guard a page or an endpoint.

Every entry was checked against the code, not against a design table: each
serializer's ``read_permissions`` and ``write_permissions``, the vendor view's
``_require_sensitive_access``, the quotation request's ``can_view_contacts``
and the movements feed's mask. ``gates`` records what that code enforced at
field level, which is not always what a key's name suggests: several keys gated
only reading, while the value was written on a path that asked for the
endpoint's own key instead.

Translating a key into switches
-------------------------------

For role ``r`` and converted field ``f``::

    read  = r holds f's read key, or f's open default when nothing gates reads
    write = f.writable and read and (r holds f's write key, if one gates writes)

Two consequences are worth stating plainly.

**Write follows Read where nothing gated writes.** Write implies Read is an
invariant of the new model (D10, and a check constraint in the database), so
"may change a value it cannot see" is not expressible. Where a key gated only
reading, the write switch therefore follows the read switch, and a role that
could change the value without holding the read key loses that.
:data:`ACCEPTED_DIFFERENCES` names every field where this bites and why.

**A field whose default is open needs an explicit OFF row.** A role that lacks
the key must end up with the access off, and for a field that is not sensitive
"no row" means on. ``school.students.enrolment_date`` is the one converted
field in that shape: everybody reads it, only ``school.students.manage``
changes it on an existing record, and it is not sensitive. Without a stored
row saying Write off, enforcement day would hand that write to every role.

Rows are written only where the state differs from the field's default, so a
stored row is always a decision and "reset to default" keeps meaning delete.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

#: The two switches a key can stand for.
READ = "READ"
WRITE = "WRITE"

#: Prepended to the reason of a field exception grown from a permission exception.
CONVERSION_REASON_PREFIX = "Converted from permission exception: "

_BOTH = frozenset({READ, WRITE})
_READ_ONLY = frozenset({READ})
_WRITE_ONLY = frozenset({WRITE})


@dataclass(frozen=True)
class KeyConversion:
    """One permission key, the registered fields it guarded, and how.

    ``gates`` holds the accesses the key decided in the serializers and views
    that checked it. ``survives`` is False for a key that guarded fields and
    nothing else, which migration 0026 deletes, and True for one that also
    guards a page or an endpoint and therefore stays.

    ``note`` records where the guard lived, so a reader can check the entry
    against the history of that file without searching for it.
    """

    key: str
    fields: tuple[str, ...]
    gates: frozenset[str]
    survives: bool
    note: str = ""


#: Every key that decided a registered field, and what it decided.
CONVERSIONS: tuple[KeyConversion, ...] = (
    KeyConversion(
        key="procurement.vendor.view_sensitive",
        fields=(
            "procurement.vendor.email",
            "procurement.vendor.phone",
            "procurement.vendor.address",
            "procurement.vendor.tax_id",
            "procurement.vendor.bank_name",
            "procurement.vendor.bank_code",
            "procurement.vendor.bank_account_number",
            "procurement.vendor.bank_account_name",
            "procurement.vendor.contacts",
        ),
        gates=_BOTH,
        survives=False,
        note=(
            "Reads: VendorSerializer.read_permissions. Writes: "
            "vs_procurement/views/vendors.py _require_sensitive_access over "
            "_SENSITIVE_VENDOR_FIELDS, which is the same nine names. The same "
            "key also decides whether a quotation request's invitations carry "
            "vendor contact details (serializers.py get_invitations)."
        ),
    ),
    KeyConversion(
        key="finance.bankaccount.view_sensitive",
        fields=("finance.bankaccount.account_number",),
        gates=_READ_ONLY,
        survives=False,
        note=(
            "BankAccountSerializer.read_permissions. The serializer is never "
            "used for input: the number is written by the bank account create "
            "and update endpoints from the raw body, behind "
            "finance.bankaccount.create and .update."
        ),
    ),
    KeyConversion(
        key="finance.payrollrun.view_sensitive",
        fields=(
            "finance.payrollrun.employee_name",
            "finance.payrollrun.gross_amount",
            "finance.payrollrun.paye_amount",
            "finance.payrollrun.pension_amount",
            "finance.payrollrun.net_amount",
            "finance.payrollrun.components",
            "finance.salary.gross_amount",
            "finance.salary.paye_amount",
            "finance.salary.pension_amount",
            "finance.salary.net_amount",
            "finance.salary.components",
        ),
        gates=_READ_ONLY,
        survives=False,
        note=(
            "PayrollLineSerializer and EmployeeSalarySerializer "
            "read_permissions. One key covers both, deliberately: it governs "
            "seeing a pay figure, while finance.salary.* governs the roster "
            "endpoints. Neither serializer takes input; the figures are "
            "written by the payroll run create and salary create and update "
            "endpoints."
        ),
    ),
    KeyConversion(
        key="payments.virtual_account.view_sensitive",
        fields=(
            "payments.virtual_account.account_number",
            "payments.virtual_account.account_name",
        ),
        gates=_READ_ONLY,
        survives=False,
        note="VirtualAccountSerializer.read_permissions. Provider issued, so nothing writes them.",
    ),
    KeyConversion(
        key="payments.payout.view_sensitive",
        fields=(
            "payments.payout.beneficiary_name",
            "payments.payout.beneficiary_account_number",
            "payments.payout.beneficiary_bank_code",
        ),
        gates=_READ_ONLY,
        survives=False,
        note=(
            "PayoutInstructionSerializer.read_permissions, and the movements "
            "feed in vs_payments/views.py, which masks the same two values "
            "under the feed's own column names."
        ),
    ),
    KeyConversion(
        key="school.students.view_sensitive",
        fields=(
            "school.students.blood_group",
            "school.students.allergies",
            "school.students.conditions",
        ),
        gates=_BOTH,
        survives=False,
        note=(
            "StudentDetailSerializer.read_permissions, and both write maps in "
            "schools/vs_students/constants.py, so a medical value is refused "
            "on enrolment as well as on edit."
        ),
    ),
    KeyConversion(
        key="platform.staff_payroll.view",
        fields=(
            "platform.staff_profile.bank_name",
            "platform.staff_profile.account_name",
            "platform.staff_profile.account_number",
        ),
        gates=_READ_ONLY,
        survives=False,
        note="PlatformStaffProfileSerializer.read_permissions.",
    ),
    KeyConversion(
        key="platform.staff_payroll.manage",
        fields=(
            "platform.staff_profile.bank_name",
            "platform.staff_profile.account_name",
            "platform.staff_profile.account_number",
        ),
        gates=_WRITE_ONLY,
        survives=False,
        note=(
            "PlatformStaffProfileSerializer.write_permissions. A staff member "
            "reading and writing their own details is an owner rule on the "
            "serializer and belongs to enforcement, not to a switch."
        ),
    ),
    KeyConversion(
        key="platform.team.view",
        fields=(
            "platform.team.password_changed_at",
            "platform.team.last_login_at",
            "platform.team.invited_by",
            "platform.team.invitation_email_status",
            "platform.team.invitation_expires_at",
        ),
        gates=_READ_ONLY,
        survives=True,
        note=(
            "UserReadSerializer and UserListSerializer read_permissions "
            "between them. The server records every one of these facts, so "
            "none is writable. The key stays because it also guards pages."
        ),
    ),
    KeyConversion(
        key="school.students.manage",
        fields=("school.students.enrolment_date",),
        gates=_WRITE_ONLY,
        survives=True,
        note=(
            "EDIT_WRITE_PERMISSIONS in schools/vs_students/constants.py. "
            "Reading the date is open to everybody who may open the record, "
            "and enrolling a pupil sets it without the key, which is what "
            "open_on_create carries. The key stays because it also guards "
            "transfers and other whole record acts."
        ),
    ),
    KeyConversion(
        key="import.templates.manage",
        fields=("import.templates.validation_rules",),
        gates=_READ_ONLY,
        survives=True,
        note=(
            "ImportTemplateDetailSerializer.read_permissions. The template "
            "create and update endpoints ask for the same key, so the write "
            "switch following the read switch changes nothing."
        ),
    ),
    KeyConversion(
        key="import.jobs.view",
        fields=(
            "import.jobs.row_payload",
            "import.jobs.normalized_payload",
            "import.jobs.execution_summary",
            "import.jobs.error_details",
            "import.jobs.last_error_code",
            "import.jobs.last_error_message",
        ),
        gates=_READ_ONLY,
        survives=True,
        note=(
            "ImportJobRowResultSerializer and ImportJobDetailSerializer "
            "read_permissions. The engine produces all of them, so none is "
            "writable."
        ),
    ),
    KeyConversion(
        key="import.batches.view",
        fields=("import.batches.file", "import.batches.preview_rows"),
        gates=_READ_ONLY,
        survives=True,
        note=(
            "ImportBatchDetailSerializer.read_permissions. The file is the "
            "upload itself, written by the batch create endpoint behind "
            "import.batches.create."
        ),
    ),
)


#: The key that actually decided a write where no field level check did.
#:
#: Used only to describe what access looked like before conversion. A caller
#: holding one of these could change the value whether or not they could read
#: it, which is the access :data:`ACCEPTED_DIFFERENCES` records as lost.
#: A field whose write path asks for the same key that gates its reads is
#: absent here, because for it nothing changes.
WRITE_REACHED_BY: dict[str, tuple[str, ...]] = {
    "finance.bankaccount.account_number": (
        "finance.bankaccount.create", "finance.bankaccount.update",
    ),
    "finance.payrollrun.employee_name": ("finance.payrollrun.create",),
    "finance.payrollrun.gross_amount": ("finance.payrollrun.create",),
    "finance.payrollrun.paye_amount": ("finance.payrollrun.create",),
    "finance.payrollrun.pension_amount": ("finance.payrollrun.create",),
    "finance.salary.gross_amount": ("finance.salary.create", "finance.salary.update"),
    "finance.salary.paye_amount": ("finance.salary.create", "finance.salary.update"),
    "finance.salary.pension_amount": ("finance.salary.create", "finance.salary.update"),
    "import.batches.file": ("import.batches.create",),
    "import.templates.validation_rules": ("import.templates.manage",),
}


@dataclass(frozen=True)
class AcceptedDifference:
    """One access the conversion is allowed to change, and the reason.

    The conversion preserves what every user can read and write, with the
    exceptions listed here. Each names the field, the switch, the answer
    before and after, and why the change is deliberate, so the verification
    command can allow exactly these and refuse everything else instead of
    skipping a class of differences quietly.

    ``only_without_roles`` marks a difference that exists only for a person who
    holds no role at all, where the evaluator applies the registry defaults by
    design.
    """

    field_key: str
    access: str
    old: bool
    new: bool
    reason: str
    only_without_roles: bool = False


_D10_CLAMP = (
    "Write implies Read, so a role that could change this value without "
    "being able to see it keeps neither. The write path asked for {keys}, not "
    "for the key that gated reading."
)


def _clamped(field_key: str) -> AcceptedDifference:
    keys = " or ".join(WRITE_REACHED_BY[field_key])
    return AcceptedDifference(
        field_key=field_key,
        access=WRITE,
        old=True,
        new=False,
        reason=_D10_CLAMP.format(keys=keys),
    )


#: Every difference the conversion may produce. Anything else is a defect.
ACCEPTED_DIFFERENCES: tuple[AcceptedDifference, ...] = (
    _clamped("finance.bankaccount.account_number"),
    _clamped("finance.payrollrun.employee_name"),
    _clamped("finance.payrollrun.gross_amount"),
    _clamped("finance.payrollrun.paye_amount"),
    _clamped("finance.payrollrun.pension_amount"),
    _clamped("finance.salary.gross_amount"),
    _clamped("finance.salary.paye_amount"),
    _clamped("finance.salary.pension_amount"),
    _clamped("import.batches.file"),
    AcceptedDifference(
        field_key="platform.staff_profile.bank_name", access=WRITE, old=True, new=False,
        reason="Write implies Read: managing payroll details without being able to read them ends.",
    ),
    AcceptedDifference(
        field_key="platform.staff_profile.account_name", access=WRITE, old=True, new=False,
        reason="Write implies Read: managing payroll details without being able to read them ends.",
    ),
    AcceptedDifference(
        field_key="platform.staff_profile.account_number", access=WRITE, old=True, new=False,
        reason="Write implies Read: managing payroll details without being able to read them ends.",
    ),
    AcceptedDifference(
        field_key="school.students.enrolment_date", access=WRITE, old=False, new=True,
        reason=(
            "A person holding no role at all gets the registry defaults, and "
            "this field's default is open. They hold no permission either, so "
            "no endpoint lets them reach the value."
        ),
        only_without_roles=True,
    ),
)


def converted_keys() -> set[str]:
    """Every permission key the conversion reads."""
    return {entry.key for entry in CONVERSIONS}


def converted_field_keys() -> tuple[str, ...]:
    """Every registered field the conversion writes switches for, in order."""
    seen: list[str] = []
    for entry in CONVERSIONS:
        for field_key in entry.fields:
            if field_key not in seen:
                seen.append(field_key)
    return tuple(seen)


def gate_index() -> dict[str, dict[str, str]]:
    """``{field key: {access: permission key}}`` for the converted fields.

    An access missing from a field's entry is one no field level check
    decided: reading such a field was open to whoever could open the record,
    and writing it was decided by the endpoint the write went through.
    """
    index: dict[str, dict[str, str]] = {}
    for entry in CONVERSIONS:
        for field_key in entry.fields:
            gates = index.setdefault(field_key, {})
            for access in entry.gates:
                gates[access] = entry.key
    return index


def default_state(field) -> tuple[bool, bool]:
    """The Read and Write a role gets before anybody sets a switch (D1)."""
    return (not field.sensitive, bool(not field.sensitive and field.writable))


def switch_state(field, held_keys, gates) -> tuple[bool, bool]:
    """The Read and Write switch *held_keys* earns on *field*.

    *gates* is that field's entry from :func:`gate_index`. Reading falls back
    to the field's open default when nothing gates it; writing follows reading,
    because a switch that allowed a write without a read could not be stored.
    """
    read_key = gates.get(READ)
    write_key = gates.get(WRITE)
    can_read = read_key in held_keys if read_key else not field.sensitive
    can_write = bool(
        field.writable
        and can_read
        and (write_key in held_keys if write_key else True)
    )
    return can_read, can_write


def _models(apps=None) -> SimpleNamespace:
    """The models the conversion touches, from *apps* or the live registry.

    The data migration passes its historical registry; a test or a shell
    passes nothing and gets the real models.
    """
    from django.apps import apps as global_apps

    registry = apps or global_apps
    names = (
        "FieldDefinition", "RoleFieldAccess", "PrebuiltRoleFieldAccess",
        "PrebuiltRoleTemplate", "TenantRoleTemplate", "TenantRolePermission",
        "TenantRoleGroup", "GroupPermission", "PrebuiltRolePermission",
        "UserPermissionOverride", "UserFieldAccessOverride", "Permission",
    )
    models = {name: registry.get_model("vs_rbac", name) for name in names}
    models["Tenant"] = registry.get_model("vs_tenants", "Tenant")
    return SimpleNamespace(**models)


def _bulk_create(model, rows) -> int:
    """Insert *rows* through the plain queryset, in batches.

    The switch models carry a per-row scope guard for the API paths that write
    them one at a time. Here it would re-read the field and the role from the
    database once per row, which is the query storm a conversion over a large
    tenant must not have, and it would answer questions this module has
    already answered in bulk: a field the tenant may not hold is never planned,
    and a write switch is never planned on a field nothing can write. The
    migration's historical models carry no guard at all, so taking the same
    route here keeps both paths identical.
    """
    if not rows:
        return 0
    model.objects.get_queryset().bulk_create(rows, batch_size=2000)
    return len(rows)


def _converted_fields(models) -> dict:
    """The converted fields that exist and are active, by key.

    A field the registry has not been synced with yet is simply absent, so the
    conversion writes what it can and stays correct on a database whose seeds
    have not caught up.
    """
    rows = models.FieldDefinition.objects.filter(
        key__in=converted_field_keys(), is_active=True,
    ).only("key", "sensitive", "writable", "scope")
    return {row.key: row for row in rows}


def _role_key_sets(models) -> dict:
    """``{role id: set of converted keys the role effectively holds}``.

    Four queries whatever the number of roles: the role's own grants and
    denies, the groups that carry a converted key, and the roles those groups
    are attached to. The rule is the evaluator's own: direct grants minus
    direct denies, plus the keys reachable through an attached group, and a
    restricted key never travels through a group.
    """
    keys = converted_keys()
    granted: dict[int, set[str]] = {}
    denied: dict[int, set[str]] = {}
    for role_id, key, is_granted in models.TenantRolePermission.objects.filter(
        permission_id__in=keys,
    ).values_list("role_id", "permission_id", "granted"):
        (granted if is_granted else denied).setdefault(role_id, set()).add(key)

    group_keys: dict[object, set[str]] = {}
    for group_id, key in models.GroupPermission.objects.filter(
        permission_id__in=keys, group__is_active=True, permission__is_restricted=False,
    ).values_list("group_id", "permission_id"):
        group_keys.setdefault(group_id, set()).add(key)

    if group_keys:
        for role_id, group_id in models.TenantRoleGroup.objects.filter(
            group_id__in=group_keys,
        ).values_list("role_id", "group_id"):
            granted.setdefault(role_id, set()).update(group_keys[group_id])

    return {
        role_id: held - denied.get(role_id, set())
        for role_id, held in granted.items()
    }


def _rows_for_role(*, fields, gates, held_keys, platform) -> list[tuple[str, bool, bool]]:
    """``(field key, read, write)`` for every switch this role needs stored.

    A state equal to the field's default is left unstored, so a row always
    records a decision. A field the tenant may not hold is skipped: it can
    never be read or written there, and the guard would refuse the row.
    """
    rows = []
    for field_key, field in fields.items():
        if not platform and field.scope != "TENANT":
            continue
        state = switch_state(field, held_keys, gates.get(field_key, {}))
        if state != default_state(field):
            rows.append((field_key, state[0], state[1]))
    return rows


def _tenant_role_plan(models) -> list[tuple[int, str, bool, bool]]:
    """Every ``(role id, field key, read, write)`` a tenant role needs.

    Built from three passes over whole tables rather than a query per role, so
    a tenant with thousands of roles costs the same handful of queries as one
    with three.
    """
    fields = _converted_fields(models)
    if not fields:
        return []
    gates = gate_index()
    key_sets = _role_key_sets(models)

    plan = []
    for role_id, tenant_kind in models.TenantRoleTemplate.objects.values_list(
        "pk", "tenant__kind",
    ):
        rows = _rows_for_role(
            fields=fields,
            gates=gates,
            held_keys=key_sets.get(role_id, frozenset()),
            platform=tenant_kind == "PLATFORM",
        )
        plan.extend((role_id, *row) for row in rows)
    return plan


def _prebuilt_role_plan(models) -> list[tuple[int, str, bool, bool]]:
    """Every ``(prebuilt role id, field key, read, write)`` Codex ships.

    Prebuilt roles are tenant blueprints, so only ``TENANT`` fields belong on
    them, and they carry neither denies nor groups: a default is a plain list
    of keys. Two queries, whatever the size of the library.
    """
    fields = _converted_fields(models)
    if not fields:
        return []
    gates = gate_index()

    key_sets: dict[int, set[str]] = {}
    for role_id, key in models.PrebuiltRolePermission.objects.filter(
        permission_id__in=converted_keys(),
    ).values_list("prebuilt_role_id", "permission_id"):
        key_sets.setdefault(role_id, set()).add(key)

    plan = []
    for role_id in models.PrebuiltRoleTemplate.objects.values_list("pk", flat=True):
        rows = _rows_for_role(
            fields=fields,
            gates=gates,
            held_keys=key_sets.get(role_id, frozenset()),
            platform=False,
        )
        plan.extend((role_id, *row) for row in rows)
    return plan


def _override_plan(models) -> list[dict]:
    """The field exceptions that the permission exceptions become.

    One row per access the key gates, carrying the original mode, expiry and
    author. An ALLOW on a restricted key is left behind because it confers
    nothing: the permission evaluator refuses it, so converting it would hand
    somebody access they do not have. A DENY is always carried, because
    taking access away is never an escalation.
    """
    fields = _converted_fields(models)
    if not fields:
        return []
    restricted = set(
        models.Permission.objects.filter(
            key__in=converted_keys(), is_restricted=True,
        ).values_list("key", flat=True)
    )
    by_key: dict[str, list] = {}
    for entry in CONVERSIONS:
        by_key.setdefault(entry.key, []).append(entry)

    plan = []
    overrides = models.UserPermissionOverride.objects.filter(
        permission_id__in=converted_keys(),
    ).values(
        "tenant_id", "user_id", "permission_id", "mode", "reason",
        "created_by_id", "expires_at",
    )
    for override in overrides.iterator():
        key = override["permission_id"]
        if override["mode"] == "ALLOW" and key in restricted:
            continue
        for entry in by_key.get(key, ()):
            for field_key in entry.fields:
                field = fields.get(field_key)
                if field is None:
                    continue
                for access in sorted(entry.gates):
                    if access == WRITE and not field.writable:
                        continue
                    plan.append({
                        "tenant_id": override["tenant_id"],
                        "user_id": override["user_id"],
                        "field_id": field_key,
                        "access": access,
                        "mode": override["mode"],
                        "reason": f"{CONVERSION_REASON_PREFIX}{override['reason']}",
                        "created_by_id": override["created_by_id"],
                        "expires_at": override["expires_at"],
                    })
    return plan


def _override_scope_ok(models, plan) -> list[dict]:
    """Drop exceptions on a field the target's tenant may not hold."""
    fields = _converted_fields(models)
    kinds = dict(models.Tenant.objects.values_list("pk", "kind"))
    return [
        row for row in plan
        if kinds.get(row["tenant_id"]) == "PLATFORM"
        or fields[row["field_id"]].scope == "TENANT"
    ]


def run_conversion(apps=None) -> dict:
    """Write the switches and exceptions the old keys stand for.

    Idempotent: a switch or exception already present is left exactly as it
    is, so a second run writes nothing and an administrator's own decision is
    never overwritten by a rerun. Returns a count per table, for the migration
    and for tests.
    """
    models = _models(apps)
    written = {"role_field_access": 0, "prebuilt_role_field_access": 0, "user_field_access_override": 0}

    plan = _tenant_role_plan(models)
    if plan:
        existing = set(
            models.RoleFieldAccess.objects.filter(
                field_id__in=converted_field_keys(),
            ).values_list("role_id", "field_id")
        )
        rows = [
            models.RoleFieldAccess(
                role_id=role_id, field_id=field_key, can_read=read, can_write=write,
            )
            for role_id, field_key, read, write in plan
            if (role_id, field_key) not in existing
        ]
        written["role_field_access"] = _bulk_create(models.RoleFieldAccess, rows)

    prebuilt = _prebuilt_role_plan(models)
    if prebuilt:
        existing = set(
            models.PrebuiltRoleFieldAccess.objects.filter(
                field_id__in=converted_field_keys(),
            ).values_list("prebuilt_role_id", "field_id")
        )
        rows = [
            models.PrebuiltRoleFieldAccess(
                prebuilt_role_id=role_id, field_id=field_key,
                can_read=read, can_write=write,
            )
            for role_id, field_key, read, write in prebuilt
            if (role_id, field_key) not in existing
        ]
        written["prebuilt_role_field_access"] = _bulk_create(
            models.PrebuiltRoleFieldAccess, rows,
        )

    exceptions = _override_scope_ok(models, _override_plan(models))
    if exceptions:
        # A field reached through two keys must still get one exception row.
        seen = set(
            models.UserFieldAccessOverride.objects.filter(
                field_id__in=converted_field_keys(),
            ).values_list("user_id", "field_id", "access")
        )
        rows = []
        for row in exceptions:
            identity = (row["user_id"], row["field_id"], row["access"])
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(models.UserFieldAccessOverride(**row))
        written["user_field_access_override"] = _bulk_create(
            models.UserFieldAccessOverride, rows,
        )

    return written


def reverse_conversion(apps=None) -> dict:
    """Take back exactly what :func:`run_conversion` writes.

    The switches are found by recomputing the same plan, so a row on a field
    and role pair the conversion would not have touched survives. The
    exceptions are found by the reason the conversion writes, which no other
    path produces.
    """
    models = _models(apps)
    removed = {"role_field_access": 0, "prebuilt_role_field_access": 0, "user_field_access_override": 0}

    pairs = {(role_id, field_key) for role_id, field_key, _, _ in _tenant_role_plan(models)}
    if pairs:
        role_ids = {role_id for role_id, _ in pairs}
        doomed = [
            pk for pk, role_id, field_key in models.RoleFieldAccess.objects.filter(
                role_id__in=role_ids, field_id__in=converted_field_keys(),
            ).values_list("pk", "role_id", "field_id")
            if (role_id, field_key) in pairs
        ]
        removed["role_field_access"] = models.RoleFieldAccess.objects.filter(
            pk__in=doomed,
        ).delete()[0]

    prebuilt_pairs = {
        (role_id, field_key) for role_id, field_key, _, _ in _prebuilt_role_plan(models)
    }
    if prebuilt_pairs:
        role_ids = {role_id for role_id, _ in prebuilt_pairs}
        doomed = [
            pk for pk, role_id, field_key in models.PrebuiltRoleFieldAccess.objects.filter(
                prebuilt_role_id__in=role_ids, field_id__in=converted_field_keys(),
            ).values_list("pk", "prebuilt_role_id", "field_id")
            if (role_id, field_key) in prebuilt_pairs
        ]
        removed["prebuilt_role_field_access"] = models.PrebuiltRoleFieldAccess.objects.filter(
            pk__in=doomed,
        ).delete()[0]

    removed["user_field_access_override"] = models.UserFieldAccessOverride.objects.filter(
        field_id__in=converted_field_keys(),
        reason__startswith=CONVERSION_REASON_PREFIX,
    ).delete()[0]
    return removed
