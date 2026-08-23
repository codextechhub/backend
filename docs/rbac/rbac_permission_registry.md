# rbac_permission_registry

The platform's vocabulary of privilege: the module / resource / action words a
permission key is built from, the `Permission` rows themselves, the scope column
that decides which audience may ever hold one, the dependency graph between
them, the reusable bundles (`PermissionGroup`), and the prebuilt role library a
new school is provisioned from.

Nothing in this slice grants anybody anything. It is the dictionary. Who holds
what is `rbac_roles_assignments`; how a held key is turned into a yes or no at
request time is `rbac_evaluation_scoping`; the exception layer and the approval
queue are `rbac_change_requests_overrides`.

Routes covered by this slice, all mounted at `/v1/rbac/` (`apps/urls.py:27`):
`vision/permission-modules/`, `vision/permission-modules/<name>/`,
`vision/permission-resources/`, `vision/permission-resources/<pk>/`,
`vision/permission-actions/`, `vision/permission-actions/<name>/`,
`vision/permissions/`, `vision/permissions/<key>/`,
`vision/permission-dependencies/`, `vision/permission-dependencies/<id>/`,
`vision/permission-groups/`, `vision/permission-groups/<uuid>/`.

Findings for the whole module are collected in
**`error/rbac/rbac_code_issues.md`**; §8 below points at the items that belong to
this slice rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **A permission key is a composed primary key, not a free string.**
  `Permission.key` is built as `module.resource.action` from three foreign keys
  and rebuilt on every save (`models.py:293-296`). You cannot post a key; you
  post the three parts and the key falls out.
- **The registry is global, and deliberately so.** There is no tenant column on
  `Permission`, `PermissionGroup` or any vocabulary table. One dictionary, every
  tenant. What varies per tenant is which keys its roles carry.
- **`scope` is the security boundary, and it is a stored column, not a prefix
  rule.** `PermissionScope` (`models.py:22-66`) has exactly two values -
  `TENANT` ("any tenant may hold it") and `PLATFORM` ("only a role on a
  `Tenant.Kind.PLATFORM` tenant may hold it"). The class docstring is explicit
  that the `platform.` namespace and the `PLATFORM` scope are **not** the same
  split, and names the two families that contradict their own prefix:
  `platform.team.*` and `platform.audit.view` / `.export` are `TENANT`.
- **The field has no default, on purpose.** An unclassified key (`scope = ""`)
  is not tenant-safe by omission - `assert_tenant_may_hold` refuses it for any
  non-platform tenant and names it in the error (`models.py:91-111`). The
  intent is that a seeder which forgets to classify a new key fails closed and
  loudly. See `rbac_code_issues` §4 for what happens when the key is created
  through the API instead of a seeder.
- **A group grants nothing on its own.** `PermissionGroup` is a container;
  attaching it to a role is what makes its contents effective
  (`evaluator.py:174-177`).
- **A group's declared scope must agree with its contents.**
  `GroupPermission.assert_scope_allowed` (`models.py:425-440`) refuses to put a
  platform-only key inside a bundle that is not itself declared `PLATFORM`, so
  the declaration cannot drift from what the bundle actually carries.
- **`PrebuiltRoleTemplate` is a blueprint library, not a role.** It is
  platform-owned, seeded, has no tenant, and confers nothing until a tenant
  copies it (`services.py:54-111`). There is **no API for it at all** - no list,
  no detail, no create. It is reachable only from school provisioning code.
- **`is_active` on any vocabulary row is metadata, not a switch.** Nothing in
  the evaluator consults `Permission.is_active`, `PermissionGroup.is_active`,
  `PermissionModule.is_active`, `PermissionResource.is_active` or
  `PermissionAction.is_active`. Deactivating any of them revokes nothing. The
  audit signals nevertheless announce that "all permissions under this module
  are affected" (`signals.py:230`) - see `rbac_code_issues` §7.
- **The dependency graph is advisory and one-directional.** It is checked when a
  role's permission set is written (`validators.py:159-192`), and nowhere else -
  not on a personal override, not on a prebuilt provisioning copy.
- **This slice is CX-only.** Every route here takes a `platform.permissions.*`
  key, and those keys are `PermissionScope.PLATFORM`, so no school role can hold
  one.

## 2. Domain model

### `PermissionScope` (`models.py:22`)

| Value | Meaning |
|---|---|
| `TENANT` | Any tenant's role may hold it, a school's and the platform's alike |
| `PLATFORM` | Only a role on a `Tenant.Kind.PLATFORM` tenant may hold it |
| `""` (blank) | Unclassified. Refused for every non-platform tenant |

There is deliberately no third value. "Tenant-only, never platform" was
considered and rejected, because `xvs_consultant` is a codex role that
legitimately holds `school.*` view keys (`models.py:54-56`).

Three module-level functions carry the rule (`models.py:69-111`):

| Function | What it does |
|---|---|
| `platform_only_keys(keys)` | One query; returns the subset of *keys* whose scope is anything other than `TENANT`, so an unclassified key is in the refused set |
| `tenant_is_platform(tenant)` | `tenant.kind == Tenant.Kind.PLATFORM` |
| `assert_tenant_may_hold(keys, tenant, field=…)` | Raises `ValidationError` unless every key may be held inside *tenant*. Platform tenants pass unconditionally |

### `ScopeGuardedManager` (`models.py:114-126`)

A manager whose `bulk_create` calls `assert_scope_allowed()` on every object
first. It exists because `bulk_create` bypasses `save()` and `clean()` entirely,
and `bulk_create` is exactly how the role serializers write permission sets - so
without it the model guard would be decorative on the one path that matters. It
is the default manager on `GroupPermission`, `PrebuiltRolePermission`,
`TenantRolePermission`, `TenantRoleGroup`, `TenantUserRoleAssignment` and
`UserPermissionOverride`.

### `PermissionModule` (`models.py:164`)

`name` is the primary key (a `SlugField`, max 64). Plus `description` and
`is_active`. Ordered `["-updated_at", "name"]`.

The seeded modules are `platform`, `school`, `academics`, `onboarding`,
`communication`, `config`, `exports`, `finance`, `import`, `payments`,
`procurement`, `tickets`, `todo`, `workflow` and `health`
(`core/management/commands/seed_all_permissions.py:54-79` lists the seeders
that register them).

### `PermissionResource` (`models.py:178`)

`module` FK (CASCADE) + `name` (`SlugField`), unique together. `str()` is
`"module.resource"`. Its `name` is unique only *within* a module, which is why
the permission serializer needs both to resolve one (`registry.py:169-187`).

### `PermissionAction` (`models.py:198`)

`name` is the primary key. **69 verbs** are seeded by
`core/management/commands/seed_actions.py` - the canonical ones (`view`,
`create`, `update`, `delete`, `manage`), a workflow family (`approve`, `reject`,
`submit`, `cancel`, `publish`, `archive`, `suspend`, `reactivate`, `replay`), a
finance family (`post`, `reverse`, `allocate`, `settle`, `depreciate`,
`writeoff`, `close`, `reopen`, `lock`, `establish`, `replenish`, `file`,
`approve_senior`), a procurement family (`award`, `issue`, `match`,
`override_variance`, `adjust`, `attach`), and the impersonation tier
(`impersonate`, `start`, `start_all`, `start_cx`, `start_school`, `end`).

### `Permission` (`models.py:215`)

| Field | Meaning |
|---|---|
| `key` | Primary key, max 180. Auto-composed `module.resource.action`, never posted |
| `module` / `resource` / `action` | FKs with `db_constraint=False` and `on_delete=PROTECT` |
| `description` | Free text |
| `sensitivity_level` | `NORMAL` / `SENSITIVE` / `CRITICAL` - grades how dangerous a key is *within* an audience |
| `scope` | `TENANT` / `PLATFORM` / blank - says which audience exists at all |
| `is_restricted` | Marks keys that "must flow through approvals". Read by nothing in this app |
| `is_active` | Soft-delete toggle. Read by nothing in the evaluator |

`sensitivity_level`, `is_restricted` and `scope` answer three different
questions and are stored separately on purpose (`models.py:226-232`).

`save()` recomputes `key` from `module_id`, `resource.name` and `action_id`
unless `update_fields` was passed (`models.py:293-296`). Because `key` *is* the
primary key, saving a `Permission` whose parts changed does not rename the row -
it writes a new one. `PermissionDetailView.update` works around this with a
manual `.update(key=new_key)` before saving (`views.py:340-343`); see
`rbac_code_issues` §5.

Indexes: `(module, action)` and `(is_restricted, sensitivity_level)`; `scope` is
`db_index=True` on its own.

### `PermissionDependency` (`models.py:302`)

`permission` → `depends_on`, both FKs to `Permission.key`, CASCADE, unique
together. Example: `finance.invoice.approve` depends on
`finance.invoice.view`. Related names are `dependencies` and `required_by`.

### `PermissionGroup` (`models.py:342`)

UUID primary key. `name` (120, case-insensitively unique enforced in the
serializer, not the database), `description`, `scope`, `is_system`,
`is_active`, and an M2M to `Permission` through `GroupPermission`.

`is_system` blocks deletion through the API (`views.py:488-496`); nothing else
reads it.

### `GroupPermission` (`models.py:395`)

The join row. Unique on `(group, permission)`, indexed on each side.
`assert_scope_allowed` (`models.py:425-440`): a `PLATFORM` group may carry
anything, because only CX can attach one; any other group refuses a
platform-only key.

### `PrebuiltRoleTemplate` (`models.py:457`)

| Field | Meaning |
|---|---|
| `key` | Unique string, e.g. `school_admin` |
| `name` | Display name |
| `scope` | `institution` / `branch` / `class` / `portal` - **a different, unrelated vocabulary** from `PermissionScope` |
| `tier` | `A` Core / `B` Module-Dependent / `C` Optional |
| `is_active` | Only active templates can be provisioned (`services.py:68`) |

Three rows are seeded
(`core/management/commands/seed_prebuilt_role_templates.py`): `school_admin`
(institution, tier A), `branch_admin` (branch, tier A) and `teacher` (branch,
tier B).

### `PrebuiltRolePermission` (`models.py:499`)

`prebuilt_role` + `permission`, unique together. `assert_scope_allowed`
(`models.py:525-540`) refuses a platform key outright - there is no platform
prebuilt role, so a platform key here would be a fleet-wide grant.

## 3. Endpoint map

Every route below takes `IsAuthenticatedAndActive` + `HasRBACPermission`, and
every one of them requires the `?tenant=` assertion (no view in this file sets
`tenant_param_required = False`).

| Route | Verb | `rbac_permission` | Notes |
|---|---|---|---|
| `vision/permission-modules/` | GET | `platform.permissions.view` | Filters `?is_active=`, `?search=` (name contains). Paginated |
| | POST | `platform.permissions.create` | Body: `name`, `description`, `is_active` |
| `vision/permission-modules/<name>/` | GET | `platform.permissions.view` | Looked up by `name` |
| | PUT/PATCH | `platform.permissions.update` | |
| | DELETE | `platform.permissions.manage` | |
| `vision/permission-resources/` | GET | `platform.permissions.view` | Filters `?module=`, `?is_active=`, `?search=`; annotates `permissions_count` |
| | POST | `platform.permissions.create` | Body: `module` (name slug), `name`, `description`, `is_active` |
| `vision/permission-resources/<pk>/` | GET / PUT / PATCH / DELETE | `view` / `update` / `update` / `manage` | |
| `vision/permission-actions/` | GET | `platform.permissions.view` | Filters `?is_active=`, `?search=`; annotates `permissions_count` |
| | POST | `platform.permissions.create` | |
| `vision/permission-actions/<name>/` | GET / PUT / PATCH / DELETE | `view` / `update` / `update` / `manage` | |
| `vision/permissions/` | GET | `platform.permissions.view` | Filters `?module_key=`, `?action=`, `?is_active=`, `?is_restricted=`, `?sensitivity_level=`, `?search=` (key, module name, resource name, action name, description) |
| | POST | `platform.permissions.create` | Body reads **only** `module`, `resource` (name slug), `action`, `description`, `sensitivity_level`, `is_restricted`, `is_active`. `scope` is not on the serializer |
| `vision/permissions/<key>/` | GET | `platform.permissions.view` | `PermissionDetailSerializer` adds `groups`, `dependencies`, `dependents` |
| | PUT/PATCH | `platform.permissions.update` | Custom `update()`; recomposes and rewrites the primary key |
| | DELETE | `platform.permissions.delete` | Custom `delete()`; wraps failures as a 500 envelope |
| `vision/permission-dependencies/` | GET | `platform.permissions.view` | Paginated |
| | POST | `platform.permissions.manage` | Body: `permission_key`, `depends_on_key` |
| `vision/permission-dependencies/<id>/` | GET / DELETE | `view` / `manage` | No update route |
| `vision/permission-groups/` | GET | `platform.permissions.view` | Filters `?is_active=`, `?is_system=`, `?search=`; annotates `permissions_count`; ordered by name |
| | POST | `platform.permissions.manage` | Body: `name`, `description`, `is_active`, `permission_keys[]` |
| `vision/permission-groups/<uuid>/` | GET | `platform.permissions.view` | Expands full `Permission` rows |
| | PUT/PATCH | `platform.permissions.manage` | `permission_keys` **replaces** membership |
| | DELETE | `platform.permissions.manage` | 403 for `is_system = True` |

Responses use the `success_response` envelope; list routes use `XVSPagination`
(page 25, `?page_size=` ≤ 100).

## 4. Lifecycle / state machine

There is no state machine here. Vocabulary rows have one flag (`is_active`) that
nothing enforces, and permissions have no status at all. The only genuine
lifecycle is the seeding order, which is a dependency chain rather than a state
graph (`seed_all_permissions.py:54-79`):

```
seed_actions                     verbs first - every later seeder skips a
                                 permission whose action verb is missing
  └─ seed_prebuilt_role_templates    school_admin / branch_admin / teacher
       └─ seed_school_permissions    school + academics keys, prebuilt defaults,
                                     backfill into existing tenant roles
            └─ seed_platform_permissions   the platform module + codex roles
                 └─ per-module seeders (import, workflow, config, finance,
                    procurement, payments, exports, todo, tickets,
                    notifications, onboarding, health)
                      └─ seed_school_permission_groups   runs LAST, because it
                         groups keys from five modules and can only see the ones
                         already registered
```

`seed_all_permissions` then runs `_ensure_super_admin_has_every_permission`,
which writes an explicit `TenantRolePermission` row on `xvs_super_admin` for
every active key - not because the role needs it (it holds a runtime bypass) but
because the console consumes the effective key list for navigation
(`seed_all_permissions.py:122-140`).

Every seeder is idempotent (`get_or_create` throughout) and safe to re-run.

## 5. Derivations

### The permission key

```
key = f"{module_id}.{resource.name}.{action_id}"
```

`models.py:295`. Recomputed on every save without `update_fields`. Because
`module_id` and `action_id` *are* the module and action names (both are their
own primary keys), this is three string reads and one attribute traversal to
`resource.name`.

The serializer composes the same string independently for its duplicate guard
(`registry.py:204`) and the detail view composes it a third time when the parts
change (`views.py:338`).

### Scope classification

`vs_rbac/migrations/0007_classify_permission_scope.py:70-88` is the one place
the whole registry was classified, and it is derived from what the seeders
register rather than from the key text:

```python
Permission.objects.filter(module_id="platform").update(scope="PLATFORM")
Permission.objects.exclude(module_id="platform").update(scope="TENANT")
Permission.objects.filter(key__in=TENANT_HOLDABLE_KEYS).update(scope="TENANT")
```

`TENANT_HOLDABLE_KEYS` is eight keys - `platform.team.view/create/update/
delete/suspend/reactivate` and `platform.audit.view/export` - kept in step with
`seed_platform_permissions.TENANT_HOLDABLE_KEYS` (`migrations/0007:58-67`,
`seed_platform_permissions.py:179-188`). `platform.audit.manage` is deliberately
excluded: it edits compliance and retention rules through an unscoped queryset.

Groups are then classified by contents - any platform member makes the whole
bundle platform (`migrations/0007:79-88`).

From that migration on, new keys are classified at creation time by their
seeder, e.g. `seed_platform_permissions.py:256-260`:

```python
scope=(PermissionScope.TENANT
       if expected_key in TENANT_HOLDABLE_KEYS
       else PermissionScope.PLATFORM)
```

and `seed_school_permissions.py:256` hardcodes `PermissionScope.TENANT`.

### Dependency closure

`PermissionDependencyValidator` (`validators.py:18-132`):

1. `_load_dependencies()` reads the entire `PermissionDependency` table once
   into `{permission_key: {required_keys}}` (`validators.py:33-46`).
2. `get_all_dependencies(key, visited)` walks it recursively, adding each direct
   prerequisite and then its own closure, passing `visited.copy()` down each
   branch so a diamond is not mistaken for a cycle (`validators.py:54-79`).
   Re-entering a key already on the current path raises
   `"Circular dependency detected for permission: <key>"`.
3. `validate_permission_set(keys)` returns
   `{"valid": bool, "missing_dependencies": {key: [missing…]}, "errors": [...]}`
   (`validators.py:82-115`).

`flatten_permission_keys(permission_keys, group_ids)` (`validators.py:136-155`)
unions the direct keys with every key inside the named groups, so a prerequisite
satisfied *through a group* counts. `validate_role_permissions` is the public
entry point: it flattens, returns silently for an empty set, and otherwise
raises `ValidationError({"permission_keys": ["Permission 'X' requires: a, b"]})`
(`validators.py:159-192`).

`detect_circular_dependencies()` exists (`validators.py:118-132`) and is called
by nothing.

### Provisioning a tenant role from a prebuilt

`services.provision_role_from_prebuilt` (`services.py:54-111`):

| Input | Result |
|---|---|
| `branch=None` | `key = prebuilt.key`, `name = prebuilt.name` |
| `branch=<Branch>` | `key = f"{prebuilt.key}-{branch.pk}"`, `name = f"{prebuilt.name} - {branch.name}"` |

The per-branch suffix exists because `TenantRoleTemplate` is unique on
`(tenant, key)` *and* `(tenant, name)`, so several branches each needing their
own copy would collide otherwise. The role is created `is_system_role=True,
is_locked=True`, and its defaults are copied only on first creation
(`services.py:94-109`) via `bulk_create(..., ignore_conflicts=True)` - which
still runs the scope guard, because `TenantRolePermission.objects` is a
`ScopeGuardedManager`.

`create_role_from_suggestion` (`services.py:302-334`) is the second, unused
variant: it refuses when a role of that name already exists, allocates a unique
key with `_unique_tenant_role_key`, and creates a **non**-system, **non**-locked
role. Nothing in the repo calls it.

## 6. What writing writes

| Action | Rows written | Audit |
|---|---|---|
| `POST vision/permission-modules/` | 1 `PermissionModule` | `RBACAuditLog` CREATE, severity WARNING (`signals.py:204-215`) |
| Deactivate a module | `is_active` flipped | UPDATE, severity **CRITICAL**, summary claims every permission under it is affected (`signals.py:221-233`) |
| `DELETE` a module | Cascade to `PermissionResource`; `Permission` is `PROTECT` | DELETE, CRITICAL, summary claims a cascade (`signals.py:243-252`) |
| `POST vision/permissions/` | 1 `Permission` | CREATE, WARNING when `sensitivity_level != NORMAL` (`signals.py:60-75`) |
| Deactivate a permission | `is_active` flipped | UPDATE, WARNING, with a `before/after` diff (`signals.py:77-89`) |
| `POST vision/permission-dependencies/` | 1 `PermissionDependency` | CREATE, WARNING (`signals.py:96-118`) |
| `DELETE` a dependency | row removed | DELETE, WARNING (`signals.py:121-140`) |
| `POST vision/permission-groups/` | 1 `PermissionGroup` (+ N `GroupPermission`) | one PERMISSION_CHANGED row **per member** (`signals.py:147-167`) |
| `PATCH` a group's `permission_keys` | all `GroupPermission` rows deleted and rebuilt; every attached `TenantRoleTemplate.version` bumped | one row per removal and one per addition |
| `DELETE` a group | Cascades `GroupPermission` **and** `TenantRoleGroup` across every tenant | one PERMISSION_CHANGED row per member |

Every one of those rows goes through `record_rbac_audit` (`audit.py:20-91`),
which writes the durable `RBACAuditLog` row **first** - a failure there raises
and rolls the action back - then mirrors best-effort to `vs_audit`, swallowing
and logging any mirror failure.

`RBACAuditLog.school_id` is filled from `metadata["school_id"]`
(`audit.py:46`). No signal in this slice puts one there, so every registry audit
row carries `school_id = ""`. Since the table has no tenant column either, these
rows are attributable only by `actor` and `entity_id`.

Two things this slice writes that are **not** audited: creating a
`PermissionResource` or `PermissionAction` is audited, but editing any field
other than `is_active` on a module, resource, action or permission is not - the
receivers only diff `is_active` (`signals.py:217-233`, `284-300`, `350-366`) and
`Permission`'s receiver diffs nothing else at all (`signals.py:77-89`). Renaming
a permission's description, changing its `sensitivity_level`, or flipping
`is_restricted` leaves no trace.

## 7. Worked example

CX adds a new privilege: schools should be able to waive a fee.

**1. The verb already exists.** `waive` is one of the 69 seeded actions
(`seed_actions.py:72`), so no `POST vision/permission-actions/` is needed.

**2. The resource exists too.** `school.fees` was registered by
`seed_school_permissions` (`seed_school_permissions.py:78-79`).

**3. Create the key.**

```http
POST /v1/rbac/vision/permissions/?tenant=codex
{ "module": "school", "resource": "fees", "action": "waive",
  "description": "Grant a fee exemption", "sensitivity_level": "SENSITIVE",
  "is_restricted": true }
```

`PermissionSerializer.validate` (`registry.py:165-213`) resolves `"fees"` to the
`PermissionResource` row inside module `school`, confirms the resource really
belongs to that module, composes `school.fees.waive`, and checks nothing already
holds that key. `Permission.save()` writes the key. The `post_save` receiver
writes an `RBACAuditLog` CREATE row at severity WARNING, because the sensitivity
is not NORMAL.

**4. Add its prerequisite.**

```http
POST /v1/rbac/vision/permission-dependencies/?tenant=codex
{ "permission_key": "school.fees.waive", "depends_on_key": "school.fees.view" }
```

From here on, any attempt to write a role carrying `school.fees.waive` without
`school.fees.view` fails with
`"Permission 'school.fees.waive' requires: school.fees.view"`.

**5. Corona's bursar role gets it.** Corona's admin PATCHes their Bursar role
with `permission_keys: ["school.fees.view", "school.fees.waive", …]`. The
serializer flattens, validates the dependency, checks
`platform_only_keys(["school.fees.waive", …])`… and **refuses**, because the
key was created through the API and its `scope` is the empty string. The error
reads *"Permission(s) school.fees.waive are platform-scoped and cannot be
granted inside a tenant. If a key is missing a scope, classify it in the seeder
that registers it."* There is no seeder - and no endpoint that can set `scope`
either. The key is stuck. This is `rbac_code_issues` §4, and the way round it
today is to edit the row in the database or add it to a seeder and re-run.

**6. Had the key come from a seeder**, `scope` would be `TENANT`
(`seed_school_permissions.py:256`), the PATCH would succeed, the role's
`version` would go from 3 to 4, and the bursar's next request would find
`school.fees.waive` in `get_effective_permissions`.

## 8. Gotchas / known limitations

Recorded in full in **`error/rbac/rbac_code_issues.md`**. The items belonging to
this slice:

| # in that file | One line |
|---|---|
| §4 | **Confirmed by execution.** A permission created through `POST vision/permissions/` gets no `scope`, so no school role can ever hold it, and no endpoint can fix it |
| §5 | Editing a permission's module / resource / action rewrites the primary key while every grant row still points at the old one |
| §7 | **Confirmed by execution.** `is_active` on a permission, group, module, resource or action revokes nothing; the audit rows announce a cascade that does not happen |
| §8 | Deleting a `PermissionGroup` silently strips it from every tenant's roles, with no confirmation and no PROTECT |
| §11 | Nothing checks the dependency graph for cycles when a dependency is created, and one cycle bricks every role edit touching those keys |
| §14 | `PermissionDetailSerializer` and the group detail serializer are N+1 on `resource` |
| §17 | **Confirmed by execution.** Renaming a permission module or action creates a duplicate row instead of renaming |
| §19 | `PermissionGroup` scope is invisible: it is on neither the list nor the detail serializer, so CX cannot see or set it |

Two design choices worth stating as choices, not defects:

- **The registry is unscoped by tenant on purpose.** One dictionary shared by
  every tenant is what lets a key mean the same thing everywhere, and it is why
  the boundary had to become a column (`scope`) rather than a filter.
- **`ScopeGuardedManager.bulk_create` is the guard that matters.** Putting the
  check on `save()` alone would have left the serializers' `bulk_create` path -
  the one an attacker reaches - unguarded (`models.py:117-120`).

## 9. Permissions & tenant isolation

- **Six keys gate this slice**, all in the `platform.permissions` resource:
  `view`, `create`, `update`, `manage`, `delete`
  (`seed_platform_permissions.py:27-37`). `manage` is `SENSITIVE` and
  restricted; the rest are `NORMAL`.
- **All five are `PermissionScope.PLATFORM`**, because
  `seed_platform_permissions` classifies everything in the module as `PLATFORM`
  unless it is in `TENANT_HOLDABLE_KEYS`, and `platform.permissions.*` is not.
  So a school role cannot hold one, and `TenantRolePermission.save()` refuses to
  write it (`models.py:630-640`).
- **Both codex roles get them.** `xvs_super_admin` and `xvs_platform_admin` are
  granted every platform key except `platform.roles.transfer`
  (`seed_platform_permissions.py:277-302`).
- **There is no tenant scoping on the querysets, and there should not be** - the
  registry is global. Isolation here comes entirely from the key being
  `PLATFORM`-scoped plus the fact that `TenantJWTAuthentication` will not let a
  school actor assert a foreign `?tenant=`.
- **The super-admin bypass short-circuits the scope check.**
  `HasRBACPermission.has_permission` returns `True` for `is_vision_super_admin`
  before any key is evaluated (`permissions.py:300-302`), so `scope` never runs
  for that caller. That is intended for a real CX super admin, and is the
  amplifier for `rbac_code_issues` §1.
- **`PrebuiltRoleTemplate` has no endpoint**, so its blueprints cannot be
  inspected, edited or added without a code change and a re-seed.

## 10. Code map

| File | What lives there |
|---|---|
| `vs_rbac/models.py:22-66` | `PermissionScope` and its reasoning |
| `vs_rbac/models.py:69-126` | `platform_only_keys`, `tenant_is_platform`, `assert_tenant_may_hold`, `ScopeGuardedManager` |
| `vs_rbac/models.py:164-209` | `PermissionModule`, `PermissionResource`, `PermissionAction` |
| `vs_rbac/models.py:215-336` | `Permission`, `PermissionDependency` |
| `vs_rbac/models.py:342-451` | `PermissionGroup`, `GroupPermission` |
| `vs_rbac/models.py:457-551` | `PrebuiltRoleTemplate`, `PrebuiltRolePermission` |
| `vs_rbac/views.py:101-234` | Module / resource / action CRUD |
| `vs_rbac/views.py:241-406` | Permission and dependency CRUD |
| `vs_rbac/views.py:413-496` | Permission group CRUD |
| `vs_rbac/serializers/registry.py` | Every serializer in this slice |
| `vs_rbac/validators.py` | Dependency graph loading, closure, validation |
| `vs_rbac/services.py:54-111` | `provision_role_from_prebuilt` |
| `vs_rbac/services.py:302-334` | `create_role_from_suggestion` (uncalled) |
| `vs_rbac/signals.py:50-385` | Every audit receiver for the vocabulary tables |
| `vs_rbac/migrations/0007_classify_permission_scope.py` | The one-off classification and its evidence |
| `core/management/commands/seed_actions.py` | 69 action verbs |
| `core/management/commands/seed_prebuilt_role_templates.py` | The three prebuilt roles |
| `core/management/commands/seed_platform_permissions.py` | The `platform` module, `TENANT_HOLDABLE_KEYS`, codex grants |
| `core/management/commands/seed_school_permissions.py` | `school` + `academics` keys, prebuilt defaults, tenant backfill |
| `core/management/commands/seed_all_permissions.py` | The dependency-ordered master seed |

Dead or unreachable code in this slice: `models._unique_slug` (`models.py:129`)
is called by nothing; `validators.detect_circular_dependencies` is called by
nothing; `services.create_role_from_suggestion` is called by nothing.

## 11. Test coverage & gaps

Module baseline at the time of writing: **`Ran 326 tests in 89.035s` - OK**
(`cd apps && DB_NAME=cx_rbacslice ../cx/Scripts/python.exe manage.py test
vs_rbac --settings=apps.settings.local --noinput`). The single traceback in that
run is `test_audit`'s deliberate `RuntimeError: boom`, proving the central
mirror's failure is swallowed.

Covered for this slice:

- `tests/test_views.py` - permission registry CRUD end to end, including the
  duplicate-key guard and the module/resource ownership check.
- `tests/test_models.py` - key composition, `PermissionDependency` uniqueness,
  group membership.
- `tests/test_validators.py` - dependency closure, missing prerequisites, cycle
  detection through `get_all_dependencies`.
- `tests/test_platform_scope_escalation.py` - the scope guard on every grant
  path, including the `bulk_create` route.
- `core/test_seed_all_permissions.py`, `core/test_seed_school_permissions.py`,
  `core/test_seed_school_permission_groups.py` - seeder idempotence and the
  classification the seeders write.

Not covered:

- **No test asserts that a permission created through the API is grantable.**
  That is exactly the gap `rbac_code_issues` §4 falls through.
- No test exercises `PermissionDetailView.update` when the module, resource or
  action changes - the key-rewrite path (§5).
- No test deactivates a permission or group and asserts anything about the
  effective set (§7), which is why the no-op is invisible.
- No test renames a module or action through the API, which is why the duplicate
  row §17 describes has gone unnoticed.
- No test creates a dependency cycle through the API (§11).
- `PrebuiltRoleTemplate` is exercised only indirectly, through
  `provision_role_from_prebuilt` in the school seed tests.
- `detect_circular_dependencies` has no test, because it has no caller.
