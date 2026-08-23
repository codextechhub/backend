# tenant_code_issues

Everything wrong with `vs_tenants`, in one place, ordered by how much it costs.
Each item states the defect, the evidence, what actually happens to a user, and
the fix. The four slice reports (`tenant_identity_lifecycle`,
`tenant_sites_branches`, `tenant_request_context`,
`tenant_references_numbering`) point here rather than repeating it.

Baseline: **`Ran 62 tests in 4.805s` - OK**

```
cd apps && DB_NAME=cx_tenantslice ../cx/Scripts/python.exe manage.py test \
    vs_tenants --settings=apps.settings.local --noinput
```

Sixty-two tests over roughly 1,300 lines of non-test, non-migration code, and
the app is the foundation every other app scopes against.

The four findings marked **confirmed by execution** (§1, §2, §3, §5) were
reproduced against a real PostgreSQL test database in a throwaway test module
that was deleted afterwards. Everything else is traced to file and line.

Line references are relative to `apps/vs_tenants/` unless another app is named.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in
the code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | Suspending a tenant does not end the impersonation sessions inside it, because no production path calls `Tenant.save()` | **High** |
| 2 | The same bypass voids both slug guards the model docstring says no writer can miss | **High** |
| 3 | `resolve_tenant` is unreferenced dead code that refuses tenants the auth layer admits | Medium |
| 4 | `Tenant.activate()` - the one helper that gets all five lifecycle columns right - has no production caller | Medium |
| 5 | `Tenant.Status.INACTIVE` is unreachable, and would lock every user out if it were reached | Medium |
| 6 | `reconcile_tenants` checks two of a role assignment's three cross-tenant columns and misses `branch` | Medium |
| 7 | `BranchLifecycle` has no reader anywhere in the repo | Medium |
| 8 | `TenantOwnedModel` is an abstract contract no model signs, and the models it would have standardised disagree | Low |
| 9 | The main-branch refusal counts any sibling while promotion needs an in-service one, so its advice can point at a dead end | Low |
| 10 | `LastBranchCannotLeaveService` says "every school" to tenants that are not schools | Low |
| 11 | The four `mark_*` branch helpers have no production caller | Low |
| 12 | The proxy access trail silently stops recording new paths after 200 | Low |
| 13 | The foundation app imports `vs_admin_console` and `vs_audit` from inside its own request path | Low |
| 14 | The document reference has no separator between the tenant id and the date | Low |
| 15 | `TenantDocumentSequence` rows accumulate for ever with no pruning | Low |
| 16 | Four tenant-reference resolvers exist across the repo, two of them byte-identical | Low |
| 17 | Smaller defects and dead code | Low |

---

## 1. Suspending a tenant does not end the sessions inside it

**High. Confirmed by execution.**

### The defect

`vs_admin_console` registers a `post_save` receiver on `Tenant` whose entire
purpose is to cut off staff access when a tenant stops being usable:

```python
# vs_admin_console/receivers.py:1-19
# Keeps impersonation state in sync with tenant lifecycle without vs_tenants
# having to import vs_admin_console: when a Tenant leaves ACTIVE, every active
# impersonation session scoped to it must be ended.

@receiver(post_save, sender=Tenant, dispatch_uid="vs_admin_console.tenant_deactivated")
def on_tenant_saved(sender, instance, **kwargs):
    if instance.status != Tenant.Status.ACTIVE:
        # Deactivation must immediately stop staff from acting inside the tenant.
        end_impersonations_for_tenant(instance)
```

`post_save` fires on `Model.save()`. It does not fire on `queryset.update()`.

Every production path that changes a tenant's status is a `queryset.update()`:

```python
# schools/vs_schools/models.py:345   - the mirror, for every school tenant
Tenant.objects.filter(pk=self.tenant_id).update(**mirrored)

# schools/vs_onboarding/services/lifecycle.py:199   - suspension, no-school tenants
Tenant.objects.filter(pk=tenant.pk).update(status=Tenant.Status.SUSPENDED, ...)

# schools/vs_onboarding/services/lifecycle.py:479   - reinstatement
Tenant.objects.filter(pk=tenant.pk).update(status=Tenant.Status.PENDING, ...)
```

And the school-owned path routes through the mirror deliberately - the comment
says so:

```python
# schools/vs_onboarding/services/lifecycle.py:192-196
school.status = SchoolStatus.SUSPENDED
school.deactivated_at = now
# No update_fields: the mirror in School.save() is what writes the
# tenant, and it is the only thing keeping the two statuses equal.
school.save()
```

`school.save()` fires `post_save` on **School**, not on Tenant. The tenant is
written by the `update()` inside it.

`end_impersonations_for_tenant` has exactly one non-test caller - that receiver
(`vs_admin_console/services.py:32-37`). The tests call the service directly
(`vs_admin_console/tests.py:804`, `:957`), never through a real status change,
which is why this has never surfaced.

### What actually happens

A CX support engineer, Bola, is proxied into Greenfield College investigating a
billing complaint. Greenfield's onboarding window expires overnight and the
sweep suspends it.

Greenfield's own users are locked out immediately and correctly: `SUSPENDED` is
not in `AUTHENTICABLE_STATUSES`, so `TenantJWTAuthentication` refuses them
(`vs_rbac/authentication.py:109-113`).

Bola is not. Her impersonation session is still `ACTIVE`, and
`_load_impersonation` never re-checks the tenant's status
(`vs_rbac/authentication.py:16-70`) - it checks the session's own expiry, the
actor's eligibility and the target's status, and nothing else. She can keep
reading and writing inside the suspended tenant until the session's idle timeout
lapses, which for an open-ended session is `proxy_idle_timeout_minutes` from
`vs_config` and for a fixed session is whenever `ends_at` says.

Confirmed by execution, both production shapes and the control:

```
PROBEA  tenant status after queryset update:      SUSPENDED
PROBEA  session status (ENDED if receiver fired): ACTIVE
PROBEA2 tenant status after school.save() mirror: SUSPENDED
PROBEA2 session status:                           ACTIVE
PROBEA3 session status after tenant.save():       ENDED
```

The third line is the control: the receiver works perfectly. Nothing in
production calls the method that triggers it.

### The fix

The receiver is the wrong mechanism for a codebase whose status writes are all
`update()`. Two changes, and the first alone closes it:

1. **Call the service from the writers.** `schools/vs_onboarding/services/lifecycle.py`
   is where a tenant leaves ACTIVE, and it already imports across app boundaries
   freely. After each suspension write:

   ```python
   from vs_admin_console.services import end_impersonations_for_tenant
   end_impersonations_for_tenant(tenant)
   ```

   Do it in both branches of `_suspend_tenant` (`lifecycle.py:185-209`), so the
   school-owned and no-school paths behave the same.

2. **Make the check live rather than event-driven.** The durable fix is in
   `vs_rbac.authentication._load_impersonation`: re-read the session's tenant
   status on every proxied request and refuse when it is not in
   `AUTHENTICABLE_STATUSES`. That is one condition beside the three already
   there, it cannot be bypassed by any write path, and it closes the same hole
   for a tenant suspended by a future writer nobody has thought of yet.

Then add the test that was missing: suspend a tenant *the way production does*
and assert the session ended.

Worth noting for the same sweep: `queryset.update()` skipping signals is a class
of problem in this repo, not an instance. `error/rbac/rbac_code_issues.md` §18
records the same shape in `transfer_super_admin`.

---

## 2. The same bypass voids both slug guards

**High. Confirmed by execution.**

### The defect

`Tenant`'s class docstring makes an explicit promise:

> Two rules follow, **both enforced in `save()` so no writer can miss them**: it
> may not be one of `RESERVED_TENANT_SLUGS`, and once the tenant has gone live it
> may not change at all. - `models.py:76-78`

And `save()` delivers (`models.py:214-223`):

```python
def save(self, *args, **kwargs):
    self.slug = (self.slug or "").strip().lower()
    self._assert_slug_allowed()
    self._assert_slug_unchanged_once_live()
    return super().save(*args, **kwargs)
```

The comment above it explains why the guards are there rather than on the field:
*"nothing in this codebase creates a tenant through a form"* - so field
validators would never run.

The same reasoning applies one step further and was not followed: nothing in this
codebase **updates** a tenant through `save()` either. `School.save()`'s mirror
is a queryset `update()`, and it writes the slug:

```python
# schools/vs_schools/models.py:335-345
mirrored = {"name": ..., "status": ..., "activated_at": ..., ...}
if not slug_is_frozen:
    mirrored["slug"] = self.slug
Tenant.objects.filter(pk=self.tenant_id).update(**mirrored)
```

`update()` calls neither `save()` nor `clean()`, so neither guard runs.

Both rules do have school-side counterparts -
`School._check_slug_change` for the freeze
(`schools/vs_schools/models.py:199-228`) and `School.clean()` plus the two
serializers for the reserved list. But `School.clean()` is not called by
`School.objects.create()` or by `school.save()` either, so outside the API the
reserved rule has **no** enforcement at all.

### What actually happens

Confirmed by execution:

```
PROBEB direct Tenant.save('admin'):  REFUSED -> {'slug': ['This address is
                                     reserved for the platform. Choose another.']}
PROBEB school renamed to 'admin' -> tenant slug now: 'admin'
```

and for the freeze:

```
PROBEC direct Tenant.save(rename on live): REFUSED -> {'slug': ['This school is
                                           live, so its sign-in address is fixed…']}
PROBEC after queryset update, tenant slug: 'corona-moved'
```

So a data migration, a management command, a shell session or the bulk importer
doing `school.slug = "admin"; school.save()` puts a reserved hostname on a
tenant. That tenant is then served from `admin.xvs.codexng.com` - a host the
platform's own CORS origin regex treats as first-party, and one the reserved list
exists to protect (`models.py:25-31`).

Over HTTP the serializers catch both, so this is not currently exploitable
through the API. It is a broken invariant, not a live breach - but the invariant
is the one the class docstring says cannot be missed, and every future writer will
believe it.

### The fix

Put the guard where the write happens. The mirror is a deliberate `update()` -
it has to be, since calling `tenant.save()` from inside `School.save()` would
recurse through the receiver - so validate before writing:

```python
# schools/vs_schools/models.py, before the update()
if "slug" in mirrored:
    from vs_tenants.models import slug_is_reserved
    if slug_is_reserved(mirrored["slug"]):
        raise ValidationError({"slug": "This address is reserved for the platform."})
```

Better, because it covers the two `vs_onboarding` writers too: give `Tenant` a
classmethod that owns the safe update, and route all three writers through it.

```python
@classmethod
def apply_mirror(cls, *, pk, **fields):
    """The one safe way to write a tenant's columns without an instance save."""
    if "slug" in fields:
        cls._assert_slug_allowed_for(fields["slug"], kind=...)
        cls._assert_slug_unchanged_once_live_for(pk, fields["slug"])
    cls.objects.filter(pk=pk).update(**fields)
```

That also gives §1 its natural home: the same method can call
`end_impersonations_for_tenant` when the new status is not ACTIVE.

Whatever shape is chosen, the docstring at `models.py:76-78` must stop claiming
`save()` is the choke point until it is.

---

## 3. `resolve_tenant` is dead code that disagrees with the live auth layer

**Medium. Confirmed by execution.**

### The defect

```python
# resolution.py:8-19
def resolve_tenant(request):
    """Resolve and authorize the mandatory ``?tenant=<slug>`` assertion."""
    slug = (request.query_params.get("tenant") or "").strip().lower()
    if not slug:
        raise ValidationError({"tenant": "A 'tenant' query parameter is required."})
    tenant = Tenant.objects.filter(slug=slug, status=Tenant.Status.ACTIVE).first()
    effective_user = getattr(request, "effective_user", None) or request.user
    if tenant is None or getattr(effective_user, "tenant_id", None) != tenant.pk:
        raise NotFound("No tenant matches the requested context.")
    request.tenant = tenant
    return tenant
```

`grep -rn "resolve_tenant"` over the repo finds it in its own module and in
`vs_tenants/tests.py`. Nothing else. The two `core` management commands that
define a `_resolve_tenant` are a different function over `references.find_tenant`
(§16).

The live implementation is `vs_rbac.authentication.TenantJWTAuthentication`
(`vs_rbac/authentication.py:91-138`), and it differs in two ways:

| | `resolve_tenant` | the live one |
|---|---|---|
| Statuses admitted | `ACTIVE` only | `AUTHENTICABLE_STATUSES` = `ACTIVE` **and** `PENDING` |
| Cross-tenant | never | a `PLATFORM` actor may assert another tenant on a view declaring `platform_cross_tenant_param` |
| What it sets | `request.tenant` | five request attributes plus both contextvars |

The first difference is the substantive one. `AUTHENTICABLE_STATUSES` includes
PENDING with a stated reason - a school has to onboard itself before it goes
live (FR-012), and being admitted says nothing about which surfaces are open
(`models.py:92-97`). `resolve_tenant` predates that decision and never absorbed
it.

### What actually happens

Nothing today, because nothing calls it. Confirmed by execution:

```
PROBED tenant status: PENDING | in AUTHENTICABLE_STATUSES: True
PROBED resolve_tenant: 404 -> No tenant matches the requested context.
```

A tenant the platform's own authentication layer admits is refused by this
helper, and the refusal is the non-enumerating 404 - so a caller could not tell
"your school is still onboarding" from "no such school".

The risk is the next person who needs a `?tenant=` resolver in a non-DRF context
- a management command, a Channels consumer, a webhook - finds a function whose
name and docstring say exactly what they want, uses it, and locks every
onboarding school out of that surface with a 404 nobody can diagnose.

### The fix

Delete it, and delete its tests. `TenantJWTAuthentication` is the resolver, and
its behaviour is the contract; a second implementation that has drifted is worse
than none.

If a non-DRF caller genuinely needs one later, the right shape is to extract the
tenant-resolution block out of `TenantJWTAuthentication.authenticate`
(`vs_rbac/authentication.py:101-138`) into a function both call, so they cannot
drift again - which is the same fix `error/rbac/rbac_code_issues.md` §2
recommends for the evaluator and its routing twin.

If it is kept as-is for now, at minimum change the filter to
`status__in=Tenant.AUTHENTICABLE_STATUSES` and add the test that would have
caught the divergence.

---

## 4. `Tenant.activate()` has no production caller

**Medium.**

### The defect

```python
def activate(self):
    self.status = self.Status.ACTIVE
    self.activated_at = self.activated_at or timezone.now()
    self.deactivated_at = None
    self.pending_since = None
    self.expiry_warned_at = None
```

`models.py:266-272`. It is the only place in the repo that gets all five
lifecycle columns right in one expression, including the write-once
`activated_at or now` that the slug freeze depends on.

`grep -rn "\.activate()"` finds it at `vs_tenants/tests.py:803`, `:821` and
`:835`, and nowhere else. Production activation is `School.save()`'s mirror,
which reassembles the same five columns by hand from a status map and two
classmethods (`schools/vs_schools/models.py:299-345`).

The result is that the model's own lifecycle helper is documentation, and the
real logic lives in another app's `save()`.

### What actually happens

Nothing visible - the mirror is careful and gets it right. The cost is structural
and shows up in the other findings on this page:

- §1 exists because the mirror uses `update()`, which `activate()` would not.
- §2 exists for the same reason.
- There is no `suspend()` or `deactivate()` counterpart either (§17), so the
  onboarding lifecycle service hand-writes the five columns twice more
  (`lifecycle.py:199-208`, `:479-486`) with a comment at each site explaining
  that it is copying what the mirror does.

Four call sites reassembling the same five-column invariant is three chances to
get it wrong, and one of them - `expiry_warned_at` - was already got wrong once,
which is why `pending_stamps_for` exists at all (`models.py:246-256`).

### The fix

Make `activate()` the shape the writers can actually use, and add its siblings:

```python
@classmethod
def apply_status(cls, *, pk, new_status, now=None):
    """Write a tenant's status and every column that describes it."""
```

one classmethod that reads the stored row, computes `pending_since` /
`expiry_warned_at` through `pending_stamps_for`, sets `activated_at` /
`deactivated_at` per the target, issues the `update()`, and calls
`end_impersonations_for_tenant` when the target is not ACTIVE (§1).

Then `School.save()`'s mirror, both `vs_onboarding` writers and `activate()`
itself all become one call, and the five-column invariant has one home. That is
the same change §2 needs, so do them together.

---

## 5. `Tenant.Status.INACTIVE` is unreachable and would lock everyone out

**Medium. Confirmed by execution** (of the reachability half).

### The defect

`Tenant.Status` declares four values (`models.py:86-90`) and
`AUTHENTICABLE_STATUSES` admits two (`models.py:97`). `INACTIVE` is in neither
the admitted set nor any writer's vocabulary.

Searching the repo for writers of a tenant status finds five sites, and
`INACTIVE` appears in exactly one:

```python
# schools/vs_schools/models.py:300-304
tenant_status = {
    SchoolStatus.ACTIVE:    Tenant.Status.ACTIVE,
    SchoolStatus.INACTIVE:  Tenant.Status.INACTIVE,
    SchoolStatus.SUSPENDED: Tenant.Status.SUSPENDED,
}.get(self.status, Tenant.Status.PENDING)
```

- the mirror's map. And `SchoolStatus.INACTIVE` is itself written by nothing:
`error/schools/school_code_issues.md` §9 records that finding, confirmed by
execution.

So the map entry is reachable only if the school-side value becomes reachable
first.

### What actually happens

Today: nothing. A dashboard bucket that is always zero, on both sides.

The danger is the next change. Someone implementing "deactivate a school" will
find `SchoolStatus.INACTIVE` declared, filterable and counted, wire a button to
it, and ship. The mirror will then write `Tenant.Status.INACTIVE`, which is not
in `AUTHENTICABLE_STATUSES` - so **every user at that school is locked out of
every surface immediately**, with the same 404 an unknown slug gets.

That may well be the intent. But nothing in `SchoolStatus`, nothing in
`Tenant.Status` and nothing in the mirror says so. `SUSPENDED` carries a comment
explaining exactly what it means and why it exists (`schools/vs_schools/models.py:49-53`);
`INACTIVE` carries none on either side.

### The fix

Decide, and write it down either way.

**If INACTIVE is meant to exist**, document it on `Tenant.Status` beside
`AUTHENTICABLE_STATUSES` - "INACTIVE locks every user out; use SUSPENDED for a
recoverable pause" - and implement the school-side transition in
`vs_onboarding.services.lifecycle` beside suspension and reinstatement, where
school status already lives.

**If it is not**, remove it from both enums. It is a `CharField` choice with no
rows carrying the value, so the migration is trivial, and the removal deletes
three dead dashboard buckets and one dead map entry with it.

The one thing not to do is leave a status declared, filterable, counted and
mapped, whose only effect if reached is a silent platform-wide lockout.

---

## 6. `reconcile_tenants` misses the branch column

**Medium.**

### The defect

The command checks three cross-tenant invariants
(`management/commands/reconcile_tenants.py:48-53`):

```python
if TenantRoleTemplate.objects.filter(branch__isnull=False).exclude(tenant=F("branch__tenant")).exists():
    failures.append("role templates with cross-tenant branches")
if TenantUserRoleAssignment.objects.exclude(tenant=F("user__tenant")).exists():
    failures.append("role assignments with cross-tenant users")
if TenantUserRoleAssignment.objects.exclude(tenant=F("role__tenant")).exists():
    failures.append("role assignments with cross-tenant roles")
```

`TenantUserRoleAssignment` has **three** cross-tenant columns, and its own
`clean()` guards all three (`vs_rbac/models.py:793-804`):

```python
if self.user_id and self.user.tenant_id != self.tenant_id:   errors["user"]   = ...
if self.role_id and self.role.tenant_id != self.tenant_id:   errors["role"]   = ...
if self.branch_id and self.branch.tenant_id != self.tenant_id: errors["branch"] = ...
```

The reconciler checks two of them. It also does not check the branch on a role
*template* against anything other than… actually it does check that one - so the
gap is specifically the assignment's `branch`.

That matters more than it looks, because `TenantUserRoleAssignment.clean()` is
**never called on the API path**: the serializer relies on its tenant-scoped
reference fields instead (`docs/rbac/rbac_roles_assignments.md` §2), and
`save()` runs only `assert_scope_allowed`, not `clean()`. So the branch column's
only enforcement is a serializer field, and the reconciler - the tool whose job
is to catch what enforcement missed - does not look at it.

The command's own docstring is about exactly this class of rot: two checks were
left pointing at columns the refactor had dropped, so the whole command raised
`FieldError` on the first line and none of the surviving invariants were ever
verified (`reconcile_tenants.py:6-13`).

### What actually happens

A data migration, an import, or any writer that bypasses the serializer can
create an assignment pinned to another tenant's branch. `vs_rbac.scoping._grant_scope`
would then return that foreign branch id as part of the caller's visible set
(`vs_rbac/scoping.py:82-100`), and `BranchScope.filter` would render it into
every list query as `branch_id IN (…)`.

Whether that actually leaks rows depends on the surrounding entity/tenant
scoping catching it first - in most cases it would. But `reconcile_tenants`
exists precisely to find rows nothing else is looking at, and this is one of
them.

### The fix

One more check, in the same shape as its two neighbours:

```python
if TenantUserRoleAssignment.objects.filter(branch__isnull=False).exclude(
        tenant=F("branch__tenant")).exists():
    failures.append("role assignments with cross-tenant branches")
```

While there, two more invariants this app enforces at write time and the
reconciler does not assert:

```python
# every tenant has at most one main branch (the partial index should hold it,
# but a restore or a raw insert may not have)
from django.db.models import Count
if (Branch.all_objects.filter(is_main=True).values("tenant")
        .annotate(n=Count("id")).filter(n__gt=1).exists()):
    failures.append("tenants with more than one main branch")

# no non-platform tenant holds a reserved slug
from vs_tenants.models import RESERVED_TENANT_SLUGS
if Tenant.objects.exclude(kind=Tenant.Kind.PLATFORM).filter(
        slug__in=RESERVED_TENANT_SLUGS).exists():
    failures.append("tenants holding reserved platform slugs")
```

That second one is what would have caught §2 in production.

---

## 7. `BranchLifecycle` has no reader

**Medium.**

### The defect

`BranchLifecycle` is a well-built audit table: a foreign key with `CASCADE`, a
required `to_state`, an optional `from_state` for creation events, a free-text
actor, a reason, a timestamp, and **two** indexes chosen for querying -
`(branch, occurred_at)` for a timeline and `(branch, to_state)` for filtering by
resulting state (`models.py:731-736`). Its docstring says the indexes "support
timeline views per branch or filtering by resulting state".

There are no timeline views. `grep -rn "BranchLifecycle\|lifecycle_events"` finds:

| Site | What it does |
|---|---|
| `vs_tenants/models.py` | Defines it; `transition()` writes it |
| `schools/vs_schools/serializers.py:523`, `:1095` | Writes a creation event |
| `schools/vs_schools/serializers.py:116-126` | `BranchLifecycleSerializer` - a read-only `ModelSerializer` |
| tests | Assertions |

`BranchLifecycleSerializer` is declared and **referenced by nothing**: no view
uses it, no other serializer nests it, and `BranchDetailSerializer` does not
include `lifecycle_events` in its fields (`schools/vs_schools/serializers.py:412-435`).

There is also no `vs_exports` dataset for it (`schools/vs_schools/export_datasets.py`
registers `platform.schools` only) and no Django admin registration anywhere in
the repo.

### What actually happens

Every branch suspension, closure, activation and reactivation since the platform
launched is recorded, indexed, and unreachable. An operator asking "when was
Lekki closed, by whom, and why?" has the answer in the database and no way to
read it short of a shell.

It is worse than an unused table, because the transition path emits **no central
audit event either** (§13 and `error/schools/school_code_issues.md` §17). So for
a branch's most consequential operation, `BranchLifecycle` is the only record and
nothing can display it.

### The fix

The serializer already exists, so the cheapest useful version is one nested field
on the branch detail response:

```python
# schools/vs_schools/serializers.py, BranchDetailSerializer
lifecycle_events = BranchLifecycleSerializer(many=True, read_only=True)
```

with `.prefetch_related("lifecycle_events")` on the two branch detail querysets,
and the field ordered newest-first.

For a real timeline, add a route beside the transition endpoint -
`GET <slug>/branches/<code>/lifecycle/` under `platform.branches.view` - and
paginate it.

Either way, fix `actor_id` first (`error/schools/school_code_issues.md` §10), or
the timeline will display three different kinds of value in its "who" column.

---

## 8. `TenantOwnedModel` is a contract nobody signs

**Low.**

```python
class TenantOwnedModel(models.Model):
    """Abstract contract for rows owned by exactly one tenant."""
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+", db_index=True,
    )
    class Meta:
        abstract = True
```

`models.py:739-750`. `grep -rn "TenantOwnedModel"` finds the definition and one
passing mention in a `vs_rbac` test docstring
(`vs_rbac/tests/test_tenant_isolation.py:61`). No model inherits it.

Every tenant-owned model declares its own FK by hand, and they do not agree:

| Model | `related_name` | `on_delete` |
|---|---|---|
| `Branch` | `"branches"` | `PROTECT` |
| `TenantDocumentSequence` | `"document_sequences"` | `PROTECT` |
| `School` | `"school_profile"` (a `OneToOneField`) | `PROTECT` |
| `TenantRoleTemplate` | `"role_templates"` | `PROTECT` |
| `TenantUserRoleAssignment` | `"role_assignments"` | `PROTECT` |
| `UserPermissionOverride` | `"user_permission_overrides"` | `PROTECT` |
| `ImpersonationSession` | `"impersonation_sessions"` | `PROTECT` |

They agree on the important thing - `PROTECT` throughout - and disagree on
`related_name`, which the base would have forced to `"+"` (no reverse accessor).
Given that half of those reverse accessors are used, adopting the base as written
would break them.

**Fix:** delete it. It documents a convention the codebase follows anyway
(`PROTECT`, indexed, non-null) and prescribes one it deliberately does not
(`related_name="+"`). If a shared base is wanted, it should drop the
`related_name` and let each model set its own - but a base contributing one field
and no behaviour earns little.

---

## 9. The main-branch refusal can point at a dead end

**Low.**

### The defect

`_assert_may_leave_service` picks its refusal on whether *any* sibling exists
(`models.py:567-579`):

```python
has_sibling = (
    Branch.all_objects.filter(tenant_id=self.tenant_id).exclude(pk=self.pk).exists()
)
if has_sibling:
    raise MainBranchCannotLeaveService(branch_name=self.name, to_state=to_state)
raise LastBranchCannotLeaveService(branch_name=self.name, to_state=to_state)
```

`MainBranchCannotLeaveService`'s message is *"Make another branch the main branch
first, then take this one out of service."*

But `promote_to_main` requires the candidate to be **in service**
(`models.py:607-608`):

```python
if self.status not in self.IN_SERVICE_STATES:
    raise BranchNotInService(branch_name=self.name, status=self.status)
```

`exists()` counts SUSPENDED, INACTIVE and CLOSED siblings. So a tenant whose only
other branches are all out of service is told to promote one, and every promotion
attempt is refused.

### What actually happens

Corona Secondary has two sites: Ikeja (main, active) and Lekki (closed last
term). Corona is winding down and CX tries to close Ikeja.

The refusal says to make another branch the main branch first. The only candidate
is Lekki, and `CLOSED` is terminal - `ALLOWED_TRANSITIONS[CLOSED]` is empty
(`models.py:538`) - so Lekki can never come back into service and can never be
promoted. Corona is stuck in a loop the error message sends it round.

The *correct* advice for that situation is `LastBranchCannotLeaveService`'s -
"deactivate the school itself instead" - because in every way that matters Ikeja
is the last branch.

### The fix

Count only siblings that could actually take the handover:

```python
has_sibling = (
    Branch.all_objects
    .filter(tenant_id=self.tenant_id, status__in=self.IN_SERVICE_STATES)
    .exclude(pk=self.pk)
    .exists()
)
```

That makes the two messages exhaustive and correct: if an in-service sibling
exists, promotion will work; if none does, the school-level advice is the only
route. It is a one-line change and it uses the set the class already derives for
exactly this kind of question.

---

## 10. A school-shaped message in a domain-neutral app

**Low.**

```python
class LastBranchCannotLeaveService(BranchLifecycleError):
    def __init__(self, *, branch_name: str = "", to_state: str = ""):
        ...
        super().__init__(
            f"{subject} is the only branch, and every school must keep one in "
            f"service. Deactivate the school itself instead."
        )
```

`exceptions.py:88-105`. The module docstring directly above it makes the opposite
promise:

> These arrived here with `Branch`. They describe a *site* lifecycle, not a school
> one: **a clinic chain or a retail group refuses the same edges for the same
> reasons**, and `Branch.transition` raises them, so leaving them in the school app
> would have meant a platform model importing a product app.
> - `exceptions.py:8-11`

`MainBranchCannotLeaveService` gets it right - "Make another branch the main
branch first" - and its docstring says "the tenant's main branch". Only the
`LastBranch` variant's user-facing string says "school", twice, and its docstring
adds a third ("Every school has at least one branch", "Winding a school down is a
school-level action").

`CLAUDE.md` is explicit that outside `apps/schools/` the word is **tenant**, and
that the ban is on identifiers rather than explanations - but this is a
user-facing string, not an explanation, and a VIGIL clinic group will read it.

**Fix:** two words.

```python
f"{subject} is the only site, and a tenant must keep one in service. "
f"Deactivate the organisation itself instead."
```

Check the schools-side copy at the same time: `BranchUpdateSerializer.validate`
raises *"A school must always have a main branch"*
(`schools/vs_schools/serializers.py:656-660`), which is correct there because
that serializer is school-facing.

---

## 11. The four `mark_*` branch helpers have no caller

**Low.**

```python
def mark_active(self, *, actor_id, reason=""):    self.transition(to_state=ACTIVE, ...)
def suspend(self, *, actor_id, reason):           self.transition(to_state=SUSPENDED, ...)
def reactivate(self, *, actor_id, reason=""):     self.transition(to_state=ACTIVE, ...)
def mark_inactive(self, *, actor_id, reason):     self.transition(to_state=INACTIVE, ...)
```

`models.py:490-500`. `grep` finds no production caller for any of them; the API
goes straight to `transition()` through `BranchStateTransitionSerializer`
(`schools/vs_schools/serializers.py:1492`).

They are harmless, and `transition()`'s docstring names them as one of the routes
it guards (`models.py:643-646`). Two small oddities:

- `mark_active` and `reactivate` are the same call with different names and
  different `reason` defaults (`reason=""` on both, but `suspend` and
  `mark_inactive` make `reason` required). The distinction between "activate" and
  "reactivate" exists in the method names and nowhere in the data - the
  `BranchLifecycle` row records `from_state`, which is where the difference
  actually lives.
- There is no `close()` helper, so the one terminal, irreversible transition is
  the one with no convenience wrapper.

**Fix:** either delete all four, or complete the set and use them from the
serializer so the API's vocabulary and the model's agree. Deleting is the smaller
change and loses nothing.

---

## 12. The proxy access trail stops recording after 200 paths

**Low.**

```python
ACCESS_LOG_MAX_PATHS = 200   # Distinct paths kept per session; existing entries keep counting past the cap.
...
elif len(log) < ACCESS_LOG_MAX_PATHS:
    log.append({"path": request.path, "count": 1, ...})
    update_fields.append("access_log")
```

`middleware.py:21-22`, `43-50`. Past 200 distinct paths, a new path is silently
dropped: no counter, no marker, no log line. Existing entries keep incrementing,
so the trail continues to grow in `count` while going blind to anything new.

The cap is sensible - the column is a `JSONField` on a row saved on every proxied
request, and an unbounded list would be a real problem. What is missing is the
signal that truncation happened.

### What actually happens

A long support session browsing a large console can pass 200 distinct paths.
From that point the trail reads as though the engineer stopped visiting new
screens. An auditor reconstructing what was viewed would conclude the session
covered 200 paths when it covered more, and there is nothing in the record
saying otherwise.

**Fix:** record the fact.

```python
elif len(log) < ACCESS_LOG_MAX_PATHS:
    log.append({...})
    update_fields.append("access_log")
else:
    session.access_log_truncated = True      # or a counter on the session
    update_fields.append("access_log_truncated")
```

A boolean column, or a `{"path": "…truncated…", "count": N}` sentinel entry if a
migration is unwelcome. The principle is the one `error/exports/export_code_issues.md`
states for its own caps: silent truncation reads as "covered everything" when it
did not.

---

## 13. The foundation app imports upward, inside the request path

**Low.**

`vs_tenants` is the app everything else depends on - `Tenant` is the boundary,
`context` is the storage, and its middleware runs on every request. It imports
two apps that sit far above it, and it does so inside `__call__`:

```python
# middleware.py:116
from vs_audit.services import emit_audit_event
# middleware.py:126
from vs_admin_console.views import is_platform_actor
```

Both are deferred into the function body, which is what keeps Django's app
loading from breaking. But the dependency is real, it runs per request, and
`is_platform_actor` is imported from a **views** module - so a middleware in the
foundation app pulls in an admin console's view layer, and everything that
imports.

`vs_tenants/models.py:9` also does `from vs_rbac.managers import TenantAwareManager`
at module level, which is the reverse of the usual direction (`vs_rbac` imports
`vs_tenants` heavily).

None of this is currently broken. It is recorded because the app's own comments
work hard to avoid exactly this: `exceptions.py:8-11` explains that the branch
exceptions moved here so a platform model would not import a product app, and
`vs_admin_console/receivers.py:1-4` says its receiver exists *"without vs_tenants
having to import vs_admin_console"* - a statement the middleware contradicts
three files away.

**Fix:** move `is_platform_actor` out of `vs_admin_console.views` into a
`vs_admin_console.services` (or `selectors`) module, so at least the views layer
is not on the import path. Better, since the whole proxy-audit fallback is about
impersonation rather than about tenancy: move the fallback block itself into a
`vs_admin_console` middleware placed after this one, leaving
`TenantContextCleanupMiddleware` doing only what its name and docstring say.

Related, and worth folding into the same review: **`Branch` writes no audit event
of its own**, so a `transition()` called from anywhere but the schools serializer
leaves nothing in the central trail (see also §7 and
`error/schools/school_code_issues.md` §17).

---

## 14. The document reference has no separator between tenant and date

**Low.**

```python
return f"{code}-{tenant.pk}{day:%y%m%d}{sequence.last_number}"
```

`numbering.py:39`. One separator, between the code and everything else. The
tenant id is variable-width, the date is fixed at six digits, and the sequence is
variable-width and unpadded.

The string is therefore parseable only because the middle segment has a known
fixed width, and uniqueness across `(tenant, n)` pairs is a property of the
current date range rather than of the format. A concatenation of two
variable-width integers around a fixed-width one is ambiguous in principle: two
different `(tenant.pk, last_number)` pairs can produce the same string when the
date's digits happen to align with the boundary shift.

Working the arithmetic through for the simplest collision - tenant 1 with
sequence 12 against tenant 11 with sequence 2 - requires the date to be
`111111`, i.e. 11 November 2011. Every date this platform will ever allocate for
begins `2` and is far from the pathological cases, so the format is safe in
practice and will stay safe.

It matters anyway for one reason: `School.code` is `unique=True` platform-wide
and is filled from this allocator (`schools/vs_schools/models.py:288-295`). If a
collision ever did occur, the caller would get
`"A record with these details already exists."` from
`core/exceptions.py:145-151` - a 400 with no field and no explanation - on a
school creation that was entirely valid.

**Fix:** one character, and a zero-pad.

```python
return f"{code}-{tenant.pk}-{day:%y%m%d}-{sequence.last_number:04d}"
```

That is a **format change**, so it needs a decision rather than a patch: existing
codes stay as they are, `School.code` is `max_length=32` and has room, and any
consumer parsing the old shape would need checking. If the appetite is not there,
leave it and record the reasoning - which is what this entry is for.

---

## 15. `TenantDocumentSequence` rows accumulate for ever

**Low.**

One row per `(tenant, document_code, local date)`, created by `get_or_create` on
first use and never removed (`numbering.py:29-32`). No management command, no
Celery beat task and no migration prunes them.

A tenant issuing invoices, receipts, payments, journals, purchase orders, GRNs
and stock adjustments daily generates roughly seven rows a day, or about 2,500 a
year. Two hundred tenants is half a million rows a year - small by any measure,
indexed on `(tenant, date)`, and never scanned in bulk.

So this is genuinely harmless today, and is recorded only because the table's own
docstring does not say it is append-only and nothing else does either. A future
reader may reasonably wonder whether the absence of pruning is deliberate.

**Fix:** none needed. Add one sentence to the model docstring saying the rows are
kept indefinitely and why (they are the audit trail for how a reference was
allocated), so nobody writes a cleanup task that breaks the sequence.

If pruning is ever wanted, deleting a *past* date's row is safe - the allocator
would simply `get_or_create` it again at zero - but only if that date can never
be passed as `allocation_date` again, which is a back-dating decision rather than
a storage one.

---

## 16. Four tenant-reference resolvers, two of them identical

**Low.**

| Where | What it does |
|---|---|
| `vs_tenants/references.py:34-59` `find_tenant` | pk or slug → `Tenant` or `None`. The real one |
| `vs_tenants/resolution.py:8-19` `resolve_tenant` | `?tenant=` slug → `Tenant`, ACTIVE only, ownership-checked. Dead (§3) |
| `core/management/commands/create_superuser.py:36-43` `_resolve_tenant` | `find_tenant` + `CommandError` on miss |
| `core/management/commands/delete_user.py:52-59` `_resolve_tenant` | **byte-identical to the above** |

And a fifth, in spirit: `vs_rbac.authentication` resolves `?tenant=` itself
(`vs_rbac/authentication.py:101-138`) and is the one that actually runs on
requests.

The two command copies are eight lines each, identical down to the docstring and
the error message. They exist because `find_tenant` returns `None` rather than
raising, which is right for a library function and awkward for a command that
wants to abort.

**Fix:** put the raising variant in `references.py` beside the one it wraps, and
have both commands import it:

```python
def require_tenant(ref, *, error_cls=ValueError):
    tenant = find_tenant(ref)
    if tenant is None:
        raise error_cls(f"No tenant found for {ref!r} (id or slug).")
    return tenant
```

with the commands passing `CommandError`. `TENANT_NOT_FOUND` is already defined
in that module and unused (§17), so it can supply the message.

Then delete `resolution.py` per §3, and the count goes from four to two: one
library resolver and the authentication layer.

---

## 17. Smaller defects and dead code

**Low.** Individually minor; listed so they are not rediscovered.

**Dead code:**

- `TENANT_NOT_FOUND` (`references.py:31`) - defined, exported by position, used
  nowhere. `BRANCH_NOT_FOUND` beside it is used twice.
- `reset_current_tenant(token)` (`context.py:21-22`) - the only consumer of the
  token `set_current_tenant` returns, and it has no caller. Neither does
  `clear_current_audit_identity` (`context.py:96-100`), which
  `clear_current_tenant` duplicates inline.
- `TenantOwnedModel` (§8).
- `resolve_tenant` (§3).
- The four `mark_*` branch helpers (§11).
- `Branch.mark_active` and `Branch.reactivate` are the same call under two names
  (§11).

**Missing counterparts:**

- `Tenant.activate()` has no `suspend()` or `deactivate()`, so four call sites
  hand-write the same five-column invariant (§4). Two of them carry comments
  explaining that they are copying what the mirror does
  (`schools/vs_onboarding/services/lifecycle.py:202-206`, `:475-484`), which is
  the clearest possible signal that the helper is missing.
- `Branch` has helpers for four transitions and none for `CLOSED`, the one that
  is terminal.

**Documentation that has drifted:**

- `Tenant`'s class docstring says both slug rules are "enforced in `save()` so no
  writer can miss them" (`models.py:76-78`). No production writer calls `save()`
  (§2).
- `vs_admin_console/receivers.py:1-4` says the receiver keeps impersonation state
  in sync "without vs_tenants having to import vs_admin_console" -
  `vs_tenants/middleware.py:126` imports `vs_admin_console.views` (§13).
- `migrations/0002_seed_platform_tenant.py:6` says the reverse "refuses if codex
  already owns rows". It does refuse, but through `PROTECT` on a dozen foreign
  keys rather than through any check in the migration. The behaviour is right and
  the sentence implies a mechanism that is not there.
- `BranchLifecycle`'s docstring says its indexes "support timeline views per
  branch" (`models.py:697-698`). There are no timeline views (§7).

**Inconsistency:**

- `find_tenant` accepts a non-numeric reference and tries it as a slug;
  `find_branch_in_tenant` refuses a non-numeric reference outright
  (`references.py:55-59` vs `:76-77`). Correct in both cases - branches have no
  slug - but the asymmetry is worth a comment in the module docstring, which
  currently describes only the branch rule.
- `numbering.py` raises `ValueError` for its two programming errors
  (`numbering.py:23`, `26`). `core/exceptions.py` has no `ValueError` branch, so
  either would be an unhandled 500 if it ever reached a request. Both are
  unreachable from a request path today.
- `Branch.__str__` returns `tenant.slug:code`, which costs a query when the
  tenant is not already loaded. It is a debugging repr used in no response or
  audit label (`models.py:418-425`), so this is a note rather than a defect.

---

## What is right, and should not be "tidied"

This is the most carefully reasoned app in the repo, and a great deal of it looks
like it could be simplified without loss. Recording the load-bearing parts so a
later pass does not undo them.

- **The reserved-slug rule enforced in `save()` rather than on the field**
  (`models.py:38-43`). Field validators do not run on `objects.create()`, which
  is how every writer in this codebase makes a tenant, so a validator would have
  looked like enforcement and caught nothing. (§2 is that the reasoning did not
  go one step further to `update()`, not that this reasoning is wrong.)
- **The freeze reading `activated_at`, not `status`** (`models.py:165-178`),
  with all three rejected alternatives written down: `status == ACTIVE` alone
  would let a school suspended for an unpaid invoice rename itself while it is
  off and come back at an address its families cannot reach.
- **PENDING deliberately outside the freeze** (`models.py:183-189`), because the
  only people affected before go-live are the handful of admins doing the
  onboarding, one of whom is the person fixing the typo.
- **PLATFORM exempt from the reserved list** (`models.py:141-145`) - `codex` is
  on the list precisely *for* the platform tenant.
- **`pending_since` separate from `created_at`** (`models.py:109-115`) and
  **`expiry_warned_at` belonging to the spell** (`models.py:117-121`). Both exist
  because the naive version expired a reinstated school on the sweep's very next
  run, and warned it a dozen times or never.
- **`pending_stamps_for` returning both columns as a pair**
  (`models.py:246-264`), so a warning can never outlive the spell it describes.
- **Locking the *tenant* row in `allocate_next_code`** (`models.py:454-459`).
  The obvious version locks the branch rows, which locks nothing at all when the
  tenant has none yet - so two concurrent first-branch creates both wrote code 1.
- **Reading through `all_objects` in the allocator** (`models.py:461-464`), or
  platform code creating a branch for a customer aggregates over zero rows and
  hands back a duplicate.
- **Demote-then-promote in `promote_to_main`** (`models.py:585-592`), required
  by the non-deferrable partial unique index.
- **Guarding every out-of-service state, not only CLOSED**
  (`models.py:550-557`), because a suspended main branch is already wrong even
  though the damage is not yet permanent.
- **`IN_SERVICE_STATES` derived from the choices and stated positively**
  (`models.py:510-518`), because `vs_rbac` filters across a nullable branch
  column where a negative filter would drop whole-tenant grants.
- **Keeping `db_table = "vs_schools_branch"`** (`models.py:388-391`), which
  avoided rewriting 39 foreign key constraints for a cosmetic gain.
- **The branch exceptions living here rather than in the schools app**
  (`exceptions.py:8-11`), so a platform model does not import a product app.
- **`TenantNotLive` being 403 and not 404** (`exceptions.py:161-164`): 404 is
  reserved for asserting a tenant that is not yours, where even its existence
  must stay hidden.
- **`ContextVar` rather than thread-local** (`context.py:3-10`), so the context
  follows async tasks.
- **Clearing the request context before *and* in a `finally`**
  (`middleware.py:105`, `176-177`), so neither an inherited context nor a raised
  view can leak one.
- **The audit-event counter** (`context.py:52-58`), so a request-level fallback
  fires only when no feature-level event did.
- **`resolve_audit_identity` matching either request identity**
  (`context.py:77-81`), because a service under impersonation may pass either
  spelling of "the current user", and the durable row must name the engineer.
- **`add_proxy_audit_metadata` copying rather than mutating**
  (`context.py:87`), because the same dict reaches two sinks.
- **Collapsing five reference-failure modes to `None`**
  (`references.py:9-19`), which is what removes the id oracle - the docstring
  names all three prior failures by name.
- **The `_MAX_BIGINT` guard** (`references.py:25-27`), which turns a PostgreSQL
  cast error into a 400.
- **Tenant-level rather than entity-level numbering** (`models.py:754-759`), so
  two entities under one tenant cannot open competing series for one document
  code.
- **Locking the sequence row *after* `get_or_create`** (`numbering.py:29-36`),
  so the row is guaranteed to exist before it is locked.
