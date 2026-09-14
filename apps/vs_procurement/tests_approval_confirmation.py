"""What a school's books arrive with, and how spend moves when no ladder exists yet.

Two halves of one story.

A school's books arrive holding a route per approvable document type with **no steps
in it**, and no approver group. Who approves a school's spend comes from the organogram
that school builds; a ladder published at creation would be a guess at both the people
and the amounts. The empty route still matters and is not the same as no route: it
stands in front of the shared platform row, so a change to that shared row can never
begin to govern this school's spend.

The consequence is that the first requisition of a school's life submits against a
template with no steps, which the engine refuses rather than approving unseen. The
refusal tells the submitter to confirm that the document goes out anyway, so the
confirmation has to be something a request can actually carry, through every one of the
four submit endpoints. These tests hold both ends of that: the refusal, and the
confirmation that clears it and is recorded against whoever gave it.

The case that matters most is the last one. A confirmation must never be a way past a
ladder that genuinely exists: a school that has built its steps sees them run, whatever
the request body says.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_audit.models import AuditEvent
from vs_finance.constants import DocumentStatus
from vs_finance.models import LedgerEntity
from vs_workflow.constants import WorkflowInstanceStatus
from vs_workflow.models import (
    WorkflowApproverGroup, WorkflowInstance, WorkflowTemplate,
)

from vs_procurement.constants import (
    PROCUREMENT_APPROVAL_TYPES,
    ProcApprovalState,
    WF_DEFAULT_MANAGER_GROUP,
    WF_DEFAULT_SENIOR_GROUP,
    WF_DEFAULT_TEMPLATE_CODE,
    WF_DOCTYPE_REQUISITION,
)
from vs_procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    VendorInvoice,
    VendorInvoiceLine,
    VendorPayment,
    VendorPaymentAllocation,
)
from vs_procurement.tests import _P2PFixtureMixin, _platform_tenant


def _live_stage_codes(template):
    """The step codes that would actually run, retired history excluded."""
    return set(
        template.stages.filter(retired_at__isnull=True)
        .values_list("code", flat=True)
    )


class ProvisionedBooksCarryNoLadderTests(TestCase):
    """What provisioning publishes, and what it deliberately leaves for the school."""

    def _provisioned(self, slug, code):
        """A school whose books have been provisioned. Returns ``(school, entity)``."""
        from vs_finance.provisioning import provision_entity
        from schools.vs_schools.models import School

        school = School.objects.create(
            name=slug.title(), slug=slug, code=code, status="ACTIVE")
        entity = LedgerEntity.objects.create(
            name=f"{slug.title()} Books", code=f"{code}B",
            kind=LedgerEntity.Kind.TENANT, tenant=school.tenant,
        )
        provision_entity(entity)
        return school, entity

    def _tenant_template(self, tenant, document_type=WF_DOCTYPE_REQUISITION):
        return WorkflowTemplate.all_objects.get(
            tenant=tenant, branch=None, code=WF_DEFAULT_TEMPLATE_CODE,
            document_type=document_type,
        )

    def test_every_approvable_type_gets_a_route_of_its_own_with_no_steps(self):
        """The route exists so the school never resolves to the shared platform row.

        Empty, because nobody has said who approves anything yet.
        """
        school, _entity = self._provisioned("larch-confirm", "LRCCF")

        for document_type in PROCUREMENT_APPROVAL_TYPES:
            with self.subTest(document_type=document_type):
                template = self._tenant_template(school.tenant, document_type)
                # Nothing live and nothing retired: no step was ever published here.
                self.assertEqual(template.stages.count(), 0)

    def test_no_approver_group_is_created(self):
        """A group exists to be named by a step, and there are no steps.

        A school opening its approvals screen on day one should not find groups it
        never asked for, named after a workflow it has not read.
        """
        school, _entity = self._provisioned("rowan-confirm", "RWNCF")

        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(
                tenant=school.tenant,
                code__in=(WF_DEFAULT_MANAGER_GROUP, WF_DEFAULT_SENIOR_GROUP),
            ).exists(),
        )

    def test_a_school_that_already_has_a_ladder_keeps_every_step(self):
        """The destructive case, and the reason provisioning skips existing routes.

        Publishing a stageless payload over a school's real ladder would soft-retire
        every step in it: the school would still see a ladder on its screen, and its
        next purchase order would route through none of it. The second set of books in
        one school runs provisioning again, so this is not a hypothetical path.
        """
        from vs_procurement.approvals import ensure_tenant_approval_templates
        from vs_finance.provisioning import provision_entity

        school, _first = self._provisioned("ash-confirm", "ASHCF")
        # The school asks for the default ladder, deliberately.
        ensure_tenant_approval_templates(school.tenant)
        template = self._tenant_template(school.tenant)
        self.assertEqual(_live_stage_codes(template), {"manager", "senior"})

        second = LedgerEntity.objects.create(
            name="Ash Annex", code="ASHCF2", kind=LedgerEntity.Kind.TENANT,
            tenant=school.tenant,
        )
        provision_entity(second)

        template.refresh_from_db()
        self.assertEqual(_live_stage_codes(template), {"manager", "senior"})
        self.assertFalse(template.stages.filter(retired_at__isnull=False).exists())

    def test_the_seeding_command_still_publishes_the_full_ladder(self):
        """An operator running it by hand is as deliberate as a school asking."""
        import io

        from django.core.management import call_command

        school, _entity = self._provisioned("elm-confirm", "ELMCF")

        call_command(
            "seed_procurement_approvals", "--tenant", school.slug,
            stdout=io.StringIO(),
        )

        self.assertEqual(
            _live_stage_codes(self._tenant_template(school.tenant)),
            {"manager", "senior"},
        )
        self.assertTrue(WorkflowApproverGroup.all_objects.filter(
            tenant=school.tenant, code=WF_DEFAULT_MANAGER_GROUP).exists())

    def test_seeding_twice_leaves_the_school_ladder_alone(self):
        """Once the steps are real, they are somebody's decision and stay put."""
        from vs_procurement.approvals import ensure_tenant_approval_templates

        school, _entity = self._provisioned("oak-confirm", "OAKCF")
        ensure_tenant_approval_templates(school.tenant, threshold=10)
        template = self._tenant_template(school.tenant)
        senior = template.stages.get(code="senior")

        again = ensure_tenant_approval_templates(school.tenant, threshold=99_000_000)

        self.assertEqual([created for _template, created in again], [False] * 4)
        senior.refresh_from_db()
        # The school's own threshold survives a re-run with a different default.
        self.assertEqual(senior.inclusion_condition["value"], 10)


class _SubmitFixture(_P2PFixtureMixin, TestCase):
    """One entity, one of each approvable document, and a client that can submit."""

    def setUp(self):
        self.entity, _period, self.vendor, _vat, _wht = self.build_p2p()
        self.bursar = get_user_model().objects.create_user(
            tenant=_platform_tenant(), email="bursar@confirm.test",
            status="ACTIVE", first_name="B", last_name="Ursar",
        )

    def api_client(self):
        """A real JWT client for the bursar. Not ``client``, which Django owns."""
        from core.test_utils import TenantAPIClient

        return TenantAPIClient(user=self.bursar)

    def submit(self, path, pk, body=None):
        """POST one of the four submit endpoints with RBAC satisfied."""
        with patch("vs_rbac.permissions.HasRBACPermission.has_permission",
                   return_value=True):
            return self.api_client().post(
                f"/v1/procurement/{path}/{pk}/submit/?entity={self.entity.code}",
                body or {}, format="json",
            )

    # -- the four documents -------------------------------------------------- #

    def a_requisition(self):
        requisition = PurchaseRequisition.objects.create(
            entity=self.entity, request_date=datetime.date(2026, 1, 3),
            requested_by=self.bursar,
        )
        PurchaseRequisitionLine.objects.create(
            requisition=requisition, line_no=1, description="Exercise books",
            quantity=1, estimated_unit_price=100_000,
            expense_account=self.acc(self.entity, "5300"),
        )
        requisition.recompute_total(save=True)
        return requisition

    def a_purchase_order(self):
        from vs_procurement.purchasing import price_po

        order = PurchaseOrder.objects.create(
            entity=self.entity, vendor=self.vendor,
            order_date=datetime.date(2026, 1, 5),
        )
        PurchaseOrderLine.objects.create(
            purchase_order=order, description="Exercise books", line_no=1,
            expense_account=self.acc(self.entity, "5300"),
            quantity=1, unit_price=100_000,
        )
        price_po(order)
        order.refresh_from_db()
        return order

    def a_vendor_invoice(self):
        from vs_procurement.payables import price_vendor_invoice

        bill = VendorInvoice.objects.create(
            entity=self.entity, vendor=self.vendor,
            invoice_date=datetime.date(2026, 1, 10),
            due_date=datetime.date(2026, 1, 20),
        )
        VendorInvoiceLine.objects.create(
            vendor_invoice=bill, line_no=1, quantity=1, unit_price=100_000,
            expense_account=self.acc(self.entity, "5300"),
        )
        price_vendor_invoice(bill)
        bill.refresh_from_db()
        return bill

    def a_vendor_payment(self):
        """A draft settling a posted bill, which is what the submit gate requires."""
        from vs_procurement.payables import post_vendor_invoice

        settled = self.make_bill(
            self.entity, self.vendor, [("5300", 1, 100_000, None, None)])
        post_vendor_invoice(settled)
        payment = VendorPayment.objects.create(
            entity=self.entity, vendor=self.vendor,
            payment_date=datetime.date(2026, 1, 15), gross_amount=100_000,
            net_amount=100_000, payment_account=self.acc(self.entity, "1100"),
        )
        VendorPaymentAllocation.objects.create(
            payment=payment, vendor_invoice=settled, amount=100_000,
        )
        return payment

    def every_document(self):
        """``[(label, endpoint, document), ...]`` covering all four types."""
        return [
            ("requisition", "requisitions", self.a_requisition()),
            ("purchase order", "purchase-orders", self.a_purchase_order()),
            ("vendor invoice", "vendor-invoices", self.a_vendor_invoice()),
            ("vendor payment", "vendor-payments", self.a_vendor_payment()),
        ]


class UnconfiguredSubmitIsRefusedThenConfirmableTests(_SubmitFixture):
    """The school has a route of its own and has put no steps in it yet."""

    def setUp(self):
        super().setUp()
        from vs_procurement.approvals import ensure_tenant_approval_templates

        # Exactly what provisioning leaves behind: a route per type, no steps.
        ensure_tenant_approval_templates(
            self.entity.tenant, with_default_stages=False)

    def test_every_submit_endpoint_refuses_with_a_code_a_client_can_act_on(self):
        """409 and a named code, so the client can offer the confirmation.

        Reading prose out of the message to decide whether to show the dialog is how
        the dialog stops appearing the day the wording changes.
        """
        for label, endpoint, document in self.every_document():
            with self.subTest(document=label):
                response = self.submit(endpoint, document.pk)

                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["error"]["code"], "APPROVAL_NOT_CONFIGURED")

    def test_a_refused_submit_leaves_the_document_exactly_as_it_was(self):
        """A refusal that banked the PENDING flip would strand the document.

        Nobody could approve it, and its owner could not edit or resubmit it either.
        """
        for label, endpoint, document in self.every_document():
            with self.subTest(document=label):
                self.submit(endpoint, document.pk)

                document.refresh_from_db()
                self.assertEqual(
                    document.approval_state, ProcApprovalState.NOT_SUBMITTED)
                self.assertFalse(WorkflowInstance.all_objects.for_document(
                    document).exists())

    def test_confirming_sends_it_and_records_who_said_so(self):
        """The remedy the refusal names, working from the request that names it."""
        for label, endpoint, document in self.every_document():
            with self.subTest(document=label):
                response = self.submit(endpoint, document.pk, {
                    "confirm_without_approval": True,
                    "reason": "No ladder built yet; the head agreed in person.",
                })

                self.assertEqual(response.status_code, 200)
                instance = WorkflowInstance.all_objects.for_document(document).get()
                self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
                document.refresh_from_db()
                self.assertEqual(
                    document.approval_state, ProcApprovalState.APPROVED)

                event = AuditEvent.objects.filter(
                    action_type="POSTED_WITHOUT_APPROVAL",
                    entity_id=str(document.pk),
                    entity_type=type(document).__name__,
                ).latest("event_at")
                self.assertEqual(event.actor_user, self.bursar)
                self.assertIn("the head agreed", event.metadata["reason"])

    def test_the_confirmation_has_to_be_asked_for_by_name(self):
        """An ordinary submit is not a confirmation, however many times it is sent."""
        requisition = self.a_requisition()

        self.submit("requisitions", requisition.pk)
        second = self.submit("requisitions", requisition.pk, {"reason": "Urgent."})

        self.assertEqual(second.status_code, 409)
        self.assertFalse(AuditEvent.objects.filter(
            action_type="POSTED_WITHOUT_APPROVAL").exists())


class ConfirmationNeverBypassesARealLadderTests(_SubmitFixture):
    """The case that matters most: a school that HAS built its steps.

    A confirmation is an answer to "nobody has said what approving this looks like".
    Where somebody has said it, the answer is not wanted and must not be taken. If the
    flag could skip a live ladder, any submitter could approve their own spend by
    adding one field to a request body, and the workflow record would show a properly
    approved instance.
    """

    def setUp(self):
        super().setUp()
        from vs_procurement.approvals import ensure_tenant_approval_templates

        ensure_tenant_approval_templates(self.entity.tenant)

    @staticmethod
    def _appoint(user, group_code, *, tenant):
        """Put ``user`` in the ladder's approver group, the way a school does.

        Membership through a role rather than by name, because that is the ordinary
        way a school fills a group and it is what keeps branch narrowing meaningful.
        """
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_workflow.constants import GroupMemberKind
        from vs_workflow.models import WorkflowApproverGroupMember

        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=tenant, key=group_code,
            defaults={"name": group_code, "status": "ACTIVE", "is_system_role": True},
        )
        TenantUserRoleAssignment.objects.get_or_create(
            tenant=tenant, user=user, role=role,
        )
        group = WorkflowApproverGroup.all_objects.get(tenant=tenant, code=group_code)
        WorkflowApproverGroupMember.objects.get_or_create(
            group=group, kind=GroupMemberKind.ROLE, role=role,
        )
        return role

    def test_confirming_against_an_unstaffed_ladder_parks_instead_of_approving(self):
        """A ladder with nobody in its group is still a ladder.

        The school has said what approving looks like and has not yet said who does
        it. That is a document waiting for a person, not a document nobody will ever
        review, so it parks and the confirmation is ignored.
        """
        for label, endpoint, document in self.every_document():
            with self.subTest(document=label):
                response = self.submit(endpoint, document.pk, {
                    "confirm_without_approval": True,
                    "reason": "Trying to push this through.",
                })

                self.assertEqual(response.status_code, 200)
                instance = WorkflowInstance.all_objects.for_document(document).get()
                self.assertNotEqual(
                    instance.status, WorkflowInstanceStatus.APPROVED)
                document.refresh_from_db()
                self.assertEqual(
                    document.approval_state, ProcApprovalState.PENDING)

        # Nothing went out unreviewed, so nothing was recorded as having done so.
        self.assertFalse(AuditEvent.objects.filter(
            action_type="POSTED_WITHOUT_APPROVAL").exists())

    def test_confirming_against_a_staffed_ladder_still_waits_for_the_approver(self):
        """The approver must still decide, and the document must still wait."""
        approver = get_user_model().objects.create_user(
            tenant=self.entity.tenant, email="approver@confirm.test",
            status="ACTIVE", first_name="A", last_name="Pprover",
        )
        self._appoint(approver, WF_DEFAULT_MANAGER_GROUP, tenant=self.entity.tenant)
        requisition = self.a_requisition()

        self.submit("requisitions", requisition.pk, {
            "confirm_without_approval": True, "reason": "In a hurry.",
        })

        instance = WorkflowInstance.all_objects.for_document(requisition).get()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        requisition.refresh_from_db()
        self.assertEqual(requisition.approval_state, ProcApprovalState.PENDING)
        self.assertNotEqual(requisition.status, DocumentStatus.APPROVED)
        self.assertFalse(AuditEvent.objects.filter(
            action_type="POSTED_WITHOUT_APPROVAL").exists())

    def test_a_ladder_whose_every_step_is_retired_is_confirmable_again(self):
        """Retired steps are history, not configuration.

        A school that retires its last live step is back where it started: nothing
        will run, so the submit is refused and the confirmation is the way through.
        Counting the retired rows as a ladder would let the router skip each in turn
        and terminate the instance approved with nobody looking.
        """
        from django.utils import timezone

        template = WorkflowTemplate.all_objects.get(
            tenant=self.entity.tenant, branch=None,
            code=WF_DEFAULT_TEMPLATE_CODE, document_type=WF_DOCTYPE_REQUISITION,
        )
        template.stages.update(retired_at=timezone.now())
        requisition = self.a_requisition()

        refused = self.submit("requisitions", requisition.pk)
        self.assertEqual(refused.status_code, 409)

        confirmed = self.submit("requisitions", requisition.pk, {
            "confirm_without_approval": True, "reason": "The steps were withdrawn.",
        })

        self.assertEqual(confirmed.status_code, 200)
        instance = WorkflowInstance.all_objects.for_document(requisition).get()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
