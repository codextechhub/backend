"""A booked transfer into a pupil's account number is told to the family that sent it.

Tunde, at Corona's Ikeja branch, has a student app account; his mother Ngozi
and his father Emeka each have a parent app account, and an aunt is a guardian
with no account at all. Bola, at Corona's Lekki branch, has a family of his
own, and so has Ada at Greenfield, a school with one branch. When ₦2,500.00 is
transferred into Tunde's number and the bank confirms it, Tunde, Ngozi and
Emeka are told in their apps, and nobody else is. The push says a payment was
confirmed and names no amount; the in-app notice names the pupil and the
amount; a tap opens the receipt.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from schools.core.fal.deposit_notices import EVENT_PAYMENT_RECEIVED
from schools.core.fal.tests.test_transfer_account_on_first_bill import _FirstBillFixture
from schools.vs_parent_portal.push import destination as parent_destination
from schools.vs_student_portal.push import destination as student_destination
from schools.vs_student_portal.tests.test_fees import give_access, set_school_config
from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification
from vs_notifications.services.seed import (
    _build_default_templates,
    seed_event_types,
    seed_notification_templates,
    seed_school_settings,
)
from vs_payments import services
from vs_payments.constants import CollectionOrigin

_SEND = "vs_notifications.notify.send_notification"
PUSH = ChannelChoices.PUSH


def app_notices(send):
    """The calls to *send* that tell somebody with an account in the app (not the receipt email)."""
    return [
        call for call in send.call_args_list
        if call.kwargs["event_key"] == EVENT_PAYMENT_RECEIVED and call.kwargs.get("recipients")
    ]


class _DepositFixture(_FirstBillFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from vs_finance.models import Customer

        for pupil in (cls.tunde, cls.bola, cls.ada):
            Customer.objects.filter(source_id=str(pupil.pk)).update(name=pupil.full_name)
        seed_event_types()
        seed_notification_templates()
        seed_school_settings(cls.corona)
        seed_school_settings(cls.greenfield)

        cls.tunde_user = give_access(cls.tunde, "COR/001")
        cls.ngozi = cls.guardian_with_account(cls.corona, cls.tunde, "ngozi@corona.test", "Ngozi Adeyemi", True)
        cls.emeka = cls.guardian_with_account(cls.corona, cls.tunde, "emeka@corona.test", "Emeka Adeyemi", False)
        cls.aunt = cls.guardian_of(
            cls.corona, cls.tunde, full_name="Aunt Bisi", is_primary=False,
        )  # A guardian record with no account of her own.
        cls.bola_mum = cls.guardian_with_account(cls.corona, cls.bola, "bola.mum@corona.test", "Mrs Okoro", True)
        cls.ada_mum = cls.guardian_with_account(cls.greenfield, cls.ada, "ada.mum@greenfield.test", "Mrs Eze", True)

    @classmethod
    def guardian_with_account(cls, school, pupil, email, name, primary):
        guardian = cls.guardian_of(school, pupil, full_name=name, is_primary=primary)
        guardian.user = cls.user_for(school, email)
        guardian.save(update_fields=["user"])
        return guardian.user

    def issue_account(self, pupil, customer=None):
        from vs_finance.models import Customer

        customer = customer or Customer.objects.get(
            source_type="vs_students.Student", source_id=str(pupil.pk),
        )
        return services.create_virtual_account(
            entity=customer.entity, customer=customer, provider="FAKE",
        )

    def transfer(self, account, amount=250_000, reference="DEP-001"):
        """A deposit the bank reported, confirmed and booked; returns the collection."""
        intent, _ = services.record_virtual_account_deposit(
            virtual_account=account, reference=reference, amount=amount,
        )
        self.fake.forced_status[reference] = "SUCCEEDED"
        self.fake.forced_amount[reference] = amount
        with self.captureOnCommitCallbacks(execute=True):
            services.confirm_collection(intent)
        intent.refresh_from_db()
        return intent

    def told(self):
        return Notification.objects.filter(
            event_type__key=EVENT_PAYMENT_RECEIVED, recipient__isnull=False,
        )


class WhoIsToldTests(_DepositFixture):
    def setUp(self):
        super().setUp()
        self.tunde_account = self.issue_account(self.tunde)

    def recipients(self):
        with mock.patch(_SEND, return_value=["sent"]) as send:
            self.transfer(self.tunde_account)
        return send, [call.kwargs["recipients"][0] for call in app_notices(send)]

    def test_the_pupil_and_every_guardian_with_an_account_are_told_and_no_one_else(self):
        _, told = self.recipients()
        self.assertCountEqual(told, [self.tunde_user, self.ngozi, self.emeka])

    def test_other_families_and_staff_are_not_told(self):
        _, told = self.recipients()
        for stranger in (self.bola_mum, self.ada_mum, self.bursar, self.lekki_bursar):
            self.assertNotIn(stranger, told)

    def test_it_goes_in_the_app_and_by_push_only_and_carries_the_receipt(self):
        send, _ = self.recipients()
        call = app_notices(send)[0]
        self.assertEqual(call.kwargs["only_channels"], [ChannelChoices.IN_APP, PUSH])
        self.assertEqual(call.kwargs["branch"], self.ikeja)
        self.assertEqual(call.kwargs["tenant"], self.corona.tenant)
        metadata = call.kwargs["metadata"]
        self.assertTrue(metadata["payment_id"])
        self.assertEqual(metadata["student_id"], self.tunde.pk)
        self.assertEqual(call.kwargs["context"]["customer_name"], "Tunde Adeyemi")
        self.assertEqual(call.kwargs["context"]["amount_paid"], "2,500.00")

    def test_the_in_app_notice_names_the_pupil_and_the_amount(self):
        self.transfer(self.tunde_account)
        mine = self.told().filter(recipient=self.ngozi, channel=ChannelChoices.IN_APP).get()
        self.assertIn("Tunde Adeyemi", mine.subject)
        self.assertIn("2,500.00", mine.subject)
        self.assertEqual(
            set(self.told().filter(channel=ChannelChoices.IN_APP).values_list("recipient_id", flat=True)),
            {self.tunde_user.pk, self.ngozi.pk, self.emeka.pk},
        )

    def test_no_second_email_is_sent_to_the_people_told_in_the_app(self):
        self.transfer(self.tunde_account)
        emailed = self.told().filter(channel=ChannelChoices.EMAIL)
        self.assertFalse(emailed.exists())

    def test_the_push_names_no_amount_and_no_pupil(self):
        template = _build_default_templates()[(EVENT_PAYMENT_RECEIVED, PUSH)]
        text = f"{template['subject']} {template['body']}".lower()
        self.assertIn("payment confirmed", text)
        for word in ("amount", "₦", "tunde", "balance", "customer_name", "amount_paid"):
            self.assertNotIn(word, text)

    def test_a_transfer_is_told_once_however_often_it_is_confirmed_again(self):
        with mock.patch(_SEND, return_value=["sent"]) as send:
            intent = self.transfer(self.tunde_account)
            with self.captureOnCommitCallbacks(execute=True):
                services.confirm_collection(intent)
        self.assertEqual(len(app_notices(send)), 3)

    def test_a_school_that_stops_pupils_paying_still_tells_the_parents_but_not_the_pupil(self):
        set_school_config(self.corona.tenant, self.bursar, "fees.student_payment_enabled", False)
        _, told = self.recipients()
        self.assertCountEqual(told, [self.ngozi, self.emeka])

    def test_a_guardian_whose_account_cannot_sign_in_is_not_told(self):
        type(self.emeka).objects.filter(pk=self.emeka.pk).update(status="PENDING")
        _, told = self.recipients()
        self.assertCountEqual(told, [self.tunde_user, self.ngozi])

    def test_one_notice_that_fails_does_not_stop_the_others(self):
        calls = []

        def flaky(**kwargs):
            calls.append(kwargs["recipients"][0])
            if kwargs["recipients"][0] == self.tunde_user:
                raise RuntimeError("boom")
            return ["sent"]

        with mock.patch(_SEND, side_effect=flaky), self.assertLogs("schools.core.fal.deposit_notices", "ERROR"):
            intent = self.transfer(self.tunde_account)
        self.assertEqual(intent.status, "SUCCEEDED")
        self.assertIn(self.ngozi, calls)
        self.assertIn(self.emeka, calls)


class OtherShapesOfSchoolTests(_DepositFixture):
    def test_a_lekki_pupil_s_family_is_told_with_that_branch(self):
        account = self.issue_account(self.bola)
        with mock.patch(_SEND, return_value=["sent"]) as send:
            self.transfer(account)
        (call,) = app_notices(send)
        self.assertEqual(call.kwargs["recipients"], [self.bola_mum])
        self.assertEqual(call.kwargs["branch"], self.lekki)

    def test_a_school_with_one_branch_tells_the_family_too(self):
        account = self.issue_account(self.ada)
        with mock.patch(_SEND, return_value=["sent"]) as send:
            self.transfer(account)
        self.assertEqual([c.kwargs["recipients"][0] for c in app_notices(send)], [self.ada_mum])

    def test_a_deposit_on_an_account_that_is_not_a_pupils_tells_nobody(self):
        from vs_finance.models import Customer

        stray = Customer.objects.create(
            entity_id=self.corona_books.entity_ref, code="HALL-1", name="Hall hire",
            receivable_account=self.tunde_customer.receivable_account, branch=self.ikeja,
            source_type="other.Thing", source_id=str(self.tunde.pk),
        )
        account = self.issue_account(None, customer=stray)
        with mock.patch(_SEND, return_value=["sent"]) as send:
            self.transfer(account)
        self.assertFalse(app_notices(send))


class CardPaymentNoticeIsUnchangedTests(_DepositFixture):
    def test_a_portal_card_payment_still_tells_only_the_account_that_started_it(self):
        from vs_payments.models import CollectionIntent

        invoice = self.manual_invoice(self.tunde_customer)
        with mock.patch(_SEND, return_value=["sent"]) as send, \
                mock.patch("vs_payments.services._announce_deposit") as announce, \
                self.captureOnCommitCallbacks(execute=True):
            intent = services.initiate_collection(
                entity=invoice.entity, amount=20_000, customer=invoice.customer, invoice=invoice,
                payer_email="ngozi@corona.test", payer_name="Ngozi", actor_user=self.ngozi,
                origin=CollectionOrigin.PORTAL,
            )
            self.fake.forced_status[intent.reference] = "SUCCEEDED"
            self.fake.forced_amount[intent.reference] = 20_000
            services.confirm_collection(intent)
        announce.assert_not_called()
        self.assertEqual([c.kwargs["recipients"] for c in app_notices(send)], [[self.ngozi]])
        self.assertTrue(CollectionIntent.objects.filter(pk=intent.pk, payment__isnull=False).exists())


class WhereATapOpensTests(_DepositFixture):
    def notification(self, recipient, payment_id):
        return SimpleNamespace(
            recipient=recipient, metadata={"payment_id": payment_id},
            event_type=SimpleNamespace(key=EVENT_PAYMENT_RECEIVED),
        )

    def setUp(self):
        super().setUp()
        self.payment_id = self.transfer(self.issue_account(self.tunde)).payment_id

    def test_a_parent_s_tap_opens_the_receipt_as_that_child(self):
        found = parent_destination(self.notification(self.ngozi, self.payment_id))
        self.assertEqual(found.url, f"/parent/fees/receipts/{self.payment_id}")
        self.assertEqual(found.child, self.tunde.pk)

    def test_a_pupil_s_tap_opens_the_receipt_in_the_student_app(self):
        found = student_destination(self.notification(self.tunde_user, self.payment_id))
        self.assertEqual(found.url, f"/student/fees/receipts/{self.payment_id}")

    def test_another_family_s_parent_is_never_pointed_at_this_receipt(self):
        self.assertIsNone(parent_destination(self.notification(self.bola_mum, self.payment_id)))

    def test_another_pupil_is_never_pointed_at_this_receipt(self):
        bola_user = give_access(self.bola, "COR/002")
        self.assertIsNone(student_destination(self.notification(bola_user, self.payment_id)))

    def test_a_parent_is_not_a_pupil_and_a_pupil_is_not_a_parent(self):
        self.assertIsNone(student_destination(self.notification(self.ngozi, self.payment_id)))
        self.assertIsNone(parent_destination(self.notification(self.tunde_user, self.payment_id)))

    def test_other_events_pass(self):
        other = SimpleNamespace(
            recipient=self.tunde_user, metadata={"payment_id": self.payment_id},
            event_type=SimpleNamespace(key="notice.published"),
        )
        self.assertIsNone(student_destination(other))

    def test_a_payment_with_no_id_passes(self):
        self.assertIsNone(student_destination(self.notification(self.tunde_user, None)))
