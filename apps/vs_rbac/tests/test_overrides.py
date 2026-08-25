"""Tests for per-user permission overrides (UserPermissionOverride).

Covers the evaluator precedence rules, the tenant-scoped API (authz,
cross-tenant isolation, instant effect, lift, replace), the audit trail, the
self-override ban, and - the security crux - the self-visibility rule: an
affected user must never be able to learn they have overrides unless they
themselves hold the ``.view``/``.manage`` key.
"""
import itertools
from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.evaluator import get_effective_permissions, has_permission
from vs_rbac.models import RBACAuditLog, UserPermissionOverride
from vs_user.models import User
from vs_user.tokens import CodeXRefreshToken
from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
    make_vision_user,
)


_grant_counter = itertools.count(1)

MANAGE_KEY = "school.user_overrides.manage"
VIEW_KEY = "school.user_overrides.view"
TARGET_KEY = "school.students.update"


def _grant(user, keys, tenant=None):
    """Grant *user* a fresh tenant role carrying *keys* on their tenant."""
    tenant = tenant or user.tenant
    role = make_role(tenant, name=f"ovr-role-{next(_grant_counter)}")
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(tenant, user, role)
    return role


def _client(user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(user).access_token}",
    )
    return client


def _q(url, tenant_slug, **params):
    return f"{url}?{urlencode({'tenant': tenant_slug, **params})}"


def _fresh(user):
    """Re-fetch the user so the per-request evaluator cache is dropped."""
    return User.objects.get(pk=user.pk)


def _override(user, key, mode, **kwargs):
    defaults = {"reason": "Because tests.", "tenant": user.tenant}
    defaults.update(kwargs)
    return UserPermissionOverride.objects.create(
        user=user, permission=make_permission(key), mode=mode, **defaults,
    )


class UserPermissionOverrideTests(TestCase):
    def setUp(self):
        # NB: the school name must not contain any word the self-visibility
        # test scans the /me payload for ("override", "exception", ...).
        self.school = make_school(slug="ovr-school", name="Riverbank School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.slug = self.tenant.slug

        # Actor: holds the manage key. Target: holds the role grant under test.
        self.actor = make_school_admin(self.branch, email="ovr-actor@test.com")
        self.target = make_staff_user(self.branch, email="ovr-target@test.com")
        _grant(self.actor, [MANAGE_KEY])
        _grant(self.target, [TARGET_KEY])

        self.list_url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": self.slug, "user_id": self.target.pk},
        )

    def _detail_url(self, override_id, user_id=None):
        return reverse(
            "rbac-user-permission-override-detail",
            kwargs={
                "tenant_slug": self.slug,
                "user_id": user_id or self.target.pk,
                "id": override_id,
            },
        )

    # =========================================================================
    # Evaluator precedence
    # =========================================================================
    def test_deny_override_beats_role_grant(self):
        self.assertTrue(has_permission(_fresh(self.target), TARGET_KEY, tenant=self.tenant))
        _override(self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        self.assertFalse(has_permission(_fresh(self.target), TARGET_KEY, tenant=self.tenant))

    def test_allow_override_adds_a_key_no_role_grants(self):
        extra = "school.fees.view"
        self.assertFalse(has_permission(_fresh(self.target), extra, tenant=self.tenant))
        _override(self.target, extra, UserPermissionOverride.Mode.ALLOW)
        self.assertTrue(has_permission(_fresh(self.target), extra, tenant=self.tenant))

    def test_legacy_restricted_allow_is_ignored(self):
        key = "payments.payout.create"
        permission = make_permission(key)
        _override(self.target, key, UserPermissionOverride.Mode.ALLOW)
        permission.is_restricted = True
        permission.save(update_fields=["is_restricted", "updated_at"])

        self.assertNotIn(
            key, get_effective_permissions(_fresh(self.target), tenant=self.tenant),
        )

    def test_deny_beats_allow_for_the_same_key(self):
        # The unique constraint makes an ALLOW+DENY pair impossible to persist,
        # so the ordering is asserted at the evaluator boundary directly:
        # denies are subtracted last, so they win.
        with mock.patch(
            "vs_rbac.evaluator.get_user_override_keys",
            return_value=({TARGET_KEY}, {TARGET_KEY}),
        ):
            self.assertNotIn(
                TARGET_KEY, get_effective_permissions(_fresh(self.target), tenant=self.tenant),
            )

    def test_one_override_per_user_per_key(self):
        _override(self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _override(self.target, TARGET_KEY, UserPermissionOverride.Mode.ALLOW)

    def test_expired_override_has_no_effect(self):
        _override(
            self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        # Lazy expiry: the row is still there, it just stops matching.
        self.assertTrue(has_permission(_fresh(self.target), TARGET_KEY, tenant=self.tenant))
        self.assertEqual(UserPermissionOverride.objects.count(), 1)

    def test_unexpired_override_applies(self):
        _override(
            self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.assertFalse(has_permission(_fresh(self.target), TARGET_KEY, tenant=self.tenant))

    def test_effective_permissions_cached_within_a_request(self):
        user = _fresh(self.target)
        first = get_effective_permissions(user, tenant=self.tenant)
        self.assertIn(TARGET_KEY, first)
        _override(self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        # Same in-request user instance → served from the per-request cache, so
        # override evaluation costs one query per request, not per check.
        with self.assertNumQueries(0):
            self.assertIn(TARGET_KEY, get_effective_permissions(user, tenant=self.tenant))
        # A new request (fresh instance) sees the override.
        self.assertNotIn(
            TARGET_KEY, get_effective_permissions(_fresh(self.target), tenant=self.tenant),
        )

    def test_override_costs_one_extra_query_per_evaluation(self):
        user = _fresh(self.target)
        with self.assertNumQueries(4):
            get_effective_permissions(user, tenant=self.tenant)

    def test_cross_tenant_override_row_is_ignored(self):
        other = make_school(slug="ovr-other", name="Other School")
        # A row pinned to another tenant never reaches this tenant's evaluation.
        UserPermissionOverride.objects.create(
            tenant=other.tenant, user=self.target,
            permission=make_permission(TARGET_KEY),
            mode=UserPermissionOverride.Mode.DENY, reason="Wrong tenant.",
        )
        self.assertTrue(has_permission(_fresh(self.target), TARGET_KEY, tenant=self.tenant))

    def test_clean_rejects_user_from_another_tenant(self):
        from django.core.exceptions import ValidationError

        other = make_school(slug="ovr-clean-other", name="Clean Other")
        row = UserPermissionOverride(
            tenant=other.tenant, user=self.target,
            permission=make_permission(TARGET_KEY),
            mode=UserPermissionOverride.Mode.DENY, reason="Mismatch.",
        )
        with self.assertRaises(ValidationError):
            row.clean()

    def test_resolve_users_with_permission_honours_overrides(self):
        from vs_rbac.evaluator import resolve_users_with_permission

        emails = lambda: set(  # noqa: E731
            resolve_users_with_permission(self.tenant, None, TARGET_KEY)
            .values_list("email", flat=True)
        )
        self.assertIn(self.target.email, emails())
        _override(self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        self.assertNotIn(self.target.email, emails())
        _override(self.actor, TARGET_KEY, UserPermissionOverride.Mode.ALLOW)
        self.assertIn(self.actor.email, emails())

    # =========================================================================
    # API - authorisation
    # =========================================================================
    def test_list_requires_the_view_or_manage_key(self):
        stranger = make_staff_user(self.branch, email="ovr-stranger@test.com")
        resp = _client(stranger).get(_q(self.list_url, self.slug))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_view_key_alone_allows_listing_but_not_creating(self):
        viewer = make_staff_user(self.branch, email="ovr-viewer@test.com")
        _grant(viewer, [VIEW_KEY])
        client = _client(viewer)
        self.assertEqual(
            client.get(_q(self.list_url, self.slug)).status_code, status.HTTP_200_OK,
        )
        resp = client.post(
            _q(self.list_url, self.slug),
            {"permission": TARGET_KEY, "mode": "DENY", "reason": "No."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_requires_the_manage_key(self):
        viewer = make_staff_user(self.branch, email="ovr-viewer2@test.com")
        _grant(viewer, [VIEW_KEY])
        resp = _client(viewer).post(
            _q(self.list_url, self.slug),
            {"permission": TARGET_KEY, "mode": "DENY", "reason": "No."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(UserPermissionOverride.objects.exists())

    def test_cross_tenant_target_user_is_404(self):
        other_school = make_school(slug="ovr-x-school", name="X School")
        other_user = make_staff_user(
            make_branch(other_school), email="ovr-x-user@test.com",
        )
        url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": self.slug, "user_id": other_user.pk},
        )
        resp = _client(self.actor).get(_q(url, self.slug))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_url_tenant_must_match_the_asserted_tenant(self):
        other_school = make_school(slug="ovr-y-school", name="Y School")
        url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": other_school.tenant.slug, "user_id": self.target.pk},
        )
        resp = _client(self.actor).get(_q(url, self.slug))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_school_actor_cannot_assert_another_tenant(self):
        other_school = make_school(slug="ovr-z-school", name="Z School")
        other_user = make_staff_user(
            make_branch(other_school), email="ovr-z-user@test.com",
        )
        url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": other_school.tenant.slug, "user_id": other_user.pk},
        )
        resp = _client(self.actor).get(_q(url, other_school.tenant.slug))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # =========================================================================
    # API - behaviour
    # =========================================================================
    def _create(self, client=None, **payload):
        body = {"permission": TARGET_KEY, "mode": "DENY", "reason": "Under review."}
        body.update(payload)
        return (client or _client(self.actor)).post(
            _q(self.list_url, self.slug), body, format="json",
        )

    def test_deny_applies_instantly_and_shows_in_the_targets_me(self):
        resp = self._create()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["data"]["mode"], "DENY")
        self.assertTrue(resp.json()["data"]["granted_by_role"])

        me = _client(self.target).get(_q(reverse("auth-me"), self.slug))
        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertNotIn(TARGET_KEY, me.json()["data"]["permissions"])

    def test_allow_applies_instantly(self):
        extra = "school.fees.view"
        make_permission(extra)
        resp = self._create(permission=extra, mode="ALLOW", reason="Temporary cover.")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertFalse(resp.json()["data"]["granted_by_role"])

        me = _client(self.target).get(_q(reverse("auth-me"), self.slug))
        self.assertIn(extra, me.json()["data"]["permissions"])

    def test_restricted_allow_override_is_rejected(self):
        key = "payments.payout.create"
        make_permission(
            key, is_restricted=True,
            sensitivity_level="CRITICAL",
        )

        resp = self._create(
            permission=key, mode="ALLOW", reason="Temporary payout cover.",
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(UserPermissionOverride.objects.filter(permission_id=key).exists())

    def test_lift_restores_role_access(self):
        created = self._create().json()["data"]
        self.assertFalse(has_permission(_fresh(self.target), TARGET_KEY, tenant=self.tenant))

        resp = _client(self.actor).delete(_q(self._detail_url(created["id"]), self.slug))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(UserPermissionOverride.objects.exists())
        self.assertTrue(has_permission(_fresh(self.target), TARGET_KEY, tenant=self.tenant))

    def test_new_override_replaces_rather_than_stacks(self):
        self._create(mode="DENY")
        resp = self._create(mode="ALLOW", reason="Reinstated with an expiry.")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        rows = UserPermissionOverride.objects.filter(user=self.target)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().mode, "ALLOW")

    def test_list_returns_rows_with_role_context(self):
        self._create()
        resp = _client(self.actor).get(_q(self.list_url, self.slug))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.json()["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["permission_key"], TARGET_KEY)
        self.assertTrue(rows[0]["granted_by_role"])
        self.assertFalse(rows[0]["is_expired"])
        self.assertEqual(rows[0]["created_by_id"], str(self.actor.pk))

    def test_empty_list_returns_an_empty_array(self):
        resp = _client(self.actor).get(_q(self.list_url, self.slug))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"], [])

    def test_reason_is_required(self):
        resp = self._create(reason="   ")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_past_expiry_is_rejected(self):
        resp = self._create(expires_at=(timezone.now() - timedelta(days=1)).isoformat())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_permission_key_is_rejected(self):
        resp = self._create(permission="school.nope.nope")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_tenant_and_user_cannot_be_mass_assigned(self):
        other = make_school(slug="ovr-mass", name="Mass School")
        victim = make_staff_user(make_branch(other), email="ovr-victim@test.com")
        resp = self._create(user=victim.pk, tenant=other.tenant.pk)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        row = UserPermissionOverride.objects.get()
        self.assertEqual(row.user_id, self.target.pk)
        self.assertEqual(row.tenant_id, self.tenant.pk)

    # =========================================================================
    # Self-override ban
    # =========================================================================
    def test_cannot_create_an_override_on_yourself(self):
        url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": self.slug, "user_id": self.actor.pk},
        )
        resp = _client(self.actor).post(
            _q(url, self.slug),
            {"permission": TARGET_KEY, "mode": "ALLOW", "reason": "Self escalation."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(UserPermissionOverride.objects.exists())

    def test_cannot_lift_an_override_on_yourself(self):
        row = _override(self.actor, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        resp = _client(self.actor).delete(
            _q(self._detail_url(row.pk, user_id=self.actor.pk), self.slug),
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(UserPermissionOverride.objects.filter(pk=row.pk).exists())

    # =========================================================================
    # Self-visibility - the security crux
    # =========================================================================
    def test_affected_user_without_the_view_key_cannot_list_their_own_overrides(self):
        _override(self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        resp = _client(self.target).get(_q(self.list_url, self.slug))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_me_payload_carries_no_override_signal(self):
        _override(self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        _override(self.target, "school.fees.view", UserPermissionOverride.Mode.ALLOW)

        resp = _client(self.target).get(_q(reverse("auth-me"), self.slug))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        payload = resp.json()["data"]

        # The permission list reflects the overrides (that is the whole point),
        # but nothing names, counts, or hints at them.
        self.assertNotIn(TARGET_KEY, payload["permissions"])
        self.assertIn("school.fees.view", payload["permissions"])

        blob = resp.content.decode().lower()
        for signal in ("override", "exception", "permission_overrides", "because tests"):
            self.assertNotIn(signal, blob)
        self.assertEqual(
            set(payload.keys()), {"user", "tenant", "school", "permissions"},
        )
        for field in payload["user"]:
            self.assertNotIn("override", field.lower())

    def test_holder_of_the_view_key_can_list_their_own_overrides(self):
        # The gate is the VIEWER's key, not "is this me" - a user who legitimately
        # holds .view sees their own row like any other.
        _grant(self.actor, [VIEW_KEY])
        _override(self.actor, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": self.slug, "user_id": self.actor.pk},
        )
        resp = _client(self.actor).get(_q(url, self.slug))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()["data"]), 1)

    # =========================================================================
    # Audit
    # =========================================================================
    def test_create_and_lift_write_audit_rows(self):
        created = self._create(expires_at=(timezone.now() + timedelta(days=2)).isoformat())
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.content)

        log = RBACAuditLog.objects.filter(action_type="OVERRIDE_CREATED").get()
        self.assertEqual(log.entity_type, "UserPermissionOverride")
        self.assertEqual(log.actor_id, self.actor.pk)
        self.assertEqual(log.metadata["permission_key"], TARGET_KEY)
        self.assertEqual(log.metadata["mode"], "DENY")
        self.assertEqual(log.metadata["reason"], "Under review.")
        self.assertEqual(log.metadata["target_user_id"], str(self.target.pk))
        self.assertIsNotNone(log.metadata["expires_at"])

        _client(self.actor).delete(
            _q(self._detail_url(created.json()["data"]["id"]), self.slug),
        )
        lifted = RBACAuditLog.objects.filter(action_type="OVERRIDE_LIFTED").get()
        self.assertEqual(lifted.metadata["permission_key"], TARGET_KEY)
        self.assertFalse(lifted.metadata["replaced"])

    def test_replacement_audits_both_the_lift_and_the_create(self):
        self._create(mode="DENY")
        self._create(mode="ALLOW", reason="Reinstated.")
        self.assertEqual(
            RBACAuditLog.objects.filter(action_type="OVERRIDE_CREATED").count(), 2,
        )
        lifted = RBACAuditLog.objects.filter(action_type="OVERRIDE_LIFTED").get()
        self.assertTrue(lifted.metadata["replaced"])

    # =========================================================================
    # Impersonation - the evaluator runs as the effective user
    # =========================================================================
    def test_proxied_me_reflects_the_targets_overrides(self):
        from vs_admin_console.models import ImpersonationSession

        _grant(self.actor, ["school.impersonation.start"])
        _override(self.target, TARGET_KEY, UserPermissionOverride.Mode.DENY)
        _override(self.target, "school.fees.view", UserPermissionOverride.Mode.ALLOW)

        session = ImpersonationSession.objects.create(
            staff_user=self.actor,
            tenant=self.tenant,
            target_user=self.target,
            justification="Support diagnosis.",
        )
        client = _client(self.actor)
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {CodeXRefreshToken.for_user(self.actor).access_token}",
            HTTP_X_IMPERSONATION_SESSION=str(session.pk),
        )
        resp = client.get(_q(reverse("auth-me"), self.slug))
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["user"]["email"], self.target.email)
        self.assertNotIn(TARGET_KEY, data["permissions"])
        self.assertIn("school.fees.view", data["permissions"])


class PlatformUserPermissionOverrideTests(TestCase):
    """CX-side namespace: platform.team_overrides.* gates CX team profiles."""

    def setUp(self):
        self.actor = make_vision_user(email="cx-actor@test.com")
        self.target = make_vision_user(email="cx-target@test.com")
        self.tenant = self.actor.tenant
        self.slug = self.tenant.slug
        _grant(self.actor, ["platform.team_overrides.manage"])
        make_permission("platform.team.view")
        make_permission(TARGET_KEY)
        self.url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": self.slug, "user_id": self.target.pk},
        )

    def test_platform_key_gates_cx_targets(self):
        resp = _client(self.actor).post(
            _q(self.url, self.slug),
            {"permission": "platform.team.view", "mode": "DENY", "reason": "Offboarding."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

    def test_school_key_does_not_grant_a_platform_actor_access(self):
        stranger = make_vision_user(email="cx-stranger@test.com")
        _grant(stranger, [MANAGE_KEY])  # school namespace - no reach here
        resp = _client(stranger).get(_q(self.url, self.slug))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_platform_actor_can_manage_a_school_users_overrides_cross_tenant(self):
        school = make_school(slug="cx-cross-school", name="Cross School")
        branch = make_branch(school)
        school_user = make_staff_user(branch, email="cx-cross-user@test.com")
        url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": school.tenant.slug, "user_id": school_user.pk},
        )
        resp = _client(self.actor).post(
            _q(url, school.tenant.slug),
            {"permission": TARGET_KEY, "mode": "DENY", "reason": "Escalated support."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        row = UserPermissionOverride.objects.get()
        self.assertEqual(row.tenant_id, school.tenant.pk)
        self.assertEqual(row.user_id, school_user.pk)
