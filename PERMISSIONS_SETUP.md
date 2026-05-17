# Platform Permissions Setup Checklist

**Purpose:** After a DB reset, follow this checklist top-to-bottom to fully protect the system.
Use `- [x]` to mark done, `- [ ]` to mark pending.

> **Scope:** Schools, Branches, Team, Roles, Permissions registry, Audit, Dashboard.
> Config, Notifications, Imports, and Impersonation are deferred — add them when those features are ready.

> **Legend**
> - 🔴 Super Admin only (restricted)
> - 🟡 Platform Admin + Super Admin

---

## 0. Pre-flight: Verify Seed Commands Ran

The `reset_db --yes` build step should have already run these. Confirm before proceeding:

- [ ] `seed_actions` — 30 action verbs exist in `/rbac/vision/permission-actions/`
- [ ] `seed_prebuilt_role_templates` — 25 prebuilt role templates exist
- [ ] `seed_package` — 4 subscription tiers exist (basic, standard, premium, enterprise)
- [ ] `seed_xvs_modules` — 7 XVS modules exist (students, teachers, parents, attendance, finance, procurement, vendors)
- [x] `create_superuser` — Super admin user + `vision-super-admin` platform role exist

---

## 1. Create Permission Module

Create via `POST /rbac/vision/permission-modules/`.
The 7 school-feature modules (students, finance, etc.) are seeded by `seed_xvs_modules`.
The `platform` module must be created manually:

| Done | Name (slug) | Description |
|------|-------------|-------------|
| - [x] | `platform` | Vision platform administration (schools, team, RBAC, audit) |

---

## 2. Create Permission Resources

Create via `POST /rbac/vision/permission-resources/`.
Use `platform` in the `module` field for all of these.

| Done | Resource Name | Description |
|------|--------------|-------------|
| - [x] | `schools` | Platform-level school record management |
| - [x] | `branches` | Platform-level branch management |
| - [x] | `team` | Vision staff team member management |
| - [x] | `roles` | Platform role template management |
| - [x] | `permissions` | Global permission registry management |
| - [ ] | `audit` | Audit log and compliance management |
| - [ ] | `dashboard` | Admin dashboard and analytics |

---

## 3. Create All Platform Permissions

Create via `POST /rbac/vision/permissions/`.
All keys follow format `module.resource.action`.
**Requires:** Module and resources from steps 1–2 must exist first.
All actions (`view`, `create`, `update`, `manage`, `delete`, `export`, `assign`, `suspend`, `reactivate`, `transfer`) are already in the seeded action vocabulary.

### 3a. School Management (`platform.schools.*`)

| Done | Key | Sensitivity | Restricted | Description |
|------|-----|-------------|------------|-------------|
| - [ ] | `platform.schools.view` | NORMAL | No | List and view school records |
| - [ ] | `platform.schools.create` | NORMAL | No | Onboard a new school |
| - [ ] | `platform.schools.update` | NORMAL | No | Edit school details |
| - [ ] | `platform.schools.manage` | SENSITIVE | No | Reset school config, manage school settings |
| - [ ] | `platform.schools.delete` | CRITICAL | Yes | Hard-delete a school record 🔴 |

### 3b. Branch Management (`platform.branches.*`)

| Done | Key | Sensitivity | Restricted | Description |
|------|-----|-------------|------------|-------------|
| - [ ] | `platform.branches.view` | NORMAL | No | List and view branches |
| - [ ] | `platform.branches.create` | NORMAL | No | Add a branch to a school |
| - [ ] | `platform.branches.update` | NORMAL | No | Edit branch details |
| - [ ] | `platform.branches.manage` | SENSITIVE | No | Transition branch lifecycle (active → suspended → closed) |

### 3c. Team Management (`platform.team.*`)

| Done | Key | Sensitivity | Restricted | Description |
|------|-----|-------------|------------|-------------|
| - [ ] | `platform.team.view` | NORMAL | No | List and view Vision staff members |
| - [ ] | `platform.team.create` | NORMAL | No | Invite a new Vision staff member |
| - [ ] | `platform.team.update` | NORMAL | No | Edit team member profile |
| - [ ] | `platform.team.suspend` | SENSITIVE | No | Suspend a staff account |
| - [ ] | `platform.team.reactivate` | SENSITIVE | No | Reactivate a suspended staff account |
| - [ ] | `platform.team.delete` | CRITICAL | Yes | Permanently delete a staff account 🔴 |

### 3d. Role Management (`platform.roles.*`)

| Done | Key | Sensitivity | Restricted | Description |
|------|-----|-------------|------------|-------------|
| - [x] | `platform.roles.view` | NORMAL | No | List and view platform roles and assignments |
| - [x] | `platform.roles.create` | SENSITIVE | No | Create a new platform role template |
| - [x] | `platform.roles.update` | SENSITIVE | No | Edit a platform role's permissions and metadata |
| - [x] | `platform.roles.assign` | SENSITIVE | No | Assign or revoke platform roles from users |
| - [x] | `platform.roles.delete` | CRITICAL | Yes | Delete a platform role template 🔴 |
| - [x] | `platform.roles.transfer` | CRITICAL | Yes | Transfer the Super Admin role to another user 🔴 |

### 3e. Permission Registry Management (`platform.permissions.*`)

| Done | Key | Sensitivity | Restricted | Description |
|------|-----|-------------|------------|-------------|
| - [x] | `platform.permissions.view` | NORMAL | No | View global permission registry (keys, modules, resources, actions) |
| - [x] | `platform.permissions.create` | SENSITIVE | No | Add new permissions, modules, resources, or actions |
| - [x] | `platform.permissions.update` | SENSITIVE | No | Edit permission metadata |
| - [x] | `platform.permissions.manage` | CRITICAL | Yes | Manage groups, dependencies, and vocabulary — full registry control 🔴 |
| - [x] | `platform.permissions.delete` | CRITICAL | Yes | Delete permissions from the registry 🔴 |

### 3f. Audit & Compliance (`platform.audit.*`)

| Done | Key | Sensitivity | Restricted | Description |
|------|-----|-------------|------------|-------------|
| - [ ] | `platform.audit.view` | SENSITIVE | No | View audit events and entity trails |
| - [ ] | `platform.audit.export` | SENSITIVE | No | Export audit data to file |
| - [ ] | `platform.audit.manage` | SENSITIVE | Yes | Create and manage compliance rules 🔴 |

### 3g. Dashboard (`platform.dashboard.*`)

| Done | Key | Sensitivity | Restricted | Description |
|------|-----|-------------|------------|-------------|
| - [ ] | `platform.dashboard.view` | NORMAL | No | View admin dashboard metrics and statistics |

---

## 4. Create Permission Dependencies

Create via `POST /rbac/vision/permission-dependencies/`.
A dependency means: to grant `permission_key`, `depends_on_key` **must also be granted**.

### School & Branch

| Done | permission_key | depends_on_key | Reason |
|------|----------------|----------------|--------|
| - [ ] | `platform.schools.update` | `platform.schools.view` | Can't edit what you can't see |
| - [ ] | `platform.schools.manage` | `platform.schools.view` | Config reset requires view access |
| - [ ] | `platform.schools.delete` | `platform.schools.view` | Delete requires view access |
| - [ ] | `platform.branches.create` | `platform.schools.view` | Branch creation requires school context |
| - [ ] | `platform.branches.create` | `platform.branches.view` | Must see branches to create one |
| - [ ] | `platform.branches.update` | `platform.branches.view` | Must see branches to edit |
| - [ ] | `platform.branches.manage` | `platform.branches.view` | Lifecycle transitions require view |

### Team

| Done | permission_key | depends_on_key | Reason |
|------|----------------|----------------|--------|
| - [ ] | `platform.team.create` | `platform.team.view` | Must see team to invite |
| - [ ] | `platform.team.update` | `platform.team.view` | Must see team to edit |
| - [ ] | `platform.team.suspend` | `platform.team.view` | Must see member to suspend |
| - [ ] | `platform.team.reactivate` | `platform.team.view` | Must see member to reactivate |
| - [ ] | `platform.team.delete` | `platform.team.view` | Must see member to delete |

### Roles

| Done | permission_key | depends_on_key | Reason |
|------|----------------|----------------|--------|
| - [ ] | `platform.roles.create` | `platform.roles.view` | Must see roles to create |
| - [ ] | `platform.roles.update` | `platform.roles.view` | Must see roles to edit |
| - [ ] | `platform.roles.assign` | `platform.roles.view` | Must see roles to assign |
| - [ ] | `platform.roles.delete` | `platform.roles.view` | Must see roles to delete |
| - [ ] | `platform.roles.transfer` | `platform.roles.view` | Transfer is a role-level action |

### Permission Registry

| Done | permission_key | depends_on_key | Reason |
|------|----------------|----------------|--------|
| - [ ] | `platform.permissions.create` | `platform.permissions.view` | Must see registry to add |
| - [ ] | `platform.permissions.update` | `platform.permissions.view` | Must see registry to edit |
| - [ ] | `platform.permissions.manage` | `platform.permissions.view` | Manage is a superset of view |
| - [ ] | `platform.permissions.delete` | `platform.permissions.view` | Must see to delete |

### Audit

| Done | permission_key | depends_on_key | Reason |
|------|----------------|----------------|--------|
| - [ ] | `platform.audit.export` | `platform.audit.view` | Must view before exporting |
| - [ ] | `platform.audit.manage` | `platform.audit.view` | Compliance rule management requires audit view |

---

## 5. Create New Platform Roles

Create via `POST /rbac/platform/roles/`. Leave `permission_keys` empty for now — wire permissions in Section 6.

| Done | Role Name | Description | is_locked |
|------|-----------|-------------|-----------|
| - [ ] | **XVS Super Admin** | Full unrestricted platform access. Single-seat. Transfer via `/rbac/platform/transfer-super-admin/`. | true |
| - [ ] | **XVS Platform Admin** | Day-to-day operational access. Cannot delete system records or transfer super admin. | false |

> **Note:** The existing `vision-super-admin` role (created by `create_superuser`) is the bootstrap
> role. `XVS Super Admin` is the managed operational role going forward. Assign the super admin user
> to `XVS Super Admin` via `POST /rbac/platform/role-assignments/`.

---

## 6. Wire Permissions to Platform Roles

Update via `PATCH /rbac/platform/roles/{id}/` with `permission_keys: [...]`.

### 6a. XVS Super Admin — Full Access (30 keys)

```json
{
  "permission_keys": [
    "platform.schools.view",
    "platform.schools.create",
    "platform.schools.update",
    "platform.schools.manage",
    "platform.schools.delete",
    "platform.branches.view",
    "platform.branches.create",
    "platform.branches.update",
    "platform.branches.manage",
    "platform.team.view",
    "platform.team.create",
    "platform.team.update",
    "platform.team.suspend",
    "platform.team.reactivate",
    "platform.team.delete",
    "platform.roles.view",
    "platform.roles.create",
    "platform.roles.update",
    "platform.roles.assign",
    "platform.roles.delete",
    "platform.roles.transfer",
    "platform.permissions.view",
    "platform.permissions.create",
    "platform.permissions.update",
    "platform.permissions.manage",
    "platform.permissions.delete",
    "platform.audit.view",
    "platform.audit.export",
    "platform.audit.manage",
    "platform.dashboard.view"
  ]
}
```

- [ ] Wired

### 6b. XVS Platform Admin — Operational Access (23 keys)

```json
{
  "permission_keys": [
    "platform.schools.view",
    "platform.schools.create",
    "platform.schools.update",
    "platform.schools.manage",
    "platform.branches.view",
    "platform.branches.create",
    "platform.branches.update",
    "platform.branches.manage",
    "platform.team.view",
    "platform.team.create",
    "platform.team.update",
    "platform.team.suspend",
    "platform.team.reactivate",
    "platform.roles.view",
    "platform.roles.create",
    "platform.roles.update",
    "platform.roles.assign",
    "platform.permissions.view",
    "platform.permissions.create",
    "platform.permissions.update",
    "platform.audit.view",
    "platform.audit.export",
    "platform.dashboard.view"
  ]
}
```

- [ ] Wired

---

## 7. Assign Roles to Users

Assign via `POST /rbac/platform/role-assignments/`.

| Done | User | Role |
|------|------|------|
| - [ ] | `admin@codexng.com` (bootstrap superuser) | XVS Super Admin |
| - [ ] | Other Vision staff | XVS Platform Admin |

---

## 8. Quick Reference — What Each Role Can Do

| Capability | XVS Super Admin 🔴 | XVS Platform Admin 🟡 |
|------------|--------------------|-----------------------|
| View schools & branches | ✅ | ✅ |
| Create schools & branches | ✅ | ✅ |
| Edit schools & branches | ✅ | ✅ |
| Manage school config / branch lifecycle | ✅ | ✅ |
| **Delete schools** | ✅ | ❌ |
| Manage team (invite, edit, suspend, reactivate) | ✅ | ✅ |
| **Delete team members** | ✅ | ❌ |
| View & assign platform roles | ✅ | ✅ |
| Create & edit platform roles | ✅ | ✅ |
| **Delete platform roles** | ✅ | ❌ |
| **Transfer super admin** | ✅ | ❌ |
| View, create & edit permissions | ✅ | ✅ |
| **Manage vocab / groups / dependencies** | ✅ | ❌ |
| **Delete permissions** | ✅ | ❌ |
| View & export audit logs | ✅ | ✅ |
| **Manage compliance rules** | ✅ | ❌ |
| Dashboard view | ✅ | ✅ |

---

## Deferred (add when features are ready)

| Area | Permissions | Notes |
|------|-------------|-------|
| Config | `platform.config.*` | Feature flags & config key management |
| Notifications | `platform.notifications.*` | Template management and event config |
| Imports | `platform.imports.*` | Data import batch management |
| Impersonation | `platform.impersonation.use` | Admin user impersonation |
| Communication | `communication.*` | Notification delivery and enforcement |

---

## Counts

| Section | Total |
|---------|-------|
| New modules | 1 |
| New resources | 7 |
| Permissions to create | 30 |
| Dependencies to create | 22 |
| Platform roles to create | 2 |
| Role-permission wirings | 2 sets |
