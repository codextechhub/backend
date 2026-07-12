"""
Cross-tenant isolation tests for TenantAwareManager.

The ambient tenant context is established per-request (TenantJWTAuthentication
sets the ``vs_tenants.context`` contextvar). When it is set, the default
manager on school-owned models must only return rows whose school belongs to
that tenant. ``all_objects`` stays unscoped for platform code.

Note: the old thread-local *school* context (core.thread_locals /
vs_rbac.middleware) was retired in the multi-tenant refactor; scoping now keys
off the tenant contextvar. Tests that used to exercise now-removed model
``school`` fields (WorkflowTemplate/ComplianceRule/ConfigurationAuditEvent moved
to tenant-shaped ownership) were dropped with those fields.
"""
from django.test import TestCase

from vs_tenants.context import set_current_tenant, clear_current_tenant
from vs_schools.models import Branch
from vs_rbac.models import SchoolRoleTemplate

from .helpers import make_school, make_branch


class TenantAwareManagerIsolationTests(TestCase):
    def setUp(self):
        clear_current_tenant()
        self.school_a = make_school(slug="school-a", name="School A")
        self.school_b = make_school(slug="school-b", name="School B")
        self.branch_a = make_branch(self.school_a, name="A Main")
        self.branch_b = make_branch(self.school_b, name="B Main")
        self.role_a = SchoolRoleTemplate.objects.create(
            school=self.school_a, name="Registrar A", status="ACTIVE"
        )
        self.role_b = SchoolRoleTemplate.objects.create(
            school=self.school_b, name="Registrar B", status="ACTIVE"
        )

    def tearDown(self):
        clear_current_tenant()

    def test_no_context_returns_everything(self):
        self.assertEqual(Branch.objects.count(), 2)
        self.assertEqual(SchoolRoleTemplate.objects.count(), 2)

    def test_tenant_context_scopes_default_manager(self):
        set_current_tenant(self.school_a.tenant)
        branches = list(Branch.objects.all())
        roles = list(SchoolRoleTemplate.objects.all())
        self.assertEqual(branches, [self.branch_a])
        self.assertEqual(roles, [self.role_a])

    def test_tenant_context_scopes_filter_and_get(self):
        set_current_tenant(self.school_a.tenant)
        # filter() must not leak the other tenant's rows
        self.assertFalse(Branch.objects.filter(pk=self.branch_b.pk).exists())
        # get() on a foreign row must behave like it doesn't exist
        with self.assertRaises(SchoolRoleTemplate.DoesNotExist):
            SchoolRoleTemplate.objects.get(pk=self.role_b.pk)

    def test_all_objects_is_unscoped(self):
        set_current_tenant(self.school_a.tenant)
        self.assertEqual(Branch.all_objects.count(), 2)
        self.assertEqual(SchoolRoleTemplate.all_objects.count(), 2)

    def test_for_school_overrides_context(self):
        set_current_tenant(self.school_a.tenant)
        roles = list(SchoolRoleTemplate.objects.for_school(self.school_b))
        self.assertEqual(roles, [self.role_b])


class TenantAwareManagerWaveTwoTests(TestCase):
    """Session/security logs stay tenant-scoped under the contextvar."""

    def setUp(self):
        clear_current_tenant()
        self.school_a = make_school(slug="iso2-a", name="Iso2 A")
        self.school_b = make_school(slug="iso2-b", name="Iso2 B")

    def tearDown(self):
        clear_current_tenant()

    def test_login_sessions_and_auth_logs_scoped(self):
        from .helpers import make_branch, make_school_admin
        from vs_user.models import AuthAttempt, LoginSession

        branch_a = make_branch(self.school_a)
        user_a = make_school_admin(branch_a, email="iso2a@test.com", school=self.school_a)
        LoginSession.objects.create(user=user_a, school=self.school_a)
        AuthAttempt.objects.create(email_entered="x@a.com", school=self.school_a, result="FAIL")
        AuthAttempt.objects.create(email_entered="cx@platform.com", school=None, result="FAIL")

        set_current_tenant(self.school_b.tenant)
        self.assertEqual(LoginSession.objects.count(), 0)
        # Platform rows (school NULL) are hidden from other tenants too.
        self.assertEqual(AuthAttempt.objects.count(), 0)

        set_current_tenant(self.school_a.tenant)
        self.assertEqual(LoginSession.objects.count(), 1)
        self.assertEqual(AuthAttempt.objects.count(), 1)
        self.assertEqual(AuthAttempt.all_objects.count(), 2)
