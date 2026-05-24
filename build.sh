#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

# Migration state guard.
#
# Detects three states and acts accordingly:
#   fresh      — django_migrations table is empty or missing (brand-new DB)
#   needs_fake — tables exist but migration records are stale or incomplete
#   normal     — everything is in sync
#
# The grep at the end strips Django's auto-import noise from the captured output
# so only our marker line reaches the bash variable.

DB_STATE=$(python manage.py shell -c "
from django.db import connection

CUSTOM_APPS = [
    'vs_admin_console', 'vs_audit', 'vs_config', 'vs_import_data',
    'vs_notifications', 'vs_rbac', 'vs_schools', 'vs_user',
]

try:
    with connection.cursor() as c:

        # How many rows are in django_migrations at all?
        c.execute('SELECT COUNT(*) FROM django_migrations')
        total_rows = c.fetchone()[0]

        if total_rows == 0:
            # Table exists but is empty — could be a fresh DB or a fully-cleared one.
            # Check whether the vs_user_user table itself exists.
            try:
                c.execute('SELECT 1 FROM vs_user_user LIMIT 1')
                # Table exists → records were cleared by a prior deploy; need fake.
                print('needs_fake')
            except Exception:
                # Table does not exist → genuinely fresh DB.
                print('fresh')
        else:
            # Records exist. Check whether they are stale or inconsistent.
            c.execute(
                'SELECT COUNT(*) FROM django_migrations WHERE app = ANY(%s)',
                [CUSTOM_APPS]
            )
            custom_total = c.fetchone()[0]

            c.execute(
                \"SELECT 1 FROM django_migrations WHERE app='vs_user' AND name='0001_initial' LIMIT 1\"
            )
            has_vs_user = c.fetchone() is not None

            if custom_total > 16 or not has_vs_user:
                print('needs_fake')
            else:
                print('normal')

except Exception as e:
    # django_migrations table does not exist yet — truly fresh DB.
    print('fresh')
" 2>/dev/null | grep -E "^(fresh|needs_fake|normal)$" | tail -1)

echo "==> Migration state: ${DB_STATE:-unknown}"

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
    print('contenttypes entries ensured.')
"

    python manage.py migrate auth --fake
    python manage.py migrate admin --fake
    python manage.py migrate sessions --fake
    python manage.py migrate token_blacklist --fake
    python manage.py migrate --fake-initial
    echo "Migration history reset complete."

else
    echo "Normal deploy — running migrate..."
    python manage.py migrate
fi

# Run seeding commands AFTER migrate succeeds
# python manage.py clear_permissions --yes
# python manage.py reset_db --yes
# python manage.py create_superuser --assign-role --email admin@codexng.com
