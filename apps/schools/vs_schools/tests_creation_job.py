"""Creating a school as a background job: the 202 contract and its progress feed.

``POST /i/create/`` validates the payload in the request and hands the writing
to a worker. A successful call answers ``202`` carrying the job's state, and
``GET /i/create/<job_id>/`` keeps answering that state until the work finishes.
Under eager Celery, which is how the tests and local development run, the worker
has already finished by the time the POST returns, so the job the POST answers
with is already terminal.

These tests pin the contract the console depends on: the shape of the state, the
steps a payload reports, that a validation refusal queues nothing, that a
caller-supplied job id is honoured and cannot be reused, that only the operator
who started a job may read it, and that a failure inside the creation is
recorded as a failed job whose message is fit to show and never a raw traceback.
"""
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import BackgroundJob
from vs_rbac.tests.helpers import make_vision_user

from .exceptions import AdminProvisioningError
from .models import School
from .services.creation import (
    GENERIC_FAILURE,
    STEP_BOOKS,
    STEP_BRANCHES,
    STEP_INVITATIONS,
    STEP_ONBOARDING,
    STEP_PLAN,
    STEP_ROLES,
    STEP_SCHOOL,
    STEP_SCHOOL_ADMIN,
    TASK_NAME,
)


def _branch(name="Main Branch", *, email, is_main=True):
    """The smallest branch a school can be created with, its admin included."""
    return {
        "name": name,
        "_type": "Main" if is_main else "Annex",
        "state": "Lagos",
        "is_main": is_main,
        "primary_admin_data": {"full_name": f"{name} Head", "email": email},
    }


class SchoolCreationJobContractTests(TestCase):
    """The state a create returns and the progress the console then polls."""

    @classmethod
    def setUpTestData(cls):
        cls.operator = make_vision_user(
            email="creation-job@example.com", super_admin=True,
        )

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.operator)
        return client

    def _create(self, payload, *, client=None):
        """POST a create with the invitation dispatch mocked out."""
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                return (client or self._client()).post(
                    reverse("school-create"), payload, format="json",
                )

    # --- the success contract ---------------------------------------------

    def test_a_create_succeeds_and_answers_the_finished_job(self):
        response = self._create({
            "name": "Bright Star", "slug": "bright-star",
            "branches": [_branch(email="head@bright-star.test")],
        })

        self.assertEqual(response.status_code, 202, response.data)
        data = response.data["data"]
        self.assertEqual(data["status"], "SUCCEEDED")
        self.assertTrue(School.objects.filter(slug="bright-star").exists())
        self.assertEqual(data["school"]["slug"], "bright-star")
        self.assertEqual(data["current"], None)
        # Every reported step is finished on a succeeded job.
        self.assertEqual(data["done"], data["steps"])
        self.assertIn(STEP_SCHOOL, data["steps"])
        self.assertIn(STEP_BRANCHES, data["steps"])

    def test_the_steps_omit_school_admin_and_plan_when_neither_is_asked_for(self):
        """A payload with no school administrator and no plan reports neither step."""
        response = self._create({
            "name": "No Extras", "slug": "no-extras",
            "branches": [_branch(email="head@no-extras.test")],
        })

        steps = response.data["data"]["steps"]
        self.assertNotIn(STEP_SCHOOL_ADMIN, steps)
        self.assertNotIn(STEP_PLAN, steps)
        # The steps that always run are still there and in order.
        self.assertEqual(
            steps,
            [STEP_SCHOOL, STEP_ROLES, STEP_BRANCHES, STEP_BOOKS, STEP_ONBOARDING,
             STEP_INVITATIONS],
        )

    # --- validation refuses before anything is queued ---------------------

    def test_a_validation_refusal_queues_no_job(self):
        """A payload refused field by field is a 400 that starts no work."""
        before = BackgroundJob.objects.count()

        response = self._client().post(
            reverse("school-create"),
            {"name": "Nowhere", "slug": "nowhere"},  # no branches
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(BackgroundJob.objects.count(), before)
        self.assertFalse(School.objects.filter(slug="nowhere").exists())

    # --- the caller's job id ----------------------------------------------

    def test_a_caller_supplied_job_id_is_the_job_id(self):
        job_id = "11111111-1111-4111-8111-111111111111"
        response = self._create({
            "job_id": job_id,
            "name": "Named Job", "slug": "named-job",
            "branches": [_branch(email="head@named-job.test")],
        })

        self.assertEqual(response.data["data"]["job_id"], job_id)
        self.assertTrue(
            BackgroundJob.objects.filter(celery_task_id=job_id).exists(),
        )

    def test_a_reused_job_id_is_refused(self):
        job_id = "22222222-2222-4222-8222-222222222222"
        self._create({
            "job_id": job_id,
            "name": "First Use", "slug": "first-use",
            "branches": [_branch(email="head@first-use.test")],
        })

        response = self._create({
            "job_id": job_id,
            "name": "Second Use", "slug": "second-use",
            "branches": [_branch(email="head@second-use.test")],
        })

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("job_id", str(response.data))
        self.assertFalse(School.objects.filter(slug="second-use").exists())

    def test_a_job_id_that_is_not_a_uuid_is_refused(self):
        response = self._client().post(
            reverse("school-create"),
            {
                "job_id": "not-a-uuid",
                "name": "Bad Id", "slug": "bad-id",
                "branches": [_branch(email="head@bad-id.test")],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("job_id", str(response.data))

    # --- who may read a job -----------------------------------------------

    def test_the_owner_reads_the_job_state(self):
        created = self._create({
            "name": "Readable", "slug": "readable",
            "branches": [_branch(email="head@readable.test")],
        })
        job_id = created.data["data"]["job_id"]

        response = self._client().get(
            reverse("school-create-job", kwargs={"job_id": job_id}),
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["job_id"], job_id)
        self.assertEqual(response.data["data"]["status"], "SUCCEEDED")

    def test_another_operator_cannot_read_someone_elses_job(self):
        """A job id that is not the caller's answers 404, the same as a missing one."""
        created = self._create({
            "name": "Private", "slug": "private",
            "branches": [_branch(email="head@private.test")],
        })
        job_id = created.data["data"]["job_id"]

        other = make_vision_user(
            email="other-operator@example.com", super_admin=True,
        )
        response = self._client(other).get(
            reverse("school-create-job", kwargs={"job_id": job_id}),
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_reading_a_job_needs_the_create_permission(self):
        created = self._create({
            "name": "Gated", "slug": "gated",
            "branches": [_branch(email="head@gated.test")],
        })
        job_id = created.data["data"]["job_id"]

        # A platform account with no role holds no permission key.
        nobody = make_vision_user(email="no-permission@example.com")
        response = self._client(nobody).get(
            reverse("school-create-job", kwargs={"job_id": job_id}),
        )

        self.assertEqual(response.status_code, 403, response.data)

    # --- failures are recorded, not raw ------------------------------------

    def test_a_provisioning_failure_yields_a_failed_job_that_says_where_it_stopped(self):
        """An administrator that cannot be provisioned fails the job on that step."""
        message = "Ada could not be provisioned. Try again."
        with mock.patch(
            "schools.vs_schools.services.admin_provisioning.provision_admin_user",
            side_effect=AdminProvisioningError(message),
        ), mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            response = self._client().post(
                reverse("school-create"),
                {
                    "name": "Stops Early", "slug": "stops-early",
                    "primary_admin_data": {
                        "full_name": "Ada Obi", "email": "ada@stops-early.test",
                    },
                    "branches": [_branch(email="branch@stops-early.test")],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202, response.data)
        data = response.data["data"]
        self.assertEqual(data["status"], "FAILED")
        self.assertEqual(data["current"], STEP_SCHOOL_ADMIN)
        # The steps finished before the one it stopped on.
        self.assertEqual(data["done"], [STEP_SCHOOL, STEP_ROLES])
        self.assertEqual(data["message"], message)
        self.assertFalse(School.objects.filter(slug="stops-early").exists())

    def test_an_unexpected_failure_is_shown_as_a_generic_message(self):
        """A fault that is not a written refusal never leaks its own text.

        The exception below carries a database URL with a password. What reaches
        the console is the fixed sentence, and the raw text stays in the
        diagnostic record the failure handler keeps server-side.
        """
        with mock.patch(
            "schools.vs_schools.services.admin_provisioning.provision_admin_user",
            side_effect=RuntimeError("psql://user:hunter2@db.internal/cx"),
        ), mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            response = self._client().post(
                reverse("school-create"),
                {
                    "name": "Blows Up", "slug": "blows-up",
                    "primary_admin_data": {
                        "full_name": "Ada Obi", "email": "ada@blows-up.test",
                    },
                    "branches": [_branch(email="branch@blows-up.test")],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 202, response.data)
        data = response.data["data"]
        self.assertEqual(data["status"], "FAILED")
        self.assertEqual(data["message"], GENERIC_FAILURE)
        self.assertNotIn("hunter2", data["message"])
        self.assertNotIn("RuntimeError", data["message"])
        self.assertFalse(School.objects.filter(slug="blows-up").exists())

    def test_the_created_job_carries_the_creation_task_name(self):
        """The GET view keys on the task name, so the create must stamp it."""
        created = self._create({
            "name": "Named Task", "slug": "named-task",
            "branches": [_branch(email="head@named-task.test")],
        })
        job = BackgroundJob.objects.get(
            celery_task_id=created.data["data"]["job_id"],
        )

        self.assertEqual(job.task_name, TASK_NAME)
        self.assertEqual(job.owner_id, self.operator.pk)
