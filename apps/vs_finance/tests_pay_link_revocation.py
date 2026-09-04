"""Killing one invoice's pay links, and letting an unrevoked copy die on its own.

The invoice's own state was once the only gate on a pay link, on the reasoning
that a cancelled or settled invoice stops being payable however many copies are
in circulation. That is true, and it says nothing about the case that actually
leaks: an invoice that stays **open**.

    Mrs Nwosu gets the fee invoice for Chidi's term and forwards it to her
    husband, who forwards it into the family WhatsApp group so somebody can pay
    it. The link opens a page showing Corona Secondary School, her name, the
    invoice number and the ₦1,800.00 still outstanding. It kept working for as
    long as the balance did, to everyone who ever received it, and the school had
    no way to stop that one link - rotating the salt would have killed the pay
    link in every other parent's inbox too.

So the token now carries the invoice's ``pay_token_version`` and an age. These
tests pin both, and pin the two things that must NOT change: an old token minted
before the column existed still works, and revoking does not make the invoice
unpayable - it makes the *copies* unusable, and the next reminder carries a fresh
one.
"""
from __future__ import annotations

import datetime
from unittest import mock

from django.core import signing
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.exceptions import NotFound

from .models import Invoice
from .pay_links import (
    TOKEN_SALT,
    invoice_from_token,
    invoice_pay_url,
    make_invoice_pay_token,
    revoke_pay_links,
    summary,
)
from .views_public import InvoicePayLinkReadThrottle
from .tests_pay_links import _PayLinkFixture


class PayLinkRevocationTests(_PayLinkFixture, TestCase):
    """Bumping the version invalidates this invoice's links and no other's."""

    def setUp(self):
        super().setUp()
        cache.clear()  # The pay routes are throttled per link; keep cases independent.
        self.entity, _period, self.customer, self.invoice = self.build_payable()

    def test_a_link_works_before_it_is_revoked(self):
        token = make_invoice_pay_token(self.invoice)

        self.assertEqual(invoice_from_token(token).pk, self.invoice.pk)

    def test_revoking_kills_the_link_already_sent(self):
        """The whole point: one forwarded copy, stopped."""
        token = make_invoice_pay_token(self.invoice)

        revoke_pay_links(self.invoice)

        with self.assertRaises(NotFound):
            invoice_from_token(token)

    def test_a_revoked_link_is_refused_exactly_like_a_forged_one(self):
        """A revoked link saying "this was revoked" confirms the invoice exists.

        The holder of a forwarded copy is precisely who must not be told that.
        """
        token = make_invoice_pay_token(self.invoice)
        revoke_pay_links(self.invoice)

        with self.assertRaises(NotFound) as revoked:
            invoice_from_token(token)
        with self.assertRaises(NotFound) as forged:
            invoice_from_token("not-a-real-token")

        self.assertEqual(str(revoked.exception), str(forged.exception))

    def test_revoking_does_not_make_the_invoice_unpayable(self):
        """It kills copies, not the debt. The next reminder must still work."""
        revoke_pay_links(self.invoice)

        fresh = make_invoice_pay_token(self.invoice)

        self.assertEqual(invoice_from_token(fresh).pk, self.invoice.pk)
        self.assertTrue(summary(fresh)["payable"])

    def test_the_rendered_url_carries_the_current_version(self):
        """``invoice_pay_url`` is what dunning renders, so it must mint, not cache."""
        revoke_pay_links(self.invoice)

        token = invoice_pay_url(self.invoice).rsplit("/", 1)[-1]

        self.assertEqual(invoice_from_token(token).pk, self.invoice.pk)

    def test_revoking_one_invoice_leaves_another_alone(self):
        """A column rather than a rotated salt, and this is why.

        Rotating ``TOKEN_SALT`` would have answered the same question by
        invalidating every pay link the school has ever sent.
        """
        other = self.make_invoice(
            self.entity, self.customer, lines=[("4100", 1, 50000, None)],
            date=datetime.date(2026, 1, 5), due=datetime.date(2026, 1, 30),
        )
        from .receivables import post_invoice

        post_invoice(other)
        other.refresh_from_db()
        other_token = make_invoice_pay_token(other)

        revoke_pay_links(self.invoice)

        self.assertEqual(invoice_from_token(other_token).pk, other.pk)

    def test_two_revocations_at_once_both_count(self):
        """Written with ``F``, so a second revoke cannot hand back a dead version.

        Incrementing in Python would let two operators read version 1 and both
        write 2, leaving the second holding a token the first had invalidated.
        """
        first = revoke_pay_links(self.invoice)
        second = revoke_pay_links(self.invoice)

        self.assertEqual((first, second), (2, 3))
        self.assertEqual(
            Invoice.objects.get(pk=self.invoice.pk).pay_token_version, 3,
        )


class PayLinkCompatibilityTests(_PayLinkFixture, TestCase):
    """A token minted before the column existed must keep working."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.entity, _period, self.customer, self.invoice = self.build_payable()

    def test_a_token_with_no_version_reads_as_version_one(self):
        """What makes this safe to deploy without a flag day.

        Every link in circulation at deploy time was signed without ``v``. If
        those were refused, every parent mid-payment would be locked out at once.
        """
        legacy = signing.dumps(
            {"invoice": self.invoice.pk}, salt=TOKEN_SALT, compress=True,
        )

        self.assertEqual(invoice_from_token(legacy).pk, self.invoice.pk)

    def test_a_legacy_token_is_still_killed_by_a_revocation(self):
        """Grandfathering the shape must not grandfather the exposure."""
        legacy = signing.dumps(
            {"invoice": self.invoice.pk}, salt=TOKEN_SALT, compress=True,
        )

        revoke_pay_links(self.invoice)

        with self.assertRaises(NotFound):
            invoice_from_token(legacy)


class PayLinkExpiryTests(_PayLinkFixture, TestCase):
    """A copy nobody revoked still stops working eventually."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.entity, _period, self.customer, self.invoice = self.build_payable()

    def test_an_aged_link_is_refused(self):
        """``signing.loads`` measures age from the signature's own timestamp.

        Signing in the past is how the age is exercised without waiting; there is
        no clock to freeze inside ``django.core.signing``.
        """
        from unittest.mock import patch

        old = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
        with patch("django.core.signing.time.time", return_value=old):
            stale = make_invoice_pay_token(self.invoice)

        with self.assertRaises(NotFound):
            invoice_from_token(stale)

    def test_a_recent_link_is_not_refused(self):
        self.assertEqual(
            invoice_from_token(make_invoice_pay_token(self.invoice)).pk,
            self.invoice.pk,
        )


class PayLinkReadThrottleTests(_PayLinkFixture, TestCase):
    """The summary read is bounded per link, not only per IP.

    It is the route that discloses the payer's name, the invoice number and the
    balance. IP alone cannot bound it, because a whole school's parents share
    one address.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.entity, _period, self.customer, self.invoice = self.build_payable()
        self.token = make_invoice_pay_token(self.invoice)

    def _url(self, token=None):
        return reverse(
            "public-invoice-pay", kwargs={"token": token or self.token},
        )

    def test_one_link_can_be_worked_only_so_hard(self):
        """The rate is stated here, not read from the running settings.

        DRF binds ``THROTTLE_RATES`` onto the throttle class at import, so
        patching the class is the only thing that reaches it, and a settings
        module is free to change or drop the deployed rate without changing what
        this test means: a link has a budget, and running it out closes it.
        """
        from rest_framework.test import APIClient

        rate = 3
        client = APIClient()

        with mock.patch.object(
            InvoicePayLinkReadThrottle, "THROTTLE_RATES",
            {"invoice_pay_link_read": f"{rate}/hour"},
        ):
            statuses = [
                client.get(self._url()).status_code for _ in range(rate + 1)
            ]

        self.assertEqual(statuses[:rate], [200] * rate)
        self.assertEqual(statuses[-1], 429)

    def test_one_links_budget_is_not_spent_by_another(self):
        """The reason it is keyed on the token and not the address.

        Two parents behind the school's own wifi share an IP. If the per-link
        budget were shared, the second parent would be refused because the first
        one read their own invoice.
        """
        from rest_framework.test import APIClient

        other = self.make_invoice(
            self.entity, self.customer, lines=[("4100", 1, 50000, None)],
            date=datetime.date(2026, 1, 5), due=datetime.date(2026, 1, 30),
        )
        from .receivables import post_invoice

        post_invoice(other)
        other.refresh_from_db()

        rate = 3
        client = APIClient()
        with mock.patch.object(
            InvoicePayLinkReadThrottle, "THROTTLE_RATES",
            {"invoice_pay_link_read": f"{rate}/hour"},
        ):
            for _ in range(rate + 1):
                client.get(self._url())

            # Same client, same address, a different link: still served.
            response = client.get(self._url(make_invoice_pay_token(other)))

        self.assertEqual(response.status_code, 200)
