#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

# One-time migration reset guard.
# Detects whether the DB still holds old migration names that no longer exist
# on disk (from a migration consolidation). If so, resets the migration history
# so `migrate` below can run cleanly. Safe to leave here permanently — it
# does nothing once the DB is already in sync.
python manage.py shell -c "
from django.db import connection
import datetime

CUSTOM_APPS = [
    'vs_admin_console', 'vs_audit', 'vs_config', 'vs_import_data',
    'vs_notifications', 'vs_rbac', 'vs_schools', 'vs_user',
]

with connection.cursor() as c:
    # Count migration records that don't match any file we ship (old names).
    # After consolidation each app has at most 2 migration files.
    c.execute(
        'SELECT COUNT(*) FROM django_migrations WHERE app IN %s',
        [tuple(CUSTOM_APPS)]
    )
    total = c.fetchone()[0]

    # We ship exactly 2 migrations per app (16 total). More means old records exist.
    needs_reset = total > 16

    if not needs_reset:
        print('Migration state is already in sync — skipping reset.')
    else:
        print(f'Found {total} migration records for custom apps (expected <=16). Running reset...')

        now = datetime.datetime.now()

        for app in CUSTOM_APPS:
            c.execute('DELETE FROM django_migrations WHERE app = %s', [app])

        # contenttypes.0002 is a data migration that reads a dropped column.
        # Insert both records directly to avoid Django trying to run it.
        for name in ['0001_initial', '0002_remove_content_type_name']:
            c.execute(
                'INSERT IGNORE INTO django_migrations (app, name, applied) VALUES (%s, %s, %s)',
                ['contenttypes', name, now]
            )

        print('Migration history cleared. Running fake-initial next.')
"

# Determine migrate strategy:
#  - After a migration reset: internal apps have no records → fake them, then fake-initial our apps
#  - Fresh DB (no tables yet): run normal migrate to create everything
#  - Normal deploy: run normal migrate (applies any new migrations)
DB_STATE=$(python manage.py shell -c "
from django.db import connection
try:
    with connection.cursor() as c:
        # Check if our tables exist at all (fresh DB vs existing DB after reset)
        c.execute(\"SHOW TABLES LIKE 'vs_user_user'\")
        has_tables = c.fetchone() is not None
        if not has_tables:
            print('fresh')
        else:
            c.execute(\"SELECT COUNT(*) FROM django_migrations WHERE app = 'auth'\")
            print('no_auth' if c.fetchone()[0] == 0 else 'normal')
except Exception:
    print('fresh')
" 2>/dev/null)

if [ "$DB_STATE" = "fresh" ]; then
    echo "Fresh database — running full migrate..."
    python manage.py migrate
elif [ "$DB_STATE" = "no_auth" ]; then
    echo "Post-reset state — faking internal migrations then fake-initial..."
    python manage.py migrate auth --fake
    python manage.py migrate admin --fake
    python manage.py migrate sessions --fake
    python manage.py migrate token_blacklist --fake
    python manage.py migrate --fake-initial
else
    python manage.py migrate
fi

# Run seeding commands AFTER migrate succeeds
# python manage.py clear_permissions --yes
# python manage.py reset_db --yes
# python manage.py create_superuser --assign-role --email admin@codexng.com
