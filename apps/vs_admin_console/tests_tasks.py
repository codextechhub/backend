"""Tests for the admin-console task monitoring endpoints (BackgroundJob-backed)."""
import uuid

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import BackgroundJob
from vs_tenants.models import Tenant
from vs_user.models import User


def _staff_user():
    return User.objects.create_user(
        email="monitor@codexng.com", password="x",
        user_type="CX_STAFF", status="ACTIVE",
        first_name="Mon", last_name="Itor", is_staff=True,
    )


def _job(name, job_status="SUCCEEDED", **extra):
    if "tenant" not in extra and "tenant_id" not in extra:
        extra["tenant"] = Tenant.objects.get(slug="codex")
    return BackgroundJob.objects.create(
        celery_task_id=str(uuid.uuid4()), task_name=name, status=job_status, **extra,
    )


class TaskMonitorTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=_staff_user())

    def test_list_and_filters(self):
        _job("vs_import_data.tasks.execute_import_batch_task", kind="import")
        _job("vs_user.tasks.send_invitation_email_task", job_status="FAILED", kind="email")

        url = reverse("tasks-list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 2)

        resp = self.client.get(url + "?status=FAILED")
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertIn("invitation", resp.data["data"][0]["task_name"])

        resp = self.client.get(url + "?task=import")
        self.assertEqual(len(resp.data["data"]), 1)

        resp = self.client.get(url + "?kind=email")
        self.assertEqual(len(resp.data["data"]), 1)

    def test_stats(self):
        _job("a.b.c")
        _job("a.b.c", job_status="FAILED", finished_at=timezone.now())

        resp = self.client.get(reverse("tasks-stats"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["by_status"].get("FAILED"), 1)
        self.assertEqual(len(data["recent_failures"]), 1)

    def test_schedule_lists_beat_entries(self):
        resp = self.client.get(reverse("tasks-schedule"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        names = {e["name"] for e in data["entries"]}
        self.assertIn("dispatch-pending-import-notifications", names)
        self.assertIn("prune-background-jobs", names)
        self.assertIn("eager_mode", data)

    def test_non_staff_denied(self):
        school_client = APIClient()
        resp = school_client.get(reverse("tasks-list"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
