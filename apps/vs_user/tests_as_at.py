"""A CX staff profile read as it stood at the end of an earlier day.

The recorder's clock is patched for each change, so every test reads the
profile before and after a change and proves the page answers with what the
profile and its account held on that day.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from vs_history.as_at import RECORD_DAY_TIMEZONE
from vs_rbac.tests.helpers import make_vision_user
from vs_user.models import PlatformStaffProfile


def _at(month, day, hour=10):
    return dt.datetime(2026, month, day, hour, tzinfo=RECORD_DAY_TIMEZONE)


@contextmanager
def recorded_on(month, day):
    with mock.patch("vs_history.recorder.timezone.now", return_value=_at(month, day)):
        yield


class PlatformStaffProfileAsAtTests(TestCase):
    def setUp(self):
        with recorded_on(3, 1):
            self.person = make_vision_user(
                email="ada.asat@codex.test", first_name="Ada", last_name="Obi",
            )
            self.profile = PlatformStaffProfile.objects.create(
                user=self.person, employee_id="CX-ASAT-1", job_title="Analyst",
            )
        self.client = APIClient()
        self.client.force_authenticate(user=self.person)

    def read(self, day=None):
        url = f"/v1/user/platform-staff-profiles/{self.profile.pk}/"
        return self.client.get(url, {"as_at": day} if day else {})

    def test_the_live_profile_names_the_day_its_history_starts(self):
        data = self.read().data["data"]
        self.assertEqual(data["history_starts"], "2026-03-01")
        self.assertNotIn("as_at", data)

    def test_the_job_title_and_the_name_read_as_they_were(self):
        with recorded_on(3, 10):
            self.profile.job_title = "Senior Analyst"
            self.profile.save()
            self.person.last_name = "Adeyemi"
            self.person.save(update_fields=["last_name"])
        before = self.read("2026-03-05").data["data"]
        after = self.read("2026-03-10").data["data"]
        self.assertEqual(before["job_title"], "Analyst")
        self.assertEqual(before["user"]["last_name"], "Obi")
        self.assertEqual(before["as_at"]["date"], "2026-03-05")
        self.assertEqual(after["job_title"], "Senior Analyst")
        self.assertEqual(after["user"]["last_name"], "Adeyemi")

    def test_a_day_before_the_history_starts_is_refused(self):
        response = self.read("2026-02-01")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["detail"]["history_starts"], "2026-03-01")
