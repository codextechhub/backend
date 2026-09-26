"""Reading a student or guardian profile as it stood at the end of an earlier day.

Each change below is recorded at a fixed instant (the recorder's clock is
patched), so a test reads the profile before and after it and proves the page
answers with the value it held that day, not today's.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest import mock

from schools.vs_students.constants import DocumentType, Relationship
from schools.vs_students.models import Guardian, StudentDocument
from vs_history.as_at import RECORD_DAY_TIMEZONE
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .base import StudentsFixture


def _at(month, day, hour=10):
    return dt.datetime(2026, month, day, hour, tzinfo=RECORD_DAY_TIMEZONE)


@contextmanager
def recorded_on(month, day):
    with mock.patch("vs_history.recorder.timezone.now", return_value=_at(month, day)):
        yield


class AsAtFixture(StudentsFixture):
    def setUp(self):
        with recorded_on(3, 1):
            self.pupil = self.student(first="Tunde", last="Bakare")
            self.place(self.pupil)
            self.mother = Guardian.all_objects.create(
                tenant=self.tenant, first_name="Ngozi", last_name="Bakare",
                phone="08030000001",
            )
            self.link(self.pupil, self.mother, relationship=Relationship.MOTHER)

    def as_at(self, name, day, user=None, **kwargs):
        return self.get(user or self.admin, name, {"as_at": day}, **kwargs)


class StudentRecordAsAtTests(AsAtFixture):
    def test_the_live_record_names_the_day_its_history_starts(self):
        data = self.get(self.admin, "student-detail", pk=self.pupil.pk).data["data"]
        self.assertEqual(data["history_starts"], "2026-03-01")
        self.assertNotIn("as_at", data)

    def test_a_past_day_reads_the_values_held_then(self):
        with recorded_on(3, 10):
            self.pupil.last_name = "Adeyemi"
            self.pupil.phone = "08099999999"
            self.pupil.save()
        before = self.as_at("student-detail", "2026-03-05", pk=self.pupil.pk).data["data"]
        after = self.as_at("student-detail", "2026-03-10", pk=self.pupil.pk).data["data"]
        self.assertEqual(before["last_name"], "Bakare")
        self.assertEqual(before["full_name"], "Tunde Bakare")
        self.assertEqual(before["as_at"]["date"], "2026-03-05")
        self.assertEqual(before["allowed_transitions"], [])
        self.assertEqual(after["last_name"], "Adeyemi")
        self.assertEqual(after["phone"], "08099999999")

    def test_the_class_reads_as_it_was_that_day(self):
        with recorded_on(3, 12):
            self.pupil.enrolments.update(is_active=False)
            self.place(self.pupil, self.lekki_class)
        before = self.as_at("student-detail", "2026-03-05", pk=self.pupil.pk).data["data"]
        after = self.as_at("student-detail", "2026-03-12", pk=self.pupil.pk).data["data"]
        self.assertEqual(before["class_name"], self.shared_class.name)
        self.assertEqual(after["class_name"], self.lekki_class.name)
        history = self.as_at("student-class-history", "2026-03-05", pk=self.pupil.pk).data
        rows = history["data"] if isinstance(history["data"], list) else history["data"]["results"]
        self.assertEqual([row["class_name"] for row in rows], [self.shared_class.name])

    def test_a_day_before_the_history_starts_is_refused_with_the_first_day(self):
        response = self.as_at("student-detail", "2026-02-27", pk=self.pupil.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "HISTORY_NOT_KEPT")
        self.assertEqual(response.data["error"]["detail"]["history_starts"], "2026-03-01")

    def test_a_future_day_is_refused(self):
        response = self.as_at("student-detail", "2999-01-01", pk=self.pupil.pk)
        self.assertEqual(response.status_code, 400)

    def test_another_school_cannot_read_the_record_at_any_date(self):
        response = self.as_at("student-detail", "2026-03-05", user=self.solo_admin, pk=self.pupil.pk)
        self.assertEqual(response.status_code, 404)

    def test_a_branch_head_cannot_read_another_branch_at_any_date(self):
        with recorded_on(3, 1):
            elsewhere = self.student(branch=self.ikeja, first="Ada", last="Obi")
        response = self.as_at("student-detail", "2026-03-05", user=self.lekki_head, pk=elsewhere.pk)
        self.assertEqual(response.status_code, 404)


class GuardiansAndDocumentsAsAtTests(AsAtFixture):
    def test_a_guardian_unlinked_later_is_on_the_earlier_list_with_their_details_then(self):
        with recorded_on(3, 8):
            self.mother.phone = "08031111111"
            self.mother.save()
        with recorded_on(3, 15):
            self.pupil.guardian_links.all().delete()
        early = self.as_at("student-guardians", "2026-03-05", pk=self.pupil.pk).data["data"]
        middle = self.as_at("student-guardians", "2026-03-10", pk=self.pupil.pk).data["data"]
        late = self.as_at("student-guardians", "2026-03-16", pk=self.pupil.pk).data["data"]
        self.assertEqual(early[0]["guardian"]["phone"], "08030000001")
        self.assertEqual(middle[0]["guardian"]["phone"], "08031111111")
        self.assertEqual(late, [])

    def test_a_document_replaced_since_is_held_without_a_link(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with recorded_on(3, 2):
            StudentDocument.all_objects.create(
                tenant=self.tenant, student=self.pupil,
                document_type=DocumentType.BIRTH_CERTIFICATE,
                file=SimpleUploadedFile("birth.pdf", b"%PDF-1", content_type="application/pdf"),
            )
        with recorded_on(3, 9):
            StudentDocument.all_objects.filter(student=self.pupil).delete()
        rows = self.as_at("student-documents", "2026-03-05", pk=self.pupil.pk).data["data"]
        birth = next(row for row in rows if row["document_type"] == DocumentType.BIRTH_CERTIFICATE)
        self.assertTrue(birth["attached"])
        self.assertTrue(birth["file_retired"])
        self.assertEqual(birth["url"], "")


class FieldAccessAppliesToThePastTests(AsAtFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students")
        cls.clerk_role = make_role(cls.school, name="Clerk", key="clerk")
        make_role_permission(cls.clerk_role, cls.permissions["school.students.view"])
        set_field_access(cls.clerk_role, "school.students.phone", "school.students.first_name",
                         read=False, write=False)
        cls.clerk = make_school_admin(None, email="clerk@brightfield.test", tenant=cls.tenant)
        make_assignment(cls.school, cls.clerk, cls.clerk_role, branch=None)

    def test_a_hidden_field_is_absent_from_the_past_as_from_the_present(self):
        with recorded_on(3, 10):
            self.pupil.phone = "08099999999"
            self.pupil.save()
        live = self.get(self.clerk, "student-detail", pk=self.pupil.pk).data["data"]
        past = self.as_at("student-detail", "2026-03-10", user=self.clerk, pk=self.pupil.pk).data["data"]
        for data in (live, past):
            self.assertNotIn("phone", data)
            self.assertNotIn("first_name", data)
            self.assertEqual(data["full_name"], "Bakare")

    def test_a_hidden_first_name_is_not_carried_inside_the_list_rows_full_name(self):
        rows = self.get(self.clerk, "student-list").data["data"]
        rows = rows["results"] if isinstance(rows, dict) else rows
        row = next(r for r in rows if r["id"] == self.pupil.pk)
        self.assertEqual(row["full_name"], "Bakare")


class GuardianProfileAsAtTests(AsAtFixture):
    def test_the_guardian_page_reads_their_details_and_wards_as_they_were(self):
        with recorded_on(3, 8):
            self.mother.first_name = "Ngozika"
            self.mother.save()
        before = self.as_at("guardian-detail", "2026-03-05", pk=self.mother.pk).data["data"]
        live = self.get(self.admin, "guardian-detail", pk=self.mother.pk).data["data"]
        self.assertEqual(before["first_name"], "Ngozi")
        self.assertEqual(before["full_name"], "Ngozi Bakare")
        self.assertEqual([w["name"] for w in before["wards"]], ["Tunde Bakare"])
        self.assertEqual(live["first_name"], "Ngozika")
        self.assertEqual(live["history_starts"], "2026-03-01")
