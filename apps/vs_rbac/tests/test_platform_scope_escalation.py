"""A school admin must not be able to hand anyone a platform-only key.

These tests drive the real endpoints - override create, role create, role
assign - and then call the platform-gated endpoint the granted key opens, so
the boundary is demonstrated rather than argued.

Before ``Permission.scope`` existed all of this worked: the override serializer
offered every active key, ``UserPermissionOverride.clean()`` checked only tenant
membership and a reason, and the role serializer wrote whatever keys the payload
named. A school admin could grant a colleague ``platform.audit.export`` and the
colleague could then read every CX audit export job on the platform - that
history has no tenant column, because only CX was ever meant to reach it.
"""
import itertools

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from urllib.parse import urlencode

from vs_rbac.evaluator import get_effective_permissions, resolve_users_with_permission
from vs_rbac.models import (
    Permission,
    PermissionScope,
    TenantRolePermission,
    UserPermissionOverride,
)
from vs_user.models import User
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    codex_tenant,
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


_counter = itertools.count(1)

OVERRIDE_MANAGE_KEY = "school.user_overrides.manage"
ROLE_CREATE_KEY = "school.roles.create"
ROLE_ASSIGN_KEY = "school.roles.assign"
PLATFORM_TARGET_KEY = "platform.schools.view"
TENANT_TARGET_KEY = "school.students.update"


def _grant(user, keys, tenant=None):
    tenant = tenant or user.tenant
    role = make_role(tenant, name=f"probe-role-{next(_counter)}")
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
    return User.objects.get(pk=user.pk)


def _demote(key):
    """Make *key* platform-scoped after the fact.

    The way to test the evaluator's backstop is to produce the row it exists
    for: one written while the key was tenant-safe (or before the column
    existed at all) and still sitting in the database now. Writing it through
    a test-only bypass would prove less - the guards would never have run.
    """
    Permission.objects.filter(key=key).update(scope=PermissionScope.PLATFORM)


class PlatformScopeEscalationTests(TestCase):
    """A school admin in tenant A tries to give a colleague platform reach."""

    def setUp(self):
        self.school = make_school(slug="probe-school", name="Riverbank School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.slug = self.tenant.slug

        self.attacker = make_school_admin(self.branch, email="probe-admin@test.com")
        _grant(
            self.attacker,
            [OVERRIDE_MANAGE_KEY, ROLE_CREATE_KEY, ROLE_ASSIGN_KEY],
        )
        self.colleague = make_staff_user(self.branch, email="probe-colleague@test.com")
        self.platform_key = make_permission(PLATFORM_TARGET_KEY)
        self.tenant_key = make_permission(TENANT_TARGET_KEY)

        # Another customer on the platform. The schools register is Codex's
        # own list of every school it sells to, and SchoolListView serves
        # School.objects.all() behind platform.schools.view - the boundary is
        # the key alone, as schools/vs_schools/export_datasets.py says out loud.
        self.rival = make_school(slug="rival-college", name="Rival College")
        make_branch(self.rival)

    def _override_url(self):
        return reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": self.slug, "user_id": self.colleague.pk},
        )

    def _create_override(self, key, mode="ALLOW"):
        return _client(self.attacker).post(
            _q(self._override_url(), self.slug),
            {"permission": key, "mode": mode, "reason": "Probing the boundary."},
            format="json",
        )

    # ── the registry declares the boundary ───────────────────────────────────
    def test_seeded_scopes_are_declared_not_inferred(self):
        self.assertEqual(self.platform_key.scope, PermissionScope.PLATFORM)
        self.assertEqual(self.tenant_key.scope, PermissionScope.TENANT)

    def test_the_namespace_is_not_the_audience(self):
        """Two ``platform.*`` families are tenant-holdable, and must stay so.

        This is why the discriminator is a declared column rather than a
        ``startswith("platform.")`` check. ``platform.team.create`` is how a
        school adds its own staff (``UserAccountViewSet`` fences the queryset to
        the caller's tenant), and ``platform.audit.export`` belongs to an audit
        officer inside a tenant - ``vs_audit``'s own suite builds one. Enforcing
        on the prefix would have locked both of them out.
        """
        for key in ("platform.team.create", "platform.audit.export"):
            self.assertEqual(
                make_permission(key).scope, PermissionScope.TENANT, key,
            )
        # ...while the rest of the module stays CX-only.
        for key in ("platform.audit.manage", "platform.team_overrides.manage"):
            self.assertEqual(
                make_permission(key).scope, PermissionScope.PLATFORM, key,
            )

    # ── path 1: personal permission override ─────────────────────────────────
    def test_override_cannot_grant_a_platform_key(self):
        response = self._create_override(PLATFORM_TARGET_KEY)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(
            UserPermissionOverride.objects.filter(
                user=self.colleague, permission=self.platform_key,
            ).exists()
        )

        colleague = _fresh(self.colleague)
        self.assertNotIn(
            PLATFORM_TARGET_KEY,
            get_effective_permissions(colleague, tenant=self.tenant),
        )
        register = _client(colleague).get(_q(reverse("school-list"), self.slug))
        self.assertEqual(register.status_code, 403)

    def test_override_may_still_deny_a_platform_key(self):
        """A DENY is not an escalation, so it stays legal."""
        response = self._create_override(PLATFORM_TARGET_KEY, mode="DENY")
        self.assertEqual(response.status_code, 201, response.data)

    def test_override_of_a_tenant_key_still_works(self):
        response = self._create_override(TENANT_TARGET_KEY)
        self.assertEqual(response.status_code, 201, response.data)
        colleague = _fresh(self.colleague)
        self.assertIn(
            TENANT_TARGET_KEY,
            get_effective_permissions(colleague, tenant=self.tenant),
        )

    def test_platform_actor_may_still_override_a_platform_key(self):
        """The rule is about the tenant the grant lands in, not the key's name."""
        codex = codex_tenant()
        cx_admin = make_vision_user(email="probe-cx-admin@codex.test")
        cx_target = make_vision_user(email="probe-cx-target@codex.test")
        _grant(cx_admin, ["platform.team_overrides.manage"], tenant=codex)

        url = reverse(
            "rbac-user-permission-override-list-create",
            kwargs={"tenant_slug": codex.slug, "user_id": cx_target.pk},
        )
        response = _client(cx_admin).post(
            _q(url, codex.slug),
            {
                "permission": PLATFORM_TARGET_KEY,
                "mode": "ALLOW",
                "reason": "CX colleague needs the export desk.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn(
            PLATFORM_TARGET_KEY,
            get_effective_permissions(_fresh(cx_target), tenant=codex),
        )

    # ── path 2: tenant role grant + assignment ───────────────────────────────
    def test_role_grant_cannot_carry_a_platform_key(self):
        role_url = reverse("rbac-role-list-create", kwargs={"tenant_slug": self.slug})
        created = _client(self.attacker).post(
            _q(role_url, self.slug),
            {
                "name": "Probe Auditors",
                "description": "Probing the platform boundary.",
                "permission_keys": [PLATFORM_TARGET_KEY],
            },
            format="json",
        )

        self.assertEqual(created.status_code, 400, created.data)
        self.assertFalse(
            TenantRolePermission.objects.filter(
                role__tenant=self.tenant, permission=self.platform_key,
            ).exists()
        )

    def test_role_grant_of_a_tenant_key_still_works(self):
        role_url = reverse("rbac-role-list-create", kwargs={"tenant_slug": self.slug})
        created = _client(self.attacker).post(
            _q(role_url, self.slug),
            {"name": "Probe Registrars", "permission_keys": [TENANT_TARGET_KEY]},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)

    # ── the model is the boundary, not the serializer ────────────────────────
    def test_model_refuses_the_write_however_it_is_reached(self):
        role = make_role(self.tenant, name="Direct ORM Role")

        with self.assertRaises(ValidationError):
            TenantRolePermission.objects.create(
                role=role, permission=self.platform_key, granted=True,
            )
        with self.assertRaises(ValidationError):
            TenantRolePermission.objects.bulk_create([
                TenantRolePermission(
                    role=role, permission=self.platform_key, granted=True,
                ),
            ])
        with self.assertRaises(ValidationError):
            UserPermissionOverride.objects.create(
                tenant=self.tenant, user=self.colleague,
                permission=self.platform_key, mode="ALLOW", reason="Direct.",
            )

    def test_an_explicit_deny_row_is_still_writable(self):
        role = make_role(self.tenant, name="Deny Role")
        TenantRolePermission.objects.create(
            role=role, permission=self.platform_key, granted=False,
        )

    # ── defence in depth: a row already in the database confers nothing ──────
    def test_evaluator_ignores_a_grant_that_predates_the_guard(self):
        key = TENANT_TARGET_KEY
        role = make_role(self.tenant, name="Legacy Role")
        make_role_permission(role, make_permission(key))
        make_assignment(self.tenant, self.colleague, role)
        _demote(key)

        colleague = _fresh(self.colleague)
        self.assertNotIn(
            key, get_effective_permissions(colleague, tenant=self.tenant),
        )
        # Routing must agree with the gate, or the same person would be
        # nominated as an approver for work they cannot actually do.
        self.assertNotIn(
            colleague,
            resolve_users_with_permission(self.tenant, None, key),
        )

    def test_evaluator_ignores_an_override_that_predates_the_guard(self):
        key = TENANT_TARGET_KEY
        self.assertEqual(self._create_override(key).status_code, 201)
        _demote(key)

        colleague = _fresh(self.colleague)
        self.assertNotIn(
            key, get_effective_permissions(colleague, tenant=self.tenant),
        )

    def test_assignment_refuses_a_role_carrying_a_stale_platform_grant(self):
        key = TENANT_TARGET_KEY
        role = make_role(self.tenant, name="Stale Role")
        make_role_permission(role, make_permission(key))
        _demote(key)

        with self.assertRaises(ValidationError):
            make_assignment(self.tenant, self.colleague, role)


class PlatformImpersonationNamespaceTests(TestCase):
    """The impersonation split is a second, independent guard - keep it proven."""

    def setUp(self):
        self.school = make_school(slug="imp-school", name="Lakeside School")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant
        self.actor = make_school_admin(self.branch, email="imp-actor@test.com")
        _grant(self.actor, ["school.impersonation.start"])

    def test_a_school_actor_is_gated_on_the_school_namespace(self):
        """Even holding every platform impersonation key would change nothing.

        ``ImpersonationSessionViewSet.get_permissions`` picks the namespace from
        the actor's own tenant, so a school actor is asked for
        ``school.impersonation.*`` and the platform keys are never consulted.
        That guard is deliberate and predates this work; the scope field now
        also stops those keys being granted in the first place.
        """
        for key in ("platform.impersonation.start_all", "platform.impersonation.view"):
            self.assertEqual(make_permission(key).scope, PermissionScope.PLATFORM)

        targets = _client(self.actor).get(
            _q(reverse("impersonations-targets"), self.tenant.slug, search="imp"),
        )
        # The school key it actually requires is held, so this succeeds and the
        # pool is the actor's own tenant - never another school's.
        self.assertEqual(targets.status_code, 200, targets.data)
        emails = {row["email"] for row in (targets.data.get("data") or [])}
        self.assertTrue(all(email.endswith("@test.com") for email in emails))
