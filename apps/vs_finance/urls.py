"""URL routes for the vs_finance REST API (mounted at ``/v1/finance/``).

Two families: entity-scoped master-data / document lists + actions, and the
financial-statement reports. Every endpoint expects ``?entity=<id or code>``.
"""
from django.urls import path

from .views import (
    AccountListView,
    ARAgingView,
    ARReconciliationView,
    BalanceSheetView,
    CashFlowView,
    EntityListView,
    FiscalPeriodListView,
    IncomeStatementView,
    InvoiceListView,
    JournalEntryDetailView,
    JournalEntryListView,
    JournalPostView,
    JournalReverseView,
    PeriodCloseView,
    TrialBalanceView,
)

urlpatterns = [
    # Master data + documents
    path("entities/", EntityListView.as_view(), name="finance-entity-list"),
    path("accounts/", AccountListView.as_view(), name="finance-account-list"),
    path("periods/", FiscalPeriodListView.as_view(), name="finance-period-list"),
    path("journals/", JournalEntryListView.as_view(), name="finance-journal-list"),
    path("journals/<int:id>/", JournalEntryDetailView.as_view(), name="finance-journal-detail"),
    path("invoices/", InvoiceListView.as_view(), name="finance-invoice-list"),

    # Actions
    path("journals/<int:id>/post/", JournalPostView.as_view(), name="finance-journal-post"),
    path("journals/<int:id>/reverse/", JournalReverseView.as_view(), name="finance-journal-reverse"),
    path("periods/<int:id>/close/", PeriodCloseView.as_view(), name="finance-period-close"),

    # Reports / financial statements
    path("reports/trial-balance/", TrialBalanceView.as_view(), name="finance-trial-balance"),
    path("reports/income-statement/", IncomeStatementView.as_view(), name="finance-income-statement"),
    path("reports/balance-sheet/", BalanceSheetView.as_view(), name="finance-balance-sheet"),
    path("reports/cash-flow/", CashFlowView.as_view(), name="finance-cash-flow"),
    path("reports/ar-aging/", ARAgingView.as_view(), name="finance-ar-aging"),
    path("reports/ar-reconciliation/", ARReconciliationView.as_view(), name="finance-ar-reconciliation"),
]
