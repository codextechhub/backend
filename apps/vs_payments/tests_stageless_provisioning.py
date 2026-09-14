"""What a tenant's books arrive with for payouts, and what only a deliberate ask adds.

A tenant's books arrive holding a payout-approval route with **no steps in it**, and no
approver group. Who signs off on money leaving is read from the organogram the tenant
builds; a ladder published at creation would be a guess at the people and at the amount
that needs a second pair of eyes.

The empty row still earns its place. It stands in front of the shared platform route, so
a change to that shared row can never begin governing this tenant's cash-out, and a
batch submitted against it is refused as unconfigured rather than paid unseen.

Two hazards sit either side of that, and both are covered here. Publishing a stageless
payload over a route that already holds real steps would soft-retire every one of them,
so provisioning skips a route that exists. Treating the empty placeholder as "this
tenant already has rules" would make the seed command a no-op, so the deliberate path
fills an empty route in.

The second half of the file covers the sweep that removes the groups tenants were given
before any of this was true.
"""
from __future__ import annotations

import importlib
import io

from django.apps import apps as live_apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase

from vs_finance.models import LedgerEntity
from vs_finance.provisioning import provision_entity
from vs_finance.seed import seed_currencies
from vs_tenants.models import Tenant
from vs_workflow.models import (
    WorkflowApproverGroup,
    WorkflowApproverGroupMember,
    WorkflowInstance,
    WorkflowStage,
    WorkflowStageInstance,
    WorkflowTemplate,
)

from .approvals import DOCUMENT_TYPE
from .constants import (
    WF_DEFAULT_APPROVE_GROUP,
    WF_DEFAULT_HIGH_VALUE_GROUP,
    WF_DEFAULT_TEMPLATE_CODE,
)

SEEDED_GROUPS = (WF_DEFAULT_APPROVE_GROUP, WF_DEFAULT_HIGH_VALUE_GROUP)


def _tenant(slug, name):
    """A customer tenant, built from ``vs_tenants`` alone."""
    return Tenant.objects.create(
        name=name, slug=slug, kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
    )


def _books(tenant, code):
    """A set of books whose creation runs every registered provisioner."""
    entity = LedgerEntity.objects.create(
        name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
    )
    provision_entity(entity)
    return entity


def _template(tenant):
    return WorkflowTemplate.all_objects.get(
        tenant=tenant, branch=None, code=WF_DEFAULT_TEMPLATE_CODE,
        document_type=DOCUMENT_TYPE,
    )


def _live_stage_codes(template):
    """The step codes that would actually run, retired history excluded."""
    return set(
        template.stages.filter(retired_at__isnull=True).values_list("code", flat=True)
    )


class ProvisionedBooksCarryNoPayoutLadderTests(TestCase):
    """What provisioning publishes, and what it deliberately leaves to the tenant."""

    def setUp(self):
        seed_currencies()

    def test_the_payout_route_arrives_with_no_steps(self):
        """The route exists so the tenant never resolves to the shared platform row."""
        tenant = _tenant("larch-pay", "Larch")
        _books(tenant, "LARCHPAY")

        template = _template(tenant)
        # Nothing live and nothing retired: no step was ever published here.
        self.assertEqual(template.stages.count(), 0)

    def test_provisioning_creates_no_approver_group(self):
        """A group exists to be named by a step, and there are no steps."""
        tenant = _tenant("rowan-pay", "Rowan")
        _books(tenant, "ROWANPAY")

        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(
                tenant=tenant, code__in=SEEDED_GROUPS).exists(),
        )

    def test_a_tenant_that_already_has_a_ladder_keeps_every_step(self):
        """The destructive case, and the reason provisioning skips an existing route.

        Publishing a stageless payload over a real ladder would soft-retire every step
        in it: the tenant would still see a maker-checker on its screen and its next
        batch would route through none of it. A second set of books in one tenant runs
        provisioning again, so this is not a hypothetical path.
        """
        from .approvals import ensure_tenant_approval_templates

        tenant = _tenant("ash-pay", "Ash")
        _books(tenant, "ASHPAY")
        ensure_tenant_approval_templates(tenant)
        self.assertEqual(_live_stage_codes(_template(tenant)), {"checker", "senior"})

        second = LedgerEntity.objects.create(
            name="Ash Annex", code="ASHPAY2", kind=LedgerEntity.Kind.TENANT,
            tenant=tenant,
        )
        provision_entity(second)

        template = _template(tenant)
        self.assertEqual(_live_stage_codes(template), {"checker", "senior"})
        self.assertFalse(template.stages.filter(retired_at__isnull=False).exists())

    def test_the_seeding_command_still_publishes_the_full_ladder(self):
        """A tenant asking for the default rules gets them, empty route or not.

        The route provisioning published is a placeholder holding nothing anybody
        chose. Treating it as "this tenant already has rules" would make the command a
        no-op for every tenant whose books published the placeholder, and the tenant
        would be told it had a maker-checker no batch could ever route through.
        """
        tenant = _tenant("birch-pay", "Birch")
        _books(tenant, "BIRCHPAY")
        self.assertEqual(_live_stage_codes(_template(tenant)), set())

        call_command(
            "seed_payout_approvals", "--tenant", tenant.slug, stdout=io.StringIO())

        self.assertEqual(_live_stage_codes(_template(tenant)), {"checker", "senior"})
        for code in SEEDED_GROUPS:
            self.assertTrue(
                WorkflowApproverGroup.all_objects.filter(
                    tenant=tenant, code=code).exists(),
                code,
            )

    def test_filling_an_empty_route_reports_it_as_created(self):
        """The placeholder held nothing, so publishing the steps is the creation."""
        from .approvals import ensure_tenant_approval_templates

        tenant = _tenant("elm-pay", "Elm")
        _books(tenant, "ELMPAY")

        template, created = ensure_tenant_approval_templates(tenant)

        self.assertTrue(created)
        self.assertEqual(_live_stage_codes(template), {"checker", "senior"})

    def test_seeding_twice_leaves_the_tenant_ladder_alone(self):
        """Once the steps are real, they are somebody's decision and stay put."""
        from .approvals import ensure_tenant_approval_templates

        tenant = _tenant("oak-pay", "Oak")
        _books(tenant, "OAKPAY")
        ensure_tenant_approval_templates(tenant, approve_group_code="other-approvers")

        again, created = ensure_tenant_approval_templates(tenant)

        self.assertFalse(created)
        checker = again.stages.get(code="checker")
        self.assertEqual(checker.approver_group.code, "other-approvers")


class SeededApproverGroupCleanupTests(TestCase):
    """The sweep that removes the payout groups a tenant was given without asking."""

    def setUp(self):
        seed_currencies()
        self.tenant = _tenant("sweep-pay", "Sweep")

    def _sweep(self):
        migration = importlib.import_module(
            "vs_payments.migrations.0007_approver_groups_nobody_asked_for")
        migration.remove_approver_groups_nobody_asked_for(live_apps, None)

    def _seeded_ladder(self):
        """Exactly what a tenant used to be given at provisioning time."""
        from .approvals import ensure_tenant_approval_templates

        ensure_tenant_approval_templates(self.tenant)

    def _group(self, code):
        return WorkflowApproverGroup.all_objects.get(tenant=self.tenant, code=code)

    def _a_user(self, local_part):
        return get_user_model().objects.create_user(
            email=f"{local_part}@sweep-pay.test", tenant=self.tenant, status="ACTIVE",
            first_name=local_part.title(), last_name="Tester",
        )

    def _run_a_batch_through(self, stage):
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
        group = self._group(WF_DEFAULT_APPROVE_GROUP)
        stage_ids = list(
            WorkflowStage.objects.filter(approver_group=group).values_list("pk", flat=True))
        self.assertTrue(stage_ids)

        self._sweep()

        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(
                tenant=self.tenant, code__in=SEEDED_GROUPS).exists(),
        )
        self.assertFalse(WorkflowStage.objects.filter(pk__in=stage_ids).exists())
        # The route stays, empty. That is the intended end state, not a leftover.
        self.assertEqual(_live_stage_codes(_template(self.tenant)), set())

    def test_a_group_with_a_member_survives_with_its_steps(self):
        """A staffed group is somebody's decision, whatever its code says."""
        self._seeded_ladder()
        group = self._group(WF_DEFAULT_APPROVE_GROUP)
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
                tenant=self.tenant, code=WF_DEFAULT_HIGH_VALUE_GROUP).exists(),
        )

    def test_a_group_whose_step_has_run_survives_with_its_steps(self):
        """A step a batch ran through is evidence, and the database refuses to lose it."""
        self._seeded_ladder()
        group = self._group(WF_DEFAULT_APPROVE_GROUP)
        stages = list(WorkflowStage.objects.filter(approver_group=group))
        self._run_a_batch_through(stages[0])

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
        procurement, _ = ensure_approver_group(self.tenant, "procurement-approver")

        self._sweep()

        self.assertTrue(
            WorkflowApproverGroup.all_objects.filter(pk=adjustment.pk).exists())
        self.assertTrue(
            WorkflowApproverGroup.all_objects.filter(pk=procurement.pk).exists())
