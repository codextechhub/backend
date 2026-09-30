"""The Finance dashboard answers each reader with what they may see.

Corona runs Ikeja, Lekki and Yaba. The bursar at Ikeja holds the invoice and
receipt keys and a report key, pinned to Ikeja. The proprietor holds the report
key school-wide. A payments clerk holds only a payout key.

The dashboard opens to all three, because each works in the Finance console. What
comes back differs: every figure the bursar gets, ledger figures included, covers
Ikeja's entries only, never one not yet given a branch, and the school's budget and period close
are not sent to him at all. The proprietor gets every block. The clerk gets the
page with no figures in it.
"""
from __future__ import annotations

import datetime

from core.test_utils import TenantAPIClient
from .tests_branch_scope import _FinanceBranchFixture

LEDGER_KPIS = ("cash_position", "payables", "net_income_ytd")


class FinanceDashboardAccessTests(_FinanceBranchFixture):
    """Who may open the dashboard, and which blocks each reader receives."""

    def setUp(self):
        super().setUp()
        e = self.books
        # Each invoice is one line of 100,000 kobo, posted for real so the dated
        # aging the dashboard reads counts it. Lekki carries three.
        self.posted(self.invoice(e, self.customer(e, "CIKJ", self.ikeja), self.ikeja))
        lekki = self.customer(e, "CLEK", self.lekki)
        for _ in range(3):
            self.posted(self.invoice(e, lekki, self.lekki))
        self.posted(self.invoice(e, self.customer(e, "CALL", None), None))

    def posted(self, invoice):
        """Post ``invoice`` through the real service, journal and all.

        The fixture's line books to the 4000 heading, which cannot take a posting,
        so the line is pointed at 4100 first.
        """
        from vs_finance.models import Account, InvoiceLine
        from vs_finance.receivables import post_invoice

        InvoiceLine.objects.filter(invoice=invoice).update(
            revenue_account=Account.objects.get(entity=invoice.entity, code="4100"),
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def client_holding(self, email, *keys, branch=None):
        user = self.grant(
            self.user_for(self.tenant, email), *keys,
            tenant=self.tenant, role_key=f"role-{email}", branch=branch,
        )
        return TenantAPIClient(user=user)

    def dashboard(self, client, *, entity=None):
        entity = entity or self.books
        return client.get(f"/v1/finance/reports/dashboard/?entity={entity.code}")

    # -- who may open it ------------------------------------------------------ #

    def test_a_reader_with_no_finance_or_payments_key_is_refused(self):
        client = self.client_holding("no-finance@corona.test", "procurement.vendor.view")
        self.assertEqual(self.dashboard(client).status_code, 403)

    def test_another_tenants_books_stay_unreachable(self):
        client = self.client_holding("proprietor-x@corona.test", "finance.report.view")
        self.assertEqual(self.dashboard(client, entity=self.rival_books).status_code, 404)

    def test_a_payments_clerk_opens_the_page_and_receives_no_figures(self):
        client = self.client_holding("clerk@corona.test", "payments.payout.view")
        response = self.dashboard(client)

        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertTrue(all(value is None for value in data["kpis"].values()))
        for block in ("revenue_vs_budget", "ar_aging", "trend", "top_overdue",
                      "vendor_due", "close_progress", "recent_journals"):
            self.assertIsNone(data[block], block)

    # -- a branch bursar ------------------------------------------------------ #

    def test_a_branch_bursar_sees_their_branchs_figures_only(self):
        client = self.client_holding(
            "bursar-ikeja@corona.test",
            "finance.invoice.view", "finance.payment.view", "finance.report.view",
            branch=self.ikeja,
        )
        data = self.dashboard(client).data["data"]

        own = 100_000  # Ikeja's alone; never Lekki's, never the unbranched invoice.
        self.assertTrue(data["narrowed"])
        self.assertEqual(data["ar_aging"]["total"]["kobo"], own)
        self.assertEqual(data["kpis"]["receivables"]["value"]["kobo"], own)

    def test_a_branch_bursar_gets_their_own_ledger_but_not_the_school_budget_or_close(self):
        client = self.client_holding(
            "bursar-ledger@corona.test",
            "finance.report.view", "finance.invoice.view", "finance.period.view",
            branch=self.ikeja,
        )
        data = self.dashboard(client).data["data"]

        for kpi in LEDGER_KPIS:
            self.assertIsNotNone(data["kpis"][kpi], kpi)
        # Net income from Ikeja's invoice alone; never Lekki's three or the unbranched one.
        self.assertEqual(data["kpis"]["net_income_ytd"]["value"]["kobo"], 100_000)
        self.assertIsNone(data["revenue_vs_budget"])
        self.assertIsNone(data["close_progress"])

    def test_trend_series_follow_their_own_keys(self):
        client = self.client_holding(
            "invoices-only@corona.test", "finance.invoice.view", branch=self.ikeja,
        )
        trend = self.dashboard(client).data["data"]["trend"]

        self.assertIsNotNone(trend["issued"])
        self.assertIsNone(trend["collected"])

    # -- a school-wide reader ------------------------------------------------- #

    def test_a_school_wide_report_reader_gets_the_ledger_figures(self):
        client = self.client_holding("proprietor@corona.test", "finance.report.view")
        data = self.dashboard(client).data["data"]

        self.assertFalse(data["narrowed"])
        for kpi in (*LEDGER_KPIS, "receivables"):
            self.assertIsNotNone(data["kpis"][kpi], kpi)
        self.assertIsNotNone(data["revenue_vs_budget"])
        # Every branch's invoices count toward a school-wide aging.
        self.assertEqual(data["ar_aging"]["total"]["kobo"], 5 * 100_000)


class FinanceDashboardPlanTests(_FinanceBranchFixture):
    """A block needs the school's plan as well as the reader's key.

    Corona's proprietor holds the report and journal keys school-wide, and the
    journal key sits in a deeper band than reports. Below that band the journals
    screen refuses him, so the dashboard leaves out the recent journals as well;
    at that band he gets them, as the key alone gave him before.
    """

    KEYS = ("finance.report.view", "finance.journal.view")

    def proprietor(self):
        user = self.grant(
            self.user_for(self.tenant, "plan-proprietor@corona.test"), *self.KEYS,
            tenant=self.tenant, role_key="role-plan-proprietor",
        )
        return TenantAPIClient(user=user)

    def blocks(self, depth):
        from vs_config.models import Capability
        from vs_rbac.tests.helpers import put_on_plan

        client = self.proprietor()
        put_on_plan(self.tenant, depth, banded_keys=["finance.journal.view"],
                    band_depth=Capability.Depth.ADVANCED)
        response = client.get(f"/v1/finance/reports/dashboard/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_recent_journals_are_absent_below_their_band(self):
        from vs_config.models import Capability

        data = self.blocks(Capability.Depth.PLUS)
        self.assertIsNone(data["recent_journals"])
        self.assertIsNotNone(data["kpis"]["cash_position"])

    def test_recent_journals_are_present_at_their_band(self):
        from vs_config.models import Capability

        data = self.blocks(Capability.Depth.ADVANCED)
        self.assertIsNotNone(data["recent_journals"])


class ApprovalsWaitingOnYouTests(_FinanceBranchFixture):
    """"Approvals waiting on you" counts every finance document an approver acts on.

    Corona's head of finance approves credit and debit notes. With a credit note
    and a refund both sitting at her stage, the dashboard tells her two things
    wait on her, not one.
    """

    def waiting_stage(self, document_type, document, approver):
        """An in-progress approval of ``document`` whose active stage names ``approver``."""
        from django.contrib.contenttypes.models import ContentType
        from vs_workflow.models import (
            WorkflowInstance, WorkflowStageApprover, WorkflowStageInstance,
        )
        from vs_workflow.services.roles import ensure_approver_role
        from vs_workflow.services.templates import publish_template

        ensure_approver_role(self.tenant, "finance-head")
        template = publish_template(
            tenant=self.tenant, branch=None, document_type=document_type,
            code="standard", name="Head approval",
            stages_payload=[{
                "code": "head", "label": "Head approval", "kind": "APPROVAL",
                "order": 1, "approver_source": "ROLE", "approver_role_key": "finance-head",
                "approver_scope": "SCHOOL", "advance_rule": "ANY",
                "on_rejection": "RETURN_TO_REQUESTER", "skip_if_no_approvers": False,
            }],
        )
        stage = template.stages.get()
        instance = WorkflowInstance.all_objects.create(
            tenant=self.tenant, template=template,
            document_content_type=ContentType.objects.get_for_model(document),
            document_object_id=str(document.pk), document_type=document_type,
            status="IN_PROGRESS", requested_by=approver, current_stage=stage,
        )
        stage_instance = WorkflowStageInstance.objects.create(
            instance=instance, stage=stage, status="ACTIVE",
        )
        WorkflowStageApprover.objects.create(stage_instance=stage_instance, user=approver)

    def test_a_credit_note_waiting_on_her_is_counted_with_the_rest(self):
        from vs_finance.dashboard_blocks import approvals_waiting_on
        from vs_finance.models import CreditNote, Refund

        e = self.books
        head = self.user_for(self.tenant, "finance-head@corona.test")
        customer = self.customer(e, "CNOTE", None)
        note = CreditNote.objects.create(
            entity=e, customer=customer, note_date=datetime.date(2026, 1, 12),
        )
        refund = Refund.objects.create(
            entity=e, customer=customer, refund_date=datetime.date(2026, 1, 12),
        )
        self.waiting_stage("finance.credit_note", note, head)
        self.waiting_stage("finance.refund", refund, head)

        waiting = approvals_waiting_on(e, head)

        self.assertEqual(waiting["total"], 2)
        self.assertIn(
            {"type": "finance.credit_note", "label": "credit or debit note", "count": 1},
            waiting["items"],
        )

    def test_every_finance_approval_type_is_counted(self):
        from vs_finance.dashboard_blocks import APPROVAL_TYPES
        from vs_workflow.handlers.registry import list_registered_handlers

        finance_types = {
            t for t in list_registered_handlers() if t.startswith(("finance.", "payments."))
        }
        self.assertEqual(finance_types - set(APPROVAL_TYPES), set())
