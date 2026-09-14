"""The sweep that removes the procurement approver groups nobody asked for.

A tenant that had said nothing about who approves a purchase used to be handed two
groups with nobody in them and a ladder whose threshold was a guess. Provisioning now
publishes the route and leaves it empty, and this is the clean-up of what the earlier
behaviour left behind.

Three refusals keep the sweep from touching anything anybody chose, and each has a test
below: a staffed group is somebody's decision; a step a document ran through is
evidence the database will not let go; and only the codes this app seeded are ever in
scope, so leave approvals and a tenant's own groups are never considered.

Driven through the migration's own function rather than by rewinding the migration
graph. The rules worth covering are those refusals, and calling the sweep directly keeps
the cover on them rather than on Django's executor.
"""
from __future__ import annotations

import importlib

from django.apps import apps as live_apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from vs_finance.models import LedgerEntity
from vs_tenants.models import Tenant
from vs_workflow.models import (
    WorkflowApproverGroup,
    WorkflowApproverGroupMember,
    WorkflowInstance,
    WorkflowStage,
    WorkflowStageInstance,
    WorkflowTemplate,
)

from .constants import (
    WF_DEFAULT_MANAGER_GROUP,
    WF_DEFAULT_SENIOR_GROUP,
    WF_DEFAULT_TEMPLATE_CODE,
    WF_DOCTYPE_REQUISITION,
)

SEEDED_GROUPS = (WF_DEFAULT_MANAGER_GROUP, WF_DEFAULT_SENIOR_GROUP)


def _live_stage_codes(template):
    """The step codes that would actually run, retired history excluded."""
    return set(
        template.stages.filter(retired_at__isnull=True).values_list("code", flat=True)
    )


class SeededApproverGroupCleanupTests(TestCase):
    """What the sweep removes, and the three things it refuses to touch."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Sweep", slug="sweep-proc", kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )

    def _sweep(self):
        migration = importlib.import_module(
            "vs_procurement.migrations.0034_approver_groups_nobody_asked_for")
        migration.remove_approver_groups_nobody_asked_for(live_apps, None)

    def _seeded_ladder(self):
        """Exactly what a tenant used to be given at provisioning time."""
        from .approvals import ensure_tenant_approval_templates

        ensure_tenant_approval_templates(self.tenant)

    def _group(self, code):
        return WorkflowApproverGroup.all_objects.get(tenant=self.tenant, code=code)

    def _a_user(self, local_part):
        return get_user_model().objects.create_user(
            email=f"{local_part}@sweep-proc.test", tenant=self.tenant, status="ACTIVE",
            first_name=local_part.title(), last_name="Tester",
        )

    def _run_a_document_through(self, stage):
        """A stage instance, which is the evidence that protects a step."""
        instance = WorkflowInstance.all_objects.create(
            tenant=self.tenant, template=stage.template,
            document_content_type=ContentType.objects.get_for_model(LedgerEntity),
            document_object_id="1", document_type=stage.template.document_type,
            requested_by=self._a_user("submitter"),
        )
        return WorkflowStageInstance.objects.create(instance=instance, stage=stage)

    def test_an_unused_group_and_the_steps_naming_it_are_removed(self):
        """Nobody in it and nothing ran through it, so it held no decision."""
        self._seeded_ladder()
        group = self._group(WF_DEFAULT_MANAGER_GROUP)
        stage_ids = list(
            WorkflowStage.objects.filter(approver_group=group).values_list("pk", flat=True))
        self.assertTrue(stage_ids)

        self._sweep()

        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(
                tenant=self.tenant, code__in=SEEDED_GROUPS).exists(),
        )
        self.assertFalse(WorkflowStage.objects.filter(pk__in=stage_ids).exists())
        # The routes stay, empty. That is the intended end state, not a leftover.
        template = WorkflowTemplate.all_objects.get(
            tenant=self.tenant, branch=None, code=WF_DEFAULT_TEMPLATE_CODE,
            document_type=WF_DOCTYPE_REQUISITION,
        )
        self.assertEqual(_live_stage_codes(template), set())

    def test_a_group_with_a_member_survives_with_its_steps(self):
        """A staffed group is somebody's decision, whatever its code says."""
        self._seeded_ladder()
        group = self._group(WF_DEFAULT_MANAGER_GROUP)
        WorkflowApproverGroupMember.objects.create(
            group=group, kind="USER", user=self._a_user("member"))
        stage_count = WorkflowStage.objects.filter(approver_group=group).count()

        self._sweep()

        self.assertTrue(WorkflowApproverGroup.all_objects.filter(pk=group.pk).exists())
        self.assertEqual(
            WorkflowStage.objects.filter(approver_group=group).count(), stage_count)
        # Its unstaffed sibling is still swept, one group at a time.
        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(
                tenant=self.tenant, code=WF_DEFAULT_SENIOR_GROUP).exists(),
        )

    def test_a_group_whose_step_has_run_survives_with_its_steps(self):
        """A step a document ran through is evidence, and the database refuses to lose it.

        The group is left whole rather than half-cleared: the siblings of the step that
        ran stay with it, so the ladder a reader sees is the ladder that ran.
        """
        self._seeded_ladder()
        group = self._group(WF_DEFAULT_MANAGER_GROUP)
        stages = list(WorkflowStage.objects.filter(approver_group=group))
        self._run_a_document_through(stages[0])

        self._sweep()

        self.assertTrue(WorkflowApproverGroup.all_objects.filter(pk=group.pk).exists())
        self.assertEqual(
            WorkflowStage.objects.filter(approver_group=group).count(), len(stages))

    def test_a_group_this_app_did_not_seed_is_untouched(self):
        """Leave approvals, and anything a tenant built itself, are out of scope."""
        from vs_workflow.services.groups import ensure_approver_group

        leave, _ = ensure_approver_group(self.tenant, "leave-approvers")
        hand_made, _ = ensure_approver_group(self.tenant, "special")
        self._seeded_ladder()

        self._sweep()

        self.assertTrue(WorkflowApproverGroup.all_objects.filter(pk=leave.pk).exists())
        self.assertTrue(WorkflowApproverGroup.all_objects.filter(pk=hand_made.pk).exists())

    def test_another_apps_groups_are_left_to_that_app(self):
        """Each migration deletes only its own vocabulary."""
        from vs_workflow.services.groups import ensure_approver_group

        adjustment, _ = ensure_approver_group(self.tenant, "finance-adjustment-approver")
        payout, _ = ensure_approver_group(self.tenant, "payout-approver")

        self._sweep()

        self.assertTrue(
            WorkflowApproverGroup.all_objects.filter(pk=adjustment.pk).exists())
        self.assertTrue(WorkflowApproverGroup.all_objects.filter(pk=payout.pk).exists())
