"""URL routes for vs_procurement (mounted at /v1/procurement/).

The Procure-to-Pay surface: master data (vendor categories, vendors) and the document
chain requisition → purchase order → goods receipt → vendor invoice → vendor payment,
each with its lifecycle action endpoints, plus the AP reports. Every endpoint is
entity-scoped via ``?entity=<id|code>``.
"""
from django.urls import path

from . import views

urlpatterns = [
    # Master data
    path("categories/", views.VendorCategoryListCreateView.as_view(), name="proc-categories"),
    path("categories/insights/", views.VendorCategoryInsightsView.as_view(), name="proc-category-insights"),
    path("categories/<int:pk>/", views.VendorCategoryDetailView.as_view(), name="proc-category-detail"),
    path("vendors/", views.VendorListCreateView.as_view(), name="proc-vendors"),
    path("vendors/summary/", views.VendorSummaryView.as_view(), name="proc-vendor-summary"),
    path("vendors/<int:pk>/insights/", views.VendorInsightsView.as_view(), name="proc-vendor-insights"),
    path("vendors/<int:pk>/", views.VendorDetailView.as_view(), name="proc-vendor-detail"),

    # Item catalog
    path("catalog-items/", views.CatalogItemListCreateView.as_view(), name="proc-catalog-items"),
    path("catalog-items/<int:pk>/insights/", views.CatalogItemInsightsView.as_view(), name="proc-catalog-item-insights"),
    path("catalog-items/<int:pk>/", views.CatalogItemDetailView.as_view(), name="proc-catalog-item-detail"),

    # Vendor contracts
    path("contracts/", views.ContractListCreateView.as_view(), name="proc-contracts"),
    path("contracts/summary/", views.ContractSummaryView.as_view(), name="proc-contract-summary"),
    path("contracts/renewals/", views.ContractRenewalsView.as_view(), name="proc-contract-renewals"),
    path("contracts/<int:pk>/", views.ContractDetailView.as_view(), name="proc-contract-detail"),
    path("contracts/<int:pk>/linked-pos/", views.ContractLinkedPurchaseOrdersView.as_view(),
         name="proc-contract-linked-pos"),
    path("contracts/<int:pk>/activate/", views.ContractActivateView.as_view(), name="proc-contract-activate"),
    path("contracts/<int:pk>/renew/", views.ContractRenewView.as_view(), name="proc-contract-renew"),
    path("contracts/<int:pk>/terminate/", views.ContractTerminateView.as_view(), name="proc-contract-terminate"),
    path("contracts/<int:pk>/milestones/<int:milestone_id>/complete/",
         views.ContractMilestoneCompleteView.as_view(), name="proc-contract-milestone-complete"),

    # Purchase requisitions
    path("requisitions/", views.RequisitionListCreateView.as_view(), name="proc-requisitions"),
    path("requisitions/summary/", views.RequisitionSummaryView.as_view(), name="proc-requisition-summary"),
    path("requisitions/budget-availability/", views.RequisitionBudgetAvailabilityView.as_view(), name="proc-requisition-budget"),
    path("requisitions/<int:pk>/", views.RequisitionDetailView.as_view(), name="proc-requisition-detail"),
    path("requisitions/<int:pk>/submit/", views.RequisitionSubmitView.as_view(), name="proc-requisition-submit"),

    # Requests for quotation (sourcing)
    path("rfqs/", views.RfqListCreateView.as_view(), name="proc-rfqs"),
    path("rfqs/summary/", views.RfqSummaryView.as_view(), name="proc-rfq-summary"),
    path("rfqs/<int:pk>/", views.RfqDetailView.as_view(), name="proc-rfq-detail"),
    path("rfqs/<int:pk>/issue/", views.RfqIssueView.as_view(), name="proc-rfq-issue"),
    path("rfqs/<int:pk>/close/", views.RfqCloseView.as_view(), name="proc-rfq-close"),
    path("rfqs/<int:pk>/cancel/", views.RfqCancelView.as_view(), name="proc-rfq-cancel"),

    # Vendor quotations (sourcing)
    path("quotations/", views.QuotationListCreateView.as_view(), name="proc-quotations"),
    path("quotations/<int:pk>/", views.QuotationDetailView.as_view(), name="proc-quotation-detail"),
    path("quotations/<int:pk>/submit/", views.QuotationSubmitView.as_view(), name="proc-quotation-submit"),
    path("quotations/<int:pk>/award/", views.QuotationAwardView.as_view(), name="proc-quotation-award"),

    # Purchase orders
    path("purchase-orders/", views.PurchaseOrderListCreateView.as_view(), name="proc-purchase-orders"),
    path("purchase-orders/summary/", views.PurchaseOrderSummaryView.as_view(), name="proc-purchase-order-summary"),
    path("purchase-orders/<int:pk>/", views.PurchaseOrderDetailView.as_view(), name="proc-purchase-order-detail"),
    path("purchase-orders/<int:pk>/submit/", views.PurchaseOrderSubmitApprovalView.as_view(),
         name="proc-purchase-order-submit"),

    # Goods received notes
    path("goods-receipts/", views.GoodsReceiptListCreateView.as_view(), name="proc-goods-receipts"),
    path("goods-receipts/<int:pk>/", views.GoodsReceiptDetailView.as_view(), name="proc-goods-receipt-detail"),
    path("goods-receipts/<int:pk>/post/", views.GoodsReceiptPostView.as_view(), name="proc-goods-receipt-post"),

    # Vendor invoices (bills)
    path("vendor-invoices/", views.VendorInvoiceListCreateView.as_view(), name="proc-vendor-invoices"),
    path("vendor-invoices/summary/", views.VendorInvoiceSummaryView.as_view(), name="proc-vendor-invoice-summary"),
    path("vendor-invoices/<int:pk>/", views.VendorInvoiceDetailView.as_view(), name="proc-vendor-invoice-detail"),
    path("vendor-invoices/<int:pk>/match/", views.VendorInvoiceMatchView.as_view(), name="proc-vendor-invoice-match"),
    path("vendor-invoices/<int:pk>/submit/", views.VendorInvoiceSubmitApprovalView.as_view(),
         name="proc-vendor-invoice-submit"),
    path("vendor-invoices/<int:pk>/post/", views.VendorInvoicePostView.as_view(), name="proc-vendor-invoice-post"),

    # Vendor payments
    path("vendor-payments/", views.VendorPaymentListCreateView.as_view(), name="proc-vendor-payments"),
    path("vendor-payments/eligible-invoices/", views.VendorPaymentEligibleInvoiceView.as_view(), name="proc-vendor-payment-eligible-invoices"),
    path("vendor-payments/<int:pk>/", views.VendorPaymentDetailView.as_view(), name="proc-vendor-payment-detail"),
    path("vendor-payments/<int:pk>/submit/", views.VendorPaymentSubmitView.as_view(), name="proc-vendor-payment-submit"),
    path("vendor-payments/<int:pk>/post/", views.VendorPaymentPostView.as_view(), name="proc-vendor-payment-post"),
    path("vendor-payments/<int:pk>/cancel/", views.VendorPaymentCancelView.as_view(), name="proc-vendor-payment-cancel"),
    path("vendor-payments/<int:pk>/reverse/", views.VendorPaymentReverseView.as_view(), name="proc-vendor-payment-reverse"),

    # Spend approvals (vs_workflow)
    path("approvals/default-templates/", views.ApprovalTemplateSetupView.as_view(),
         name="proc-approval-default-templates"),
    path("approvals/", views.ProcurementApprovalListView.as_view(),
         name="proc-approvals"),
    path("approvals/<str:workflow_id>/", views.ProcurementApprovalDetailView.as_view(),
         name="proc-approval-detail"),
    path("approvals/<str:workflow_id>/actions/", views.ProcurementApprovalActionView.as_view(),
         name="proc-approval-action"),

    # Inventory / stock ledger
    path("stock-items/", views.StockItemListCreateView.as_view(), name="proc-stock-items"),
    path("stock-items/<int:pk>/", views.StockItemDetailView.as_view(), name="proc-stock-item-detail"),
    path("stock-items/<int:pk>/issue/", views.StockIssueView.as_view(), name="proc-stock-issue"),
    path("stock-items/<int:pk>/adjust/", views.StockAdjustView.as_view(), name="proc-stock-adjust"),
    path("stock-movements/", views.StockMovementListView.as_view(), name="proc-stock-movements"),

    # AP reports
    path("reports/dashboard/", views.ProcurementDashboardView.as_view(), name="proc-dashboard"),
    path("reports/ap-aging/", views.APAgingView.as_view(), name="proc-ap-aging"),
    path("reports/ap-reconciliation/", views.APReconciliationView.as_view(), name="proc-ap-reconciliation"),
    path("reports/grir/", views.GRIRBalanceView.as_view(), name="proc-grir"),
    path("reports/ap-cash-requirements/", views.APCashRequirementsView.as_view(), name="proc-ap-cash-requirements"),
    path("reports/grir-aging/", views.GRIRAgingView.as_view(), name="proc-grir-aging"),
    path("reports/spend-analysis/", views.SpendAnalysisView.as_view(), name="proc-spend-analysis"),
    path("reports/vendor-performance/", views.VendorPerformanceView.as_view(), name="proc-vendor-performance"),
    path("reports/cycle-time/", views.ProcurementCycleTimeView.as_view(), name="proc-cycle-time"),
    path("reports/stock-reorder/", views.StockReorderReportView.as_view(), name="proc-stock-reorder"),
    path("reports/stock-valuation/", views.StockValuationReportView.as_view(), name="proc-stock-valuation"),
]
