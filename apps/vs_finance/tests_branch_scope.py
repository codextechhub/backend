"""A branch-pinned grant must narrow the finance screens, not just open them.

``HasRBACPermission`` deliberately names no branch: it answers "may I open this
screen?" and stops there. The other half - "whose rows, then?" - is
:mod:`vs_rbac.scoping`, and until now :mod:`vs_procurement` was the only module
on the platform that asked it. So a grant of "Bursar at Ikeja" opened the fee
screens and returned Lekki's and Yaba's rows too. The gate held; the narrowing
never happened.

These tests are the end-to-end evidence that it now happens, over real HTTP with
real grants rather than by inspecting a queryset. The rule they exist to protect
is the inclusive one: a row whose branch is NULL is **shared across the school**
and stays visible. Corona publishes one fee structure for all three branches and
leaves its branch empty; a Bursar at Ikeja must still see it. Hiding it would
look like missing data rather than a permission error, which is why every class
below asserts the shared row by name and not merely as part of a set.

Two shapes of school throughout: the three-branch one where the narrowing bites,
and the single-branch one where the dimension should recede and nothing changes.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_finance.models import (
    Account,
    Customer,
    FeeStructure,
    FiscalPeriod,
    FiscalYear,
    FixedAsset,
    Invoice,
    InvoiceLine,
    LedgerEntity,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies


class _FinanceBranchFixture(TestCase):
    """One three-branch school, one single-branch school, and a rival.

    Three branches rather than two: with only two, a predicate that quietly means
    "any branch but the one I asked about" still passes. The rival tenant proves
    the narrowing has not weakened the tenant boundary it sits inside.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        seed_currencies()

        self.school = make_school(slug="fin-multi", name="Corona Group", status="ACTIVE")
        self.tenant = self.school.tenant
        self.ikeja = make_branch(self.school, name="Ikeja Branch")
        self.lekki = make_branch(self.school, name="Lekki Branch", is_main=False)
        self.yaba = make_branch(self.school, name="Yaba Branch", is_main=False)
        self.books = self.build_books("FINMULTI", self.tenant)

        # The other shape of school. One branch is the common case, and a grant
        # pinned to the only branch there must not start hiding the school-wide
        # rows that branch shares the school with.
        self.solo_school = make_school(slug="fin-solo", name="Single Site", status="ACTIVE")
        self.solo_tenant = self.solo_school.tenant
        self.solo_main = make_branch(self.solo_school, name="Main Branch")
        self.solo_books = self.build_books("FINSOLO", self.solo_tenant)

        self.rival_school = make_school(slug="fin-rival", name="Rival Group", status="ACTIVE")
        self.rival_tenant = self.rival_school.tenant
        self.rival_branch = make_branch(self.rival_school, name="Ikeja Branch")
        self.rival_books = self.build_books("FINRIVAL", self.rival_tenant)

    # -- books ---------------------------------------------------------------- #

    def build_books(self, code, tenant):
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
        )
        return entity

    # -- rows ----------------------------------------------------------------- #

    def customer(self, entity, code, branch):
        return Customer.objects.create(
            entity=entity, code=code, name=f"Parent {code}", branch=branch,
            receivable_account=Account.objects.get(entity=entity, code="1200"),
        )

    def invoice(self, entity, customer, branch):
        inv = Invoice.objects.create(
            entity=entity, customer=customer, branch=branch,
            invoice_date=datetime.date(2026, 1, 10),
            due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=inv, line_no=1, quantity=1, unit_price=100_000,
            revenue_account=Account.objects.get(entity=entity, code="4000"),
        )
        return inv

    def fee_structure(self, entity, code, branch):
        return FeeStructure.objects.create(
            entity=entity, code=code, name=f"Fees {code}", branch=branch,
        )

    def fixed_asset(self, entity, name, branch):
        return FixedAsset.objects.create(
            entity=entity, name=name, branch=branch,
            acquisition_date=datetime.date(2026, 1, 5), useful_life_months=60,
        )

    # -- people --------------------------------------------------------------- #

    def user_for(self, tenant, email):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=tenant, branch=None,
            status="ACTIVE", first_name="Fin", last_name="Tester",
        )

    def grant(self, user, *keys, tenant, role_key, branch=None):
        """A real RBAC grant, optionally pinned to one branch.

        Through the registry rather than by patching the gate: whether a
        branch-pinned grant opens the screen at all is part of what makes the
        narrowing meaningful, so the grant has to be the real thing.
        """
        from vs_rbac.models import (
            Permission, PermissionAction, PermissionModule, PermissionResource,
            TenantRolePermission, TenantRoleTemplate, TenantUserRoleAssignment,
        )
        from vs_rbac.tests.helpers import scope_for_key

        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=tenant, key=role_key, defaults={"name": role_key, "status": "ACTIVE"},
        )
        for key in keys:
            module_name, resource_name, action_name = key.split(".")
            module, _ = PermissionModule.objects.get_or_create(name=module_name)
            resource, _ = PermissionResource.objects.get_or_create(
                module=module, name=resource_name,
            )
            action, _ = PermissionAction.objects.get_or_create(name=action_name)
            permission, _ = Permission.objects.get_or_create(
                key=key,
                defaults={"module": module, "resource": resource, "action": action,
                          "scope": scope_for_key(key)},
            )
            TenantRolePermission.objects.get_or_create(
                role=role, permission=permission, defaults={"granted": True},
            )
        TenantUserRoleAssignment.objects.get_or_create(
            tenant=tenant, user=user, role=role,
            defaults={"assignment_status": "ACTIVE", "branch": branch},
        )
        return user

    READ_KEYS = (
        "finance.invoice.view", "finance.customer.view", "finance.feestructure.view",
        "finance.fixedasset.view", "finance.journal.view",
    )

    def reader(self, tenant, email, role_key, *, branch=None):
        user = self.grant(
            self.user_for(tenant, email), *self.READ_KEYS,
            tenant=tenant, role_key=role_key, branch=branch,
        )
        return TenantAPIClient(user=user)

    # -- calling -------------------------------------------------------------- #

    def ids(self, client, path, entity, *, key="id"):
        response = client.get(f"/v1/finance/{path}?entity={entity.code}")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("data") or []
        return {row[key] for row in rows}


class BranchPinnedReadsTests(_FinanceBranchFixture):
    """The headline defect, one endpoint family at a time."""

    def setUp(self):
        super().setUp()
        e = self.books
        self.cust_ikeja = self.customer(e, "CIKJ", self.ikeja)
        self.cust_lekki = self.customer(e, "CLEK", self.lekki)
        self.cust_shared = self.customer(e, "CALL", None)

        self.inv_ikeja = self.invoice(e, self.cust_ikeja, self.ikeja)
        self.inv_lekki = self.invoice(e, self.cust_lekki, self.lekki)
        self.inv_yaba = self.invoice(e, self.cust_shared, self.yaba)
        self.inv_shared = self.invoice(e, self.cust_shared, None)

        self.fee_ikeja = self.fee_structure(e, "FIKJ", self.ikeja)
        self.fee_lekki = self.fee_structure(e, "FLEK", self.lekki)
        self.fee_shared = self.fee_structure(e, "FALL", None)

        self.asset_ikeja = self.fixed_asset(e, "Ikeja bus", self.ikeja)
        self.asset_lekki = self.fixed_asset(e, "Lekki bus", self.lekki)
        self.asset_shared = self.fixed_asset(e, "Group minibus", None)

        self.bursar = self.reader(
            self.tenant, "bursar-ikeja@fin.test", "fin-ikeja", branch=self.ikeja,
        )

    def test_the_fee_screen_shows_ikejas_fees_and_the_school_wide_one(self):
        """The brief's own example, and the reason the rule is inclusive.

        Corona publishes one fee structure for all three branches with no branch
        set. A Bursar at Ikeja must see Ikeja's fees *and* that one, and neither
        Lekki's nor Yaba's.
        """
        seen = self.ids(self.bursar, "fee-structures/", self.books)

        self.assertIn(self.fee_shared.id, seen)
        self.assertIn(self.fee_ikeja.id, seen)
        self.assertNotIn(self.fee_lekki.id, seen)

    def test_the_invoice_list_narrows_and_keeps_the_school_wide_invoice(self):
        seen = self.ids(self.bursar, "invoices/", self.books)

        self.assertEqual(seen, {self.inv_ikeja.id, self.inv_shared.id})
        self.assertNotIn(self.inv_lekki.id, seen)
        self.assertNotIn(self.inv_yaba.id, seen)

    def test_the_customer_list_narrows_and_keeps_the_school_wide_payer(self):
        seen = self.ids(self.bursar, "customers/", self.books)

        self.assertIn(self.cust_shared.id, seen)
        self.assertIn(self.cust_ikeja.id, seen)
        self.assertNotIn(self.cust_lekki.id, seen)

    def test_an_operational_list_narrows_too(self):
        """Fixed assets reach the API through a different base class and paginator.

        Finance's lists are not built on one shared list view, so a narrowing that
        works on the AR screens proves nothing about the operational ones.
        """
        seen = self.ids(self.bursar, "fixed-assets/", self.books)

        self.assertIn(self.asset_shared.id, seen)
        self.assertIn(self.asset_ikeja.id, seen)
        self.assertNotIn(self.asset_lekki.id, seen)

    def test_the_kpi_header_agrees_with_the_list_below_it(self):
        """A header counting rows the list does not show is its own kind of leak.

        It reports another branch's money to somebody who cannot open a single
        document behind the number, which is both a disclosure and a support
        ticket nobody can answer.
        """
        response = self.bursar.get(
            f"/v1/finance/invoices/summary/?entity={self.books.code}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        listed = self.ids(self.bursar, "invoices/", self.books)

        self.assertEqual(response.data["data"]["totals"]["count"], len(listed))

    def test_another_branchs_invoice_is_not_readable_by_guessing_its_id(self):
        """Narrowing a list while leaving detail open moves the leak, it does not close it."""
        response = self.bursar.get(
            f"/v1/finance/invoices/{self.inv_lekki.pk}/?entity={self.books.code}",
        )

        self.assertEqual(response.status_code, 404, response.data)

    def test_the_school_wide_invoice_is_readable_by_id(self):
        """The other half of the same rule: shared rows stay reachable, not just listed."""
        response = self.bursar.get(
            f"/v1/finance/invoices/{self.inv_shared.pk}/?entity={self.books.code}",
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_a_caller_pinned_to_two_branches_sees_both(self):
        """``User.branch`` could never express this; a set of grants can."""
        user = self.user_for(self.tenant, "roving@fin.test")
        self.grant(user, *self.READ_KEYS, tenant=self.tenant,
                   role_key="fin-ikeja-2", branch=self.ikeja)
        self.grant(user, *self.READ_KEYS, tenant=self.tenant,
                   role_key="fin-lekki-2", branch=self.lekki)
        client = TenantAPIClient(user=user)

        seen = self.ids(client, "invoices/", self.books)

        self.assertEqual(
            seen, {self.inv_ikeja.id, self.inv_lekki.id, self.inv_shared.id},
        )
        self.assertNotIn(self.inv_yaba.id, seen)


class WholeTenantReadsTests(_FinanceBranchFixture):
    """The common case, which must not regress by a single row.

    A null branch became normal for school users in ``a4916e9``, and
    ``visible_branch_ids`` answers "the whole tenant" for such a caller. Everyone
    working today holds their access this way.
    """

    def setUp(self):
        super().setUp()
        e = self.books
        self.cust = self.customer(e, "CANY", None)
        self.rows = {
            self.invoice(e, self.cust, self.ikeja).id,
            self.invoice(e, self.cust, self.lekki).id,
            self.invoice(e, self.cust, self.yaba).id,
            self.invoice(e, self.cust, None).id,
        }
        self.hq = self.reader(self.tenant, "hq@fin.test", "fin-hq")

    def test_a_whole_tenant_caller_sees_every_branch_and_the_shared_rows(self):
        self.assertEqual(self.ids(self.hq, "invoices/", self.books), self.rows)

    def test_every_narrowed_list_is_a_subset_of_what_hq_sees(self):
        """The property that fails for any way of widening, thought of or not."""
        bursar = self.reader(
            self.tenant, "sub-bursar@fin.test", "fin-sub", branch=self.lekki,
        )

        self.assertLess(self.ids(bursar, "invoices/", self.books), self.rows)


class SingleBranchSchoolTests(_FinanceBranchFixture):
    """One branch: the dimension recedes and nothing is hidden.

    A single-branch test proves nothing about a multi-branch school, and the
    reverse is equally true - this is the shape most schools are, and the one
    where an over-eager exclusive predicate would silently empty the screens.
    """

    def test_a_grant_pinned_to_the_only_branch_still_sees_the_school_wide_rows(self):
        e = self.solo_books
        cust = self.customer(e, "SOLO", None)
        at_main = self.invoice(e, cust, self.solo_main)
        school_wide = self.invoice(e, cust, None)
        client = self.reader(
            self.solo_tenant, "solo@fin.test", "fin-solo-role", branch=self.solo_main,
        )

        seen = self.ids(client, "invoices/", e)

        self.assertEqual(seen, {at_main.id, school_wide.id})

    def test_an_unbound_caller_in_a_single_branch_school_sees_everything(self):
        e = self.solo_books
        cust = self.customer(e, "SOLO2", None)
        rows = {
            self.invoice(e, cust, self.solo_main).id,
            self.invoice(e, cust, None).id,
        }
        client = self.reader(self.solo_tenant, "solo-hq@fin.test", "fin-solo-hq")

        self.assertEqual(self.ids(client, "invoices/", e), rows)


class TenantIsolationUnchangedTests(_FinanceBranchFixture):
    """Branch narrows *inside* a tenant and must not have weakened the boundary."""

    def test_a_branch_pinned_caller_cannot_reach_a_rival_schools_books(self):
        rival_cust = self.customer(self.rival_books, "RIV", self.rival_branch)
        self.invoice(self.rival_books, rival_cust, self.rival_branch)
        bursar = self.reader(
            self.tenant, "iso-bursar@fin.test", "fin-iso", branch=self.ikeja,
        )

        response = bursar.get(f"/v1/finance/invoices/?entity={self.rival_books.code}")

        # Unknown and forbidden entities are reported the same way, so the code
        # cannot be used to discover which entities exist.
        self.assertEqual(response.status_code, 404, getattr(response, "data", None))

    def test_a_rival_branch_id_matching_ours_confers_nothing(self):
        """Grants resolve against the caller's own tenant, never by branch id alone.

        Entity resolution is what refuses this, and it refuses it before the
        branch predicate is ever built - which is the point. Branch narrowing was
        added *inside* that boundary and is not load-bearing for it.
        """
        mine = self.customer(self.books, "MINE", self.ikeja)
        inv = self.invoice(self.books, mine, self.ikeja)
        outsider = self.reader(
            self.rival_tenant, "outsider@fin.test", "fin-out", branch=self.rival_branch,
        )

        response = outsider.get(f"/v1/finance/invoices/?entity={self.books.code}")

        self.assertEqual(response.status_code, 404, getattr(response, "data", None))
        # Not merely absent from a payload - there is no payload to be absent from.
        self.assertNotIn("data", response.data)
        self.assertTrue(Invoice.objects.filter(pk=inv.pk).exists())


class EveryBranchBearingListNarrowsTests(_FinanceBranchFixture):
    """One assertion, applied to every finance list that carries a branch column.

    The endpoints below do not share a base class, a paginator or a queryset
    helper - finance grew three list conventions over time - so a narrowing proved
    on the invoice screen proves nothing about the payroll one. Rather than a
    hand-written test per screen, the same three rows are built for each model and
    the same question asked: does an Ikeja-pinned caller get Ikeja's row and the
    school-wide row, and not Lekki's?

    A screen added later that forgets the narrowing does not fail here - it simply
    is not listed - so this is a floor, not a proof of completeness. The floor is
    still worth having: it is what catches a narrowing removed from an existing
    screen by a refactor.
    """

    #: (url path, permission key, builder attribute)
    SCREENS = (
        ("journals/", "finance.journal.view", "_journal"),
        ("credit-notes/", "finance.creditnote.view", "_credit_note"),
        ("refunds/", "finance.refund.view", "_refund"),
        ("write-offs/", "finance.writeoff.view", "_write_off"),
        ("concessions/", "finance.concession.view", "_concession"),
        ("payment-plans/", "finance.paymentplan.view", "_payment_plan"),
        ("dunning-notices/", "finance.dunning.view", "_dunning_notice"),
        ("expense-claims/", "finance.expenseclaim.view", "_expense_claim"),
        ("payroll-runs/", "finance.payrollrun.view", "_payroll_run"),
        ("petty-cash-funds/", "finance.pettycash.view", "_petty_cash_fund"),
        ("bank-accounts/", "finance.bankaccount.view", "_bank_account"),
        ("fixed-assets/", "finance.fixedasset.view", "_fixed_asset_row"),
    )

    JAN = datetime.date(2026, 1, 12)

    # -- one builder per model, each taking (entity, branch, tag) --------------- #

    def _journal(self, e, branch, tag):
        from vs_finance.models import JournalEntry

        return JournalEntry.objects.create(
            entity=e, branch=branch, date=self.JAN, narration=f"J{tag}",
        )

    def _payer(self, e, tag):
        return self.customer(e, f"P{tag}"[:12], None)

    def _bill(self, e, tag):
        return self.invoice(e, self._payer(e, tag), None)

    def _credit_note(self, e, branch, tag):
        from vs_finance.models import CreditNote

        return CreditNote.objects.create(
            entity=e, branch=branch, customer=self._payer(e, tag), note_date=self.JAN,
        )

    def _refund(self, e, branch, tag):
        from vs_finance.models import Refund

        return Refund.objects.create(
            entity=e, branch=branch, customer=self._payer(e, tag), refund_date=self.JAN,
        )

    def _write_off(self, e, branch, tag):
        from vs_finance.models import WriteOffRequest

        return WriteOffRequest.objects.create(
            entity=e, branch=branch, invoice=self._bill(e, tag),
        )

    def _concession(self, e, branch, tag):
        from vs_finance.models import Concession

        bill = self._bill(e, tag)
        return Concession.objects.create(
            entity=e, branch=branch, customer=bill.customer, invoice=bill,
            concession_date=self.JAN,
        )

    def _payment_plan(self, e, branch, tag):
        from vs_finance.models import PaymentPlan

        return PaymentPlan.objects.create(
            entity=e, branch=branch, customer=self._payer(e, tag), start_date=self.JAN,
        )

    def _dunning_notice(self, e, branch, tag):
        from vs_finance.models import DunningNotice

        bill = self._bill(e, tag)
        return DunningNotice.objects.create(
            entity=e, branch=branch, customer=bill.customer, invoice=bill,
            level=1, notice_date=self.JAN,
        )

    def _expense_claim(self, e, branch, tag):
        from vs_finance.models import ExpenseClaim

        return ExpenseClaim.objects.create(entity=e, branch=branch, claim_date=self.JAN)

    def _payroll_run(self, e, branch, tag):
        from vs_finance.models import PayrollRun

        return PayrollRun.objects.create(entity=e, branch=branch, pay_date=self.JAN)

    def _cash_account(self, e, tag):
        """A distinct GL account per fund/bank row: both hold a OneToOne to one."""
        return Account.objects.create(
            entity=e, code=f"11{tag}", name=f"Cash {tag}",
            account_type=Account.objects.get(entity=e, code="1000").account_type,
            is_postable=True,
        )

    def _petty_cash_fund(self, e, branch, tag):
        from vs_finance.models import PettyCashFund

        return PettyCashFund.objects.create(
            entity=e, branch=branch, name=f"Float {tag}",
            gl_account=self._cash_account(e, tag),
        )

    def _bank_account(self, e, branch, tag):
        from vs_finance.models import BankAccount

        return BankAccount.objects.create(
            entity=e, branch=branch, name=f"Bank {tag}",
            gl_account=self._cash_account(e, tag),
        )

    def _fixed_asset_row(self, e, branch, tag):
        return self.fixed_asset(e, f"Asset {tag}", branch)

    # -- the sweep ------------------------------------------------------------- #

    def test_every_listed_screen_narrows_and_keeps_the_school_wide_row(self):
        for index, (path, key, builder) in enumerate(self.SCREENS):
            with self.subTest(screen=path):
                e = self.books
                make = getattr(self, builder)
                # The screen's position, not a hash of its path. `hash()` on a
                # string is salted per process, and folding twelve paths into
                # 900 buckets collided about one run in fourteen - two screens
                # drawing the same tag, the second `user_for` failing on a
                # duplicate email, and a green suite failing for a reason that
                # had nothing to do with branch scoping. The index is unique by
                # construction and the tag keeps its three-digit shape, which
                # `_cash_account` builds a GL code from.
                tag = str(100 + index)
                at_ikeja = make(e, self.ikeja, f"{tag}I")
                at_lekki = make(e, self.lekki, f"{tag}L")
                shared = make(e, None, f"{tag}S")

                user = self.grant(
                    self.user_for(self.tenant, f"sweep-{tag}@fin.test"), key,
                    tenant=self.tenant, role_key=f"sweep-{tag}", branch=self.ikeja,
                )
                seen = self.ids(TenantAPIClient(user=user), path, e)

                self.assertIn(shared.pk, seen, f"{path} hid the school-wide row")
                self.assertIn(at_ikeja.pk, seen, f"{path} hid the caller's own row")
                self.assertNotIn(at_lekki.pk, seen, f"{path} leaked another branch")

                # Without this the sweep would also pass on a screen that returns
                # nothing at all, or one whose rows never reached the list: the
                # unbound caller establishes that all three rows *are* listable
                # and that the branch grant is what removed one of them.
                hq = self.grant(
                    self.user_for(self.tenant, f"sweep-hq-{tag}@fin.test"), key,
                    tenant=self.tenant, role_key=f"sweep-hq-{tag}",
                )
                everything = self.ids(TenantAPIClient(user=hq), path, e)
                self.assertEqual(
                    {at_ikeja.pk, at_lekki.pk, shared.pk} - everything, set(),
                    f"{path} did not list all three rows for an unbound caller",
                )
