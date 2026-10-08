"""What reaches Sentry, and when reporting is allowed to start."""
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from core.observability import init_sentry, scrub_event


class ScrubEventTests(SimpleTestCase):
    def test_nothing_that_names_a_person_survives(self):
        event = {
            "message": "boom",
            "user": {"email": "parent@example.com", "ip_address": "1.2.3.4"},
            "request": {
                "url": "https://corona.xvs.example.com/v1/fees/",
                "method": "POST",
                "data": {"student": "Tunde"},
                "cookies": {"refresh_token": "secret"},
                "query_string": "student=42",
                "env": {"REMOTE_ADDR": "1.2.3.4"},
                "headers": {
                    "Authorization": "Bearer abc",
                    "Cookie": "refresh_token=secret",
                    "X-CSRFToken": "t",
                    "CF-Connecting-IP": "1.2.3.4",
                    "User-Agent": "Mozilla",
                },
            },
        }

        scrubbed = scrub_event(event, {})

        self.assertNotIn("user", scrubbed)
        request = scrubbed["request"]
        for removed in ("data", "cookies", "query_string", "env"):
            self.assertNotIn(removed, request)
        self.assertEqual(request["headers"], {"User-Agent": "Mozilla"})
        self.assertEqual(request["url"], "https://corona.xvs.example.com/v1/fees/")
        self.assertEqual(scrubbed["message"], "boom")

    def test_an_event_with_no_request_passes_through(self):
        self.assertEqual(scrub_event({"message": "task failed"}, {}), {"message": "task failed"})


class InitSentryTests(SimpleTestCase):
    def test_no_dsn_means_reporting_stays_off(self):
        self.assertFalse(init_sentry("", ""))
        self.assertFalse(init_sentry("", "production"))

    def test_a_dsn_without_an_environment_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            init_sentry("https://key@example.ingest.de.sentry.io/1", "")

    def test_nothing_but_the_failure_itself_is_collected(self):
        with mock.patch("core.observability.sentry_sdk.init") as init:
            started = init_sentry("https://key@example.ingest.de.sentry.io/1", "staging")

        self.assertTrue(started)
        options = init.call_args.kwargs
        self.assertEqual(options["max_breadcrumbs"], 0)
        self.assertFalse(options["send_default_pii"])
        self.assertFalse(options["include_local_variables"])
        self.assertEqual(options["max_request_body_size"], "never")
