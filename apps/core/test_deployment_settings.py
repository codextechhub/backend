"""What a deployed settings module resolves its own addresses to.

The addresses a deployment serves and links to are the ones its people meet
before they can sign in. A default pointing at a live host makes a
misconfigured deployment invisible: the service starts, and the activation mail
a tester opens on staging signs them into the production product and activates
a real account there. So the rule these pin is that a deployed service either
knows its own addresses or refuses to start.

``apps.settings.staging`` is imported in a subprocess rather than here. Two
reasons: importing it inserts WhiteNoise into the ``MIDDLEWARE`` list this
process is already serving requests from, and the subject is what a service
reads from its own environment, which only a fresh interpreter can answer.

The environment handed to that subprocess is the whole environment the module
sees, apart from a ``.env`` file sitting beside the settings package. None of
these addresses belongs in that file - a deployment's address belongs to the
deployment - so a developer who adds one there will see these fail, and the fix
is to remove it rather than to relax the test.
"""

import json
import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase

#: Printed by the subprocess: either the resolved addresses or the refusal.
_PROBE = """
import json

NAMES = (
    "FRONTEND_BASE_URL",
    "SCHOOL_APP_BASE_URL",
    "API_PUBLIC_BASE_URL",
    "HEALTH_PROBE_BASE_URL",
    "HEALTH_SSL_DOMAIN",
    "CSRF_TRUSTED_ORIGINS",
)

try:
    from apps.settings import staging
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
else:
    print(json.dumps(
        {"settings": {name: getattr(staging, name, None) for name in NAMES}}
    ))
"""

#: Everything the module needs that is not one of the addresses under test.
_BASE_ENV = {
    "DJANGO_SETTINGS_MODULE": "apps.settings.staging",
    "SECRET_KEY": "deployment-settings-test-key",
    "ALLOWED_HOSTS": "staging.example.com",
    "DB_NAME": "unused_by_an_import",
    "DB_USER": "unused_by_an_import",
    "DB_PASSWORD": "",
    "DB_HOST": "localhost",
}

#: The addresses under test, cleared from the inherited environment so the run
#: says the same thing on a developer's machine as it does in CI.
_ADDRESS_VARS = (
    "FRONTEND_BASE_URL",
    "SCHOOL_APP_BASE_URL",
    "API_PUBLIC_BASE_URL",
    "HEALTH_PROBE_BASE_URL",
    "HEALTH_SSL_DOMAIN",
    "CSRF_TRUSTED_ORIGINS",
)

#: A full, self-consistent staging configuration.
_STAGING_ADDRESSES = {
    "FRONTEND_BASE_URL": "https://console.staging.example.com",
    "SCHOOL_APP_BASE_URL": "https://apps.staging.example.com",
    "API_PUBLIC_BASE_URL": "https://api.staging.example.com",
    "HEALTH_PROBE_BASE_URL": "https://api.staging.example.com",
    "HEALTH_SSL_DOMAIN": "api.staging.example.com",
}

#: The hosts the live product answers on. Nothing a staging box resolves may
#: mention one of them.
PRODUCTION_HOSTS = ("xvs.codexng.com", "intranet.codexng.com", "api.codexng.com")


class DeployedAddressTests(SimpleTestCase):
    def _resolve(self, **environment):
        """Import the staging settings with ``environment`` and report what it did.

        Returns the module's addresses, or the name and message of whatever it
        raised on the way up.
        """
        env = {key: value for key, value in os.environ.items()
               if key not in _ADDRESS_VARS}
        env.update(_BASE_ENV)
        env.update(environment)

        result = subprocess.run(
            [sys.executable, "-c", _PROBE],
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

    def test_a_deployment_that_names_nothing_refuses_to_start(self):
        """Whichever address it asks for first, it asks for one of them."""
        outcome = self._resolve()

        self.assertEqual(outcome.get("error"), "ImproperlyConfigured", outcome)

    def test_a_deployment_that_names_no_school_app_refuses_to_start(self):
        """Rather than addressing the live one.

        This is the failure as it happened: nothing on staging set the
        variable, so every school address, activation link and password reset
        it built carried the production host.
        """
        outcome = self._resolve(**{
            key: value for key, value in _STAGING_ADDRESSES.items()
            if key != "SCHOOL_APP_BASE_URL"
        })

        self.assertEqual(outcome.get("error"), "ImproperlyConfigured", outcome)
        self.assertIn("SCHOOL_APP_BASE_URL", outcome["message"])

    def test_a_deployment_that_names_no_console_refuses_to_start(self):
        outcome = self._resolve(**{
            key: value for key, value in _STAGING_ADDRESSES.items()
            if key != "FRONTEND_BASE_URL"
        })

        self.assertEqual(outcome.get("error"), "ImproperlyConfigured", outcome)
        self.assertIn("FRONTEND_BASE_URL", outcome["message"])

    def test_the_addresses_it_resolves_are_its_own(self):
        outcome = self._resolve(**_STAGING_ADDRESSES)

        self.assertNotIn("error", outcome, outcome)
        resolved = outcome["settings"]
        for name, expected in _STAGING_ADDRESSES.items():
            with self.subTest(setting=name):
                self.assertEqual(resolved[name], expected)

        # Including the ones built from them: the trusted origins follow the
        # school app's own host rather than naming a deployment.
        rendered = json.dumps(resolved)
        for host in PRODUCTION_HOSTS:
            with self.subTest(host=host):
                self.assertNotIn(host, rendered)
        self.assertIn(
            "https://*.apps.staging.example.com", resolved["CSRF_TRUSTED_ORIGINS"],
        )

    def test_an_address_left_blank_is_refused_rather_than_used(self):
        """A half-finished variable is a configuration nobody completed.

        Honouring it builds links with no host in them, which reaches the
        recipient as a broken mail rather than as the error it is.
        """
        outcome = self._resolve(**{**_STAGING_ADDRESSES, "SCHOOL_APP_BASE_URL": "   "})

        self.assertEqual(outcome.get("error"), "ImproperlyConfigured", outcome)
        self.assertIn("SCHOOL_APP_BASE_URL", outcome["message"])

    def test_an_address_with_no_scheme_is_refused(self):
        """``vs_tenants.app_urls`` cannot put a tenant's slug in front of a bare
        host, and answers "" rather than guess, so a value shaped this way would
        silently remove every link built on it."""
        outcome = self._resolve(**{
            **_STAGING_ADDRESSES, "SCHOOL_APP_BASE_URL": "apps.staging.example.com",
        })

        self.assertEqual(outcome.get("error"), "ImproperlyConfigured", outcome)
        self.assertIn("SCHOOL_APP_BASE_URL", outcome["message"])

    def test_a_certificate_probe_is_given_a_host_not_a_url(self):
        outcome = self._resolve(**{
            **_STAGING_ADDRESSES,
            "HEALTH_SSL_DOMAIN": "https://api.staging.example.com",
        })

        self.assertEqual(outcome.get("error"), "ImproperlyConfigured", outcome)
        self.assertIn("HEALTH_SSL_DOMAIN", outcome["message"])
