"""The open bills a credit or a payment may settle, listed from the server.

Corona's bursar, Mrs Okafor, applies the credit left on a Chuks Stationery
note by choosing bills. The drawer used to read the vendor's newest 100 posted
bills and keep the unpaid ones, so a supplier billing weekly hid last year's
unpaid bill behind a hundred paid ones. It now reads the bills applying the
credit would settle, due first, from the same rule the server settles them
by, with the total so it can say how many more wait behind a full page. A
vendor payment's eligible bills are paged the same way rather than cut off at
a hundred without a word.
"""
from __future__ import annotations

from core.test_utils import TenantAPIClient

from .corrections import credit_note_open_bills, post_vendor_credit_note
from .tests_ap_corrections import JAN, _APCorrectionsFixture, _roles

ALLOCATE = ("procurement.vendor_credit_note.view", "procurement.vendor_credit_note.allocate")


class _OpenBillsFixture(_APCorrectionsFixture):

    def officer(self, email, *branches, keys=ALLOCATE, tenant=None):
        tenant = tenant or self.tenant
        user = self.user_for(tenant, email)
        for branch in branches:
            self.grant(user, *keys, tenant=tenant, role_key=f"open-bills-{next(_roles)}", branch=branch)
        return TenantAPIClient(user=user)

    def url(self, path, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        return f"/v1/procurement/{path}?entity={self.books.code}{'&' + query if query else ''}"

    def credit_left(self):
        """A posted Ikeja note holding N40,000 of credit, from a bill paid in full."""
        bill = self.non_po_bill(100_000, tax=False, day=2)
        self.pay(bill, 100_000, day=3)
        note = self.credit_note(bill, amount=40_000, day=4)
        post_vendor_credit_note(note)
        note.refresh_from_db()
        return note


class CreditNoteOpenBillsTests(_OpenBillsFixture):

    def test_lists_ikejas_unpaid_bills_due_first_and_nothing_paid_or_lekkis(self):
        note = self.credit_left()
        late = self.non_po_bill(30_000, tax=False, day=20)
        early = self.non_po_bill(20_000, tax=False, day=6)
        self.non_po_bill(25_000, branch=self.lekki, tax=False, day=7)
        paid = self.non_po_bill(10_000, tax=False, day=5)
        self.pay(paid, 10_000, day=8)
        okafor = self.officer("okafor@corona.test", self.ikeja)

        body = okafor.get(self.url(f"vendor-credit-notes/{note.pk}/open-bills/")).json()

        self.assertEqual([row["id"] for row in body["data"]], [early.pk, late.pk])
        self.assertEqual(body["pagination"]["totalItems"], 2)
        self.assertEqual(body["data"][0]["balance_due"], 20_000)

    def test_lists_exactly_what_applying_it_automatically_would_settle(self):
        note = self.credit_left()
        self.non_po_bill(30_000, tax=False, day=20)
        self.non_po_bill(20_000, tax=False, day=6)
        okafor = self.officer("okafor-auto@corona.test", self.ikeja)

        listed = [row["id"] for row in okafor.get(self.url(f"vendor-credit-notes/{note.pk}/open-bills/")).json()["data"]]

        self.assertEqual(listed, list(credit_note_open_bills(note).values_list("id", flat=True)))

    def test_says_how_many_more_wait_behind_a_full_page(self):
        note = self.credit_left()
        for day in (6, 7, 8):
            self.non_po_bill(10_000, tax=False, day=day)
        okafor = self.officer("okafor-page@corona.test", self.ikeja)

        body = okafor.get(self.url(f"vendor-credit-notes/{note.pk}/open-bills/", page_size=2)).json()

        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["pagination"]["totalItems"], 3)

    def test_a_note_with_no_credit_left_lists_none(self):
        bill = self.non_po_bill(100_000, tax=False, day=2)
        note = self.credit_note(bill, amount=40_000, day=4)
        post_vendor_credit_note(note)
        self.non_po_bill(30_000, tax=False, day=20)
        okafor = self.officer("okafor-none@corona.test", self.ikeja)

        body = okafor.get(self.url(f"vendor-credit-notes/{note.pk}/open-bills/")).json()

        self.assertEqual(body["data"], [])

    def test_needs_the_key_to_apply_credit(self):
        note = self.credit_left()
        viewer = self.officer("viewer@corona.test", self.ikeja, keys=("procurement.vendor_credit_note.view",))

        response = viewer.get(self.url(f"vendor-credit-notes/{note.pk}/open-bills/"))

        self.assertEqual(response.status_code, 403)

    def test_another_branch_cannot_read_ikejas_note(self):
        note = self.credit_left()
        lekki = self.officer("lekki@corona.test", self.lekki)

        response = lekki.get(self.url(f"vendor-credit-notes/{note.pk}/open-bills/"))

        self.assertEqual(response.status_code, 404)

    def test_another_school_cannot_read_coronas_note(self):
        note = self.credit_left()
        rival = self.officer("rival@rival.test", self.rival_branch, tenant=self.rival_tenant)

        refused = rival.get(self.url(f"vendor-credit-notes/{note.pk}/open-bills/"))
        own_books = rival.get(f"/v1/procurement/vendor-credit-notes/{note.pk}/open-bills/?entity={self.rival_books.code}")

        self.assertIn(refused.status_code, (403, 404))
        self.assertEqual(own_books.status_code, 404)


class VendorPaymentEligibleBillsTests(_OpenBillsFixture):

    PAY_KEYS = ("procurement.vendor_payment.view",)

    def test_pages_the_unpaid_bills_due_first_and_counts_them_all(self):
        bills = [self.non_po_bill(10_000, tax=False, day=day) for day in (9, 6, 7)]
        paid = self.non_po_bill(10_000, tax=False, day=5)
        self.pay(paid, 10_000, day=8)
        okafor = self.officer("okafor-pay@corona.test", self.ikeja, keys=self.PAY_KEYS)

        body = okafor.get(self.url("vendor-payments/eligible-invoices/", vendor="CHUKS", page_size=2)).json()

        self.assertEqual([row["id"] for row in body["data"]], [bills[1].pk, bills[2].pk])
        self.assertEqual(body["pagination"]["totalItems"], 3)
        self.assertIn("subtotal", body["data"][0])

    def test_a_lekki_clerk_is_not_offered_ikejas_bills(self):
        self.non_po_bill(10_000, tax=False, day=6)
        lekki = self.officer("lekki-pay@corona.test", self.lekki, keys=self.PAY_KEYS)

        body = lekki.get(self.url("vendor-payments/eligible-invoices/", vendor="CHUKS")).json()

        self.assertEqual(body["data"], [])
        self.assertEqual(body["pagination"]["totalItems"], 0)

    def test_needs_the_key_to_view_payments(self):
        nobody = self.officer("nobody@corona.test", self.ikeja, keys=("procurement.vendor_invoice.view",))

        response = nobody.get(self.url("vendor-payments/eligible-invoices/", vendor="CHUKS"))

        self.assertEqual(response.status_code, 403)
