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
    path("vendors/", views.VendorListCreateView.as_view(), name="proc-vendors"),
    path("vendors/<int:pk>/", views.VendorDetailView.as_view(), name="proc-vendor-detail"),

    # Purchase requisitions
    path("requisitions/", views.RequisitionListCreateView.as_view(), name="proc-requisitions"),
    path("requisitions/<int:pk>/", views.RequisitionDetailView.as_view(), name="proc-requisition-detail"),
    path("requisitions/<int:pk>/submit/", views.RequisitionSubmitView.as_view(), name="proc-requisition-submit"),
    path("requisitions/<int:pk>/approve/", views.RequisitionApproveView.as_view(), name="proc-requisition-approve"),

    # Purchase orders
    path("purchase-orders/", views.PurchaseOrderListCreateView.as_view(), name="proc-purchase-orders"),
    path("purchase-orders/<int:pk>/", views.PurchaseOrderDetailView.as_view(), name="proc-purchase-order-detail"),

    # Goods received notes
    path("goods-receipts/", views.GoodsReceiptListCreateView.as_view(), name="proc-goods-receipts"),
    path("goods-receipts/<int:pk>/", views.GoodsReceiptDetailView.as_view(), name="proc-goods-receipt-detail"),
    path("goods-receipts/<int:pk>/post/", views.GoodsReceiptPostView.as_view(), name="proc-goods-receipt-post"),

    # Vendor invoices (bills)
    path("vendor-invoices/", views.VendorInvoiceListCreateView.as_view(), name="proc-vendor-invoices"),
    path("vendor-invoices/<int:pk>/", views.VendorInvoiceDetailView.as_view(), name="proc-vendor-invoice-detail"),
    path("vendor-invoices/<int:pk>/match/", views.VendorInvoiceMatchView.as_view(), name="proc-vendor-invoice-match"),
    path("vendor-invoices/<int:pk>/post/", views.VendorInvoicePostView.as_view(), name="proc-vendor-invoice-post"),

    # Vendor payments
    path("vendor-payments/", views.VendorPaymentListCreateView.as_view(), name="proc-vendor-payments"),
    path("vendor-payments/<int:pk>/", views.VendorPaymentDetailView.as_view(), name="proc-vendor-payment-detail"),
    path("vendor-payments/<int:pk>/post/", views.VendorPaymentPostView.as_view(), name="proc-vendor-payment-post"),

    # AP reports
    path("reports/ap-aging/", views.APAgingView.as_view(), name="proc-ap-aging"),
    path("reports/ap-reconciliation/", views.APReconciliationView.as_view(), name="proc-ap-reconciliation"),
    path("reports/grir/", views.GRIRBalanceView.as_view(), name="proc-grir"),
]
