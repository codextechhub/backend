"""Refusals the pupil portal raises, each written for the pupil reading it."""


class PortalError(Exception):
    error_code = "PORTAL_ERROR"
    default_message = "That could not be done."
    http_status = 400

    def __init__(self, message: str = "", **extra):
        self.message = message or self.default_message
        self.extra = extra
        super().__init__(self.message)


class ProfileFieldInvalid(PortalError):
    """A contact detail the pupil typed that cannot be stored as typed."""

    error_code = "PROFILE_FIELD_INVALID"
    http_status = 400


class PhotoSelfEditOff(PortalError):
    error_code = "PHOTO_SELF_EDIT_OFF"
    default_message = (
        "Your school changes student photographs itself. Ask the school office "
        "if yours needs updating."
    )
    http_status = 403


class StudentPaymentOff(PortalError):
    error_code = "STUDENT_PAYMENT_OFF"
    default_message = (
        "Your school takes fee payments from your parent or guardian. Ask them "
        "to pay through their own app, or speak to the school office."
    )
    http_status = 403


class PaymentAmountInvalid(PortalError):
    """An amount the school would not take; ``error_code`` says which rule."""

    error_code = "PAYMENT_AMOUNT_INVALID"
    http_status = 400


class AmountBelowMinimum(PaymentAmountInvalid):
    error_code = "AMOUNT_BELOW_MINIMUM"


class AmountAboveBalance(PaymentAmountInvalid):
    error_code = "AMOUNT_ABOVE_BALANCE"


class InvoiceNotPayable(PortalError):
    error_code = "INVOICE_NOT_PAYABLE"
    default_message = "This invoice cannot be paid right now. It may already be paid."
    http_status = 409


class FinanceUnavailable(PortalError):
    error_code = "FINANCE_UNAVAILABLE"
    default_message = "Your fees cannot be shown right now. Try again in a few minutes."
    http_status = 503


class PaymentProviderUnavailable(PortalError):
    error_code = "PROVIDER_UNAVAILABLE"
    default_message = (
        "The payment service is not answering right now. Nothing has been "
        "charged. Try again in a few minutes."
    )
    http_status = 503
