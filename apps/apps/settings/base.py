"""
Django base settings for apps project.
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

# Three live secrets sat here as commented-out fallbacks, and commenting a
# secret out does not unpublish it: they are in the committed history of this
# repository and in every clone of it. All three are to be rotated at their
# source (Render dashboard for the API key and the env group for the other
# two) rather than merely deleted here.
#
# Removing the lines is still worth doing. It stops the next person copying
# them into a shell, and it stops a local run silently succeeding against a
# production credential when the env var is missing - which is precisely how
# a fallback secret gets used in anger.

AUTH_USER_MODEL = "vs_user.User"

# auth.E003 says USERNAME_FIELD must be globally unique. It deliberately is
# not: one real address can be a login at more than one customer of this
# platform (a parent with a child at two schools), so uniqueness lives on
# vs_user.User's uq_user_email_per_tenant instead - see the per-tenant email
# work in vs_user/models.py and migration 0007.
#
# The check exists because django.contrib.auth's ModelBackend resolves a login
# with get_by_natural_key(), a bare .get() on the username field, which would
# raise MultipleObjectsReturned. Nothing here goes through it: requests
# authenticate with JWT via vs_rbac.authentication, LoginService checks the
# password itself against a tenant-scoped lookup, and django.contrib.admin -
# the other ModelBackend caller - is deliberately absent from INSTALLED_APPS.
#
# Silenced in base, not per environment: the design decision is the same in
# development, CI, staging and production. Environment files that add their own
# entries must extend this list rather than replace it.
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
        # nothing else. The gate sits in the defaults so a view that declares
        # no permission_classes at all is closed to it rather than open by
        # omission; views that set their own list get the same check through
        # IsAuthenticatedAndActive / HasRBACPermission.
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
    # django.contrib.admin and django.contrib.messages are deliberately absent.
    # This is an API serving its own console: the admin site was never routed,
    # no app ships an admin.py, and messages is a server-rendered flash
    # framework that nothing here imports - the frontend owns its own toasts.
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
    # M9. School-specific by construction, so it sits beside vs_schools under
    # apps/schools/ and never beside the domain-neutral engines.
    "schools.vs_onboarding",
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
# Every school is served from its own subdomain (bright-star.xvs.codexng.com),
# so the browser origin differs per tenant and cannot be enumerated in advance.
# The pattern matches one label only - it does not reach a.b.xvs.codexng.com -
# and the bare product site keeps its explicit entry above.
# django-cors-headers echoes the specific matched origin rather than "*", which
# is what keeps this legal alongside CORS_ALLOW_CREDENTIALS.
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

# Canonical policy = 12 chars + uppercase + lowercase + digit + special
# (PasswordComplexityValidator, the single source of truth in
# vs_user/password_policy.py), plus not-common and not-similar-to-user-info.
# MinimumLength/Numeric are dropped - the complexity validator subsumes both.
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
# Monitoring copies are BCC, never CC.
#
# Every list below is an internal mailbox we copy so somebody can see what went
# out. Copying it visibly put internal addresses in front of customers and vendors,
# showed each recipient that their mail is monitored, and handed anyone who hits
# reply-all a route into an internal inbox. None of that was intended; CC was simply
# the first thing reached for. BCC delivers the same copy without any of it.
#
# Each reads its old CC environment variable as the fallback default, so a
# deployment that has not renamed its variables keeps the addresses it had.
EMAIL_BCC = [
    addr.strip()
    for addr in config("EMAIL_BCC", default=config("EMAIL_CC", default="")).split(",")
    if addr.strip()
]
# Procurement messages sent to external vendors use a narrower list so monitoring
# does not copy unrelated platform email into the procurement inbox.
PROCUREMENT_VENDOR_EMAIL_BCC = [
    addr.strip()
    for addr in config(
        "PROCUREMENT_VENDOR_EMAIL_BCC",
        default=config("PROCUREMENT_VENDOR_EMAIL_CC", default="backend-test@codexng.com"),
    ).split(",")
    if addr.strip()
]
# Finance documents sent to paying customers (invoice, receipt, statement) copy a
# finance-owned mailbox rather than the platform-wide EMAIL_BCC, for the same reason
# procurement narrows its own: a customer document is not general platform mail.
FINANCE_CUSTOMER_EMAIL_BCC = [
    addr.strip()
    for addr in config(
        "FINANCE_CUSTOMER_EMAIL_BCC",
        default=config("FINANCE_CUSTOMER_EMAIL_CC", default="backend-test@codexng.com"),
    ).split(",")
    if addr.strip()
]
FRONTEND_BASE_URL = config("FRONTEND_BASE_URL", default="http://localhost:3000")

# Public targets used by the platform-health synthetic probes. Keep these
# configurable per environment; the defaults are the production API domain.
HEALTH_PROBE_BASE_URL = config(
    "HEALTH_PROBE_BASE_URL", default="https://api.codexng.com"
).rstrip("/")
HEALTH_SSL_DOMAIN = config("HEALTH_SSL_DOMAIN", default="api.codexng.com")

# --------------------------------------------------------------------------- #
# Payment providers (vs_payments)                                             #
# --------------------------------------------------------------------------- #
# Secrets come from the environment - NEVER commit live keys. Each provider is
# optional; an unconfigured provider raises ProviderNotConfiguredError when used.
# Test/sandbox keys (sk_test_… for Paystack) are safe to use in non-production.
# ``PAYMENTS_DEFAULT_PROVIDER`` selects the provider when a caller doesn't.
PAYMENTS_DEFAULT_PROVIDER = config("PAYMENTS_DEFAULT_PROVIDER", default="PAYSTACK")
# A callback URL the hosted checkout returns the payer to after paying.
PAYMENTS_CALLBACK_URL = config(
    "PAYMENTS_CALLBACK_URL", default=f"{FRONTEND_BASE_URL}/payments/return"
)

# Platform (CodeX) issuer identity - the letterhead printed on invoices/receipts the
# CodeX *platform* entity raises for its own customers (the schools). School-owned
# entities take their letterhead from the school's own branding instead; this is only
# the fallback identity for the platform books. The pay-to bank still comes from the
# platform entity's primary collection BankAccount. Configure per environment.
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

# Media: the platform only receives import spreadsheets and images, all
# small - so uploads live in the DATABASE (core.storage.DatabaseStorage).
# They survive ephemeral-disk redeploys, ride along with DB backups, and are
# served with authentication by core.views.MediaView at /media/<name>.
# Outgrow it? Point STORAGES["default"] at S3 and migrate the rows.
MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")  # unused by DatabaseStorage; kept for tooling

STORAGES = {
    "default": {"BACKEND": "core.storage.DatabaseStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
# Upload ceiling for the DB-backed storage (bytes).
MEDIA_DB_MAX_BYTES = config("MEDIA_DB_MAX_BYTES", default=25 * 1024 * 1024, cast=int)

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
# There was no LOGGING block at all until now, which meant Django's implicit
# default applied: app loggers propagated to a root with no handler, so
# anything below WARNING vanished and everything above it reached stderr in
# whatever shape the caller happened to write it. On Render that stderr is the
# only log there is, it is unstructured, and it is dropped after roughly a
# week.
#
# Two things are fixed here, and they are separate:
#
# 1. **Shape.** Records are emitted as one JSON object per line, so the stream
#    can be searched, filtered by tenant or task, and shipped to a log service
#    later without touching a single call site. ``LOG_FORMAT=plain`` restores
#    human-readable output for local work.
#
# 2. **Content.** Every handler carries ``core.redaction.RedactingLogFilter``.
#    The same guardian email that this change stops writing to
#    ``BackgroundJob.error`` reaches the log stream by a completely separate
#    route - a dozen ``logger.warning(..., exc_info=True)`` calls, plus
#    Celery's own traceback printing - and scrubbing only the database would
#    have moved the leak rather than closed it.
#
# The unredacted traceback is not lost: ``core.models.TaskDiagnostic`` holds
# it for 400 days behind ``platform.tasks.view_sensitive``, with every read
# audited. That table, not this stream, is where an investigation into last
# quarter's failure goes.
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
        # Under `manage.py test` nothing is written to the terminal at all.
        # The suite drives 401s, 403s and 404s by the dozen on purpose and
        # django.request logs every one at WARNING, which put a line between
        # every pair of dots and buried the one line that matters when
        # something actually failed. It lives here rather than in
        # settings/test.py because the suite is run on local.py and on ci.py as
        # well - see CLAUDE.md, "Running the test suite on this machine" - so
        # this is the only place the rule holds for all three.
        #
        # A test that cares what was logged should use assertLogs, which
        # attaches its own handler and is unaffected by this.
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
