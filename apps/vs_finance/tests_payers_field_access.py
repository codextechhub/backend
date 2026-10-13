"""Field Access on payer links and on payments from a payer.

Mr Okafor pays for Ada and Emeka at Ikeja and Chidi at Lekki, and sends one
transfer into Ikeja's bank. Mrs Bello pays for her two children at Single Site,
which has one branch, and Rival Group is a third school. Each class builds one
role per access state in ``setUpTestData`` and drives the real endpoints as
those users, so a rule is proved on the route that serves it and on every other
route that carries the same value.
"""
from __future__ import annotations

import itertools

from core.test_utils import TenantAPIClient
from vs_finance.models import PayerLink, PayerPayment
from vs_finance.payer_payments import link_customer, record_payer_payment, unlink_customer
from vs_finance.tests_payer_payments import JAN_15, KEYS, _PayerFixture
from vs_rbac.field_enforcement import field_access_payload
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

LINK = "finance.payer_link"
PAY = "finance.payer_payment"
_seq = itertools.count(1)


def _k(resource, *names):
    return [f"{resource}.{name}" for name in names]


class _PayerFieldAccess(_PayerFixture):
    """Mr Okafor's books, Mrs Bello's, and helpers that mint a user for an access state."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields(LINK, PAY)
        cls.bello = cls.account(cls.solo_books, "BELLO", "Mrs Bello", None)
        cls.kemi = cls.account(cls.solo_books, "KEMI", "Kemi Bello", None)
        cls.tobi = cls.account(cls.solo_books, "TOBI", "Tobi Bello", cls.solo_main)
        for child in (cls.kemi, cls.tobi):
            link_customer(cls.bello, child)
        cls.bill(cls.solo_books, cls.kemi, None, 60_000_00, 4)
        cls.bill(cls.solo_books, cls.tobi, cls.solo_main, 40_000_00, 6)

    @classmethod
    def staff(cls, tenant, *, hide=(), read_only=(), branch=None):
        """A user holding every payer permission, with the named field states.

        ``hide`` closes Read and Write, and ``read_only`` leaves Read and closes
        Write. A write without a read is not a state a role can hold.
        """
        name = f"payerfa-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@payer.test"), *KEYS,
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


# --------------------------------------------------------------------------- #
# Links                                                                       #
# --------------------------------------------------------------------------- #

class PayerLinkFieldAccessTests(_PayerFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant)
        cls.no_payer = cls.staff(cls.tenant, hide=_k(LINK, "payer"))
        cls.no_customer = cls.staff(cls.tenant, hide=_k(LINK, "customer"))
        cls.no_active = cls.staff(cls.tenant, hide=_k(LINK, "is_active"))
        cls.no_source = cls.staff(cls.tenant, hide=_k(
            LINK, "source_type", "source_id"))
        cls.no_names = cls.staff(cls.tenant, hide=_k(LINK, "payer", "customer"))
        cls.ro_payer = cls.staff(cls.tenant, read_only=_k(LINK, "payer"))
        cls.ro_customer = cls.staff(cls.tenant, read_only=_k(LINK, "customer"))
        cls.ro_active = cls.staff(cls.tenant, read_only=_k(LINK, "is_active"))
        cls.ro_source = cls.staff(cls.tenant, read_only=_k(
            LINK, "source_type", "source_id"))
        cls.solo_open = cls.staff(cls.solo_tenant)
        cls.solo_no_payer = cls.staff(cls.solo_tenant, hide=_k(LINK, "payer"))
        cls.newborn = cls.account(cls.books, "OBI", "Obi Okafor", cls.ikeja)

    def links(self, user, query="", books=None):
        return self.api(user).get(self.url("payer-links/", books) + query)

    def link(self, user, **body):
        body = {"payer": "OKAFOR", "customer": "OBI", **body}
        return self.api(user).post(self.url("payer-links/"), body, format="json")

    # -- reads ---------------------------------------------------------------- #

    def test_each_hidden_link_field_leaves_every_row(self):
        for user, gone, kept in (
            (self.no_payer, ["payer"], ["customer", "is_active", "source_type"]),
            (self.no_customer, ["customer"], ["payer", "is_active", "source_type"]),
            (self.no_active, ["is_active"], ["payer", "customer", "source_type"]),
            (self.no_source, ["source_type", "source_id"], ["payer", "customer", "is_active"]),
        ):
            rows = self.links(user).data["data"]
            self.assertEqual(len(rows), 3)
            for row in rows:
                for name in gone:
                    self.assertNotIn(name, row)
                for name in kept:
                    self.assertIn(name, row)
        row = self.links(self.open).data["data"][0]
        self.assertEqual(row["payer"]["code"], "OKAFOR")
        self.assertTrue(row["is_active"])

    def test_a_list_row_carries_no_read_only_list(self):
        for row in self.links(self.ro_payer).data["data"]:
            self.assertNotIn("_read_only_fields", row)

    def test_the_created_link_lists_the_read_only_names_a_form_greys(self):
        created = self.link(self.ro_active)
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["data"]["_read_only_fields"], ["is_active"])

    def test_a_filter_on_a_hidden_field_is_refused_not_answered(self):
        for user, query, label in (
            (self.no_payer, "&payer=OKAFOR", "Payer"),
            (self.no_customer, "&customer=ADA", "Linked customer"),
            (self.no_active, "&is_active=true", "Link active"),
        ):
            with self.subTest(query=query):
                response = self.links(user, query)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_read_denied")
                self.assertIn(label, response.data["message"])
        self.assertEqual(self.links(self.open, "&payer=OKAFOR&is_active=true").status_code, 200)

    # -- writes --------------------------------------------------------------- #

    def test_a_link_that_sets_a_read_only_field_is_refused_and_saves_nothing(self):
        before = PayerLink.objects.count()
        for user, extra, label in (
            (self.ro_payer, {}, "Payer"),
            (self.ro_customer, {}, "Linked customer"),
            (self.ro_source, {"source_type": "IMPORT", "source_id": "row-4"},
             "Source reference"),
        ):
            with self.subTest(label=label):
                response = self.link(user, **extra)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_write_denied")
                self.assertIn(label, response.data["message"])
        self.assertEqual(PayerLink.objects.count(), before)

    def test_a_form_that_leaves_the_source_empty_is_not_refused(self):
        response = self.link(self.ro_source, source_type="", source_id="")
        self.assertEqual(response.status_code, 201, response.data)

    def test_an_open_role_links_and_the_sentence_names_both_customers(self):
        response = self.link(self.open, source_type="IMPORT", source_id="row-4")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["message"], "Mr Okafor pays for Obi Okafor.")
        link = PayerLink.objects.get(payer=self.okafor, customer=self.newborn)
        self.assertEqual((link.source_type, link.source_id), ("IMPORT", "row-4"))

    def test_ending_a_link_is_a_write_of_whether_it_is_active(self):
        link = PayerLink.objects.get(payer=self.okafor, customer=self.ada)

        refused = self.api(self.ro_active).delete(self.url(f"payer-links/{link.pk}/"))

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("Link active", refused.data["message"])
        link.refresh_from_db()
        self.assertTrue(link.is_active)
        ended = self.api(self.open).delete(self.url(f"payer-links/{link.pk}/"))
        self.assertEqual(ended.status_code, 200, ended.data)
        self.assertEqual(ended.data["message"], "Mr Okafor no longer pays for Ada Okafor.")
        link.refresh_from_db()
        self.assertFalse(link.is_active)

    def test_an_ended_link_response_names_only_what_the_caller_may_read(self):
        link = PayerLink.objects.get(payer=self.okafor, customer=self.ada)

        ended = self.api(self.no_names).delete(self.url(f"payer-links/{link.pk}/"))

        self.assertEqual(ended.status_code, 200, ended.data)
        self.assertEqual(ended.data["message"], "The link has ended.")
        for name in ("payer", "customer"):
            self.assertNotIn(name, ended.data["data"])
        self.assertNotIn("is_active", self.api(self.no_active).get(
            self.url("payer-links/")).data["data"][0])

    def test_switching_an_ended_link_back_on_is_a_write_of_whether_it_is_active(self):
        link = PayerLink.objects.get(payer=self.okafor, customer=self.ada)
        unlink_customer(link)

        refused = self.link(self.ro_active, customer="ADA")

        self.assertEqual(refused.status_code, 403, refused.data)
        link.refresh_from_db()
        self.assertFalse(link.is_active)
        revived = self.link(self.open, customer="ADA")
        self.assertEqual(revived.status_code, 201, revived.data)
        link.refresh_from_db()
        self.assertTrue(link.is_active)

    def test_linking_a_pair_already_linked_is_not_a_write_of_the_flag(self):
        response = self.link(self.ro_active, customer="ADA")
        self.assertEqual(response.status_code, 201, response.data)

    # -- shapes of school and tenants ----------------------------------------- #

    def test_a_one_branch_school_hides_the_payer_the_same_way(self):
        hidden = self.links(self.solo_no_payer, books=self.solo_books).data["data"]
        shown = self.links(self.solo_open, books=self.solo_books).data["data"]
        self.assertEqual(len(hidden), 2)
        for row in hidden:
            self.assertNotIn("payer", row)
            self.assertIn("customer", row)
        self.assertEqual({row["payer"]["code"] for row in shown}, {"BELLO"})

    def test_a_multi_branch_bursar_pinned_to_a_branch_sees_only_that_branchs_links_filtered(self):
        lekki = self.staff(self.tenant, hide=_k(LINK, "payer"), branch=self.lekki)
        rows = self.links(lekki).data["data"]
        self.assertEqual([row["customer"]["code"] for row in rows], ["CHIDI"])
        self.assertNotIn("payer", rows[0])

    def test_one_schools_switches_and_rows_never_reach_another(self):
        self.assertNotIn("payer", self.links(self.no_payer).data["data"][0])
        self.assertIn("payer", self.links(self.solo_open, books=self.solo_books).data["data"][0])
        rival = self.staff(self.rival_tenant, hide=_k(LINK, "payer"))
        self.assertIn(self.links(rival, books=self.books).status_code, (403, 404))

    def test_the_me_map_marks_a_read_only_link_flag(self):
        payload = field_access_payload(self.ro_active, self.tenant)[LINK]
        self.assertEqual(payload["read_only"], ["is_active"])


# --------------------------------------------------------------------------- #
# Payments                                                                    #
# --------------------------------------------------------------------------- #

class PayerPaymentFieldAccessTests(_PayerFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.document = record_payer_payment(
            cls.okafor, bank_account=cls.ikeja_bank, amount=450_000_00, payment_date=JAN_15,
            reference="TRF-9", narration="Fees for the term",
        )
        cls.solo_document = record_payer_payment(
            cls.bello, bank_account=cls.solo_bank, amount=100_000_00, payment_date=JAN_15,
        )
        k = PAY + "."
        cls.open = cls.staff(cls.tenant)
        cls.no_status = cls.staff(cls.tenant, hide=[k + "status"])
        cls.no_payer = cls.staff(cls.tenant, hide=[k + "payer"])
        cls.no_bank = cls.staff(cls.tenant, hide=[k + "bank_account"])
        cls.no_amount = cls.staff(cls.tenant, hide=[k + "amount"])
        cls.no_dates = cls.staff(cls.tenant, hide=[
            k + "payment_date", k + "method", k + "reference", k + "narration"])
        cls.no_split = cls.staff(cls.tenant, hide=[k + "split"])
        cls.no_shares = cls.staff(cls.tenant, hide=[k + "shares"])
        cls.no_results = cls.staff(cls.tenant, hide=[k + "allocation_results"])
        cls.ro_amount = cls.staff(cls.tenant, read_only=[k + "amount"])
        cls.ro_inputs = cls.staff(cls.tenant, read_only=[
            k + "payer", k + "bank_account", k + "payment_date", k + "method",
            k + "reference", k + "narration", k + "split", k + "shares"])
        cls.ro_optional = cls.staff(cls.tenant, read_only=[
            k + "method", k + "reference", k + "narration", k + "split"])
        cls.lekki_no_amount = cls.staff(cls.tenant, hide=[k + "amount"], branch=cls.lekki)
        cls.solo_open = cls.staff(cls.solo_tenant)

    def body(self, **extra):
        return {"payer": "OKAFOR", "bank_account": self.ikeja_bank.pk, "amount": 450_000_00,
                "payment_date": "2026-01-15", **extra}

    def detail(self, user, document=None, books=None):
        document = document or self.document
        return self.api(user).get(self.url(f"payer-payments/{document.pk}/", books))

    def listed(self, user, query="", books=None):
        return self.api(user).get(self.url("payer-payments/", books) + query)

    def record(self, user, path="payer-payments/", **extra):
        return self.api(user).post(self.url(path), self.body(**extra), format="json")

    # -- reads ---------------------------------------------------------------- #

    def test_hidden_payment_values_leave_the_list_and_the_detail(self):
        for user, gone in (
            (self.no_status, ["status"]),
            (self.no_payer, ["payer"]),
            (self.no_bank, ["bank_account"]),
            (self.no_dates, ["payment_date", "method", "reference", "narration"]),
            (self.no_split, ["split", "split_label"]),
            (self.no_shares, ["shares"]),
        ):
            rows = [*self.listed(user).data["data"], self.detail(user).data["data"]]
            for row in rows:
                for name in gone:
                    self.assertNotIn(name, row)
                for name in ("document_number", "branch_id", "branch_name"):
                    self.assertIn(name, row)
                self.assertIn("amount", row)
        full = self.detail(self.open).data["data"]
        self.assertEqual(
            (full["document_number"], full["status"], full["reference"], full["method"]),
            (self.document.document_number, "POSTED", "TRF-9", "BANK_TRANSFER"),
        )
        self.assertEqual(full["bank_account"]["name"], "Ikeja GTBank")

    def test_hidden_shares_take_every_row_with_them(self):
        full = self.detail(self.open).data["data"]
        self.assertEqual(len(full["shares"]), 3)
        hidden = self.detail(self.no_shares).data["data"]
        self.assertNotIn("shares", hidden)
        self.assertIn("amount", hidden)

    def test_what_each_share_settled_is_one_switch_and_leaves_the_amounts(self):
        rows = self.detail(self.no_results).data["data"]["shares"]
        self.assertEqual(len(rows), 3)
        for row in rows:
            for name in ("kind", "credit", "outstanding", "bills", "document", "forwarded_by"):
                self.assertNotIn(name, row)
            self.assertIn("amount", row)
            self.assertIn("customer", row)
        row = self.detail(self.open).data["data"]["shares"][0]
        self.assertIn("kind", row)
        self.assertIn("bills", row)

    def test_a_hidden_total_takes_the_share_amounts_and_the_figures_that_restate_them(self):
        data = self.detail(self.no_amount).data["data"]
        self.assertNotIn("amount", data)
        self.assertEqual(len(data["shares"]), 3)
        for row in data["shares"]:
            for name in ("amount", "credit", "bills", "outstanding"):
                self.assertNotIn(name, row)
            self.assertIn("kind", row)
            self.assertIn("customer", row)
        for row in self.listed(self.no_amount).data["data"]:
            self.assertNotIn("amount", row)

    def test_a_branch_bursar_who_may_not_read_the_amount_sees_none_of_his_share(self):
        data = self.detail(self.lekki_no_amount).data["data"]
        self.assertEqual([row["customer"]["code"] for row in data["shares"]], ["CHIDI"])
        self.assertNotIn("amount", data)
        self.assertNotIn("amount", data["shares"][0])
        self.assertEqual(self.detail(self.staff(self.tenant, branch=self.lekki)).data["data"]["amount"],
                         120_000_00)

    def test_read_only_names_are_listed_on_a_detail_and_never_on_a_list_row(self):
        detail = self.detail(self.ro_amount).data["data"]
        self.assertIn("amount", detail["_read_only_fields"])
        self.assertIn("status", detail["_read_only_fields"])
        self.assertNotIn("document_number", detail["_read_only_fields"])
        self.assertNotIn("payer", detail["_read_only_fields"])
        for row in self.listed(self.ro_amount).data["data"]:
            self.assertNotIn("_read_only_fields", row)
        for share in detail["shares"]:
            self.assertNotIn("_read_only_fields", share)

    def test_a_filter_on_a_hidden_field_is_refused_not_answered(self):
        for user, query, label in (
            (self.no_payer, "&payer=OKAFOR", "Payer"),
            (self.no_status, "&status=POSTED", "Status"),
        ):
            with self.subTest(query=query):
                response = self.listed(user, query)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_read_denied")
                self.assertIn(label, response.data["message"])
        open_filtered = self.listed(
            self.open, f"&payer=OKAFOR&status=POSTED&branch={self.ikeja.pk}")
        self.assertEqual(open_filtered.status_code, 200, open_filtered.data)
        self.assertEqual(len(open_filtered.data["data"]), 1)

    # -- the preview ---------------------------------------------------------- #

    def test_the_preview_is_rendered_through_the_same_switches(self):
        data = self.record(self.no_results, path="payer-payments/preview/",
                           amount=400_000_00).data["data"]
        for row in data["shares"]:
            for name in ("kind", "credit", "outstanding", "bills"):
                self.assertNotIn(name, row)
            self.assertIn("amount", row)
        full = self.record(self.open, path="payer-payments/preview/",
                           amount=400_000_00).data["data"]
        self.assertEqual(full["amount"], 400_000_00)
        self.assertIn("bills", full["shares"][0])
        self.assertFalse(PayerPayment.objects.exclude(pk__in=[
            self.document.pk, self.solo_document.pk]).exists())

    def test_a_preview_whose_body_sets_a_read_only_field_is_refused(self):
        for extra, label in (
            ({}, "Amount"),
            ({"shares": [{"customer": "ADA", "amount": 100_000_00}]}, "Customer shares"),
        ):
            with self.subTest(label=label):
                response = self.record(
                    self.ro_amount if not extra else self.ro_inputs,
                    path="payer-payments/preview/", **extra)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(self.code(response), "field_write_denied")
                self.assertIn(label, response.data["message"])

    def test_the_split_choices_a_recorder_is_offered_are_not_a_payment_field(self):
        response = self.api(self.no_split).get(self.url("payer-payments/preview/"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("split_options", response.data["data"])

    # -- writes --------------------------------------------------------------- #

    def test_a_payment_that_sets_a_read_only_field_is_refused_and_saves_nothing(self):
        before = PayerPayment.objects.count()
        response = self.record(self.ro_amount)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(self.code(response), "field_write_denied")
        self.assertEqual(list(response.data["error"]["detail"]), ["amount"])
        every = self.record(
            self.ro_inputs, method="CASH", reference="X", narration="Y", split="PROPORTIONAL",
            shares=[{"customer": "ADA", "amount": 1}])
        self.assertEqual(every.status_code, 403, every.data)
        self.assertEqual(
            sorted(every.data["error"]["detail"]),
            ["bank_account", "method", "narration", "payer", "payment_date", "reference",
             "shares", "split"],
        )
        self.assertEqual(PayerPayment.objects.count(), before)

    def test_an_empty_read_only_input_is_not_a_write(self):
        empty = self.record(self.ro_optional, reference="", narration="", method="")
        self.assertEqual(empty.status_code, 201, empty.data)
        filled = self.record(self.ro_optional, reference="X-1")
        self.assertEqual(filled.status_code, 403, filled.data)
        self.assertEqual(list(filled.data["error"]["detail"]), ["reference"])

    def test_an_open_role_records_and_the_sentence_names_the_number_and_the_payer(self):
        response = self.record(self.open)
        self.assertEqual(response.status_code, 201, response.data)
        number = PayerPayment.objects.latest("id").document_number
        self.assertEqual(response.data["message"], f"Payment {number} from Mr Okafor recorded.")
        self.assertEqual(response.data["data"]["amount"], 450_000_00)

    def test_a_hidden_status_is_left_out_of_the_payload_and_the_number_stays_in_the_sentence(self):
        plain = self.record(self.no_status)
        self.assertEqual(plain.status_code, 201, plain.data)
        number = PayerPayment.objects.latest("id").document_number
        self.assertEqual(plain.data["message"], f"Payment {number} from Mr Okafor recorded.")
        self.assertEqual(plain.data["data"]["document_number"], number)
        self.assertNotIn("status", plain.data["data"])

    def test_the_void_response_is_filtered_and_names_the_number(self):
        voided = self.api(self.no_status).post(
            self.url(f"payer-payments/{self.document.pk}/void/"), {}, format="json")
        self.assertEqual(voided.status_code, 200, voided.data)
        self.assertEqual(
            voided.data["message"], f"Payment {self.document.document_number} voided.")
        self.assertEqual(voided.data["data"]["document_number"], self.document.document_number)
        self.assertNotIn("status", voided.data["data"])
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, "REVERSED")

    def test_the_void_date_is_the_reversal_date_not_the_payment_date(self):
        voided = self.api(self.ro_inputs).post(
            self.url(f"payer-payments/{self.document.pk}/void/"), {"date": "2026-01-16"},
            format="json")
        self.assertEqual(voided.status_code, 200, voided.data)

    # -- shapes of school and tenants ----------------------------------------- #

    def test_a_one_branch_school_shows_its_branch(self):
        shown = self.detail(self.solo_open, self.solo_document, self.solo_books).data["data"]
        self.assertEqual(shown["branch_name"], "Main Branch")
        self.assertEqual({row["kind"] for row in shown["shares"]}, {"RECEIPT"})

    def test_one_schools_switches_and_rows_never_reach_another(self):
        self.assertNotIn("amount", self.detail(self.no_amount).data["data"])
        self.assertEqual(
            self.detail(self.solo_open, self.solo_document, self.solo_books).data["data"]["amount"],
            100_000_00,
        )
        rival = self.staff(self.rival_tenant, hide=_k(PAY, "amount"))
        for response in (
            self.detail(rival, self.document, self.books),
            self.record(rival),
        ):
            self.assertIn(response.status_code, (403, 404), response.data)
