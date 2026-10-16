"""Which school a scenario seeder fills (``schools.vs_schools.dev.targets``).

Meridian Crest Academy was onboarded for real on staging, with an administrator
of its own. Its fill follows lagoon-view's recipe, so every table keyed by
school is read as lagoon-view's while every person is named at Meridian. The
same run on production, where Paystack takes live keys, is refused before it
writes anything.
"""
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from schools.vs_schools.dev.targets import Target, admin_email, quiet, resolve_targets

CAST = ("sunrise-academy", "lagoon-view")


def options(**given):
    return {"only": None, "school": None, "like": None, "admin_email": "", **given}


class ResolveTargetsTests(SimpleTestCase):
    def test_the_whole_cast_by_default_each_following_itself(self):
        self.assertEqual(
            resolve_targets(options(), CAST),
            [Target("sunrise-academy", "sunrise-academy"), Target("lagoon-view", "lagoon-view")],
        )

    def test_one_cast_member(self):
        self.assertEqual(resolve_targets(options(only="lagoon-view"), CAST), [Target("lagoon-view", "lagoon-view")])

    def test_a_school_outside_the_cast_follows_the_recipe_named(self):
        self.assertEqual(
            resolve_targets(options(
                school="meridian-crest-academy", like="lagoon-view",
                admin_email=" nneka.okafor@example.com ",
            ), CAST),
            [Target("meridian-crest-academy", "lagoon-view", "nneka.okafor@example.com")],
        )

    def test_a_school_needs_a_recipe_from_the_cast(self):
        with self.assertRaisesMessage(CommandError, "--school needs --like"):
            resolve_targets(options(school="meridian-crest-academy"), CAST)
        with self.assertRaisesMessage(CommandError, "--school needs --like"):
            resolve_targets(options(school="meridian-crest-academy", like="holy-cross"), CAST)

    def test_only_and_school_together_are_refused(self):
        with self.assertRaisesMessage(CommandError, "not both"):
            resolve_targets(options(only="lagoon-view", school="meridian-crest-academy", like="lagoon-view"), CAST)

    def test_a_slug_outside_the_cast_is_refused_without_school(self):
        with self.assertRaisesMessage(CommandError, "not in the cast"):
            resolve_targets(options(only="meridian-crest-academy"), CAST)

    @override_settings(PAYSTACK_KEY_MODE="live")
    def test_production_is_refused(self):
        with self.assertRaisesMessage(CommandError, "production"):
            resolve_targets(options(school="meridian-crest-academy", like="lagoon-view"), CAST)

    @override_settings(PAYSTACK_KEY_MODE="test")
    def test_staging_is_allowed(self):
        self.assertEqual(len(resolve_targets(options(only="lagoon-view"), CAST)), 1)


class AdminAndQuietTests(SimpleTestCase):
    def test_the_named_administrator_else_the_fixture_one(self):
        self.assertEqual(admin_email(Target("meridian-crest-academy", "lagoon-view", "nneka.okafor@example.com")), "nneka.okafor@example.com")
        self.assertEqual(admin_email(Target("lagoon-view", "lagoon-view")), "admin@lagoon-view.example.com")

    def test_quiet_holds_email_and_runs_tasks_here_then_puts_both_back(self):
        from django.conf import settings

        from apps.celery import app

        before = app.conf.task_always_eager
        with quiet():
            self.assertTrue(app.conf.task_always_eager)
            self.assertEqual(settings.EMAIL_BACKEND, "django.core.mail.backends.locmem.EmailBackend")
        self.assertEqual(app.conf.task_always_eager, before)
