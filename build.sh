#!/usr/bin/env bash

set -o errexit

# WeasyPrint (invoice/receipt PDF rendering) needs native libraries at runtime:
# pango, cairo, gdk-pixbuf, ffi, shared-mime-info. Best-effort install where the
# build has apt with privileges (Docker images, self-hosted).
#
# NOTE — Render's *native* Python runtime does NOT permit apt installs, so this is
# a no-op there and the .pdf endpoints will return HTTP 503 (HTML invoices/receipts
# still work). To render PDFs on Render, run this service from a Dockerfile that
# installs these libs, or use an image that already ships them.
if command -v apt-get >/dev/null 2>&1; then
  (apt-get update && apt-get install -y --no-install-recommends \
     libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libcairo2 \
     libffi8 shared-mime-info) \
    || echo "WeasyPrint native libs not installed (no apt privileges) — PDF endpoints will 503."
fi

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

python manage.py migrate

# Run seeding commands AFTER migrate succeeds
python manage.py seed_all_permissions
python manage.py seed_notification_event_types
python manage.py seed_notification_templates
python manage.py seed_notification_settings
# python manage.py reset_db --yes
# python manage.py create_superuser --assign-role --email admin@codexng.com
