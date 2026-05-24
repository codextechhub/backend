#!/bin/bash
# sync_migrations.sh
#
# Run this ONCE after pulling the migration reset commit.
# Safe on an existing database — it does NOT drop or alter any table or data.
# Only needed if your local DB already has data from before the reset.
#
# Usage (from project root):
#   chmod +x sync_migrations.sh
#   ./sync_migrations.sh

set -e

cd "$(dirname "$0")/apps"

echo ""
echo "==> Step 1: Clearing old migration history for custom apps..."
python manage.py shell -c "
from django.db import connection
import datetime

apps = [
    'vs_admin_console',
    'vs_audit',
    'vs_config',
    'vs_import_data',
    'vs_notifications',
    'vs_rbac',
    'vs_schools',
    'vs_user',
]
now = datetime.datetime.now()

with connection.cursor() as c:
    for app in apps:
        c.execute('DELETE FROM django_migrations WHERE app = %s', [app])
        print(f'  Cleared: {app}')

    # contenttypes.0002 is a data migration that reads a column already dropped.
    # Insert its record directly instead of letting Django run it.
    for name in ['0001_initial', '0002_remove_content_type_name']:
        c.execute(
            'INSERT IGNORE INTO django_migrations (app, name, applied) VALUES (%s, %s, %s)',
            ['contenttypes', name, now]
        )
    print('  contenttypes entries ensured.')
"

echo ""
echo "==> Step 2: Faking Django internal app migrations..."
python manage.py migrate auth --fake 2>&1 | grep -E "Applying|Faking|FAKED|No migrations|already"
python manage.py migrate admin --fake 2>&1 | grep -E "Applying|Faking|FAKED|No migrations|already"
python manage.py migrate sessions --fake 2>&1 | grep -E "Applying|Faking|FAKED|No migrations|already"
python manage.py migrate token_blacklist --fake 2>&1 | grep -E "Applying|Faking|FAKED|No migrations|already"

echo ""
echo "==> Step 3: Fake-applying fresh migrations for custom apps..."
python manage.py migrate --fake-initial 2>&1 | grep -E "Applying|FAKED|OK|No migrations"

echo ""
echo "==> Done. Your migration state is now in sync with the repository."
echo "    No data was modified."
