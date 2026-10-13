"""A payout to a vendor shows the vendor's banking only to somebody who may read it on the vendor.

Corona pays Ade Stationers from Ikeja. The payout line copies the vendor's
account name, account number and bank code when it is made, so the same three
values sit on the vendor record and on the payout. Mrs Okafor, the bursar, may
read the payout's beneficiary and the vendor's bank details. Tunde, the clerk,
may read the payout's beneficiary but his role hides the vendor's account
number: he must not be able to read it off the payout instead, on the payout
line, in the movements feed, on the approval document or in an export. A held
settlement paid to the school's own bank copies nothing from a vendor, so it
follows the payout switches alone.

Two shapes of school, a multi-branch one and a single-branch one, and a rival
tenant whose switches and rows must never meet either.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient
from vs_finance.tests_branch_scope import _FinanceBranchFixture
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, make_branch, set_field_access

from .constants import PayoutStatus
from .models import PayoutBatch, PayoutInstruction

BENEFICIARY = ("beneficiary_name", "beneficiary_account_number", "beneficiary_bank_code")
VENDOR_FIELD = {
    "beneficiary_name": "bank_account_name",
    "beneficiary_account_number": "bank_account_number",
    "beneficiary_bank_code": "bank_code",
}


class PayoutVendorBankingTests(_FinanceBranchFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_branch(cls.rival_school, name="Lekki Branch", is_main=False)
        cls.payout_keys = install_declared_fields("payments.payout")
        cls.vendor_keys = install_declared_fields("procurement.vendor")
        cls.lines = {}
        for books, tag, branch in (
            (cls.books, "multi", cls.ikeja),
            (cls.solo_books, "solo", cls.solo_main),
            (cls.rival_books, "rival", cls.rival_branch),
        ):
            cls.lines[tag] = (cls.payout(books, branch, f"{tag}-vendor", vendor=True),
                              cls.payout(books, branch, f"{tag}-own", vendor=False))

    @classmethod
    def payout(cls, books, branch, ref, *, vendor):
        batch = PayoutBatch.objects.create(
            entity=books, branch=branch, provider="PAYSTACK", reference=f"B-{ref}",
            total_amount=50_000_00, item_count=1,
        )
        return PayoutInstruction.objects.create(
            entity=books, branch=branch, batch=batch, provider="PAYSTACK",
            reference=f"P-{ref}", amount=50_000_00, currency=books.base_currency,
            beneficiary_name="Ade Stationers Ltd", beneficiary_account_number="0123456789",
            beneficiary_bank_code="058", status=PayoutStatus.PENDING,
            vendor_source_type="vs_procurement.Vendor" if vendor else "",
            vendor_source_id="1" if vendor else "",
        )

    @classmethod
    def reader(cls, tenant, tag, *, payout_hidden=(), vendor_hidden=(), branch=None, extra=()):
        """A user whose role hides the named payout and vendor fields and shows the rest."""
        user = cls.user_for(tenant, f"{tag}@pay.test")
        cls.grant(user, "payments.payout.view", "payments.payout_batch.view", *extra,
                  tenant=tenant, role_key=tag, branch=branch)
        role = TenantRoleTemplate.objects.get(tenant=tenant, key=tag)
        set_field_access(role, *cls.payout_keys, read=True, write=False)
        for key in cls.payout_keys:
            if key.rsplit(".", 1)[1] in payout_hidden:
                set_field_access(role, key, read=False, write=False)
        set_field_access(role, *cls.vendor_keys, read=True, write=True)
        for key in cls.vendor_keys:
            if key.rsplit(".", 1)[1] in vendor_hidden:
                set_field_access(role, key, read=False, write=False)
        return user

    def rows(self, user, books, path="payouts/"):
        response = TenantAPIClient(user=user).get(f"/v1/payments/{path}?entity={books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["reference"]: row for row in response.data["data"]}

    def test_a_vendor_line_shows_each_value_only_when_both_switches_are_readable(self):
        for beneficiary, vendor_field in VENDOR_FIELD.items():
            with self.subTest(vendor_field=vendor_field):
                user = self.reader(self.tenant, f"clerk-{vendor_field}",
                                   vendor_hidden=(vendor_field,), branch=self.ikeja)
                row = self.rows(user, self.books)["P-multi-vendor"]
                self.assertNotIn(beneficiary, row)
                for other in set(BENEFICIARY) - {beneficiary}:
                    self.assertIn(other, row)

    def test_a_reader_of_both_switches_sees_the_whole_beneficiary(self):
        user = self.reader(self.tenant, "bursar", branch=self.ikeja)
        row = self.rows(user, self.books)["P-multi-vendor"]
        self.assertEqual(
            [row[name] for name in BENEFICIARY], ["Ade Stationers Ltd", "0123456789", "058"],
        )

    def test_the_payout_switch_still_hides_what_the_vendor_switch_shows(self):
        user = self.reader(self.tenant, "no-payout", payout_hidden=BENEFICIARY, branch=self.ikeja)
        row = self.rows(user, self.books)["P-multi-vendor"]
        for name in BENEFICIARY:
            self.assertNotIn(name, row)

    def test_the_batch_detail_nests_the_same_rule(self):
        user = self.reader(self.tenant, "batch-clerk", vendor_hidden=("bank_account_number",),
                           branch=self.ikeja)
        batch = PayoutBatch.objects.get(reference="B-multi-vendor")
        response = TenantAPIClient(user=user).get(
            f"/v1/payments/payout-batches/{batch.pk}/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        (line,) = response.data["data"]["instructions"]
        self.assertNotIn("beneficiary_account_number", line)
        self.assertEqual(line["beneficiary_name"], "Ade Stationers Ltd")

    def test_a_payout_to_the_schools_own_bank_follows_the_payout_switches_alone(self):
        user = self.reader(self.tenant, "own-bank", branch=self.ikeja,
                           vendor_hidden=tuple(VENDOR_FIELD.values()))
        row = self.rows(user, self.books)["P-multi-own"]
        self.assertEqual(
            [row[name] for name in BENEFICIARY], ["Ade Stationers Ltd", "0123456789", "058"],
        )

    def test_a_single_branch_school_is_held_to_the_same_rule(self):
        user = self.reader(self.solo_tenant, "solo-clerk", vendor_hidden=("bank_code",))
        rows = self.rows(user, self.solo_books)
        self.assertNotIn("beneficiary_bank_code", rows["P-solo-vendor"])
        self.assertIn("beneficiary_bank_code", rows["P-solo-own"])

    def test_one_schools_switches_do_not_reach_another_and_its_rows_are_never_served(self):
        hidden = self.reader(self.tenant, "hides-vendor-bank", branch=self.ikeja,
                             vendor_hidden=tuple(VENDOR_FIELD.values()))
        rival = self.reader(self.rival_tenant, "hides-vendor-bank", branch=self.rival_branch)
        self.assertEqual(
            [self.rows(rival, self.rival_books)["P-rival-vendor"][name] for name in BENEFICIARY],
            ["Ade Stationers Ltd", "0123456789", "058"],
        )
        self.assertNotIn("beneficiary_name", self.rows(hidden, self.books)["P-multi-vendor"])
        response = TenantAPIClient(user=hidden).get(
            f"/v1/payments/payouts/?entity={self.rival_books.code}")
        self.assertIn(response.status_code, (403, 404))


class _SurfaceFixture(PayoutVendorBankingTests):
    """The same schools and readers, held to the rule on the other surfaces."""

    # The inherited tests are not repeated on each surface.
    test_a_vendor_line_shows_each_value_only_when_both_switches_are_readable = None
    test_a_reader_of_both_switches_sees_the_whole_beneficiary = None
    test_the_payout_switch_still_hides_what_the_vendor_switch_shows = None
    test_the_batch_detail_nests_the_same_rule = None
    test_a_payout_to_the_schools_own_bank_follows_the_payout_switches_alone = None
    test_a_single_branch_school_is_held_to_the_same_rule = None
    test_one_schools_switches_do_not_reach_another_and_its_rows_are_never_served = None


class MovementsFeedTests(_SurfaceFixture):
    """The feed's ``party`` and ``beneficiary_account`` follow the line they restate."""

    FEED = {"party": "beneficiary_name", "beneficiary_account": "beneficiary_account_number"}

    def feed(self, user, books):
        response = TenantAPIClient(user=user).get(
            f"/v1/payments/movements/?direction=out&entity={books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return {row["reference"]: row for row in response.data["data"]}

    def clerk(self, tenant, tag, **kwargs):
        return self.reader(tenant, tag, extra=("payments.report.view",), **kwargs)

    def test_a_vendor_row_drops_a_value_whose_vendor_switch_is_closed(self):
        for feed_name, beneficiary in self.FEED.items():
            with self.subTest(feed=feed_name):
                user = self.clerk(self.tenant, f"feed-{feed_name}", branch=self.ikeja,
                                  vendor_hidden=(VENDOR_FIELD[beneficiary],))
                rows = self.feed(user, self.books)
                self.assertNotIn(feed_name, rows["P-multi-vendor"])
                for other in set(self.FEED) - {feed_name}:
                    self.assertIn(other, rows["P-multi-vendor"])
                self.assertIn(feed_name, rows["P-multi-own"])
                self.assertNotIn("vendor_payout", rows["P-multi-vendor"])

    def test_a_reader_of_both_switches_sees_the_vendor_row_whole(self):
        rows = self.feed(self.clerk(self.tenant, "feed-bursar", branch=self.ikeja), self.books)
        self.assertEqual(
            (rows["P-multi-vendor"]["party"], rows["P-multi-vendor"]["beneficiary_account"]),
            ("Ade Stationers Ltd", "0123456789"),
        )

    def test_the_payout_switch_still_hides_both_kinds_of_row(self):
        user = self.clerk(self.tenant, "feed-no-payout", branch=self.ikeja,
                          payout_hidden=BENEFICIARY)
        for row in self.feed(user, self.books).values():
            self.assertNotIn("party", row)
            self.assertNotIn("beneficiary_account", row)

    def test_a_payout_to_the_schools_own_bank_ignores_the_vendor_switches(self):
        user = self.clerk(self.tenant, "feed-own", branch=self.ikeja,
                          vendor_hidden=tuple(VENDOR_FIELD.values()))
        row = self.feed(user, self.books)["P-multi-own"]
        self.assertEqual((row["party"], row["beneficiary_account"]),
                         ("Ade Stationers Ltd", "0123456789"))

    def test_a_single_branch_school_is_held_to_the_same_rule(self):
        user = self.clerk(self.solo_tenant, "feed-solo",
                          vendor_hidden=("bank_account_number",))
        rows = self.feed(user, self.solo_books)
        self.assertNotIn("beneficiary_account", rows["P-solo-vendor"])
        self.assertIn("party", rows["P-solo-vendor"])
        self.assertIn("beneficiary_account", rows["P-solo-own"])

    def test_another_schools_switches_and_rows_stay_apart(self):
        closed = self.clerk(self.tenant, "feed-closed", branch=self.ikeja,
                            vendor_hidden=tuple(VENDOR_FIELD.values()))
        rival = self.clerk(self.rival_tenant, "feed-closed", branch=self.rival_branch)
        self.assertIn("party", self.feed(rival, self.rival_books)["P-rival-vendor"])
        self.assertNotIn("party", self.feed(closed, self.books)["P-multi-vendor"])
        response = TenantAPIClient(user=closed).get(
            f"/v1/payments/movements/?direction=out&entity={self.rival_books.code}")
        self.assertIn(response.status_code, (403, 404))


class ApprovalDocumentTests(_SurfaceFixture):
    """The approval document's beneficiary table follows the lines it lists."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # One batch holding a vendor line and a line to the school's own bank.
        cls.mixed = PayoutInstruction.objects.get(reference="P-multi-own")
        cls.vendor_line = PayoutInstruction.objects.get(reference="P-multi-vendor")
        cls.mixed.batch = cls.vendor_line.batch
        cls.mixed.save(update_fields=["batch"])
        cls.vendor_batch = PayoutBatch.objects.get(reference="B-multi-vendor")
        cls.solo_batch = PayoutBatch.objects.get(reference="B-solo-vendor")
        cls.rival_batch = PayoutBatch.objects.get(reference="B-rival-vendor")

    def shown(self, user, batch):
        from rest_framework.test import APIRequestFactory

        from vs_workflow.presentation import for_reader

        from .workflow_handlers import PayoutBatchApprovalHandler

        stored = PayoutBatchApprovalHandler().get_document_details(batch)
        request = APIRequestFactory().get("/")
        request.user = user
        request.tenant = user.tenant
        return stored, for_reader(stored, request)

    @staticmethod
    def tables(details):
        return {s["title"]: [c["key"] for c in s["columns"]]
                for s in details["sections"] if s["kind"] == "table"}

    def test_a_batch_of_both_kinds_lists_vendor_lines_apart(self):
        user = self.reader(self.tenant, "doc-open", branch=self.ikeja)
        stored, shown = self.shown(user, self.vendor_batch)
        self.assertEqual(
            self.tables(shown),
            {"Vendor beneficiaries": ["beneficiary", "account", "amount", "narration"],
             "Other beneficiaries": ["beneficiary", "account", "amount", "narration"]},
        )
        self.assertEqual(stored, self.shown(user, self.vendor_batch)[0])

    def test_a_closed_vendor_switch_removes_the_column_from_the_vendor_lines_only(self):
        for vendor_field, column in (("bank_account_name", "beneficiary"),
                                     ("bank_account_number", "account")):
            with self.subTest(vendor_field=vendor_field):
                user = self.reader(self.tenant, f"doc-{vendor_field}", branch=self.ikeja,
                                   vendor_hidden=(vendor_field,))
                stored, shown = self.shown(user, self.vendor_batch)
                tables = self.tables(shown)
                self.assertNotIn(column, tables["Vendor beneficiaries"])
                self.assertIn(column, tables["Other beneficiaries"])
                self.assertIn("amount", tables["Vendor beneficiaries"])
                self.assertEqual(stored, self.shown(user, self.vendor_batch)[0])

    def test_a_batch_of_vendor_lines_alone_keeps_the_plain_title(self):
        user = self.reader(self.solo_tenant, "doc-solo", vendor_hidden=("bank_account_number",))
        _, shown = self.shown(user, self.solo_batch)
        self.assertEqual(self.tables(shown), {"Beneficiaries": ["beneficiary", "amount", "narration"]})
        user = self.reader(self.solo_tenant, "doc-solo-open")
        _, shown = self.shown(user, self.solo_batch)
        self.assertIn("account", self.tables(shown)["Beneficiaries"])

    def test_the_payout_switch_removes_the_columns_from_every_table(self):
        user = self.reader(self.tenant, "doc-no-payout", branch=self.ikeja,
                           payout_hidden=BENEFICIARY)
        _, shown = self.shown(user, self.vendor_batch)
        for columns in self.tables(shown).values():
            self.assertEqual(columns, ["amount", "narration"])

    def test_a_batch_to_the_schools_own_bank_ignores_the_vendor_switches(self):
        own = PayoutInstruction.objects.get(reference="P-solo-own")
        batch = PayoutBatch.objects.get(pk=own.batch_id)
        user = self.reader(self.solo_tenant, "doc-own",
                           vendor_hidden=tuple(VENDOR_FIELD.values()))
        _, shown = self.shown(user, batch)
        self.assertEqual(self.tables(shown),
                         {"Beneficiaries": ["beneficiary", "account", "amount", "narration"]})

    def test_one_schools_switches_do_not_reach_another(self):
        closed = self.reader(self.tenant, "doc-closed", branch=self.ikeja,
                             vendor_hidden=tuple(VENDOR_FIELD.values()))
        rival = self.reader(self.rival_tenant, "doc-closed", branch=self.rival_branch)
        self.assertEqual(
            self.tables(self.shown(rival, self.rival_batch)[1]),
            {"Beneficiaries": ["beneficiary", "account", "amount", "narration"]},
        )
        self.assertEqual(
            self.tables(self.shown(closed, self.vendor_batch)[1])["Vendor beneficiaries"],
            ["amount", "narration"],
        )


class ExportTests(_SurfaceFixture):
    """The payout export cuts a vendor line's values where the vendor switch is closed."""

    COLUMNS = ["reference", *BENEFICIARY]
    BLANK = "-"  # How the export writes a value with nothing in it.

    def exported(self, user, tenant, books):
        from vs_exports.catalogue import ScopeContext, get_dataset
        from vs_exports.engine import sample_rows

        config = {
            "columns": self.COLUMNS,
            "filters": [{"id": "created_at", "start": "2000-01-01", "end": "2100-01-01"}],
        }
        labels, rows = sample_rows(
            user, get_dataset("payments.payouts"),
            ScopeContext(tenant=tenant, entity=books, user=user), config, tenant,
        )
        return labels, {row[0]: dict(zip(labels[1:], row[1:])) for row in rows}

    def exporter(self, tenant, tag, **kwargs):
        return self.reader(tenant, tag, extra=("exports.sensitive_field.export",), **kwargs)

    def test_a_vendor_row_is_blank_where_its_vendor_switch_is_closed(self):
        for vendor_field, label in (("bank_account_name", "Beneficiary"),
                                    ("bank_account_number", "Account number"),
                                    ("bank_code", "Bank code")):
            with self.subTest(vendor_field=vendor_field):
                user = self.exporter(self.tenant, f"exp-{vendor_field}", branch=self.ikeja,
                                     vendor_hidden=(vendor_field,))
                labels, rows = self.exported(user, self.tenant, self.books)
                self.assertEqual(rows["P-multi-vendor"][label], self.BLANK)
                self.assertNotEqual(rows["P-multi-own"][label], self.BLANK)
                for other in {"Beneficiary", "Account number", "Bank code"} - {label}:
                    self.assertNotEqual(rows["P-multi-vendor"][other], "")

    def test_a_reader_of_both_switches_exports_the_whole_beneficiary(self):
        user = self.exporter(self.tenant, "exp-bursar", branch=self.ikeja)
        _, rows = self.exported(user, self.tenant, self.books)
        self.assertEqual(
            list(rows["P-multi-vendor"].values()), ["Ade Stationers Ltd", "0123456789", "058"],
        )

    def test_the_payout_switch_still_drops_the_column_for_every_row(self):
        user = self.exporter(self.tenant, "exp-no-payout", branch=self.ikeja,
                             payout_hidden=("beneficiary_account_number",))
        labels, rows = self.exported(user, self.tenant, self.books)
        self.assertNotIn("Account number", labels)
        self.assertNotIn("0123456789", str(rows))

    def test_a_payout_to_the_schools_own_bank_is_exported_whatever_the_vendor_switches_say(self):
        user = self.exporter(self.tenant, "exp-own", branch=self.ikeja,
                             vendor_hidden=tuple(VENDOR_FIELD.values()))
        _, rows = self.exported(user, self.tenant, self.books)
        self.assertEqual(
            list(rows["P-multi-own"].values()), ["Ade Stationers Ltd", "0123456789", "058"],
        )
        self.assertEqual(set(rows["P-multi-vendor"].values()), {self.BLANK})

    def test_a_single_branch_school_is_held_to_the_same_rule(self):
        user = self.exporter(self.solo_tenant, "exp-solo", vendor_hidden=("bank_code",))
        _, rows = self.exported(user, self.solo_tenant, self.solo_books)
        self.assertEqual(rows["P-solo-vendor"]["Bank code"], self.BLANK)
        self.assertEqual(rows["P-solo-own"]["Bank code"], "058")

    def test_another_schools_switches_and_rows_stay_apart(self):
        closed = self.exporter(self.tenant, "exp-closed", branch=self.ikeja,
                               vendor_hidden=tuple(VENDOR_FIELD.values()))
        rival = self.exporter(self.rival_tenant, "exp-closed", branch=self.rival_branch)
        _, rival_rows = self.exported(rival, self.rival_tenant, self.rival_books)
        self.assertEqual(rival_rows["P-rival-vendor"]["Account number"], "0123456789")
        _, rows = self.exported(closed, self.tenant, self.books)
        self.assertNotIn("P-rival-vendor", rows)
        self.assertEqual(rows["P-multi-vendor"]["Account number"], self.BLANK)
