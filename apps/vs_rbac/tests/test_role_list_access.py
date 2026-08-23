"""Who may read a tenant's role list.

The list names roles and their holder counts, and reading it is a prerequisite
of configuring an approval stage: a stage names the role that approves it.
Rather than making every workflow template manager a role administrator, the
list endpoint accepts the template-manage key as well - and nothing else does.
"""
import itertools

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_rbac.tests.helpers import (
    make_assignment, make_branch, make_permission, make_role,
    make_role_permission, make_school, make_school_admin,
)
from vs_rbac.views import TenantRoleTemplateListCreateView

_counter = itertools.count(1)
factory = APIRequestFactory()
VIEW = TenantRoleTemplateListCreateView.as_view()


def _grant(user, keys):
    role = make_role(user.tenant, name=f"grant-{next(_counter)}")
    for k in keys:
        make_role_permission(role, make_permission(k))
    make_assignment(user.tenant, user, role)


def _call(method, user, tenant, data=None):
    path = f"/v1/rbac/tenants/{tenant.slug}/roles/"
    request = getattr(factory, method)(path, data, format="json")
    request.tenant = tenant
    request.rbac_tenant = tenant
    force_authenticate(request, user=user)
    return VIEW(request, tenant_slug=tenant.slug)


class RoleListAccessTests(TestCase):

    def setUp(self):
        self.school = make_school(slug=f"role-list-{next(_counter)}", name="Role List School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant

    def test_template_manager_can_read_the_role_list(self):
        user = make_school_admin(self.branch, email=f"tpl-{next(_counter)}@test.com")
        _grant(user, ["workflow.template.manage"])
        resp = _call("get", user, self.tenant)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_template_manager_cannot_create_a_role(self):
        """Reading travels with template management; writing does not."""
        user = make_school_admin(self.branch, email=f"tpl2-{next(_counter)}@test.com")
        _grant(user, ["workflow.template.manage"])
        resp = _call("post", user, self.tenant, {"name": "Sneaky", "key": "sneaky"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unrelated_permission_still_denied(self):
        user = make_school_admin(self.branch, email=f"none-{next(_counter)}@test.com")
        _grant(user, ["workflow.instance.view"])
        resp = _call("get", user, self.tenant)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
