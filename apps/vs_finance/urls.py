"""URL routes for the vs_finance REST API (mounted at ``/v1/finance/``).

Families: entity-scoped master-data / document lists + actions, the financial-statement
reports, and the operational/setup surface (reference data, banking, expense claims,
payroll, budgets, fixed assets, audit) wired from :mod:`vs_finance.views_ops`. Every
endpoint expects ``?entity=<id or code>`` — except the **global** currency/FX routes.
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
from .views_ops import (
    BankAccountDetailView,
    BankAccountListCreateView,
    BankAutoReconcileView,
    BankStatementLineAdjustView,
    BankStatementLineMatchView,
    BankStatementLineView,
    BudgetApproveView,
    BudgetDetailView,
    BudgetLineCreateView,
    BudgetListCreateView,
    BudgetVarianceView,
    CostCenterListCreateView,
    CurrencyListCreateView,
    DimensionListCreateView,
    ExpenseClaimDetailView,
    ExpenseClaimListCreateView,
    ExpenseClaimPostView,
    ExpenseClaimSettleView,
    FinanceAuditLogListView,
    FixedAssetAcquireView,
    FixedAssetDepreciateView,
    FixedAssetDetailView,
    FixedAssetListCreateView,
    FxRateListCreateView,
    PayrollRunDetailView,
    PayrollRunListCreateView,
    PayrollRunPayView,
    PayrollRunPostView,
    TaxCodeListCreateView,
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

    # Setup / reference data (currencies + FX rates are GLOBAL — no ?entity)
    path("currencies/", CurrencyListCreateView.as_view(), name="finance-currency-list"),
    path("fx-rates/", FxRateListCreateView.as_view(), name="finance-fxrate-list"),
    path("tax-codes/", TaxCodeListCreateView.as_view(), name="finance-taxcode-list"),
    path("cost-centers/", CostCenterListCreateView.as_view(), name="finance-costcenter-list"),
    path("dimensions/", DimensionListCreateView.as_view(), name="finance-dimension-list"),

    # Banking + reconciliation
    path("bank-accounts/", BankAccountListCreateView.as_view(), name="finance-bank-list"),
    path("bank-accounts/<int:pk>/", BankAccountDetailView.as_view(), name="finance-bank-detail"),
    path("bank-accounts/<int:pk>/statement-lines/", BankStatementLineView.as_view(),
         name="finance-bank-statement-lines"),
    path("bank-accounts/<int:pk>/auto-reconcile/", BankAutoReconcileView.as_view(),
         name="finance-bank-auto-reconcile"),
    path("statement-lines/<int:pk>/match/", BankStatementLineMatchView.as_view(),
         name="finance-statement-line-match"),
    path("statement-lines/<int:pk>/adjust/", BankStatementLineAdjustView.as_view(),
         name="finance-statement-line-adjust"),

    # Expense claims
    path("expense-claims/", ExpenseClaimListCreateView.as_view(), name="finance-expense-list"),
    path("expense-claims/<int:pk>/", ExpenseClaimDetailView.as_view(), name="finance-expense-detail"),
    path("expense-claims/<int:pk>/post/", ExpenseClaimPostView.as_view(), name="finance-expense-post"),
    path("expense-claims/<int:pk>/settle/", ExpenseClaimSettleView.as_view(), name="finance-expense-settle"),

    # Payroll
    path("payroll-runs/", PayrollRunListCreateView.as_view(), name="finance-payroll-list"),
    path("payroll-runs/<int:pk>/", PayrollRunDetailView.as_view(), name="finance-payroll-detail"),
    path("payroll-runs/<int:pk>/post/", PayrollRunPostView.as_view(), name="finance-payroll-post"),
    path("payroll-runs/<int:pk>/pay/", PayrollRunPayView.as_view(), name="finance-payroll-pay"),

    # Budgets
    path("budgets/", BudgetListCreateView.as_view(), name="finance-budget-list"),
    path("budgets/<int:pk>/", BudgetDetailView.as_view(), name="finance-budget-detail"),
    path("budgets/<int:pk>/lines/", BudgetLineCreateView.as_view(), name="finance-budget-line"),
    path("budgets/<int:pk>/approve/", BudgetApproveView.as_view(), name="finance-budget-approve"),
    path("budgets/<int:pk>/variance/", BudgetVarianceView.as_view(), name="finance-budget-variance"),

    # Fixed assets
    path("fixed-assets/", FixedAssetListCreateView.as_view(), name="finance-asset-list"),
    path("fixed-assets/<int:pk>/", FixedAssetDetailView.as_view(), name="finance-asset-detail"),
    path("fixed-assets/<int:pk>/acquire/", FixedAssetAcquireView.as_view(), name="finance-asset-acquire"),
    path("fixed-assets/<int:pk>/depreciate/", FixedAssetDepreciateView.as_view(),
         name="finance-asset-depreciate"),

    # Audit trail
    path("audit-logs/", FinanceAuditLogListView.as_view(), name="finance-audit-list"),
]
