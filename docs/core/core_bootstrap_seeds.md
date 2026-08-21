# core_bootstrap_seeds

How an empty database becomes a working platform. One master command,
`seed_all_permissions`, chains eighteen module seeds in dependency order;
`create_superuser` mints the first account; a handful of reference-data seeds
fill the catalogues the product needs to function. The destructive and
development-only commands are `core_operations_and_mail`.

`core` owns the master command and five of the seeds; the other thirteen live in
the module they belong to and are invoked from here.

---

## 1. What it is (and what it is NOT)

- **`seed_all_permissions` is the platform bootstrap, and it is idempotent.**
  Every step uses `get_or_create` or `update_or_create`, so it is safe to run on
  every deploy - and `build.sh` does exactly that (`build.sh`, after `migrate`).
- **Order is dependency order, not alphabetical.** Verbs before keys, prebuilt
  roles before defaults, and `seed_school_permission_groups` last because it
  spans five modules and can only group keys already registered
  (`seed_all_permissions.py:42-46,76-78`).
- **It seeds permissions and *required* reference data**, not demo data.
  `seed_import` is in the chain with a comment explaining why: a migrated
  environment must not expose working import endpoints backed by an empty
  template catalogue (`seed_all_permissions.py:61-64`).
- **It does not create any user.** Platform roles are created by the seeds
  themselves; the first *account* comes from `create_superuser`, which is a
  separate command and is **commented out in `build.sh`**.
- **It does not seed notification events or templates.** Those are three separate
  commands run by `build.sh` afterwards, and an environment that skips them has
  a working notification permission set over an empty event registry (§5).
- **The last step grants the super admin everything.**
  `_ensure_super_admin_has_every_permission` writes an explicit
  `TenantRolePermission` row for every active key
  (`seed_all_permissions.py:122-164`) - not because the evaluator needs it (the
  role has a runtime bypass) but because the console uses the effective key list
  for navigation and action visibility.
- **It is not safe to run on a Windows console.** The banner and the summary
  line contain characters cp1252 cannot encode, and the command dies on the last
  step (§8). This is what makes `core`'s own suite red.

## 2. Domain model

None of its own. The commands write `vs_rbac` rows -
`PermissionAction`, `PermissionModule`, `PermissionResource`, `Permission`,
`PrebuiltRoleTemplate`, `PrebuiltRolePermission`, `TenantRoleTemplate`,
`TenantRolePermission`, `PermissionGroup` - plus `vs_import_data.ImportTemplate`
and `vs_tenants.Tenant`.

## 3. The chain

`SEED_STEPS` (`seed_all_permissions.py:54-79`), eighteen entries:

| # | Command | Owned by | Registers |
|---|---|---|---|
| 1 | `seed_actions` | core | the global `PermissionAction` verb vocabulary |
| 2 | `seed_prebuilt_role_templates` | core | `school_admin`, `branch_admin`, `teacher` |
| 3 | `seed_school_permissions` | core | school + academics modules, prebuilt defaults, backfill |
| 4 | `seed_platform_permissions` | core | the `platform` module → both platform roles |
| 5 | `seed_import_permissions` | core | import keys; all → super admin, templates → platform admin |
| 6 | `seed_import` | core | the canonical bulk-upload templates |
| 7 | `seed_workflow_permissions` | vs_workflow | |
| 8 | `seed_config_permissions` | vs_config | |
| 9 | `seed_finance_permissions` | vs_finance | |
| 10 | `seed_procurement_permissions` | vs_procurement | |
| 11 | `seed_payments_permissions` | vs_payments | |
| 12 | `seed_exports_permissions` | vs_exports | |
| 13 | `seed_todo_permissions` | vs_todo | |
| 14 | `seed_ticket_permissions` | vs_tickets | platform **and** school roles |
| 15 | `seed_notification_permissions` | vs_notifications | platform + school admin defaults |
| 16 | `seed_onboarding_permissions` | vs_onboarding | |
| 17 | `seed_health` | vs_health | |
| 18 | `seed_school_permission_groups` | core | groups the school keys; grants nothing |

Then `_ensure_super_admin_has_every_permission`.

`--dry-run` prints the chain without executing. A step that raises aborts the
whole command (`seed_all_permissions.py:110-114`) - there is no
continue-on-error, deliberately, because a half-seeded registry is worse than a
failed run.

`_check_platform_roles` (`seed_all_permissions.py:166-188`) runs **first** and
only warns: if `xvs_super_admin` or `xvs_platform_admin` is missing it prints
"Run: python manage.py create_superuser" and carries on, because most seeds
create the role they need anyway.

## 4. The two account commands

### `create_superuser` (`core/management/commands/create_superuser.py`)

Mints the first platform staff account and gives it the Vision Super Admin role.
Three modes: non-interactive (flags), `--interactive` (prompts), and
`--assign-role` (attach the role to an existing account, with `--tenant_id` to
disambiguate an address that exists at more than one tenant).

Two guards: it refuses when any platform-tenant user already exists unless
`--force`, and it refuses a duplicate address **within the codex tenant** -
not platform-wide, because a CX staff member may also be a parent at a school
that uses the product.

Its defaults are the finding in §8: `admin@codexng.com` / `Admin@123456`,
hardcoded at `create_superuser.py:90-91`, with the environment-variable version
present but commented out directly beneath.

### `seed_vision_staff`

Creates or updates a fixed roster of CX staff accounts, setting each one's
password to `--password` (default `Vision@2025`,
`seed_vision_staff.py:53`). Development convenience, and not in the master
chain.

## 5. What a real environment actually runs

`build.sh`, in order:

```bash
python manage.py collectstatic --no-input
# (a commented-out one-time `rebuild_database` block)
python manage.py migrate
python manage.py seed_all_permissions
# python manage.py seed_import            ← commented; step 6 covers it
python manage.py seed_notification_event_types
python manage.py seed_notification_templates
python manage.py seed_notification_settings
python manage.py seed_config_catalogue
python manage.py seed_package
# python manage.py create_superuser \     ← commented out entirely
```

So the deployed sequence is: migrate, permissions, notification registry,
capability catalogue, package plans - and **no account**. The comment above the
commented block says the command "self-skips (exits cleanly) once a
platform-tenant staff account exists, so it is safe to leave in permanently",
which is true, and it is commented out anyway (§8).

The notification seeds being outside the master chain matters: an operator who
runs `seed_all_permissions` by hand on a fresh database gets
`communication.*` permissions and an **empty** `NotificationEventType` table, so
every dispatch in the platform raises `UnknownEventTypeError`. Most callers
swallow it - `vs_todo`'s review request returns
`{"skipped": "event-not-seeded"}` (`vs_todo/tasks.py:89-95`), `core`'s own job
notification logs a warning - so the symptom is silence, not an error.

## 6. What the seeds write

| Command | Writes |
|---|---|
| `seed_actions` | `PermissionAction` rows - the verb vocabulary every key composes from |
| `seed_prebuilt_role_templates` | three `PrebuiltRoleTemplate` rows, **no permissions attached** |
| `seed_school_permissions` | the school/academics keys, prebuilt defaults, and a backfill onto existing tenant role templates |
| `seed_platform_permissions` | the `platform` module and its grants to both platform roles |
| `seed_import_permissions` + `seed_import` | import keys and the canonical templates |
| `seed_school_permission_groups` | named bundles over already-registered keys; grants nothing |
| `_ensure_super_admin…` | one `TenantRolePermission` per active key on `xvs_super_admin`, flipping any `granted=False` back to true |

Nothing writes an audit event. A permission registry change made by a seed is
indistinguishable, afterwards, from one made by hand.

## 7. Worked example

A fresh database, brought up by hand:

```text
$ python manage.py migrate
$ python manage.py seed_all_permissions

  ╔══════════════════════════════════════╗
  ║      seed_all_permissions            ║
  ╚══════════════════════════════════════╝

  ⚠  Platform role(s) not found: xvs_super_admin, xvs_platform_admin
     Permission grants will be skipped for missing roles.
     Run: python manage.py create_superuser

  [1/18] seed_actions
  ─────────────────────────────────────────
  ...
  [18/18] seed_school_permission_groups
  ─────────────────────────────────────────
  ...
  ✔ Super Admin reconciled with all 412 active permissions.
  ✔ All permission seeds completed successfully.
```

On Windows, the last two lines never print: `✔` is U+2714, the console stream is
cp1252, and `self.stdout.write` raises `UnicodeEncodeError`
(`seed_all_permissions.py:163`). Every one of the eighteen steps has already
committed; the reconciliation is `@transaction.atomic` so its own rows roll back;
and the command exits non-zero. The operator sees a traceback at the end of a
run that mostly worked (`core_code_issues.md` §11).

Then the account:

```text
$ python manage.py create_superuser
  ══════════════════════════════════════════════════════════
    CodeX Vision - Superuser Creation
  ══════════════════════════════════════════════════════════
  Email:      admin@codexng.com
  Name:       System Administrator
  Password:   ************
```

Twelve asterisks, and the password is `Admin@123456` - in the repository, at
`create_superuser.py:91` (`core_code_issues.md` §12).

## 8. Gotchas / known limitations

Full evidence in **`error/core/core_code_issues.md`**.

- **`create_superuser` has a committed default password**, and the env-var
  version that would fix it is commented out two lines below
  (`core_code_issues.md` §12).
- **`seed_all_permissions` crashes on a Windows console** and takes `core`'s own
  test suite red with it (`core_code_issues.md` §11).
- **`create_superuser` is commented out of `build.sh`**, so a fresh deployment
  has roles and no account (`core_code_issues.md` §13).
- **The notification registry is seeded outside the master chain**, so a
  hand-bootstrapped environment dispatches nothing and says nothing
  (`core_code_issues.md` §14).
- **The docstring and the code disagree about the chain.** The module docstring
  numbers fourteen steps and omits `seed_health`; `SEED_STEPS` has eighteen
  entries and the progress line prints `[i/18]`
  (`core_code_issues.md` §15).
- **`seed_vision_staff` sets a fixed password** for a roster of real staff
  addresses (`seed_vision_staff.py:53`), with no environment override.
- **Seeds write no audit trail**, so a permission registry change has no
  provenance.
- **`_check_platform_roles` swallows every exception** (`:187-188`), so a
  database error during the check is indistinguishable from the roles existing.
- **Justified by design:** the chain aborts on the first failure rather than
  continuing into a half-seeded registry.
- **Justified by design:** `seed_school_permission_groups` runs last, and
  `seed_import` is in the chain as required reference data.
- **Justified by design:** the super admin gets explicit rows despite the
  runtime bypass, because the console reads the effective key list.

## 9. Permissions & tenant isolation

These are commands, not endpoints: the gate is shell access to the environment.
Anybody who can run `manage.py` can grant any permission to any role, create a
Vision Super Admin, or reset a password.

Within what they write, two scoping decisions matter:

1. **Platform grants target the `codex` tenant explicitly.** Every seed looks up
   `Tenant.objects.filter(slug="codex", kind=PLATFORM)` and warns-and-skips if it
   is absent, rather than falling back to any tenant.
2. **School defaults go to `PrebuiltRoleTemplate`, not to a tenant's roles**, and
   are then backfilled onto existing tenant role templates by key
   (`seed_school_permissions`). A tenant that has edited its own role keeps its
   explicit denies - `test_explicit_deny_not_overwritten`
   (`test_seed_school_permissions.py:222`) pins that.

## 10. Code map

| File | Responsibility |
|---|---|
| `core/management/commands/seed_all_permissions.py:54-79` | `SEED_STEPS` |
| `…/seed_all_permissions.py:92-120` | `handle` - the chain, `--dry-run` |
| `…/seed_all_permissions.py:122-164` | `_ensure_super_admin_has_every_permission` |
| `…/seed_all_permissions.py:166-188` | `_check_platform_roles` |
| `…/seed_actions.py` | the verb vocabulary |
| `…/seed_prebuilt_role_templates.py` | the three school prebuilts |
| `…/seed_school_permissions.py` | school + academics keys, defaults, backfill |
| `…/seed_school_permission_groups.py` | the named bundles |
| `…/seed_platform_permissions.py` | the `platform` module |
| `…/seed_import_permissions.py`, `…/seed_import.py` | import keys and templates |
| `…/create_superuser.py` | the first account, and `--assign-role` |
| `…/seed_vision_staff.py` | the CX roster |
| `…/seed_consultant_role.py` | the view-only platform role |
| `build.sh` | what a deploy actually runs |

## 11. Test coverage & gaps

The seeds are the best-tested part of `core`, by some distance:

- `test_seed_school_permissions.py` (319 lines) - key creation and total count,
  `CRITICAL`/restricted flags on impersonation and override keys, sensitivity
  levels, idempotency, the three prebuilt default sets, and four backfill cases
  including that an explicit deny is not overwritten and a non-system role with a
  prebuilt-like key is not backfilled.
- `test_seed_school_permission_groups.py` (252 lines).
- `test_seed_import_permissions.py` (168 lines) - the templates are seeded, the
  master seed includes them, super admin gets all import keys while platform
  admin gets only template management, legacy excess grants are removed, and the
  seeded groups are tenant-scoped.
- `test_seed_all_permissions.py` - the super-admin reconciliation, asserted not
  to expand platform admin.

What they do not cover:

1. **`create_superuser`, entirely.** Not the duplicate-address guard, not the
   `--force` path, not `--assign-role`, and not the default credentials.
2. **The chain itself.** No test asserts that `SEED_STEPS` runs in dependency
   order, or that a failure aborts rather than continuing.
3. **A run on a stream that cannot encode the banner** - the failure that is
   currently red.
4. **`seed_actions`, `seed_prebuilt_role_templates`, `seed_vision_staff`,
   `seed_consultant_role`** - none has a test of its own.
5. **The notification-registry gap** - that a `seed_all_permissions`-only
   environment cannot dispatch a notification.
6. **`--dry-run`.**
