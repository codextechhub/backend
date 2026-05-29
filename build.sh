#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

python manage.py migrate

python manage.py seed_actions               # adds submit/cancel/reverse verbs if missing
python manage.py seed_workflow_permissions  # seeds the 6 workflow permission keys + grants

# Run seeding commands AFTER migrate succeeds
# python manage.py clear_permissions --yes
# python manage.py reset_db --yes
# python manage.py create_superuser --assign-role --email admin@codexng.com
