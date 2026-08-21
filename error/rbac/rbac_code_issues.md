# rbac_code_issues

Everything wrong with `vs_rbac`, in one place, ordered by how much it costs.
Each item states the defect, the evidence, what actually happens to a user, and
the fix. The four slice reports (`rbac_permission_registry`,
`rbac_roles_assignments`, `rbac_change_requests_overrides`,
`rbac_evaluation_scoping`) point here rather than repeating it.

Baseline: the `vs_rbac` suite is **326 tests, all green**
(`Ran 326 tests in 89.035s` - OK, via
`cd apps && DB_NAME=cx_rbacslice ../cx/Scripts/python.exe manage.py test
vs_rbac --settings=apps.settings.local --noinput`). The single traceback in that
run is `test_audit`'s deliberate `RuntimeError: boom`, which proves the central
audit mirror's failure is swallowed. Every item below is therefore something
326 tests do not currently catch.

The nine items marked **confirmed by execution** (§1, §2, §4, §6, §7, §9, §13,
§17, §21) were reproduced against a real PostgreSQL test database in a throwaway
test module that was deleted afterwards. Everything else is traced to file and
line.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in
the code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | Any school admin can mint the Vision Super Admin bypass and hand it to an account they control | **Critical** |
| 2 | Approval routing nominates people the permission gate then refuses | **High** |
| 3 | Editing a role's permissions writes no audit row - the commonest access change is the one that leaves no trace | **High** |
| 4 | A permission created through the registry API can never be granted to any school, and no endpoint can repair it | **High** |
| 5 | Editing a permission's module, resource or action rewrites the primary key that every grant points at | **High** |
| 6 | Four list filters are fed straight into `filter(…_id=…)`, so a non-numeric value is a 500 | **High** |
| 7 | `is_active` on a permission, group, module, resource or action revokes nothing, while the audit row announces a cascade | **High** |
| 8 | Deleting one permission group silently strips it from every tenant on the platform | **Medium** |
| 9 | A school admin can permanently freeze one of their own roles, and nobody on the platform can unfreeze it | **Medium** |
| 10 | `TenantRoleTemplate.branch` is validated, filterable and read by no evaluation code | **Medium** |
| 11 | Nothing checks the dependency graph for cycles, and one cycle bricks every role edit touching those keys | **Medium** |
| 12 | CX cannot reach a school's role endpoints at all, so the super-admin branches inside them are dead code | **Medium** |
| 13 | Moving an assignment's branch or user through PATCH writes no audit row | **Medium** |
| 14 | Group and change-request detail responses are N+1 on `resource` | **Medium** |
| 15 | `version` is bumped by three code paths, described as a cache invalidator, and read by none | **Medium** |
| 16 | `permissions_count` on the role list ignores every key that arrives through a group | **Medium** |
| 17 | Renaming a permission module or action creates a duplicate row instead of renaming | **Medium** |
| 18 | The super-admin transfer revokes the incoming holder's other roles with a bulk update, so those revocations are unaudited | **Medium** |
| 19 | A permission group's `scope` is on neither serializer, so CX cannot see or set the field the security model rests on | **Medium** |
| 20 | A fixable dependency mistake kills a change request permanently and reports it as a 500 | **Medium** |
| 21 | Approving a change request deletes the role's explicit deny rows and does not mention them in the diff | **Medium** |
| 22 | A change request may queue a platform-scoped key; it fails only at apply time, as a terminal 500 | **Medium** |
| 23 | The "approval queue" is the same query, with the same permission key, as the plain list | **Low** |
| 24 | `impact_summary` is on the model, the serializer and the API contract, and is written by nothing | **Low** |
| 25 | The APPROVED transition has no `post_save` audit branch | **Low** |
| 26 | The override uniqueness constraint omits `tenant` | **Low** |
| 27 | `rbac_group_permission` is documented, unused, and would raise if any view used it | **Low** |
| 28 | `HasAnyModuleAccess` loads the caller's entire key set to answer a prefix question | **Low** |
| 29 | `RBACAuditLog` has no tenant column, an almost-always-empty `school_id`, and no reader anywhere | **Low** |
| 30 | `RBACAuditLog` immutability is Python-only | **Low** |
| 31 | The FLS permission cache is not keyed by tenant | **Low** |
| 32 | `_stripped_fields` tells an unauthorised caller which sensitive fields exist | **Low** |
| 33 | Smaller defects and dead code | **Low** |

---

## 1. Any school admin can mint the Vision Super Admin bypass

**Critical. Confirmed by execution.**

### The defect

`is_vision_super_admin` decides who bypasses the entire RBAC layer, and it never
asks what kind of tenant the role lives in:

```python
# permissions.py:33-40
from .models import TenantUserRoleAssignment
result = TenantUserRoleAssignment.objects.filter(
    user=user,
    tenant=getattr(user, "tenant", None),
    role__key="xvs_super_admin",
    role__tenant=getattr(user, "tenant", None),
    assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
).exists()
```

The role's tenant is the *user's own* tenant, whatever that tenant is. There is
no `kind=PLATFORM` filter and no `slug="codex"` filter. The only thing standing
between a school and that key is the assumption that no school role can be
keyed `xvs_super_admin`.

That assumption is false. A tenant role's key is derived from its name by
`slugify`, once, at creation (`serializers/tenant.py:145-157`), and Django's
`slugify` preserves underscores because `\w` includes them:

```
>>> slugify("xvs_super_admin")
'xvs_super_admin'
```

Uniqueness on `key` is per tenant (`models.py:587`), so the name is free inside
every school on the platform.

The assignment endpoint does refuse the key
(`serializers/tenant.py:688-696`, `views.py:791-816`), but it is not the only
writer. `vs_user`'s user-creation service writes an assignment directly:

```python
# vs_user/services/user.py:99-106
if role_instance is not None:
    TenantUserRoleAssignment.objects.create(
        tenant=user.tenant,
        branch=user.branch if role_instance.branch_id else None,
        user=user, role=role_instance, assigned_by=requesting_user,
    )
```

and the only `xvs_super_admin` guard on that path is gated on
`creating_platform_staff`, so it never runs for a school tenant:

```python
# vs_user/serializers.py:378-387
if creating_platform_staff and role_key == 'xvs_super_admin':
    ... "A Vision Super Admin already exists. Only one is allowed."
```

### What actually happens

Corona Secondary School's admin, Mrs Balogun, holds the seeded `school_admin`
defaults, which include `school.roles.create`, `school.roles.update` and
`school.roles.assign` (`seed_school_permissions.py:84-90`). In four calls:

1. She creates a role carrying `platform.team.create` - a key whose scope is
   `TENANT`, so the guard passes - and assigns it to herself.
2. She creates a second role named exactly `xvs_super_admin`.
3. She creates a user, `it-support@corona.example`, with `role: "xvs_super_admin"`.
4. She activates that account from the invitation email she controls.

That account now returns `True` from `is_vision_super_admin`, and
`HasRBACPermission.has_permission` returns `True` for **every** key on **every**
view in the repo before a single grant is read (`permissions.py:300-302`) -
including the `PermissionScope.PLATFORM` keys the scope column exists to keep
away from schools. `FieldSecurityMixin` also stops masking anything
(`fls.py:79-82`), and `IsVisionSuperAdmin` opens
(`permissions.py:248-255`).

What that reaches: the global permission registry with full write access
(create, edit and delete permission keys and groups used by every other tenant),
the platform dashboard, the `vs_audit` Event Explorer, `vs_health`'s
cross-tenant console, the schools roster, and every other surface whose queryset
is not tenant-filtered. `TenantJWTAuthentication` still refuses a foreign
`?tenant=` slug, so she cannot assert another school's context - but none of the
platform surfaces above need her to.

Confirmed by execution against a real database:

```
PROBE1 status: 201
PROBE1 key: xvs_super_admin              # minted through POST /roles/ as a school admin
PROBE2 is_vision_super_admin: True
PROBE2 tenant kind: SCHOOL
PROBE2 HasRBACPermission(platform key): True
```

The one thing that does hold is `transfer_super_admin`, which re-derives the
caller's authority from the codex assignment rather than trusting the gate
(`services.py:234-242`), so the counterfeit cannot hand the *real* Super Admin
role to anyone.

### The fix

Pin the check to the platform tenant. The role is a platform singleton, so this
is what it always meant:

```python
result = TenantUserRoleAssignment.objects.filter(
    user=user,
    role__key=SUPER_ADMIN_ROLE_KEY,
    role__tenant__kind=Tenant.Kind.PLATFORM,
    role__tenant=user.tenant,
    tenant=user.tenant,
    assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
).exists()
```

That is the class fix - it closes the hole however the counterfeit role was
created. Two supporting changes belong in the same batch:

- **Reserve the key namespace.** `_unique_tenant_role_key` should refuse to
  produce a key in `{SUPER_ADMIN_ROLE_KEY, PLATFORM_ADMIN_ROLE_KEY}` for a
  non-platform tenant, suffixing instead - so the counterfeit cannot be built at
  all.
- **Move the `xvs_super_admin` guard in `vs_user/serializers.py:378` out from
  under `creating_platform_staff`**, so every user-creation path refuses the key
  the same way the assignment endpoint does.

Add a test asserting `is_vision_super_admin` is `False` for a school-tenant user
holding a role keyed `xvs_super_admin`. It is one line and nothing in 326 tests
currently makes that assertion.

---

## 2. Approval routing nominates people the gate then refuses

**High. Confirmed by execution.**

### The defect

`resolve_users_with_permission` is what `vs_workflow` asks "who may approve
this?". Its docstring is explicit about the guarantee:

> Routing shares `_assignment_branch_q` with the permission gate so a person
> this function nominates as an approver cannot be someone `has_permission`
> would then refuse. - `evaluator.py:250-253`

It shares the *branch* condition. It does not share the *role status* condition.
The evaluator filters on it:

```python
# evaluator.py:157-166
TenantUserRoleAssignment.objects.filter(
    tenant=tenant, user=user,
    assignment_status=ACTIVE,
    role__status="ACTIVE",              # <-- here
).filter(_assignment_branch_q(branch))
```

Routing does not:

```python
# evaluator.py:287-291
assignments = TenantUserRoleAssignment.objects.filter(
    tenant=tenant,
    role_id__in=role_ids,
    assignment_status=ACTIVE,           # <-- and nothing about role__status
).filter(_assignment_branch_q(branch))
```

`role_ids` is built from `TenantRolePermission` and `TenantRoleGroup` rows
(`evaluator.py:276-285`), neither of which knows the role's status either.

### What actually happens

Corona retires its "Deputy Bursar" role by setting `status = INACTIVE` - the
documented way to stand a role down, since deleting it is a 409 while any
assignment exists. Mr Eze held it.

Every invoice raised after that is routed to Mr Eze for approval: the workflow
engine calls `resolve_users_with_permission(corona, None,
"finance.invoice.approve")`, gets him back, creates the approval task and
notifies him. He opens it and gets a 403, because `has_permission` reads
`role__status` and answers no. The invoice sits in his queue, unapprovable, and
nobody else is nominated because the routing thinks it has an approver.

Confirmed by execution:

```
PROBE6 has_permission: False
PROBE6 resolve_users_with_permission: ['ap@corona.test']
```

### The fix

Add the status filter in both places routing derives from - the role id lookup
and the assignment query:

```python
live_roles = TenantRoleTemplate.objects.filter(
    pk__in=role_ids, status=TenantRoleTemplate.Status.ACTIVE,
).values_list("pk", flat=True)
assignments = TenantUserRoleAssignment.objects.filter(
    tenant=tenant, role_id__in=live_roles, assignment_status=ACTIVE,
).filter(_assignment_branch_q(branch))
```

Better still, factor the "live assignment" query out of `_role_permission_keys`
and `resolve_users_with_permission` into one helper the way
`_assignment_branch_q` already is, so the two cannot drift again. That is the
class fix; the same drift is what `scoping._grant_scope` avoided by copying the
filter (`scoping.py:83-88`), and a third copy is a third chance to get it wrong.

---

## 3. Editing a role's permissions writes no audit row

**High.**

### The defect

`signals.py` imports and wires receivers for `Permission`,
`PermissionDependency`, `GroupPermission`, `PermissionModule`,
`PermissionResource`, `PermissionAction`, `TenantUserRoleAssignment`,
`TenantRoleTemplate`, `TenantRoleGroup` and `TenantRoleChangeRequest`
(`signals.py:4-15`).

`TenantRolePermission` is not in that list, and there is no receiver for it
anywhere in the file.

The role detail serializer replaces the whole set on every write:

```python
# serializers/tenant.py:483-493
if permission_keys is not None:
    TenantRolePermission.objects.filter(role=instance).delete()
    perms = Permission.objects.filter(key__in=permission_keys)
    TenantRolePermission.objects.bulk_create([...])
```

`bulk_create` fires no `post_save`, and `queryset.delete()` fires no
`post_delete` for the individual rows. Neither would matter if a receiver
existed, because there is none.

The `TenantRoleTemplate` receiver does fire on that save, but it only writes a
row when `status` changed:

```python
# signals.py:479-490
old_status = getattr(instance, "_pre_save_status", None)
if old_status and old_status != instance.status:
    emit_audit_event(...)
```

So a PATCH that swaps a role's entire permission set writes **nothing** to
`RBACAuditLog` and **nothing** to `vs_audit`.

### What actually happens

Corona's admin opens the Bursar role, ticks `finance.payment.approve` and
`finance.bank.reconcile`, and saves. Twelve bursars gain the ability to approve
payments and reconcile the bank. The durable RBAC audit trail - the table whose
own docstring calls role changes "security system-of-record events"
(`models.py:1093-1097`) - records that nothing happened.

The only role-permission change that *is* audited is the one that went through
the optional approval queue (`services.py:183-198`), which nothing forces anyone
to use. The screen a tenant actually uses is the unaudited one.

### The fix

Add a receiver pair for `TenantRolePermission`, and change the serializer to
write through a path that fires them. Since the update is a wholesale replace,
the cleanest shape is to audit the replace itself rather than each row - compute
`before_keys` and `after_keys` in `TenantRoleTemplateDetailSerializer.update`
and emit one PERMISSION_CHANGED row with the same
`before_data` / `diff_data` shape `apply_role_change_request` already uses
(`services.py:191-192`), so the two paths produce comparable rows.

Do the same for `TenantRoleGroup` replacement in that method: the attach and
detach receivers do exist (`signals.py:497-538`), but `bulk_create` bypasses the
attach one, so a group added through the role editor is also silent.

---

## 4. A permission created through the API can never be granted

**High. Confirmed by execution.**

### The defect

`Permission.scope` has no default, deliberately - an unclassified key must fail
closed (`models.py:58-62`, `272-281`).

`PermissionSerializer` does not expose the field:

```python
# serializers/registry.py:146-163
fields = [
    "key", "module", "module_key", "resource", "resource_key",
    "action", "action_key", "description", "sensitivity_level",
    "is_restricted", "is_active", "created_at", "updated_at",
]
```

No `scope`. It is not writable, not readable, and not defaulted. So
`POST /v1/rbac/vision/permissions/` produces a row with `scope = ""`.

`platform_only_keys` counts anything that is not exactly `TENANT`
(`models.py:78-82`), so the new key is refused for every non-platform tenant by
`assert_tenant_may_hold` - on `TenantRolePermission.save()`, on
`TenantRoleGroup`, on `TenantUserRoleAssignment`, on `UserPermissionOverride`
and in the serializer's pre-check (`serializers/tenant.py:347-377`).

The same omission was found and fixed for `PermissionGroup` - its create method
sets `validated_data.setdefault("scope", PermissionScope.TENANT)` with a
five-line comment explaining that without it "the bundle was therefore unusable
by the only people who can build one" (`serializers/registry.py:365-376`). The
identical problem on `Permission` was not fixed.

### What actually happens

CX adds `school.fees.waive` through the registry screen, because that is what
the screen is for. Corona's admin then tries to put it on the Bursar role and
gets:

> Permission(s) school.fees.waive are platform-scoped and cannot be granted
> inside a tenant. If a key is missing a scope, classify it in the seeder that
> registers it.

There is no seeder - the key was created through the API. And there is no
endpoint that can set `scope` either, so the message names a repair the caller
cannot perform. The key is inert until somebody edits the database directly or
ships a code change.

Confirmed by execution. Creating `finance.invoice.approve` through
`POST /v1/rbac/vision/permissions/` returns 201, and:

```
PROBE3 scope: ''
PROBE3 grant to school role: REFUSED -> {'permission': ['Permission(s)
  finance.invoice.approve are platform-scoped and cannot be granted inside a
  tenant. If a key is missing a scope, classify it in the seeder that
  registers it.']}
PROBE3 role create with that key: 400 {'permission_keys': ["'finance.invoice.approve'
  is platform-scoped and cannot be granted to a tenant role."]}
```

### The fix

Mirror the group fix. Either default it in `PermissionSerializer.create`:

```python
validated_data.setdefault("scope", PermissionScope.TENANT)
```

or - better, since this is the field the whole security model rests on - put
`scope` on the serializer as a **required** write field with the choices
exposed, so CX has to decide rather than inherit a default. Expose it read-only
on the detail serializer either way, so the registry screen can show what a key
is classified as.

Add a test that creates a permission through the API and grants it to a school
role. Nothing in 326 tests does.

---

## 5. Editing a permission rewrites the primary key that grants point at

**High.**

### The defect

`Permission.key` is the primary key, and `save()` recomputes it from the three
FKs on every save without `update_fields` (`models.py:293-296`). Because Django
treats a changed pk as a new row, `PermissionDetailView.update` works around it
with a raw update first:

```python
# views.py:335-345
new_key = f"{new_module.pk}.{new_resource.name}.{new_action.pk}"
if new_key != instance.key:
    Permission.objects.filter(key=instance.key).update(key=new_key)
    instance.key = new_key
self.perform_update(serializer)
```

Nothing updates the rows that reference it. Nine tables carry the dotted key as
a foreign key with `to_field="key"`: `PermissionDependency` (twice),
`GroupPermission`, `PrebuiltRolePermission`, `TenantRolePermission`,
`UserPermissionOverride`, `TenantRoleChangeDeltaItem`. Django does not emit
`ON UPDATE CASCADE`, so PostgreSQL refuses the update with a foreign key
violation the moment any of them points at the row - and the view swallows it:

```python
# views.py:346-351
except Exception as exc:
    return error_response(message="Update failed.", error={"error": str(exc)},
                          status=status.HTTP_500_INTERNAL_SERVER_ERROR)
```

For a key nothing references yet, the rename succeeds - and every hardcoded
`rbac_permission = "..."` string in the repo now names a key that no longer
exists, so the view it gates becomes unreachable for everyone except the Vision
super admin.

### What actually happens

CX notices `school.teachers.manage` should have been filed under a `staff`
resource and edits it. If any school has already granted it, the request 500s
with a PostgreSQL error string in the body. If none has, the rename goes
through, `TeacherViewSet`'s `rbac_permission = "school.teachers.manage"` matches
nothing, and every school admin loses teacher management with no error anyone
can connect to the edit.

### The fix

The registry key is derived data, and derived data should not be editable. Make
`module`, `resource` and `action` read-only after creation on
`PermissionSerializer`, and delete the manual key-rewrite block in
`PermissionDetailView.update` - a mis-filed key is replaced by creating the
right one and deactivating the wrong one, which is what `is_active` is for
(once it does something - see §7).

If renaming genuinely has to be supported, it needs a service that rewrites
every referencing row inside one transaction, plus a scan of the repo's
hardcoded key strings. That is a much larger change than the screen implies, and
the read-only route is the honest one.

---

## 6. Four list filters turn a bad query parameter into a 500

**High. Confirmed by execution.**

### The defect

Four filters put a raw query string into an integer column lookup with no
validation:

```python
# views.py:540-541   role list
if branch_id := qp.get("branch"):
    qs = qs.filter(branch_id=branch_id)

# views.py:645-646   assignment list
if user_id := qp.get("user"):
    qs = qs.filter(user_id=user_id)

# views.py:903-904   change-request list
if role_id := qp.get("target_role"):
    qs = qs.filter(target_role_id=role_id)

# views.py:934-935   approval queue
if target_role := qp.get("target_role"):
    qs = qs.filter(target_role_id=target_role)
```

`filter(branch_id="abc")` raises `ValueError` before SQL is generated.
`core/exceptions.py` handles `ValidationError`, `ProtectedError`,
`RestrictedError` and `IntegrityError`, but not `ValueError`, so it falls
through to an unhandled 500.

The same view file gets this right two lines away, for `?role=`:

```python
# views.py:647-651
if role := qp.get("role"):
    if role.isdigit():
        qs = qs.filter(role_id=role)
    else:
        qs = qs.filter(role__key=role)
```

and the serializers' `TenantScopedRelatedField` guards the same class of input
properly, including the bigint overflow case
(`serializers/tenant.py:134-139`).

### What actually happens

A frontend that sends `?branch=all` for "no filter" - a completely ordinary
convention, and the one `vs_health`'s tenant filter uses - 500s the role list on
every load. So does a stale bookmark, a typo, or a `?branch=` value carried over
from a screen that used slugs.

Confirmed by execution: `GET /v1/rbac/tenants/corona/roles/?tenant=corona&branch=abc`
returns **500**.

### The fix

One helper, applied at all four sites, matching the `?role=` precedent and the
serializer's `_MAX_BIGINT` guard:

```python
def _int_or_none(raw):
    raw = str(raw).strip()
    return int(raw) if raw.isdigit() and int(raw) <= 9_223_372_036_854_775_807 else None
```

Ignore the filter (or 400 it) when it comes back `None`. Sweep the other engine
apps for the same shape while the helper is being written - this is a class of
defect, not four instances, and `error/config/config_code_issues.md` records the
same failure in `vs_config`.

---

## 7. `is_active` revokes nothing, and the audit row says otherwise

**High. Confirmed by execution.**

### The defect

Five models in this module carry an `is_active` flag that reads as a kill
switch: `Permission`, `PermissionGroup`, `PermissionModule`,
`PermissionResource`, `PermissionAction`.

The evaluator reads none of them:

```python
# evaluator.py:169-177
for key, is_granted in TenantRolePermission.objects.filter(
    role_id__in=role_ids, **_holdable_filter(tenant),
).values_list("permission_id", "granted"): ...

group_ids = TenantRoleGroup.objects.filter(role_id__in=role_ids)...
granted.update(_group_permission_keys(group_ids, tenant=tenant))
```

`_holdable_filter` adds `permission__scope`, and nothing else. `GroupPermission`
is filtered by `group_id__in` and nothing else (`evaluator.py:100-106`).
`resolve_users_with_permission` reads no `is_active` either.

Meanwhile the audit receivers state the opposite, at CRITICAL severity:

```python
# signals.py:230
summary=f"Permission module '{instance.name}' {verb} - all permissions under "
        f"this module are affected"
# signals.py:250
summary=f"Permission module '{instance.name}' deleted - all associated "
        f"permissions and resources are cascade-removed"
```

The second sentence is wrong twice over: `Permission.module` is
`on_delete=PROTECT` (`models.py:246`), so the delete is refused with a 409 while
any permission exists - it does not cascade.

The only real off-switch in the module is `TenantRoleTemplate.status`, which the
evaluator does read (`evaluator.py:162`).

### What actually happens

CX discovers `finance.bank.reconcile` was granted too widely and deactivates the
key from the registry screen. The audit trail records a WARNING saying it was
deactivated. Nothing changes: every role that holds it still holds it, every
holder can still reconcile, and the list screen shows the key greyed out. The
incident is closed on the strength of an audit row describing an action the code
never performed.

Confirmed by execution:

```
PROBE7 deactivated permission still effective: True
PROBE7 deactivated group still grants: True
```

### The fix

Decide what the flag means and make the code say it. The honest reading is
"hidden from the registry pickers, still honoured for existing grants" - in
which case the audit summaries must stop claiming a cascade, and the field needs
a docstring saying so.

If it is meant to revoke, the evaluator is the single choke point and it is two
filters:

```python
def _holdable_filter(tenant) -> dict:
    base = {"permission__is_active": True}
    if tenant_is_platform(tenant):
        return base
    return {**base, "permission__scope": PermissionScope.TENANT}

# and in _group_permission_keys:
qs = GroupPermission.objects.filter(group_id__in=group_ids, group__is_active=True)
```

with the module / resource / action flags folded in the same way if they are
meant to cascade. Either way, correct the two summaries at `signals.py:230` and
`signals.py:250`, and add the tests that would have caught this - deactivate,
then assert the effective set.

---

## 8. Deleting one permission group strips it from every tenant

**Medium.**

### The defect

`DELETE /v1/rbac/vision/permission-groups/<uuid>/` refuses system groups and
allows everything else (`views.py:488-496`). `TenantRoleGroup.group` is
`on_delete=CASCADE` (`models.py:655-657`), and `TenantRoleGroup` has no tenant
column of its own - it reaches one through `role.tenant`.

So a single Vision action deletes every attachment of that bundle across every
tenant on the platform, with no confirmation, no impact preview, and no
`PROTECT` to stop it.

The audit is per-member, not per-attachment: `post_delete` on `GroupPermission`
writes one row per key in the group (`signals.py:170-187`), and `post_delete` on
`TenantRoleGroup` writes one row per detached role (`signals.py:521-538`). So
the trail does record it - as N rows with no summary line saying "42 roles
across 17 schools lost this bundle".

### What actually happens

CX tidies up an old "Finance Read-Only" group. Eleven schools had attached it to
their Accounts Clerk role. Every clerk on the platform loses finance read access
at once, the schools call support, and the only evidence is a scatter of
individual PERMISSION_CHANGED rows.

### The fix

Refuse the delete when the group is attached anywhere, exactly as `PROTECT`
would:

```python
attached = TenantRoleGroup.objects.filter(group=instance).count()
if attached:
    return error_response(
        message=f"This group is attached to {attached} role(s). Detach it first.",
        status=status.HTTP_409_CONFLICT,
    )
```

Or change the FK to `on_delete=PROTECT` and let `core/exceptions.py` produce the
409 it already produces for every other protected reference
(`core/exceptions.py:130-140`) - which is the better fix, because it also covers
the ORM paths the view does not.

Either way, add an `attached_roles_count` annotation to the group list so the
blast radius is visible before the button is pressed.

---

## 9. A school admin can permanently freeze one of their own roles

**Medium. Confirmed by execution.**

### The defect

`is_locked` is writable on the role serializer - it is absent from
`read_only_fields`:

```python
# serializers/tenant.py:303-311
read_only_fields = [
    "id", "key", "is_system_role", "version", "created_by",
    "created_at", "updated_at",
]
```

and both write paths on the detail view refuse a locked role:

```python
# views.py:588-592
if instance.is_locked and not super_admin:
    return error_response(message="This role is locked and cannot be modified.",
                          status=status.HTTP_403_FORBIDDEN)
# views.py:607-611
if instance.is_locked:
    return error_response(message="This role is locked and cannot be deleted.",
                          status=status.HTTP_403_FORBIDDEN)
```

So `PATCH {"is_locked": true}` succeeds once and every subsequent PATCH and
DELETE on that role is a 403. There is no unlock endpoint. The escape hatch in
the code is `is_vision_super_admin`, and per §12 a Vision super admin cannot
reach a school tenant's role routes at all.

### What actually happens

Corona's admin locks the Bursar role to stop a colleague fiddling with it. Two
months later the school adopts the payments module and the Bursar role needs
three new keys. The PATCH is a 403, the DELETE is a 403, and CX cannot help
because the endpoint refuses their `?tenant=corona` before the view runs. The
only remedies are a database edit or building a new role and re-assigning
everyone.

Confirmed by execution:

```
PROBE9 lock PATCH:      200   is_locked now: True
PROBE9 later edit:      403   "This role is locked and cannot be modified."
PROBE9 unlock attempt:  403
PROBE9 delete attempt:  403
```

### The fix

Make `is_locked` read-only on the tenant-facing serializer - it is a
provisioning flag set by `provision_role_from_prebuilt` (`services.py:88`), not
a user-facing control - and add it to `read_only_fields` alongside
`is_system_role`.

If a tenant-facing lock is actually wanted, it needs its own endpoint with the
inverse operation, gated on a key the locker holds.

---

## 10. `TenantRoleTemplate.branch` narrows nothing

**Medium.**

### The defect

`TenantRoleTemplate` carries a nullable `branch` FK (`models.py:569-572`). It is
validated against the tenant (`models.py:595-598`), indexed
(`models.py:592`), filterable on the list endpoint (`views.py:540-541`),
resolved through a tenant-scoped reference field
(`serializers/tenant.py:258-264`) and returned on both list and detail
serializers.

No evaluation code reads it. `_role_permission_keys` selects role ids by
assignment and reads `TenantRolePermission` (`evaluator.py:156-178`);
`_assignment_branch_q` reads the **assignment's** branch
(`evaluator.py:61-81`); `_grant_scope` reads the assignment's branch
(`scoping.py:83-88`); `resolve_users_with_permission` reads the assignment's
branch (`evaluator.py:287-291`).

The one place the column has any effect at all is indirect, in another app:

```python
# vs_user/services/user.py:102
branch=user.branch if role_instance.branch_id else None,
```

- a branch-pinned *template* causes the *assignment* to inherit the user's home
branch. That is the whole of its influence.

### What actually happens

Corona's admin creates a "Branch Bursar" role and pins it to Ikeja, reasonably
expecting that anyone given it works at Ikeja. They then assign it to Mr Bello
through the role-assignment screen without setting `branch`. Bello holds a
whole-tenant grant: `_grant_scope` sees a NULL branch id and returns
`WHOLE_TENANT`, so he sees Lekki's and Yaba's books too. The screen said Ikeja.

### The fix

Either make the column mean something or take it off the API. The smaller and
safer change is to make the assignment inherit it consistently - if
`role.branch_id` is set and the assignment names no branch, default the
assignment's branch to the role's, and refuse an assignment whose branch
disagrees with a pinned role's. That matches what `helpers.make_assignment`
already does in tests (`tests/helpers.py:249`) and what `vs_user` does on user
creation, and it makes the three writers agree.

Document the decision either way in the model docstring, because the field
currently reads as a scope and is not one.

---

## 11. Nothing checks the dependency graph for cycles

**Medium.**

### The defect

`PermissionDependencyListCreateView` creates a dependency with no graph check at
all - the serializer only resolves the two keys
(`serializers/registry.py:216-246`).

`PermissionDependencyValidator.detect_circular_dependencies` exists
(`validators.py:118-132`) and is called by nothing. The only cycle detection is
inside `get_all_dependencies`, which raises when it re-enters a key already on
the current path (`validators.py:63-67`) - and that runs at *role edit* time,
not at dependency creation time.

`validate_role_permissions` turns that raise into a role-level refusal:

```python
# validators.py:99-103
try:
    required_deps = self.get_all_dependencies(perm_key)
except ValidationError as e:
    errors.append(str(e)); continue
```

which lands in `result["errors"]` and makes the whole set invalid.

### What actually happens

CX records that `finance.invoice.approve` requires `finance.invoice.view`, and
later - tidying up - records that `finance.invoice.view` requires
`finance.invoice.approve`. Both POSTs return 201.

From that moment, **every** role edit anywhere on the platform that includes
either key fails with `"Circular dependency detected for permission:
finance.invoice.view"`. Every school's Bursar role becomes uneditable, and every
change request touching one becomes a terminal `APPLY_FAILED` (§20). The message
names a permission, not the dependency row that caused it, and there is no
screen that shows the graph.

### The fix

Validate on write. In `PermissionDependencySerializer.validate`, build the
closure of `depends_on` and refuse if `permission` is in it:

```python
validator = PermissionDependencyValidator()
if permission_key in validator.get_all_dependencies(depends_on_key) \
        or permission_key == depends_on_key:
    raise serializers.ValidationError({
        "depends_on_key": f"'{depends_on_key}' already requires "
                          f"'{permission_key}', so this would create a cycle.",
    })
```

That is the choke point - one check, at the only place a cycle can enter the
graph. Wire `detect_circular_dependencies` into a management command as well, so
an existing cycle can be found rather than inferred from a role edit failing.

---

## 12. CX cannot reach a school's role endpoints, so the super-admin branches are dead

**Medium.**

### The defect

`TenantRoleTemplateDetailView.update` has two branches that only make sense for
a caller from outside the tenant:

```python
# views.py:587-597
super_admin = is_vision_super_admin(request.user)
if instance.is_locked and not super_admin: ... 403
if instance.is_system_role and not super_admin: ... 403
```

For a school tenant, neither branch can be reached. `TenantJWTAuthentication`
refuses a foreign `?tenant=` slug unless the view opts in:

```python
# authentication.py:119-128
elif actor.tenant_id != tenant.pk:
    allowed = (getattr(actor.tenant, "kind", None) == Tenant.Kind.PLATFORM
               and getattr(view, "platform_cross_tenant_param", False))
    if not allowed:
        raise NotFound("No tenant matches the requested context.")
```

No view in `rbac_roles_assignments` or `rbac_change_requests_overrides` sets
`platform_cross_tenant_param` - only `_UserPermissionOverrideBase` does
(`views.py:1086`). And `TenantScopedRBACMixin.get_tenant` would refuse the URL
slug anyway (`views.py:83-88`).

So a real Vision super admin asserting `?tenant=corona` on
`/v1/rbac/tenants/corona/roles/school_admin/` gets a 404. The super-admin
branches only ever fire for codex's **own** roles.

### What actually happens

A school locks itself out (§9) or provisions a broken system role, and calls
support. CX has a super admin whose whole purpose is bypassing RBAC, code that
explicitly anticipates them editing a locked role - and no route that lets them
do it. The fix is a database edit.

### The fix

Set `platform_cross_tenant_param = True` on the tenant-scoped RBAC views, the
same way the override views do. RBAC still evaluates against
`request.rbac_tenant`, which is the **actor's** tenant when there is no
impersonation (`authentication.py:145`), so a platform actor would need the
`platform.roles.*` key rather than the school one - which is exactly the
intended split, and is already how override management works across tenants.

Do it deliberately and with tests: this widens a boundary, and the tests must
assert that a *school* actor still cannot assert another school's slug (they
cannot - the `kind == PLATFORM` check is on the actor's tenant, not the target's).

---

## 13. Moving an assignment's branch or user is unaudited

**Medium. Confirmed by execution.**

### The defect

The assignment receiver has exactly two branches (`signals.py:397-446`):

```python
if created:      ROLE_ASSIGNED ...; return
if instance.assignment_status == REVOKED:   ROLE_CHANGED ...
```

`TenantUserRoleAssignmentDetailView` accepts PUT and PATCH with
`ROLE_ASSIGN_KEYS` (`views.py:669-674`), and the serializer leaves `user`,
`role` and `branch` writable (`serializers/tenant.py:521-541`, none of them in
`read_only_fields`).

So `PATCH {"branch": 3}` on an active assignment changes the grant's scope and
writes nothing. `PATCH {"user": 58}` moves the whole grant to a different person
and writes nothing. `PATCH {"role": 9}` changes which role is held - the
serializer refuses only `xvs_super_admin` (`serializers/tenant.py:688-696`) -
and writes nothing.

### What actually happens

Mrs Balogun's Storekeeper grant is pinned to Ikeja. Somebody PATCHes it to
Lekki. She now sees and adjusts Lekki's stock, and the audit trail contains one
ROLE_ASSIGNED row from three months ago naming Ikeja. There is no record that
anything changed, and `RBACAuditLog` - the table built precisely so permission
changes cannot be lost - has nothing to show.

The `replace` endpoint exists for exactly this and does it properly, revoking
and re-creating so both halves are audited (`views.py:844-859`). The PATCH route
is the quiet way round it.

Confirmed by execution. `PATCH /role-assignments/<id>/ {"branch": <lekki>}` on a
grant pinned to Ikeja:

```
PROBE13 status: 200   branch now: 2 (was 1)
PROBE13 audit rows written: 0
```

### The fix

Two changes, either of which closes it, and both are worth making:

- Make `user`, `role` and `branch` read-only on
  `TenantUserRoleAssignmentSerializer`, so the only way to change a grant is
  revoke + create or the `replace` endpoint. That matches the model's stated
  intent - `assigned_at` / `revoked_at` are a history, and mutating a row in
  place makes them lie.
- Add an `else` branch to the receiver that diffs the changed fields (capture
  them in a `pre_save` hook, as `_capture_old_status` already does for two other
  models) and writes a ROLE_CHANGED row.

---

## 14. Group and change-request detail responses are N+1 on `resource`

**Medium.**

### The defect

`PermissionSerializer.get_resource_key` dereferences the relation per row:

```python
# serializers/registry.py:137-138
def get_resource_key(self, obj):
    return obj.resource.name if obj.resource_id else None
```

The permission list view knows this and prefetches
(`views.py:243`: `select_related("module", "resource", "action")`). Two other
consumers do not:

```python
# views.py:486   group detail
return PermissionGroup.objects.all().prefetch_related("permissions")
```

`prefetch_related("permissions")` fetches the `Permission` rows but not their
`resource`, so serializing a group with 60 keys costs 60 extra queries - on GET,
and again on the response to every POST and PATCH, since
`PermissionGroupDetailSerializer` is the write serializer too
(`views.py:458-462`).

```python
# views.py:897   change-request list
.prefetch_related("delta_items__permission")
```

Same shape: `TenantRoleChangeDeltaItemSerializer.permission` is a nested
`PermissionSerializer` (`serializers/tenant.py:767`), so a page of 25 requests
with four delta items each costs 100 extra queries.

`PermissionDetailSerializer` adds three more per-object queries on top
(`serializers/registry.py:263-279`), which is acceptable on a detail route and
would not be inside a list.

### The fix

Extend the prefetches to the depth the serializer actually reads:

```python
# group detail
.prefetch_related("permissions__resource", "permissions__module", "permissions__action")
# change-request list and approval queue
.prefetch_related("delta_items__permission__resource")
```

Better, since `module_id` and `action_id` *are* the names, is to stop
dereferencing `resource` at all - store the composed key's parts on the row or
read `obj.key.split(".")[1]`. But the prefetch is the one-line fix and should go
in first.

---

## 15. `version` is bumped by three code paths and read by none

**Medium.**

### The defect

`TenantRoleTemplate.version` is incremented in three places, each with a comment
saying what it is for:

```python
# services.py:173-175
# Version bump invalidates downstream effective-permission caches.
target_role.version = (target_role.version or 1) + 1

# serializers/registry.py:401-409
# Any tenant role attached to this group now has a changed effective
# permission set, so bump their versions to invalidate caches downstream.

# serializers/tenant.py:475-476
if permission_keys is not None or group_ids is not None:
    instance.version = (instance.version or 1) + 1
```

Nothing reads it. `grep` over the repo finds it only in those three writers and
in three test assertions. The evaluator's cache is keyed by
`(tenant.pk, branch)` on the user instance and lives for one request
(`evaluator.py:206-221`); it has no reason to consult a version and does not.

The group-update path pays real cost for it: one `save()` per attached role, in
a Python loop (`serializers/registry.py:404-409`), so a bundle attached to 200
roles across the platform issues 200 UPDATE statements to bump a number nobody
reads.

### What actually happens

Nothing user-visible - which is the problem. The comments describe a cache
invalidation mechanism that does not exist, so the next person to add a real
cross-request permission cache will believe the plumbing is already there.

### The fix

Pick one:

- **Keep it as an optimistic-concurrency token**, which is what a `version`
  column usually is, and start reading it: have the role update serializer
  accept the client's `version` and 409 on mismatch. That would also close a
  real lost-update window on the role editor.
- **Or delete it**, along with the three bumps and the loop, and say in the
  model docstring that effective permissions are resolved per request.

Either way, replace the group-update loop with a single
`TenantRoleTemplate.objects.filter(id__in=…).update(version=F("version") + 1)`.

---

## 16. `permissions_count` ignores keys that arrive through a group

**Medium.**

### The defect

```python
# views.py:531-535
permissions_count=Count(
    "role_permissions",
    filter=Q(role_permissions__granted=True),
    distinct=True,
),
```

`role_permissions` is `TenantRolePermission` - direct grants only. Keys reaching
the role through an attached `PermissionGroup` are counted by nothing, and the
list serializer exposes the number as `permissions_count`
(`serializers/tenant.py:213`).

The role *detail* response is correct, because it expands both
`role_permissions` and `role_groups` (`serializers/tenant.py:266-267`).

### What actually happens

`seed_school_permission_groups` deliberately bundles the school-facing keys into
named groups (`core/management/commands/seed_school_permission_groups.py`), so a
role built the recommended way - attach two bundles, add nothing directly -
shows **0 permissions** on the role list. An admin reads that as an empty role
and either deletes it or grants everything again directly.

### The fix

Count both, which needs a subquery rather than a second `Count` (two joins in
one annotation multiply):

```python
group_key_count = Subquery(
    GroupPermission.objects
    .filter(group__tenant_role_attachments__role=OuterRef("pk"))
    .values("group__tenant_role_attachments__role")
    .annotate(n=Count("permission_id", distinct=True))
    .values("n")[:1]
)
```

and expose the two numbers separately (`direct_permissions_count`,
`group_permissions_count`) or sum them - but the union, not the sum, is the
honest total, since a key can be in both.

---

## 17. Renaming a permission module or action creates a duplicate row

**Medium. Confirmed by execution.**

### The defect

`PermissionModule.name` and `PermissionAction.name` are both
`primary_key=True` (`models.py:167`, `201`), and both detail views accept
PUT/PATCH through the plain `UpdateModelMixin` with `name` writable on the
serializer (`serializers/registry.py:72-76`, `102-108`).

Django's `Model.save()` on a row whose pk has changed issues
`UPDATE … WHERE name = <new name>`, matches zero rows, and falls through to an
INSERT. The old row is untouched.

`Permission` has the same shape and the detail view works around it with an
explicit `.update(key=…)` first (`views.py:340-343`, and see §5). Modules and
actions have no such workaround.

### What actually happens

CX renames the `communication` module to `notifications`. The response is a 200
with the new name. The database now holds two modules: `communication`, still
carrying every resource and permission, and `notifications`, empty. The registry
list shows both. Every key still reads `communication.*`, and the rename has
achieved nothing except a confusing duplicate.

Renaming an action is worse, because `PermissionAction` is referenced by
`Permission.action` with `db_constraint=False` (`models.py:256-262`) - so
PostgreSQL does not object, and the orphan is invisible until somebody notices
the action list has grown.

Confirmed by execution. `PATCH /vision/permission-modules/communication/
{"name": "notifications"}`:

```
PROBE17 status: 200
PROBE17 modules added: ['notifications']   removed: []
PROBE17 permission key still: ['communication.message.send']
```

### The fix

Make `name` read-only on both serializers. A module or action name is a
primary key and part of every composed permission key; renaming it is a data
migration, not a form field.

If it must be editable, it needs the same treatment §5 describes for
`Permission` - a service that rewrites every referencing row in one transaction
- and the same conclusion applies: read-only is the honest answer.

---

## 18. The super-admin transfer's bulk revoke is unaudited

**Medium.**

### The defect

`transfer_super_admin` revokes the incoming holder's other roles with a
queryset update:

```python
# services.py:257-266
TenantUserRoleAssignment.objects.filter(
    tenant=codex, user=to_user, assignment_status=ACTIVE,
).update(
    assignment_status=REVOKED, revoked_at=now, revoked_by=from_user,
    reason_note="Role revoked as part of super admin transfer.",
)
```

`queryset.update()` fires no `post_save`, so `audit_tenant_role_assignment`
(`signals.py:397`) never runs for those rows. The same function's *other*
revocation - of the outgoing super admin - uses `revoke()` + `save()` and is
audited (`services.py:253-254`).

The two `is_superuser` flips are also bulk updates (`services.py:285-286`), and
`vs_user` has no receiver on that field either.

### What actually happens

The Vision Super Admin role moves from Mr Adeleke to Mrs Chukwu. Mrs Chukwu was
a Platform Admin and an Audit Officer. Both of those roles are revoked, and the
only trail is the one ROLE_CHANGED row the service writes about the transfer
itself (`services.py:288-297`), whose metadata names the two user ids and
nothing about the roles that were stripped.

### The fix

Iterate and save, since the volume is one or two rows:

```python
for row in TenantUserRoleAssignment.objects.filter(
        tenant=codex, user=to_user, assignment_status=ACTIVE):
    row.revoke(by_user=from_user, reason="Revoked as part of super admin transfer.")
    row.save(update_fields=["assignment_status", "revoked_at", "revoked_by",
                            "reason_note", "updated_at"])
```

That is the whole fix, and it makes the transfer's trail complete. The broader
lesson - a `queryset.update()` on a model with audit receivers silently skips
them - is worth a sweep of the other engine apps.

---

## 19. A permission group's `scope` is invisible on both serializers

**Medium.**

### The defect

`PermissionGroup.scope` is the field that decides whether a bundle may be
attached to a school role at all (`models.py:368-374`,
`models.py:683-689`). It is on neither serializer:

```python
# serializers/registry.py:293-302   list
fields = ["id", "name", "description", "is_system", "is_active",
          "permissions_count", "created_at", "updated_at"]
# serializers/registry.py:336-346   detail
fields = ["id", "name", "description", "is_system", "is_active",
          "permissions", "permission_keys", "created_at", "updated_at"]
```

Create forces it to `TENANT` (`serializers/registry.py:375`), with a good
reason - `GroupPermission` already refuses a platform key inside a non-platform
bundle, so this endpoint could never have produced a platform bundle. But the
consequence is that the registry screen cannot show CX which of the seeded
bundles are platform-only, and cannot mark a new one as platform even when that
is what is wanted.

### What actually happens

CX builds a bundle of the impersonation keys for their own support tier. The
create silently classifies it `TENANT`; `GroupPermission.assert_scope_allowed`
then refuses every member because they are all `PLATFORM`
(`models.py:432-440`), and the endpoint returns a validation error about the
first key with no hint that the bundle's own classification is the problem.
There is no field to change.

### The fix

Expose `scope` read-only on `PermissionGroupListSerializer` and writable (with
the `TENANT` default retained) on `PermissionGroupDetailSerializer`. Add a
`?scope=` filter to the list. The guard on `GroupPermission` already makes a
wrong choice safe, so making the field visible costs nothing and removes a dead
end.

---

## 20. A fixable dependency mistake kills a change request permanently

**Medium.**

### The defect

The decide endpoint catches every exception and marks the request terminal:

```python
# views.py:1019-1033
if action == "APPROVE":
    try:
        with transaction.atomic():
            apply_role_change_request(obj=obj, reviewer=request.user, notes=notes)
    except Exception as exc:
        obj.mark_apply_failed(reviewer=request.user, notes=str(exc))
        obj.save(...)
        return error_response(message="Approval failed while applying changes.",
                              error={"error": str(exc)},
                              status=status.HTTP_500_INTERNAL_SERVER_ERROR)
```

`apply_role_change_request` calls `validate_role_permissions`, which raises a
Django `ValidationError` for a missing prerequisite (`validators.py:190-192`) -
an ordinary, user-fixable input problem.

And `APPLY_FAILED` is terminal, because only `PENDING` may be decided:

```python
# views.py:996-1000
if obj.status != TenantRoleChangeRequest.Status.PENDING:
    return error_response(message=f"Request already decided ({obj.status}).",
                          status=status.HTTP_409_CONFLICT)
```

The same catch-all also swallows the scope refusal from §22 and any genuine
server error, so all three look identical to the reviewer.

### What actually happens

Corona's admin files a request adding `finance.invoice.approve` to a role that
does not hold `finance.invoice.view`. The head of finance approves it and gets a
500 reading "Approval failed while applying changes", with
`{'permission_keys': ["Permission 'finance.invoice.approve' requires:
finance.invoice.view"]}` buried in an `error` object. The request is now
`APPLY_FAILED` and cannot be retried, edited or reopened. The correct move -
add the missing key to the delta - is impossible; a whole new request must be
filed from scratch.

### The fix

Separate "the request is wrong" from "the apply broke":

```python
except DjangoValidationError as exc:
    return error_response(
        message="These changes cannot be applied yet.",
        error={"errors": _validation_error_detail(exc)},
        status=status.HTTP_400_BAD_REQUEST,
    )                       # request stays PENDING - fix the delta and retry
except Exception as exc:
    obj.mark_apply_failed(...); ... 500
```

`core/exceptions._validation_error_detail` already exists for exactly this
shape. And validate the delta at **submission** time as well, so the requester
learns about the missing prerequisite before a reviewer is involved rather than
after.

---

## 21. Approving a change request deletes the role's explicit denies

**Medium. Confirmed by execution.**

### The defect

`apply_role_change_request` snapshots only the granted keys, then deletes
everything and rebuilds only grants:

```python
# services.py:132-137
current_keys = set(
    TenantRolePermission.objects.filter(role=target_role, granted=True)
    .values_list("permission_id", flat=True)
)
before_keys = sorted(current_keys)
...
# services.py:159-171
TenantRolePermission.objects.filter(role=target_role).delete()   # <-- ALL rows
perms = Permission.objects.filter(key__in=final_keys)
TenantRolePermission.objects.bulk_create([
    TenantRolePermission(role=target_role, permission=perm, granted=True, ...)
    for perm in perms
])
```

`granted=False` rows are a first-class concept - the evaluator subtracts them
(`evaluator.py:168-178`), the scope guard exempts them because a deny cannot
escalate (`models.py:634-638`), and `resolve_users_with_permission` honours them
(`evaluator.py:282-285`). They do not survive an approval, and because
`before_keys` was built from grants only, the audit diff does not mention them
either.

The role detail serializer has the same problem
(`serializers/tenant.py:483-493`), so a direct PATCH wipes denies too - and that
path writes no audit row at all (§3).

### What actually happens

Corona added an explicit deny of `finance.report.export` to the Bursar role
after a data-handling incident, so bursars keep every other finance key but
cannot export. Six months later an unrelated change request adds one invoice
key. On approval, the deny row is deleted, bursars can export again, and the
audit row's `before`/`after` lists are identical apart from the one added key.
Nobody can see what happened, and the incident's remediation is silently undone.

Confirmed by execution. A Bursar role holding `finance.invoice.view` (granted)
and `finance.report.export` (denied), with a change request adding
`finance.invoice.create`:

```
PROBE21 denies before: ['finance.report.export']
PROBE21 decide status: 200
PROBE21 denies after:  []
PROBE21 audit diff: {'permission_keys': {
    'before': ['finance.invoice.view'],
    'after':  ['finance.invoice.create', 'finance.invoice.view']}}
```

The deny is gone and the diff never mentions it.

### The fix

Preserve deny rows across the rebuild:

```python
denies = list(TenantRolePermission.objects.filter(role=target_role, granted=False)
              .values_list("permission_id", flat=True))
...
TenantRolePermission.objects.filter(role=target_role).delete()
TenantRolePermission.objects.bulk_create(
    [TenantRolePermission(role=target_role, permission_id=k, granted=True, ...)
     for k in final_keys if k not in denies] +
    [TenantRolePermission(role=target_role, permission_id=k, granted=False)
     for k in denies]
)
```

and include the deny list in `before_data` / `diff_data` so the audit shows the
full state, not just half of it. Apply the same change in
`TenantRoleTemplateDetailSerializer.update` - this is one root cause with two
call sites, and fixing only the approval path leaves the commoner one broken.

A REMOVE delta item on a denied key should probably clear the deny row rather
than being a no-op, which is a small decision worth making explicitly.

---

## 22. A change request may queue a platform-scoped key

**Medium.**

### The defect

`TenantRoleChangeDeltaItemSerializer.validate_permission_key` checks existence
and nothing else:

```python
# serializers/tenant.py:781-787
if not Permission.objects.filter(key=value).exists():
    raise serializers.ValidationError("Unknown permission_key.")
```

The role detail serializer, by contrast, rejects out-of-scope keys up front with
a proper field error and names every offender at once
(`serializers/tenant.py:347-377`). The change-request path has no equivalent.

The scope guard does eventually fire - `bulk_create` in
`apply_role_change_request` goes through `ScopeGuardedManager`
(`models.py:122-126`) - but only at approval time, and the catch-all in §20
turns it into a 500 and a terminal `APPLY_FAILED`.

### What actually happens

Corona's admin files a request adding `platform.audit.manage` to their Audit
Officer role. It is accepted (201), appears in the approval queue, is reviewed
by the head of compliance, and approved. The reviewer gets a 500. The request is
dead, and the error text is a raw `ValidationError` repr about platform scope
that nobody in the tenant can act on.

It fails closed, which is the important thing. But it fails four steps too late,
in the most expensive possible way.

### The fix

Reuse the check that already exists. In
`TenantRoleChangeRequestSerializer.validate`, run the ADD keys through
`platform_only_keys` against the request's tenant, exactly as
`_reject_out_of_scope_keys` does (`serializers/tenant.py:347-365`) - extract that
method into a shared function so both serializers call the same code rather than
growing a second copy.

---

## 23. The approval queue is the same query as the plain list

**Low.**

`TenantRoleChangeRequestApprovalQueueView.get_queryset` (`views.py:923-936`) and
`TenantRoleChangeRequestListCreateView.get_queryset` (`views.py:892-905`) are
byte-for-byte the same query, with the same filters, the same ordering and the
same serializer. Both take `ROLE_VIEW_KEYS` (`views.py:889`, `920`).

So `role-change-requests/approval/` is an alias. It is not restricted to
`PENDING`, not restricted to requests the caller may decide, and not gated on a
different key. A view named "approval queue" that carries no approval authority
invites the assumption that reviewing is a separate privilege. It is not - and
per §9 of `rbac_change_requests_overrides`, the same person can file and decide
their own request.

**Fix:** either delete the route and let the frontend pass `?status=PENDING`, or
make it mean something - default to `PENDING`, and gate it on a distinct
`*.roles.approve` key so requester and reviewer can be different people.

---

## 24. `impact_summary` is written by nothing

**Low.**

`TenantRoleChangeRequest.impact_summary` is a `JSONField(default=dict)`
documented as "Cached diff to help the reviewer" (`models.py:961`,
`models.py:1007`). It is on the serializer's field list
(`serializers/tenant.py:820`) and read-only there, so a client cannot set it.
No code writes it. Every request carries `{}`.

The reviewer screen therefore has the delta items (which it can read) and no
computed impact - no "12 users affected", no "this adds a CRITICAL key", no
before/after set. `apply_role_change_request` computes exactly that information
at approval time (`services.py:132-149`) and discards it into the audit row
instead of the request.

**Fix:** compute it at submission in
`TenantRoleChangeRequestSerializer.create` - replay the delta over the role's
current grants, count affected assignments, and flag any CRITICAL or restricted
key - and store it. The replay code already exists in `services.py:139-149` and
should be extracted so both callers share it. Or drop the field.

---

## 25. The APPROVED transition has no `post_save` audit branch

**Low.**

`audit_tenant_role_change_request` handles `created`, `DENIED` and
`APPLY_FAILED` (`signals.py:558-613`). There is no `APPROVED` branch, on the
stated grounds that "approval + permission diff is audited in
services.apply_role_change_request" (`signals.py:542-543`) - which is true of
that one code path.

It means the audit trail's completeness depends on nobody ever calling
`obj.mark_approved()` outside `apply_role_change_request`. `mark_approved` is a
public method on the model (`models.py:1031-1035`), and a future caller - a
management command, a data fix, a bulk approver - would leave a request APPROVED
with no row anywhere.

**Fix:** add the `APPROVED` branch to the receiver for symmetry. The duplicate
with the service's row is harmless and the two carry different detail (the
receiver has the status diff, the service has the key lists).

---

## 26. The override uniqueness constraint omits `tenant`

**Low.**

```python
# models.py:894-897
models.UniqueConstraint(fields=["user", "permission"],
                        name="uq_user_permission_override")
```

The model is otherwise scrupulously tenant-scoped - `tenant` is a required FK,
`clean()` requires the user to belong to it (`models.py:930-931`), the evaluator
filters on it (`evaluator.py:122-123`), and the views filter on it
(`views.py:1183`). Only the constraint does not.

Today a user belongs to exactly one tenant, so the two are equivalent. If a user
is ever moved between tenants - or if a support script writes an override with
the wrong tenant - the row follows the user and becomes invisible to the
evaluator (which filters `tenant=`) while still occupying the one slot that key
has, so a correct override for the new tenant cannot be created. The error would
be "A record with these details already exists" against a row the admin cannot
see.

**Fix:** add `tenant` to the constraint fields. It is a one-line migration and
it makes the constraint say what every other query already assumes.

---

## 27. `rbac_group_permission` is documented, unused, and broken

**Low.**

`HasRBACPermission`'s docstring advertises it with an example:

```
For group-based permissions (all-of), use rbac_group_permission::
    rbac_group_permission = "finance_group"
```

`permissions.py:273-279`. The implementation:

```python
# permissions.py:343
perm_keys = _group_permission_keys(rbac_group_perms)
```

and `_group_permission_keys` does
`GroupPermission.objects.filter(group_id__in=group_ids)`
(`evaluator.py:100-106`). `PermissionGroup.id` is a `UUIDField`
(`models.py:363`), so passing the string `"finance_group"` raises rather than
returning an empty set.

`grep` finds no view in the repo setting `rbac_group_permission`, which is why
this has never fired.

**Fix:** either delete the branch and the docstring section, or make it work by
resolving groups by **name** (which is what the docstring's example implies) and
caching the lookup. Deleting is the better answer - the any-of `rbac_permission`
list covers every case a view has actually needed, and an all-of gate whose
membership can be changed by a Vision admin editing a bundle is a surprising
thing to hang a view on.

---

## 28. `HasAnyModuleAccess` loads the entire key set for a prefix question

**Low.**

```python
# permissions.py:400-402
keys = get_effective_permissions(u, tenant=tenant)
prefixes = tuple(f"{m}." for m in modules)
return any(key.startswith(prefixes) for key in keys)
```

The question is "does this caller hold *any* key under `finance.`", and the
answer is computed by materialising every key the caller holds - four queries on
a cold cache - and scanning them in Python.

In practice the set is already memoised for the request by the time this runs
(the same view's other permission classes will have populated it), so the real
cost is usually zero. The class is used by exactly one view file
(`vs_finance/views.py`).

The `startswith` on a tuple with the trailing dot is right, and the comment says
why: `"finance."` must not match `"financex."` (`permissions.py:401`).

**Fix:** low priority. If it ever matters, an `EXISTS` on
`TenantRolePermission` filtered by `permission__module_id__in=modules` would
answer the same question in one query - but it would have to fold in group and
override rows to stay consistent with the evaluator, which is precisely why the
current shape was chosen. Leave it unless it shows up in a profile.

---

## 29. `RBACAuditLog` has no tenant, no populated `school_id`, and no reader

**Low.**

Three separate gaps in one table:

- **No tenant column.** `RBACAuditLog` has `actor`, `school_id`,
  `entity_type`/`entity_id` and JSON blobs (`models.py:1102-1124`). There is no
  FK to `Tenant`.
- **`school_id` is almost always empty.** It is read from
  `metadata["school_id"]` (`audit.py:46`), and the only writers that supply one
  are the two override views (`views.py:1139`). Every signal-based row - role
  assignments, revocations, role creation, group attachment, the entire
  vocabulary trail - carries `school_id = ""`. The
  `(school_id, created_at)` index is therefore an index on one value.
- **Nothing reads the table.** `grep` finds `RBACAuditLog` in `models.py`,
  `audit.py`, and two test files. No view, no serializer, no export dataset, no
  management command. The durable trail the module was built around is
  write-only.

The `vs_audit` mirror *is* readable, through the Event Explorer - but it is
best-effort by contract and is exactly what `RBACAuditLog` exists to be more
reliable than (`models.py:1092-1097`).

**What actually happens:** an auditor asking "who changed permissions at Corona
last quarter?" cannot be answered from this table. The rows exist; they cannot
be filtered to a tenant, and there is no endpoint to ask.

**Fix:** add a nullable `tenant` FK, populate it in `record_rbac_audit` from the
ambient tenant context (`vs_tenants.context.get_current_tenant`) with the
explicit `metadata["tenant_id"]` taking precedence, backfill from
`metadata["tenant_id"]` where the callers already supply it
(`signals.py:419`, `473`, `489`, `517`, and others), and index
`(tenant, created_at)`. Then give it a read endpoint, or an
`vs_exports` dataset, gated on `platform.audit.view`.

---

## 30. `RBACAuditLog` immutability is Python-only

**Low.**

```python
# models.py:1134-1140
def save(self, *args, **kwargs):
    if self.pk is not None:
        raise ValidationError("RBACAuditLog entries are immutable.")
    super().save(*args, **kwargs)

def delete(self, *args, **kwargs):
    raise ValidationError("RBACAuditLog entries cannot be deleted.")
```

`Model.delete()` and `Model.save()` are the only two paths guarded.
`RBACAuditLog.objects.filter(...).update(...)` and
`RBACAuditLog.objects.filter(...).delete()` bypass both entirely - a queryset
delete does not call the model's `delete()`, and a queryset update never touches
`save()`. So does raw SQL, and so does a cascade from `actor` (though that FK is
`SET_NULL`, so it does not delete).

`vs_config`'s equivalent table has a "double immutability guard"
(`docs/config/config_audit_trail_exports.md`), which is the pattern to copy.

**Fix:** add a manager whose `update()` and `delete()` raise, set it as
`objects`, and - for real durability - add a PostgreSQL rule or trigger
forbidding UPDATE and DELETE on the table. The Python guard documents intent;
the database guard enforces it.

---

## 31. The FLS permission cache is not keyed by tenant

**Low.**

```python
# fls.py:86-89
if not hasattr(request, "_fls_permissions"):
    tenant = getattr(request, "tenant", None) or getattr(user, "tenant", None)
    request._fls_permissions = get_effective_permissions(user, tenant=tenant)
```

One attribute, one value, for the whole request. The evaluator's own cache is
keyed by `(tenant.pk, branch)` precisely because the answer differs per tenant
(`evaluator.py:206-209`); this one is not.

A request that serialises two objects belonging to different tenants - a
platform actor's cross-tenant screen, a report spanning tenants, a nested
serializer reaching another tenant's rows - resolves the first tenant's
permissions and applies them to both.

In practice this is hard to reach today, because `request.tenant` is fixed for
the request and most cross-tenant surfaces are CX-only (where the super admin
bypasses FLS anyway, `fls.py:79-82`). It is a latent trap rather than a live
bug.

**Fix:** key the cache the way the evaluator does:

```python
cache = getattr(request, "_fls_permissions", None) or {}
key = getattr(tenant, "pk", None)
if key not in cache:
    cache[key] = get_effective_permissions(user, tenant=tenant)
    request._fls_permissions = cache
return cache[key]
```

---

## 32. `_stripped_fields` tells an unauthorised caller what exists

**Low.**

```python
# fls.py:124-125
if stripped:
    data["_stripped_fields"] = stripped
```

The mask removes the values and then names the fields it removed. A caller
without `procurement.stock.view_sensitive` learns that `unit_cost`,
`supplier_reference` and `margin_pct` exist on the record, on every row of every
list.

This is a deliberate contract - the docstring says the point is to "tell clients
which sensitive fields were withheld" (`fls.py:125`) so the UI can render a
lock icon rather than an absence. That is a real product need and the trade is
defensible.

It is worth recording as a trade rather than an oversight, because the field
*names* are themselves informative: a salary field on a colleague's record tells
you the field exists even when the number does not.

**Fix (if wanted):** make it opt-in per serializer -
`expose_stripped_fields = True` - so a serializer holding genuinely secret
structure can withhold the names too. Default it to the current behaviour so
nothing changes for existing screens.

---

## 33. Smaller defects and dead code

**Low.** Individually minor; listed so they are not rediscovered.

**Dead code:**

- `models._unique_slug` (`models.py:129-144`) - called by nothing. The two live
  key allocators are `serializers/tenant.py:145` and `services.py:39`, which are
  themselves verbatim duplicates of each other and should be one function.
- `validators.detect_circular_dependencies` (`validators.py:118-132`) - called
  by nothing (see §11 for where it should be called).
- `services.create_role_from_suggestion` (`services.py:302-334`) - called by
  nothing. `provision_role_from_prebuilt` is the live one, and the two differ in
  ways nobody has had to reconcile (`is_system_role`, `is_locked`, and the
  name-collision behaviour).
- `Permission.is_restricted` (`models.py:283`) - documented as "marks
  permissions that must flow through approvals". Nothing in `vs_rbac` reads it.
- `PrebuiltRoleTemplate.scope` and `.tier` (`models.py:470-484`) - read by
  nothing. Note `scope` here is an entirely different vocabulary from
  `PermissionScope` and shares only the field name, which is a live trap for a
  reader.
- `TenantRoleChangeDeltaItem.Operation` supports both ADD and REMOVE and the
  constraint allows both for one key on one request
  (`models.py:1074-1079`); the replay applies them in iteration order
  (`services.py:141-145`), which is non-deterministic. Either forbid the pair or
  order the replay.

**No API surface:**

- `PrebuiltRoleTemplate` and `PrebuiltRolePermission` have no endpoint at all.
  The blueprint library that decides what every new school gets can only be
  inspected or changed by a code change plus a re-seed.

**Sloppy error handling:**

- `except (UserModel.DoesNotExist, Exception)` (`views.py:1335`) - the second
  term makes the first redundant and turns every failure, including a
  programming error, into "User not found."
- `PermissionDetailView.update` and `.delete` wrap `get_object()` in
  `except Exception` and return 404 (`views.py:314-320`, `358-365`), so a
  genuine database error reads as a missing permission.
- `TenantRoleChangeRequestSerializer.create` calls
  `Permission.objects.get(key=permission_key)` (`serializers/tenant.py:869`)
  after the field validator already confirmed existence - a second query per
  delta item, and a `DoesNotExist` race if the key is deleted between the two.

**Duplicated logic:**

- `_unique_tenant_role_key` exists in `serializers/tenant.py:145-157` and
  `services.py:39-50`, identically.
- The tenant checks in `TenantUserRoleAssignment.clean()` (`models.py:793-804`)
  duplicate the serializer's fallback checks (`serializers/tenant.py:664-669`),
  and `clean()` is never called on the API path. The serializer's own comment
  says the fallbacks are unreachable through the API, which is true - they are
  belt and braces, and worth keeping, but the model's `clean()` is a third copy
  that runs nowhere.

**Inconsistency:**

- `TenantRoleTemplate.Status` has `INACTIVE` and `ARCHIVED`, and no code
  anywhere distinguishes them - every query filters `status="ACTIVE"`. One of
  the two is redundant, or `ARCHIVED` needs to mean something (hidden from the
  list, perhaps).
- The `?is_active=` filters accept `"true"/"1"/"false"/"0"` and silently ignore
  anything else (`views.py:110-115` and five more copies of the same block).
  That is safe but the block is repeated six times in one file and belongs in a
  helper.

---

## What is right, and should not be "tidied"

This module is unusually well reasoned, and several things that look like
candidates for cleanup are load-bearing. Recording them so a future pass does
not undo them.

- **`ScopeGuardedManager.bulk_create`** (`models.py:114-126`). Putting the scope
  check only on `save()` would have left the serializers' `bulk_create` path -
  the one an attacker reaches - completely unguarded. The manager is the reason
  the guard is real.
- **`ANY_BRANCH` is a sentinel, not `None`** (`evaluator.py:43-58`). Collapsing
  it would merge two different questions - "no branch was named" and "the entity
  as a whole" - and that merge is exactly what previously made a branch-pinned
  grant confer nothing at all.
- **`_grant_scope` does not filter branch liveness in SQL**
  (`scoping.py:76-81`). "No grants" and "every granted branch withdrawn" must
  stay distinguishable, because the first falls back to the home posting and the
  second must show nothing.
- **`BranchScope.filter` short-circuits rather than filtering on an empty `Q`**
  (`scoping.py:233-243`), so a whole-tenant caller produces byte-identical SQL.
  Verified as a design goal in the docstring and worth keeping.
- **`BranchScope.include_shared` defaults to inclusive** (`scoping.py:146-163`).
  A NULL branch means "shared across the school", and hiding shared rows from a
  pinned caller looks like missing data rather than a permission error - so
  nobody would report it.
- **`PlatformDecisionAllowed` returns `False` rather than raising**
  (`permissions.py:182-186`). A distinct refusal message would be a probe: mint
  a role, call the endpoint, read the wording to learn whether the grant landed.
- **Every tenant-scoped reference resolves *inside* the tenant**
  (`serializers/tenant.py:44-54`, `98-139`), with one wording for "malformed",
  "absent" and "someone else's". That removes an enumeration oracle that
  resolve-then-compare would leave open.
- **The `_MAX_BIGINT` guard** (`serializers/tenant.py:56-59`, `134-139`).
  PostgreSQL raises on an oversized bigint rather than returning no rows, so an
  oversized id has to be caught before the query. §6 is the same class of
  problem in the places that *lack* this guard.
- **`record_rbac_audit` writes the durable row first and raises on failure**
  (`audit.py:20-61`). That is deliberately the opposite contract to
  `vs_audit.emit_audit_event`, and the reasoning is in
  `models.py:1090-1100`.
- **`core/exceptions.py` handles `ProtectedError` properly.** Deleting a role
  that still has assignments is a clean 409 - *"This record cannot be deleted
  because 1 tenant user role assignment still reference it. Remove or reassign
  them first."* - not the 500 it would have been. Verified by execution. Several
  findings above would have been much worse without that handler, and §6 is
  precisely the case it does not cover (`ValueError` is not in its ladder).
- **The split unique constraint on assignments** (`models.py:737-761`). The
  comment explains that one combined constraint would silently permit duplicate
  whole-tenant grants, because PostgreSQL treats NULLs as distinct. Two partial
  indexes keep both guarantees.
- **`_holdable_filter` is honest about being a backstop**
  (`evaluator.py:84-97`), not the boundary. The grant models refuse the write in
  the first place; this catches rows that predate the column or arrived by raw
  SQL.
- **School impersonation is intra-tenant by construction**
  (`authentication.py:56-65`). One choke point, and the session's tenant is
  fixed at start, so no `?tenant=` assertion can widen it.
