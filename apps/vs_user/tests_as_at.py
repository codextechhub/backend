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


class OrganisationAsAtTests(TestCase):
    """Ada's team, department and line manager on the day asked about.

    Ada sits in the Analyst seat in Payments, which reports to the Head of
    Payments seat. Grace holds that seat until 10 March, when Tunde takes it
    over, and on 15 March the Analyst seat moves to Treasury. A past view has
    to name the manager and the department of that day, not today's.
    """

    def setUp(self):
        from vs_user.models import OrgNode, Position, PositionAssignment

        with recorded_on(3, 1):
            self.person = make_vision_user(
                email="ada.org@codex.test", first_name="Ada", last_name="Obi",
            )
            self.grace = make_vision_user(
                email="grace.org@codex.test", first_name="Grace", last_name="Eze",
            )
            self.tunde = make_vision_user(
                email="tunde.org@codex.test", first_name="Tunde", last_name="Bello",
            )
            finance = OrgNode.objects.create(
                name="Finance", code="ASAT-FIN", kind=OrgNode.Kind.DIVISION,
            )
            self.payments = OrgNode.objects.create(
                name="Payments", code="ASAT-PAY", kind=OrgNode.Kind.DEPARTMENT, parent=finance,
            )
            self.treasury = OrgNode.objects.create(
                name="Treasury", code="ASAT-TRE", kind=OrgNode.Kind.DEPARTMENT, parent=finance,
            )
            head = Position.objects.create(
                title="Head of Payments", code="ASAT-HEAD", org_node=self.payments,
            )
            self.seat = Position.objects.create(
                title="Analyst", code="ASAT-AN", org_node=self.payments, reports_to=head,
            )
            self.profile = PlatformStaffProfile.objects.create(
                user=self.person, employee_id="CX-ORG-1", job_title="Analyst",
                position=self.seat,
            )
        PositionAssignment.objects.create(
            user=self.person, position=self.seat, is_primary=True,
            start_date=dt.date(2026, 3, 1),
        )
        PositionAssignment.objects.create(
            user=self.grace, position=head, is_primary=True,
            start_date=dt.date(2026, 3, 1), end_date=dt.date(2026, 3, 10),
        )
        PositionAssignment.objects.create(
            user=self.tunde, position=head, is_primary=True,
            start_date=dt.date(2026, 3, 10),
        )
        with recorded_on(3, 15):
            self.seat.org_node = self.treasury
            self.seat.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.person)

    def read(self, day):
        url = f"/v1/user/platform-staff-profiles/{self.profile.pk}/"
        response = self.client.get(url, {"as_at": day})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_the_line_manager_is_whoever_held_the_seat_that_day(self):
        self.assertEqual(self.read("2026-03-05")["current_line_manager"]["first_name"], "Grace")
        self.assertEqual(self.read("2026-03-10")["current_line_manager"]["first_name"], "Tunde")

    def test_the_department_is_the_one_the_seat_sat_in_that_day(self):
        before = self.read("2026-03-12")
        after = self.read("2026-03-15")
        self.assertEqual(before["department"]["name"], "Payments")
        self.assertEqual(before["division"]["name"], "Finance")
        self.assertEqual(before["org_node"]["name"], "Payments")
        self.assertEqual(after["department"]["name"], "Treasury")
        self.assertEqual(after["division"]["name"], "Finance")

    def test_before_the_organisation_history_the_unit_and_manager_are_left_empty(self):
        from vs_history.models import TrackingStart

        TrackingStart.objects.create(record_type="vs_user.orgnode", started_at=_at(3, 4))
        TrackingStart.objects.create(record_type="vs_user.position", started_at=_at(3, 1))
        data = self.read("2026-03-02")
        self.assertEqual(data["position"]["title"], "Analyst")
        self.assertIsNone(data["department"])
        self.assertIsNone(data["division"])
        self.assertIsNone(data["current_line_manager"])
        self.assertEqual(data["as_at"]["organisation_history_starts"], "2026-03-04")

    def test_the_live_profile_still_reads_todays_organisation(self):
        url = f"/v1/user/platform-staff-profiles/{self.profile.pk}/"
        data = self.client.get(url).data["data"]
        self.assertEqual(data["department"]["name"], "Treasury")
        self.assertEqual(data["current_line_manager"]["first_name"], "Tunde")
