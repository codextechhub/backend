# tenant_identity_lifecycle

The security boundary the whole platform scopes on: the `Tenant` row, the three
kinds it can be, the four statuses it can hold, the slug that is simultaneously a
primary identifier and a DNS label, the two rules that protect it, the pair of
columns that describe an onboarding spell, and the one seeded platform tenant
everything else is built on top of.

Sites are `tenant_sites_branches`; the request-local context and the middleware
are `tenant_request_context`; reference resolution and document numbering are
`tenant_references_numbering`.

**This app publishes no HTTP routes at all.** It is a library: models, a
contextvar store, one middleware, three helper modules and one management
command. Its consumers are every other app in the repo.

Findings for the whole module are collected in
**`error/tenants/tenant_code_issues.md`**; §8 below points at the ones belonging
here rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **A tenant is the ownership boundary, and it is not a school.** `Tenant.Kind`
  has three values - `PLATFORM`, `SCHOOL`, `ORGANIZATION` (`models.py:81-84`).
  `schools.vs_schools.School` is a *profile* hanging off a SCHOOL tenant, not
  the tenant itself. VIGIL clinics are a second domain on the same foundation.
- **The slug is a DNS label first and an identifier second.** Every tenant is
  served from its own subdomain - `bright-star.xvs.codexng.com`, matched by the
  CORS origin regex in `settings.base` - so a school called "Support Academy"
  that took `support` would be served the help site instead of its own
  (`models.py:25-31`).
- **The reserved list lives here, not in the schools app**, because the names it
  protects are platform infrastructure. An ORGANIZATION tenant and a VIGIL
  clinic group get a subdomain off the same wildcard and must be held to the
  same list, and the engines may not import the schools app to reach it
  (`models.py:32-36`).
- **The reserved rule is enforced in `save()`, not on the field**, and the
  reason is blunt: field validators do not run on `Tenant.objects.create()`,
  which is how every writer in this codebase makes a tenant. A validator on the
  field would have looked like enforcement and caught nothing
  (`models.py:38-43`, `214-223`).
- **The slug freezes the first time a tenant goes live, and never thaws.**
  "Live" is `activated_at is not None or status == ACTIVE`
  (`models.py:200-203`). The first half carries the rule; the second is a
  fallback for a row activated by some path that forgot the stamp.
- **PENDING is deliberately excluded from the freeze.** A pending school's
  admins *can* already sign in, and they are the only people who can - the
  handful of admins setting the school up, one of whom is the person fixing the
  typo. Before go-live a rename costs two admins a re-bookmark; after it, it
  costs every parent their sign-in (`models.py:183-189`).
- **PLATFORM tenants are exempt from the reserved list**, because they *are* the
  infrastructure the list protects: `codex` is reserved precisely so no school
  can take the host the platform tenant already answers on
  (`models.py:141-148`).
- **`pending_since` is not `created_at`.** A school suspended for an abandoned
  onboarding and later reinstated is pending again from the moment it was
  reinstated, while its creation date stays where it always was. A sweep reading
  `created_at` would expire such a school again on its very next run, for ever
  (`models.py:109-115`).
- **`expiry_warned_at` belongs to the spell, not to the tenant.** A sweep that
  asked only "is this school within the warning window?" would answer yes every
  day and send the same warning a dozen times; one that never cleared the stamp
  would leave a reinstated school silently unwarnable for ever
  (`models.py:117-121`).
- **`Tenant` has no tenant-aware manager**, correctly: it *is* the boundary.
  `Tenant.objects` is a plain manager and every query against it is global.
- **Nothing in this app writes a tenant's status.** The writers are
  `schools.vs_schools.School.save()` (the mirror) and
  `schools.vs_onboarding.services.lifecycle` (suspension, reinstatement, the
  expiry sweep). Every one of them uses a queryset `update()` - which is the
  root of `tenant_code_issues` §1 and §2.
- **There is exactly one PLATFORM tenant, and it arrives by migration.**
  `vs_tenants/migrations/0002_seed_platform_tenant.py` creates `codex`. It is
  load-bearing infrastructure, not fixture data: CX user creation derives its
  home tenant from it, every platform permission seed grants into codex-owned
  roles, and the test suite builds its database from this chain.

## 2. Domain model

### `Tenant.Kind` (`models.py:81-84`)

| Value | Meaning |
|---|---|
| `PLATFORM` | CodeX itself. Exactly one row, `codex`, seeded by migration 0002. Exempt from the reserved-slug rule |
| `SCHOOL` | An XVS customer. Carries a `School` profile through the `school_profile` reverse one-to-one |
| `ORGANIZATION` | Any other customer - the shape VIGIL clinic groups take |

`Kind` has **no default**, so every writer must state it. `School.save()` passes
`Kind.SCHOOL` (`schools/vs_schools/models.py:281`).

### `Tenant.Status` (`models.py:86-90`) and who writes it

| Value | Written by |
|---|---|
| `PENDING` | `School.save()` mirror (the default for a new school); `vs_onboarding` reinstatement |
| `ACTIVE` | `School.save()` mirror when the school goes live; migration 0002 for `codex` |
| `SUSPENDED` | `vs_onboarding` expiry sweep, through the mirror or directly |
| `INACTIVE` | **Nothing reachable.** Only `School.save()`'s map produces it, from `SchoolStatus.INACTIVE`, which itself has no writer (see `docs/schools/school_records.md` §2) |

```python
AUTHENTICABLE_STATUSES = (Status.ACTIVE, Status.PENDING)
```

`models.py:97`. PENDING is admitted because a school has to onboard itself
before it goes live (FR-012); being admitted says nothing about which *surfaces*
are open, which is settled per view by `vs_rbac.permissions.TenantSurfaceAllowed`.
SUSPENDED and INACTIVE are refused outright, exactly as an unknown slug is
(`vs_rbac/authentication.py:109-113`).

### `Tenant` (`models.py:70`)

| Field | Meaning |
|---|---|
| `name` | Display name. Mirrored from `School.name` for school tenants |
| `slug` | `SlugField(80)`, unique, `tenant_slug_validator`. The `?tenant=` key and the sign-in subdomain |
| `kind` | `Kind`, no default |
| `status` | `Status`, default `PENDING`, indexed |
| `activated_at` | Written once, never cleared. The freeze reads it |
| `deactivated_at` | Describes the current state; cleared on activation |
| `pending_since` | Indexed. When the *current* PENDING spell began; null when not PENDING |
| `expiry_warned_at` | When this spell's warning was sent; cleared whenever the spell changes |
| `created_at`, `updated_at` | Standard |

Meta: ordering `["name", "id"]`, index on `(kind, status)`, and a
`tenant_slug_not_empty` check constraint complementing the validator.

`__str__` is the slug.

### `RESERVED_TENANT_SLUGS` (`models.py:44-62`)

A `frozenset` of 67 names in five groups, each group carrying its own reason:

| Group | Examples |
|---|---|
| Product and marketing hosts | `www`, `xvs`, `vigil`, `support`, `status`, `portal`, `legal` |
| The API and its neighbours | `api`, `app`, `auth`, `login`, `signup`, `oauth`, `sso`, `graphql` |
| Infrastructure and delivery | `admin`, `root`, `static`, `cdn`, `media`, `mail`, `ns1`, `vpn`, `metrics`, `health` |
| Environments | `dev`, `staging`, `test`, `demo`, `sandbox`, `preview`, `local` |
| The platform tenant and commercial surfaces | `codex`, `billing`, `payments`, `pay`, `checkout` |

Extending it needs no migration, because nothing about it is stored in the field
definition (`models.py:42-43`).

`slug_is_reserved(slug)` normalises (strip, lowercase) before testing
(`models.py:65-67`), so the callers do not each have to.

### `TenantDocumentSequence` (`models.py:753`)

Covered in `tenant_references_numbering`; named here because it is the only
other table this app owns besides `Branch` and `BranchLifecycle`.

### `TenantOwnedModel` (`models.py:739`)

An abstract base declaring a `tenant` FK with `PROTECT` and `related_name="+"`.
**No model in the repo inherits it** (`tenant_code_issues` §8).

## 3. Endpoint map

**None.** This app is imported, never routed. `apps/urls.py` has no
`vs_tenants` entry.

Its surface to the rest of the repo:

| Import | Consumed by |
|---|---|
| `vs_tenants.models.Tenant` | Effectively every app |
| `vs_tenants.models.Branch`, `BranchStatus`, `BranchLifecycle` | `vs_schools`, `vs_rbac`, `vs_user`, `vs_config`, `vs_procurement`, and more |
| `RESERVED_TENANT_SLUGS`, `slug_is_reserved` | `schools.vs_schools.models` re-exports both under the same names |
| `Tenant.pending_since_for`, `Tenant.pending_stamps_for` | `School.save()`, `vs_onboarding.services.lifecycle` |
| `vs_tenants.context.*` | `vs_rbac.authentication`, `vs_rbac.managers`, `vs_audit`, `vs_user` |
| `vs_tenants.middleware.TenantContextCleanupMiddleware` | Installed at `apps/settings/base.py:146` |
| `vs_tenants.numbering.next_tenant_document_number` | `School.save()`, and the finance/procurement/payments numbering |
| `vs_tenants.references.*` | `vs_config`, `vs_procurement`, `vs_user`, two `core` commands |
| `vs_tenants.exceptions.*` | `vs_schools` serializers, `vs_tenants.models` itself |

One management command: `manage.py reconcile_tenants`.

## 4. Lifecycle / state machine

```
   migration 0002                School.save()  (first save, tenant absent)
        │                                │
        ▼                                ▼
   codex: ACTIVE                    <school>: PENDING, pending_since stamped
   (activated_at set,                     │
    exempt from the                       │  vs_onboarding.go_live
    reserved list)                        ▼
                                     ACTIVE, activated_at stamped
                                     pending_since = None, expiry_warned_at = None
                                     ── the slug freezes here, for ever ──
                                          │
                                          │  vs_onboarding expiry sweep
                                          ▼
                                     SUSPENDED, deactivated_at stamped
                                          │
                                          │  vs_onboarding reinstatement
                                          ▼
                                     PENDING, pending_since re-stamped,
                                     expiry_warned_at cleared (a new spell)

   INACTIVE: declared, excluded from AUTHENTICABLE_STATUSES, reachable by
             no code path. See tenant_code_issues §5.
```

Every arrow after creation is drawn by another app. This one supplies the rules
(`pending_since_for`, `pending_stamps_for`, `activate`) and the guards
(`_assert_slug_allowed`, `_assert_slug_unchanged_once_live`).

The guards live in `save()`. Every arrow above is drawn with a queryset
`update()`, which does not call `save()` - which is `tenant_code_issues` §1
and §2.

## 5. Derivations

### `pending_since_for`

```python
@classmethod
def pending_since_for(cls, *, new_status, previous_status, current):
    if new_status != cls.Status.PENDING:
        return None
    if previous_status == cls.Status.PENDING and current is not None:
        return current
    return timezone.now()
```

`models.py:225-244`. Three rules, one place, because more than one writer has to
get it right - `School.save()` mirrors a school's status, and the onboarding
lifecycle service writes a tenant that has no school:

| Transition | Result |
|---|---|
| Leaving PENDING | `None` - the column describes the spell the tenant is in *now* |
| Entering PENDING | stamped `now` |
| Staying PENDING | the existing stamp, so an ordinary edit (a rename, a metadata fix) does not restart the expiry clock |

### `pending_stamps_for`

```python
new_pending_since = cls.pending_since_for(...)
if new_pending_since != pending_since:
    return new_pending_since, None
return new_pending_since, warned_at
```

`models.py:246-264`. A warning describes the spell it was sent during, so
whenever `pending_since` changes - a new spell begins, or the spell ends - the
warning stamp goes with it. A school put back into onboarding is warned again in
its new cycle instead of being silently skipped for ever. When the spell simply
continues, both are left exactly as they were.

`School.save()` calls this one (`schools/vs_schools/models.py:317-322`);
`School.save()`'s *first*-save branch calls the simpler `pending_since_for`,
because a brand-new tenant has no warning stamp to reconcile
(`schools/vs_schools/models.py:284-286`).

### The freeze test

```python
stored = Tenant.objects.filter(pk=self.pk).values("slug", "activated_at", "status").first()
if stored is None or stored["slug"] == self.slug:
    return
has_been_live = stored["activated_at"] is not None or stored["status"] == Status.ACTIVE
if not has_been_live:
    return
raise ValidationError({"slug": "This school is live, so its sign-in address is fixed. …"})
```

`models.py:191-212`. Read as a table:

| Stored state | Rename |
|---|---|
| Unsaved (`self.pk` is None) | Allowed - there is nothing to change |
| Slug unchanged | Allowed - it is not a rename |
| `activated_at` set | **Refused**, whatever the current status |
| `status == ACTIVE` | **Refused** |
| Anything else (PENDING, or SUSPENDED having never been live) | Allowed |

Three alternatives were considered and rejected in the docstring
(`models.py:165-181`): `status == ACTIVE` alone would let a suspended school
rename itself while it is off and come back at an address its families cannot
reach; `activated_at` alone would miss a row activated by a path that forgot the
stamp; and the onboarding gate's `ReadinessState.LIVE` is "a projection, not the
source of truth" by its own constants file, and a tenant that is not a school has
no readiness row at all.

`School._check_slug_change` (`schools/vs_schools/models.py:199-228`) applies the
same test to the school's own slug, and the comment there explains why both are
needed: guarding the tenant alone would leave the school's `/v1/i/<slug>/` path
key free to drift away from the sign-in address after go-live.

### `activate()`

```python
def activate(self):
    self.status = self.Status.ACTIVE
    self.activated_at = self.activated_at or timezone.now()
    self.deactivated_at = None
    self.pending_since = None
    self.expiry_warned_at = None
```

`models.py:266-272`. It mutates in memory and does **not** save, so a caller
must. `activated_at or now` is what makes the stamp write-once, which is what
the freeze depends on.

It is the only helper that gets all five columns right in one place - and it has
no production caller (`tenant_code_issues` §4).

### The codex seed

```python
Tenant.objects.get_or_create(
    slug="codex",
    defaults={"name": "CodeX", "kind": "PLATFORM", "status": "ACTIVE",
              "activated_at": timezone.now()},
)
```

`migrations/0002_seed_platform_tenant.py:15-25`. Idempotent, and reversible -
the reverse deletes the row, which `PROTECT` from a dozen models refuses the
moment codex owns anything, so the reversal is safe by construction rather than
by an explicit check.

Note the migration writes through the **historical** model, so `Tenant.save()`'s
guards do not run - which is correct here, since `codex` is on the reserved list
it would otherwise be refused by.

### The invariants `reconcile_tenants` asserts

`management/commands/reconcile_tenants.py:28-53`, in order:

| Check | Query |
|---|---|
| Exactly one codex PLATFORM tenant | `Tenant.objects.filter(kind=PLATFORM, slug="codex").count() != 1` |
| No user without a tenant | `User.objects.filter(tenant__isnull=True)` |
| No branch without a tenant | `Branch.all_objects.filter(tenant__isnull=True)` |
| No ledger entity without a tenant | `LedgerEntity.objects.filter(tenant__isnull=True)` |
| No role template whose branch is another tenant's | `TenantRoleTemplate.filter(branch__isnull=False).exclude(tenant=F("branch__tenant"))` |
| No assignment whose user is another tenant's | `TenantUserRoleAssignment.exclude(tenant=F("user__tenant"))` |
| No assignment whose role is another tenant's | `TenantUserRoleAssignment.exclude(tenant=F("role__tenant"))` |

Failures accumulate and are raised together as one `CommandError`. The command's
own docstring records why it is shaped this way: two checks were left pointing at
`User.school` and `LedgerEntity.source_school` after the refactor dropped both
columns, so the whole command raised `FieldError` on the first line it reached
and none of the surviving invariants were ever verified. A third check -
"schools without tenants" - could never fail, because `School.tenant` is a
non-nullable one-to-one.

It does **not** check the assignment's *branch* against its tenant, which is the
one cross-tenant column of the three that `TenantUserRoleAssignment.clean()`
guards (`tenant_code_issues` §6).

## 6. What writing writes

`Tenant.save()` writes one row and runs two guards
(`models.py:214-223`):

```python
self.slug = (self.slug or "").strip().lower()
self._assert_slug_allowed()
self._assert_slug_unchanged_once_live()
return super().save(*args, **kwargs)
```

The normalisation is done here as well as in `clean()`, because `clean()` is not
called on the `objects.create()` path either.

`Tenant` emits **one** signal that anything listens to:

```python
@receiver(post_save, sender=Tenant, dispatch_uid="vs_admin_console.tenant_deactivated")
def on_tenant_saved(sender, instance, **kwargs):
    if instance.status != Tenant.Status.ACTIVE:
        end_impersonations_for_tenant(instance)
```

`vs_admin_console/receivers.py:15-19`. The file's own header states the intent -
*"Keeps impersonation state in sync with tenant lifecycle without vs_tenants
having to import vs_admin_console: when a Tenant leaves ACTIVE, every active
impersonation session scoped to it must be ended"* - and the inline comment adds
*"Deactivation must immediately stop staff from acting inside the tenant."*

No production path saves a `Tenant` instance to change its status, so that
receiver does not run in production. `tenant_code_issues` §1.

Nothing in this app emits an `AuditEvent` for a tenant change. A school's status
change is audited by `vs_onboarding`; a school's rename is audited by
`SchoolUpdateSerializer` (`docs/schools/school_records.md` §6). A tenant that is
not a school produces no audit row for a status change at all.

## 7. Worked example

Bright Star Academy, from creation to a frozen address.

**1. Creation.** `School.objects.create(slug="bright-star", ...)` runs
`School.save()`, which finds no tenant and creates one:

```python
Tenant.objects.create(
    name="Bright Star Academy", slug="bright-star", kind=Kind.SCHOOL,
    status=Status.PENDING, activated_at=None,
    pending_since=Tenant.pending_since_for(
        new_status=PENDING, previous_status=None, current=None),
)
```

`objects.create()` calls `save()`, so both guards run. `bright-star` is not
reserved. `self.pk` is None, so the freeze returns immediately. `pending_since`
is stamped now, because the tenant is entering PENDING.

**2. The typo.** The school meant `bright-star-academy`. A CX operator PATCHes
the school; `SchoolUpdateSerializer.validate_slug` checks it is not reserved, not
taken by another school, and not taken by another tenant, and
`School._check_slug_change` finds the school has never been live, so the rename
is allowed. `School.save()` then pushes the new slug onto the tenant.

That push is `Tenant.objects.filter(pk=...).update(slug="bright-star-academy", ...)`
(`schools/vs_schools/models.py:345`). It does not call `Tenant.save()`, so
neither tenant-side guard runs - it is the serializer that caught the reserved
name, not the model. Had the same rename been made from a shell
(`school.slug = "admin"; school.save()`), nothing would have stopped it
(`tenant_code_issues` §2).

**3. Go-live.** `vs_onboarding.services.go_live` sets `School.status = ACTIVE`.
`School.save()` maps that to `Tenant.Status.ACTIVE`, reads the tenant's previous
status (PENDING), and calls `pending_stamps_for(new_status=ACTIVE,
previous_status=PENDING, …)`. `pending_since_for` returns `None` because the new
status is not PENDING; `None != pending_since`, so both columns clear. The
mirror writes `status=ACTIVE`, `activated_at`, `pending_since=None`,
`expiry_warned_at=None` - and, because `slug_is_frozen` is now True, it does
**not** write the slug.

**4. The freeze bites.** A second rename attempt is refused twice over: the
serializer raises `TenantSlugFrozen` (a typed 409), and behind it
`School._check_slug_change` would refuse it too. `Tenant._assert_slug_unchanged_once_live`
would be the third layer - if anything called `Tenant.save()`.

**5. An ordinary edit does not restart the clock.** Six months later somebody
fixes the school's motto. `School.save()` reads the tenant's previous status
(ACTIVE), computes `pending_stamps_for(new_status=ACTIVE, previous_status=ACTIVE, …)`
→ `(None, None)`, unchanged. Nothing about the onboarding spell moves, which is
exactly what `pending_since` exists to guarantee.

**6. Suspension.** An unrelated school, Greenfield, never finished onboarding.
The expiry sweep sets `School.status = SUSPENDED`, and the mirror writes
`Tenant.Status.SUSPENDED` with `pending_since=None` and `expiry_warned_at=None` -
the spell has ended, so neither column describes anything.

Greenfield's users can no longer sign in: `SUSPENDED` is not in
`AUTHENTICABLE_STATUSES`, so `TenantJWTAuthentication` answers *"No tenant
matches the requested context"* - the same answer an unknown slug gets, so a
suspended tenant is not distinguishable from a nonexistent one.

**7. What did not happen.** A CX support engineer had an active impersonation
session inside Greenfield. `on_tenant_saved` was written to end it. The
suspension went through `queryset.update()`, so `post_save` never fired, the
session is still ACTIVE, and the engineer can keep working inside the suspended
tenant until the session's own idle timeout expires it
(`vs_rbac/authentication.py:39-55`). That is `tenant_code_issues` §1, and it is
confirmed by execution.

**8. Reinstatement.** Greenfield pays. `vs_onboarding` sets the school back to
PENDING; the mirror computes `pending_since_for(new_status=PENDING,
previous_status=SUSPENDED, current=None)` → `now`, so a *new* spell begins and
`expiry_warned_at` clears with it. Greenfield gets the full warning cycle again
rather than being expired on the sweep's next run.

## 8. Gotchas / known limitations

Recorded in full in **`error/tenants/tenant_code_issues.md`**. The items
belonging to this slice:

| # in that file | One line |
|---|---|
| §1 | **Confirmed by execution.** Every production status change uses `queryset.update()`, so the `post_save` receiver that ends impersonation sessions inside a suspended tenant never fires |
| §2 | **Confirmed by execution.** The same bypass voids both slug guards the model docstring says "no writer can miss" |
| §4 | `Tenant.activate()` - the one helper that gets all five lifecycle columns right - has no production caller |
| §5 | **Confirmed by execution.** `Tenant.Status.INACTIVE` is unreachable, and would lock every user out if it were reached |
| §6 | `reconcile_tenants` checks two of the three cross-tenant columns on a role assignment and misses `branch` |
| §13 | `Tenant` has no `suspend()`/`deactivate()` counterpart to `activate()`, so four call sites hand-write the same five columns |

Design choices worth stating as choices - this model is unusually well argued and
most of its surprises are deliberate:

- **Guards in `save()` rather than on the field** (`models.py:214-219`), because
  nothing in this codebase creates a tenant through a form.
- **The freeze reads `activated_at`, not `status`** (`models.py:165-178`), so a
  school suspended for an unpaid invoice cannot rename itself while it is off.
- **PENDING is outside the freeze** (`models.py:183-189`), because the only
  people affected before go-live are the admins doing the onboarding.
- **PLATFORM is exempt from the reserved list** (`models.py:141-145`) - `codex`
  is reserved *for* the platform tenant.
- **`pending_since` separate from `created_at`** (`models.py:109-115`) and
  **`expiry_warned_at` tied to the spell** (`models.py:117-121`): both exist
  because a reinstated school broke the naive version.
- **The reserved set needs no migration** (`models.py:42-43`), because it is not
  part of the field definition.

## 9. Permissions & tenant isolation

- **This app enforces no permissions.** It has no views, so there is no
  `rbac_permission` anywhere in it. What it provides is the *boundary* those
  permissions are evaluated against.
- **`Tenant.objects` is global and must be.** `vs_rbac.managers.TenantAwareManager`
  scopes rows *by* tenant; scoping the tenant table itself would be circular.
  Every consumer that queries tenants is therefore responsible for its own
  filtering - which is why `resolve_tenant`, `find_tenant` and
  `TenantJWTAuthentication` each apply their own.
- **Three properties do the isolation work**, and all three are on this model:
  `slug` uniqueness (so a `?tenant=` assertion names exactly one row), `status`
  membership in `AUTHENTICABLE_STATUSES` (so a suspended tenant is refused at
  the door), and `kind == PLATFORM` (which `vs_rbac` reads for the platform
  scope guard, the cross-tenant parameter and `IsVisionStaff`).
- **`kind` is the platform/tenant boundary for RBAC.**
  `vs_rbac.models.tenant_is_platform` is `tenant.kind == Kind.PLATFORM`
  (`vs_rbac/models.py:85-88`), and it decides whether a `PermissionScope.PLATFORM`
  key may be held at all. A tenant whose `kind` was wrong would hand a school
  every platform key in the registry.
- **`Tenant` is `PROTECT` from everything.** `School.tenant`,
  `Branch.tenant`, `TenantRoleTemplate.tenant`, `TenantUserRoleAssignment.tenant`,
  `UserPermissionOverride.tenant`, `ImpersonationSession.tenant`,
  `TenantDocumentSequence.tenant` and more all use `on_delete=PROTECT`, so a
  tenant with any history cannot be deleted. There is no delete path anywhere
  and there should not be.
- **The reserved list is a security control, not cosmetics.** A tenant that took
  `api` or `auth` would be served from a host the platform answers on, and the
  CORS origin regex would treat it as first-party.

## 10. Code map

| File | What lives there |
|---|---|
| `vs_tenants/models.py:19-22` | `tenant_slug_validator` |
| `vs_tenants/models.py:25-67` | `RESERVED_TENANT_SLUGS` and `slug_is_reserved`, with the reasoning for both the contents and the location |
| `vs_tenants/models.py:70-134` | `Tenant` fields, `Kind`, `Status`, `AUTHENTICABLE_STATUSES`, Meta |
| `vs_tenants/models.py:135-154` | `clean()`, `_assert_slug_allowed` |
| `vs_tenants/models.py:156-212` | `_assert_slug_unchanged_once_live` and the three rejected alternatives |
| `vs_tenants/models.py:214-223` | `save()` |
| `vs_tenants/models.py:225-264` | `pending_since_for`, `pending_stamps_for` |
| `vs_tenants/models.py:266-275` | `activate()` |
| `vs_tenants/models.py:739-750` | `TenantOwnedModel` (unused) |
| `vs_tenants/migrations/0002_seed_platform_tenant.py` | The codex row |
| `vs_tenants/migrations/0005_tenant_pending_since.py` | `pending_since` and its backfill |
| `vs_tenants/migrations/0006_tenant_expiry_warned_at.py` | `expiry_warned_at` |
| `vs_tenants/management/commands/reconcile_tenants.py` | The seven invariants |
| `vs_admin_console/receivers.py` | The one `post_save` listener on `Tenant` |
| `schools/vs_schools/models.py:265-346` | `School.save()`, the mirror, and the only place a school tenant's columns move |
| `schools/vs_onboarding/services/lifecycle.py:185-209`, `:460-486` | Suspension and reinstatement for tenants with and without a school |
| `vs_rbac/authentication.py:101-138` | Where `AUTHENTICABLE_STATUSES` is applied |
| `vs_rbac/models.py:85-88` | Where `kind` becomes the permission-scope boundary |

## 11. Test coverage & gaps

Module baseline: **`Ran 62 tests in 4.805s` - OK**
(`cd apps && DB_NAME=cx_tenantslice ../cx/Scripts/python.exe manage.py test
vs_tenants --settings=apps.settings.local --noinput`).

Covered for this slice:

- `tests.py::TenantSlugRuleTests` - the reserved list, the PLATFORM exemption,
  the freeze after go-live, and that PENDING may still rename.
- `tests.py::TenantFoundationTests` - that creating a school atomically
  provisions its tenant, and the `resolve_tenant` behaviours.
- `tests.py::ReconcileTenantsInvariantTests` - each surviving invariant, and
  that the command no longer raises `FieldError`.
- `tests.py::TenantAuthorityTests` - that classifying a user grants nothing,
  which is the tenant-refactor property this app exists to hold.

Not covered:

- **No test changes a tenant's status the way production does** - through
  `queryset.update()` - and asserts what did or did not happen. That single gap
  hides §1 and §2.
- No test calls `Tenant.activate()` from anything but a test, which is how §4
  survived.
- No test exercises `Tenant.Status.INACTIVE` (§5), because nothing can produce
  it.
- No test asserts a role assignment whose *branch* belongs to another tenant is
  caught by `reconcile_tenants` (§6).
- `pending_stamps_for` is tested indirectly through the school mirror; there is
  no direct unit test of the four transitions it encodes.
- `TenantOwnedModel` has no test, because it has no subclass.
