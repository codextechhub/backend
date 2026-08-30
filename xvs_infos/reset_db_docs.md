# Database reset command

`reset_db` rebuilds an explicitly disposable development or test database from
the repository's committed migrations, then runs seed commands. It is never a
production recovery or maintenance command.

## Safety contract

The command stops before any destructive action unless every check passes:

1. `DJANGO_SETTINGS_MODULE` is one of `apps.settings.local`,
   `apps.settings.test`, or `apps.settings.ci`.
2. `DEBUG` is true, unless the active module is the approved test or CI module.
3. The resolved database `NAME` appears in `RESET_DB_ALLOWED_DATABASES`.
4. The operator types the resolved database name exactly.

`--yes` does not bypass these checks or the exact-name confirmation. It skips
only the later prompts for dropping tables, applying migrations, and running
seed commands.

Render's build removes `core/management/commands/reset_db.py` from the deployed
artifact before Django discovers management commands. Runtime guards remain in
the source command for local checkouts and other packaging paths.

## Configure a disposable database

Local and test settings allow only `cx_db` by default. To use a different
disposable name, add it explicitly:

```bash
export DB_NAME=cx_dev_ada
export RESET_DB_ALLOWED_DATABASES=cx_db,cx_dev_ada
```

Do not derive the allowlist automatically from `DB_NAME`. The independent list
is what catches a shell whose database variables still point at live data.

CI permits `cx_ci`.

## Basic use

From the `apps` directory:

```text
$ python manage.py reset_db --settings=apps.settings.local
Database alias: default
Database name: cx_db
Database host: localhost
Type the resolved database name 'cx_db' to continue: cx_db
```

The command then asks separately before it drops tables, applies migrations,
and runs seed commands.

To skip those later prompts:

```bash
python manage.py reset_db --yes --settings=apps.settings.local
```

The resolved-name prompt still appears and still requires `cx_db` exactly.
Typing `default`, `yes`, or any other value aborts the command.

## Options

| Option | Default | Effect |
|---|---|---|
| `--database` | `default` | Selects a Django database alias. Safety checks use the alias's resolved name and host. |
| `--skip-drop` | false | Leaves existing tables in place. |
| `--skip-migrate` | false | Does not apply committed migrations. |
| `--yes` | false | Skips step prompts after the mandatory exact-name confirmation. |
| `--post-commands` | five standard seeds | Replaces the commands run after migration. |

The default post-command list is:

1. `seed_actions`
2. `seed_all_permissions`
3. `create_superuser`
4. `seed_package`
5. `seed_config_catalogue`

## What the reset does

1. Django resolves the selected alias to a physical database name and host.
2. The environment, `DEBUG`, allowlist, and exact-name checks run.
3. The command introspects the connected database and drops every returned
   table. PostgreSQL uses `CASCADE`; MySQL temporarily disables foreign-key
   checks; SQLite drops each table directly.
4. Django applies the committed migration chain with `migrate`.
5. The selected seed commands run in order.

The command does not delete migration files and does not run `makemigrations`.
Migration history is source code and is not part of a database reset.

## Refusal examples

Staging settings are rejected even if `DEBUG` is accidentally enabled:

```text
CommandError: Refusing to run outside the development and test settings modules.
```

An unapproved target is rejected before a confirmation prompt:

```text
CommandError: Database 'xvs_staging' is not allowlisted for reset.
```

A target-name mismatch aborts the operation:

```text
Type the resolved database name 'cx_db' to continue: default
CommandError: Database-name confirmation did not match. Aborting.
```

## Operational warning

Passing every guard means the database is approved for destruction. It does not
create a backup. Confirm that the target is disposable before adding its name to
`RESET_DB_ALLOWED_DATABASES`.
