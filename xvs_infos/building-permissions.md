# Building Permissions — Step-by-Step Guide

This doc describes the exact construction order for the XVS RBAC permission registry.
Each entity depends on the entities above it. Never create a child before its parent exists.

---

## Dependency graph

```
PermissionAction          ← no deps, seed first
PermissionModule          ← no deps, seed first

PermissionResource        ← FK → PermissionModule
  └── Permission          ← FK → PermissionModule + PermissionResource + PermissionAction
        └── PermissionDependency  ← FK → Permission (×2, self-referential)

PermissionGroup           ← no deps
  └── GroupPermission     ← FK → PermissionGroup + Permission

PrebuiltRoleTemplate      ← no deps (platform library, Vision-owned)
  └── PrebuiltRolePermission  ← FK → PrebuiltRoleTemplate + Permission

PlatformRoleTemplate      ← no deps (roles for Vision Staff users)
  ├── PlatformRolePermission  ← FK → PlatformRoleTemplate + Permission
  └── PlatformRoleGroup       ← FK → PlatformRoleTemplate + PermissionGroup

School                    ← defined in vs_schools (must exist before school roles)
  └── SchoolRoleTemplate  ← FK → School (roles for school-level users)
        ├── SchoolRolePermission  ← FK → SchoolRoleTemplate + Permission
        └── SchoolRoleGroup      ← FK → SchoolRoleTemplate + PermissionGroup

User                      ← must be active before role assignments
  ├── PlatformUserRoleAssignment  ← FK → User + PlatformRoleTemplate
  └── SchoolUserRoleAssignment    ← FK → User + School + SchoolRoleTemplate
```

---

## Step 1 — PermissionAction

**File:** `global-actions.md`
**Model:** `vs_rbac.models.PermissionAction`
**PK:** `name` (SlugField)

Actions are the verbs — `view`, `create`, `approve`, etc.
They have no foreign key dependencies.

```python
from vs_rbac.models import PermissionAction

PermissionAction.objects.get_or_create(
    name="view",
    defaults={"description": "Read or list records.", "is_active": True},
)
```

**Rules:**
- Seed the full actions list before anything else.
- `name` is the PK and will appear verbatim in permission keys as the suffix.
- Use only lowercase slugs (no spaces, no uppercase).
- Refer to `global-actions.md` for the canonical list and descriptions.

---

## Step 2 — PermissionModule

**Model:** `vs_rbac.models.PermissionModule`
**PK:** `name` (SlugField)

Modules are the top-level namespaces — `finance`, `students`, `platform`, etc.
They have no foreign key dependencies.

```python
from vs_rbac.models import PermissionModule

PermissionModule.objects.get_or_create(
    name="finance",
    defaults={"description": "Financial operations and records.", "is_active": True},
)
```

**Rules:**
- `name` is the PK and appears as the first segment of every permission key.
- One module per domain. Do not create a module per sub-feature — use resources for that.
- Keep names short (1-2 words, lowercase slug). Examples: `finance`, `students`, `platform`.
- Create a module before creating any resource under it.

**Current modules in the system:**
`dashboard`, `students`, `staff`, `academics`, `assessments`, `attendance`,
`finance`, `library`, `health`, `communication`, `admissions`, `hostel`,
`transport`, `canteen`, `events`, `alumni`, `settings`, `reports`, `audit`, `platform`

---

## Step 3 — PermissionResource

**Model:** `vs_rbac.models.PermissionResource`
**PK:** auto (BigInt), unique on `(module, name)`

Resources are the nouns within a module — `invoice` under `finance`, `profile` under `students`.
They depend on `PermissionModule`.

```python
from vs_rbac.models import PermissionModule, PermissionResource

module = PermissionModule.objects.get(name="finance")

PermissionResource.objects.get_or_create(
    module=module,
    name="invoice",
    defaults={"description": "Fee invoices issued to students.", "is_active": True},
)
```

**Rules:**
- `name` is unique per module — two modules can have a resource named `profile`, that is fine.
- `name` appears as the middle segment of the permission key: `module.resource.action`.
- If a module only has one logical resource, name it the same as the module (e.g. `reports.reports.view` is awkward — prefer `reports.school_wide.view`).
- Create the resource before creating any `Permission` that references it.

---

## Step 4 — Permission

**Model:** `vs_rbac.models.Permission`
**PK:** `key` (auto-built as `module.resource.action` by `save()`)

This is the atomic grant unit. Each combination of module + resource + action is one permission.
Depends on `PermissionModule`, `PermissionResource`, and `PermissionAction`.

```python
from vs_rbac.models import Permission, PermissionModule, PermissionResource, PermissionAction

module   = PermissionModule.objects.get(name="finance")
resource = PermissionResource.objects.get(module=module, name="invoice")
action   = PermissionAction.objects.get(name="view")

# key is auto-generated as "finance.invoice.view" by Permission.save()
perm, created = Permission.objects.get_or_create(
    module=module,
    resource=resource,
    action=action,
    defaults={
        "description": "View student fee invoices.",
        "sensitivity_level": Permission.Sensitivity.SENSITIVE,
        "is_restricted": False,
        "is_active": True,
    },
)
```

**Rules:**
- **Never set `key` manually.** `Permission.save()` auto-builds it from `module_id.resource.name.action_id`.
- Use `update_or_create(module=..., resource=..., action=...)` to be idempotent.
- `sensitivity_level`: NORMAL / SENSITIVE / CRITICAL. Use CRITICAL for financial or irreversible operations.
- `is_restricted`: True means this permission requires a formal approval workflow to grant. Use sparingly.
- One permission per `(module, resource, action)` combination — the DB does not enforce a unique constraint
  on this triple, but `save()` will overwrite the `key` to the same value, making duplicates functionally identical.

**Key format examples:**
```
finance.invoice.view
finance.invoice.approve
students.medical.manage
platform.schools.create
platform.audit.logs.export
```

---

## Step 5 — PermissionDependency

**Model:** `vs_rbac.models.PermissionDependency`

Declares that holding permission A implicitly requires permission B.
Used by the permission validation layer to block logically incoherent role configurations.

```python
from vs_rbac.models import Permission, PermissionDependency

approve_perm = Permission.objects.get(key="finance.invoice.approve")
view_perm    = Permission.objects.get(key="finance.invoice.view")

PermissionDependency.objects.get_or_create(
    permission=approve_perm,
    depends_on=view_perm,
)
# reads as: "invoice.approve requires invoice.view"
```

**Rules:**
- Add dependencies **after** all `Permission` records are created.
- Common pattern: every `create`, `update`, `delete`, `approve` depends on the corresponding `view`.
- Do not create circular dependencies — there is no cycle-detection guard in the model.
- Dependencies are informational/validation constraints. The runtime evaluator (`get_effective_permissions`)
  does **not** auto-inject dependency permissions — you must grant them explicitly on the role.

---

## Step 6 — PermissionGroup

**Model:** `vs_rbac.models.PermissionGroup`
**PK:** UUID (auto)

Named, reusable bundles of permissions that can be attached to multiple roles at once.
No foreign key dependencies — can be created any time after `Permission` records exist.

```python
from vs_rbac.models import PermissionGroup

group, _ = PermissionGroup.objects.get_or_create(
    name="Finance Viewer",
    defaults={
        "description": "Read-only access to all finance module resources.",
        "is_system": True,   # True = Vision-seeded, locked from school edit
        "is_active": True,
    },
)
```

**Rules:**
- `is_system=True` marks groups seeded by Vision. School admins cannot modify system groups.
- Groups are reusable across school roles AND platform roles — design them to be broadly applicable.
- Naming convention: `{Module} {Capability}` — e.g. "Finance Viewer", "Student Manager", "Attendance Marker".
- A group with no `GroupPermission` members is valid but useless. Add members next (Step 7).

---

## Step 7 — GroupPermission

**Model:** `vs_rbac.models.GroupPermission`

Places a `Permission` inside a `PermissionGroup`.
Depends on both `PermissionGroup` (Step 6) and `Permission` (Step 4).

```python
from vs_rbac.models import GroupPermission, PermissionGroup, Permission

group = PermissionGroup.objects.get(name="Finance Viewer")
perms = Permission.objects.filter(
    module__name="finance",
    action__name="view",
    is_active=True,
)

for perm in perms:
    GroupPermission.objects.get_or_create(group=group, permission=perm)
```

**Rules:**
- Each `(group, permission)` pair is unique — `get_or_create` is safe to re-run.
- A permission can belong to multiple groups. A group can hold any number of permissions.
- At runtime the evaluator unions all group permissions with direct role permissions, then subtracts explicit denies.

---

## Step 8 — PrebuiltRoleTemplate + PrebuiltRolePermission  *(optional library)*

**Models:** `vs_rbac.models.PrebuiltRoleTemplate`, `PrebuiltRolePermission`

Vision-owned role suggestions that schools can adopt as a starting point.
When a school "uses" a prebuilt, a `SchoolRoleTemplate` is created from it.
Prebuilts are immutable from the school's perspective.

```python
from vs_rbac.models import PrebuiltRoleTemplate, PrebuiltRolePermission, Permission

prebuilt, _ = PrebuiltRoleTemplate.objects.get_or_create(
    key="finance_admin",
    defaults={
        "name": "Finance Administrator",
        "description": "Full access to finance operations for a branch.",
        "scope": "branch",
        "tier": "A",
        "is_active": True,
    },
)

for perm_key in ["finance.invoice.view", "finance.invoice.approve", "finance.fees.manage"]:
    perm = Permission.objects.get(key=perm_key)
    PrebuiltRolePermission.objects.get_or_create(prebuilt_role=prebuilt, permission=perm)
```

**Rules:**
- `key` is a human-readable slug identifier (e.g. `finance_admin`, `class_teacher`).
- `scope` controls where the role makes sense: `institution`, `branch`, `class`, `portal`.
- `tier`: A = Core (every school needs this), B = Module-dependent, C = Optional.
- Prebuilts are a convenience library, not a mandatory step. Skip if you are building roles manually per school.

---

## Step 9 — PlatformRoleTemplate  *(Vision Staff roles)*

**Model:** `vs_rbac.models.PlatformRoleTemplate`
**PK:** SlugField `id`

Roles for Vision Staff (internal CodeX team) — not school users.
No foreign key dependencies.

```python
from vs_rbac.models import PlatformRoleTemplate

role, _ = PlatformRoleTemplate.objects.get_or_create(
    id="vision-super-admin",
    defaults={
        "name": "Vision Super Admin",
        "description": "Unrestricted platform-wide access.",
        "is_active": True,
    },
)
```

**Rules:**
- Only one `vision-super-admin` can exist (enforced in `UserCreateSerializer`).
- Platform roles are independent of schools — they apply globally.
- Assign permissions via `PlatformRolePermission` or groups via `PlatformRoleGroup` (Steps 10–11).

---

## Step 10 — PlatformRolePermission / PlatformRoleGroup

**Models:** `vs_rbac.models.PlatformRolePermission`, `PlatformRoleGroup`

Wire platform roles to specific permissions (direct) or permission groups (inherited).

```python
from vs_rbac.models import PlatformRoleTemplate, PlatformRolePermission, Permission

role = PlatformRoleTemplate.objects.get(id="vision-super-admin")
perm = Permission.objects.get(key="platform.schools.view")

PlatformRolePermission.objects.get_or_create(
    role=role,
    permission=perm,
    defaults={"granted": True},
)
```

```python
from vs_rbac.models import PlatformRoleGroup, PermissionGroup

group = PermissionGroup.objects.get(name="Finance Viewer")
PlatformRoleGroup.objects.get_or_create(role=role, group=group)
```

**Rules:**
- `granted=True` → grant. `granted=False` → explicit deny (overrides all grants from groups).
- Explicit denies win at evaluation time — use them sparingly and document why.
- Prefer groups over direct permission rows where multiple roles share the same permission surface.

---

## Step 11 — SchoolRoleTemplate  *(per-school roles)*

**Model:** `vs_rbac.models.SchoolRoleTemplate`
**PK:** auto-slug from `name`

School-scoped roles. Depends on `School` existing first.
School roles can optionally trace back to a `PrebuiltRoleTemplate` via `prebuilt_from`.

```python
from vs_rbac.models import SchoolRoleTemplate
from vs_schools.models import School

school = School.objects.get(slug="demo-primary")

role, _ = SchoolRoleTemplate.objects.get_or_create(
    school=school,
    name="Finance Administrator",
    defaults={
        "description": "Full finance access for this school.",
        "status": SchoolRoleTemplate.Status.ACTIVE,
        "is_system_role": False,
        "is_locked": False,
    },
)
```

**Rules:**
- `id` is auto-generated as a slug from `name` by `SchoolRoleTemplate.save()` — do not set it.
- `is_system_role=True` means Vision provisioned the role and the school cannot edit it.
- `is_locked=True` blocks edits while a change-request workflow is in progress.
- A school role with `branch` set is branch-scoped; without `branch` it is school-wide.

---

## Step 12 — SchoolRolePermission / SchoolRoleGroup

**Models:** `vs_rbac.models.SchoolRolePermission`, `SchoolRoleGroup`

Wire school roles to permissions or groups. Same pattern as platform (Steps 10–11).

```python
from vs_rbac.models import SchoolRolePermission, SchoolRoleTemplate, Permission

role = SchoolRoleTemplate.objects.get(school=school, name="Finance Administrator")
perm = Permission.objects.get(key="finance.invoice.approve")

SchoolRolePermission.objects.get_or_create(
    role=role,
    permission=perm,
    defaults={"granted": True},
)
```

**Rules:**
- Same `granted` flag semantics as platform — False = explicit deny.
- Use `SchoolRoleGroup` to attach a `PermissionGroup` and inherit all its permissions at runtime.
- The evaluator at `vs_rbac/evaluator.py::get_effective_permissions()` resolves the final set:
  `(direct grants ∪ group grants) − explicit denies`

---

## Step 13 — Role assignments

**Models:** `vs_rbac.models.PlatformUserRoleAssignment`, `SchoolUserRoleAssignment`

Assigns a role to a specific user. Depends on `User` being ACTIVE and the role existing.

```python
from vs_rbac.models import SchoolUserRoleAssignment, SchoolRoleTemplate
from vs_user.models import User

user = User.objects.get(email="admin@demo-primary.xvs")
role = SchoolRoleTemplate.objects.get(school=school, name="Finance Administrator")

SchoolUserRoleAssignment.objects.get_or_create(
    user=user,
    school=school,
    role=role,
    defaults={"assignment_status": SchoolUserRoleAssignment.AssignmentStatus.ACTIVE},
)
```

**Rules:**
- A user can hold multiple roles simultaneously — the evaluator unions all of them.
- `assignment_status` must be `ACTIVE` for the evaluator to include it.
- Do not assign roles to `PENDING` or `SUSPENDED` users — wait until the account is `ACTIVE`.

---

## Quick-reference seeding checklist

```
[ ] 1. PermissionAction records          (global-actions.md)
[ ] 2. PermissionModule records          (one per domain)
[ ] 3. PermissionResource records        (one per noun per module)
[ ] 4. Permission records                (key auto-generated)
[ ] 5. PermissionDependency records      (A requires B rules)
[ ] 6. PermissionGroup records           (named bundles)
[ ] 7. GroupPermission records           (fill the bundles)
[ ] 8. PrebuiltRoleTemplate records      (Vision library, optional)
[ ] 9. PlatformRoleTemplate records      (Vision Staff roles)
[ ]10. PlatformRolePermission / Group    (wire platform roles)
[ ]11. SchoolRoleTemplate records        (per school, after school exists)
[ ]12. SchoolRolePermission / Group      (wire school roles)
[ ]13. Role assignments                  (after users are ACTIVE)
```

---

## Common mistakes

| Mistake | Consequence | Fix |
|---|---|---|
| Creating `Permission` before its `PermissionResource` | IntegrityError | Always seed resources first |
| Setting `Permission.key` manually | `save()` will overwrite it | Never set key; let `save()` build it |
| Seeding dependencies before all permissions exist | FK violation | Run deps in a second pass after all permissions are created |
| Assigning a role to a PENDING user | Assignment exists but evaluator ignores it | Wait for activation |
| Using `granted=False` on a direct `SchoolRolePermission` without documentation | Silent mystery deny for future maintainers | Always add a comment or reason field |
| Creating platform permissions (`platform.*`) as `SchoolRolePermission` | Functionally wrong — school roles cannot hold platform perms | Platform perms go on `PlatformRolePermission` only |
