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

import hashlib

from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

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
        token = str(view.kwargs.get("token") or "")
        if not token:
            return None
        # Hashed because the raw token is a bearer credential and cache keys are the
        # sort of thing that ends up in a log line.
        ident = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": ident}


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

    def get(self, request, token):
        return success_response("Invoice retrieved.", data=pay_links.summary(token))


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
