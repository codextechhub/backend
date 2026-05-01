from .base import *

DEBUG = True

ALLOWED_HOSTS = []

# Run Celery tasks synchronously in local dev — no broker needed.
# Zoho SMTP — port 465 + SSL works where 587/TLS is blocked locally.
EMAIL_PORT    = 465
EMAIL_USE_SSL = True
EMAIL_USE_TLS = False

CELERY_TASK_ALWAYS_EAGER     = True
CELERY_TASK_EAGER_PROPAGATES = False

# Frontend URL
FRONTEND_BASE_URL = 'http://127.0.0.1:8000'  # Dev

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
