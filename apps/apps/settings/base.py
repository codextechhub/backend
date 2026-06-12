"""
Django base settings for apps project.
"""

from datetime import timedelta
import os
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

# SECRET_KEY = "django-insecure-i7@+=ild@90+jm5dew6h%1#rcpmvb0%83j^5$hqvlc2^*hihwd"
# RENDER_API_KEY = "rnd_eA8qL7X50e5Wqtf6bFlpJMiUxMxa"
# TEMP_PASSWORD_PEPPER = "a9f8s7d6g5h4j3k2l1q0w9e8r7t6y5u4i3o2p1z0x9c8v7b6n5m4#@hg!$%^^&*()"

AUTH_USER_MODEL = "vs_user.User"

REST_FRAMEWORK = {
    # JSON only by default — local.py adds the browsable API for development.
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    # JWT auth that also resolves request.school + the thread-local tenant
    # context (Django middleware runs too early to see JWT users).
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "vs_rbac.authentication.TenantJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
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
        # Public barcode-login preview — throttled hard because it confirms
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
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",  # Custom management commands

    # Django-rest framework
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "drf_spectacular",

    # apps
    "vs_schools",
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
    'vs_todo',
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",

    # --- Custom middleware for school context and tenant isolation ---
    'vs_rbac.middleware.TenantContextMiddleware',
    'vs_rbac.middleware.TenantBoundaryEnforcementMiddleware',
    # --- End of custom middleware ---
    
    "django.contrib.messages.middleware.MessageMiddleware",
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

# CORS — locked to known frontend origins (comma-separated env override).
# local.py re-opens this for development servers.
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in config(
        "CORS_ALLOWED_ORIGINS", default="https://intranet.codexng.com"
    ).split(",")
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

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
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "apps.wsgi.application"


# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Email settings — Zoho SMTP (credentials come from environment)
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
# Comma-separated CC addresses added to every outgoing email. Clear to disable.
EMAIL_CC = [
    addr.strip()
    for addr in config("EMAIL_CC", default="").split(",")
    if addr.strip()
]
FRONTEND_BASE_URL = config("FRONTEND_BASE_URL", default="http://localhost:3000")

# --------------------------------------------------------------------------- #
# Payment providers (vs_payments)                                             #
# --------------------------------------------------------------------------- #
# Secrets come from the environment — NEVER commit live keys. Each provider is
# optional; an unconfigured provider raises ProviderNotConfiguredError when used.
# Test/sandbox keys (sk_test_… for Paystack; sandbox host + sandbox keys for OPay)
# are safe to use in non-production. Hosts/paths are overridable so the OPay
# endpoints can be pinned to whatever the merchant dashboard issues without a code
# change. ``PAYMENTS_DEFAULT_PROVIDER`` selects the provider when a caller doesn't.
PAYMENTS_DEFAULT_PROVIDER = config("PAYMENTS_DEFAULT_PROVIDER", default="PAYSTACK")
# A callback URL the hosted checkout returns the payer to after paying.
PAYMENTS_CALLBACK_URL = config(
    "PAYMENTS_CALLBACK_URL", default=f"{FRONTEND_BASE_URL}/payments/return"
)

# Paystack — https://api.paystack.co ; Authorization: Bearer <secret_key>.
PAYSTACK_SECRET_KEY = config("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_PUBLIC_KEY = config("PAYSTACK_PUBLIC_KEY", default="")
PAYSTACK_BASE_URL = config("PAYSTACK_BASE_URL", default="https://api.paystack.co")

# OPay — merchant ID + secret (signing) + public key (bearer for status calls).
# Base URL & paths default to the documented cashier host but stay overridable
# because OPay issues environment-specific hosts/paths per merchant on onboarding.
OPAY_MERCHANT_ID = config("OPAY_MERCHANT_ID", default="")
OPAY_SECRET_KEY = config("OPAY_SECRET_KEY", default="")
OPAY_PUBLIC_KEY = config("OPAY_PUBLIC_KEY", default="")
OPAY_BASE_URL = config("OPAY_BASE_URL", default="https://api.opaycheckout.com")
OPAY_CREATE_PATH = config("OPAY_CREATE_PATH", default="/api/v1/international/cashier/create")
OPAY_STATUS_PATH = config("OPAY_STATUS_PATH", default="/api/v1/international/cashier/status")
OPAY_TRANSFER_PATH = config("OPAY_TRANSFER_PATH", default="/api/v1/international/transfer/toBank")
OPAY_TRANSFER_STATUS_PATH = config(
    "OPAY_TRANSFER_STATUS_PATH", default="/api/v1/international/transfer/status"
)

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
# small — so uploads live in the DATABASE (core.storage.DatabaseStorage).
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
        "platform — the single API layer through which all platform "
        "functionality is exposed, consumed by frontend collaborators "
        "building against defined contracts and by backend engineers "
        "extending the platform.\n\n"
        "**Authentication** — all endpoints require a JWT Bearer token issued "
        "at login (`/v1/user/auth/login/`). Unauthenticated requests receive "
        "401; authenticated requests without sufficient permission receive "
        "403. Use the Authorize button with `Bearer <access token>`.\n\n"
        "**Permission model** — access is governed by the two-layer RBAC "
        "system (platform roles for CX staff, school roles for school "
        "users); the required permission is enforced per endpoint.\n\n"
        "**Response envelope** — every response is wrapped in "
        "`{success, message, data}`; list endpoints add a `pagination` "
        "block (`currentPage`, `pageSize`, `totalItems`, `totalPages`, "
        "`next`, `previous`). Errors use `{success: false, message, error}`.\n\n"
        "**School references** — schools are addressed by numeric `id`; "
        "write fields and URL segments that accept a school also accept the "
        "slug, and responses render the slug for backward compatibility."
    ),
    "SCHEMA_PATH_PREFIX": r"/v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    # Hide noisy warnings for plain APIViews without declared serializers —
    # they are still listed, just without typed bodies (annotate over time).
    "DISABLE_ERRORS_AND_WARNINGS": False,
}
