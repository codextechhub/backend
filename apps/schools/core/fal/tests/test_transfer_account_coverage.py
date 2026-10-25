"""Every pupil on the roll ends up with an account number, however they were billed.

Tunde joins Corona's Ikeja branch, Bola its Lekki branch, and Ada joins
Greenfield, a school with a single branch. None has been billed, so none has an
account number yet. Joining the roll issues one, and a daily sweep issues one
to any pupil on the roll who still has none (the provider was down when they
joined, the school switched payments on later, a family detail was filled in
since). Neither route is stopped by ``fees.student_payment_enabled``, which only
decides what a pupil may do in the student app.
"""
from __future__ import annotations

from unittest import mock

from django.test import override_settings

from apps.celery import app as celery_app
from schools.core.fal import transfer_accounts
from schools.core.fal.tasks import issue_missing_transfer_accounts, issue_pupil_transfer_account
from schools.core.fal.tests.base import FALFixture
from schools.vs_schools.models import School
from schools.vs_student_portal.tests.test_fees import set_school_config
from schools.vs_students.constants import StudentStatus
from schools.vs_students.services.status import transition
from vs_payments.constants import PaymentAuditAction, VirtualAccountStatus
from vs_payments.custody import SubaccountMissingError
from vs_payments.exceptions import ProviderError
from vs_payments.models import PaymentEvent, VirtualAccount
from vs_payments.providers import registry as provider_registry
from vs_payments.providers.fake import FakeProvider
from vs_tenants.models import Branch


def accounts(pupil):
    return VirtualAccount.objects.filter(customer__source_id=str(pupil.pk))


class _CoverageFixture(FALFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tunde = cls.student(cls.corona, cls.ikeja)
        cls.tunde_2 = cls.student(cls.corona, cls.ikeja, first="Kemi", last="Bello")
        cls.bola = cls.student(cls.corona, cls.lekki, first="Bola", last="Okoro")
        cls.ada = cls.student(cls.greenfield, cls.greenfield_main, first="Ada", last="Eze")

    def setUp(self):
        super().setUp()
        self.fake = FakeProvider(secret="test-secret")
        provider_registry.register("PAYSTACK", self.fake)
        provider_registry.register("FAKE", self.fake)
        self.addCleanup(provider_registry.unregister)
        self.issued = mock.patch.object(
            self.fake, "create_virtual_account", wraps=self.fake.create_virtual_account,
        ).start()
        self.addCleanup(mock.patch.stopall)

    def not_issued_events(self):
        return PaymentEvent.objects.filter(action=PaymentAuditAction.VIRTUAL_ACCOUNT_NOT_ISSUED)

    def applicant(self, school, branch, **names):
        pupil = self.student(school, branch, **names)
        type(pupil).all_objects.filter(pk=pupil.pk).update(status=StudentStatus.APPLICANT)
        pupil.refresh_from_db()
        return pupil

    def enrol(self, pupil):
        with self.captureOnCommitCallbacks(execute=True):
            transition(pupil, StudentStatus.ENROLLED, actor=self.bursar, reason="Enrolled.")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class JoiningTheRollTests(_CoverageFixture):
    def test_a_pupil_who_joins_gets_an_account_before_any_bill_exists(self):
        pupil = self.applicant(self.corona, self.ikeja, first="Dayo", last="Salami")
        self.assertFalse(accounts(pupil).exists())
        self.enrol(pupil)
        account = accounts(pupil).get()
        self.assertEqual(account.status, VirtualAccountStatus.ACTIVE)
        self.assertEqual(account.customer.name, "Dayo Salami")
        self.assertEqual(account.customer.entity_id, self.corona_books.entity_ref)

    def test_the_account_belongs_to_the_pupils_own_branch_at_a_school_with_several(self):
        pupil = self.applicant(self.corona, self.lekki, first="Ngozi", last="Obi")
        self.enrol(pupil)
        self.assertEqual(accounts(pupil).get().branch_id, self.lekki.pk)

    def test_a_school_with_one_branch_gets_it_too(self):
        pupil = self.applicant(self.greenfield, self.greenfield_main, first="Sade", last="Ola")
        self.enrol(pupil)
        self.assertEqual(accounts(pupil).get().branch_id, self.greenfield_main.pk)

    def test_joining_the_roll_again_issues_no_second_account(self):
        pupil = self.applicant(self.corona, self.ikeja, first="Dayo", last="Salami")
        self.enrol(pupil)
        number = accounts(pupil).get().account_number
        with self.captureOnCommitCallbacks(execute=True):
            transition(pupil, StudentStatus.WITHDRAWN, actor=self.bursar, reason="Left.")
        self.enrol(pupil)
        self.assertEqual(accounts(pupil).get().account_number, number)
        self.assertEqual(self.issued.call_count, 1)

    def test_moving_between_statuses_on_the_roll_asks_the_provider_for_nothing(self):
        pupil = self.applicant(self.corona, self.ikeja, first="Dayo", last="Salami")
        self.enrol(pupil)
        calls = self.issued.call_count
        with self.captureOnCommitCallbacks(execute=True):
            transition(pupil, StudentStatus.ACTIVE, actor=self.bursar, reason="Placed.")
            transition(pupil, StudentStatus.SUSPENDED, actor=self.bursar)
        self.assertEqual(self.issued.call_count, calls)

    def test_leaving_the_roll_issues_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            transition(self.ada, StudentStatus.WITHDRAWN, actor=self.bursar, reason="Left.")
        self.assertFalse(accounts(self.ada).exists())
        self.issued.assert_not_called()

    def test_a_school_that_stops_pupils_paying_still_issues_it_for_the_parents(self):
        set_school_config(self.corona.tenant, self.bursar, "fees.student_payment_enabled", False)
        pupil = self.applicant(self.corona, self.ikeja, first="Dayo", last="Salami")
        self.enrol(pupil)
        self.assertEqual(accounts(pupil).count(), 1)

    def test_a_provider_that_is_off_issues_nothing_and_the_move_still_stands(self):
        self.issued.side_effect = SubaccountMissingError()
        pupil = self.applicant(self.corona, self.ikeja, first="Dayo", last="Salami")
        self.enrol(pupil)
        pupil.refresh_from_db()
        self.assertEqual(pupil.status, StudentStatus.ENROLLED)
        self.assertFalse(accounts(pupil).exists())

    def test_a_provider_that_fails_is_recorded_once_and_the_move_still_stands(self):
        self.issued.side_effect = ProviderError("down")
        pupil = self.applicant(self.corona, self.ikeja, first="Dayo", last="Salami")
        self.enrol(pupil)
        pupil.refresh_from_db()
        self.assertEqual(pupil.status, StudentStatus.ENROLLED)
        self.assertFalse(accounts(pupil).exists())
        self.assertEqual(self.not_issued_events().get().metadata["reason"], "PROVIDER_FAILED")

    def test_a_pupil_who_has_not_joined_the_roll_is_left_alone(self):
        pupil = self.applicant(self.corona, self.ikeja, first="Dayo", last="Salami")
        self.assertEqual(transfer_accounts.issue_for_pupil(pupil.pk), transfer_accounts.NOT_ON_ROLL)
        self.assertEqual(transfer_accounts.issue_for_pupil(99_999_999), transfer_accounts.NOT_ON_ROLL)

    def test_a_school_with_no_books_yet_is_passed_over_without_a_fault(self):
        bare = School.objects.create(slug="bare", name="Bare School", code="BAR-1", status="ACTIVE")
        branch = Branch.objects.create(tenant=bare.tenant, name="Main", is_main=True, status="ACTIVE")
        pupil = self.student(bare, branch, first="Femi", last="Ade")
        with self.assertLogs("schools.core.fal.transfer_accounts", "WARNING"):
            self.assertEqual(transfer_accounts.issue_for_pupil(pupil.pk), transfer_accounts.NO_BOOKS)
        self.issued.assert_not_called()

    def test_a_pupil_billed_already_keeps_the_account_the_bill_issued(self):
        self.student_customer(self.corona_books, str(self.tunde.pk), branch=self.ikeja)
        self.assertEqual(transfer_accounts.issue_for_pupil(self.tunde.pk), transfer_accounts.ISSUED)
        self.assertEqual(transfer_accounts.issue_for_pupil(self.tunde.pk), transfer_accounts.ALREADY_ISSUED)
        self.assertEqual(accounts(self.tunde).count(), 1)

    def test_the_task_is_safe_to_run_twice(self):
        issue_pupil_transfer_account(self.tunde.pk)
        issue_pupil_transfer_account(self.tunde.pk)
        self.assertEqual(accounts(self.tunde).count(), 1)
        self.assertEqual(self.issued.call_count, 1)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class DailySweepTests(_CoverageFixture):
    def test_the_sweep_issues_one_to_every_pupil_on_the_roll_who_has_none(self):
        outcome = transfer_accounts.sweep_missing_accounts()
        for pupil in (self.tunde, self.tunde_2, self.bola, self.ada):
            self.assertEqual(accounts(pupil).count(), 1)
        self.assertEqual(outcome, {transfer_accounts.ISSUED: 4})

    def test_a_pupil_off_the_roll_is_not_swept(self):
        applicant = self.applicant(self.corona, self.ikeja, first="Dayo", last="Salami")
        gone = self.student(self.corona, self.lekki, first="Gone", last="Away")
        type(gone).all_objects.filter(pk=gone.pk).update(status=StudentStatus.WITHDRAWN)
        transfer_accounts.sweep_missing_accounts()
        self.assertFalse(accounts(applicant).exists())
        self.assertFalse(accounts(gone).exists())

    def test_a_suspended_pupil_is_still_on_the_roll(self):
        type(self.bola).all_objects.filter(pk=self.bola.pk).update(status=StudentStatus.SUSPENDED)
        transfer_accounts.sweep_missing_accounts()
        self.assertEqual(accounts(self.bola).count(), 1)

    def test_running_it_again_asks_the_provider_for_nothing_more(self):
        transfer_accounts.sweep_missing_accounts()
        calls = self.issued.call_count
        again = transfer_accounts.sweep_missing_accounts()
        self.assertEqual(self.issued.call_count, calls)
        self.assertEqual(again, {})
        for pupil in (self.tunde, self.ada):
            self.assertEqual(accounts(pupil).count(), 1)

    def test_a_pupil_whose_account_was_issued_at_enrolment_is_not_asked_for_again(self):
        transfer_accounts.issue_for_pupil(self.tunde.pk)
        self.issued.reset_mock()
        transfer_accounts.sweep_missing_accounts()
        self.assertEqual(self.issued.call_count, 3)
        self.assertEqual(accounts(self.tunde).count(), 1)

    def test_it_issues_when_the_school_stops_pupils_paying(self):
        set_school_config(self.corona.tenant, self.bursar, "fees.student_payment_enabled", False)
        transfer_accounts.sweep_missing_accounts()
        self.assertEqual(accounts(self.tunde).count(), 1)

    def test_a_batch_is_bounded_and_the_rest_wait_for_the_next_run(self):
        first = transfer_accounts.sweep_missing_accounts(limit=2)
        self.assertEqual(first, {transfer_accounts.ISSUED: 2})
        self.assertEqual(VirtualAccount.objects.count(), 2)
        second = transfer_accounts.sweep_missing_accounts(limit=2)
        self.assertEqual(second, {transfer_accounts.ISSUED: 2})
        self.assertEqual(VirtualAccount.objects.count(), 4)

    def test_a_branch_the_provider_will_not_serve_is_asked_once_per_run(self):
        with mock.patch("vs_payments.custody.is_direct", return_value=True), \
                mock.patch("vs_payments.custody.branch_subaccount",
                           side_effect=SubaccountMissingError()) as asked:
            outcome = transfer_accounts.sweep_missing_accounts()
        # Ikeja holds two pupils and Lekki one, Greenfield one: one ask each, not four.
        self.assertEqual(asked.call_count, 3)
        self.assertEqual(outcome[transfer_accounts.NOT_OFFERED], 3)
        self.assertEqual(outcome["skipped"], 1)
        self.assertFalse(VirtualAccount.objects.exists())
        self.assertFalse(self.not_issued_events().exists())

    def test_a_school_whose_provider_keeps_failing_is_left_alone_after_three_tries(self):
        for number in range(3):
            self.student(self.corona, self.ikeja, first=f"Extra{number}", last="Pupil")
        self.issued.side_effect = lambda **kwargs: (_ for _ in ()).throw(ProviderError("down"))
        outcome = transfer_accounts.sweep_missing_accounts()
        corona_asked = self.issued.call_count
        self.assertEqual(corona_asked, transfer_accounts.SWEEP_FAILURES_BEFORE_SKIPPING_SCHOOL + 1)
        self.assertEqual(outcome[transfer_accounts.PROVIDER_FAILED], corona_asked)
        self.assertGreaterEqual(outcome["skipped"], 3)
        failed = self.not_issued_events().filter(metadata__reason="PROVIDER_FAILED")
        self.assertEqual(failed.count(), corona_asked)

    def test_a_failure_is_recorded_once_per_pupil_however_many_days_it_repeats(self):
        self.issued.side_effect = ProviderError("down")
        transfer_accounts.sweep_missing_accounts()
        transfer_accounts.sweep_missing_accounts()
        per_customer = self.not_issued_events().values_list("metadata__customer_id", flat=True)
        self.assertEqual(len(per_customer), len(set(per_customer)))

    def test_missing_family_details_are_recorded_once_and_the_provider_is_not_asked(self):
        self.fake.virtual_account_requires = ("name", "email", "phone")
        transfer_accounts.sweep_missing_accounts()
        transfer_accounts.sweep_missing_accounts()
        self.issued.assert_not_called()
        events = self.not_issued_events().filter(metadata__reason="MISSING_DETAILS")
        self.assertEqual(events.count(), 4)

    def test_a_school_with_no_books_is_skipped_quietly(self):
        bare = School.objects.create(slug="bare", name="Bare School", code="BAR-1", status="ACTIVE")
        branch = Branch.objects.create(tenant=bare.tenant, name="Main", is_main=True, status="ACTIVE")
        pupil = self.student(bare, branch, first="Femi", last="Ade")
        outcome = transfer_accounts.sweep_missing_accounts()
        self.assertEqual(outcome[transfer_accounts.NO_BOOKS], 1)
        self.assertFalse(accounts(pupil).exists())
        self.assertEqual(accounts(self.tunde).count(), 1)

    def test_one_pupil_that_blows_up_does_not_stop_the_rest(self):
        real = transfer_accounts.issue_for_pupil

        def flaky(pk):
            if pk == self.tunde.pk:
                raise RuntimeError("boom")
            return real(pk)

        with mock.patch.object(transfer_accounts, "issue_for_pupil", side_effect=flaky), \
                self.assertLogs("schools.core.fal.transfer_accounts", "ERROR"):
            outcome = transfer_accounts.sweep_missing_accounts()
        self.assertEqual(outcome["ERROR"], 1)
        self.assertEqual(accounts(self.ada).count(), 1)

    def test_the_task_runs_the_sweep(self):
        self.assertEqual(issue_missing_transfer_accounts(), {transfer_accounts.ISSUED: 4})

    def test_beat_runs_it_every_day(self):
        entry = celery_app.conf.beat_schedule["fal-issue-missing-transfer-accounts"]
        self.assertEqual(entry["task"], "fal.issue_missing_transfer_accounts")
        self.assertEqual(entry["schedule"].hour, {1})
        self.assertEqual(entry["schedule"].day_of_week, set(range(7)))
        self.assertIn("fal.issue_missing_transfer_accounts", celery_app.tasks)
