#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

python manage.py migrate

# Run seeding commands AFTER migrate succeeds
python manage.py seed_perms
python manage.py seed_role_perms
python manage.py seed_suggested_role_templates
python manage.py create_superuser || python manage.py create_superuser --assign-role --email admin@codexng.com
