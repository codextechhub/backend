# Removing the school coupling from the engine apps

Scoping document. Analysis only, no code changed.

Status: proposal, awaiting a decision.
Subject: `vs_schools.Branch` and the 39 foreign keys that point at it.

---

## 1. What this is and why

`CLAUDE.md` states the rule: the engine apps (`vs_finance`, `vs_procurement`,
`vs_payments`, `vs_rbac`, `vs_workflow`, `vs_notifications`, `vs_audit`, `core`)
are domain-neutral. They know entities, customers, invoices, vendors, roles and
approvals. They must not know about schools.

Today six of those eight import `vs_schools`, because the platform's *site*
primitive lives inside the schools app. `vs_schools.Branch`
(`apps/vs_schools/models.py:252`) has no tenant of its own; it hangs off `School`
via a CASCADE foreign key, so anything that needs to know "which tenant owns this
site" has to travel `branch.school.tenant`. `vs_tenants.Tenant` is already
properly neutral. The site concept is the last piece that is not.

The goal being scoped: **no engine app imports `vs_schools`**, with no loss of
behaviour.

---

## 2. The headline: this is much smaller than it looks

Three things were expected to be the hard part. All three turned out to be
already solved, or never true.

**`LedgerEntity.source_school` does not exist.** The prompt for this work, the
FAL spec, and five files in `docs/finance/` all describe `source_school` as the
entity-to-school link. The tenant refactor removed it. `LedgerEntity` now carries
a plain `tenant` FK (`apps/vs_finance/models/core.py:127-132`) and nothing else.
The loose `source_type`/`source_id` pattern was proposed as the fix for
`source_school`; it is not needed, because the field it would replace is gone.

**`School` has zero foreign keys from outside `vs_schools`.** Every one of the 21
school FKs that B23 had to rewrite has since been removed. The four remaining
`School` FKs are all internal to `vs_schools` (`Branch`, `SchoolBranding`,
`SchoolPackageSetup`, `SchoolPrimaryAdmin`). `School` is already fully contained.

**`resolve_entity` has no school in it.** `apps/vs_finance/views.py:56-83`
resolves `?entity=` strictly against `request.tenant`. The FAL spec's description
of it (`filters LedgerEntity.objects.filter(source_school=school)`, reads
`request.school` set by `TenantContextMiddleware`) describes code that no longer
exists; `request.school` is set by nothing anywhere in the repo.

What is left is one model. `Branch` is referenced by 39 foreign key columns
outside its own app, and every single one of them:

* is named `branch_id`,
* is `bigint NULL`,
* points at `vs_schools_branch(id)`, an ordinary `BigAutoField` primary key,
* sits on a table that **already carries its own `tenant` or `entity` FK**, so
  the tenant is on the row and the `branch.school.tenant` walk is redundant.

That last point is the one that decides the shape of the work. Nothing depends on
`Branch` to *establish* tenancy. It only depends on `Branch` to *check* tenancy,
and that check is one join longer than it needs to be.

---

## 3. Recommendation

**Move `Branch` to `vs_tenants` with a direct `tenant` foreign key, keep the
table name, keep the class name, and drop the `school` FK.** `School` reaches its
sites through `school.tenant.branches`.

Why this one:

* It is the only option that actually achieves "no engine imports `vs_schools`".
  An alias or an indirection layer (option b below) leaves the import in place
  and just hides it.
* It removes the `branch.school.tenant` traversal at 14 sites rather than
  rerouting it, and each of those is a cross-tenant security check that gets
  shorter and cheaper.
* Because the primary key value, the column type and the table are all unchanged,
  the model move itself is a **state-only migration**: no FK constraint is
  dropped, no column is rewritten, no row is touched. This is the opposite of
  B23, and the reason it is opposite is explained in §7.
* It does not change one byte of the API. `branch_id` in the JWT
  (`apps/vs_user/tokens.py:50`), `branch_id`/`branch_name` on every procurement
  serializer, `?branch=` query params: all unchanged, because the ids are
  unchanged.

**Keep the class name `Branch`.** "Branch" is not school vocabulary. A bank, a
clinic chain and a retail group all have branches; `vs_health` will want the same
concept. Renaming it to `Site` would touch 39 field declarations, every
serializer field name, the JWT claim and every API response key, for no
neutrality gain. The leak is the `school` FK and the app label, not the noun.

**The real cost is not the migration.** It is:

1. One genuine DDL migration (add `Branch.tenant`, backfill, set NOT NULL).
2. Ten state-only migrations for the move.
3. Fourteen traversal rewrites, each of which is a tenant-isolation check that
   must not be got wrong.
4. Thirteen test files that import `vs_schools` and construct `Branch` through
   `School`.

Item 3 is where the risk lives. Everything else is mechanical.

**One thing to fix while in there, and it is not caused by this work.** `Branch`
has **no database uniqueness of any kind**. `Meta.constraints = []`
(`apps/vs_schools/models.py:334`) while the docstring five lines above promises
"unique constraints for non-zero codes per school and single main branch"
(`:272-273`). Neither exists. `allocate_next_code` (`:346-355`) locks with
`select_for_update().filter(school=school)`, which locks **no rows at all** when a
school has no branches yet, so two concurrent first-branch creates can both read
`max=0` and both write code 1. And nothing stops two `is_main=True` branches. See
R1.

---

## 4. The dependency surface

Counted by introspecting the loaded app registry, not by grepping.

### 4.1 Structural: foreign keys to `Branch` (39 outside `vs_schools`)

| App | Tables | Where declared | on_delete |
|---|---|---|---|
| `vs_finance` | 19 | `models/core.py:280` (`FinanceDocument`, abstract, 14 concrete tables), `models/core.py:229` (`DocumentSequence`), `models/ar.py:51` (`Customer`), `models/ar.py:378` (`FeeStructure`), `models/ops.py:56` (`BankAccount`), `models/ops.py:502` (`PettyCashFund`) | PROTECT |
| `vs_procurement` | 8 | `models.py:241` (`Vendor`); the other seven inherit `FinanceDocument` | PROTECT |
| `vs_config` | 5 | `models.py:169` (`ScopedModel`, abstract, 5 concrete tables) | CASCADE |
| `vs_rbac` | 2 | `models.py:376` (`TenantRoleTemplate`), `models.py:463` (`TenantUserRoleAssignment`); import at `models.py:12` | PROTECT |
| `vs_workflow` | 2 | `models.py:69` (`WorkflowTemplate`), `models.py:273` (`WorkflowInstance`) | PROTECT |
| `vs_user` | 1 | `models.py:127` (`User.branch`); import at `models.py:28` | PROTECT |
| `vs_tickets` | 1 | `models.py:95` (`Ticket`); import at `models.py:9` | PROTECT |
| `vs_import_data` | 1 | `models.py:220` (`ImportBatch`); import at `models.py:10` | CASCADE |

Plus two inside `vs_schools` that stay behind: `BranchLifecycle`
(`models.py:548`) and `BranchPrimaryAdmin` (`models.py:600`).

All 39 external columns are nullable. All are `bigint`.

### 4.2 Structural: foreign keys to `School`

Four, all inside `vs_schools`: `Branch.school` (`models.py:281`, CASCADE),
`SchoolBranding.school`, `SchoolPackageSetup.school`, `SchoolPrimaryAdmin.school`.
**None from any other app.**

### 4.3 Structural: migration dependencies

Nine apps declare `("vs_schools", "0001_initial")`:

* `apps/vs_config/migrations/0002_initial.py:13`, `0007_...py:14`
* `apps/vs_finance/migrations/0001_initial.py:13`, `0002_initial.py:13`
* `apps/vs_import_data/migrations/0001_initial.py:14`
* `apps/vs_procurement/migrations/0001_initial.py:14`, `0002_initial.py:14`,
  `0008_...py:37`
* `apps/vs_rbac/migrations/0002_initial.py:13`
* `apps/vs_tickets/migrations/0002_initial.py:12`
* `apps/vs_user/migrations/0001_initial.py:18`
* `apps/vs_workflow/migrations/0001_initial.py:15`

These stay. Historical migrations are never edited; the state-only move layers on
top of them.

### 4.4 Incidental: `branch.school.tenant` traversals (disappear with the move)

Each of these is a cross-tenant check or a scope resolution. All 14 become
`branch.tenant`.

| File:line | What it does |
|---|---|
| `apps/vs_rbac/managers.py:56, 58` | `for_tenant` path selection (`school__tenant`, `branch__school__tenant`) |
| `apps/vs_rbac/managers.py:106, 108` | `TenantAwareManager.get_queryset` lookup rewriting |
| `apps/vs_rbac/models.py:403` | `TenantRoleTemplate.clean` cross-tenant guard |
| `apps/vs_rbac/models.py:507` | `TenantUserRoleAssignment.clean` cross-tenant guard |
| `apps/vs_rbac/serializers/tenant.py:205` | `validate_branch` on the role serializer |
| `apps/vs_rbac/serializers/tenant.py:505` | branch check on the assignment serializer |
| `apps/vs_procurement/views/base.py:151` | `_resolve_branch_reference` tenant filter (import at `:142`) |
| `apps/vs_procurement/approval_coverage.py:196` | branch enumeration per tenant (import at `:191`) |
| `apps/vs_config/models.py:182` | `ScopedModel.clean` tenant derivation |
| `apps/vs_config/services/scopes.py:26, 64, 67` | `normalize_scope` / `resolve_request_scope` (import at `:3`) |
| `apps/vs_config/services/audit.py:32` | audit row tenant derivation |
| `apps/vs_user/models.py:251, 268` | `_derive_tenant` and the save-time guard |
| `apps/vs_user/serializers.py:331` | branch tenant check (import at `:20`) |
| `apps/vs_tickets/models.py:127` | `Ticket.clean` cross-tenant guard |
| `apps/vs_tenants/management/commands/reconcile_tenants.py:24, 30` | invariant checks (import at `:10`) |

Useful confirmation: `TenantAwareManager` was introspected across every model
that uses it. **`Branch` is the only model in the codebase that resolves through
`school`, and no model at all resolves through `branch__school`.** So
`managers.py:55-58` and `:91-93, 105-108` are entirely dead once `Branch` carries
its own tenant. That is a simplification, not a risk.

### 4.5 Incidental: everything else

| File:line | What it is | Verdict |
|---|---|---|
| `apps/vs_rbac/serializers/registry.py:25-57` | `SchoolField` (accepts id or slug, renders slug, a B23 compatibility shim) | **Dead code.** Referenced nowhere. Delete. |
| `apps/vs_user/services/auth.py:149-157` | `_resolve_school` | **Dead code.** Defined, never called. Delete. |
| `apps/vs_tenants/management/commands/reconcile_tenants.py:28` | `LedgerEntity.objects.filter(source_school__isnull=False)` | **Broken.** The field does not exist; this command raises `FieldError` when run. Fix or delete. |
| `apps/core/tasks_base.py:55-57` | `_job_school_id` metadata branch | Unreachable. Nothing ever writes `_job_school_id` (`:46` is only an allowlist). |
| `apps/vs_finance/serializers.py:73-84` | `source_school_id` derived from `tenant.school_profile` | Live API field, deliberate wire compatibility. Keep, but it is the last "school" word in `vs_finance` and should be renamed on the next frontend contract change. |
| `apps/vs_config/serializers.py:20`, `apps/vs_config/views.py:91` | imports `Currency`, `OwnershipType`, `TermStructure` from `vs_schools` | **A separate leak, in the wrong direction.** `vs_config` is a platform app importing school enumerations for choice fields (`serializers.py:130-132`, `views.py:364-370`). Not fixed by moving `Branch`. Track separately. |
| `apps/vs_config/runtime_settings.py:101-116` | dotted-path strings naming `vs_schools.serializers.*` as setting consumers | Metadata only, no import. Harmless. |
| `apps/vs_notifications/management/commands/seed_notification_settings.py:59` | late import of `School` for `--all-schools` | A seeding command, not engine code. Acceptable, but it is why the naive grep on `vs_notifications` is not clean. |
| `apps/core/management/commands/seed_dev_data.py:312, 382`, `seed_package.py:4`, `reset_db.py:153` | dev/seed commands | Not engine code. `core` fails a naive grep because of these. |
| `apps/vs_admin_console/overview.py:63` | active-school count for the CX landing screen | Legitimately about schools. `vs_admin_console` is not an engine app. |
| `apps/vs_payments/views.py:1123` | a comment mentioning `vs_schools` | Comment only. `vs_payments` has **no** code dependency on `vs_schools`. |
| `apps/apps/urls.py:24`, `apps/apps/settings/base.py:88` | project wiring | Expected. |
| 13 test files | see §9 | Mechanical. |

---

## 5. `Branch` itself: what is generic and what is not

| Element | Generic site concern | School-specific | Notes |
|---|---|---|---|
| `school` FK (`:281`) | | **yes** | The whole problem. Replaced by `tenant`. |
| `name` (`:288`) | yes | | |
| `code` (`:289`) | yes | | Per-owner 1..N sequence. Per-school and per-tenant are identical because `School.tenant` is a `OneToOneField` (`:139-144`). |
| `is_main` (`:295`) | yes | | Every multi-site org has a head office. |
| `_type` (`:300`) | yes | | Free-form CharField. The *values* ("Primary", "Secondary") are school-flavoured; the field is not. |
| `address`, `email`, `country`, `state` (`:303-306`) | yes | | |
| `status` + `BranchStatus` (`:48-53`, `:308`) | yes | | ACTIVE / PENDING / SUSPENDED / INACTIVE / CLOSED. Nothing school-shaped. |
| `opened_at`, `closed_at`, `activated_at`, `deactivated_at` (`:315-319`) | yes | | |
| `objects = TenantAwareManager()` (`:323`) | yes | | Gets simpler: the lookup becomes `tenant`, one join shorter. |
| `allocate_next_code` (`:346-355`) | yes | | Moves as-is with `school=` becoming `tenant=`. See R1 for the race it does not prevent. |
| `transition` / `mark_*` (`:366-397`) | yes | | Generic lifecycle. |
| `BranchLifecycle` (`:538-566`) | yes | | Generic status-transition audit. Moves with `Branch`. |
| `BranchPrimaryAdmin` (`:591-625`) | | **yes** | Invite/onboarding machinery, defaults `"Head Teacher"` / `"BRANCH_ADMIN"` (`:610-611`). Nothing outside `vs_schools` references it. **Leave in the school app**, pointing at the moved `Branch`. It is M9's territory. |
| `ContactInfo` (`:573-588`) | yes, in principle | | Referenced only by `BranchPrimaryAdmin` and `SchoolPrimaryAdmin`. Leave it where its only two users are. |

So: `Branch` + `BranchStatus` + `BranchLifecycle` move. `BranchPrimaryAdmin` and
`ContactInfo` stay in the school app. `School` gains nothing and loses one
reverse accessor, replaced by a property.

---

## 6. Options considered

### (a) Move `Branch` to `vs_tenants` with a direct `tenant` FK (RECOMMENDED)

`School` keeps a relationship to its sites through `school.tenant.branches`, plus
a `branches` property for source compatibility.

* Behaviour preserved: fully. Same ids, same table, same API.
* Migration risk: low. One real DDL step (add + backfill + NOT NULL), then
  state-only. Reversible up to the point where `school_id` is physically dropped,
  which is a separate final migration.
* Blast radius: 39 FK declarations retargeted (string change only), 14 traversals
  rewritten, 13 test files, 10 state-only migrations.
* Achieves the goal: **yes**, for all eight engine apps and for `vs_config`,
  `vs_user`, `vs_tickets` and `vs_import_data` as well.

### (b) Leave the model, add a neutral alias or indirection

For example `core.sites.get_site_model()` returning `vs_schools.Branch`, with
engine FKs declared through a swappable-model setting.

* Behaviour preserved: fully, trivially.
* Migration risk: none.
* Blast radius: small.
* Achieves the goal: **no.** The import is still there, just laundered. Django
  swappable models also bring their own migration pain (every FK becomes
  `settings.SITE_MODEL`, and the app registry still has to load `vs_schools` for
  any engine to work). It hides the violation from the grep without removing it,
  which is worse than leaving it visible.

### (c) New neutral `Site` owned by `Tenant`, `School` holds school-specific site attributes

A fresh `vs_tenants.Site` table; `Branch` becomes a school-side satellite holding
`_type` and the primary-admin link, with a one-to-one to `Site`.

* Behaviour preserved: yes, but only after a data migration that splits 39 FK
  columns' worth of references onto new ids.
* Migration risk: **high.** This is the only option that changes primary key
  values, and it is therefore the only option that reproduces the B23 problem in
  full: 39 columns to rewrite, on two database vendors, with FK constraints
  dropped and rebuilt around it.
* Blast radius: everything in (a), plus a value rewrite, plus a join added to
  every read that wants `_type` or the primary admin.
* Achieves the goal: yes.
* Verdict: **the same destination as (a) at several times the risk.** The only
  thing it buys is that `_type` lives in the school app. `_type` is a free-form
  CharField. That is not worth a value-rewriting migration.

### (d) Do nothing

Worth stating because the cost is real but deferred. `vs_schools` cannot move
into `apps/schools/` cleanly while nine apps depend on its label, and every new
school module added in August adds more code that reaches sites through
`school__…`. The traversal count only grows.

---

## 7. Migration shape, and what B23 teaches

### 7.1 The B23 precedent

B23 (`58b98ae`, hardened by `7cfee69`, recorded at `todo.md:113-119`) flipped
`School` from a slug primary key to a surrogate `BigAutoField`. One migration,
`vs_schools/0003_school_id_alter_school_slug.py`, using
`SeparateDatabaseAndState`: the state side changed two fields and Django derived
all 21 dependent FKs from the target PK automatically; the database side was a
hand-written, vendor-branched routine that discovered and dropped every FK
constraint referencing the school table, dropped CHECK constraints that mentioned
a referencing column, added and populated `id`, rewrote all 21 columns from slug
values to id values, swapped the primary key, converted the columns to `BIGINT`
and re-added the constraints. It was declared irreversible.

Six things it teaches, and what each means here:

1. **`SeparateDatabaseAndState` is the tool, and this repo has already run it at
   larger scale.** The pattern is proven in this codebase on both vendors.
2. **B23 was hard because the primary key's *value and type* changed. Here
   neither changes.** No column is rewritten. No column is retyped. If `db_table`
   is preserved, the referenced table is literally the same table, so **no FK
   constraint is dropped or rebuilt at all**. The move is state-only end to end.
3. **The `_like` index trap cannot occur.** B23's worst PostgreSQL bug was that
   Django pairs every varchar FK column with a `varchar_pattern_ops` index that
   cannot hold a `bigint`, so the indexes had to be discovered and dropped before
   the type conversion (`7cfee69`). That trap only exists when a column changes
   type. Nothing changes type here.
4. **Dependency blocks matter for fresh installs.** B23's migration listed every
   app whose migrations create a school FK, so a clean database replays in the
   right order. Do the same: the `vs_tenants` create must come first, the eight
   per-app `AlterField` migrations must depend on it, and the `vs_schools` delete
   must depend on all eight. Getting this wrong does not corrupt anything; it
   fails the CI fresh-migrate job loudly, which is the point.
5. **Vendor branching is only needed where raw SQL is.** B23 needed MariaDB and
   PostgreSQL branches throughout. Here the only data step is the `tenant`
   backfill, and it should be written as an ORM `RunPython` using the historical
   model and `Subquery`, not raw SQL, so it is vendor-neutral by construction.
   Local MariaDB (`DB_ENGINE=mysql`) and PostgreSQL then need no separate code
   path. Note also from `todo.md:96-104` that PostgreSQL is the standard across
   local, CI and staging since B17; MariaDB is a fallback, not the primary.
6. **Back up before the DDL step, as B23 did** (`/tmp/cx_db_backup_before_b23.sql`,
   and the explicit "BACK UP staging Postgres before deploying this" note).

### 7.2 The order

**Phase A: clear the dead wood.** No migration. Delete `SchoolField`
(`vs_rbac/serializers/registry.py:25-57`) and `_resolve_school`
(`vs_user/services/auth.py:149-157`); fix the broken `source_school` check in
`reconcile_tenants.py:28`; remove the unreachable `_job_school_id` path in
`core/tasks_base.py:55-57`. This alone removes three of the six engine-app
`vs_schools` imports and is independently shippable.

**Phase B: give `Branch` a tenant.** Real DDL, in `vs_schools`, three operations
that must be three operations:

1. `AddField("branch", "tenant", FK(vs_tenants.Tenant, PROTECT, null=True, db_index=True))`
2. `RunPython` backfill: `tenant_id = school.tenant_id`, reverse = `noop`.
   `School.tenant` is a non-nullable `OneToOneField`, so every branch has exactly
   one tenant and the backfill cannot leave a null.
3. `AlterField` to `null=False`.

Also add `Branch.save()` derivation of `tenant` from `school` so nothing can
create a tenantless branch during the deploy window, and add the constraints from
R1 here (after de-duplicating, if there is anything to de-duplicate). Reversible.
Independently shippable.

**Phase C: retire the traversal.** Pure code, no migration. Rewrite all 14 sites
in §4.4 from `branch.school.tenant` to `branch.tenant`, and delete the now-dead
`school` / `branch__school` cases from `TenantAwareManager` (`managers.py:55-58,
91-93, 105-108`). Every one of these gets one join shorter. Independently
shippable, and the point at which most of the value is already banked.

**Phase D: the move.** State-only, no SQL, ten migrations:

1. `vs_tenants`: `SeparateDatabaseAndState(state_operations=[CreateModel("Branch",
   …, options={"db_table": "vs_schools_branch", …})])`.
2. Eight per-app migrations, one each in `vs_finance`, `vs_procurement`,
   `vs_config`, `vs_rbac`, `vs_workflow`, `vs_user`, `vs_tickets`,
   `vs_import_data`: `SeparateDatabaseAndState(state_operations=[AlterField(...,
   to="vs_tenants.Branch")])` for each branch FK, each depending on D1.
3. `vs_schools`: `SeparateDatabaseAndState(state_operations=[DeleteModel("Branch")])`
   plus the state-only removal of `Branch.school`, depending on all eight.
   `BranchLifecycle` moves with `Branch` the same way.

`School.branches` becomes a property over `self.tenant.branches`. `Branch.school`
becomes a property over `self.tenant.school_profile`, which keeps
`BranchListSerializer.school_slug` / `BranchDetailSerializer.school_slug`
(`vs_schools/serializers.py:335, 354`) working without an API change. See R9 for
the caveat on that property.

**Phase E, optional and separate: tidy the database.** Drop the physical
`vs_schools_branch.school_id` column; optionally `AlterModelTable` to
`vs_tenants_branch` (both PostgreSQL and MySQL carry FK constraints through a
table rename, so this is safe, but the constraint *names* keep saying
`vs_schools` and that is only cosmetic). This is the only step that is not
cleanly reversible, so it must be its own migration and must not block Phase D.

### 7.3 Where a naive migration breaks

* **Running `makemigrations` after moving the class** emits `DeleteModel` +
  `CreateModel` with real DDL. That drops the table, every row, and all 39 FK
  constraints. `SeparateDatabaseAndState` is not optional.
* **Forgetting `db_table` on the new model.** Django would then expect
  `vs_tenants_branch`, and every subsequent query and migration targets a table
  that does not exist.
* **Deleting from `vs_schools` before the dependent apps retarget.** State has
  FKs to a missing model; `migrate` fails on any fresh database.
* **Adding `tenant` as NOT NULL in one step.** A PROTECT FK with no default
  cannot be added NOT NULL to a populated table. Three steps or nothing.
* **Writing the backfill against `Branch.objects`.** That is the
  `TenantAwareManager`; in a service it would silently scope to the ambient
  tenant. Use the historical model inside `RunPython`, or `all_objects`.
* **Assuming `School.slug == Tenant.slug`.** `School.save` syncs the tenant's
  `name`, `status` and activation timestamps but **not** its `slug`
  (`vs_schools/models.py:228-233`), so a renamed school has divergent slugs.
  `Branch.__str__` prints `f"{self.school.slug}:{self.code}"` (`:338`); rerouting
  it to `tenant.slug` changes that string for renamed schools. Route it through
  the `school` property, not through `tenant`.
* **Dropping `Branch.school` before checking onboarding.** `BranchCreateSerializer`
  and the school onboarding flow create the main branch under a school.

---

## 8. Risk register

| # | Risk | Rating | Mitigation |
|---|---|---|---|
| **R1** | **`Branch` has no database uniqueness at all.** `Meta.constraints = []` (`models.py:334`) contradicts its own docstring (`:272-273`). `allocate_next_code` (`:346-355`) locks `select_for_update().filter(school=school)`, which locks **zero rows** when the school has no branches yet, so two concurrent first-branch creates both compute code 1. Nothing prevents two `is_main=True` rows either. Pre-existing, not caused by this work, but this work is the moment to fix it. | **High** | Add `UniqueConstraint(tenant, code)` in Phase B, after checking live data for existing duplicates. Enforce single-main in `save()` rather than a conditional constraint: MariaDB cannot do conditional uniqueness (the same reason `LedgerEntityManager.platform` resolves by code, `vs_finance/models/core.py:52-56`). |
| **R2** | **The 14 traversal rewrites are tenant-isolation checks.** Getting one wrong turns a cross-tenant guard into a no-op, silently. | **High** | Do Phase C as one change, not spread across phases. Cover with `vs_rbac/tests/test_tenant_isolation.py` plus a case per site. Use the negative-control technique from the procurement branch work: neutralise the check and prove the tests fail. |
| **R3** | **`db_table` preservation.** If missed, every query breaks. | High impact, near-zero likelihood | Caught by the first test run and by `makemigrations --check`. |
| **R4** | **Fresh-install replay ordering** across ten new migrations in nine apps. | **Medium** | The CI fresh-migrate job added in B17 (`.github/workflows/ci.yml`) exists precisely for this. It fails loudly, never silently. |
| **R5** | **`Branch.school` is CASCADE today.** Deleting a `School` deletes its branches (blocked in practice by 39 PROTECT FKs once documents exist). With only `tenant` (PROTECT), that cascade is gone. | **Medium** | A deliberate decision, not an accident: state it. In practice the change is only visible for branches with no documents. If school deletion must still remove empty sites, do it explicitly in the school delete service. |
| **R6** | **RBAC scope resolution and the branch argument.** `HasRBACPermission` reads `request.branch` (`vs_rbac/permissions.py:181, 255`), which **nothing anywhere sets**, so branch-scoped grants never gate a view. Already recorded in `todo.md`. | Medium, pre-existing | Out of scope here, but must be flagged in the change summary so the move is not mistaken for having fixed it. `vs_workflow/services/approvers.py:60` forwards `branch` only for `BRANCH` scope, and that is unaffected. |
| **R7** | **JWT school/branch claims.** | **Low** | Already checked. `tokens.py:48-52` emits `tenant_id`, `tenant_slug`, `branch_id`, `account_status`, `full_name`. No school claim exists (the module docstring at `:5` and `:37` is stale and says otherwise). `branch_id` is an opaque integer PK that does not change. Zero wire impact. |
| **R8** | **`TenantAwareManager` and tenant middleware.** | **Low** | Introspected: `Branch` is the only model resolving through `school`, and no model resolves through `branch__school`. `vs_tenants/middleware.py` and `vs_tenants/context.py` contain no school reference at all. The manager change is a deletion of dead paths. |
| **R9** | **`Branch.school` as a property.** Two live API fields depend on it (`serializers.py:335, 354`). A property doing `self.tenant.school_profile` adds a query per branch in list serialization. | **Low** | Keep `select_related("tenant__school_profile")` on the branch list queryset (it currently does `select_related("school")`, `views/branch.py:35`). Cheap, but easy to forget and it turns a branch list into an N+1. |
| **R10** | **`resolve_entity`.** | **None** | `vs_finance/views.py:56-83` is already purely tenant-based. Nothing to change. |
| **R11** | **Does anything assume a branch has exactly one school?** | **None found** | The relationship is `Branch.school` FK to `School`, and `School.tenant` is a `OneToOneField`. Branch-to-school is many-to-one and branch-to-tenant becomes many-to-one with identical cardinality. Nothing anywhere walks the reverse direction expecting more than one. |
| **R12** | **Unique constraints that are currently per-school.** | **None** | There are none. See R1: the promised per-school constraints were never created. The only site-shaped uniqueness in the codebase is `WorkflowTemplate`'s `(tenant, branch, document_type, code)` (`vs_workflow/models.py:88-92`) and `vs_config`'s `scope_key`, both already keyed on tenant, not school. |
| **R13** | **`vs_config` imports school enumerations** (`Currency`, `OwnershipType`, `TermStructure`) for choice fields. | Medium, separate | Not fixed by this work and must not be conflated with it. Either invert the dependency (the school app registers its choices) or accept that `vs_config` is a product-configuration app rather than an engine. Decide separately. |

---

## 9. Tests

Thirteen non-school files import `vs_schools`, most only to build a `School` and
a `Branch` in fixtures:

`vs_finance/tests.py:182, 7528`, `vs_finance/tests_settings.py:27`,
`vs_procurement/tests_settings.py:28`, `vs_payments/tests.py:1301, 1346`,
`vs_rbac/tests/helpers.py:11`, `vs_rbac/tests/test_tenant_isolation.py:18`,
`vs_workflow` (via helpers), `vs_tickets/tests.py:24`, `vs_user/tests.py:348, 902`,
`vs_notifications/tests.py:24, 840`, `vs_tenants/tests.py:7`,
`vs_admin_console/tests_overview.py:33`, `core/test_seed_school_permissions.py:27`,
`vs_exports/tests.py:1360` (a string in an app-name list).

`vs_rbac/tests/helpers.py:11` is the one that matters: it is the shared fixture
builder, so fixing it fixes most of the rest. Test imports of `vs_schools` are
acceptable in principle (a test may exercise the school product against the
engines), but the fixture helper should build a `Tenant` and a `Branch` directly,
so that engine tests stop needing a school to exist at all. That is also the
strongest proof the decoupling is real.

---

## 10. Sequencing against August

Two items already in `todo.md` interact with this:

* `todo.md:21`: create `apps/schools/` and move `vs_schools` inside it,
  preserving app labels and `db_table`s via an explicit `AppConfig`. Scheduled
  before the M9 backend starts.
* `todo.md:20`: implement `core/fal/`, wave 1 being the school-to-entity and
  student-to-customer resolvers.

**Do the `Branch` move before both, and before the nine school modules.** The
reasons are ordering, not urgency:

1. **It makes the `apps/schools/` move smaller, not bigger.** Today nine apps
   depend on the `vs_schools` label. Moving the package while that is true means
   the label must be preserved forever by an `AppConfig` override, which is
   exactly what `todo.md:21` already plans. Move `Branch` out first and the
   remaining `vs_schools` label has **no external dependents at all** except
   dev-seed commands and tests, so the package move becomes genuinely free, and
   the label could even be renamed later.
2. **Every new school module written against `school__…` adds traversals to
   rewrite.** Nine modules, each with lists and reports that want branch scope.
   The cost of doing this later scales with how much school code exists.
3. **Phases A, B and C are independently shippable and do not block anything.**
   Only Phase D needs a quiet moment, and it is state-only.

**Does the FAL's design change?** Yes, but not because of `source_school`
becoming loose. It changes because **the FAL spec is already written against
code that no longer exists**, and this work does not make that worse:

* The spec describes `LedgerEntity.source_school` and
  `LedgerEntity.objects.filter(source_school=school)` (spec §4, lines 143-146,
  164-169, 234, 268-270). That field is gone; the link is `entity.tenant`.
* The spec makes `request.school`, set by `TenantContextMiddleware`, a hard
  precondition and warns that "if neither is attached, the lookup 404s for every
  school-scoped user" (spec lines 149-155). **Nothing in the repo sets
  `request.school`.** The middleware that did was replaced by
  `TenantContextCleanupMiddleware` plus `TenantJWTAuthentication`, and the
  assertion is now `?tenant=<slug>` (`vs_tenants/resolution.py`).
* The spec's `FinanceRbacPort` is specified as
  `has_permission(user, key, school=...)` (spec line 27, 255-270). The evaluator's
  signature is `has_permission(user, key, tenant=None, branch=None)`
  (`vs_rbac/evaluator.py:140`); the `school=` kwarg survives only on the thin
  wrapper `vs_rbac/permissions.py:48` and is not forwarded.

So the FAL adapter should be written against `tenant` and `branch` from the
start. Concretely: `resolve_entity(school_ref)` becomes a tenant lookup with the
school resolved as `tenant.school_profile` at the FAL boundary only; the
"one primary entity per school" rule becomes one primary entity per tenant; and
`ensure_customer(..., branch_ref=...)` resolves the branch against
`tenant`, not against `school__tenant`. None of that is caused by moving
`Branch`; it is caused by the tenant refactor that already shipped, and it needs
correcting in the spec before wave 1 regardless.

**The `Customer.source_type`/`source_id` pattern stays exactly as it is.** It is
the right pattern (`vs_finance/models/ar.py:65-69`, indexed at `:80`), it already
carries `"vs_schools.Student"` as a plain string, and the FAL's student-to-customer
resolver is built on it. Nothing about this work touches it. The reason it was
proposed as the fix for `source_school` no longer applies, because `source_school`
is already gone.

---

## 11. The finish line

### 11.1 The check

Match real couplings (imports, FK target strings, `get_model`) rather than the
bare word, so a comment or a docstring cannot fail the build and an import cannot
hide behind one. Across the eight engine apps named in `CLAUDE.md`:

```sh
grep -rnE "from vs_schools|import vs_schools|['\"]vs_schools\.[A-Z]|get_model\(['\"]vs_schools" \
  --include="*.py" \
  apps/vs_finance apps/vs_procurement apps/vs_payments apps/vs_rbac \
  apps/vs_workflow apps/vs_notifications apps/vs_audit apps/core \
  | grep -vE "/migrations/|/test"
```

Today that returns **23 lines**. `vs_audit` and `vs_payments` are already clean
(`vs_payments` matches the bare word only in a comment, `views.py:1123`). The 23
break down as:

| Count | What | Resolved by |
|---|---|---|
| 12 | `"vs_schools.Branch"` FK target strings | Phase D: they become `"vs_tenants.Branch"` |
| 5 | `import Branch` (`vs_rbac/models.py:12`, `vs_rbac/serializers/tenant.py:18`, `vs_procurement/views/base.py:142`, `vs_procurement/approval_coverage.py:191`, and the `Branch` in `vs_rbac/models.py:12`) | Phases C and D: they become `vs_tenants` imports, or disappear with the traversal |
| 4 | `import School` in dead code (`vs_rbac/serializers/registry.py:34, 39, 52`, `core/tasks_base.py:56`) | Phase A: deleted outright |
| 2 | `"vs_schools.Student"` as a *documentation example* of the loose `source_type` reference (`vs_finance/models/ar.py:37, 67`) | A one-line docstring and `help_text` edit to a domain-agnostic example. The pattern is correct; only the example names a product app. |
| 4 | dev-seed commands (`core/management/commands/seed_dev_data.py:312, 382`, `seed_package.py:4`, `vs_notifications/.../seed_notification_settings.py:59`) | Move the school dev seeds into the school package when `apps/schools/` is created. Until then, add `/management/commands/seed_` to the exclusion and say so out loud, rather than pretending they are not there. |

The wider version, covering the platform apps that are not on the `CLAUDE.md`
engine list but are equally not school apps (`vs_config`, `vs_user`,
`vs_tickets`, `vs_import_data`, `vs_exports`), returns 42 today:

```sh
grep -rnE "from vs_schools|import vs_schools|['\"]vs_schools\.[A-Z]|get_model\(['\"]vs_schools" \
  --include="*.py" apps/ \
  | grep -vE "^apps/vs_schools/|^apps/apps/|/migrations/|/test"
```

`vs_admin_console` and the onboarding paths in `vs_import_data` legitimately
belong to the school product and will not reach zero; that is an argument for
scoping the CI gate to the eight engine apps and reviewing the wider number by
hand.

Historical migrations keep their `("vs_schools", "0001_initial")` dependencies
forever, which is correct and must stay excluded. Tests are excluded because a
test may legitimately exercise the school product; the fixture helper
(`vs_rbac/tests/helpers.py`) should still be converted, as §9 says, but that is a
quality goal rather than a boundary rule.

### 11.2 What to add to `CLAUDE.md`

Under "Keep the engines domain-neutral", after the existing paragraph:

> The engines must not import `vs_schools` (or anything under `apps/schools/`).
> The site primitive is `vs_tenants.Branch`, owned directly by `Tenant`; reach it
> as `row.branch` or `tenant.branches`, never as `branch.school.tenant`. If an
> engine needs a school-only fact, it belongs behind the FAL, not behind an
> import. The check is the grep in
> `docs/architecture/school-decoupling-scope.md` §11.1; it must return nothing.

And, because this is the failure mode that actually recurs, a second line:

> An engine app may not import school enumerations either. `Currency`,
> `OwnershipType` and `TermStructure` living in `vs_schools` and being imported
> by `vs_config` is the same leak wearing a different hat.

A CI step running the §11.1 grep is worth more than either paragraph.

---

## 12. Summary of decisions being asked for

1. Move `Branch` to `vs_tenants` with a direct `tenant` FK, keeping the class
   name and the table name. (Option (a).)
2. Keep `BranchPrimaryAdmin` and `ContactInfo` in the school app.
3. Add the missing uniqueness on `(tenant, code)` and fix the first-branch
   allocation race while in there.
4. Sequence this before the `apps/schools/` package move, before `core/fal/`, and
   before the nine August school modules.
5. Correct the FAL spec's `source_school` and `request.school` assumptions
   independently, because they are already wrong.
6. Track the `vs_config` school-enumeration import as a separate leak.
