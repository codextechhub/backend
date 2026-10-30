"""Resetting a staff member's two-step sign-in from their profile, for a lost phone.

Mr Eze has lost his phone. The school administrator clears his authenticator
and backup codes, and he sets up again at his next sign-in. Nobody clears
their own, and the key is the account-update key.
"""
from vs_rbac.models import TenantRolePermission
from vs_user import totp
from vs_user.models import TwoStepAuthenticator, TwoStepRecoveryCode

from ..exceptions import CannotActOnSelf
from ..services import accounts
from .base import StaffFixture


class TwoStepResetTests(StaffFixture):
    def setUp(self):
        TwoStepAuthenticator.objects.create(
            user=self.eze.user, secret=totp.new_secret(), confirmed_at="2026-10-01T00:00:00Z",
        )
        TwoStepRecoveryCode.objects.create(user=self.eze.user, code_hash="digest")

    def test_the_administrator_clears_his_authenticator_and_backup_codes(self):
        response = self.post(self.admin, "staff-account-reset-two-step", pk=self.eze.pk)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(TwoStepAuthenticator.objects.filter(user=self.eze.user).exists())
        self.assertFalse(TwoStepRecoveryCode.objects.filter(user=self.eze.user).exists())

    def test_it_needs_the_account_update_key(self):
        TenantRolePermission.objects.filter(
            role=self.role, permission__key="school.administrators.update",
        ).delete()

        response = self.post(self.admin, "staff-account-reset-two-step", pk=self.eze.pk)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertTrue(TwoStepAuthenticator.objects.filter(user=self.eze.user).exists())

    def test_a_teacher_cannot(self):
        response = self.post(self.ikeja_teacher.user, "staff-account-reset-two-step", pk=self.eze.pk)

        self.assertEqual(response.status_code, 403, response.data)

    def test_nobody_resets_their_own(self):
        with self.assertRaises(CannotActOnSelf):
            accounts.reset_two_step(self.eze, actor=self.eze.user)
        self.assertTrue(TwoStepAuthenticator.objects.filter(user=self.eze.user).exists())

    def test_another_schools_person_is_not_found(self):
        response = self.post(self.solo_admin, "staff-account-reset-two-step", pk=self.eze.pk)

        self.assertEqual(response.status_code, 404, response.data)
