"""What the staff bank accounts screen reads and writes beyond the account itself.

Mr Eze, Corona's whole-school bursar, adds Ada Obi's account: POST adds, PUT edits,
and an add over an account already on file is a 409 so two bursars cannot overwrite
each other. Mrs Bello, a second person, sees a Confirm button only on accounts she
may confirm. The Add picker lists the payroll staff who have no account, within the
reach of whoever asks, with only the key to add an account.
"""
from __future__ import annotations

from unittest import mock

from core.test_utils import TenantAPIClient

from .constants import StaffBankState
from .models import EmployeeSalary, StaffBankAccount
from .staff_banking import (
    confirm_staff_bank_account,
    reader_may_confirm,
    set_staff_bank_account,
)
from .tests_staff_banking import _BankFixture


class AddAndEditTests(_BankFixture):
    """Adding an account never replaces one on file: POST adds, PUT edits."""

    GOOD = {"bank_code": "058", "account_number": "0123456789", "bank_name": "GTBank"}

    def url(self, user=None):
        return (f"/v1/finance/staff-bank-accounts/{(user or self.ada_user).pk}/"
                f"?entity={self.books.code}")

    def test_adding_when_one_exists_is_a_409_and_the_account_is_untouched(self):
        client = TenantAPIClient(user=self.maker)
        self.assertEqual(client.post(self.url(), self.GOOD, format="json").status_code, 201)
        before = StaffBankAccount.objects.get(employee=self.ada_user)
        response = client.post(self.url(), {
            "bank_code": "058", "account_number": "0123456780"}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "STAFF_BANK_ACCOUNT_EXISTS")
        after = StaffBankAccount.objects.get(employee=self.ada_user)
        self.assertEqual((after.pk, after.account_number, after.verification_state),
                         (before.pk, "0123456789", StaffBankState.VERIFIED))

    def test_the_service_refuses_an_add_over_an_account_under_the_lock(self):
        from .exceptions import StaffBankAccountExistsError

        self.save()
        with self.assertRaises(StaffBankAccountExistsError):
            set_staff_bank_account(
                entity=self.books, employee=self.ada_user, bank_code="058",
                account_number="0123456780", actor_user=self.maker, must_be_new=True)
        self.assertEqual(
            StaffBankAccount.objects.get(employee=self.ada_user).account_number, "0123456789")

    def test_editing_changes_the_account_and_answers_200(self):
        client = TenantAPIClient(user=self.maker)
        client.post(self.url(), self.GOOD, format="json")
        response = client.put(self.url(), {
            "bank_code": "058", "account_number": "0123456780"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["verification_state"], "NEEDS_CONFIRMATION")

    def test_editing_an_account_that_does_not_exist_is_a_404_and_creates_nothing(self):
        response = TenantAPIClient(user=self.maker).put(self.url(), self.GOOD, format="json")
        self.assertEqual(response.status_code, 404, response.data)
        self.assertFalse(StaffBankAccount.objects.exists())


class CanConfirmTests(_BankFixture):
    """``can_confirm`` is the reader's own answer: waiting, holds the key, and a second person."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.viewer = cls.grant(
            cls.user_for(cls.tenant, "viewer@corona.test"), "finance.staff_bank.view",
            tenant=cls.tenant, role_key="bank-viewer")
        cls.ada_reader = cls.grant(
            cls.ada_user, "finance.staff_bank.view", "finance.staff_bank.confirm",
            tenant=cls.tenant, role_key="ada-reader")

    def detail(self, user, employee=None):
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/staff-bank-accounts/{(employee or self.ada_user).pk}/"
            f"?entity={self.books.code}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_true_for_a_second_person_holding_the_key(self):
        self.save(number="0123456780")
        self.assertIs(self.detail(self.checker)["can_confirm"], True)

    def test_false_for_the_person_who_made_the_last_change(self):
        self.save(number="0123456780")
        self.assertIs(self.detail(self.maker)["can_confirm"], False)

    def test_false_for_a_reader_without_the_confirm_key(self):
        self.save(number="0123456780")
        self.assertIs(self.detail(self.viewer)["can_confirm"], False)

    def test_false_for_the_staff_member_even_holding_the_key(self):
        self.save(number="0123456780")
        self.assertIs(self.detail(self.ada_reader)["can_confirm"], False)

    def test_false_unless_the_account_is_waiting_for_confirmation(self):
        self.save()
        self.assertIs(self.detail(self.checker)["can_confirm"], False)
        self.save(number="0123456780")
        confirm_staff_bank_account(
            account=StaffBankAccount.objects.get(employee=self.ada_user), actor_user=self.checker)
        self.assertIs(self.detail(self.checker)["can_confirm"], False)

    def test_a_session_riding_as_the_changer_is_judged_as_the_real_person(self):
        account = self.save(number="0123456780")
        with mock.patch("vs_finance.staff_banking.real_person", return_value=self.maker):
            self.assertFalse(reader_may_confirm(account, self.checker, self.tenant))
        self.assertTrue(reader_may_confirm(account, self.checker, self.tenant))

    def test_the_list_carries_it_for_each_row_by_the_reader(self):
        self.save(number="0123456780")
        self.save(number="0123456780", employee=self.bola_user, user=self.checker)
        listed = TenantAPIClient(user=self.checker).get(
            f"/v1/finance/staff-bank-accounts/?entity={self.books.code}")
        self.assertEqual(listed.status_code, 200, listed.data)
        flags = {row["employee_id"]: row["can_confirm"] for row in listed.data["data"]}
        self.assertEqual(flags, {self.ada_user.pk: True, self.bola_user.pk: False})

    def test_it_goes_with_the_verification_status_switch(self):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import install_declared_fields, set_field_access

        keys = install_declared_fields("finance.staff_bank")
        self.save(number="0123456780")
        role = TenantRoleTemplate.objects.get(tenant=self.tenant, key="bank-checker")
        set_field_access(role, *keys, read=True, write=False)
        self.assertIn("can_confirm", self.detail(self.checker))
        set_field_access(role, "finance.staff_bank.verification_state", read=False, write=False)
        data = self.detail(self.checker)
        for hidden in ("can_confirm", "verification_state", "is_payable"):
            self.assertNotIn(hidden, data)


class MissingStaffTests(_BankFixture):
    """The Add picker lists payroll staff with no account, within the caller's reach."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.updater = cls.grant(
            cls.user_for(cls.tenant, "updater@corona.test"), "finance.staff_bank.update",
            tenant=cls.tenant, role_key="bank-updater-only")
        cls.lekki_officer = cls.grant(
            cls.user_for(cls.tenant, "lekki-bank@corona.test"), *cls.KEYS,
            tenant=cls.tenant, role_key="lekki-bank", branch=cls.lekki)
        cls.solo_user = cls.person(cls.solo_tenant, "sade@solo.test", "Sade", "Bello")
        EmployeeSalary.objects.create(
            entity=cls.solo_books, name="Sade Bello", branch=cls.solo_main,
            employee=cls.solo_user, gross_amount=60_000)
        cls.solo_officer = cls.grant(
            cls.person(cls.solo_tenant, "solo-bank@solo.test", "Solo", "Officer"), *cls.KEYS,
            tenant=cls.solo_tenant, role_key="solo-bank")

    def listed(self, user, books=None, query=""):
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/staff-bank-accounts/missing/?entity={(books or self.books).code}{query}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_everyone_without_an_account_is_listed_with_id_name_and_branch(self):
        rows = self.listed(self.maker)
        self.assertEqual(
            {(r["employee_id"], r["employee_name"], r["branch_name"]) for r in rows},
            {(self.ada_user.pk, "Ada Obi", "Ikeja Branch"),
             (self.bola_user.pk, "Bola Lawal", "Lekki Branch")})
        self.assertEqual(
            set(rows[0]), {"employee_id", "employee_name", "branch_id", "branch_name"})

    def test_someone_who_has_an_account_is_left_out(self):
        self.save()
        self.assertEqual([r["employee_id"] for r in self.listed(self.maker)], [self.bola_user.pk])

    def test_a_branch_bound_caller_is_offered_only_their_branchs_staff(self):
        self.assertEqual(
            [r["employee_id"] for r in self.listed(self.lekki_officer)], [self.bola_user.pk])

    def test_inactive_staff_and_roster_names_with_no_login_are_left_out(self):
        EmployeeSalary.objects.filter(pk=self.ada.pk).update(is_active=False)
        EmployeeSalary.objects.create(
            entity=self.books, name="Casual Helper", branch=self.lekki, gross_amount=10_000)
        self.assertEqual([r["employee_id"] for r in self.listed(self.maker)], [self.bola_user.pk])

    def test_search_narrows_by_name(self):
        self.assertEqual(
            [r["employee_id"] for r in self.listed(self.maker, query="&search=ada")],
            [self.ada_user.pk])

    def test_the_update_key_alone_is_enough_and_the_view_key_is_not(self):
        self.assertEqual(len(self.listed(self.updater)), 2)
        viewer = self.grant(
            self.user_for(self.tenant, "viewer2@corona.test"), "finance.staff_bank.view",
            tenant=self.tenant, role_key="bank-viewer-2")
        response = TenantAPIClient(user=viewer).get(
            f"/v1/finance/staff-bank-accounts/missing/?entity={self.books.code}")
        self.assertEqual(response.status_code, 403)

    def test_another_tenants_staff_never_appear(self):
        ids = {r["employee_id"] for r in self.listed(self.maker)}
        self.assertNotIn(self.rival_user.pk, ids)
        response = TenantAPIClient(user=self.maker).get(
            f"/v1/finance/staff-bank-accounts/missing/?entity={self.rival_books.code}")
        self.assertIn(response.status_code, (403, 404))

    def test_a_single_branch_school_lists_its_staff_too(self):
        rows = self.listed(self.solo_officer, books=self.solo_books)
        self.assertEqual([(r["employee_id"], r["branch_name"]) for r in rows],
                         [(self.solo_user.pk, "Main Branch")])

    def test_the_list_is_paginated(self):
        response = TenantAPIClient(user=self.maker).get(
            f"/v1/finance/staff-bank-accounts/missing/?entity={self.books.code}&page_size=1")
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["pagination"]["totalItems"], 2)

    def test_the_picker_follows_the_staff_member_switch(self):
        from vs_rbac.models import TenantRoleTemplate
        from vs_rbac.tests.helpers import install_declared_fields, set_field_access

        keys = install_declared_fields("finance.staff_bank")
        role = TenantRoleTemplate.objects.get(tenant=self.tenant, key="bank-maker")
        set_field_access(role, *keys, read=True, write=False)
        set_field_access(role, "finance.staff_bank.employee", read=False, write=False)
        row = self.listed(self.maker)[0]
        self.assertEqual(set(row), {"branch_id", "branch_name"})
