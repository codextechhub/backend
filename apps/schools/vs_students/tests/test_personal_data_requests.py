"""A pupil's or a guardian's data request: the copy, and erasing on request.

Mrs Nwosu asks Brightfield for everything it holds on her son Chiamaka, then,
once he has left, for it to be erased. The administrator holding the two
personal data keys answers both; a member of staff without them cannot.
"""
from __future__ import annotations

import json

from django.apps import apps
from django.test import SimpleTestCase

from core import personal_data
from schools.vs_staff.models import StaffProfile
from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import Guardian, Student
from vs_audit.models import AuditActionType, AuditEvent
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import make_permission, make_role_permission

from .base import StudentsFixture

EXPORT = "school.personal_data.export"
ERASE = "school.personal_data.erase"


class EveryTableAboutAPersonIsInTheCopyTests(SimpleTestCase):
    def test_every_model_linked_to_a_person_is_exported_or_says_why_not(self):
        """A new table about pupils, guardians or staff fails here until it is placed."""
        kinds = {Student: personal_data.PUPIL, Guardian: personal_data.GUARDIAN,
                 StaffProfile: personal_data.STAFF}
        missing = []
        for model in apps.get_models():
            for field in model._meta.concrete_fields:
                kind = kinds.get(field.related_model) if field.is_relation else None
                if kind and model._meta.label not in personal_data.covered(kind):
                    missing.append(f"{kind}: {model._meta.label}.{field.name}")
        self.assertEqual(missing, [], "Add a section (core.personal_data.register) or not_exported for:")


class PersonalDataRequestTests(StudentsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key in (EXPORT, ERASE):
            make_role_permission(cls.role, make_permission(key, scope=PermissionScope.TENANT))

    def setUp(self):
        super().setUp()
        self.son = self.student(first="Chiamaka", last="Nwosu", allergies="Peanuts",
                                address="4 Adeola Odeku Street")
        self.mother = self.guardian(name="Mrs. Ngozi Nwosu", email="ngozi@example.ng")
        self.link(self.son, self.mother)

    def download(self, user, name, pk):
        return self.get(user, name, pk=pk)

    # -- the copy ------------------------------------------------------------ #

    def test_the_copy_holds_everything_including_fields_the_reader_cannot_open(self):
        response = self.download(self.admin, "student-personal-data", self.son.pk)

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        copy = json.loads(response.content)
        self.assertEqual(copy["format"], personal_data.FORMAT)
        self.assertEqual(list(copy["sections"])[0], "record")
        record = copy["sections"]["record"]["data"]
        self.assertEqual(record["Allergies"], "Peanuts")
        self.assertEqual(record["Address"], "4 Adeola Odeku Street")
        self.assertEqual(copy["sections"]["guardians"]["data"][0]["Guardian"], "Mrs. Ngozi Nwosu")
        self.assertTrue(AuditEvent.objects.filter(
            action_type=AuditActionType.PERSONAL_DATA_EXPORTED, entity_id=str(self.son.pk),
        ).exists())

    def test_a_guardians_copy_names_their_children(self):
        response = self.download(self.admin, "guardian-personal-data", self.mother.pk)

        self.assertEqual(response.status_code, 200)
        copy = json.loads(response.content)
        self.assertEqual(copy["sections"]["children"]["data"][0]["Child"], str(self.son))

    def test_staff_without_the_key_get_no_copy_and_another_schools_pupil_is_not_found(self):
        self.assertEqual(self.download(self.nobody, "student-personal-data", self.son.pk).status_code, 403)
        other = self.student(tenant=self.solo.tenant, branch=self.solo_branch)
        self.assertEqual(self.download(self.admin, "student-personal-data", other.pk).status_code, 404)

    # -- erasing ------------------------------------------------------------- #

    def test_a_pupil_still_at_the_school_is_not_erased(self):
        response = self.post(self.admin, "student-erase", {"reason": "Mother's email, 2 Nov"},
                             pk=self.son.pk)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Withdraw them first", str(response.data))

    def test_a_reason_is_required(self):
        Student.all_objects.filter(pk=self.son.pk).update(status=StudentStatus.WITHDRAWN)
        response = self.post(self.admin, "student-erase", {}, pk=self.son.pk)

        self.assertEqual(response.status_code, 400)
        self.assertIn("reason", response.data["error"]["detail"])

    def test_a_pupil_who_left_is_erased_now_and_the_trail_keeps_the_reason(self):
        Student.all_objects.filter(pk=self.son.pk).update(status=StudentStatus.WITHDRAWN)

        response = self.post(self.admin, "student-erase", {"reason": "Mother's email, 2 Nov"},
                             pk=self.son.pk)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("Mrs. Ngozi Nwosu", response.data["message"])  # Her last child here.
        son = Student.all_objects.get(pk=self.son.pk)
        self.assertIsNotNone(son.erased_at)
        self.assertEqual((son.allergies, son.address), ("", ""))
        self.assertEqual(son.first_name, "Chiamaka")
        event = AuditEvent.objects.get(
            action_type=AuditActionType.PERSONAL_DATA_ERASED, entity_id=str(self.son.pk),
        )
        self.assertEqual(event.metadata["reason"], "Mother's email, 2 Nov")
        self.assertEqual(event.entity_label, str(self.son))

    def test_a_guardian_with_a_child_still_here_is_not_erased_until_the_child_leaves(self):
        refused = self.post(self.admin, "guardian-erase", {"reason": "Her letter"}, pk=self.mother.pk)
        self.assertEqual(refused.status_code, 400)

        Student.all_objects.filter(pk=self.son.pk).update(status=StudentStatus.GRADUATED)
        response = self.post(self.admin, "guardian-erase", {"reason": "Her letter"}, pk=self.mother.pk)

        self.assertEqual(response.status_code, 200, response.data)
        mother = Guardian.all_objects.get(pk=self.mother.pk)
        self.assertEqual((mother.full_name, mother.email), ("Former parent", ""))

    def test_staff_without_the_erase_key_cannot_erase(self):
        Student.all_objects.filter(pk=self.son.pk).update(status=StudentStatus.WITHDRAWN)
        response = self.post(self.nobody, "student-erase", {"reason": "x"}, pk=self.son.pk)

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(Student.all_objects.get(pk=self.son.pk).erased_at)
