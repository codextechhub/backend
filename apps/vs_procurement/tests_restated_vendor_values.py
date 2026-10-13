"""Vendor values travel under other names: a portal submitter, a category total, a delivery history.

Bright Star School files Ade Stationers under Stationery. Amina Bello is the
contact who receives its orders, and she is also the person who submitted its
quotation from the vendor portal. Mrs Okafor, the procurement officer, may
read the quotation, the order and the category screen, but her role hides the
contact people's emails and the vendor's category:

* the quotation's submission list prints Amina's email as who submitted it;
* the category screen totals Stationery's spend, which says which vendors are
  filed there;
* the order's delivery history counts the people it went to and quotes the
  mail system's complaint, which names the address that bounced.

Each follows the switch that governs the value it restates. A school with one
branch is held to the same switches, and another school's roles and rows never
meet these.
"""
from __future__ import annotations

import datetime

from django.utils import timezone

from .constants import PurchaseOrderVendorDeliverySource, PurchaseOrderVendorDeliveryStatus
from .models import (
    PurchaseOrder,
    PurchaseOrderVendorDelivery,
    RequestForQuotation,
    VendorQuotation,
    VendorQuotationSubmission,
)
from .tests_vendor_field_access import _VendorAccessFixture

BOUNCE = "550 5.1.1 <amina@multi.example>: Recipient address rejected: amina@multi.example"


class _RestatedFixture(_VendorAccessFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for books in (cls.multi, cls.flat, cls.foreign):
            cls.fill_documents(books)
        for user, role, tenant in (
            (cls.officer_user, cls.officer_role, cls.multi_tenant),
            (cls.flat_user, cls.flat_role, cls.flat_tenant),
            (cls.foreign_user, cls.foreign_role, cls.foreign_tenant),
        ):
            cls.grant(user, "procurement.quotation.view", tenant=tenant,
                      role_key="officer", branch=None)

    @classmethod
    def fill_documents(cls, books):
        """A quotation submitted from the portal and an order whose email bounced."""
        rfq = RequestForQuotation.objects.create(
            entity=books.entity, title="Chalk", issue_date=datetime.date(2026, 1, 10),
        )
        books.quotation = VendorQuotation.objects.create(
            entity=books.entity, rfq=rfq, vendor=books.vendor, quote_date=datetime.date(2026, 1, 12),
        )
        VendorQuotationSubmission.objects.create(
            quotation=books.quotation, revision=1, rfq_version=1,
            submitted_at=timezone.now(), submitted_by_email=books.amina.email, snapshot={},
        )
        books.order = PurchaseOrder.objects.create(
            entity=books.entity, vendor=books.vendor, order_date=datetime.date(2026, 1, 5),
            status="APPROVED",
        )
        PurchaseOrderVendorDelivery.objects.create(
            purchase_order=books.order, source=PurchaseOrderVendorDeliverySource.MANUAL,
            status=PurchaseOrderVendorDeliveryStatus.FAILED,
            recipients=[books.amina.email, books.tunde.email], bcc=["procurement@school.example"],
            failure_reason=BOUNCE,
        )


class SubmittedByEmailTests(_RestatedFixture):

    def submissions(self, books=None, user=None):
        books = books or self.multi
        response = self.call(
            user or self.officer_user, f"quotations/{books.quotation.pk}/", books=books)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["submissions"]

    def test_the_submitters_email_is_a_contact_email_and_follows_its_switch(self):
        self.open_everything()
        (shown,) = self.submissions()
        self.assertEqual(shown["submitted_by_email"], "amina@multi.example")

        self.switch(self.officer_role, self.contact_keys, {"email": "hidden"})
        (hidden,) = self.submissions()
        self.assertNotIn("submitted_by_email", hidden)
        self.assertEqual((hidden["revision"], hidden["rfq_version"]), (1, 1))
        self.assertNotIn("amina@multi.example", str(self.submissions()))

    def test_another_contact_detail_does_not_govern_the_email(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"name": "hidden", "phone": "hidden"})

        (shown,) = self.submissions()

        self.assertIn("submitted_by_email", shown)

    def test_a_role_that_never_opened_contact_details_reads_no_email(self):
        (row,) = self.submissions()

        self.assertNotIn("submitted_by_email", row)

    def test_a_single_branch_school_is_held_to_the_same_switch(self):
        self.switch(self.flat_role, self.contact_keys, {"email": "hidden"})
        (row,) = self.submissions(self.flat, self.flat_user)
        self.assertNotIn("submitted_by_email", row)
        self.switch(self.flat_role, self.contact_keys, {})
        (row,) = self.submissions(self.flat, self.flat_user)
        self.assertEqual(row["submitted_by_email"], "amina@flat.example")

    def test_one_schools_switch_does_not_reach_another_and_its_quotation_is_not_served(self):
        self.switch(self.officer_role, self.contact_keys, {"email": "hidden"})
        self.switch(self.foreign_role, self.contact_keys, {})

        (row,) = self.submissions(self.foreign, self.foreign_user)
        self.assertEqual(row["submitted_by_email"], "amina@foreign.example")
        refused = self.call(
            self.officer_user, f"quotations/{self.foreign.quotation.pk}/", books=self.foreign)
        self.assertIn(refused.status_code, (403, 404))


class CategorySpendFieldAccessTests(_RestatedFixture):

    def test_the_spend_by_category_is_served_to_a_reader_of_the_category(self):
        self.open_everything()

        response = self.call(self.officer_user, "categories/insights/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["category_id"] for row in response.data["data"]],
                         [self.multi.category.pk])

    def test_it_is_refused_whole_when_the_category_is_hidden(self):
        self.open_everything()
        self.switch(self.officer_role, self.vendor_keys, {"category": "hidden"})

        refused = self.call(self.officer_user, "categories/insights/")

        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["error"]["code"], "field_read_denied")

    def test_another_vendor_value_being_hidden_does_not_close_it(self):
        self.open_everything()
        self.switch(self.officer_role, self.vendor_keys, {"risk": "hidden", "on_hold": "hidden"})

        self.assertEqual(self.call(self.officer_user, "categories/insights/").status_code, 200)

    def test_a_single_branch_school_is_held_to_the_same_switch(self):
        self.switch(self.flat_role, self.vendor_keys, {"category": "hidden"})
        self.assertEqual(
            self.call(self.flat_user, "categories/insights/", books=self.flat).status_code, 403)

    def test_one_schools_switch_does_not_reach_another(self):
        self.switch(self.officer_role, self.vendor_keys, {"category": "hidden"})
        self.switch(self.foreign_role, self.vendor_keys, {})

        response = self.call(self.foreign_user, "categories/insights/", books=self.foreign)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["category_id"] for row in response.data["data"]],
                         [self.foreign.category.pk])


class DeliveryHistoryFieldAccessTests(_RestatedFixture):

    def history(self, books=None, user=None):
        books = books or self.multi
        response = self.call(
            user or self.officer_user, f"purchase-orders/{books.order.pk}/", books=books)
        self.assertEqual(response.status_code, 200, response.data)
        (row,) = response.data["data"]["email_deliveries"]
        return row

    def test_a_reader_of_everything_is_given_the_counts_and_the_reason(self):
        self.open_everything()

        row = self.history()

        self.assertEqual((row["recipient_count"], row["bcc_count"]), (2, 1))
        self.assertEqual(row["failure_reason"], BOUNCE)

    def test_the_count_of_recipients_needs_the_flags_that_chose_them(self):
        for hidden in ("is_active", "receives_purchase_orders", "is_primary"):
            with self.subTest(hidden=hidden):
                self.open_everything()
                self.switch(self.officer_role, self.contact_keys, {hidden: "hidden"})
                row = self.history()
                self.assertNotIn("recipient_count", row)
                self.assertEqual(row["bcc_count"], 1)

    def test_a_flag_that_does_not_choose_order_recipients_leaves_the_count(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"receives_rfqs": "hidden"})

        self.assertEqual(self.history()["recipient_count"], 2)

    def test_the_failure_reason_names_no_address_to_a_caller_who_cannot_read_it(self):
        for key_group, hidden in (
            (self.contact_keys, "email"), (self.vendor_keys, "email"),
        ):
            with self.subTest(hidden=hidden, group=key_group[0].rsplit(".", 2)[1]):
                self.open_everything()
                self.switch(self.officer_role, key_group, {hidden: "hidden"})
                row = self.history()
                self.assertNotIn("amina@multi.example", row["failure_reason"])
                self.assertIn("Recipient address rejected", row["failure_reason"])

    def test_a_single_branch_school_is_held_to_the_same_switches(self):
        self.switch(self.flat_role, self.contact_keys, {"is_primary": "hidden", "email": "hidden"})

        row = self.history(self.flat, self.flat_user)

        self.assertNotIn("recipient_count", row)
        self.assertNotIn("amina@flat.example", row["failure_reason"])

    def test_one_schools_switches_do_not_reach_another(self):
        self.switch(self.officer_role, self.contact_keys, {"email": "hidden", "is_active": "hidden"})
        self.switch(self.foreign_role, self.contact_keys, {})
        self.switch(self.foreign_role, self.vendor_keys, {})

        row = self.history(self.foreign, self.foreign_user)

        self.assertEqual(row["recipient_count"], 2)
        self.assertEqual(row["failure_reason"], BOUNCE)


class VendorCategoryCountTests(_RestatedFixture):
    """The number of vendors in a category restates which vendors carry it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for user, tenant in (
            (cls.officer_user, cls.multi_tenant), (cls.flat_user, cls.flat_tenant),
            (cls.foreign_user, cls.foreign_tenant),
        ):
            cls.grant(user, "procurement.category.view", tenant=tenant,
                      role_key="officer", branch=None)

    def row(self, user=None, books=None):
        books = books or self.multi
        response = self.call(user or self.officer_user, "categories/", books=books)
        self.assertEqual(response.status_code, 200, response.data)
        (row,) = response.data["data"]
        return row

    def test_the_count_is_served_to_a_reader_of_the_category(self):
        self.open_everything()

        self.assertEqual(self.row()["vendor_count"], 1)

    def test_it_goes_with_the_category_switch_and_nothing_else_does(self):
        self.open_everything()
        self.switch(self.officer_role, self.vendor_keys, {"category": "hidden"})

        row = self.row()

        self.assertNotIn("vendor_count", row)
        self.assertIn("child_count", row)
        self.assertEqual(row["name"], "Stationery")

    def test_another_vendor_value_does_not_remove_it(self):
        self.open_everything()
        self.switch(self.officer_role, self.vendor_keys, {"risk": "hidden"})

        self.assertIn("vendor_count", self.row())

    def test_the_detail_and_a_single_branch_school_follow_the_same_switch(self):
        self.switch(self.flat_role, self.vendor_keys, {"category": "hidden"})
        detail = self.call(
            self.flat_user, f"categories/{self.flat.category.pk}/", books=self.flat)
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertNotIn("vendor_count", detail.data["data"])

    def test_one_schools_switch_does_not_reach_another(self):
        self.switch(self.officer_role, self.vendor_keys, {"category": "hidden"})
        self.switch(self.foreign_role, self.vendor_keys, {})

        self.assertEqual(self.row(self.foreign_user, self.foreign)["vendor_count"], 1)


class VendorCategoryWriteAliasTests(_RestatedFixture):
    """The form sends the category as ``category``, so the record names that key too."""

    def read_only(self):
        return self.vendor_detail()["_read_only_fields"]

    def test_a_hidden_or_read_only_category_names_the_body_key_the_form_sends(self):
        for state in ("hidden", "read"):
            with self.subTest(state=state):
                self.open_everything()
                self.switch(self.officer_role, self.vendor_keys, {"category": state})
                self.assertIn("category", self.read_only())

    def test_a_writable_category_names_no_body_key(self):
        self.open_everything()

        self.assertNotIn("category", self.read_only())
        self.assertNotIn("category_id", self.read_only())

    def test_a_single_branch_school_names_it_too(self):
        self.switch(self.flat_role, self.vendor_keys, {"category": "read"})

        self.assertIn("category", self.vendor_detail(self.flat_user, self.flat)["_read_only_fields"])

    def test_one_schools_switch_does_not_reach_another(self):
        self.switch(self.officer_role, self.vendor_keys, {"category": "hidden"})
        self.switch(self.foreign_role, self.vendor_keys, {})

        self.assertNotIn(
            "category", self.vendor_detail(self.foreign_user, self.foreign)["_read_only_fields"])
