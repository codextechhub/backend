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
