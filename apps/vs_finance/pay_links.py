"""Public "pay this invoice" links: a signed token that mints a checkout on click.

An invoice email carries a link to a public pay page rather than a hosted checkout
URL, and the difference matters. A checkout minted when the email is *sent* freezes
the amount at that moment: a customer who pays part of the invoice by bank transfer
a week later, then opens the same email, would be charged the original full amount
and have to be refunded. Hosted checkout sessions also expire, so a link sent on the
1st is often dead by the time somebody clicks it on the 20th.

So the email carries a token that identifies the invoice and nothing else. The amount
and whether the invoice is payable at all are decided when the payer clicks, against
the invoice as it stands right then.

The token is an HMAC-signed payload (``django.core.signing``), so nothing guessable or
enumerable is exposed and no revocation column is needed: the invoice's own state is
the gate. A cancelled or fully settled invoice stops being payable the moment it
changes, however many copies of the link are in circulation.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone
from rest_framework.exceptions import NotFound

from .constants import DocumentStatus
from .exceptions import FinanceError
from .models import Invoice
from .money import format_naira

#: Salt for the invoice pay token. Changing it invalidates every link in circulation.
TOKEN_SALT = "vs_finance.invoice_pay"

#: A checkout started within this window is handed back instead of minting another.
#: It absorbs the ordinary double-click / back-button / refresh, which would otherwise
#: leave a trail of abandoned intents and, worse, two live ways to pay the same money.
#: Short enough that the reused session has not expired at the provider.
CHECKOUT_REUSE_MINUTES = 15


class InvoiceNotPayableError(FinanceError):
    """The link is genuine but the invoice cannot take a payment right now."""

    error_code = "INVOICE_NOT_PAYABLE"
    default_message = "This invoice is not open for payment."
    http_status = 409


# --------------------------------------------------------------------------- #
# Token                                                                        #
# --------------------------------------------------------------------------- #

def make_invoice_pay_token(invoice) -> str:
    """Sign a link that identifies one invoice and carries no other authority."""
    return signing.dumps({"invoice": invoice.pk}, salt=TOKEN_SALT, compress=True)


def invoice_pay_url(invoice) -> str:
    """The public pay page for this invoice, or "" when no frontend is configured.

    Read at call time, never cached at import: the deployment that renders the email
    is the one that knows its own domain.
    """
    base = str(getattr(settings, "FRONTEND_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/pay/{make_invoice_pay_token(invoice)}"


def invoice_from_token(raw_token: str) -> Invoice:
    """Resolve a pay token to its invoice, or 404.

    Every rejection says the same thing. A link that reported "this invoice is
    cancelled" for one token and "no such invoice" for another would answer
    questions about other people's invoices to anyone holding a forged token.
    """
    if not raw_token:
        raise NotFound("This payment link is invalid.")
    try:
        payload = signing.loads(raw_token, salt=TOKEN_SALT)
        invoice_id = int(payload["invoice"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise NotFound("This payment link is invalid.")
    invoice = Invoice.objects.select_related(
        "customer", "entity__tenant__school_profile", "entity__base_currency",
        "branch", "currency",
    ).filter(pk=invoice_id).first()
    if invoice is None:
        raise NotFound("This payment link is invalid.")
    return invoice


# --------------------------------------------------------------------------- #
# Payability                                                                   #
# --------------------------------------------------------------------------- #

def _unpayable_reason(invoice) -> str:
    """Why this invoice cannot take a payment, or "" when it can.

    Phrased for the payer standing in front of the page, not for a developer: the
    person reading it wants to know whether they still owe money.
    """
    if invoice.status == DocumentStatus.CANCELLED:
        return "This invoice has been cancelled. Please contact the sender."
    if invoice.status != DocumentStatus.POSTED:
        return "This invoice is not ready for payment yet. Please contact the sender."
    if invoice.balance_due <= 0:
        # A balance can reach zero without money changing hands - a credit note, a
        # concession or a write-off. Thanking somebody for paying when the school
        # waived the fee is a small lie the payer would notice.
        if invoice.amount_paid >= invoice.total:
            return "This invoice has been paid in full. Thank you."
        return "There is nothing left to pay on this invoice."
    return ""


def is_payable(invoice) -> bool:
    return not _unpayable_reason(invoice)


# --------------------------------------------------------------------------- #
# Page payload                                                                 #
# --------------------------------------------------------------------------- #

def summary(raw_token: str) -> dict:
    """What the pay page shows before the payer commits to anything.

    Deliberately narrow. The invoice PDF already went to this address, so the
    document number, the amount and who is billing are not new exposure - but the
    line items, the billing address and the phone number would be, and none of them
    help somebody decide to pay.
    """
    invoice = invoice_from_token(raw_token)
    from .documents import _issuer_block

    issuer = _issuer_block(invoice.entity, branch=invoice.branch)
    reason = _unpayable_reason(invoice)
    balance = max(invoice.balance_due, 0)
    return {
        "issuer_name": issuer["name"],
        "customer_name": invoice.customer.name,
        "invoice_number": invoice.document_number,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else "",
        "due_date": invoice.due_date.isoformat() if invoice.due_date else "",
        "currency": _currency_code(invoice),
        "total": format_naira(invoice.total),
        "amount_due": format_naira(balance),
        "amount_due_kobo": balance,
        "payable": not reason,
        "message": reason,
    }


def _currency_code(invoice) -> str:
    currency = invoice.currency or getattr(invoice.entity, "base_currency", None)
    return getattr(currency, "code", "") or "NGN"


# --------------------------------------------------------------------------- #
# Checkout                                                                     #
# --------------------------------------------------------------------------- #

def _reusable_intent(invoice, amount: int):
    """A checkout started moments ago for this same balance, if there is one."""
    from vs_payments.constants import CollectionStatus
    from vs_payments.models import CollectionIntent

    return (
        CollectionIntent.objects
        .filter(
            invoice=invoice, amount=amount,
            status=CollectionStatus.PROCESSING,
            created_at__gte=timezone.now() - timedelta(minutes=CHECKOUT_REUSE_MINUTES),
        )
        .exclude(checkout_url="")
        .order_by("-created_at", "-id")
        .first()
    )


def start_checkout(raw_token: str) -> dict:
    """Mint (or reuse) a hosted checkout for whatever this invoice still owes.

    The amount is read here, at click time, which is the whole point of the token: it
    is the balance now, not the balance when the email was written.
    """
    from vs_payments.services import initiate_collection

    invoice = invoice_from_token(raw_token)
    reason = _unpayable_reason(invoice)
    if reason:
        raise InvoiceNotPayableError(reason)

    amount = invoice.balance_due
    intent = _reusable_intent(invoice, amount)
    if intent is None:
        intent = initiate_collection(
            entity=invoice.entity,
            amount=amount,
            customer=invoice.customer,
            invoice=invoice,
            payer_email=invoice.customer.billing_email or "",
            payer_name=invoice.customer.name,
            narration=f"Invoice {invoice.document_number}",
            metadata={"source": "invoice_pay_link", "invoice_number": invoice.document_number},
        )

    return {
        "checkout_url": intent.checkout_url,
        "reference": intent.reference,
        "amount": format_naira(amount),
        "amount_kobo": amount,
        "currency": _currency_code(invoice),
        "invoice_number": invoice.document_number,
    }
