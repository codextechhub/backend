from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TicketDashboardView, TicketViewSet

router = DefaultRouter()
router.register(r"tickets", TicketViewSet, basename="ticket")

urlpatterns = [
    path("", include(router.urls)),
    path("dashboard/", TicketDashboardView.as_view(), name="ticket-dashboard"),
]
