"""Field Access on the new-hire and leave-request approval cards.

Brightfield runs two branches and Sunrise one. A hire card restates the staff
fields a school can close per role (email, staff ID, employment type, hire
date), so an approver whose role may not read one is not shown it. The person's
name and job title, the branch and the starting role are what identify the hire
and always show. A leave card restates only a name and a job title.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json

from django.contrib.contenttypes.models import ContentType

from core.test_utils import TenantAPIClient
from schools.vs_staff.constants import EmploymentStatus, LeaveStatus
from schools.vs_staff.models import LeaveRequest
from schools.vs_staff.workflow_handlers import (
    HIRE_DOCUMENT_TYPE, LEAVE_DOCUMENT_TYPE, LeaveRequestWorkflowHandler,
    StaffHireWorkflowHandler,
)
from vs_rbac.models import PermissionScope, TenantRoleTemplate
from vs_rbac.tests.helpers import (
    install_declared_fields, make_assignment, make_permission, make_role,
    make_role_permission, set_field_access,
)
from vs_workflow.constants import WorkflowInstanceStatus
from vs_workflow.models import WorkflowInstance, WorkflowTemplate
from vs_workflow.services.submission import document_snapshot

from .base import StaffFixture

_seq = itertools.count(1)

#: The registered staff field behind each row of a hire card.
HIRE_ROWS = {
    "email": "Email",
    "staff_number": "Staff ID",
    "employment_type": "Employment type",
    "hire_date": "Hire date",
}


class _CardFixture(StaffFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields("school.teachers")
        cls.view = make_permission("workflow.instance.view", scope=PermissionScope.TENANT)
        cls.hire = cls.pending_hire(
            "funke@brightfield.test", "Funke", "Adeyemi", cls.ikeja, cls.tenant, cls.school)
        cls.solo_hire = cls.pending_hire(
            "tobi@sunrise.test", "Tobi", "Bello", cls.solo_branch, cls.solo.tenant, cls.solo)

    @classmethod
    def pending_hire(cls, email, first, last, branch, tenant, school):
        profile = cls.make_staff(
            email, first, last, branch=branch, job_title="Bursar",
            staff_number="STF/HIRE/0042", tenant=tenant, school=school,
            status=EmploymentStatus.PENDING_APPROVAL,
        )
        profile.employment_type = "FULL_TIME"
        profile.hire_date = dt.date(2026, 9, 7)
        profile.save(update_fields=["employment_type", "hire_date"])
        return profile

    @classmethod
    def approver(cls, school, *, hide=()):
        name = f"approver-{next(_seq)}"
        tenant = school.tenant
        role = make_role(school, name=name, key=name)
        make_role_permission(role, cls.view)
        user = cls.make_staff(f"{name}@cards.test", "Ada", "Reader", tenant=tenant, school=school)
        make_assignment(school, user.user, role, branch=None)
        set_field_access(role, *cls.field_keys, read=True, write=True)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        return user.user

    def request_for(self, document, handler, tenant, requester, *, document_type, branch):
        summary, details = document_snapshot(handler, document, document_type)
        template = WorkflowTemplate.objects.create(
            tenant=tenant, branch=None, document_type=document_type,
            code=f"card-{next(_seq)}", name="Card route")
        return WorkflowInstance.all_objects.create(
            tenant=tenant, branch=branch, template=template,
            document_content_type=ContentType.objects.get_for_model(type(document)),
            document_object_id=str(document.pk), document_type=document_type,
            status=WorkflowInstanceStatus.IN_PROGRESS, requested_by=requester,
            document_summary=summary, document_details=details)

    def read(self, user, instance):
        response = TenantAPIClient(user=user).get(f"/v1/workflow/instances/{instance.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    @staticmethod
    def labels(card) -> set:
        found = {f["label"] for f in card["document_summary"].get("fields", [])}
        for section in card["document_details"].get("sections", []):
            if section["kind"] == "fields":
                found.update(item["label"] for item in section["items"])
        return found

    @staticmethod
    def dump(card) -> str:
        return json.dumps(card, default=str, ensure_ascii=False)


class HireCardFieldAccessTests(_CardFixture):

    def card(self, school, hire, hide=()):
        handler = StaffHireWorkflowHandler()
        instance = self.request_for(
            hire, handler, school.tenant, self.admin if school is self.school else self.solo_admin,
            document_type=HIRE_DOCUMENT_TYPE, branch=hire.branch)
        return self.read(self.approver(school, hide=[f"school.teachers.{n}" for n in hide]), instance)

    def test_an_approver_with_every_field_open_sees_the_whole_hire_card(self):
        for school, hire in ((self.school, self.hire), (self.solo, self.solo_hire)):
            with self.subTest(school=school.slug):
                card = self.card(school, hire)
                self.assertTrue(set(HIRE_ROWS.values()) <= self.labels(card))
                self.assertIn("STF/HIRE/0042", self.dump(card))
                self.assertIn(hire.user.email, self.dump(card))

    def test_closing_one_field_removes_its_row_and_value_only(self):
        for school, hire in ((self.school, self.hire), (self.solo, self.solo_hire)):
            values = {
                "email": hire.user.email, "staff_number": "STF/HIRE/0042",
            }
            baseline = self.labels(self.card(school, hire))
            for name, label in HIRE_ROWS.items():
                with self.subTest(school=school.slug, field=name):
                    card = self.card(school, hire, hide=[name])
                    self.assertEqual(self.labels(card), baseline - {label})
                    if name in values:
                        self.assertNotIn(values[name], self.dump(card))

    def test_names_job_title_branch_and_starting_role_always_show(self):
        for school, hire, first in (
            (self.school, self.hire, "Funke Adeyemi"), (self.solo, self.solo_hire, "Tobi Bello"),
        ):
            with self.subTest(school=school.slug):
                card = self.card(school, hire, hide=list(HIRE_ROWS))
                self.assertEqual(card["document_summary"]["title"], first)
                self.assertEqual(card["document_summary"]["subtitle"], "Bursar")
                self.assertTrue({"Posting", "Starting role"} <= self.labels(card))
                self.assertIn(hire.branch.name, self.dump(card))
                self.assertNotIn(hire.user.email, self.dump(card))
                self.assertNotIn("STF/HIRE/0042", self.dump(card))

    def test_the_stored_snapshot_keeps_the_values_and_names_each_key(self):
        instance = self.request_for(
            self.hire, StaffHireWorkflowHandler(), self.tenant, self.admin,
            document_type=HIRE_DOCUMENT_TYPE, branch=self.ikeja)
        self.read(self.approver(self.school, hide=[f"school.teachers.{n}" for n in HIRE_ROWS]), instance)
        stored = WorkflowInstance.all_objects.get(pk=instance.pk).document_details
        items = stored["sections"][0]["items"]
        self.assertEqual(
            {item["access"] for item in items},
            {f"school.teachers.{name}" for name in HIRE_ROWS},
        )
        self.assertIn(self.hire.user.email, self.dump(stored))

    def test_one_schools_switches_do_not_reach_another_and_its_cards_are_not_served(self):
        mine = self.request_for(
            self.hire, StaffHireWorkflowHandler(), self.tenant, self.admin,
            document_type=HIRE_DOCUMENT_TYPE, branch=self.ikeja)
        theirs = self.request_for(
            self.solo_hire, StaffHireWorkflowHandler(), self.solo.tenant, self.solo_admin,
            document_type=HIRE_DOCUMENT_TYPE, branch=self.solo_branch)
        closed = self.approver(self.school, hide=["school.teachers.email"])
        open_solo = self.approver(self.solo)
        self.assertNotIn(self.hire.user.email, self.dump(self.read(closed, mine)))
        self.assertIn(self.solo_hire.user.email, self.dump(self.read(open_solo, theirs)))
        refused = TenantAPIClient(user=closed).get(f"/v1/workflow/instances/{theirs.pk}/")
        self.assertEqual(refused.status_code, 404, refused.data)


class LeaveCardFieldAccessTests(_CardFixture):

    def test_a_leave_card_restates_only_a_name_and_a_job_title_so_every_switch_leaves_it_whole(self):
        leave = LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze, leave_type="SICK",
            start_date=dt.date(2026, 3, 2), end_date=dt.date(2026, 3, 6), days=5,
            status=LeaveStatus.PENDING, note="Recovering from surgery.",
        )
        instance = self.request_for(
            leave, LeaveRequestWorkflowHandler(), self.tenant, self.admin,
            document_type=LEAVE_DOCUMENT_TYPE, branch=self.lekki)
        every = [f"school.teachers.{spec}" for spec in (
            "first_name", "last_name", "job_title", "email", "staff_number",
            "hire_date", "employment_type", "employment_status")]
        open_card = self.read(self.approver(self.school), instance)
        closed_card = self.read(self.approver(self.school, hide=every), instance)
        self.assertEqual(open_card["document_summary"], closed_card["document_summary"])
        self.assertEqual(open_card["document_details"], closed_card["document_details"])
        self.assertEqual(closed_card["document_summary"]["title"], "Chukwuemeka Eze")
        self.assertIn("Lead Teacher", self.dump(closed_card))
        self.assertNotIn("eze@brightfield.test", self.dump(closed_card))
        self.assertNotIn("BFS/STF/0012", self.dump(closed_card))
