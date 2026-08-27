"""Who may open an expense receipt.

The receipt is the sharpest case for per-record authorisation because the tenant
check cannot reach it. Corona's bursar and Corona's classroom assistant are the
same tenant, hold equally valid sessions, and would both have satisfied
``/media/`` as it stood. Only one of them should be able to open a photograph of
what a colleague spent and where they were when they spent it.
"""
from __future__ import annotations

import datetime

from django.core.files.base import ContentFile
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from core import media
from core.models import StoredFile
from vs_finance.models import Account, ExpenseClaim, ExpenseClaimLine, LedgerEntity


class ExpenseReceiptPolicyTests(TestCase):
    def setUp(self):
        from schools.vs_schools.models import School, SchoolStatus
        from vs_rbac.tests.helpers import make_branch
        from vs_finance.seed import seed_chart_of_accounts, seed_currencies
        from vs_tenants.context import clear_current_tenant, set_current_tenant
        from vs_user.models import User

        self.school = School.objects.create(
            name="Corona Secondary School", slug="corona-secondary",
            status=SchoolStatus.ACTIVE,
        )
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school)
        seed_currencies()
        self.entity = LedgerEntity.objects.create(
            name="Corona Books", code="CRN", kind=LedgerEntity.Kind.TENANT,
            tenant=self.tenant,
        )
        seed_chart_of_accounts(self.entity)

        self.claimant = User.objects.create_user(
            tenant=self.tenant, email="ada@corona.test", password="testpass123",
            status="ACTIVE", first_name="Ada", last_name="Okonkwo",
        )
        self.assistant = User.objects.create_user(
            tenant=self.tenant, email="tunde@corona.test", password="testpass123",
            status="ACTIVE", first_name="Tunde", last_name="Bello",
        )

        claim = ExpenseClaim.objects.create(
            entity=self.entity, claimant=self.claimant,
            claim_date=datetime.date(2026, 1, 10), title="Conference travel",
        )
        self.line = ExpenseClaimLine.objects.create(
            claim=claim,
            expense_account=Account.objects.filter(entity=self.entity).first(),
            quantity=1, unit_price=500000, line_no=1,
        )
        set_current_tenant(self.tenant)
        self.addCleanup(clear_current_tenant)
        self.line.receipt.save("taxi.pdf", ContentFile(b"%PDF receipt"), save=True)
        clear_current_tenant()
        self.name = self.line.receipt.name

    def _request(self, user):
        request = APIRequestFactory().get(
            f"/media/{self.name}", {media.TOKEN_PARAM: media.sign(self.name, user)},
        )
        request.user = user
        request.tenant = self.tenant
        return request

    def _row(self):
        return StoredFile.objects.get(name=self.name)

    def test_the_receipt_is_bound_to_its_line_and_its_school(self):
        row = self._row()
        self.assertEqual(row.tenant_id, self.tenant.pk)
        self.assertEqual(row.owner, self.line)

    def test_the_claimant_can_reopen_her_own_receipt(self):
        """Before finance has looked at it, she is the only one who can."""
        self.assertTrue(media.authorize(self._request(self.claimant), self._row()))

    def test_a_colleague_in_the_same_school_cannot(self):
        """The case the tenant check can never catch, because both pass it."""
        self.assertFalse(media.authorize(self._request(self.assistant), self._row()))

    def test_finance_can(self):
        """The verb that opens the claim is the verb that opens its receipt."""
        from vs_rbac.tests.helpers import (
            make_assignment, make_permission, make_role, make_role_permission,
        )

        role = make_role(self.tenant, name="Bursar")
        make_role_permission(role, make_permission("finance.expenseclaim.view"))
        make_assignment(self.tenant, self.assistant, role)
        self.assertTrue(media.authorize(self._request(self.assistant), self._row()))
