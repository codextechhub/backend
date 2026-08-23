"""REST API for emailing a finance document to a customer (mounted at ``/v1/finance/``).

Three documents, one shape. ``GET`` previews what a send would do - the addresses, the
CC and the subject - so nobody puts a document in a customer's inbox without seeing
where it is going. ``POST`` sends it. A fourth pair lists the delivery history for a
document and retries a failed attempt.

Every view resolves its subject **within the caller's entity** (``?entity=``), so an id
from another entity's books is a 404 rather than a send. That is the only tenant
boundary that matters here: an email endpoint that leaked across entities would post
one customer's figures to another's address.

The services in :mod:`vs_finance.document_email` own recipients, rendering, queueing
and outcome; these views only resolve, authorize and translate.
"""
from __future__ import annotations

import datetime

from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from core.response import success_response

from .constants import FinanceDeliveryDocument
from .document_email import preview as build_preview
from .document_email import retry as retry_delivery
from .document_email import send_invoice, send_receipt, send_statement
from .models import FinanceDocumentDelivery, Invoice, Payment
from .serializers import FinanceDocumentDeliverySerializer
from .views import resolve_entity
from .views_ops.base import _FinanceBase

# Which permission key gates each document type. Reading a document's delivery
# history needs the same grant as sending it: both answer "what has this customer
# been told", which is not something a plain viewer should be able to enumerate.
EMAIL_PERMISSION = {
    FinanceDeliveryDocument.INVOICE: "finance.invoice.email",
    FinanceDeliveryDocument.RECEIPT: "finance.payment.email",
    FinanceDeliveryDocument.STATEMENT: "finance.customer.email_statement",
}


def _date(raw, field):
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(str(raw))
    except ValueError:
        raise ValidationError({field: "Use an ISO date (YYYY-MM-DD)."})


def _note(request) -> str:
    return (request.data or {}).get("note", "") or (request.data or {}).get("message", "")


class _DocumentEmailBase(_FinanceBase):
    """Resolve one emailable document inside the caller's entity."""

    document_type = None

    @property
    def rbac_permission(self):
        return EMAIL_PERMISSION[self.document_type]

    def resolve(self, request):  # pragma: no cover - implemented per document
        raise NotImplementedError

    def deliveries(self, entity, subject):
        return FinanceDocumentDelivery.objects.filter(
            entity=entity, document_type=self.document_type, document_id=str(subject.pk),
        ).select_related("customer", "requested_by")

    # Handle GET requests for this endpoint.
    def get(self, request, pk):
        """Preview the send and list what has already gone out."""
        entity, subject, customer = self.resolve(request, pk)
        data = build_preview(
            customer=customer, document_type=self.document_type, entity=entity,
            document_number=getattr(subject, "document_number", ""),
        )
        data["deliveries"] = FinanceDocumentDeliverySerializer(
            self.deliveries(entity, subject), many=True,
        ).data
        return success_response("Email preview retrieved.", data=data)


class InvoiceEmailView(_DocumentEmailBase):
    """GET/POST /finance/invoices/<pk>/email/ - preview or email an invoice to its customer.

    docstring-name: Email an invoice
    """

    document_type = FinanceDeliveryDocument.INVOICE

    def resolve(self, request, pk):
        entity = resolve_entity(request)
        invoice = Invoice.objects.filter(entity=entity, pk=pk).select_related("customer").first()
        if invoice is None:
            raise NotFound("Invoice not found for this entity.")
        return entity, invoice, invoice.customer

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        _entity, invoice, _customer = self.resolve(request, pk)
        delivery = send_invoice(invoice, actor_user=request.user, note=_note(request))
        return success_response(
            f"Invoice {invoice.document_number} sent to {', '.join(delivery.recipients)}.",
            data=FinanceDocumentDeliverySerializer(delivery).data,
        )


class PaymentEmailView(_DocumentEmailBase):
    """GET/POST /finance/payments/<pk>/email/ - preview or email a receipt to its customer.

    docstring-name: Email a receipt
    """

    document_type = FinanceDeliveryDocument.RECEIPT

    def resolve(self, request, pk):
        entity = resolve_entity(request)
        payment = Payment.objects.filter(entity=entity, pk=pk).select_related("customer").first()
        if payment is None:
            raise NotFound("Receipt not found for this entity.")
        return entity, payment, payment.customer

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        _entity, payment, _customer = self.resolve(request, pk)
        delivery = send_receipt(payment, actor_user=request.user, note=_note(request))
        return success_response(
            f"Receipt {payment.document_number} sent to {', '.join(delivery.recipients)}.",
            data=FinanceDocumentDeliverySerializer(delivery).data,
        )


class CustomerStatementEmailView(_DocumentEmailBase):
    """GET/POST /finance/customers/<pk>/statement-email/ - preview or email a statement.

    ``?start=`` / ``?end=`` (GET) or ``start``/``end`` (POST) bound the period, matching
    the statement report's own parameters so the customer receives what the screen shows.

    docstring-name: Email a customer statement
    """

    document_type = FinanceDeliveryDocument.STATEMENT

    def resolve(self, request, pk):
        # Customers are addressed by code or id everywhere else on this surface
        # (``customers/<str:pk>/``), so the same reference has to work here.
        from .views_ar import _resolve_customer

        entity = resolve_entity(request)
        customer = _resolve_customer(entity, pk)
        return entity, customer, customer

    def deliveries(self, entity, subject):
        # A statement has no document id, so its history is per customer.
        return FinanceDocumentDelivery.objects.filter(
            entity=entity, document_type=self.document_type, customer=subject,
        ).select_related("customer", "requested_by")

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        _entity, customer, _ = self.resolve(request, pk)
        body = request.data or {}
        delivery = send_statement(
            customer, actor_user=request.user,
            start_date=_date(body.get("start"), "start"),
            end_date=_date(body.get("end"), "end"),
            note=_note(request),
        )
        return success_response(
            f"Statement for {customer.name} sent to {', '.join(delivery.recipients)}.",
            data=FinanceDocumentDeliverySerializer(delivery).data,
        )


class FinanceDeliveryRetryView(_FinanceBase):
    """POST /finance/document-deliveries/<pk>/retry/ - try a failed delivery again.

    A retry needs the same grant as the original send, and which grant that is depends
    on the row. Rather than resolve the delivery inside permission evaluation - which
    would run a query and an entity resolution before the request is authorized at all
    - the class-level gate is any-of the three send keys, and the exact key is then
    checked against the resolved row. Holding one send key is never enough to re-send
    a document of a kind you cannot send.

    docstring-name: Retry a document email
    """

    rbac_permission = list(EMAIL_PERMISSION.values())

    # Handle POST requests for this endpoint.
    def post(self, request, pk):
        from vs_rbac.permissions import is_vision_super_admin, user_has_rbac_permission

        entity = resolve_entity(request)
        delivery = FinanceDocumentDelivery.objects.filter(
            entity=entity, pk=pk,
        ).select_related("customer", "entity__tenant").first()
        if delivery is None:
            raise NotFound("Delivery not found for this entity.")

        required = EMAIL_PERMISSION[delivery.document_type]
        if not is_vision_super_admin(request.user) and not user_has_rbac_permission(
            request.user, required, tenant=entity.tenant,
        ):
            raise PermissionDenied(
                f"You do not have permission to email a "
                f"{delivery.get_document_type_display().lower()} to a customer."
            )

        new_delivery = retry_delivery(delivery, actor_user=request.user, note=_note(request) or None)
        return success_response(
            f"Retrying delivery to {', '.join(new_delivery.recipients)}.",
            data=FinanceDocumentDeliverySerializer(new_delivery).data,
        )
