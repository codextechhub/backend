"""Impersonation security matrix (tenant refactor test plan).

Covers: start permission gate, target/tenant validation, single-active-session
rule, effective-user substitution with target-only permissions (no union with
the actor's platform grants), header forgery, actor mismatch, expiry,
tenant-mismatch assertion, Codex-on-Codex sessions, lifecycle termination
(logout service + tenant deactivation receiver), and dual-identity audit
attribution on impersonated requests.

All API calls go through the real JWT layer (CodeXRefreshToken + ?tenant=),
never force_authenticate - the auth path IS the subject under test.
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


# start_all covers every target kind; end/view are scope-agnostic.
IMPERSONATION_KEYS = (
    "platform.impersonation.start_all",
    "platform.impersonation.end",
    "platform.impersonation.view",
)

# The school-scoped namespace. Deliberately disjoint from the platform keys:
# holding one set must never satisfy a check for the other.
SCHOOL_IMPERSONATION_KEYS = (
    "school.impersonation.start",
    "school.impersonation.end",
    "school.impersonation.view",
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


# The helper is tenant-agnostic - it builds the role on the user's OWN tenant -
# so school actors use the same machinery under a clearer name.
_grant_school = _grant_platform


class ImpersonationTestBase(TestCase):
    def setUp(self):
        self.school = make_school(slug="imp-school", name="Impersonation School")
        self.branch = make_branch(self.school)
        self.target = make_school_admin(
            self.branch, email="target@school.test",
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

    def test_school_actor_cannot_start_in_a_foreign_tenant(self):
        # School impersonation exists now, but it stops at the tenant edge: a
        # school user cannot even assert the foreign tenant, so this is a
        # non-enumerating 404 from the auth layer - even when they hold the
        # full school.impersonation.* set in their OWN tenant.
        other_school = make_school(slug="imp-other", name="Other School")
        other_branch = make_branch(other_school)
        school_actor = make_school_admin(
            other_branch, email="sneaky@other.test",
        )
        _grant_school(school_actor, SCHOOL_IMPERSONATION_KEYS)
        resp = self.start_session(actor=school_actor)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_school_actor_without_the_school_key_is_forbidden(self):
        # Same tenant this time, so the auth layer lets it through - the denial
        # must come from RBAC, and platform.* keys must not satisfy it.
        #
        # Since Permission.scope, the platform keys cannot even be granted
        # inside a school tenant, so the refusal now happens one layer earlier.
        # Both halves are asserted: the grant is refused, and the actor - left
        # holding nothing - is refused too.
        from django.core.exceptions import ValidationError

        school_actor = make_school_admin(self.branch, email="nokey@school.test")
        with self.assertRaises(ValidationError):
            _grant_school(school_actor, IMPERSONATION_KEYS)  # platform.* only

        resp = self.start_session(actor=school_actor)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_the_target_is_labelled_by_role_not_by_persona(self):
        """"Staff proxied Staff" is not an audit trail anybody can review.

        A CS lead proxying Corona's principal and a CS lead proxying a Year 4
        teacher are the same row if the label reads the persona, because both
        people are STAFF. The role is the only thing that still tells them
        apart, so it is what the label carries.
        """
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment

        role = TenantRoleTemplate.objects.create(
            tenant=self.school.tenant, key="principal",
            name="Principal", status="ACTIVE",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=self.school.tenant, user=self.target, role=role,
            assignment_status="ACTIVE",
        )

        payload = self.start_session().json()["data"]

        self.assertEqual(payload["target_type_label"], "Principal")

    def test_start_creates_active_session(self):
        from vs_audit.models import AuditEvent

        resp = self.start_session()
        self.assertEqual(resp.status_code, 201)
        session = ImpersonationSession.objects.get()
        self.assertEqual(session.staff_user, self.admin)
        self.assertEqual(session.target_user, self.target)
        self.assertEqual(session.tenant, self.school.tenant)
        self.assertEqual(session.status, "ACTIVE")
        payload = resp.json()["data"]
        self.assertEqual(payload["tenant_name"], self.school.tenant.name)
        self.assertEqual(payload["tenant_slug"], self.school.tenant.slug)
        self.assertEqual(payload["staff_type_label"], "CX Staff")
        # There is no persona to name at all now, and the target here holds no
        # active role and no denormalised role string, so the label falls all
        # the way through to the one thing still true of the account: which
        # side of the platform boundary it sits on. A target WITH a role gets
        # its role named - see the impersonation serializer tests - which is
        # what a reviewer reading a proxy log actually needs to know.
        self.assertEqual(payload["target_type_label"], "Tenant user")
        event = AuditEvent.objects.get(
            impersonation_session=session, action_type="IMPERSONATION_STARTED",
        )
        self.assertEqual(event.actor_user, self.admin)
        self.assertEqual(event.effective_user, self.target)
        self.assertIn("started a proxy session", event.summary)

    def test_xvs_role_holder_is_labeled_xvs_staff(self):
        xvs_admin = make_vision_user(
            email="xvs-admin@codex.test", super_admin=True,
        )

        resp = self.start_session(actor=xvs_admin)

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["data"]["staff_type_label"], "XVS Staff")

    def test_target_must_belong_to_asserted_tenant(self):
        other_school = make_school(slug="imp-elsewhere", name="Elsewhere")
        other_branch = make_branch(other_school)
        foreign_target = make_school_admin(
            other_branch, email="foreign@elsewhere.test",
        )
        resp = self.start_session(target=foreign_target)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_inactive_target_rejected(self):
        self.target.status = User.Status.SUSPENDED
        self.target.save(update_fields=["status"])
        resp = self.start_session()
        self.assertEqual(resp.status_code, 404)

    def test_start_while_active_atomically_switches_target(self):
        self.assertEqual(self.start_session().status_code, 201)
        other_target = make_school_admin(
            self.branch, email="other-target@school.test",
        )
        resp = self.start_session(target=other_target)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ImpersonationSession.objects.count(), 2)
        first, second = ImpersonationSession.objects.order_by("started_at", "pk")
        self.assertEqual(first.status, "ENDED")
        self.assertIsNotNone(first.ended_at)
        self.assertEqual(second.status, "ACTIVE")
        self.assertEqual(second.target_user, other_target)

    def test_failed_switch_keeps_current_session_active(self):
        self.assertEqual(self.start_session().status_code, 201)
        current = ImpersonationSession.objects.get()
        resp = self.start_session(target=self.admin)
        self.assertEqual(resp.status_code, 404)
        current.refresh_from_db()
        self.assertEqual(current.status, "ACTIVE")

    def test_start_without_reason_or_duration_is_manual_until_exit(self):
        resp = self.client_for(self.admin).post(
            f"/v1/admin/impersonations/start/?tenant={self.school.tenant.slug}",
            {"target_user": self.target.pk},
        )
        self.assertEqual(resp.status_code, 201)
        session = ImpersonationSession.objects.get()
        self.assertIsNone(session.ends_at)
        self.assertEqual(session.justification, "Started from proxy user menu.")

    def test_codex_staff_may_impersonate_codex_staff(self):
        codex_target = make_vision_user(email="peer@codex.test")
        resp = self.start_session(
            tenant_slug=self.codex.slug, target=codex_target,
        )
        self.assertEqual(resp.status_code, 201)
        session = ImpersonationSession.objects.get()
        self.assertEqual(session.tenant, self.codex)


class ImpersonationScopeTests(ImpersonationTestBase):
    """The tiered start permissions: the target's tenant kind decides which key
    is required. start_all → anyone; start_cx → CX staff only; start_school →
    school users only."""

    def _scoped_admin(self, email, *scope_keys):
        admin = make_vision_user(email=email)
        _grant_platform(admin, ("platform.impersonation.view", *scope_keys))
        return admin

    def test_no_start_key_cannot_impersonate(self):
        # view-only: may list sessions, but starting one is denied.
        viewer = self._scoped_admin("viewonly@codex.test")
        self.assertEqual(self.start_session(actor=viewer).status_code, 403)

    def test_start_cx_can_impersonate_cx_but_not_school(self):
        admin = self._scoped_admin("cxonly@codex.test", "platform.impersonation.start_cx")
        codex_target = make_vision_user(email="cxtarget@codex.test")
        # CX target (PLATFORM tenant) → allowed.
        self.assertEqual(
            self.start_session(actor=admin, tenant_slug=self.codex.slug, target=codex_target).status_code,
            201,
        )
        # School target → denied (needs start_school or start_all).
        ImpersonationSession.objects.update(status="ENDED", ended_at=timezone.now())
        self.assertEqual(self.start_session(actor=admin).status_code, 403)

    def test_start_school_can_impersonate_school_but_not_cx(self):
        admin = self._scoped_admin("schoolonly@codex.test", "platform.impersonation.start_school")
        # School target → allowed.
        self.assertEqual(self.start_session(actor=admin).status_code, 201)
        # CX target → denied (needs start_cx or start_all).
        ImpersonationSession.objects.update(status="ENDED", ended_at=timezone.now())
        codex_target = make_vision_user(email="cxtarget2@codex.test")
        self.assertEqual(
            self.start_session(actor=admin, tenant_slug=self.codex.slug, target=codex_target).status_code,
            403,
        )

    def test_start_all_covers_both_kinds(self):
        admin = self._scoped_admin("allscopes@codex.test", "platform.impersonation.start_all")
        self.assertEqual(self.start_session(actor=admin).status_code, 201)
        ImpersonationSession.objects.update(status="ENDED", ended_at=timezone.now())
        codex_target = make_vision_user(email="cxtarget3@codex.test")
        self.assertEqual(
            self.start_session(actor=admin, tenant_slug=self.codex.slug, target=codex_target).status_code,
            201,
        )


class ImpersonationTargetSearchTests(ImpersonationTestBase):
    def search(self, actor, query="target"):
        return self.client_for(actor).get(
            f"/v1/admin/impersonations/targets/?tenant={actor.tenant.slug}&search={query}"
        )

    def test_search_requires_a_start_permission(self):
        viewer = make_vision_user(email="target-viewer@codex.test")
        _grant_platform(viewer, ("platform.impersonation.view",))
        self.assertEqual(self.search(viewer).status_code, 403)

    def test_school_scope_returns_only_school_users(self):
        actor = make_vision_user(email="target-school-scope@codex.test")
        _grant_platform(actor, ("platform.impersonation.start_school",))
        make_vision_user(email="target-platform@codex.test")
        resp = self.search(actor)
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertTrue(any(row["id"] == self.target.pk for row in rows))
        self.assertTrue(all(row["tenant_slug"] != actor.tenant.slug for row in rows))

    def test_cx_scope_includes_super_admin_role_and_excludes_school_users(self):
        actor = make_vision_user(email="target-cx-scope@codex.test")
        _grant_platform(actor, ("platform.impersonation.start_cx",))
        super_target = make_vision_user(email="target-super@codex.test")
        super_target.role = "Super Admin"
        super_target.save(update_fields=["role"])
        resp = self.search(actor)
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertTrue(any(row["id"] == super_target.pk for row in rows))
        self.assertTrue(all(row["tenant_slug"] == actor.tenant.slug for row in rows))

    def test_search_excludes_actor_and_rejects_short_query(self):
        resp = self.search(self.admin, query="cxadmin")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(any(row["id"] == self.admin.pk for row in resp.json()["data"]))
        self.assertEqual(self.search(self.admin, query="x").status_code, 400)

    def test_search_matches_full_and_partial_cross_field_names(self):
        rashida = make_vision_user(email="rashida.sule@codex.test")
        rashida.first_name = "Rashida"
        rashida.last_name = "Sule"
        rashida.save(update_fields=["first_name", "last_name"])
        for query in (
            "Rashida Sule",
            "Ras Su",
            "Sule Ras",
            "Rashida",
            "Sule",
            "rashida.sule@",
        ):
            with self.subTest(query=query):
                resp = self.search(self.admin, query=query)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(any(row["id"] == rashida.pk for row in resp.json()["data"]))


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

    def test_successful_impersonated_read_does_not_create_request_noise(self):
        from vs_audit.models import AuditEvent

        before = AuditEvent.objects.filter(impersonation_session=self.session).count()
        resp = self.impersonated_client().get(
            f"/v1/user/auth/me/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            AuditEvent.objects.filter(impersonation_session=self.session).count(),
            before,
        )
        self.assertFalse(AuditEvent.objects.filter(action_type="IMPERSONATED_REQUEST").exists())

    def test_denied_impersonated_action_audits_both_identities(self):
        from vs_audit.models import AuditEvent

        resp = self.impersonated_client().get(
            f"/v1/admin/impersonations/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 403)
        event = AuditEvent.objects.get(
            impersonation_session=self.session,
            action_type="PROXY_ACTION_FAILED",
        )
        self.assertEqual(event.actor_user, self.admin)
        self.assertEqual(event.effective_user, self.target)
        self.assertEqual(event.status, "DENIED")

    def test_successful_reads_land_in_the_session_access_trail(self):
        url = f"/v1/user/auth/me/?tenant={self.school.tenant.slug}"
        self.assertEqual(self.impersonated_client().get(url).status_code, 200)
        self.assertEqual(self.impersonated_client().get(url).status_code, 200)

        self.session.refresh_from_db()
        self.assertEqual(len(self.session.access_log), 1)
        entry = self.session.access_log[0]
        self.assertEqual(entry["path"], "/v1/user/auth/me/")
        self.assertEqual(entry["count"], 2)

    def test_failed_request_updates_activity_but_not_the_trail(self):
        before = self.session.last_activity_at
        resp = self.impersonated_client().get(
            f"/v1/admin/impersonations/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 403)
        self.session.refresh_from_db()
        self.assertEqual(self.session.access_log, [])
        self.assertGreater(self.session.last_activity_at, before)

    def test_proxied_self_service_auth_event_is_attributed_to_the_real_actor(self):
        from vs_audit.models import AuditEvent

        resp = self.impersonated_client().post(
            f"/v1/user/sessions/end-all-mine/?tenant={self.school.tenant.slug}",
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        event = AuditEvent.objects.get(action_type="FORCE_LOGOUT")
        self.assertEqual(event.actor_user, self.admin)
        self.assertEqual(event.effective_user, self.target)
        self.assertEqual(event.impersonation_session, self.session)
        # The explicit auth event suppresses the generic PROXY_CHANGE fallback.
        self.assertFalse(AuditEvent.objects.filter(action_type="PROXY_CHANGE").exists())


class SchoolImpersonationTests(ImpersonationTestBase):
    """School-scoped proxy: an actor holding school.impersonation.* may proxy
    ANY active user in their OWN tenant (peers and fellow school admins
    included, self excluded). Cross-tenant reach stays impossible, and the
    platform.* tiering is untouched."""

    def setUp(self):
        super().setUp()
        self.slug = self.school.tenant.slug
        # A school admin in the same tenant as self.target.
        self.school_actor = make_school_admin(
            self.branch, email="principal@school.test",
        )
        _grant_school(self.school_actor, SCHOOL_IMPERSONATION_KEYS)

    def _start(self, actor=None, target=None, tenant_slug=None):
        return self.start_session(
            actor=actor or self.school_actor,
            target=target or self.target,
            tenant_slug=tenant_slug or self.slug,
        )

    def _proxied_client(self, session, actor=None):
        actor = actor or self.school_actor
        client = self.client_for(actor)
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(actor).access_token}",
            HTTP_X_IMPERSONATION_SESSION=str(session.pk),
        )
        return client

    # ── target search ────────────────────────────────────────────────────────

    def test_school_actor_searches_only_their_own_tenant(self):
        # A same-tenant peer is visible; a same-named user in another school and
        # a platform (CX) user must both be invisible.
        other_school = make_school(slug="imp-search-other", name="Other")
        foreign = make_school_admin(
            make_branch(other_school), email="target@other.test",
        )
        cx_user = make_vision_user(email="target-cx@codex.test")

        resp = self.client_for(self.school_actor).get(
            f"/v1/admin/impersonations/targets/?tenant={self.slug}&search=target"
        )
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.json()["data"]}
        self.assertIn(self.target.pk, ids)
        self.assertNotIn(foreign.pk, ids)
        self.assertNotIn(cx_user.pk, ids)

    def test_school_target_search_excludes_self(self):
        resp = self.client_for(self.school_actor).get(
            f"/v1/admin/impersonations/targets/?tenant={self.slug}&search=principal"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(
            self.school_actor.pk, {row["id"] for row in resp.json()["data"]},
        )

    def test_school_target_search_requires_the_start_key(self):
        viewer = make_school_admin(self.branch, email="viewer@school.test")
        _grant_school(viewer, ("school.impersonation.view",))
        resp = self.client_for(viewer).get(
            f"/v1/admin/impersonations/targets/?tenant={self.slug}&search=target"
        )
        self.assertEqual(resp.status_code, 403)

    # ── start ────────────────────────────────────────────────────────────────

    def test_school_actor_can_start_in_own_tenant(self):
        from vs_audit.models import AuditEvent

        resp = self._start()
        self.assertEqual(resp.status_code, 201)
        session = ImpersonationSession.objects.get()
        self.assertEqual(session.staff_user, self.school_actor)
        self.assertEqual(session.target_user, self.target)
        self.assertEqual(session.tenant, self.school.tenant)
        self.assertEqual(session.status, "ACTIVE")
        # The bookend is scoped to the school surface, not the platform stream.
        event = AuditEvent.objects.get(
            impersonation_session=session, action_type="IMPERSONATION_STARTED",
        )
        self.assertEqual(event.module_key, "SCHOOL")
        self.assertEqual(event.tenant, self.school.tenant)
        self.assertEqual(event.actor_user, self.school_actor)
        self.assertEqual(event.effective_user, self.target)

    def test_school_actor_may_impersonate_a_fellow_school_admin(self):
        # Eligibility is the permission key, not the target's seniority.
        peer_admin = make_school_admin(self.branch, email="peer-admin@school.test")
        self.assertEqual(self._start(target=peer_admin).status_code, 201)
        self.assertEqual(
            ImpersonationSession.objects.get().target_user, peer_admin,
        )

    def test_school_actor_cannot_impersonate_self(self):
        resp = self._start(target=self.school_actor)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_school_actor_cannot_target_a_foreign_user(self):
        other_school = make_school(slug="imp-foreign", name="Foreign")
        foreign = make_school_admin(
            make_branch(other_school), email="foreign-t@other.test",
        )
        resp = self._start(target=foreign)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_school_actor_cannot_target_a_platform_user(self):
        # The CX user is not in the actor's tenant, so it is a 404 - and the
        # actor could not assert the codex tenant either (covered below).
        resp = self._start(target=self.admin)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_school_actor_cannot_assert_the_platform_tenant(self):
        resp = self._start(target=self.admin, tenant_slug=self.codex.slug)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(ImpersonationSession.objects.exists())

    # ── proxying a request ───────────────────────────────────────────────────

    def test_school_session_proxies_a_request(self):
        self.assertEqual(self._start().status_code, 201)
        session = ImpersonationSession.objects.get()
        resp = self._proxied_client(session).get(
            f"/v1/user/auth/me/?tenant={self.slug}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["data"]["user"]["email"], self.target.email,
        )

    def test_school_proxy_cannot_assert_another_tenant(self):
        self.assertEqual(self._start().status_code, 201)
        session = ImpersonationSession.objects.get()
        resp = self._proxied_client(session).get(
            f"/v1/user/auth/me/?tenant={self.codex.slug}"
        )
        self.assertEqual(resp.status_code, 404)

    def test_school_actor_cannot_ride_a_foreign_tenants_session(self):
        # Forge the header with a session pinned to a DIFFERENT tenant that the
        # school actor happens to own: authentication must refuse it.
        other_school = make_school(slug="imp-ride", name="Ride")
        other_target = make_school_admin(
            make_branch(other_school), email="ride@other.test",
        )
        foreign_session = ImpersonationSession.objects.create(
            staff_user=self.school_actor,
            tenant=other_school.tenant,
            target_user=other_target,
            justification="Forged cross-tenant session.",
        )
        resp = self._proxied_client(foreign_session).get(
            f"/v1/user/auth/me/?tenant={other_school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 401)

    def test_school_proxy_evaluates_rbac_as_the_target(self):
        # The actor holds school.impersonation.view; the target does not, so the
        # proxied request must be denied - no union with the actor's grants.
        self.assertEqual(self._start().status_code, 201)
        session = ImpersonationSession.objects.get()
        resp = self._proxied_client(session).get(
            f"/v1/admin/impersonations/?tenant={self.slug}"
        )
        self.assertEqual(resp.status_code, 403)

    def test_school_session_expires_when_idle(self):
        self.assertEqual(self._start().status_code, 201)
        session = ImpersonationSession.objects.get()
        ImpersonationSession.objects.filter(pk=session.pk).update(
            last_activity_at=timezone.now() - timezone.timedelta(minutes=31),
        )
        resp = self._proxied_client(session).get(
            f"/v1/user/auth/me/?tenant={self.slug}"
        )
        self.assertEqual(resp.status_code, 401)
        session.refresh_from_db()
        self.assertEqual(session.status, "EXPIRED")

    # ── list / end / lifecycle ───────────────────────────────────────────────

    def test_school_actor_lists_only_their_own_tenants_sessions(self):
        self.assertEqual(self._start().status_code, 201)
        # A platform-run session in a different tenant must not leak.
        other_school = make_school(slug="imp-list-other", name="List Other")
        ImpersonationSession.objects.create(
            staff_user=self.admin,
            tenant=other_school.tenant,
            target_user=make_school_admin(
                make_branch(other_school), email="list-other@other.test",
            ),
            justification="Other tenant session.",
        )
        resp = self.client_for(self.school_actor).get(
            f"/v1/admin/impersonations/?tenant={self.slug}"
        )
        self.assertEqual(resp.status_code, 200)
        tenants = {row["tenant_slug"] for row in resp.json()["data"]}
        self.assertEqual(tenants, {self.slug})

    def test_school_list_requires_the_view_key(self):
        starter = make_school_admin(self.branch, email="startonly@school.test")
        _grant_school(starter, ("school.impersonation.start",))
        resp = self.client_for(starter).get(
            f"/v1/admin/impersonations/?tenant={self.slug}"
        )
        self.assertEqual(resp.status_code, 403)

    def test_school_actor_ends_own_session(self):
        from vs_audit.models import AuditEvent

        self.assertEqual(self._start().status_code, 201)
        session = ImpersonationSession.objects.get()
        resp = self.client_for(self.school_actor).post(
            f"/v1/admin/impersonations/end/?tenant={self.slug}",
            {"session_id": session.pk},
        )
        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, "ENDED")
        ended = AuditEvent.objects.get(
            impersonation_session=session, action_type="IMPERSONATION_ENDED",
        )
        self.assertEqual(ended.module_key, "SCHOOL")

    def test_school_start_key_alone_can_exit_own_session(self):
        starter = make_school_admin(self.branch, email="exit@school.test")
        _grant_school(starter, ("school.impersonation.start",))
        self.assertEqual(self._start(actor=starter).status_code, 201)
        session = ImpersonationSession.objects.get()
        resp = self.client_for(starter).post(
            f"/v1/admin/impersonations/end/?tenant={self.slug}",
            {"session_id": session.pk},
        )
        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, "ENDED")

    def test_school_end_key_terminates_another_actors_session_in_tenant(self):
        starter = make_school_admin(self.branch, email="starter2@school.test")
        _grant_school(starter, ("school.impersonation.start",))
        self.assertEqual(self._start(actor=starter).status_code, 201)
        session = ImpersonationSession.objects.get()
        # self.school_actor holds school.impersonation.end - the tenant kill switch.
        resp = self.client_for(self.school_actor).post(
            f"/v1/admin/impersonations/end/?tenant={self.slug}",
            {"session_id": session.pk},
        )
        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, "ENDED")

    def test_school_end_cannot_reach_another_tenants_session(self):
        # A platform-run session in a foreign tenant stays a non-enumerating 404
        # even though the actor holds the school kill-switch key.
        other_school = make_school(slug="imp-end-other", name="End Other")
        foreign_session = ImpersonationSession.objects.create(
            staff_user=self.admin,
            tenant=other_school.tenant,
            target_user=make_school_admin(
                make_branch(other_school), email="end-other@other.test",
            ),
            justification="Foreign tenant session.",
        )
        resp = self.client_for(self.school_actor).post(
            f"/v1/admin/impersonations/end/?tenant={self.slug}",
            {"session_id": foreign_session.pk},
        )
        self.assertEqual(resp.status_code, 404)
        foreign_session.refresh_from_db()
        self.assertEqual(foreign_session.status, "ACTIVE")

    def test_target_logout_ends_a_school_session(self):
        self.assertEqual(self._start().status_code, 201)
        end_impersonations_for_user(self.target)
        self.assertEqual(
            ImpersonationSession.objects.filter(status="ACTIVE").count(), 0,
        )

    def test_school_session_ends_when_tenant_is_deactivated(self):
        self.assertEqual(self._start().status_code, 201)
        end_impersonations_for_tenant(self.school.tenant)
        self.assertEqual(
            ImpersonationSession.objects.filter(status="ACTIVE").count(), 0,
        )


class ImpersonationIdleExpiryTests(ImpersonationTestBase):
    def _make_idle(self, session, minutes):
        ImpersonationSession.objects.filter(pk=session.pk).update(
            last_activity_at=timezone.now() - timezone.timedelta(minutes=minutes),
        )

    def _open_ended_session(self):
        resp = self.client_for(self.admin).post(
            f"/v1/admin/impersonations/start/?tenant={self.school.tenant.slug}",
            {"target_user": self.target.pk},
        )
        self.assertEqual(resp.status_code, 201)
        return ImpersonationSession.objects.get()

    def _impersonated_get(self, session):
        client = self.client_for(self.admin)
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(self.admin).access_token}",
            HTTP_X_IMPERSONATION_SESSION=str(session.pk),
        )
        return client.get(f"/v1/user/auth/me/?tenant={self.school.tenant.slug}")

    def test_idle_open_ended_session_is_rejected_and_expired(self):
        session = self._open_ended_session()
        self._make_idle(session, minutes=31)
        self.assertEqual(self._impersonated_get(session).status_code, 401)
        session.refresh_from_db()
        self.assertEqual(session.status, "EXPIRED")
        self.assertIsNotNone(session.ended_at)

    def test_recently_active_open_ended_session_still_works(self):
        session = self._open_ended_session()
        self._make_idle(session, minutes=29)
        self.assertEqual(self._impersonated_get(session).status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, "ACTIVE")

    def test_monitoring_list_sweeps_stale_sessions(self):
        idle_session = self._open_ended_session()
        self._make_idle(idle_session, minutes=45)
        # A bounded session past its deadline is swept by the same pass even
        # though its actor never sent another proxied request.
        overdue = ImpersonationSession.objects.create(
            staff_user=make_vision_user(email="overdue-owner@codex.test"),
            tenant=self.school.tenant,
            target_user=self.target,
            justification="Overdue bounded session.",
            ends_at=timezone.now() - timezone.timedelta(minutes=5),
        )
        resp = self.client_for(self.admin).get(
            f"/v1/admin/impersonations/?tenant={self.school.tenant.slug}"
        )
        self.assertEqual(resp.status_code, 200)
        idle_session.refresh_from_db()
        overdue.refresh_from_db()
        self.assertEqual(idle_session.status, "EXPIRED")
        self.assertEqual(overdue.status, "EXPIRED")
        statuses = {row["id"]: row["status"] for row in resp.json()["data"]}
        self.assertEqual(statuses.get(idle_session.pk), "EXPIRED")
        self.assertEqual(statuses.get(overdue.pk), "EXPIRED")


class ImpersonationLifecycleTests(ImpersonationTestBase):
    def _active(self):
        return ImpersonationSession.objects.filter(status="ACTIVE").count()

    def test_owner_end_emits_audit_bookend(self):
        from vs_audit.models import AuditEvent

        self.start_session()
        session = ImpersonationSession.objects.get()
        resp = self.client_for(self.admin).post(
            f"/v1/admin/impersonations/end/?tenant={self.codex.slug}",
            {"session_id": session.pk},
        )
        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, "ENDED")
        ended = AuditEvent.objects.get(
            impersonation_session=session, action_type="IMPERSONATION_ENDED",
        )
        self.assertEqual(ended.actor_user, self.admin)
        self.assertEqual(ended.effective_user, self.target)

    def test_end_key_holder_can_terminate_another_actors_session(self):
        from vs_audit.models import AuditEvent

        self.start_session()
        session = ImpersonationSession.objects.get()
        security_admin = make_vision_user(email="cxadmin3@codex.test")
        _grant_platform(security_admin, ("platform.impersonation.end",))
        resp = self.client_for(security_admin).post(
            f"/v1/admin/impersonations/end/?tenant={self.codex.slug}",
            {"session_id": session.pk},
        )
        self.assertEqual(resp.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, "ENDED")
        ended = AuditEvent.objects.get(
            impersonation_session=session, action_type="IMPERSONATION_ENDED",
        )
        self.assertEqual(ended.actor_user, security_admin)
        self.assertEqual(ended.effective_user, self.target)
        self.assertIn("terminated", ended.summary)

    def test_start_only_holder_cannot_end_another_actors_session(self):
        self.start_session()
        session = ImpersonationSession.objects.get()
        starter = make_vision_user(email="starter-no-end@codex.test")
        _grant_platform(starter, ("platform.impersonation.start_school",))
        resp = self.client_for(starter).post(
            f"/v1/admin/impersonations/end/?tenant={self.codex.slug}",
            {"session_id": session.pk},
        )
        self.assertEqual(resp.status_code, 404)
        session.refresh_from_db()
        self.assertEqual(session.status, "ACTIVE")

    def test_start_permission_can_exit_own_session_without_end_permission(self):
        actor = make_vision_user(email="starter-exit@codex.test")
        _grant_platform(actor, ("platform.impersonation.start_school",))
        self.assertEqual(self.start_session(actor=actor).status_code, 201)
        session = ImpersonationSession.objects.get()
        resp = self.client_for(actor).post(
            f"/v1/admin/impersonations/end/?tenant={actor.tenant.slug}",
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
