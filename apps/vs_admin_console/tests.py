"""Impersonation security matrix (tenant refactor test plan).

Covers: start permission gate, target/tenant validation, single-active-session
rule, effective-user substitution with target-only permissions (no union with
the actor's platform grants), header forgery, actor mismatch, expiry,
tenant-mismatch assertion, Codex-on-Codex sessions, lifecycle termination
(logout service + tenant deactivation receiver), and dual-identity audit
attribution on impersonated requests.

All API calls go through the real JWT layer (CodeXRefreshToken + ?tenant=),
never force_authenticate — the auth path IS the subject under test.
"""
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_branch,
    make_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_rbac.models import (
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_tenants.models import Tenant
from vs_user.models import User
from vs_user.tokens import CodeXRefreshToken

from .models import ImpersonationSession
from .services import end_impersonations_for_tenant, end_impersonations_for_user


IMPERSONATION_KEYS = (
    "platform.impersonation.start",
    "platform.impersonation.end",
    "platform.impersonation.view",
)


def _grant_platform(user, keys):
    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=user.tenant, key=f"imp-test-{user.pk}",
        defaults={"name": f"Impersonation Test Role {user.pk}", "status": "ACTIVE"},
    )
    for key in keys:
        TenantRolePermission.objects.get_or_create(
            role=role, permission=make_permission(key),
        )
    TenantUserRoleAssignment.objects.get_or_create(
        tenant=user.tenant, user=user, role=role,
        defaults={"assignment_status": "ACTIVE"},
    )
    return role


class ImpersonationTestBase(TestCase):
    def setUp(self):
        self.school = make_school(slug="imp-school", name="Impersonation School")
        self.branch = make_branch(self.school)
        self.target = make_school_admin(
            self.branch, email="target@school.test", school=self.school,
        )
        self.admin = make_vision_user(email="cxadmin@codex.test")
        _grant_platform(self.admin, IMPERSONATION_KEYS)
        self.codex = self.admin.tenant

    def client_for(self, user):
        client = APIClient()
        token = CodeXRefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def start_session(self, *, actor=None, tenant_slug=None, target=None, **overrides):
        actor = actor or self.admin
        tenant_slug = tenant_slug or self.school.tenant.slug
        target = target or self.target
        payload = {
            "target_user": target.pk,
            "justification": "Investigating a reported billing defect.",
            **overrides,
        }
        return self.client_for(actor).post(
            f"/v1/admin/impersonations/start/?tenant={tenant_slug}", payload,
        )


class ImpersonationStartTests(ImpersonationTestBase):
    def test_start_requires_platform_permission(self):
        unprivileged = make_vision_user(email="nostart@codex.test")
        resp = self.start_session(actor=unprivileged)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_school_actor_cannot_start(self):
        # A school user cannot even assert the foreign tenant: non-enumerating 404
        # from the auth layer, regardless of any permission they hold.
        other_school = make_school(slug="imp-other", name="Other School")
        other_branch = make_branch(other_school)
        school_actor = make_school_admin(
            other_branch, email="sneaky@other.test", school=other_school,
        )
        resp = self.start_session(actor=school_actor)
        self.assertEqual(resp.status_code, 404)

    def test_start_creates_active_session(self):
        resp = self.start_session()
        self.assertEqual(resp.status_code, 201)
        session = ImpersonationSession.objects.get()
        self.assertEqual(session.staff_user, self.admin)
        self.assertEqual(session.target_user, self.target)
        self.assertEqual(session.tenant, self.school.tenant)
        self.assertEqual(session.status, "ACTIVE")

    def test_target_must_belong_to_asserted_tenant(self):
        other_school = make_school(slug="imp-elsewhere", name="Elsewhere")
        other_branch = make_branch(other_school)
        foreign_target = make_school_admin(
            other_branch, email="foreign@elsewhere.test", school=other_school,
        )
        resp = self.start_session(target=foreign_target)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_inactive_target_rejected(self):
        self.target.status = User.Status.SUSPENDED
        self.target.save(update_fields=["status"])
        resp = self.start_session()
        self.assertEqual(resp.status_code, 404)

    def test_second_concurrent_session_rejected(self):
        self.assertEqual(self.start_session().status_code, 201)
        resp = self.start_session()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ImpersonationSession.objects.count(), 1)

    def test_codex_staff_may_impersonate_codex_staff(self):
        codex_target = make_vision_user(email="peer@codex.test")
        resp = self.start_session(
            tenant_slug=self.codex.slug, target=codex_target,
        )
        self.assertEqual(resp.status_code, 201)
        session = ImpersonationSession.objects.get()
        self.assertEqual(session.tenant, self.codex)


class ImpersonatedRequestTests(ImpersonationTestBase):
    def setUp(self):
        super().setUp()
        self.start_session()
        self.session = ImpersonationSession.objects.get()

    def impersonated_client(self):
        client = self.client_for(self.admin)
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(self.admin).access_token}",
            HTTP_X_IMPERSONATION_SESSION=str(self.session.pk),
        )
        return client

    def test_effective_user_is_target(self):
        resp = self.impersonated_client().get(
            f"/v1/user/auth/me/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["data"]["user"]["email"], self.target.email,
        )

    def test_actor_platform_permissions_are_not_unioned(self):
        # The actor holds platform.impersonation.view; the target does not.
        # While impersonating, RBAC must evaluate ONLY the target.
        resp = self.impersonated_client().get(
            f"/v1/admin/impersonations/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 403)

    def test_tenant_assertion_must_match_session(self):
        resp = self.impersonated_client().get(
            f"/v1/user/auth/me/?tenant={self.codex.slug}"
        )
        self.assertEqual(resp.status_code, 404)

    def test_forged_session_id_rejected(self):
        client = self.client_for(self.admin)
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(self.admin).access_token}",
            HTTP_X_IMPERSONATION_SESSION=str(self.session.pk + 999),
        )
        resp = client.get(f"/v1/user/auth/me/?tenant={self.school.tenant.slug}")
        self.assertEqual(resp.status_code, 401)

    def test_actor_mismatch_rejected(self):
        # Another Codex admin cannot ride a session they did not start.
        other_admin = make_vision_user(email="cxadmin2@codex.test")
        _grant_platform(other_admin, IMPERSONATION_KEYS)
        client = self.client_for(other_admin)
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(other_admin).access_token}",
            HTTP_X_IMPERSONATION_SESSION=str(self.session.pk),
        )
        resp = client.get(f"/v1/user/auth/me/?tenant={self.school.tenant.slug}")
        self.assertEqual(resp.status_code, 401)

    def test_expired_session_rejected_and_flipped(self):
        ImpersonationSession.objects.filter(pk=self.session.pk).update(
            ends_at=timezone.now() - timezone.timedelta(minutes=1),
        )
        resp = self.impersonated_client().get(
            f"/v1/user/auth/me/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 401)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "EXPIRED")

    def test_suspended_target_terminates_session(self):
        self.target.status = User.Status.SUSPENDED
        self.target.save(update_fields=["status"])
        resp = self.impersonated_client().get(
            f"/v1/user/auth/me/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 401)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "ENDED")

    def test_impersonated_request_audits_both_identities(self):
        from vs_audit.models import AuditEvent

        resp = self.impersonated_client().get(
            f"/v1/user/auth/me/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 200)
        event = AuditEvent.objects.filter(
            impersonation_session=self.session,
        ).latest("event_at")
        self.assertEqual(event.actor_user, self.admin)
        self.assertEqual(event.effective_user, self.target)


class ImpersonationLifecycleTests(ImpersonationTestBase):
    def _active(self):
        return ImpersonationSession.objects.filter(status="ACTIVE").count()

    def test_end_endpoint_requires_owner(self):
        self.start_session()
        session = ImpersonationSession.objects.get()
        other_admin = make_vision_user(email="cxadmin3@codex.test")
        _grant_platform(other_admin, IMPERSONATION_KEYS)
        resp = self.client_for(other_admin).post(
            f"/v1/admin/impersonations/end/?tenant={self.codex.slug}",
            {"session_id": session.pk},
        )
        self.assertEqual(resp.status_code, 404)
        resp = self.client_for(self.admin).post(
            f"/v1/admin/impersonations/end/?tenant={self.codex.slug}",
            {"session_id": session.pk},
        )
        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, "ENDED")

    def test_user_termination_service_ends_sessions(self):
        self.start_session()
        self.assertEqual(self._active(), 1)
        end_impersonations_for_user(self.admin)
        self.assertEqual(self._active(), 0)

    def test_tenant_deactivation_ends_sessions(self):
        self.start_session()
        self.assertEqual(self._active(), 1)
        tenant = self.school.tenant
        tenant.status = Tenant.Status.SUSPENDED
        tenant.save()
        self.assertEqual(self._active(), 0)

    def test_service_end_for_tenant(self):
        self.start_session()
        end_impersonations_for_tenant(self.school.tenant)
        self.assertEqual(self._active(), 0)
