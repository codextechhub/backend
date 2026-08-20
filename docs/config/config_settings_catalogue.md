# config_settings_catalogue

The generic half of `vs_config`: the typed settings catalogue
(`ConfigurationDefinition`), the scoped values written against it
(`ConfigurationValue`), the three-layer precedence chain that turns them into
one effective answer, and the read API (`vs_config.conf.get_config`) that the
rest of the platform uses instead of touching the tables.

Routes are mounted at `/v1/config/` (`apps/urls.py:28`):
`definitions/`, `definitions/<key>/`, `values/`, `values/<key>/`,
`effective-values/`, `effective-values/<key>/`, `export/`.

The curated screens built on top of this engine (Platform, Security,
Integrations) are a separate slice: `config_platform_runtime_settings`.

---

## 1. What it is (and what it is NOT)

- **A definition is a schema, not a value.** `ConfigurationDefinition` declares
  a key, a type, validation rules, the scopes at which it may be written, and a
  fallback default (`models.py:38-120`). It holds no live value beyond that
  default.
- **A value row is one setting at exactly one scope.** At most one
  `ConfigurationValue` may exist per `(definition, scope_key)` - the
  `uniq_config_value_scope` constraint (`models.py:256-260`) - so every write is
  an upsert and there is no such thing as "two values fighting at the same
  level".
- **Scope is three levels, not two.** `platform`, `tenant:<id>`, `branch:<id>`,
  normalised into one `scope_key` string by `ScopedModel.set_scope_key`
  (`models.py:187-193`). A platform row is a *scope marker*, not tenant
  ownership: tenant NULL and branch NULL means "applies everywhere".
- **Reads inherit; lists do not.** `resolve_value` walks branch, then tenant,
  then platform, then the definition default (`services/resolution.py:75-95`).
  `GET /values/` deliberately shows only rows physically stored at the resolved
  scope (`views.py:254-264`), so an operator can see what *this* level sets
  rather than what it inherits.
- **The definition, not the caller, decides where a value may live.**
  `set_value` refuses any scope not in `definition.allowed_scopes`
  (`services/resolution.py:104-107`). This is the module's real write boundary:
  a school admin with `config.value.update` still cannot write a
  platform-only key.
- **Definitions are never deleted.** `DELETE /definitions/<key>/` sets
  `is_active = False` (`views.py:227-243`), because values and immutable audit
  rows point at the row.
- **Keys are immutable.** `validate_key` rejects any change on update
  (`serializers.py:53-55`), since application code and stored values reference
  the string.
- **This is not a secrets store.** `SECRET_REFERENCE` redacts a value from
  every response, but nothing resolves the reference and nothing checks its
  format - see `config_code_issues.md` §6.
- **Values are not tenant-manager scoped.** `ConfigurationValue.objects` is a
  `TenantAwareManager(include_global=True)` (`models.py:250`), and **every read
  path in this app deliberately uses `all_objects` with an explicit scope
  filter instead**. That is the correct choice here (the precedence chain has
  to see platform rows from inside a tenant request), and it is the opposite of
  the mistake `vs_notifications` made.

## 2. Domain model

### `ConfigurationDefinition` (`models.py:38`)

| Field | Meaning |
|---|---|
| `key` | Immutable dotted machine key, unique, indexed (`models.py:98`) |
| `label`, `description` | Admin-facing text |
| `value_type` | STRING, INTEGER, DECIMAL, BOOLEAN, JSON, CHOICE, SECRET_REFERENCE (`models.py:83-90`) |
| `default_value` | JSON fallback returned when no row exists at any scope |
| `validation_rules` | `choices` for CHOICE, `min`/`max` for numerics (`services/resolution.py:47-70`) |
| `allowed_scopes` | List of `platform` / `school` / `branch`; the write boundary |
| `sensitivity` | PUBLIC, INTERNAL, SECRET_REFERENCE. Only the third one changes behaviour |
| `is_active` | Soft-archive flag, indexed. Archived definitions never resolve |
| `created_by`, `created_at`, `updated_at` | Provenance |

`Meta.ordering = ["key"]` (`models.py:117`), and `key` is unique, so the
definitions list paginates deterministically.

### `ScopedModel` (`models.py:123`)

The abstract scope contract shared by `ConfigurationValue`,
`CapabilityOverride`, `ConfigurationAuditEvent`, `ConfigurationAuditSavedView`
and `ConfigurationAuditExportJob`.

```text
tenant NULL, branch NULL  ->  scope_key = "platform"
tenant set,  branch NULL  ->  scope_key = "tenant:<id>"
branch set                ->  scope_key = "branch:<id>"   (tenant auto-filled)
```

`scope_key` exists because SQL treats `NULL != NULL`: a plain unique constraint
on `(definition, tenant, branch)` would happily allow two platform rows
(`models.py:138-145`). `save()` always runs `clean()` then `set_scope_key()`
(`models.py:195-198`), so a row can never be persisted with a `scope_key` that
disagrees with its foreign keys.

`clean()` reads `self.branch.tenant_id` directly (`models.py:179-185`). There is
no detour through a school: the site primitive is `vs_tenants.Branch`, owned by
`Tenant`.

### `ConfigurationValue` (`models.py:201`)

| Field | Meaning |
|---|---|
| `definition` | FK, CASCADE. Values die with their definition |
| `value` | JSON payload, shape guaranteed by `validate_value` at write time |
| `tenant`, `branch`, `scope_key` | Scope placement from `ScopedModel` |
| `updated_by` | Last writer, SET_NULL |

Constraint `uniq_config_value_scope` on `(definition, scope_key)`; index on
`(tenant, branch)` (`models.py:256-261`).

## 3. Endpoint map

Every route in this app inherits `ConfigAPIView` (`views.py:99`):

- `permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]`
- `rbac_permission` is resolved per HTTP method from `permission_map`; an
  unmapped method raises `MethodNotAllowed` (`views.py:113-122`), which is why
  `PUT /values/` is a 405 rather than a permission error.
- `tenant_param_required = False` - `?tenant=` is optional, and the scope falls
  back to the caller's home tenant (`views.py:106`).
- `platform_cross_tenant_param = True` - a Codex staffer may assert a school
  tenant on **any** config route (`views.py:110`, honoured at
  `vs_rbac/authentication.py:113-121`).
- `platform_methods` names the methods that additionally require the caller's
  **home** tenant to be a PLATFORM tenant (`views.py:137-144`). Home tenant, not
  asserted tenant, so a CX staffer keeps platform rights while working inside a
  school - and an impersonated CX staffer does not, because `request.user` is
  the effective school user by then.

| Method + path | Permission | Platform-only | Notes |
|---|---|---|---|
| `GET /definitions/` | `config.definition.view` | no | `?include_inactive=true` shows archived (`views.py:156-161`) |
| `POST /definitions/` | `config.definition.create` | yes | `created_by = request.user` |
| `GET /definitions/<key>/` | `config.definition.view` | no | Archived rows are retrievable by key |
| `PATCH /definitions/<key>/` | `config.definition.update` | yes | Curated keys reject schema edits (`views.py:204-214`) |
| `DELETE /definitions/<key>/` | `config.definition.archive` | yes | Soft archive, idempotent; curated keys refused (`views.py:230-233`) |
| `GET /values/` | `config.value.view` | no | Rows at the resolved scope only, ordered by key |
| `POST /values/` | `config.value.update` | no | Single or bulk, one transaction |
| `DELETE /values/<key>/` | `config.value.update` | no | Clears one layer, returns the new effective value |
| `GET /effective-values/` | `config.value.view` | no | Whole catalogue, resolved and redacted |
| `GET /effective-values/<key>/` | `config.value.view` | no | One key; unknown key is 404 |
| `GET /export/` | `config.export.create` | no | Values plus capabilities in one snapshot |

### Request bodies actually read

`POST /values/` accepts two shapes and normalises them (`views.py:269-272`):

```jsonc
// single
{"key": "ui.theme", "value": "dark", "reason": "Brand refresh"}

// bulk
{"values": [{"key": "ui.theme", "value": "dark"}, {"key": "ui.density", "value": "compact"}],
 "branch": 12}
```

The only fields read per item are `key`, `value` and `reason`
(`serializers.py:109-116`). **Scope is never taken from the item.** It is
resolved once per request from `request.tenant` plus `?branch=` or a top-level
`branch` in the body (`services/scopes.py:38-74`), which is what stops a caller
writing another tenant's row by decorating the payload.

`DELETE /values/<key>/` reads only `reason` (`serializers.py:204-205`), from a
DELETE body.

### Serializer field sets

| Serializer | Fields |
|---|---|
| `ConfigurationDefinitionSerializer` (`serializers.py:29`) | `id`, `key`, `label`, `description`, `value_type`, `default_value`, `validation_rules`, `allowed_scopes`, `sensitivity`, `is_active`, `consumer`, `created_by`, `created_at`, `updated_at` |
| `ConfigurationValueSerializer` (`serializers.py:90`) | `id`, `definition`, `key`, `tenant`, `branch`, `value`, `updated_by`, `created_at`, `updated_at` - all read-only |

`consumer` is a synthetic field: it looks the key up in the code-owned
`SETTING_CONSUMERS` map (`runtime_settings.py:63-169`) so an administrator can
see which service reads a setting and what breaks if it changes. Administrators
cannot author that claim; it is not a database column.

## 4. Lifecycle / state machine

A definition has exactly two states and one transition that matters:

```text
POST /definitions/        ->  is_active = True
DELETE /definitions/<key>/ ->  is_active = False    (archived; values remain)
PATCH  {"is_active": true} ->  back to active       (see issues file §9)
```

An archived definition disappears from the default listing, is skipped by
`/effective-values/` and `/export/` (both filter `is_active=True`), and makes
`get_config` return the caller's own default (`conf.py:11-16`). Its stored
values are **not** deleted and reappear if the definition is un-archived.

A value has no state at all. It exists at a scope or it does not:

```text
POST /values/            ->  upsert at the resolved scope
DELETE /values/<key>/    ->  row deleted; the next layer up becomes effective
```

## 5. Derivations

- **Precedence** (`services/resolution.py:75-95`). The candidate scope keys are
  built most-specific-first, fetched in **one** query with
  `scope_key__in=[...]`, then walked in order:

  ```text
  branch:<id>  ->  tenant:<id>  ->  platform  ->  definition.default_value
  ```

  The first physical row wins and is returned alongside the value, which is
  what lets every response report a `source`.

- **`normalize_scope` is the single place branch and tenant are reconciled**
  (`services/scopes.py:23-34`). A branch supplied without a tenant fills the
  tenant in from `branch.tenant`; a branch supplied with a *different* tenant
  raises `InvalidConfigurationScope` (422). It compares `branch.tenant_id` with
  `tenant.pk`, so the common case costs no extra query.

- **`resolve_request_scope` derives the scope from the request, never the body**
  (`services/scopes.py:38-74`):

  ```text
  request.tenant is PLATFORM   ->  scope tenant = None      (platform layer)
  request.tenant is a school   ->  scope tenant = that tenant
  ?branch= or body "branch"    ->  branch, and scope tenant = branch.tenant
  ```

  The branch lookup goes through `find_branch_in_tenant`
  (`vs_tenants/references.py:33-55`), which collapses "not a number",
  "does not exist" and "belongs to someone else" into the same `None`, so a
  caller cannot use `?branch=` as an id oracle. The view turns that into a
  `404` (`services/scopes.py:69-70`). Tested for both the foreign case and the
  malformed case (`tests.py:311-341`).

- **`scope_name` maps a resolved scope onto the definition's vocabulary**
  (`services/scopes.py:10-19`): branch -> `"branch"`, tenant -> `"school"`,
  neither -> `"platform"`. The middle label is deliberately still `school`
  even though the stored `scope_key` reads `tenant:<id>`, so definition payloads
  did not have to change across the tenant cutover (`constants.py:33-41`).

- **Type validation** (`services/resolution.py:20-45`):

  | Type | Accepted |
  |---|---|
  | STRING, SECRET_REFERENCE | non-blank `str` (an empty string counts as unset) |
  | INTEGER | `int` and **not** `bool` (bool is an int subclass in Python) |
  | DECIMAL | anything `Decimal(str(value))` accepts |
  | BOOLEAN | `bool` |
  | CHOICE | membership in `validation_rules["choices"]` |
  | JSON | `dict` or `list` |

  Bounds run afterwards. DECIMAL bounds are compared as `Decimal`, and a bound
  that cannot be compared with the value raises a 422 rather than a 500
  (`services/resolution.py:47-70`, tested at `tests.py:111-127`).

- **Redaction is applied at three separate points**, all keyed on
  `sensitivity == SECRET_REFERENCE`: the audit snapshot before it is stored
  (`services/resolution.py:13-16`), the value serializer
  (`serializers.py:102-106`), and every effective read
  (`views.py:338-339`, `581-583`, `1214-1216`). The definition serializer
  redacts `default_value` too (`serializers.py:83-87`).

- **`sensitivity` is forced, not trusted.** A SECRET_REFERENCE value type
  overwrites `sensitivity` to match; claiming secret sensitivity on any other
  type is a 400 (`serializers.py:65-70`).

- **`get_config` is the public read API** (`conf.py:10-16`). It resolves through
  the same path as the HTTP endpoints, so an internal caller and the frontend
  can never see different values. An unknown or archived key returns the
  caller's own `default`, never `None` by surprise.

## 6. What writing writes

Every mutation in this slice writes an immutable
`ConfigurationAuditEvent` in the **same transaction** as the change, through
`record_configuration_event` (`services/audit.py:19-56`). The local row is
authoritative; a mirror is also pushed to `vs_audit` best-effort.

| Action | Written by | Target |
|---|---|---|
| `config.definition.created` | `views.py:168-171` | the definition |
| `config.definition.updated` | `views.py:220-223` | the definition |
| `config.definition.archived` | `views.py:238-242` | the definition |
| `config.value.updated` | `services/resolution.py:123-132` | the **value row** |
| `config.value.cleared` | `services/resolution.py:155-164` | the **definition** |

That last asymmetry is real and it splits one setting's history across two
target ids: see `config_code_issues.md` §15.

Bulk writes share one transaction (`views.py:267`), so a single bad key in a
batch of ten rolls the whole batch back, audit rows included. Tested at
`tests.py:397-419`.

Reading writes nothing. `/effective-values/`, `/export/` and both definition
reads leave no trace anywhere.

## 7. Worked example

```text
POST /v1/config/values/?tenant=alpha-nt
{"key": "ui.theme", "value": "dark", "reason": "Brand refresh"}
```

`resolve_request_scope` returns `(alpha-nt, None)`; `scope_name` calls that
`"school"`; the definition allows `["school"]`; `validate_value` accepts a
non-blank string; the row is upserted at `scope_key = "tenant:<alpha-id>"`.

```json
{ "success": true, "message": "Configuration value saved.",
  "data": { "id": "…", "definition": "…", "key": "ui.theme",
            "tenant": "…", "branch": null, "value": "dark",
            "updated_by": {"id": "7", "email": "admin@alpha.ng",
                           "full_name": "Ada Nwosu"},
            "created_at": "2026-08-20T09:14:02Z",
            "updated_at": "2026-08-20T09:14:02Z" } }
```

Then, from a branch under the same tenant:

```text
GET /v1/config/effective-values/ui.theme/?tenant=alpha-nt&branch=12
```

```json
{ "success": true, "message": "Effective configuration retrieved.",
  "data": { "key": "ui.theme", "value": "dark", "source": "tenant:<alpha-id>" } }
```

`source` is the honest answer to "where did this come from": there is no
`branch:12` row, so the tenant row won. Clearing it:

```text
DELETE /v1/config/values/ui.theme/?tenant=alpha-nt
{"reason": "Back to platform default"}
```

```json
{ "success": true, "message": "Configuration value reset.",
  "data": { "key": "ui.theme", "cleared": true,
            "effective_value": "light", "source": "platform" } }
```

Calling the same DELETE twice returns `"cleared": false` and the message
`"No scoped override was present."` (`views.py:340-341`, tested at
`tests.py:564-572`).

## 8. Gotchas / known limitations

Full evidence for each is in **`error/config/config_code_issues.md`**. The items
belonging to this slice:

- **No school role is ever granted a `config.*` permission**, so every route
  here is unreachable for a school user unless someone hand-builds a role
  (§3).
- **`SECRET_REFERENCE` is a label with no resolver and no format check.**
  Nothing turns `env://X` into a secret, and a secret pasted into the field is
  stored in cleartext in `ConfigurationValue.value` and handed to any caller of
  `get_config` (§6).
- **`/effective-values/` and `/export/` run one query per definition** and are
  unpaginated, so both get linearly slower as the catalogue grows (§10).
- **`config.definition.update` can archive and un-archive** any non-curated
  definition, because `is_active` is a writable serializer field
  (`serializers.py:34-39`). The separate `config.definition.archive` key is
  decorative (§9).
- **`config.value.cleared` targets the definition, not the value row**, so
  filtering the audit trail by target id shows only half of a setting's
  history (§15).
- **A CHOICE definition with no `choices` rule can never hold a value**, and
  nothing at create time says so (§17).
- **`sensitivity` PUBLIC and INTERNAL do nothing.** The model docstring says
  they are "shown as stored", which is accurate but means two of three
  enumeration members carry no behaviour at all (§19).
- **`from uuid import UUID` in `views.py:6` is unused**, as are three scope
  constants imported into `services/resolution.py:5` (§19).
- **Justified by design:** `GET /values/` lists only physically stored rows and
  ignores inheritance (`views.py:254-264`). Operators need to see what this
  layer sets; `/effective-values/` answers the other question.
- **Justified by design:** every read path uses `all_objects` with an explicit
  scope filter rather than the tenant-aware manager
  (`views.py:256`, `services/resolution.py:86`). The precedence chain must be
  able to see platform rows from inside a tenant request, which an ambient
  tenant filter would hide.
- **Justified by design:** a foreign, missing and malformed `?branch=` are all
  the same 404 (`services/scopes.py:68-70`).

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Restricted | Seeded to |
|---|---|---|---|---|
| Definition read | `config.definition.view` | NORMAL | no | `xvs_super_admin`, `xvs_platform_admin` |
| Definition create/update/archive | `config.definition.create` / `.update` / `.archive` | SENSITIVE | yes | same |
| Value read | `config.value.view` | NORMAL | no | same |
| Value write/reset | `config.value.update` | SENSITIVE | yes | same |
| Snapshot export | `config.export.create` | SENSITIVE | yes | same |

Seeded by `management/commands/seed_config_permissions.py:5-15`, granted to the
two platform roles only (`:16`, `:59-75`). `is_restricted` is derived from
sensitivity (`:53`).

**Isolation holds, and it is enforced in one place.** Every view derives its
scope from `resolve_request_scope`, which reads `request.tenant` (set by
`TenantJWTAuthentication` from the validated `?tenant=` assertion) and never
from the request body. There is no `?school=` parameter anywhere in the module.
Three consequences:

1. A school caller cannot address the platform layer: `scope_tenant` is only
   `None` when the asserted tenant is itself a PLATFORM tenant
   (`services/scopes.py:51-55`).
2. A school caller cannot address another tenant's branch: the branch lookup is
   filtered by tenant before the row is fetched.
3. A platform caller **can** address any tenant, because
   `platform_cross_tenant_param = True` is set on the base view class and
   therefore on every route in the app. That is intentional for entitlements
   and support, but it is worth knowing that it applies to the settings routes
   as well.

Definitions themselves are global and unscoped by design: they are the schema.
Only platform staff may create or change them (`platform_methods`).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:38-120` | `ConfigurationDefinition` - the schema half |
| `models.py:123-198` | `ScopedModel` - the shared three-level scope contract |
| `models.py:201-261` | `ConfigurationValue` - the data half |
| `services/resolution.py:20-71` | `validate_value` - type and bounds contract |
| `services/resolution.py:75-95` | `resolve_value` - the precedence chain |
| `services/resolution.py:100-165` | `set_value` / `clear_value` - audited upsert and delete |
| `services/scopes.py:10-34` | `scope_name`, `normalize_scope` |
| `services/scopes.py:38-74` | `resolve_request_scope` - the request-to-scope boundary |
| `views.py:99-144` | `ConfigAPIView` - RBAC binding, pagination, platform guard |
| `views.py:148-348` | Definition and value endpoints |
| `views.py:567-590` | `EffectiveValueView` |
| `views.py:1204-1222` | `ConfigExportView` |
| `serializers.py:29-116` | Definition and value serializers, write serializer |
| `conf.py` | `get_config`, `is_capability_enabled` - the public read API |
| `constants.py` | RBAC keys and the three scope labels |
| `exceptions.py` | Typed 422s, mapped by `core/exceptions.py:128-134` |
| `management/commands/seed_config_catalogue.py` | 21 seeded definitions, 13 capabilities |

## 11. Test coverage & gaps

Baseline: **`Ran 61 tests in 94.867s` - OK**
(`cd apps && DB_NAME=cx_configslice ../cx/Scripts/python.exe manage.py test
vs_config --settings=apps.settings.local --noinput`). The single traceback in
the output is `test_oversized_export_fails_with_the_size_limit_in_its_own_words`
logging its own expected failure.

What this slice covers:

- `ConfigurationResolutionTests` (`tests.py:49-161`) - full branch/tenant/
  platform/default precedence, branch-tenant mismatch rejection, every typed
  validation, mismatched min/max bounds failing as 422 rather than 500, the
  `get_config` public API, secret redaction inside audit snapshots, and audit
  immutability.
- `ConfigurationAPISecurityTests` (`tests.py:265-419`) - a `SCHOOL_ADMIN` user
  type alone is a 403; a granted read key does not grant a platform mutation;
  a foreign branch and a malformed branch are both 404; a school write lands at
  the school's own tenant scope and is audited; an unmapped method is 405;
  bulk writes are atomic.
- `GenericValueResetAPITests` (`tests.py:535-572`) - reset clears only the
  selected layer and reports the newly effective value; reset is idempotent.
- `BranchScopeTenantBoundaryTests` (`tests.py:1235-1310`) - the
  `normalize_scope` matrix, `set_value` refusing a branch outside the named
  tenant, and a branch-scoped audit row inheriting the branch's own tenant.

What it does not cover:

1. **The real tenant assertion path.** 35 of the tests authenticate with
   `force_authenticate`, which never sets `request.tenant`, so
   `resolve_request_scope` silently falls back to the user's home tenant. Only
   `test_entitlement_accepts_schedule_and_reset_restores_parent`
   (`tests.py:819-866`) logs in for a real JWT and exercises `?tenant=` and
   `platform_cross_tenant_param`. Nothing tests a school caller *attempting* a
   foreign `?tenant=`, which is the assertion the auth layer is supposed to
   refuse.
2. **Archiving through PATCH.** Nothing asserts that `config.definition.update`
   should not be able to flip `is_active` (issues file §9).
3. **The empty-list response shape** on `/values/`, `/effective-values/` and
   `/export/`, which matters because `success_response` coerces `[]` to `{}`
   (`core/response.py:6-11`).
4. **A CHOICE definition with no `choices` rule**, and a SECRET_REFERENCE value
   round-tripping through `get_config`.
5. **`?include_inactive=true`** on the definitions list, and retrieving an
   archived definition by key.
6. **Pagination** on any list in this slice: no test asks for page 2.
7. **DELETE `/values/<key>/` at branch scope**, or with a `?branch=` that names
   another tenant's branch.
