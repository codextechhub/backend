"""A branch bursar reads their branch's part of a doubtful-debt provision run.

A provision run is raised for every branch at once and names no branch itself,
so an exclusive transaction read would hide it from everyone bound to a branch.
Corona runs Ikeja, Lekki and Yaba. Its December run provides N2,500 against the
Eze family's debt at Ikeja and N1,800 against the Bellos' at Lekki; Yaba owes
nothing old enough to need an allowance. Mrs Bello keeps Lekki's books: she sees
the run, with Lekki's line and Lekki's totals alone. Mr Ade at Ikeja sees Ikeja's
part. Mrs Yusuf at Yaba has no line in the run and does not see it. Mr Okafor
covers the whole school and sees the run as it is. Raising, submitting and
posting stay with a whole-school reader. Harbour, with one branch, reads the run
whole whichever way its bursar's grant is written.
"""
from __future__ import annotations

import datetime
import json

from core.test_utils import TenantAPIClient
from vs_finance.models import Invoice, InvoiceLine

from .tests_accruals import D, _AccrualFixture
from .tests_branch_scope import _FinanceBranchFixture

KEYS = (
    "finance.provision.view", "finance.provision.create", "finance.provision.submit",
    "finance.provision.post",
)


class ProvisionBranchReachTests(_AccrualFixture):
    """The provision list and detail show a branch-bound reader their branches' part."""

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import Account
        from vs_finance.provisions import post_provision, prepare_provision
        from vs_finance.receivables import post_invoice
        from vs_rbac.tests.helpers import make_branch

        super().setUpTestData()
        cls.yaba = make_branch(cls.group, name="Yaba Branch", is_main=False)

        def owe(customer, amount, due):
            books = customer.entity
            invoice = Invoice.objects.create(
                entity=books, customer=customer, branch=customer.branch,
                invoice_date=due - datetime.timedelta(days=5), due_date=due,
            )
            InvoiceLine.objects.create(
                invoice=invoice, line_no=1, quantity=1, unit_price=amount,
                revenue_account=Account.objects.get(entity=books, code="4100"),
            )
            post_invoice(invoice)

        owe(cls.eze, 1_000_000, D(2026, 1, 10))   # 355 days at the run: 25%.
        owe(cls.bello, 720_000, D(2026, 1, 6))    # 359 days at the run: 25%.
        owe(cls.ada, 400_000, D(2026, 1, 10))     # Harbour, 355 days: 25%.
        cls.december = post_provision(prepare_provision(cls.books, as_of=D(2026, 12, 31)))
        cls.solo_run = prepare_provision(cls.solo_books, as_of=D(2026, 12, 31))

        def bursar(tenant, email, role, branch=None):
            return _FinanceBranchFixture.grant(
                _FinanceBranchFixture.user_for(tenant, email), *KEYS,
                tenant=tenant, role_key=role, branch=branch)

        cls.okafor = bursar(cls.tenant, "okafor@reach.test", "reach-whole")
        cls.mrs_bello = bursar(cls.tenant, "bello@reach.test", "reach-lekki", cls.lekki)
        cls.mr_ade = bursar(cls.tenant, "ade@reach.test", "reach-ikeja", cls.ikeja)
        cls.mrs_yusuf = bursar(cls.tenant, "yusuf@reach.test", "reach-yaba", cls.yaba)
        cls.harbour_whole = bursar(cls.solo_tenant, "whole@harbour.test", "reach-hb")
        cls.harbour_main = bursar(
            cls.solo_tenant, "main@harbour.test", "reach-hb-main", cls.solo_main)

    def url(self, path, books=None):
        return f"/v1/finance/{path}?entity={(books or self.books).code}"

    def read(self, user, path, books=None, **params):
        client = TenantAPIClient(user=user)
        query = "".join(f"&{key}={value}" for key, value in params.items())
        return client.get(self.url(path, books) + query)

    def listed(self, user, books=None, **params):
        response = self.read(user, "provisions/", books, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def detail(self, user, run=None, books=None):
        run = run or self.december
        response = self.read(user, f"provisions/{run.pk}/", books)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def line(self, branch):
        return self.december.lines.get(branch=branch)

    # -- the whole-school reader ------------------------------------------- #

    def test_a_whole_school_reader_sees_the_run_whole(self):
        (row,) = self.listed(self.okafor)
        whole = self.detail(self.okafor)
        for data in (row, whole):
            self.assertFalse(data["partial_view"])
            self.assertEqual(
                {line["branch_id"] for line in data["lines"]}, {self.ikeja.pk, self.lekki.pk})
            self.assertEqual(data["required_total"], 430_000)
            self.assertIs(data["approval_required"], False)
            self.assertEqual(data["movement_total"], 430_000)

    # -- branch-bound readers ---------------------------------------------- #

    def test_the_lekki_bursar_sees_only_lekkis_line_and_lekkis_totals(self):
        (row,) = self.listed(self.mrs_bello)
        data = self.detail(self.mrs_bello)
        lekki = self.line(self.lekki)
        for view in (row, data):
            self.assertEqual(view["id"], self.december.pk)
            self.assertTrue(view["partial_view"])
            (only,) = view["lines"]
            self.assertEqual(only["branch_id"], self.lekki.pk)
            self.assertEqual(only["journal_id"], lekki.journal_id)
            self.assertEqual(only["bands"], {"180": {"owed": 720_000, "required": 180_000}})
            self.assertEqual(view["required_total"], 180_000)
            self.assertEqual(view["movement_total"], 180_000)

    def test_nothing_of_ikejas_part_reaches_the_lekki_bursar(self):
        ikeja = self.line(self.ikeja)
        (row,) = self.listed(self.mrs_bello)
        for view in (row, self.detail(self.mrs_bello)):
            text = json.dumps(view, default=str)
            self.assertNotIn("Ikeja", text)
            self.assertNotIn("250000", text)
            self.assertNotIn("430000", text)
            self.assertNotIn(f'"journal_id": {ikeja.journal_id}', text)
            self.assertIsNone(view["approval_required"])

    def test_the_ikeja_bursar_sees_only_ikejas_part(self):
        data = self.detail(self.mr_ade)
        (only,) = data["lines"]
        self.assertEqual(only["branch_id"], self.ikeja.pk)
        self.assertEqual(only["journal_id"], self.line(self.ikeja).journal_id)
        self.assertEqual((data["required_total"], data["movement_total"]), (250_000, 250_000))
        self.assertTrue(data["partial_view"])

    def test_a_bursar_whose_branch_has_no_line_sees_no_run(self):
        self.assertEqual(self.listed(self.mrs_yusuf), [])
        response = self.read(self.mrs_yusuf, f"provisions/{self.december.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_the_branch_filter_still_keeps_runs_with_a_line_for_the_branch(self):
        self.assertEqual(len(self.listed(self.okafor, branch=self.lekki.pk)), 1)
        self.assertEqual(self.listed(self.okafor, branch=self.yaba.pk), [])
        (row,) = self.listed(self.mrs_bello, branch=self.lekki.pk)
        self.assertEqual([line["branch_id"] for line in row["lines"]], [self.lekki.pk])
        refused = self.read(self.mrs_bello, "provisions/", branch=self.ikeja.pk)
        self.assertEqual(refused.status_code, 400)

    # -- writes ------------------------------------------------------------- #

    def test_a_branch_bound_bursar_cannot_raise_submit_or_post_a_run(self):
        from vs_finance.provisions import prepare_provision

        draft = prepare_provision(self.books, as_of=D(2026, 12, 31))
        client = TenantAPIClient(user=self.mrs_bello)
        attempts = (
            ("provisions/", {"as_of": "2026-12-31"}),
            (f"provisions/{draft.pk}/submit/", {}),
            (f"provisions/{draft.pk}/post/",
             {"confirm_without_approval": True, "reason": "Year end"}),
        )
        for path, body in attempts:
            with self.subTest(path=path):
                response = client.post(self.url(path), body, format="json")
                self.assertEqual(response.status_code, 403, response.data)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "DRAFT")

    # -- other tenants and one-branch schools ------------------------------ #

    def test_another_tenants_bursar_cannot_reach_the_run(self):
        response = self.read(
            self.harbour_whole, f"provisions/{self.december.pk}/", books=self.solo_books)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.listed(self.harbour_whole, books=self.solo_books)[0]["id"],
                         self.solo_run.pk)

    def test_a_one_branch_school_reads_its_run_whole_however_the_grant_is_written(self):
        for user in (self.harbour_whole, self.harbour_main):
            with self.subTest(user=user.email):
                data = self.detail(user, run=self.solo_run, books=self.solo_books)
                self.assertFalse(data["partial_view"])
                (line,) = data["lines"]
                self.assertEqual(line["branch_id"], self.solo_main.pk)
                self.assertEqual(data["required_total"], 100_000)
