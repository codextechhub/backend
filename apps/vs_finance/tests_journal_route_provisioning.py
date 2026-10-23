"""The hand-journal approval route a tenant's books arrive with.

Provisioning publishes a tenant-scoped ``finance.journal`` route carrying no steps. The
route is a placeholder: it keeps the tenant off any shared platform rule and makes a
hand post approval-undecided (confirm or refuse) rather than approval-free. The posting
behaviour it produces for direct entries and drafts is covered in
``tests_direct_post_confirmation``; this file covers what is published, for whom, and
that it reaches only hand journals.

Corona runs three branches and Single Site runs one, so the route is checked against
both shapes. The route itself is tenant-wide, so no test needs a branch-specific one.
"""
from __future__ import annotations

import io

from django.core.management import call_command

from vs_finance.approvals import (
    approval_required,
    ensure_tenant_journal_template,
)
from vs_finance.constants import DocumentStatus, WF_JOURNAL_DOCUMENT_TYPE
from vs_finance.models import Account, JournalEntry
from vs_finance.provisioning import provision_entity
from vs_finance.receivables import post_invoice
from vs_workflow.models import WorkflowTemplate
from vs_workflow.services.resolution import approval_unconfigured
from vs_workflow.services.roles import ensure_approver_role
from vs_workflow.services.templates import publish_template

from .tests_branch_scope import _FinanceBranchFixture


def _routes(tenant):
    return WorkflowTemplate.all_objects.filter(
        tenant=tenant, document_type=WF_JOURNAL_DOCUMENT_TYPE)


class JournalRouteProvisioningTests(_FinanceBranchFixture):
    """Corona's books are provisioned; Single Site's and Rival's are not."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        provision_entity(cls.books)

    def test_provisioning_publishes_a_tenant_scoped_route_with_no_steps(self):
        route = _routes(self.tenant).get()

        self.assertEqual(route.name, "Journal entry approval")
        self.assertEqual(route.code, "standard")
        self.assertIsNone(route.branch_id)
        self.assertEqual(route.stages.count(), 0)

    def test_provisioning_one_tenant_leaves_every_other_tenant_alone(self):
        self.assertFalse(_routes(self.solo_tenant).exists())
        self.assertFalse(_routes(self.rival_tenant).exists())

    def test_publishing_again_changes_nothing(self):
        route = _routes(self.tenant).get()

        again, created = ensure_tenant_journal_template(self.tenant)
        provision_entity(self.books)

        self.assertFalse(created)
        self.assertEqual(again.pk, route.pk)
        self.assertEqual(_routes(self.tenant).count(), 1)

    def test_a_route_with_a_step_is_left_exactly_as_configured(self):
        """Publishing a stageless payload over it would retire the tenant's own step."""
        ensure_approver_role(self.tenant, "journal-checker")
        publish_template(
            tenant=self.tenant, branch=None, document_type=WF_JOURNAL_DOCUMENT_TYPE,
            code="standard", name="Corona journal checks",
            stages_payload=[{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_source": "ROLE",
                "approver_role_key": "journal-checker", "approver_scope": "SCHOOL",
                "advance_rule": "ANY", "on_rejection": "RETURN_TO_REQUESTER",
                "skip_if_no_approvers": False,
            }],
        )

        route, created = ensure_tenant_journal_template(self.tenant)

        self.assertFalse(created)
        self.assertEqual(route.name, "Corona journal checks")
        self.assertEqual(
            set(route.stages.filter(retired_at__isnull=True).values_list("code", flat=True)),
            {"checker"},
        )
        self.assertFalse(route.stages.filter(retired_at__isnull=False).exists())

    def test_a_tenant_is_required(self):
        with self.assertRaises(ValueError):
            ensure_tenant_journal_template(None)


class ProvisionJournalRoutesCommandTests(_FinanceBranchFixture):
    """The deploy backfill for tenants whose books predate the route."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        provision_entity(cls.books)

    def run_command(self, *args):
        out = io.StringIO()
        call_command("provision_journal_routes", *args, stdout=out)
        return out.getvalue()

    def test_it_publishes_for_a_tenant_without_the_route_and_skips_one_with_it(self):
        route_before = _routes(self.tenant).get()

        output = self.run_command()

        self.assertIn("fin-solo", output)
        self.assertIn("fin-rival", output)
        self.assertNotIn("fin-multi", output)
        self.assertEqual(_routes(self.solo_tenant).get().stages.count(), 0)
        self.assertEqual(_routes(self.rival_tenant).count(), 1)
        self.assertEqual(_routes(self.tenant).get().pk, route_before.pk)
        self.assertEqual(_routes(self.tenant).count(), 1)

    def test_it_publishes_only_the_journal_route(self):
        """Backfilling the refund and claim routes would change older tenants' posting."""
        self.run_command()

        self.assertEqual(
            set(WorkflowTemplate.all_objects.filter(tenant=self.solo_tenant)
                .values_list("document_type", flat=True)),
            {WF_JOURNAL_DOCUMENT_TYPE},
        )

    def test_a_dry_run_reports_and_writes_nothing(self):
        output = self.run_command("--dry-run")

        self.assertIn("fin-solo", output)
        self.assertIn("Dry run", output)
        self.assertFalse(_routes(self.solo_tenant).exists())
        self.assertFalse(_routes(self.rival_tenant).exists())

    def test_running_it_twice_publishes_once(self):
        self.run_command()
        second = self.run_command()

        self.assertNotIn("fin-solo", second)
        self.assertEqual(_routes(self.solo_tenant).count(), 1)


class JournalRouteReachTests(_FinanceBranchFixture):
    """The route governs hand journals and nothing a document raises."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        provision_entity(cls.books)
        provision_entity(cls.solo_books)

    def test_a_hand_journal_is_undecided_not_approval_free(self):
        for books in (self.books, self.solo_books):
            with self.subTest(books=books.code):
                entry = JournalEntry.objects.create(
                    entity=books, date="2026-01-15", narration="Accrual")

                self.assertTrue(approval_unconfigured(entry))
                self.assertFalse(approval_required(entry))

    def test_a_posted_invoice_still_posts_and_its_journal_is_not_held(self):
        """The invoice service posts its own journal and never asks the journal route."""
        shapes = (
            (self.books, self.customer(self.books, "EZE", self.ikeja), self.ikeja),
            (self.solo_books, self.customer(self.solo_books, "SOLO", self.solo_main),
             self.solo_main),
        )
        for books, customer, branch in shapes:
            with self.subTest(books=books.code):
                invoice = self.invoice(books, customer, branch)
                invoice.lines.update(
                    revenue_account=Account.objects.get(entity=books, code="4100"))

                post_invoice(invoice)

                invoice.refresh_from_db()
                self.assertEqual(invoice.status, DocumentStatus.POSTED)
                self.assertIsNotNone(invoice.journal_id)
                self.assertEqual(invoice.journal.status, DocumentStatus.POSTED)
