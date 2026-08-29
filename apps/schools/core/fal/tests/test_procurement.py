"""Component 7: school procurement through the FAL.

The rule under test throughout is **park, don't skip**. A school that has rules
but nobody in the approving role must hold its spend, not wave it through, and
the only way past that is an audited override nobody holds by default.
"""

from __future__ import annotations

import datetime

from schools.core.fal.adapters.django_finance import (
    DjangoProcurementActionAdapter,
    DjangoProcurementReadAdapter,
)
from schools.core.fal.contracts import (
    BillLine,
    ProcApprovalState,
    ProcDocRef,
    ProcDocType,
)
from schools.core.fal.exceptions import (
    ApprovalNotParkedError,
    ApprovalTemplateMissingError,
    CrossBranchError,
    CrossTenantError,
    OverrideNotPermittedError,
    ProcurementStateError,
)

from .base import FALFixture

LINES = (BillLine(description="Exercise books", quantity=100, unit_price=25_000),)


class _ProcFixture(FALFixture):
    def setUp(self):
        super().setUp()
        self.port = DjangoProcurementActionAdapter()

    def raise_one(self, *, books=None, raiser=None, branch_ref=None, lines=LINES):
        books = books or self.corona_books
        return self.port.raise_requisition(
            entity_ref=books.entity_ref, raiser_ref=(raiser or self.bursar).pk,
            lines=lines, branch_ref=branch_ref, narration="Termly stationery",
        ).unwrap()

    def staff_the_approver_role(self, school, user, *, branch=None):
        """Appoint somebody to the role the seeded ladder routes to."""
        from vs_procurement.constants import WF_DEFAULT_MANAGER_ROLE
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import make_assignment

        role = TenantRoleTemplate.objects.get(
            tenant=school.tenant, key=WF_DEFAULT_MANAGER_ROLE,
        )
        make_assignment(school.tenant, user, role, branch=branch)
        return role


class RaiseRequisitionTests(_ProcFixture):
    def test_a_school_level_raiser_writes_no_branch(self):
        """An empty branch is a head-office purchase, not a validation failure."""
        document = self.raise_one()

        self.assertIsNone(document.ref.branch_ref)
        self.assertIs(document.ref.doc_type, ProcDocType.REQUISITION)
        self.assertEqual(document.total, 2_500_000)

    def test_a_branch_bound_raiser_writes_their_own_branch(self):
        document = self.raise_one(raiser=self.lekki_bursar)

        self.assertEqual(document.ref.branch_ref, self.lekki.pk)

    def test_a_branch_bound_raiser_cannot_buy_for_another_branch(self):
        with self.assertRaises(CrossBranchError):
            self.raise_one(raiser=self.lekki_bursar, branch_ref=self.ikeja.pk)

    def test_a_school_level_raiser_may_name_a_branch(self):
        document = self.raise_one(branch_ref=self.lekki.pk)

        self.assertEqual(document.ref.branch_ref, self.lekki.pk)

    def test_a_branch_from_another_school_is_refused(self):
        with self.assertRaises(CrossTenantError):
            self.raise_one(branch_ref=self.greenfield_main.pk)

    def test_a_raiser_from_another_school_is_refused(self):
        with self.assertRaises(CrossTenantError):
            self.raise_one(raiser=self.greenfield_bursar)

    def test_a_requisition_with_no_lines_is_refused(self):
        with self.assertRaises(ProcurementStateError):
            self.raise_one(lines=())

    def test_a_single_branch_school_works_the_same_way(self):
        """Greenfield has one branch, which is the common shape in production."""
        document = self.raise_one(books=self.greenfield_books,
                                  raiser=self.greenfield_bursar)

        self.assertIsNone(document.ref.branch_ref)
        self.assertEqual(document.total, 2_500_000)


class SubmissionParksTests(_ProcFixture):
    """The heart of decision 2."""

    def test_a_school_with_rules_but_no_approver_parks(self):
        """Corona's books arrive with a ladder and nobody appointed to it.

        The first requisition must sit and say so. The failure this guards
        against is the opposite: a stage with no approver skipping straight to
        APPROVED, so a school's spend approves itself on day one.
        """
        document = self.raise_one()

        submission = self.port.submit_for_approval(
            document.ref, actor_ref=self.bursar.pk,
        ).unwrap()

        self.assertIs(submission.approval_state, ProcApprovalState.PENDING)
        self.assertTrue(submission.is_parked)
        self.assertEqual(submission.parked_stage_code, "manager")
        self.assertIsNot(submission.approval_state, ProcApprovalState.APPROVED)

    def test_parking_is_a_success_not_an_error(self):
        document = self.raise_one()

        result = self.port.submit_for_approval(document.ref, actor_ref=self.bursar.pk)

        self.assertTrue(result.is_available)

    def test_appointing_an_approver_releases_it_with_no_resubmission(self):
        from vs_procurement import approval_parking
        from vs_procurement.models import PurchaseRequisition

        document = self.raise_one()
        self.port.submit_for_approval(document.ref, actor_ref=self.bursar.pk)
        requisition = PurchaseRequisition.objects.get(pk=document.ref.doc_ref)
        self.assertTrue(approval_parking.is_document_parked(requisition))

        approver = self.user_for(self.corona, "head@corona.test")
        self.staff_the_approver_role(self.corona, approver)

        requisition.refresh_from_db()
        self.assertFalse(approval_parking.is_document_parked(requisition))
        self.assertEqual(requisition.approval_state, ProcApprovalState.PENDING.value)

    def test_a_school_with_no_rules_at_all_is_refused_and_nothing_is_persisted(self):
        from vs_procurement.models import PurchaseRequisition
        from vs_workflow.models import WorkflowInstance, WorkflowTemplate

        WorkflowTemplate.all_objects.all().delete()
        document = self.raise_one()

        with self.assertRaises(ApprovalTemplateMissingError):
            self.port.submit_for_approval(document.ref, actor_ref=self.bursar.pk)

        requisition = PurchaseRequisition.objects.get(pk=document.ref.doc_ref)
        self.assertEqual(
            requisition.approval_state, ProcApprovalState.NOT_SUBMITTED.value,
        )
        self.assertFalse(WorkflowInstance.all_objects.exists())

    def test_resubmitting_does_not_start_a_second_workflow(self):
        document = self.raise_one()
        self.port.submit_for_approval(document.ref, actor_ref=self.bursar.pk)

        with self.assertRaises(ProcurementStateError):
            self.port.submit_for_approval(document.ref, actor_ref=self.bursar.pk)

    def test_another_schools_document_cannot_be_submitted(self):
        document = self.raise_one()
        foreign = ProcDocRef(
            doc_ref=document.ref.doc_ref, doc_type=ProcDocType.REQUISITION,
            entity_ref=self.greenfield_books.entity_ref,
        )

        with self.assertRaises(CrossTenantError):
            self.port.submit_for_approval(foreign, actor_ref=self.greenfield_bursar.pk)

    def test_a_document_in_another_branch_is_refused(self):
        document = self.raise_one(raiser=self.lekki_bursar)
        wrong_branch = ProcDocRef(
            doc_ref=document.ref.doc_ref, doc_type=ProcDocType.REQUISITION,
            entity_ref=self.corona_books.entity_ref, branch_ref=self.ikeja.pk,
        )

        with self.assertRaises(CrossBranchError):
            self.port.submit_for_approval(wrong_branch, actor_ref=self.bursar.pk)


class OverrideTests(_ProcFixture):
    """Decision 3: the escape hatch, and everything it refuses."""

    def setUp(self):
        super().setUp()
        self.document = self.raise_one()
        self.port.submit_for_approval(self.document.ref, actor_ref=self.bursar.pk)
        self.head = self.user_for(self.corona, "head@corona.test")

    def grant_override(self, user):
        from vs_procurement.constants import WF_APPROVAL_OVERRIDE_PERMISSION

        return self.grant(user, WF_APPROVAL_OVERRIDE_PERMISSION)

    def test_a_user_without_the_key_cannot_override(self):
        with self.assertRaises(OverrideNotPermittedError):
            self.port.approve_without_review(
                self.document.ref, actor_ref=self.head.pk, reason="Term starts Monday.",
            )

    def test_a_blank_reason_is_refused(self):
        self.grant_override(self.head)

        with self.assertRaises(OverrideNotPermittedError):
            self.port.approve_without_review(
                self.document.ref, actor_ref=self.head.pk, reason="   ",
            )

    def test_a_holder_can_release_a_parked_document_and_it_is_recorded(self):
        from vs_procurement.models import ApprovalOverride

        self.grant_override(self.head)

        submission = self.port.approve_without_review(
            self.document.ref, actor_ref=self.head.pk,
            reason="Term starts Monday and nobody holds the approver role yet.",
        ).unwrap()

        self.assertFalse(submission.is_parked)
        self.assertIsNotNone(submission.override)
        self.assertEqual(submission.override.actor_ref, self.head.pk)
        self.assertEqual(submission.override.amount, 2_500_000)
        self.assertEqual(ApprovalOverride.objects.count(), 1)

    def test_the_override_record_cannot_be_edited_afterwards(self):
        from vs_procurement.models import ApprovalOverride

        self.grant_override(self.head)
        self.port.approve_without_review(
            self.document.ref, actor_ref=self.head.pk, reason="Urgent.",
        )

        row = ApprovalOverride.objects.get()
        row.reason = "Something more flattering."
        with self.assertRaises(Exception):
            row.save()

    def test_a_document_that_is_not_parked_cannot_be_overridden(self):
        """The override releases stuck work; it does not skip live approvers."""
        approver = self.user_for(self.corona, "approver@corona.test")
        self.staff_the_approver_role(self.corona, approver)
        self.grant_override(self.head)

        with self.assertRaises(ApprovalNotParkedError):
            self.port.approve_without_review(
                self.document.ref, actor_ref=self.head.pk, reason="Faster this way.",
            )


class ApproveDeclineTests(_ProcFixture):
    def setUp(self):
        super().setUp()
        self.approver = self.user_for(self.corona, "approver@corona.test")
        self.staff_the_approver_role(self.corona, self.approver)
        self.document = self.raise_one()
        self.submission = self.port.submit_for_approval(
            self.document.ref, actor_ref=self.bursar.pk,
        ).unwrap()

    def test_a_staffed_ladder_does_not_park(self):
        self.assertFalse(self.submission.is_parked)

    def test_an_approver_can_approve(self):
        decision = self.port.approve(
            self.document.ref, approver_ref=self.approver.pk, comment="Fine.",
        ).unwrap()

        self.assertIs(decision.approval_state, ProcApprovalState.APPROVED)
        self.assertEqual(decision.decided_by_ref, self.approver.pk)

    def test_an_approver_can_decline(self):
        decision = self.port.decline(
            self.document.ref, approver_ref=self.approver.pk, comment="Too much.",
        ).unwrap()

        self.assertIs(decision.approval_state, ProcApprovalState.REJECTED)

    def test_a_requester_cannot_approve_their_own_spend(self):
        with self.assertRaises(ProcurementStateError):
            self.port.approve(self.document.ref, approver_ref=self.bursar.pk)

    def test_a_document_with_nothing_in_flight_cannot_be_decided(self):
        fresh = self.raise_one()

        with self.assertRaises(ProcurementStateError):
            self.port.approve(fresh.ref, approver_ref=self.approver.pk)


class SeedingTests(_ProcFixture):
    def test_seeding_is_idempotent(self):
        """Corona's books already seeded a ladder when they were provisioned."""
        again = self.port.seed_approval_rules(
            entity_ref=self.corona_books.entity_ref, threshold=50_000_000,
        ).unwrap()

        self.assertFalse(again)

    def test_seeding_never_restores_defaults_over_a_customised_ladder(self):
        from vs_workflow.models import WorkflowTemplate

        before = set(
            WorkflowTemplate.all_objects
            .filter(tenant=self.corona.tenant)
            .values_list("pk", flat=True)
        )

        self.port.seed_approval_rules(
            entity_ref=self.corona_books.entity_ref, threshold=10,
        )

        after = set(
            WorkflowTemplate.all_objects
            .filter(tenant=self.corona.tenant)
            .values_list("pk", flat=True)
        )
        self.assertEqual(before, after)

    def test_seeding_an_unknown_entity_is_refused(self):
        from schools.core.fal.exceptions import EntityNotProvisioned

        with self.assertRaises(EntityNotProvisioned):
            self.port.seed_approval_rules(entity_ref=9_999_999, threshold=10)

    def test_a_fresh_school_starts_blocked_rather_than_open(self):
        """Seeded rules, no approver: the first document parks. That is the design."""
        document = self.raise_one(books=self.greenfield_books,
                                  raiser=self.greenfield_bursar)

        submission = self.port.submit_for_approval(
            document.ref, actor_ref=self.greenfield_bursar.pk,
        ).unwrap()

        self.assertTrue(submission.is_parked)


class ProcurementReadTests(_ProcFixture):
    def setUp(self):
        super().setUp()
        self.reader = DjangoProcurementReadAdapter()

    def test_rows_are_scoped_to_the_school(self):
        self.raise_one()
        self.raise_one(books=self.greenfield_books, raiser=self.greenfield_bursar)

        page = self.reader.rows(self.corona.pk).unwrap()

        self.assertEqual(page.total_items, 1)

    def test_rows_narrow_by_branch(self):
        self.raise_one(raiser=self.lekki_bursar)
        self.raise_one()

        page = self.reader.rows(self.corona.pk, branch_ref=self.lekki.pk).unwrap()

        self.assertEqual(page.total_items, 1)
        self.assertEqual(page.items[0].branch_ref, self.lekki.pk)

    def test_the_snapshot_counts_what_is_waiting(self):
        document = self.raise_one()
        self.port.submit_for_approval(document.ref, actor_ref=self.bursar.pk)

        snapshot = self.reader.snapshot(self.corona.pk).unwrap()

        self.assertEqual(snapshot.pending_approvals, 1)
        self.assertEqual(snapshot.open_requests, 1)

    def test_an_empty_school_reads_as_zero_and_not_unavailable(self):
        result = self.reader.snapshot(self.greenfield.pk)

        self.assertTrue(result.is_available)
        self.assertEqual(result.value.open_requests, 0)
        self.assertIsNotNone(datetime.date.today())
