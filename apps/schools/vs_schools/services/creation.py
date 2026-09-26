"""
Creating a school as a background job, with its steps reported as they finish.

Creating a school writes the school, its roles, its administrators, its
branches, its plan, its set of books and its onboarding checklist, all in one
transaction. On a deployed database that is many seconds of work, too long to
hold an HTTP request open for and too long to show nothing but a spinner.

So ``POST /i/create/`` validates the payload in the request, where a mistake
is still answered field by field, and hands the creation itself to
:func:`run_school_creation` on a worker. The job reports each step as it
starts, through :class:`core.job_progress.JobProgress`, and the console polls
``GET /i/create/<job_id>/`` to show them. The school is still all or nothing:
a step that fails rolls every earlier one back, and the job says which step it
stopped on.

The job re-validates the payload before it writes anything. The request's
check and the job's run are moments apart, and in that gap another operator
can take the slug or an administrator's email address. The second check is
what refuses the second school instead of colliding with the first.

The caller may name the job's id (a UUID it generated). That lets the console
start polling before the POST has answered, which matters wherever the job runs
inline in the request (local development, and the ``CELERY_EAGER`` lever on a
deployment).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from rest_framework.exceptions import ValidationError as DRFValidationError

from core.job_progress import JobProgress

logger = logging.getLogger(__name__)

#: Every step a creation can report, in the order it runs. A payload without
#: a school administrator or a plan skips those steps; the rest always run.
STEP_SCHOOL = "school"
STEP_ROLES = "roles"
STEP_SCHOOL_ADMIN = "school_admin"
STEP_BRANCHES = "branches"
STEP_PLAN = "plan"
STEP_BOOKS = "books"
STEP_ONBOARDING = "onboarding"
STEP_INVITATIONS = "invitations"

#: What the console is told when a failure carries no message fit to show.
GENERIC_FAILURE = (
    "The school could not be created, and nothing was saved. Try again, and "
    "contact support if it keeps happening."
)

TASK_NAME = "vs_schools.create_school"


def creation_steps(validated_data: dict) -> list[str]:
    """The steps a creation from ``validated_data`` will report, in order."""
    has_school_admin = bool(validated_data.get("primary_admin_data"))
    has_branch_admin = any(
        branch.get("primary_admin_data") for branch in validated_data.get("branches", [])
    )
    steps = [STEP_SCHOOL, STEP_ROLES]
    if has_school_admin:
        steps.append(STEP_SCHOOL_ADMIN)
    steps.append(STEP_BRANCHES)
    if validated_data.get("package_setup_data"):
        steps.append(STEP_PLAN)
    steps += [STEP_BOOKS, STEP_ONBOARDING]
    if has_school_admin or has_branch_admin:
        steps.append(STEP_INVITATIONS)
    return steps


def failure_message(exc: BaseException) -> str:
    """The sentence an operator is shown for ``exc``, never a stack trace.

    A validation refusal and an administrator that could not be provisioned
    both carry a message written for the person creating the school. Anything
    else is an internal fault, and its text (a constraint name, a SQL
    fragment) is for the diagnostic record, not the screen.
    """
    from schools.vs_schools.exceptions import AdminProvisioningError

    if isinstance(exc, DRFValidationError):
        return _first_message(exc.detail) or GENERIC_FAILURE
    if isinstance(exc, AdminProvisioningError):
        return str(exc) or exc.default_message
    return GENERIC_FAILURE


def _first_message(detail) -> str:
    """The first human sentence in a DRF error detail, however deeply nested."""
    if isinstance(detail, dict):
        if isinstance(detail.get("message"), str):
            return detail["message"]
        for value in detail.values():
            found = _first_message(value)
            if found:
                return found
        return ""
    if isinstance(detail, (list, tuple)):
        for value in detail:
            found = _first_message(value)
            if found:
                return found
        return ""
    return str(detail) if detail else ""


class _StepReporter:
    """Turns "this step is starting" into a published job report."""

    def __init__(self, progress: JobProgress, job_id: str):
        self.progress = progress
        self.job_id = job_id
        self.steps: list[str] = []
        self.current: str | None = None

    def start(self, steps: list[str]) -> None:
        self.steps = list(steps)
        self(self.steps[0])

    def __call__(self, step: str) -> None:
        if step not in self.steps:
            return
        self.current = step
        done = self.steps.index(step)
        self.progress.report(
            round(100 * done / len(self.steps)),
            {"steps": self.steps, "current": step},
        )

    def fail(self, message: str) -> None:
        """Record the step the creation stopped on, and why.

        Written on the task's own connection, after the creation's transaction
        has rolled back, so it lands even when the progress channel could not
        open. The job's terminal write keeps ``result`` as this leaves it.
        """
        from core.models import BackgroundJob
        from core.redaction import redact_payload

        detail = {"steps": self.steps, "current": self.current, "message": message}
        try:
            BackgroundJob.objects.filter(
                celery_task_id=self.job_id,
                status__in=(BackgroundJob.Status.QUEUED, BackgroundJob.Status.RUNNING),
            ).update(result=redact_payload(detail))
        except Exception:  # pragma: no cover - reporting must never mask the failure
            logger.warning("Could not record where school creation %s stopped", self.job_id, exc_info=True)


def run_school_creation(*, payload: dict, actor_id: str, job_id: str) -> dict:
    """Create the school ``payload`` describes, reporting each step on the job.

    Returns the school's slug and label with every step done, which is what the
    job's result holds once it succeeds. Raises on failure, so the job is
    recorded as failed, after publishing the step it stopped on and a message
    fit to show.
    """
    from vs_user.models import User

    from schools.vs_schools.serializers import SchoolCreateSerializer

    actor = User.objects.select_related("tenant").get(pk=actor_id)
    request = SimpleNamespace(user=actor, tenant=actor.tenant)

    with JobProgress(job_id) as progress:
        reporter = _StepReporter(progress, job_id)
        serializer = SchoolCreateSerializer(
            data=payload,
            context={"request": request, "actor_id": actor, "report_stage": reporter},
        )
        try:
            serializer.is_valid(raise_exception=True)
            reporter.start(creation_steps(serializer.validated_data))
            school = serializer.save()
        except Exception as exc:
            reporter.fail(failure_message(exc))
            raise

    return {
        # "label": the result redactor masks any key called "name".
        "school": {"slug": school.slug, "label": school.name},
        "steps": reporter.steps,
        "current": None,
    }


def creation_job_state(job) -> dict:
    """What ``GET /i/create/<job_id>/`` answers for ``job``.

    ``done`` lists the finished steps. A running job has finished every step
    before ``current``; a succeeded one has finished them all; a failed one
    finished those before the step it stopped on, and ``message`` says why.
    """
    result = job.result if isinstance(job.result, dict) else {}
    steps = [step for step in result.get("steps") or [] if isinstance(step, str)]
    current = result.get("current") if isinstance(result.get("current"), str) else None
    status = job.status

    if status == job.Status.SUCCEEDED:
        done, current = steps, None
    elif current in steps:
        done = steps[: steps.index(current)]
    else:
        done = []

    message = None
    if status == job.Status.FAILED:
        message = result.get("message") if isinstance(result.get("message"), str) else None
        message = message or GENERIC_FAILURE

    school = result.get("school") if status == job.Status.SUCCEEDED else None
    return {
        "job_id": job.celery_task_id,
        "status": status,
        "steps": steps,
        "done": done,
        "current": current,
        "school": school if isinstance(school, dict) else None,
        "message": message,
    }
