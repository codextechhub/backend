"""Security regression coverage for shared requisition cost-centre options."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_finance.models import CostCenter, LedgerEntity
from vs_finance.views_ops.masterdata import CostCenterListCreateView
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
)
from vs_tenants.models import Tenant


class CostCenterProcurementAccessTests(TestCase):
    """Requisition writers can read scoped options but cannot maintain them."""

    def setUp(self):
        self.tenant = Tenant.objects.get(slug="codex")
        self.entity = LedgerEntity.objects.create(
            name="Cost Centre Access Books",
            code="CCACCESS",
            kind=LedgerEntity.Kind.TENANT,
            tenant=self.tenant,
        )
        self.active = CostCenter.objects.create(
            entity=self.entity, code="ACTIVE", name="Active Centre",
        )
        CostCenter.objects.create(
            entity=self.entity, code="INACTIVE", name="Inactive Centre", is_active=False,
        )
        self.factory = APIRequestFactory()

    def user_holding(self, *permission_keys, email):
        user = get_user_model().objects.create_user(
            email=email,
            password="x",
            status="ACTIVE",
            first_name="Cost",
            last_name="Centre Reader",
            tenant=self.tenant,
        )
        role = make_role(self.tenant, name=f"Role {email}")
        for key in permission_keys:
            make_role_permission(role, make_permission(key))
        make_assignment(self.tenant, user, role)
        return user

    def call_as(self, user, *, method="get", entity_code=None, data=None, active=None):
        params = {"entity": entity_code or self.entity.code}
        if active is not None:
            params["is_active"] = "true" if active else "false"
        path = "/v1/finance/cost-centers/"
        if method == "post":
            request = self.factory.post(f"{path}?entity={params['entity']}", data or {}, format="json")
        else:
            request = self.factory.get(path, params)
        force_authenticate(request, user=user)
        request.tenant = user.tenant
        request.rbac_tenant = user.tenant
        response = CostCenterListCreateView.as_view()(request)
        response.render()
        return response

    def test_requisition_create_or_update_grant_can_read_active_options(self):
        for index, permission in enumerate((
            "procurement.requisition.create",
            "procurement.requisition.update",
        )):
            with self.subTest(permission=permission):
                user = self.user_holding(
                    permission, email=f"procurement-reader-{index}@test.com",
                )
                response = self.call_as(user, active=True)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [row["code"] for row in response.data["data"]], [self.active.code],
                )

    def test_finance_view_grant_keeps_existing_read_access(self):
        user = self.user_holding(
            "finance.costcenter.view", email="finance-cost-centre-reader@test.com",
        )

        response = self.call_as(user, active=True)

        self.assertEqual(response.status_code, 200)

    def test_requisition_grant_cannot_create_or_update_cost_centres(self):
        user = self.user_holding(
            "procurement.requisition.create", email="procurement-no-mutation@test.com",
        )

        attempts = (
            ({"code": "FORBIDDEN", "name": "Forbidden Centre"}, "create"),
            ({"code": self.active.code, "name": "Renamed Centre"}, "update"),
        )
        for payload, operation in attempts:
            with self.subTest(operation=operation):
                response = self.call_as(user, method="post", data=payload)
                self.assertEqual(response.status_code, 403)

        self.assertFalse(
            CostCenter.objects.filter(entity=self.entity, code="FORBIDDEN").exists(),
        )
        self.active.refresh_from_db()
        self.assertEqual(self.active.name, "Active Centre")

    def test_requisition_grant_cannot_read_another_tenants_entity(self):
        foreign_tenant = Tenant.objects.create(
            name="Foreign Cost Centre Tenant",
            slug="foreign-cost-centre-tenant",
            kind=Tenant.Kind.ORGANIZATION,
            status=Tenant.Status.ACTIVE,
        )
        foreign_entity = LedgerEntity.objects.create(
            name="Foreign Cost Centre Books",
            code="CCFOREIGN",
            kind=LedgerEntity.Kind.TENANT,
            tenant=foreign_tenant,
        )
        CostCenter.objects.create(
            entity=foreign_entity, code="FOREIGN", name="Foreign Centre",
        )
        user = self.user_holding(
            "procurement.requisition.create", email="procurement-isolated@test.com",
        )

        response = self.call_as(user, entity_code=foreign_entity.code, active=True)

        self.assertEqual(response.status_code, 404)

    def test_unrelated_permission_cannot_read_cost_centres(self):
        user = self.user_holding(
            "procurement.vendor.view", email="unrelated-procurement-reader@test.com",
        )

        response = self.call_as(user, active=True)

        self.assertEqual(response.status_code, 403)
