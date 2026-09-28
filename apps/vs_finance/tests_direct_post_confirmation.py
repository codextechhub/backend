"""Journals and expense claims post under the same guard as the adjustments.

Bright Star's books are published with an expense-claim approval route that has
no steps yet, and its administrator may publish a journal route the same way.
An empty route means nobody has decided who approves, not that nothing needs
approving. So the bursar posting a claim or a journal directly is refused until
she confirms it goes out without approval, the confirmation is recorded against
her, and a route that does have steps still sends her to submit instead.
"""
from __future__ import annotations

import datetime
import io

from django.core.management import call_command
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus, PeriodStatus
from vs_finance.models import (
    Account,
    ExpenseClaim,
    FiscalPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
    LedgerEntity,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies

from .tests import _school_finance_requester


class _DirectPostFixture(TestCase):
    """A school with open books and a bursar holding every finance key."""

    def setUp(self):
        from schools.vs_schools.models import School

        call_command("seed_finance_permissions", verbosity=0, stdout=io.StringIO())
        seed_currencies()
        self.school = School.objects.create(
            name="Bright Star School", slug="bright-star-direct", code="BSDIR", status="ACTIVE")
        self.tenant = self.school.tenant
        self.entity = LedgerEntity.objects.create(
            name="Bright Star Books", code="BSDBK", kind=LedgerEntity.Kind.TENANT,
            tenant=self.tenant,
        )
        seed_chart_of_accounts(self.entity)
        year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        self.period = FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        self.bursar = _school_finance_requester(self.school, "bursar-direct@test.com")
        self.client = TenantAPIClient(user=self.bursar)

    def publish_route(self, document_type, *, staged):
        """This school's own route for ``document_type``, with one step or none."""
        from vs_workflow.services.roles import ensure_approver_role
        from vs_workflow.services.templates import publish_template

        stages = []
        if staged:
            ensure_approver_role(self.tenant, "direct-post-checker")
            stages = [{
                "code": "checker", "label": "Checker approval", "kind": "APPROVAL",
                "order": 1, "approver_source": "ROLE",
                "approver_role_key": "direct-post-checker",
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": "RETURN_TO_REQUESTER", "skip_if_no_approvers": False,
            }]
        return publish_template(
            tenant=self.tenant, branch=None, document_type=document_type,
            code="standard", name="Direct post route", stages_payload=stages,
        )

    def recorded(self, entity_type):
        from vs_audit.models import AuditEvent

        return set(AuditEvent.objects.filter(
            action_type="POSTED_WITHOUT_APPROVAL", entity_type=entity_type,
        ).values_list("entity_id", "actor_user_id"))

    def post(self, path, **body):
        return self.client.post(
            f"/v1/finance/{path}?entity={self.entity.code}", body, format="json")


class JournalDirectPostTests(_DirectPostFixture):
    """``/journals/<id>/post/`` against an empty route, a staged one, and none."""

    def draft(self):
        entry = JournalEntry.objects.create(
            entity=self.entity, date=datetime.date(2026, 1, 15), period=self.period,
            narration="Accrual", created_by=self.bursar,
        )
        JournalLine.objects.create(
            entry=entry, line_no=1, debit=50_000, credit=0,
            account=Account.objects.get(entity=self.entity, code="1100"))
        JournalLine.objects.create(
            entry=entry, line_no=2, debit=0, credit=50_000,
            account=Account.objects.get(entity=self.entity, code="4100"))
        return entry

    def test_an_empty_route_refuses_until_confirmed_and_writes_nothing(self):
        self.publish_route("finance.journal", staged=False)
        entry = self.draft()

        refused = self.post(f"journals/{entry.pk}/post/")

        self.assertEqual(refused.status_code, 409, refused.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)
        self.assertEqual(self.recorded("JournalEntry"), set())

    def test_a_confirmed_post_goes_through_and_is_recorded_against_her(self):
        self.publish_route("finance.journal", staged=False)
        entry = self.draft()

        confirmed = self.post(f"journals/{entry.pk}/post/", confirm_without_approval=True)

        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertEqual(self.recorded("JournalEntry"), {(str(entry.pk), self.bursar.pk)})

    def test_a_route_with_steps_still_sends_her_to_submit(self):
        self.publish_route("finance.journal", staged=True)
        entry = self.draft()

        refused = self.post(f"journals/{entry.pk}/post/", confirm_without_approval=True)

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn("approval-gated", str(refused.content))
        entry.refresh_from_db()
        self.assertEqual(entry.status, DocumentStatus.DRAFT)

    def test_no_route_at_all_posts_without_asking(self):
        entry = self.draft()

        posted = self.post(f"journals/{entry.pk}/post/")

        self.assertEqual(posted.status_code, 200, posted.content)
        self.assertEqual(self.recorded("JournalEntry"), set())


class ExpenseClaimDirectPostTests(_DirectPostFixture):
    """``/expense-claims/<id>/post/`` against the empty route a school starts with."""

    def draft(self):
        response = self.client.post(
            f"/v1/finance/expense-claims/?entity={self.entity.code}",
            {
                "claimant_name": "Jane Staff", "claim_date": "2026-01-10",
                "title": "School visit",
                "lines": [{"description": "Taxi", "expense_account": "5300",
                           "quantity": 1, "unit_price": 100_000}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return ExpenseClaim.objects.get(pk=response.json()["data"]["id"])

    def test_the_starting_route_refuses_until_confirmed_and_writes_nothing(self):
        from vs_finance.approvals import ensure_tenant_expense_claim_template

        ensure_tenant_expense_claim_template(self.tenant, with_default_stages=False)
        claim = self.draft()

        refused = self.post(f"expense-claims/{claim.pk}/post/")

        self.assertEqual(refused.status_code, 409, refused.content)
        claim.refresh_from_db()
        self.assertEqual(claim.status, DocumentStatus.DRAFT)
        self.assertIsNone(claim.journal_id)
        self.assertEqual(self.recorded("ExpenseClaim"), set())

    def test_a_confirmed_post_goes_through_and_is_recorded_against_her(self):
        from vs_finance.approvals import ensure_tenant_expense_claim_template

        ensure_tenant_expense_claim_template(self.tenant, with_default_stages=False)
        claim = self.draft()

        confirmed = self.post(
            f"expense-claims/{claim.pk}/post/",
            confirm_without_approval=True, reason="Head agreed")

        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        claim.refresh_from_db()
        self.assertEqual(claim.status, DocumentStatus.POSTED)
        self.assertEqual(self.recorded("ExpenseClaim"), {(str(claim.pk), self.bursar.pk)})

    def test_a_route_with_steps_still_sends_her_to_submit(self):
        self.publish_route("finance.expense_claim", staged=True)
        claim = self.draft()

        refused = self.post(f"expense-claims/{claim.pk}/post/", confirm_without_approval=True)

        self.assertEqual(refused.status_code, 400, refused.content)
        self.assertIn("approval-gated", str(refused.content))
        claim.refresh_from_db()
        self.assertEqual(claim.status, DocumentStatus.DRAFT)


class DirectEntryApprovalRouteTests(_DirectPostFixture):
    """``/direct-entries/`` is a journal, so the journal route governs it too.

    Bright Star's bursar books a ₦50,000 grant. With a journal route that has a
    step, it waits for the checker instead of reaching the books. With an empty
    route she must confirm it goes out unreviewed. With no route, it posts.
    """

    def book(self, **extra):
        return self.post("direct-entries/", **{
            "date": "2026-01-15", "narration": "Grant received",
            "lines": [
                {"account": "1100", "debit": 50_000, "credit": 0},
                {"account": "4100", "debit": 0, "credit": 50_000},
            ],
            **extra,
        })

    def entries(self):
        return JournalEntry.objects.filter(entity=self.entity, narration="Grant received")

    def test_a_route_with_steps_holds_it_for_approval(self):
        self.publish_route("finance.journal", staged=True)

        response = self.book()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("waiting for approval", response.json()["message"])
        self.assertIn("approval", response.json()["data"])
        entry = self.entries().get()
        self.assertEqual(entry.status, DocumentStatus.PENDING_APPROVAL)

    def test_an_empty_route_refuses_until_confirmed_and_writes_nothing(self):
        self.publish_route("finance.journal", staged=False)

        refused = self.book()

        self.assertEqual(refused.status_code, 409, refused.content)
        self.assertEqual(refused.json()["error"]["code"], "APPROVAL_NOT_CONFIGURED")
        self.assertFalse(self.entries().exists())
        self.assertEqual(self.recorded("JournalEntry"), set())

    def test_a_confirmed_entry_posts_and_is_recorded_against_her(self):
        self.publish_route("finance.journal", staged=False)

        confirmed = self.book(confirm_without_approval=True, reason="Proprietor agreed")

        self.assertEqual(confirmed.status_code, 201, confirmed.content)
        entry = self.entries().get()
        self.assertEqual(entry.status, DocumentStatus.POSTED)
        self.assertEqual(self.recorded("JournalEntry"), {(str(entry.pk), self.bursar.pk)})

    def test_no_route_at_all_posts_directly(self):
        response = self.book()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn("posted", response.json()["message"])
        self.assertEqual(self.entries().get().status, DocumentStatus.POSTED)
