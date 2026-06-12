#!/usr/bin/env bash
# reseed-dev.sh — drop, recreate, migrate and fully seed the LOCAL dev database.
#
#   ./reseed-dev.sh
#
# Safe to run from any directory. DESTRUCTIVE for the local cx_db only —
# it never touches staging. Kicks out any open DBeaver/psql sessions first.
#
# Result: the CX-staff-focused dev world — 25-seat Codex organogram with HR
# profiles and platform roles, ToDo board, login/security history, one
# impersonation session, plus 3 schools / 35 school users / RBAC /
# notifications as the customer base. Finance, procurement and payments
# are deliberately NOT seeded.
#
# Logins after seeding:
#   super admin   admin@codexng.com            Admin@123456
#   vision staff  ada.nwachukwu@vision.edu     Vision@2025   (MD — and 24 others)
#   school admin  admin@greenfield-academy.example.com  School@2025

set -o errexit
set -o pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/cx/bin/python"
MG="$REPO/apps/manage.py"
SETTINGS="--settings=apps.settings.local"
DB_NAME="${DB_NAME:-cx_db}"

run() { "$PY" "$MG" "$@" "$SETTINGS"; }

echo "→ Recreating database '$DB_NAME' (terminating open sessions)..."
psql -d postgres -qc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                      WHERE datname='$DB_NAME' AND pid <> pg_backend_pid();" >/dev/null
dropdb --if-exists "$DB_NAME"
createdb "$DB_NAME"

echo "→ Migrating..."
run migrate | tail -1

echo "→ Seeding foundations (permissions, modules, packages)..."
run seed_all_permissions
run seed_xvs_modules
run seed_package

echo "→ Superuser + platform-role grants..."
run create_superuser --force
# Re-run: the workflow/finance permission grants attach to platform roles
# that only exist after create_superuser. Idempotent.
run seed_all_permissions

echo "→ Staff, import templates, notification catalogue..."
run seed_vision_staff
run seed_import
run seed_notification_event_types
run seed_notification_templates

echo "→ Dev world (organogram, schools, users, todo, security)..."
run seed_dev_data

echo "→ Per-school notification settings..."
run seed_notification_settings --all

echo ""
echo "✔ Done. Logins: admin@codexng.com / Admin@123456 · *.vision.edu / Vision@2025 · school users / School@2025"
