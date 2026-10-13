"""Field Access on a vendor's own values and on the people at the vendor.

Bright Star School keeps its supplier records in two parts. The supplier is
Ade Stationers, filed under Stationery, on 30-day terms, KYC verified and
rated medium risk. Amina Bello and Tunde Okafor are the two people who receive
its quotation requests and orders. A school decides per role who sees and
changes each of those details: Mrs Okafor, the procurement officer, maintains
names and emails but must not see phone numbers; the head of sourcing sees the
risk rating that the storekeeper does not.

Two shapes of school are built, one with two branches and one with a single
branch, plus a third tenant that must never be reachable from either. The
fixture holds the roles and the vendors; each test sets the switches it is
about on a role and calls the real endpoints with a real token.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, make_branch, set_field_access

from .models import (
    PurchaseOrder,
    RequestForQuotation,
    RfqInvitation,
    RfqInvitationRecipient,
    Vendor,
    VendorCategory,
    VendorContact,
)
from .tests import _BranchTenantsFixture
from .tests_dashboard_overview import _OverviewFixture

VENDOR_PERMISSIONS = (
    "procurement.vendor.view", "procurement.vendor.create", "procurement.vendor.update",
    "procurement.vendor.verify", "procurement.rfq.view", "procurement.purchase_order.view",
    "procurement.purchase_order.email_vendor", "procurement.report.view",
    "procurement.analytics.view",
)

#: Each open value, the names it travels under in a response, and a body that changes it.
OPEN_VALUES = {
    "category": (("category_id", "category_code"), {"category": "STAT"}),
    "payment_terms": (("payment_terms",), {"payment_terms": "NET_60"}),
    "kyc_status": (("kyc_status",), {"kyc_status": "REJECTED"}),
    "risk": (("risk",), {"risk": "HIGH"}),
    "on_hold": (("on_hold",), {"on_hold": True}),
}

CONTACT_DETAILS = ("name", "email", "phone")
CONTACT_FLAGS = ("is_primary", "receives_rfqs", "receives_purchase_orders", "is_active")
CONTACT_FLAGS_HIDDEN = {flag: "hidden" for flag in CONTACT_FLAGS}
ALL_CONTACT_HIDDEN = {name: "hidden" for name in CONTACT_DETAILS + CONTACT_FLAGS}


class _VendorAccessFixture(_BranchTenantsFixture, TestCase):
    """Two schools and a stranger, each with a vendor that has two contact people."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_branch(cls.flat_school, name="Only Branch")
        cls.vendor_keys = install_declared_fields("procurement.vendor")
        cls.contact_keys = install_declared_fields("procurement.vendor_contact")
        for books, tag in ((cls.multi, "multi"), (cls.flat, "flat"), (cls.foreign, "foreign")):
            cls.fill_vendor(books, tag)
        cls.officer_user, cls.officer_role = cls.persona(cls.multi_tenant, "officer@multi.test", "officer")
        cls.flat_user, cls.flat_role = cls.persona(cls.flat_tenant, "officer@flat.test", "officer")
        cls.foreign_user, cls.foreign_role = cls.persona(cls.foreign_tenant, "officer@foreign.test", "officer")

    @classmethod
    def fill_vendor(cls, books, tag):
        """Give ``books``' vendor every value and two contact people."""
        category = VendorCategory.objects.create(entity=books.entity, code="STAT", name="Stationery")
        Vendor.objects.filter(pk=books.vendor.pk).update(
            email=f"accounts@{tag}.example", phone="08030000000", address="12 Broad Street",
            tax_id=f"TIN-{tag}", bank_name="GTBank", bank_code="058",
            bank_account_number="0123456789", bank_account_name="Ade Stationers Ltd",
            category=category, payment_terms="NET_30", risk="MEDIUM", on_hold=False,
        )
        books.vendor.refresh_from_db()
        books.category = category
        books.amina = VendorContact.objects.create(
            vendor=books.vendor, name="Amina Bello", email=f"amina@{tag}.example",
            phone="08030000001", is_primary=True, receives_purchase_orders=True,
        )
        books.tunde = VendorContact.objects.create(
            vendor=books.vendor, name="Tunde Okafor", email=f"tunde@{tag}.example",
            phone="08030000002",
        )

    @classmethod
    def persona(cls, tenant, email, role_key):
        """A user holding every vendor key through one role whose switches a test sets."""
        user = cls.user_for(tenant, email)
        for key in VENDOR_PERMISSIONS:
            cls.grant(user, key, tenant=tenant, role_key=role_key, branch=None)
        return user, TenantRoleTemplate.objects.get(tenant=tenant, key=role_key)

    @staticmethod
    def switch(role, keys, states, default="write"):
        """Set ``role``'s switches over ``keys``: ``hidden``, ``read`` or ``write`` per name."""
        for key in keys:
            state = states.get(key.rsplit(".", 1)[1], default)
            set_field_access(role, key, read=state != "hidden", write=state == "write")

    def open_everything(self, role=None):
        role = role or self.officer_role
        self.switch(role, self.vendor_keys, {})
        self.switch(role, self.contact_keys, {})

    def call(self, user, path, method="get", data=None, books=None):
        books = books or self.multi
        client = TenantAPIClient(user=user)
        sep = "&" if "?" in path else "?"
        url = f"/v1/procurement/{path}{sep}entity={books.entity.code}"
        if method == "get":
            return client.get(url)
        return getattr(client, method)(url, data or {}, format="json")

    def vendor_detail(self, user=None, books=None):
        books = books or self.multi
        response = self.call(user or self.officer_user, f"vendors/{books.vendor.pk}/", books=books)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def vendor_row(self, user=None, books=None):
        books = books or self.multi
        response = self.call(user or self.officer_user, "vendors/", books=books)
        self.assertEqual(response.status_code, 200, response.data)
        return next(row for row in response.data["data"] if row["id"] == books.vendor.pk)


class VendorValueFieldTests(_VendorAccessFixture):
    """Category, payment terms, KYC status, risk and hold each have their own switches."""

    def test_each_value_is_hidden_on_its_own_from_the_detail_and_the_list(self):
        """Hiding risk from the storekeeper leaves the category and terms in place."""
        for hidden, (names, _body) in OPEN_VALUES.items():
            with self.subTest(hidden=hidden):
                self.open_everything()
                self.switch(self.officer_role, self.vendor_keys, {hidden: "hidden"})
                detail = self.vendor_detail()
                row = self.vendor_row()
                for payload in (detail, row):
                    for name in names:
                        self.assertNotIn(name, payload)
                    for other, (other_names, _) in OPEN_VALUES.items():
                        if other != hidden:
                            for name in other_names:
                                self.assertIn(name, payload)
                    self.assertEqual(payload["name"], "Acme Supplies")
                # The category is also the body key a form sends, so a form is told
                # not to offer it; every other hidden value names nothing.
                if hidden == "category":
                    self.assertEqual(detail["_read_only_fields"], ["category"])
                else:
                    self.assertNotIn(hidden, detail["_read_only_fields"])

    def test_each_value_is_named_read_only_and_refused_on_write(self):
        """A value the officer may read but not change is greyed, and a save is refused."""
        for readonly, (names, body) in OPEN_VALUES.items():
            with self.subTest(readonly=readonly):
                self.open_everything()
                self.switch(self.officer_role, self.vendor_keys, {readonly: "read"})
                detail = self.vendor_detail()
                for name in names:
                    self.assertIn(name, detail)
                self.assertIn(
                    "category_id" if readonly == "category" else readonly,
                    detail["_read_only_fields"],
                )
                before = Vendor.objects.values().get(pk=self.multi.vendor.pk)
                refused = self.call(
                    self.officer_user, f"vendors/{self.multi.vendor.pk}/", "patch", body,
                )
                self.assertEqual(refused.status_code, 403, refused.data)
                self.assertEqual(refused.data["error"]["code"], "field_write_denied")
                self.assertIn(next(iter(body)), refused.data["error"]["detail"])
                self.assertEqual(Vendor.objects.values().get(pk=self.multi.vendor.pk), before)

    def test_a_category_is_named_read_only_by_both_names_it_travels_under(self):
        self.open_everything()
        self.switch(self.officer_role, self.vendor_keys, {"category": "read"})
        read_only = self.vendor_detail()["_read_only_fields"]
        self.assertIn("category_id", read_only)
        self.assertIn("category_code", read_only)

    def test_each_value_is_written_when_its_switch_is_on(self):
        for writable, (_names, body) in OPEN_VALUES.items():
            with self.subTest(writable=writable):
                self.open_everything()
                response = self.call(
                    self.officer_user, f"vendors/{self.multi.vendor.pk}/", "patch", body,
                )
                self.assertEqual(response.status_code, 200, response.data)
                vendor = Vendor.objects.get(pk=self.multi.vendor.pk)
                field, value = next(iter(body.items()))
                if field == "category":
                    self.assertEqual(vendor.category.code, value)
                else:
                    self.assertEqual(getattr(vendor, field), value)

    def test_a_school_with_no_switches_set_reads_and_changes_every_value(self):
        """Default access: the five values are open to a role nobody has configured."""
        from vs_rbac.models import RoleFieldAccess

        RoleFieldAccess.objects.filter(role=self.flat_role).delete()
        detail = self.vendor_detail(self.flat_user, self.flat)
        for names, _body in OPEN_VALUES.values():
            for name in names:
                self.assertIn(name, detail)
        response = self.call(
            self.flat_user, f"vendors/{self.flat.vendor.pk}/", "patch",
            {"payment_terms": "NET_60", "risk": "HIGH"}, books=self.flat,
        )
        self.assertEqual(response.status_code, 200, response.data)
        # The contact details are sensitive, so they stay closed until a school
        # opens them, and the open routing flags alone do not show the list.
        self.assertNotIn("contacts", detail)

    def test_the_compliance_key_is_still_needed_beside_the_switch(self):
        """An open KYC switch does not make the bursar a verifier."""
        self.open_everything()
        clerk = self.user_for(self.multi_tenant, "clerk@multi.test")
        for key in ("procurement.vendor.view", "procurement.vendor.update"):
            self.grant(clerk, key, tenant=self.multi_tenant, role_key="clerk", branch=None)
        clerk_role = TenantRoleTemplate.objects.get(tenant=self.multi_tenant, key="clerk")
        self.switch(clerk_role, self.vendor_keys, {})
        refused = self.call(clerk, f"vendors/{self.multi.vendor.pk}/", "patch", {"risk": "HIGH"})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertNotEqual(Vendor.objects.get(pk=self.multi.vendor.pk).risk, "HIGH")
        allowed = self.call(clerk, f"vendors/{self.multi.vendor.pk}/", "patch", {"payment_terms": "NET_60"})
        self.assertEqual(allowed.status_code, 200, allowed.data)

    def test_creating_a_vendor_may_echo_the_starting_compliance_values_but_not_change_them(self):
        self.open_everything()
        self.switch(
            self.officer_role, self.vendor_keys,
            {"kyc_status": "read", "risk": "read", "on_hold": "read"},
        )
        created = self.call(self.officer_user, "vendors/", "post", {
            "name": "Echo Stores", "kyc_status": "PENDING", "risk": "LOW", "on_hold": False,
        })
        self.assertEqual(created.status_code, 201, created.data)
        count = Vendor.objects.filter(entity=self.multi.entity).count()
        refused = self.call(self.officer_user, "vendors/", "post", {
            "name": "Forced Stores", "on_hold": True,
        })
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["error"]["code"], "field_write_denied")
        self.assertEqual(Vendor.objects.filter(entity=self.multi.entity).count(), count)

    def test_a_filter_on_a_hidden_value_is_refused_and_a_readable_one_filters(self):
        self.open_everything()
        Vendor.objects.filter(pk=self.multi.vendor.pk).update(on_hold=True)
        shown = self.call(self.officer_user, "vendors/?on_hold=true")
        self.assertEqual([row["id"] for row in shown.data["data"]], [self.multi.vendor.pk])
        self.switch(self.officer_role, self.vendor_keys, {"on_hold": "hidden", "kyc_status": "hidden"})
        for query in ("on_hold=true", "kyc_status=VERIFIED"):
            refused = self.call(self.officer_user, f"vendors/?{query}")
            self.assertEqual(refused.status_code, 403, refused.data)
            self.assertEqual(refused.data["error"]["code"], "field_read_denied")

    def test_a_search_never_matches_on_a_category_the_caller_cannot_read(self):
        """Typing Stationery finds Ade through its category only for a reader of categories."""
        self.open_everything()
        found = self.call(self.officer_user, "vendors/?search=Stationery")
        self.assertEqual([row["id"] for row in found.data["data"]], [self.multi.vendor.pk])
        self.switch(self.officer_role, self.vendor_keys, {"category": "hidden"})
        missed = self.call(self.officer_user, "vendors/?search=Stationery")
        self.assertEqual(missed.data["data"], [])
        by_name = self.call(self.officer_user, "vendors/?search=Acme")
        self.assertEqual(len(by_name.data["data"]), 1)

    def test_the_summary_leaves_out_a_count_of_something_the_caller_cannot_read(self):
        self.open_everything()
        Vendor.objects.filter(pk=self.multi.vendor.pk).update(on_hold=True)
        full = self.call(self.officer_user, "vendors/summary/").data["data"]
        for key in ("on_hold", "kyc_pending", "average_payment_days"):
            self.assertIn(key, full)
        self.assertEqual(full["active"], 0)
        self.switch(
            self.officer_role, self.vendor_keys,
            {"on_hold": "hidden", "kyc_status": "hidden", "payment_terms": "hidden"},
        )
        trimmed = self.call(self.officer_user, "vendors/summary/").data["data"]
        for key in ("on_hold", "kyc_pending", "average_payment_days"):
            self.assertNotIn(key, trimmed)
        # Without the hold switch the count of active vendors cannot subtract held ones.
        self.assertEqual(trimmed["active"], 1)

    def test_one_schools_switches_do_not_reach_another(self):
        """Closing risk for Multi's officer leaves the single-branch school's officer reading it."""
        self.open_everything()
        self.open_everything(self.flat_role)
        self.switch(self.officer_role, self.vendor_keys, {"risk": "hidden"})
        self.assertNotIn("risk", self.vendor_detail())
        self.assertIn("risk", self.vendor_detail(self.flat_user, self.flat))
        stranger = self.call(self.officer_user, f"vendors/{self.foreign.vendor.pk}/", books=self.foreign)
        self.assertIn(stranger.status_code, (403, 404))
        cross = self.call(self.officer_user, f"vendors/{self.foreign.vendor.pk}/")
        self.assertEqual(cross.status_code, 404)

    def test_a_single_branch_school_is_held_to_the_same_switches(self):
        self.open_everything(self.flat_role)
        self.switch(self.flat_role, self.vendor_keys, {"payment_terms": "read", "on_hold": "hidden"})
        detail = self.vendor_detail(self.flat_user, self.flat)
        self.assertNotIn("on_hold", detail)
        self.assertIn("payment_terms", detail["_read_only_fields"])
        refused = self.call(
            self.flat_user, f"vendors/{self.flat.vendor.pk}/", "patch",
            {"payment_terms": "NET_60"}, books=self.flat,
        )
        self.assertEqual(refused.status_code, 403, refused.data)


class VendorContactFieldTests(_VendorAccessFixture):
    """A contact person's name, email and phone, and the four routing flags, each have a switch."""

    def contacts(self, user=None, books=None):
        return self.vendor_detail(user, books).get("contacts")

    def test_each_detail_and_flag_is_hidden_on_its_own_from_the_nested_list(self):
        for hidden in CONTACT_DETAILS + CONTACT_FLAGS:
            with self.subTest(hidden=hidden):
                self.open_everything()
                self.switch(self.officer_role, self.contact_keys, {hidden: "hidden"})
                rows = self.contacts()
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertNotIn(hidden, row)
                    for other in CONTACT_DETAILS + CONTACT_FLAGS:
                        if other != hidden:
                            self.assertIn(other, row)

    def test_the_vendors_own_email_and_phone_are_a_different_switch(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"email": "hidden", "phone": "hidden"})
        detail = self.vendor_detail()
        self.assertEqual(detail["email"], "accounts@multi.example")
        self.assertEqual(detail["phone"], "08030000000")
        self.switch(self.officer_role, self.vendor_keys, {"email": "hidden", "phone": "hidden"})
        self.switch(self.officer_role, self.contact_keys, {})
        detail = self.vendor_detail()
        self.assertNotIn("email", detail)
        self.assertEqual(detail["contacts"][0]["email"], "amina@multi.example")

    def test_no_contact_list_at_all_when_no_detail_is_readable(self):
        """Rows of bare routing flags would still say how many people the vendor has."""
        for states in (ALL_CONTACT_HIDDEN, {name: "hidden" for name in CONTACT_DETAILS}):
            with self.subTest(states=states):
                self.open_everything()
                self.switch(self.officer_role, self.contact_keys, states)
                self.assertNotIn("contacts", self.vendor_detail())

    def test_the_list_stays_while_any_one_detail_is_readable_and_shows_only_it(self):
        for readable in CONTACT_DETAILS:
            with self.subTest(readable=readable):
                self.open_everything()
                self.switch(
                    self.officer_role, self.contact_keys,
                    {**ALL_CONTACT_HIDDEN, readable: "read"},
                )
                rows = self.contacts()
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertEqual(set(row) - {"id"}, {readable})

    def test_a_role_with_no_contact_switch_set_reads_no_contacts(self):
        """The details are sensitive, so a school opens them role by role; the flags alone show nothing."""
        from vs_rbac.models import RoleFieldAccess

        RoleFieldAccess.objects.filter(role=self.officer_role).delete()
        self.assertNotIn("contacts", self.vendor_detail())

    def test_a_read_only_contact_detail_is_greyed_for_the_form_through_the_access_map(self):
        from vs_rbac.field_enforcement import field_access_payload

        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"phone": "read", "name": "hidden"})
        payload = field_access_payload(self.officer_user, self.multi_tenant)
        self.assertEqual(payload["procurement.vendor_contact"]["read_only"], ["phone"])
        self.assertEqual(payload["procurement.vendor_contact"]["hidden"], ["name"])

    # -- recipients on a quotation request ---------------------------------- #

    def make_rfq(self, books):
        rfq = RequestForQuotation.objects.create(
            entity=books.entity, title="Chalk", issue_date=datetime.date(2026, 1, 10),
        )
        invitation = RfqInvitation.objects.create(rfq=rfq, vendor=books.vendor)
        RfqInvitationRecipient.objects.create(
            invitation=invitation, contact=books.amina, name=books.amina.name,
            email=books.amina.email,
        )
        return rfq

    def test_the_people_a_request_was_sent_to_follow_the_same_switches(self):
        rfq = self.make_rfq(self.multi)
        path = f"rfqs/{rfq.pk}/"

        def recipients():
            response = self.call(self.officer_user, path)
            self.assertEqual(response.status_code, 200, response.data)
            return response.data["data"]["invitations"][0]["recipients"]

        self.open_everything()
        self.assertEqual(recipients(), [{"name": "Amina Bello", "email": "amina@multi.example"}])
        self.switch(self.officer_role, self.contact_keys, {"email": "hidden"})
        self.assertEqual(recipients(), [{"name": "Amina Bello"}])
        self.switch(self.officer_role, self.contact_keys, {"name": "hidden"})
        self.assertEqual(recipients(), [{"email": "amina@multi.example"}])
        self.switch(self.officer_role, self.contact_keys, {"name": "hidden", "email": "hidden"})
        self.assertEqual(recipients(), [])

    def test_the_people_a_request_was_sent_to_need_the_flags_that_chose_them(self):
        """A list of recipients says who receives requests and is active, so it needs both flags."""
        rfq = self.make_rfq(self.multi)
        for hidden in ("receives_rfqs", "is_active"):
            with self.subTest(hidden=hidden):
                self.open_everything()
                self.switch(self.officer_role, self.contact_keys, {hidden: "hidden"})
                response = self.call(self.officer_user, f"rfqs/{rfq.pk}/")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data["data"]["invitations"][0]["recipients"], [])
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"receives_purchase_orders": "hidden"})
        response = self.call(self.officer_user, f"rfqs/{rfq.pk}/")
        self.assertEqual(len(response.data["data"]["invitations"][0]["recipients"]), 1)

    # -- the order email preview -------------------------------------------- #

    def make_order(self, books):
        return PurchaseOrder.objects.create(
            entity=books.entity, vendor=books.vendor, order_date=datetime.date(2026, 1, 5),
            status="APPROVED",
        )

    def test_the_order_email_preview_shows_addresses_only_to_a_reader_of_them(self):
        po = self.make_order(self.multi)
        path = f"purchase-orders/{po.pk}/email-preview/"
        self.open_everything()
        shown = self.call(self.officer_user, path).data["data"]
        self.assertEqual(shown["recipients"], ["amina@multi.example"])
        self.switch(self.officer_role, self.contact_keys, {"email": "hidden"})
        hidden = self.call(self.officer_user, path).data["data"]
        self.assertEqual(hidden["recipients"], [])
        self.assertEqual(hidden["recipient_count"], 1)
        self.assertNotIn("amina@multi.example", str(hidden))

    def test_the_order_email_preview_needs_the_flags_that_chose_the_addresses(self):
        """Naming the contacts an order goes to says which of them receive orders."""
        po = self.make_order(self.multi)
        path = f"purchase-orders/{po.pk}/email-preview/"
        for hidden in ("is_active", "receives_purchase_orders", "is_primary"):
            with self.subTest(hidden=hidden):
                self.open_everything()
                self.switch(self.officer_role, self.contact_keys, {hidden: "hidden"})
                shown = self.call(self.officer_user, path).data["data"]
                self.assertEqual(shown["recipients"], [])
                self.assertNotIn("recipient_count", shown)
                self.assertNotIn("amina@multi.example", str(shown))
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"receives_rfqs": "hidden"})
        shown = self.call(self.officer_user, path).data["data"]
        self.assertEqual(shown["recipients"], ["amina@multi.example"])
        self.assertEqual(shown["recipient_count"], 1)

    def test_the_vendors_own_address_does_not_depend_on_the_contact_flags(self):
        VendorContact.objects.filter(vendor=self.multi.vendor).delete()
        po = self.make_order(self.multi)
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, ALL_CONTACT_HIDDEN)
        shown = self.call(
            self.officer_user, f"purchase-orders/{po.pk}/email-preview/",
        ).data["data"]
        self.assertEqual(shown["recipients"], ["accounts@multi.example"])
        self.assertEqual(shown["recipient_count"], 1)

    def test_the_vendors_own_address_is_used_when_there_is_no_contact_and_follows_its_switch(self):
        VendorContact.objects.filter(vendor=self.multi.vendor).delete()
        po = self.make_order(self.multi)
        path = f"purchase-orders/{po.pk}/email-preview/"
        self.open_everything()
        self.assertEqual(
            self.call(self.officer_user, path).data["data"]["recipients"],
            ["accounts@multi.example"],
        )
        self.switch(self.officer_role, self.vendor_keys, {"email": "hidden"})
        self.assertEqual(self.call(self.officer_user, path).data["data"]["recipients"], [])

    # -- writes ------------------------------------------------------------- #

    def row(self, contact, **changes):
        return {
            "id": contact.pk, "name": contact.name, "email": contact.email,
            "phone": contact.phone, "is_primary": contact.is_primary,
            "receives_rfqs": contact.receives_rfqs,
            "receives_purchase_orders": contact.receives_purchase_orders,
            **changes,
        }

    def patch_contacts(self, rows, user=None, books=None):
        books = books or self.multi
        return self.call(
            user or self.officer_user, f"vendors/{books.vendor.pk}/", "patch",
            {"contacts": rows}, books=books,
        )

    def stored(self, books=None):
        books = books or self.multi
        return list(
            VendorContact.objects.filter(vendor=books.vendor).order_by("email")
            .values_list("name", "email", "phone")
        )

    def test_a_writer_of_all_three_replaces_the_list(self):
        self.open_everything()
        response = self.patch_contacts([
            self.row(self.multi.amina, name="Amina B. Bello"),
            self.row(self.multi.tunde, phone="08099999999"),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(), [
            ("Amina B. Bello", "amina@multi.example", "08030000001"),
            ("Tunde Okafor", "tunde@multi.example", "08099999999"),
        ])

    def test_a_detail_the_caller_cannot_write_keeps_the_value_the_contact_holds(self):
        """Mrs Okafor edits names and emails; the phone numbers she cannot see survive."""
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"phone": "hidden"})
        rows = [
            {"id": self.multi.amina.pk, "name": "Amina B. Bello", "email": "amina@multi.example"},
            {"id": self.multi.tunde.pk, "name": "Tunde Okafor", "email": "tunde.new@multi.example"},
        ]
        response = self.patch_contacts(rows)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(), [
            ("Amina B. Bello", "amina@multi.example", "08030000001"),
            ("Tunde Okafor", "tunde.new@multi.example", "08030000002"),
        ])
        for row in response.data["data"]["contacts"]:
            self.assertNotIn("phone", row)

    def test_a_row_is_matched_by_email_when_the_form_sent_no_id(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"phone": "hidden"})
        response = self.patch_contacts([
            {"name": "Amina Bello-Ade", "email": "amina@multi.example"},
            {"name": "Tunde Okafor", "email": "tunde@multi.example"},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [phone for _name, _email, phone in self.stored()], ["08030000001", "08030000002"],
        )

    def test_a_changed_detail_the_caller_cannot_write_is_refused_naming_it(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"phone": "read"})
        before = self.stored()
        refused = self.patch_contacts([
            self.row(self.multi.amina, phone="08011111111"), self.row(self.multi.tunde),
        ])
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["error"]["code"], "field_write_denied")
        self.assertIn("phone", refused.data["error"]["detail"])
        self.assertIn("Contact person's phone", refused.data["message"])
        self.assertEqual(self.stored(), before)

    def test_echoing_a_readable_unchanged_detail_is_not_a_write(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"phone": "read"})
        response = self.patch_contacts([
            self.row(self.multi.amina, name="Amina B. Bello"), self.row(self.multi.tunde),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored()[0], ("Amina B. Bello", "amina@multi.example", "08030000001"))

    def test_a_hidden_detail_is_refused_even_when_the_value_is_the_stored_one(self):
        """Equal-is-accepted would answer the question the hidden field exists to keep closed."""
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"phone": "hidden"})
        refused = self.patch_contacts([
            self.row(self.multi.amina), self.row(self.multi.tunde),
        ])
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("phone", refused.data["error"]["detail"])

    def test_a_caller_who_may_change_no_detail_cannot_touch_the_list_at_all(self):
        """Read-only everywhere must not become able to re-flag or remove contacts."""
        self.open_everything()
        self.switch(
            self.officer_role, self.contact_keys,
            {name: "read" for name in CONTACT_DETAILS + CONTACT_FLAGS},
        )
        before = list(VendorContact.objects.filter(vendor=self.multi.vendor).values())
        for rows in (
            [self.row(self.multi.amina), self.row(self.multi.tunde)],
            [self.row(self.multi.amina, receives_purchase_orders=False)],
            [],
        ):
            refused = self.patch_contacts(rows)
            self.assertEqual(refused.status_code, 403, refused.data)
            self.assertEqual(refused.data["error"]["code"], "field_write_denied")
        self.assertEqual(list(VendorContact.objects.filter(vendor=self.multi.vendor).values()), before)

    def test_adding_or_removing_a_contact_needs_the_email_switch(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"email": "read"})
        before = self.stored()
        removed = self.patch_contacts([self.row(self.multi.amina, name="Amina")])
        self.assertEqual(removed.status_code, 403, removed.data)
        self.assertIn("email", removed.data["error"]["detail"])
        added = self.patch_contacts([
            self.row(self.multi.amina), self.row(self.multi.tunde),
            {"name": "New Person", "email": "new@multi.example"},
        ])
        self.assertEqual(added.status_code, 403, added.data)
        self.assertEqual(self.stored(), before)
        renamed = self.patch_contacts([
            self.row(self.multi.amina, name="Amina B."), self.row(self.multi.tunde),
        ])
        self.assertEqual(renamed.status_code, 200, renamed.data)

    def test_creating_a_vendor_with_contacts_is_refused_whole_for_a_detail_it_cannot_write(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"name": "read"})
        count = Vendor.objects.filter(entity=self.multi.entity).count()
        refused = self.call(self.officer_user, "vendors/", "post", {
            "name": "New Stores",
            "contacts": [{"name": "Kemi Ade", "email": "kemi@new.example"}],
        })
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("name", refused.data["error"]["detail"])
        self.assertEqual(Vendor.objects.filter(entity=self.multi.entity).count(), count)
        self.switch(self.officer_role, self.contact_keys, {})
        created = self.call(self.officer_user, "vendors/", "post", {
            "name": "New Stores",
            "contacts": [{"name": "Kemi Ade", "email": "kemi@new.example", "phone": "0801"}],
        })
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(len(created.data["data"]["contacts"]), 1)

    def test_creating_a_vendor_with_a_blank_name_is_not_a_write_of_the_name(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"name": "read"})
        created = self.call(self.officer_user, "vendors/", "post", {
            "name": "Blank Name Stores",
            "contacts": [{"name": "", "email": "front@blank.example", "phone": ""}],
        })
        # The name is read-only and empty, which is what a new contact holds
        # anyway, so nothing is being written.
        self.assertEqual(created.status_code, 201, created.data)

    def test_the_vendors_email_is_copied_to_its_primary_contact_only_by_a_writer_of_contact_emails(self):
        """Changing the vendor's address is not a back door to the contact's."""
        self.open_everything()
        Vendor.objects.filter(pk=self.multi.vendor.pk).update(email="amina@multi.example")
        self.switch(self.officer_role, self.contact_keys, {"email": "read"})
        response = self.call(
            self.officer_user, f"vendors/{self.multi.vendor.pk}/", "patch",
            {"email": "new-accounts@multi.example"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Vendor.objects.get(pk=self.multi.vendor.pk).email, "new-accounts@multi.example")
        self.assertEqual(
            VendorContact.objects.get(pk=self.multi.amina.pk).email, "amina@multi.example",
        )
        self.switch(self.officer_role, self.contact_keys, {})
        response = self.call(
            self.officer_user, f"vendors/{self.multi.vendor.pk}/", "patch",
            {"email": "latest@multi.example"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        Vendor.objects.filter(pk=self.multi.vendor.pk).update(email="amina@multi.example")

    def test_a_single_branch_school_writes_contacts_under_the_same_rules(self):
        self.open_everything(self.flat_role)
        self.switch(self.flat_role, self.contact_keys, {"phone": "hidden"})
        response = self.patch_contacts(
            [{"id": self.flat.amina.pk, "name": "Amina F.", "email": "amina@flat.example"},
             {"id": self.flat.tunde.pk, "name": "Tunde F.", "email": "tunde@flat.example"}],
            user=self.flat_user, books=self.flat,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.stored(self.flat),
            [("Amina F.", "amina@flat.example", "08030000001"),
             ("Tunde F.", "tunde@flat.example", "08030000002")],
        )

    def test_another_schools_contacts_are_never_matched_or_changed(self):
        """A contact id from a stranger's vendor is ignored, not adopted."""
        self.open_everything()
        before = self.stored(self.foreign)
        response = self.patch_contacts([
            {"id": self.foreign.amina.pk, "name": "Stolen", "email": "amina@multi.example"},
            self.row(self.multi.tunde),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored(self.foreign), before)
        self.assertEqual(
            [name for name, email, _ in self.stored() if email == "amina@multi.example"], ["Stolen"],
        )
        self.assertEqual(
            VendorContact.objects.get(pk=self.foreign.amina.pk).name, "Amina Bello",
        )

    # -- routing flags ------------------------------------------------------ #

    def flags(self, books=None):
        books = books or self.multi
        return {
            email: tuple(getattr(contact, flag) for flag in CONTACT_FLAGS)
            for email, contact in (
                (c.email, c) for c in VendorContact.objects.filter(vendor=books.vendor)
            )
        }

    def test_each_flag_changes_when_its_switch_is_on(self):
        for flag in ("receives_rfqs", "receives_purchase_orders", "is_active"):
            with self.subTest(flag=flag):
                self.open_everything()
                self.switch(self.officer_role, self.contact_keys, {flag: "write"}, default="read")
                tunde = VendorContact.objects.get(email=self.multi.tunde.email)
                flipped = not getattr(tunde, flag)
                response = self.patch_contacts([
                    self.row(self.multi.amina),
                    self.row(tunde, **{flag: flipped}),
                ])
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(
                    getattr(VendorContact.objects.get(email=self.multi.tunde.email), flag), flipped,
                )

    def test_the_primary_flag_moves_to_the_contact_that_is_given_it(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"is_primary": "write"}, default="read")
        response = self.patch_contacts([
            self.row(self.multi.amina, is_primary=False),
            self.row(self.multi.tunde, is_primary=True),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [email for email, flags in self.flags().items() if flags[0]], ["tunde@multi.example"],
        )

    def test_a_flag_the_caller_cannot_write_is_refused_naming_it(self):
        """Mrs Okafor may fix a phone number but must not send orders to a different person."""
        labels = {
            "is_primary": "Primary contact",
            "receives_rfqs": "Receives quotation requests",
            "receives_purchase_orders": "Receives purchase orders",
            "is_active": "Active contact",
        }
        for flag, label in labels.items():
            for state in ("read", "hidden"):
                with self.subTest(flag=flag, state=state):
                    self.open_everything()
                    self.switch(self.officer_role, self.contact_keys, {flag: state})
                    before = self.flags()
                    flipped = not getattr(self.multi.tunde, flag)
                    refused = self.patch_contacts([
                        self.row(self.multi.amina), self.row(self.multi.tunde, **{flag: flipped}),
                    ] if flag != "is_primary" else [
                        self.row(self.multi.amina, is_primary=False),
                        self.row(self.multi.tunde, is_primary=True),
                    ])
                    self.assertEqual(refused.status_code, 403, refused.data)
                    self.assertEqual(refused.data["error"]["code"], "field_write_denied")
                    self.assertIn(flag, refused.data["error"]["detail"])
                    self.assertIn(label, refused.data["message"])
                    self.assertEqual(self.flags(), before)

    def test_echoing_a_readable_unchanged_flag_is_not_a_write(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {flag: "read" for flag in CONTACT_FLAGS})
        before = self.flags()
        response = self.patch_contacts([
            self.row(self.multi.amina, is_active=True, name="Amina B. Bello"),
            self.row(self.multi.tunde, is_active=True),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.flags(), before)
        self.assertEqual(self.stored()[0][0], "Amina B. Bello")

    def test_a_hidden_flag_is_refused_even_when_the_value_is_the_stored_one(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"receives_purchase_orders": "hidden"})
        refused = self.patch_contacts([self.row(self.multi.amina), self.row(self.multi.tunde)])
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("receives_purchase_orders", refused.data["error"]["detail"])

    def test_a_form_that_cannot_show_the_flags_never_re_routes_them(self):
        """The rows a restricted form posts back carry no flags, and the stored ones survive."""
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, CONTACT_FLAGS_HIDDEN)
        before = self.flags()
        response = self.patch_contacts([
            {"id": self.multi.amina.pk, "name": "Amina B. Bello", "email": "amina@multi.example"},
            {"id": self.multi.tunde.pk, "name": "Tunde O.", "email": "tunde@multi.example"},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.flags(), before)
        for row in response.data["data"]["contacts"]:
            for flag in CONTACT_FLAGS:
                self.assertNotIn(flag, row)

    def test_a_writer_of_flags_keeps_the_stored_value_of_a_flag_the_row_leaves_out(self):
        self.open_everything()
        before = self.flags()
        response = self.patch_contacts([
            {"id": self.multi.amina.pk, "name": "Amina Bello", "email": "amina@multi.example",
             "phone": "08030000001"},
            {"id": self.multi.tunde.pk, "name": "Tunde Okafor", "email": "tunde@multi.example",
             "phone": "08030000002", "receives_purchase_orders": True},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        after = self.flags()
        self.assertEqual(after["amina@multi.example"], before["amina@multi.example"])
        self.assertTrue(after["tunde@multi.example"][2])

    def test_a_caller_who_may_write_only_the_flags_may_re_flag_but_not_add_a_contact(self):
        self.open_everything()
        self.switch(
            self.officer_role, self.contact_keys,
            {name: "read" for name in CONTACT_DETAILS},
        )
        response = self.patch_contacts([
            self.row(self.multi.amina),
            self.row(self.multi.tunde, receives_purchase_orders=True),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.flags()["tunde@multi.example"][2])
        added = self.patch_contacts([
            self.row(self.multi.amina), self.row(self.multi.tunde),
            {"name": "New Person", "email": "new@multi.example"},
        ])
        self.assertEqual(added.status_code, 403, added.data)

    def test_a_new_contact_with_non_default_flags_is_judged(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, {"receives_purchase_orders": "read"})
        existing = [self.row(self.multi.amina), self.row(self.multi.tunde)]
        refused = self.patch_contacts(existing + [
            {"name": "New Person", "email": "new@multi.example", "receives_purchase_orders": True},
        ])
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("receives_purchase_orders", refused.data["error"]["detail"])
        self.assertNotIn("new@multi.example", [email for _n, email, _p in self.stored()])
        allowed = self.patch_contacts(existing + [
            {"name": "New Person", "email": "new@multi.example", "receives_purchase_orders": False},
        ])
        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.assertEqual(self.flags()["new@multi.example"], (False, True, False, True))

    def test_creating_a_vendor_gives_the_first_contact_the_primary_flag_without_a_switch(self):
        self.open_everything()
        self.switch(self.officer_role, self.contact_keys, CONTACT_FLAGS_HIDDEN)
        created = self.call(self.officer_user, "vendors/", "post", {
            "name": "Flagless Stores",
            "contacts": [{"name": "Kemi Ade", "email": "kemi@flagless.example"}],
        })
        self.assertEqual(created.status_code, 201, created.data)
        vendor = Vendor.objects.get(entity=self.multi.entity, name="Flagless Stores")
        contact = vendor.contacts.get()
        self.assertEqual(
            (contact.is_primary, contact.receives_rfqs, contact.receives_purchase_orders, contact.is_active),
            (True, True, False, True),
        )
        refused = self.call(self.officer_user, "vendors/", "post", {
            "name": "Routed Stores",
            "contacts": [{"email": "kemi@routed.example", "receives_purchase_orders": True}],
        })
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertFalse(Vendor.objects.filter(name="Routed Stores").exists())

    def test_a_single_branch_school_holds_the_flags_to_the_same_switches(self):
        self.open_everything(self.flat_role)
        self.switch(self.flat_role, self.contact_keys, {"is_active": "read"})
        refused = self.patch_contacts(
            [{"id": self.flat.amina.pk, "email": "amina@flat.example", "is_active": False},
             {"id": self.flat.tunde.pk, "email": "tunde@flat.example"}],
            user=self.flat_user, books=self.flat,
        )
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertIn("is_active", refused.data["error"]["detail"])
        self.assertTrue(VendorContact.objects.get(pk=self.flat.amina.pk).is_active)

    def test_one_schools_flag_switches_do_not_reach_another(self):
        self.open_everything()
        self.open_everything(self.foreign_role)
        self.switch(self.foreign_role, self.contact_keys, ALL_CONTACT_HIDDEN)
        self.assertNotIn("contacts", self.vendor_detail(self.foreign_user, self.foreign))
        for row in self.contacts():
            self.assertTrue(set(CONTACT_FLAGS) <= set(row))
        response = self.patch_contacts([
            self.row(self.multi.amina, receives_rfqs=False), self.row(self.multi.tunde),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(VendorContact.objects.get(pk=self.foreign.amina.pk).receives_rfqs)
