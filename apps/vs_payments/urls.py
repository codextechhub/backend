"""URL routes for vs_payments (mounted at /v1/payments/)."""
from django.urls import path

from . import views

urlpatterns = [
    path("collections/", views.CollectionListCreateView.as_view(), name="payments-collections"),
    path("collections/<int:pk>/", views.CollectionDetailView.as_view(), name="payments-collection-detail"),
    path("virtual-accounts/", views.VirtualAccountCreateView.as_view(), name="payments-virtual-accounts"),
    path("payouts/", views.PayoutListCreateView.as_view(), name="payments-payouts"),
    path("webhooks/<str:provider>/", views.WebhookView.as_view(), name="payments-webhook"),
]
