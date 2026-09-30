"""Who may change who approves what: branch reach on every workflow write.

A set of approval steps, an approver group, a Dynamic Role and a stage's
approver override all decide approvals for some set of branches. One with no
branch decides them for every branch, so only a caller whose reach is the
whole school may change it; one that is a branch's own is that branch's. The
permission key says whether somebody may configure workflow at all, and this
says where.

Bright Star runs Ikeja and Lekki. Mrs Bello's Workflow Admin grant is pinned to
Lekki, Mr Emeka's covers Ikeja and Lekki through two grants, and Mr Okafor
holds it school-wide and is posted at Ikeja. Every refusal is a 403
``SHARED_RECORD_READ_ONLY`` with nothing written, except a delegate's, which
is an ordinary 403.
"""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

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
from vs_user.tokens import CodeXRefreshToken
from vs_workflow.constants import (
    PERM_GROUP_CREATE,
    PERM_GROUP_DELETE,
    PERM_GROUP_UPDATE,
    PERM_GROUP_VIEW,
    PERM_TEMPLATE_PUBLISH,
    PERM_TEMPLATE_UPDATE,
    PERM_TEMPLATE_VIEW,
)
from vs_workflow.models import (
    ApprovalDelegation,
    WorkflowApproverGroup,
    WorkflowApproverGroupMember,
    WorkflowDynamicRole,
    WorkflowStageApproverOverride,
    WorkflowTemplate,
)
from vs_workflow.services import templates as templates_svc
from vs_workflow.services.dynamic_roles import replace_rules, validate_rules
from vs_workflow.views import (
    DELEGATION_SHARED,
    DYNAMIC_ROLE_SHARED,
    GROUP_OTHER_BRANCH,
    GROUP_SHARED,
    STAGE_OTHER_BRANCH,
    STAGE_SHARED,
    TEMPLATE_OTHER_BRANCH,
    TEMPLATE_SHARED,
)

ADMIN_KEYS = [
    PERM_TEMPLATE_UPDATE, PERM_TEMPLATE_PUBLISH, PERM_TEMPLATE_VIEW,
    PERM_GROUP_CREATE, PERM_GROUP_UPDATE, PERM_GROUP_DELETE, PERM_GROUP_VIEW,
]
DOC = "finance.refund"


def _client(user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
    )
    return client


def _stages(role_key="approver"):
    return [{
        "code": "sign-off", "label": "Sign off", "kind": "APPROVAL", "order": 1,
        "approver_source": "ROLE", "approver_role_key": role_key,
        "approver_scope": "SCHOOL", "advance_rule": "ANY",
        "on_rejection": "TERMINAL", "skip_if_no_approvers": False,
    }]


class _BrightStar(TestCase):

    @classmethod
    def setUpTestData(cls):
        school = make_school(slug="workflow-reach", name="Bright Star School")
        cls.tenant = school.tenant
        cls.slug = cls.tenant.slug
        cls.ikeja = make_branch(school, name="Ikeja Branch")
        cls.lekki = make_branch(school, name="Lekki Branch", is_main=False)

        admin = make_role(cls.tenant, name="Workflow Admin", key="workflow-admin")
        for key in ADMIN_KEYS:
            make_role_permission(admin, make_permission(key))
        cls.bello = make_school_admin(cls.lekki, email="bello@workflow-reach.test")
        make_assignment(cls.tenant, cls.bello, admin, branch=cls.lekki)
        cls.emeka = make_school_admin(cls.ikeja, email="emeka@workflow-reach.test")
        make_assignment(cls.tenant, cls.emeka, admin, branch=cls.ikeja)
        make_assignment(cls.tenant, cls.emeka, admin, branch=cls.lekki)
        cls.okafor = make_school_admin(cls.ikeja, email="okafor@workflow-reach.test")
        make_assignment(cls.tenant, cls.okafor, admin, branch=None)

        cls.approver = make_role(
            cls.tenant, name="Approver", key="approver", is_system_role=True,
        )
        cls.school_ladder = cls._publish(None, "School ladder")
        cls.lekki_ladder = cls._publish(cls.lekki, "Lekki ladder", code="lekki")
        cls.ikeja_ladder = cls._publish(cls.ikeja, "Ikeja ladder", code="ikeja")

    @classmethod
    def _publish(cls, branch, name, code="standard"):
        return templates_svc.publish_template(
            tenant=cls.tenant, branch=branch, document_type=DOC, code=code, name=name,
            stages_payload=_stages(),
        )

    def _url(self, name, **kwargs):
        return reverse(name, kwargs=kwargs) + f"?tenant={self.slug}"

    def _refused(self, response, message):
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.content)
        body = response.json()
        self.assertEqual(body["error"]["code"], "SHARED_RECORD_READ_ONLY")
        self.assertEqual(body["message"], message)


class TemplateReachTests(_BrightStar):

    def _publish_as(self, caller, **extra):
        body = {"document_type": DOC, "code": "standard", "name": "Refunds",
                "stages": _stages(), **extra}
        return _client(caller).post(self._url("workflow-template-publish"), body, format="json")

    def test_a_branch_admin_naming_no_branch_publishes_for_their_own_branch(self):
        response = self._publish_as(self.bello)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertTrue(WorkflowTemplate.all_objects.filter(
            tenant=self.tenant, branch=self.lekki, code="standard", name="Refunds",
        ).exists())
        self.school_ladder.refresh_from_db()
        self.assertEqual(self.school_ladder.name, "School ladder")

    def test_a_branch_admin_may_name_their_own_branch(self):
        response = self._publish_as(self.bello, branch=self.lekki.pk)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def test_a_branch_admin_cannot_publish_for_another_branch(self):
        response = self._publish_as(self.bello, branch=self.ikeja.pk, code="ikeja")
        self._refused(response, TEMPLATE_OTHER_BRANCH)
        self.ikeja_ladder.refresh_from_db()
        self.assertEqual(self.ikeja_ladder.name, "Ikeja ladder")

    def test_a_caller_covering_two_branches_cannot_publish_the_schools_ladder(self):
        response = self._publish_as(self.emeka)
        self._refused(response, TEMPLATE_SHARED)
        self.school_ladder.refresh_from_db()
        self.assertEqual(self.school_ladder.name, "School ladder")
        self.assertFalse(WorkflowTemplate.all_objects.filter(
            tenant=self.tenant, code="standard", branch__isnull=False,
        ).exists())

    def test_the_whole_school_admin_publishes_the_schools_ladder_not_his_postings(self):
        response = self._publish_as(self.okafor)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.school_ladder.refresh_from_db()
        self.assertEqual(self.school_ladder.name, "Refunds")
        self.assertFalse(WorkflowTemplate.all_objects.filter(
            tenant=self.tenant, branch=self.ikeja, code="standard",
        ).exists())

    def test_the_whole_school_admin_may_publish_for_one_branch(self):
        response = self._publish_as(self.okafor, branch=self.ikeja.pk)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertTrue(WorkflowTemplate.all_objects.filter(
            tenant=self.tenant, branch=self.ikeja, code="standard",
        ).exists())

    def _use_platform(self, caller, template):
        WorkflowTemplate.all_objects.get_or_create(
            tenant=None, branch=None, document_type=DOC, code=template.code,
            defaults={"name": "Platform"},
        )
        return _client(caller).post(
            self._url("workflow-template-use-platform-version", pk=template.pk),
        )

    def test_a_branch_admin_cannot_switch_off_the_schools_ladder(self):
        response = self._use_platform(self.bello, self.school_ladder)
        self._refused(response, TEMPLATE_SHARED)
        self.school_ladder.refresh_from_db()
        self.assertTrue(self.school_ladder.is_active)

    def test_a_branch_admin_may_switch_off_their_own_branchs_ladder(self):
        response = self._use_platform(self.bello, self.lekki_ladder)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.lekki_ladder.refresh_from_db()
        self.assertFalse(self.lekki_ladder.is_active)

    def test_another_branchs_ladder_is_not_found(self):
        response = self._use_platform(self.bello, self.ikeja_ladder)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.content)
        self.ikeja_ladder.refresh_from_db()
        self.assertTrue(self.ikeja_ladder.is_active)

    def test_the_whole_school_admin_may_switch_off_the_schools_ladder(self):
        response = self._use_platform(self.okafor, self.school_ladder)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.school_ladder.refresh_from_db()
        self.assertFalse(self.school_ladder.is_active)


class ApproverGroupReachTests(_BrightStar):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.shared = WorkflowApproverGroup.objects.create(
            tenant=cls.tenant, code="school-approvers", name="School approvers",
        )
        cls.lekki_group = WorkflowApproverGroup.objects.create(
            tenant=cls.tenant, code="lekki-approvers", name="Lekki approvers",
            branch=cls.lekki,
        )
        cls.tunde = make_staff_user(cls.lekki, email="tunde@workflow-reach.test")
        cls.member = WorkflowApproverGroupMember.objects.create(
            group=cls.shared, kind="USER", user=cls.tunde,
        )

    def _create(self, caller, **extra):
        return _client(caller).post(
            self._url("workflow-approver-group-list"),
            {"code": "exam-board", "name": "Exam board", **extra}, format="json",
        )

    def _add(self, caller, group):
        return _client(caller).post(
            self._url("workflow-approver-group-add-member", pk=group.pk),
            {"kind": "USER", "user": str(self.tunde.pk)}, format="json",
        )

    def test_a_branch_admin_naming_no_branch_creates_their_branchs_group(self):
        response = self._create(self.bello)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(
            WorkflowApproverGroup.all_objects.get(code="exam-board").branch_id, self.lekki.pk,
        )

    def test_a_branch_admin_cannot_create_a_group_for_another_branch(self):
        self._refused(self._create(self.bello, branch=self.ikeja.pk), GROUP_OTHER_BRANCH)
        self.assertFalse(WorkflowApproverGroup.all_objects.filter(code="exam-board").exists())

    def test_a_caller_covering_two_branches_cannot_create_a_school_wide_group(self):
        self._refused(self._create(self.emeka), GROUP_SHARED)
        self.assertFalse(WorkflowApproverGroup.all_objects.filter(code="exam-board").exists())

    def test_the_whole_school_admin_creates_a_school_wide_group(self):
        response = self._create(self.okafor)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertIsNone(WorkflowApproverGroup.all_objects.get(code="exam-board").branch_id)

    def test_a_branch_admin_cannot_change_a_school_wide_group_in_any_way(self):
        client = _client(self.bello)
        detail = self._url("workflow-approver-group-detail", pk=self.shared.pk)
        self._refused(client.patch(detail, {"is_active": False}, format="json"), GROUP_SHARED)
        self._refused(self._add(self.bello, self.shared), GROUP_SHARED)
        self._refused(client.delete(self._url(
            "workflow-approver-group-remove-member", pk=self.shared.pk, member_id=self.member.pk,
        )), GROUP_SHARED)
        self._refused(client.delete(detail), GROUP_SHARED)
        self.shared.refresh_from_db()
        self.assertTrue(self.shared.is_active)
        self.assertTrue(WorkflowApproverGroupMember.objects.filter(pk=self.member.pk).exists())

    def test_the_whole_school_admin_may_change_a_school_wide_group(self):
        response = _client(self.okafor).patch(
            self._url("workflow-approver-group-detail", pk=self.shared.pk),
            {"is_active": False}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

    def test_a_branch_admin_may_change_their_own_branchs_group(self):
        response = self._add(self.bello, self.lekki_group)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def test_a_branch_admin_cannot_make_their_group_school_wide(self):
        response = _client(self.bello).patch(
            self._url("workflow-approver-group-detail", pk=self.lekki_group.pk),
            {"branch": None}, format="json",
        )
        self._refused(response, GROUP_SHARED)
        self.lekki_group.refresh_from_db()
        self.assertEqual(self.lekki_group.branch_id, self.lekki.pk)

    def test_a_branch_group_on_the_schools_ladder_is_the_school_wide_admins(self):
        """Bright Star's purchase ladder names Lekki's group, so a member added
        from Lekki would approve Ikeja's purchases too."""
        stage = self.school_ladder.stages.get(code="sign-off")
        stage.approver_source = "WORKFLOW_GROUP"
        stage.approver_group = self.lekki_group
        stage.save(update_fields=["approver_source", "approver_group"])
        self._refused(self._add(self.bello, self.lekki_group), GROUP_SHARED)
        self.assertFalse(WorkflowApproverGroupMember.objects.filter(
            group=self.lekki_group,
        ).exists())

    def test_a_branch_group_on_another_branchs_ladder_needs_that_branch_too(self):
        stage = self.ikeja_ladder.stages.get(code="sign-off")
        stage.approver_source = "WORKFLOW_GROUP"
        stage.approver_group = self.lekki_group
        stage.save(update_fields=["approver_source", "approver_group"])
        self._refused(self._add(self.bello, self.lekki_group), GROUP_OTHER_BRANCH)
        response = self._add(self.emeka, self.lekki_group)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)


class DynamicRoleReachTests(_BrightStar):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.spend = WorkflowDynamicRole.objects.create(
            tenant=cls.tenant, code="spend", name="Spend", document_types=[DOC],
        )
        replace_rules(cls.spend, validate_rules(
            tenant=cls.tenant, document_types=[DOC],
            rules=[{"condition": None, "target_kind": "ROLE", "role_key": "approver"}],
        ))

    def test_a_branch_admin_cannot_create_change_or_delete_a_dynamic_role(self):
        client = _client(self.bello)
        response = client.post(self._url("workflow-dynamic-role-list"), {
            "code": "refunds", "name": "Refunds", "document_types": [DOC],
            "rules": [{"condition": None, "target_kind": "ROLE", "role_key": "approver"}],
        }, format="json")
        self._refused(response, DYNAMIC_ROLE_SHARED)
        self.assertFalse(WorkflowDynamicRole.all_objects.filter(code="refunds").exists())
        detail = self._url("workflow-dynamic-role-detail", pk=self.spend.pk)
        self._refused(client.patch(detail, {"name": "Mine"}, format="json"), DYNAMIC_ROLE_SHARED)
        self._refused(client.delete(detail), DYNAMIC_ROLE_SHARED)
        self.spend.refresh_from_db()
        self.assertEqual(self.spend.name, "Spend")

    def test_the_whole_school_admin_may_change_a_dynamic_role(self):
        response = _client(self.okafor).patch(
            self._url("workflow-dynamic-role-detail", pk=self.spend.pk),
            {"name": "Spend approvals"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.spend.refresh_from_db()
        self.assertEqual(self.spend.name, "Spend approvals")


class StageOverrideReachTests(_BrightStar):
    """Repointing a step is the other way to change who approves a ladder."""

    def _override(self, caller, template):
        return _client(caller).post(self._url("workflow-stage-approver-list"), {
            "stage": template.stages.get(code="sign-off").pk,
            "approver_source": "ROLE", "approver_role_key": "approver",
        }, format="json")

    def test_a_branch_admin_cannot_repoint_the_schools_ladder(self):
        self._refused(self._override(self.bello, self.school_ladder), STAGE_SHARED)
        self.assertFalse(WorkflowStageApproverOverride.all_objects.exists())

    def test_a_branch_admin_cannot_repoint_another_branchs_ladder(self):
        self._refused(self._override(self.bello, self.ikeja_ladder), STAGE_OTHER_BRANCH)
        self.assertFalse(WorkflowStageApproverOverride.all_objects.exists())

    def test_a_branch_admin_may_repoint_their_own_branchs_ladder(self):
        response = self._override(self.bello, self.lekki_ladder)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def test_the_whole_school_admin_may_repoint_the_schools_ladder(self):
        response = self._override(self.okafor, self.school_ladder)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def test_a_branch_admin_cannot_remove_the_schools_override(self):
        row = WorkflowStageApproverOverride.objects.create(
            tenant=self.tenant, stage=self.school_ladder.stages.get(code="sign-off"),
            approver_source="ROLE", approver_role_key="approver",
        )
        response = _client(self.bello).delete(
            self._url("workflow-stage-approver-detail", pk=row.pk),
        )
        self._refused(response, STAGE_SHARED)
        self.assertTrue(WorkflowStageApproverOverride.all_objects.filter(pk=row.pk).exists())


class DelegationReachTests(_BrightStar):
    """Tunde (Lekki) and Mrs Nwankwo (school-wide) hand their approvals to Sade."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde = make_staff_user(cls.lekki, email="tunde@workflow-reach.test")
        cls.nwankwo = make_staff_user(
            None, email="nwankwo@workflow-reach.test", tenant=cls.tenant,
        )
        cls.sade = make_staff_user(cls.lekki, email="sade@workflow-reach.test")

    def setUp(self):
        now = timezone.now()
        self.ends = now + timedelta(days=7)
        self.tundes = ApprovalDelegation.objects.create(
            tenant=self.tenant, delegator=self.tunde, delegate=self.sade,
            starts_at=now, ends_at=self.ends,
        )
        self.nwankwos = ApprovalDelegation.objects.create(
            tenant=self.tenant, delegator=self.nwankwo, delegate=self.sade,
            starts_at=now, ends_at=self.ends,
        )

    def test_the_delegate_cannot_extend_or_delete_a_delegation(self):
        client = _client(self.sade)
        detail = self._url("workflow-delegation-detail", pk=self.tundes.pk)
        later = (self.ends + timedelta(days=30)).isoformat()
        response = client.patch(detail, {"ends_at": later}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.content)
        response = client.delete(detail)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.content)
        self.tundes.refresh_from_db()
        self.assertEqual(self.tundes.ends_at, self.ends)

    def test_the_delegator_may_change_their_own(self):
        later = self.ends + timedelta(days=1)
        response = _client(self.tunde).patch(
            self._url("workflow-delegation-detail", pk=self.tundes.pk),
            {"ends_at": later.isoformat()}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        response = _client(self.tunde).post(
            self._url("workflow-delegation-revoke", pk=self.tundes.pk),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

    def test_a_branch_admin_may_revoke_their_own_branchs_persons_delegation(self):
        response = _client(self.bello).post(
            self._url("workflow-delegation-revoke", pk=self.tundes.pk),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.tundes.refresh_from_db()
        self.assertIsNotNone(self.tundes.revoked_at)

    def test_a_branch_admin_cannot_revoke_a_school_wide_persons_delegation(self):
        response = _client(self.bello).post(
            self._url("workflow-delegation-revoke", pk=self.nwankwos.pk),
        )
        self._refused(response, DELEGATION_SHARED)
        self.nwankwos.refresh_from_db()
        self.assertIsNone(self.nwankwos.revoked_at)

    def test_the_whole_school_admin_may_revoke_anybodys_delegation(self):
        response = _client(self.okafor).post(
            self._url("workflow-delegation-revoke", pk=self.nwankwos.pk),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
