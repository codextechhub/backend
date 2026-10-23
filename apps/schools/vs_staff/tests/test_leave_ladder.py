"""The default leave ladder: the person's line manager, ensured wherever leave is filed.

Brightfield runs Lekki and Ikeja with a chart drawn: Mr Eze teaches at Lekki
under Mrs Okafor, who heads the Lekki sciences department. Mr Sule teaches at
Ikeja and has no post on the chart, so nobody is his line manager.

The tests say, in order: a school with no ladder is repaired by its first filing
and the request reaches the right manager; leave recorded by an administrator
climbs from the person it is for; a person nobody manages parks the request and
the filing says so; a ladder a school already holds is left alone; and the
operator's command and the gap reports agree with all of it.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command

from schools.vs_staff import approvals
from schools.vs_staff.constants import LeaveStatus
from schools.vs_staff.models import LeaveRequest, StaffPosition, StaffProfile
from schools.vs_staff.services.organogram import StaffOrganogramService
from vs_workflow.models import (
    WorkflowApproverGroup,
    WorkflowInstance,
    WorkflowStageApprover,
    WorkflowTemplate,
)

from .test_leave import publish_group_leave_ladder
from .test_organogram import OrganogramFixture

LEAVE_TYPE = "schools.leave_request"


class LeaveLadderFixture(OrganogramFixture):
    """Mr Eze reports to Mrs Okafor; Mr Sule reports to nobody."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.lekki_teacher_post = StaffPosition.all_objects.create(
            tenant=cls.tenant, title="Maths Teacher, Lekki", code="LMTH",
            org_node=cls.lekki_sci, reports_to=cls.lekki_hod, headcount=3,
        )
        cls.okafor = cls.make_staff(
            "okafor@brightfield.test", "Ngozi", "Okafor", branch=cls.lekki,
        )
        StaffOrganogramService.assign_position(cls.okafor, cls.lekki_hod)
        StaffOrganogramService.assign_position(cls.eze, cls.lekki_teacher_post)
        cls.stand_as_staff(cls.okafor)

    def file(self, user, staff, **body):
        payload = {
            "leave_type": "ANNUAL", "start_date": "2025-12-22",
            "end_date": "2025-12-24",
        }
        payload.update(body)
        return self.post(user, "staff-leave", payload, pk=staff.pk)

    def instance_for(self, response):
        return WorkflowInstance.all_objects.get(
            document_type=LEAVE_TYPE,
            document_object_id=str(response.data["data"]["leave"]["id"]),
        )

    @staticmethod
    def approver_ids(instance):
        return set(WorkflowStageApprover.objects.filter(
            stage_instance__instance=instance,
        ).values_list("user_id", flat=True))

    @staticmethod
    def warning_codes(response):
        return [w["code"] for w in response.data["data"]["warnings"]]


class FirstFilingRepairsTheSchoolTests(LeaveLadderFixture):
    def test_a_school_with_no_ladder_files_leave_and_it_reaches_the_line_manager(self):
        self.assertFalse(
            WorkflowTemplate.all_objects.filter(
                tenant=self.tenant, document_type=LEAVE_TYPE,
            ).exists(),
        )
        response = self.file(self.admin, self.eze)

        self.assertEqual(response.status_code, 201, response.data)
        template = WorkflowTemplate.all_objects.get(
            tenant=self.tenant, document_type=LEAVE_TYPE,
        )
        [stage] = template.stages.filter(retired_at__isnull=True)
        self.assertEqual(
            (stage.approver_source, stage.organogram_target, stage.approver_scope,
             stage.advance_rule, stage.on_rejection, stage.skip_if_no_approvers),
            ("ORGANOGRAM", "DIRECT_MANAGER", "SCHOOL", "ANY", "TERMINAL", False),
        )
        self.assertFalse(
            WorkflowApproverGroup.all_objects.filter(tenant=self.tenant).exists(),
            "the default ladder creates no approver group",
        )
        self.assertEqual(self.approver_ids(self.instance_for(response)), {self.okafor.user_id})
        self.assertNotIn("NO_APPROVER", self.warning_codes(response))

    def test_the_manager_can_decide_it(self):
        from vs_workflow.services import actions

        response = self.file(self.admin, self.eze)
        actions.record_action(self.instance_for(response).id, self.okafor.user, "APPROVED", "Fine.")
        row = LeaveRequest.all_objects.get(pk=response.data["data"]["leave"]["id"])
        self.assertEqual(row.status, LeaveStatus.APPROVED)

    def test_filing_again_does_not_publish_a_second_ladder(self):
        self.file(self.admin, self.eze)
        self.file(self.admin, self.eze, start_date="2026-01-12", end_date="2026-01-13")
        self.assertEqual(
            WorkflowTemplate.all_objects.filter(
                tenant=self.tenant, document_type=LEAVE_TYPE,
            ).count(),
            1,
        )

    def test_another_schools_filing_is_ensured_in_its_own_tenant(self):
        response = self.post(
            self.solo_admin, "staff-leave",
            {"leave_type": "ANNUAL", "start_date": "2025-12-22", "end_date": "2025-12-24"},
            pk=self.solo_staff.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            WorkflowTemplate.all_objects.filter(
                tenant=self.solo.tenant, document_type=LEAVE_TYPE,
            ).exists(),
        )
        self.assertFalse(
            WorkflowTemplate.all_objects.filter(
                tenant=self.tenant, document_type=LEAVE_TYPE,
            ).exists(),
        )


class ClimbsFromThePersonTheLeaveIsForTests(LeaveLadderFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # The administrator has a chart position of their own, under somebody
        # else, so a climb from the wrong person lands on a different manager.
        cls.ikeja_boss = cls.make_staff(
            "boss@ikeja.test", "Tayo", "Balogun", branch=cls.ikeja,
        )
        StaffOrganogramService.assign_position(cls.ikeja_boss, cls.ikeja_hod)
        cls.stand_as_staff(cls.ikeja_boss)
        cls.admin_staff = StaffProfile.all_objects.create(
            tenant=cls.tenant, user=cls.admin, branch=cls.ikeja,
            job_title="Administrator", employment_status="ACTIVE",
        )
        StaffOrganogramService.assign_position(cls.admin_staff, cls.ikeja_teacher_post)

    def test_leave_recorded_by_an_administrator_goes_to_the_teachers_manager(self):
        response = self.file(self.admin, self.eze)

        self.assertEqual(response.status_code, 201, response.data)
        instance = self.instance_for(response)
        self.assertEqual(instance.requested_by_id, self.admin.pk)
        self.assertEqual(instance.request_for_id, self.eze.user_id)
        self.assertEqual(self.approver_ids(instance), {self.okafor.user_id})
        self.assertNotIn(self.ikeja_boss.user_id, self.approver_ids(instance))

    def test_the_administrators_own_leave_goes_to_the_administrators_manager(self):
        response = self.file(self.admin, self.admin_staff)
        self.assertEqual(
            self.approver_ids(self.instance_for(response)), {self.ikeja_boss.user_id},
        )

    def test_the_person_taking_leave_cannot_approve_it_even_in_their_managers_seat(self):
        """Mr Eze also holds his manager's post, so the climb finds him and drops him."""
        StaffOrganogramService.close_for_exit(self.okafor)
        StaffOrganogramService.assign_position(
            self.eze, self.lekki_hod, is_primary=False,
        )
        response = self.file(self.eze.user, self.eze)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.approver_ids(self.instance_for(response)), set())
        self.assertIn("NO_APPROVER", self.warning_codes(response))

    def test_the_person_taking_leave_cannot_approve_when_an_administrator_filed_it(self):
        """The conflict is the person the leave is for, not only whoever filed it."""
        from vs_workflow.models import WorkflowStageInstance
        from vs_workflow.services.approvers import approval_conflict_ids

        response = self.file(self.admin, self.eze)
        instance = self.instance_for(response)
        self.assertIn(self.eze.user_id, approval_conflict_ids(instance))
        self.assertTrue(
            WorkflowStageInstance.objects.filter(instance=instance, status="ACTIVE").exists(),
        )


class NobodyCanApproveTests(LeaveLadderFixture):
    def test_a_person_with_no_line_manager_is_filed_parked_and_warned(self):
        response = self.file(self.admin, self.ikeja_teacher)

        self.assertEqual(response.status_code, 201, response.data)
        [warning] = [
            w for w in response.data["data"]["warnings"] if w["code"] == "NO_APPROVER"
        ]
        self.assertEqual(
            warning["message"],
            "Nobody can approve this yet. To fix that, give Ibrahim Sule a manager on "
            "the organogram (in Staff, Organogram), or ask an administrator to choose "
            "an approver for this request. It is filed and waits until then.",
        )
        row = LeaveRequest.all_objects.get(pk=response.data["data"]["leave"]["id"])
        self.assertEqual(row.status, LeaveStatus.PENDING)
        instance = self.instance_for(response)
        self.assertEqual(instance.status, "IN_PROGRESS")
        self.assertEqual(self.approver_ids(instance), set())

    def test_a_person_with_a_line_manager_gets_no_warning(self):
        response = self.file(self.admin, self.eze)
        self.assertEqual(self.warning_codes(response), [])

    def test_giving_the_person_a_manager_later_unparks_the_request(self):
        from vs_workflow.services.parking import repair_workflows

        response = self.file(self.admin, self.ikeja_teacher)
        instance = self.instance_for(response)
        boss = self.make_staff("boss@ikeja.test", "Tayo", "Balogun", branch=self.ikeja)
        StaffOrganogramService.assign_position(boss, self.ikeja_hod)
        self.stand_as_staff(boss)
        StaffOrganogramService.assign_position(self.ikeja_teacher, self.ikeja_teacher_post)

        self.assertEqual(repair_workflows(instance_id=instance.id), 1)
        self.assertEqual(self.approver_ids(instance), {boss.user_id})

    def test_other_warnings_still_come_with_the_no_approver_one(self):
        self.file(self.admin, self.ikeja_teacher)
        response = self.file(self.admin, self.ikeja_teacher, start_date="2025-12-23")
        self.assertEqual(
            sorted(self.warning_codes(response)), ["LEAVE_OVERLAP", "NO_APPROVER"],
        )


class AnExistingLadderIsLeftAloneTests(LeaveLadderFixture):
    def test_a_group_based_ladder_is_not_replaced_or_changed(self):
        before = publish_group_leave_ladder(self.tenant)
        stage_before = before.stages.get()

        response = self.file(self.admin, self.eze)

        self.assertEqual(response.status_code, 201, response.data)
        templates = WorkflowTemplate.all_objects.filter(
            tenant=self.tenant, document_type=LEAVE_TYPE,
        )
        self.assertEqual(templates.count(), 1)
        [stage] = templates.get().stages.filter(retired_at__isnull=True)
        self.assertEqual(stage.pk, stage_before.pk)
        self.assertEqual(stage.approver_source, "WORKFLOW_GROUP")
        # Nobody is in the group, so it parks and says so.
        self.assertIn("NO_APPROVER", self.warning_codes(response))

    def test_ensuring_returns_the_existing_template_uncreated(self):
        before = publish_group_leave_ladder(self.tenant)
        template, created = approvals.ensure_tenant_approval_templates(self.tenant)
        self.assertEqual((template.pk, created), (before.pk, False))

    def test_a_second_ensure_is_a_no_op(self):
        template, created = approvals.ensure_tenant_approval_templates(self.tenant)
        again, created_again = approvals.ensure_tenant_approval_templates(self.tenant)
        self.assertEqual((created, created_again, template.pk), (True, False, again.pk))


class ProvisionCommandTests(LeaveLadderFixture):
    def run_command(self, *args):
        out = StringIO()
        call_command("provision_leave_approvals", *args, stdout=out)
        return out.getvalue()

    def has_ladder(self, tenant):
        return WorkflowTemplate.all_objects.filter(
            tenant=tenant, document_type=LEAVE_TYPE,
        ).exists()

    def test_it_publishes_for_every_school_and_names_them(self):
        output = self.run_command()

        self.assertTrue(self.has_ladder(self.tenant))
        self.assertTrue(self.has_ladder(self.solo.tenant))
        self.assertIn("brightfield: published the leave ladder", output)
        self.assertIn("sunrise: published the leave ladder", output)

    def test_it_is_idempotent(self):
        self.run_command()
        count = WorkflowTemplate.all_objects.filter(document_type=LEAVE_TYPE).count()
        output = self.run_command()

        self.assertEqual(
            WorkflowTemplate.all_objects.filter(document_type=LEAVE_TYPE).count(), count,
        )
        self.assertNotIn("published the leave ladder", output)
        self.assertIn("0 published", output)

    def test_it_leaves_a_group_based_ladder_exactly_as_it_is(self):
        before = publish_group_leave_ladder(self.tenant)
        self.run_command()
        [stage] = before.stages.filter(retired_at__isnull=True)
        self.assertEqual(stage.approver_source, "WORKFLOW_GROUP")
        self.assertTrue(self.has_ladder(self.solo.tenant))

    def test_a_dry_run_writes_nothing(self):
        output = self.run_command("--dry-run")
        self.assertIn("Dry run", output)
        self.assertFalse(self.has_ladder(self.tenant))

    def test_it_can_be_limited_to_one_school(self):
        self.run_command("--tenant", "sunrise")
        self.assertTrue(self.has_ladder(self.solo.tenant))
        self.assertFalse(self.has_ladder(self.tenant))

    def test_an_unknown_school_is_an_error(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_command("--tenant", "nowhere")


class GapReportsTests(LeaveLadderFixture):
    def test_a_school_without_a_ladder_is_a_gap_until_it_has_one(self):
        slugs = {gap["tenant_slug"] for gap in approvals.leave_template_gaps()}
        self.assertIn("brightfield", slugs)

        approvals.ensure_tenant_approval_templates(self.tenant)
        slugs = {gap["tenant_slug"] for gap in approvals.leave_template_gaps()}
        self.assertNotIn("brightfield", slugs)

    def test_an_empty_leave_group_is_reported_only_while_the_ladder_uses_it(self):
        publish_group_leave_ladder(self.tenant)
        slugs = {row["tenant_slug"] for row in approvals.unstaffed_leave_groups()}
        self.assertIn("brightfield", slugs)

    def test_the_line_manager_ladder_reports_no_unstaffed_group(self):
        from vs_workflow.services.groups import ensure_approver_group

        # A leftover empty group that no live stage routes through parks nothing.
        ensure_approver_group(self.tenant, "leave-approvers")
        approvals.ensure_tenant_approval_templates(self.tenant)
        slugs = {row["tenant_slug"] for row in approvals.unstaffed_leave_groups()}
        self.assertNotIn("brightfield", slugs)
