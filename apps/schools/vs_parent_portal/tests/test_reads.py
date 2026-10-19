"""A parent reads a child's timetable and attendance, and only that child's.

Brightfield's JSS1 A (Chiamaka) has English then Mathematics on Monday; JSS1 B
(Zainab) has Mathematics first. Mrs. Okon is mother to both, so what comes back
for each child shows the call was made for that child and not the other.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from schools.vs_attendance.tests.lesson_base import LessonFixture
from schools.vs_students.constants import Relationship
from schools.vs_students.models import Guardian, StudentGuardian
from vs_rbac.tests.helpers import make_school_admin


class ParentReadsFixture(LessonFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.okon_user = make_school_admin(
            None, email="okon.parent@example.ng", tenant=cls.tenant,
            first_name="Ify", last_name="Okon",
        )
        okon = Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Ify", last_name="Okon",
            phone="08110000020", user=cls.okon_user,
        )
        for pupil in (cls.chiamaka, cls.zainab):
            StudentGuardian.all_objects.create(
                tenant=cls.tenant, student=pupil, guardian=okon,
                relationship=Relationship.MOTHER, is_primary=False,
            )

    def read(self, name, pupil, **params):
        url = reverse(name, kwargs={"child_id": pupil.pk})
        return TenantAPIClient(self.okon_user).get(url, params)


class ParentTimetableTests(ParentReadsFixture):
    def first_lesson(self, pupil):
        data = self.read("parent-portal-timetable", pupil).data["data"]
        monday = next(day for day in data["days"] if day["day_label"] == "Monday")
        lesson = next(cell for cell in monday["cells"] if cell["kind"] == "LESSON")
        return data["class_name"], lesson["subject"]

    def test_each_childs_own_class_grid_comes_back(self):
        self.assertEqual(self.first_lesson(self.chiamaka), (self.jss1a.name, "English Language"))
        self.assertEqual(self.first_lesson(self.zainab), (self.jss1b.name, "Mathematics"))


class ParentAttendanceTests(ParentReadsFixture):
    def test_the_childs_own_marks_come_back_and_not_a_siblings(self):
        self.mark(self.admin, self.jss1a, self.chiamaka, "L", arrived_at="08:20", note="Bus broke down")
        self.fill(self.admin, self.jss1a)
        self.submit(self.admin, self.jss1a)
        chiamaka = self.read("parent-portal-attendance", self.chiamaka).data["data"]
        self.assertEqual(chiamaka["counts"]["late"], 1)
        self.assertEqual(chiamaka["notes"][0]["note"], "Bus broke down")
        zainab = self.read("parent-portal-attendance", self.zainab).data["data"]
        self.assertEqual(zainab["counts"]["late"], 0)

    def test_a_term_of_another_school_or_not_a_number_is_not_found(self):
        self.assertEqual(
            self.read("parent-portal-attendance", self.chiamaka, term="999999").status_code, 404,
        )
        self.assertEqual(
            self.read("parent-portal-attendance", self.chiamaka, term="first").status_code, 404,
        )
