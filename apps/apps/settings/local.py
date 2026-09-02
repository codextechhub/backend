from .base import *

import sys

DEBUG = True

ALLOWED_HOSTS = []

# Dev conveniences - open CORS and the browsable API (both locked down in base).
CORS_ALLOW_ALL_ORIGINS = True
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# Email prints to the console by default: with eager Celery every delivery
# runs inline in the request, so an unreachable SMTP host stalls responses
# for EMAIL_TIMEOUT × recipient count. Set EMAIL_BACKEND in .env to
# django.core.mail.backends.smtp.EmailBackend to send real mail
# (Zoho: port 465 + SSL works where 587/TLS is blocked locally).
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
EMAIL_PORT    = 465
EMAIL_USE_SSL = True
EMAIL_USE_TLS = False

# Run Celery tasks synchronously in local dev - no broker needed.
CELERY_TASK_ALWAYS_EAGER     = True
CELERY_TASK_EAGER_PROPAGATES = True

# Frontend URL - must point to the React dev server, not the Django backend
FRONTEND_BASE_URL = 'http://localhost:5173'  # Console (console-fe)
# The school app, where a paying parent goes. Its slug is inserted as a
# subdomain at call time: corona.localhost:5174 resolves to 127.0.0.1 without
# any hosts-file entry, which is the same shape the onboarding seeder prints.
SCHOOL_APP_BASE_URL = 'http://localhost:5174'  # school-fe

# PostgreSQL - the only engine, same as staging and CI. The MariaDB
# fallback was retired 2026-06-12; final dump: ~/cx_db_mariadb_final_backup.sql.gz
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="cx_db"),
        "USER": config("DB_USER", default=os.environ.get("USER", "postgres")),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

# reset_db refuses every target not named here. Add a disposable developer
# database explicitly rather than deriving this list from DB_NAME, because the
# independent name check is what catches a shell still pointing at live data.
RESET_DB_ALLOWED_DATABASES = {
    name.strip()
    for name in config("RESET_DB_ALLOWED_DATABASES", default="cx_db").split(",")
    if name.strip()
}

# ---------------------------------------------------------------------------
# System checks
# ---------------------------------------------------------------------------
# vs_notifications.W001 reports active notification event types with no active
# template (see vs_notifications/checks.py). Under `manage.py test` it is true
# but useless: the test database is built by migrations, so it gets the whole
# event-type registry from vs_notifications migration 0008 and NO templates
# (those come from seed_notification_templates, which the suite calls per test),
# and the runner would print a paragraph naming every event type before every
# run of every app.
#
# Silenced only for a test run, not for the whole file. This module is also the
# dev-server and dev-migrate settings, and a developer whose database has never
# been seeded is exactly the environment the check exists to warn. The suite is
# documented to run with --settings=apps.settings.local (see CLAUDE.md), so
# silencing it in test.py or ci.py alone would silence nothing here.
if "test" in sys.argv:
    # core.W001 too: Django forces DEBUG=False for a test run, which is exactly
    # the condition the scheduler check fires on, so leaving it would print the
    # same paragraph above every suite in the repo.
    SILENCED_SYSTEM_CHECKS = [
        *SILENCED_SYSTEM_CHECKS, "vs_notifications.W001", "core.W001",
    ]

    # Hash test fixtures cheaply. PBKDF2 is deliberately slow, and the suite
    # creates users constantly, so the default cost dominates the run:
    # schools.vs_schools took 17.5 minutes at 100% CPU for 143 tests, most of
    # it hashing passwords nobody checks the strength of.
    #
    # ci.py and test.py already do this. local.py did not, and local.py is the
    # module CLAUDE.md tells you to run the suite with - so the documented
    # command was the one path still paying full price.
    #
    # Inside the test guard on purpose. This file is also the dev-server
    # settings, and MD5-hashing a real developer's password would be a genuine
    # weakening of that environment, not a speed-up.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# --- Logging: readable, and quiet by default --------------------------------
# base.py defaults to JSON at INFO, which is right for the deployed log stream
# and wrong for a terminal somebody is working in: a wall of one-line JSON
# objects, most of them routine.
#
# Locally the format is plain text and the floor is WARNING, so ordinary
# request chatter stays out of the way and only something worth reading
# appears. Raise it deliberately when chasing a problem:
#
#     LOG_LEVEL=INFO ./cx/bin/python manage.py runserver ...
#
# The redaction filter is untouched and still applies - a quieter terminal is
# not a less careful one.
LOG_FORMAT = config("LOG_FORMAT", default="plain")
LOG_LEVEL = config("LOG_LEVEL", default="WARNING")

# base.py already drops the handler entirely under `manage.py test`; this only
# changes the shape of what a developer sees when the server is actually running.
LOGGING = {
    **LOGGING,
    "handlers": {
        **LOGGING["handlers"],
        "console": {**LOGGING["handlers"]["console"], "formatter": "plain"}
        if not RUNNING_TESTS else LOGGING["handlers"]["console"],
    },
    "root": {**LOGGING["root"], "level": LOG_LEVEL},
    "loggers": {
        key: {**cfg, "level": LOG_LEVEL}
        for key, cfg in LOGGING["loggers"].items()
    },
}
