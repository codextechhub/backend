"""Field Access on petty cash returns.

Corona runs Ikeja, Lekki and Yaba: Ikeja keeps a N100,000 float with Mrs Adeyemi
as custodian and Lekki a N20,000 one. Single Site runs one branch, and Rival
Group is a third school. Each class builds one role per access state in
``setUpTestData`` and drives the real endpoints as those users, so a rule is
proved on the route that serves it and on every other route that carries the
same value.
"""
from __future__ import annotations

import itertools

from core.test_utils import TenantAPIClient
from vs_finance.constants import DocumentStatus
from vs_finance.models import Account, PettyCashFund, PettyCashReturn
from vs_finance.petty_cash import establish_fund, post_petty_cash_return
from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_bank_account_reach import _bank
from .tests_petty_cash_returns import (
    FLOAT,
    JAN_2,
    JAN_10,
    NAIRA,
    _PettyCashReturnFixture,
)

PC = "finance.pettycash_return"
KEYS = (
    "finance.pettycash.view", "finance.pettycash.return", "finance.pettycash.close",
    "finance.pettycash.reverse",
)
_seq = itertools.count(1)


def _k(*names):
    return [f"{PC}.{name}" for name in names]


class _PettyCashFieldAccess(_PettyCashReturnFixture):
    """The returns fixture, plus helpers that mint a user for an access state."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields(PC)
        solo = cls.solo_books
        cls.solo_bank = _bank(solo, "Main Operations", None, "50")
        cls.solo_fund = PettyCashFund.objects.create(
            entity=solo, name="Front desk", float_amount=FLOAT,
            gl_account=Account.objects.get(entity=solo, code="1110"),
        )
        establish_fund(cls.solo_fund, bank_account=cls.solo_bank, amount=FLOAT, date=JAN_2)

    @classmethod
    def staff(cls, tenant, *, hide=(), read_only=(), branch=None):
        """A user holding every petty cash return permission, with the named field states.

        ``hide`` closes Read and Write, and ``read_only`` leaves Read and closes
        Write. A write without a read is not a state a role can hold.
        """
        name = f"pcfa-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@petty.test"), *KEYS,
            tenant=tenant, role_key=name, branch=branch,
        )
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
        set_field_access(role, *cls.field_keys, read=True, write=True)
        if hide:
            set_field_access(role, *hide, read=False, write=False)
        if read_only:
            set_field_access(role, *read_only, read=True, write=False)
        return user

    def api(self, user):
        return TenantAPIClient(user=user)

    def code(self, response):
        return response.data["error"]["code"]

    def url(self, path, books=None):
        return f"/v1/finance/{path}?entity={(books or self.books).code}"

    def reduce_body(self, **extra):
        return {
            "counted_amount": FLOAT, "new_float_amount": 50_000 * NAIRA,
            "bank_account": self.ikeja_bank.pk, "return_date": JAN_10.isoformat(), **extra,
        }

    def reduce(self, user, **extra):
        return self.api(user).post(
            self.url(f"petty-cash-funds/{self.fund.pk}/reduce/"), self.reduce_body(**extra),
            format="json")


class PettyCashReturnFieldAccessTests(_PettyCashFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.lekki_return = PettyCashReturn.objects.create(
            entity=cls.books, branch=cls.lekki, fund=cls.lekki_fund, kind="REDUCE",
            bank_account=cls.lekki_bank, return_date=JAN_10, counted_amount=20_000 * NAIRA,
            book_balance=20_000 * NAIRA, amount=10_000 * NAIRA,
            previous_float_amount=20_000 * NAIRA, new_float_amount=10_000 * NAIRA,
            counted_by=cls.custodian, created_by=cls.custodian, narration="Cut Lekki's float",
            reference="PCR-7",
        )
        post_petty_cash_return(cls.lekki_return, actor_user=cls.custodian)
        cls.lekki_return.refresh_from_db()
        cls.solo_return = PettyCashReturn.objects.create(
            entity=cls.solo_books, branch=cls.solo_main, fund=cls.solo_fund, kind="REDUCE",
            bank_account=cls.solo_bank, return_date=JAN_10, counted_amount=FLOAT,
            book_balance=FLOAT, amount=60_000 * NAIRA, previous_float_amount=FLOAT,
            new_float_amount=40_000 * NAIRA, narration="Cut the solo float",
        )
        cls.open = cls.staff(cls.tenant)
        cls.no_count = cls.staff(cls.tenant, hide=_k(
            "counted_amount", "book_balance", "difference", "difference_reason"))
        cls.no_cash = cls.staff(cls.tenant, hide=_k(
            "amount", "bank_account", "new_float_amount", "previous_float_amount", "cash_left"))
        cls.no_identity = cls.staff(cls.tenant, hide=_k(
            "status", "kind", "journal"))
        cls.no_people = cls.staff(cls.tenant, hide=_k("counted_by", "raised_by"))
        cls.no_words = cls.staff(cls.tenant, hide=_k("narration", "reference", "return_date"))
        cls.ro_count = cls.staff(cls.tenant, read_only=_k(
            "counted_amount", "new_float_amount", "difference_reason"))
        cls.ro_cash = cls.staff(cls.tenant, read_only=_k(
            "amount", "bank_account", "counted_by", "return_date"))
        cls.ro_words = cls.staff(cls.tenant, read_only=_k("narration", "reference"))
        cls.no_book = cls.staff(cls.tenant, hide=_k("book_balance"))
        cls.solo_open = cls.staff(cls.solo_tenant)

    def listed(self, user, query="", books=None):
        return self.api(user).get(self.url("petty-cash-returns/", books) + query)

    def detail(self, user, ret=None, books=None):
        ret = ret or self.lekki_return
        return self.api(user).get(self.url(f"petty-cash-returns/{ret.pk}/", books))

    # -- reads ---------------------------------------------------------------- #

    def test_hidden_count_figures_leave_the_list_and_the_detail(self):
        gone = ["counted_amount", "book_balance", "difference", "shortage", "overage",
                "difference_reason"]
        for row in (*self.listed(self.no_count).data["data"], self.detail(self.no_count).data["data"]):
            for name in gone:
                self.assertNotIn(name, row)
            self.assertIn("amount", row)
        full = self.detail(self.open).data["data"]
        self.assertEqual(
            (full["counted_amount"], full["book_balance"], full["difference"]),
            (20_000 * NAIRA, 20_000 * NAIRA, 0),
        )

    def test_hidden_cash_figures_take_their_names_and_ids_with_them(self):
        gone = ["amount", "amount_naira", "bank_account_id", "bank_account_name",
                "new_float_amount", "previous_float_amount", "cash_left"]
        for row in (*self.listed(self.no_cash).data["data"], self.detail(self.no_cash).data["data"]):
            for name in gone:
                self.assertNotIn(name, row)
            self.assertIn("counted_amount", row)
        full = self.detail(self.open).data["data"]
        self.assertEqual(
            (full["amount"], full["bank_account_name"], full["new_float_amount"]),
            (10_000 * NAIRA, "Lekki Operations", 10_000 * NAIRA),
        )

    def test_hidden_identity_leaves_status_kind_and_journal_and_keeps_number_branch_and_fund(self):
        gone = ["status", "approval_state", "approval_returned", "kind",
                "kind_label", "journal_id"]
        for row in (*self.listed(self.no_identity).data["data"],
                    self.detail(self.no_identity).data["data"]):
            for name in gone:
                self.assertNotIn(name, row)
            for name in ("document_number", "branch_id", "branch_name", "fund_id", "fund_name"):
                self.assertIn(name, row)
            self.assertIn("amount", row)
        full = self.detail(self.open).data["data"]
        self.assertEqual(
            (full["fund_name"], full["kind"], full["status"], full["branch_name"]),
            ("Lekki front desk", "REDUCE", DocumentStatus.POSTED, "Lekki Branch"),
        )

    def test_hidden_people_leave_with_their_exit_flags(self):
        gone = ["counted_by_id", "counted_by_name", "counted_by_is_exited", "created_by_id",
                "created_by_name", "created_by_is_exited"]
        for row in (*self.listed(self.no_people).data["data"],
                    self.detail(self.no_people).data["data"]):
            for name in gone:
                self.assertNotIn(name, row)
        full = self.detail(self.open).data["data"]
        for name in gone:
            self.assertIn(name, full)

    def test_hidden_words_leave_the_detail(self):
        data = self.detail(self.no_words).data["data"]
        for name in ("narration", "reference", "return_date"):
            self.assertNotIn(name, data)

    def test_read_only_names_are_listed_on_a_detail_and_never_on_a_list_row(self):
        detail = self.detail(self.ro_cash).data["data"]
        for name in ("amount", "amount_naira", "bank_account_id", "bank_account_name",
                     "counted_by_id", "counted_by_name", "return_date", "book_balance",
                     "cash_left", "status"):
            self.assertIn(name, detail["_read_only_fields"])
        self.assertNotIn("narration", detail["_read_only_fields"])
        for row in self.listed(self.ro_cash).data["data"]:
            self.assertNotIn("_read_only_fields", row)

    def test_the_body_keys_a_form_sends_are_listed_when_their_field_is_read_only(self):
        listed = self.detail(self.ro_cash).data["data"]["_read_only_fields"]
        self.assertIn("bank_account", listed)
        self.assertIn("counted_by", listed)
        open_listed = self.detail(self.open).data["data"]["_read_only_fields"]
        self.assertNotIn("bank_account", open_listed)
        self.assertNotIn("counted_by", open_listed)

    def test_the_me_map_marks_the_aliases_a_form_sends(self):
        payload = field_access_payload(self.ro_cash, self.tenant)[PC]
        self.assertIn("bank_account", payload["read_only"])
        self.assertIn("counted_by", payload["read_only"])
        self.assertNotIn("counted_amount", payload["read_only"])

    def test_a_filter_on_a_hidden_field_is_refused_not_answered(self):
        for user, query, label in (
            (self.no_identity, "&kind=REDUCE", "Kind of return"),
            (self.no_identity, "&status=POSTED", "Status"),
            (self.no_identity, "&approval=returned", "Status"),
        ):
            with self.subTest(query=query):
                response = self.listed(user, query)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_read_denied")
                self.assertIn(label, response.data["message"])
        ok = self.listed(
            self.open, f"&fund={self.lekki_fund.pk}&kind=REDUCE&status=POSTED")
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(len(ok.data["data"]), 1)

    def test_the_fund_is_a_label_with_no_switch_and_filters_for_everyone(self):
        listed = self.listed(self.no_identity, f"&fund={self.lekki_fund.pk}")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(len(listed.data["data"]), 1)

    # -- writes --------------------------------------------------------------- #

    def test_a_return_that_sets_a_read_only_field_is_refused_and_saves_nothing(self):
        before = PettyCashReturn.objects.count()
        for user, extra, label in (
            (self.ro_count, {}, "Cash counted"),
            (self.ro_count, {"difference_reason": "Two notes"}, "Reason for the difference"),
            (self.ro_cash, {"amount": 50_000 * NAIRA}, "Cash banked"),
            (self.ro_cash, {"counted_by": self.custodian.pk}, "Counted by"),
            (self.ro_words, {"narration": "Cut it"}, "Narration"),
        ):
            with self.subTest(label=label, extra=extra):
                response = self.reduce(user, **extra)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_write_denied")
                self.assertIn(label, response.data["message"])
        self.assertEqual(PettyCashReturn.objects.count(), before)
        self.fund.refresh_from_db()
        self.assertEqual(self.fund.float_amount, FLOAT)

    def test_one_refusal_names_every_field_the_body_sets(self):
        response = self.reduce(self.ro_cash, amount=50_000 * NAIRA, counted_by=self.custodian.pk)
        self.assertEqual(
            sorted(response.data["error"]["detail"]),
            ["amount", "bank_account", "counted_by", "return_date"],
        )

    def test_closing_the_fund_is_refused_on_the_same_terms(self):
        response = self.api(self.ro_count).post(
            self.url(f"petty-cash-funds/{self.fund.pk}/close/"),
            {"counted_amount": FLOAT, "bank_account": self.ikeja_bank.pk,
             "return_date": JAN_10.isoformat()}, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self.code(response), "field_write_denied")
        self.fund.refresh_from_db()
        self.assertFalse(self.fund.is_closed)

    def test_an_empty_read_only_input_is_not_a_write(self):
        empty = self.reduce(self.ro_words, narration="", reference="")
        self.assertEqual(empty.status_code, 201, empty.data)

    def test_an_open_role_reduces_the_float_and_the_sentence_names_the_number(self):
        response = self.reduce(self.open)
        self.assertEqual(response.status_code, 201, response.data)
        number = PettyCashReturn.objects.latest("id").document_number
        self.assertEqual(response.data["message"], f"Petty cash return posted as {number}.")
        self.assertEqual(response.data["data"]["amount"], 50_000 * NAIRA)
        self.assertEqual(response.data["data"]["fund"]["float_amount"], 50_000 * NAIRA)

    def test_hidden_identity_values_leave_the_payload_and_the_number_stays_in_the_sentence(self):
        response = self.reduce(self.no_identity)
        self.assertEqual(response.status_code, 201, response.data)
        number = PettyCashReturn.objects.latest("id").document_number
        self.assertEqual(response.data["message"], f"Petty cash return posted as {number}.")
        for name in ("status", "journal_id"):
            self.assertNotIn(name, response.data["data"])
        self.assertIn("fund_name", response.data["data"])
        self.assertEqual(response.data["data"]["document_number"], number)
        self.assertIn("amount", response.data["data"])

    def test_the_sentence_about_a_difference_names_the_books_only_to_a_reader(self):
        named = self.reduce(self.open, counted_amount=98_000 * NAIRA)
        self.assertEqual(named.status_code, 400, named.data)
        self.assertIn("on the books", str(named.data))
        plain = self.reduce(self.no_book, counted_amount=98_000 * NAIRA)
        self.assertEqual(plain.status_code, 400, plain.data)
        self.assertNotIn("on the books", str(plain.data))
        self.assertIn("differs from the books", str(plain.data))
        self.assertFalse(PettyCashReturn.objects.filter(fund=self.fund).exists())

    def test_a_route_holds_the_return_and_the_waiting_sentence_names_the_number(self):
        from vs_workflow.models import WorkflowStage, WorkflowTemplate

        template = WorkflowTemplate.objects.create(
            tenant=self.tenant, branch=None, document_type="finance.petty_cash_return",
            code="standard", name="Petty cash returns")
        WorkflowStage.objects.create(
            template=template, code="approver", label="Return approval", order=10,
            approver_role_key="finance-approver")
        named = self.reduce(self.open)
        self.assertEqual(named.status_code, 201, named.data)
        number = PettyCashReturn.objects.latest("id").document_number
        self.assertTrue(named.data["message"].startswith(f"Petty cash return {number} is waiting"))
        self.assertEqual(named.data["data"]["approval_state"], "PENDING")
        self.assertIn("approval", named.data["data"])

        plain = self.api(self.no_identity).post(
            self.url(f"petty-cash-funds/{self.lekki_fund.pk}/reduce/"),
            {"counted_amount": 10_000 * NAIRA, "new_float_amount": 5_000 * NAIRA,
             "bank_account": self.lekki_bank.pk, "return_date": JAN_10.isoformat()},
            format="json")
        self.assertEqual(plain.status_code, 201, plain.data)
        number = PettyCashReturn.objects.latest("id").document_number
        self.assertTrue(
            plain.data["message"].startswith(f"Petty cash return {number} is waiting"))
        self.assertNotIn("approval_state", plain.data["data"])

    def test_the_void_response_is_filtered_and_names_the_number(self):
        voided = self.api(self.no_identity).post(
            self.url(f"petty-cash-returns/{self.lekki_return.pk}/void/"), {}, format="json")
        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(
            voided.data["message"],
            f"Petty cash return {self.lekki_return.document_number} voided.",
        )
        self.assertEqual(
            voided.data["data"]["document_number"], self.lekki_return.document_number)
        self.assertNotIn("status", voided.data["data"])
        self.assertEqual(
            PettyCashReturn.objects.get(pk=self.lekki_return.pk).status,
            DocumentStatus.REVERSED,
        )

    def test_the_void_date_is_the_reversal_date_not_the_return_date(self):
        voided = self.api(self.ro_cash).post(
            self.url(f"petty-cash-returns/{self.lekki_return.pk}/void/"),
            {"date": "2026-01-12"}, format="json")
        self.assertEqual(voided.status_code, 200, voided.data)

    # -- shapes of school and tenants ----------------------------------------- #

    def test_a_one_branch_school_shows_its_branch(self):
        shown = self.listed(self.solo_open, books=self.solo_books).data["data"]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["branch_name"], "Main Branch")

    def test_a_branch_bursar_sees_only_their_branchs_returns_through_the_same_switches(self):
        ikeja = self.staff(self.tenant, hide=_k("amount"), branch=self.ikeja)
        self.assertEqual(self.listed(ikeja).data["data"], [])
        lekki = self.staff(self.tenant, hide=_k("amount"), branch=self.lekki)
        rows = self.listed(lekki).data["data"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("amount", rows[0])
        self.assertNotIn("amount_naira", rows[0])

    def test_one_schools_switches_and_rows_never_reach_another(self):
        self.assertNotIn("amount", self.listed(self.no_cash).data["data"][0])
        self.assertEqual(
            self.listed(self.solo_open, books=self.solo_books).data["data"][0]["amount"],
            60_000 * NAIRA,
        )
        rival = self.staff(self.rival_tenant, hide=_k("amount"))
        for response in (
            self.detail(rival, self.lekki_return, self.books),
            self.api(rival).post(
                self.url(f"petty-cash-funds/{self.fund.pk}/reduce/"), self.reduce_body(),
                format="json"),
        ):
            self.assertIn(response.status_code, (403, 404), response.data)

    def test_the_fund_and_voucher_payloads_are_other_records(self):
        """A fund's float and a voucher's narration are not values of a return."""
        fund = self.api(self.no_cash).get(self.url(f"petty-cash-funds/{self.lekki_fund.pk}/"))
        self.assertEqual(fund.status_code, 200, fund.data)
        self.assertIn("float_amount", fund.data["data"])
