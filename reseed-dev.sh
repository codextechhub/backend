#!/usr/bin/env bash
# reseed-dev.sh - drop, recreate, migrate and fully seed the LOCAL dev database.
#
#   ./reseed-dev.sh
#
# Safe to run from any directory. DESTRUCTIVE for the database Django is
# configured to use (DB_NAME, default cx_db), and only when that name is in
# RESET_DB_ALLOWED_DATABASES - it never touches staging. Kicks out any open
# DBeaver/psql sessions first.
#
# The connection is Django's own (DB_* from the environment or apps/.env), so
# the database recreated is the one then migrated and seeded. With no DB_USER
# set, that is the operating-system account, as for psql.
#
# Environment:
#   SECRET_KEY     required, as for any manage.py command
#   PYTHON         the interpreter to run (default: ./cx/bin/python); CI sets it
#   EMAIL_BACKEND  defaults to the in-memory backend, so no seeded address is
#                  mailed and the console is not flooded with message bodies
#
# Result: the CX-staff-focused dev world - 40-seat Codex organogram with HR
# profiles and platform roles, ToDo board, login/security history, one
# impersonation session, plus 3 customer schools / 35 school users / RBAC /
# notifications; then the onboarding cast (one school per state) and, at the
# schools that carry them, academic structure, staff, calendars, timetables,
# students, attendance, results and the student portal. Finance, procurement
# and payments are NOT seeded, except lagoon-view's starter fee structure,
# which the results and student-portal scenarios bill from.
#
# Logins after seeding:
#   super admin   admin@codexng.com            Admin@123456
#   vision staff  ada.nwachukwu@vision.edu     Vision@2025   (MD - and 39 others)
#   school admin  admin@greenfield-academy.example.com  School@2025

set -o errexit
set -o pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$REPO/cx/bin/python}"
MG="$REPO/apps/manage.py"
SETTINGS="--settings=apps.settings.local"
export EMAIL_BACKEND="${EMAIL_BACKEND:-django.core.mail.backends.locmem.EmailBackend}"

run() { "$PY" "$MG" "$@" "$SETTINGS"; }

echo "→ Recreating the database (terminating open sessions)..."
run recreate_dev_db --noinput

echo "→ Migrating..."
run migrate | tail -1

echo "→ Seeding foundations (permissions, capability catalogue, packages)..."
run seed_all_permissions
run seed_config_catalogue
run seed_package

echo "→ Superuser + platform-role grants..."
run create_superuser --force
# Re-run: the workflow/finance permission grants attach to platform roles
# that only exist after create_superuser. Idempotent.
run seed_all_permissions

echo "→ Staff and notification catalogue..."
run seed_vision_staff
run seed_notification_event_types
run seed_notification_templates

echo "→ Dev world (organogram, schools, users, todo, security)..."
run seed_dev_data

echo "→ Per-school notification settings..."
run seed_notification_settings --all

# A school per onboarding state, so every screen in the control room can be
# opened rather than imagined. lagoon-view and sunrise-academy finish LIVE;
# every module gated on a live school is driven against those two.
echo "→ Onboarding scenario schools (one per state)..."
run seed_onboarding_scenarios

# Years, classes and subjects for brightfield-lekki, st-monicas, holy-cross,
# sunrise-academy and lagoon-view. Everything below hangs off them.
echo "→ Academic structure..."
run seed_academic_scenarios

# Before the timetables: a school with nobody holding the teacher role gets its
# calendar and no timetable.
echo "→ Staff for holy-cross, brightfield-lekki, sunrise-academy and st-monicas..."
run seed_staff_scenarios

echo "→ Calendars and timetables for brightfield-lekki, st-monicas and holy-cross..."
run seed_timetable_scenarios

# Must run AFTER the academic seeder: a student is placed into a class in a
# year, and the command refuses rather than inventing either.
echo "→ Students, guardians and the applicant pipeline..."
run seed_student_scenarios

# The two live schools from here on. These seeders invite lagoon-view's
# teachers, administrators and bursar themselves.
echo "→ Attendance registers at sunrise-academy and lagoon-view..."
run seed_attendance_scenarios

echo "→ lagoon-view's starter fee structure (the families who owe are billed from it)..."
run seed_finance_ar_demo --entity LAGOONVIEW

echo "→ Results for the current term, then last year's..."
run seed_gradebook_scenarios
run seed_gradebook_history

echo "→ Student portal: pupils' access, notices, a billed and part-paid family..."
run seed_student_portal_scenarios

echo "→ Parent portal: parents with accounts, siblings at two branches, a teacher who is a parent..."
run seed_parent_portal_scenarios

echo ""
# Rows the seeders wrote without a save (raw bulk paths) still get a first
# version, so every seeded profile can be read "as at" from today.
run baseline_record_history

echo "✔ Done. Logins: admin@codexng.com / Admin@123456 · *.vision.edu / Vision@2025 · school users / School@2025"
echo "  Onboarding cast (School@2025): brightfield-lekki not-ready · st-monicas ready · holy-cross pending"
echo "                                grace-fields rejected · crescent-model failed · lagoon-view live"
echo "                                new-dawn unprovisioned · riverbank expiring"
echo "                                sunrise-academy live, ONE branch - admin@<slug>.example.com"
echo "  Academic structure + students for: brightfield-lekki (2 br) · st-monicas (1 br)"
echo "                                holy-cross (2) · sunrise-academy (1) · lagoon-view (2)"
echo "  Calendars + timetables for brightfield-lekki, st-monicas and holy-cross"
echo "  Every module gated on a LIVE school is driven against:"
echo "                                sunrise-academy  1 branch, live - the branch"
echo "                                                 dimension must RECEDE here"
echo "                                lagoon-view      2 branches, live - the branch"
echo "                                                 filter has real rows here"
echo "                                The other cast schools answer 403 TENANT_NOT_LIVE:"
echo "                                the cast parks them mid-onboarding on purpose."
echo "  Staff, attendance, results and the student portal at the two live schools:"
echo "                                the logins each seeder printed above. Teachers"
echo "                                sign in with SchoolStaff@2026, pupils with Pupil@School2026."
