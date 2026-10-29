"""Commands that write known passwords or destroy data refuse the servers they must not touch.

Production is the server whose Paystack keys are live. Staging runs the same
settings module with test keys. Every refusal comes before the command reads
or writes anything, which the ``SimpleTestCase`` classes hold: they fail on
any database access.
"""
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from core.dev_guards import on_deployed_server, refuse_deployed_server, refuse_production
from vs_user.models import User

PRODUCTION = override_settings(SETTINGS_MODULE="apps.settings.staging", PAYSTACK_KEY_MODE="live")
STAGING = override_settings(SETTINGS_MODULE="apps.settings.staging", PAYSTACK_KEY_MODE="test")

#: Seeders that give CodeX staff a password from the code: a developer's machine or CI only.
LOCAL_ONLY = ("seed_dev_data", "seed_vision_staff")
#: Seeders that may fill a staging school, and the command that drops the schema: never production.
NOT_PRODUCTION = (
    "seed_test_school", "seed_onboarding_scenarios", "seed_staff_scenarios", "rebuild_database",
)


class GuardTests(SimpleTestCase):
    def test_a_developer_machine_is_not_a_deployed_server(self):
        self.assertFalse(on_deployed_server())
        refuse_production()
        refuse_deployed_server()

    @STAGING
    def test_staging_is_a_deployed_server_but_not_production(self):
        self.assertTrue(on_deployed_server())
        refuse_production()
        with self.assertRaisesMessage(CommandError, "deployed server"):
            refuse_deployed_server()

    @PRODUCTION
    def test_production_refuses_both(self):
        with self.assertRaisesMessage(CommandError, "production"):
            refuse_production()
        with self.assertRaisesMessage(CommandError, "production"):
            refuse_deployed_server()


class SeederRefusalTests(SimpleTestCase):
    @STAGING
    def test_platform_staff_seeders_refuse_staging(self):
        for command in LOCAL_ONLY:
            with self.subTest(command), self.assertRaisesMessage(CommandError, "deployed server"):
                call_command(command)

    @PRODUCTION
    def test_every_guarded_command_refuses_production(self):
        for command in LOCAL_ONLY + NOT_PRODUCTION:
            with self.subTest(command), self.assertRaisesMessage(CommandError, "production"):
                call_command(command)


class SuperuserDefaultPasswordTests(TestCase):
    @STAGING
    def test_a_server_refuses_the_password_written_in_the_code(self):
        with self.assertRaisesMessage(CommandError, "default password"):
            call_command("create_superuser", "--email", "root@codexng.com", stdout=None)
        self.assertFalse(User.objects.filter(email="root@codexng.com").exists())
