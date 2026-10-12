"""Pupil names built into a value follow the reader's name switches.

A promotion preview lists every pupil by name and number, and every move
answers with a sentence naming the pupil ("Chiamaka Nwosu is suspended.").
Neither is a serialized field, so each is held to the same rule a serialized
name follows (``serializers.readable_name``). Brightfield (two branches) and
Sunrise (one) each have a registrar holding every student key who may not
read a pupil's first name or admission number, and Brightfield one who may
read no name part at all.
"""
from __future__ import annotations

from schools.vs_students.constants import StudentStatus
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .base import ALL_KEYS, StudentsFixture

FIRST = "school.students.first_name"
LAST = "school.students.last_name"
MIDDLE = "school.students.middle_name"
NUMBER = "school.students.student_number"


class PupilNamesBySwitchTests(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.students")
        cls.registrars = {}
        for label, school in (("multi", cls.school), ("single", cls.solo)):
            cls.registrars[label] = cls._reader(school, f"registrar_{label}", FIRST, NUMBER)
        cls.nameless = cls._reader(cls.school, "nameless", FIRST, MIDDLE, LAST)

    @classmethod
    def _reader(cls, school, key, *hidden):
        role = make_role(school, name=key, key=key)
        for perm in ALL_KEYS:
            make_role_permission(role, cls.permissions[perm])
        set_field_access(role, *hidden, read=False, write=False)
        user = make_school_admin(None, email=f"{key}@names.test", tenant=school.tenant)
        make_assignment(school, user, role, branch=None)
        return user

    def _pupil(self, label, **extra):
        if label == "multi":
            return self.student(number="BF-0042", **extra)
        return self.student(
            tenant=self.solo.tenant, branch=self.solo_branch, number="SR-0042", **extra,
        )

    # -- promotion preview ----------------------------------------------

    def test_the_promotion_preview_names_pupils_by_the_readers_switches(self):
        placed = self._pupil("multi")
        self.place(placed, self.shared_class)
        unplaced = self.student(first="Tobi", last="Adewale", number="BF-0043")
        response = self.post(
            self.registrars["multi"], "student-promotion-preview",
            {"to_session": self.next_year.pk},
        )
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        row = next(r for r in data["students"] if r["id"] == placed.pk)
        self.assertEqual(row["name"], "Nwosu")
        self.assertNotIn("student_number", row)
        exception = next(
            e for e in data["exceptions"]["by_student"] if e["student"] == unplaced.pk
        )
        self.assertEqual(exception["name"], "Adewale")
        self.assertTrue(exception["reason"].startswith("This pupil has no class"))
        for secret in ("Chiamaka", "Tobi", "BF-0042", "BF-0043"):
            self.assertNotIn(secret, repr(data))

    def test_a_default_reader_still_reads_the_whole_preview(self):
        unplaced = self.student(first="Tobi", last="Adewale", number="BF-0043")
        data = self.post(
            self.admin, "student-promotion-preview", {"to_session": self.next_year.pk},
        ).data["data"]
        exception = next(
            e for e in data["exceptions"]["by_student"] if e["student"] == unplaced.pk
        )
        self.assertEqual(exception["name"], "Tobi Adewale")
        self.assertTrue(exception["reason"].startswith("Tobi has no class"))

    # -- move confirmations ---------------------------------------------

    def test_a_suspension_message_names_only_what_the_reader_may_read(self):
        for label, registrar in self.registrars.items():
            with self.subTest(school=label):
                pupil = self._pupil(label)
                response = self.post(registrar, "student-suspend", {}, pk=pupil.pk)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["message"], "Nwosu is suspended.")

    def test_a_reader_who_may_read_no_name_part_reads_the_pupil(self):
        pupil = self._pupil("multi")
        response = self.post(self.nameless, "student-suspend", {}, pk=pupil.pk)
        self.assertEqual(response.data["message"], "The pupil is suspended.")

    def test_a_bulk_result_row_names_only_what_the_reader_may_read(self):
        pupil = self._pupil("multi")
        response = self.post(self.registrars["multi"], "student-bulk-status", {
            "student_ids": [pupil.pk], "to_status": StudentStatus.WITHDRAWN,
            "reason": "Family relocated.",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["results"][0]["name"], "Nwosu")
        self.assertNotIn("Chiamaka", repr(response.data))

    def test_a_default_reader_reads_the_whole_name_in_a_message(self):
        pupil = self._pupil("multi")
        response = self.post(self.admin, "student-suspend", {}, pk=pupil.pk)
        self.assertEqual(response.data["message"], "Chiamaka Nwosu is suspended.")
