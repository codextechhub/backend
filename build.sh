#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

# Migration state guard.
#
# Three possible states:
#   fresh      — no tables yet (brand-new DB); run normal migrate
#   needs_fake — tables exist but migration records are missing or stale
#                (migration consolidation, or a previous partially-failed deploy)
#   normal     — everything is in sync; run normal migrate
#
# "needs_fake" triggers when:
#   (a) old migration names exist (count > 16), OR
#   (b) tables exist but vs_user.0001_initial is not recorded
#       (catches a partially-completed reset from a prior failed deploy)
#
# Safe to leave permanently — it is a no-op once the DB is in sync.

DB_STATE=$(python manage.py shell -c "
from django.db import connection

CUSTOM_APPS = [
    'vs_admin_console', 'vs_audit', 'vs_config', 'vs_import_data',
    'vs_notifications', 'vs_rbac', 'vs_schools', 'vs_user',
]

with connection.cursor() as c:
    # 1. Check whether our tables exist (fresh DB?)
    c.execute(
        \"SELECT 1 FROM information_schema.tables WHERE table_name = 'vs_user_user' LIMIT 1\"
    )
    has_tables = c.fetchone() is not None

    if not has_tables:
        print('fresh')
    else:
        # 2. Check for stale or missing migration records
        c.execute(
            'SELECT COUNT(*) FROM django_migrations WHERE app = ANY(%s)',
            [CUSTOM_APPS]
        )
        total = c.fetchone()[0]

        c.execute(
            \"SELECT 1 FROM django_migrations WHERE app = 'vs_user' AND name = '0001_initial' LIMIT 1\"
        )
        has_vs_user = c.fetchone() is not None

        if total > 16 or not has_vs_user:
            print('needs_fake')
        else:
            print('normal')
" 2>/dev/null)

echo "Migration state: $DB_STATE"

if [ "$DB_STATE" = "fresh" ]; then
    echo "Fresh database — running full migrate..."
    python manage.py migrate

elif [ "$DB_STATE" = "needs_fake" ]; then
    echo "Resetting migration history and faking all migrations..."

    python manage.py shell -c "
from django.db import connection
import datetime

CUSTOM_APPS = [
    'vs_admin_console', 'vs_audit', 'vs_config', 'vs_import_data',
    'vs_notifications', 'vs_rbac', 'vs_schools', 'vs_user',
]
now = datetime.datetime.now()

with connection.cursor() as c:
    for app in CUSTOM_APPS:
        c.execute('DELETE FROM django_migrations WHERE app = %s', [app])
        print(f'  Cleared: {app}')

    # contenttypes.0002 is a data migration that reads a column already dropped.
    # Insert records directly to avoid Django trying to execute it.
    for name in ['0001_initial', '0002_remove_content_type_name']:
        c.execute(
            'SELECT 1 FROM django_migrations WHERE app = %s AND name = %s',
            ['contenttypes', name]
        )
        if not c.fetchone():
            c.execute(
                'INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, %s)',
                ['contenttypes', name, now]
            )
    print('  contenttypes entries ensured.')
"

    python manage.py migrate auth --fake
    python manage.py migrate admin --fake
    python manage.py migrate sessions --fake
    python manage.py migrate token_blacklist --fake
    python manage.py migrate --fake-initial

else
    echo "Normal deploy — running migrate..."
    python manage.py migrate
fi

# Run seeding commands AFTER migrate succeeds
# python manage.py clear_permissions --yes
# python manage.py reset_db --yes
# python manage.py create_superuser --assign-role --email admin@codexng.com
