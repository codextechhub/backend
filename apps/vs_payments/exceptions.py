"""Domain exceptions for vs_payments.

These extend :class:`vs_finance.exceptions.FinanceError` so they render through the same
typed-exception path (``core.exceptions.custom_exception_handler`` reads ``error_code`` +
``message`` + ``extra`` at ``http_status``) as the rest of the finance stack. The payments
app depends on vs_finance; vs_finance never imports vs_payments.
"""
from __future__ import annotations

from vs_finance.exceptions import FinanceError


class PaymentError(FinanceError):
    error_code = "PAYMENT_ERROR"
    default_message = "A payment error occurred."


class ProviderError(PaymentError):
    """The external PSP returned an error or could not be reached."""

    error_code = "PAYMENT_PROVIDER_ERROR"
    default_message = "The payment provider rejected or failed the request."
    http_status = 502

    def __init__(self, message=None, *, provider=None, provider_code=None, **kwargs):
        self.provider = provider
        self.provider_code = provider_code
        super().__init__(message, provider=provider, provider_code=provider_code, **kwargs)


class ProviderNotConfiguredError(PaymentError):
    """A provider was requested but its keys/host are not configured in settings."""

    error_code = "PAYMENT_PROVIDER_NOT_CONFIGURED"
    default_message = "The payment provider is not configured."
    http_status = 503


class WebhookSignatureError(PaymentError):
    """The inbound webhook signature did not verify against the provider secret."""

    error_code = "WEBHOOK_SIGNATURE_INVALID"
    default_message = "The webhook signature is invalid."
    http_status = 401


class DuplicateWebhookError(PaymentError):
    """A webhook event we have already processed arrived again (idempotency guard).

    This is *not* an error condition for the caller — it means "already handled, do
    nothing" — but it is raised internally so the dispatch path can short-circuit
    without double-booking. The view turns it into a 200 OK acknowledgement.
    """

    error_code = "WEBHOOK_DUPLICATE"
    default_message = "This webhook event has already been processed."
    http_status = 200


class PaymentStateError(PaymentError):
    """An action was attempted from an invalid collection/payout state."""

    error_code = "PAYMENT_STATE_ERROR"
    default_message = "The payment is not in a valid state for this action."
    http_status = 409
