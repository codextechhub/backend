"""``/v1/health/live/``: what Render and the uptime monitor are told, and how.

Render calls the instance with its own host name over plain HTTP; the monitor
calls the public address. Both are answered without signing in, whatever the
host or scheme, and only a database that does not answer makes it a 503.
"""
from __future__ import annotations

from unittest import mock

from django.db import OperationalError
from django.test import TestCase, override_settings

PATH = "/v1/health/live/"


class LivenessTests(TestCase):
    def test_a_working_api_says_ok_without_anyone_signed_in(self):
        response = self.client.get(PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["database"], "ok")
        self.assertEqual(response["Cache-Control"], "no-store")

    @override_settings(ALLOWED_HOSTS=["api.codexng.com"], SECURE_SSL_REDIRECT=True)
    def test_render_probe_on_its_own_host_over_http_is_answered_not_refused_or_redirected(self):
        response = self.client.get(PATH, HTTP_HOST="10.0.4.7:10000")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    @override_settings(ALLOWED_HOSTS=["api.codexng.com"], SECURE_SSL_REDIRECT=True)
    def test_every_other_path_still_meets_the_host_check(self):
        response = self.client.get("/v1/health/overview/", HTTP_HOST="10.0.4.7:10000")

        self.assertEqual(response.status_code, 400)

    def test_a_database_that_does_not_answer_is_a_503(self):
        with mock.patch("vs_health.liveness.connection.cursor", side_effect=OperationalError("down")):
            response = self.client.get(PATH)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            "status": "unavailable", "database": "unavailable", "cache": "not used",
        })

    def test_redis_away_is_reported_but_the_api_still_serves(self):
        with mock.patch("vs_health.liveness.cache_state", return_value="unavailable"):
            response = self.client.get(PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cache"], "unavailable")

    def test_a_head_request_is_answered_too(self):
        self.assertEqual(self.client.head(PATH).status_code, 200)

    def test_only_reads_are_answered_here(self):
        self.assertNotEqual(self.client.post(PATH).status_code, 200)

    def test_the_answer_names_nothing_about_the_platform(self):
        body = self.client.get(PATH).json()
        self.assertEqual(set(body), {"status", "database", "cache"})
