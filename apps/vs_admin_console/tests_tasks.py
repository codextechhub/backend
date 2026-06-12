"""Tests for the Celery task-monitoring endpoints."""
from django.urls import reverse
from django.utils import timezone
from django_celery_results.models import TaskResult
from rest_framework import status
from rest_framework.test import APIClient
from django.test import TestCase

from vs_user.models import User


def _staff_user():
    return User.objects.create_user(
        email="monitor@codexng.com", password="x",
        user_type="CX_STAFF", status="ACTIVE",
        first_name="Mon", last_name="Itor", is_staff=True,
    )


def _result(name, task_status="SUCCESS", **extra):
    import uuid
    return TaskResult.objects.create(
        task_id=str(uuid.uuid4()), task_name=name, status=task_status, **extra,
    )


class TaskMonitorTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=_staff_user())

    def test_list_and_filters(self):
        _result("vs_import_data.tasks.execute_import_batch_task")
        _result("vs_user.tasks.send_invitation_email_task", task_status="FAILURE")

        url = reverse("tasks-list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 2)

        resp = self.client.get(url + "?status=FAILURE")
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertIn("invitation", resp.data["data"][0]["task_name"])

        resp = self.client.get(url + "?task=import")
        self.assertEqual(len(resp.data["data"]), 1)

    def test_stats(self):
        _result("a.b.c")
        _result("a.b.c", task_status="FAILURE", date_done=timezone.now())

        resp = self.client.get(reverse("tasks-stats"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["by_status"].get("FAILURE"), 1)
        self.assertEqual(len(data["recent_failures"]), 1)

    def test_schedule_lists_beat_entries(self):
        resp = self.client.get(reverse("tasks-schedule"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data["data"]
        names = {e["name"] for e in data["entries"]}
        self.assertIn("dispatch-pending-import-notifications", names)
        self.assertIn("eager_mode", data)

    def test_non_staff_denied(self):
        school_client = APIClient()
        resp = school_client.get(reverse("tasks-list"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
