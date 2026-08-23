# school_records

The school itself: the `School` row, the `Tenant` it is welded to, the slug that
becomes a sign-in address and then freezes, the school code, the branding row,
the school-level primary administrator, and the five endpoints CX uses to
create, find, read and edit a school.

Branches are `school_branches`; subscription plans and module entitlements are
`school_packages_entitlements`; turning a contact into a real account, giving a
school its books and the reset-config operation are `school_provisioning`.

Routes covered by this slice, mounted at `/v1/i/` (`apps/urls.py:23`):
`` (list), `create/`, `stats/`, `<slug>/`, `<slug>/update/`.

Findings for the whole module are collected in
**`error/schools/school_code_issues.md`**; §8 below points at the ones belonging
here rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **A school is a profile on a tenant, not the tenant itself.** `School.tenant`
  is a non-nullable `OneToOneField` with `on_delete=PROTECT` and
  `related_name="school_profile"` (`models.py:140-145`). The ownership boundary
  the whole platform scopes on is `vs_tenants.Tenant`; `School` is the
  schools-product record hanging off it.
- **`School.save()` creates the tenant when there is none**, inside its own
  `transaction.atomic`, so the pair is committed or rolled back as one unit -
  and that holds for direct ORM creation and tests, not only for the onboarding
  service (`models.py:270-287`).
- **The slug is editable until go-live and frozen for ever after.**
  `_check_slug_change` refuses a rename once `activated_at` is set or the status
  is ACTIVE (`models.py:199-228`). `activated_at` is written once, so it answers
  "has this school ever been live?" rather than "is it live now?" - a school
  suspended for an unpaid invoice cannot rename itself while it is off.
- **The slug is mirrored onto the tenant on every save, but only while it is
  still thawed** (`models.py:335-345`). Correcting a typo before go-live has to
  reach the tenant or it does not reach the sign-in address at all; after
  go-live the mirror is skipped so an ordinary metadata save cannot move a live
  school's host.
- **`School.slug` is no longer the primary key.** It is a unique business
  identifier over a surrogate `BigAutoField`, so renaming a school does not
  rewrite every FK on the platform (`models.py:147-156`). Audit events are keyed
  on the pk for exactly that reason (`serializers.py:1235-1246`).
- **`status` is mirrored onto the tenant and is owned by another app.** Nothing
  in `vs_schools` ever sets it to ACTIVE, INACTIVE or SUSPENDED. Go-live is
  `schools/vs_onboarding/services/go_live.py:204`; expiry and reinstatement are
  `schools/vs_onboarding/services/lifecycle.py:192` and `:473`. This app creates
  every school PENDING (`serializers.py:1026-1029`) and never moves it.
- **How many sites a school has is counted, never stored.** There was a boolean
  for it, and a flag can disagree with the rows it describes
  (`models.py:166-169`). `School.branches` is a property that hops through the
  tenant.
- **Every school has at least one branch, from the moment it exists.**
  `branches` on the create serializer is `required=True, allow_empty=False`
  (`serializers.py:865-875`). It used to default to an empty list, which let
  this endpoint mint a school with nowhere to put a user, a document or a
  student - and every branch rule sat behind an `if branches:` that never ran
  for the one payload that needed it.
- **`name` is deliberately not editable.** The spreadsheet importer identifies a
  school by name when the row carries no slug, so a rename would silently turn a
  school's own import file into a request to create a second school
  (`serializers.py:1276-1281`).
- **This is a CX console, not a school-facing surface.** Every route takes a
  `platform.schools.*` key, and all of them are `PermissionScope.PLATFORM`, so
  no school role can hold one. The list and the stats deliberately return
  **every school on the platform** - the export dataset says so out loud
  (`export_datasets.py:5-21`).
- **There is no delete.** No endpoint removes a school, and `School.tenant` is
  `PROTECT` in the other direction. Winding a school down is an onboarding
  lifecycle action, not a DELETE.

## 2. Domain model

### `SchoolStatus` (`models.py:45`)

| Value | Written by |
|---|---|
| `PENDING` | `SchoolCreateSerializer.create` (every school starts here), and onboarding reinstatement |
| `ACTIVE` | `vs_onboarding.services.go_live` only |
| `SUSPENDED` | `vs_onboarding.services.lifecycle` (onboarding expiry) only |
| `INACTIVE` | **Nothing.** Filterable and counted, reachable by no code path |

`SUSPENDED` is a school status and not only a tenant one on purpose: `save()`
mirrors this column onto the tenant on every write, so a tenant suspended on its
own would be quietly returned to PENDING by the next ordinary edit of its school
(`models.py:49-53`).

### `School` (`models.py:110`)

| Field | Meaning |
|---|---|
| `tenant` | OneToOne, PROTECT. Created by `save()` if absent |
| `name` | Display name. Not editable through the API |
| `slug` | Unique, `slug_validator`, 80 chars. The `/v1/i/<slug>/` path key and the sign-in host |
| `code` | Unique, uppercase, allocated as `SC-…` on first save if blank |
| `address`, `website`, `motto`, `registration_id` | Optional metadata |
| `ownership_type` | `PUBLIC` / `PRIVATE` / `FAITH_BASED` / `NGO` |
| `term_structure` | `2_SEMESTERS` / `3_TERMS` |
| `currency` | `NGN` / `USD`. Read by `provision_books_for_school` as the ledger base currency |
| `status` | Indexed; mirrored onto `Tenant.status` |
| `activated_at`, `deactivated_at` | Mirrored onto the tenant; written by `vs_onboarding` |

Meta: indexes on `slug` and `(status, created_at)`; a `slug_not_empty` check
constraint complementing the strict validator; ordering `-created_at`.

Three helpers around the freeze (`models.py:199-263`):

| Helper | What it answers |
|---|---|
| `_stored_identity()` | The row as the database currently holds it, as a dict - because the in-memory instance is exactly the thing that may already have been edited |
| `_has_been_live(stored)` | `activated_at is not None or status == ACTIVE` |
| `has_ever_been_live()` | The public half, so the update serializer can refuse a rename with a typed 409 rather than letting a field error escape from `save()` |

Two branch helpers (`models.py:350-374`):

| Property | Query |
|---|---|
| `branches` | `self.tenant.branches` - still a manager, so `.filter()`, `.count()` and DRF's `many=True` behave as before. Prefetch it as `"tenant__branches"` |
| `main_branch` | `self.branches.select_related("primary_admin", "primary_admin__contact").filter(is_main=True).first()` - **a new queryset, so it defeats any prefetch**; see `school_code_issues` §8 |

### `SchoolBranding` (`models.py:377`)

`school` OneToOne (CASCADE) + `logo` (`ImageField`, `upload_to="school_logos/"`).
That is the whole model. The docstring anticipates theme fields; none exist.

### `ContactInfo` (`models.py:516`)

`full_name`, `email`, `phone`, indexed on `email`. Deliberately stand-alone -
an invitation target that does not require a `User` row yet. **The email is not
unique**, and every creation path calls `ContactInfo.objects.create(...)`
unconditionally (`serializers.py:545-549`, `1052-1056`, `1106-1110`), so the
same person invited twice gets two rows.

### `SchoolPrimaryAdmin` (`models.py:574`)

`school` OneToOne (CASCADE), `contact` FK (PROTECT), `school_role`
(default `"IT Head"`), `invite_status`, `invite_queued_at`, `invite_sent_at`.
Indexed on `(school, invite_status)`.

`InviteStatus` is `QUEUED` / `SENT` / `FAILED`. `FAILED` is written by nothing:
`provision_admin_user` stamps SENT on success and swallows every failure, so a
failed invite stays QUEUED for ever (`services/admin_provisioning.py:170-178`).

### Dead vocabulary in this file

`OperationOutcome` (`models.py:62`) and `Modules` (`models.py:74`) are declared
and referenced nowhere in the repo. The live module catalogue is
`vs_config.Capability` with `kind=MODULE`; `Modules` is a stale second copy of
that vocabulary sitting in the model file
(`school_code_issues` §17).

## 3. Endpoint map

| Route | Verb | `rbac_permission` | Body / filters actually read |
|---|---|---|---|
| `/v1/i/` | GET | `platform.schools.view` | `?status=` (comma-separated), `?active=`, `?inactive=`, `?q=`, `?ordering=` (allowlisted to eight values). Paginated (`XVSPagination`, 25) |
| `/v1/i/stats/` | GET | `platform.schools.view` | none. One aggregate query |
| `/v1/i/create/` | POST | `platform.schools.create` | `name`, `slug`, `code`, `ownership_type`, `address`, `website`, `motto`, `term_structure`, `currency`, `registration_id`, `branding`, `primary_admin_data`, **`branches[]` (required)**, `package_setup_data` |
| `/v1/i/<slug>/` | GET | `platform.schools.view` | none |
| `/v1/i/<slug>/update/` | PUT/PATCH | `platform.schools.update` | `slug`, `ownership_type`, `address`, `website`, `motto`, `term_structure`, `currency`, `registration_id`, `branding` |

All five carry `IsAuthenticatedAndActive & HasRBACPermission`.

The `?q=` search on the list spans the school's own columns **and** its
branches' `state`, `country` and `name`, with `.distinct()` to undo the join
multiplication (`views/school.py:63-72`).

`SchoolListView` prefetches `tenant__branches` - "`tenant__branches`, not
`branches`", because `School.branches` is a property over the tenant's sites and
the prefetch has to name the real path (`views/school.py:42-44`).

`SchoolDetailView.retrieve` is overridden for one reason, recorded in its own
docstring (`views/school.py:146-157`): it used to wrap the whole method in
`except Exception` and answer with a `DEBUG:` message plus
`traceback.format_exc()`, which turned an unknown slug into a 500 and shipped a
full Python stack trace to whoever asked.

`SchoolUpdateView.update` reads the primary key **before** the write and re-reads
the row by it afterwards (`views/school.py:197-205`), because `lookup_field` is
the slug and the slug is editable - re-fetching by the URL key would 404 on
exactly the rename that had just succeeded.

## 4. Lifecycle / state machine

```
  POST /v1/i/create/
        │
        ▼
     PENDING ──────────────────────────────► ACTIVE
   (tenant PENDING,                    vs_onboarding.go_live
    pending_since stamped)             (activated_at set; slug freezes here)
        │                                      │
        │  onboarding expiry sweep             │  onboarding expiry sweep
        ▼                                      ▼
    SUSPENDED ◄──────────────────────────────────
        │
        │  onboarding reinstatement
        ▼
     PENDING            INACTIVE: declared, filterable, counted, unreachable
```

`vs_schools` owns the first arrow and nothing else. Everything to the right of
PENDING is written by `schools/vs_onboarding/`.

The tenant mirror is computed on every `School.save()`
(`models.py:299-345`):

| `School.status` | `Tenant.status` |
|---|---|
| `ACTIVE` | `ACTIVE` |
| `INACTIVE` | `INACTIVE` |
| `SUSPENDED` | `SUSPENDED` |
| anything else (i.e. `PENDING`) | `PENDING` |

and `pending_since` / `expiry_warned_at` are recomputed through
`Tenant.pending_stamps_for` from the **previous** stored status. Reading the row
first is what keeps an ordinary save - a rename, a metadata fix - from
restarting the 90-day onboarding clock (`models.py:305-322`).

## 5. Derivations

### The slug

Three different paths, and they do not agree - see `school_code_issues` §12.

**On create** (`serializers.py:899-940`):

```
supplied slug  -> _normalize_slug  -> reserved? -> unique among Schools? -> use it
no slug        -> _normalize_slug(name) -> if reserved, append "-school"
                                        -> if taken, 400 with suggestions
```

`_normalize_slug` lowercases, strips, runs `django.utils.text.slugify` and then
replaces `_` with `-` (`serializers.py:36-42`). `_slug_is_unique` checks
`School.objects` **only** (`serializers.py:52-56`).

**On update** (`serializers.py:1309-1358`), in this order and for stated reasons:

1. Empty after normalising → 400.
2. Unchanged → return early.
3. `has_ever_been_live()` → `TenantSlugFrozen` (409). First, because it outranks
   the rest: a live school may not move to a free slug either, so "that one is
   taken" would be misleading advice.
4. Reserved → 400.
5. Taken by another **School** → 400 with suggestions.
6. Taken by any other **Tenant** → 400. This step exists because the mirror in
   `School.save()` is a queryset `update()` which cannot raise a field error,
   only an `IntegrityError` against the tenant's unique index - a clinic group or
   an ORGANIZATION tenant holding the name is enough.

Step 6 has no counterpart on the create path.

### The school code

```python
self.code = str(self.code or "").strip().upper()
if not self.code:
    self.code = next_tenant_document_number(tenant=self.tenant, document_code="SC")
```

`models.py:288-297`. If `update_fields` was passed, `"code"` is added to it so
the freshly allocated value is actually written.

`next_tenant_document_number` returns `f"{code}-{tenant.pk}{day:%y%m%d}{n}"`
(`vs_tenants/numbering.py:39`), so a school created today on tenant 7 gets
`SC-72608211`. The sequence restarts at one per `(tenant, document_code, local
date)` and is protected by both a uniqueness constraint and a row lock. The
tenant primary key is inside the string, which is what makes a column that is
`unique=True` platform-wide safe to fill from a per-tenant counter.

### The tenant mirror

Six columns are pushed onto the tenant by a single queryset `update()`
(`models.py:335-345`): `name`, `status`, `activated_at`, `deactivated_at`,
`pending_since`, `expiry_warned_at` - plus `slug`, but only while
`slug_is_frozen` is False.

Because it is an `update()` and not a `save()`, it fires no signals on `Tenant`
and cannot raise a field error.

### The two aggregates

```python
School.objects.aggregate(
    all=Count("slug"),
    active=Count("slug", filter=Q(status=SchoolStatus.ACTIVE)),
    pending=Count("slug", filter=Q(status=SchoolStatus.PENDING)),
    inactive=Count("slug", filter=Q(status=SchoolStatus.INACTIVE)),
)
```

`views/school.py:103-108`. One query, no N+1 - and no `suspended` bucket, so
`active + pending + inactive` stops equalling `all` the moment onboarding
expires a school (`school_code_issues` §7).

### `total_students`

```python
total_students = serializers.ReadOnlyField(default=0)
```

`serializers.py:730` and `:760`. `School` has no `total_students` attribute, no
annotation supplies one, and `ReadOnlyField` has no source - so both the list
and the detail always report `0` (`school_code_issues` §6).

## 6. What writing writes

### `POST /v1/i/create/`

One `transaction.atomic` (`serializers.py:1018`), in this order:

| Step | Rows |
|---|---|
| 1 | `School` (status PENDING) → and inside its `save()`, one `Tenant` and one `SC-…` code |
| 2 | `SchoolBranding`, if `branding` was sent |
| 3 | `TenantRoleTemplate` for `school_admin` via `provision_role_from_prebuilt`, plus its `TenantRolePermission` rows |
| 4 | If `primary_admin_data`: one `ContactInfo`, one `SchoolPrimaryAdmin` (QUEUED), then `provision_admin_user` → `User`, `TenantUserRoleAssignment`, `UserInvitation`, a queued invitation email, and the link stamped SENT |
| 5 | Per branch: one `Branch` (PENDING, code allocated under a tenant-row lock), a branch-scoped `TenantRoleTemplate`, one `BranchLifecycle`, and the same contact/link/user provisioning |
| 6 | If `package_setup_data`: one `SchoolPackageSetup` plus one `CapabilityEntitlement` per module **and per transitive dependency** |
| 7 | `provision_books_for_school` - a `LedgerEntity` and its chart, in its own savepoint, never fatal |
| 8 | `provision_onboarding_for_school` - the checklist, in its own savepoint, never fatal |
| 9 | Audit: one `SCHOOL/CREATE` event, plus one `BRANCH/CREATE` per branch |

The two "best effort" steps are best effort for a stated reason: a school, its
tenant, its first administrator, its branches and its entitlements are worth far
more than its books or its checklist, and both can be repaired afterwards
(`serializers.py:1206-1227`, `services/books.py:16-20`).

Audit events are keyed on `str(school.pk)`, **not** the slug, because a
slug-keyed trail splits down the middle the moment the slug is corrected
(`serializers.py:1235-1246`). The slug is named in the `summary` instead, which
is what the Event Explorer's free-text search reads.

### `PATCH /v1/i/<slug>/update/`

`serializers.py:1360-1454`. Snapshot → apply → refuse if nothing changed
(`{"detail": "No changes detected in update payload."}`) → `full_clean` as field
errors → `save()` → branding upsert → one `SCHOOL/UPDATE` audit event.

A slug move gets its own sentence and its own severity: `WARNING` rather than
`INFO`, with a summary naming **both** addresses, because the old slug is
recoverable from the diff but not *searchable* - and someone holding the dead
address needs to find out where the school went (`serializers.py:1412-1425`).

`emit_audit_event` never raises and logs its own failures, which is the audit
app's stated contract - so a failed audit write leaves the school edit standing
rather than rolling back a legitimate correction over a logging fault
(`serializers.py:1427-1432`).

## 7. Worked example

CX onboards Bright Star Academy: two sites, a school-level IT head, the Standard
plan.

**1. The request.**

```http
POST /v1/i/create/?tenant=codex
{
  "name": "Bright Star Academy",
  "ownership_type": "PRIVATE",
  "currency": "NGN",
  "primary_admin_data": {"full_name": "Ada Okoye", "email": "ada@brightstar.test"},
  "branches": [
    {"name": "Ikeja", "is_main": true,
     "primary_admin_data": {"full_name": "Tunde Bello", "email": "tunde@brightstar.test"}},
    {"name": "Lekki",
     "primary_admin_data": {"full_name": "Ngozi Eze", "email": "ngozi@brightstar.test"}}
  ],
  "package_setup_data": {"package_plan": "standard", "student_capacity": 700,
                         "teacher_capacity": 50, "admin_capacity": 8,
                         "enabled_modules": ["procurement"]}
}
```

**2. Validation.** No slug was sent, so one is generated:
`_normalize_slug("Bright Star Academy")` → `bright-star-academy`, not reserved,
free among schools. `ownership_type`, `term_structure`, `currency` and each
branch's `country` fall back to the platform onboarding defaults. The three
admin addresses are checked in one query. Branch names are unique within the
submission and exactly one is `is_main`.

**3. The school and its tenant.** `School.objects.create(status=PENDING)` runs
`save()`, which creates `Tenant(slug="bright-star-academy", kind=SCHOOL,
status=PENDING)` and allocates `code = "SC-0001"` for that tenant. Then the
mirror `update()` pushes name, status and the pending stamps back onto the
tenant. Because the school is not live, the slug is mirrored too.

**4. Ada gets an account.** A `ContactInfo`, a `SchoolPrimaryAdmin` at QUEUED,
then `provision_admin_user`: the `school_admin` role template was provisioned a
moment earlier, so `role_obj` resolves, a PENDING inactive `User` is created on
the tenant with `branch=None`, a `TenantUserRoleAssignment` is written, an
invitation is minted and the email is queued. The link flips to SENT.

**5. Two branches.** Each gets `code` 1 and 2 under a tenant-row lock, a
`BranchLifecycle` row from `""` to `PENDING`, its own branch-scoped
`branch_admin` role template, and its own admin provisioned the same way -
except that if Tunde's address had matched Ada's, the link would be stamped SENT
with no second user and no second email (`serializers.py:1119-1123`).

**6. The package.** `SchoolPackageSetup` is created against the Standard plan,
whose ceilings (800 students, 60 teachers, 10 admins) are all satisfied.
`enabled_modules` was `["procurement"]`; the dependency closure walks
`dependency_links` and adds `finance`, because a picked module must not end up
entitled-but-off (`serializers.py:1180-1191`). Two `CapabilityEntitlement` rows
are written through `vs_config.set_entitlement`, which owns the canonical scope
key and audits each grant.

**7. Books and checklist.** `provision_books_for_school` derives
`BRIGHTSTARACADE` (sixteen characters of the slug, letters and digits only) and
calls `vs_finance.provision_books`. `provision_onboarding_for_school` builds the
control room. Both run in their own savepoint.

**8. The typo.** Someone notices the school should be `brightstar-academy`.
Because it has never been live, `PATCH /v1/i/bright-star-academy/update/`
with `{"slug": "brightstar-academy"}` is accepted: not reserved, free among
schools, free among tenants. `School.save()` mirrors it onto the tenant, so the
sign-in host moves with it. The audit event is a WARNING reading *"Sign-in
address for Bright Star Academy moved from bright-star-academy to
brightstar-academy"*.

**9. After go-live.** Once `vs_onboarding` flips the school ACTIVE and stamps
`activated_at`, the same PATCH is refused with `TenantSlugFrozen` - a 409 saying
the address is fixed because changing it would break every link and sign-in its
users already have. And an ordinary metadata save no longer mirrors the slug at
all, so a school whose slug drifted from its tenant's under the old rules cannot
have that drift resolved by a motto edit silently moving a live host.

**10. What the console then shows.** `GET /v1/i/` returns Bright Star with
`total_students: 0` - and it will still read `0` when the school has nine hundred
students, because the field is a literal (`school_code_issues` §6). Its
`main_branch` costs one extra query per row on every page
(`school_code_issues` §8).

## 8. Gotchas / known limitations

Recorded in full in **`error/schools/school_code_issues.md`**. The items
belonging to this slice:

| # in that file | One line |
|---|---|
| §6 | **Confirmed by execution.** `total_students` is a hardcoded `0` on both the list and the detail |
| §7 | **Confirmed by execution.** The stats endpoint has no `suspended` bucket, so its numbers stop adding up once onboarding expires a school |
| §8 | **Confirmed by execution.** `School.main_branch` builds a fresh queryset, so the school list is N+1 despite its prefetch |
| §9 | `SchoolStatus.INACTIVE` is filterable and counted, and no code path can produce it |
| §12 | **Confirmed by execution.** The create path checks the slug against schools only; the update path also checks tenants, so a name a clinic already holds passes creation and dies as an unhelpful 400 |
| §13 | There is no endpoint to view, correct or re-send a school's primary-admin invitation |
| §16 | `ContactInfo` rows are created unconditionally, so the same person invited twice gets two |
| §17 | `Modules`, `OperationOutcome`, `InviteStatus.FAILED` and an empty `signals.py` are declared and used by nothing |

Design choices worth stating as choices:

- **The list is deliberately unfenced.** `School.objects.all()` behind
  `platform.schools.view` is the CX register of every school, and the export
  dataset states the consequence in its own module docstring: anyone holding
  that key can export the whole register, so it must stay a platform grant
  (`export_datasets.py:5-21`).
- **Audit events are keyed on the pk, never the slug**, on all three write paths
  (`serializers.py:1235-1246`, `:1439-1442`, `:1536-1538`), so a rename does not
  split a school's trail in two.
- **`name` is not editable**, and the reason is written down
  (`serializers.py:1276-1281`) rather than left as an omission.
- **The freeze is enforced twice.** `validate_slug` answers an HTTP caller
  properly; `School._check_slug_change` still refuses a shell or a data
  migration (`serializers.py:1310-1318`).

## 9. Permissions & tenant isolation

- **Three keys gate this slice**, all in the `platform.schools` resource:
  `view` and `create` and `update` are `NORMAL`; `delete` and `manage` exist and
  are `SENSITIVE` but no view in this app uses them
  (`core/management/commands/seed_platform_permissions.py:113-122`).
- **All five are `PermissionScope.PLATFORM`.** `platform.schools.*` is not in
  `TENANT_HOLDABLE_KEYS`, so `seed_platform_permissions` classifies it PLATFORM
  and the RBAC grant guard refuses to write it onto a school role
  (`vs_rbac/models.py:91-111`).
- **Both codex roles hold them** - `xvs_super_admin` and `xvs_platform_admin`
  are granted every platform key except `platform.roles.transfer`.
- **There is no tenant scoping on any queryset here, by design.** `School`,
  unlike almost every other model in the repo, does not use `TenantAwareManager`
  - it has no `objects = TenantAwareManager()` declaration - so the ambient
  tenant context does nothing. Isolation is the permission key and nothing else.
- **The `?tenant=` assertion is still required.** No view sets
  `tenant_param_required = False`, so a caller must name a tenant they may
  assert - in practice `codex` - even though the response spans every tenant.
- **A cross-tenant read is impossible for a school actor** not because the
  queryset stops them but because `TenantJWTAuthentication` refuses a foreign
  slug and the key is PLATFORM-scoped. Both would have to fail together.
- **The one live risk is the RBAC escalation** recorded as
  `error/rbac/rbac_code_issues.md` §1: a counterfeit `xvs_super_admin` role
  bypasses `HasRBACPermission` entirely, and this register is one of the
  unfenced surfaces it reaches.

## 10. Code map

| File | What lives there |
|---|---|
| `schools/vs_schools/models.py:45-104` | `SchoolStatus`, `InviteStatus`, `OperationOutcome`, `PlanTier`, `Modules`, `OwnershipType`, `TermStructure`, `Currency`, `BillingCycle` |
| `schools/vs_schools/models.py:110-374` | `School`, the slug freeze, the tenant mirror, `branches` / `main_branch` |
| `schools/vs_schools/models.py:377-392` | `SchoolBranding` |
| `schools/vs_schools/models.py:516-531` | `ContactInfo` |
| `schools/vs_schools/models.py:574-608` | `SchoolPrimaryAdmin` |
| `schools/vs_schools/views/school.py` | All five school endpoints |
| `schools/vs_schools/serializers.py:36-79` | `_normalize_slug`, `_build_slug_suggestions`, `_slug_is_unique`, `full_clean_as_field_errors` |
| `schools/vs_schools/serializers.py:724-798` | `SchoolListSerializer`, `SchoolDetailSerializer` |
| `schools/vs_schools/serializers.py:841-1262` | `SchoolCreateSerializer` and its nine-step create |
| `schools/vs_schools/serializers.py:1265-1454` | `SchoolUpdateSerializer`, the slug rules and the audit |
| `schools/vs_schools/export_datasets.py` | The `platform.schools` dataset and its screen binding |
| `schools/vs_schools/apps.py` | The pinned `vs_schools` label and the dataset registration |
| `vs_tenants/models.py` | `Tenant`, `RESERVED_TENANT_SLUGS`, `slug_is_reserved`, `pending_stamps_for` |
| `schools/vs_onboarding/services/go_live.py:204` | The one place a school becomes ACTIVE |
| `schools/vs_onboarding/services/lifecycle.py:192`, `:473` | Suspension and reinstatement |

`schools/vs_schools/signals.py` is an **empty file** that `AppConfig.ready`
imports (`apps.py:17`). There are no signals in this app; every audit event is
emitted from a serializer.

## 11. Test coverage & gaps

Module baseline at the time of writing: see `school_code_issues` for the exact
`Ran N tests` line and the command. The app is the slowest in the repo, and
`apps/settings/local.py:73` records why: two `TransactionTestCase` classes tagged
`slow` flush and rebuild the database around every test and re-run the migration
graph.

Covered for this slice:

- `tests.py` (1,344 lines) - school creation end to end, slug generation and
  collision, reserved slugs, the required-branches rule, the exactly-one-main
  rule, branch name uniqueness within a submission.
- `tests_update_endpoints.py` (1,155 lines) - `SchoolSlugUpdateTests` covers the
  rename, the freeze after go-live, the reserved and taken cases, the tenant
  mirror, and the audit event's severity and summary.
- The `_MigrationHarness` classes re-run the migration graph, which is what
  guards the `vs_schools` → `schools.vs_schools` move and the `Branch`
  relocation.

Not covered:

- **No test asserts `total_students` reflects anything** (§6), which is why the
  literal has survived.
- No test creates a school whose slug is already held by a **non-school** tenant
  (§12).
- No test calls the stats endpoint with a SUSPENDED school present (§7).
- No test counts queries on the school list, so the `main_branch` N+1 (§8) is
  invisible.
- `SchoolStatus.INACTIVE` has no test, because nothing can produce it (§9).
