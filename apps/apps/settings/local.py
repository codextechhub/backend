from .base import *

DEBUG = True

ALLOWED_HOSTS = []

# Run Celery tasks synchronously in local dev — no broker needed.
# Zoho SMTP — port 465 + SSL works where 587/TLS is blocked locally.
EMAIL_PORT    = 465
EMAIL_USE_SSL = True
EMAIL_USE_TLS = False

CELERY_TASK_ALWAYS_EAGER     = True
CELERY_TASK_EAGER_PROPAGATES = True

# Frontend URL — must point to the React dev server, not the Django backend
FRONTEND_BASE_URL = 'http://localhost:5173'  # Vite default

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
