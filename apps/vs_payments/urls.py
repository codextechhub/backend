"""URL routes for vs_payments (mounted at /v1/payments/)."""
from django.urls import path

from . import views

urlpatterns = [
    path("collections/", views.CollectionListCreateView.as_view(), name="payments-collections"),
    path("collections/summary/", views.CollectionSummaryView.as_view(), name="payments-collections-summary"),
    path("collections/<int:pk>/", views.CollectionDetailView.as_view(), name="payments-collection-detail"),
    path("virtual-accounts/", views.VirtualAccountListCreateView.as_view(), name="payments-virtual-accounts"),
    path("virtual-accounts/<int:pk>/", views.VirtualAccountDetailView.as_view(), name="payments-virtual-account-detail"),
    path("payouts/", views.PayoutListCreateView.as_view(), name="payments-payouts"),
    path("payouts/summary/", views.PayoutSummaryView.as_view(), name="payments-payouts-summary"),
    path("payout-batches/", views.PayoutBatchListCreateView.as_view(), name="payments-payout-batches"),
    path("payout-batches/summary/", views.PayoutBatchSummaryView.as_view(), name="payments-payout-batches-summary"),
    path("payout-batches/<int:pk>/", views.PayoutBatchDetailView.as_view(), name="payments-payout-batch-detail"),
    path("payout-batches/<int:pk>/submit-for-approval/", views.PayoutBatchSubmitForApprovalView.as_view(),
         name="payments-payout-batch-submit-for-approval"),
    path("reports/settlement-reconciliation/", views.SettlementReconciliationView.as_view(),
         name="payments-settlement-reconciliation"),
    path("transactions/", views.TransactionsLogView.as_view(), name="payments-transactions"),
    path("movements/", views.MovementsView.as_view(), name="payments-movements"),
    path("movements/summary/", views.MovementsSummaryView.as_view(), name="payments-movements-summary"),
    path("webhooks/<str:provider>/", views.WebhookView.as_view(), name="payments-webhook"),
]
