# config_capabilities_entitlements

The feature-gating half of `vs_config`: the unified **capability** catalogue
(modules and features in one table), the **entitlement** that records whether a
tenant is allowed a capability, the **override** that records whether it is
actually switched on, the dependency graph between capabilities, and the
evaluator that turns all four into one boolean.

Routes (`urls.py:17-24`):
`capabilities/`, `capabilities/<key>/`, `entitlements/`,
`entitlements/calendar/`, `entitlements/bulk-schedule/`,
`entitlements/<capability>/`, `overrides/`, `effective-capabilities/`.

---

## 1. What it is (and what it is NOT)

- **One catalogue, not two.** `Capability` replaced the legacy XVSModules plus
  BranchFeatureFlag split (`models.py:264-271`). A capability is either a
  sellable `MODULE` or a smaller `FEATURE`; `kind` is the only difference, and
  it is presentational.
- **Entitlement answers "did they buy it". Override answers "is it on".** They
  are deliberately separate tables so a tenant can pause a module it pays for
  without losing the grant, and so no branch toggle can widen commercial access
  (`models.py:402-421`, `:501-521`).
- **An override can never switch on something unentitled.** `set_override`
  raises `CapabilityNotEntitled` (422) when asked to write ENABLED for a
  capability the scope's tenant does not hold (`services/capabilities.py:330-333`).
  There is no path around it: every write goes through that function.
- **Entitlements live at tenant or platform level only.** Branches do not buy
  modules, so `CapabilityEntitlement` carries its own tenant FK and `scope_key`
  rather than inheriting the three-level contract (`models.py:412-421`,
  `:496-498`). `EntitlementListSetView` explicitly discards the branch half of
  the resolved scope (`views.py:685-688`).
- **Overrides live at all three levels**, and the most specific non-INHERIT row
  wins (`models.py:506-513`).
- **`INHERIT` is not the same as "no row".** It hands the decision back up the
  chain while preserving who set it and why (`models.py:529-531`).
- **A dependency must be *effective*, not merely present.** Enabling
  `procurement` at a branch demands that `finance` resolves as effective for
  that same branch, entitlement and overrides included
  (`services/capabilities.py:73-76`).
- **Nothing is ever hard-deleted.** Archiving is the DELETE verb for
  capabilities (`views.py:660-672`); entitlements and overrides reference the
  row, as does immutable audit history.
- **`default_enabled` is not what its name and docstring promise.** For any
  capability with `requires_entitlement = True`, a missing override resolves to
  `True` regardless of `default_enabled` (`services/capabilities.py:99-101`).
  See `config_code_issues.md` §4.

## 2. Domain model

### `Capability` (`models.py:264`)

| Field | Meaning |
|---|---|
| `key` | Immutable unique slug (`finance`, `bulk_import`) |
| `label`, `description` | Admin-facing text |
| `kind` | MODULE or FEATURE |
| `requires_entitlement` | True: a GRANTED entitlement is required before it can ever be on |
| `default_enabled` | Runtime fallback - but only consulted when `requires_entitlement` is False |
| `is_active` | Soft-archive flag, indexed. Inactive never resolves as enabled |
| `metadata` | Free-form display hints. **Never** used in enablement decisions |

`Meta.ordering = ["kind", "label"]` (`models.py:328`) - and `label` is not
unique, so ties are possible on a paginated catalogue.

### `CapabilityDependency` (`models.py:334`)

Two FKs (`capability`, `requires`), forming a directed acyclic graph. Three
layers of integrity:

1. `uniq_capability_dependency` rejects duplicate edges.
2. A database `CheckConstraint` named `capability_cannot_require_itself`
   rejects self-reference (`models.py:371-374`).
3. `clean()` walks the existing graph and rejects any edge that would create a
   cycle (`models.py:377-395`), and `save()` always calls it (`:397-399`), so
   the API path is covered too. A cycle would make evaluation non-terminating.

Seeded edges: `procurement -> finance`, `parent_portal -> student_portal`
(`seed_config_catalogue.py:133-136`).

### `CapabilityEntitlement` (`models.py:402`)

| Field | Meaning |
|---|---|
| `capability` | FK, CASCADE |
| `tenant` | The tenant the decision applies to; NULL means platform-wide |
| `scope_key` | `"tenant:<id>"` or `"platform"`, computed in `save()` (`models.py:496-498`) |
| `state` | GRANTED or DENIED. A tenant DENIED beats a platform GRANTED |
| `source` | PACKAGE, PLATFORM, MANUAL, IMPORT |
| `starts_at` | Optional activation; the grant is inert before it |
| `ends_at` | Optional **exclusive** expiry; the grant is inert from that moment |
| `updated_by` | Last writer, SET_NULL |

Constraint `uniq_capability_entitlement_scope` on `(capability, scope_key)`.
Indexes on `(state, ends_at)` and `(state, starts_at)` (`models.py:491-494`),
which are exactly the columns the renewal calendar filters on.

**No `Meta.ordering`**, which is why the entitlements list paginates unstably
(`config_code_issues.md` §5).

### `CapabilityOverride` (`models.py:501`)

| Field | Meaning |
|---|---|
| `capability` | FK, CASCADE |
| `state` | ENABLED, DISABLED, INHERIT |
| `reason` | Operator explanation, on the row and in the audit event |
| `tenant`, `branch`, `scope_key` | From `ScopedModel` |

Constraint `uniq_capability_override_scope` on `(capability, scope_key)`. Also
no `Meta.ordering`.

## 3. Endpoint map

| Method + path | Permission | Platform-only |
|---|---|---|
| `GET /capabilities/` | `config.capability.view` | no |
| `POST /capabilities/` | `config.capability.manage` | yes |
| `GET /capabilities/<key>/` | `config.capability.view` | no |
| `PATCH /capabilities/<key>/` | `config.capability.manage` | yes |
| `DELETE /capabilities/<key>/` | `config.capability.manage` | yes |
| `GET /entitlements/` | `config.entitlement.view` | no |
| `POST /entitlements/` | `config.entitlement.manage` | yes |
| `GET /entitlements/calendar/` | `config.entitlement.view` | no (`?all_tenants=true` is) |
| `POST /entitlements/bulk-schedule/` | `config.entitlement.manage` | yes |
| `DELETE /entitlements/<capability>/` | `config.entitlement.manage` | yes |
| `GET /overrides/` | `config.override.view` | no |
| `POST /overrides/` | `config.override.manage` | no |
| `GET /effective-capabilities/` | `config.capability.view` | no |

`GET /capabilities/` accepts `?include_inactive=true` and prefetches
`dependency_links__requires` so the serializer's dependency read stays O(1) per
page (`views.py:602-608`).

### Request bodies actually read

`POST /capabilities/` and `PATCH /capabilities/<key>/`
(`serializers.py:208-282`): the model fields plus a write-only `dependencies`
list of capability keys. `dependencies` is validated against the catalogue and
against self-reference before any write (`serializers.py:233-244`), then
reconciled by `_set_dependencies`, which deletes edges no longer named and
`get_or_create`s the rest (`serializers.py:251-261`). On PATCH, omitting
`dependencies` leaves the graph alone; sending `[]` clears it.

`POST /entitlements/` (`serializers.py:297-317`):

```jsonc
{"capability": "finance", "state": "GRANTED", "source": "MANUAL",
 "starts_at": "2026-09-01T00:00:00Z", "ends_at": "2027-09-01T00:00:00Z",
 "reason": "Annual renewal"}
```

**Scope is not in the payload.** It comes from `request.tenant`. Validation:
a DENIED state may not carry dates, an `ends_at` in the past is rejected, and
`starts_at` must precede `ends_at`.

`POST /entitlements/bulk-schedule/` (`serializers.py:320-348`):

```jsonc
{"items": [{"capability": "finance", "tenant": "alpha-nt"},
           {"capability": "finance", "tenant": "beta-nt"}],
 "ends_at": "2027-09-01T00:00:00Z",
 "reason": "Renewed group contract"}
```

1 to 100 items, each `(capability, tenant)` pair unique, at least one of
`starts_at` / `ends_at` present in the raw payload, and `reason` mandatory
(3-500 characters). An item without `tenant` targets the platform scope.

`POST /overrides/` (`serializers.py:363-369`): `capability`, `state`, optional
`branch`, optional `reason`. The `branch` field is declared on the serializer
but consumed by `resolve_request_scope` reading `request.data`
(`services/scopes.py:61`).

`DELETE /entitlements/<capability>/` reads only `reason`.

### Serializer field sets

| Serializer | Fields |
|---|---|
| `CapabilitySerializer` (`serializers.py:208`) | `id`, `key`, `label`, `description`, `kind`, `requires_entitlement`, `default_enabled`, `is_active`, `metadata`, `dependencies`, `created_at`, `updated_at` |
| `CapabilityEntitlementSerializer` (`serializers.py:285`) | `id`, `capability`, `capability_key`, `tenant`, `state`, `source`, `starts_at`, `ends_at`, `updated_by`, `created_at`, `updated_at` - all read-only |
| `CapabilityOverrideSerializer` (`serializers.py:351`) | `id`, `capability`, `capability_key`, `tenant`, `branch`, `state`, `reason`, `updated_by`, `created_at`, `updated_at` - all read-only |

`dependencies` is write-only on input and re-added on output as the ordered list
of required keys (`serializers.py:276-282`), because a capability with an unmet
dependency resolves OFF regardless of grants and overrides, and the client needs
that list to explain the state.

## 4. Lifecycle / state machine

### Capability

```text
POST /capabilities/            ->  is_active = True
DELETE /capabilities/<key>/    ->  is_active = False   (idempotent; a second
                                    call returns 200 and writes no audit row)
```

### Entitlement

```text
(no row)  --POST GRANTED-->  GRANTED
(no row)  --POST DENIED -->  DENIED
GRANTED   --POST-->          GRANTED with new dates / source   (upsert)
any       --DELETE-->        (no row); the platform row, if any, takes over
```

Plus two time-derived states that no write produces:

```text
GRANTED, starts_at > now   ->  "scheduled"  (inert)
GRANTED, ends_at <= now    ->  "expired"    (inert, no data change)
```

`entitlement_resolution` names all five: `not_required`, `not_granted`,
`denied`, `scheduled`, `expired`, `active` (`services/capabilities.py:41-60`).

### Override

```text
(no row) / INHERIT   ->  decision passes to the next scope up
ENABLED              ->  on here (requires an active entitlement to write)
DISABLED             ->  off here, even though the tenant stays entitled
```

## 5. Derivations

### `effective_capability` (`services/capabilities.py:64-101`)

The single-row evaluator, in order:

1. `capability.is_active` must be True.
2. `_active_entitlement` must pass (skipped when `requires_entitlement` is
   False).
3. Every `requires` edge must itself be effective **at the same scope**,
   recursively, with a `seen` set that raises `CapabilityDependencyError` on a
   cycle.
4. Overrides are read most-specific-first (`branch`, `tenant`, `platform`) in
   one query; the first non-INHERIT state decides.
5. With no override: `True` if `requires_entitlement`, else
   `capability.default_enabled`.

Step 5 is the surprising one, and it is deliberate per the inline comment
(`:96-98`): being in the plan is what switches a plan-gated module on, and a
`DISABLED` override is the lever to suppress it. It nonetheless contradicts the
`default_enabled` field docstring (`config_code_issues.md` §4).

### `_active_entitlement` (`services/capabilities.py:20-38`)

Fetches every row for the capability whose tenant is NULL **or** the resolved
tenant, then prefers the tenant-specific row. So an explicit tenant `DENIED`
beats a platform-wide `GRANTED`. Then it applies the time window:
`starts_at > now` or `ends_at <= now` means inert. Note `ends_at` is
**exclusive**, so a grant ending at midnight is off at midnight.

### `BulkCapabilityEvaluator` (`services/capabilities.py:239-310`)

The same logic with a fixed query budget, used by `/effective-capabilities/` and
`/export/`. It preloads capabilities with their dependency links, all candidate
entitlements in one query, and all candidate overrides in one query, then
memoises per capability. `bulk_effective_capabilities` evaluates **every**
capability (so an inactive prerequisite still resolves correctly) but returns
only the active ones (`services/capabilities.py:313-322`). Tested for
equivalence with the single-row evaluator under a query-count assertion
(`tests.py:246-264`).

### Entitlement renewal calendar (`views.py:742-829`)

- `?window_days=` defaults to 90, must be a whole number between 7 and 366; a
  non-integer is a 400 (`views.py:754-759`).
- `?all_tenants=true` drops the tenant filter and is refused with a 403 unless
  the caller's home tenant is a PLATFORM tenant (`views.py:748-753`).
- `summary` counts expired, and expiring within 7 / 30 / 90 days, plus
  scheduled - five separate `COUNT` queries (`views.py:773-779`).
- `entries` selects grants whose `ends_at` falls between 30 days ago and the
  window end, or whose `starts_at` is in the future inside the window, capped
  at `CALENDAR_LIMIT = 500` with a `truncated` flag computed by fetching
  501 rows (`views.py:780-786`).
- Per row, `warning` is `expired` / `scheduled` / `critical` (<= 7 days) /
  `warning` (<= 30) / `notice` (<= 90) / `none`, and `days_until_expiry` uses
  `ceil` so a grant with 6 hours left reads as 1 day, not 0
  (`views.py:788-819`).

### `bulk_schedule_entitlements` (`services/capabilities.py:141-213`)

Locks every target row with `select_for_update`, then per target:

- `UNSET` (the field absent from the request) keeps the existing value;
  an explicit `null` clears it (`services/capabilities.py:169-176`).
- `starts_at >= ends_at` raises a validation error naming the capability and
  the tenant, and because the whole function is `@transaction.atomic`, one bad
  pair rolls the entire batch back.
- **It then forces `state = GRANTED` and `source = MANUAL`**
  (`services/capabilities.py:190-191`). That is not a schedule change; see
  `config_code_issues.md` §2.

### `EntitlementResetView` (`views.py:711-739`)

Deletes only the selected layer, then reports what took over:

```jsonc
{"capability": "finance", "cleared": true, "effective": true,
 "status": "active", "source": "school" | "platform" | "none"}
```

`status` is the five-state name from `entitlement_resolution`.

## 6. What writing writes

| Action | Written by | Target |
|---|---|---|
| `config.capability.created` | `views.py:616-619` | the capability |
| `config.capability.updated` | `views.py:653-656` | the capability |
| `config.capability.archived` | `views.py:667-671` | the capability |
| `config.entitlement.updated` | `services/capabilities.py:134-137` | the entitlement row |
| `config.entitlement.updated` (bulk) | `services/capabilities.py:202-211` | the entitlement row, with `metadata: {"bulk_schedule": true}` |
| `config.entitlement.cleared` | `services/capabilities.py:232-235` | the **capability** |
| `config.override.updated` | `services/capabilities.py:351-354` | the override row |

Every one is inside the same transaction as the change. The before/after
snapshots for entitlements carry `state`, `source`, `starts_at`, `ends_at` as
ISO strings; for overrides, `state` only (`services/capabilities.py:341`,
`:353`) - the `reason` change is captured in the event's own `reason` field.

**Dependency edges are audited only through the capability snapshot.**
`_set_dependencies` writes and deletes `CapabilityDependency` rows without any
event of their own; what survives is the `dependencies` list inside the
`config.capability.updated` before/after payload.

Reading writes nothing: the catalogue, entitlement list, override list,
calendar and `/effective-capabilities/` all leave no trace.

## 7. Worked example

Grant a school a module for one academic year:

```text
POST /v1/config/entitlements/?tenant=alpha-nt
{"capability": "procurement", "state": "GRANTED", "source": "MANUAL",
 "starts_at": "2026-09-01T00:00:00Z", "ends_at": "2027-07-31T00:00:00Z",
 "reason": "2026/27 contract"}
```

```json
{ "success": true, "message": "Capability entitlement saved.",
  "data": { "id": "…", "capability": "…", "capability_key": "procurement",
            "tenant": "…", "state": "GRANTED", "source": "MANUAL",
            "starts_at": "2026-09-01T00:00:00Z",
            "ends_at": "2027-07-31T00:00:00Z",
            "updated_by": 7, "created_at": "…", "updated_at": "…" } }
```

On 20 August 2026 the grant has not started, so:

```text
GET /v1/config/effective-capabilities/?tenant=alpha-nt
```

returns `{"key": "procurement", "enabled": false}` - and it would return `false`
even after 1 September unless `finance` is also effective for the same scope,
because of the seeded dependency edge.

A school pausing a module it owns:

```text
POST /v1/config/overrides/?tenant=alpha-nt
{"capability": "parent_portal", "state": "DISABLED", "reason": "Exam window"}
```

Attempting the reverse on something never granted:

```json
{ "success": false,
  "message": "'procurement' cannot be enabled because it is not entitled.",
  "error": { "code": "CAPABILITY_NOT_ENTITLED", "detail": {} } }
```

with HTTP 422 (`exceptions.py:22-23`, mapped at `core/exceptions.py:128-134`).
Tested at `tests.py:174-181`.

## 8. Gotchas / known limitations

Full evidence in **`error/config/config_code_issues.md`**. Items belonging to
this slice:

- **Bulk scheduling silently grants.** `POST /entitlements/bulk-schedule/`
  forces `state = GRANTED` and `source = MANUAL` on every target, so adjusting
  an expiry date across a list that happens to include a DENIED tenant hands
  that tenant the module, and erases PACKAGE provenance on the rest (§2).
- **`default_enabled` is ignored** for every entitlement-gated capability,
  contradicting its own field docstring (§4). An operator setting
  `default_enabled: false` on a module sees it stay on.
- **Entitlement and override lists paginate an unordered queryset** - neither
  model declares `Meta.ordering` and neither view adds one
  (`views.py:686-689`, `:893-901`), so rows can repeat or vanish between pages
  (§5).
- **No school role holds `config.capability.view`**, so
  `/v1/config/effective-capabilities/` - which the model docstring names as the
  frontend's source of truth (`models.py:283-284`) - is a 403 for every school
  user out of the box (§3).
- **Entitlement resolution is implemented three times**: `_active_entitlement`,
  `entitlement_resolution` and `BulkCapabilityEvaluator._entitled`
  (`services/capabilities.py:20-38`, `:41-60`, `:275-285`). They agree today;
  nothing keeps them agreeing (§19).
- **The calendar runs five COUNT queries for one summary block**
  (`views.py:773-779`), where a single conditional aggregate would do (§19).
- **A platform-scope `DELETE /entitlements/<capability>/`** made with no
  `?tenant=` removes the platform-wide grant for every tenant that was relying
  on it, with only a free-text `reason` as a speed bump (`views.py:715-725`).
  Judgment call rather than a defect: the key is CRITICAL and platform-gated.
- **`Capability.metadata` is exposed raw** by the serializer and stored raw in
  the audit snapshot. It is documented as non-authoritative and is never read
  by the evaluator, so this is a note rather than a control-surface risk.
- **Justified by design:** `bulk_effective_capabilities` evaluates inactive
  capabilities but omits them from the response
  (`services/capabilities.py:315-322`). An inactive prerequisite must still be
  able to turn a dependent OFF.
- **Justified by design:** entitlements ignore branch scope entirely
  (`views.py:685-688`). Branches do not buy modules.
- **Justified by design:** `ends_at` is exclusive
  (`services/capabilities.py:37`). A subscription ending "on" a date is off from
  that instant.

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Restricted |
|---|---|---|---|
| Catalogue read | `config.capability.view` | NORMAL | no |
| Catalogue write/archive | `config.capability.manage` | SENSITIVE | yes |
| Entitlement read | `config.entitlement.view` | SENSITIVE | yes |
| Entitlement write/reset/bulk | `config.entitlement.manage` | **CRITICAL** | yes |
| Override read | `config.override.view` | NORMAL | no |
| Override write | `config.override.manage` | SENSITIVE | yes |

Seeded at `seed_config_permissions.py:8-10`, granted to `xvs_super_admin` and
`xvs_platform_admin` only.

**Commercial writes are platform-only; runtime writes are not.** `POST
/entitlements/`, `/bulk-schedule/` and `DELETE /entitlements/<capability>/` all
appear in `platform_methods` (`views.py:677`, `:833`, `:712`). `POST
/overrides/` does not, on purpose: a school that holds
`config.override.manage` may pause its own features, and
`resolve_request_scope` guarantees the write lands at that school's own scope
or a branch inside it.

**A school can never reach the platform override layer.** `scope_tenant` is
`None` only when the asserted tenant is itself a PLATFORM tenant
(`services/scopes.py:51-55`).

**`?all_tenants=true` on the calendar is the one cross-tenant read in this
slice**, and it is gated on the caller's **home** tenant being PLATFORM
(`views.py:748-753`), so an impersonated CX staffer is refused. It exposes
every school's slug, name and subscription dates, which is why
`config.entitlement.view` is SENSITIVE rather than NORMAL. Tested at
`tests.py:1037-1057`.

**Bulk scheduling cannot be aimed at a tenant the caller did not name.** Every
slug in `items` is resolved against `Tenant.objects.filter(kind=SCHOOL)` and an
unknown slug is refused with a message that does not distinguish "no such
school" from "not yours" (`views.py:850-864`).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:264-331` | `Capability` |
| `models.py:334-399` | `CapabilityDependency` - three-layer cycle protection |
| `models.py:402-498` | `CapabilityEntitlement` |
| `models.py:501-573` | `CapabilityOverride` |
| `services/capabilities.py:20-60` | `_active_entitlement`, `entitlement_resolution` |
| `services/capabilities.py:64-101` | `effective_capability` - the single-row evaluator |
| `services/capabilities.py:105-138` | `set_entitlement` |
| `services/capabilities.py:141-213` | `bulk_schedule_entitlements` |
| `services/capabilities.py:216-236` | `clear_entitlement` |
| `services/capabilities.py:239-322` | `BulkCapabilityEvaluator`, `bulk_effective_capabilities` |
| `services/capabilities.py:326-355` | `set_override` - the entitlement gate |
| `views.py:594-672` | Capability catalogue endpoints |
| `views.py:676-880` | Entitlement list/set, reset, calendar, bulk schedule |
| `views.py:884-928` | Override list/set, effective capabilities |
| `serializers.py:208-369` | Capability, entitlement and override serializers |
| `conf.py:20-25` | `is_capability_enabled` - the public boolean API, fails closed |
| `seed_config_catalogue.py:116-136` | 13 seeded capabilities and 2 dependency edges |

### Who reads capabilities

| Consumer | File |
|---|---|
| Vendor portal gate | `vs_procurement/vendor_portal.py:21` |
| Import validation | `vs_import_data/services/validation_service.py:232` |
| School package setup and serializers | `vs_schools/views/package.py:5`, `vs_schools/serializers.py:26` |
| Dev seeding | `core/management/commands/seed_dev_data.py:358` |

`is_capability_enabled` fails closed: an unknown or inactive key returns
`False` (`conf.py:20-25`).

## 11. Test coverage & gaps

Baseline: **`Ran 61 tests in 94.867s` - OK**.

What this slice covers:

- `CapabilityEvaluationTests` (`tests.py:163-264`) - a runtime override cannot
  bypass entitlement; entitlement and the most specific override are
  independent; dependencies must be effective, not merely present; dependency
  cycles are rejected; a scheduled entitlement activates and expires purely by
  the clock; and bulk evaluation matches single evaluation inside a bounded
  query count.
- `EntitlementManagementAPITests` (`tests.py:809-879`) - a real JWT login,
  `?tenant=` cross-tenant assertion, scheduling accepted, reset restoring the
  platform layer with the right `source`, and a past expiry rejected.
- `EntitlementOperationsAPITests` (`tests.py:967-1057`) - the calendar's expiry
  warnings across two schools, bulk schedule updating every selected grant
  atomically, and a school operator refused a cross-school bulk schedule.
- `ConfigurationAPISecurityTests.test_capability_archive_is_idempotent`
  (`tests.py:376-396`).

What it does not cover:

1. **Bulk schedule against a DENIED entitlement** (issues file §2). Every
   fixture row in `test_bulk_schedule_updates_every_selected_grant_atomically`
   is already GRANTED, which is exactly why the state-forcing survived.
2. **`default_enabled: false` on an entitlement-gated capability** (issues
   file §4). No test asserts what the field is documented to do.
3. **Pagination of `/entitlements/` or `/overrides/`** (issues file §5): no
   test requests page 2, so the missing ordering is invisible.
4. **`POST /overrides/` from a school caller at branch scope**, and the
   `INHERIT` state - nothing writes an INHERIT row through the API.
5. **`PATCH /capabilities/<key>/` with `dependencies: []`**, and a PATCH that
   would introduce a cycle through the API rather than the model.
6. **`?include_inactive=true`** on the capability catalogue.
7. **The calendar's `truncated` flag** and the 500-entry cap.
8. **The empty-list shape** of `/effective-capabilities/`, which
   `success_response` would render as `{}` (`core/response.py:6-11`).
