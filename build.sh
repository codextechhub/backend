#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ONE-TIME DATABASE REBUILD — REMOVE THIS BLOCK AFTER THE FIRST DEPLOY.   ║
# ║                                                                          ║
# ║  The migration history was squashed to fresh 0001 chains, so a database  ║
# ║  that still carries the OLD django_migrations rows cannot migrate        ║
# ║  forward — it must be dropped and rebuilt once. This block does that     ║
# ║  automatically on the next deploy (no env var to set).                   ║
# ║                                                                          ║
# ║  ⚠️  IT WIPES ALL DATA. Leaving it in place wipes the database on EVERY  ║
# ║  deploy. As soon as this deploy succeeds, delete this whole block        ║
# ║  (down to the END marker) and commit — future deploys then just migrate. ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# RESET_DB=true python manage.py rebuild_database --yes
# ╚═══════════════════════════ END ONE-TIME BLOCK ═══════════════════════════╝

python manage.py migrate

# Run seeding commands AFTER migrate succeeds (all idempotent — safe every deploy)
python manage.py seed_all_permissions
python manage.py seed_notification_event_types
python manage.py seed_notification_templates
python manage.py seed_notification_settings
# Product reference data: config capability catalogue + billing package plans.
python manage.py seed_config_catalogue
python manage.py seed_package
# View-only Consultant platform role — re-syncs to all *.view permissions,
# so it must run after the permission seeders above.
python manage.py seed_consultant_role

# Repair the CX user left pending when staging was missing the platform-user
# approval template. The command is idempotent and only targets this email, so
# keeping it here is safe after the user has been submitted or invited.
python manage.py repair_pending_user_approvals --email manuelola6@gmail.com

# Bootstrap the first platform superuser. Self-skips (exits cleanly) once a
# platform-tenant staff account exists, so it is safe to leave in permanently.
# Set SUPERUSER_EMAIL / SUPERUSER_PASSWORD in the staging environment for a real
# credential (the fallback below is a known default — change it after first login).
# python manage.py create_superuser \
#   --email "${SUPERUSER_EMAIL:-chidera.ohanenye@codexng.com}" \
#   --password "${SUPERUSER_PASSWORD:-Admin@123456}" \
#   --first-name "${SUPERUSER_FIRST_NAME:-Chidera}" \
#   --last-name "${SUPERUSER_LAST_NAME:-Ohanenye}" \
