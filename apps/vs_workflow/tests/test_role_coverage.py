"""What ``workflow_role_coverage`` is willing to call healthy.

The command exists to answer one question before it costs anything: can this
tenant actually staff the approval stages a central template routes to it?

It used to answer that by counting holders, which is not the same question. A
role held by exactly one person reads as staffed and is not: a requester may
never approve their own submission, so everything that person raises has zero
eligible approvers and parks with nobody able to release it. A live tenant sat
in exactly that state while the command reported it green, which is what these
tests pin.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from vs_rbac.tests.helpers import codex_tenant, make_assignment, make_role, make_vision_user
from vs_workflow.models import WorkflowStage, WorkflowTemplate

ROLE_KEY = "coverage-approver"


def _run(*args) -> str:
    out = StringIO()
    call_command("workflow_role_coverage", *args, stdout=out, stderr=out)
    return out.getvalue()


class RoleCoverageTests(TestCase):
    """Every case is about one tenant and one central stage naming one role."""

    def setUp(self):
        self.tenant = codex_tenant()
        # A central template: no tenant of its own, so its role key is resolved
        # inside whichever tenant raises the document. That indirection is the
        # whole reason this command has to exist.
        template = WorkflowTemplate.objects.create(
            tenant=None, branch=None, document_type="coverage.doc",
            code="standard", name="Coverage",
        )
        WorkflowStage.objects.create(
            template=template, code="approve", label="Approval", kind="APPROVAL",
            order=10, approver_source="ROLE", approver_role_key=ROLE_KEY,
            approver_scope="SCHOOL", advance_rule="ANY", on_rejection="TERMINAL",
            skip_if_no_approvers=False,
        )

    def _staff(self, count):
        """Give the tenant the role and ``count`` active holders."""
        role = make_role(self.tenant, name="Coverage Approver")
        role.key = ROLE_KEY
        role.save(update_fields=["key"])
        for i in range(count):
            make_assignment(
                self.tenant, make_vision_user(email=f"cov{i}@codex.test"), role)
        return role

    def test_a_missing_role_is_reported(self):
        out = _run()
        self.assertIn("no such role", out)
        self.assertIn(ROLE_KEY, out)

    def test_a_role_with_no_holders_is_reported(self):
        self._staff(0)
        out = _run()
        self.assertIn("nobody holds it", out)

    def test_a_role_held_by_one_person_is_not_called_healthy(self):
        """The regression this module was written for.

        One holder passed the old holders-above-zero check, so a tenant that
        could never approve anything was reported as fully staffed.
        """
        self._staff(1)
        out = _run()
        self.assertIn("held by exactly one person", out)
        self.assertIn("cov0@codex.test", out)  # Names who, so it is actionable.
        self.assertNotIn("Every tenant can staff", out)

    def test_two_holders_is_healthy(self):
        """Two is the smallest bench where any requester still leaves an approver."""
        self._staff(2)
        out = _run()
        self.assertIn("Every tenant can staff", out)
        self.assertNotIn("held by exactly one person", out)
        self.assertNotIn("nobody holds it", out)

    def test_create_makes_the_role_but_grants_nobody_anything(self):
        """--create must never invent approval authority, only the empty role."""
        from vs_rbac.models import TenantRoleTemplate

        out = _run("--create")
        self.assertTrue(
            TenantRoleTemplate.objects.filter(tenant=self.tenant, key=ROLE_KEY).exists())
        self.assertIn("nobody assigned yet", out)
        # Created but unstaffed is still a reported problem, not a success.
        self.assertNotIn("Every tenant can staff", out)
