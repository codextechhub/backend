"""What the permission-key field mixin still does.

``FieldSecurityMixin`` strips a protected field when the caller lacks the
grant, returns it when they hold it, and no-ops when there is no request
context or the caller is the Vision super admin.

No production serializer declares a key map any more: which fields a person
reads and writes is Field Access's answer, and the serializers that carried
maps now name a resource instead (``vs_rbac.field_enforcement``). What each
one withholds is proved against the registry in ``test_field_registry`` and
against a real caller in each module's own suite. The mixin itself survives
this slice unused, and goes with the keys it reads.
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

    def test_protected_field_is_absent_without_grant(self):
        """Absent, with nothing naming it: a response says nothing about it."""
        data = self._represent(perms=set())
        self.assertNotIn("secret", data)
        self.assertIn("public", data)
        self.assertEqual(set(data), {"public"})

    def test_protected_field_present_with_grant(self):
        data = self._represent(perms={"demo.thing.view_sensitive"})
        self.assertEqual(data.get("secret"), "s")

    def test_no_request_context_skips_fls(self):
        data = self._represent(perms=None, context_request=False)
        self.assertEqual(data.get("secret"), "s")

    def test_super_admin_bypasses_fls(self):
        # Super admin holds no explicit field grant, but the bypass exposes all
        # fields regardless. Patch the predicate the mixin consults.
        with mock.patch("vs_rbac.permissions.is_vision_super_admin", return_value=True):
            data = self._represent(perms=set())
        self.assertEqual(data.get("secret"), "s")
