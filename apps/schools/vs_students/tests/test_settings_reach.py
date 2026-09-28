"""Who may change a school's student settings: the key, and the reach behind it.

The enrolment, admission and guardian rules and the school's admission-number
rule bind every branch. Holding the key that changes them is not enough on its
own: the caller's reach has to be the whole school, or a branch administrator
given the key to look after their own branch rewrites a rule for branches that
never agreed to it. A branch's own admission-number rule is that branch's, and
a caller who covers the branch may set or remove it.

Brightfield runs Lekki and Ikeja. Kemi is Lekki's settings administrator: her
role carries ``school.settings.update``, pinned to Lekki. The Lekki head holds
every student key, ``school.students.update`` included, pinned to Lekki.
"""
from __future__ import annotations

from django.urls import reverse

from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)

from ..models import AdmissionStage
from ..services.guardian_rules import read_guardian_rules
from ..services.policy import read_policy
from ..services.rules import read_rules
from .base import StudentsFixture

REFUSED = "SHARED_RECORD_READ_ONLY"


class _ReachFixture(StudentsFixture):
    """A settings administrator for the whole school and one pinned to Lekki."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        settings_key = make_permission(
            "school.settings.update", scope=PermissionScope.TENANT,
        )
        role = make_role(cls.school, name="Settings Admin", key="settings_admin")
        make_role_permission(role, settings_key)
        make_role_permission(role, cls.permissions["school.students.view"])

        cls.settings_admin = make_school_admin(
            None, email="settings@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.settings_admin, role, branch=None)

        cls.kemi = make_school_admin(
            cls.lekki, email="kemi@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.kemi, role, branch=cls.lekki)

    def assert_refused(self, response, message):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], REFUSED)
        self.assertEqual(response.data["message"], message)


class EnrolmentRulesReachTests(_ReachFixture):

    BODY = {
        "min_age_years": 4, "max_age_years": 25,
        "required_documents": [], "required_fields": [],
        "capacity_mode": "HARD", "default_capacity": None,
    }

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        before = read_rules(self.tenant).as_dict()
        response = self.put(self.kemi, "student-enrolment-rules", self.BODY)
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the school's enrolment rules.",
        )
        self.assertEqual(read_rules(self.tenant).as_dict(), before)

    def test_a_school_wide_caller_changes_them(self):
        response = self.put(self.settings_admin, "student-enrolment-rules", self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(read_rules(self.tenant).as_dict()["capacity_mode"], "HARD")


class AdmissionRulesReachTests(_ReachFixture):

    BODY = {
        "stages": [{"name": "Interview", "is_offer": False}],
        "required_documents_to_confirm": [],
    }

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        response = self.put(self.kemi, "student-admission-rules", self.BODY)
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the school's admission rules.",
        )
        self.assertFalse(AdmissionStage.all_objects.filter(tenant=self.tenant).exists())

    def test_a_school_wide_caller_changes_them(self):
        response = self.put(self.settings_admin, "student-admission-rules", self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            list(AdmissionStage.all_objects.filter(tenant=self.tenant)
                 .values_list("name", flat=True)),
            ["Interview"],
        )


class GuardianRulesReachTests(_ReachFixture):
    """Kemi raising the minimum would hold Ikeja's registrar to it too."""

    BODY = {
        "min_per_student": 2, "email_required": False,
        "matching": "EMAIL_THEN_PHONE", "extra_relationships": [],
    }

    def test_a_branch_bound_caller_holding_the_key_is_refused_and_nothing_moves(self):
        response = self.put(self.kemi, "student-guardian-rules", self.BODY)
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the school's guardian rules.",
        )
        self.assertEqual(read_guardian_rules(self.tenant).as_dict()["min_per_student"], 1)

    def test_a_school_wide_caller_changes_them(self):
        response = self.put(self.settings_admin, "student-guardian-rules", self.BODY)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(read_guardian_rules(self.tenant).as_dict()["min_per_student"], 2)


class AdmissionPolicyReachTests(_ReachFixture):
    """The school's rule needs the whole school; a branch's own needs that branch."""

    BODY = {"required": True, "pattern": "", "hint": "Ask the bursar."}

    def _path(self, branch=None):
        path = f"{reverse('student-admission-policy')}?tenant={self.tenant.slug}"
        return f"{path}&branch={branch.pk}" if branch is not None else path

    def test_the_schools_rule_is_refused_to_a_branch_bound_caller_and_nothing_moves(self):
        response = self.client_for(self.lekki_head).put(
            self._path(), self.BODY, format="json",
        )
        self.assert_refused(
            response,
            "Only a school-wide administrator can change the school's admission "
            "number rule. Choose one of your branches to set its own.",
        )
        self.assertEqual(read_policy(self.tenant).source, "default")
        self.assertFalse(read_policy(self.tenant).required)

    def test_a_school_wide_caller_sets_the_schools_rule(self):
        response = self.client_for(self.admin).put(self._path(), self.BODY, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(read_policy(self.tenant).source, "school")

    def test_a_branch_bound_caller_sets_and_removes_their_own_branchs_rule(self):
        client = self.client_for(self.lekki_head)
        response = client.put(self._path(self.lekki), self.BODY, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(read_policy(self.tenant, self.lekki).source, "branch")
        self.assertEqual(read_policy(self.tenant).source, "default")

        removed = client.delete(self._path(self.lekki))
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertEqual(read_policy(self.tenant, self.lekki).source, "default")

    def test_another_branchs_rule_is_a_404_to_a_branch_bound_caller(self):
        client = self.client_for(self.lekki_head)
        self.assertEqual(
            client.put(self._path(self.ikeja), self.BODY, format="json").status_code, 404,
        )
        self.assertEqual(client.delete(self._path(self.ikeja)).status_code, 404)
        self.assertEqual(read_policy(self.tenant, self.ikeja).source, "default")
