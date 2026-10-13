"""An approver reads a permission in the same words the role screens show.

A permission with no description is worded from its action and resource, so the
approval details for a role change or a restricted role grant never fall back to
the dotted key.
"""
import json

from django.test import TestCase

from vs_rbac.services import grant_role, raise_role_change_request
from vs_rbac.workflow_handlers import RoleChangeWorkflowHandler, RoleGrantWorkflowHandler

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

BLANK_KEY = "school.students.transfer"
DESCRIBED_KEY = "finance.payout.approve"
DESCRIPTION = "Approve a payout batch"


def _labels(details):
    changes = details["sections"][-1]
    return [item["label"] for item in changes["items"]]


class RoleChangeLabelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="labels-change", name="Labels Change School")
        cls.branch = make_branch(cls.school)
        cls.head = make_school_admin(cls.branch, email="head@labels-change.test")
        cls.role_admin = make_role(
            cls.school, name="School Admin", key="school_admin", is_system_role=True,
        )
        make_role_permission(cls.role_admin, make_permission("school.roles.approve"))
        make_assignment(cls.school, cls.head, cls.role_admin)
        cls.target = make_role(cls.school, name="Registrar")
        make_permission(BLANK_KEY)
        make_permission(DESCRIBED_KEY, description=DESCRIPTION, is_restricted=True)
        cls.request = raise_role_change_request(
            tenant=cls.school.tenant,
            requested_by=cls.head,
            target_role=cls.target,
            justification="Ada covers admissions this term.",
            deltas=[
                {"permission_key": BLANK_KEY, "operation": "ADD"},
                {"permission_key": DESCRIBED_KEY, "operation": "ADD"},
            ],
        )

    def test_a_blank_description_reads_as_action_and_resource(self):
        details = RoleChangeWorkflowHandler().get_document_details(self.request)

        self.assertIn("Transfer students", _labels(details))

    def test_a_described_permission_shows_its_description(self):
        details = RoleChangeWorkflowHandler().get_document_details(self.request)

        self.assertIn(DESCRIPTION, _labels(details))

    def test_no_raw_key_reaches_the_approver(self):
        details = RoleChangeWorkflowHandler().get_document_details(self.request)

        text = json.dumps(details)
        self.assertNotIn(BLANK_KEY, text)
        self.assertNotIn(DESCRIBED_KEY, text)

    def test_the_permissions_are_read_without_a_query_per_item(self):
        handler = RoleChangeWorkflowHandler()
        handler.get_document_details(self.request)

        with self.assertNumQueries(1):
            handler.get_document_details(self.request)


class RoleGrantLabelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="labels-grant", name="Labels Grant School")
        cls.tenant = cls.school.tenant
        cls.branch = make_branch(cls.school)
        cls.head = make_school_admin(cls.branch, email="head@labels-grant.test")
        cls.role_admin = make_role(
            cls.school, name="School Admin", key="school_admin", is_system_role=True,
        )
        make_role_permission(cls.role_admin, make_permission("school.roles.approve"))
        make_assignment(cls.school, cls.head, cls.role_admin)
        cls.registrar = make_role(cls.school, name="Registrar", key="registrar")
        make_role_permission(
            cls.registrar, make_permission(BLANK_KEY, is_restricted=True),
        )
        make_role_permission(
            cls.registrar,
            make_permission(DESCRIBED_KEY, description=DESCRIPTION, is_restricted=True),
        )
        cls.request = grant_role(
            tenant=cls.tenant, user=cls.head, role=cls.registrar,
            branch=None, actor=cls.head,
        ).request

    def test_a_blank_description_reads_as_action_and_resource(self):
        details = RoleGrantWorkflowHandler().get_document_details(self.request)

        self.assertCountEqual(_labels(details), ["Transfer students", DESCRIPTION])

    def test_no_raw_key_reaches_the_approver(self):
        details = RoleGrantWorkflowHandler().get_document_details(self.request)

        text = json.dumps(details)
        self.assertNotIn(BLANK_KEY, text)
        self.assertNotIn(DESCRIBED_KEY, text)
