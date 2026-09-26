#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

# reset_db is local development and test tooling. Render builds in an
# ephemeral checkout, so remove the command from the deployed artifact before
# Django discovers management commands. Runtime guards remain the backstop for
# source checkouts and any non-Render packaging path.
if [[ "${RENDER:-}" == "true" ]]; then
  rm -f core/management/commands/reset_db.py
fi

python manage.py collectstatic --no-input

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ONE-TIME DATABASE REBUILD - REMOVE THIS BLOCK AFTER THE FIRST DEPLOY.   ║
# ║                                                                          ║
# ║  The migration history was squashed to fresh 0001 chains, so a database  ║
# ║  that still carries the OLD django_migrations rows cannot migrate        ║
# ║  forward - it must be dropped and rebuilt once. This block does that     ║
# ║  automatically on the next deploy (no env var to set).                   ║
# ║                                                                          ║
# ║  ⚠️  IT WIPES ALL DATA. Leaving it in place wipes the database on EVERY  ║
# ║  deploy. As soon as this deploy succeeds, delete this whole block        ║
# ║  (down to the END marker) and commit - future deploys then just migrate. ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# RESET_DB=true python manage.py rebuild_database --yes
# ╚═══════════════════════════ END ONE-TIME BLOCK ═══════════════════════════╝

python manage.py migrate

# Run seeding commands AFTER migrate succeeds (all idempotent - safe every deploy)
python manage.py seed_all_permissions
# Import templates and their columns. Upserts a template by `code` and a column
# by (template, column_name), and deletes a column the definition has dropped,
# so a template that gains a column gains it here rather than only where somebody
# remembered to run this by hand. It does NOT retire a template whose `code` was
# renamed: that leaves the old row answering alongside the new one, and the
# command warns instead of deleting, because two templates for one dataset can
# be legitimate. Read its output when a code changes.
python manage.py seed_import
python manage.py seed_notification_event_types
python manage.py seed_notification_templates
python manage.py seed_notification_settings
# Product reference data: config capability catalogue + billing package plans.
python manage.py seed_config_catalogue
python manage.py seed_package
# The first version of every tracked record that has none: the day a record's
# history starts, and so the earliest day its profile can be read "as at".
# Idempotent, and it does work only for rows it has not seen.
python manage.py baseline_record_history

# Bootstrap the first platform superuser. Self-skips (exits cleanly) once a
# platform-tenant staff account exists, so it is safe to leave in permanently.
# Set SUPERUSER_EMAIL / SUPERUSER_PASSWORD in the staging environment for a real
# credential (the fallback below is a known default - change it after first login).
# python manage.py create_superuser \
#   --email "${SUPERUSER_EMAIL:-chidera.ohanenye@codexng.com}" \
#   --password "${SUPERUSER_PASSWORD:-Admin@123456}" \
#   --first-name "${SUPERUSER_FIRST_NAME:-Chidera}" \
#   --last-name "${SUPERUSER_LAST_NAME:-Ohanenye}" \
