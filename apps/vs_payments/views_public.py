"""The payer's return page asking how its payment went.

A parent who has just paid on the provider's checkout is sent back to their
school's ``/payments/return?reference=...`` page. They have no account and no
session, so this route answers that page from the reference alone, and tells it
the little it needs and nothing more:

``status``
    ``PENDING`` (not yet confirmed), ``CONFIRMED``, ``FAILED`` or ``ABANDONED``.
``amount`` / ``amount_naira`` / ``currency``
    What was asked of the payer, in kobo and as the school prints it.
``payee_name``
    The tenant's name, or the platform's own name for an invoice the platform
    raised.
``paid_at``
    When the payment was confirmed and booked (ISO 8601, UTC), else null.

Nothing about the customer or pupil, the invoice, the payer's email or the
branch is in it, and none of the fields is a Field Access field: Field Access
narrows what a signed-in member of staff reads by role, and this answer goes to
the person who made the payment, from a reference only they were given, so it
carries no registered field at all.

The reference is the only credential. References for collections are
unguessable (:func:`vs_payments.services._new_collection_reference`), and one
that is not of that shape is answered exactly like one that does not exist
(404, no hint), so an older sequential reference cannot be walked.

When the payment is still pending after ``VERIFY_AFTER_SECONDS`` the route asks
the provider, through the same confirmation ``GET collections/<id>/?verify=1``
uses, at most once per ``VERIFY_EVERY_SECONDS`` for that reference, however
often the page asks. The route is throttled per address and per reference.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from core.response import success_response
from vs_finance.exceptions import FinanceError

from . import services
from .constants import CollectionStatus
from .models import CollectionIntent

logger = logging.getLogger("vs_payments.public")

#: A payment younger than this is not asked about: the payer is still on the checkout.
VERIFY_AFTER_SECONDS = 10

#: The provider is asked about one reference at most this often.
VERIFY_EVERY_SECONDS = 10

#: How long a payee's name is remembered; it changes with a school's settings, rarely.
PAYEE_NAME_CACHE_SECONDS = 600

#: The neutral word for each collection status.
PUBLIC_STATUS = {
    CollectionStatus.PENDING: "PENDING",
    CollectionStatus.PROCESSING: "PENDING",
    CollectionStatus.SUCCEEDED: "CONFIRMED",
    CollectionStatus.REFUNDED: "CONFIRMED",
    CollectionStatus.FAILED: "FAILED",
    CollectionStatus.ABANDONED: "ABANDONED",
}


class PublicCollectionReferenceThrottle(SimpleRateThrottle):
    """Bound how often ONE reference can be asked about, whoever asks.

    A school's parents share an address, so the per-address limit alone is a
    blunt backstop; this gives each payment its own budget, so one page polling
    its payment costs another payer nothing.
    """

    scope = "payments_public_status_reference"

    def get_cache_key(self, request, view):
        reference = str(view.kwargs.get("reference") or "")
        return self.cache_format % {"scope": self.scope, "ident": reference} if reference else None


def _payee_name(intent) -> str:
    """The name of whoever is being paid, remembered briefly."""
    from vs_finance.documents import issuer_name

    key = f"payments:payee-name:{intent.entity_id}"
    name = cache.get(key)
    if name is None:
        name = issuer_name(intent.entity)
        cache.set(key, name, PAYEE_NAME_CACHE_SECONDS)
    return name


def _maybe_verify(intent):
    """Ask the provider about a payment still pending after a few seconds, once per interval."""
    if intent.status not in (CollectionStatus.PENDING, CollectionStatus.PROCESSING):
        return intent
    if (timezone.now() - intent.created_at).total_seconds() < VERIFY_AFTER_SECONDS:
        return intent
    if not cache.add(f"payments:public-verify:{intent.reference}", 1, VERIFY_EVERY_SECONDS):
        return intent
    try:
        return services.confirm_collection(intent)
    except FinanceError as exc:  # The page only wants to know; the sweep books it later.
        logger.info("Public status check of %s could not confirm: %s", intent.reference, exc)
        return intent


class PublicCollectionStatusView(APIView):
    """GET /payments/public/collections/<reference>/status/ - how a payer's payment went.

    docstring-name: Public payment status
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_scope = "payments_public_status"
    throttle_classes = [ScopedRateThrottle, PublicCollectionReferenceThrottle]

    def get(self, request, reference):
        reference = str(reference)
        intent = None
        if services.COLLECTION_REFERENCE_RE.match(reference):
            intent = (CollectionIntent.objects
                      .select_related("entity__tenant", "currency")
                      .filter(reference=reference).first())
        if intent is None:
            raise NotFound("Not found.")
        intent = _maybe_verify(intent)
        return success_response(data={
            "status": PUBLIC_STATUS.get(intent.status, "PENDING"),
            "amount": int(intent.amount),
            "amount_naira": _naira(intent.amount),
            "currency": getattr(intent.currency, "code", "") or "NGN",
            "payee_name": _payee_name(intent),
            "paid_at": intent.confirmed_at.isoformat() if intent.confirmed_at else None,
        })


def _naira(kobo) -> str:
    from vs_finance.money import format_naira

    return format_naira(int(kobo))
