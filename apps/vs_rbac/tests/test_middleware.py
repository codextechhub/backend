"""
Tests for vs_rbac middleware:
- TenantContextMiddleware: injects school into request + thread-local
- TenantBoundaryEnforcementMiddleware: blocks cross-school access for school users
"""
from unittest.mock import MagicMock, patch
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.test import TestCase, RequestFactory

from core.thread_locals import get_current_school, clear_current_school
from vs_rbac.middleware import (
    TenantContextMiddleware,
    TenantBoundaryEnforcementMiddleware,
    _get_school_from_request,
)
from .helpers import (
    make_school,
    make_branch,
    make_vision_user,
    make_school_admin,
    make_staff_user,
)


def _ok_response(request):
    return HttpResponse("OK")


# =============================================================================
# _get_school_from_request (helper function)
# =============================================================================
class GetSchoolFromRequestTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)

    def test_unauthenticated_returns_none(self):
        request = HttpRequest()
        request.user = MagicMock(is_authenticated=False)
        result = _get_school_from_request(request)
        self.assertIsNone(result)

    def test_no_user_returns_none(self):
        request = HttpRequest()
        result = _get_school_from_request(request)
        self.assertIsNone(result)

    def test_vision_staff_returns_none(self):
        """Vision staff bypass school scoping — no school context."""
        request = HttpRequest()
        request.user = make_vision_user()
        result = _get_school_from_request(request)
        self.assertIsNone(result)

    def test_caches_result(self):
        """Second call returns the cached value without querying again."""
        request = HttpRequest()
        request.user = make_vision_user()
        _get_school_from_request(request)
        self.assertTrue(hasattr(request, "_cached_school"))
        # Second call should return the cached value
        result = _get_school_from_request(request)
        self.assertIsNone(result)

    def test_explicit_school_id_on_request(self):
        """If request.school_id is set (e.g. from JWT), it's used."""
        request = HttpRequest()
        admin = make_school_admin(self.branch)
        request.user = admin
        request.school_id = self.school.slug
        result = _get_school_from_request(request)
        self.assertEqual(result, self.school)

    def test_explicit_invalid_school_id_raises(self):
        request = HttpRequest()
        admin = make_school_admin(self.branch)
        request.user = admin
        request.school_id = "nonexistent-school"
        with self.assertRaises(PermissionDenied):
            _get_school_from_request(request)


# =============================================================================
# TenantContextMiddleware
# =============================================================================
class TenantContextMiddlewareTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.middleware = TenantContextMiddleware(_ok_response)
        # Ensure clean thread-local state
        clear_current_school()

    def tearDown(self):
        clear_current_school()

    def test_sets_request_school_property(self):
        """Middleware sets request.school as a lazy-loaded property."""
        request = HttpRequest()
        request.user = make_school_admin(self.branch)
        request.school_id = self.school.slug

        response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        # request.school should have been set
        self.assertTrue(hasattr(request, "school"))

    def test_sets_thread_local_for_school_user(self):
        """School context is stored in thread-local for ORM access."""
        request = HttpRequest()
        admin = make_school_admin(self.branch)
        request.user = admin
        request.school_id = self.school.slug

        thread_local_school_during_request = []

        def capture_response(req):
            thread_local_school_during_request.append(get_current_school())
            return HttpResponse("OK")

        middleware = TenantContextMiddleware(capture_response)
        middleware(request)

        self.assertEqual(thread_local_school_during_request[0], self.school)

    def test_clears_thread_local_after_request(self):
        """Thread-local is cleaned up after the request completes."""
        request = HttpRequest()
        admin = make_school_admin(self.branch)
        request.user = admin
        request.school_id = self.school.slug

        self.middleware(request)

        # After middleware completes, thread-local should be cleared
        self.assertIsNone(get_current_school())

    def test_vision_staff_no_thread_local(self):
        """Vision staff should NOT set thread-local school (they bypass scoping)."""
        request = HttpRequest()
        request.user = make_vision_user()

        thread_local_school_during_request = []

        def capture_response(req):
            thread_local_school_during_request.append(get_current_school())
            return HttpResponse("OK")

        middleware = TenantContextMiddleware(capture_response)
        middleware(request)

        self.assertIsNone(thread_local_school_during_request[0])

    def test_unauthenticated_no_thread_local(self):
        request = HttpRequest()
        request.user = MagicMock(is_authenticated=False)

        thread_local_school_during_request = []

        def capture_response(req):
            thread_local_school_during_request.append(get_current_school())
            return HttpResponse("OK")

        middleware = TenantContextMiddleware(capture_response)
        middleware(request)

        self.assertIsNone(thread_local_school_during_request[0])

    def test_clears_stale_thread_local_at_start(self):
        """If a previous request left stale data, middleware clears it."""
        from core.thread_locals import set_current_school
        stale_school = make_school(slug="stale-school", name="Stale")
        set_current_school(stale_school)

        request = HttpRequest()
        request.user = MagicMock(is_authenticated=False)

        thread_local_school_during_request = []

        def capture_response(req):
            thread_local_school_during_request.append(get_current_school())
            return HttpResponse("OK")

        middleware = TenantContextMiddleware(capture_response)
        middleware(request)

        # Stale school should have been cleared before processing
        self.assertIsNone(thread_local_school_during_request[0])


# =============================================================================
# TenantBoundaryEnforcementMiddleware
# =============================================================================
class TenantBoundaryEnforcementMiddlewareTests(TestCase):
    def setUp(self):
        self.school = make_school()
        self.branch = make_branch(self.school)
        self.middleware = TenantBoundaryEnforcementMiddleware(_ok_response)

    def test_unauthenticated_passes_through(self):
        """Unauthenticated requests are skipped (handled by auth layer)."""
        request = HttpRequest()
        request.user = MagicMock(is_authenticated=False)
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_vision_staff_passes_through(self):
        """Vision staff bypass tenant enforcement."""
        request = HttpRequest()
        request.user = make_vision_user()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_school_admin_with_school_context_passes(self):
        """School admin with a valid school context should pass."""
        request = HttpRequest()
        request.user = make_school_admin(self.branch)
        request.school = self.school
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_school_admin_without_school_context_denied(self):
        """School admin with NO school context is a security violation."""
        request = HttpRequest()
        request.user = make_school_admin(self.branch)
        request.school = None
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_staff_user_without_school_context_denied(self):
        request = HttpRequest()
        request.user = make_staff_user(self.branch)
        request.school = None
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_student_without_school_context_denied(self):
        from vs_user.models import User
        student = User.objects.create_user(
            email="student@test.com",
            password="testpass123",
            user_type="STUDENT",
            status="ACTIVE",
            first_name="Test",
            last_name="Student",
            school=self.school,
            branch=self.branch,
        )
        request = HttpRequest()
        request.user = student
        request.school = None
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_parent_without_school_context_denied(self):
        from vs_user.models import User
        parent = User.objects.create_user(
            email="parent@test.com",
            password="testpass123",
            user_type="PARENT",
            status="ACTIVE",
            first_name="Test",
            last_name="Parent",
            school=self.school,
            branch=self.branch,
        )
        request = HttpRequest()
        request.user = parent
        request.school = None
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_staff_with_school_context_passes(self):
        request = HttpRequest()
        request.user = make_staff_user(self.branch)
        request.school = self.school
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_no_user_attribute_passes(self):
        """Requests with no user at all are skipped."""
        request = HttpRequest()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)
