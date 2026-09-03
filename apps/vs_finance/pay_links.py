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
enumerable is exposed. The invoice's own state is the first gate: a cancelled or fully
settled invoice stops being payable the moment it changes, however many copies of the
link are in circulation.

That gate was once the only one, on the reasoning that no revocation column was needed.
It is not enough for the case it does not cover - an invoice that stays *open*. A link
mailed to Mrs Nwosu, forwarded to her husband and on into a family group, kept working
for as long as the balance did, showing the school, her name, the invoice number and
what she still owed to everyone it reached. Nothing could kill that one link: rotating
``SECRET_KEY`` or ``TOKEN_SALT`` kills every link the school has ever sent.

So the token now carries two more things, and both are checked on the way back in:

* ``v``, the invoice's :attr:`~vs_finance.models.Invoice.pay_token_version`. Bumping it
  (:func:`revoke_pay_links`) invalidates the links for that invoice and no other -
  which is what the RFQ vendor portal does with ``token_version``, and the right
  answer here for the same reason;
* an age. :data:`TOKEN_MAX_AGE` bounds how long a copy stays live, so a link nobody
  revoked still dies on its own. It is deliberately generous, and it costs a slow payer
  nothing: every dunning email renders a fresh URL through :func:`invoice_pay_url`, so
  the newest reminder always works even when the first one has expired.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings

from vs_tenants.app_urls import school_app_url
from django.core import signing
from django.utils import timezone
from rest_framework.exceptions import NotFound

from .constants import DocumentStatus
from .exceptions import FinanceError
from .models import Invoice
from .money import format_naira

#: Salt for the invoice pay token. Changing it invalidates every link in circulation.
TOKEN_SALT = "vs_finance.invoice_pay"

#: How long one issued link stays usable.
#:
#: The backstop for a link nobody thought to revoke, so it is measured against how long
#: a copy should be able to sit in a forwarded mailbox rather than how long a debt takes
#: to collect. Two terms is generous for the first and short enough for the second.
#:
#: It cannot lock out a genuine late payer: dunning renders the URL at send time, so the
#: reminder chasing a nine-month-old debt carries a nine-month-old debt's fresh token.
#: What expires is the *copy*, which is the thing being bounded.
TOKEN_MAX_AGE = timedelta(days=180)

#: The host a platform invoice's payer lands on, as a subdomain of the school app.
#: Reserved in :data:`vs_tenants.models.RESERVED_TENANT_SLUGS`, so it cannot collide
#: with a school, and covered by the same wildcard DNS and certificate every school
#: subdomain uses, so it needs no new infrastructure.
PLATFORM_PAY_SUBDOMAIN = "pay"

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
    """Sign a link that identifies one invoice and carries no other authority.

    ``v`` is the invoice's current pay-token version, so a token minted before a
    revocation is distinguishable from one minted after it.
    """
    return signing.dumps(
        {"invoice": invoice.pk, "v": int(invoice.pay_token_version)},
        salt=TOKEN_SALT, compress=True,
    )


def revoke_pay_links(invoice) -> int:
    """Kill every pay link already issued for *invoice*. Returns the new version.

    One invoice's links and no other's - which is the whole point, and the reason
    this is a column rather than a rotated salt. A school that learns one link has
    been forwarded somewhere it should not have been can stop that link without
    invalidating the ones sitting in every other parent's inbox.

    Written with ``F`` and re-read rather than incremented in Python: two operators
    revoking the same invoice at once must produce two bumps, not one, or the
    second would hand back a version the first had already invalidated.
    """
    from django.db.models import F

    Invoice.objects.filter(pk=invoice.pk).update(
        pay_token_version=F("pay_token_version") + 1,
    )
    invoice.refresh_from_db(fields=["pay_token_version"])
    return invoice.pay_token_version


def _school_slug(invoice) -> str:
    """The slug of the school whose books raised this invoice, or "".

    Empty for the platform's own books and for any other school-less tenant.
    ``getattr`` with a default is safe on the reverse one-to-one: Django makes
    ``RelatedObjectDoesNotExist`` subclass ``AttributeError`` precisely so this
    reads as "no school" rather than raising.
    """
    tenant = getattr(invoice.entity, "tenant", None)
    if tenant is None or getattr(tenant, "school_profile", None) is None:
        return ""
    return str(getattr(tenant, "slug", "") or "")


def payer_base_url(invoice) -> str:
    """Scheme and host the *payer* of this invoice belongs on.

    A school's own customer goes to that school's app, at its own subdomain. The
    slug is inserted into the configured host rather than stored per school, so
    one setting covers every tenant and a new school needs no configuration at
    all: ``https://xvs.codexng.com`` becomes ``https://corona.xvs.codexng.com``,
    and ``http://localhost:5174`` becomes ``http://corona.localhost:5174``.

    An invoice from the platform's own books has no school to derive from, so it
    falls back to whatever ``PLATFORM_PAY_BASE_URL`` names, and to the bare
    product host when that is unset.

    Read on every call, never at import: see the note on PAYMENTS_CALLBACK_URL
    in settings/base.py for what the other way costs.
    """
    base = str(getattr(settings, "SCHOOL_APP_BASE_URL", "") or "").strip().rstrip("/")
    slug = _school_slug(invoice)
    if not slug:
        platform = str(getattr(settings, "PLATFORM_PAY_BASE_URL", "") or "").strip()
        if platform:
            return platform.rstrip("/")
        # No school to name, so use the reserved label instead of one. It goes
        # through the same subdomain insertion below, which matters: the bare
        # product host does NOT serve the app, so falling back to it would have
        # sent these payers to a marketing page. A subdomain does serve it, and
        # "pay" is in RESERVED_TENANT_SLUGS ("commercial surfaces that will want
        # their own host"), so no school can ever take it out from under this.
        slug = PLATFORM_PAY_SUBDOMAIN
    if not base:
        return ""
    # The insertion itself lives in vs_tenants.app_urls, because the Console
    # needs the same answer to show a school where its own app is served, and
    # two copies of "how do we build a school's address" would drift.
    return school_app_url(slug)


def payer_return_url(invoice) -> str:
    """Where the gateway sends this payer back to once they are done.

    The same host they paid from. Returning a Corona parent to the Console, or
    to another school's address, would look like the payment went somewhere it
    did not.
    """
    base = payer_base_url(invoice)
    return f"{base}/payments/return" if base else ""


def invoice_pay_url(invoice) -> str:
    """The public pay page for this invoice, or "" when no frontend is configured.

    Read at call time, never cached at import: the deployment that renders the email
    is the one that knows its own domain.
    """
    base = payer_base_url(invoice)
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
        # ``max_age`` turns an expired copy into a BadSignature, which is caught
        # below and answered exactly like a forged one. A payer whose link has
        # aged out is told the link is no good, not how old it is.
        payload = signing.loads(
            raw_token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE,
        )
        invoice_id = int(payload["invoice"])
        # A token minted before this field existed carries no ``v``. Reading it as
        # version 1 keeps every link already in circulation working, which is what
        # makes this change safe to deploy without a flag day.
        token_version = int(payload.get("v", 1))
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise NotFound("This payment link is invalid.")
    invoice = Invoice.objects.select_related(
        "customer", "entity__tenant__school_profile", "entity__base_currency",
        "branch", "currency",
    ).filter(pk=invoice_id, pay_token_version=token_version).first()
    if invoice is None:
        # Covers all three: no such invoice, and a token whose version the invoice
        # has moved past. Reported identically, because a revoked link telling its
        # holder "this was revoked" confirms the invoice exists.
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

def logo_storage_name(invoice) -> str:
    """The storage name of the issuer's brand logo, or "" when there is none.

    Only a school's own uploaded logo has one. The platform identity carries a
    plain ``logo_url`` string configured in Platform Settings rather than a file
    this system holds, so it has no storage name and the pay page falls back to
    the XVS mark for it.
    """
    from .documents import _issuer_block

    issuer = _issuer_block(invoice.entity, branch=invoice.branch)
    return str(issuer.get("logo_name") or "")


def summary(raw_token: str, *, request=None) -> dict:
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
    # An ABSOLUTE url, not a path. The school app and the API are on different
    # hosts (a school sits at <slug>.xvs.codexng.com, the API at api.codexng.com),
    # so a bare "/finance/public/..." would resolve against the app's own origin
    # and 404. Built from the request so each environment names itself.
    logo_path = f"/v1/finance/public/invoices/{raw_token}/logo/"
    logo_url = ""
    if logo_storage_name(invoice):
        logo_url = request.build_absolute_uri(logo_path) if request is not None else logo_path
    balance = max(invoice.balance_due, 0)
    return {
        "issuer_name": issuer["name"],
        # The school's own crest, served by the public logo route to a payer who
        # has no session. Empty when the school has not uploaded one.
        "logo_url": logo_url,
        # Whether CodeX itself is billing, rather than a school billing its own
        # customer. The pay page shows the XVS mark for the platform and the
        # school's name for a school, and it cannot tell the two apart from the
        # name alone: PLATFORM_ISSUER_NAME is configurable, so matching on the
        # string "CodeX" would break the first time somebody edits it.
        "issuer_is_platform": bool(invoice.entity.is_platform),
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
            # Back to the host they paid from, not the platform-wide default.
            # A Corona parent finishing at the Console would reasonably think
            # the money had gone somewhere it had not.
            callback_url=payer_return_url(invoice) or None,
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
