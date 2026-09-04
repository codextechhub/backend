"""CI settings, used by the GitHub Actions workflow (.github/workflows/ci.yml).

PostgreSQL, because that is what staging runs: the whole point of CI is to
exercise the schema and engine path production code will actually meet.

Throttling is disabled by zeroing the rates, never by emptying the dict. Every
scope keeps its entry and loses only its rate. An empty dict does not turn
throttling off, it breaks it: a view naming its own ``throttle_classes`` builds
them whatever the default classes are, and DRF raises ``ImproperlyConfigured``
for a scope with no entry at all, so those views answer 500 to every request
rather than being served without a limit. The public pay-an-invoice routes are
the ones that name their own. ``None`` is the value DRF reads as "no limit", so
every scope resolves, none of them counts, and a scope added later inherits that
without anybody having to remember this file exists.

Two system checks are silenced. ``vs_notifications.W001`` reports active event
types with no active template, which is true and useless here: the test database
is built by migrations, so it holds the whole event-type registry and no
templates at all, those being seeded per test. ``core.W001`` fires on
``DEBUG=False`` with no worker, which is exactly how CI runs. Both are facts
about the deployment rather than about the build, and real environments keep
the warnings; see ``vs_notifications/checks.py``.
"""
from .base import *

DEBUG = False
ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "cx_ci",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "127.0.0.1",
        "PORT": "5432",
    }
}

RESET_DB_ALLOWED_DATABASES = {"cx_ci"}

# Fast hashing - these are throwaway test users.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Never send real email or hit a broker from CI.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Throttling off: tests hammer endpoints far faster than the rates allow. Rates
# are zeroed, never removed. See the module docstring.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": dict.fromkeys(
        REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], None,
    ),
}

# vs_health: no background metric-flush thread under tests. It holds a DB
# connection open, blocking test-database teardown, and races the explicit
# flush() the collector tests assert on.
HEALTH_METRICS_BACKGROUND_FLUSH = False

# Both are true under CI and useless. See the module docstring.
SILENCED_SYSTEM_CHECKS = [
    *SILENCED_SYSTEM_CHECKS, "vs_notifications.W001", "core.W001",
]
