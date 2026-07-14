#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

# ── One-shot database rebuild (squashed migrations cutover) ───────────────────
# The migration history was reset to fresh 0001 chains, so a database carrying
# the old django_migrations rows cannot migrate forward — it must be rebuilt.
# Set RESET_DB=true in the environment for the cutover deploy, then REMOVE the
# env var: the command is double-guarded (env var + --yes) and destroys all
# data in the schema. Seeds below repopulate permissions/notifications; the
# codex platform tenant is seeded by migration vs_tenants/0002.
if [ "${RESET_DB:-false}" = "true" ]; then
  echo ">>> RESET_DB=true — dropping and rebuilding the database schema"
  python manage.py rebuild_database --yes
fi

python manage.py migrate

# Run seeding commands AFTER migrate succeeds
python manage.py seed_all_permissions
python manage.py seed_notification_event_types
python manage.py seed_notification_templates
python manage.py seed_notification_settings
# python manage.py reset_db --yes
# python manage.py create_superuser --assign-role --email admin@codexng.com
