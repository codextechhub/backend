"""Public, unauthenticated endpoints for the pay-an-invoice page.

A customer paying an invoice has no account on the platform and never will, so these
two routes authenticate the way the vendor RFQ portal does: by the signed token in
the URL, not by a session. The token names one invoice and grants nothing else, and
both routes are throttled because they are reachable by anyone holding a link.

Nothing here trusts the caller for an amount. The balance is read from the invoice at
the moment of the call, which is the reason the link points here instead of carrying
a checkout URL minted when the email went out.
"""
from __future__ import annotations

from django.core import signing
from django.http import HttpResponse
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from core.models import StoredFile
from core.response import success_response

from . import pay_links


class InvoicePayLinkThrottle(SimpleRateThrottle):
    """Bound how hard ONE pay link can be worked, whoever is holding it.

    The IP-keyed throttle alone cannot do this job. Parents paying from a school's
    own wifi share one address, so a limit tight enough to stop somebody hammering a
    single link would also stop the fourth parent in the queue from paying at all.
    Keying on the token separates the two: a link has its own budget, and one payer's
    use of it costs another payer nothing. The IP limit stays as the backstop for
    forged tokens, which never reach a real link and so never share a bucket here.
    """

    scope = "invoice_pay_link"

    def get_cache_key(self, request, view):
        """Key on the LINK, which means the invoice, not the token string.

        ``signing.dumps`` stamps the time into every token it mints, so the same
        invoice produces a different string one second later. Keying on the string
        therefore gave a resent invoice a brand new budget, and made this class
        untestable: two calls in the same test were throttled together or not at
        all depending on which side of a second boundary they landed.

        The payload is read here rather than the database: it is signed, so this
        costs one HMAC and no query. A token that fails to verify names no link
        and gets no bucket of its own; the IP-scoped throttle is what bounds
        forged traffic.
        """
        token = str(view.kwargs.get("token") or "")
        if not token:
            return None
        try:
            payload = signing.loads(
                token, salt=pay_links.TOKEN_SALT, max_age=pay_links.TOKEN_MAX_AGE,
            )
            ident = f"{int(payload['invoice'])}:{int(payload.get('v', 1))}"
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}


class InvoicePayLinkReadThrottle(InvoicePayLinkThrottle):
    """The same per-link budget for the summary read, counted separately.

    The read needs bounding for the same reason the checkout does, and more so: it
    is the route that discloses the payer's name, the invoice number and what is
    still owed, so a forwarded link being opened over and over is exactly what
    should be limited. IP alone cannot bound it.

    A **separate scope**, not a share of the checkout's, because the two are used at
    different rates by the same honest payer: she loads the page, starts a checkout,
    is bounced back from the gateway and loads it again. Spending page loads out of
    the same small budget that stops a link being worked would eventually refuse
    somebody in the middle of paying, which is the one outcome worse than the
    exposure being closed.
    """

    scope = "invoice_pay_link_read"


class _PublicInvoicePayView(APIView):
    """Shared shape: no session, token-authorised, tenant resolved from the invoice."""

    authentication_classes = []
    permission_classes = [AllowAny]
    tenant_param_required = False


class PublicInvoicePayView(_PublicInvoicePayView):
    """GET /finance/public/invoices/<token>/ - what the payer sees before paying.

    docstring-name: Public invoice pay summary
    """

    throttle_scope = "invoice_pay"
    # Both limits, deliberately: the IP one bounds somebody forging tokens at the
    # route, and the token-keyed one bounds a real link being worked. Neither
    # substitutes for the other, and until now only the first was here.
    throttle_classes = [ScopedRateThrottle, InvoicePayLinkReadThrottle]

    def get(self, request, token):
        # The request goes in so the logo URL can name this host rather than
        # guessing at one. See pay_links.summary.
        return success_response(
            "Invoice retrieved.", data=pay_links.summary(token, request=request),
        )


class PublicInvoiceCheckoutView(_PublicInvoicePayView):
    """POST /finance/public/invoices/<token>/checkout/ - start paying it.

    Creates the hosted checkout now, for the balance outstanding now, and returns the
    URL for the browser to send the payer to.

    docstring-name: Start an invoice checkout
    """

    throttle_scope = "invoice_pay_start"
    throttle_classes = [ScopedRateThrottle, InvoicePayLinkThrottle]

    def post(self, request, token):
        return success_response("Checkout ready.", data=pay_links.start_checkout(token))


class PublicInvoiceLogoView(_PublicInvoicePayView):
    """GET /finance/public/invoices/<token>/logo/ - the school's crest.

    A parent paying a fee invoice should see their school's own badge, not a
    product logo they have never heard of. They have no session, so the ordinary
    signed media URL cannot serve them: it is bound to a reader, and there is no
    reader here.

    This can be public where ``/media/`` cannot because the caller never names a
    file. The invoice named by the token decides which bytes come back, so there
    is no reference for anyone to tamper with or replay against another school's
    storage. Same reasoning, same shape as the vendor RFQ portal's logo route.

    docstring-name: Public invoice issuer logo
    """

    throttle_scope = "invoice_pay"

    def get(self, request, token):
        invoice = pay_links.invoice_from_token(token)
        name = pay_links.logo_storage_name(invoice)
        if not name:
            raise NotFound("Brand logo not found.")
        row = StoredFile.objects.filter(name=name, revoked_at__isnull=True).first()
        if row is None:
            raise NotFound("Brand logo not found.")
        response = HttpResponse(
            bytes(row.content), content_type=row.content_type or "image/png",
        )
        response["Content-Length"] = row.size
        # Public in the sense that it needs no session, but it is still one
        # school's crest answered to one link holder, so it must not be parked
        # in a shared cache.
        response["Cache-Control"] = "private, max-age=3600"
        response["X-Content-Type-Options"] = "nosniff"
        return response
