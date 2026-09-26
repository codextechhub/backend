"""
Live progress for a running :class:`core.models.BackgroundJob`.

A task that does its work inside one transaction cannot report progress on its
own job row: every write it makes to that row belongs to the same transaction
and stays invisible to the screen polling it until the work commits, by which
time there is nothing left to report. A school's creation is exactly that
shape. It is all or nothing, and it takes long enough that the person who
started it wants to see where it has got to.

:class:`JobProgress` therefore writes on a connection of its own, opened for
the duration of the task and in autocommit, so each report is published the
moment it is made while the task's own transaction is still open. It writes
``progress`` and ``result`` and nothing else: the status, timing and outcome
still belong to :class:`core.tasks_base.TrackedTask`, whose terminal write
lands after the task returns.

``result`` carries the report while the job runs and the task's return value
once it succeeds. A failed job keeps the last report, which is how a screen
can say which step it stopped on and why.

Reporting is best-effort, like the rest of job tracking. A report that cannot
be written is logged and dropped; it never fails the work it describes. Only
a job that has not finished is touched, so a late report cannot overwrite an
outcome.
"""
from __future__ import annotations

import json
import logging

from django.db import DEFAULT_DB_ALIAS, connections

from core.redaction import redact_payload

logger = logging.getLogger(__name__)


class JobProgress:
    """Publish progress for the job with ``celery_task_id``.

    Use as a context manager around the work::

        with JobProgress(task_id) as progress:
            progress.report(40, {"current": "books"})
    """

    def __init__(self, celery_task_id: str):
        self.celery_task_id = str(celery_task_id or "")
        self._connection = None

    def __enter__(self) -> "JobProgress":
        if self.celery_task_id:
            try:
                self._connection = connections.create_connection(DEFAULT_DB_ALIAS)
            except Exception:  # pragma: no cover - reporting must never block the work
                logger.warning("Job progress channel could not open for %s", self.celery_task_id, exc_info=True)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # pragma: no cover
                logger.warning("Job progress channel did not close cleanly", exc_info=True)
            self._connection = None

    def report(self, progress: int, detail: dict) -> None:
        """Publish ``progress`` (0-100) and ``detail`` on the job, now."""
        if self._connection is None:
            return
        from core.models import BackgroundJob

        meta = BackgroundJob._meta
        quote = self._connection.ops.quote_name
        column = {name: quote(meta.get_field(name).column) for name in ("progress", "result", "celery_task_id", "status")}
        sql = (
            f"UPDATE {quote(meta.db_table)} "
            f"SET {column['progress']} = %s, {column['result']} = CAST(%s AS jsonb) "
            f"WHERE {column['celery_task_id']} = %s AND {column['status']} IN (%s, %s)"
        )
        params = [
            max(0, min(100, int(progress))),
            json.dumps(redact_payload(detail)),
            self.celery_task_id,
            BackgroundJob.Status.QUEUED,
            BackgroundJob.Status.RUNNING,
        ]
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql, params)
        except Exception:  # pragma: no cover - reporting must never block the work
            logger.warning("Job progress report failed for %s", self.celery_task_id, exc_info=True)
