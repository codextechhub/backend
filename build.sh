#!/usr/bin/env bash

set -o errexit

pip install -r requirements.txt

cd apps

python manage.py collectstatic --no-input

python manage.py migrate

# Run seeding commands AFTER migrate succeeds
python manage.py seed_import
# python manage.py clear_permissions --yes
# python manage.py reset_db --yes
# python manage.py create_superuser --assign-role --email admin@codexng.com
