"""Which Paystack keys each kind of server may hold.

The rule lives in ``apps.settings.paystack_keys`` as two pure functions, so the
whole matrix is checked by calling them. How a deployed settings module reacts
is pinned in ``core.test_deployment_settings``; the development settings are
imported in a subprocess here, because a fresh interpreter is the only honest
answer to what a settings module does at start.
"""

import json
import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase

from apps.settings.paystack_keys import deployed_key_problem, local_key_problem

#: Long enough to spot if a message ever echoed any of it.
_TAIL = "Zq9fK3mP7xWvL2nR8"
LIVE_SECRET = "sk_live_" + _TAIL
TEST_SECRET = "sk_test_" + _TAIL
LIVE_PUBLIC = "pk_live_" + _TAIL
TEST_PUBLIC = "pk_test_" + _TAIL


class DeployedKeyRuleTests(SimpleTestCase):
    def test_a_missing_or_unknown_mode_is_refused(self):
        for mode in (None, "", "  ", "sandbox", "LIVE", "Test", "live,test"):
            with self.subTest(mode=mode):
                problem = deployed_key_problem(mode, "", "")

                self.assertIn("PAYSTACK_KEY_MODE", problem)
                self.assertIn("live or test", problem)

    def test_matching_keys_are_accepted_in_each_mode(self):
        cases = (
            ("live", LIVE_SECRET, LIVE_PUBLIC),
            ("test", TEST_SECRET, TEST_PUBLIC),
            ("live", LIVE_SECRET, ""),
            ("test", "", TEST_PUBLIC),
        )
        for mode, secret, public in cases:
            with self.subTest(mode=mode, secret=secret[:8], public=public[:8]):
                self.assertIsNone(deployed_key_problem(mode, secret, public))

    def test_no_key_at_all_is_allowed_in_either_mode(self):
        for mode in ("live", "test"):
            for empty in ("", None, "   "):
                with self.subTest(mode=mode, empty=empty):
                    self.assertIsNone(deployed_key_problem(mode, empty, empty))

    def test_a_test_server_refuses_a_live_secret_key(self):
        problem = deployed_key_problem("test", LIVE_SECRET, "")

        self.assertEqual(
            problem,
            "This server takes test Paystack keys only (PAYSTACK_KEY_MODE=test), "
            "but PAYSTACK_SECRET_KEY is a live key (it starts with sk_live_).",
        )

    def test_a_live_server_refuses_a_test_secret_key(self):
        problem = deployed_key_problem("live", TEST_SECRET, "")

        self.assertEqual(
            problem,
            "This server takes live Paystack keys only (PAYSTACK_KEY_MODE=live), "
            "but PAYSTACK_SECRET_KEY is a test key (it starts with sk_test_).",
        )

    def test_the_public_key_is_held_to_the_same_rule(self):
        self.assertIn(
            "PAYSTACK_PUBLIC_KEY is a live key",
            deployed_key_problem("test", TEST_SECRET, LIVE_PUBLIC),
        )
        self.assertIn(
            "PAYSTACK_PUBLIC_KEY is a test key",
            deployed_key_problem("live", LIVE_SECRET, TEST_PUBLIC),
        )

    def test_a_public_key_in_the_secret_slot_is_refused(self):
        """Swapping the two variables is an easy slip."""
        self.assertIn(
            "PAYSTACK_SECRET_KEY is not a Paystack key",
            deployed_key_problem("test", TEST_PUBLIC, ""),
        )
        self.assertIn(
            "PAYSTACK_PUBLIC_KEY is not a Paystack key",
            deployed_key_problem("test", TEST_SECRET, TEST_SECRET),
        )

    def test_a_malformed_key_is_refused_without_being_echoed(self):
        problem = deployed_key_problem("live", "not-a-key-" + _TAIL, "")

        self.assertIn("PAYSTACK_SECRET_KEY is not a Paystack key", problem)
        self.assertNotIn("not-a-key", problem)

    def test_no_message_carries_more_than_the_key_prefix(self):
        problems = [
            deployed_key_problem("test", LIVE_SECRET, ""),
            deployed_key_problem("live", TEST_SECRET, ""),
            deployed_key_problem("test", TEST_SECRET, LIVE_PUBLIC),
            deployed_key_problem("live", LIVE_SECRET, TEST_PUBLIC),
            deployed_key_problem("live", "garbage" + _TAIL, ""),
            deployed_key_problem("bogus", LIVE_SECRET, LIVE_PUBLIC),
            local_key_problem(LIVE_SECRET),
        ]
        for problem in problems:
            with self.subTest(problem=problem[:40]):
                self.assertNotIn(_TAIL, problem)
                self.assertNotIn(_TAIL[:4], problem)


class LocalKeyRuleTests(SimpleTestCase):
    def test_a_live_secret_key_is_refused_with_the_reason(self):
        problem = local_key_problem(LIVE_SECRET)

        self.assertIn("PAYSTACK_SECRET_KEY is a live key", problem)
        self.assertIn("never move real money by accident", problem)
        self.assertIn("PAYSTACK_ALLOW_LIVE_KEY_LOCALLY", problem)

    def test_the_escape_hatch_allows_it(self):
        self.assertIsNone(local_key_problem(LIVE_SECRET, allow_live=True))

    def test_test_empty_and_unknown_keys_are_not_this_rules_business(self):
        for key in (TEST_SECRET, "", None, "  ", "whatever"):
            with self.subTest(key=key):
                self.assertIsNone(local_key_problem(key))


#: Imports one settings module and reports the refusal, if any.
_PROBE = """
import importlib, json, sys
try:
    importlib.import_module(sys.argv[1])
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
else:
    print(json.dumps({"ok": True}))
"""


class DevelopmentSettingsTests(SimpleTestCase):
    """The settings a laptop, a test run and CI start under."""

    def _import(self, module, **environment):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("PAYSTACK_")}
        env["SECRET_KEY"] = "paystack-key-rule-test"
        env.update(environment)
        result = subprocess.run(
            [sys.executable, "-c", _PROBE, module],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertTrue(
            result.stdout.strip(),
            f"the probe printed nothing.\nstderr:\n{result.stderr}",
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_every_development_module_refuses_a_live_secret_key(self):
        for module in ("local", "test", "ci"):
            with self.subTest(module=module):
                outcome = self._import(
                    f"apps.settings.{module}", PAYSTACK_SECRET_KEY=LIVE_SECRET,
                )

                self.assertEqual(outcome.get("error"), "ImproperlyConfigured", outcome)
                self.assertIn("never move real money", outcome["message"])
                self.assertNotIn(_TAIL, outcome["message"])

    def test_the_escape_hatch_lets_a_live_key_through(self):
        outcome = self._import(
            "apps.settings.local",
            PAYSTACK_SECRET_KEY=LIVE_SECRET,
            PAYSTACK_ALLOW_LIVE_KEY_LOCALLY="true",
        )

        self.assertEqual(outcome, {"ok": True})

    def test_a_test_key_and_no_key_both_start(self):
        for key in (TEST_SECRET, ""):
            with self.subTest(key=key[:8]):
                outcome = self._import("apps.settings.local", PAYSTACK_SECRET_KEY=key)

                self.assertEqual(outcome, {"ok": True})
