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
from django.contrib import admin
from django.urls import path, include

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
    # path("admin/", admin.site.urls),
]
