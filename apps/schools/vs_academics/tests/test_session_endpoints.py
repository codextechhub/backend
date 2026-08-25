"""The HTTP surface for sessions and terms.

Security first, because those are the cases that must not be deferred: a
missing key, another school's row, and a school that has not gone live.
"""
from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)
from vs_user.tokens import CodeXRefreshToken
from schools.vs_academics.models import AcademicSession, AcademicTerm, SessionStatus
from schools.vs_academics.services.sessions import archive_session, set_branches

D = dt.date

KEYS = (
    "academics.session.view",
    "academics.session.create",
    "academics.session.update",
    "academics.session.manage",
)


def _school_with_admin(slug, name, email, keys=KEYS, status="ACTIVE"):
    school = make_school(slug=slug, name=name, status=status)
    branch = make_branch(school, name="Main Campus", is_main=True)
    role = make_role(school, name="School Admin", key="school_admin")
    for key in keys:
        make_role_permission(role, make_permission(key, scope=PermissionScope.TENANT))
    admin = make_school_admin(None, email=email, tenant=school.tenant)
    make_assignment(school, admin, role, branch=None)
    return school, branch, admin


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school, cls.main, cls.admin = _school_with_admin(
            "brightfield", "Brightfield Schools", "adaeze@brightfield.test",
        )
        cls.tenant = cls.school.tenant
        cls.other, cls.other_branch, cls.other_admin = _school_with_admin(
            "sunrise", "Sunrise Academy", "tunde@sunrise.test",
        )

    def client_for(self, user):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
        )
        return client

    def url(self, name, **kwargs):
        return reverse(name, kwargs=kwargs or None)

    def session(self, tenant=None, name="2026/2027", status=SessionStatus.DRAFT):
        return AcademicSession.all_objects.create(
            tenant=tenant or self.tenant, name=name,
            start_date=D(2026, 9, 1), end_date=D(2027, 7, 31), status=status,
        )


class SecurityTests(_Base):
    def test_a_caller_without_the_key_is_refused(self):
        school, _b, nobody = _school_with_admin(
            "greenfield", "Greenfield", "no@greenfield.test", keys=(),
        )
        response = self.client_for(nobody).get(
            self.url("academics-session-list"), {"tenant": school.tenant.slug},
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_another_schools_session_is_a_404_not_a_403(self):
        """So a session id cannot be used to probe another school."""
        theirs = self.session(tenant=self.other.tenant)
        response = self.client_for(self.admin).get(
            self.url("academics-session-detail", pk=theirs.pk),
            {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_another_schools_session_cannot_be_activated(self):
        theirs = self.session(tenant=self.other.tenant)
        response = self.client_for(self.admin).post(
            self.url("academics-session-activate", pk=theirs.pk),
            {}, format="json", QUERY_STRING=f"tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 404, response.data)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, SessionStatus.DRAFT)

    def test_a_request_with_no_tenant_is_refused(self):
        response = self.client_for(self.admin).get(self.url("academics-session-list"))
        self.assertEqual(response.status_code, 400, response.data)

    def test_no_session_of_another_tenant_appears_in_the_list(self):
        self.session(name="mine")
        self.session(tenant=self.other.tenant, name="theirs")
        response = self.client_for(self.admin).get(
            self.url("academics-session-list"), {"tenant": self.tenant.slug},
        )
        names = {row["name"] for row in response.data["data"]}
        self.assertEqual(names, {"mine"})


class PendingTenantTests(TestCase):
    """A school builds its structure before it goes live, or it never does."""

    def setUp(self):
        self.school, self.branch, self.admin = _school_with_admin(
            "pending-school", "Pending School", "head@pending.test",
            status="PENDING",
        )
        self.tenant = self.school.tenant
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, "PENDING")

    def test_a_pending_school_can_build_its_year(self):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(self.admin).access_token}",
        )
        response = client.post(
            f"{reverse('academics-session-list')}?tenant={self.tenant.slug}",
            {
                "name": "2026/2027",
                "start_date": "2026-09-01",
                "end_date": "2027-07-31",
                "terms": [
                    {"name": "First Term", "order_index": 1,
                     "start_date": "2026-09-01", "end_date": "2026-12-11"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(AcademicTerm.all_objects.count(), 1)

    def test_every_view_declares_the_pending_surface(self):
        """Asserted by enumerating the URL conf, not by sampling.

        A view added in six months is caught by this test rather than by a
        customer who cannot finish onboarding.
        """
        from schools.vs_academics import urls

        missing = []
        for pattern in urls.urlpatterns:
            view = pattern.callback.cls
            if getattr(view, "pending_tenant_surface", None) is not True:
                missing.append(f"{view.__name__} ({pattern.name})")
        self.assertEqual(
            missing, [],
            "every view in this module must be reachable before go-live",
        )


class SessionWriteTests(_Base):
    def post_session(self, **overrides):
        body = {
            "name": "2026/2027",
            "start_date": "2026-09-01",
            "end_date": "2027-07-31",
        }
        body.update(overrides)
        return self.client_for(self.admin).post(
            f"{reverse('academics-session-list')}?tenant={self.tenant.slug}",
            body, format="json",
        )

    def test_a_year_and_its_terms_are_created_in_one_call(self):
        response = self.post_session(terms=[
            {"name": "First Term", "order_index": 1,
             "start_date": "2026-09-01", "end_date": "2026-12-11"},
            {"name": "Second Term", "order_index": 2,
             "start_date": "2027-01-05", "end_date": "2027-04-01"},
        ])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["term_count"], 2)

    def test_terms_numbered_against_their_dates_are_refused(self):
        """Non-overlap is not enough: these two do not overlap at all."""
        response = self.post_session(terms=[
            {"name": "First Term", "order_index": 1,
             "start_date": "2027-01-05", "end_date": "2027-04-01"},
            {"name": "Second Term", "order_index": 2,
             "start_date": "2026-09-01", "end_date": "2026-12-11"},
        ])
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TERM_ORDER_CONFLICT")
        self.assertEqual(AcademicSession.all_objects.count(), 0)

    def test_overlapping_terms_are_refused(self):
        response = self.post_session(terms=[
            {"name": "First Term", "order_index": 1,
             "start_date": "2026-09-01", "end_date": "2026-12-11"},
            {"name": "Second Term", "order_index": 2,
             "start_date": "2026-12-11", "end_date": "2027-04-01"},
        ])
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TERM_DATES_OVERLAP")

    def test_a_term_outside_the_session_is_refused(self):
        response = self.post_session(terms=[
            {"name": "First Term", "order_index": 1,
             "start_date": "2025-01-01", "end_date": "2025-04-01"},
        ])
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TERM_OUTSIDE_SESSION")

    def test_a_duplicate_name_is_refused(self):
        self.post_session()
        response = self.post_session()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["code"], "DUPLICATE")

    def test_case_alone_is_not_a_different_name(self):
        self.post_session(name="2026/2027")
        response = self.post_session(name="2026/2027 ")
        # Trailing space is a different string; the case that matters is below.
        del response
        response = self.client_for(self.admin).post(
            f"{reverse('academics-session-list')}?tenant={self.tenant.slug}",
            {"name": "2026/2027", "start_date": "2027-09-01",
             "end_date": "2028-07-31"}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_an_archived_year_refuses_an_edit(self):
        session = self.session()
        archive_session(session, self.tenant)
        response = self.client_for(self.admin).patch(
            f"{reverse('academics-session-detail', kwargs={'pk': session.pk})}"
            f"?tenant={self.tenant.slug}",
            {"name": "renamed"}, format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "SESSION_ARCHIVED_READ_ONLY",
        )


class TermRouteTests(_Base):
    def setUp(self):
        self.sess = self.session()
        self.term = AcademicTerm.all_objects.create(
            tenant=self.tenant, session=self.sess, name="First Term",
            order_index=1, start_date=D(2026, 9, 1), end_date=D(2026, 12, 11),
        )

    def test_a_term_of_a_draft_year_is_deleted(self):
        response = self.client_for(self.admin).delete(
            f"{reverse('academics-term-detail', kwargs={'pk': self.term.pk})}"
            f"?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(AcademicTerm.all_objects.filter(pk=self.term.pk).exists())

    def test_a_term_of_a_live_year_is_not_deleted(self):
        self.sess.status = SessionStatus.ACTIVE
        self.sess.save(update_fields=["status"])
        response = self.client_for(self.admin).delete(
            f"{reverse('academics-term-detail', kwargs={'pk': self.term.pk})}"
            f"?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "TERM_SESSION_NOT_DRAFT")
        self.assertTrue(AcademicTerm.all_objects.filter(pk=self.term.pk).exists())

    def test_an_archived_year_answers_first(self):
        """Both refusals are asserted: one hiding behind the other is how a
        missing guard is discovered years later."""
        archive_session(self.sess, self.tenant)
        response = self.client_for(self.admin).delete(
            f"{reverse('academics-term-detail', kwargs={'pk': self.term.pk})}"
            f"?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"], "SESSION_ARCHIVED_READ_ONLY",
        )

    def test_no_standalone_term_archive_route(self):
        """Asserted against the URL conf rather than by trusting the router.

        A term is never archived on its own. If somebody restores the route in
        six months this fails and asks them to read FRD v2.6 FR-003 first.
        """
        from schools.vs_academics import urls

        routes = {str(p.pattern) for p in urls.urlpatterns}
        self.assertNotIn("terms/<int:pk>/archive/", routes)
        for route in routes:
            self.assertFalse(
                route.startswith("terms/") and route.endswith("archive/"),
                f"a standalone term archive route reappeared: {route}",
            )


class ListShapeTests(_Base):
    def test_an_empty_list_is_still_a_list(self):
        response = self.client_for(self.admin).get(
            self.url("academics-session-list"), {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], [])

    def test_the_list_is_paginated(self):
        self.session(name="a")
        response = self.client_for(self.admin).get(
            self.url("academics-session-list"), {"tenant": self.tenant.slug},
        )
        self.assertIn("pagination", response.data)

    def test_status_and_search_filter(self):
        self.session(name="2026/2027")
        self.session(name="2027/2028", status=SessionStatus.ACTIVE)
        client = self.client_for(self.admin)
        active = client.get(
            self.url("academics-session-list"),
            {"tenant": self.tenant.slug, "status": "ACTIVE"},
        )
        self.assertEqual([r["name"] for r in active.data["data"]], ["2027/2028"])
        found = client.get(
            self.url("academics-session-list"),
            {"tenant": self.tenant.slug, "search": "2026"},
        )
        self.assertEqual([r["name"] for r in found.data["data"]], ["2026/2027"])

    def test_a_single_branch_school_sees_no_branch_dimension(self):
        """The dimension recedes at one branch: absent, not greyed out."""
        self.session(name="2026/2027")
        response = self.client_for(self.admin).get(
            self.url("academics-session-list"), {"tenant": self.tenant.slug},
        )
        row = response.data["data"][0]
        for field in ("branches", "scope_label", "is_school_wide"):
            self.assertNotIn(field, row, f"{field} must be absent at one branch")

    def test_a_multi_branch_school_sees_the_scope(self):
        make_branch(self.school, name="Ikeja Campus", is_main=False)
        session = self.session(name="2026/2027")
        set_branches(session, self.tenant, [])
        response = self.client_for(self.admin).get(
            self.url("academics-session-list"), {"tenant": self.tenant.slug},
        )
        row = response.data["data"][0]
        self.assertEqual(row["scope_label"], "The whole school")
        self.assertEqual(row["branches"], [])
