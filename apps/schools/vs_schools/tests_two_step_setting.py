"""The school's two-step sign-in switch: ``/v1/i/me/settings/two-step/``.

On by default. The head of Bright Star may turn it off for the whole school; a
branch administrator may look but not change it; a teacher may not even look;
and Bright Star's choice never reaches Green Field.
"""
from core.test_utils import TenantAPIClient
from vs_config.models import ConfigurationDefinition
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
)
from vs_user.services import two_step

from django.test import TestCase

URL = "/v1/i/me/settings/two-step/"


class TwoStepSettingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ConfigurationDefinition.objects.get_or_create(
            key=two_step.SETTING_KEY,
            defaults={
                "label": "Two-step", "value_type": "BOOLEAN", "default_value": True,
                "allowed_scopes": ["platform", "school"],
            },
        )
        view = make_permission("school.settings.view")
        update = make_permission("school.settings.update")

        cls.bright = make_school(slug="bright-star", name="Bright Star")
        cls.ikeja = make_branch(cls.bright, name="Ikeja Branch")
        # Two branches: at a one-branch school a branch's reach is the whole school.
        make_branch(cls.bright, name="Lekki Branch", is_main=False)
        head_role = make_role(cls.bright, name="School Admin", key="school_admin")
        make_role_permission(head_role, view)
        make_role_permission(head_role, update)
        cls.head = make_school_admin(cls.ikeja, email="head@bright.example.com")
        make_assignment(cls.bright, cls.head, head_role, branch=None)

        branch_role = make_role(cls.bright, name="Branch Admin", key="branch_admin")
        make_role_permission(branch_role, view)
        make_role_permission(branch_role, update)
        cls.branch_admin = make_school_admin(cls.ikeja, email="ikeja@bright.example.com")
        make_assignment(cls.bright, cls.branch_admin, branch_role, branch=cls.ikeja)

        cls.teacher = make_staff_user(cls.ikeja, email="teacher@bright.example.com")

        cls.green = make_school(slug="green-field", name="Green Field")
        make_branch(cls.green, name="Main Branch")

    def call(self, user, method="get", body=None):
        client = TenantAPIClient(user=user)
        if method == "get":
            return client.get(URL)
        return client.patch(f"{URL}?tenant={user.tenant.slug}", body, format="json")

    def test_on_by_default(self):
        response = self.call(self.head)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"], {"required": True, "source": "default"})

    def test_the_head_turns_it_off_for_this_school_only(self):
        response = self.call(self.head, "patch", {"required": False})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"], {"required": False, "source": "school"})
        self.assertFalse(two_step.tenant_requires(self.bright.tenant))
        self.assertTrue(two_step.tenant_requires(self.green.tenant))

    def test_a_branch_administrator_may_look_but_not_change_it(self):
        self.assertEqual(self.call(self.branch_admin).status_code, 200)
        self.assertEqual(self.call(self.branch_admin, "patch", {"required": False}).status_code, 403)
        self.assertTrue(two_step.tenant_requires(self.bright.tenant))

    def test_a_teacher_may_not_look(self):
        self.assertEqual(self.call(self.teacher).status_code, 403)
