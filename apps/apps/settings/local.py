from .base import *

DEBUG = True

ALLOWED_HOSTS = []

# Dev conveniences — open CORS and the browsable API (both locked down in base).
CORS_ALLOW_ALL_ORIGINS = True
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# Run Celery tasks synchronously in local dev — no broker needed.
# Zoho SMTP — port 465 + SSL works where 587/TLS is blocked locally.
EMAIL_PORT    = 465
EMAIL_USE_SSL = True
EMAIL_USE_TLS = False

CELERY_TASK_ALWAYS_EAGER     = True
CELERY_TASK_EAGER_PROPAGATES = True

# Frontend URL — must point to the React dev server, not the Django backend
FRONTEND_BASE_URL = 'http://localhost:5173'  # Vite default

# PostgreSQL by default — the same engine as staging and CI (B17).
# Set DB_ENGINE=mysql in the environment to fall back to the old MariaDB
# database (kept intact as a safety net; no longer migrated).
if config("DB_ENGINE", default="postgres") == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": "cx_db",
            "USER": "root",
            "PASSWORD": "",
            "HOST": "localhost",
            "PORT": "3306",
            "OPTIONS": {
                "charset": "utf8mb4",
            },
        }
    }
else:
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
