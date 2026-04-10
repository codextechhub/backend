"""
Tests for vs_rbac permission classes.
"""
from unittest.mock import MagicMock
from django.test import TestCase

from vs_rbac.permissions import (
    IsAuthenticatedAndActive,
    IsVisionStaff,
    IsSchoolAdmin,
    ReadOnly,
)
from .helpers import make_school, make_branch, make_vision_user, make_school_admin, make_staff_user


def _make_request(user=None, method="GET"):
    request = MagicMock()
    request.user = user
    request.method = method
    return request


class IsAuthenticatedAndActiveTests(TestCase):
    def setUp(self):
        self.perm = IsAuthenticatedAndActive()
        self.view = MagicMock()

    def test_anonymous_denied(self):
        request = _make_request(user=MagicMock(is_authenticated=False))
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_none_user_denied(self):
        request = _make_request(user=None)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_active_user_allowed(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch)
        request = _make_request(user=user)
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_suspended_user_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, status="SUSPENDED")
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_locked_user_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, status="LOCKED")
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_deleted_user_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch, status="DELETED")
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_vision_staff_allowed(self):
        user = make_vision_user()
        request = _make_request(user=user)
        self.assertTrue(self.perm.has_permission(request, self.view))


class IsVisionStaffTests(TestCase):
    def setUp(self):
        self.perm = IsVisionStaff()
        self.view = MagicMock()

    def test_vision_staff_allowed(self):
        user = make_vision_user()
        request = _make_request(user=user)
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_school_admin_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch)
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_staff_user_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_staff_user(branch)
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_anonymous_denied(self):
        request = _make_request(user=MagicMock(is_authenticated=False))
        self.assertFalse(self.perm.has_permission(request, self.view))


class IsSchoolAdminTests(TestCase):
    def setUp(self):
        self.perm = IsSchoolAdmin()
        self.view = MagicMock()

    def test_school_admin_allowed(self):
        school = make_school()
        branch = make_branch(school)
        user = make_school_admin(branch)
        request = _make_request(user=user)
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_vision_staff_denied(self):
        user = make_vision_user()
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_regular_staff_denied(self):
        school = make_school()
        branch = make_branch(school)
        user = make_staff_user(branch)
        request = _make_request(user=user)
        self.assertFalse(self.perm.has_permission(request, self.view))


class ReadOnlyTests(TestCase):
    def setUp(self):
        self.perm = ReadOnly()
        self.view = MagicMock()

    def test_get_allowed(self):
        request = _make_request(method="GET")
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_head_allowed(self):
        request = _make_request(method="HEAD")
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_options_allowed(self):
        request = _make_request(method="OPTIONS")
        self.assertTrue(self.perm.has_permission(request, self.view))

    def test_post_denied(self):
        request = _make_request(method="POST")
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_put_denied(self):
        request = _make_request(method="PUT")
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_delete_denied(self):
        request = _make_request(method="DELETE")
        self.assertFalse(self.perm.has_permission(request, self.view))

    def test_patch_denied(self):
        request = _make_request(method="PATCH")
        self.assertFalse(self.perm.has_permission(request, self.view))
