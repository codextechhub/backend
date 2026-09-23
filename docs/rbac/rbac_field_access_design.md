# rbac_field_access_design

Backend design for **Field Access**: per-role, per-field Read and Write switches
that an administrator sets on a screen, replacing the code-declared
`FieldSecurityMixin` (`apps/vs_rbac/fls.py`).

Status: **approved**. Stages S1 (commit 9557ad6e) and S2 (commit a2634100) are
built. S3 is next; S3 and S4 are not started.

Companion: `rbac_field_access_frontend_prompt.md` (the brief for the frontend
agent).

---

## 1. What it is

An administrator opens **Field Access**, walks **Module → Resource → Field**,
picks a **role**, and turns **Read** and **Write** on or off per field. The change
applies on the next request of every person holding that role, on every surface
the field reaches: screens, lists, exports, feeds and approval documents.

Worked example. Bright Star School, vendor Ade Stationers.

| Field | Storekeeper: Read | Storekeeper: Write |
|---|---|---|
| Name | ON | ON |
| Phone | ON | OFF |
| Bank account number | OFF | OFF |

- Mr. Bello (Storekeeper) opens Ade Stationers. The response carries `phone` and
  lists it in `_read_only_fields`; `bank_account_number` is absent, with no marker.
- His form shows Phone greyed and has no bank section.
- He sends `PATCH {"phone": "0803..."}` from a script. The API answers **403** and
  names `phone`. Nothing is saved.
- The admin turns Bank account number **Read ON** for Storekeeper. Mr. Bello's next
  request carries it.

The permission allocation screen is reshaped the same way: role → Module →
Resource → the resource's permissions, each with a readable label.

---

## 2. Decisions (locked by the owner)

| # | Decision |
|---|---|
| D1 | A field nobody has set: **normal fields default Read ON / Write ON; fields a developer marks sensitive default OFF / OFF.** |
| D2 | **Developers decide which fields appear** in the menu (a code registry). Admins decide who reads and writes them. IDs, tenant links and internal data are never registered. |
| D3 | A person with several roles: **the most generous role wins.** |
| D4 | **Each tenant sets its own**, starting from Codex defaults carried on prebuilt roles. Applies to every tenant kind, platform included. |
| D5 | **No approval.** Every switch change takes effect immediately. |
| D6 | An admin **may** open a field they cannot read themselves. **Every on and every off is audited.** |
| D7 | Read OFF means **hidden completely**: absent from the payload, no placeholder, no list of stripped names. |
| D8 | **One-person exceptions exist**, with the same rules as permission overrides: reason required, optional expiry, never on yourself, a new one replaces the old, a DENY beats everything, audited. |
| D9 | The old `*.view_sensitive` field keys are **converted and removed**. Release day changes nobody's access. Keys that also guard a page stay. |
| D10 | Write ON **implies** Read ON. Read OFF **forces** Write OFF. |
| D11 | Read-only fields are **greyed in the UI**; the API refuses a direct write with 403. |
| D12 | A staff member always reads and writes **their own** payroll bank details, whatever their roles say. |
| D14 | Some fields are **set freely when a record is created** and need Write only to change afterwards. A pupil's enrolment date is the first: whoever enrols the pupil, by form or by spreadsheet, sets it, and only changing it on an existing record needs Write. Declared per field in the registry (`FieldSpec.open_on_create`), and honoured by the write check on create. |
| D15 | **Managing Field Access is a restricted permission; viewing it is not.** Switch changes need no approval (D5), but *who may change switches* does. Adding `*.field_access.manage` to a role you hold goes through the role-change ladder, it cannot travel through a permission group, and nobody can assign a role carrying it without holding it. Otherwise a person who can only edit roles could grant themselves manage and open a sensitive field for their own role with nobody approving. `*.field_access.view` only shows switches, so it stays groupable. |
| D16 | **Frontend Field Access tools also require the tenant's role-view key.** The role editor and one-person field exception picker both consume the role catalogue, so the UI opens only when the actor holds `*.roles.view` alongside the relevant Field Access or override key. The exception endpoints keep their override-key guards. Platform role readers may request a school's access catalogue when administering that school's user. |
| D17 | **The Field Access frontend is committed to main, and reaches users with stage 3.** The Field Access screen, field exceptions and the permission tree picker are built on stages 1 and 2, but until stage 3 no screen follows a switch: an admin who turned Bank account number off for Storekeeper would see the save succeed while Storekeeper still sees every bank number. The code lands on main in each app; deployment is a manual step, so the screens reach users on the first deploy after it, which is meant to be the stage 3 release. |
| D18 | **Ten write abilities end at the switch-over, not one.** Where a key gates only reading and the value is written behind the endpoint's own key, a role can change a number it cannot see. Write implies Read (D10), so those writes end: four payroll-line figures, three salary figures, a bank account number, an import batch file and three staff bank fields. A role that genuinely needs one is given sight of the field, which is a switch. Owner decision, 2026-09-21. |

Fields declared open on create under D14: a pupil's enrolment date, a staff
member's email (written only at creation, so a role without Write on it could
not create staff at all), and a guardian's phone number (required by every
route that adds a guardian, while the correction form may still change it
under its switch; owner decision). A guardian's email, address and occupation
are optional on every create route, so they stay governed by their switches
there.

## 3. Branch: every role counts everywhere (D13)

Mrs. Adeyemi at Bright Star holds **School Nurse pinned to Ikeja Branch** (Allergies
Read ON) and **Teacher pinned to Lekki Branch** (Allergies Read OFF). She can open
Lekki pupils because Teacher lets her, and she **sees allergies for Lekki pupils
too**: her roles are pooled, exactly as every permission gate pools them
(`ANY_BRANCH`). Which records she can open at all stays the job of branch
visibility (`vs_rbac.scoping.visible_branch_ids`), not of Field Access.

This keeps screens and saves in agreement: the `/me` map and a record's
`_read_only_fields` always give the same answer, apart from owner rules.

The rejected alternative counted only roles valid at the record's branch. It would
have made field access stricter than page access, and let a form offer a field that
the save then refused. The enforcement layer always passes `ANY_BRANCH`.

---

## 4. Vocabulary

| Level | Example | Key |
|---|---|---|
| Module | Procurement | `procurement` |
| Resource | Vendors | `procurement.vendor` |
| Field | Bank account number | `procurement.vendor.bank_account_number` |
| Permission | View vendors | `procurement.vendor.view` |

A field's last segment is its registry name. The names clients actually receive
are the field's `api_names`, which default to that one name. Where one field
reaches clients under several names (`invited_by` as `invited_by_id` and
`invited_by_name`), the registry lists them all, so every serializer, the `/me`
map and the catalogue agree on what a switch covers.

Modules and resources reuse the existing `PermissionModule` and
`PermissionResource` rows, so permissions and fields sit in the same tree.

---

## 5. Data model (`vs_rbac`)

### 5.1 Readable names on the existing tree

- `PermissionModule.label` `CharField(80, blank=True)`, e.g. "Procurement".
- `PermissionResource.label` `CharField(120, blank=True)`, e.g. "Vendors".
- Seeds fill both. The procurement seed already carries resource labels
  ("vendors", "purchase orders") that are not stored today. An empty label falls
  back to the slug title-cased, the same way `_permission_label` composes a
  permission label.

### 5.2 `FieldDefinition` (global registry, Vision-owned)

| Column | Type | Meaning |
|---|---|---|
| `key` | `CharField(200)` PK | `module.resource.name`, built on save |
| `resource` | FK `PermissionResource` | the resource it sits under (module comes through it) |
| `name` | `SlugField(64)` | registry name, the key's last segment |
| `api_names` | `JSONField` list | names clients receive; defaults to `[name]` |
| `label` | `CharField(120)` | "Bank account number" |
| `group` | `CharField(60, blank)` | "Banking", "Contact", "Medical": headings inside a resource |
| `description` | `TextField(blank)` | one sentence for the admin |
| `sensitive` | `BooleanField` | D1 default: OFF/OFF when true |
| `writable` | `BooleanField` | false for computed or provider-issued fields; no Write switch |
| `scope` | `PermissionScope` | `TENANT` or `PLATFORM`, same meaning and guard as `Permission.scope` |
| `sort_order` | `PositiveSmallIntegerField` | order inside the group |
| `is_active` | `BooleanField` | a field removed from code is deactivated, never deleted |

Unique `(resource, name)`. Deactivation bumps `PermissionRegistryRevision`, as a
permission deactivation does.

### 5.3 `RoleFieldAccess` (tenant-owned switch)

| Column | Type |
|---|---|
| `role` | FK `TenantRoleTemplate`, CASCADE |
| `field` | FK `FieldDefinition` (`to_field="key"`), CASCADE |
| `can_read` | `BooleanField` |
| `can_write` | `BooleanField` |
| `set_by` | FK User, SET_NULL |
| `set_at` | `DateTimeField` |

- Unique `(role, field)`. Index `(field, can_read)`.
- `CheckConstraint`: `NOT can_write OR can_read` (D10 in the database, not only in
  the serializer).
- No row means the field's default (D1). "Reset to default" deletes the row.
- Scope guard in `save()` via `ScopeGuardedManager`, like `TenantRolePermission`: a
  `PLATFORM` field refuses a row whose role belongs to a non-platform tenant.
- `can_write` on a non-`writable` field is refused.

### 5.4 `PrebuiltRoleFieldAccess` (Codex defaults, D4)

Same switch columns, FK `PrebuiltRoleTemplate`. Copied onto the tenant's roles when
a tenant's roles are created from prebuilt templates, alongside
`PrebuiltRolePermission`. Later edits to a prebuilt template do not rewrite an
existing tenant's rows, matching how prebuilt permissions behave.

### 5.5 `UserFieldAccessOverride` (one-person exception, D8)

| Column | Type |
|---|---|
| `tenant` | FK Tenant, PROTECT |
| `user` | FK User, CASCADE |
| `field` | FK `FieldDefinition` (`to_field="key"`), PROTECT |
| `access` | `READ` or `WRITE` |
| `mode` | `ALLOW` or `DENY` |
| `reason` | `TextField`, required |
| `created_by` | FK User, SET_NULL |
| `expires_at` | nullable; lazily ignored once past, no sweep |

- Unique `(user, field, access)`. A new row replaces the old (delete + create, both
  audited). Index `(tenant, user, expires_at)`.
- `ALLOW WRITE` also grants Read. `DENY READ` also removes Write.
- Unlike permission overrides, there is **no restricted field**, because D5 removes
  approval. Any active field the tenant may hold can be allowed.

---

## 6. The registry lives in code

Each app declares its fields in `field_access.py`, registered from `AppConfig.ready()`,
the same pattern as `export_datasets.py`:

```python
register_fields("procurement", "vendor", label="Vendors", fields=(
    FieldSpec("name", "Name", group="Vendor"),
    FieldSpec("phone", "Phone", group="Contact", sensitive=True),
    FieldSpec("bank_account_number", "Bank account number",
              group="Banking", sensitive=True),
    ...
))
```

- `manage.py sync_field_registry` writes `FieldDefinition` rows idempotently,
  updates labels, deactivates declarations that disappeared, and runs in the
  deploy seed sequence after the permission seeds.
- **What gets registered** (D2): every field guarded today, plus fields a module
  owner judges an admin may reasonably restrict. Never ids, tenant or branch
  links, audit columns or internal metadata.
- The export catalogue's `Field` gains `access="procurement.vendor.bank_account_number"`.
  Its `sensitive` flag is read from the registry instead of being declared twice.

Registry tests (run in CI):
1. Every name a serializer maps with the new mixin exists in the registry.
2. Every registered field is enforced by at least one serializer or declared raw
   surface, so a registration can never silently do nothing.
3. Every export `Field` whose column is a registered field carries `access=`.
4. Every serializer that can **write** a registered writable field is a declared
   surface, or sits on an allowlist with a one-line reason. Declaring rules per
   serializer is exactly how a second write path loses them: enrolment accepted a
   pupil's allergies with no medical permission while the edit form refused the
   same field. The check finds such paths by field name and model, not by whether
   they already use the mixin.

---

## 7. Evaluation (`vs_rbac/field_evaluator.py`)

```python
get_field_access(user, tenant, branch=ANY_BRANCH) -> FieldAccessMap
FieldAccessMap.can_read(field_key) -> bool
FieldAccessMap.can_write(field_key) -> bool
```

The module is `field_evaluator.py` rather than `field_access.py` because every
domain app already has a `field_access.py` holding its field declarations, and
one name must not mean two things.

Only registered, active fields are in the map. **An unregistered field is always
readable and writable**, which keeps the system opt-in exactly like today.

For field `f` and user `u`:

```
role_read  = any over u's active roles r:  row(r,f).can_read  if row else f.default_read
role_write = any over u's active roles r:  row(r,f).can_write if row else f.default_write
             (no roles at all: the defaults)

read  = (role_read or ALLOW READ or ALLOW WRITE)  and not DENY READ
write = (role_write or ALLOW WRITE) and f.writable and not DENY WRITE and not DENY READ
write = write and read
```

A personal DENY beats a role and a personal ALLOW, the same order as
`get_effective_permissions`.

Bypasses and fixed rules, all in one place:

| Case | Result |
|---|---|
| Vision super admin | everything readable and writable |
| Field `scope=PLATFORM`, tenant not platform | never readable or writable |
| Owner rule declared on a serializer (D12: own payroll) | read and write true for the owner |
| No request and no actor passed | "system" context: everything, as today. Only for jobs acting on nobody's behalf |

Queries and caching:
- The role-id lookup inside `_role_permission_keys` becomes a shared
  `_active_role_ids(user, tenant, branch)`, so permissions and field access use the
  same assignment rules (branch liveness, role status).
- Cost is at most three queries: role rows, override rows and the active field set.
  They run only when a field-enforcing surface or `/me` is served. The map is
  memoised on the user instance beside `_rbac_effective_perms`, keyed by
  `(tenant.pk, branch key)` and checked against `PermissionRegistryRevision`.
- This key carries the tenant, which closes finding §31 (the old `_fls_permissions`
  cache ignored the tenant).
- A switch change needs no cache bump: memoisation lasts one request.

---

## 8. Enforcement: one service, every surface

"Works accordingly" fails if one surface forgets, so the map is consulted through
four named choke points, and a test fails any surface that bypasses them.

### 8.1 Serializers: `FieldAccessMixin`

```python
class VendorSerializer(FieldAccessMixin, serializers.ModelSerializer):
    field_resource = "procurement.vendor"
    owner_rule = None               # callable(obj, user) -> bool, for D12
```

- **Read** (`to_representation`): drops every registered field the caller cannot
  read. No `_stripped_fields` (D7, closes §32). Detail responses add
  `_read_only_fields`: names **present in this payload** that the caller cannot
  write. Lists omit it.
- **Write** (`to_internal_value`): collects every submitted registered field the
  caller cannot write, hidden ones included, and raises `FieldWriteDenied` (403,
  code `field_write_denied`, per-field errors). Hidden and read-only get the same
  message, so a refusal reveals nothing beyond what the caller already sees.
- **Echoed values**: on update, a read-only field whose submitted value equals the
  stored value is dropped, not refused, so a form that sends the whole object back
  does not fail. That leniency belongs to a caller who may **read** the field. For a
  hidden field an equal value is refused like any other, because "equal is accepted,
  different is refused" answers the question hiding the field exists to keep
  unanswered: Mr. Bello could read Ade Stationers' bank account number a digit at a
  time from his browser console. On create, an empty or null value is dropped; anything else is refused,
  unless the field is declared open on create (D14), in which case any value is
  accepted at creation and the Write switch governs only later changes.
- **Nested serializers** inherit the request context, so `contacts` inside a vendor
  is filtered as its own resource or dropped as a whole field.
- **Method fields are opaque.** The mixin filters a `SerializerMethodField` by its
  own name and cannot see inside what it returns. A block that carries a
  registered field (the account block inside a staff record carries the sign-in
  address) is declared as a nested serializer carrying the mixin, or built by
  hand and passed through `visible()`. The deep payload tests render every
  declared surface as a caller with every field of its resource closed, walk the
  whole JSON, and fail on any registered name found at any depth; a surface no
  app's case renders fails the registry tests.

### 8.2 Hand-built responses and raw writes

For views that build dicts or read `request.data` directly:

```python
field_access.visible(request, "payments.payout", row)          # drops keys
field_access.assert_writable(request, "procurement.vendor", body)
```

Known sites to move onto these (each currently re-implements the check):

| Code | Today | After |
|---|---|---|
| `vs_procurement/views/vendors.py` `_require_sensitive_access` | 403 if any sensitive key sent without the key | `assert_writable` |
| `vs_procurement/serializers.py` RFQ `get_invitations` `can_view_contacts` | key check | `can_read` on contact fields |
| `vs_payments/views.py` `MovementsView` | masks with `"••••"` | payout rows drop `party` and `beneficiary_account` (D7) |

### 8.3 Exports

`vs_exports` hides and refuses columns the caller cannot read, using the registry
link from section 6. `exports.sensitive_field.export` **stays** as a second gate: it
controls taking restricted data out of the building in a file, not seeing it on screen.

### 8.4 Documents, notifications and background jobs

- Any job acting **for a person** (a scheduled export, an approval document built for
  an approver) passes that person as `actor=` and is filtered as them.
- Documents **addressed to an outside party** (an invoice PDF showing the school's own
  bank account to a payer) run in system context by design and are listed in
  `field_access.SYSTEM_SURFACES` with the reason. A test fails any unlisted
  system-context render of a registered field.
- `vs_workflow/presentation.py` builds approval summaries without serializers; any
  registered field it reads goes through `visible()`.

---

## 9. API contract

All tenant routes sit under `/rbac/tenants/<slug>/` and use
`TenantScopedRBACMixin`, so the slug must match the bound tenant. Roles, users and
fields from another tenant, or `PLATFORM` fields in a school, return a
non-enumerating 404 or 400.

### 9.1 New permission keys

Same dual pattern as `ROLE_*_KEYS`:

| Key | Scope | Meaning |
|---|---|---|
| `school.field_access.view` | TENANT | open Field Access, read switches |
| `school.field_access.manage` | TENANT | change switches |
| `platform.field_access.view` | PLATFORM | same, platform tenant |
| `platform.field_access.manage` | PLATFORM | same |

`sensitivity=CRITICAL`. The manage keys are restricted and the view keys are not
(D15): changing a switch needs no approval (D5), but gaining the right to change
switches does. Core in `permission_bands` (never
sold). Seeded to School Admin, XVS Super Admin and XVS Platform Admin. One-person
field exceptions reuse the existing **override** keys (D8).

### 9.2 `GET access-catalogue/`: the tree for both screens

Replaces `permission-catalogue/`, which stays until both frontends move (section 13).
Guard: `ROLE_VIEW_KEYS`. Query: `module`, `resource`, `search` (all optional).
Same tenant-scope and plan filtering as today's catalogue. A platform actor may
assert a school tenant here; the returned tree is scoped to that school and
still requires the actor's platform role-view key.

```json
[
  {
    "module": "procurement", "label": "Procurement", "available": true,
    "resources": [
      {
        "resource": "vendor", "label": "Vendors", "available": true,
        "permissions": [
          {"key": "procurement.vendor.view", "label": "View vendors",
           "action": "view", "sensitivity": "NORMAL", "is_restricted": false,
           "available": true, "band": null, "depth_label": null,
           "unavailable_reason": null}
        ],
        "fields": [
          {"key": "procurement.vendor.bank_account_number",
           "name": "bank_account_number", "label": "Bank account number",
           "group": "Banking", "description": "Supplier banking data.",
           "sensitive": true, "writable": true,
           "default": {"read": false, "write": false}}
        ]
      }
    ]
  }
]
```

Permissions inside a resource are ordered view first, then the remaining actions.

A platform operator holding `platform.roles.view` may read a school's catalogue by
asserting that school (its slug in the path and as `?tenant=`), as the console does
when choosing a field for a school user's exception. Entries follow the asserted
tenant, so a school's catalogue never carries a PLATFORM permission or field,
whoever reads it.

### 9.3 `GET roles/<key>/field-access/`

Guard: `*.field_access.view` or `.manage`. Query: `module`, `resource`, `search`,
`state=hidden|read_only|full`. Not paginated: it is a filtered vocabulary.

```json
{
  "role": {"key": "storekeeper", "name": "Storekeeper", "branch_name": null},
  "fields": [
    {"key": "procurement.vendor.phone", "name": "phone", "label": "Phone",
     "module": "procurement", "resource": "vendor", "group": "Contact",
     "sensitive": true, "writable": true,
     "read": true, "write": false, "source": "role",
     "default": {"read": false, "write": false},
     "set_by_name": "Mrs. Eze", "set_at": "2026-09-14T10:02:00Z"}
  ]
}
```

`source` is `role` when a row exists, `default` otherwise.

### 9.4 `PATCH roles/<key>/field-access/`

Guard: `*.field_access.manage`. Atomic; up to 200 changes per call.

```json
{"changes": [
  {"field": "procurement.vendor.phone", "read": true, "write": false},
  {"field": "procurement.vendor.bank_account_number", "reset": true}
]}
```

- `write: true` stores Read true. `read: false` stores Write false. The response
  shows what was stored, so the UI never guesses.
- `write: true` on a non-writable field returns 400 naming it.
- A change equal to the stored state writes nothing and audits nothing.
- Rows lock with `select_for_update`, so two admins on the same field serialise.
- An admin may change a role they hold (D6). Nothing here is self-gated.
- Response: the 9.3 shape for the changed fields only.

### 9.5 One-person exceptions

`GET|POST users/<user_id>/field-access-overrides/`, `DELETE .../<id>/`

Guards and behaviour copy `UserPermissionOverride*View`: override keys, no self
(actor or proxied identity), replace rather than stack, `platform_cross_tenant_param`,
non-enumerating user lookup.

The frontend additionally requires the actor's tenant-specific role-view key so
it can populate the field picker from `access-catalogue/` (D16). This is a UI
companion requirement, not an extra guard on the exception endpoints.

```json
{"field": "procurement.vendor.bank_account_number", "access": "READ",
 "mode": "ALLOW", "reason": "Covering bursar duties 14-28 Sept",
 "expires_at": "2026-09-28T23:59:00Z"}
```

Read rows add `field_label`, `is_expired`, `created_by_name` and
`role_state: {"read": false, "write": false}`, so the UI can say "the role hides this,
allowed for this person".

### 9.6 `GET /rbac/vision/fields/`

Platform registry view, guard `platform.permissions.view`. Read-only, because the
registry is owned by code (D2).

### 9.7 What the logged-in user receives

`/user/auth/me/` and the login payload gain:

```json
"field_access": {
  "procurement.vendor": {"hidden": ["bank_account_number"], "read_only": ["phone"],
                         "open_on_create": []},
  "school.students":    {"hidden": [], "read_only": ["blood_group", "enrolment_date"],
                         "open_on_create": ["enrolment_date"]},
  "school.teachers":    {"hidden": ["email"], "read_only": [],
                         "open_on_create": ["email"]}
}
```

- Only resources where something is not full. An absent resource or name means full.
- Each resource carries all three lists, each sorted. `hidden` and `read_only`
  describe an existing record and never share a name.
- `open_on_create` lists every field declared open on create (D14) that is not
  fully open to the user, **hidden and read-only alike**. An Add form offers
  these as ordinary inputs. Every name in it also appears in `hidden` or
  `read_only`, which is what it is on every record that already exists: the
  staff Add form asks a registrar with email Read off for the address, and the
  staff record never shows it to her.
- Used for **create forms and columns**, where no record exists yet.
- On an existing record the response's `_read_only_fields` wins, because it carries
  owner rules and, under option B, the record's branch.
- This names field kinds the user cannot see, never a value. That is accepted: the
  same names are in the catalogue and in every module FRD.

---

## 10. Audit (D6)

Every stored change writes `record_rbac_audit` inside the same transaction, so an
audit failure rolls the change back.

| `action_type` | When | Severity |
|---|---|---|
| `FIELD_ACCESS_CHANGED` | one per field per switch flipped | `WARNING` if the result opens a sensitive field, else `INFO` |
| `FIELD_ACCESS_RESET` | row deleted back to default | `INFO` |
| `FIELD_OVERRIDE_CREATED` / `FIELD_OVERRIDE_LIFTED` | exceptions, mirroring permission overrides | `WARNING` |
| `FIELD_ACCESS_CONVERTED` | one per role or override during conversion (section 11) | `INFO` |

`entity_type="RoleFieldAccess"`, `entity_label="<role key>:<field key>"`,
`before_data={"read":..,"write":..}`, `diff_data` holds the new state, and `metadata`
carries `tenant_id`, `school_id` (slug), `role_key`, `field_key`, `sensitive` and
`actor_holds_read`. That last value records D6 escalations explicitly, so "who opened
a field they could not see" is one filter.

---

## 11. Converting the old keys (D9)

**Principle:** after conversion, for every user in every tenant, the set of fields
they can read and write equals what the old keys gave them. The one exception is
marked below.

| Old key | Becomes switches on | Key afterwards |
|---|---|---|
| `procurement.vendor.view_sensitive` | `procurement.vendor`: email, phone, address, tax_id, bank_name, bank_code, bank_account_number, bank_account_name, contacts. Read + Write | **removed** |
| `finance.bankaccount.view_sensitive` | `finance.bankaccount.account_number`. Read + Write * | **removed** |
| `finance.payrollrun.view_sensitive` | `finance.payrollrun` (payroll lines): employee_name, gross_amount, paye_amount, pension_amount, net_amount, components. `finance.salary` (employee salaries): gross_amount, paye_amount, pension_amount, net_amount, components. Read (Write where writable) | **removed** |
| `payments.virtual_account.view_sensitive` | `payments.virtual_account`: account_number, account_name. Read (provider-issued, not writable) | **removed** |
| `payments.payout.view_sensitive` | `payments.payout`: beneficiary_name, beneficiary_account_number, beneficiary_bank_code; also the movements feed | **removed** |
| `school.students.view_sensitive` | `school.students`: blood_group, allergies, conditions. Read + Write | **removed** |
| `platform.staff_payroll.view` / `.manage` | `platform.staff_profile`: bank_name, account_name, account_number. view → Read, manage → Write. Owner rule kept (D12). `PLATFORM` scope | **removed** |
| `platform.team.view` (reused for fields) | `platform.team`: password_changed_at, last_login_at, invited_by (api names `invited_by_id`, `invited_by_name`), invitation_email_status, invitation_expires_at. Read. Scope follows `platform.team.view` | kept: guards pages |
| `school.students.manage` (reused for `enrolment_date` write) | `school.students.enrolment_date`. Write | kept |
| Import keys (template manage, job view, batch view) | `import.templates.validation_rules`, `import.jobs` payload and error fields, `import.batches` file and preview_rows. Read | kept: guard pages |
| `platform.tasks.view_sensitive` | not a field: raw tracebacks, audited per read | kept as a permission |
| `exports.sensitive_field.export` | not a field: see 8.3 | kept |

\* **Deliberate tightening, ten fields (D18).** Several keys gate only reading, while
the value is written on a path that asks for the endpoint's own key, so a role can
change a number it cannot see. Write implies Read (D10, and a database check), so
that stops being expressible. The ten: four payroll-line figures, three employee
salary figures, a finance bank account number, an import batch file, and the three
platform staff bank fields, where a role can hold `staff_payroll.manage` without
`.view`. Bright Star's payroll clerk types a gross salary today against a blank
column and will not afterwards. Every one is listed with its reason in
`vs_rbac.field_conversion.ACCEPTED_DIFFERENCES`, and the verification command
prints what it forgave rather than skipping it. Every other row preserves access
exactly. Each mapping is re-verified
against the serializer and view code while building, and resource slugs are
confirmed against the permission seeds.

Every place the old keys reach is converted:
1. **Tenant roles.** A role's effective key set (direct grants minus role denies,
   plus permission groups) decides the switches written.
2. **Prebuilt roles.** `PrebuiltRolePermission` becomes `PrebuiltRoleFieldAccess`.
3. **Permission groups.** The keys are removed from any legacy group membership
   after roles holding those groups have been converted.
4. **Permission overrides.** ALLOW/DENY on an old key becomes field overrides with
   the same mode, expiry and creator. The reason becomes "Converted from permission
   exception: <original reason>".
5. **Seeds, `permission_bands`, `seed_actions` description, frontend permission maps.**
   The keys leave the registry and every seed.

Safety:
- `manage.py verify_field_access_conversion --snapshot` records, per user, the fields
  readable and writable under the old keys. `--compare` recomputes through the new
  evaluator and fails listing every difference. Required on a production-copy
  database before release; the tightening above is its only allowed difference.
- The data migration is reversible: reverse re-grants the keys from the stored
  switches, then deletes the switch rows.

---

## 12. Tenants of every kind

- **School tenants** see `TENANT` fields and edit their own roles.
- **The platform tenant** (Codex staff) sees both scopes and edits its own roles,
  staff payroll bank fields included.
- **Health and later domains** get Field Access by declaring fields. The engine names
  no domain. School resources (`school.students`) are declared inside
  `apps/schools/`, never in `vs_rbac`.
- **One branch or many**: switches belong to roles, and a branch-pinned role already
  carries its branch, so nothing changes shape with the branch count. See section 3.

---

## 13. Build order

| Slice | Contents | Behaviour change | Frontend can start |
|---|---|---|---|
| **S1 Tree and registry** | labels on module/resource, `FieldDefinition`, `field_access.py` declarations for every field in section 11, `sync_field_registry`, `access-catalogue/`, `vision/fields/`, registry tests | none | permission allocation by Module → Resource |
| **S2 Switches and exceptions** | `RoleFieldAccess`, `PrebuiltRoleFieldAccess`, `UserFieldAccessOverride`, evaluator, audit, 9.3-9.5 endpoints, new permission keys and seeds | none: switches are stored, not yet enforced | Field Access screen and exceptions |
| **S3 Enforcement and conversion** | `FieldAccessMixin`, raw-surface helpers, the three sites in 8.2, exports, documents, `/me` and login `field_access`, `_read_only_fields`, removal of `_stripped_fields`, conversion migration, verify command, removal of old keys everywhere, deletion of `fls.py` | yes | greying and hiding everywhere, released together |
| **S4 Cleanup** | remove `permission-catalogue/` once both frontends use `access-catalogue/` | endpoint removed | - |

**S3 must ship as one release, frontends included.** Enforcement without conversion
leaves bursars without bank numbers, because sensitive defaults are OFF. Conversion
without enforcement leaves the old keys gone while nothing reads the switches.

Per the playbook: Opus-high agents write the slices sequentially (S2 and S3 share
migrations and `vs_rbac` models), and the conductor runs each app's suite.

---

## 14. Regressions to expect

1. **Response shapes.** `_stripped_fields` disappears. `/me` and login gain
   `field_access`. Detail responses gain `_read_only_fields`. The movements feed drops
   keys instead of masking them. The school frontend's `src/utils/fls.ts` and the
   console's organogram drawer read `_stripped_fields` today.
2. **Status codes.** A refused field write moves from 400 (`ValidationError`) to 403
   `field_write_denied`. Tests asserting 400 change.
3. **Permission keys removed** (section 11). Both frontends map them in
   `src/permissions/index.ts`, and the staff screens print their names in copy.
4. **Seeds.** Prebuilt roles, groups, `permission_bands` and module seeds all change.
   A fresh database must come up converted without the migration's help.
5. **The one access tightening** in section 11.
6. **Performance.** Up to three queries on field-enforcing requests. Query-budget
   tests on those endpoints (academics and finance carry named budgets) gain a named
   `FIELD_ACCESS_READ` allowance.
7. **Shared helpers.** `_role_permission_keys` is refactored to share
   `_active_role_ids`, so the permission suite must stay green unchanged.
8. **Docs.** After the build: `docs/rbac/rbac_evaluation_scoping.md` (field-level
   masking, §31, §32), `rbac_change_requests_overrides.md`, `rbac_permission_registry.md`,
   the MRD, the M04 RBAC FRD, and the FRDs of every module whose fields convert (M03,
   M11 students, M12 staff, M18 payments, M19 finance, procurement).

---

## 15. Tests

Security first:
1. School A admin cannot read or change School B's role switches or exceptions,
   whether by slug, role key or user id (404, no enumeration).
2. A caller without `field_access.view`/`.manage` gets 403 on each endpoint and verb.
3. A school cannot see or set a `PLATFORM` field, and the model guard refuses a row
   written directly.
4. A read-only field sent through the API gets 403 and nothing is saved. A hidden
   field gets the same 403 and message.
5. A hidden field is absent from detail, list, nested, export catalogue, export file,
   movements feed and approval document for the same user.
6. An exception on yourself is refused, including through impersonation.
7. Every flip writes one audit row. An audit failure rolls back the flip.

Behaviour:
8. Defaults: a normal field with no row is open; a sensitive field with no row is closed.
9. Most generous wins across two roles. Tested in a single-branch school and in a
   multi-branch school with branch-pinned roles (section 3 option as decided).
10. Personal DENY beats a role grant and a personal ALLOW. An expired override stops
    applying with no sweep.
11. Write implies read, both in the API response and in the database constraint.
12. Owner rule: a staff member reads and writes their own bank fields without roles.
13. Vision super admin sees every field. No request context passes everything, and an
    unlisted system-context render of a registered field fails the surface test.
14. Echoed unchanged read-only value on update is accepted; a changed one is refused.
15. `/me` map matches the evaluator. Empty tenants return `field_access: {}`.
16. Conversion: the snapshot/compare command passes on fixtures covering direct
    grants, group grants, role denies, overrides of both modes, prebuilt roles, and
    the tightening case.
17. Registry tests from section 6.
18. The permission suite passes unchanged after the `_active_role_ids` refactor.
