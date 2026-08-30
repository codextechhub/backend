# core_operations_and_mail

The sharp tools and the outbound mailbox. Four commands that destroy data,
one that fills a development database, one shared test client, and the single
function every outgoing email in the platform goes through. The commands that
*build* an environment are `core_bootstrap_seeds`.

Nothing here is reachable over HTTP. The gate on all of it is shell access.

---

## 1. What it is (and what it is NOT)

- **Four commands destroy data, and they are guarded differently.**
  `rebuild_database` needs two independent confirmations. `reset_db` is confined
  to approved development and test settings, requires an allowlisted database
  name, and always requires the resolved name to be typed. `delete_user` needs a
  typed `YES` or `--force`; `clear_permissions` needs a typed `yes` or `--yes`.
- **`send_email` is the only outbound mail path.** Every module calls it rather
  than Django's `send_mail`, which is what makes the BCC policy and the From-name
  policy apply uniformly.
- **BCC, never CC.** The monitoring addresses are ours, not the recipient's. A
  visible copy showed internal addresses to customers and vendors, told each
  recipient their mail is monitored, and gave reply-all a route into an internal
  inbox (`core/mail.py:36-41`).
- **The From address is configuration, the display name is per-call.**
  `build_from_email` takes the address from the runtime integration settings and
  swaps only the name (`core/mail.py:11-27`).
- **`seed_dev_data` is not a fixture and not a demo script for customers.** It
  fills a fresh dev database with a connected dataset across most modules, with
  every school user sharing one known password.
- **`TenantAPIClient` is a test utility, not a runtime thing** - but it is the
  one every module's tests should be using and several are not (§5).

## 2. Domain model

None. These are commands and helpers.

## 3. The destructive commands

| Command | What it does | Guards |
|---|---|---|
| `rebuild_database` | `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` | `--yes` **and** `RESET_DB=true` in the environment; PostgreSQL only |
| `reset_db` | drops every table, applies committed migrations, then runs five seed commands | approved dev/test settings, `DEBUG` or approved test settings, allowlisted database name, and exact typed-name confirmation; removed from Render artifacts |
| `delete_user` | hard-deletes users and every row they touched | a typed `YES`, or `--force` |
| `clear_permissions` | wipes the whole RBAC registry, preserving `PrebuiltRoleTemplate` | a typed `yes`, or `--yes`; `--dry-run` available |

### `rebuild_database` - the careful one

Written for the tenant-refactor cutover, and guarded twice on purpose: "so a
stray invocation in a build pipeline can never wipe an environment by accident"
(`rebuild_database.py:1-7`). It refuses without `--yes`, refuses without
`RESET_DB=true`, refuses on a non-PostgreSQL connection, and names the database
in its warning line before acting.

`build.sh` still carries a commented invocation of it inside a banner that says
to delete the block after the first deploy (§8).

### `reset_db` - the local reset

The command resolves the selected database alias before doing any work and
refuses unless all of these conditions hold:

1. `DJANGO_SETTINGS_MODULE` is `apps.settings.local`, `apps.settings.test`, or
   `apps.settings.ci`.
2. `DEBUG` is true, unless the settings module is an approved test module.
3. The resolved database `NAME` appears in `RESET_DB_ALLOWED_DATABASES`.
4. The operator types that exact resolved name. `--yes` cannot bypass this
   confirmation.

The warning prints the alias, resolved name, and host. After confirmation the
command can drop every table, apply the committed migration chain, and run seed
commands. Migration source deletion and `makemigrations` are no longer part of
the workflow. Render's build removes the command file from the deployed
artifact before Django discovers management commands.

The default seed list still includes `create_superuser` with no arguments,
which is the committed-credential path (`core_bootstrap_seeds` §4). The reset
guards confine that behavior to an explicitly disposable database.

### `delete_user`

Resolves addresses to accounts, refuses an address that exists at more than one
tenant unless `--tenant_id` says which, prints what it is about to delete with
tenant and status, then deletes each user in its own transaction and reports
per-model counts.

Its docstring header says "Local testing only." Its usage section says the
command "will run against whatever `DATABASE_URL` / DB env vars Render has
configured for that service - so it hits the live Render DB"
(`delete_user.py:40-42`). Both sentences are in the same docstring (§8).

### `clear_permissions`

Wipes `Permission`, `PermissionAction`, `PermissionModule`,
`PermissionResource`, `PermissionGroup`, `PermissionDependency`,
`GroupPermission`, `PrebuiltRolePermission`, `TenantRoleTemplate`,
`TenantUserRoleAssignment` and `TenantRoleChangeRequest` - preserving
`PrebuiltRoleTemplate` so the seeds can rebuild. `--dry-run` prints the counts
first, which is the right affordance and the only one of the four to have it.

## 4. `seed_dev_data`

One command that fills a fresh dev database with a connected dataset - schools,
branches, staff, students, parents - all ACTIVE with login passwords
(`seed_dev_data.py:17-19`). Every school user gets
`SCHOOL_USER_PASSWORD = "School@2025"` (`seed_dev_data.py:61`), and the command
prints it on completion so a developer can log in.

It deliberately excludes finance, procurement and payments, which have their own
demo seeds (`seed_finance_ar_demo`, `seed_procurement_demo`).

## 5. `send_email` and `TenantAPIClient`

### `send_email` (`core/mail.py:31`)

```python
send_email(subject=…, plain_message=…, html_message=None,
           recipient_list=[…], from_email=None, bcc=None,
           attachments=[(filename, bytes, mimetype), …])
```

- `from_email` defaults to `build_from_email()`.
- `bcc` defaults to `settings.EMAIL_BCC`, which is built from the `EMAIL_BCC`
  environment variable (falling back to `EMAIL_CC`) and is **empty by default**
  (`apps/settings/base.py:279-284`). Pass `[]` to send no copy at all.
- `html_message` is optional: with it the message is multipart, without it
  plain-text only.

Two module-specific BCC lists exist beside the platform-wide one -
`PROCUREMENT_VENDOR_EMAIL_BCC` and `FINANCE_CUSTOMER_EMAIL_BCC`
(`apps/settings/base.py:286-302`) - so vendor and customer mail can be copied to
the owning team's mailbox rather than the platform-wide one.

`build_from_email` reads `vs_config.runtime_settings.get_integration_settings()`
on **every call**, so the sender name and address are runtime-configurable
without a deploy - at the cost of a settings lookup per email.

### `TenantAPIClient` (`core/test_utils.py:14`)

An `APIClient` that mints a real JWT for the user and appends
`?tenant=<slug>` to every request, so a test exercises the same code path
production traffic takes - `TenantJWTAuthentication`, `request.tenant`, the
ambient tenant context and RBAC.

Its docstring is explicit that it should be used **instead of
`force_authenticate`** for any endpoint that reads `request.tenant`. Several
module suites still use `force_authenticate` throughout, which is why their
tenant-parameter paths are untested - recorded in
`error/tickets/ticket_code_issues.md` and `error/todo/todo_code_issues.md`.

It handles the awkward part correctly: `GET`/`HEAD` encode `data` into the query
string, which would override a path-appended parameter, so the tenant is
injected into `data` when `data` is used and into the path otherwise
(`test_utils.py:33-42`).

## 6. What they write

| Command | Writes |
|---|---|
| `rebuild_database` | drops and recreates the `public` schema |
| `reset_db` | drops tables, applies committed migrations, runs seeds |
| `delete_user` | deletes user rows and their dependent rows, per-user transaction |
| `clear_permissions` | deletes eleven RBAC tables |
| `seed_dev_data` | tenants, branches, users, students, parents and their links |
| `send_email` | nothing local - an SMTP send |

**None of the four destructive commands writes an audit event.** A wiped
permission registry and a hard-deleted user leave no trace beyond the console
output of whoever ran it.

## 7. Worked example

Bringing a local database back to zero, the safe way:

```text
$ RESET_DB=true python manage.py rebuild_database --yes
Dropping and recreating schema 'public' on database 'cx_db'…
Schema rebuilt. Run `migrate` and the seed commands next.

$ python manage.py migrate
$ python manage.py seed_all_permissions
$ python manage.py create_superuser --email me@codexng.com --password '…'
$ python manage.py seed_dev_data
  …
  School-user password: School@2025
```

The guarded equivalent through `reset_db`:

```text
$ python manage.py reset_db --yes
Database alias: default
Database name: cx_db
Database host: localhost
Type the resolved database name 'cx_db' to continue: cx_db
```

`--yes` skips the later step prompts, not the target-name confirmation. A shell
still pointing at `xvs_staging` is refused because that resolved name is not in
the development allowlist. The staging settings module is refused before the
connection is used.

An email, from anywhere in the platform:

```python
send_email(
    subject="Your XVS account is ready",
    plain_message=text,
    html_message=html,
    recipient_list=["ngozi@brightstar.test"],
)
```

From: `CodeX System <system@codexng.com>` (name and address from runtime
settings), BCC: whatever `EMAIL_BCC` holds - nothing, unless the environment sets
it.

## 8. Gotchas / known limitations

Full evidence in **`error/core/core_code_issues.md`**.

- **`reset_db` remains intentionally destructive in development.** The target
  confirmation prevents confusion; it does not create a backup. Use it only on
  a disposable database whose name has been explicitly allowlisted.
- **`delete_user`'s docstring contradicts itself** - "Local testing only" in the
  header, and instructions for running it against the live Render database in
  the usage section (`core_code_issues.md` §17).
- **`build.sh` still carries the commented one-time `rebuild_database` block**,
  inside a banner that says to delete it after the first deploy. Uncommenting one
  line wipes the database on every deploy (`core_code_issues.md` §18).
- **`seed_dev_data` sets one known password for every school user** and prints
  it. Fine for a development database; nothing stops it running elsewhere.
- **None of the destructive commands is audited.**
- **`build_from_email` hits the settings service per email.**
- **Justified by design:** `rebuild_database`'s two independent guards.
- **Justified by design:** `clear_permissions` preserving `PrebuiltRoleTemplate`
  so the seeds can rebuild, and offering `--dry-run`.
- **Justified by design:** BCC rather than CC, with per-module lists where the
  audience differs.
- **Justified by design:** `TenantAPIClient` going through the real auth layer.

## 9. Permissions & tenant isolation

Neither applies: these are shell commands. Shell access remains the first
boundary. `reset_db` adds environment, settings, target-name, and deployment
boundaries because shell access alone was not enough for a database-wide wipe.

`delete_user` and `clear_permissions` cross tenant boundaries by nature -
`delete_user` refuses an ambiguous address until you name the tenant, which is
the one place tenancy is respected here.

`send_email` has no tenant concept at all: the caller supplies the recipients,
and the platform-wide BCC (when configured) receives every tenant's mail in one
mailbox.

## 10. Code map

| File | Responsibility |
|---|---|
| `core/management/commands/rebuild_database.py` | the guarded schema drop |
| `core/management/commands/reset_db.py` | the guarded local database reset |
| `core/management/commands/delete_user.py` | hard user deletion with per-model counts |
| `core/management/commands/clear_permissions.py` | the RBAC wipe, with `--dry-run` |
| `core/management/commands/seed_dev_data.py` | the connected dev dataset |
| `core/mail.py:11-27` | `build_from_email` |
| `core/mail.py:31-71` | `send_email` |
| `core/test_utils.py` | `TenantAPIClient` |
| `apps/settings/base.py:279-302` | the three BCC lists |
| `build.sh` | the deploy sequence, including the commented rebuild block |

## 11. Test coverage & gaps

`core.test_reset_db_command` covers the settings refusal, the `DEBUG=False`
refusal, the CI test exception, the database-name allowlist, the mandatory exact
typed confirmation under `--yes`, resolved name and host output, connection
error preservation, and Render artifact exclusion. It mocks the destructive
operations rather than dropping a real schema.

There is still no test for `rebuild_database`, `delete_user`,
`clear_permissions`, `seed_dev_data`, `send_email`, `build_from_email`, or
`TenantAPIClient` itself.

That is defensible for the destructive commands - a test that exercises
`DROP SCHEMA` is its own hazard - but three of the gaps are worth closing:

1. **`send_email`'s BCC default.** Nothing asserts that `bcc=None` picks up
   `settings.EMAIL_BCC` while `bcc=[]` sends no copy. The distinction is the
   whole of the policy, and it is one keyword argument away from inverting.
2. **`build_from_email`'s fallback chain** - supplied name, then the configured
   name, then `'CodeX System'`.
3. **`TenantAPIClient`'s query-string handling** - that a `GET` with `data` still
   carries the tenant, which is the branch its own comment says was a bug once.

`delete_user`'s guards are also worth a test each: the ambiguous-address refusal
and the typed-`YES` requirement are the two things standing between a mistyped
command and a deleted account.
