"""
Cross-tenant isolation tests for TenantAwareManager adoption (B2).

When the thread-local school context is set (established per-request by
TenantJWTAuthentication / TenantContextMiddleware), the default manager on
school-owned models must only return rows belonging to that school.
``all_objects`` stays unscoped for platform code.
"""
from django.test import TestCase

from core.thread_locals import clear_current_school, set_current_school
from vs_schools.models import Branch
from vs_rbac.models import SchoolRoleTemplate

from .helpers import make_school, make_branch


class TenantAwareManagerIsolationTests(TestCase):
    def setUp(self):
        clear_current_school()
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
        clear_current_school()

    def test_no_context_returns_everything(self):
        self.assertEqual(Branch.objects.count(), 2)
        self.assertEqual(SchoolRoleTemplate.objects.count(), 2)

    def test_school_context_scopes_default_manager(self):
        set_current_school(self.school_a)
        branches = list(Branch.objects.all())
        roles = list(SchoolRoleTemplate.objects.all())
        self.assertEqual(branches, [self.branch_a])
        self.assertEqual(roles, [self.role_a])

    def test_school_context_scopes_filter_and_get(self):
        set_current_school(self.school_a)
        # filter() must not leak the other school's rows
        self.assertFalse(Branch.objects.filter(pk=self.branch_b.pk).exists())
        # get() on a foreign row must behave like it doesn't exist
        with self.assertRaises(SchoolRoleTemplate.DoesNotExist):
            SchoolRoleTemplate.objects.get(pk=self.role_b.pk)

    def test_all_objects_is_unscoped(self):
        set_current_school(self.school_a)
        self.assertEqual(Branch.all_objects.count(), 2)
        self.assertEqual(SchoolRoleTemplate.all_objects.count(), 2)

    def test_for_school_overrides_context(self):
        set_current_school(self.school_a)
        roles = list(SchoolRoleTemplate.objects.for_school(self.school_b))
        self.assertEqual(roles, [self.role_b])


class TenantAwareManagerWaveTwoTests(TestCase):
    """B2 follow-up: session/security logs, notifications, workflow, audit, config."""

    def setUp(self):
        clear_current_school()
        self.school_a = make_school(slug="iso2-a", name="Iso2 A")
        self.school_b = make_school(slug="iso2-b", name="Iso2 B")

    def tearDown(self):
        clear_current_school()

    def test_login_sessions_and_auth_logs_scoped(self):
        from .helpers import make_branch, make_school_admin
        from vs_user.models import AuthAttempt, LoginSession

        branch_a = make_branch(self.school_a)
        user_a = make_school_admin(branch_a, email="iso2a@test.com", school=self.school_a)
        LoginSession.objects.create(user=user_a, school=self.school_a)
        AuthAttempt.objects.create(email_entered="x@a.com", school=self.school_a, result="FAIL")
        AuthAttempt.objects.create(email_entered="cx@platform.com", school=None, result="FAIL")

        set_current_school(self.school_b)
        self.assertEqual(LoginSession.objects.count(), 0)
        # Platform rows (school NULL) are hidden from school users too.
        self.assertEqual(AuthAttempt.objects.count(), 0)

        set_current_school(self.school_a)
        self.assertEqual(LoginSession.objects.count(), 1)
        self.assertEqual(AuthAttempt.objects.count(), 1)
        self.assertEqual(AuthAttempt.all_objects.count(), 2)

    def test_workflow_templates_include_global_rows(self):
        from vs_workflow.models import WorkflowTemplate

        WorkflowTemplate.objects.create(
            school=self.school_a, document_type="leave.request", code="std-a", name="A"
        )
        WorkflowTemplate.objects.create(
            school=None, document_type="leave.request", code="global", name="Global"
        )

        set_current_school(self.school_b)
        codes = set(WorkflowTemplate.objects.values_list("code", flat=True))
        # B sees the global template but never A's.
        self.assertEqual(codes, {"global"})

        set_current_school(self.school_a)
        codes = set(WorkflowTemplate.objects.values_list("code", flat=True))
        self.assertEqual(codes, {"std-a", "global"})

    def test_compliance_rules_include_global_rows(self):
        from vs_audit.models import ComplianceRule

        ComplianceRule.objects.create(school=self.school_a, name="A rule")
        ComplianceRule.objects.create(school=None, name="Global rule")

        set_current_school(self.school_b)
        names = set(ComplianceRule.objects.values_list("name", flat=True))
        self.assertEqual(names, {"Global rule"})

    def test_config_audit_events_include_global_rows(self):
        from vs_config.models import ConfigurationAuditEvent

        ConfigurationAuditEvent.objects.create(
            school=self.school_a, action="config.value.updated",
            target_type="ConfigurationValue", target_id="k",
        )
        ConfigurationAuditEvent.objects.create(
            action="config.value.updated",
            target_type="ConfigurationValue", target_id="g",
        )

        set_current_school(self.school_b)
        keys = set(ConfigurationAuditEvent.objects.values_list("target_id", flat=True))
        self.assertEqual(keys, {"g"})

        set_current_school(self.school_a)
        keys = set(ConfigurationAuditEvent.objects.values_list("target_id", flat=True))
        self.assertEqual(keys, {"k", "g"})
