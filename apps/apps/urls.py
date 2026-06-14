"""
URL configuration for apps project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import path, include

from core.views import MediaView

urlpatterns = [
    path("v1/i/", include("vs_schools.urls")),
    path("v1/admin/", include("vs_admin_console.urls")),
    path("v1/user/", include("vs_user.urls")),
    path("v1/rbac/", include("vs_rbac.urls")),
    path("v1/audit/", include("vs_audit.urls")),
    path('v1/config/', include('vs_config.urls')),
    path('v1/notify/', include('vs_notifications.urls')),
    path("v1/import/", include("vs_import_data.urls")),
    path("v1/workflow/", include("vs_workflow.urls")),
    path("v1/finance/", include("vs_finance.urls")),
    path("v1/procurement/", include("vs_procurement.urls")),
    path("v1/payments/", include("vs_payments.urls")),
    path("v1/todo/", include("vs_todo.urls")),
    path("v1/health/", include("vs_health.urls")),
    # path("admin/", admin.site.urls),
]

# Media is database-backed (core.storage.DatabaseStorage) and served with
# authentication in every environment.
urlpatterns += [
    path("media/<path:name>", MediaView.as_view(), name="stored-media"),
]

# API docs — generated from code (drf-spectacular). Enabled in DEBUG by
# default; set API_DOCS_ENABLED=true to expose temporarily on a deployed tier.
_docs_enabled = settings.API_DOCS_ENABLED
if _docs_enabled is None:
    _docs_enabled = settings.DEBUG
if str(_docs_enabled).lower() in ("1", "true", "yes"):
    from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

    urlpatterns += [
        path("v1/schema/", SpectacularAPIView.as_view(), name="api-schema"),
        path("v1/docs/", SpectacularSwaggerView.as_view(url_name="api-schema"), name="api-docs"),
    ]
