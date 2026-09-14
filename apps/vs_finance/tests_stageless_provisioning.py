"""What a tenant's books arrive with, and what only a deliberate ask publishes.

A tenant's books arrive holding an approval route per finance document type with **no
steps in it**, and no approver group. Who approves a refund, a waiver or a staff
reimbursement is read from the organogram the tenant builds; a ladder published at
creation would be a guess at both the people and the amounts.

The empty route is not the same as no route, and for these documents that difference is
the whole gate. With no route at all the direct post is silently allowed. With the empty
route it is approval-undecided: refused until somebody confirms it in as many words,
recorded against them.

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
    WF_ADJUSTMENT_APPROVER_GROUP,
    WF_DEFAULT_TEMPLATE_CODE,
    WF_EXPENSE_CLAIM_APPROVER_GROUP,
    WF_SENIOR_ADJUSTMENT_APPROVER_GROUP,
)
from .models import LedgerEntity
from .provisioning import provision_entity
from .seed import seed_currencies

ADJUSTMENT_TYPES = (
    "finance.refund", "finance.write_off", "finance.concession", "finance.credit_note",
)
EXPENSE_CLAIM_TYPE = "finance.expense_claim"
ALL_TYPES = ADJUSTMENT_TYPES + (EXPENSE_CLAIM_TYPE,)
SEEDED_GROUPS = (
    WF_ADJUSTMENT_APPROVER_GROUP,
    WF_SENIOR_ADJUSTMENT_APPROVER_GROUP,
    WF_EXPENSE_CLAIM_APPROVER_GROUP,
)


def _tenant(slug, name):
    """A customer tenant, built from ``vs_tenants`` alone.

    The finance engine must not know that a school is what usually sits behind one.
    """
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


def _template(tenant, document_type):
    return WorkflowTemplate.all_objects.get(
        tenant=tenant, branch=None, code=WF_DEFAULT_TEMPLATE_CODE,
        document_type=document_type,
    )


def _live_stage_codes(template):
    """The step codes that would actually run, retired history excluded."""
    return set(
        template.stages.filter(retired_at__isnull=True).values_list("code", flat=True)
    )


class ProvisionedBooksCarryNoLadderTests(TestCase):
    """What provisioning publishes, and what it deliberately leaves to the tenant."""

    def setUp(self):
        seed_currencies()

    def test_every_finance_type_gets_a_route_of_its_own_with_no_steps(self):
        """The route exists so the tenant's own answer has somewhere to go."""
        tenant = _tenant("larch-fin", "Larch")
        _books(tenant, "LARCHFIN")

        for document_type in ALL_TYPES:
            with self.subTest(document_type=document_type):
                template = _template(tenant, document_type)
                # Nothing live and nothing retired: no step was ever published here.
                self.assertEqual(template.stages.count(), 0)

    def test_provisioning_creates_no_approver_group(self):
        """A group exists to be named by a step, and there are no steps.

        A tenant opening its approvals screen on day one should not find groups it
        never asked for, named after a workflow nobody there has read.
        """
        tenant = _tenant("rowan-fin", "Rowan")
        _books(tenant, "ROWANFIN")

        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(
                tenant=tenant, code__in=SEEDED_GROUPS).exists(),
        )

    def test_a_tenant_that_already_has_a_ladder_keeps_every_step(self):
        """The destructive case, and the reason provisioning skips an existing route.

        Publishing a stageless payload over a real ladder would soft-retire every step
        in it: the tenant would still see a ladder on its screen and its next refund
        would route through none of it. A second set of books in one tenant runs
        provisioning again, so this is not a hypothetical path.
        """
        from .approvals import (
            ensure_tenant_approval_templates, ensure_tenant_expense_claim_template,
        )

        tenant = _tenant("ash-fin", "Ash")
        _books(tenant, "ASHFIN")
        # The tenant asks for the default ladders, deliberately.
        ensure_tenant_approval_templates(tenant)
        ensure_tenant_expense_claim_template(tenant)
        before = {dt: _live_stage_codes(_template(tenant, dt)) for dt in ALL_TYPES}
        self.assertTrue(all(before.values()))

        second = LedgerEntity.objects.create(
            name="Ash Annex", code="ASHFIN2", kind=LedgerEntity.Kind.TENANT,
            tenant=tenant,
        )
        provision_entity(second)

        for document_type in ALL_TYPES:
            with self.subTest(document_type=document_type):
                template = _template(tenant, document_type)
                self.assertEqual(_live_stage_codes(template), before[document_type])
                self.assertFalse(
                    template.stages.filter(retired_at__isnull=False).exists())

    def test_the_seeding_command_still_publishes_the_full_ladder(self):
        """A tenant asking for the default rules gets them, empty route or not.

        The route provisioning published is a placeholder holding nothing anybody
        chose. Treating it as "this tenant already has rules" would make the command a
        no-op for every tenant whose books published the placeholder, and the tenant
        would be told it had ladders no document could ever route through.
        """
        tenant = _tenant("birch-fin", "Birch")
        _books(tenant, "BIRCHFIN")
        for document_type in ALL_TYPES:
            self.assertEqual(_live_stage_codes(_template(tenant, document_type)), set())

        call_command(
            "seed_finance_approvals", "--tenant", tenant.slug, stdout=io.StringIO())

        for document_type in ADJUSTMENT_TYPES:
            with self.subTest(document_type=document_type):
                self.assertTrue(_live_stage_codes(_template(tenant, document_type)))
        self.assertEqual(
            _live_stage_codes(_template(tenant, EXPENSE_CLAIM_TYPE)),
            {"finance-approval"},
        )
        for code in SEEDED_GROUPS:
            self.assertTrue(
                WorkflowApproverGroup.all_objects.filter(
                    tenant=tenant, code=code).exists(),
                code,
            )

    def test_seeding_twice_leaves_the_tenant_ladder_alone(self):
        """Once the steps are real, they are somebody's decision and stay put."""
        from .approvals import ensure_tenant_approval_templates

        tenant = _tenant("oak-fin", "Oak")
        _books(tenant, "OAKFIN")
        ensure_tenant_approval_templates(tenant, threshold=10)
        senior = _template(tenant, "finance.concession").stages.get(code="senior")

        again = ensure_tenant_approval_templates(tenant, threshold=99_000_000)

        self.assertEqual([created for _template_row, created in again], [False] * 4)
        senior.refresh_from_db()
        # The tenant's own threshold survives a re-run with a different default.
        self.assertEqual(senior.inclusion_condition["value"], 10)


class SeededApproverGroupCleanupTests(TestCase):
    """The sweep that removes the groups a tenant was given without asking.

    Driven through the migration's own function rather than by rewinding the migration
    graph. The rules worth covering are the three refusals below, and calling the sweep
    directly keeps the cover on those rather than on Django's executor.
    """

    def setUp(self):
        seed_currencies()
        self.tenant = _tenant("sweep-fin", "Sweep")

    def _sweep(self):
        migration = importlib.import_module(
            "vs_finance.migrations.0028_approver_groups_nobody_asked_for")
        migration.remove_approver_groups_nobody_asked_for(live_apps, None)

    def _seeded_ladder(self):
        """Exactly what a tenant used to be given at provisioning time."""
        from .approvals import (
            ensure_tenant_approval_templates, ensure_tenant_expense_claim_template,
        )

        ensure_tenant_approval_templates(self.tenant)
        ensure_tenant_expense_claim_template(self.tenant)

    def _group(self, code):
        return WorkflowApproverGroup.all_objects.get(tenant=self.tenant, code=code)

    def _a_user(self, local_part):
        return get_user_model().objects.create_user(
            email=f"{local_part}@sweep-fin.test", tenant=self.tenant, status="ACTIVE",
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
        group = self._group(WF_ADJUSTMENT_APPROVER_GROUP)
        stage_ids = list(
            WorkflowStage.objects.filter(approver_group=group).values_list("pk", flat=True))
        self.assertTrue(stage_ids)

        self._sweep()

        self.assertFalse(WorkflowApproverGroup.all_objects.filter(pk=group.pk).exists())
        self.assertFalse(WorkflowStage.objects.filter(pk__in=stage_ids).exists())
        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(
                tenant=self.tenant, code__in=SEEDED_GROUPS).exists(),
        )
        # The routes stay, empty. That is the intended end state, not a leftover.
        for document_type in ALL_TYPES:
            with self.subTest(document_type=document_type):
                self.assertEqual(
                    _live_stage_codes(_template(self.tenant, document_type)), set())

    def test_a_group_with_a_member_survives_with_its_steps(self):
        """A staffed group is somebody's decision, whatever its code says."""
        self._seeded_ladder()
        group = self._group(WF_ADJUSTMENT_APPROVER_GROUP)
        WorkflowApproverGroupMember.objects.create(
            group=group, kind="USER", user=self._a_user("member"))
        stage_count = WorkflowStage.objects.filter(approver_group=group).count()

        self._sweep()

        self.assertTrue(WorkflowApproverGroup.all_objects.filter(pk=group.pk).exists())
        self.assertEqual(
            WorkflowStage.objects.filter(approver_group=group).count(), stage_count)
        # Its unstaffed siblings are still swept, one group at a time.
        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(
                tenant=self.tenant, code=WF_EXPENSE_CLAIM_APPROVER_GROUP).exists(),
        )

    def test_a_group_whose_step_has_run_survives_with_its_steps(self):
        """A step a document ran through is evidence, and the database refuses to lose it.

        The group is left whole rather than half-cleared: the siblings of the step that
        ran stay with it, so the ladder a reader sees is the ladder that ran.
        """
        self._seeded_ladder()
        group = self._group(WF_ADJUSTMENT_APPROVER_GROUP)
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

        payout, _ = ensure_approver_group(self.tenant, "payout-approver")
        procurement, _ = ensure_approver_group(self.tenant, "procurement-approver")

        self._sweep()

        self.assertTrue(WorkflowApproverGroup.all_objects.filter(pk=payout.pk).exists())
        self.assertTrue(
            WorkflowApproverGroup.all_objects.filter(pk=procurement.pk).exists())
