"""The monitoring copy lists are empty unless the environment names an address.

The copies hold what schools' families and staff are sent, and the privacy
policy says Code X keeps no such copy, so a deployment that sets nothing must
copy nothing. The environment is stubbed, so a developer's own ``.env`` does
not decide the answer.
"""
from unittest import mock

from django.test import SimpleTestCase

from apps.settings import base


def environment(values):
    """A stand-in for decouple's ``config`` that reads only *values*."""

    def config(name, default=None, cast=None):
        return values.get(name, default)

    return mock.patch.object(base, "config", side_effect=config)


class MonitoringCopyTests(SimpleTestCase):
    def test_nothing_is_copied_unless_the_environment_names_a_mailbox(self):
        with environment({}):
            self.assertEqual(base._addresses("EMAIL_BCC", "EMAIL_CC"), [])
            self.assertEqual(base._addresses("FINANCE_CUSTOMER_EMAIL_BCC", "FINANCE_CUSTOMER_EMAIL_CC"), [])

    def test_a_named_mailbox_is_copied_and_the_old_name_still_counts(self):
        with environment({"EMAIL_BCC": "ops@codexng.com, audit@codexng.com"}):
            self.assertEqual(base._addresses("EMAIL_BCC", "EMAIL_CC"), ["ops@codexng.com", "audit@codexng.com"])
        with environment({"FINANCE_CUSTOMER_EMAIL_CC": "fin@codexng.com"}):
            self.assertEqual(
                base._addresses("FINANCE_CUSTOMER_EMAIL_BCC", "FINANCE_CUSTOMER_EMAIL_CC"), ["fin@codexng.com"],
            )
