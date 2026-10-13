"""Field Access while a CodeX operator acts on a school.

An operator reaches a school by asserting it as the request's tenant, but their
roles live in the platform tenant. A field only the platform can hold is judged
where those roles are, and a field a school can hold is judged in the school,
where the operator has no role. A school user is judged in their school and
nothing changes for them.
"""
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from vs_rbac.field_enforcement import (
    assert_writable,
    can_read,
    field_access_payload,
    visible,
)
from vs_rbac.field_evaluator import get_acting_field_access, get_field_access
from vs_rbac.models import PermissionScope
from vs_user.models import User

from .helpers import (
    make_assignment,
    make_branch,
    make_field_definition,
    make_platform_assignment,
    make_platform_role,
    make_role,
    make_school,
    make_staff_user,
    make_vision_user,
    set_field_access,
)

PLATFORM_FIELD = "actfa.school.subscription"
SECOND_PLATFORM_FIELD = "actfa.school.owner_note"
TENANT_FIELD = "actfa.vendor.phone"


def _fresh(user):
    """The user re-read, so no memoised map from an earlier call survives."""
    return User.objects.get(pk=user.pk)


def _request(user, tenant):
    request = APIRequestFactory().get("/")
    request.user = _fresh(user)
    request.tenant = tenant
    return request


class _ActingAccess(TestCase):
    """Bright Star School, a CodeX operator with one platform switch closed, a school user."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="act-bright-star", name="Bright Star School")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.tenant = cls.school.tenant
        cls.platform = make_field_definition(
            PLATFORM_FIELD, "Subscription", scope=PermissionScope.PLATFORM,
        )
        cls.platform_other = make_field_definition(
            SECOND_PLATFORM_FIELD, "Owner note", scope=PermissionScope.PLATFORM,
        )
        cls.vendor_phone = make_field_definition(TENANT_FIELD, "Phone")

        cls.operator = make_vision_user(email="act-operator@codex.test")
        role = make_platform_role(name="Act Operator")
        make_platform_assignment(cls.operator, role)
        set_field_access(role, PLATFORM_FIELD, read=False, write=False)

        cls.super_admin = make_vision_user(email="act-super@codex.test", super_admin=True)

        cls.teacher = make_staff_user(cls.ikeja, email="act-teacher@test.com")
        teacher_role = make_role(cls.tenant, name="Act Teacher")
        make_assignment(cls.tenant, cls.teacher, teacher_role)
        set_field_access(teacher_role, TENANT_FIELD, read=False, write=False)


class ActingAccessMapTests(_ActingAccess):

    def test_platform_fields_follow_the_operators_platform_switches(self):
        access = get_acting_field_access(_fresh(self.operator), tenant=self.tenant)

        self.assertFalse(access.can_read(PLATFORM_FIELD))
        self.assertTrue(access.can_read(SECOND_PLATFORM_FIELD))
        self.assertTrue(access.can_write(SECOND_PLATFORM_FIELD))

    def test_without_the_door_the_school_would_close_every_platform_field(self):
        access = get_field_access(_fresh(self.operator), tenant=self.tenant)

        self.assertFalse(access.can_read(SECOND_PLATFORM_FIELD))

    def test_tenant_fields_stay_judged_in_the_school(self):
        access = get_acting_field_access(_fresh(self.operator), tenant=self.tenant)

        self.assertFalse(access.can_read(TENANT_FIELD))

    def test_a_super_admin_reads_every_platform_field_on_a_school(self):
        access = get_acting_field_access(_fresh(self.super_admin), tenant=self.tenant)

        self.assertTrue(access.can_read(PLATFORM_FIELD))
        self.assertTrue(access.can_write(PLATFORM_FIELD))
        self.assertFalse(access.can_read(TENANT_FIELD))

    def test_an_operator_in_their_own_tenant_gets_the_plain_answer(self):
        user = _fresh(self.operator)
        plain = get_field_access(user, tenant=user.tenant)
        acting = get_acting_field_access(user, tenant=user.tenant)

        for key in (PLATFORM_FIELD, SECOND_PLATFORM_FIELD, TENANT_FIELD):
            self.assertEqual(plain.state(key), acting.state(key))

    def test_a_school_user_is_unaffected(self):
        user = _fresh(self.teacher)
        plain = get_field_access(user, tenant=self.tenant)
        acting = get_acting_field_access(user, tenant=self.tenant)

        for key in (PLATFORM_FIELD, SECOND_PLATFORM_FIELD, TENANT_FIELD):
            self.assertEqual(plain.state(key), acting.state(key))
        self.assertFalse(acting.can_read(SECOND_PLATFORM_FIELD))
        self.assertFalse(acting.can_read(TENANT_FIELD))

    def test_the_answer_is_memoised_on_the_user(self):
        user = _fresh(self.operator)

        first = get_acting_field_access(user, tenant=self.tenant)
        with self.assertNumQueries(1):
            second = get_acting_field_access(user, tenant=self.tenant)

        self.assertIs(first, second)


class ActingEnforcementTests(_ActingAccess):
    """Every enforcement door reads the same acting map."""

    def test_can_read_and_visible_agree(self):
        request = _request(self.operator, self.tenant)

        self.assertFalse(can_read(request, PLATFORM_FIELD))
        self.assertTrue(can_read(request, SECOND_PLATFORM_FIELD))
        row = visible(request, "actfa.school", {"subscription": 1, "owner_note": 2})
        self.assertEqual(row, {"owner_note": 2})

    def test_assert_writable_judges_a_platform_field_by_the_platform_switch(self):
        request = _request(self.operator, self.tenant)

        assert_writable(request, "actfa.school", {"owner_note": "x"})

    def test_a_school_user_reads_no_platform_field(self):
        request = _request(self.teacher, self.tenant)

        self.assertFalse(can_read(request, SECOND_PLATFORM_FIELD))

    def test_the_me_payload_lists_platform_fields_for_an_operator_on_a_school(self):
        payload = field_access_payload(_fresh(self.operator), self.tenant)

        self.assertEqual(payload["actfa.school"]["hidden"], ["subscription"])
        self.assertNotIn("owner_note", payload["actfa.school"]["hidden"])

    def test_the_me_payload_never_names_a_platform_field_to_a_school_user(self):
        payload = field_access_payload(_fresh(self.teacher), self.tenant)

        self.assertNotIn("actfa.school", payload)
