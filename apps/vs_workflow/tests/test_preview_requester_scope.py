"""The template builder's approver preview answers only for the caller's own people.

Resolution runs in the sample requester's tenant, so a requester id from another
tenant would answer with that tenant's approvers - names and email addresses the
caller has no business reading. User ids are sequential, so they are guessed as
easily as typed. The requester is looked up inside the caller's tenant.
"""
import itertools

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_rbac.tests.helpers import (
    make_assignment, make_branch, make_permission, make_role,
    make_role_permission, make_school, make_school_admin,
)
from vs_workflow.constants import PERM_TEMPLATE_VIEW
from vs_workflow.views import WorkflowTemplateViewSet

PREVIEW = WorkflowTemplateViewSet.as_view({"post": "preview_approvers"})
factory = APIRequestFactory()
_counter = itertools.count(1)


def _grant(user, keys):
    role = make_role(user.tenant, name=f"pv-grant-{next(_counter)}", is_system_role=True)
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(user.tenant, user, role)


def _preview(user, tenant, data):
    request = factory.post("/v1/workflow/templates/preview-approvers/", data, format="json")
    request.tenant = tenant
    request.rbac_tenant = tenant
    force_authenticate(request, user=user)
    return PREVIEW(request)


class PreviewRequesterScopeTests(TestCase):

    def setUp(self):
        school = make_school(slug="pv-own-school", name="Own School")
        self.branch = make_branch(school)
        self.tenant = school.tenant
        self.admin = make_school_admin(self.branch, email="pv-admin@test.com")
        _grant(self.admin, [PERM_TEMPLATE_VIEW])
        make_role(self.tenant, name="Bursar", key="bursar", is_system_role=True)

        other = make_school(slug="pv-other-school", name="Other School")
        other_branch = make_branch(other)
        self.outsider = make_school_admin(other_branch, email="pv-outsider@test.com")
        other_bursar = make_role(other.tenant, name="Bursar", key="bursar", is_system_role=True)
        make_assignment(other.tenant, make_school_admin(other_branch, email="pv-their-bursar@test.com"),
                        other_bursar)

    def test_a_requester_from_another_tenant_is_not_found(self):
        resp = _preview(self.admin, self.tenant, {
            "requester": str(self.outsider.pk),
            "approver_source": "ROLE", "approver_role_key": "bursar",
        })
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertNotIn("approvers", resp.data)

    def test_a_requester_from_the_callers_tenant_is_previewed(self):
        colleague = make_school_admin(self.branch, email="pv-colleague@test.com")
        resp = _preview(self.admin, self.tenant, {
            "requester": str(colleague.pk),
            "approver_source": "ROLE", "approver_role_key": "bursar",
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_a_malformed_requester_id_is_not_found(self):
        resp = _preview(self.admin, self.tenant, {
            "requester": "not-an-id",
            "approver_source": "ROLE", "approver_role_key": "bursar",
        })
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
