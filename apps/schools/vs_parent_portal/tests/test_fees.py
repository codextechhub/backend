"""A parent's view of their children's fees, and paying them.

Mrs. Nwosu has Chiamaka (Lekki), Tobi (Ikeja) and Folu (Epe) at Brightfield,
which has three branches; Dayo has left. Chiamaka and Tobi owe a ₦3,000.00
tuition invoice and a ₦500.00 levy, Folu tuition only. Mrs. Yusuf's Zainab owes
tuition of her own and Mr. Ade, at Sunrise (one branch), has a child who has
never been billed. Nothing of one family's is visible to another: an invoice, a
receipt or a transfer account that is not a parent's child's answers 404 and
starts nothing.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from schools.core.fal import registry as fal_registry
from vs_payments import services as payment_services
from vs_payments.constants import CollectionOrigin
from vs_payments.models import CollectionIntent

from .fees_base import ParentBooksFixture, UnreadableChildReader

FEES = "parent-portal-fees"


class ParentFeesReadTests(ParentBooksFixture):
    def fees(self, user=None):
        response = self.read(FEES, user)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_every_child_is_listed_with_the_family_total(self):
        data = self.fees()
        self.assertEqual(
            [(row["child"]["first_name"], row["balance"]) for row in data["children"]],
            [("Chiamaka", 350_000), ("Tobi", 350_000), ("Folu", 300_000)],
        )
        self.assertEqual(data["combined"], {
            "available": True, "billed": 1_000_000, "paid": 0, "outstanding": 1_000_000,
            "credit": 0,
        })
        self.assertEqual(data["min_part_payment"], 100_000)
        chiamaka = data["children"][0]
        self.assertEqual(chiamaka["child"]["id"], self.chiamaka.pk)
        self.assertEqual(chiamaka["child"]["full_name"], "Chiamaka Nwosu")
        self.assertEqual(len(chiamaka["invoices"]), 2)
        tuition = next(r for r in chiamaka["invoices"] if r["total"] == 300_000)
        self.assertEqual(tuition["number"], self.chiamaka_tuition.document_number)
        self.assertEqual(tuition["status_label"], "Owing")
        self.assertEqual(tuition["lines"][0]["amount"], 300_000)
        self.assertNotIn("can_pay", chiamaka)

    def test_a_child_who_has_left_is_not_listed(self):
        names = [row["child"]["first_name"] for row in self.fees()["children"]]
        self.assertNotIn("Dayo", names)

    def test_a_payment_lowers_the_balance_and_the_total(self):
        self.pay(self.books, self.chiamaka_tuition.customer_id, 100_000, invoice=self.chiamaka_tuition)
        data = self.fees()
        self.assertEqual(data["children"][0]["balance"], 250_000)
        self.assertEqual(data["combined"]["paid"], 100_000)
        self.assertEqual(data["combined"]["outstanding"], 900_000)

    def test_one_childs_unreadable_books_never_read_as_zero(self):
        fal_registry.set_finance_reader(UnreadableChildReader(self.tobi.pk))
        data = self.fees()
        chiamaka, tobi, folu = data["children"]
        self.assertTrue(chiamaka["available"])
        self.assertEqual(chiamaka["balance"], 350_000)
        self.assertEqual(
            tobi, {
                "child": {"id": self.tobi.pk, "first_name": "Tobi", "full_name": "Tobi Nwosu"},
                "available": False,
                "message": "Tobi's fees cannot be shown right now. Try again in a few minutes.",
            },
        )
        self.assertTrue(folu["available"])
        self.assertEqual(data["combined"]["available"], False)
        self.assertNotIn("outstanding", data["combined"])
        self.assertFalse(data["pay_all"]["complete"])
        self.assertNotIn(self.tobi.pk, [s["child_id"] for s in data["pay_all"]["steps"]])

    def test_pay_all_is_one_step_per_open_invoice_by_child_then_due_date(self):
        self.due(self.chiamaka_tuition, 30)
        self.due(self.chiamaka_levy, 10)
        self.due(self.tobi_tuition, 20)
        self.due(self.tobi_levy, 5)
        self.due(self.folu_tuition, 15)
        pay_all = self.fees()["pay_all"]
        self.assertEqual(
            [(s["first_name"], s["amount"]) for s in pay_all["steps"]],
            [
                ("Chiamaka", 50_000), ("Chiamaka", 300_000),
                ("Tobi", 50_000), ("Tobi", 300_000),
                ("Folu", 300_000),
            ],
        )
        self.assertEqual(pay_all["total"], 1_000_000)
        self.assertTrue(pay_all["complete"])
        first = pay_all["steps"][0]
        self.assertEqual(first["child_id"], self.chiamaka.pk)
        self.assertEqual(first["invoice_ref"], self.chiamaka_levy.pk)
        self.assertEqual(first["number"], self.chiamaka_levy.document_number)
        self.assertEqual(
            set(first), {"child_id", "first_name", "invoice_ref", "number", "title", "amount"},
        )

    def test_a_paid_invoice_drops_out_of_pay_all_and_a_part_paid_one_steps_down(self):
        self.pay(self.books, self.chiamaka_levy.customer_id, 50_000, invoice=self.chiamaka_levy)
        self.pay(self.books, self.tobi_tuition.customer_id, 100_000, invoice=self.tobi_tuition)
        steps = self.fees()["pay_all"]["steps"]
        self.assertNotIn(self.chiamaka_levy.pk, [s["invoice_ref"] for s in steps])
        self.assertEqual(
            next(s for s in steps if s["invoice_ref"] == self.tobi_tuition.pk)["amount"], 200_000,
        )

    def test_nothing_owed_is_an_empty_list_not_a_missing_one(self):
        """Mr. Ade's child has never been billed: that is a fact, and a zero."""
        data = self.fees(self.ade_user)
        self.assertEqual([r["child"]["first_name"] for r in data["children"]], ["Bola"])
        self.assertEqual(data["children"][0]["balance"], 0)
        self.assertEqual(data["children"][0]["invoices"], [])
        self.assertEqual(data["combined"]["outstanding"], 0)
        self.assertEqual(
            data["pay_all"], {"total": 0, "steps": [], "charges_total": 0, "complete": True},
        )

    def test_the_schools_smallest_part_payment_is_reported(self):
        self.set_config("fees.min_part_payment", 2500)
        self.assertEqual(self.fees()["min_part_payment"], 250_000)

    def test_staff_without_a_child_get_404(self):
        self.assertEqual(self.read(FEES, self.admin).status_code, 404)


class ParentOwingAndCreditTests(ParentBooksFixture):
    """What a family owes is never netted against credit, child by child or across children.

    Mrs. Nwosu's Chiamaka (Lekki) holds ₦5,000.00 of credit while Tobi (Ikeja)
    owes ₦3,500.00: the family still owes ₦3,500.00, with ₦5,000.00 of credit
    beside it, where one netted figure used to read "in credit".
    """

    def fees(self, user=None):
        response = self.read(FEES, user)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_an_open_bill_beside_larger_credit_reads_owing_with_the_credit_beside_it(self):
        self.hold_credit(self.books, self.chiamaka_tuition.customer_id, 500_000)
        chiamaka = self.fees()["children"][0]
        self.assertEqual((chiamaka["owing"], chiamaka["credit"]), (350_000, 500_000))
        self.assertEqual(chiamaka["balance"], chiamaka["owing"] - chiamaka["credit"])
        self.assertEqual(chiamaka["balance"], -150_000)

    def test_one_childs_credit_does_not_hide_a_siblings_debt(self):
        self.pay(self.books, self.chiamaka_tuition.customer_id, 300_000, invoice=self.chiamaka_tuition)
        self.pay(self.books, self.chiamaka_levy.customer_id, 50_000, invoice=self.chiamaka_levy)
        self.hold_credit(self.books, self.chiamaka_tuition.customer_id, 500_000)
        data = self.fees()
        chiamaka, tobi, folu = data["children"]
        self.assertEqual((chiamaka["owing"], chiamaka["credit"]), (0, 500_000))
        self.assertEqual((tobi["owing"], tobi["credit"]), (350_000, 0))
        self.assertEqual(data["combined"]["outstanding"], 350_000 + 300_000)
        self.assertEqual(data["combined"]["credit"], 500_000)
        self.assertEqual(data["combined"]["paid"], 350_000)

    def test_a_debit_note_is_listed_and_counts_in_owing_but_not_in_pay_all_steps(self):
        note = self.add_charge(self.tobi_tuition, 25_000)
        data = self.fees()
        tobi = data["children"][1]
        self.assertEqual(tobi["owing"], 375_000)
        [charge] = tobi["debit_notes"]
        self.assertEqual(charge["note_ref"], note.pk)
        self.assertEqual(charge["number"], note.document_number)
        self.assertEqual(charge["reason"], "Replacement ID card")
        self.assertEqual((charge["total"], charge["paid"], charge["balance"]), (25_000, 0, 25_000))
        self.assertEqual(charge["status_label"], "Owing")
        self.assertEqual(data["children"][0]["debit_notes"], [])
        self.assertEqual(data["combined"]["outstanding"], 1_025_000)
        self.assertEqual(data["pay_all"]["total"], 1_000_000)
        self.assertEqual(data["pay_all"]["charges_total"], 25_000)
        self.assertEqual(len(data["pay_all"]["steps"]), 5)

    def test_a_single_branch_school_reports_the_same_shape(self):
        data = self.fees(self.ade_user)
        bola = data["children"][0]
        self.assertEqual((bola["owing"], bola["credit"], bola["balance"], bola["debit_notes"]), (0, 0, 0, []))
        self.assertEqual(data["combined"]["credit"], 0)


class ParentHistoryTests(ParentBooksFixture):
    def test_every_childs_payments_are_merged_newest_first_with_their_child(self):
        self.pay(self.books, self.chiamaka_tuition.customer_id, 100_000,
                 invoice=self.chiamaka_tuition, when=self.chiamaka_tuition.invoice_date)
        self.pay(self.books, self.tobi_tuition.customer_id, 120_000,
                 invoice=self.tobi_tuition, when=self.tobi_tuition.invoice_date + dt.timedelta(days=2))
        rows = self.read("parent-portal-payments").data["data"]
        self.assertEqual(
            [(r["child"]["first_name"], r["amount"]) for r in rows],
            [("Tobi", 120_000), ("Chiamaka", 100_000)],
        )
        self.assertEqual(rows[0]["child"], {"id": self.tobi.pk, "first_name": "Tobi"})
        self.assertTrue(rows[0]["has_receipt"])
        self.assertGreaterEqual(rows[0]["paid_on"], rows[1]["paid_on"])

    def test_a_payment_still_being_confirmed_is_listed_as_pending(self):
        self.pay_invoice(self.folu_tuition, 300_000)
        rows = self.read("parent-portal-payments").data["data"]
        self.assertEqual([(r["child"]["first_name"], r["status"]) for r in rows], [("Folu", "PENDING")])
        self.assertFalse(rows[0]["has_receipt"])

    def test_another_familys_payments_are_not_listed(self):
        self.pay(self.books, self.zainab_tuition.customer_id, 100_000, invoice=self.zainab_tuition)
        self.assertEqual(self.read("parent-portal-payments").data["data"], [])

    def test_an_unreadable_history_fails_the_list_rather_than_dropping_a_child(self):
        fal_registry.set_student_payment(_UnreadableHistory(str(self.tobi.pk)))
        response = self.read("parent-portal-payments")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "FINANCE_UNAVAILABLE")


class ParentReceiptTests(ParentBooksFixture):
    def receipt_url(self, payment):
        return reverse("parent-portal-receipt", kwargs={"payment_ref": payment.pk})

    def test_a_childs_receipt_reads_as_json_and_as_a_pdf(self):
        payment = self.pay(
            self.books, self.tobi_tuition.customer_id, 100_000, invoice=self.tobi_tuition,
        )
        response = self.as_parent().get(self.receipt_url(payment))
        self.assertEqual(response.status_code, 200, response.data)
        receipt = response.data["data"]
        self.assertEqual(receipt["amount"], 100_000)
        self.assertEqual(receipt["child"], {"id": self.tobi.pk, "first_name": "Tobi"})
        self.assertEqual(receipt["lines"][0]["balance_after"], 200_000)
        pdf = self.as_parent().get(self.receipt_url(payment), {"as": "pdf"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_another_familys_receipt_is_not_found(self):
        payment = self.pay(
            self.books, self.zainab_tuition.customer_id, 100_000, invoice=self.zainab_tuition,
        )
        for params in ({}, {"as": "pdf"}):
            with self.subTest(params=params):
                self.assertEqual(self.as_parent().get(self.receipt_url(payment), params).status_code, 404)
        self.assertEqual(self.as_parent(self.yusuf_user).get(self.receipt_url(payment)).status_code, 200)

    def test_a_receipt_from_another_school_is_not_found(self):
        payment = self.pay(
            self.books, self.chiamaka_tuition.customer_id, 100_000, invoice=self.chiamaka_tuition,
        )
        self.assertEqual(self.as_parent(self.ade_user).get(self.receipt_url(payment)).status_code, 404)

    def test_a_payment_that_does_not_exist_is_not_found(self):
        url = reverse("parent-portal-receipt", kwargs={"payment_ref": 9_999_999})
        self.assertEqual(self.as_parent().get(url).status_code, 404)

    def test_an_unreadable_child_turns_a_missing_receipt_into_an_outage(self):
        payment = self.pay(
            self.books, self.zainab_tuition.customer_id, 100_000, invoice=self.zainab_tuition,
        )
        fal_registry.set_student_payment(_UnreadableHistory(str(self.tobi.pk)))
        self.assertEqual(self.as_parent().get(self.receipt_url(payment)).status_code, 503)


class ParentInvoiceTests(ParentBooksFixture):
    def invoice_url(self, invoice):
        return reverse("parent-portal-invoice", kwargs={"invoice_ref": invoice.pk})

    def test_a_childs_invoice_reads_for_the_screen_and_as_a_pdf(self):
        response = self.as_parent().get(self.invoice_url(self.tobi_levy))
        self.assertEqual(response.status_code, 200, response.data)
        invoice = response.data["data"]
        self.assertEqual(invoice["number"], self.tobi_levy.document_number)
        self.assertEqual(invoice["balance"], 50_000)
        self.assertEqual(invoice["child"], {"id": self.tobi.pk, "first_name": "Tobi"})
        self.assertEqual(invoice["lines"][0]["amount"], 50_000)
        pdf = self.as_parent().get(self.invoice_url(self.tobi_levy), {"as": "pdf"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_another_familys_invoice_is_not_found_in_either_form(self):
        for params in ({}, {"as": "pdf"}):
            with self.subTest(params=params):
                response = self.as_parent().get(self.invoice_url(self.zainab_tuition), params)
                self.assertEqual(response.status_code, 404)

    def test_an_invoice_from_another_school_is_not_found(self):
        response = self.as_parent(self.ade_user).get(self.invoice_url(self.chiamaka_tuition))
        self.assertEqual(response.status_code, 404)

    def test_an_unreadable_child_turns_a_missing_invoice_into_an_outage(self):
        fal_registry.set_finance_reader(UnreadableChildReader(self.tobi.pk))
        response = self.as_parent().get(self.invoice_url(self.zainab_tuition))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "FINANCE_UNAVAILABLE")
        # An invoice on a child who can be read is still found.
        self.assertEqual(self.as_parent().get(self.invoice_url(self.folu_tuition)).status_code, 200)


class ParentPayTests(ParentBooksFixture):
    def test_a_part_payment_hands_back_a_checkout_started_by_the_parent(self):
        response = self.pay_invoice(self.chiamaka_tuition, 200_000)
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertTrue(data["checkout_url"])
        self.assertEqual(data["balance_after"], 100_000)
        self.assertEqual(data["child"], {"id": self.chiamaka.pk, "first_name": "Chiamaka"})
        intent = CollectionIntent.objects.get()
        self.assertEqual(intent.reference, data["reference"])
        self.assertEqual(intent.origin, CollectionOrigin.PORTAL)
        self.assertEqual(intent.created_by_id, self.nwosu_user.pk)
        self.assertEqual(intent.invoice_id, self.chiamaka_tuition.pk)
        self.assertEqual(intent.amount, 200_000)
        self.assertEqual(intent.payer_email, self.nwosu_user.email)

    @override_settings(SCHOOL_APP_BASE_URL="https://xvs.codexng.com", PARENT_APP_BASE_URL="")
    def test_the_checkout_returns_the_parent_to_the_parent_apps_paid_page(self):
        with mock.patch.object(
            self.fake, "create_checkout", wraps=self.fake.create_checkout,
        ) as checkout:
            self.assertEqual(self.pay_invoice(self.tobi_tuition, 200_000).status_code, 201)
        self.assertEqual(
            checkout.call_args.kwargs["callback_url"],
            "https://brightfield.xvs.codexng.com/parent/fees/paid",
        )

    @override_settings(
        SCHOOL_APP_BASE_URL="https://xvs.codexng.com", PARENT_APP_BASE_URL="http://dev.test:5176",
    )
    def test_a_separately_served_parent_app_has_its_own_host(self):
        with mock.patch.object(
            self.fake, "create_checkout", wraps=self.fake.create_checkout,
        ) as checkout:
            self.assertEqual(self.pay_invoice(self.tobi_tuition, 200_000).status_code, 201)
        self.assertEqual(
            checkout.call_args.kwargs["callback_url"],
            "http://brightfield.dev.test:5176/parent/fees/paid",
        )

    def test_the_whole_balance_may_always_be_paid(self):
        self.assertEqual(self.pay_invoice(self.chiamaka_tuition, 300_000).status_code, 201)

    def test_the_whole_balance_under_the_minimum_is_allowed_a_part_of_it_is_not(self):
        """The ₦500.00 levy is below the ₦1,000.00 minimum: all of it, or none."""
        self.assertEqual(self.pay_invoice(self.tobi_levy, 20_000).data["error"]["code"], "AMOUNT_BELOW_MINIMUM")
        self.assertFalse(CollectionIntent.objects.exists())
        self.assertEqual(self.pay_invoice(self.tobi_levy, 50_000).status_code, 201)
        self.assertEqual(CollectionIntent.objects.count(), 1)

    def test_a_part_payment_under_the_minimum_is_refused(self):
        response = self.pay_invoice(self.chiamaka_tuition, 50_000)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "AMOUNT_BELOW_MINIMUM")
        self.assertEqual(response.data["error"]["detail"]["minimum"], 100_000)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_the_schools_own_minimum_applies_to_a_parent(self):
        self.set_config("fees.min_part_payment", 2500)
        self.assertEqual(self.pay_invoice(self.chiamaka_tuition, 200_000).status_code, 400)
        self.assertEqual(self.pay_invoice(self.chiamaka_tuition, 250_000).status_code, 201)

    def test_more_than_the_balance_is_refused(self):
        response = self.pay_invoice(self.chiamaka_tuition, 300_001)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "AMOUNT_ABOVE_BALANCE")
        self.assertFalse(CollectionIntent.objects.exists())

    def test_an_amount_that_is_not_a_positive_whole_number_of_kobo_is_refused(self):
        for amount in (0, -100_000, "abc", "", None, 150_000.5, 200_000.0, True, [200_000]):
            with self.subTest(amount=amount):
                response = self.pay_invoice(self.chiamaka_tuition, amount)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["error"]["code"], "PAYMENT_AMOUNT_INVALID")
        self.assertFalse(CollectionIntent.objects.exists())

    def test_a_missing_amount_is_refused(self):
        url = reverse("parent-portal-pay", kwargs={"invoice_ref": self.chiamaka_tuition.pk})
        response = self.as_parent().post(url, {}, format="json")
        self.assertEqual(response.data["error"]["code"], "PAYMENT_AMOUNT_INVALID")

    def test_an_invoice_already_paid_is_not_payable(self):
        self.pay(self.books, self.tobi_levy.customer_id, 50_000, invoice=self.tobi_levy)
        response = self.pay_invoice(self.tobi_levy, 50_000)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["code"], "INVOICE_NOT_PAYABLE")
        self.assertFalse(CollectionIntent.objects.exists())

    def test_a_parent_may_pay_where_the_school_stops_pupils_paying(self):
        self.set_config("fees.student_payment_enabled", False)
        self.assertEqual(self.pay_invoice(self.chiamaka_tuition, 300_000).status_code, 201)

    def test_a_provider_that_is_down_answers_503_and_nothing_is_charged(self):
        from vs_payments.exceptions import ProviderError

        with mock.patch.object(self.fake, "create_checkout", side_effect=ProviderError("timed out")):
            response = self.pay_invoice(self.chiamaka_tuition, 200_000)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "PROVIDER_UNAVAILABLE")

    def test_an_unreadable_book_is_an_outage_not_a_missing_invoice(self):
        fal_registry.set_finance_reader(UnreadableChildReader(self.chiamaka.pk))
        response = self.pay_invoice(self.chiamaka_tuition, 200_000)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "FINANCE_UNAVAILABLE")
        self.assertFalse(CollectionIntent.objects.exists())

    # ── another family's invoice ─────────────────────────────────────────

    def test_another_familys_invoice_is_not_found_and_starts_nothing(self):
        response = self.pay_invoice(self.zainab_tuition, 200_000)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_an_invoice_in_another_school_is_not_found_and_starts_nothing(self):
        response = self.pay_invoice(self.chiamaka_tuition, 200_000, user=self.ade_user)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_an_invoice_that_does_not_exist_is_not_found(self):
        url = reverse("parent-portal-pay", kwargs={"invoice_ref": 9_999_999})
        response = self.as_parent().post(url, {"amount": 200_000}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_staff_without_a_child_cannot_pay(self):
        self.assertEqual(self.pay_invoice(self.chiamaka_tuition, 200_000, user=self.admin).status_code, 404)
        self.assertFalse(CollectionIntent.objects.exists())

    def test_a_confirmed_payment_lowers_the_balance_the_family_sees(self):
        reference = self.pay_invoice(self.chiamaka_tuition, 200_000).data["data"]["reference"]
        intent = CollectionIntent.objects.get(reference=reference)
        self.fake.forced_status[reference] = "SUCCEEDED"
        payment_services.confirm_collection(intent)

        data = self.read(FEES).data["data"]
        self.assertEqual(data["children"][0]["balance"], 150_000)
        rows = self.read("parent-portal-payments").data["data"]
        self.assertEqual(rows[0]["payer_name"], self.nwosu_user.full_name)
        self.assertEqual(rows[0]["status"], "CONFIRMED")


class ParentTransferAccountTests(ParentBooksFixture):
    """Each child has a permanent account of their own, and the parent reads it.

    Brightfield stops pupils paying for themselves, which has no bearing on a
    parent. Mrs. Yusuf sees nothing of Chiamaka's account.
    """

    def transfer(self, child, method="get", user=None):
        url = reverse("parent-portal-transfer-account", kwargs={"child_id": child.pk})
        return getattr(self.as_parent(user), method)(url, format="json")

    def deposits(self, child, since, user=None):
        url = reverse("parent-portal-transfer-deposits", kwargs={"child_id": child.pk})
        return self.as_parent(user).get(url, {"since": since})

    def deposit(self, child, amount, reference="DEP-001"):
        from vs_payments.models import VirtualAccount

        account = VirtualAccount.objects.get(customer__source_id=str(child.pk))
        intent, _ = payment_services.record_virtual_account_deposit(
            virtual_account=account, reference=reference, amount=amount,
        )
        return intent

    def test_a_parent_is_issued_the_childs_account_where_pupils_may_not_pay(self):
        self.set_config("fees.student_payment_enabled", False)
        self.assertEqual(self.transfer(self.chiamaka).data["data"]["state"], "NONE")
        first = self.transfer(self.chiamaka, "post")
        self.assertEqual(first.status_code, 200, first.data)
        issued = first.data["data"]
        self.assertEqual(issued["state"], "ISSUED")
        self.assertTrue(issued["account_number"])
        again = self.transfer(self.chiamaka, "post").data["data"]
        self.assertEqual(again["account_number"], issued["account_number"])
        self.assertEqual(self.transfer(self.chiamaka).data["data"]["account_number"], issued["account_number"])

    def test_each_child_has_an_account_of_their_own(self):
        chiamaka = self.transfer(self.chiamaka, "post").data["data"]["account_number"]
        tobi = self.transfer(self.tobi, "post").data["data"]["account_number"]
        self.assertNotEqual(chiamaka, tobi)

    def test_a_deposit_is_seen_arriving_then_received(self):
        as_of = self.transfer(self.chiamaka, "post").data["data"]["as_of"]
        intent = self.deposit(self.chiamaka, 250_000)

        rows = self.deposits(self.chiamaka, as_of).data["data"]["deposits"]
        self.assertEqual([(r["amount"], r["status"]) for r in rows], [(250_000, "PENDING")])

        self.fake.forced_status[intent.reference] = "SUCCEEDED"
        self.fake.forced_amount[intent.reference] = 250_000
        payment_services.confirm_collection(intent)
        row = self.deposits(self.chiamaka, as_of).data["data"]["deposits"][0]
        self.assertEqual(row["status"], "CONFIRMED")
        self.assertTrue(row["has_receipt"])
        # The school applies transfers to open bills, so the child's balance moved.
        self.assertEqual(self.read(FEES).data["data"]["children"][0]["balance"], 100_000)

    def test_a_deposit_into_one_child_is_not_in_a_siblings_list(self):
        as_of = self.transfer(self.chiamaka, "post").data["data"]["as_of"]
        self.transfer(self.tobi, "post")
        self.deposit(self.chiamaka, 250_000)
        self.assertEqual(self.deposits(self.tobi, as_of).data["data"]["deposits"], [])

    def test_another_familys_child_is_not_found_for_every_verb(self):
        self.transfer(self.zainab, "post", user=self.yusuf_user)
        for method in ("get", "post"):
            with self.subTest(method=method):
                self.assertEqual(self.transfer(self.zainab, method).status_code, 404)
        self.assertEqual(self.deposits(self.zainab, "2026-01-01T00:00:00Z").status_code, 404)

    def test_a_child_in_another_school_is_not_found(self):
        self.assertEqual(self.transfer(self.chiamaka, user=self.ade_user).status_code, 404)

    def test_a_since_that_is_not_a_time_is_refused(self):
        response = self.deposits(self.chiamaka, "yesterday")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "SINCE_INVALID")

    def test_a_provider_that_issues_no_accounts_says_so(self):
        from vs_payments.exceptions import ProviderNotConfiguredError

        with mock.patch.object(
            self.fake, "create_virtual_account", side_effect=ProviderNotConfiguredError("no"),
        ):
            data = self.transfer(self.chiamaka, "post").data["data"]
        self.assertEqual(data["state"], "UNAVAILABLE")
        self.assertIn("bank transfers", data["message"])

    def test_a_provider_that_has_not_given_the_school_dedicated_accounts_says_so(self):
        from vs_payments.exceptions import ProviderFeatureUnavailableError

        refusal = ProviderFeatureUnavailableError("Dedicated NUBAN is not available for your business")
        with mock.patch.object(self.fake, "create_virtual_account", side_effect=refusal):
            response = self.transfer(self.chiamaka, "post")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["state"], "UNAVAILABLE")
        self.assertIn("bank transfers", response.data["data"]["message"])

    def test_a_provider_that_is_down_answers_503(self):
        from vs_payments.exceptions import ProviderError

        with mock.patch.object(self.fake, "create_virtual_account", side_effect=ProviderError("down")):
            response = self.transfer(self.chiamaka, "post")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "PROVIDER_UNAVAILABLE")

    def test_the_account_request_shares_the_pupil_portals_throttle(self):
        from rest_framework.throttling import ScopedRateThrottle

        from schools.vs_parent_portal.views import TransferAccountView

        view = TransferAccountView()
        view.request = mock.Mock(method="POST")
        throttles = view.get_throttles()
        self.assertIsInstance(throttles[0], ScopedRateThrottle)
        self.assertEqual(view.throttle_scope, "portal_transfer_account")


class _UnreadableHistory:
    """A student-payment port whose reads fail for one child and otherwise defer to the real one."""

    def __init__(self, *student_refs):
        from schools.core.fal.adapters.django_finance import DjangoStudentPaymentAdapter

        self._real = DjangoStudentPaymentAdapter()
        self._refs = set(student_refs)

    def __getattr__(self, name):
        real = getattr(self._real, name)
        if name not in ("payment_history", "receipt", "receipt_pdf"):
            return real

        def read(student_ref, *args, **kwargs):
            from schools.core.fal.contracts import FinanceResult, Unavailable

            if str(student_ref) in self._refs:
                return FinanceResult.unavailable(Unavailable.BACKEND_UNAVAILABLE)
            return real(student_ref, *args, **kwargs)

        return read
