"""Tests for BackgroundJob tracking (TrackedTask) and the me/tasks API."""
from celery import shared_task
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from core.models import BackgroundJob
from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
from vs_user.models import User


@shared_task
def _job_probe_ok(x):
    return {"doubled": x * 2}


@shared_task
def _job_probe_boom():
    raise RuntimeError("probe failure")


def _cx(email, *, platform_admin=False, **extra):
    user = User.objects.create_user(
        email=email, password="x", user_type="CX_STAFF", status="ACTIVE",
        first_name="T", last_name="User", **extra,
    )
    if platform_admin:
        # can_view_all_jobs reads the codex-tenant role assignment; CX_STAFF
        # users derive their tenant to the codex PLATFORM tenant on save.
        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=user.tenant, key="xvs_platform_admin",
            defaults={"name": "XVS Platform Admin", "status": "ACTIVE",
                      "is_system_role": True, "is_locked": True},
        )
        TenantUserRoleAssignment.objects.get_or_create(
            tenant=user.tenant, user=user, role=role,
            defaults={"assignment_status": "ACTIVE"},
        )
    return user


class TrackedTaskTests(TestCase):
    def setUp(self):
        self.owner = _cx("owner@codexng.com")
        # _notify_owner dispatches task.completed / task.failed through the
        # notification engine, which needs the event registry + DB templates.
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates,
        )
        seed_event_types()
        seed_notification_templates()

    def test_owned_task_lifecycle_and_notification(self):
        _job_probe_ok.delay(
            21,
            _job_owner_id=str(self.owner.id),
            _job_label="Probe job",
            _job_kind="export",
        )
        job = BackgroundJob.objects.get(owner=self.owner)
        self.assertEqual(job.status, BackgroundJob.Status.SUCCEEDED)
        self.assertEqual(job.kind, "export")
        self.assertEqual(job.result, {"doubled": 42})
        self.assertEqual(job.progress, 100)
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)

        # task.completed is IN_APP only; the label is carried in the body, so the
        # notification exists and names the job even though the subject is empty.
        from vs_notifications.constants import ChannelChoices
        from vs_notifications.models import Notification
        note = Notification.objects.get(
            recipient=self.owner, channel=ChannelChoices.IN_APP,
        )
        self.assertEqual(note.event_type.key, "task.completed")
        self.assertIn("Probe job", note.body)
        self.assertEqual(note.tenant_id, self.owner.tenant_id)

    def test_failure_recorded_with_error(self):
        with self.assertRaises(RuntimeError):
            _job_probe_boom.delay(
                _job_owner_id=str(self.owner.id), _job_label="Doomed job",
            )
        job = BackgroundJob.objects.get(owner=self.owner)
        self.assertEqual(job.status, BackgroundJob.Status.FAILED)
        self.assertIn("probe failure", job.error)

    def test_system_task_recorded_without_owner(self):
        _job_probe_ok.delay(1)
        job = BackgroundJob.objects.get(task_name__contains="_job_probe_ok")
        self.assertIsNone(job.owner_id)
        self.assertEqual(job.status, BackgroundJob.Status.SUCCEEDED)


class MyTasksAPITests(TestCase):
    def setUp(self):
        self.me = _cx("me@codexng.com")
        self.other = _cx("other@codexng.com")
        self.admin = _cx("padmin@codexng.com", platform_admin=True)
        BackgroundJob.objects.create(
            owner=self.me, tenant=self.me.tenant,
            celery_task_id="t-1", label="Mine", status="SUCCEEDED", kind="export",
        )
        BackgroundJob.objects.create(
            owner=self.other, tenant=self.other.tenant,
            celery_task_id="t-2", label="Theirs", status="FAILED", kind="import",
        )
        BackgroundJob.objects.create(
            tenant=self.admin.tenant,
            celery_task_id="t-3", label="", task_name="beat.thing", status="SUCCEEDED", kind="system",
        )
        self.client = APIClient()

    def test_mine_scope_shows_only_my_jobs(self):
        self.client.force_authenticate(self.me)
        resp = self.client.get(reverse("me-tasks"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        labels = [j["label"] for j in resp.data["data"]]
        self.assertEqual(labels, ["Mine"])

    def test_all_scope_requires_admin(self):
        self.client.force_authenticate(self.me)
        resp = self.client.get(reverse("me-tasks") + "?scope=all")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(self.admin)
        resp = self.client.get(reverse("me-tasks") + "?scope=all")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 3)

    def test_filters(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(reverse("me-tasks") + "?scope=all&status=FAILED")
        self.assertEqual(len(resp.data["data"]), 1)
        resp = self.client.get(reverse("me-tasks") + "?scope=all&kind=system")
        self.assertEqual(len(resp.data["data"]), 1)

    def test_summary_flags_admin_toggle(self):
        self.client.force_authenticate(self.me)
        resp = self.client.get(reverse("me-tasks-summary"))
        self.assertFalse(resp.data["data"]["can_view_all"])
        self.assertEqual(resp.data["data"]["total"], 1)

        self.client.force_authenticate(self.admin)
        resp = self.client.get(reverse("me-tasks-summary"))
        self.assertTrue(resp.data["data"]["can_view_all"])
