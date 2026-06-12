# Migration Reset — Staging Runbook (one-time, 2026-06-12)

All apps' migrations were squashed to fresh initials (77 files → 26).
The two finance data seeds survive as `vs_finance/migrations/0003_seed_currencies`
and `0004_seed_platform_entity`. Everything else with RunPython was legacy-DB
repair (MySQL-era fixes, the B23 PK flip, the rbac dedupe) that a fresh chain
no longer needs.

## Why staging needs a one-time manual step

Staging's `django_migrations` table records the OLD migration names. Deploying
the new chain and running `migrate` normally would try to apply `0001_initial`
on top of existing tables and fail. The fix: tell Django the new chain is
already applied — the schema is identical, only the bookkeeping changes.

## Steps (run on staging, IN THIS ORDER)

⚠️ Do NOT use the Render Shell for the adoption: the shell runs the
PREVIOUSLY-deployed build, which still carries the OLD migration files —
faking there records the wrong names (we learned this the hard way). The
adoption must execute with the NEW code, and the only place that's guaranteed
pre-deploy is the build itself.

1. **Back up the staging database first.** Render → the Postgres instance →
   create a manual backup (or `pg_dump` via the external connection string).

2. **Temporary commit:** in build.sh, replace `python manage.py migrate` with:

   ```bash
   python manage.py shell -c "from django.db import connection; cur = connection.cursor(); cur.execute('TRUNCATE django_migrations'); print('django_migrations truncated')"
   python manage.py migrate --fake
   ```

   `--fake` records every new migration as applied WITHOUT executing any SQL.
   That includes the data-seed migrations — correct, because staging already
   holds the currencies and the CODEX entity.

3. Deploy (`./deploy-staging.sh`). The build log shows the truncate line and
   the full FAKED list; the deploy goes green on the new code.

4. **Revert build.sh to the plain `migrate` immediately** (leaving the
   truncate in would re-fake on every deploy) and deploy once more. That
   build's migrate prints "No migrations to apply." — done forever.

## Colleagues' local databases

Easiest: `./reseed-dev.sh` (fresh rebuild). To keep existing local data
instead, run the same truncate + `migrate --fake` procedure locally.

## Why the schema is guaranteed identical

`makemigrations --check` is clean against the new chain, and the full test
suite (520 tests) passed on a database built from scratch with it. The new
initials are generated from the same models the old chain converged to.
