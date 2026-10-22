"""Only a member of staff can be given a seat on a school's approvals.

Greenfield runs a two-stage ladder: Funke heads the department and Tunde is
the bursar. Mrs. Okafor is a parent with a guardian record and her son Emeka
has a pupil portal account; neither holds a role. Sam is staff at another
school. Every route that names a person to approve (a delegate, a group
member, a stage's people, a replacement, a Dynamic Role rule) refuses all
three with one 400 on the field that named them, "Choose a member of staff.",
and accepts Chidi Okafor, who is on Greenfield's staff.
"""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

from core.test_utils import TenantAPIClient
from schools.vs_students.constants import Gender, StudentStatus
from schools.vs_students.models import Guardian, Student
from vs_workflow.constants import PERM_GROUP_CREATE, PERM_GROUP_UPDATE
from vs_workflow.models import ApprovalDelegation, WorkflowApproverGroup
from vs_workflow.tests.test_reassignment import _Fixture, _grant

BASE = "/v1/workflow"
NOT_STAFF = ["Choose a member of staff."]
REFUND = "finance.refund"


class ApprovalSeatsAreForStaffTests(_Fixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _grant(cls.admin, [PERM_GROUP_CREATE, PERM_GROUP_UPDATE])

        cls.parent = _account(cls.tenant, "mrs.okafor@example.ng")
        Guardian.all_objects.create(
            tenant=cls.tenant, first_name="Ada", last_name="Okafor",
            phone="08030000000", email=cls.parent.email, user=cls.parent,
        )
        cls.pupil = _account(cls.tenant, "emeka.okafor@example.ng")
        Student.all_objects.create(
            tenant=cls.tenant, branch=cls.ikeja, first_name="Emeka",
            last_name="Okafor", student_number="GF/0001",
            date_of_birth=dt.date(2014, 3, 2), gender=Gender.MALE,
            status=StudentStatus.ACTIVE, enrolment_date=dt.date(2025, 9, 8),
            user=cls.pupil,
        )
        cls.cover = WorkflowApproverGroup.objects.create(
            tenant=cls.tenant, code="cover", name="Cover",
        )

    def refused(self):
        """Mrs. Okafor, Emeka and Sam, each with a label for the failure."""
        return (
            ("a parent", self.parent),
            ("a pupil", self.pupil),
            ("another school's staff", self.stranger),
        )

    def assert_refused(self, response, field):
        self.assertEqual(response.status_code, 400, response.data)
        detail = response.data["error"]["detail"]
        self.assertEqual([str(m) for m in detail[field]], NOT_STAFF)

    # ── each route ─────────────────────────────────────────────────────

    def delegate(self, person):
        now = timezone.now()
        return TenantAPIClient(self.funke).post(f"{BASE}/delegations/", {
            "delegate": person.pk, "starts_at": now.isoformat(),
            "ends_at": (now + dt.timedelta(days=5)).isoformat(), "reason": "Leave",
        }, format="json")

    def add_member(self, person):
        return TenantAPIClient(self.admin).post(
            f"{BASE}/approver-groups/{self.cover.pk}/members/",
            {"kind": "USER", "user": str(person.pk)}, format="json",
        )

    def set_stage(self, person, request):
        return TenantAPIClient(self.admin).post(
            f"{BASE}/instances/{request.pk}/approvers/",
            {"stage": self.hod.pk, "approvers": [self.funke.pk, person.pk],
             "reason": "Funke needs cover"}, format="json",
        )

    def replace(self, person, request):
        return TenantAPIClient(self.admin).post(
            f"{BASE}/instances/replace-approver/",
            {"from_user": self.funke.pk, "to_user": person.pk,
             "instance_ids": [request.pk], "reason": "Funke is away"}, format="json",
        )

    def rule(self, person, code):
        return TenantAPIClient(self.admin).post(f"{BASE}/dynamic-roles/", {
            "code": code, "name": "Refund cover", "document_types": [REFUND],
            "rules": [{"condition": None, "target_kind": "USER", "user": str(person.pk)}],
        }, format="json")

    # ── refusals ───────────────────────────────────────────────────────

    def test_a_non_staff_delegate_is_refused(self):
        for label, person in self.refused():
            with self.subTest(person=label):
                self.assert_refused(self.delegate(person), "delegate")
        self.assertFalse(ApprovalDelegation.objects.filter(delegator=self.funke).exists())

    def test_a_non_staff_group_member_is_refused(self):
        for label, person in self.refused():
            with self.subTest(person=label):
                self.assert_refused(self.add_member(person), "user")
        self.assertEqual(self.cover.members.count(), 0)

    def test_a_non_staff_stage_approver_is_refused(self):
        request = self.raise_request()
        for label, person in self.refused():
            with self.subTest(person=label):
                self.assert_refused(self.set_stage(person, request), "approvers")
        self.assertEqual(self.seats(request), {(self.funke.pk, None)})

    def test_a_non_staff_replacement_is_refused(self):
        request = self.raise_request()
        for label, person in self.refused():
            with self.subTest(person=label):
                self.assert_refused(self.replace(person, request), "to_user")
        self.assertEqual(self.seats(request), {(self.funke.pk, None)})

    def test_a_non_staff_dynamic_role_target_is_refused(self):
        for n, (label, person) in enumerate(self.refused()):
            with self.subTest(person=label):
                response = self.rule(person, f"refund-cover-{n}")
                self.assertEqual(response.status_code, 400, response.data)
                rules = response.data["error"]["detail"]["rules"]
                self.assertEqual([str(m) for m in rules], ["Rule 1: Choose a member of staff."])

    # ── staff accepted ─────────────────────────────────────────────────

    def test_a_member_of_staff_is_accepted_on_every_route(self):
        request = self.raise_request()
        response = self.set_stage(self.okafor, request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.seats(request),
                         {(self.funke.pk, None), (self.okafor.pk, None)})

        later = self.raise_request()
        response = self.replace(self.tunde, later)
        self.assertEqual(response.status_code, 200, response.data)

        self.assertEqual(self.delegate(self.okafor).status_code, 201)
        self.assertEqual(self.add_member(self.okafor).status_code, 201)
        self.assertEqual(self.rule(self.okafor, "refund-cover").status_code, 201)


def _account(tenant, email):
    """An account at the school holding no role: the shape of a parent's or pupil's."""
    from vs_rbac.tests.helpers import make_school_admin

    return make_school_admin(None, email=email, tenant=tenant)
