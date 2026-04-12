#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

python manage.py migrate

# Run seeding commands AFTER migrate succeeds
python manage.py seed_perms
python manage.py seed_missing_perms
python manage.py seed_role_perms
python manage.py create_superuser
