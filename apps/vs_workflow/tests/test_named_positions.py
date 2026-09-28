"""A stage or an approver group naming one post, on the chart its tenant uses.

The engine's half of the rule, told without any tenant's own chart: which chart
a row's tenant binds to, the refusals when the code is not on it, and the
database constraints that keep one post per row. Resolving a post on a tenant's
own chart is tested by the app that registers that chart.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase, tag

from core.migration_testing import RewoundSchemaTestCase
from vs_tenants.models import Tenant
from vs_workflow.exceptions import UnknownPositionError
from vs_workflow.models import (
    WorkflowApproverGroup, WorkflowApproverGroupMember, WorkflowStage, WorkflowTemplate,
)
from vs_workflow.services.templates import publish_template


def _cx_seat(code="CX-SEAT"):
    from vs_user.models import OrgNode, Position

    node, _ = OrgNode.objects.get_or_create(
        code="DV-NAMED", defaults={"name": "Named", "kind": "DIVISION"},
    )
    return Position.objects.create(title="Chief Auditor", code=code, org_node=node)


def _platform():
    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


class BindingTests(TestCase):
    """Which chart a code is looked up on, and what happens when it is not there."""

    def _publish(self, tenant, code, **stage):
        return publish_template(
            tenant=tenant, document_type="NAMED_DOC", code="named", name="Named",
            stages_payload=[{
                "code": "s1", "label": "Named", "approver_source": "ORGANOGRAM",
                "organogram_target": "SPECIFIC_POSITION",
                "organogram_position_code": code, **stage,
            }],
        )

    def test_the_platform_tenant_binds_a_seat_on_the_cx_chart(self):
        seat = _cx_seat()
        stage = self._publish(_platform(), "CX-SEAT").stages.get()
        self.assertEqual(
            (stage.organogram_position_id, stage.organogram_tenant_position_id),
            (seat.pk, None),
        )

    def test_an_unknown_seat_is_refused_on_the_cx_chart_too(self):
        with self.assertRaises(UnknownPositionError) as refused:
            self._publish(None, "NO-SUCH-SEAT")
        self.assertIn("'NO-SUCH-SEAT'", refused.exception.message)
        self.assertFalse(WorkflowTemplate.all_objects.filter(document_type="NAMED_DOC").exists())

    def test_a_tenant_kind_with_no_chart_cannot_name_a_post(self):
        """Refused naming the code, rather than stored as a step that reaches nobody."""
        _cx_seat()
        tenant = Tenant.objects.create(
            name="Chartless", slug="chartless", kind=Tenant.Kind.ORGANIZATION,
        )
        with self.assertRaises(UnknownPositionError) as refused:
            self._publish(tenant, "CX-SEAT")
        self.assertIn("no organogram", refused.exception.message)

    def test_a_stage_that_names_no_post_ignores_a_stray_code(self):
        stage = self._publish(None, "NO-SUCH-SEAT", organogram_target="DIRECT_MANAGER").stages.get()
        self.assertEqual(
            (stage.organogram_position_id, stage.organogram_tenant_position_id), (None, None),
        )


class OnePostPerRowTests(TestCase):
    """The database refuses a row naming two posts, or the same post twice."""

    def setUp(self):
        self.seat = _cx_seat()
        self.group = WorkflowApproverGroup.all_objects.create(
            tenant=_platform(), code="named", name="Named",
        )

    def test_a_stage_never_names_both_charts(self):
        template = WorkflowTemplate.objects.create(
            document_type="NAMED_DOC", code="both", name="Both",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkflowStage.objects.create(
                template=template, code="s1", label="S1",
                organogram_position=self.seat, organogram_tenant_position_id=7,
            )

    def test_a_position_member_names_exactly_one_post(self):
        for reference in ({}, {"position": self.seat, "tenant_position_id": 7}):
            with self.subTest(reference=reference), \
                    self.assertRaises(IntegrityError), transaction.atomic():
                WorkflowApproverGroupMember.objects.create(
                    group=self.group, kind="POSITION", **reference,
                )

    def test_a_user_or_role_member_carries_no_post(self):
        from vs_rbac.tests.helpers import make_school_admin

        user = make_school_admin(None, email="named-user@test.com", tenant=_platform())
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkflowApproverGroupMember.objects.create(
                group=self.group, kind="USER", user=user, tenant_position_id=7,
            )

    def test_a_group_names_a_tenant_post_once(self):
        WorkflowApproverGroupMember.objects.create(
            group=self.group, kind="POSITION", tenant_position_id=7,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkflowApproverGroupMember.objects.create(
                group=self.group, kind="POSITION", tenant_position_id=7,
            )


@tag("slow")
class NamedPostMigrationTests(RewoundSchemaTestCase):
    """0019 adds the columns without touching a row, and reverses cleanly."""

    APP = "vs_workflow"
    BEFORE = "0018_a_restricted_role_grant_is_decided_by_a_ladder"
    AFTER = "0019_a_named_post_on_a_tenants_own_chart"

    def test_a_cx_seat_member_survives_both_ways(self):
        seat = _cx_seat()
        apps = self.historical_apps(self.BEFORE)
        Group = apps.get_model("vs_workflow", "WorkflowApproverGroup")
        Member = apps.get_model("vs_workflow", "WorkflowApproverGroupMember")
        group = Group.objects.create(id="named01", tenant_id=_platform().pk,
                                     code="named", name="Named")
        Member.objects.create(id="member01", group=group, kind="POSITION", position_id=seat.pk)

        self.migrate_to(self.AFTER)
        Member = self.historical_apps(self.AFTER).get_model(
            "vs_workflow", "WorkflowApproverGroupMember")
        row = Member.objects.get(pk="member01")
        self.assertEqual((row.position_id, row.tenant_position_id), (seat.pk, None))

        self.migrate_to(self.BEFORE)
        Member = self.historical_apps(self.BEFORE).get_model(
            "vs_workflow", "WorkflowApproverGroupMember")
        self.assertEqual(Member.objects.get(pk="member01").position_id, seat.pk)
