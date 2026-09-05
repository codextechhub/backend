"""Leave: applied for, decided by the workflow engine, never approved here.

The point of every test in this file is the separation. This module files an
absence and reads it back; the engine decides it. So the status column must be
unreachable from any request body, an approval must arrive through the engine's
own callback, and a request that has been decided must refuse an edit.

FRD M12 v2.1, FR-013.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_staff.approvals import ensure_tenant_approval_templates
from schools.vs_staff.constants import LEAVE_APPROVER_GROUP_CODE, LeaveStatus
from schools.vs_staff.models import LeaveRequest
from schools.vs_staff.services import leave as leave_service
from vs_audit.models import AuditActionType, AuditEvent
from vs_workflow.models import WorkflowInstance

from .base import StaffFixture


class LeaveFixture(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # This school's own ladder. There is no platform fallback for leave, so
        # publishing it here is not belt-and-braces: without it every test below
        # would be testing TEMPLATE_NOT_FOUND rather than the feature.
        cls.leave_template, _ = ensure_tenant_approval_templates(cls.tenant)

        # Somebody has to be IN the group, because provisioning creates it
        # empty. Mrs. Adeyemi is nominated by name at Lekki, which is where Mr.
        # Eze is posted, so she is the one the BRANCH-scoped stage resolves.
        from vs_workflow.models import WorkflowApproverGroup, WorkflowApproverGroupMember

        cls.leave_group = WorkflowApproverGroup.all_objects.get(
            tenant=cls.tenant, code=LEAVE_APPROVER_GROUP_CODE,
        )
        WorkflowApproverGroupMember.objects.create(
            group=cls.leave_group, kind="USER", user=cls.lekki_head,
        )

    def body(self, **overrides):
        payload = {
            "leave_type": "ANNUAL",
            "start_date": "2025-12-22",
            "end_date": "2026-01-02",
        }
        payload.update(overrides)
        return payload


class FilingTests(LeaveFixture):
    def test_filing_creates_a_pending_request_and_a_workflow_instance(self):
        response = self.post(
            self.admin, "staff-leave", self.body(), pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        row = LeaveRequest.all_objects.get(staff=self.eze)
        self.assertEqual(row.status, LeaveStatus.PENDING)
        self.assertTrue(
            WorkflowInstance.all_objects.filter(
                document_type="schools.leave_request",
                document_object_id=str(row.pk),
            ).exists(),
            "an absence is never both filed and allowed by one act",
        )

    def test_days_defaults_to_the_inclusive_calendar_span(self):
        self.post(self.admin, "staff-leave", self.body(), pk=self.eze.pk)
        row = LeaveRequest.all_objects.get(staff=self.eze)
        self.assertEqual(row.days, 12)

    def test_a_school_may_correct_the_day_count(self):
        """Working days are not calendar days and nothing records a teaching week."""
        self.post(
            self.admin, "staff-leave", self.body(days=8), pk=self.eze.pk,
        )
        self.assertEqual(LeaveRequest.all_objects.get(staff=self.eze).days, 8)

    def test_an_end_before_a_start_is_refused(self):
        response = self.post(
            self.admin, "staff-leave",
            self.body(start_date="2026-01-02", end_date="2025-12-22"),
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "INVALID_DATE_RANGE")

    def test_overlapping_leave_warns_and_writes_the_record(self):
        """A sick day inside booked annual leave is a correction, not a mistake."""
        self.post(self.admin, "staff-leave", self.body(), pk=self.eze.pk)
        response = self.post(
            self.admin, "staff-leave",
            self.body(
                leave_type="SICK", start_date="2025-12-24",
                end_date="2025-12-26",
            ),
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data["data"]["warnings"][0]["code"], "LEAVE_OVERLAP",
        )
        self.assertEqual(LeaveRequest.all_objects.filter(staff=self.eze).count(), 2)

    def test_a_cancelled_request_does_not_cause_an_overlap_warning(self):
        """A cancelled absence is not an absence."""
        self.post(self.admin, "staff-leave", self.body(), pk=self.eze.pk)
        row = LeaveRequest.all_objects.get(staff=self.eze)
        self.delete(self.admin, "staff-leave-detail", pk=row.pk)
        response = self.post(
            self.admin, "staff-leave",
            self.body(leave_type="SICK", start_date="2025-12-24",
                      end_date="2025-12-26"),
            pk=self.eze.pk,
        )
        self.assertEqual(response.data["data"]["warnings"], [])

    def test_filing_writes_an_audit_event(self):
        self.post(self.admin, "staff-leave", self.body(), pk=self.eze.pk)
        self.assertTrue(
            AuditEvent.objects.filter(
                action_type=AuditActionType.STAFF_LEAVE_RECORDED,
            ).exists(),
        )

    def test_the_person_who_filed_it_cannot_approve_it(self):
        """The engine's own rule, and this module must not route around it.

        An administrator filing their own leave and then approving it would be
        the whole separation undone in two clicks, which is exactly why leave
        runs on the engine rather than on a status column here.
        """
        from vs_workflow.exceptions import RequesterCannotApproveError
        from vs_workflow.services import actions

        self.post(self.admin, "staff-leave", self.body(), pk=self.eze.pk)
        row = LeaveRequest.all_objects.get(staff=self.eze)
        instance = WorkflowInstance.all_objects.get(
            document_type="schools.leave_request",
            document_object_id=str(row.pk),
        )
        with self.assertRaises(RequesterCannotApproveError):
            actions.record_action(instance.id, self.admin, "APPROVED", "")

    def test_a_status_in_the_request_body_is_ignored(self):
        """The one rule the whole design of this depends on.

        A status a form can set is a status that disagrees with the instance
        that decided it: an administrator could mark their own leave approved
        without anybody voting, and the profile would show an approval the trail
        has no record of.
        """
        self.post(
            self.admin, "staff-leave",
            {**self.body(), "status": "APPROVED"}, pk=self.eze.pk,
        )
        self.assertEqual(
            LeaveRequest.all_objects.get(staff=self.eze).status,
            LeaveStatus.PENDING,
        )


class DecisionTests(LeaveFixture):
    def _file(self):
        self.post(self.admin, "staff-leave", self.body(), pk=self.eze.pk)
        return LeaveRequest.all_objects.get(staff=self.eze)

    def _instance(self, row):
        return WorkflowInstance.all_objects.get(
            document_type="schools.leave_request",
            document_object_id=str(row.pk),
        )

    def test_approving_through_the_engine_moves_the_status(self):
        from vs_workflow.services import actions

        row = self._file()
        actions.record_action(
            self._instance(row).id, self.lekki_head, "APPROVED", "Fine.",
        )
        row.refresh_from_db()
        self.assertEqual(row.status, LeaveStatus.APPROVED)
        self.assertIsNotNone(row.decided_at)

    def test_rejecting_through_the_engine_moves_the_status(self):
        from vs_workflow.services import actions

        row = self._file()
        actions.record_action(
            self._instance(row).id, self.lekki_head, "REJECTED", "Too close to exams.",
        )
        row.refresh_from_db()
        self.assertEqual(row.status, LeaveStatus.REJECTED)

    def test_a_decision_writes_its_own_audit_event(self):
        """Separate from the filing: who asked and who allowed it are two questions."""
        from vs_workflow.services import actions

        row = self._file()
        actions.record_action(self._instance(row).id, self.lekki_head, "APPROVED", "")
        self.assertTrue(
            AuditEvent.objects.filter(
                action_type=AuditActionType.STAFF_LEAVE_DECIDED,
            ).exists(),
        )

    def test_a_decided_request_refuses_an_edit(self):
        from vs_workflow.services import actions

        row = self._file()
        actions.record_action(self._instance(row).id, self.lekki_head, "APPROVED", "")
        response = self.patch(
            self.admin, "staff-leave-detail", {"days": 4}, pk=row.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["code"], "LEAVE_ALREADY_DECIDED",
        )

    def test_a_pending_request_accepts_a_correction(self):
        row = self._file()
        response = self.patch(
            self.admin, "staff-leave-detail", {"days": 8}, pk=row.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        row.refresh_from_db()
        self.assertEqual(row.days, 8)

    def test_cancelling_never_deletes(self):
        """Leave taken is part of the employment history."""
        row = self._file()
        self.delete(self.admin, "staff-leave-detail", pk=row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, LeaveStatus.CANCELLED)
        self.assertTrue(LeaveRequest.all_objects.filter(pk=row.pk).exists())

    def test_an_approval_arriving_after_a_cancellation_does_not_reinstate_it(self):
        """The person withdrew it, and an approval later does not put them back on leave."""
        from vs_workflow.services import actions

        row = self._file()
        instance = self._instance(row)
        self.delete(self.admin, "staff-leave-detail", pk=row.pk)
        try:
            actions.record_action(instance.id, self.lekki_head, "APPROVED", "")
        except Exception:
            pass
        row.refresh_from_db()
        self.assertEqual(row.status, LeaveStatus.CANCELLED)


class ReportingTests(LeaveFixture):
    def test_days_taken_counts_approved_requests_only(self):
        from vs_workflow.services import actions

        self.post(self.admin, "staff-leave", self.body(days=5), pk=self.eze.pk)
        approved = LeaveRequest.all_objects.get(staff=self.eze)
        actions.record_action(
            WorkflowInstance.all_objects.get(
                document_object_id=str(approved.pk),
                document_type="schools.leave_request",
            ).id,
            self.lekki_head, "APPROVED", "",
        )
        # A second, still pending, which must not be counted.
        self.post(
            self.admin, "staff-leave",
            self.body(leave_type="SICK", start_date="2026-03-02",
                      end_date="2026-03-04", days=3),
            pk=self.eze.pk,
        )
        taken = leave_service.days_taken(self.eze)
        self.assertEqual(taken, [{"leave_type": "ANNUAL", "days": 5}])

    def test_the_payload_says_there_is_no_balance(self):
        response = self.get(self.admin, "staff-leave", pk=self.eze.pk)
        self.assertIn("no balance", response.data["data"]["balance_note"])

    def test_completed_is_derived_and_never_stored(self):
        """An approved absence whose end date has passed reads Completed.

        Derived at read time rather than stored beside the date it follows from,
        which is a second thing that can be wrong.
        """
        row = LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze, leave_type="ANNUAL",
            start_date=dt.date(2020, 1, 1), end_date=dt.date(2020, 1, 5),
            days=5, status=LeaveStatus.APPROVED,
        )
        self.assertEqual(row.display_status(), "COMPLETED")
        self.assertEqual(row.status, LeaveStatus.APPROVED)

    def test_a_pending_request_whose_dates_passed_is_not_completed(self):
        row = LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze, leave_type="ANNUAL",
            start_date=dt.date(2020, 1, 1), end_date=dt.date(2020, 1, 5),
            days=5, status=LeaveStatus.PENDING,
        )
        self.assertEqual(row.display_status(), LeaveStatus.PENDING)

    def test_leave_running_today_does_not_change_the_employment_status(self):
        """Two separate facts, and the directory reports the disagreement.

        A school may set either, both or neither, so nothing here resolves it.
        """
        today = dt.date.today()
        LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze, leave_type="SICK",
            start_date=today - dt.timedelta(days=1),
            end_date=today + dt.timedelta(days=1),
            days=3, status=LeaveStatus.APPROVED,
        )
        response = self.get(self.admin, "staff-list")
        row = next(
            item for item in response.data["data"]
            if item["full_name"] == "Chukwuemeka Eze"
        )
        self.assertTrue(row["on_leave_today"])
        self.assertEqual(row["employment_status"], "ACTIVE")


class NobodyToApproveTests(StaffFixture):
    """A school with no ladder at all is refused rather than granted.

    There is no platform fallback for leave, so this is the state of any school
    that was never provisioned, and ``leave_template_gaps`` is what finds them.
    """

    def test_filing_with_no_template_is_refused_rather_than_auto_approved(self):
        response = self.post(
            self.admin, "staff-leave",
            {
                "leave_type": "ANNUAL",
                "start_date": "2025-12-22",
                "end_date": "2026-01-02",
            },
            pk=self.eze.pk,
        )
        self.assertNotEqual(response.status_code, 201, response.data)
        self.assertFalse(
            LeaveRequest.all_objects.filter(
                staff=self.eze, status=LeaveStatus.APPROVED,
            ).exists(),
            "leave must never approve itself",
        )
