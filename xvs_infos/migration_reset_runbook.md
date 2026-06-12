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

1. **Back up the staging database first.** Render → the Postgres instance →
   create a manual backup (or `pg_dump` via the external connection string).

2. Deploy the commit containing the new migrations to staging as usual
   (`./deploy-staging.sh`). **The build will fail at the migrate step — that
   is expected.** (Or temporarily comment the `migrate` line out of build.sh
   for this one deploy.)

3. Open a shell on the staging service (Render → Shell tab) and run:

   ```bash
   cd apps
   python manage.py dbshell -c "TRUNCATE django_migrations;"
   python manage.py migrate --fake
   ```

   `--fake` records every new migration as applied WITHOUT executing any SQL.
   This includes the two finance seeds — correct, because staging already has
   the currencies and the CODEX entity in its data.

4. Verify:

   ```bash
   python manage.py migrate          # → "No migrations to apply."
   python manage.py makemigrations --check --dry-run   # → "No changes detected"
   ```

5. Re-deploy (or restore the migrate line in build.sh). From now on,
   migrations behave completely normally on staging.

## Colleagues' local databases

Easiest: `./reseed-dev.sh` (fresh rebuild). To keep existing local data
instead, run the same truncate + `migrate --fake` procedure locally.

## Why the schema is guaranteed identical

`makemigrations --check` is clean against the new chain, and the full test
suite (520 tests) passed on a database built from scratch with it. The new
initials are generated from the same models the old chain converged to.
