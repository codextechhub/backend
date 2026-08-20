"""What branch finance puts **on** a row, and where it gets it from.

``tests_branch_scope`` proves the read half: a branch-pinned grant narrows what
comes back. That half was latent, because nothing in finance ever stamped a
branch on anything it created - every document reached the database with
``branch = NULL``, NULL means shared across the school, and so everybody kept
seeing everything. These tests are the other half.

There are only two ways a finance row may acquire a branch, and the split is the
whole design:

* a row that **starts** a chain captures the branch its creator works in
  (``_raised_branch``);
* a row that **continues** a chain takes the branch from the row it continues and
  from nothing else (``_inherited_branch_id``) - a receipt is its customer's
  branch, a credit note is its invoice's, a voucher is its float's.

The case worth arguing about is the caller entitled to *several* branches who
names none. Finance asks them, for anything that records something that happened
somewhere - Mrs Adebayo covers Ikeja and Lekki, and an invoice she raises was
raised for one of them; filing it school-wide would leave Yaba's bursar reading
that family's fee debt for the life of the row, with nothing later in the chain
able to narrow it again. Three kinds of row take the opposite reading and say why
at their own call site: a fee template, a bank account and a payroll run are all
things a school genuinely publishes once for everybody.

Two shapes of school throughout, because a single-branch test proves nothing about
a multi-branch one, and a rival tenant because none of this may weaken the tenant
boundary it sits inside.
"""
from __future__ import annotations

import datetime

from vs_finance.models import (
    Account,
    BankAccount,
    Customer,
    FeeStructure,
    Invoice,
    Payment,
    PayrollRun,
)

from .tests_branch_scope import _FinanceBranchFixture


class _WriteFixture(_FinanceBranchFixture):
    """The read fixture plus the grants and payloads the write half needs."""

    WRITE_KEYS = (
        "finance.customer.create", "finance.customer.view",
        "finance.invoice.create", "finance.invoice.view",
        "finance.feestructure.create", "finance.feestructure.view",
        "finance.payment.create",
        "finance.bankaccount.create", "finance.bankaccount.view",
        "finance.payrollrun.create", "finance.payrollrun.view",
    )

    def writer(self, tenant, email, role_key, *, branches=()):
        """A caller who may create, pinned to zero, one or several branches.

        Several branches means several grants: an assignment carries one branch,
        so "covers Ikeja and Lekki" is two rows and not one. That is the shape the
        ambiguous case actually arrives in, so the tests build it that way rather
        than by stubbing the resolver.
        """
        from core.test_utils import TenantAPIClient

        user = self.user_for(tenant, email)
        if not branches:
            self.grant(user, *self.WRITE_KEYS, tenant=tenant, role_key=role_key)
        else:
            for index, branch in enumerate(branches):
                self.grant(
                    user, *self.WRITE_KEYS, tenant=tenant,
                    role_key=f"{role_key}-{index}", branch=branch,
                )
        return TenantAPIClient(user=user)

    def post(self, client, path, entity, body):
        return client.post(
            f"/v1/finance/{path}?entity={entity.code}", body, format="json",
        )

    # -- payloads -------------------------------------------------------------- #

    def customer_body(self, code, **extra):
        return {
            "code": code, "name": f"Parent {code}",
            "billing_email": f"{code.lower()}@example.com",
            "billing_phone": "08030000000",
            **extra,
        }

    def fee_body(self, code, **extra):
        return {
            "code": code, "name": f"Fees {code}",
            "items": [{"description": "Tuition", "revenue_account": "4100",
                       "amount": 300_000}],
            **extra,
        }

    def bank_body(self, name, entity, **extra):
        return {
            "name": name,
            "gl_account": Account.objects.get(entity=entity, code="1100").code,
            **extra,
        }

    def payroll_body(self, **extra):
        return {
            "pay_date": "2026-01-25",
            "lines": [{"employee_name": "A Teacher", "gross_amount": 500_000}],
            **extra,
        }

    def invoice_body(self, customer, **extra):
        return {
            "customer": customer.code,
            "invoice_date": "2026-01-10",
            "post": False,
            "lines": [{
                "revenue_account": "4100",
                "unit_price": 100_000,
            }],
            **extra,
        }


class RaisedBranchTests(_WriteFixture):
    """Rows that start a chain take the branch their creator works in."""

    def test_a_caller_pinned_to_one_branch_stamps_it_without_asking(self):
        ikeja = self.writer(
            self.tenant, "raise-ikeja@fin.test", "w-ikeja", branches=[self.ikeja],
        )
        response = self.post(ikeja, "customers/", self.books, self.customer_body("RB1"))

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Customer.objects.get(entity=self.books, code="RB1").branch_id,
            self.ikeja.pk,
        )

    def test_an_unbound_caller_who_names_nothing_writes_a_school_wide_row(self):
        """NULL here is a real answer, not missing data, so it must stay reachable.

        The head-office bursar who is not pinned anywhere is how most of Corona's
        finance is run today, and a customer she onboards without naming a site is
        genuinely the school's. Nothing about the write half may turn that into a
        400.
        """
        hq = self.writer(self.tenant, "raise-hq@fin.test", "w-hq")
        response = self.post(hq, "customers/", self.books, self.customer_body("RB2"))

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(
            Customer.objects.get(entity=self.books, code="RB2").branch_id,
        )

    def test_an_unbound_caller_may_name_any_branch_in_the_tenant(self):
        hq = self.writer(self.tenant, "raise-hq2@fin.test", "w-hq2")
        response = self.post(
            hq, "customers/", self.books,
            self.customer_body("RB3", branch=self.yaba.pk),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Customer.objects.get(entity=self.books, code="RB3").branch_id,
            self.yaba.pk,
        )

    def test_a_caller_covering_two_branches_is_asked_which_one(self):
        """The decision this whole piece turns on, stated as a test.

        Mrs Adebayo is Bursar at Ikeja *and* at Lekki. She onboards a family and
        names no branch. Answering NULL would file that family school-wide, and
        school-wide is permanent: Yaba's bursar would read their phone number and
        fee debt for as long as the row exists, and no later step revisits it. So
        she is asked, once, at the only moment anybody knows the answer.
        """
        both = self.writer(
            self.tenant, "raise-both@fin.test", "w-both",
            branches=[self.ikeja, self.lekki],
        )
        response = self.post(both, "customers/", self.books, self.customer_body("RB4"))

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("branch", response.data["error"]["detail"])
        self.assertFalse(Customer.objects.filter(entity=self.books, code="RB4").exists())

    def test_a_caller_covering_two_branches_may_name_either_of_theirs(self):
        both = self.writer(
            self.tenant, "raise-both2@fin.test", "w-both2",
            branches=[self.ikeja, self.lekki],
        )
        response = self.post(
            both, "customers/", self.books,
            self.customer_body("RB5", branch=self.lekki.pk),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Customer.objects.get(entity=self.books, code="RB5").branch_id,
            self.lekki.pk,
        )

    def test_a_caller_covering_two_branches_may_not_name_a_third(self):
        both = self.writer(
            self.tenant, "raise-both3@fin.test", "w-both3",
            branches=[self.ikeja, self.lekki],
        )
        response = self.post(
            both, "customers/", self.books,
            self.customer_body("RB6", branch=self.yaba.pk),
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Customer.objects.filter(entity=self.books, code="RB6").exists())

    def test_a_pinned_caller_naming_another_branch_is_refused_not_retargeted(self):
        """Refused, deliberately: silently rewriting it would hide the mistake."""
        ikeja = self.writer(
            self.tenant, "raise-ikeja2@fin.test", "w-ikeja2", branches=[self.ikeja],
        )
        response = self.post(
            ikeja, "customers/", self.books,
            self.customer_body("RB7", branch=self.lekki.pk),
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Customer.objects.filter(entity=self.books, code="RB7").exists())

    def test_another_tenants_branch_is_refused_exactly_like_an_unknown_one(self):
        """The parameter must not become an id oracle.

        A rival school's real branch id and an id that has never existed must be
        indistinguishable in the response, or the field can be used to enumerate
        which branches exist outside the caller's own tenant.
        """
        hq = self.writer(self.tenant, "raise-hq3@fin.test", "w-hq3")

        foreign = self.post(
            hq, "customers/", self.books,
            self.customer_body("RB8", branch=self.rival_branch.pk),
        )
        unknown = self.post(
            hq, "customers/", self.books,
            self.customer_body("RB9", branch=99_999_999),
        )

        self.assertEqual(foreign.status_code, 400, foreign.data)
        self.assertEqual(unknown.status_code, 400, unknown.data)
        self.assertEqual(foreign.data["error"]["detail"], unknown.data["error"]["detail"])
        self.assertFalse(
            Customer.objects.filter(entity=self.books, code__in=["RB8", "RB9"]).exists(),
        )

    def test_a_single_branch_school_stamps_its_only_branch(self):
        """One branch is the common case and it must not be a special case.

        The dimension recedes in the UI there, but the column still has to be
        right: the row belongs to that branch, and if the school opens a second
        one tomorrow the history must not read as school-wide.
        """
        solo = self.writer(
            self.solo_tenant, "raise-solo@fin.test", "w-solo",
            branches=[self.solo_main],
        )
        response = self.post(
            solo, "customers/", self.solo_books, self.customer_body("RB10"),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Customer.objects.get(entity=self.solo_books, code="RB10").branch_id,
            self.solo_main.pk,
        )


class SharedWhenAmbiguousTests(_WriteFixture):
    """The three row types that read the ambiguous case the other way.

    A fee template, a bank account and a payroll run are things a school publishes
    once for everybody, so asking a two-branch bursar to pick one would make the
    school's own row invisible at every branch but that one. A *pinned* caller
    still stamps her branch in all three, so a site with its own fees or its own
    collection account keeps them to itself.
    """

    def setUp(self):
        super().setUp()
        self.both = self.writer(
            self.tenant, "amb-both@fin.test", "amb-both",
            branches=[self.ikeja, self.lekki],
        )
        self.ikeja_only = self.writer(
            self.tenant, "amb-ikeja@fin.test", "amb-ikeja", branches=[self.ikeja],
        )

    def test_a_fee_template_from_a_two_branch_bursar_is_published_school_wide(self):
        response = self.post(
            self.both, "fee-structures/", self.books, self.fee_body("AMB1"),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(
            FeeStructure.objects.get(entity=self.books, code="AMB1").branch_id,
        )

    def test_a_fee_template_from_a_pinned_bursar_still_belongs_to_her_branch(self):
        response = self.post(
            self.ikeja_only, "fee-structures/", self.books, self.fee_body("AMB2"),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            FeeStructure.objects.get(entity=self.books, code="AMB2").branch_id,
            self.ikeja.pk,
        )

    def test_a_bank_account_from_a_two_branch_bursar_is_the_schools(self):
        response = self.post(
            self.both, "bank-accounts/", self.books,
            self.bank_body("Group Operations", self.books),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(
            BankAccount.objects.get(entity=self.books, name="Group Operations").branch_id,
        )

    def test_a_payroll_run_from_a_two_branch_officer_covers_the_school(self):
        """A central school's run covers the school, so there is nothing to pick.

        ``payroll.scope`` defaults to CENTRAL and no school here has opted out, so
        a run covers everybody the school employs whichever branches the officer
        happens to be granted. Asking her to name a site would be asking her to
        narrow a run that is not narrowed. The school that has switched to
        PER_BRANCH is asked instead, and ``tests_payroll_branch`` holds that half.
        """
        response = self.post(self.both, "payroll-runs/", self.books, self.payroll_body())

        self.assertEqual(response.status_code, 201, response.data)
        run = PayrollRun.objects.get(entity=self.books)
        self.assertIsNone(run.branch_id)


class InheritedBranchTests(_WriteFixture):
    """Rows that continue a chain take the source's branch and nothing else."""

    def setUp(self):
        super().setUp()
        self.cust_ikeja = self.customer(self.books, "INHI", self.ikeja)
        self.cust_lekki = self.customer(self.books, "INHL", self.lekki)
        self.cust_shared = self.customer(self.books, "INHS", None)
        self.ikeja_only = self.writer(
            self.tenant, "inh-ikeja@fin.test", "inh-ikeja", branches=[self.ikeja],
        )
        self.hq = self.writer(self.tenant, "inh-hq@fin.test", "inh-hq")

    def receipt_body(self, **extra):
        return {
            "amount": 50_000, "payment_date": "2026-01-15",
            "deposit_account": "1100", "auto_allocate": False,
            **extra,
        }

    def test_an_invoice_takes_its_customers_branch_not_its_raisers(self):
        """The chain decides. The head-office bursar is unbound, the family is not."""
        response = self.post(
            self.hq, "invoices/", self.books, self.invoice_body(self.cust_ikeja),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Invoice.objects.get(pk=response.data["data"]["id"]).branch_id,
            self.ikeja.pk,
        )

    def test_naming_a_branch_in_the_body_cannot_move_an_inherited_row(self):
        """The source is the only input; the body is not a second one."""
        response = self.post(
            self.hq, "invoices/", self.books,
            self.invoice_body(self.cust_ikeja, branch=self.yaba.pk),
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Invoice.objects.get(pk=response.data["data"]["id"]).branch_id,
            self.ikeja.pk,
        )

    def test_a_pinned_caller_may_continue_a_school_wide_chain(self):
        """The inclusive reading, and where finance parts company with procurement.

        The Ikeja bursar can see the school-wide customer on her own screen - that
        is what the inclusive read half promises her. If the write half refused
        her that customer's receipt she would be looking at a row she is told she
        may not touch, which reads as a broken screen rather than a rule.
        """
        response = self.post(
            self.ikeja_only, f"customers/{self.cust_shared.code}/receipt/",
            self.books, self.receipt_body(),
        )

        self.assertEqual(response.status_code, 201, response.data)
        payment = Payment.objects.get(entity=self.books, customer=self.cust_shared)
        self.assertIsNone(
            payment.branch_id,
            "the chain decides: a school-wide customer keeps a school-wide receipt",
        )

    def test_a_pinned_caller_may_not_continue_another_branchs_chain(self):
        """The write-side half of the isolation the read half already gives.

        ``_resolve_customer`` is deliberately not branch-filtered - it resolves by
        code inside the entity - so without this check the Ikeja bursar could post
        a receipt onto a Lekki family's account by typing their code. The
        inheritance rule is the choke point that closes it for every AR document
        at once rather than one endpoint at a time.
        """
        response = self.post(
            self.ikeja_only, f"customers/{self.cust_lekki.code}/receipt/",
            self.books, self.receipt_body(),
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(
            Payment.objects.filter(entity=self.books, customer=self.cust_lekki).exists(),
        )

    def test_an_opening_balance_invoice_lands_where_its_customer_does(self):
        """A propagation path that was simply missing, not deliberately null."""
        from vs_finance.receivables import post_opening_balance

        customer = Customer.objects.create(
            entity=self.books, code="INHO", name="Opening Parent",
            branch=self.lekki, opening_balance=250_000,
            receivable_account=Account.objects.get(entity=self.books, code="1200"),
        )
        invoice = post_opening_balance(customer, date=datetime.date(2026, 1, 6))

        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.branch_id, self.lekki.pk)

    def test_a_fee_run_bills_each_family_in_its_own_branch(self):
        """The customer decides, not the template.

        Corona publishes one JSS1 tuition structure school-wide and bills it to
        families at three sites. Taking the structure's branch would file every one
        of those receivables school-wide; taking the customer's puts each family's
        debt where the family is.
        """
        from vs_finance.fees import generate_invoices

        structure = FeeStructure.objects.create(
            entity=self.books, code="INHF", name="JSS1 Tuition", branch=None,
        )
        from vs_finance.models import FeeItem

        FeeItem.objects.create(
            structure=structure, line_no=1, description="Tuition",
            revenue_account=Account.objects.get(entity=self.books, code="4100"),
            amount=300_000,
        )
        invoices = generate_invoices(
            structure, [self.cust_ikeja, self.cust_lekki, self.cust_shared],
            invoice_date=datetime.date(2026, 1, 12),
        )

        by_customer = {inv.customer_id: inv.branch_id for inv in invoices}
        self.assertEqual(by_customer[self.cust_ikeja.pk], self.ikeja.pk)
        self.assertEqual(by_customer[self.cust_lekki.pk], self.lekki.pk)
        self.assertIsNone(by_customer[self.cust_shared.pk])


class WriteThenReadTests(_WriteFixture):
    """The two halves closing on each other, which is the point of the change.

    Neither half means anything alone: the read half was latent because nothing
    stamped a branch, and the write half would be pointless if the reads ignored
    the column. These assert the loop over real HTTP.
    """

    def test_what_ikeja_raises_ikeja_sees_and_lekki_does_not(self):
        ikeja = self.writer(
            self.tenant, "loop-ikeja@fin.test", "loop-ikeja", branches=[self.ikeja],
        )
        lekki = self.writer(
            self.tenant, "loop-lekki@fin.test", "loop-lekki", branches=[self.lekki],
        )
        created = self.post(ikeja, "customers/", self.books, self.customer_body("LOOP1"))
        self.assertEqual(created.status_code, 201, created.data)
        new_id = created.data["data"]["id"]

        self.assertIn(new_id, self.ids(ikeja, "customers/", self.books))
        self.assertNotIn(new_id, self.ids(lekki, "customers/", self.books))

    def test_a_school_wide_row_stays_visible_to_every_branch(self):
        hq = self.writer(self.tenant, "loop-hq@fin.test", "loop-hq")
        ikeja = self.writer(
            self.tenant, "loop-ikeja2@fin.test", "loop-ikeja2", branches=[self.ikeja],
        )
        lekki = self.writer(
            self.tenant, "loop-lekki2@fin.test", "loop-lekki2", branches=[self.lekki],
        )
        created = self.post(hq, "customers/", self.books, self.customer_body("LOOP2"))
        self.assertEqual(created.status_code, 201, created.data)
        new_id = created.data["data"]["id"]

        self.assertIn(new_id, self.ids(ikeja, "customers/", self.books))
        self.assertIn(new_id, self.ids(lekki, "customers/", self.books))

    def test_an_unbound_caller_still_sees_everything_anyone_raised(self):
        hq = self.writer(self.tenant, "loop-hq2@fin.test", "loop-hq2")
        ikeja = self.writer(
            self.tenant, "loop-ikeja3@fin.test", "loop-ikeja3", branches=[self.ikeja],
        )
        mine = self.post(ikeja, "customers/", self.books, self.customer_body("LOOP3"))
        theirs = self.post(hq, "customers/", self.books, self.customer_body("LOOP4"))
        self.assertEqual(mine.status_code, 201, mine.data)
        self.assertEqual(theirs.status_code, 201, theirs.data)

        seen = self.ids(hq, "customers/", self.books)
        self.assertIn(mine.data["data"]["id"], seen)
        self.assertIn(theirs.data["data"]["id"], seen)

    def test_the_tenant_boundary_is_untouched_by_any_of_this(self):
        """Branch narrowing sits *inside* tenant isolation and never replaces it."""
        rival = self.writer(
            self.rival_tenant, "loop-rival@fin.test", "loop-rival",
            branches=[self.rival_branch],
        )
        response = self.post(
            rival, "customers/", self.books, self.customer_body("LOOP5"),
        )

        self.assertIn(response.status_code, (403, 404), response.data)
        self.assertFalse(Customer.objects.filter(entity=self.books, code="LOOP5").exists())
