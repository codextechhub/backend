"""Account phone Field Access follows the same account across management paths."""

from django.test import SimpleTestCase, TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.field_registry import get_declaration
from vs_rbac.tests.helpers import (
    install_declared_fields, make_assignment, make_branch, make_permission,
    make_role, make_role_permission, make_school, make_school_admin,
    set_field_access,
)
from vs_user.models import User
from vs_user.workflow_handlers import UserCreationWorkflowHandler
from vs_workflow.presentation import for_reader


class TeamPhoneDeclarationTests(SimpleTestCase):
    def test_account_phone_has_a_read_and_write_switch(self):
        declaration = get_declaration("platform", "team")
        phone = next(field for field in declaration.fields if field.name == "phone")

        self.assertEqual(phone.label, "Phone")
        self.assertTrue(phone.writable)
        self.assertFalse(phone.sensitive)
        self.assertEqual(phone.scope, "TENANT")


class TeamPhoneAccessTests(TestCase):
    """An administrator cannot read or change a colleague's closed phone."""

    @classmethod
    def setUpTestData(cls):
        school = make_school(slug="team-phone-bright", name="Bright Star School")
        cls.tenant = school.tenant
        cls.ikeja = make_branch(school, name="Ikeja Branch")
        make_branch(school, name="Lekki Branch", is_main=False)
        cls.actor = make_school_admin(cls.ikeja, email="ada@team-phone.test")
        cls.member = make_school_admin(
            cls.ikeja, email="tunde@team-phone.test", phone="08031234567",
        )
        cls.role = make_role(cls.tenant, name="Account administrator")
        for key in ("platform.team.view", "platform.team.update", "platform.team.create"):
            make_role_permission(cls.role, make_permission(key))
        make_assignment(cls.tenant, cls.actor, cls.role)
        install_declared_fields("platform.team")

    def client_for(self, user=None):
        return TenantAPIClient(user=user or self.actor)

    def detail_url(self):
        return f"/v1/user/users/{self.member.pk}/"

    def test_default_read_keeps_the_phone_on_the_account_and_approval_card(self):
        response = self.client_for().get(self.detail_url())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["phone"], "08031234567")

        from rest_framework.test import APIRequestFactory

        request = APIRequestFactory().get("/")
        request.user = self.actor
        request.tenant = self.tenant
        details = UserCreationWorkflowHandler().get_document_details(self.member)
        visible = for_reader(details, request)
        self.assertIn("08031234567", str(visible))

    def test_closed_read_hides_the_phone_from_detail_and_approval_card(self):
        set_field_access(self.role, "platform.team.phone", read=False, write=False)
        response = self.client_for().get(self.detail_url())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("phone", response.data["data"])

        from rest_framework.test import APIRequestFactory

        request = APIRequestFactory().get("/")
        request.user = self.actor
        request.tenant = self.tenant
        details = UserCreationWorkflowHandler().get_document_details(self.member)
        self.assertNotIn("08031234567", str(for_reader(details, request)))

    def test_closed_write_refuses_an_account_update(self):
        set_field_access(self.role, "platform.team.phone", read=True, write=False)
        response = self.client_for().patch(
            self.detail_url(), {"phone": "08039876543"}, format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.phone, "08031234567")

    def test_updating_another_field_does_not_return_a_hidden_phone(self):
        set_field_access(self.role, "platform.team.phone", read=False, write=False)
        response = self.client_for().patch(
            self.detail_url(), {"gender": "FEMALE"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("phone", response.data["data"])

    def test_closed_write_refuses_phone_on_create(self):
        set_field_access(self.role, "platform.team.phone", read=True, write=False)
        response = self.client_for().post("/v1/user/users/", {
            "first_name": "Ife", "last_name": "Okon",
            "email": "ife@team-phone.test", "phone": "08039876543",
            "role": self.role.key, "branch": self.ikeja.pk,
        }, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(User.objects.filter(email="ife@team-phone.test").exists())

    def test_owner_may_correct_their_own_phone(self):
        set_field_access(self.role, "platform.team.phone", read=False, write=False)
        response = self.client_for(self.actor).patch(
            f"/v1/user/users/{self.actor.pk}/", {"phone": "08031111111"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.actor.refresh_from_db()
        self.assertEqual(self.actor.phone, "08031111111")
