"""Tests for the central redaction of stored and logged task failures.

The endpoint tests in ``vs_admin_console`` prove who may open the door. These
prove that what is behind it was scrubbed on the way in - which is the half
that survives someone building a new endpoint tomorrow, or reading the row
straight out of a nightly backup.
"""
import logging
import uuid

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import BackgroundJob, TaskDiagnostic
from core.redaction import (
    RedactingLogFilter,
    is_sensitive_key,
    redact_payload,
    redact_text,
)
from vs_tenants.models import Tenant

#: The exact string Postgres produces for a duplicate guardian email. This is
#: the payload the whole change exists to stop storing.
PG_DUPLICATE = (
    'duplicate key value violates unique constraint "vs_user_user_email_key"\n'
    "DETAIL:  Key (email)=(ada.okeye@gmail.com) already exists."
)


class RedactTextTests(TestCase):
    def test_postgres_detail_value_is_removed(self):
        out = redact_text(PG_DUPLICATE)
        self.assertNotIn("ada.okeye@gmail.com", out)
        # The constraint name survives: it is what tells an operator which
        # rule was broken, and it names a column rather than a person.
        self.assertIn("vs_user_user_email_key", out)

    def test_detail_value_of_a_shape_no_other_rule_matches(self):
        """A guardian's NAME is not an email or a digit run, and still goes."""
        out = redact_text("Key (full_name)=(Tunde Bello) already exists.")
        self.assertNotIn("Tunde Bello", out)

    def test_smtp_refusal_loses_the_recipient(self):
        out = redact_text(
            "SMTPRecipientsRefused: {'ada.okeye@gmail.com': (550, b'No such user')}"
        )
        self.assertNotIn("ada.okeye@gmail.com", out)
        self.assertIn("550", out)

    def test_bank_account_number_is_removed(self):
        self.assertNotIn("0123456789", redact_text("payout to 0123456789 failed"))

    def test_ordinary_diagnostics_are_left_alone(self):
        message = "Imported 42 rows in 3 seconds"
        self.assertEqual(redact_text(message), message)

    def test_non_strings_pass_through(self):
        self.assertIsNone(redact_text(None))
        self.assertEqual(redact_text(7), 7)


class RedactPayloadTests(TestCase):
    def test_sensitive_keys_lose_their_value_whatever_it_looks_like(self):
        out = redact_payload({"guardian_email": "x@y.com", "address": "12 Awolowo Rd"})
        self.assertNotIn("x@y.com", str(out))
        self.assertNotIn("Awolowo", str(out))

    def test_counts_and_task_names_survive(self):
        """The fields the monitor exists to show must not be collateral."""
        out = redact_payload({
            "task_name": "vs_import_data.execute_import_batch_task",
            "template_name": "guardians",
            "file_name": "guardians.xlsx",
            "processed_rows": 214,
            "failed_rows": 1,
        })
        self.assertEqual(out["processed_rows"], 214)
        self.assertEqual(out["template_name"], "guardians")
        self.assertEqual(out["file_name"], "guardians.xlsx")

    def test_free_text_inside_a_payload_is_scrubbed(self):
        out = redact_payload({"notes": "could not mail ada.okeye@gmail.com"})
        self.assertNotIn("ada.okeye@gmail.com", out["notes"])

    def test_nested_and_listed_values_are_reached(self):
        out = redact_payload({"rows": [{"email": "a@b.com"}, {"count": 2}]})
        self.assertEqual(out["rows"][0]["email"], "[redacted]")
        self.assertEqual(out["rows"][1]["count"], 2)

    def test_name_is_exact_match_only(self):
        self.assertTrue(is_sensitive_key("name"))
        self.assertFalse(is_sensitive_key("task_name"))


class RedactingLogFilterTests(TestCase):
    def _record(self, msg, args=(), exc_info=None):
        return logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1,
            msg=msg, args=args, exc_info=exc_info,
        )

    def test_message_and_args_are_scrubbed(self):
        record = self._record("delivering to %s", ("ada.okeye@gmail.com",))
        RedactingLogFilter().filter(record)
        self.assertNotIn("ada.okeye@gmail.com", record.getMessage())

    def test_exception_text_is_scrubbed(self):
        """The leak that matters: ``logger.warning(..., exc_info=True)``.

        Scrubbing only msg and args would leave every traceback in the stream
        carrying the exception message, which is exactly where Postgres puts
        the duplicated address.
        """
        try:
            raise ValueError(PG_DUPLICATE)
        except ValueError:
            import sys
            record = self._record("task failed", exc_info=sys.exc_info())

        RedactingLogFilter().filter(record)
        self.assertNotIn("ada.okeye@gmail.com", record.exc_text)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class TrackedTaskStorageTests(TestCase):
    """The choke point: what actually lands in the two tables."""

    def setUp(self):
        self.tenant = Tenant.objects.get(slug="codex")
        self.job = BackgroundJob.objects.create(
            celery_task_id=str(uuid.uuid4()),
            tenant=self.tenant, task_name="t.import",
            status=BackgroundJob.Status.RUNNING,
        )

    def _finish(self, **kwargs):
        from core.tasks_base import TrackedTask

        task = TrackedTask()
        task._finish(self.job.celery_task_id, **kwargs)
        self.job.refresh_from_db()

    def test_failure_stores_redacted_on_the_job(self):
        self._finish(
            succeeded=False, error=PG_DUPLICATE, traceback_text=PG_DUPLICATE,
        )
        self.assertNotIn("ada.okeye@gmail.com", self.job.error)
        self.assertNotIn("ada.okeye@gmail.com", self.job.traceback)
        self.assertEqual(self.job.status, BackgroundJob.Status.FAILED)

    def test_failure_keeps_the_raw_text_in_the_diagnostic(self):
        self._finish(
            succeeded=False, error=PG_DUPLICATE, traceback_text=PG_DUPLICATE,
        )
        diagnostic = TaskDiagnostic.objects.get(job=self.job)
        self.assertIn("ada.okeye@gmail.com", diagnostic.raw_error)
        self.assertEqual(diagnostic.tenant_id, self.tenant.pk)
        self.assertGreater(diagnostic.expires_at, timezone.now())

    def test_success_result_is_redacted_and_keeps_no_raw_copy(self):
        self._finish(
            succeeded=True,
            retval={"processed_rows": 3, "notes": "mailed ada.okeye@gmail.com"},
        )
        self.assertEqual(self.job.result["processed_rows"], 3)
        self.assertNotIn("ada.okeye@gmail.com", self.job.result["notes"])
        self.assertFalse(TaskDiagnostic.objects.filter(job=self.job).exists())

    def test_owner_facing_serializer_serves_the_redacted_column(self):
        """The school-facing endpoint reads the same column, so it is covered.

        ``/v1/user/me/tasks/`` serialises ``error``. Scrubbing at the choke
        point is what makes that endpoint safe without changing it.
        """
        from vs_user.views.jobs import BackgroundJobSerializer

        self._finish(succeeded=False, error=PG_DUPLICATE)
        data = BackgroundJobSerializer(self.job).data
        self.assertNotIn("ada.okeye@gmail.com", data["error"])


class PruneDiagnosticsTests(TestCase):
    def test_only_expired_rows_are_pruned(self):
        from core.tasks import prune_task_diagnostics_task

        tenant = Tenant.objects.get(slug="codex")
        live, expired = [
            TaskDiagnostic.objects.create(
                job=BackgroundJob.objects.create(
                    celery_task_id=str(uuid.uuid4()), tenant=tenant,
                    status=BackgroundJob.Status.FAILED,
                ),
                tenant=tenant, expires_at=timezone.now() + delta,
            )
            for delta in (timezone.timedelta(days=1), timezone.timedelta(days=-1))
        ]

        result = prune_task_diagnostics_task()
        self.assertEqual(result["pruned"], 1)
        self.assertTrue(TaskDiagnostic.objects.filter(pk=live.pk).exists())
        self.assertFalse(TaskDiagnostic.objects.filter(pk=expired.pk).exists())
