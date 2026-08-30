"""
Test settings.

PostgreSQL, like local, CI, staging and production. SQLite used to live here
because it needs no server, and it cost more than it saved: threaded tests,
row locking (select_for_update), the finance audit trigger and the branch-code
allocator all behave differently or not at all on it, so a green local run
could hide a broken deploy and a red one could mean nothing. There is now one
engine everywhere.

Celery runs EAGER (in-process). Without it, every test that dispatches a
notification, an export or a webhook needs a live Redis broker just to enqueue,
and fails with a connection error that has nothing to do with the code.

DB_NAME picks the database this run builds test_<name> from, so two sessions
on one machine can run the suite at the same time without fighting:

    DB_NAME=cx_db_mine python manage.py test --settings=apps.settings.test
"""
from .base import *

DEBUG = True
ALLOWED_HOSTS = ["*"]

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

RESET_DB_ALLOWED_DATABASES = {
    name.strip()
    for name in config("RESET_DB_ALLOWED_DATABASES", default="cx_db").split(",")
    if name.strip()
}

# Fast hashing - these are throwaway test users.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Never send real email or reach for a broker.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Disable throttling in tests - they hammer endpoints far faster than the
# configured rates allow.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {},
}

# vs_health: no background metric-flush thread under tests (see ci.py).
HEALTH_METRICS_BACKGROUND_FLUSH = False

# vs_notifications.W001 (active event types with no active template) is true
# and useless here: the test database is built by migrations, so it holds the
# whole event-type registry from vs_notifications migration 0008 and no
# templates at all, which the suite seeds per test. Real environments keep the
# warning; see vs_notifications/checks.py.
SILENCED_SYSTEM_CHECKS = [*SILENCED_SYSTEM_CHECKS, "vs_notifications.W001"]


# Logging is silent under `manage.py test`, and that is decided in base.py so
# it holds for local.py and ci.py too - the suite is run on all three.
