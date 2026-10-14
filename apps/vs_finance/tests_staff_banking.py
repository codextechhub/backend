"""A member of staff is paid only into an account the bank says is theirs, or a second person has confirmed.

Corona's Ikeja bursar, Mr Eze, saves Ada Obi's account. The bank holds it for
"OBI ADA CHINEDU", so it is verified and she can be paid. When he mistypes a digit
and the bank names "BOLA OKAFOR", the account waits: a second person, who is neither
Mr Eze nor Ada, must confirm it before a naira goes there. Changing the bank or the
number afterwards puts it back to unchecked.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from rest_framework.exceptions import PermissionDenied, ValidationError

from core.test_utils import TenantAPIClient
from vs_payments.exceptions import ProviderError
from vs_payments.providers import registry
from vs_payments.providers.fake import FakeProvider

from .constants import FinanceAuditAction, StaffBankState
from .models import EmployeeSalary, FinanceAuditLog, StaffBankAccount
from .staff_banking import (
    confirm_staff_bank_account,
    names_match,
    name_words,
    reverify_staff_bank_account,
    set_staff_bank_account,
)
from .tests_branch_scope import _FinanceBranchFixture


class NameRuleTests(SimpleTestCase):
    """The surname and one other name must both appear, in any order, ignoring case and accents."""

    def match(self, surname, others, holder):
        return names_match(
            surname_words=name_words(surname), other_words=name_words(others), holder=holder)

    def test_either_order_and_extra_names(self):
        self.assertTrue(self.match("Obi", "Ada", "OBI ADA CHINEDU"))
        self.assertTrue(self.match("Obi", "Ada", "ada obi"))
        self.assertTrue(self.match("Obi", "Ada Ngozi", "OBI NGOZI"))

    def test_accents_and_punctuation_are_ignored(self):
        self.assertTrue(self.match("Ọbì", "Àda", "OBI, ADA."))

    def test_each_part_of_a_hyphenated_surname_counts(self):
        self.assertTrue(self.match("Adeyemi-Bello", "Funmi", "BELLO FUNMI"))

    def test_surname_alone_or_first_name_alone_does_not_match(self):
        self.assertFalse(self.match("Obi", "Ada", "OBI CHIDI"))
        self.assertFalse(self.match("Obi", "Ada", "ADA JOHN"))
        self.assertFalse(self.match("Obi", "Ada", "BOLA OKAFOR"))

    def test_a_single_word_name_cannot_match_and_needs_a_second_person(self):
        self.assertFalse(self.match("Obi", "", "OBI ADA"))

    def test_an_empty_holder_never_matches(self):
        self.assertFalse(self.match("Obi", "Ada", ""))


class _BankFixture(_FinanceBranchFixture):
    KEYS = (
        "finance.staff_bank.view", "finance.staff_bank.update", "finance.staff_bank.confirm",
    )

    @classmethod
    def person(cls, tenant, email, first, last):
        return get_user_model().objects.create_user(
            email=email, password="pw", tenant=tenant, branch=None, status="ACTIVE",
            first_name=first, last_name=last,
        )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ada_user = cls.person(cls.tenant, "ada@corona.test", "Ada", "Obi")
        cls.bola_user = cls.person(cls.tenant, "bola@corona.test", "Bola", "Lawal")
        cls.ada = EmployeeSalary.objects.create(
            entity=cls.books, name="Ada Obi", branch=cls.ikeja, employee=cls.ada_user,
            gross_amount=50_000)
        cls.bola = EmployeeSalary.objects.create(
            entity=cls.books, name="Bola Lawal", branch=cls.lekki, employee=cls.bola_user,
            gross_amount=80_000)
        cls.rival_user = cls.person(cls.rival_tenant, "rival@rival.test", "Rita", "Eze")
        EmployeeSalary.objects.create(
            entity=cls.rival_books, name="Rita Eze", branch=cls.rival_branch,
            employee=cls.rival_user, gross_amount=10_000)
        cls.maker = cls.grant(
            cls.person(cls.tenant, "maker@corona.test", "Mr", "Eze"),
            *cls.KEYS, tenant=cls.tenant, role_key="bank-maker")
        cls.checker = cls.grant(
            cls.person(cls.tenant, "checker@corona.test", "Mrs", "Bello"),
            *cls.KEYS, tenant=cls.tenant, role_key="bank-checker")

    def setUp(self):
        super().setUp()
        self.fake = FakeProvider(secret="test-secret")
        self.fake.account_names[("058", "0123456789")] = "OBI ADA CHINEDU"
        self.fake.account_names[("058", "0123456780")] = "BOLA OKAFOR"
        self.fake.account_names[("057", "0123456789")] = "OBI ADA"
        registry.register("PAYSTACK", self.fake)
        self.addCleanup(registry.unregister, "PAYSTACK")

    def save(self, user=None, number="0123456789", bank="058", employee=None, books=None):
        return set_staff_bank_account(
            entity=books or self.books, employee=employee or self.ada_user, bank_code=bank,
            account_number=number, bank_name="GTBank", actor_user=user or self.maker)


class StaffBankStateTests(_BankFixture):

    def test_a_holder_who_matches_is_verified_and_payable(self):
        account = self.save()
        self.assertEqual(account.verification_state, StaffBankState.VERIFIED)
        self.assertEqual(account.resolved_account_name, "OBI ADA CHINEDU")
        self.assertTrue(account.is_payable)
        self.assertEqual(account.changed_by, self.maker)

    def test_a_holder_who_does_not_match_waits_for_a_second_person(self):
        account = self.save(number="0123456780")
        self.assertEqual(account.verification_state, StaffBankState.NEEDS_CONFIRMATION)
        self.assertFalse(account.is_payable)

    def test_an_account_the_provider_cannot_find_fails(self):
        account = self.save(number="0000000001")
        self.assertEqual(account.verification_state, StaffBankState.FAILED)
        self.assertFalse(account.is_payable)

    def test_an_unreachable_provider_leaves_it_unchecked_and_it_can_be_checked_again(self):
        original = self.fake.resolve_account

        def down(**kwargs):
            raise ProviderError("Paystack is down.", provider="PAYSTACK", provider_code="503")

        self.fake.resolve_account = down
        account = self.save()
        self.assertEqual(account.verification_state, StaffBankState.UNVERIFIED)
        self.assertIn("try again", account.verification_note)
        self.fake.resolve_account = original
        again = reverify_staff_bank_account(account=account, actor_user=self.maker)
        self.assertEqual(again.verification_state, StaffBankState.VERIFIED)

    def test_saving_the_same_account_again_checks_nothing_new(self):
        first = self.save()
        self.fake.account_names.clear()
        again = self.save()
        self.assertEqual((again.pk, again.verification_state), (first.pk, StaffBankState.VERIFIED))

    def test_another_tenants_person_is_refused(self):
        with self.assertRaises(ValidationError):
            self.save(employee=self.rival_user)

    def test_a_bad_account_number_or_bank_is_refused(self):
        with self.assertRaises(ValidationError):
            self.save(number="12345")
        with self.assertRaises(ValidationError):
            self.save(bank="")

    def test_every_change_is_audited_by_who_and_where_and_carries_no_account_value(self):
        self.save()
        row = FinanceAuditLog.objects.get(action=FinanceAuditAction.STAFF_BANK_CHANGED)
        self.assertEqual((row.branch_id, row.actor), (self.ikeja.pk, self.maker))
        everything = f"{row.message} {row.before} {row.after} {row.metadata}"
        for value in ("0123456789", "OBI ADA", "Verified", "VERIFIED"):
            self.assertNotIn(value, everything)


class SecondPersonTests(_BankFixture):

    def test_the_person_who_made_the_change_cannot_confirm_it(self):
        account = self.save(number="0123456780")
        with self.assertRaises(PermissionDenied):
            confirm_staff_bank_account(account=account, actor_user=self.maker)
        account.refresh_from_db()
        self.assertEqual(account.verification_state, StaffBankState.NEEDS_CONFIRMATION)

    def test_the_staff_member_cannot_confirm_their_own_account(self):
        account = self.save(number="0123456780")
        with self.assertRaises(PermissionDenied):
            confirm_staff_bank_account(account=account, actor_user=self.ada_user)

    def test_a_different_person_confirms_and_the_account_becomes_payable(self):
        account = self.save(number="0123456780")
        confirmed = confirm_staff_bank_account(account=account, actor_user=self.checker)
        self.assertEqual(confirmed.verification_state, StaffBankState.CONFIRMED)
        self.assertEqual(confirmed.confirmed_by, self.checker)
        self.assertTrue(confirmed.is_payable)
        self.assertTrue(FinanceAuditLog.objects.filter(
            action=FinanceAuditAction.STAFF_BANK_CONFIRMED).exists())

    def test_only_an_account_waiting_for_confirmation_can_be_confirmed(self):
        account = self.save()
        with self.assertRaises(ValidationError):
            confirm_staff_bank_account(account=account, actor_user=self.checker)

    def test_a_change_after_confirmation_resets_it_and_asks_again(self):
        account = self.save(number="0123456780")
        confirm_staff_bank_account(account=account, actor_user=self.checker)
        again = self.save(number="0123456789")  # The bank now names the right holder.
        self.assertEqual(again.verification_state, StaffBankState.VERIFIED)
        self.assertIsNone(again.confirmed_by)
        back = self.save(number="0123456780", user=self.checker)
        self.assertEqual(back.verification_state, StaffBankState.NEEDS_CONFIRMATION)
        self.assertIsNone(back.confirmed_at)
        self.assertEqual(back.changed_by, self.checker)

    def test_any_save_that_changes_the_account_resets_it_whoever_wrote_it(self):
        account = self.save()
        row = StaffBankAccount.objects.get(pk=account.pk)
        row.account_number = "0123456780"
        row.save()  # An admin edit, an import: no service involved.
        row.refresh_from_db()
        self.assertEqual(row.verification_state, StaffBankState.UNVERIFIED)
        self.assertEqual(row.resolved_account_name, "")
        self.assertFalse(row.is_payable)

    def test_changing_only_the_display_name_of_the_bank_does_not_reset_it(self):
        account = self.save()
        row = StaffBankAccount.objects.get(pk=account.pk)
        row.bank_name = "Guaranty Trust"
        row.save()
        row.refresh_from_db()
        self.assertEqual(row.verification_state, StaffBankState.VERIFIED)


class StaffBankApiTests(_BankFixture):

    def put(self, client, user, body, books=None):
        return client.put(
            f"/v1/finance/staff-bank-accounts/{user.pk}/?entity={(books or self.books).code}",
            body, format="json")

    def post(self, client, user, body, books=None):
        return client.post(
            f"/v1/finance/staff-bank-accounts/{user.pk}/?entity={(books or self.books).code}",
            body, format="json")

    def test_saving_through_the_api_verifies_and_hides_nothing_from_a_holder_of_the_key(self):
        client = TenantAPIClient(user=self.maker)
        response = self.post(client, self.ada_user, {
            "bank_code": "058", "account_number": "0123456789", "bank_name": "GTBank"})
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        self.assertEqual(data["verification_state"], "VERIFIED")
        self.assertEqual(data["resolved_account_name"], "OBI ADA CHINEDU")
        self.assertEqual(self.put(client, self.ada_user, {
            "bank_code": "058", "account_number": "0123456789"}).status_code, 200)

    def test_a_caller_without_the_key_is_refused(self):
        nobody = self.user_for(self.tenant, "nobody@corona.test")
        client = TenantAPIClient(user=nobody)
        body = {"bank_code": "058", "account_number": "0123456789"}
        self.assertEqual(self.put(client, self.ada_user, body).status_code, 403)
        self.assertEqual(self.post(client, self.ada_user, body).status_code, 403)
        self.assertEqual(client.get(
            f"/v1/finance/staff-bank-accounts/?entity={self.books.code}").status_code, 403)

    def test_updating_is_not_confirming(self):
        updater = self.grant(
            self.user_for(self.tenant, "upd@corona.test"),
            "finance.staff_bank.view", "finance.staff_bank.update",
            tenant=self.tenant, role_key="bank-updater")
        account = self.save(number="0123456780")
        response = TenantAPIClient(user=updater).post(
            f"/v1/finance/staff-bank-accounts/{self.ada_user.pk}/confirm/?entity={self.books.code}")
        self.assertEqual(response.status_code, 403)
        account.refresh_from_db()
        self.assertEqual(account.verification_state, StaffBankState.NEEDS_CONFIRMATION)

    def test_confirming_through_the_api_needs_a_second_person(self):
        self.save(number="0123456780")
        url = f"/v1/finance/staff-bank-accounts/{self.ada_user.pk}/confirm/?entity={self.books.code}"
        self.assertEqual(TenantAPIClient(user=self.maker).post(url).status_code, 403)
        response = TenantAPIClient(user=self.checker).post(url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["verification_state"], "CONFIRMED")

    def test_another_tenants_staff_are_not_found(self):
        client = TenantAPIClient(user=self.maker)
        body = {"bank_code": "058", "account_number": "0123456789"}
        self.assertEqual(self.post(client, self.rival_user, body).status_code, 404)
        self.assertEqual(self.put(client, self.rival_user, body).status_code, 404)

    def test_a_branch_bound_caller_reaches_only_their_branchs_staff(self):
        lekki_officer = self.grant(
            self.user_for(self.tenant, "lekki@corona.test"), *self.KEYS,
            tenant=self.tenant, role_key="lekki-bank", branch=self.lekki)
        client = TenantAPIClient(user=lekki_officer)
        own = self.post(client, self.bola_user, {"bank_code": "058", "account_number": "0123456789"})
        self.assertEqual(own.status_code, 201, own.data)
        other = self.post(client, self.ada_user, {"bank_code": "058", "account_number": "0123456789"})
        self.assertEqual(other.status_code, 404)
        self.save()  # Ada's, saved by the whole-school bursar.
        listed = client.get(f"/v1/finance/staff-bank-accounts/?entity={self.books.code}")
        self.assertEqual([row["employee_id"] for row in listed.data["data"]], [self.bola_user.pk])

    def test_an_unknown_key_is_refused(self):
        client = TenantAPIClient(user=self.maker)
        response = self.post(client, self.ada_user, {
            "bank_code": "058", "account_number": "0123456789", "verification_state": "VERIFIED"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(StaffBankAccount.objects.exists())

    def test_a_role_that_reads_but_may_not_write_the_account_is_refused_and_nothing_is_saved(self):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import install_declared_fields, set_field_access

        keys = install_declared_fields("finance.staff_bank")
        reader = self.grant(
            self.user_for(self.tenant, "reader@corona.test"), *self.KEYS,
            tenant=self.tenant, role_key="bank-reader")
        set_field_access(
            TenantRoleTemplate.objects.get(tenant=self.tenant, key="bank-reader"),
            *keys, read=True, write=False)
        response = self.post(TenantAPIClient(user=reader), self.ada_user, {
            "bank_code": "058", "account_number": "0123456789"})
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(StaffBankAccount.objects.exists())

    def test_a_role_with_the_account_fields_switched_off_does_not_see_them(self):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import install_declared_fields, set_field_access

        keys = install_declared_fields("finance.staff_bank")
        self.save()
        viewer = self.grant(
            self.user_for(self.tenant, "viewer@corona.test"), "finance.staff_bank.view",
            tenant=self.tenant, role_key="bank-viewer")
        role = TenantRoleTemplate.objects.get(tenant=self.tenant, key="bank-viewer")
        set_field_access(role, *keys, read=False, write=False)
        set_field_access(role, "finance.staff_bank.verification_state", read=True, write=False)
        response = TenantAPIClient(user=viewer).get(
            f"/v1/finance/staff-bank-accounts/{self.ada_user.pk}/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["verification_state"], "VERIFIED")
        for hidden in ("account_number", "resolved_account_name", "bank_code", "bank_name"):
            self.assertNotIn(hidden, data)

    def test_the_bank_list_comes_from_the_provider(self):
        response = TenantAPIClient(user=self.maker).get(
            f"/v1/finance/staff-bank-accounts/banks/?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], [{"name": "Fake MFB", "code": "999"}])
