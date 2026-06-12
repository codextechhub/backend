#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

# ── TEMPORARY (migration reset adoption, 2026-06-12) ────────────────────────
# One-time: wipe the old chain's bookkeeping and record the new chain as
# applied WITHOUT executing SQL (schema already matches — verified by CI).
# REVERT this block to a plain `python manage.py migrate` immediately after
# the first successful deploy.
python manage.py shell -c "from django.db import connection; cur = connection.cursor(); cur.execute('TRUNCATE django_migrations'); print('django_migrations truncated')"
python manage.py migrate --fake
# ─────────────────────────────────────────────────────────────────────────────

# Run seeding commands AFTER migrate succeeds
# python manage.py clear_permissions --yes
# python manage.py reset_db --yes
# python manage.py create_superuser --assign-role --email admin@codexng.com
