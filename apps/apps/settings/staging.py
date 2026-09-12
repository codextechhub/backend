"""Settings for a deployed service, whatever domain that service answers on.

Every address is read from this deployment's own environment. Nothing here
falls back to a host somebody else serves, because a fallback makes a
misconfigured deployment look healthy: the service starts, and the school
addresses it shows, the activation links it mails and the password resets it
sends all point at whichever product the fallback names. A tester invited on
one box would open the mail and activate a real account on another. Refusing
to start names the variable that is missing instead.
"""
from urllib.parse import urlsplit

from .base import *
from decouple import config

DEBUG = False

ALLOWED_HOSTS = config("ALLOWED_HOSTS").split(",")
assert ALLOWED_HOSTS, "ALLOWED_HOSTS must be set in production."

# Where this deployment's people are sent, and where its probes knock. See the
# module docstring, and the base URL section of base.py for what each answers.
FRONTEND_BASE_URL = deployment_url("FRONTEND_BASE_URL")
SCHOOL_APP_BASE_URL = deployment_url("SCHOOL_APP_BASE_URL")
API_PUBLIC_BASE_URL = deployment_url("API_PUBLIC_BASE_URL")
HEALTH_PROBE_BASE_URL = deployment_url("HEALTH_PROBE_BASE_URL")
HEALTH_SSL_DOMAIN = deployment_host("HEALTH_SSL_DOMAIN")

# Every tenant is served from its own subdomain of the school app's host, so
# the wildcard follows that host rather than naming one deployment's domain.
_school_app = urlsplit(SCHOOL_APP_BASE_URL)
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in config(
        "CSRF_TRUSTED_ORIGINS",
        default=(
            f"{FRONTEND_BASE_URL},"
            f"{_school_app.scheme}://*.{_school_app.netloc}"
        ),
    ).split(",")
    if origin.strip()
]
# The Console and API use sibling hosts. Only the non-secret CSRF token is
# shared with the Console; the refresh cookie remains host-only on the API.
CSRF_COOKIE_DOMAIN = config("CSRF_COOKIE_DOMAIN", default=".codexng.com")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME"),
        "USER": config("DB_USER"),
        "PASSWORD": config("DB_PASSWORD"),
        "HOST": config("DB_HOST"),
        "PORT": config("DB_PORT", default="5432"),
    }
}

# WhiteNoise - insert after SecurityMiddleware
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Security hardening
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# Celery runs eager, in the web process, until the worker service is live.
# Set CELERY_EAGER=false and REDIS_URL on BOTH services to switch over: an
# env change, no redeploy. Flip it back to bypass a broken broker.
CELERY_TASK_ALWAYS_EAGER     = config("CELERY_EAGER", default=True, cast=bool)
CELERY_TASK_EAGER_PROPAGATES = CELERY_TASK_ALWAYS_EAGER
