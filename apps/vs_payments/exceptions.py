"""Domain exceptions for vs_payments.  # Payment-specific exception types.

These extend :class:`vs_finance.exceptions.FinanceError` so they render through the same
typed-exception path (``core.exceptions.custom_exception_handler`` reads ``error_code`` +
``message`` + ``extra`` at ``http_status``) as the rest of the finance stack. The payments
app depends on vs_finance; vs_finance never imports vs_payments.  # Keep the error contract aligned with finance.
"""
from __future__ import annotations  # Defer annotation evaluation for forward references.

from vs_finance.exceptions import FinanceError  # Base finance exception used across the stack.


class PaymentError(FinanceError):
    error_code = "PAYMENT_ERROR"  # Generic payment-layer error code.
    default_message = "A payment error occurred."  # Fallback message when none is supplied.


class ProviderError(PaymentError):
    """The external PSP returned an error or could not be reached."""

    error_code = "PAYMENT_PROVIDER_ERROR"  # Provider request failed or was rejected.
    default_message = "The payment provider rejected or failed the request."  # Default provider failure message.
    http_status = 502  # Surface provider failures as bad gateway.

    def __init__(self, message=None, *, provider=None, provider_code=None, **kwargs):
        self.provider = provider  # Store the provider name for downstream handling.
        self.provider_code = provider_code  # Store the provider-specific error code, if any.
        super().__init__(message, provider=provider, provider_code=provider_code, **kwargs)  # Pass context to FinanceError.


class ProviderNotConfiguredError(PaymentError):
    """A provider was requested but its keys/host are not configured in settings."""

    error_code = "PAYMENT_PROVIDER_NOT_CONFIGURED"  # Settings are missing required provider config.
    default_message = "The payment provider is not configured."  # Default configuration error message.
    http_status = 503  # Service unavailable until configuration is supplied.


class WebhookSignatureError(PaymentError):
    """The inbound webhook signature did not verify against the provider secret."""

    error_code = "WEBHOOK_SIGNATURE_INVALID"  # Signature verification failed.
    default_message = "The webhook signature is invalid."  # Default message for rejected webhook signatures.
    http_status = 401  # Invalid signature should be treated as unauthorized.


class DuplicateWebhookError(PaymentError):
    """A webhook event we have already processed arrived again (idempotency guard).

    This is *not* an error condition for the caller — it means "already handled, do
    nothing" — but it is raised internally so the dispatch path can short-circuit
    without double-booking. The view turns it into a 200 OK acknowledgement.
    """

    error_code = "WEBHOOK_DUPLICATE"  # Event was already handled.
    default_message = "This webhook event has already been processed."  # Informational duplicate message.
    http_status = 200  # Duplicate webhooks are acknowledged successfully.


class PaymentStateError(PaymentError):
    """An action was attempted from an invalid collection/payout state."""

    error_code = "PAYMENT_STATE_ERROR"  # The requested action does not match the current payment state.
    default_message = "The payment is not in a valid state for this action."  # Default invalid-state message.
    http_status = 409  # Conflicts reflect invalid state transitions.
