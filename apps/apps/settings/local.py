"""Development settings, and the settings the test suite is run under.

Email prints to the console by default. Celery is eager here, so every delivery
runs inline in the request and an unreachable SMTP host stalls responses for
``EMAIL_TIMEOUT`` times the recipient count. Set ``EMAIL_BACKEND`` in ``.env``
to the SMTP backend to send real mail; Zoho works on port 465 with SSL where
587 and TLS are blocked locally.

Two system checks are silenced for a test run only, never for the whole file.
This module is also the dev-server and dev-migrate settings, and a developer
whose database has never been seeded is exactly who those checks exist to warn.

``vs_notifications.W001`` reports active event types with no active template. It
is true and useless under ``manage.py test``: the test database is built by
migrations, so it holds the whole event-type registry and no templates at all,
those being seeded per test, and the runner would print a paragraph naming every
event type before every run of every app. ``core.W001`` is the same shape:
Django forces ``DEBUG=False`` for a test run, which is precisely the condition
the scheduler check fires on.

They are silenced here rather than in ``test.py`` or ``ci.py`` because the suite
is documented to run with ``--settings=apps.settings.local`` (see CLAUDE.md), so
silencing them anywhere else would silence nothing here.

Test fixtures are hashed cheaply for the same reason. PBKDF2 is deliberately
slow and the suite creates users constantly, so the default cost dominates a
run: ``schools.vs_schools`` took 17.5 minutes at 100% CPU for 143 tests, most
of it hashing passwords nobody checks the strength of. ``ci.py`` and
``test.py`` do this too, and this module needs it because it is the one
CLAUDE.md tells you to run the suite with. It sits inside the test guard on
purpose: this file is also the dev-server settings, and MD5-hashing a real
developer's password would weaken that environment rather than speed anything
up.

Logging is plain text at WARNING here, where ``base.py`` defaults to JSON at
INFO. JSON at INFO is right for a deployed log stream and wrong for a terminal
somebody is working in, where it is a wall of one-line objects, most of them
routine. Raise it deliberately when chasing something::

    LOG_LEVEL=INFO ./cx/bin/python manage.py runserver ...

The redaction filter is untouched: a quieter terminal is not a less careful one.
"""
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

# Console backend by default; see the module docstring.
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
# This API, for the links that point at it rather than at an application - the
# school logo in an email is the one so far. Without this a locally sent email
# would carry a production URL and quietly show no logo, or somebody else's.
API_PUBLIC_BASE_URL = 'http://localhost:8000'

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
# Two checks are silenced for a test run only. See the module docstring.
if "test" in sys.argv:
    # Both are true under a test run and useless. See the module docstring.
    SILENCED_SYSTEM_CHECKS = [
        *SILENCED_SYSTEM_CHECKS, "vs_notifications.W001", "core.W001",
    ]

    # Cheap hashing for fixtures, inside the test guard. See the module
    # docstring.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# --- Logging: readable, and quiet by default --------------------------------
# Plain text at WARNING; see the module docstring.
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
