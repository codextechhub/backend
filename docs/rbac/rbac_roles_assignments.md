# rbac_roles_assignments

How a tenant turns the global permission dictionary into actual access: the role
templates a tenant owns, the permission and group rows attached to them, the
assignments that hand a role to a person, the branch column that narrows a grant
to one site, and the two guarded operations - revoke and replace - plus the
singleton Super Admin transfer.

The dictionary itself is `rbac_permission_registry`. Turning a held key into a
yes or no at request time is `rbac_evaluation_scoping`. The approval queue and
the per-user exception layer are `rbac_change_requests_overrides`.

Routes covered by this slice, mounted at `/v1/rbac/` (`apps/urls.py:27`):
`tenants/<tenant_slug>/roles/`, `tenants/<tenant_slug>/roles/<key>/`,
`tenants/<tenant_slug>/role-assignments/`,
`tenants/<tenant_slug>/role-assignments/<id>/`,
`tenants/<tenant_slug>/role-assignments/<id>/revoke/`,
`tenants/<tenant_slug>/role-assignments/<id>/replace/`,
`platform/transfer-super-admin/`.

Findings for the whole module are collected in
**`error/rbac/rbac_code_issues.md`**; §8 points at the ones that belong here.

---

## 1. What it is (and what it is NOT)

- **A role belongs to exactly one tenant and is addressed by a per-tenant key.**
  `TenantRoleTemplate` is unique on `(tenant, key)` *and* on `(tenant, name)`
  (`models.py:585-589`). The URL is `roles/<key>/`, not `roles/<id>/`.
- **The key is derived from the name, once, and then frozen.**
  `_unique_tenant_role_key(tenant, name)` slugifies the name and appends `-1`,
  `-2`… until it is free inside that tenant (`serializers/tenant.py:145-157`).
  `key` is read-only on the serializer and is never recomputed on rename
  (`serializers/tenant.py:303-311`). Django's `slugify` keeps underscores, which
  matters - see `rbac_code_issues` §1.
- **Branch appears on both the role and the assignment, and only the assignment
  one does anything.** `TenantRoleTemplate.branch` is validated
  (`models.py:595-598`), filterable (`views.py:540-541`) and read by no part of
  evaluation. `TenantUserRoleAssignment.branch` is the real scope column.
- **A branch-pinned grant confers real access, not narrower nothing.** The
  evaluator's default is `ANY_BRANCH` - "the caller named no branch, so do not
  narrow" - and every grant counts, whole-tenant or pinned
  (`evaluator.py:52-58`). Which rows the holder then *sees* is answered
  separately and once by `scoping.visible_branch_ids`.
- **A branch-pinned grant dies with its branch.** `_assignment_branch_q` only
  counts a pinned grant while `branch.status` is in `Branch.IN_SERVICE_STATES`
  (`evaluator.py:61-81`). Suspending a site withdraws the access it conferred.
- **One person can hold the same role at two sites.** That is why the uniqueness
  constraint is split in two (`models.py:737-761`): one partial unique index for
  whole-tenant grants (`branch IS NULL`) and one for pinned grants including
  `branch`. A single constraint over `(tenant, user, role, branch)` would not
  do, because PostgreSQL treats NULLs as distinct and would silently permit
  duplicate whole-tenant grants.
- **A revoked assignment is never reactivated.** The serializer refuses the
  transition outright: *"A revoked assignment cannot be reactivated. Create a
  new assignment instead."* (`serializers/tenant.py:714-727`).
- **`xvs_super_admin` is a singleton with its own endpoint.** It cannot be
  assigned (`serializers/tenant.py:688-696`), revoked
  (`serializers/tenant.py:698-712`, `views.py:720-725`) or swapped in
  (`views.py:791-816`) through the ordinary routes. Only
  `POST platform/transfer-super-admin/` moves it.
- **These routes are same-tenant only.** No view in this slice sets
  `platform_cross_tenant_param`, so `TenantJWTAuthentication` refuses a foreign
  `?tenant=` slug (`authentication.py:119-128`) before the view is reached. CX
  cannot administer a school's roles at all - which makes the super-admin
  branches inside `TenantRoleTemplateDetailView.update` unreachable for school
  tenants (`rbac_code_issues` §12).
- **`version` is written and read by nothing.** Three code paths bump it and
  describe the bump as invalidating downstream caches
  (`serializers/tenant.py:476`, `serializers/registry.py:408`,
  `services.py:174`). No cache consults it.
- **This is not the audit trail for role permission edits.** Assignments and
  role creation are audited; changing a role's `permission_keys` through the
  detail endpoint writes no `RBACAuditLog` row at all (`rbac_code_issues` §3).

## 2. Domain model

### `TenantRoleTemplate` (`models.py:558`)

| Field | Meaning |
|---|---|
| `tenant` | FK, PROTECT. The owning tenant |
| `branch` | FK, PROTECT, nullable. Validated against the tenant; read by no evaluation code |
| `key` | `SlugField(120)`, unique per tenant, read-only after creation |
| `name` | `CharField(80)`, unique per tenant (DB constraint + case-insensitive serializer check) |
| `description` | Free text |
| `status` | `ACTIVE` / `INACTIVE` / `ARCHIVED`, default `ACTIVE` |
| `is_system_role` | Blocks update and delete through the API unless the caller is a Vision super admin |
| `is_locked` | Same, for provisioned copies |
| `version` | `PositiveIntegerField(default=1)`. Bumped, never read |
| `created_by` | FK, SET_NULL |

`clean()` refuses a branch belonging to another tenant (`models.py:595-598`).
Indexes: `(tenant, status)` and `(tenant, branch, status)`.

Only `status = "ACTIVE"` roles are evaluated (`evaluator.py:162`), so setting a
role `INACTIVE` is the real off-switch - unlike `is_active` anywhere in the
registry.

### `TenantRolePermission` (`models.py:604`)

`role` (CASCADE) + `permission` (CASCADE, `to_field="key"`, so `permission_id`
*is* the dotted key), `granted` (bool), `granted_by`, `granted_at`. Unique on
`(role, permission)`; indexed on `(role, granted)` and `(permission, granted)`.

`granted=False` is an explicit **deny** row, subtracted from the role's grants
(`evaluator.py:168-178`).

`assert_scope_allowed` (`models.py:630-640`) calls `assert_tenant_may_hold` for
grants only - an explicit deny is exempt, because taking a key away is never an
escalation and refusing it would make an existing deny row unsaveable.

### `TenantRoleGroup` (`models.py:651`)

`role` + `group`, unique together, plus `attached_by` / `attached_at`.

`assert_scope_allowed` (`models.py:671-693`) is the strictest guard in the
module: for a non-platform tenant it checks **both** that the group's declared
scope is `TENANT` **and** that every key actually inside it is holdable. The
belt-and-braces is deliberate - a group seeded before `scope` existed could be
declared `TENANT` while holding something it should not.

### `TenantUserRoleAssignment` (`models.py:704`)

| Field | Meaning |
|---|---|
| `tenant` | FK, PROTECT |
| `branch` | FK, PROTECT, nullable. **The** branch scope column |
| `user` | FK, CASCADE |
| `role` | FK, PROTECT |
| `assignment_status` | `ACTIVE` / `REVOKED` |
| `assigned_by`, `assigned_at` | Who and when |
| `revoked_at`, `revoked_by`, `reason_note` | Revocation record |

Constraints (`models.py:751-760`):

```
uq_active_tenant_user_role         (tenant, user, role)          WHERE status=ACTIVE AND branch IS NULL
uq_active_tenant_user_role_branch  (tenant, user, role, branch)  WHERE status=ACTIVE AND branch IS NOT NULL
```

Indexes: `(tenant, user, assignment_status)` and
`(tenant, role, assignment_status)`.

`assert_scope_allowed` (`models.py:769-787`) re-checks the role's granted keys
against the tenant on every save. It "cannot normally fire" - the role's own
rows are guarded as they are written - and exists for the row that predates the
guard, so a role already carrying a platform key stops being *assignable* as
well as ineffective.

`clean()` additionally pins user, role and branch to the assignment's tenant
(`models.py:793-804`); the API path relies on the serializer's tenant-scoped
reference fields rather than on `clean()`, which nothing calls.

`revoke(by_user, reason)` (`models.py:806-817`) is idempotent - a second call on
an already-REVOKED row returns immediately - and mutates in memory without
saving; every caller saves with an explicit `update_fields` list.

### `PrebuiltRoleTemplate` → tenant role

See `rbac_permission_registry` §5. `provision_role_from_prebuilt` is what school
provisioning calls (`schools/vs_schools/serializers.py:533`, `1036`, `1087`).

## 3. Endpoint map

`TenantScopedRBACMixin` (`views.py:69-93`) sits under every tenant-scoped view.
Its `initial()` runs after authentication and permission checks, then resolves
`self.tenant` by requiring that the URL `tenant_slug` equals
`request.tenant.slug`; a mismatch is a non-enumerating
`NotFound("No tenant matches the requested context.")` (`views.py:83-88`). It
also injects `tenant` into the serializer context, which is what every reference
field resolves inside.

The permission keys are any-of lists spanning the school and platform
namespaces (`views.py:55-66`):

```python
ROLE_VIEW_KEYS   = ["school.roles.view",   "platform.roles.view"]
ROLE_LIST_KEYS   = ROLE_VIEW_KEYS + ["workflow.template.manage"]
ROLE_CREATE_KEYS = ["school.roles.create", "platform.roles.create"]
ROLE_UPDATE_KEYS = ["school.roles.update", "platform.roles.update"]
ROLE_DELETE_KEYS = ["school.roles.delete", "platform.roles.delete"]
ROLE_ASSIGN_KEYS = ["school.roles.assign", "platform.roles.assign"]
```

`workflow.template.manage` is on the **list** route only, and for a stated
reason: an approval stage names the role that approves it, and there is no way
to name one without seeing the list. Role detail and every write still take the
role keys themselves (`views.py:56-62`).

| Route | Verb | Keys | Body actually read |
|---|---|---|---|
| `tenants/<slug>/roles/` | GET | `ROLE_LIST_KEYS` | `?branch=`, `?status=`. Annotates `assigned_users_count` (ACTIVE only) and `permissions_count` (granted only); ordered by name; paginated |
| | POST | `ROLE_CREATE_KEYS` | `name`, `description`, `status`, `branch`, `permission_keys[]`, `group_ids[]` |
| `tenants/<slug>/roles/<key>/` | GET | `ROLE_VIEW_KEYS` | Expands `role_permissions` and `role_groups` |
| | PUT/PATCH | `ROLE_UPDATE_KEYS` | Same as POST minus `key`. 403 first if `is_locked` or `is_system_role` and the caller is not a Vision super admin (`views.py:585-598`) |
| | DELETE | `ROLE_DELETE_KEYS` | 403 for `is_system_role`, 403 for `is_locked` (`views.py:600-612`) |
| `tenants/<slug>/role-assignments/` | GET | `ROLE_VIEW_KEYS` | `?user=`, `?role=` (numeric id or key), `?assignment_status=`; ordered `-created_at`; paginated |
| | POST | `ROLE_ASSIGN_KEYS` | `user`, `role`, `branch`, `reason_note` |
| `tenants/<slug>/role-assignments/<id>/` | GET | `ROLE_VIEW_KEYS` | |
| | PUT/PATCH | `ROLE_ASSIGN_KEYS` | `user`, `role`, `branch`, `assignment_status`, `reason_note` |
| `tenants/<slug>/role-assignments/<id>/revoke/` | POST | `ROLE_ASSIGN_KEYS` | `reason_note` (**required**) |
| `tenants/<slug>/role-assignments/<id>/replace/` | POST | `ROLE_ASSIGN_KEYS` | `role` (**required**, the new role's numeric id), `reason_note` (optional) |
| `platform/transfer-super-admin/` | POST | `platform.roles.transfer` + `IsVisionSuperAdmin` | `new_super_admin_id` |

Every reference on the write serializers is a `TenantScopedRelatedField`
(`serializers/tenant.py:98-139`), which:

- filters its queryset by the serializer's tenant, so another tenant's row is
  simply not in scope;
- replaces **both** DRF failure messages (`does_not_exist` and
  `incorrect_type`) with one wording, so a foreign id, an absent id and a
  malformed id are indistinguishable;
- rejects a non-numeric or oversized id before it reaches PostgreSQL
  (`serializers/tenant.py:134-139`), because a bigint overflow is a 500 rather
  than an empty result.

`branch` is resolved against `Branch.all_objects` plus an explicit tenant filter
- deliberately, so the boundary is the tenant the view supplied and not the
ambient request-local tenant state `Branch.objects` reads
(`serializers/tenant.py:255-264`, `533-541`).

`TenantScopedSerializerMixin.run_validation` (`serializers/tenant.py:80-95`)
refuses the whole payload with `{"tenant": ["Tenant context is required."]}`
before a single reference is resolved when no tenant is knowable, so the
non-enumerating guarantee is unconditional rather than "unless the context is
missing".

## 4. Lifecycle / state machine

### Role

```
                      POST roles/            PATCH status=INACTIVE
      (nothing)  ────────────────────►  ACTIVE  ◄──────────────────►  INACTIVE
                                          │                              │
                                          │  PATCH status=ARCHIVED       │
                                          └──────────────►  ARCHIVED  ◄──┘
```

Only `ACTIVE` is evaluated (`evaluator.py:162`, `scoping.py:87`). `INACTIVE` and
`ARCHIVED` behave identically everywhere in this module; nothing distinguishes
them. DELETE is refused for system and locked roles, and for any other role it
raises `ProtectedError` the moment an assignment or change request has ever
pointed at it - which `core/exceptions.py:130-140` renders as a clean 409 naming
the blocker.

### Assignment

```
   POST role-assignments/        ┌── POST …/revoke/ (reason required) ──┐
        or vs_user user creation │   PATCH assignment_status=REVOKED    │
   (nothing) ──────────► ACTIVE ─┤   POST …/replace/ (revoke + create)  ├─► REVOKED
                                 └──────────────────────────────────────┘
                                              (terminal - no way back)
```

Reactivation is refused by the serializer. A second revoke of an
already-REVOKED row is a 409 through the revoke endpoint (`views.py:714-718`)
and a no-op through `Model.revoke` (`models.py:807-808`).

### Super Admin transfer

```
from_user holds xvs_super_admin on codex
        │
        ├─ that assignment is revoked, reason "transferred to another user"
        ├─ every ACTIVE codex assignment on to_user is bulk-revoked
        ├─ from_user is granted xvs_platform_admin
        ├─ to_user is granted xvs_super_admin
        └─ is_superuser is flipped on both rows
```

All inside one `@transaction.atomic` (`services.py:202-297`).

## 5. Derivations

### The role key

```python
base = slugify(name) or "role"
slug = base
n = 1
while TenantRoleTemplate.objects.filter(tenant=tenant, key=slug).exists():
    slug = f"{base}-{n}"; n += 1
```

`serializers/tenant.py:145-157`. Duplicated verbatim in `services.py:39-50`.
`django.utils.text.slugify` strips everything except word characters, spaces and
hyphens, then collapses runs of whitespace and hyphens - and `\w` includes the
underscore, so `slugify("xvs_super_admin") == "xvs_super_admin"`. That is the
mechanism behind `rbac_code_issues` §1.

### The two list annotations

```python
assigned_users_count = Count("user_assignments",
                             filter=Q(user_assignments__assignment_status="ACTIVE"),
                             distinct=True)
permissions_count    = Count("role_permissions",
                             filter=Q(role_permissions__granted=True),
                             distinct=True)
```

`views.py:525-536`. `permissions_count` counts direct grants only - keys reaching
the role through an attached `PermissionGroup` are not in it, so a role built
entirely from bundles reads as `0`.

### Which branch an assignment inherits

Three different callers answer this differently:

| Caller | Branch on the new assignment |
|---|---|
| `POST role-assignments/` | Whatever the body says, or `NULL` (`serializers/tenant.py:535-541`) |
| `POST …/replace/` | Copied from the assignment being replaced (`views.py:852-859`) |
| `vs_user` user creation | `user.branch` **if the role template is branch-pinned**, else `NULL` (`vs_user/services/user.py:102`) |
| `helpers.make_assignment` (tests) | The role template's branch unless overridden (`tests/helpers.py:249`) |

The third is the only place `TenantRoleTemplate.branch` influences anything, and
it influences the *assignment's* branch rather than being read at evaluation
time.

### Effective keys for one role

Given the role ids a user's live assignments point at:

```
granted  = {key : TenantRolePermission(role in ids, granted=True)}
denied   = {key : TenantRolePermission(role in ids, granted=False)}
granted |= every key inside every PermissionGroup attached to those roles
result   = granted - denied
```

`evaluator.py:156-178`. A deny on any one of the user's roles removes the key
from all of them - denies are unioned across roles, not scoped to the role that
carries them.

### Super-admin transfer preconditions

`services.transfer_super_admin` (`services.py:202-297`) raises `ValueError` -
surfaced as a 400 by the view (`views.py:1345-1346`) - for:

| Condition | Message |
|---|---|
| `from_user.pk == to_user.pk` | "Cannot transfer super admin to yourself." |
| `not to_user.is_platform_user` | "The new super admin must be a Vision Staff member." |
| No `codex` PLATFORM tenant | "Codex platform tenant not found." |
| Caller holds no ACTIVE `xvs_super_admin` on codex | "You do not hold the Vision Super Admin role." |
| `xvs_super_admin` or `xvs_platform_admin` role row missing | "Required platform role not found: …" |

That fourth check is what stops a counterfeit `xvs_super_admin` role in a school
tenant from being used here, even though `IsVisionSuperAdmin` lets its holder
through the door (`rbac_code_issues` §1).

## 6. What writing writes

| Action | Rows written | Audit |
|---|---|---|
| `POST roles/` | 1 `TenantRoleTemplate`, N `TenantRolePermission` (bulk), M `TenantRoleGroup` (bulk) | `RBACAuditLog` CREATE for the template (`signals.py:463-476`); one PERMISSION_CHANGED per attached group (`signals.py:497-518`). **Nothing for the permission rows** |
| `PATCH roles/<key>/` with `permission_keys` | every `TenantRolePermission` for the role deleted, then rebuilt; `version += 1` | UPDATE **only if `status` changed** (`signals.py:479-490`). The permission replacement is silent |
| `PATCH roles/<key>/` with `group_ids` | every `TenantRoleGroup` deleted and rebuilt | one PERMISSION_CHANGED per detach and per attach |
| `DELETE roles/<key>/` | Cascades `TenantRolePermission` and `TenantRoleGroup`; `PROTECT` on assignments and change requests | one PERMISSION_CHANGED per detached group; nothing for the template itself |
| `POST role-assignments/` | 1 `TenantUserRoleAssignment` | ROLE_ASSIGNED, actor `assigned_by` (`signals.py:407-423`) |
| `POST …/revoke/` | status, `revoked_at`, `revoked_by`, `reason_note` | ROLE_CHANGED, actor `revoked_by`, with the reason in metadata (`signals.py:425-446`) |
| `PATCH role-assignments/<id>/` changing `branch` or `user` | those fields | **Nothing.** The receiver fires on create and on transition-to-REVOKED only |
| `POST …/replace/` | old row revoked + 1 new row, both inside `@transaction.atomic` | ROLE_CHANGED for the revoke, ROLE_ASSIGNED for the create |
| `POST platform/transfer-super-admin/` | 1 revoke (saved), a bulk `.update()` revoking the incoming holder's other roles, 2 creates, 2 `is_superuser` updates | one ROLE_CHANGED written by the service (`services.py:288-297`), plus the receivers that fire for the two `create()` calls. **The bulk `.update()` fires no signal**, so those revocations are unaudited |

All audit writes go through `record_rbac_audit`, which writes the durable
`RBACAuditLog` row transactionally with the action and mirrors best-effort to
`vs_audit` (`audit.py:20-91`).

`replace` holds `select_for_update(of=("self",))` on the assignment being
replaced and `select_for_update()` on the duplicate probe (`views.py:775`,
`826`), so two concurrent replaces of the same assignment serialise.

## 7. Worked example

Corona Secondary School has two branches, Ikeja and Lekki. Mrs Adeyemi is the
storekeeper at Ikeja; Mr Bello runs Lekki's store. Corona's admin wants each of
them to manage stock at their own site and nowhere else.

**1. Create the role once.**

```http
POST /v1/rbac/tenants/corona/roles/?tenant=corona
{ "name": "Storekeeper",
  "permission_keys": ["procurement.stock.view", "procurement.stock.adjust"] }
```

The serializer flattens the keys, runs `validate_role_permissions` (so
`procurement.stock.adjust`'s dependency on `procurement.stock.view` is
satisfied), calls `platform_only_keys` on both and finds neither is
platform-scoped, allocates `key = "storekeeper"`, creates the template, and
`bulk_create`s the two `TenantRolePermission` rows through
`ScopeGuardedManager`. `version` is 1. An `RBACAuditLog` CREATE row lands.

Note that `branch` was **not** set on the template. It could have been, and it
would have changed nothing about who can do what.

**2. Pin one grant per person.**

```http
POST /v1/rbac/tenants/corona/role-assignments/?tenant=corona
{ "user": 41, "role": 7, "branch": 2 }     # Adeyemi, Storekeeper, Ikeja

POST /v1/rbac/tenants/corona/role-assignments/?tenant=corona
{ "user": 58, "role": 7, "branch": 3 }     # Bello, Storekeeper, Lekki
```

Both land under `uq_active_tenant_user_role_branch`. Had the admin tried to give
Adeyemi the same role at Ikeja twice, the second would collide.

**3. What Adeyemi can now do.** Her request to
`GET /v1/procurement/stock-adjustments/?tenant=corona` reaches
`HasRBACPermission`, which asks `has_permission(user, "procurement.stock.adjust",
tenant=corona)` with no branch named. `_assignment_branch_q(ANY_BRANCH)` is
`branch IS NULL OR branch.status IN IN_SERVICE_STATES`, so her Ikeja grant
counts and the gate opens.

**4. What Adeyemi can now see.** The view calls `branch_visible(request, qs)`.
`_grant_scope` reads her assignments: one row, `branch_id = 2`, status ACTIVE,
no `NULL` present - so the answer is `frozenset({2})`. `BranchScope.q()` renders
`branch_id IN (2) OR branch_id IS NULL`, because the default is inclusive: a
stock item Corona publishes for every branch is shared, not hidden.

**5. Adeyemi covers Lekki for a term.** The admin adds a second assignment,
`{"user": 41, "role": 7, "branch": 3}`. Her scope becomes `frozenset({2, 3})`.
This is the arrangement a single `User.branch` field cannot express, and it is
why the answer is a set (`scoping.py:39-43`).

**6. Lekki is suspended.** `Branch.status` leaves `IN_SERVICE_STATES`.
`_assignment_branch_q` stops counting the Lekki grant, and `_grant_scope` -
which deliberately does *not* filter liveness in SQL - returns
`frozenset({2})`. Adeyemi is back to Ikeja only, with no row edited anywhere.

**7. Adeyemi leaves.**

```http
POST /v1/rbac/tenants/corona/role-assignments/91/revoke/?tenant=corona
{ "reason_note": "Resigned, effective 31 August." }
```

Without `reason_note` this is a 400. With it, both rows go REVOKED, each writing
a ROLE_CHANGED audit row carrying the reason. Her `_rbac_effective_perms` cache
lives only for the length of one request, so her very next call finds nothing.

**8. Retiring the role itself.** `DELETE /v1/rbac/tenants/corona/roles/storekeeper/`
is refused, because `TenantUserRoleAssignment.role` is `PROTECT` and even the
revoked rows count. `core/exceptions.py:130-140` turns that into a clean 409 -
*"This record cannot be deleted because 2 tenant user role assignments still
reference it. Remove or reassign them first."* - which is the right answer and is
verified by execution. The workable move is `PATCH {"status": "INACTIVE"}`, which
does stop the role granting anything.

**9. The corner that bites.** What the admin must not do is
`PATCH {"is_locked": true}`. `is_locked` is writable on the serializer, and both
update and delete refuse a locked role afterwards - so the role becomes
permanently uneditable, and the super-admin escape hatch in the code cannot be
reached for a school tenant (`rbac_code_issues` §9 and §12).

## 8. Gotchas / known limitations

Recorded in full in **`error/rbac/rbac_code_issues.md`**. The items belonging to
this slice:

| # in that file | One line |
|---|---|
| §1 | **Critical, confirmed by execution.** A school admin can mint a role keyed `xvs_super_admin` and, through `vs_user` user creation, hand somebody the platform-wide RBAC bypass |
| §2 | A school admin with `school.roles.create` + `.assign` can grant themselves any `TENANT`-scoped key in the registry, including other modules' |
| §3 | Editing a role's permission set writes no `RBACAuditLog` row - the one screen that changes access most often is the one that leaves no trace |
| §6 | **Confirmed by execution.** `?branch=`, `?user=` and `?target_role=` are fed straight into `filter(…_id=…)`, so a non-numeric value is a 500 |
| §9 | **Confirmed by execution.** A school admin can PATCH `is_locked: true` on their own role and then nobody on the platform can edit or delete it again |
| §10 | `TenantRoleTemplate.branch` is validated, filterable and read by no evaluation code - a "branch role" narrows nothing |
| §12 | CX cannot reach a school's role endpoints at all, so the super-admin branches inside the detail view are dead for school tenants |
| §13 | **Confirmed by execution.** `PATCH` on an assignment can move `branch` or `user` with no audit row |
| §15 | `version` is bumped by three code paths and read by none |
| §16 | `permissions_count` on the role list ignores group-derived keys |
| §18 | The super-admin transfer's bulk `.update()` revokes the incoming holder's roles without firing the audit receiver |

Design choices worth stating as choices:

- **The split unique constraint** (`models.py:737-761`) is a deliberate trade:
  two partial indexes keep both guarantees - at most one active whole-tenant
  grant per person per role, and at most one per branch - where one combined
  index would have silently permitted duplicate whole-tenant grants.
- **Resolving references inside the tenant** rather than resolving globally and
  then comparing is what removes the existence oracle
  (`serializers/tenant.py:44-54`). The fallback tenancy checks in `validate()`
  are unreachable through HTTP and say so, and they raise the *same* message so
  neither route leaks.
- **Revocation is terminal.** Creating a fresh assignment rather than reviving
  one keeps `assigned_at` / `revoked_at` honest as a history.

## 9. Permissions & tenant isolation

- **Ten keys gate this slice.** School side:
  `school.roles.view` (NORMAL), `.create` / `.update` / `.delete` / `.assign`
  (all SENSITIVE), every one seeded to the `school_admin` prebuilt role
  (`seed_school_permissions.py:84-90`). Platform side: `platform.roles.view` /
  `.create` / `.update` (NORMAL), `.assign` / `.manage` / `.delete` (SENSITIVE),
  `.transfer` (CRITICAL) (`seed_platform_permissions.py:38-50`).
- **`platform.roles.transfer` goes to `xvs_super_admin` only** - the platform
  admin is skipped explicitly (`seed_platform_permissions.py:293-295`).
- **All `platform.roles.*` keys are `PermissionScope.PLATFORM`**, so no school
  role can carry one, and the any-of lists are therefore effectively "the school
  key, for a school; the platform key, for CX operating codex".
- **Three independent layers keep a tenant inside its own rows**: the auth layer
  refuses a foreign `?tenant=` (`authentication.py:119-128`); the mixin refuses
  a URL slug that disagrees with it (`views.py:83-88`); every queryset filters
  `tenant=self.get_tenant()` and every reference field resolves inside that
  tenant.
- **`TransferSuperAdminView` is triple-gated**: `IsAuthenticatedAndActive`,
  `IsVisionSuperAdmin` and `HasRBACPermission` with
  `platform.roles.transfer` - and the service then re-derives the caller's
  authority from the codex assignment itself rather than trusting the gate
  (`services.py:234-242`).
- **`UserModel.objects.get(pk=new_id)` in that view is unscoped**
  (`views.py:1334`), which is correct here (any CX staff member is a legitimate
  target) but means a wrong id reveals nothing beyond "user not found".
- **The gap.** `HasRBACPermission` never asks whether the *caller* holds the
  keys they are putting into a role. Any holder of `school.roles.create` can
  build a role carrying every `TENANT`-scoped key in the registry and assign it
  to themselves - see `rbac_code_issues` §2.

## 10. Code map

| File | What lives there |
|---|---|
| `vs_rbac/models.py:558-602` | `TenantRoleTemplate` |
| `vs_rbac/models.py:604-701` | `TenantRolePermission`, `TenantRoleGroup` and their scope guards |
| `vs_rbac/models.py:704-817` | `TenantUserRoleAssignment`, its split constraints and `revoke()` |
| `vs_rbac/views.py:55-93` | The any-of key lists and `TenantScopedRBACMixin` |
| `vs_rbac/views.py:503-612` | Role list / create / detail / update / delete |
| `vs_rbac/views.py:619-681` | Assignment list / create / detail |
| `vs_rbac/views.py:685-745` | Revoke |
| `vs_rbac/views.py:748-867` | Replace |
| `vs_rbac/views.py:1305-1350` | `TransferSuperAdminView` |
| `vs_rbac/serializers/tenant.py:56-139` | Tenant-scoped reference resolution and the not-found wording |
| `vs_rbac/serializers/tenant.py:163-505` | Role serializers, scope rejection, create/update |
| `vs_rbac/serializers/tenant.py:511-757` | Assignment serializer and its five refusals |
| `vs_rbac/services.py:39-111` | Key allocation, `provision_role_from_prebuilt` |
| `vs_rbac/services.py:202-297` | `transfer_super_admin` |
| `vs_rbac/signals.py:397-490` | Assignment and role-template audit receivers |
| `vs_rbac/signals.py:497-538` | Role-group attach / detach receivers |
| `vs_rbac/migrations/0005_branch_scoped_role_assignments.py` | The split unique constraints |
| `core/management/commands/seed_school_permissions.py:84-90` | `school.roles.*` and their prebuilt defaults |
| `core/management/commands/seed_platform_permissions.py:38-50` | `platform.roles.*` |

## 11. Test coverage & gaps

Module baseline: **`Ran 326 tests in 89.035s` - OK** (see
`rbac_permission_registry` §11 for the command).

Covered:

- `tests/test_views.py` - role CRUD, assignment CRUD, revoke including the
  missing-reason 400, the replace flow, 403 on a missing key, 404 across
  tenants, mass-assignment rejection, and that a revoke is reflected in the
  evaluator.
- `tests/test_branch_scoped_grants.py` - a pinned grant confers access, a
  suspended branch withdraws it, two grants at two sites coexist.
- `tests/test_branch_tenant_boundary.py` - the branch/tenant invariant, and the
  guard that fails if any model regrows a `school`-shaped ownership path.
- `tests/test_tenant_isolation.py`, `tests/test_reference_scoping.py` - foreign
  ids are reported exactly like absent ones on every reference field.
- `tests/test_platform_scope_escalation.py` - the scope guard on
  `TenantRolePermission`, `TenantRoleGroup`, `TenantUserRoleAssignment` and the
  `bulk_create` path.
- `tests/test_services.py` - `apply_role_change_request` version bump,
  `transfer_super_admin` happy path and its `ValueError` branches.
- `tests/test_role_list_access.py` - `workflow.template.manage` opens the list
  and nothing else.

Not covered:

- **Nothing tests what happens when a tenant role's key collides with
  `xvs_super_admin`** - the gap `rbac_code_issues` §1 falls through.
- No test asserts an `RBACAuditLog` row for a role permission edit, which is why
  §3 is invisible.
- No test locks a role and then attempts to edit it (§9).
- No test passes a non-numeric `?branch=` / `?user=` (§6).
- No test PATCHes an assignment's `branch` and looks for an audit row (§13).
- `TenantRoleTemplate.branch` has no test asserting it narrows anything -
  correctly, since it does not (§10).
- The replace endpoint's concurrency (`select_for_update`) is untested.
