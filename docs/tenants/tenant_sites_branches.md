# tenant_sites_branches

A tenant's physical sites, from the platform side: the `Branch` row, the
per-tenant code allocator and the two locks that make it safe, the
one-main-branch invariant and the handover that keeps it satisfiable, the
five-state lifecycle graph and the guards layered on top of it, the
`BranchLifecycle` audit table, and the six typed exceptions this app raises.

The tenant itself is `tenant_identity_lifecycle`; the request context is
`tenant_request_context`; resolving a branch reference is
`tenant_references_numbering`.

**The HTTP surface that drives all this lives in another app.** The six branch
routes under `/v1/i/<slug>/branches/` belong to `schools.vs_schools` and are
documented in **`docs/schools/school_branches.md`**. This slice is the model
contract underneath them: what the database guarantees, what `transition()`
refuses, and why.

Findings for the whole module are collected in
**`error/tenants/tenant_code_issues.md`**; §8 below points at the ones belonging
here rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **"Branch" is not school vocabulary.** A bank, a clinic chain and a retail
  group all have branches, so the model belongs beside `Tenant` rather than
  inside the schools product (`models.py:289-296`). It used to hang off
  `vs_schools.School`, and every app that needed the owning tenant had to travel
  `branch.school.tenant`; the tenant is now a column on the row.
- **The table did not move with the class.** `db_table = "vs_schools_branch"`
  (`models.py:391`), and `BranchLifecycle` keeps
  `db_table = "vs_schools_branchlifecycle"` (`models.py:732`). Keeping the class
  name, the integer primary key and the table meant the move changed only
  Django's model state: no row moved, no foreign key constraint was rebuilt, and
  every `branch_id` already stored anywhere - including in issued JWTs - still
  names the same site (`models.py:297-301`).
- **Codes are unique per tenant, not globally.** `uq_branch_tenant_code`
  (`models.py:402-405`). Every tenant's first site is code 1, which is why
  callers must always pair a code with a tenant, and why audit events are keyed
  on the primary key instead.
- **Exactly one main branch per tenant, by partial unique index.**
  `uq_branch_one_main_per_tenant` (`models.py:410-414`). Every engine this repo
  runs on supports partial indexes - PostgreSQL local, CI and staging, SQLite in
  `apps.settings.test`; the MariaDB fallback that could not was retired
  2026-06-12 (`models.py:406-409`).
- **`_assert_single_main()` is not the guarantee, the index is.** The Python
  check exists so the ordinary path fails as a field error the API can render,
  instead of surfacing an `IntegrityError` as a 500 (`models.py:435-441`).
- **The main branch may never leave service.** Not just CLOSED - every
  out-of-service state is guarded, because a SUSPENDED or INACTIVE main branch is
  already wrong: every reader of `School.main_branch` and every default-branch
  pick lands on a site nobody may be posted to, silently (`models.py:550-557`).
- **`CLOSED` is terminal and `PENDING` is never a target.** A shut-down branch is
  re-created, not resurrected; "pending activation" is a fact about a branch that
  has never opened, and activation cannot be undone (`models.py:520-524`).
- **`IN_SERVICE_STATES` is derived, and stated positively.** Derived from the
  choices so a new lifecycle state cannot be silently treated as in service;
  positive because the RBAC authorisation filters join through a *nullable*
  branch column, where a negative filter would also drop the whole-tenant grants
  that must always count (`models.py:510-518`).
- **`transition()` is the only door.** The `mark_*` helpers, the API's transition
  serializer and the shell all run through it, so the main-branch guard is stated
  once and cannot be walked around by adding another caller
  (`models.py:643-646`).
- **The status write and the history row are one unit.** A branch must never
  change state without the entry that explains it - hence
  `@transaction.atomic` on `transition()` (`models.py:631`, `640-641`).
- **`Branch.objects` is tenant-aware; `Branch.all_objects` is not.**
  `default_manager_name = "objects"`, `base_manager_name = "all_objects"`, so FK
  traversal stays unscoped (`models.py:385-393`). Platform code that must reach
  another tenant's sites says `all_objects` out loud.
- **This app never deletes a branch.** There is no delete path, and
  `Branch.tenant` is `PROTECT` in the other direction.

## 2. Domain model

### `BranchStatus` (`models.py:278-285`)

Five values, and the docstring is explicit that "nothing here is school-shaped":
`ACTIVE`, `PENDING` ("Pending Activation"), `SUSPENDED`, `INACTIVE`, `CLOSED`.

Two derived sets on `Branch` (`models.py:502-518`):

| Set | Members | Purpose |
|---|---|---|
| `OUT_OF_SERVICE_STATES` | `SUSPENDED`, `INACTIVE`, `CLOSED` | Entering any stamps `deactivated_at`; the main-branch guard fires on these |
| `IN_SERVICE_STATES` | `frozenset(BranchStatus.values) - OUT_OF_SERVICE_STATES`, i.e. `PENDING`, `ACTIVE` | Read by `vs_rbac.evaluator` and `vs_rbac.scoping` to decide whether a branch-pinned grant still counts |

Written as a set difference rather than a comprehension because a comprehension
in a class body cannot see the class-level name above it (`models.py:516-517`).

### `Branch` (`models.py:288`)

| Field | Meaning |
|---|---|
| `tenant` | FK, PROTECT, indexed, `related_name="branches"`. Every creation path must supply it |
| `name` | Display label |
| `code` | `PositiveIntegerField`, `editable=False`, indexed, non-null. Allocated 1..N on first save |
| `is_main` | The canonical site |
| `_type` | Optional free-form descriptor. Nothing branches on it |
| `address`, `email`, `country`, `state` | Contact and location. `country` defaults to `"Nigeria"` |
| `status` | `BranchStatus`, indexed, default `PENDING` |
| `opened_at`, `closed_at`, `activated_at`, `deactivated_at` | Lifecycle stamps |
| `created_at`, `updated_at` | Standard |

`_type` was `CharField(max_length=80)` with no `blank=True` - optional in prose
and required in the schema. Every row made outside the serializers (`seed_import`,
a data migration, the shell, the test factories) stored `""` and was then
permanently unpatchable through the API, because `BranchUpdateSerializer` runs
`full_clean()` over the whole instance and the blank it refused was one nobody
had touched (`models.py:349-359`).

Indexes: `(tenant, is_main)`, `(tenant, status)`, `(tenant, code)`. Ordering
`-created_at`.

`__str__` is `f"{self.tenant.slug}:{self.code}"`. It was `school.slug:code`, and
the two can differ for a renamed school, so this string is a debugging repr and
is used in no API response or audit label (`models.py:418-425`).

### `BranchLifecycle` (`models.py:691`)

| Field | Meaning |
|---|---|
| `branch` | FK, CASCADE, `related_name="lifecycle_events"` |
| `from_state` | Blank for a creation event - "a creation event has no state to come from" |
| `to_state` | Required - "an event that does not say where the branch went is not an event" |
| `actor_id` | `CharField(120)`, blank-able. Written as `str(actor_id or "")` |
| `reason` | `TextField`, blank-able. `default=None` once made every writer that omitted it raise `IntegrityError` |
| `occurred_at` | Indexed, defaults to now |

Indexed on `(branch, occurred_at)` and `(branch, to_state)`.

It travels with `Branch` because `Branch.transition` writes it: leaving it behind
would have meant this app importing the schools app on every lifecycle change
(`models.py:700-702`).

`actor_id` is a free-text column, and its three writers put three different kinds
of value in it - see `docs/schools/school_branches.md` §5 and
`error/schools/school_code_issues.md` §10.

### The six exceptions (`exceptions.py`)

Each carries the typed `error_code` / `message` pair that
`core.exceptions.custom_exception_handler` renders into the platform envelope, so
a service can refuse an operation without every calling view wrapping it in a
`try/except` (`exceptions.py:1-6`).

| Exception | Code | HTTP | Raised by |
|---|---|---|---|
| `TenantsError` | `TENANTS_ERROR` | 400 | base |
| `BranchLifecycleError` | `BRANCH_LIFECYCLE_ERROR` | **409** | base for the four below |
| `InvalidBranchTransition` | `INVALID_BRANCH_TRANSITION` | 409 | `transition()` for an edge outside the graph |
| `BranchAlreadyInState` | `BRANCH_ALREADY_IN_STATE` | 409 | the API serializer, **not** the model - `transition()` treats it as a no-op |
| `MainBranchCannotLeaveService` | `MAIN_BRANCH_CANNOT_LEAVE_SERVICE` | 409 | `_assert_may_leave_service` when a sibling exists |
| `LastBranchCannotLeaveService` | `LAST_BRANCH_CANNOT_LEAVE_SERVICE` | 409 | `_assert_may_leave_service` when it is the only branch |
| `BranchNotInService` | `BRANCH_NOT_IN_SERVICE` | 409 | `promote_to_main` for an out-of-service candidate |
| `TenantSlugFrozen` | `TENANT_SLUG_FROZEN` | **409** | `SchoolUpdateSerializer.validate_slug` |
| `TenantNotLive` | `TENANT_NOT_LIVE` | **403** | `vs_rbac.permissions.TenantSurfaceAllowed` |

409 rather than 400 throughout the lifecycle family, and the reasoning is stated:
these are conflicts with the branch's current state rather than malformed input
(`exceptions.py:29-33`). `TenantSlugFrozen` joins them because "the payload is
well-formed and would have been accepted yesterday; what refuses it is the
school's current state" (`exceptions.py:139-141`).

`TenantNotLive` is 403 and deliberately **not** 404: 404 is reserved for a caller
asserting a tenant that is not theirs, where even the existence of the tenant
must stay hidden (`exceptions.py:161-164`).

These exceptions live here rather than in the schools app because they describe a
*site* lifecycle: a clinic chain or a retail group refuses the same edges for the
same reasons, and `Branch.transition` raises them, so leaving them behind would
have meant a platform model importing a product app (`exceptions.py:8-11`).

## 3. Endpoint map

**None in this app.** The six routes that drive these models are
`schools.vs_schools`'s, and `docs/schools/school_branches.md` §3 documents them
with their permission keys and bodies.

What this app publishes instead:

| Callable | Called by |
|---|---|
| `Branch.allocate_next_code(tenant_id=…)` | `Branch.save()` only |
| `Branch.save()` | every creation path |
| `Branch.transition(to_state=…, actor_id=…, reason=…)` | `BranchStateTransitionSerializer`, the `mark_*` helpers, the shell |
| `Branch.mark_active` / `suspend` / `reactivate` / `mark_inactive` | thin wrappers over `transition()`; no production caller today |
| `Branch.promote_to_main(actor_id=…)` | `BranchUpdateSerializer.update` |
| `Branch.IN_SERVICE_STATES` | `vs_rbac.evaluator._assignment_branch_q`, `vs_rbac.scoping._grant_scope`, `BranchUpdateSerializer.validate` |
| `Branch.all_objects` | `vs_tenants.references.find_branch_in_tenant`, the allocator, the main-branch clash checks |

## 4. Lifecycle / state machine

`ALLOWED_TRANSITIONS` (`models.py:525-539`):

| From | May go to |
|---|---|
| `PENDING` | `ACTIVE`, `INACTIVE`, `CLOSED` |
| `ACTIVE` | `SUSPENDED`, `INACTIVE`, `CLOSED` |
| `SUSPENDED` | `ACTIVE`, `INACTIVE`, `CLOSED` |
| `INACTIVE` | `ACTIVE`, `CLOSED` |
| `CLOSED` | *(nothing)* |

Two rules shape the table: CLOSED is terminal, and PENDING is never a target.
`SUSPENDED` is reachable only from `ACTIVE` - you cannot suspend what was never
trading.

Three guards run in `transition()`, in this order (`models.py:648-655`):

```
1.  from_state == to_state          → return, silently. Idempotent by design.
2.  edge not in ALLOWED_TRANSITIONS → InvalidBranchTransition (409)
3.  _assert_may_leave_service       → MainBranchCannotLeaveService (409)
                                    or LastBranchCannotLeaveService (409)
```

Then the stamps (`models.py:657-671`):

| Target | Stamps |
|---|---|
| `ACTIVE` | `activated_at` if still null (first activation, never rewritten); `deactivated_at = None` |
| any out-of-service state | `deactivated_at = now` |
| `CLOSED` specifically | `closed_at = now` if still null - mirroring `clean()`, because `transition()` saves with `update_fields` and so never runs `full_clean()` |

And finally one `save(update_fields=[...])` plus one `BranchLifecycle` row, both
inside the same atomic block.

The main-branch guard, in full (`models.py:541-579`):

```
to_state not out-of-service, or not is_main  → return
        │
        ▼
  does any sibling exist in this tenant?
        │                          │
       yes                         no
        │                          │
        ▼                          ▼
MainBranchCannotLeaveService  LastBranchCannotLeaveService
"promote a sibling first"     "deactivate the school itself instead"
```

Only `is_main` rows are checked, and that is the whole invariant rather than a
shortcut: a tenant has exactly one main branch, so retiring a *non*-main branch
can never leave the tenant without an in-service main one, while retiring the
main branch always does (`models.py:544-549`).

## 5. Derivations

### The next code

```python
@staticmethod
def allocate_next_code(*, tenant_id: int) -> int:
    Tenant.objects.select_for_update().only("id").get(pk=tenant_id)
    current_max = (
        Branch.all_objects.filter(tenant_id=tenant_id).aggregate(m=Max("code"))["m"] or 0
    )
    return current_max + 1
```

`models.py:450-475`. Three decisions, each documented at the call site and each
load-bearing:

- **It locks the *tenant* row.** An older version locked
  `select_for_update().filter(school=school)`, which locks nothing at all when
  the owner has no branches yet - so two concurrent first-branch creates both
  read max 0 and both wrote code 1. The tenant row always exists (`tenant` is
  non-nullable), so locking it serialises allocation for an empty tenant exactly
  as well as for a full one.
- **It reads through `all_objects`.** `objects` is the `TenantAwareManager`, and
  under an ambient tenant context that differs from `tenant_id` - platform code
  creating a branch for a customer, which is every call in practice - it would
  aggregate over zero rows and hand back a duplicate code.
- **It must run inside a transaction**, and `save()` opens one.

```python
def save(self, *args, **kwargs):
    self._assert_single_main()
    if not self.code:
        with transaction.atomic():
            self.code = Branch.allocate_next_code(tenant_id=self.tenant_id)
            super().save(*args, **kwargs)
        return
    return super().save(*args, **kwargs)
```

`models.py:477-486`. Allocation happens only when `code` is missing or zero, so a
re-save never renumbers.

`vs_tenants.tests` covers this with a `TransactionTestCase`; on the schools side
`BranchCodeAllocationConcurrencyTests` is one of the two classes tagged `slow`
that make that app's suite take an hour, and it exists for exactly this function.

### The handover

```python
@transaction.atomic
def promote_to_main(self, *, actor_id: str = "", reason: str = ""):
    if self.status not in self.IN_SERVICE_STATES:
        raise BranchNotInService(branch_name=self.name, status=self.status)
    if self.is_main:
        return self
    Tenant.objects.select_for_update().only("id").get(pk=self.tenant_id)
    demoted = list(Branch.all_objects.filter(tenant_id=self.tenant_id, is_main=True)
                   .exclude(pk=self.pk).values_list("pk", flat=True))
    if demoted:
        Branch.all_objects.filter(pk__in=demoted).update(is_main=False, updated_at=timezone.now())
    self.is_main = True
    self.save(update_fields=["is_main", "updated_at"])
    return self
```

`models.py:581-629`. Two things make it safe, and both are stated:

- **Demote before promote.** `uq_branch_one_main_per_tenant` is an ordinary,
  non-deferrable partial unique index, so the two writes cannot be reordered or
  batched: promoting first would trip the index even though the end state is
  legal.
- **Lock the tenant row first**, exactly as `allocate_next_code` does. Two admins
  promoting two different branches at the same moment would otherwise both read
  one incumbent, both demote it, and both insert a main branch.

It is idempotent on purpose, so the serializer and any retry can call it without
first asking whether it is needed.

**No `BranchLifecycle` row is written**, and `actor_id` is accepted only so
callers can pass it symmetrically with `transition()`. That table records status
edges, and a handover changes no status; the branch update serializer already
emits an audit event whose diff carries `is_main` (`models.py:601-605`).

### Where a branch's liveness becomes an access decision

`Branch.IN_SERVICE_STATES` is read by two functions in `vs_rbac`, and they are
the reason it is a positive set:

```python
# vs_rbac/evaluator.py:74-81 - may this person act?
live = Q(branch__status__in=Branch.IN_SERVICE_STATES)
if branch is ANY_BRANCH:
    return Q(branch__isnull=True) | live
```

```python
# vs_rbac/scoping.py:96-100 - whose rows may they see?
return frozenset(
    branch_id for branch_id, status in rows if status in Branch.IN_SERVICE_STATES
)
```

So closing a branch withdraws, in the same breath, both the permission its
holders had through a pinned grant and the visibility that grant conferred. See
`docs/rbac/rbac_evaluation_scoping.md` §5.

## 6. What writing writes

| Operation | Rows |
|---|---|
| First `save()` | One `Branch`, with `code` allocated under a lock on the tenant row |
| Subsequent `save()` | The `Branch` only; `_assert_single_main` runs first |
| `transition()` | The `Branch` (`status`, `activated_at`, `deactivated_at`, `closed_at`, `updated_at`) **and** one `BranchLifecycle`, atomically |
| `promote_to_main()` | One UPDATE demoting the incumbent, then the `Branch` (`is_main`, `updated_at`). No lifecycle row |
| `clean()` | Nothing; it stamps `closed_at` in memory and asserts the single-main rule |

`Branch` emits no signals and no audit events. Every `AuditEvent` for a branch is
emitted by the schools app's serializers
(`docs/schools/school_branches.md` §6), which means a `transition()` called from
anywhere but that serializer - the shell, a management command, a future service -
writes a `BranchLifecycle` row and **nothing** in the central audit trail.

`BranchLifecycle` has no endpoint, no export dataset and no reader anywhere in
the repo (`tenant_code_issues` §7).

## 7. Worked example

A clinic group on VIGIL, to make the point that none of this is school-shaped.

**1. Two sites.** `Tenant(name="Stella Maris Clinics", slug="stella-maris",
kind=ORGANIZATION)` is created. Two `Branch` rows follow. The first save finds no
code, opens a transaction, locks the tenant row, aggregates `Max("code")` over
`all_objects` - zero rows, so `0` - and writes code 1. The second gets code 2.
Had both requests arrived at the same instant, the tenant-row lock would have
serialised them; the older allocator, which locked the branch rows, would have
given both code 1.

**2. Making one canonical.** Site 1 is created with `is_main=True`.
`_assert_single_main` finds no clash. `uq_branch_one_main_per_tenant` accepts it.

**3. Opening them.** `branch.transition(to_state=ACTIVE, actor_id="…")`.
`PENDING → ACTIVE` is in the graph. `activated_at` is stamped for the first time,
`deactivated_at` cleared, and a `BranchLifecycle` row from `PENDING` to `ACTIVE`
lands in the same transaction.

**4. Suspending site 2.** `ACTIVE → SUSPENDED` is allowed, site 2 is not main, so
`_assert_may_leave_service` returns immediately. `deactivated_at` is stamped.

Anyone whose RBAC grant was pinned to site 2 loses access in the same instant:
`_assignment_branch_q` stops counting the grant because site 2's status has left
`IN_SERVICE_STATES`, and `_grant_scope` narrows their visible set rather than
falling back to their home posting.

**5. Trying to suspend site 1.** `_assert_may_leave_service` sees `is_main` and
an out-of-service target, finds a sibling (site 2 exists, suspended or not - the
check is `exists()`, not `is in service`), and raises
`MainBranchCannotLeaveService`: *"'Ikeja Clinic' is the main branch. Make
another branch the main branch first, then take this one out of service."*

**6. Following the advice fails.** Site 2 is the only sibling and it is
SUSPENDED, so `promote_to_main` raises `BranchNotInService`. The group must
reactivate site 2 first (`SUSPENDED → ACTIVE` is allowed), promote it, and only
then suspend site 1.

That sequence is correct, but the first refusal does not say it: the sibling
check is `exists()` on any sibling, while the promotion requires an *in-service*
one, so the advice can point at a branch that cannot take the handover
(`tenant_code_issues` §9).

**7. Closing site 2 for good.** `ACTIVE → CLOSED`. `clean()`'s stamp is mirrored
inside `transition()` because the save uses `update_fields` and so skips
`full_clean()`. `ALLOWED_TRANSITIONS[CLOSED]` is empty, so nothing can move it
again - and if it had been main, the partial unique index would then refuse to
make any survivor main, which is the permanent dead end the guard exists to
prevent.

**8. A one-site group.** Stella Maris had opened only one clinic. Suspending it
raises `LastBranchCannotLeaveService`: *"'Ikeja Clinic' is the only branch, and
every school must keep one in service. Deactivate the school itself instead."*
The message says "school" to an organization tenant, which is the one place
school vocabulary leaked into this app (`tenant_code_issues` §10).

## 8. Gotchas / known limitations

Recorded in full in **`error/tenants/tenant_code_issues.md`**. The items
belonging to this slice:

| # in that file | One line |
|---|---|
| §7 | `BranchLifecycle` has no reader anywhere: no endpoint, no export dataset, no admin screen |
| §9 | The main-branch refusal counts any sibling while promotion requires an in-service one, so its advice can point at a branch that cannot take the handover |
| §10 | `LastBranchCannotLeaveService` says "every school must keep one in service" to tenants that are not schools |
| §11 | The four `mark_*` lifecycle helpers have no production caller |
| §13 | `Branch` writes no audit event of its own, so a `transition()` from anywhere but the schools serializer leaves nothing in the central trail |

Design choices worth stating as choices - almost everything surprising in this
model is deliberate and argued in place:

- **Locking the tenant row, not the branch rows** (`models.py:454-459`).
- **Reading through `all_objects` in the allocator** (`models.py:461-464`).
- **Demote-then-promote** (`models.py:585-592`), required by the non-deferrable
  partial unique index.
- **Guarding every out-of-service state, not only CLOSED**
  (`models.py:550-557`).
- **`IN_SERVICE_STATES` derived and positive** (`models.py:510-518`), because a
  negative filter across a nullable join would drop whole-tenant grants.
- **`BranchAlreadyInState` raised by the API and not the model**
  (`models.py:636-638`), so the helpers stay idempotent while an HTTP caller is
  still told their request changed nothing.
- **Keeping `db_table = "vs_schools_branch"`** (`models.py:388-391`), which
  avoided rewriting 39 foreign key constraints for a cosmetic gain.
- **`closed_at` stamped inside `transition()`** (`models.py:668-671`), because
  `update_fields` skips `full_clean()` and therefore skips `clean()`.

## 9. Permissions & tenant isolation

- **This app enforces no permissions.** The keys guarding branch operations are
  `platform.branches.*`, applied by the schools app's views
  (`docs/schools/school_branches.md` §9). The model layer's job is the
  *invariants*, and it holds them regardless of who is calling.
- **`Branch.objects` is a `TenantAwareManager`** (`models.py:385`), so ordinary
  ORM access under a request with an ambient tenant is scoped automatically. A CX
  request sets no ambient tenant, so for platform code the manager does nothing
  and the caller's own filter is the whole boundary.
- **`all_objects` is the deliberate escape hatch**, and every use of it in this
  app pairs with an explicit `tenant=` or `tenant_id=`: the allocator
  (`models.py:470`), the single-main check (`models.py:444`), the sibling check
  (`models.py:568`), the handover (`models.py:617`, `623`) and
  `references.find_branch_in_tenant` (`references.py:83`). None of them widens
  anything.
- **The tenant column is the whole ownership statement.** There is no `school`
  column any more, so `reconcile_tenants`'s null check is the entire invariant
  (`management/commands/reconcile_tenants.py:33-40`).
- **A branch code is not a secret and is not unique**, so it must never be used
  as an identity across tenants. `EntityAuditTrail` learned this the hard way -
  see `docs/schools/school_branches.md` §6.
- **Closing or suspending a branch is an authorisation event**, not just a
  status change, because `vs_rbac` reads `IN_SERVICE_STATES` on the hot path of
  every permission check and every list. That coupling is intentional and is what
  makes withdrawal immediate.

## 10. Code map

| File | What lives there |
|---|---|
| `vs_tenants/models.py:278-285` | `BranchStatus` |
| `vs_tenants/models.py:288-426` | `Branch` fields, Meta, constraints, indexes, `__str__` |
| `vs_tenants/models.py:427-448` | `clean()`, `_assert_single_main` |
| `vs_tenants/models.py:450-486` | `allocate_next_code`, `save()` |
| `vs_tenants/models.py:488-501` | The four `mark_*` helpers |
| `vs_tenants/models.py:502-539` | `OUT_OF_SERVICE_STATES`, `IN_SERVICE_STATES`, `ALLOWED_TRANSITIONS` |
| `vs_tenants/models.py:541-579` | `_assert_may_leave_service` |
| `vs_tenants/models.py:581-629` | `promote_to_main` |
| `vs_tenants/models.py:631-688` | `transition` |
| `vs_tenants/models.py:691-736` | `BranchLifecycle` |
| `vs_tenants/exceptions.py` | All nine exception classes and their HTTP mapping |
| `vs_tenants/migrations/0004_move_branch_from_vs_schools.py` | The relocation, state-only |
| `vs_tenants/migrations/0007_branch_type_and_lifecycle_blanks.py` | `_type` and `reason` made blank-able |
| `vs_tenants/migrations/0008_alter_branch_name.py` | `name` widened |
| `core/exceptions.py:159-165` | Where the typed `error_code`/`message`/`http_status` triple is rendered |
| `vs_rbac/evaluator.py:61-81` | `IN_SERVICE_STATES` as an access decision |
| `vs_rbac/scoping.py:82-100` | `IN_SERVICE_STATES` as a visibility decision |
| `schools/vs_schools/views/branch.py`, `views/lifecycle.py` | The HTTP surface |

## 11. Test coverage & gaps

Module baseline: **`Ran 62 tests in 4.805s` - OK**.

Covered for this slice:

- `tests.py::BranchDatabaseConstraintTests` (nearly 200 lines) - the per-tenant
  code uniqueness, the partial one-main index, and that the allocator serialises.
- `tests.py::MainBranchLifecycleGuardTests` (200 lines) - every out-of-service
  target refused for a main branch, the two different exceptions, the promotion
  handover, and that a promoted sibling frees the incumbent.
- `tests_branch_references.py` - covered in `tenant_references_numbering` §11.
- On the schools side, `tests_branch_lifecycle.py` walks every edge of
  `ALLOWED_TRANSITIONS` through the HTTP surface.

Not covered:

- **Nothing reads a `BranchLifecycle` row back and asserts its contents** beyond
  existence, which is how the `actor_id` inconsistency
  (`error/schools/school_code_issues.md` §10) survived.
- No test drives a branch through `transition()` from outside the schools
  serializer and checks the central audit trail (§13).
- No test reaches the sibling-exists-but-cannot-be-promoted dead end (§9).
- The four `mark_*` helpers have no test, because they have no caller (§11).
- No test asserts the "every school must keep one in service" message against an
  `ORGANIZATION` tenant (§10).
