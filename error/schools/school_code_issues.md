# school_code_issues

Everything wrong with `schools.vs_schools`, in one place, ordered by how much it
costs. Each item states the defect, the evidence, what actually happens to a
user, and the fix. The four slice reports (`school_records`, `school_branches`,
`school_packages_entitlements`, `school_provisioning`) point here rather than
repeating it.

Baseline: see §0 below for the exact `Ran N tests` line and the command. This is
the slowest app in the repo, and `apps/settings/local.py:73` records why: two
`TransactionTestCase` classes tagged `slow` flush and rebuild the database around
every test and re-run the migration graph.

The eight findings marked **confirmed by execution** (§1, §2, §4, §6, §7, §8,
§12, §15) were reproduced against a real PostgreSQL test database in a throwaway
test module that was deleted afterwards - except §2, which was observed firing
repeatedly in the app's own suite output. Everything else is traced to file and
line.

Line references are relative to `apps/schools/vs_schools/` unless another app is
named. `Branch` and `BranchLifecycle` live in `apps/vs_tenants/models.py`.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in
the code.

---

## 0. Baseline

```
cd apps && DB_NAME=cx_schoolslice ../cx/Scripts/python.exe manage.py test \
    schools.vs_schools --settings=apps.settings.local --noinput
```

**`Ran 189 tests in 5224.847s` - OK.**

Eighty-seven minutes. `CLAUDE.md` records this app taking roughly eighteen, with
the note that two `TransactionTestCase` classes tagged `slow`
(`BranchCodeAllocationConcurrencyTests` and `_MigrationHarness` with its
subclasses) account for almost all of it while the other tests take about thirty
seconds. The figure above is from a contended run on this box and should be read
as an upper bound rather than a regression - but it is the number this pass
actually measured, and it is why the fast form
(`--exclude-tag=slow`) exists for iteration.

The traceback that appears repeatedly in that run is **not** a failure: it is
`provision_admin_user` refusing a roleless administrator and being swallowed,
which is §2 below.

The run is the full one - no `--exclude-tag=slow`, no `--keepdb` - because the
excluded classes are exactly the ones exercising the migration graph and the
branch code allocator, and a change touching either would slip past the fast
form.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | A school admin cannot see, create or edit their own branches - the four keys they hold are read by no view | **High** |
| 2 | A school can be created that nobody can sign in to, and the API reports 201 | **High** |
| 3 | Every seat and branch limit the plan sells is enforced nowhere | **High** |
| 4 | "Reset configuration" accepts any confirmation token and resets almost nothing | **High** |
| 5 | The reset endpoint carries neither the account-status gate, the surface gate, nor a permission key | Medium |
| 6 | `total_students` is a hardcoded zero on the school list and the school detail | Medium |
| 7 | The school stats endpoint has no suspended bucket, so its numbers stop adding up | Medium |
| 8 | The school list is N+1 on `main_branch`, despite prefetching branches | Medium |
| 9 | `SchoolStatus.INACTIVE` is filterable and counted, and no code path can produce it | Medium |
| 10 | `BranchLifecycle.actor_id` holds a user object's `str()`, a user id or a blank depending on the writer | Medium |
| 11 | Branch creation declares its primary admin optional and then requires it from inside `create()` | Medium |
| 12 | The create path checks a new slug against schools only; the update path also checks tenants | Medium |
| 13 | There is no way to view, correct or re-send a primary-admin invitation | Medium |
| 14 | Creating a second main branch is refused outright where updating performs a handover | Low |
| 15 | Four branch routes answer 200 for a school that does not exist | Low |
| 16 | `ContactInfo` rows are created unconditionally, so re-inviting the same person accumulates rows | Low |
| 17 | Smaller defects and dead code | Low |

---

## 1. A school admin cannot manage their own branches

**High. Confirmed by execution.**

### The defect

`seed_school_permissions` registers four branch keys and attaches every one of
them to the `school_admin` prebuilt role:

```python
# core/management/commands/seed_school_permissions.py:56-59
("school", "branches", "view",   _NORMAL,    (ROLE_SCHOOL_ADMIN, ROLE_BRANCH_ADMIN)),
("school", "branches", "create", _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
("school", "branches", "update", _NORMAL,    (ROLE_SCHOOL_ADMIN,)),
("school", "branches", "manage", _SENSITIVE, (ROLE_SCHOOL_ADMIN,)),
```

`seed_school_permission_groups` then bundles them into a named group
(`core/management/commands/seed_school_permission_groups.py:112-115`), so they
appear in the role editor as a coherent capability.

Every branch endpoint in the repo takes a `platform.branches.*` key instead:

```python
# views/branch.py:34, 113, 138, 158, 181
rbac_permission = "platform.branches.view"    # list
rbac_permission = "platform.branches.view"    # stats
rbac_permission = "platform.branches.create"  # create
rbac_permission = "platform.branches.view"    # detail
rbac_permission = "platform.branches.update"  # update
# views/lifecycle.py:33
rbac_permission = "platform.branches.manage"  # transition
```

`grep -rn "school.branches"` over the whole repo returns exactly two files, and
both are seeders. No view, serializer, service or export dataset reads any of the
four keys.

And a school role cannot simply be given the platform keys instead. The
`branches` resource is registered by `seed_platform_permissions` and is not in
`TENANT_HOLDABLE_KEYS`, so every `platform.branches.*` key is classified
`PermissionScope.PLATFORM` (`core/management/commands/seed_platform_permissions.py:123-132`,
`179-188`). `vs_rbac`'s grant guard refuses to write a PLATFORM key onto a role
inside a school tenant (`vs_rbac/models.py:91-111`, `630-640`).

The transition route goes further and adds `IsVisionStaff` on top
(`views/lifecycle.py:32`), which no school account can ever satisfy.

### What actually happens

Corona Secondary School opens a third site at Yaba. Their School Admin, Mrs
Balogun, holds `school.branches.create` - it is on her role by default, and the
role editor shows it grouped under branch management. She has no screen that
uses it. `GET /v1/i/corona/branches/?tenant=corona` is a 403, and so is every
other branch route.

To add a branch, Corona has to raise a support ticket and wait for CX. The same
is true of correcting a branch's address, changing its name, or promoting a
different site to main.

This is not a misconfiguration a role edit can fix: the keys that would work
cannot legally be held by a school role, and the keys that can be held are read
by nothing.

Confirmed by execution. A school admin granted all four keys, calling their own
school's branch routes with their own `?tenant=` slug:

```
PROBE1 keys the admin actually holds:
    ['school.branches.create', 'school.branches.manage',
     'school.branches.update', 'school.branches.view']
PROBE1 branch-list:        403
PROBE1 branch-stats:       403
PROBE1 branch-create:      403
PROBE1 branch-create POST: 403
```

### The fix

Decide which side of the boundary branch management sits on, and make the code
say it. The evidence points to "both":

- **Reading and editing a branch is a tenant operation.** Change the list, stats,
  detail, create and update views to accept an any-of list spanning both
  namespaces, exactly as `vs_rbac`'s own tenant-scoped views do
  (`vs_rbac/views.py:55-66`):

  ```python
  BRANCH_VIEW_KEYS   = ["school.branches.view",   "platform.branches.view"]
  BRANCH_CREATE_KEYS = ["school.branches.create", "platform.branches.create"]
  BRANCH_UPDATE_KEYS = ["school.branches.update", "platform.branches.update"]
  ```

- **The lifecycle transition stays platform-only.** Suspending or closing a site
  is a commercial action, the seeder describes `platform.branches.manage` as
  exactly that, and the view already says so in a comment
  (`views/lifecycle.py:26-33`). Leave it, and leave `IsVisionStaff` on it.

That change alone is not enough, because these routes are mounted under
`/v1/i/<slug>/` and reached with `?tenant=`. A school actor asserting their own
slug passes authentication, so the URL works - but the querysets filter on
`tenant__school_profile__slug=<url slug>` with no check that the caller's tenant
matches it. Add that check, in the shape `vs_rbac.TenantScopedRBACMixin` already
uses (`vs_rbac/views.py:83-88`): refuse when
`request.tenant.slug != kwargs["slug"]` unless the caller is on a platform
tenant. Without it, opening the school keys would let Corona list Bright Star's
branches by changing the path.

Then add the test the whole finding fell through: authenticate as a school admin
and assert each route.

---

## 2. A school can be created that nobody can sign in to

**High. Confirmed by execution** (observed in the app's own suite output).

### The defect

`provision_admin_user` refuses, correctly, to create an administrator with no
role:

```python
# services/admin_provisioning.py:112-116
if not role_obj:
    raise ValueError(
        f"Refusing to provision {email} without a role assignment. "
        f"Expected TenantRoleTemplate key={role!r} on tenant {getattr(tenant, 'slug', tenant)}."
    )
```

and then the function's own catch-all swallows the refusal:

```python
# services/admin_provisioning.py:170-178
except Exception as exc:  # noqa: BLE001
    logger.error("provision_admin_user: failed for %s - %s", email, exc, exc_info=True)
    return None
```

Both callers ignore the return value entirely (`serializers.py:558-565`,
`1066-1073`, `1125-1132`), so the school creation transaction continues through
books, the onboarding checklist and the audit event, and the endpoint answers
**201 Created**.

`role_obj` is `None` whenever `role` is falsy, because of the short-circuit at
`services/admin_provisioning.py:100-103`. And `role` is the empty string whenever
the prebuilt role template could not be provisioned:

```python
# serializers.py:563 and :1130
role=branch_admin_role.key if branch_admin_role else "",
```

`provision_role_from_prebuilt` returns `None` for a prebuilt key that is missing
or inactive (`vs_rbac/services.py:68-70`) - which is what happens on any
environment where `seed_prebuilt_role_templates` has not run, on a partial
restore, and in any test fixture that builds a school without seeding.

### What actually happens

This is not hypothetical. Running the app's own suite prints, repeatedly:

```
ValueError: Refusing to provision head@greenfield.test without a role assignment.
Expected TenantRoleTemplate key='' on tenant greenfield.
```

Note `key=''` - the empty string, exactly as described above.

In production the shape is: CX creates Greenfield College. The response is 201
with a complete school detail payload - tenant, branches, entitlements,
`primary_admin` block and all. Greenfield has a tenant, sites, books, a checklist
and module entitlements.

It has no `User`. The `SchoolPrimaryAdmin` row sits at `QUEUED`. No invitation
email was sent. Nobody at Greenfield can sign in, and nobody at CX knows, because
the only signal is a line in the application log.

And there is no remedy through the API. There is no endpoint to read a school's
primary-admin invite status, no endpoint to correct the address, and no endpoint
to re-send (§13). The school has to be repaired from a shell.

The same swallow covers every other failure mode: a duplicate-email race, an
`InvitationService` failure, a broker outage when queueing the email. All of them
produce a 201 and a dead admin link.

### The fix

Separate "this failure is survivable" from "this failure means the school is
unusable". A school with no books can be repaired by a command and is genuinely
survivable; a school with no administrator is not.

1. **Make the caller check.** `provision_admin_user` already returns `None` on
   failure and a `User` on success. Have both create serializers raise when the
   *school-level* admin could not be provisioned, so the transaction rolls back
   and the caller gets a 400 naming the cause:

   ```python
   admin = provision_admin_user(...)
   if admin is None:
       raise serializers.ValidationError({
           "primary_admin_data": "Could not provision the administrator account. "
                                 "The school was not created.",
       })
   ```

2. **Stop passing an empty role.** `role=x.key if x else ""` turns a missing
   prebuilt into a confusing downstream error. Raise at the point the prebuilt
   is missing, where the message can name it:

   ```python
   if school_admin_role is None:
       raise serializers.ValidationError({
           "detail": "The 'school_admin' prebuilt role is not seeded. "
                     "Run manage.py seed_prebuilt_role_templates.",
       })
   ```

3. **Write `InviteStatus.FAILED`.** The choice exists and nothing sets it
   (§17). Stamping it in the `except` block makes a failed invite visible to any
   query, which is a prerequisite for §13's repair endpoint.

Whether a *branch*-level admin failure should also be fatal is a judgement call
worth making explicitly rather than by omission - a school with one working
administrator and one dead branch invite is arguably survivable, but only if
somebody can see it.

---

## 3. Every seat and branch limit the plan sells is enforced nowhere

**High.**

### The defect

`PackagePlan` carries four ceilings (`models.py:412-415`), and the four seeded
plans give them real values (`management/commands/seed_package.py:28-85`):

| Plan | students | teachers | admins | branches |
|---|---|---|---|---|
| Basic | 200 | 20 | 3 | 1 |
| Standard | 800 | 60 | 10 | 5 |
| Premium | 3000 | 200 | 30 | 20 |

Three of them are checked - twice - against the numbers an operator types into
the creation wizard:

```python
# serializers.py:233-246  (SchoolPackageSetupWriteSerializer.validate)
if plan.max_students is not None and attrs["student_capacity"] > plan.max_students: ...
# models.py:479-506  (SchoolPackageSetup.clean)
if self.package_plan.max_students is not None and self.student_capacity > ...: ...
```

Those checks compare a number against a number. Neither compares against the
rows that actually exist, and no other code in the repo reads
`student_capacity`, `teacher_capacity` or `admin_capacity` at all:

```
$ grep -rn "student_capacity\|teacher_capacity\|admin_capacity" --include="*.py" .
  → schools/vs_schools/  (definition, validation, serialization)
  → core/management/commands/seed_dev_data.py  (writes them)
  → core/management/commands/seed_import.py    (import template columns)
  → vs_import_data/  (writes them, defaults 50/10/3)
```

No user-creation path, no student-creation path and no teacher-creation path
consults any of them.

`max_branch` is worse: there is no `branch_capacity` field for it to be validated
against, so it is not even checked at the wizard. `BranchCreateSerializer`
(`serializers.py:472-502`) never looks at the plan, and neither does the inline
branch loop in `SchoolCreateSerializer.create` (`serializers.py:1076-1132`).

`subscription_expires_at` completes the picture: it is required and validated as
not-in-the-past, and no sweep, task or gate reads it afterwards.

### What actually happens

Greenfield College buys **Basic**: 200 students, 20 teachers, 3 admins, 1 branch,
and CX prices it accordingly.

Greenfield is created with a main branch. Their School Admin then works through
the year: 1,400 students imported by spreadsheet, 90 teachers invited, 11 admin
accounts, and - via a support ticket, since §1 means they cannot do it
themselves - four extra sites.

Every one of those succeeds. Nothing warns anybody. A year later the
subscription expires and nothing happens either. The commercial model the
platform sells and the platform's actual behaviour have no connection.

### The fix

This is one root cause with four surfaces, so fix it at a choke point rather
than four times over.

1. **Give the plan a `branch_capacity` on the setup row**, so `max_branch` has
   something to be validated against at the wizard, exactly like the other
   three. That is a migration plus four lines in two `validate`/`clean` methods.

2. **Add one service that answers the question**, in this app, and call it from
   the three create paths:

   ```python
   # services/capacity.py
   def assert_capacity(tenant, *, kind):   # "student" | "teacher" | "admin" | "branch"
       setup = SchoolPackageSetup.objects.filter(school__tenant=tenant, is_active=True).first()
       if setup is None:
           return                     # no package sold: no ceiling to enforce
       cap, used = _cap_and_usage(setup, kind)
       if cap is not None and used >= cap:
           raise CapacityExceeded(kind=kind, cap=cap, plan=setup.package_plan.name)
   ```

   Call it from `BranchCreateSerializer.validate` and from the inline branch loop
   for `branch`; the student, teacher and admin counts belong in the apps that own
   those rows (`vs_user`, and the school apps that own students and teachers), so
   this app should **export** the check rather than reach into them.

3. **Decide what expiry means** and either implement it - a nightly sweep that
   flips `is_active` and revokes `source=PACKAGE` entitlements through
   `vs_config` - or drop the field and say in the model docstring that renewal
   lives in `vs_config`'s capability surfaces
   (`docs/config/config_capabilities_entitlements.md`), which is where the
   renewal calendar and bulk scheduling already are.

If the decision is that capacities are advisory and CX enforces them
commercially, that is a legitimate answer - but it needs to be written in the
model docstring and the ceilings should stop being presented in the wizard as if
they bind.

---

## 4. "Reset configuration" accepts any token and resets almost nothing

**High. Confirmed by execution.**

### The defect

Two separate problems in one twelve-line method.

**The token confirms nothing:**

```python
# serializers.py:1509-1511
token = (self.validated_data.get("confirmation_token") or "").strip()
if not token:
    raise serializers.ValidationError({"confirmation_token": "Confirmation token is required."})
```

It is checked for emptiness and then never used again. Nothing derives an
expected value from the school, its slug, the actor, a nonce or a previously
issued challenge. `"x"` passes. So does `" a "`.

**The reset resets one thing:**

```python
# serializers.py:1497-1501
"""
Resets school configuration to baseline (branding/modules/localization),
without deleting core operational data (policy-driven).
"""
```

```python
# serializers.py:1517-1524
branding = SchoolBranding.objects.filter(school=school).first()
before_data = {"logo": str(branding.logo) if branding and branding.logo else ""}
SchoolBranding.objects.filter(school=school).delete()
```

That is the entire body. `CapabilityEntitlement` rows are untouched.
`SchoolPackageSetup` is untouched. `term_structure` and `currency` - the
localization - are untouched. The comment block directly beneath the docstring
even lists the three things as an "example" of what a baseline reset would do
(`serializers.py:1513-1516`), which reads as a plan rather than a description.

The audit summary is honest - *"Configuration reset for X: branding cleared"* -
so the trail says one thing while the endpoint name, the docstring and the API
contract say another.

### What actually happens

Two failure directions, and both are bad.

**A caller who believes the docstring.** CX support is asked to strip a school
back to defaults before a re-onboarding. They call `reset-config`, see a 200 and
a full school payload, and report it done. The school keeps every module
entitlement it was ever granted, its package row, its term structure and its
currency. Nobody finds out until the school complains that a module they should
have lost is still there.

**A caller who does not.** `confirmation_token` exists to make a destructive
action deliberate. Any client can satisfy it with a literal - and any client
that has been written against this endpoint almost certainly does, because there
is no documented way to obtain a real one. The guard is theatre, and worse than
no guard, because reviewers see a token and assume the action is confirmed.

### The fix

Pick one meaning and implement it.

**If the endpoint should only clear branding**, rename it - `clear-branding` -
fix the docstring, and drop `confirmation_token` or make it real. A real token
for a single-school destructive action is conventionally the thing being
destroyed:

```python
if token != school.slug:
    raise serializers.ValidationError({
        "confirmation_token": f"Type the school's address ({school.slug}) to confirm.",
    })
```

**If it should reset configuration**, implement the other two and audit each
separately: revoke `source=PACKAGE` entitlements through
`vs_config.set_entitlement` (never by deleting rows - `vs_config` owns that
write, see `school_packages_entitlements` §1), and reset `term_structure` and
`currency` to the platform onboarding defaults the create path already reads
(`vs_config.platform_settings.get_school_onboarding_defaults`).

Either way, add the test: call it with a wrong token and assert a 400.

---

## 5. The reset endpoint has the thinnest gate in the app

**Medium.**

### The defect

```python
# views/ops.py:43-48
class SchoolResetConfigView(_SchoolOpBaseView):
    """docstring-name: Reset school configuration"""
    permission_classes = [IsVisionSuperAdmin]

    def post(self, request, *args, **kwargs):
        return self._run(request, SchoolResetConfigSerializer)
```

That list is the whole gate. Compare every other write in this app, which carries
`IsAuthenticatedAndActive & HasRBACPermission` plus a named key
(`views/school.py:115-116`, `views/branch.py:137-138`, `views/lifecycle.py:32-33`).

Three consequences, and DRF's semantics are what make them consequences:
`permission_classes` **replaces** `DEFAULT_PERMISSION_CLASSES` rather than adding
to it, which is precisely why `vs_rbac` installs its surface gate in four places
(`vs_rbac/permissions.py:123-131`).

- **No account-status check.** `IsAuthenticatedAndActive` is what raises for
  `SUSPENDED`, `LOCKED` and `DEACTIVATED` accounts
  (`vs_rbac/permissions.py:218-224`). It is not here, so a suspended super admin
  still passes.
- **No pending-tenant surface check.** `TenantSurfaceAllowed` is reached only
  through `IsAuthenticatedAndActive`, `HasRBACPermission` or
  `HasAnyModuleAccess`, none of which are here.
- **No permission key.** There is no `rbac_permission`, so the action does not
  appear in the permission registry, cannot be granted or revoked, and cannot be
  audited as a grant.

And `IsVisionSuperAdmin` is `is_vision_super_admin(request.user)`, which
`error/rbac/rbac_code_issues.md` §1 records as not checking the tenant's kind at
all. `_SchoolOpBaseView.queryset` is `School.objects.all()`
(`views/ops.py:25`), so the endpoint is unfenced by school as well.

### What actually happens

A CX super admin leaves the company. Their account is suspended in `vs_user`
while the Super Admin transfer is arranged. Every other screen refuses them.
This one does not: they can still wipe any school's branding, and the audit trail
records it as a legitimate action by a named actor.

Separately, and worse: the counterfeit `xvs_super_admin` role described in
`error/rbac/rbac_code_issues.md` §1 opens this endpoint for **every school on the
platform**, because there is no tenant fence and no key to be scope-guarded.

### The fix

Bring it in line with the rest of the app:

```python
permission_classes = [IsAuthenticatedAndActive & IsVisionSuperAdmin & HasRBACPermission]
rbac_permission = "platform.schools.manage"
```

`platform.schools.manage` already exists, is seeded as `SENSITIVE` and restricted,
and is described as "Full school lifecycle administration"
(`core/management/commands/seed_platform_permissions.py:120`) - it is used by no
view today, and this is what it is for.

Keeping `IsVisionSuperAdmin` alongside the key is the same belt-and-braces the
branch transition view uses, and worth keeping for a destructive action.

---

## 6. `total_students` is a hardcoded zero

**Medium. Confirmed by execution.**

### The defect

```python
# serializers.py:730  (SchoolListSerializer)
total_students = serializers.ReadOnlyField(default=0)
# serializers.py:760  (SchoolDetailSerializer)
total_students = serializers.ReadOnlyField(default=0)
```

`ReadOnlyField` reads the attribute named by the field from the instance.
`School` has no `total_students` attribute, property or annotation - `grep`
finds the name in exactly these four lines (two declarations, two entries in
`Meta.fields`). So the field always falls back to its default.

Both serializers are `read_only_fields = fields`, so nothing can supply it from
the outside either.

### What actually happens

The School Management list is a table with a "Students" column, and the school
detail screen is built from a payload carrying `total_students`. Both read `0`
for every school on the platform, for ever - including the school with nine
hundred students. An operator comparing schools by size sees a column of zeros
and either stops trusting the screen or concludes the platform has no students.

### The fix

Annotate it in the two querysets rather than computing it per row, so the list
stays one query:

```python
# views/school.py, on both SchoolListView and SchoolDetailView querysets
.annotate(total_students=Count("tenant__students", distinct=True))
```

substituting whatever the real student model's path to the tenant is - the
student model lives in the school apps under `apps/schools/`, not here, so this
app must not import it directly. If the count needs a filter (enrolled only,
current session only), that filter is a product decision and belongs in the
annotation with a comment saying which.

If students are not yet modelled at all, remove the field from both serializers.
A column that is always zero is worse than an absent one, because it looks like
data.

---

## 7. The school stats endpoint has no suspended bucket

**Medium. Confirmed by execution.**

### The defect

```python
# views/school.py:103-108
result = School.objects.aggregate(
    all=Count("slug"),
    active=Count("slug", filter=Q(status=SchoolStatus.ACTIVE)),
    pending=Count("slug", filter=Q(status=SchoolStatus.PENDING)),
    inactive=Count("slug", filter=Q(status=SchoolStatus.INACTIVE)),
)
```

`SchoolStatus` has four values (`models.py:45-53`). Three are counted.
`SUSPENDED` is not.

The sibling endpoint gets this right: `BranchStatsView` counts all five branch
statuses (`views/branch.py:123-130`), so the branch dashboard's numbers add up
and the school dashboard's do not.

`SUSPENDED` is not a theoretical value. `schools/vs_onboarding/services/lifecycle.py:192`
writes it when a school's onboarding expires, which is an automated sweep.

### What actually happens

The School Management dashboard shows four stat cards: All 47, Active 32,
Pending 8, Inactive 0. 32 + 8 + 0 = 40, not 47. Seven schools are simply absent
from the breakdown, and they are the seven that most need attention - the ones
whose onboarding lapsed.

Confirmed by execution, with one ACTIVE school and one SUSPENDED school on the
platform:

```
PROBE7 stats: {'all': 2, 'active': 1, 'pending': 0, 'inactive': 0}
```

`1 + 0 + 0 = 1`, and `all` says 2.

An operator reading the cards concludes seven schools are unaccounted for, or
does not notice at all. There is no "Suspended" card to click into, and the list
endpoint has no `?suspended=` shortcut either (it has `?active=` and
`?inactive=` and nothing else) - though `?status=SUSPENDED` does work.

### The fix

Add the bucket, and while there, make the two stats endpoints derive their
buckets from the choices so a new status cannot be silently dropped again:

```python
result = School.objects.aggregate(
    all=Count("pk"),
    **{
        value.lower(): Count("pk", filter=Q(status=value))
        for value in SchoolStatus.values
    },
)
```

That also fixes `Count("slug")`, which is a leftover from when `slug` was the
primary key (`models.py:147-150`) and counts non-null slugs rather than rows -
harmless today because of the `slug_not_empty` constraint, but it is counting
the wrong thing.

Add a `?suspended=` shortcut to `SchoolListView` for symmetry with the branch
list, which has one for every status (`views/branch.py:49-67`).

---

## 8. The school list is N+1 on `main_branch`

**Medium. Confirmed by execution.**

### The defect

`SchoolListView` prefetches the branches, and names the real path because
`School.branches` is a property over the tenant's sites:

```python
# views/school.py:39-45
queryset = (
    School.objects.all()
    .select_related("branding")
    .prefetch_related("tenant__branches")
)
```

`SchoolListSerializer` then reads `main_branch` (`serializers.py:729`), and
`main_branch` builds a **fresh queryset**:

```python
# models.py:363-374
@property
def main_branch(self):
    return (
        self.branches
        .select_related("primary_admin", "primary_admin__contact")
        .filter(is_main=True)
        .first()
    )
```

`.select_related(...).filter(...)` on the prefetched manager discards the
prefetch cache and issues a new query. One per school, per page.

`SchoolDetailView` and `SchoolUpdateView` pay it too, but once, on one row - and
their `Prefetch` of `tenant__branches` with `primary_admin` select-related
(`views/school.py:135-142`) is doing exactly the work `main_branch` then throws
away, so those two views issue the branch query twice.

### What actually happens

A page of 25 schools costs 25 extra queries, each with a two-table join, purely
to find a row already sitting in memory. On the platform's largest page size
(`?page_size=100`) that is 100. Nothing fails; the screen is just slower than it
looks, and the cost grows with the school count rather than the page count.

Confirmed by execution, counting queries on a page of five schools:

```
PROBE8 schools on page: 5   queries: 10
PROBE8 queries mentioning is_main: 6
```

Five of those six `is_main` queries are `main_branch` re-querying per row; the
prefetch that should have answered them is one of the other four.

`SchoolDetailSerializer` also nests `branches` **and** `main_branch`
(`serializers.py:755-756`), so the main branch is serialized twice in every
detail payload - once inside the list and once on its own.

### The fix

Resolve it from the prefetch instead of re-querying:

```python
@property
def main_branch(self):
    # `.all()` reuses the prefetch cache when one exists; the filter is in
    # Python so it cannot discard it.
    for branch in self.branches.all():
        if branch.is_main:
            return branch
    return None
```

and put the `select_related` the property wanted onto the prefetch itself, which
`SchoolDetailView` already does (`views/school.py:136-141`) and
`SchoolListView` does not:

```python
.prefetch_related(Prefetch(
    "tenant__branches",
    queryset=Branch.objects.select_related("primary_admin", "primary_admin__contact"),
))
```

A school has a handful of branches, so scanning them in Python is cheaper than a
query. Add a `assertNumQueries` test on the list, because nothing currently
counts queries anywhere in this app.

---

## 9. `SchoolStatus.INACTIVE` is filterable, counted and unreachable

**Medium.**

### The defect

`SchoolStatus.INACTIVE` is a declared choice (`models.py:47`), and three surfaces
treat it as real:

```python
# views/school.py:59-61
inactive_param = (self.request.query_params.get("inactive") or "").strip().lower()
if inactive_param in ("1", "true", "yes"):
    qs = qs.filter(status=SchoolStatus.INACTIVE)
# views/school.py:107
inactive=Count("slug", filter=Q(status=SchoolStatus.INACTIVE)),
# models.py:302 - the tenant mirror maps it to Tenant.Status.INACTIVE
```

It is also a filter choice on the export dataset, through
`choice_labels("schools.vs_schools.models.SchoolStatus")`
(`export_datasets.py:49`).

No code path writes it. Searching the repo for writers of `School.status` finds
four, and none of them is INACTIVE:

```
core/management/commands/seed_dev_data.py:327   → ACTIVE
schools/vs_onboarding/services/go_live.py:204   → ACTIVE
schools/vs_onboarding/services/lifecycle.py:192 → SUSPENDED
schools/vs_onboarding/services/lifecycle.py:473 → PENDING
```

plus `SchoolCreateSerializer.create`, which writes PENDING
(`serializers.py:1026-1029`). `SchoolUpdateSerializer` does not expose `status`
at all (`serializers.py:1293-1307`), so there is no API route to it either.

`deactivated_at` is in the same position: mirrored onto the tenant
(`models.py:339`), exported (`export_datasets.py:89`), and written only by
`vs_onboarding`'s suspension path alongside SUSPENDED - never alongside
INACTIVE.

### What actually happens

Nothing visible - which is the problem. The dashboard has an "Inactive" card that
is structurally always zero, the list has an `?inactive=true` filter that always
returns nothing, and the export has a status facet with a value no row can carry.

The real risk is the next change: somebody adding a "deactivate school" feature
will reasonably assume the INACTIVE plumbing works, wire a button to it, and
discover only in production that `School.save()` mirrors INACTIVE onto
`Tenant.Status.INACTIVE`, which is **not** in `Tenant.AUTHENTICABLE_STATUSES`
(`vs_tenants/models.py:97`) - so every user at that school is locked out
immediately, with no warning anywhere in this app that that is what the value
means.

### The fix

Two honest options.

**Implement it.** Add the transition to `vs_onboarding`'s lifecycle service
beside suspension and reinstatement, which is where school status already lives,
and stamp `deactivated_at` with it. Document in `SchoolStatus` that INACTIVE
locks every user out, because that is the consequence and nothing currently says
so.

**Or remove it.** Drop the choice, the filter, the stat bucket and the mirror
entry, and let SUSPENDED be the one off-state. That is a migration on a column
with no rows carrying the value, so it is cheap.

Either way, `SchoolStatus` deserves the same treatment `BranchStatus` got: a
docstring saying which states are reachable, from where, and what each one means
for sign-in.

---

## 10. `BranchLifecycle.actor_id` holds three different kinds of value

**Medium.**

### The defect

The column is a plain string:

```python
# vs_tenants/models.py:724
actor_id = models.CharField(max_length=120, blank=True, default="")
```

Three writers put three different things in it.

**The API create paths put a `User` object**, which Django coerces with `str()`:

```python
# serializers.py:523-529  (BranchCreateSerializer.create)
BranchLifecycle.objects.create(
    branch=branch, from_state="", to_state=BranchStatus.PENDING,
    actor_id=self.context.get("actor_id", "system"),
    reason="Branch created",
)
```

`ActorContextMixin` sets `ctx["actor_id"] = self.request.user`
(`views/branch.py:25-28`, `views/school.py:25-29`,
`views/lifecycle.py:16-21`), and `vs_user.User.__str__` is
`f'{self.email} ({self.status})'` (`vs_user/models.py:620-621`). So the stored
value is `"ada@brightstar.test (ACTIVE)"`.

The same is true of the inline branch loop (`serializers.py:1099`) and of the
transition path, which passes the context value into `Branch.transition`
(`serializers.py:1478`, `1492`).

**System paths put a blank.** `Branch.transition` writes `str(actor_id or "")`,
so the onboarding sweep and management commands store `""`
(`vs_tenants/models.py:720-724`).

**The bulk importer puts a user id string.** The school create serializer's audit
call carries a comment recording exactly this: *"the API views put a User in it
but the bulk importer puts `str(user.id)`, and a string here makes
`emit_audit_event` swallow the event and write nothing"*
(`serializers.py:1143-1146`). That comment is about `actor_user`, and it is
evidence that the two callers disagree about what `context["actor_id"]` holds.

### What actually happens

Bright Star's branch timeline is queried for "everything Ada did". There is no
way to write that query. Some rows say `ada@brightstar.test (ACTIVE)`, some say
`41`, some say `""`. A join to `vs_user.User` is impossible. Filtering by actor
requires knowing which writer produced the row, which is not recorded.

Worse, the stored string embeds the account's status **at write time**, so once
Ada is suspended her old rows still read `(ACTIVE)` and her new ones read
`(SUSPENDED)` - two different actor identities for one person.

The `max_length=120` is also a live risk: an email plus `" (DEACTIVATED)"` is 15
characters of overhead, so a long address truncates or raises.

### The fix

Make the column mean one thing. The cheapest version that fixes attribution
without a migration is to normalise at the two write sites and in `transition`:

```python
def _actor_ref(actor) -> str:
    pk = getattr(actor, "pk", None)
    return str(pk) if pk is not None else ""
```

and use it everywhere `actor_id` is written. Existing rows stay mixed, so the
field's docstring should say the format changed and when.

The better version, since `BranchLifecycle` is already a first-class audit table
with two indexes: add a nullable `actor` FK to `AUTH_USER_MODEL` with
`on_delete=SET_NULL` beside the string, populate it going forward, and leave
`actor_id` as the legacy free-text column for the rows that predate it. That is
the shape `vs_rbac.RBACAuditLog` already uses (`vs_rbac/models.py:1106-1113`).

While there: `BranchStateTransitionSerializer` defaults `actor_id` to the
literal string `"system"` (`serializers.py:1478`), which is a fourth value and
is indistinguishable from a real actor named `system`.

---

## 11. Branch creation declares its primary admin optional and then requires it

**Medium.**

### The defect

The field is declared optional:

```python
# serializers.py:452
primary_admin_data = BranchPrimaryAdminWriteSerializer(required=False, write_only=True)
```

`validate()` treats it as optional too - it only runs the email check when the
block is present (`serializers.py:484-500`).

And then `create()` requires it, after five writes have already happened:

```python
# serializers.py:505-567
branch = Branch.objects.create(...)          # 1
branch.save(update_fields=["opened_at", ...])# 2
BranchLifecycle.objects.create(...)          # 3
branch_admin_role = provision_role_from_prebuilt(...)   # 4 (+ its permission rows)
if primary_admin_data:
    ...                                      # 5
else:
    raise serializers.ValidationError({"primary_admin_data": "Primary admin information is required to create a branch."})
```

The method is `@transaction.atomic`, so nothing is left behind. But the contract
is wrong in three places at once: the serializer field says optional, any
generated API schema says optional, and `validate()` - the layer whose job this
is - says nothing.

The inline path gets it right: `BranchInlineCreateSerializer` declares
`primary_admin_data = BranchPrimaryAdminWriteSerializer(required=True)`
(`serializers.py:833`).

### What actually happens

A client written against the schema omits the block and gets a 400 - which is
the correct outcome by luck rather than design. The error arrives as a
`ValidationError` raised from `create()`, so it is a 400 with a field key, but
it is raised *after* the branch code has been allocated under a tenant-row lock
and a role template has been created and rolled back. Under load, that is a lock
taken and released for a request that was never going to succeed.

It also means the two branch creation paths disagree about their own contract,
which is exactly the kind of drift that produces a third path later.

### The fix

One line, and delete four:

```python
primary_admin_data = BranchPrimaryAdminWriteSerializer(required=True, write_only=True)
```

then remove the `else: raise` from `create()`. The message is worth keeping, so
attach it to the field:

```python
primary_admin_data = BranchPrimaryAdminWriteSerializer(
    required=True, write_only=True,
    error_messages={"required": "Primary admin information is required to create a branch."},
)
```

which matches how `SchoolCreateSerializer` states the same kind of rule for
`branches` (`serializers.py:865-875`).

---

## 12. The create path checks a new slug against schools only

**Medium. Confirmed by execution.**

### The defect

`_slug_is_unique` looks at one table:

```python
# serializers.py:52-56
def _slug_is_unique(slug, exclude_school_slug=None):
    qs = School.objects.all()
    if exclude_school_slug:
        qs = qs.exclude(slug=exclude_school_slug)
    return not qs.filter(slug=slug).exists()
```

Both create-path checks use it and nothing else (`serializers.py:907`, `935`).

The update path adds a second check, and its comment explains precisely why:

```python
# serializers.py:1346-1356
# The school's slug is mirrored onto its tenant by ``School.save()``,
# and that mirror is a queryset ``update()`` - it cannot raise a field
# error, only an IntegrityError against the tenant's own unique index.
# A clinic group or an ORGANIZATION tenant holding the name is enough
# to trigger it, and there is no school row to have caught it above.
if Tenant.objects.filter(slug=normalized).exclude(pk=self.instance.tenant_id).exists():
    raise serializers.ValidationError(
        "This address is already in use on the platform. Choose another."
    )
```

The create path has no equivalent - and it needs one more than the update path
does, because `School.save()` **creates** the tenant there
(`models.py:271-287`), so the collision is on an `INSERT` rather than an
`UPDATE`.

`Tenant.slug` is `unique=True` (`vs_tenants/models.py:100-102`), and `Tenant`
has three kinds: `PLATFORM`, `SCHOOL`, `ORGANIZATION`
(`vs_tenants/models.py:81-84`). VIGIL is described in `CLAUDE.md` as a second
domain on the same foundation, so non-school tenants exist by design.

### What actually happens

A clinic group is onboarded on VIGIL as tenant `stella-maris`. Months later CX
onboards Stella Maris Secondary School. The slug `stella-maris` is free among
schools, so validation passes with no suggestions offered.

`School.save()` then runs `Tenant.objects.create(slug="stella-maris", ...)`,
which violates the unique index. `core/exceptions.py:145-151` recognises a unique
violation and returns a **400** reading *"A record with these details already
exists."* - with no field key, no mention of the slug, and none of the
`-2`, `-3` suggestions the school-collision path provides
(`serializers.py:908-912`).

The operator is told something already exists, cannot tell what, and cannot tell
which field to change. The nine-step creation transaction has rolled back, so
nothing is half-created - but they have to guess their way to a working slug.

Confirmed by execution. With an `ORGANIZATION` tenant already holding
`stella-maris`, `POST /v1/i/create/` with that slug returns:

```
PROBE12 status: 400
  {'success': False, 'message': 'A record with these details already exists.',
   'error': {'code': 'DUPLICATE'}}
```

No field key, no slug, no suggestions.

### The fix

Reuse the check the update path already has. It belongs in the helper, so both
paths get it:

```python
def _slug_is_unique(slug, exclude_school_slug=None, exclude_tenant_pk=None):
    schools = School.objects.all()
    if exclude_school_slug:
        schools = schools.exclude(slug=exclude_school_slug)
    if schools.filter(slug=slug).exists():
        return False
    tenants = Tenant.objects.filter(slug=slug)
    if exclude_tenant_pk is not None:
        tenants = tenants.exclude(pk=exclude_tenant_pk)
    return not tenants.exists()
```

That makes the create path's suggestion list correct too, since
`_build_slug_suggestions` filters through the same helper
(`serializers.py:936`, `1342`) - today a suggested `-2` slug could itself be
taken by a non-school tenant.

Two related gaps worth closing in the same change:

- `School.clean()` checks the reserved list but not tenant uniqueness
  (`models.py:194-197`), so a shell write hits the same `IntegrityError`.
- `Tenant.save()` enforces the reserved rule itself
  (`vs_tenants/models.py:76-78`), so the reserved check is triple-covered while
  the uniqueness check is single-covered on the path that most needs it.

---

## 13. There is no way to view, correct or re-send a primary-admin invitation

**Medium.**

### The defect

`SchoolPrimaryAdmin` and `BranchPrimaryAdmin` carry `invite_status`,
`invite_queued_at` and `invite_sent_at` (`models.py:559-566`, `596-603`), and
both are indexed on `(…, invite_status)` - so the schema is built for querying
pending invites.

Nothing queries them. The two read serializers are nested, read-only blocks on
the school and branch detail payloads
(`serializers.py:320-327`, `344-351`, `758`, `407`), and there is no route that
returns invite status on its own, no route that updates a link, and no route that
re-triggers `provision_admin_user`.

`SchoolUpdateSerializer` does not expose `primary_admin_data`
(`serializers.py:1293-1307`). `BranchUpdateSerializer` does not either
(`serializers.py:620-633`). Both accept it only at **creation**.

`InviteStatus.FAILED` is never written (§17), so even the nested read cannot
distinguish "still queued" from "failed and abandoned".

### What actually happens

This is the remedy §2 does not have. Greenfield is created, `provision_admin_user`
fails, the link sits at QUEUED and the school detail payload dutifully shows:

```json
"primary_admin": {"invite_status": "QUEUED", "invite_sent_at": null, ...}
```

An operator who happens to open that school and read that block can *see* the
problem. They can do nothing about it. There is no re-send. There is no way to
fix a mistyped address - the contact row is not editable through any endpoint.
The only route back is a shell, or deleting and re-creating the school, which is
not possible either because there is no delete.

The same applies to the far more ordinary case: the admin's email was typed
wrong at onboarding. The invitation went to a dead address, the link says SENT,
and nothing in the product can correct it.

### The fix

Two small endpoints, both scoped to a school the caller may already reach:

```
GET  /v1/i/<slug>/primary-admin/            invite status + contact
PATCH /v1/i/<slug>/primary-admin/           correct full_name / email / phone
POST /v1/i/<slug>/primary-admin/resend/     re-run provisioning
```

and the branch equivalents under `<slug>/branches/<code>/`. `platform.schools.update`
and `platform.branches.update` are the natural keys, with the school-side any-of
from §1 once that lands.

The resend action is `provision_admin_user` itself - it is already idempotent
(the tenant-scoped existence probe at `services/admin_provisioning.py:86-96`
stamps the link SENT and returns the existing user), so calling it again on a
QUEUED link is safe.

Correcting the address is the one that needs care: if a `User` was already
created, changing the contact must not orphan it. Refuse the edit when the link
is SENT and a matching user exists, and offer the `vs_user` account-edit path
instead.

---

## 14. Creating a second main branch is refused where updating hands over

**Low.**

### The defect

Create refuses:

```python
# serializers.py:478-482
school = self.context.get("school")
is_main = attrs.get("is_main", False)
if school and is_main:
    if Branch.all_objects.filter(tenant=school.tenant, is_main=True).exists():
        raise serializers.ValidationError({"is_main": "This school already has a main branch."})
```

Update hands over:

```python
# serializers.py:637-643
# ``is_main=true`` used to be refused whenever another main branch
# existed, which made promotion impossible for every school that had
# one … It is now a handover: ``Branch.promote_to_main`` demotes the
# incumbent in the same transaction.
```

So the exact same intent - "this new site should be the main one" - is a
handover through one endpoint and a hard refusal through the other, and the
refusal's message names no way forward.

### What actually happens

Corona opens a flagship site at Victoria Island and wants it to be the main
branch. The obvious request is
`POST /v1/i/corona/branches/create/ {"name": "Victoria Island", "is_main": true}`.
It fails with *"This school already has a main branch."*

The working sequence is: create it without `is_main`, then
`PATCH .../branches/<code>/update/ {"is_main": true}`. Nothing in the error says
so. An operator who does not know the update path exists concludes the platform
cannot move a main branch - which is exactly the dead end the update-path comment
says was fixed.

### The fix

Either make create hand over too - `promote_to_main` is already idempotent and
transactional, so calling it after the create is two lines - or leave the refusal
and make the message carry the route:

```python
raise serializers.ValidationError({"is_main": (
    "This school already has a main branch. Create this branch first, then "
    "make it the main branch from its update endpoint - the current main "
    "branch is demoted automatically."
)})
```

The handover version is better, because it removes a two-step dance for a
single-intent request. It is safe: `_assert_may_leave_service` does not apply
(nothing is leaving service), and the partial unique index is satisfied at every
point because `promote_to_main` demotes first (`vs_tenants/models.py:585-592`).

---

## 15. Four branch routes answer 200 for a school that does not exist

**Low. Confirmed by execution.**

### The defect

The branch views filter through a join and never check that the join found
anything:

```python
# views/branch.py:38-42  (list)
qs = qs.filter(tenant__school_profile__slug=self.kwargs.get("slug"))
# views/branch.py:120-121  (stats)
i_slug = self.kwargs.get("slug")
qs = qs.filter(tenant__school_profile__slug=i_slug)
```

`BranchDetailView` and `BranchUpdateView` do the same
(`views/branch.py:166-171`, `189-194`) - though those two then 404 through
`get_object()`, which is correct by accident rather than by check.

`BranchCreateView` is the only one that verifies the school:

```python
# views/branch.py:145-149
school = School.objects.filter(slug=i_slug).first()
if not school:
    raise NotFound(f"School with slug '{i_slug}' does not exist.")
```

`BranchTransitionView` has the same shape as the detail views
(`views/lifecycle.py:38-47`).

### What actually happens

`GET /v1/i/does-not-exist/branches/?tenant=codex` returns **200** with an empty
paginated list. `GET /v1/i/does-not-exist/branches/stats/?tenant=codex` returns
**200** with `{"all": 0, "active": 0, "pending": 0, "suspended": 0, "inactive":
0, "closed": 0}`.

A typo in a slug is indistinguishable from a school that genuinely has no
branches - except that no school genuinely has no branches, because
`SchoolCreateSerializer` requires at least one (`serializers.py:865-875`). So
an all-zero stats response *always* means a bad slug, and the API reports it as
success.

Confirmed by execution, against a slug no school holds:

```
PROBE15 list:  200  {'success': True, 'message': 'Data retrieved successfully', ...}
PROBE15 stats: 200  {'all': 0, 'active': 0, 'pending': 0,
                     'suspended': 0, 'inactive': 0, 'closed': 0}
```

A frontend built on this shows an empty branch table with no error, and an
operator concludes the school lost its branches.

### The fix

Resolve the school once, in a shared mixin, exactly as `BranchCreateView`
already does - and as `vs_rbac.TenantScopedRBACMixin` does for its own slug
(`vs_rbac/views.py:78-88`):

```python
class SchoolScopedBranchMixin:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.school = School.objects.filter(slug=self.kwargs["slug"]).first()
        if self.school is None:
            raise NotFound("No school matches the requested context.")

    def get_queryset(self):
        return super().get_queryset().filter(tenant=self.school.tenant)
```

That also replaces five copies of the same `tenant__school_profile__slug` join
with one `tenant=` filter, which is one fewer table in every branch query.

Note the wording: `BranchCreateView`'s current message echoes the slug back
(*"School with slug 'x' does not exist"*), which is a mild enumeration oracle.
The non-enumerating phrasing used across `vs_rbac` is the better model.

---

## 16. `ContactInfo` rows accumulate

**Low.**

### The defect

Every path creates a fresh row, unconditionally:

```python
# serializers.py:545-549  (branch create)
contact = ContactInfo.objects.create(full_name=..., email=..., phone=...)
# serializers.py:1052-1056  (school create, school admin)
contact = ContactInfo.objects.create(...)
# serializers.py:1106-1110  (school create, each branch admin)
contact = ContactInfo.objects.create(...)
```

`ContactInfo.email` is indexed but not unique (`models.py:528-531`), and there is
no `get_or_create` anywhere.

`ContactInfo` is `PROTECT` from both link tables (`models.py:552-556`,
`589-593`), so a row can never be cleaned up while any link points at it.

The email is also stored **unnormalised** - `primary_admin_data["email"]`
straight from the payload - while `provision_admin_user` normalises before
creating the `User` (`services/admin_provisioning.py:61`). So the contact row
and the user row can carry different casings of the same address.

### What actually happens

Bright Star is created with Ada as school admin and Ada again as the Ikeja branch
admin. Two `ContactInfo` rows are written for one person. (The *user* is created
once - the school-create path short-circuits the duplicate at
`serializers.py:1119-1123` - so only the contact table duplicates.)

Over a few hundred schools that is a table of near-duplicates that nothing
de-duplicates and nothing can delete. It is not a correctness bug today because
nothing queries `ContactInfo` by email; it is a bug waiting for the first feature
that does.

### The fix

Normalise and reuse:

```python
from vs_user.email_normalization import normalize_email

contact, _ = ContactInfo.objects.get_or_create(
    email=normalize_email(data["email"]),
    defaults={"full_name": data["full_name"], "phone": data.get("phone", "")},
)
```

`normalize_email` is already imported in this module (`serializers.py:29`) and
used on the adjacent lines, so the inconsistency is within a single function.

If contacts are genuinely meant to be per-link snapshots rather than people, say
so in the model docstring and add a comment at each `create()` - but then the
`PROTECT` on the links is the wrong `on_delete`, and the email index is
misleading.

---

## 17. Smaller defects and dead code

**Low.** Individually minor; listed so they are not rediscovered.

**Dead vocabulary:**

- `Modules` (`models.py:74-82`) - a TextChoices listing STUDENTS, TEACHERS,
  PARENTS, ATTENDANCE, FINANCE, PROCUREMENT, VENDORS. Referenced nowhere. The
  live module catalogue is `vs_config.Capability` with `kind=MODULE`, which the
  package serializers actually use. A stale second copy of a vocabulary sitting
  in the model file is a trap for the next reader.
- `OperationOutcome` (`models.py:62-64`) - SUCCEEDED / FAILED. Referenced
  nowhere.
- `PlanTier` (`models.py:67-71`) - BASIC / STANDARD / PREMIUM / ENTERPRISE. No
  model field uses it; the four seeded `PackagePlan` rows carry those words as
  free text in `name`.
- `InviteStatus.FAILED` (`models.py:59`) - never written. `provision_admin_user`
  stamps SENT on success and returns `None` on failure without touching the
  link, so a failed invite is indistinguishable from one still queued. This is
  what makes §2 invisible and §13 unfixable without a schema read.

**Dead file:**

- `signals.py` is a **zero-byte file** that `AppConfig.ready` imports
  (`apps.py:17`). There are no signals in this app; every audit event is emitted
  from a serializer, which means anything writing a `School` or a `Branch`
  outside those serializers produces no audit at all. That is currently safe -
  the bulk importer runs `SchoolCreateSerializer` (`serializers.py:1220-1222`) -
  but it is safe by convention, not by construction.

**Stale local directory:**

- `apps/vs_schools/` still exists on disk containing nothing but `__pycache__`
  subdirectories from before the move to `apps/schools/vs_schools/`. It is
  untracked (`git ls-files vs_schools` is empty), so it is a local artifact
  rather than a repo problem - but because it has no `__init__.py` and does have
  subdirectories, `import vs_schools` resolves it as a namespace package instead
  of raising `ImportError`. A stale import therefore fails later and less
  clearly than it should. Delete it.

**Audit gaps:**

- **A branch lifecycle transition emits no central audit event.**
  `BranchStateTransitionSerializer.save` calls `Branch.transition`, which writes
  the status and a `BranchLifecycle` row (`serializers.py:1475-1494`). No
  `emit_audit_event` call is made, unlike branch create and branch update which
  both emit one (`serializers.py:588-609`, `696-715`). So closing a school's site
  - the most consequential branch action there is - is absent from the Event
  Explorer, and `BranchLifecycle` has no endpoint and no export dataset of its
  own.

**Stale documentation:**

- `BranchStatsView`'s docstring advertises *"Supports optional school scoping via
  ?s=<school_slug>"* (`views/branch.py:96`). There is no `?s=` parameter; the
  scope comes from the URL and is mandatory.
- `SchoolResetConfigSerializer`'s docstring promises three resets and performs
  one (§4).
- `SchoolBranding`'s docstring says "Additional theme fields can be added later"
  (`models.py:382-383`); none were, and the model is a single `ImageField`.

**Inefficiency:**

- `SchoolPackageSetupReadSerializer.get_enabled_modules` nests
  `XVSModuleSerializer(capabilities, many=True)` with no prefetch
  (`serializers.py:279`), while `XVSModuleSerializer.get_dependencies` walks
  `obj.dependency_links.all()` (`serializers.py:179-180`). The list view for
  modules prefetches it (`views/package.py:34`); the school detail does not, so
  a school with six granted modules pays six extra queries on every detail read.
- `get_enabled_modules` filters on `source=PACKAGE`, so a module granted
  **manually** through `vs_config` does not appear in the school's
  `enabled_modules` even though the school has it. The payload answers "what did
  the package buy?" while its field name says "what is enabled?".
- `BranchUpdateView.update` calls `super().update()` and then `self.get_object()`
  again (`views/branch.py:196-202`), re-running the queryset with its two
  `select_related` joins purely to build the response.

**Inconsistency:**

- The two package endpoints are gated on `IsAuthenticatedAndActive &
  IsVisionStaff` with **no** `rbac_permission` (`views/package.py:17`, `:29`),
  while every other route in the app carries a key. Neither payload is sensitive,
  but it means those two surfaces cannot be granted, revoked or audited like the
  rest.
- `SchoolCreateSerializer` reads the actor as `self.context["request"].user`
  (`serializers.py:1038`) while everything around it reads
  `self.context.get("actor_id")`. Two ways of asking the same question in one
  method.
- `serializers.py:1169-1172` computes the default subscription expiry with
  `date.today()` while the validator twenty lines earlier uses
  `timezone.localdate()` (`serializers.py:250`).
- `BranchCreateView` compares `school.status != "ACTIVE"` against a string
  literal (`views/branch.py:148`) rather than `SchoolStatus.ACTIVE`. It works
  because the values coincide, and it is the one place in the app that does not
  use the enum.

---

## What is right, and should not be "tidied"

Several things in this app look like candidates for simplification and are
load-bearing. Recording them so a later pass does not undo them.

- **The nested savepoint in `provision_books_for_school`**
  (`services/books.py:79-90`). A bare `try`/`except` is *not* equivalent: a
  database failure inside the outer transaction leaves it aborted, so every
  later statement fails and the commit takes the school down with it. The
  docstring says so, and `tests_books.py` forces a failed *statement* rather
  than a Python exception precisely to prove it.
- **The tenant-scoped existence probe in `provision_admin_user`**
  (`services/admin_provisioning.py:70-96`). Unscoped, "already exists" meant
  "exists anywhere on the platform", and the row it returned was handed to the
  new school as its administrator - no exception, no error log, and the
  invitation never sent because the account it "found" was activated months ago.
- **Keying every audit event on the primary key, never the slug or the code**
  (`serializers.py:574-587`, `1235-1246`, `1439-1442`, `1536-1538`). Branch
  codes restart at 1 per tenant and `EntityAuditTrail` is unique on
  `(entity_type, entity_id)` with no tenant column, so a code-keyed trail put
  every school's main branch on one interleaved platform-wide row. School slugs
  are editable until go-live, so a slug-keyed trail split in two on rename.
- **Passing `tenant=` explicitly to `emit_audit_event`**
  (`serializers.py:592-602`). Without it the rows landed with `tenant = NULL`
  and "show me everything at Bright Star" became a search through summaries
  instead of a column filter.
- **Reading the actor with `.get()` and no default**
  (`serializers.py:1372-1379`). `actor_user` is a FK; a `"system"` string there
  raises inside `emit_audit_event`, which swallows its own failures - so a
  defaulted actor meant *no event at all* rather than a system-attributed one.
- **Building `before_data` for the reset by hand** (`serializers.py:1518-1522`).
  `model_to_dict` returns the `FieldFile` for an `ImageField`, and a `FieldFile`
  in a `JSONField` raises inside the swallowing `emit_audit_event`, losing the
  whole event.
- **Re-reading the school by primary key after an update**
  (`views/school.py:197-205`). `lookup_field` is the slug and the slug is
  editable, so re-fetching by the URL key 404s on exactly the rename that just
  succeeded.
- **`SchoolDetailView.retrieve` being overridden to do nothing special**
  (`views/school.py:146-163`). It replaced a wrapper that swallowed `Http404`
  into a 500 and shipped `traceback.format_exc()` to the caller. The override is
  the fix; deleting it would restore DRF's behaviour, but the docstring is the
  record of why it exists.
- **`branch_school_slug` as a function rather than a mixin**
  (`serializers.py:358-380`). DRF's `SerializerMetaclass` only collects declared
  fields from serializer bases, so a field on a plain mixin is silently ignored
  and `Meta.fields` then fails with "not valid for model Branch".
- **`required=True, allow_empty=False` on `branches`**
  (`serializers.py:849-875`). It used to default to an empty list, which let this
  endpoint - and the bulk importer behind it - mint a school with nowhere to put
  a user, a document or a student, and put every branch rule behind an
  `if branches:` that never ran for the one payload that needed them.
- **`name` deliberately not editable** (`serializers.py:1276-1281`). The
  spreadsheet importer identifies a school by name when the row carries no slug,
  so a rename turns a school's own import file into a request to create a second
  school.
- **`_type` made `blank=True`** (`vs_tenants/models.py:349-359`). It was
  optional in prose and required in the schema, so every row created outside the
  serializers stored `""` and was then permanently unpatchable through the API,
  because `BranchUpdateSerializer` runs `full_clean()` over the whole instance
  and the blank it refused was one nobody had touched.
