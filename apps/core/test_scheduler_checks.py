"""The system check that reports a deployment where no scheduled task can run.

The condition it catches is silent by construction. Eager mode has no beat
scheduler, so every periodic task - overdue-fee dunning, the undispatched-payout
recovery sweep, the unbooked-gateway-money alarms - simply never fires, and none
of them reports its own absence. The schedule still lists them, the code still
looks right, and a school is quietly never chased for its fees.

These tests pin the three things that make the check useful rather than noise:
it is silent where eager mode is correct, it speaks where it is not, and what it
says names the tasks that are not running.
"""
from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from core.checks import CHECK_ID, check_scheduled_tasks_can_run


class SchedulerCheckTests(SimpleTestCase):
    """No database, no broker: the check reads settings and nothing else."""

    @override_settings(DEBUG=True, CELERY_TASK_ALWAYS_EAGER=True)
    def test_silent_in_development(self):
        """Eager mode is the right answer locally.

        Firing here would train everyone to scroll past the message, which is
        how the real condition went unnoticed for months.
        """
        self.assertEqual(check_scheduled_tasks_can_run(), [])

    @override_settings(DEBUG=False, CELERY_TASK_ALWAYS_EAGER=False)
    def test_silent_when_a_worker_will_run_the_tasks(self):
        self.assertEqual(check_scheduled_tasks_can_run(), [])

    @override_settings(DEBUG=False, CELERY_TASK_ALWAYS_EAGER=True)
    def test_reports_a_production_deployment_in_eager_mode(self):
        messages = check_scheduled_tasks_can_run()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, CHECK_ID)

    @override_settings(DEBUG=False, CELERY_TASK_ALWAYS_EAGER=True)
    def test_the_hint_names_what_is_not_running(self):
        """A count and some names, because "misconfigured" is not actionable.

        The reader has to be able to tell without leaving the message that this
        is not a tuning knob - it is the reason nobody has been chased for fees.
        """
        hint = check_scheduled_tasks_can_run()[0].hint

        self.assertIn("CELERY_EAGER=false", hint)
        self.assertIn("dunning", hint)
        self.assertIn("core.W001", hint)

    @override_settings(DEBUG=False, CELERY_TASK_ALWAYS_EAGER=True)
    def test_the_check_is_registered_and_runs_with_the_others(self):
        """Registration is the whole delivery mechanism.

        The check being correct is worth nothing if ``CoreConfig.ready`` stops
        importing the module, and nothing else in the suite would notice.
        """
        from django.core.checks import run_checks

        ids = {message.id for message in run_checks()}
        self.assertIn(CHECK_ID, ids)
