"""Which address a request is taken to come from, and what a caller cannot change.

See ``core.client_ip``. The addresses are from the documentation ranges
(RFC 5737 and RFC 3849).
"""

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from core.client_ip import ClientIPMiddleware, get_client_ip

#: What Render's router shows as the peer for every request.
RENDER_INTERNAL = "10.201.3.14"

#: The settings a Render deployment runs with.
CLOUDFLARE = ("HTTP_CF_CONNECTING_IP", "HTTP_TRUE_CLIENT_IP")


def _through_middleware(**meta):
    """Build a request with ``meta``, pass it through the middleware, return it."""
    request = RequestFactory().get("/", **meta)
    ClientIPMiddleware(lambda req: HttpResponse())(request)
    return request


class NoTrustedEdgeTests(SimpleTestCase):
    """With no edge configured the socket's peer is the answer."""

    def test_the_setting_is_empty_by_default(self):
        self.assertEqual(tuple(settings.CLIENT_IP_HEADERS), ())

    def test_a_forged_forwarded_for_changes_nothing(self):
        request = _through_middleware(
            REMOTE_ADDR="192.0.2.10", HTTP_X_FORWARDED_FOR="203.0.113.99",
        )

        self.assertEqual(request.META["REMOTE_ADDR"], "192.0.2.10")
        self.assertEqual(get_client_ip(request), "192.0.2.10")

    def test_cloudflare_headers_are_ignored_when_no_edge_is_trusted(self):
        """Locally nothing sets them, so whoever sent one wrote it themselves."""
        request = _through_middleware(
            REMOTE_ADDR="192.0.2.10", HTTP_CF_CONNECTING_IP="203.0.113.99",
        )

        self.assertEqual(get_client_ip(request), "192.0.2.10")


@override_settings(CLIENT_IP_HEADERS=CLOUDFLARE)
class CloudflareEdgeTests(SimpleTestCase):
    """Behind Render, Cloudflare's own headers decide the address."""

    def test_cf_connecting_ip_wins_over_forwarded_for(self):
        request = _through_middleware(
            REMOTE_ADDR=RENDER_INTERNAL,
            HTTP_X_FORWARDED_FOR="203.0.113.99, 198.51.100.7, 172.70.1.1",
            HTTP_CF_CONNECTING_IP="198.51.100.7",
        )

        self.assertEqual(get_client_ip(request), "198.51.100.7")

    def test_true_client_ip_is_the_fallback(self):
        request = _through_middleware(
            REMOTE_ADDR=RENDER_INTERNAL, HTTP_TRUE_CLIENT_IP="198.51.100.8",
        )

        self.assertEqual(get_client_ip(request), "198.51.100.8")

    def test_an_invalid_value_is_ignored_and_the_next_header_tried(self):
        request = _through_middleware(
            REMOTE_ADDR=RENDER_INTERNAL,
            HTTP_CF_CONNECTING_IP="not-an-address",
            HTTP_TRUE_CLIENT_IP="198.51.100.8",
        )

        self.assertEqual(get_client_ip(request), "198.51.100.8")

    def test_with_no_valid_header_the_peer_address_stands(self):
        """Wrong but unforgeable, rather than whatever a header claimed."""
        for garbage in ("", "unknown", "198.51.100.7, 203.0.113.99", "999.1.1.1"):
            with self.subTest(value=garbage):
                request = _through_middleware(
                    REMOTE_ADDR=RENDER_INTERNAL,
                    HTTP_CF_CONNECTING_IP=garbage,
                    HTTP_X_FORWARDED_FOR="203.0.113.99",
                )
                self.assertEqual(get_client_ip(request), RENDER_INTERNAL)

    def test_ipv6_is_accepted_and_normalised(self):
        request = _through_middleware(
            REMOTE_ADDR=RENDER_INTERNAL,
            HTTP_CF_CONNECTING_IP=" 2001:DB8:0:0::1 ",
        )

        self.assertEqual(get_client_ip(request), "2001:db8::1")


class GetClientIPTests(SimpleTestCase):
    def test_it_reads_remote_addr_and_never_forwarded_for(self):
        request = RequestFactory().get(
            "/", REMOTE_ADDR="192.0.2.10", HTTP_X_FORWARDED_FOR="203.0.113.99",
        )

        self.assertEqual(get_client_ip(request), "192.0.2.10")

    def test_no_request_is_no_address(self):
        self.assertIsNone(get_client_ip(None))


class WiringTests(SimpleTestCase):
    def test_the_middleware_runs_first(self):
        self.assertEqual(settings.MIDDLEWARE[0], "core.client_ip.ClientIPMiddleware")

    def test_throttles_trust_no_forwarding_proxy(self):
        """``NUM_PROXIES = 0`` is what makes DRF key throttles on REMOTE_ADDR."""
        self.assertEqual(settings.REST_FRAMEWORK["NUM_PROXIES"], 0)
