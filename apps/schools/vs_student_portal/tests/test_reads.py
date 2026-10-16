"""A pupil's own timetable, attendance and profile in the portal.

Brightfield's JSS1 A has a published timetable: English with Mr. Eze and
Maths with Mrs. Bello on Monday, then break. The school clock is Monday 13
October 2025; 13:00 in Lagos by default, and 08:20 in the tests that need a
lesson in progress. Chiamaka (BFS/001) is in JSS1 A and has portal access;
Zainab is in JSS1 B and has none.
"""
from __future__ import annotations

import datetime as dt

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_attendance.tests.base import NOW, TODAY, school_clock
from schools.vs_attendance.tests.lesson_base import LessonFixture
from schools.vs_calendar.models import PublishState
from schools.vs_attendance.tests.lesson_base import publish
from schools.vs_students.services import portal_access
from vs_user.models import User

#: A one-pixel PNG, enough for the upload checks.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
    b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class PortalReadsFixture(LessonFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.chiamaka.email = "chiamaka.n@example.ng"
        cls.chiamaka.save(update_fields=["email"])
        portal_access.issue(cls.chiamaka, actor=cls.admin)
        cls.chiamaka.refresh_from_db()
        User.objects.filter(pk=cls.chiamaka.user_id).update(
            status=User.Status.ACTIVE, is_active=True,
        )
        cls.pupil = User.objects.get(pk=cls.chiamaka.user_id)

    def portal(self, name, *, data=None, method="get", **params):
        client = TenantAPIClient(self.pupil)
        url = reverse(name)
        if method == "get":
            return client.get(url, params)
        return getattr(client, method)(url, data or {}, format="json")


class TimetableTests(PortalReadsFixture):
    def test_the_pupil_reads_their_published_class_grid(self):
        data = self.portal("student-portal-timetable").data["data"]
        self.assertTrue(data["published"])
        self.assertEqual(data["class_name"], self.jss1a.name)
        monday = next(day for day in data["days"] if day["day_label"] == "Monday")
        lessons = [cell for cell in monday["cells"] if cell["kind"] == "LESSON"]
        self.assertEqual(lessons[0]["subject"], "English Language")
        self.assertEqual(lessons[0]["teacher"], self.eze.full_name)
        self.assertTrue(monday["is_today"])

    def test_a_draft_grid_is_not_shown(self):
        publish(self.tenant, self.jss1a, status=PublishState.DRAFT)
        data = self.portal("student-portal-timetable").data["data"]
        self.assertFalse(data["published"])
        self.assertEqual(data["days"], [])

    def test_today_marks_the_lesson_in_progress_and_the_next_one(self):
        with school_clock(dt.datetime(2025, 10, 13, 7, 20, tzinfo=dt.timezone.utc)):
            data = self.portal("student-portal-timetable").data["data"]
        states = [period["state"] for period in data["today"]["periods"]]
        self.assertEqual(states[:3], ["now", "next", "later"])

    def test_the_grid_carries_no_staff_detail(self):
        data = self.portal("student-portal-timetable").data["data"]
        self.assertNotIn("warnings", data)
        cell = data["days"][0]["cells"][0]
        self.assertNotIn("slot", cell)


class AttendanceTests(PortalReadsFixture):
    def test_a_late_day_is_noted_with_its_arrival_time(self):
        self.mark(self.admin, self.jss1a, self.chiamaka, "L", arrived_at="08:20", note="Bus broke down")
        self.fill(self.admin, self.jss1a)
        self.submit(self.admin, self.jss1a)
        data = self.portal("student-portal-attendance").data["data"]
        self.assertEqual(data["counts"]["late"], 1)
        note = data["notes"][0]
        self.assertEqual(note["mark"], "L")
        self.assertEqual(note["note"], "Bus broke down")
        self.assertEqual(data["today"]["mark"], "L")

    def test_the_pupils_record_carries_no_guardian_or_history(self):
        data = self.portal("student-portal-attendance").data["data"]
        for hidden in ("guardian", "history", "absences", "student"):
            self.assertNotIn(hidden, data)

    def test_a_term_of_another_school_is_not_found(self):
        response = self.portal("student-portal-attendance", term="999999")
        self.assertEqual(response.status_code, 404)

    def test_a_term_that_is_not_a_number_is_not_found(self):
        self.assertEqual(self.portal("student-portal-attendance", term="first").status_code, 404)


class ProfileTests(PortalReadsFixture):
    def test_the_profile_splits_what_the_school_manages_from_what_the_pupil_edits(self):
        data = self.portal("student-portal-profile").data["data"]
        managed = data["managed_by_school"]
        self.assertEqual(managed["admission_number"], "BFS/001")
        self.assertEqual(managed["school_email"], "chiamaka.n@example.ng")
        self.assertEqual(data["editable"]["email"], "chiamaka.n@example.ng")
        self.assertTrue(data["photo_editable"])

    def test_the_pupil_changes_their_phone_and_it_is_audited(self):
        from vs_audit.models import AuditEvent

        response = self.portal(
            "student-portal-profile", method="patch", data={"phone": "0803 456 7890"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.chiamaka.refresh_from_db()
        self.assertEqual(self.chiamaka.phone, "0803 456 7890")
        event = AuditEvent.objects.get(
            entity_id=str(self.chiamaka.pk), summary__contains="changed their own contact details",
        )
        self.assertEqual(event.actor_user_id, self.pupil.pk)

    def test_a_bad_phone_is_refused_on_its_field_and_nothing_changes(self):
        response = self.portal("student-portal-profile", method="patch", data={"phone": "12"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["detail"]["field"], "phone")
        self.chiamaka.refresh_from_db()
        self.assertNotEqual(self.chiamaka.phone, "12")

    def test_changing_the_record_email_leaves_the_sign_in_alone(self):
        self.portal("student-portal-profile", method="patch", data={"email": "chi.new@example.ng"})
        self.pupil.refresh_from_db()
        self.assertEqual(self.pupil.email, "chiamaka.n@example.ng")

    def test_the_pupil_cannot_change_anything_else(self):
        self.portal(
            "student-portal-profile", method="patch",
            data={"first_name": "Someone", "student_number": "X/1", "date_of_birth": "2000-01-01"},
        )
        self.chiamaka.refresh_from_db()
        self.assertEqual(self.chiamaka.first_name, "Chiamaka")
        self.assertEqual(self.chiamaka.student_number, "BFS/001")

    def test_the_pupil_uploads_their_own_photograph(self):
        client = TenantAPIClient(self.pupil)
        upload = SimpleUploadedFile("me.png", PNG, content_type="image/png")
        response = client.post(
            reverse("student-portal-profile-photo"), {"file": upload}, format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_school_that_fixes_photographs_refuses_the_upload(self):
        from django.core.management import call_command
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import set_value

        call_command("seed_config_catalogue", verbosity=0)
        definition = ConfigurationDefinition.objects.get(key="students.portal.photo_self_edit")
        set_value(definition=definition, value=False, actor=self.admin, tenant=self.tenant)

        client = TenantAPIClient(self.pupil)
        upload = SimpleUploadedFile("me.png", PNG, content_type="image/png")
        response = client.post(
            reverse("student-portal-profile-photo"), {"file": upload}, format="multipart",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "PHOTO_SELF_EDIT_OFF")
        self.assertFalse(self.portal("student-portal-profile").data["data"]["photo_editable"])

    def test_staff_get_404_from_every_read(self):
        client = TenantAPIClient(self.admin)
        for name in ("student-portal-profile", "student-portal-timetable", "student-portal-attendance"):
            with self.subTest(name=name):
                self.assertEqual(client.get(reverse(name)).status_code, 404)


# Keep the pinned instant importable for a reader looking for "now" here.
assert NOW and TODAY
