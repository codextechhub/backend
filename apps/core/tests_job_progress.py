"""JobProgress publishes on a connection of its own, outside the caller's work.

A task that does its writing inside one transaction cannot report progress on
its own job row through that transaction: the update would be invisible until
the work commits, by which point there is nothing left to report.
:class:`core.job_progress.JobProgress` sidesteps this by writing on a separate
connection in autocommit, so each report lands the instant it is made.

These tests prove the two properties the console relies on: a report survives
the rollback of the transaction the task is running in, and a report never
disturbs a job that has already finished.
"""
from django.db import transaction
from django.test import TransactionTestCase, tag

from core.job_progress import JobProgress
from core.models import BackgroundJob
from vs_tenants.models import Tenant


class _Rollback(Exception):
    """Raised to roll a transaction back once its point is made."""


@tag("slow")
class JobProgressCommitsOutsideTheCallerTests(TransactionTestCase):
    """The report is committed on its own connection, so a rollback keeps it.

    A real second connection only sees committed rows, which is why this is a
    ``TransactionTestCase``: under the wrapped ``TestCase`` the job row would sit
    in an uncommitted transaction the progress connection could never read.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Progress School", slug="progress-school",
            kind=Tenant.Kind.SCHOOL,
        )

    def _job(self, *, status, **extra):
        return BackgroundJob.objects.create(
            tenant=self.tenant,
            celery_task_id=f"progress-{status.lower()}",
            task_name="vs_schools.create_school",
            status=status,
            **extra,
        )

    def test_a_report_survives_the_rollback_of_the_callers_transaction(self):
        job = self._job(status=BackgroundJob.Status.RUNNING, progress=0)

        try:
            with transaction.atomic():
                with JobProgress(job.celery_task_id) as progress:
                    progress.report(
                        40, {"steps": ["books"], "current": "books"},
                    )
                # Undo everything this transaction did; the report is not part
                # of it, so it must remain.
                raise _Rollback()
        except _Rollback:
            pass

        job.refresh_from_db()
        self.assertEqual(job.progress, 40)
        self.assertEqual(job.result["current"], "books")
        self.assertEqual(job.result["steps"], ["books"])

    def test_a_report_does_not_touch_a_job_that_has_already_finished(self):
        """A late report cannot overwrite an outcome the job already recorded."""
        job = self._job(
            status=BackgroundJob.Status.SUCCEEDED,
            progress=100,
            result={"outcome": "done"},
        )

        with JobProgress(job.celery_task_id) as progress:
            progress.report(50, {"current": "too-late"})

        job.refresh_from_db()
        self.assertEqual(job.status, BackgroundJob.Status.SUCCEEDED)
        self.assertEqual(job.progress, 100)
        self.assertEqual(job.result, {"outcome": "done"})
