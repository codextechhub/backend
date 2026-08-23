# school_branches

The sites a school operates: the `Branch` row (which lives in `vs_tenants`, not
here), the per-tenant code allocator, the one-main-branch invariant and the
handover that keeps it satisfiable, the five-state lifecycle and its transition
endpoint, the branch-level primary administrator, and the six routes CX uses to
list, count, create, read, edit and move a branch.

The school record itself is `school_records`; plans and module entitlements are
`school_packages_entitlements`; turning a branch contact into a real account is
`school_provisioning`.

Routes covered by this slice, mounted at `/v1/i/` (`apps/urls.py:23`):
`<slug>/branches/`, `<slug>/branches/create/`, `<slug>/branches/stats/`,
`<slug>/branches/<code>/detail/`, `<slug>/branches/<code>/update/`,
`<slug>/branches/<code>/transition/`.

Findings for the whole module are collected in
**`error/schools/school_code_issues.md`**; §8 below points at the ones belonging
here rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **`Branch` is not a school model.** It lives in `vs_tenants`
  (`vs_tenants/models.py:288`) because a bank, a clinic chain and a retail group
  all have branches. It used to hang off `vs_schools.School`, and every app that
  needed the owning tenant had to travel `branch.school.tenant`; the tenant is
  now a column on the row.
- **The table did not move with the class.** `db_table = "vs_schools_branch"`
  (`vs_tenants/models.py:391`). Keeping the class name, the integer primary key
  and the table meant the relocation changed only Django's model state - no row
  moved, no foreign key constraint was rebuilt, and every `branch_id` already
  stored anywhere, issued JWTs included, still names the same site.
- **A branch code is unique per tenant, not globally.** `uq_branch_tenant_code`
  (`vs_tenants/models.py:402-405`), allocated 1..N. Every school's main branch is
  therefore code 1, which is why every view in this slice filters on the URL slug
  before `get_object()` runs (`views/branch.py:166-171`, `189-194`,
  `views/lifecycle.py:38-47`) and why audit events are keyed on the primary key
  rather than the code (`serializers.py:574-587`).
- **Exactly one main branch per tenant, enforced by a partial unique index.**
  `uq_branch_one_main_per_tenant` (`vs_tenants/models.py:410-414`), with
  `_assert_single_main()` in front of it so the ordinary path fails as a field
  error rather than surfacing an `IntegrityError` as a 500.
- **The main branch may not be taken out of service.** `transition()` refuses
  every out-of-service target for an `is_main` row, so a tenant is never left
  with a canonical site that is suspended, deactivated or - permanently, since
  CLOSED is terminal - shut (`vs_tenants/models.py:541-579`).
- **Promotion is a handover, not a second main.** `promote_to_main()` demotes the
  incumbent in its own UPDATE *before* promoting this row, because the unique
  index is not deferrable and promoting first would trip it even though the end
  state is legal (`vs_tenants/models.py:581-629`).
- **`CLOSED` is terminal and `PENDING` is never a target.** A shut-down branch is
  re-created, not resurrected; "pending activation" is a fact about a branch that
  has never opened, and activation cannot be undone
  (`vs_tenants/models.py:520-539`).
- **`IN_SERVICE_STATES` is stated positively on purpose.** The RBAC
  authorisation filters join through a *nullable* branch column, where a negative
  filter would also drop the whole-tenant grants that must always count
  (`vs_tenants/models.py:510-518`). It is derived from the choices, so a new
  lifecycle state cannot be silently treated as in service.
- **`Branch.objects` is tenant-aware; `Branch.all_objects` is not.**
  `default_manager_name = "objects"` with `base_manager_name = "all_objects"`, so
  FK traversal stays unscoped (`vs_tenants/models.py:385-393`). Platform code
  that must reach another tenant's branches uses `all_objects` explicitly, and
  the code allocator does exactly that.
- **These are CX routes, not school routes.** Every one takes a
  `platform.branches.*` key. The `school.branches.*` keys are seeded to the
  `school_admin` prebuilt role and bundled by
  `seed_school_permission_groups`, and **no view in the repo reads them** - see
  `school_code_issues` §1.
- **There is no delete.** A branch is closed, never removed.

## 2. Domain model

### `BranchStatus` and the state sets (`vs_tenants/models.py:504-518`)

| Set | Members |
|---|---|
| `OUT_OF_SERVICE_STATES` | `SUSPENDED`, `INACTIVE`, `CLOSED` |
| `IN_SERVICE_STATES` | every choice minus the above, i.e. `PENDING`, `ACTIVE` |

### `Branch` (`vs_tenants/models.py:288`)

| Field | Meaning |
|---|---|
| `tenant` | FK, PROTECT, indexed. Every creation path must supply it |
| `name` | Display label, e.g. "Lekki Branch" |
| `code` | `PositiveIntegerField`, `editable=False`, indexed. Filled on first save |
| `is_main` | The canonical site. One per tenant, by partial unique index |
| `_type` | Optional free-form descriptor ("Primary", "Nursery"). Nothing branches on it |
| `address`, `email`, `country`, `state` | Contact and location. `country` defaults to `"Nigeria"` |
| `status` | `BranchStatus`, indexed, default `PENDING` |
| `opened_at`, `closed_at`, `activated_at`, `deactivated_at` | Lifecycle stamps |

`_type` was `CharField(max_length=80)` with no `blank=True`: optional in prose
and required in the schema. Every row made outside the serializers -
`seed_import`, a data migration, the shell, the test factories - stored `""` and
was then permanently unpatchable through the API, because `BranchUpdateSerializer`
runs `full_clean()` over the whole instance and the blank it refused was one
nobody had touched (`vs_tenants/models.py:349-359`).

Indexes: `(tenant, is_main)`, `(tenant, status)`, `(tenant, code)`. Ordering
`-created_at`.

### `BranchLifecycle` (`vs_tenants/models.py:691`)

`branch` FK (CASCADE), `from_state` (blank for a creation event), `to_state`
(required - "an event that does not say where the branch went is not an event"),
`actor_id`, `reason`, `occurred_at`. `db_table = "vs_schools_branchlifecycle"`.
Indexed on `(branch, occurred_at)` and `(branch, to_state)`.

`actor_id` is a `CharField(max_length=120)`, not a foreign key. `transition()`
writes `str(actor_id or "")`, and the three callers pass three different kinds
of thing - see §5 and `school_code_issues` §10.

### `BranchPrimaryAdmin` (`schools/vs_schools/models.py:534`)

`branch` OneToOne (CASCADE) to `vs_tenants.Branch`, `contact` FK to
`ContactInfo` (PROTECT), `branch_role` (default `"Head Teacher"`),
`invite_status`, `invite_queued_at`, `invite_sent_at`. Indexed on
`(branch, invite_status)`.

It stays in the school app while `Branch` moves, and the docstring says why:
this is invite and onboarding machinery, its defaults are school vocabulary, and
nothing outside `vs_schools` references it (`models.py:542-544`).

## 3. Endpoint map

| Route | Verb | `rbac_permission` | Body / filters actually read |
|---|---|---|---|
| `<slug>/branches/` | GET | `platform.branches.view` | `?status=` (comma-separated), `?active=`, `?pending=`, `?suspended=`, `?inactive=`, `?closed=`, `?main=`, `?q=`, `?ordering=` (allowlisted to ten values). Paginated by the project default |
| `<slug>/branches/stats/` | GET | `platform.branches.view` | none. One aggregate over six buckets |
| `<slug>/branches/create/` | POST | `platform.branches.create` | `name`, `is_main`, `_type`, `address`, `email`, `country`, `state`, `opened_at`, `primary_admin_data` |
| `<slug>/branches/<code>/detail/` | GET | `platform.branches.view` | none |
| `<slug>/branches/<code>/update/` | PUT/PATCH | `platform.branches.update` | `name`, `is_main`, `_type`, `address`, `email`, `country`, `state`, `opened_at` |
| `<slug>/branches/<code>/transition/` | POST | `platform.branches.manage` | `to_state` (`ACTIVE` / `SUSPENDED` / `INACTIVE` / `CLOSED`), `reason` |

All six carry `IsAuthenticatedAndActive & HasRBACPermission`. The transition
route adds `IsVisionStaff` on top, and the reason is recorded at the call site:
suspending or closing a branch is a platform commercial action, so a
school-tenant role holding the key by misconfiguration still must not reach it
(`views/lifecycle.py:26-33`).

Four of the six re-filter the queryset by `tenant__school_profile__slug` before
`get_object()` runs, because `code` is unique per tenant and without the slug
filter `get_object()` matches every school's branch N
(`views/lifecycle.py:38-47`, `views/branch.py:166-171`, `189-194`, and the list
at `:42`). `BranchStatsView` filters on the same path
(`views/branch.py:120-121`).

`BranchCreateView` resolves the school inside `get_serializer_context` and
refuses two things there (`views/branch.py:141-152`):

| Condition | Response |
|---|---|
| No school with that slug | 404 `"School with slug '<slug>' does not exist."` |
| `school.status != "ACTIVE"` | 400 `"Branches can only be created for active schools."` |

That second rule means a school still onboarding - every school before go-live -
cannot have a branch added through this endpoint. Its branches come from the
inline `branches[]` list on `POST /v1/i/create/` instead.

`BranchUpdateView.update` calls `super().update()` and then re-reads the branch
so it can answer with the full detail payload rather than the update payload
(`views/branch.py:196-202`), which costs one extra query per edit.

## 4. Lifecycle / state machine

`ALLOWED_TRANSITIONS` (`vs_tenants/models.py:525-539`):

```
                 ┌──────────────────────────────────────┐
                 │                                      ▼
   PENDING ───► ACTIVE ───► SUSPENDED ───► INACTIVE ───► CLOSED
      │  │        │  │          │              ▲          ▲ (terminal)
      │  │        │  └──────────┼──────────────┘          │
      │  │        └─────────────┼─────────────────────────┘
      │  └──────────────────────┘   (SUSPENDED ─► ACTIVE)
      └──────────────────────────────────────────────────┘
              PENDING ─► INACTIVE, PENDING ─► CLOSED
```

Read as a table, which is less ambiguous:

| From | May go to |
|---|---|
| `PENDING` | `ACTIVE`, `INACTIVE`, `CLOSED` |
| `ACTIVE` | `SUSPENDED`, `INACTIVE`, `CLOSED` |
| `SUSPENDED` | `ACTIVE`, `INACTIVE`, `CLOSED` |
| `INACTIVE` | `ACTIVE`, `CLOSED` |
| `CLOSED` | nothing |

Two rules shape it: CLOSED is terminal, and `PENDING` is never a *target*.
`SUSPENDED` is reachable only from `ACTIVE` - you cannot suspend what was never
trading.

Layered on top, for `is_main` rows only (`vs_tenants/models.py:541-579`):

| Situation | Refusal |
|---|---|
| Main branch → any out-of-service state, and a sibling exists | `MainBranchCannotLeaveService` - promote a sibling first |
| Main branch → any out-of-service state, and it is the only branch | `LastBranchCannotLeaveService` - wind the school down instead |

Which refusal is raised depends on whether there is anything to hand over to,
because the advice differs. Every out-of-service state is guarded, not only
CLOSED: a SUSPENDED or INACTIVE main branch is already wrong today, because
every reader of `School.main_branch` and every default-branch pick lands on a
site nobody may be posted to.

The API layer adds one more refusal that the model treats as a no-op:

```python
if branch.status == to_state:
    raise BranchAlreadyInState(state=to_state)
```

`serializers.py:1487-1488`. `transition()` is idempotent so the helper methods
stay safe to retry; over HTTP it is worth telling the caller their request
changed nothing.

## 5. Derivations

### The branch code

```python
Tenant.objects.select_for_update().only("id").get(pk=tenant_id)
current_max = Branch.all_objects.filter(tenant_id=tenant_id).aggregate(m=Max("code"))["m"] or 0
return current_max + 1
```

`vs_tenants/models.py:450-475`. Two choices in there are load-bearing and both
are documented at the call site:

- **It locks the *tenant* row, not the branch rows.** An older version locked
  `select_for_update().filter(school=school)`, which locks nothing at all when
  the owner has no branches yet - so two concurrent first-branch creates both
  read max 0 and both wrote code 1. The tenant row always exists, so locking it
  serialises allocation for an empty tenant exactly as well as for a full one.
- **It reads through `all_objects`.** `objects` is the `TenantAwareManager`, and
  under an ambient tenant context that differs from `tenant_id` - platform code
  creating a branch for a customer, which is every call here - it would
  aggregate over zero rows and hand back a duplicate code.

`save()` allocates only when `code` is missing or zero, and opens its own
transaction to do it (`vs_tenants/models.py:477-486`).

`vs_schools.tests.BranchCodeAllocationConcurrencyTests` is one of the two
`TransactionTestCase` classes tagged `slow`, and it exists for this.

### The main-branch handover

`promote_to_main` (`vs_tenants/models.py:581-629`), in order:

1. Refuse if this branch is out of service - promoting a closed branch would
   rebuild the dead end by hand.
2. Return early if it is already main. Idempotent on purpose, so the serializer
   and any retry can call it without first asking.
3. Lock the tenant row. Two admins promoting two different branches at the same
   moment would otherwise both read one incumbent, both demote it, and both
   insert a main branch.
4. Demote the incumbent in its own UPDATE.
5. Promote this row.

No `BranchLifecycle` row is written and `actor_id` is accepted only for
symmetry: that table records status edges, and a handover changes no status. The
update serializer's audit event carries `is_main` in its diff instead.

### What the update serializer refuses

`BranchUpdateSerializer.validate` (`serializers.py:635-662`):

| Payload | Outcome |
|---|---|
| `is_main: true` on an out-of-service branch | 400, naming the status |
| `is_main: false` on the current main | 400 - "A school must always have a main branch. Make another branch the main branch instead; this one is demoted automatically" |
| `is_main: true` on an in-service non-main branch | Accepted; `update()` routes it through `promote_to_main` |

`is_main: true` used to be refused whenever another main branch existed, which
made promotion impossible for every school that had one - and since a main
branch can no longer be retired without promoting a sibling first, that refusal
was the dead end itself (`serializers.py:637-643`).

`update()` counts real changes and refuses a no-op edit with
`{"detail": "No changes detected in update payload."}` (`serializers.py:676-683`),
then runs `full_clean_as_field_errors` so a model refusal arrives keyed by field
rather than as a bare sentence.

### The stats aggregate

```python
Branch.objects.filter(tenant__school_profile__slug=slug).aggregate(
    all=Count("id"),
    active=Count("id", filter=Q(status=ACTIVE)),
    pending=..., suspended=..., inactive=..., closed=...,
)
```

`views/branch.py:118-130`. One query, all five statuses covered, so unlike the
school stats (`school_records` §5) these numbers add up.

Note it reads `Branch.objects` - the tenant-aware manager - while every other
branch view reads `Branch.objects.all()` too. Under a CX request no ambient
tenant is set, so the manager does nothing and the slug filter is the whole
boundary.

### Who the actor was

Three writers, three different values in one `CharField`:

| Writer | Value stored |
|---|---|
| `BranchCreateSerializer.create` (`serializers.py:527`) | `self.context["actor_id"]`, which `ActorContextMixin` sets to the `User` **object** - coerced by Django to `str(user)`, i.e. `"ada@x.test (ACTIVE)"` |
| `SchoolCreateSerializer.create` (`serializers.py:1099`) | the same, for inline branches |
| `Branch.transition` (`vs_tenants/models.py`) | `str(actor_id or "")` - blank for the onboarding sweep and management commands |

A column named `actor_id` that holds an email-and-status string, a user id or a
blank cannot be joined, filtered or attributed. `school_code_issues` §10.

## 6. What writing writes

### `POST <slug>/branches/create/`

One `transaction.atomic` (`serializers.py:504`):

| Step | Rows |
|---|---|
| 1 | `Branch` at `PENDING`, `code` allocated under the tenant lock |
| 2 | `opened_at` back-filled to now if it was not supplied - a second UPDATE |
| 3 | One `BranchLifecycle`, `""` → `PENDING`, reason `"Branch created"` |
| 4 | A branch-scoped `branch_admin` `TenantRoleTemplate` via `provision_role_from_prebuilt`, plus its permission rows |
| 5 | `ContactInfo` + `BranchPrimaryAdmin` (QUEUED) + `provision_admin_user` → `User`, `TenantUserRoleAssignment`, `UserInvitation`, queued email, link stamped SENT |
| 6 | One `BRANCH/CREATE` audit event |

Step 5 is not optional at runtime even though `primary_admin_data` is declared
`required=False` on the serializer: `create()` raises a `ValidationError` when it
is absent (`serializers.py:566-567`), after steps 1 to 4 have already run. The
atomic block rolls them back, so nothing is left behind - but the contract and
the runtime disagree, and the refusal arrives from the wrong layer
(`school_code_issues` §11).

The audit event is keyed on `str(branch.pk)`, and the comment explains what the
code-keyed version cost: `EntityAuditTrail` is unique on
`(entity_type, entity_id)` with no tenant column, so a code-keyed trail put
Bright Star's, Greenfield's and Corona's main branches on one platform-wide row,
interleaved, with nothing on the event saying whose branch it was
(`serializers.py:574-587`). The code is still findable - the summary carries it,
together with the school name.

`tenant=branch.tenant` is passed explicitly, and reading it off the branch rather
than off the school is deliberate: it says what the audit row means, and keeps
working if the branch ever arrives from somewhere the school is not in scope
(`serializers.py:592-602`). Without it the row landed with `tenant = NULL`, and
an investigator asking "show me everything at Bright Star" had to read summaries
instead of filtering a column.

### `PATCH <slug>/branches/<code>/update/`

Snapshot → apply → refuse a no-op → `full_clean` as field errors → `save()` →
`promote_to_main` if promoting → one `BRANCH/UPDATE` audit event carrying the
before/after diff (`serializers.py:664-717`). The code is not repeated in the
event: it is `editable=False` and never changes, so naming it once on the
creation event keeps it findable for good.

### `POST <slug>/branches/<code>/transition/`

`Branch.transition()` writes the status change and the `BranchLifecycle` row as
one unit inside `@transaction.atomic`. No `emit_audit_event` call is made on this
path at all - the lifecycle table is the record, and it is not the central audit
trail (`school_code_issues` §17).

## 7. Worked example

Bright Star Academy runs Ikeja (code 1, main) and Lekki (code 2). Both went live
with the school.

**1. Adding Yaba.**

```http
POST /v1/i/bright-star-academy/branches/create/?tenant=codex
{"name": "Yaba", "state": "Lagos",
 "primary_admin_data": {"full_name": "Chidi Nwosu", "email": "chidi@brightstar.test"}}
```

`get_serializer_context` finds the school and checks it is ACTIVE. `validate`
fills `country` from the platform onboarding defaults and runs the address
through `email_refusal` scoped to Bright Star's tenant - the same helper every
creation path shares, so all of them agree on what "already exists" means. They
did not agree twice over: this path once compared case-sensitively while
`vs_user` compared with `iexact`, and then both asked about the whole platform
after uniqueness had narrowed to one address per tenant
(`serializers.py:487-492`).

`create()` allocates code 3 under a lock on Bright Star's tenant row, stamps
`opened_at`, writes the lifecycle row, provisions a `branch_admin-3` role
template scoped to Yaba, creates Chidi's contact, link and pending account, and
emits the audit event: *"Yaba created as branch 3 of Bright Star Academy"*.

**2. Opening it.**

```http
POST /v1/i/bright-star-academy/branches/3/transition/?tenant=codex
{"to_state": "ACTIVE", "reason": "Site inspection passed."}
```

`PENDING → ACTIVE` is an allowed edge. The status is written and a
`BranchLifecycle` row from `PENDING` to `ACTIVE` lands in the same transaction.
Sending the same request again is a 409 `BranchAlreadyInState`.

**3. Closing Lekki.**

```http
POST /v1/i/bright-star-academy/branches/2/transition/?tenant=codex
{"to_state": "CLOSED", "reason": "Lease not renewed."}
```

Lekki is not `is_main`, so `_assert_may_leave_service` returns immediately. The
edge `ACTIVE → CLOSED` is allowed. `clean()` stamps `closed_at`. Lekki is now
terminal: `ALLOWED_TRANSITIONS[CLOSED]` is empty, so it can never be reopened.

Everyone whose RBAC grant was pinned to Lekki loses access in the same breath,
because `vs_rbac.evaluator._assignment_branch_q` only counts a pinned grant while
its branch is in service - and `vs_rbac.scoping._grant_scope` narrows their
visibility to the empty set rather than falling back to their home posting.

**4. Trying to close Ikeja.** The same request against branch 1 raises
`MainBranchCannotLeaveService`, because Ikeja is `is_main` and Yaba exists. The
message says to promote a sibling first.

**5. Moving the main branch to Yaba.**

```http
PATCH /v1/i/bright-star-academy/branches/3/update/?tenant=codex
{"is_main": true}
```

Yaba is ACTIVE, so it is promotable. `update()` pops `is_main`, counts one
change, saves the other fields, then calls `promote_to_main`, which locks the
tenant row, demotes Ikeja in its own UPDATE and promotes Yaba in the next. The
partial unique index is satisfied at every point. No lifecycle row is written;
the audit event's diff carries `is_main` instead.

**6. Now Ikeja can be closed** - it is no longer main, and Yaba is.

**7. What the audit trail then shows.** Three `BRANCH/*` events keyed on three
different primary keys, each carrying `tenant = Bright Star`, so filtering the
Event Explorer by tenant returns all of them. The two transitions in steps 2 and
3 are **not** in there - they are only in `BranchLifecycle`, which has no
endpoint and no export dataset (`school_code_issues` §17).

**8. What a Bright Star admin can do with any of this.** Nothing. Every route
above takes a `platform.branches.*` key, all of them are
`PermissionScope.PLATFORM`, and the RBAC grant guard refuses to write one onto a
school role. Bright Star's admin holds `school.branches.view`, `.create`,
`.update` and `.manage` - four keys that no view in the repo reads
(`school_code_issues` §1).

## 8. Gotchas / known limitations

Recorded in full in **`error/schools/school_code_issues.md`**. The items
belonging to this slice:

| # in that file | One line |
|---|---|
| §1 | **Confirmed by execution.** A school admin cannot list, create or edit their own branches: the four `school.branches.*` keys they hold are read by no view |
| §10 | `BranchLifecycle.actor_id` holds a user object's `str()`, a user id or a blank depending on which path wrote it |
| §11 | Branch creation declares `primary_admin_data` optional and then requires it from inside `create()` |
| §14 | Creating a second main branch is refused outright while updating performs a handover, and the create error names no way forward |
| §15 | **Confirmed by execution.** The branch list and stats routes answer 200 for a school that does not exist |
| §17 | The branch stats docstring advertises a `?s=` parameter that does not exist; branch transitions emit no central audit event; `InviteStatus.FAILED` is never written |

Design choices worth stating as choices - most of this slice's surprises are
deliberate and documented at the call site:

- **Locking the tenant row rather than the branch rows**
  (`vs_tenants/models.py:454-459`) is what makes first-branch allocation
  race-safe; the obvious version locks nothing.
- **Reading through `all_objects` in the allocator**
  (`vs_tenants/models.py:461-464`) is what stops platform code creating a
  duplicate code for a customer tenant.
- **Demote-then-promote** (`vs_tenants/models.py:585-592`) is required by the
  non-deferrable partial unique index, not a stylistic preference.
- **Guarding every out-of-service state, not only CLOSED**
  (`vs_tenants/models.py:550-557`), because a suspended main branch is already
  wrong even though the damage is not yet permanent.
- **Keying branch audit events on the pk** (`serializers.py:574-587`) is what
  un-merged every school's main branch from one shared platform-wide trail.
- **Keeping `db_table = "vs_schools_branch"`** (`vs_tenants/models.py:388-391`)
  avoided rewriting 39 foreign key constraints for a cosmetic gain.

## 9. Permissions & tenant isolation

- **Four keys gate this slice**, all in the `platform.branches` resource:
  `view`, `create` and `update` are `NORMAL`; `manage` is `SENSITIVE` and
  restricted and is described in the seeder as exactly the lifecycle transition
  (`core/management/commands/seed_platform_permissions.py:123-132`).
- **All four are `PermissionScope.PLATFORM`.** `platform.branches.*` is not in
  `TENANT_HOLDABLE_KEYS`, so `vs_rbac`'s grant guard refuses to write any of them
  onto a school role - which is what makes `school_code_issues` §1 a design gap
  rather than a misconfiguration somebody can fix by editing a role.
- **The transition route is gated twice**, and deliberately:
  `IsVisionStaff & HasRBACPermission`, so a school-tenant role holding the key by
  misconfiguration still cannot suspend or close a branch
  (`views/lifecycle.py:26-33`).
- **The slug filter is the tenant boundary on every route**, not the manager.
  `Branch.objects` is a `TenantAwareManager`, but a CX request sets no ambient
  tenant, so it filters nothing; `filter(tenant__school_profile__slug=slug)` is
  what stops branch 1 of one school being returned for branch 1 of another.
- **A branch belonging to a non-school tenant is unreachable here.** Every
  queryset joins through `school_profile`, so a VIGIL clinic's branches are
  outside this app entirely - which is correct, and is why
  `branch_school_slug()` returns `None` rather than raising
  (`serializers.py:358-380`).
- **`Branch.all_objects` is used in two places in this app's serializers**
  (`serializers.py:481` for the main-branch clash check) and both are reads
  narrowed by an explicit `tenant=`, so neither widens anything.

## 10. Code map

| File | What lives there |
|---|---|
| `vs_tenants/models.py:288-426` | `Branch` fields, Meta, constraints and indexes |
| `vs_tenants/models.py:427-448` | `clean()` and `_assert_single_main` |
| `vs_tenants/models.py:450-486` | `allocate_next_code`, `save()` |
| `vs_tenants/models.py:490-539` | The lifecycle helpers, the state sets and `ALLOWED_TRANSITIONS` |
| `vs_tenants/models.py:541-579` | `_assert_may_leave_service` and its two refusals |
| `vs_tenants/models.py:581-629` | `promote_to_main` |
| `vs_tenants/models.py:631-…` | `transition` |
| `vs_tenants/models.py:691-736` | `BranchLifecycle` |
| `schools/vs_schools/models.py:534-571` | `BranchPrimaryAdmin` |
| `schools/vs_schools/views/branch.py` | List, stats, create, detail, update |
| `schools/vs_schools/views/lifecycle.py` | The transition endpoint |
| `schools/vs_schools/serializers.py:358-435` | `branch_school_slug`, the two read serializers |
| `schools/vs_schools/serializers.py:442-611` | `BranchCreateSerializer` |
| `schools/vs_schools/serializers.py:614-717` | `BranchUpdateSerializer` |
| `schools/vs_schools/serializers.py:805-838` | `BranchInlineCreateSerializer` (used by the school create path) |
| `schools/vs_schools/serializers.py:1461-1494` | `BranchStateTransitionSerializer` |
| `vs_tenants/exceptions.py` | `BranchAlreadyInState`, `InvalidBranchTransition`, `MainBranchCannotLeaveService`, `LastBranchCannotLeaveService`, `BranchNotInService` |
| `vs_rbac/evaluator.py:61-81` | Where a branch's liveness becomes an access decision |
| `vs_rbac/scoping.py:57-100` | Where it becomes a visibility decision |

Note that `branch_school_slug` is a **function** called from two
`SerializerMethodField`s rather than a mixin, and the reason is worth keeping:
DRF's `SerializerMetaclass` only collects declared fields from bases that are
themselves serializers, so a field on a plain mixin is silently ignored and
`Meta.fields` then fails with "not valid for model Branch"
(`serializers.py:374-377`).

## 11. Test coverage & gaps

Module baseline: see `school_code_issues` for the exact `Ran N tests` line.

Covered for this slice:

- `tests_branch_endpoints.py` - list, stats, create, detail, update, and the
  cross-school code isolation the slug filter provides.
- `tests_branch_lifecycle.py` (416 lines) - every allowed and disallowed edge,
  the terminal CLOSED rule, the idempotent no-op and the 409 the API layer adds.
- `tests_branch_main_guard.py` (172 lines) - the main branch cannot leave
  service, the two different refusals, and the promotion handover.
- `tests.py::BranchCodeAllocationConcurrencyTests` - a `TransactionTestCase`
  tagged `slow`, which is the only test in the repo that actually exercises the
  tenant-row lock in `allocate_next_code`.

Not covered:

- **No test asserts a school-tenant caller can reach any branch route**, which is
  why §1 has gone unnoticed: every test in this file authenticates as CX.
- No test reads `BranchLifecycle.actor_id` back and asserts its shape (§10).
- No test POSTs a branch without `primary_admin_data` and checks *where* the
  refusal comes from (§11).
- No test tries to create a second main branch and then follows the error to a
  working route (§14).
- No test asserts that a transition emits (or does not emit) a central audit
  event (§17).
- `BranchStatsView`'s `?s=` parameter has no test, because it does not exist.
