"""Every literal segment resolves to its own view, not to the pk pattern.

Django tries URL patterns in list order, so ``/v1/i/me/staff/posting/`` declared
after ``/v1/i/me/staff/<int:pk>/`` would never be reached: the pk pattern is
tried first, fails to parse "posting" as an integer, and the request falls
through to a 404 about a person who does not exist. It is the kind of mistake
that survives review because the file reads correctly and only the ORDER is
wrong.

The mount order matters for the same reason and is asserted too: ``v1/i/me/staff/``
is included BEFORE ``v1/i/``, whose own ``<str:slug>/`` pattern would otherwise
swallow everything under it.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import resolve, reverse


class LiteralSegmentTests(SimpleTestCase):
    #: Every route whose first segment is a word rather than a number.
    LITERALS = {
        "staff-bulk-posting": "/v1/i/me/staff/posting/",
        "staff-roster": "/v1/i/me/staff/roster/",
        "staff-search": "/v1/i/me/staff/search/",
        "staff-bulk-role": "/v1/i/me/staff/roles/bulk/",
        "staff-teaching-coverage": "/v1/i/me/staff/teaching/coverage/",
        "staff-class-teacher": "/v1/i/me/staff/teaching/class-teacher/",
    }

    def test_each_literal_segment_reverses_to_the_path_it_claims(self):
        for name, path in self.LITERALS.items():
            with self.subTest(route=name):
                self.assertEqual(reverse(name), path)

    def test_each_literal_segment_resolves_to_its_own_view(self):
        """The assertion that actually catches the ordering mistake.

        Reversing works whatever the order, because it matches on the name.
        Resolving is what goes wrong: it walks the list from the top.
        """
        for name, path in self.LITERALS.items():
            with self.subTest(route=name):
                self.assertEqual(resolve(path).url_name, name)

    def test_the_child_detail_prefixes_do_not_collide_with_a_person(self):
        """``qualifications/3/`` is a qualification, not person 3's something.

        These four sit at the same depth as ``<int:pk>/`` and are distinguished
        only by their first segment being a word.
        """
        for name, path in (
            ("staff-qualification-detail", "/v1/i/me/staff/qualifications/3/"),
            ("staff-document-detail", "/v1/i/me/staff/documents/3/"),
            ("staff-leave-detail", "/v1/i/me/staff/leave/3/"),
            ("staff-teaching-detail", "/v1/i/me/staff/teaching/3/"),
        ):
            with self.subTest(route=name):
                self.assertEqual(resolve(path).url_name, name)

    def test_a_person_still_resolves(self):
        self.assertEqual(resolve("/v1/i/me/staff/3/").url_name, "staff-detail")
        self.assertEqual(
            resolve("/v1/i/me/staff/3/status/").url_name, "staff-status",
        )

    def test_the_staff_mount_is_reached_before_the_school_slug_pattern(self):
        """``v1/i/<str:slug>/`` would otherwise catch ``me/staff/`` and its children."""
        self.assertEqual(resolve("/v1/i/me/staff/").url_name, "staff-list")


class SurfaceTests(SimpleTestCase):
    """Which views a school still onboarding may reach.

    Read off the classes rather than asserted by hand, because the attribute is
    the whole mechanism: ``TenantSurfaceAllowed`` refuses a PENDING tenant
    anything that does not declare it, and the ABSENCE of the attribute is what
    means closed. A view added later without thinking about it is closed by
    omission, which is the right default and the reason this test lists both
    sides rather than only the open one.
    """

    OPEN = {
        "StaffListCreateView", "StaffDetailView", "StaffSearchView",
        "StaffBulkPostingView", "StaffRosterView", "StaffBulkRoleView",
        "StaffRolesView", "StaffResendInvitationView",
        "QualificationListCreateView", "QualificationDetailView",
        "DocumentListCreateView", "DocumentDetailView",
    }
    CLOSED = {
        "StaffStatusView", "StaffHistoryView", "StaffInvitationRevokeView",
        "StaffAccountSuspendView", "StaffAccountReactivateView",
        "StaffAccountUnlockView", "StaffAccountEmailView",
        "StaffTeachingView", "TeachingAssignmentDetailView",
        "ClassTeacherView", "TeachingCoverageView",
        "StaffLeaveView", "LeaveDetailView",
    }

    def _views(self):
        from schools.vs_staff import urls

        seen = {}
        for pattern in urls.urlpatterns:
            view = pattern.callback.cls
            seen[view.__name__] = getattr(view, "pending_tenant_surface", False)
        return seen

    def test_every_view_is_accounted_for_on_one_side_or_the_other(self):
        self.assertEqual(set(self._views()), self.OPEN | self.CLOSED)

    def test_the_onboarding_surfaces_are_open(self):
        views = self._views()
        for name in self.OPEN:
            with self.subTest(view=name):
                self.assertTrue(views[name], f"{name} should be open before go-live")

    def test_everything_else_is_closed(self):
        views = self._views()
        for name in self.CLOSED:
            with self.subTest(view=name):
                self.assertFalse(views[name], f"{name} should be closed before go-live")
