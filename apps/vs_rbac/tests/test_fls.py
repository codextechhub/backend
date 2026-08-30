"""Field-Level Security (FLS) behaviour + wiring guards.

Two concerns are covered:

1.  ``FieldSecurityMixin`` actually strips a protected field when the caller
    lacks the grant, returns it when they hold it, and no-ops when there is no
    request context or the caller is the Vision super admin.
2.  The finance / procurement / payments serialisers that carry PII (bank
    account numbers, beneficiary details, salaries) declare the expected
    ``read_permissions`` wiring - so a refactor that drops the mixin or renames
    a key fails loudly here instead of silently leaking data.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.http import HttpRequest
from django.test import TestCase
from rest_framework import serializers

from vs_rbac.fls import FieldSecurityMixin


def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


class _DemoSerializer(FieldSecurityMixin, serializers.Serializer):
    public = serializers.CharField()
    secret = serializers.CharField()

    read_permissions = {"secret": "demo.thing.view_sensitive"}


def _request_with(user, perms):
    """Build a request whose FLS permission set is pre-resolved to *perms*."""
    request = HttpRequest()
    request.user = user
    if perms is not None:
        request._fls_permissions = set(perms)
    return request


class FieldSecurityMixinBehaviourTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        from vs_user.models import User

        # A plain Vision staff user with no platform assignment - authenticated
        # but NOT the super admin, so FLS applies to them.
        cls.user = User.objects.create_user(tenant=_platform_tenant(), 
            email="fls-probe@test.com",
            password="testpass123",
            status="ACTIVE",
            first_name="Fls",
            last_name="Probe",
        )

    def _represent(self, perms, context_request=True):
        request = _request_with(self.user, perms) if context_request else None
        ser = _DemoSerializer(
            SimpleNamespace(public="p", secret="s"),
            context={"request": request} if context_request else {},
        )
        return ser.data

    def test_protected_field_stripped_without_grant(self):
        data = self._represent(perms=set())
        self.assertNotIn("secret", data)
        self.assertIn("public", data)
        self.assertEqual(data.get("_stripped_fields"), ["secret"])

    def test_protected_field_present_with_grant(self):
        data = self._represent(perms={"demo.thing.view_sensitive"})
        self.assertEqual(data.get("secret"), "s")
        self.assertNotIn("_stripped_fields", data)

    def test_no_request_context_skips_fls(self):
        data = self._represent(perms=None, context_request=False)
        self.assertEqual(data.get("secret"), "s")
        self.assertNotIn("_stripped_fields", data)

    def test_super_admin_bypasses_fls(self):
        # Super admin holds no explicit field grant, but the bypass exposes all
        # fields regardless. Patch the predicate the mixin consults.
        with mock.patch("vs_rbac.permissions.is_vision_super_admin", return_value=True):
            data = self._represent(perms=set())
        self.assertEqual(data.get("secret"), "s")
        self.assertNotIn("_stripped_fields", data)


class SensitiveSerializerWiringTest(TestCase):
    """The PII-bearing serialisers must keep their FLS wiring intact.

    The comparisons below are exhaustive on purpose, so they fail in both
    directions: a field dropping out of the gate is the failure that matters,
    and a field being added fails too. The second is noise-shaped but is the
    price of catching the first, so a newly gated field is added here in the
    same change that gates it - which is what did not happen when bank_code
    and beneficiary_bank_code were.
    """

    def test_payments_serializers_wired(self):
        from vs_payments.serializers import (
            PayoutInstructionSerializer,
            VirtualAccountSerializer,
        )

        self.assertTrue(issubclass(VirtualAccountSerializer, FieldSecurityMixin))
        self.assertEqual(
            VirtualAccountSerializer.read_permissions,
            {
                "account_number": "payments.virtual_account.view_sensitive",
                "account_name": "payments.virtual_account.view_sensitive",
            },
        )
        self.assertTrue(issubclass(PayoutInstructionSerializer, FieldSecurityMixin))
        self.assertEqual(
            PayoutInstructionSerializer.read_permissions,
            {
                "beneficiary_name": "payments.payout.view_sensitive",
                "beneficiary_account_number": "payments.payout.view_sensitive",
                "beneficiary_bank_code": "payments.payout.view_sensitive",
            },
        )

    def test_procurement_vendor_serializer_wired(self):
        from vs_procurement.serializers import VendorSerializer

        self.assertTrue(issubclass(VendorSerializer, FieldSecurityMixin))
        self.assertEqual(
            VendorSerializer.read_permissions,
            {
                "email": "procurement.vendor.view_sensitive",
                "phone": "procurement.vendor.view_sensitive",
                "contacts": "procurement.vendor.view_sensitive",
                "address": "procurement.vendor.view_sensitive",
                "tax_id": "procurement.vendor.view_sensitive",
                "bank_name": "procurement.vendor.view_sensitive",
                "bank_code": "procurement.vendor.view_sensitive",
                "bank_account_number": "procurement.vendor.view_sensitive",
                "bank_account_name": "procurement.vendor.view_sensitive",
            },
        )

    def test_finance_serializers_wired(self):
        from vs_finance.serializers import BankAccountSerializer, PayrollLineSerializer

        self.assertTrue(issubclass(BankAccountSerializer, FieldSecurityMixin))
        self.assertEqual(
            BankAccountSerializer.read_permissions,
            {"account_number": "finance.bankaccount.view_sensitive"},
        )
        self.assertTrue(issubclass(PayrollLineSerializer, FieldSecurityMixin))
        self.assertEqual(
            PayrollLineSerializer.read_permissions,
            {
                "employee_name": "finance.payrollrun.view_sensitive",
                "gross_amount": "finance.payrollrun.view_sensitive",
                "paye_amount": "finance.payrollrun.view_sensitive",
                "pension_amount": "finance.payrollrun.view_sensitive",
                "net_amount": "finance.payrollrun.view_sensitive",
                "components": "finance.payrollrun.view_sensitive",
            },
        )
