"""Every teacher raises requisitions, and the line manager decides them first.

Harbour Primary's teachers hold the Teacher role's requisition keys: they raise,
correct and submit their own requests and read the catalogue, and they read no
one else's. Funmi asks for microscopes and Tunde for footballs; neither sees,
edits or submits the other's. The school's buyer, who reads every requisition,
sees both.

A school's requisition route starts with the requester's line manager on the
school's own organogram, whether or not the school asked for the default ladder.
Brightfield's Ikeja teacher reports to the Ikeja head of department, so her
request waits on him.
"""
from __future__ import annotations

import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.test_utils import TenantAPIClient
from schools.vs_staff.tests.test_organogram import OrganogramFixture
from vs_finance.models import CostCenter, LedgerEntity
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

from .approvals import ensure_tenant_approval_templates
from .constants import WF_DEFAULT_TEMPLATE_CODE
from .models import PurchaseRequisition

TEACHER_KEYS = (
    "procurement.requisition.create",
    "procurement.requisition.update",
    "procurement.requisition.submit",
    "procurement.catalog_item.view",
)


class TeachersReadOnlyTheirOwnRequisitionsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        cls.harbour = make_school(slug="harbour-teacher-req", name="Harbour Primary")
        cls.main = make_branch(cls.harbour, name="Main Branch")
        cls.books = LedgerEntity.objects.create(
            name="HBRTCH Books", code="HBRTCH", kind=LedgerEntity.Kind.TENANT,
            tenant=cls.harbour.tenant,
        )
        seed_chart_of_accounts(cls.books)
        cls.lab = CostCenter.objects.create(entity=cls.books, code="SCI-LAB", name="Science lab")

        teacher = make_role(cls.harbour, name="Teacher", key="teacher")
        for key in TEACHER_KEYS:
            make_role_permission(teacher, make_permission(key))
        cls.funmi = make_school_admin(cls.main, email="funmi@harbour-teacher-req.example.com")
        cls.tunde = make_school_admin(cls.main, email="tunde@harbour-teacher-req.example.com")
        make_assignment(cls.harbour, cls.funmi, teacher, branch=cls.main)
        make_assignment(cls.harbour, cls.tunde, teacher, branch=cls.main)

        buyer = make_role(cls.harbour, name="Buyer", key="buyer")
        make_role_permission(buyer, make_permission("procurement.requisition.view"))
        cls.buyer = make_school_admin(cls.main, email="buyer@harbour-teacher-req.example.com")
        make_assignment(cls.harbour, cls.buyer, buyer, branch=cls.main)

        cls.microscopes = cls.requisition(cls.funmi, "Microscopes for Biology")
        cls.footballs = cls.requisition(cls.tunde, "Footballs for games")

    @classmethod
    def requisition(cls, user, title):
        return PurchaseRequisition.objects.create(
            entity=cls.books, branch=cls.main, title=title, requested_by=user,
            created_by=user, request_date=datetime.date(2026, 10, 3),
        )

    def url(self, path=""):
        return f"/v1/procurement/requisitions/{path}?entity={self.books.code}"

    def listed(self, user):
        response = TenantAPIClient(user=user).get(self.url())
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"] for row in response.data["data"]}

    def test_a_teacher_lists_only_their_own_requisitions(self):
        self.assertEqual(self.listed(self.funmi), {self.microscopes.pk})

    def test_the_buyer_lists_every_requisition(self):
        self.assertEqual(self.listed(self.buyer), {self.microscopes.pk, self.footballs.pk})

    def test_a_teacher_opens_their_own_and_not_a_colleagues(self):
        client = TenantAPIClient(user=self.funmi)
        self.assertEqual(client.get(self.url(f"{self.microscopes.pk}/")).status_code, 200)
        self.assertEqual(client.get(self.url(f"{self.footballs.pk}/")).status_code, 404)

    def test_a_teacher_cannot_edit_a_colleagues_draft(self):
        response = TenantAPIClient(user=self.funmi).patch(
            self.url(f"{self.footballs.pk}/"), {"title": "Microscopes instead"}, format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.footballs.refresh_from_db()
        self.assertEqual(self.footballs.title, "Footballs for games")

    def test_a_teacher_cannot_submit_a_colleagues_requisition(self):
        response = TenantAPIClient(user=self.funmi).post(
            self.url(f"{self.footballs.pk}/submit/"), {}, format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.footballs.refresh_from_db()
        self.assertEqual(self.footballs.status, "DRAFT")

    def test_the_summary_counts_only_the_teachers_own(self):
        response = TenantAPIClient(user=self.funmi).get(
            f"/v1/procurement/requisitions/summary/?entity={self.books.code}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["draft"]["count"], 1)

    def test_a_teacher_reads_the_budget_panel_on_the_form(self):
        response = TenantAPIClient(user=self.funmi).get(
            f"/v1/procurement/requisitions/budget-availability/"
            f"?entity={self.books.code}&cost_center={self.lab.code}",
        )
        self.assertEqual(response.status_code, 200, response.data)


class TeacherRoleDefaultsTests(TestCase):
    """The library Teacher role raises requisitions, and granting it stays direct."""

    @classmethod
    def setUpTestData(cls):
        out = StringIO()
        call_command("seed_actions", stdout=out)
        call_command("seed_prebuilt_role_templates", stdout=out)
        call_command("seed_school_permissions", stdout=out)
        call_command("seed_procurement_permissions", stdout=out)

    def test_the_library_teacher_role_holds_the_raise_keys_and_not_view(self):
        from vs_rbac.models import PrebuiltRolePermission

        held = set(PrebuiltRolePermission.objects.filter(
            prebuilt_role__key="teacher", permission__key__startswith="procurement.",
        ).values_list("permission_id", flat=True))
        self.assertEqual(held, set(TEACHER_KEYS))

    def test_submitting_a_requisition_is_not_a_restricted_key(self):
        from vs_rbac.models import Permission

        submit = Permission.objects.get(key="procurement.requisition.submit")
        self.assertFalse(submit.is_restricted)

    def test_an_existing_schools_teachers_gain_the_keys_on_the_next_seed(self):
        from vs_rbac.models import PrebuiltRolePermission, TenantRolePermission
        from vs_rbac.services import provision_role_from_prebuilt
        from vs_rbac.validators import role_restricted_permission_keys

        school = make_school(slug="sunrise-teacher-req", name="Sunrise")
        branch = make_branch(school, name="Sunrise Main", is_main=True)
        PrebuiltRolePermission.objects.filter(
            prebuilt_role__key="teacher", permission_id__in=TEACHER_KEYS,
        ).delete()
        teacher = provision_role_from_prebuilt(
            tenant=school.tenant, branch=branch, prebuilt_key="teacher",
        )
        TenantRolePermission.objects.filter(role=teacher, permission_id__in=TEACHER_KEYS).delete()

        call_command("seed_procurement_permissions", stdout=StringIO())

        held = set(TenantRolePermission.objects.filter(
            role=teacher, granted=True, permission__key__startswith="procurement.",
        ).values_list("permission_id", flat=True))
        self.assertEqual(held, set(TEACHER_KEYS))
        self.assertFalse(
            {key for key in role_restricted_permission_keys(teacher) if key.startswith("procurement.")},
        )


class RequisitionRouteStartsAtTheLineManagerTests(OrganogramFixture):
    def setUp(self):
        self.appoint(self.registrar, self.principal)
        self.hod = self.make_staff("hod-route@brightfield.test", "Ngozi", "Okafor", branch=self.ikeja)
        self.appoint(self.hod, self.ikeja_hod)
        self.appoint(self.ikeja_teacher, self.ikeja_teacher_post)
        self.stand_as_staff(self.registrar, self.hod)

    @staticmethod
    def routes(results):
        return {template.document_type: template for template, _ in results}

    @staticmethod
    def steps(template):
        return list(
            template.stages.filter(retired_at__isnull=True).order_by("order")
            .values_list("code", "approver_source", "organogram_target"),
        )

    def test_a_school_route_starts_at_the_line_manager_without_the_default_ladder(self):
        routes = self.routes(ensure_tenant_approval_templates(self.tenant, with_default_stages=False))

        self.assertEqual(
            self.steps(routes["procurement.requisition"]),
            [("line_manager", "ORGANOGRAM", "DIRECT_MANAGER")],
        )
        self.assertEqual(self.steps(routes["procurement.purchase_order"]), [])
        # Alone, a step nobody can fill waits rather than approving the request.
        self.assertFalse(routes["procurement.requisition"].stages.get(code="line_manager").skip_if_no_approvers)

    def test_the_default_ladder_follows_the_line_manager(self):
        routes = self.routes(ensure_tenant_approval_templates(self.tenant, with_default_stages=True))

        self.assertEqual(
            [code for code, _, _ in self.steps(routes["procurement.requisition"])],
            ["line_manager", "manager", "senior"],
        )
        self.assertEqual(
            [code for code, _, _ in self.steps(routes["procurement.purchase_order"])],
            ["manager", "senior"],
        )
        # Followed by the approver groups, a requester with no line manager on
        # the chart goes straight to them, who still have to approve it.
        stages = {s.code: s for s in routes["procurement.requisition"].stages.all()}
        self.assertTrue(stages["line_manager"].skip_if_no_approvers)
        self.assertFalse(stages["manager"].skip_if_no_approvers)

    def test_a_teachers_request_waits_on_her_head_of_department(self):
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone

        from vs_workflow.models import WorkflowInstance
        from vs_workflow.services.approvers import resolve_approvers

        route = self.routes(
            ensure_tenant_approval_templates(self.tenant, with_default_stages=False),
        )["procurement.requisition"]
        instance = WorkflowInstance.objects.create(
            tenant=self.tenant, template=route,
            document_content_type=ContentType.objects.get_for_model(PurchaseRequisition),
            document_object_id="1", document_type=route.document_type,
            status="IN_PROGRESS", requested_by=self.ikeja_teacher.user,
            submitted_at=timezone.now(),
        )
        stage = route.stages.get(code="line_manager")

        self.assertEqual(
            [approver.user.pk for approver in resolve_approvers(stage, instance)],
            [self.hod.user.pk],
        )

    def test_an_empty_requisition_route_is_given_the_step(self):
        from vs_workflow.services.templates import publish_template

        publish_template(
            tenant=self.tenant, branch=None, document_type="procurement.requisition",
            code=WF_DEFAULT_TEMPLATE_CODE, name="Requisition approval", stages_payload=[],
        )

        results = ensure_tenant_approval_templates(self.tenant, with_default_stages=False)

        route, created = next(r for r in results if r[0].document_type == "procurement.requisition")
        self.assertTrue(created)
        self.assertEqual([code for code, _, _ in self.steps(route)], ["line_manager"])

    def test_a_route_the_school_configured_is_left_alone(self):
        ensure_tenant_approval_templates(self.tenant, with_default_stages=True)

        results = ensure_tenant_approval_templates(self.tenant, with_default_stages=False)

        route, created = next(r for r in results if r[0].document_type == "procurement.requisition")
        self.assertFalse(created)
        self.assertEqual(
            [code for code, _, _ in self.steps(route)], ["line_manager", "manager", "senior"],
        )

    def test_a_tenant_without_an_organogram_keeps_an_empty_route(self):
        from vs_tenants.models import Tenant

        company = Tenant.objects.create(
            name="Acme Ltd", slug="acme-route", kind=Tenant.Kind.ORGANIZATION,
        )
        routes = self.routes(ensure_tenant_approval_templates(company, with_default_stages=False))

        self.assertEqual(self.steps(routes["procurement.requisition"]), [])
