"""A member of staff's data request: the copy, and erasing them once they have left."""
from __future__ import annotations

import json

from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.models import StaffProfile
from vs_audit.models import AuditActionType, AuditEvent
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import make_permission, make_role_permission

from .base import StaffFixture

EXPORT = "school.personal_data.export"
ERASE = "school.personal_data.erase"


class StaffPersonalDataTests(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key in (EXPORT, ERASE):
            make_role_permission(cls.role, make_permission(key, scope=PermissionScope.TENANT))
        cls.teacher = cls.make_staff("bola@brightfield.test", "Bola", "Adeyemi", branch=cls.lekki)

    def test_the_copy_holds_her_record_account_and_pay(self):
        response = self.get(self.admin, "staff-personal-data", pk=self.teacher.pk)

        self.assertEqual(response.status_code, 200)
        copy = json.loads(response.content)
        self.assertEqual(list(copy["sections"])[0], "record")
        self.assertIn("account", copy["sections"])
        self.assertIn("pay", copy["sections"])
        self.assertEqual(copy["sections"]["account"]["data"]["Account"]["Email"],
                         "bola@brightfield.test")
        self.assertNotIn("Password", copy["sections"]["account"]["data"]["Account"])
        self.assertTrue(AuditEvent.objects.filter(
            action_type=AuditActionType.PERSONAL_DATA_EXPORTED, entity_id=str(self.teacher.pk),
        ).exists())

    def test_someone_on_the_roll_is_not_erased_and_a_leaver_is(self):
        refused = self.post(self.admin, "staff-erase", {"reason": "Her email, 3 Nov"},
                            pk=self.teacher.pk)
        self.assertEqual(refused.status_code, 400)

        StaffProfile.all_objects.filter(pk=self.teacher.pk).update(
            employment_status=EmploymentStatus.RESIGNED)
        response = self.post(self.admin, "staff-erase", {"reason": "Her email, 3 Nov"},
                             pk=self.teacher.pk)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(StaffProfile.all_objects.get(pk=self.teacher.pk).erased_at)
        self.assertTrue(AuditEvent.objects.filter(
            action_type=AuditActionType.PERSONAL_DATA_ERASED, entity_id=str(self.teacher.pk),
        ).exists())

    def test_without_the_keys_there_is_no_copy_and_no_erasure(self):
        self.assertEqual(
            self.get(self.nobody, "staff-personal-data", pk=self.teacher.pk).status_code, 403)
        self.assertEqual(
            self.post(self.nobody, "staff-erase", {"reason": "x"}, pk=self.teacher.pk).status_code,
            403)
