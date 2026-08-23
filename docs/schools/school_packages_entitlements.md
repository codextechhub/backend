# school_packages_entitlements

What a school is sold and what that buys it: the `PackagePlan` catalogue, the
`SchoolPackageSetup` row that applies one to a school, the module picker backed
by `vs_config.Capability`, the dependency closure that turns a picked module into
a set of grants, and the two read-only endpoints the create wizard's dropdowns
are built from.

The school record is `school_records`; sites are `school_branches`; giving a
school its books and its first accounts is `school_provisioning`.

Routes covered by this slice, mounted at `/v1/i/` (`apps/urls.py:23`):
`package-plans/`, `modules/`.

Findings for the whole module are collected in
**`error/schools/school_code_issues.md`**; §8 below points at the ones belonging
here rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **A plan is a price sheet with ceilings, not an enforcement mechanism.**
  `PackagePlan` carries `max_students`, `max_teachers`, `max_admins` and
  `max_branch` (`models.py:412-415`). They are validated **against the capacities
  the operator types into the wizard** and against nothing else.
- **A capacity is a stored number that no create path consults.**
  `student_capacity`, `teacher_capacity` and `admin_capacity` live on
  `SchoolPackageSetup`, are exposed in the school detail payload, are importable
  from a spreadsheet - and are read by no code that gates the creation of a
  student, a teacher, an admin or a branch. See `school_code_issues` §3.
- **Modules are not stored on the package.** `SchoolPackageSetup` has no module
  column. Picking modules writes `CapabilityEntitlement` rows through
  `vs_config`, and reading them back queries those rows
  (`serializers.py:269-279`, `1196-1204`).
- **The module catalogue is `vs_config.Capability`, not `vs_schools.Modules`.**
  The `Modules` TextChoices in this app's `models.py:74-82` is a stale second
  copy of the vocabulary and is referenced nowhere (`school_code_issues` §17).
- **Entitlement is one of three inputs, not the answer.** Whether a module is ON
  for a branch is computed by `vs_config.services.capabilities.effective_capability`
  from entitlement, dependencies and overrides
  (`vs_config/models.py:270-284`). Granting an entitlement makes a module
  *allowed*, not *on*.
- **The dependency closure runs at grant time, not only at evaluation time.** A
  picked module must not end up entitled-but-off because its requirement was not
  ticked, so the grant set is closed transitively before anything is written
  (`serializers.py:1180-1191`).
- **`vs_config` owns the write.** `set_entitlement` computes the canonical
  `"tenant:<id>"` scope key and audits every grant; writing
  `CapabilityEntitlement` rows here directly is what let this path drift out of
  step with it (`serializers.py:1193-1204`).
- **Entitlements are tenant-scoped, never school-scoped.** The read filters on
  `tenant_id` rather than on a NULL-tenant platform grant, which keeps one school
  from reading another's package (`serializers.py:270-277`).
- **A package is optional.** `package_setup_data` is `required=False` on the
  create serializer (`serializers.py:876`), so a school can exist with no plan,
  no capacities and no module entitlements at all.
- **There is no endpoint to change a package.** `SchoolUpdateSerializer` does not
  expose `package_setup_data` (`serializers.py:1293-1307`). A plan is chosen once,
  at creation, and after that it can only be changed through `vs_config`'s own
  entitlement surfaces or a shell.
- **`PlanTier` is not `PackagePlan`.** `PlanTier` (`models.py:67-71`) is a
  TextChoices - BASIC / STANDARD / PREMIUM / ENTERPRISE - that no model field
  uses. The four seeded plans happen to carry those names as free text.

## 2. Domain model

### `PackagePlan` (`models.py:395`)

| Field | Meaning |
|---|---|
| `name` | Unique display name |
| `code` | Unique slug - the value API payloads carry (`package_plan: "standard"`) |
| `description` | Free text shown in the picker |
| `billing_cycle` | `YEARLY` / `MONTHLY`. Read by nothing in this repo |
| `max_students`, `max_teachers`, `max_admins` | `null` means unlimited; an int is a ceiling checked against the wizard's input |
| `max_branch` | `null` means unlimited; **an int is checked against nothing at all** |
| `is_active` | Only active plans are listed and only active plans are selectable |

Ordering `name`. The four seeded rows
(`management/commands/seed_package.py:28-85`):

| Name | code | students | teachers | admins | branches |
|---|---|---|---|---|---|
| Basic | `basic` | 200 | 20 | 3 | 1 |
| Standard | `standard` | 800 | 60 | 10 | 5 |
| Premium | `premium` | 3000 | 200 | 30 | 20 |
| Enterprise | `enterprise` | unlimited | unlimited | unlimited | unlimited |

The seeder lives in the school package rather than in `core` because that is
what it seeds - `PackagePlan` and `BillingCycle` are school models, and a command
in a domain-neutral app importing them was one of three places the school app
leaked into the engines (`seed_package.py:1-10`). Django discovers management
commands per app, so `manage.py seed_package` still resolves and every runbook
that names it keeps working.

### `SchoolPackageSetup` (`models.py:428`)

`school` OneToOne (CASCADE) - so at most one setup per school - plus
`package_plan` FK (PROTECT), the three capacities,
`subscription_expires_at` (a `DateField`, **not null**), `is_active` and `notes`.

`clean()` (`models.py:464-509`) enforces:

- each capacity ≥ 1;
- `subscription_expires_at` not before `timezone.localdate()`;
- each capacity within the plan's ceiling, where the ceiling is not `None`.

`save()` calls `full_clean()` unconditionally (`models.py:511-513`), so those
rules hold for a shell write and a data migration as well as for the API.

Nothing reads `is_active`, `notes` or `subscription_expires_at` outside this
app: no sweep expires a subscription, and no gate consults the flag.

### `Capability` (`vs_config/models.py:264`), as this slice uses it

The module picker reads `kind=MODULE, is_active=True`, ordered by `label`, with
`dependency_links__requires` prefetched (`views/package.py:31-35`). A capability
is either a whole product MODULE or a smaller FEATURE; `kind` is the only
distinction.

`CapabilityDependency` (`vs_config/models.py:334`) forms a directed acyclic graph
- the seeded edges include `procurement → finance` and
`parent_portal → student_portal`. Three layers keep it sane: a unique constraint
on duplicate edges, a check constraint on self-references, and cycle detection.

## 3. Endpoint map

| Route | Verb | Gate | Returns |
|---|---|---|---|
| `/v1/i/package-plans/` | GET | `IsAuthenticatedAndActive & IsVisionStaff` | Every `is_active=True` plan, ordered by name |
| `/v1/i/modules/` | GET | `IsAuthenticatedAndActive & IsVisionStaff` | Every active `MODULE` capability, ordered by label, each with its dependency keys |

Both are `ListAPIView` with read-only serializers and no filters. Neither
declares an `rbac_permission`: the gate is `IsVisionStaff`, which is "an account
on a `Tenant.Kind.PLATFORM` tenant" and nothing more
(`vs_rbac/permissions.py:233-244`). That is a coarser check than the rest of the
app uses - see `school_code_issues` §17.

`XVSModuleSerializer` adds a `dependencies` field so the picker can warn the
operator that procurement needs finance, because a module with an unmet
dependency stays OFF even when granted (`serializers.py:162-180`). The prefetch
on the view is what keeps that free of N+1.

**Writing a package is not a route.** It is the `package_setup_data` block on
`POST /v1/i/create/`, handled by `SchoolPackageSetupWriteSerializer`
(`serializers.py:183-258`):

| Field | Rule |
|---|---|
| `package_plan` | The plan's **`code`**, resolved against `is_active=True`. More stable than a numeric pk, and it is what the dropdown naturally emits |
| `enabled_modules` | A list of capability **`key`** strings, each resolved against `is_active=True, kind=MODULE`. Optional, defaults to `[]` |
| `student_capacity`, `teacher_capacity`, `admin_capacity` | Integers, `min_value=1` |
| `subscription_expires_at` | Optional date. Must not be in the past. Defaults to one year out |

Reading a package back is the nested `package_setup` block on the school detail
payload (`SchoolPackageSetupReadSerializer`, `serializers.py:261-296`).

## 4. Lifecycle / state machine

There is no state machine. A package setup is created once and never
transitions:

```
   POST /v1/i/create/ with package_setup_data
             │
             ▼
   SchoolPackageSetup (is_active=True, subscription_expires_at = +1 year)
             │
             ├── N CapabilityEntitlement rows, state=GRANTED, source=PACKAGE
             │   (one per picked module + one per transitive dependency)
             │
             ▼
   … and then nothing. No renewal, no expiry sweep, no upgrade endpoint,
     no downgrade endpoint, and no re-run of the closure if the dependency
     graph changes later.
```

`subscription_expires_at` passing has no effect anywhere: no task reads it, and
`vs_config`'s entitlement evaluation does not consult it. Renewal and bulk
scheduling exist, but in `vs_config`'s own capability surfaces
(`docs/config/config_capabilities_entitlements.md`), against
`CapabilityEntitlement`, not against this row.

## 5. Derivations

### Capacity validation

Two layers, and they check the same thing twice:

```python
# SchoolPackageSetupWriteSerializer.validate  (serializers.py:228-258)
if plan.max_students is not None and attrs["student_capacity"] > plan.max_students:
    errors["student_capacity"] = f"Exceeds plan limit of {plan.max_students} students."
```

```python
# SchoolPackageSetup.clean  (models.py:479-506)
if self.package_plan.max_students is not None and self.student_capacity > self.package_plan.max_students:
    errors["student_capacity"] = f"Student capacity exceeds plan limit ({self.package_plan.max_students})."
```

The serializer layer answers an HTTP caller with a field error; the model layer
catches a shell write. The wording differs between the two, which is cosmetic.

**`max_branch` has no equivalent at either layer.** There is no
`branch_capacity` field to check it against, and `BranchCreateSerializer` never
looks at the plan (`serializers.py:472-502`). A school on Basic
(`max_branch = 1`) can be given as many branches as CX cares to create.

### The dependency closure

```python
to_grant = {c.pk: c for c in enabled_modules}
stack = list(enabled_modules)
while stack:
    for link in stack.pop().dependency_links.select_related("requires"):
        required = link.requires
        if required.pk not in to_grant and required.is_active:
            to_grant[required.pk] = required
            stack.append(required)
```

`serializers.py:1184-1191`. A depth-first walk with the visited set doubling as
the result, so a diamond is visited once and a cycle cannot loop - though
`CapabilityDependency` already forbids cycles at the database level.

`required.is_active` is checked, so an archived prerequisite is not silently
granted. The wizard mirrors this expansion client-side; this loop is the
guarantee.

One query per capability popped (`dependency_links.select_related("requires")`),
which for a handful of modules is fine and is not on any hot path.

### The grant

```python
for capability in to_grant.values():
    set_entitlement(
        capability=capability,
        tenant=school.tenant,
        state=CapabilityEntitlement.State.GRANTED,
        source=CapabilityEntitlement.Source.PACKAGE,
        actor=actor,
        reason=f"School package setup for {school.name}",
    )
```

`serializers.py:1196-1204`. `source=PACKAGE` is what distinguishes these grants
from a manual one, and `vs_config`'s bulk scheduler is documented as overwriting
that provenance - see `error/config/config_code_issues.md` §2.

### Reading the modules back

```python
capability_ids = CapabilityEntitlement.all_objects.filter(
    tenant_id=obj.school.tenant_id,
    state=CapabilityEntitlement.State.GRANTED,
    source=CapabilityEntitlement.Source.PACKAGE,
).values_list("capability_id", flat=True)
capabilities = Capability.objects.filter(pk__in=capability_ids, is_active=True)
```

`serializers.py:273-278`. Three things to note:

- It uses `all_objects` and supplies `tenant_id=` explicitly, deliberately: the
  boundary is the tenant the school owns, not the ambient request tenant.
- Filtering on `source=PACKAGE` means a module granted **manually** by CX
  through `vs_config` does not appear in the school's `enabled_modules`, even
  though the school has it. The payload answers "what did the package buy?",
  not "what does this school have?", and its field name says the latter.
- It nests `XVSModuleSerializer(capabilities, many=True)` without prefetching
  `dependency_links`, so the school detail response is N+1 across the granted
  modules (`school_code_issues` §17).

### The default expiry

```python
expires_at = package_setup_data.pop("subscription_expires_at", None)
if not expires_at:
    expires_at = date.today() + relativedelta(years=1)
```

`serializers.py:1168-1172`. Note `date.today()` rather than
`timezone.localdate()`, so the default follows the server's clock rather than
the project timezone - a one-day difference at most, and harmless for an annual
date, but inconsistent with the validator directly above it, which uses
`timezone.localdate()` (`serializers.py:250`).

## 6. What writing writes

Only one path writes anything in this slice: step 5 of
`SchoolCreateSerializer.create` (`serializers.py:1163-1204`), inside the school
creation transaction.

| Row | Count |
|---|---|
| `SchoolPackageSetup` | one, and its `save()` runs `full_clean()` |
| `CapabilityEntitlement` | one per picked module **plus** one per transitive active dependency, each written through `vs_config.set_entitlement` |

`set_entitlement` emits its own audit event per grant; this app emits none of its
own for the package. So the school's `SCHOOL/CREATE` audit event does not name
the plan or the modules - a reader has to go to `vs_config`'s configuration audit
trail for that (`docs/config/config_audit_trail_exports.md`).

Nothing in this slice ever updates or deletes a `SchoolPackageSetup`. Deleting
the school cascades it (`models.py:438-442`); the `PackagePlan` behind it is
`PROTECT`, so a plan that any school has ever used cannot be removed.

## 7. Worked example

CX puts Greenfield College on the Basic plan with attendance and procurement.

**1. The wizard's dropdowns.** `GET /v1/i/package-plans/` returns the four active
plans with their ceilings; `GET /v1/i/modules/` returns the active modules, each
with a `dependencies` list. The operator can see that `procurement` depends on
`finance` before ticking anything.

**2. The payload.**

```json
"package_setup_data": {
  "package_plan": "basic",
  "enabled_modules": ["attendance", "procurement"],
  "student_capacity": 180,
  "teacher_capacity": 15,
  "admin_capacity": 3
}
```

**3. Validation.** `package_plan` resolves `"basic"` to the plan row (active
only). Each module key resolves to an active `MODULE` capability. 180 ≤ 200,
15 ≤ 20, 3 ≤ 3 - all within Basic's ceilings. No expiry was sent, so none is
validated.

**4. The write.** `SchoolPackageSetup` is created with
`subscription_expires_at = today + 1 year`; its `save()` re-runs the same three
capacity checks through `full_clean()`.

The closure starts as `{attendance, procurement}`. Popping `procurement` finds
`procurement → finance`; `finance` is active and not yet in the set, so it is
added and pushed. Popping `finance` finds no edges. Popping `attendance` finds
none. The final set is `{attendance, procurement, finance}` - **three** grants
for two ticked boxes, which is the point.

Three `CapabilityEntitlement` rows are written through `set_entitlement` at
`GRANTED` / `PACKAGE`, each audited by `vs_config`.

**5. What Greenfield actually gets.** Entitlement is one of three inputs. Whether
`procurement` is ON at a given branch still depends on its dependencies
resolving in that scope and on any `CapabilityOverride`. An entitled module with
`default_enabled = False` and no override is entitled and off.

**6. Basic says one branch.** Greenfield was created with a main branch. CX now
adds two more through `POST /v1/i/greenfield/branches/create/`. Nothing checks
`max_branch`; both are created. Greenfield is on a plan that sells one site and
has three (`school_code_issues` §3).

**7. Basic says three admins.** Greenfield's school admin invites five more
staff through `/v1/user/users/`. Nothing checks `admin_capacity`; all five are
created. The same is true of students and teachers - the numbers the operator
typed, the ceilings the plan advertises and the rows the platform actually holds
are three independent facts.

**8. A year later.** `subscription_expires_at` passes. Nothing happens: no sweep
reads it, no entitlement is revoked, no notification is sent. Greenfield keeps
every module it was granted.

**9. Upgrading to Standard.** There is no endpoint. `SchoolUpdateSerializer` does
not expose `package_setup_data`, so the plan on the row stays `basic` for ever
unless somebody edits the database. The *modules* can be changed - through
`vs_config`'s entitlement surfaces - but doing so leaves `SchoolPackageSetup`
pointing at Basic, and a module granted that way carries `source=MANUAL` and so
never appears in the school detail payload's `enabled_modules`
(§5 above).

## 8. Gotchas / known limitations

Recorded in full in **`error/schools/school_code_issues.md`**. The items
belonging to this slice:

| # in that file | One line |
|---|---|
| §3 | Every seat and branch limit the plan sells is validated against the operator's typing and enforced against nothing |
| §17 | The plan and module endpoints are gated on `IsVisionStaff` alone with no permission key; the school detail's `enabled_modules` is N+1 and silently omits manually granted modules; `PlanTier` and `Modules` are declared and unused |

Design choices worth stating as choices:

- **`package_plan` is addressed by `code`, not pk** (`serializers.py:199-203`) -
  more stable across environments and it is what the dropdown emits.
- **`enabled_modules` is addressed by capability `key`** for the same reason,
  and both querysets are narrowed to `is_active=True` so a retired plan or an
  archived module cannot be selected.
- **The closure is computed here rather than trusted from the client**
  (`serializers.py:1180-1183`): the wizard mirrors it, this loop guarantees it.
- **`set_entitlement` rather than direct row writes**
  (`serializers.py:1193-1195`) - `vs_config` owns the scope key and the audit,
  and writing rows here is precisely what let this path drift.
- **The read filters on `tenant_id` rather than accepting a NULL-tenant platform
  grant** (`serializers.py:270-272`), so one school cannot read another's
  package.

## 9. Permissions & tenant isolation

- **The two read endpoints have no `rbac_permission` at all.** They are gated by
  `IsAuthenticatedAndActive & IsVisionStaff` (`views/package.py:17`, `:29`).
  `IsVisionStaff` returns True for any account whose tenant kind is `PLATFORM`
  (`vs_rbac/permissions.py:233-244`) - so every CX account, from the newest
  support hire to the super admin, can read the plan catalogue and the module
  catalogue. Neither payload is sensitive, but the app's own branch and school
  routes all carry a key and these two do not.
- **The write path inherits `platform.schools.create`**, because it is a nested
  block on the school create endpoint. There is no separate "sell a package"
  key.
- **Nothing here is reachable by a school role.** The two reads require a
  platform tenant; the write requires a PLATFORM-scoped school key.
- **`CapabilityEntitlement` reads use `all_objects` with an explicit
  `tenant_id=`** (`serializers.py:273-277`). That is narrower than the ambient
  manager, not wider - the comment is explicit that filtering on the tenant
  rather than on a NULL-tenant platform grant is what keeps one school from
  reading another's package.
- **`PackagePlan` and `Capability` are global catalogues** with no tenant column,
  which is correct: they are the price sheet and the product, not customer data.

## 10. Code map

| File | What lives there |
|---|---|
| `schools/vs_schools/models.py:67-71` | `PlanTier` (unused) |
| `schools/vs_schools/models.py:74-82` | `Modules` (unused - see `vs_config.Capability`) |
| `schools/vs_schools/models.py:101-104` | `BillingCycle` |
| `schools/vs_schools/models.py:395-425` | `PackagePlan` |
| `schools/vs_schools/models.py:428-513` | `SchoolPackageSetup`, its `clean()` and its `full_clean()`-on-save |
| `schools/vs_schools/views/package.py` | Both read endpoints |
| `schools/vs_schools/serializers.py:135-180` | `PackagePlanSerializer`, `XVSModuleSerializer` |
| `schools/vs_schools/serializers.py:183-258` | `SchoolPackageSetupWriteSerializer` |
| `schools/vs_schools/serializers.py:261-296` | `SchoolPackageSetupReadSerializer` |
| `schools/vs_schools/serializers.py:1163-1204` | Step 5 of school creation: the setup row, the closure and the grants |
| `schools/vs_schools/management/commands/seed_package.py` | The four seeded plans |
| `vs_config/models.py:264-331` | `Capability` |
| `vs_config/models.py:334-…` | `CapabilityDependency` |
| `vs_config/services/capabilities.py` | `set_entitlement`, `effective_capability` |
| `vs_import_data/services/import_executor.py:311-347` | The importer's package block, with its own defaults (50 / 10 / 3) |

## 11. Test coverage & gaps

Module baseline: see `school_code_issues` for the exact `Ran N tests` line.

Covered for this slice:

- `tests_package_entitlements.py` (437 lines) - a package setup written at school
  creation, the capacity-versus-plan refusals, the dependency closure adding
  `finance` when `procurement` is picked, and that the grants go through
  `set_entitlement` with `source=PACKAGE`.
- `SchoolPackageSetup.clean()` is exercised indirectly through
  `full_clean()`-on-save.

Not covered:

- **No test creates a Basic school and then adds a second branch**, which is why
  the unenforced `max_branch` (§3) has never surfaced.
- No test invites more admins than `admin_capacity` (§3).
- No test grants a module manually and then reads the school detail to see
  whether it appears in `enabled_modules` (§17).
- No test lets `subscription_expires_at` pass and asserts anything about the
  result - there is nothing to assert, which is the finding.
- No test counts queries on the school detail with several granted modules
  (§17).
- The two read endpoints have no permission test, because they have no key to
  test.
