from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    GuideAnalyticsEventView,
    GuideAnalyticsSummaryView,
    TicketDashboardView,
    TicketViewSet,
)

router = DefaultRouter()
router.register(r"tickets", TicketViewSet, basename="ticket")

urlpatterns = [
    path("", include(router.urls)),
    path("dashboard/", TicketDashboardView.as_view(), name="ticket-dashboard"),
    path("guides/analytics/events/", GuideAnalyticsEventView.as_view(), name="guide-analytics-event"),
    path("guides/analytics/summary/", GuideAnalyticsSummaryView.as_view(), name="guide-analytics-summary"),
]
