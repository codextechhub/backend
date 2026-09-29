from django.urls import path

from .views.events import EventDetailView, EventListCreateView
from .views.reads import CurrentView, OverviewView, YearView
from .views.settings import CalendarRulesView

urlpatterns = [
    path("events/", EventListCreateView.as_view(), name="calendar-event-list"),
    path("events/<int:pk>/", EventDetailView.as_view(), name="calendar-event-detail"),

    path("current/", CurrentView.as_view(), name="calendar-current"),
    path("year/", YearView.as_view(), name="calendar-year"),
    path("overview/", OverviewView.as_view(), name="calendar-overview"),
    path("rules/", CalendarRulesView.as_view(), name="calendar-rules"),
]
