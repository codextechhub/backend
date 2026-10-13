"""Field Access on the customer master (``finance.customer``).

The Okafor family is one school-wide customer of Corona (Ikeja, Lekki, Yaba)
with a posted invoice and a receipt; Single Site runs one branch; Rival Group
is a third school. Each class builds one role per access state in
``setUpTestData`` (a role that cannot read a field, one that can read and not
write it) and drives the real endpoints as those users, so a rule is proved on
the route that serves it and on every other route that carries the same value.

A customer's code and name identify it wherever it is picked, printed or named
in a message and carry no switch, so every test below expects them to stay.
"""
from __future__ import annotations

import itertools
from unittest import mock

from core.test_utils import TenantAPIClient
from vs_exports import engine
from vs_exports.catalogue import get_dataset
from vs_finance.document_email import preview as email_preview
from vs_finance.documents import _customer_block, invoice_document_context
from vs_finance.models import Customer, FinanceDocumentDelivery
from vs_rbac.field_enforcement import SYSTEM_SURFACES, field_access_payload
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .tests_customer_branch_reach import _FamilyFixture

CUSTOMER = "finance.customer"
_seq = itertools.count(1)

KEYS = (
    "finance.customer.view", "finance.customer.create", "finance.customer.update",
    "finance.customer.email_statement", "finance.invoice.view", "finance.invoice.email",
    "finance.payment.view", "finance.audit.view", "finance.report.view",
    "finance.customer.import_opening",
)

CONTACT = ("billing_email", "billing_phone", "billing_address")
ACCOUNT = ("receivable_account", "opening_balance", "source_type", "source_id",
           "is_active", "created_at")
CONTACT_NAMES = ("billing_email", "billing_phone", "billing_address")


def _k(*names):
    return [f"{CUSTOMER}.{name}" for name in names]


class _CustomerFieldAccess(_FamilyFixture):
    """The family books, plus helpers that mint a user for an access state."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.keys = install_declared_fields(CUSTOMER)
        Customer.objects.filter(pk=cls.family.pk).update(
            billing_email="okafor@family.test", billing_phone="08030000001",
            billing_address="4 Ikeja Road", source_type="manual", source_id="OK-1",
        )
        cls.family.refresh_from_db()
        cls.solo_family.billing_email = "solo@family.test"
        cls.solo_family.billing_phone = "08030000002"
        cls.solo_family.billing_address = "9 Main Street"
        cls.solo_family.save()
        cls.rival_customer = cls.customer(cls.rival_books, "RIVALFAM", None)
        Customer.objects.filter(pk=cls.rival_customer.pk).update(
            billing_email="rival@family.test", billing_phone="08030000003")

    @classmethod
    def staff(cls, tenant, *, hide=(), read_only=(), branch=None, keys=KEYS):
        """A user holding the customer permission keys with the named field states."""
        name = f"custfa-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@customer.test"), *keys,
            tenant=tenant, role_key=name, branch=branch,
        )
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
        set_field_access(role, *cls.keys, read=True, write=True)
        if hide:
            set_field_access(role, *_k(*hide), read=False, write=False)
        if read_only:
            set_field_access(role, *_k(*read_only), read=True, write=False)
        return user

    def api(self, user):
        return TenantAPIClient(user=user)

    def url(self, path, books=None):
        return f"/v1/finance/{path}?entity={(books or self.books).code}"

    def row(self, user, customer=None, books=None):
        customer = customer or self.family
        response = self.api(user).get(self.url("customers/", books))
        self.assertEqual(response.status_code, 200, response.data)
        return next(r for r in response.data["data"] if r["code"] == customer.code)

    def detail(self, user, customer=None, books=None):
        customer = customer or self.family
        response = self.api(user).get(self.url(f"customers/{customer.code}/", books))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class CustomerReadTests(_CustomerFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant)
        cls.no_contact = cls.staff(cls.tenant, hide=CONTACT)
        cls.no_account = cls.staff(cls.tenant, hide=ACCOUNT)
        cls.no_balance = cls.staff(cls.tenant, hide=("balance",))
        cls.no_status = cls.staff(cls.tenant, hide=("account_status",))
        cls.no_standing = cls.staff(cls.tenant, hide=(
            "lifetime_paid", "open_invoice_count", "pays_for_count"))
        cls.no_history = cls.staff(cls.tenant, hide=(
            "open_invoices", "open_debit_notes", "transactions", "statement"))
        cls.solo_open = cls.staff(cls.solo_tenant)
        cls.solo_no_contact = cls.staff(cls.solo_tenant, hide=CONTACT)

    # -- list and detail -------------------------------------------------------- #

    def test_hidden_contact_leaves_the_list_and_the_detail(self):
        row = self.row(self.no_contact)
        customer = self.detail(self.no_contact)["customer"]
        for shown in (row, customer):
            for name in CONTACT_NAMES:
                self.assertNotIn(name, shown)
            self.assertEqual((shown["code"], shown["name"]), ("OKAFOR", "Parent OKAFOR"))
        full = self.row(self.open)
        self.assertEqual(
            (full["billing_email"], full["billing_phone"], full["billing_address"]),
            ("okafor@family.test", "08030000001", "4 Ikeja Road"),
        )

    def test_each_contact_field_has_its_own_switch(self):
        for name in CONTACT_NAMES:
            user = self.staff(self.tenant, hide=(name,))
            row = self.row(user)
            self.assertNotIn(name, row)
            for other in set(CONTACT_NAMES) - {name}:
                self.assertIn(other, row)

    def test_hidden_account_fields_take_every_alias_with_them(self):
        row = self.row(self.no_account)
        customer = self.detail(self.no_account)["customer"]
        for shown in (row, customer):
            for name in (
                "receivable_account_id", "receivable_account_code", "receivable_account_name", "opening_balance",
                "opening_balance_naira", "source_type", "source_id", "is_active", "created_at",
            ):
                self.assertNotIn(name, shown)
            self.assertIn("billing_email", shown)
        full = self.detail(self.open)["customer"]
        self.assertEqual(full["source_id"], "OK-1")
        self.assertIn("created_at", full)
        self.assertTrue(full["is_active"])

    def test_hidden_balance_leaves_every_figure_that_adds_up_to_it(self):
        row = self.row(self.no_balance)
        self.assertNotIn("balance", row)
        self.assertNotIn("balance_naira", row)
        self.assertIn("account_status", row)
        data = self.detail(self.no_balance)
        self.assertNotIn("current_balance", data["summary"])
        for line in data["statement"]:
            self.assertNotIn("balance", line)
        for bill in data["open_invoices"]:
            self.assertNotIn("balance", bill)
        self.assertTrue(data["statement"])
        summary = self.api(self.no_balance).get(self.url("customers/summary/")).data["data"]
        for name in ("receivable", "on_credit"):
            self.assertNotIn(name, summary)
        self.assertIn("total", summary)
        full = self.detail(self.open)
        self.assertIn("current_balance", full["summary"])
        self.assertIn("balance", full["statement"][0])
        self.assertIn("balance", full["open_invoices"][0])

    def test_hidden_account_status_leaves_the_list_the_detail_and_the_header(self):
        self.assertNotIn("account_status", self.row(self.no_status))
        self.assertNotIn("account_status", self.detail(self.no_status)["summary"])
        summary = self.api(self.no_status).get(self.url("customers/summary/")).data["data"]
        self.assertNotIn("overdue", summary)
        self.assertNotIn("status_counts", summary)
        self.assertIn("account_status", self.row(self.open))

    def test_hidden_standing_leaves_the_counts(self):
        self.assertNotIn("pays_for_count", self.row(self.no_standing))
        summary = self.detail(self.no_standing)["summary"]
        self.assertNotIn("lifetime_paid", summary)
        self.assertNotIn("open_invoice_count", summary)
        self.assertIn("pays_for_count", self.row(self.open))

    def test_hidden_history_blocks_leave_the_detail(self):
        data = self.detail(self.no_history)
        for name in ("open_invoices", "open_debit_notes", "transactions", "statement"):
            self.assertNotIn(name, data)
        self.assertIn("customer", data)
        full = self.detail(self.open)
        for name in ("open_invoices", "transactions", "statement"):
            self.assertTrue(full[name])

    def test_one_branch_school_hides_the_same_fields(self):
        row = self.row(self.solo_no_contact, self.solo_family, self.solo_books)
        for name in CONTACT_NAMES:
            self.assertNotIn(name, row)
        full = self.row(self.solo_open, self.solo_family, self.solo_books)
        self.assertEqual(full["billing_email"], "solo@family.test")

    # -- filters ---------------------------------------------------------------- #

    def test_a_filter_on_a_hidden_field_is_refused(self):
        cases = (
            (self.no_account, "customers/?is_active=true", "is_active"),
            (self.no_status, "customers/?status=overdue", "status"),
            (self.no_account, "customers/summary/?is_active=true", "is_active"),
        )
        for user, path, _param in cases:
            response = self.api(user).get(f"/v1/finance/{path}&entity={self.books.code}")
            with self.subTest(path=path):
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_read_denied")

    def test_the_same_filters_answer_a_reader_of_the_field(self):
        for path in ("customers/?is_active=true", "customers/?status=overdue"):
            response = self.api(self.open).get(
                f"/v1/finance/{path}&entity={self.books.code}")
            self.assertEqual(response.status_code, 200, response.data)

    def test_the_branch_is_never_hidden_and_its_filter_is_never_refused(self):
        row = self.row(self.no_account)
        self.assertIn("branch_id", row)
        self.assertIn("branch_name", row)
        response = self.api(self.no_account).get(
            f"/v1/finance/customers/?own=true&entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)

    # -- the /me map and nested copies ------------------------------------------ #

    def test_the_map_lists_the_hidden_names_and_every_alias(self):
        payload = field_access_payload(self.no_contact, self.tenant)[CUSTOMER]
        self.assertEqual(sorted(payload["hidden"]), sorted([*CONTACT_NAMES, "recipients"]))

    def test_an_invoice_carries_only_the_customers_identifiers(self):
        response = self.api(self.no_contact).get(self.url("invoices/"))
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["data"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("customer_code", row)
            self.assertIn("customer_name", row)
            for name in CONTACT_NAMES:
                self.assertNotIn(name, row)


class CustomerWriteTests(_CustomerFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant)
        cls.ro_contact = cls.staff(cls.tenant, read_only=CONTACT)
        cls.ro_account = cls.staff(cls.tenant, read_only=ACCOUNT)
        cls.no_contact = cls.staff(cls.tenant, hide=CONTACT)

    def patch(self, user, body, customer=None):
        customer = customer or self.family
        return self.api(user).patch(
            self.url(f"customers/{customer.code}/"), body, format="json")

    def fresh(self, customer=None):
        customer = customer or self.family
        customer.refresh_from_db()
        return customer

    def test_a_detail_lists_the_read_only_fields_and_their_aliases(self):
        customer = self.detail(self.ro_contact)["customer"]
        self.assertEqual(
            sorted(customer["_read_only_fields"]),
            sorted(["billing_email", "billing_phone", "billing_address", "created_at"]),
        )
        customer = self.detail(self.ro_account)["customer"]
        self.assertEqual(
            sorted(customer["_read_only_fields"]),
            sorted([
                "receivable_account_id", "receivable_account_code",
                "receivable_account_name", "opening_balance", "opening_balance_naira",
                "source_type", "source_id", "is_active", "receivable_account",
                "opening_date", "opening_branch", "created_at",
            ]),
        )
        self.assertNotIn("_read_only_fields", self.row(self.ro_contact))
        self.assertEqual(self.detail(self.open)["customer"]["_read_only_fields"], ["created_at"])

    def test_changing_a_read_only_contact_is_refused_and_nothing_is_saved(self):
        for name, value in (
            ("billing_email", "other@family.test"),
            ("billing_phone", "08099999999"),
            ("billing_address", "12 Lekki Road"),
        ):
            response = self.patch(self.ro_contact, {name: value})
            with self.subTest(field=name):
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(response.data["error"]["code"], "field_write_denied")
                self.assertIn(name, response.data["error"]["detail"])
        saved = self.fresh()
        self.assertEqual(
            (saved.billing_email, saved.billing_phone, saved.billing_address),
            ("okafor@family.test", "08030000001", "4 Ikeja Road"),
        )

    def test_one_refusal_names_every_field_and_saves_none_of_them(self):
        response = self.patch(self.ro_contact, {
            "name": "Renamed", "billing_email": "x@family.test", "billing_phone": "1",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            sorted(response.data["error"]["detail"]), ["billing_email", "billing_phone"])
        self.assertEqual(self.fresh().name, "Parent OKAFOR")

    def test_a_form_that_sends_the_stored_values_back_is_not_refused(self):
        response = self.patch(self.ro_contact, {
            "name": "Okafor Family", "billing_email": "okafor@family.test",
            "billing_phone": "08030000001", "billing_address": "4 Ikeja Road",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.fresh().name, "Okafor Family")

    def test_a_hidden_field_is_refused_even_when_the_value_matches(self):
        response = self.patch(self.no_contact, {"billing_email": "okafor@family.test"})
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_read_only_account_field_is_refused(self):
        account = self.family.receivable_account
        for body in (
            {"is_active": False}, {"source_type": "other"}, {"source_id": "OK-2"},
            {"receivable_account": "1100"},
        ):
            response = self.patch(self.ro_account, body)
            with self.subTest(body=body):
                self.assertEqual(response.status_code, 403, response.data)
        saved = self.fresh()
        self.assertTrue(saved.is_active)
        self.assertEqual((saved.source_type, saved.source_id), ("manual", "OK-1"))
        self.assertEqual(saved.receivable_account_id, account.pk)
        echo = self.patch(self.ro_account, {
            "is_active": True, "source_type": "manual", "source_id": "OK-1",
            "receivable_account": account.code,
        })
        self.assertEqual(echo.status_code, 200, echo.data)

    def test_a_writable_role_changes_the_contact_and_the_response_echoes_it(self):
        response = self.patch(self.open, {"billing_phone": "08031111111"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["billing_phone"], "08031111111")
        self.assertEqual(self.fresh().billing_phone, "08031111111")

    def test_the_response_to_a_write_hides_what_the_caller_may_not_read(self):
        user = self.staff(self.tenant, hide=("billing_phone",))
        response = self.patch(user, {"name": "Okafor Family"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("billing_phone", response.data["data"])
        self.assertIn("billing_email", response.data["data"])

    def test_the_audit_trail_shows_a_change_only_to_a_reader_of_the_fields(self):
        self.patch(self.open, {"billing_email": "new@family.test"})
        auditor = self.staff(self.tenant, hide=("billing_email",))

        def entry(user):
            response = self.api(user).get(
                f"/v1/finance/audit-logs/?target_type=Customer&entity={self.books.code}")
            self.assertEqual(response.status_code, 200, response.data)
            rows = [r for r in response.data["data"] if r["target_type"] == "Customer"
                    and "billing_email" in (r["after"] or {}) | (r["before"] or {})]
            return rows

        self.assertTrue(entry(self.open))
        self.assertEqual(entry(auditor), [])

    # -- create ----------------------------------------------------------------- #

    def body(self, **extra):
        return {
            "name": "Adeyemi Family", "billing_email": "adeyemi@family.test",
            "billing_phone": "08032222222", "branch": self.ikeja.pk, **extra,
        }

    def create(self, user, **extra):
        return self.api(user).post(self.url("customers/"), self.body(**extra), format="json")

    def test_a_read_only_role_can_still_open_a_customer_with_its_contact_and_branch(self):
        response = self.create(self.ro_contact)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["billing_email"], "adeyemi@family.test")

    def test_a_create_that_sets_a_closed_field_is_refused_and_saves_nothing(self):
        before = Customer.objects.filter(entity=self.books).count()
        for extra in (
            {"billing_address": "5 Yaba Road"}, {"source_id": "AD-1"},
            {"is_active": False}, {"receivable_account": "1200"},
        ):
            user = self.ro_contact if "billing_address" in extra else self.ro_account
            response = self.create(user, **extra)
            with self.subTest(extra=extra):
                self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Customer.objects.filter(entity=self.books).count(), before)

    def test_a_create_that_leaves_the_defaults_alone_is_not_refused(self):
        response = self.create(self.ro_account, is_active=True)
        self.assertEqual(response.status_code, 201, response.data)

    def test_an_open_role_creates_with_every_field(self):
        response = self.create(
            self.open, billing_address="5 Yaba Road", source_type="manual", source_id="AD-9")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["billing_address"], "5 Yaba Road")


class CustomerEmailTests(_CustomerFieldAccess):
    """The billing email travels in the email preview, the send messages and the history."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant)
        cls.no_email = cls.staff(cls.tenant, hide=("billing_email",))
        FinanceDocumentDelivery.objects.create(
            entity=cls.books, customer=cls.family, document_type="STATEMENT",
            source="MANUAL", status="SENT", recipients=["okafor@family.test"],
            bcc=["finance@corona.test"],
        )

    def test_the_preview_names_the_address_only_to_a_reader_of_it(self):
        path = self.url(f"customers/{self.family.code}/statement-email/")
        full = self.api(self.open).get(path).data["data"]
        self.assertEqual(full["recipients"], ["okafor@family.test"])
        self.assertEqual(full["deliveries"][0]["recipients"], ["okafor@family.test"])
        hidden = self.api(self.no_email).get(path).data["data"]
        self.assertNotIn("recipients", hidden)
        self.assertTrue(hidden["can_send"])
        self.assertNotIn("recipients", hidden["deliveries"][0])
        self.assertEqual(hidden["deliveries"][0]["bcc"], ["finance@corona.test"])

    def test_a_send_does_not_name_the_address_in_its_message(self):
        path = self.url(f"customers/{self.family.code}/statement-email/")
        with mock.patch("vs_finance.views_document_email.send_statement") as send:
            send.return_value = FinanceDocumentDelivery.objects.filter(
                customer=self.family).first()
            hidden = self.api(self.no_email).post(path, {}, format="json")
            shown = self.api(self.open).post(path, {}, format="json")
        self.assertNotIn("okafor@family.test", hidden.data["message"])
        self.assertNotIn("recipients", hidden.data["data"])
        self.assertIn("okafor@family.test", shown.data["message"])

    def test_the_service_preview_still_knows_the_address_for_the_send(self):
        self.assertEqual(
            email_preview(
                customer=self.family, document_type="STATEMENT", entity=self.books,
            )["recipients"],
            ["okafor@family.test"],
        )


class CustomerAuditTests(_CustomerFieldAccess):
    """The central trail never repeats the addresses an emailed document went to."""

    def entry(self, **metadata):
        from types import SimpleNamespace

        return SimpleNamespace(
            module_key="FINANCE", entity_type="vs_finance.InvoiceDelivery",
            action_type="FINANCIAL_TRANSACTION", entity_label="INV-1",
            summary="Invoice sent.", metadata={"recipients": ["a@b.test"], **metadata},
        )

    def test_the_addresses_are_in_the_central_trail_for_nobody(self):
        from vs_audit.protected_words import visible_metadata

        event = self.entry(finance_action="x")
        for reader in (lambda key: True, lambda key: key != f"{CUSTOMER}.billing_email"):
            shown = visible_metadata(event, reader)
            self.assertNotIn("recipients", shown)
            self.assertEqual(shown["finance_action"], "x")


class CustomerDocumentTests(_CustomerFieldAccess):
    """A document addressed to the customer reads the same whoever prints it."""

    def test_the_bill_to_block_is_a_declared_system_surface(self):
        self.assertIn("vs_finance.documents._customer_block", SYSTEM_SURFACES)

    def test_an_invoice_prints_the_customers_contact_whatever_the_caller_may_read(self):
        context = invoice_document_context(self.ikeja_invoice)
        self.assertEqual(context["customer"]["email"], "okafor@family.test")
        self.assertEqual(context["customer"]["phone"], "08030000001")
        self.assertEqual(context["customer"]["address"], "4 Ikeja Road")
        self.assertEqual(_customer_block(self.family)["customer_code"], "OKAFOR")


class CustomerExportTests(_CustomerFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.open = cls.staff(cls.tenant)
        cls.no_contact = cls.staff(cls.tenant, hide=CONTACT)
        cls.no_account = cls.staff(cls.tenant, hide=ACCOUNT + ("account_status",))
        cls.dataset = get_dataset("finance.customers")

    def columns(self, user):
        fields, _ = engine.resolve_columns(
            user, self.dataset, [f.id for f in self.dataset.fields], self.tenant)
        return {f.id for f in fields}

    def test_a_hidden_field_is_not_a_column_of_the_customer_export(self):
        self.assertIn("billing_email", self.columns(self.open) | {"billing_email"})
        columns = self.columns(self.no_contact)
        for name in CONTACT_NAMES:
            self.assertNotIn(name, columns)
        self.assertIn("name", columns)
        columns = self.columns(self.no_account)
        for name in ("is_active", "receivable_account", "opening_balance", "created_at"):
            self.assertNotIn(name, columns)

    def test_the_invoice_export_hides_the_customers_contact_columns(self):
        dataset = get_dataset("finance.customer_invoices")
        fields, _ = engine.resolve_columns(
            self.no_contact, dataset, [f.id for f in dataset.fields], self.tenant)
        ids = {f.id for f in fields}
        self.assertNotIn("customer_email", ids)
        self.assertNotIn("customer_phone", ids)
        self.assertIn("customer_name", ids)

    def test_a_filter_on_a_hidden_column_is_refused(self):
        readable = engine.readable_fields(self.no_account, self.tenant)
        for filter_id, value in (
            ("is_active", {"id": "is_active", "value": True}),
            ("account_status", {"id": "account_status", "values": ["ACTIVE"]}),
        ):
            with self.subTest(filter=filter_id), self.assertRaises(engine.FilterReadDenied):
                engine.assert_filters_readable(self.dataset, [value], readable)

    def test_the_catalogue_offers_neither_the_column_nor_the_filter(self):
        readable = engine.readable_fields(self.no_account, self.tenant)
        described = self.dataset.describe(readable=readable)
        self.assertNotIn("is_active", {f["id"] for f in described["fields"]})
        self.assertNotIn("is_active", {f["id"] for f in described["filters"]})
        self.assertNotIn("account_status", {f["id"] for f in described["filters"]})


class CustomerIsolationTests(_CustomerFieldAccess):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.closed = cls.staff(cls.tenant, hide=CONTACT)
        cls.rival_open = cls.staff(cls.rival_tenant)

    def test_one_schools_role_does_not_close_another_schools_customers(self):
        row = self.row(self.rival_open, self.rival_customer, self.rival_books)
        self.assertEqual(row["billing_email"], "rival@family.test")

    def test_a_closed_role_never_reaches_another_schools_customer(self):
        response = self.api(self.closed).get(
            f"/v1/finance/customers/{self.rival_customer.code}/?entity={self.rival_books.code}")
        self.assertIn(response.status_code, (403, 404))
        response = self.api(self.closed).get(
            f"/v1/finance/customers/{self.rival_customer.code}/?entity={self.books.code}")
        self.assertEqual(response.status_code, 404)

    def test_the_closed_role_still_reads_every_field_of_another_resource(self):
        payload = field_access_payload(self.closed, self.tenant)
        self.assertEqual(set(payload), {CUSTOMER})


class PlatformCustomerMirrorTests(_CustomerFieldAccess):
    """CodeX's customer for a school is written by a signal, with no request behind it."""

    def test_the_mirror_writes_the_contact_whatever_any_role_may_write(self):
        from schools.vs_schools.services.platform_customer import (
            SOURCE_TYPE, sync_registered_platform_customer,
        )
        from vs_finance.models import LedgerEntity

        platform = LedgerEntity.objects.platform()
        customer = Customer.objects.create(
            entity=platform, code="SCH-1", name="Corona Group", source_type=SOURCE_TYPE,
            source_id=str(self.school.pk), billing_email="old@corona.test",
        )
        self.school.email = "accounts@corona.test"
        self.school.phone = "08034444444"
        self.school.address = "1 Corona Way"
        self.assertTrue(sync_registered_platform_customer(self.school))
        customer.refresh_from_db()
        self.assertEqual(
            (customer.billing_email, customer.billing_phone, customer.billing_address),
            ("accounts@corona.test", "08034444444", "1 Corona Way"),
        )


class CustomerOpeningImportTests(_CustomerFieldAccess):
    """The opening import writes bills, not the customer record, and serves no customer contact."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.no_contact = cls.staff(cls.tenant, hide=CONTACT)

    def test_the_import_response_carries_no_customer_contact(self):
        from vs_finance.models import Invoice

        invoice = Invoice.objects.filter(customer=self.family).first()
        from vs_finance.serializers import InvoiceSerializer

        data = InvoiceSerializer(invoice).data
        for name in CONTACT_NAMES:
            self.assertNotIn(name, data)
