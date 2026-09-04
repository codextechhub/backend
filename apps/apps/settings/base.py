"""Django base settings shared by every environment.

``auth.E003`` is silenced here rather than per environment, because the
decision behind it is the same in development, CI, staging and production.
The check says ``USERNAME_FIELD`` must be globally unique, and here it
deliberately is not: one real address can be a login at more than one customer
of this platform, such as a parent with a child at two schools. Uniqueness
lives on ``vs_user.User``'s ``uq_user_email_per_tenant`` instead.

The check exists because ``django.contrib.auth``'s ``ModelBackend`` resolves a
login with ``get_by_natural_key()``, a bare ``.get()`` on the username field,
which would raise ``MultipleObjectsReturned``. Nothing here reaches it:
requests authenticate with JWT through ``vs_rbac.authentication``,
``LoginService`` checks the password itself against a tenant-scoped lookup, and
``django.contrib.admin``, the other ``ModelBackend`` caller, is deliberately
absent from ``INSTALLED_APPS``. An environment file that adds its own silenced
checks must extend the list rather than replace it.

``django.contrib.admin`` and ``django.contrib.messages`` are both absent on
purpose. This is an API serving its own console: the admin site is not routed
and no app ships an ``admin.py``, while ``messages`` is a server-rendered flash
framework that nothing here imports, the frontend owning its own toasts.

Secrets
-------
``SECRET_KEY``, ``RENDER_API_KEY`` and ``TEMP_PASSWORD_PEPPER`` carry no
fallback literal, so the server refuses to start without them. A default lets a
local run succeed against a production credential when the variable is missing,
which is how a fallback secret gets used in anger, and a commented-out secret
is still published in every clone of this repository.

OUTSTANDING: all three were once committed here as fallbacks and remain in this
repository's history. Rotate them at source - the Render dashboard for the API
key, the env group for the other two.

Throttling
----------
The public pay-an-invoice scopes are split between reads and writes rather than
sharing one budget. An honest payer reads the page several times in one
payment: opening it, starting a checkout, and returning from the gateway.
Spending those reads out of the budget that exists to stop a link being worked
would refuse somebody mid-payment.

CORS
----
Every school is served from its own subdomain, so the browser origin differs
per tenant and cannot be enumerated in advance. The regex matches one label
only, and django-cors-headers echoes the matched origin rather than ``*``,
which is what keeps this legal alongside ``CORS_ALLOW_CREDENTIALS``.

Monitoring copies
-----------------
Every monitoring list below is an internal mailbox copied so somebody can see
what went out, and every one of them is BCC rather than CC. A visible copy puts
internal addresses in front of customers and vendors, tells each recipient
their mail is monitored, and hands anyone hitting reply-all a route into an
internal inbox.

Each list reads its old ``*_CC`` variable as a fallback, so a deployment that
has not renamed its variables keeps the addresses it had, and each falls back
to :data:`MONITORING_MAILBOX` so a deployment setting neither variable still
gets the copy. That default is written once and shared: three separately-typed
defaults drift, and a list that drifts to ``""`` is empty on exactly the
deployment nobody configured, which is where a missing copy is hardest to
notice.

The copy of an invitation carries the recipient's live activation link, so
anybody who can read the monitoring mailbox can activate that account. That is
deliberate, and it is what lets the invitation and activation flow be tested
end to end without access to the invitee's own inbox. Treat read access to that
mailbox as equivalent to holding every pending invitation on the platform, and
set ``EMAIL_BCC`` empty on any deployment where that is not wanted.

Base URLs, and why none of them derives from another
----------------------------------------------------
Four base URLs answer four different questions, and each is its own setting
even where two currently hold the same value.

``FRONTEND_BASE_URL`` addresses the Console, where staff sign in. It is right
for the links staff receive: invitations and password resets.
``SCHOOL_APP_BASE_URL`` addresses the school apps, served per school at
``<slug>.xvs.codexng.com``. A parent paying a fee invoice is not staff and has
no account anywhere, so they belong there; sending them to the Console is how a
pay link comes to point at a backoffice they cannot open. It stores scheme and
host only, and the slug is inserted as a subdomain at call time, so one setting
serves every school and a local checkout still works.
``PLATFORM_PAY_BASE_URL`` covers the payer whose invoice was raised by the
platform's own books rather than a school's, which has no school subdomain to
build from. Empty means the reserved ``pay.`` subdomain of
``SCHOOL_APP_BASE_URL``: it must be a subdomain, because the bare host serves
the product site rather than the app, and ``pay`` is reserved in
``vs_tenants.RESERVED_TENANT_SLUGS`` so no school can take it.
``API_PUBLIC_BASE_URL`` is where this API answers from, kept separate from
``HEALTH_PROBE_BASE_URL`` even though they match today: that one names where
the synthetic probes knock, and re-pointing the probes at a canary would
silently re-point the logo in every school's email with them.

**None of these may be derived from another at import time.** An environment
module sets its own values *after* ``from .base import *``, so an f-string
evaluated in this file freezes the base default and keeps it for ever. Staging
shipped a localhost pay link to real customers exactly that way. Leave the
derived setting empty and let the service resolve it at call time.

Logging
-------
Records are emitted as one JSON object per line, so the stream can be searched,
filtered by tenant or task, and shipped to a log service without touching a
call site. ``LOG_FORMAT=plain`` restores human-readable output for local work.
Without an explicit block Django's implicit default applies: app loggers
propagate to a root with no handler, so anything below WARNING vanishes and
everything above it reaches stderr in whatever shape the caller wrote it. On
Render that stderr is the only log there is, it is unstructured, and it is
dropped after roughly a week.

Every handler carries ``core.redaction.RedactingLogFilter``. Personal data
reaches the log stream by a route entirely separate from the database - a dozen
``logger.warning(..., exc_info=True)`` calls, plus Celery's own traceback
printing - so scrubbing only the stored error moves the leak rather than
closing it. The unredacted traceback is not lost: ``core.models.TaskDiagnostic``
holds it for 400 days behind ``platform.tasks.view_sensitive``, with every read
audited. That table, not this stream, is where an investigation goes.

``django.request`` is silenced under ``manage.py test``. The suite drives 401s,
403s and 404s by the dozen on purpose and that logger reports every one at
WARNING, which puts a line between every pair of dots and buries the one line
that matters when something actually fails. It is silenced here rather than in
``settings/test.py`` because the suite runs on ``local.py`` and ``ci.py`` too,
so this is the only place the rule holds for all three. A test that cares what
was logged uses ``assertLogs``, which attaches its own handler and is
unaffected.
"""

from datetime import timedelta
import os
import sys
from pathlib import Path
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent


# SECURITY WARNING: keep the secret key used in production secret!
# All three values MUST be set via environment variables or a .env file.
# The server will refuse to start if any are missing.
SECRET_KEY = config("SECRET_KEY")
RENDER_API_KEY = config("RENDER_API_KEY")
TEMP_PASSWORD_PEPPER = config("TEMP_PASSWORD_PEPPER")

# No fallback literals, and three secrets still to rotate. See the module
# docstring.

AUTH_USER_MODEL = "vs_user.User"

# Email is unique per tenant, not globally. See the module docstring.
SILENCED_SYSTEM_CHECKS = ["auth.E003"]

REST_FRAMEWORK = {
    # JSON only by default - local.py adds the browsable API for development.
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    # JWT auth that also resolves request.tenant + the thread-local tenant
    # context (Django middleware runs too early to see JWT users).
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "vs_rbac.authentication.TenantJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        # A tenant that has not gone live reaches the onboarding surface and
        # nothing else. In the defaults so a view declaring no permission_classes
        # is closed to it rather than open by omission.
        "vs_rbac.permissions.TenantSurfaceAllowed",
    ],
    "EXCEPTION_HANDLER": "core.exceptions.custom_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "core.schema.EnvelopeAutoSchema",
    "DEFAULT_PAGINATION_CLASS": "core.pagination.XVSPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login":          "5/minute",
        "password_reset": "3/minute",
        "activation":     "10/minute",
        "rfq_portal":     "120/hour",
        "rfq_verification": "10/hour",
        # Public pay-an-invoice page, keyed by IP. A whole school's parents can
        # share one address, so these bound abuse rather than pace a payer; the
        # per-link limits below are what stop one link being worked.
        "invoice_pay":       "240/hour",
        "invoice_pay_start": "60/hour",
        # Keyed by the pay token, bounding one invoice's link. Read and write
        # are separate budgets; see the module docstring.
        "invoice_pay_link":  "12/hour",
        "invoice_pay_link_read": "60/hour",
        # A school's crest on its own sign-in page. Cacheable and shared by a
        # whole staff room on one address, so it is set to bound scraping rather
        # than to pace a browser.
        "school_brand":      "240/hour",
        "guide_analytics": "120/minute",
        # Public barcode-login preview - throttled hard because it confirms
        # whether an email belongs to a known account (enumeration surface).
        "login_preview":  "10/minute",
    },
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%S.%fZ",
    "DATE_FORMAT":     "%Y-%m-%d",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "TOKEN_OBTAIN_SERIALIZER": "vs_user.tokens.CustomTokenObtainPairSerializer",
}

# Application definition

INSTALLED_APPS = [
    # django.contrib.admin and django.contrib.messages are absent on purpose.
    # See the module docstring.
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "core",  # Custom management commands

    # Django-rest framework
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "drf_spectacular",

    # apps
    "vs_tenants",
    # XVS, the schools product. Everything school-shaped lives under
    # apps/schools/; the app label stays "vs_schools" (see its AppConfig).
    "schools.vs_schools",
    "schools.vs_academics",
    # After vs_academics: this app points at its session, class and subject,
    # which is the order the dependency runs in.
    "schools.vs_calendar",
    # School-specific by construction, so it sits beside vs_schools under
    # apps/schools/ and never beside the domain-neutral engines.
    "schools.vs_onboarding",
    # After vs_academics: every ClassEnrolment row carries two non-null
    # foreign keys into it, so that is the order the dependency runs in.
    "schools.vs_students",
    # The Finance Abstraction Layer: the boundary where school words meet the
    # neutral finance engines, so it belongs here and never in apps/core/. A
    # Django app only because it owns the fee-structure-to-term link table.
    "schools.core.fal.apps.FalConfig",
    "vs_admin_console",
    "vs_user",
    "vs_rbac",
    "vs_audit",
    "vs_import_data",
    'vs_config',
    'vs_notifications',
    'vs_workflow',
    'vs_finance',
    'vs_procurement',
    'vs_payments',
    'vs_exports',
    'vs_todo',
    'vs_tickets',
    'vs_health',
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",

    # --- Custom middleware for school context and tenant isolation ---
    'vs_tenants.middleware.TenantContextCleanupMiddleware',
    # Observability: record per-request metrics AFTER tenant context is
    # resolved so the school dimension is available. Self-instrumentation is
    # best-effort and never blocks a request (see vs_health.middleware).
    'vs_health.middleware.RequestMetricsMiddleware',
    # --- End of custom middleware ---

    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "apps.urls"

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL        = config("REDIS_URL", default="redis://localhost:6379/0")
CELERY_TASK_IGNORE_RESULT = True
CELERY_ACCEPT_CONTENT    = ["json"]
CELERY_TASK_SERIALIZER   = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE          = "UTC"

# CORS - locked to known frontend origins (comma-separated env override).
# local.py re-opens this for development servers.
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in config(
        "CORS_ALLOWED_ORIGINS", default="https://intranet.codexng.com"
    ).split(",")
    if origin.strip()
]
# One subdomain per school, matched a single label deep. See the module
# docstring.
CORS_ALLOWED_ORIGIN_REGEXES = [
    origin.strip()
    for origin in config(
        "CORS_ALLOWED_ORIGIN_REGEXES",
        default=r"^https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.xvs\.codexng\.com$",
    ).split(",")
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

# Proxy requests carry the audited session id in a custom header. Browsers
# preflight custom headers even when the origin itself is allowed, so this must
# be present in every environment (including production).
from corsheaders.defaults import default_headers

CORS_ALLOW_HEADERS = (
    *default_headers,
    "x-impersonation-session",
    "idempotency-key",
    # The public vendor quotation portal authenticates with its verified session
    # token in this header, so every portal call after email verification is a
    # preflighted cross-origin request.
    "x-rfq-session",
)
IMPERSONATION_IDLE_TIMEOUT_MINUTES = 30  # Proxy sessions idle beyond this are swept to EXPIRED by a cron job.

SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE    = "Lax"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            os.path.join(BASE_DIR, "templates/"),
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ],
        },
    },
]

WSGI_APPLICATION = "apps.wsgi.application"


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

# PasswordComplexityValidator in vs_user/password_policy.py is the single
# source of truth: 12 characters, upper, lower, digit and special. It subsumes
# MinimumLength and Numeric, so neither is listed here.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "vs_user.password_policy.PasswordComplexityValidator",
    },
]


# Email settings - Zoho SMTP (credentials come from environment)
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = config("EMAIL_HOST", default="smtp.zoho.com")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=False, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_TIMEOUT = config("EMAIL_TIMEOUT", default=20, cast=int)
DEFAULT_FROM_EMAIL = config(
    "DEFAULT_FROM_EMAIL",
    default="CodeX Vision <chidera.ohanenye@codexng.com>",
)
# Monitoring copies are BCC, never CC, and a copy of an invitation carries a
# working activation link. See the module docstring before changing either.
MONITORING_MAILBOX = "backend-test@codexng.com"


def _addresses(name: str, legacy_name: str) -> list[str]:
    """The monitoring list *name* holds, falling back to its old CC variable."""
    raw = config(name, default=config(legacy_name, default=MONITORING_MAILBOX))
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


EMAIL_BCC = _addresses("EMAIL_BCC", "EMAIL_CC")
# Procurement messages sent to external vendors use a narrower list so monitoring
# does not copy unrelated platform email into the procurement inbox.
PROCUREMENT_VENDOR_EMAIL_BCC = _addresses(
    "PROCUREMENT_VENDOR_EMAIL_BCC", "PROCUREMENT_VENDOR_EMAIL_CC",
)
# Finance documents sent to paying customers (invoice, receipt, statement) copy a
# finance-owned mailbox rather than the platform-wide EMAIL_BCC, for the same reason
# procurement narrows its own: a customer document is not general platform mail.
FINANCE_CUSTOMER_EMAIL_BCC = _addresses(
    "FINANCE_CUSTOMER_EMAIL_BCC", "FINANCE_CUSTOMER_EMAIL_CC",
)
FRONTEND_BASE_URL = config("FRONTEND_BASE_URL", default="http://localhost:3000")

# Public targets used by the platform-health synthetic probes. Keep these
# configurable per environment; the defaults are the production API domain.
HEALTH_PROBE_BASE_URL = config(
    "HEALTH_PROBE_BASE_URL", default="https://api.codexng.com"
).rstrip("/")
HEALTH_SSL_DOMAIN = config("HEALTH_SSL_DOMAIN", default="api.codexng.com")

# Where this API answers from. Its own setting, never derived; see the
# module docstring.
API_PUBLIC_BASE_URL = config(
    "API_PUBLIC_BASE_URL", default="https://api.codexng.com"
).rstrip("/")

# --------------------------------------------------------------------------- #
# Payment providers (vs_payments)                                             #
# --------------------------------------------------------------------------- #
# Keys come from the environment; never commit a live one. Each provider is
# optional and raises ProviderNotConfiguredError when used unconfigured.
PAYMENTS_DEFAULT_PROVIDER = config("PAYMENTS_DEFAULT_PROVIDER", default="PAYSTACK")
# Where the hosted checkout returns the payer. Empty on purpose:
# vs_payments.services.default_callback_url() derives it at call time. See
# the module docstring.
PAYMENTS_CALLBACK_URL = config("PAYMENTS_CALLBACK_URL", default="")

# --------------------------------------------------------------------------- #
# Where a PAYING CUSTOMER is sent                                              #
# --------------------------------------------------------------------------- #
# The school apps, not the Console. Scheme and host only; the slug becomes a
# subdomain at call time. See the module docstring.
SCHOOL_APP_BASE_URL = config("SCHOOL_APP_BASE_URL", default="https://xvs.codexng.com")

# Where a payer goes when the platform's own books raised the invoice.
# Empty means the reserved pay. subdomain. See the module docstring.
PLATFORM_PAY_BASE_URL = config("PLATFORM_PAY_BASE_URL", default="")

# The letterhead on invoices and receipts the CodeX platform entity raises
# for its own customers. A school-owned entity uses the school's branding
# instead. The pay-to bank still comes from the entity's own BankAccount.
PLATFORM_ISSUER = {
    "name": config("PLATFORM_ISSUER_NAME", default="CodeX"),
    "tagline": config("PLATFORM_ISSUER_TAGLINE", default=""),
    "address": config("PLATFORM_ISSUER_ADDRESS", default=""),
    "email": config("PLATFORM_ISSUER_EMAIL", default=""),
    "phone": config("PLATFORM_ISSUER_PHONE", default=""),
    "website": config("PLATFORM_ISSUER_WEBSITE", default=""),
    "logo_url": config("PLATFORM_ISSUER_LOGO_URL", default=""),
}

# Paystack - https://api.paystack.co ; Authorization: Bearer <secret_key>.
PAYSTACK_SECRET_KEY = config("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_PUBLIC_KEY = config("PAYSTACK_PUBLIC_KEY", default="")
PAYSTACK_BASE_URL = config("PAYSTACK_BASE_URL", default="https://api.paystack.co")

# Internationalization
# https://docs.djangoproject.com/en/5.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.0/howto/static-files/

STATIC_URL = "/static/"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

# Uploads live in the DATABASE (core.storage.DatabaseStorage): they are all
# small, they survive ephemeral-disk redeploys, and they ride along with DB
# backups. Outgrow it by pointing STORAGES["default"] at S3.
MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")  # unused by DatabaseStorage; kept for tooling

STORAGES = {
    "default": {"BACKEND": "core.storage.DatabaseStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
# Upload ceiling for the DB-backed storage (bytes).
MEDIA_DB_MAX_BYTES = config("MEDIA_DB_MAX_BYTES", default=25 * 1024 * 1024, cast=int)
# Expiry window for a signed /media/ URL, rounded so a file keeps one URL
# while it is open and the browser can cache it. A URL therefore lives one
# to two windows. It bounds an escaped link, not a session.
MEDIA_SIGNED_URL_TTL_SECONDS = config(
    "MEDIA_SIGNED_URL_TTL_SECONDS", default=900, cast=int,
)

STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------- #
# API documentation (drf-spectacular)                                          #
# --------------------------------------------------------------------------- #
# The schema is generated from the code, so it can never go stale. Serve URLs
# are mounted only when API_DOCS_ENABLED (default: on in DEBUG, off otherwise).
API_DOCS_ENABLED = config("API_DOCS_ENABLED", default=None)

SPECTACULAR_SETTINGS = {
    "TITLE": "XVS API (Backend)",
    "VERSION": "2.0.0",
    "DESCRIPTION": (
        "The XVS API is the complete backend API for the X Vision Systems "
        "platform - the single API layer through which all platform "
        "functionality is exposed, consumed by frontend collaborators "
        "building against defined contracts and by backend engineers "
        "extending the platform.\n\n"
        "**Authentication** - all endpoints require a JWT Bearer token issued "
        "at login (`/v1/user/auth/login/`). Unauthenticated requests receive "
        "401; authenticated requests without sufficient permission receive "
        "403. Use the Authorize button with `Bearer <access token>`.\n\n"
        "**Permission model** - access is governed by the two-layer RBAC "
        "system (platform roles for CX staff, school roles for school "
        "users); the required permission is enforced per endpoint.\n\n"
        "**Response envelope** - every response is wrapped in "
        "`{success, message, data}`; list endpoints add a `pagination` "
        "block (`currentPage`, `pageSize`, `totalItems`, `totalPages`, "
        "`next`, `previous`). Errors use `{success: false, message, error}`.\n\n"
        "**School references** - schools are addressed by numeric `id`; "
        "write fields and URL segments that accept a school also accept the "
        "slug, and responses render the slug for backward compatibility."
    ),
    "SCHEMA_PATH_PREFIX": r"/v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    # Hide noisy warnings for plain APIViews without declared serializers -
    # they are still listed, just without typed bodies (annotate over time).
    "DISABLE_ERRORS_AND_WARNINGS": False,
}


# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #
# One JSON object per line, every handler redacting. See the module docstring.
LOG_LEVEL = config("LOG_LEVEL", default="INFO")
LOG_FORMAT = config("LOG_FORMAT", default="json")

#: True while `manage.py test` is running, whichever settings module is in use.
RUNNING_TESTS = "test" in sys.argv

#: How long raw task failure diagnostics are kept. Longer than the 90-day
#: BackgroundJob prune on purpose - see core.models.TaskDiagnostic.
TASK_DIAGNOSTIC_RETENTION_DAYS = config(
    "TASK_DIAGNOSTIC_RETENTION_DAYS", default=400, cast=int,
)

LOGGING = {
    "version": 1,
    # Django's own default configuration is left in place beneath this one;
    # disabling it would silence the request logger and the security logger,
    # which are the two we would miss first.
    "disable_existing_loggers": False,
    "filters": {
        "redact_pii": {
            "()": "core.redaction.RedactingLogFilter",
        },
    },
    "formatters": {
        "json": {
            "()": "core.log_format.JSONFormatter",
        },
        "plain": {
            "format": "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        },
    },
    "handlers": {
        # Silenced under `manage.py test`. See the module docstring.
        "console": {"class": "logging.NullHandler"} if RUNNING_TESTS else {
            "class": "logging.StreamHandler",
            "formatter": "plain" if LOG_FORMAT == "plain" else "json",
            # Applied on the HANDLER rather than on individual loggers, so a
            # logger added anywhere in the codebase tomorrow is covered
            # without anyone remembering to opt in.
            "filters": ["redact_pii"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        # Every SQL statement at DEBUG would defeat the whole point of
        # redacting: parameters are logged as passed.
        "django.db.backends": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}
