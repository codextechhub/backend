"""The class teacher on the gradebook's staff screens follows the Class teacher switch.

Chidi Eze is JSS1 A's class teacher. A principal whose role may not read
``academics.classes.class_teacher`` reads the broadsheet and the approvals
queue without his name, as a hub reader reads the results hub; the class is
still there. With the switch open, the name is back. Report cards are outside
it, as documents for families.
"""
from __future__ import annotations

from django.urls import reverse

from core.test_utils import TenantAPIClient
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .class_base import ClassFixture

CLASS_TEACHER = "academics.classes.class_teacher"


class ClassTeacherSwitchTests(ClassFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.drive_to_awaiting()
        install_declared_fields("academics.classes")

    def hide(self):
        for role in (self.principal_role, self.hub_role):
            set_field_access(role, CLASS_TEACHER, read=False, write=False)

    def hub_classes(self):
        response = TenantAPIClient(self.hub_reader).get(reverse("gradebook-hub"))
        self.assertEqual(response.status_code, 200, response.data)
        return self.data(response)["classes"]

    def test_the_broadsheet_leaves_the_class_teacher_out(self):
        self.hide()
        body = self.data(self.broadsheet(self.principal))
        self.assertNotIn("class_teacher", body)
        self.assertEqual(body["class"]["name"], "JSS1 A")

    def test_the_approvals_queue_leaves_the_class_teacher_out(self):
        self.hide()
        [row] = self.data(self.approvals(self.principal))["classes"]
        self.assertNotIn("class_teacher", row)

    def test_the_results_hub_leaves_the_class_teacher_out(self):
        self.hide()
        classes = self.hub_classes()
        self.assertTrue(classes)
        for row in classes:
            self.assertNotIn("class_teacher", row)

    def test_each_names_the_class_teacher_with_the_switch_open(self):
        self.assertEqual(self.data(self.broadsheet(self.principal))["class_teacher"]["name"], "Chidi Eze")
        [row] = self.data(self.approvals(self.principal))["classes"]
        self.assertEqual(row["class_teacher"]["name"], "Chidi Eze")
        jss1a = next(c for c in self.hub_classes() if c["name"] == "JSS1 A")
        self.assertEqual(jss1a["class_teacher"]["name"], "Chidi Eze")
