"""A store app asks, before sign-in, whether its version may still run.

Version 1.3.0 of the parent app ships with a Pay button that fails on some
Android phones. CodeX raises the Android minimum to 1.3.1. Mrs Nwosu, still on
1.3.0, opens the app and is told to update; Mr Bello on 1.4.0 notices nothing;
and a parent on 1.3.5 with 1.4.0 out is only offered the new version.

The rules are tested by calling them, the route for what only the route does
(no session, status codes, caching), and the system check for each way the
setting can be wrong.
"""
from __future__ import annotations

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from core.app_versions import (
    CURRENT,
    UPDATE_AVAILABLE,
    UPDATE_REQUIRED,
    compare_versions,
    configuration_problems,
    configured_policy,
    parse_version,
    version_status,
)
from core.checks import APP_VERSIONS_CHECK_ID, check_native_app_versions

PARENT = "com.codexng.xvs.parent"
STORE = "https://play.google.com/store/apps/details?id=com.codexng.xvs.parent"
VERSIONS = {
    PARENT: {
        "android": {"minimum": "1.3.1", "latest": "1.4.0", "store_url": STORE},
    },
}


class ParseAndCompareTests(SimpleTestCase):
    def test_missing_parts_are_zero(self):
        self.assertEqual(parse_version("1"), (1, 0, 0))
        self.assertEqual(parse_version("1.2"), (1, 2, 0))
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))

    def test_invalid_versions_are_refused(self):
        for text in ("", "1.", ".1", "1..2", "1.2.3.4", "v1.2", "1.2-beta", " 1.2", "-1", "1.x", None, 1.2):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_version(text)

    def test_parts_compare_as_numbers(self):
        self.assertEqual(compare_versions("1.10", "1.9"), 1)
        self.assertEqual(compare_versions("1.9", "1.10"), -1)

    def test_trailing_zero_parts_are_equal(self):
        self.assertEqual(compare_versions("1.2", "1.2.0"), 0)
        self.assertEqual(compare_versions("1", "1.0.0"), 0)


class StatusTests(SimpleTestCase):
    def test_below_minimum_requires_an_update(self):
        self.assertEqual(version_status("1.3.0", "1.3.1", "1.4.0"), UPDATE_REQUIRED)

    def test_between_minimum_and_latest_offers_an_update(self):
        self.assertEqual(version_status("1.3.1", "1.3.1", "1.4.0"), UPDATE_AVAILABLE)
        self.assertEqual(version_status("1.3.9", "1.3.1", "1.4.0"), UPDATE_AVAILABLE)

    def test_at_or_above_latest_is_current(self):
        self.assertEqual(version_status("1.4.0", "1.3.1", "1.4.0"), CURRENT)
        self.assertEqual(version_status("2.0", "1.3.1", "1.4.0"), CURRENT)

    def test_no_version_gives_no_status(self):
        self.assertIsNone(version_status(None, "1.3.1", "1.4.0"))

    def test_an_absent_number_never_acts(self):
        self.assertEqual(version_status("0.1", None, None), CURRENT)
        self.assertEqual(version_status("0.1", None, "1.0"), UPDATE_AVAILABLE)

    def test_an_invalid_running_version_is_refused(self):
        with self.assertRaises(ValueError):
            version_status("beta", "1.0", "1.0")


class ConfiguredPolicyTests(SimpleTestCase):
    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_reads_the_policy(self):
        self.assertEqual(
            configured_policy(PARENT, "android"),
            {"minimum": "1.3.1", "latest": "1.4.0", "store_url": STORE},
        )

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_unconfigured_app_or_platform_is_none(self):
        self.assertIsNone(configured_policy("com.example.other", "android"))
        self.assertIsNone(configured_policy(PARENT, "ios"))

    @override_settings(NATIVE_APP_VERSIONS={PARENT: {"android": {"minimum": "soon", "latest": "1.4"}}})
    def test_an_invalid_number_reads_as_absent(self):
        self.assertEqual(
            configured_policy(PARENT, "android"),
            {"minimum": None, "latest": "1.4", "store_url": None},
        )


class AppVersionRouteTests(SimpleTestCase):
    """The route reads settings only, so it needs no database."""

    URL = "/v1/app-versions/"

    def get(self, **query):
        query.setdefault("app", PARENT)
        query.setdefault("platform", "android")
        return APIClient().get(self.URL, query)

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_below_minimum_is_update_required(self):
        response = self.get(version="1.3.0")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {
            "app": PARENT, "platform": "android", "minimum": "1.3.1",
            "latest": "1.4.0", "store_url": STORE, "status": UPDATE_REQUIRED,
        })

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_between_is_update_available(self):
        self.assertEqual(self.get(version="1.3.5").json()["data"]["status"], UPDATE_AVAILABLE)

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_at_or_above_latest_is_current(self):
        self.assertEqual(self.get(version="1.4.0").json()["data"]["status"], CURRENT)
        self.assertEqual(self.get(version="1.10.0").json()["data"]["status"], CURRENT)

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_no_version_gives_null_status_and_the_numbers(self):
        data = self.get().json()["data"]

        self.assertIsNone(data["status"])
        self.assertEqual(data["minimum"], "1.3.1")

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_unknown_app_is_404(self):
        self.assertEqual(self.get(app="com.example.other").status_code, 404)

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_known_app_without_that_platform_is_404(self):
        self.assertEqual(self.get(platform="ios").status_code, 404)

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_bad_platform_is_400(self):
        self.assertEqual(self.get(platform="windows").status_code, 400)

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_missing_app_is_400(self):
        response = APIClient().get(self.URL, {"platform": "android"})
        self.assertEqual(response.status_code, 400)

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_bad_version_is_400(self):
        self.assertEqual(self.get(version="one.two").status_code, 400)

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS)
    def test_needs_no_authentication_and_is_cacheable(self):
        response = self.get(version="1.4.0")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "public, max-age=300")

    @override_settings(NATIVE_APP_VERSIONS={})
    def test_nothing_configured_is_404_not_a_crash(self):
        self.assertEqual(self.get(version="1.0").status_code, 404)


class ConfigurationCheckTests(SimpleTestCase):
    def messages(self):
        return [w.msg for w in check_native_app_versions()]

    @override_settings(NATIVE_APP_VERSIONS=VERSIONS, NATIVE_APP_VERSIONS_ERROR="")
    def test_sound_setting_is_silent(self):
        self.assertEqual(check_native_app_versions(), [])

    @override_settings(NATIVE_APP_VERSIONS={}, NATIVE_APP_VERSIONS_ERROR="Expecting value")
    def test_malformed_json_warns(self):
        warnings = check_native_app_versions()

        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].id, APP_VERSIONS_CHECK_ID)
        self.assertIn("not valid JSON", warnings[0].msg)

    @override_settings(NATIVE_APP_VERSIONS=["nope"], NATIVE_APP_VERSIONS_ERROR="")
    def test_wrong_shape_warns(self):
        self.assertEqual(len(check_native_app_versions()), 1)

    @override_settings(
        NATIVE_APP_VERSIONS={PARENT: {"android": {"minimum": "1.x", "latest": "2.0"}}},
        NATIVE_APP_VERSIONS_ERROR="",
    )
    def test_invalid_version_string_warns(self):
        (message,) = self.messages()
        self.assertIn("not a valid version", message)

    @override_settings(
        NATIVE_APP_VERSIONS={PARENT: {"windows": {"minimum": "1.0", "latest": "2.0"}}},
        NATIVE_APP_VERSIONS_ERROR="",
    )
    def test_unknown_platform_warns(self):
        (message,) = self.messages()
        self.assertIn("platform must be one of", message)

    @override_settings(
        NATIVE_APP_VERSIONS={PARENT: {"ios": {"minimum": "1.5", "latest": "1.4"}}},
        NATIVE_APP_VERSIONS_ERROR="",
    )
    def test_latest_below_minimum_warns(self):
        (message,) = self.messages()
        self.assertIn("below minimum", message)

    def test_every_problem_is_listed(self):
        problems = configuration_problems({
            PARENT: {
                "android": {"minimum": "2.0", "latest": "1.0"},
                "ios": {"minimum": "x"},
                "web": {},
            },
        })
        self.assertEqual(len(problems), 3)
