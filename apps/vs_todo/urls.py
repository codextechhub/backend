"""URL routing for vs_todo. Mounted at: path("v1/todo/", include("vs_todo.urls"))"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AssignableView, MineView, OrgView, TaskViewSet, TeamView,
)

router = DefaultRouter()
router.register(r"tasks", TaskViewSet, basename="todo-task")

urlpatterns = [
    path("", include(router.urls)),
    path("dashboard/mine/", MineView.as_view(), name="todo-dashboard-mine"),
    path("dashboard/team/", TeamView.as_view(), name="todo-dashboard-team"),
    path("dashboard/org/",  OrgView.as_view(),  name="todo-dashboard-org"),
    path("assignable/",     AssignableView.as_view(), name="todo-assignable"),
]
