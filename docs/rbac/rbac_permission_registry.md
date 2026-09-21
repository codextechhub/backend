# Backend owned permission registry

The permission registry is the backend source of truth for permission modules,
resources, actions, dependencies, reusable permission groups, and prebuilt role
templates. The frontend can read these definitions and use them when composing a
custom role, but it cannot create, edit, or delete them.

The database models remain because grants, denies, role assignments, dependency
validation, and runtime authorization all reference the stored definitions. The
ownership change removes general API mutation. It does not replace the relational
registry with frontend constants or free-form strings.

## Canonical backend sources

Run the master command when a development or deployment database needs its full
registry reconciled:

```bash
python manage.py seed_all_permissions
```

The command calls the definition sources in dependency order. The main places to
review are:

| Definition | Backend source |
|---|---|
| Action vocabulary | `apps/core/management/commands/seed_actions.py` |
| Platform permissions | `apps/core/management/commands/seed_platform_permissions.py` |
| Tenant and academics permissions | `apps/core/management/commands/seed_school_permissions.py` |
| Finance permissions | `apps/vs_finance/management/commands/seed_finance_permissions.py` |
| Procurement permissions | `apps/vs_procurement/management/commands/seed_procurement_permissions.py` |
| Payments permissions | `apps/vs_payments/management/commands/seed_payments_permissions.py` |
| Workflow permissions | `apps/vs_workflow/management/commands/seed_workflow_permissions.py` |
| Module-specific permissions | Each engine app's `management/commands/seed_*_permissions.py` |
| Default role library | `apps/core/management/commands/seed_prebuilt_role_templates.py` |
| Default permission groups | `apps/core/management/commands/seed_school_permission_groups.py` |

The engines remain domain-neutral. Product-specific permission definitions belong
with the product app that owns them, while shared engine permissions remain in the
engine or core seeders.

## Registry overview command

Use this command to inspect the complete hierarchy without reading database rows
one by one:

```bash
python manage.py show_access_registry
```

Useful variants:

```bash
python manage.py show_access_registry --module finance
python manage.py show_access_registry --json
python manage.py show_access_registry --include-inactive
```

The output is ordered by module, then resource, then action. It also lists the
default permission groups and prebuilt role templates. Each permission shows its
human-readable label alongside its stable key, scope, sensitivity, and restricted
state.

## Stable permission shape

Every permission keeps the same three-part identity:

```text
module.resource.action
```

For example, `finance.invoice.view` belongs to the Finance module, the Invoice
resource, and the View action. `Permission.save()` composes the key from the three
foreign keys, so backend code should declare those parts rather than hand-writing a
second identity.

The stored description is the preferred human-readable label. If a definition has
no description, the API derives a readable fallback from the action and resource,
such as `View invoice`. Module and resource rows also carry labels, with a title
case fallback for older definitions.

## Read-only API

All global definition routes require `platform.permissions.view`. They accept GET
and reject POST, PUT, PATCH, and DELETE with HTTP 405.

| Route | Purpose |
|---|---|
| `GET /v1/rbac/vision/permission-modules/` | List modules |
| `GET /v1/rbac/vision/permission-modules/<name>/` | Read one module |
| `GET /v1/rbac/vision/permission-resources/` | List resources |
| `GET /v1/rbac/vision/permission-resources/<id>/` | Read one resource |
| `GET /v1/rbac/vision/permission-actions/` | List actions |
| `GET /v1/rbac/vision/permission-actions/<name>/` | Read one action |
| `GET /v1/rbac/vision/permissions/` | List permissions with readable labels |
| `GET /v1/rbac/vision/permissions/<key>/` | Read one permission and its relationships |
| `GET /v1/rbac/vision/permission-dependencies/` | List dependency edges with readable labels |
| `GET /v1/rbac/vision/permission-dependencies/<id>/` | Read one dependency edge |
| `GET /v1/rbac/vision/permission-groups/` | List backend-defined groups |
| `GET /v1/rbac/vision/permission-groups/<id>/` | Read one group and its permissions |

Custom tenant roles remain editable through the tenant role endpoints. A user can
create a custom role, choose from readable backend-owned permissions and groups,
and manage personal grants or denies through the existing controlled flows.

## Adding or changing a definition

1. Add the permission to the seeder owned by its module.
2. Supply the module, resource, action, description, scope, sensitivity, restricted
   state, and active state explicitly.
3. Add dependencies in backend code where the module owns them.
4. Add or update a default group only in the default group seeder.
5. Add or update a prebuilt role only in the prebuilt role seeder.
6. Run the focused seeder test, then the full `vs_rbac` test app.
7. Run `show_access_registry --module <module>` and review the ordered output.

Do not add a frontend form or RTK mutation for a global definition. A change to a
stable permission identity needs a deliberate backend data transition because role
grants, groups, dependencies, overrides, defaults, and pending changes may refer to
the old key.

## Retired registry write permissions

The platform seeder registers only `platform.permissions.view` for the registry.
It no longer creates these obsolete keys:

- `platform.permissions.create`
- `platform.permissions.update`
- `platform.permissions.manage`
- `platform.permissions.delete`

When the seeder finds one of these legacy rows in an existing database, it marks
the row inactive. This preserves historical relationships and avoids cascading
deletes while ensuring the active catalogue describes the supported read-only API.

## Runtime invariants

- `Permission.scope` controls whether a tenant role may hold a key. A blank scope
  fails closed for a non-platform tenant.
- A permission group grants nothing until a role attaches it.
- Group scope must agree with the permissions it contains.
- Dependency validation checks the final permission set, including permissions
  supplied through attached groups.
- Prebuilt roles are backend-owned blueprints. Provisioning copies their defaults
  into a tenant role; the template itself is not a role assignment.
- `is_active` is catalogue metadata. Removing effective access requires changing
  the relevant grants or definitions through an explicit backend transition.

## Main implementation files

| File | Responsibility |
|---|---|
| `apps/vs_rbac/models.py` | Registry, group, role, grant, deny, and dependency models |
| `apps/vs_rbac/serializers/registry.py` | Read response shapes and readable labels |
| `apps/vs_rbac/views.py` | Read-only definition endpoints and mutable custom role endpoints |
| `apps/vs_rbac/validators.py` | Dependency and final permission-set validation |
| `apps/vs_rbac/management/commands/show_access_registry.py` | Ordered human and JSON registry overview |
| `apps/core/management/commands/seed_all_permissions.py` | Master registry reconciliation |
| `apps/core/management/commands/seed_platform_permissions.py` | Platform definitions and registry-read permission |
