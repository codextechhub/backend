"""The roles surface a school reaches before it goes live.

"Confirm Default Roles & RBAC" is the first step on the onboarding checklist,
and until now every ``vs_rbac`` view was closed to a PENDING tenant - so a
school could be asked to confirm roles it was refused sight of. The role list,
role detail and a new tenant permission catalogue are now on the pending
surface; DELETE deliberately is not.

Two questions this module keeps honest:

**What may a school see?** The catalogue is the vocabulary its roles are
written in, and it must never name a key only CodeX may hold - not because
holding one would work (the grant guard refuses it) but because a picker that
offers a box the save rejects is a picker that lies.

**What may it change?** Its own roles, inside its own tenant, and not the
seeded baseline the onboarding gate reads.
"""
from importlib import import_module

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.models import (
    Permission,
    PermissionScope,
    TenantRoleTemplate,
)
from vs_tenants.models import Tenant
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)


class OnboardingRolesSurfaceTests(TestCase):
    """A PENDING school running its own roles."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="bright-star", name="Bright Star")
        cls.branch = make_branch(cls.school, name="Main Branch")
        cls.tenant = cls.school.tenant
        Tenant.objects.filter(pk=cls.tenant.pk).update(
            status=Tenant.Status.PENDING,
        )
        cls.tenant.refresh_from_db()

        # The five keys the screen leans on, plus one the school must not be
        # able to reach through a role of its own.
        cls.view_perm = make_permission(
            "school.roles.view", scope=PermissionScope.TENANT,
        )
        cls.create_perm = make_permission(
            "school.roles.create", scope=PermissionScope.TENANT,
        )
        cls.update_perm = make_permission(
            "school.roles.update", scope=PermissionScope.TENANT,
        )
        cls.delete_perm = make_permission(
            "school.roles.delete", scope=PermissionScope.TENANT,
        )
        cls.grantable = make_permission(
            "school.students.view", scope=PermissionScope.TENANT,
        )
        cls.platform_only = make_permission(
            "platform.impersonation.start_school",
            scope=PermissionScope.PLATFORM,
        )

        cls.admin_role = make_role(
            cls.school, name="School Admin", key="school_admin",
        )
        for perm in (
            cls.view_perm, cls.create_perm, cls.update_perm, cls.delete_perm,
        ):
            make_role_permission(cls.admin_role, perm)
        cls.admin = make_school_admin(
            None, email="admin@bright-star.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.admin_role, branch=None)

        # A branch admin holds none of the school.roles.* keys, which is the
        # whole reason the checklist card hides its own button for them.
        cls.branch_role = make_role(
            cls.school, name="Branch Admin", key="branch_admin",
        )
        cls.branch_admin = make_school_admin(
            cls.branch, email="branch@bright-star.example.com",
        )
        make_assignment(
            cls.school, cls.branch_admin, cls.branch_role, branch=cls.branch,
        )

        cls.other = make_school(slug="green-field", name="Green Field")
        make_branch(cls.other, name="Main Branch")

    def _client(self, user):
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def _roles_url(self, tenant=None):
        return reverse(
            "rbac-role-list-create",
            kwargs={"tenant_slug": (tenant or self.tenant).slug},
        )

    def _role_url(self, key, tenant=None):
        return reverse(
            "rbac-role-detail",
            kwargs={"tenant_slug": (tenant or self.tenant).slug, "key": key},
        )

    def _catalogue_url(self, tenant=None):
        return reverse(
            "rbac-tenant-permission-catalogue",
            kwargs={"tenant_slug": (tenant or self.tenant).slug},
        )

    def _assert(self, tenant=None):
        return {"tenant": (tenant or self.tenant).slug}

    # ── The gap this opens ───────────────────────────────────────────────────

    def test_a_pending_school_can_read_its_own_roles(self):
        """The step is confirmable by the school it is asked of."""
        self.assertEqual(self.tenant.status, Tenant.Status.PENDING)

        listed = self._client(self.admin).get(self._roles_url(), self._assert())
        self.assertEqual(listed.status_code, 200, listed.data)

        detail = self._client(self.admin).get(
            self._role_url("school_admin"), self._assert(),
        )
        self.assertEqual(detail.status_code, 200, detail.data)

    def test_a_pending_school_can_add_a_role_of_its_own(self):
        response = self._client(self.admin).post(
            f"{self._roles_url()}?tenant={self.tenant.slug}",
            {"key": "assistant-bursar", "name": "Assistant Bursar"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            TenantRoleTemplate.objects.filter(
                tenant=self.tenant, key="assistant-bursar",
            ).exists(),
        )

    def test_a_pending_school_can_change_what_a_role_reaches(self):
        make_role(self.school, name="Assistant Bursar", key="assistant-bursar")

        response = self._client(self.admin).patch(
            f"{self._role_url('assistant-bursar')}?tenant={self.tenant.slug}",
            {"permission_keys": ["school.students.view"]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        granted = [
            row["permission"]
            for row in response.data["data"]["role_permissions"]
            if row["granted"]
        ]
        self.assertEqual(granted, ["school.students.view"])

    def test_deleting_a_role_stays_closed_until_go_live(self):
        """Onboarding asks a school to confirm the baseline, not to remove it.

        The gate that decides whether this step can close reads the seeded
        rows, so a school able to delete them during onboarding could clear its
        own checklist by emptying the thing being checked.
        """
        make_role(self.school, name="Assistant Bursar", key="assistant-bursar")

        response = self._client(self.admin).delete(
            f"{self._role_url('assistant-bursar')}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 403, getattr(response, "data", None))
        self.assertTrue(
            TenantRoleTemplate.objects.filter(
                tenant=self.tenant, key="assistant-bursar",
            ).exists(),
        )

    # ── The catalogue ────────────────────────────────────────────────────────

    def test_the_catalogue_never_names_a_platform_only_key(self):
        """The picker cannot offer a box that ticking would fail.

        Not a second rule invented for the screen: the same ``scope`` column
        the grant guard reads. Bright Star must not be shown
        ``platform.impersonation.start_school`` even as a disabled option,
        because its existence is not a school's business.
        """
        response = self._client(self.admin).get(
            self._catalogue_url(), self._assert(),
        )
        self.assertEqual(response.status_code, 200, response.data)

        offered = {
            entry["key"]
            for group in response.data["data"]
            for entry in group["permissions"]
        }
        self.assertIn("school.students.view", offered)
        self.assertNotIn("platform.impersonation.start_school", offered)

    def test_every_offered_permission_carries_a_readable_label(self):
        """A tickbox beside a raw dotted key is not a labelled tickbox."""
        response = self._client(self.admin).get(
            self._catalogue_url(), self._assert(),
        )
        for group in response.data["data"]:
            for entry in group["permissions"]:
                self.assertTrue(entry["label"], entry["key"])
                self.assertNotEqual(entry["label"], entry["key"], entry["key"])

    def test_the_catalogue_and_the_save_agree(self):
        """Anything the catalogue offers, a role may actually be given.

        The two are one queryset apart in the code; this is the assertion that
        they have not drifted.
        """
        make_role(self.school, name="Assistant Bursar", key="assistant-bursar")
        offered = [
            entry["key"]
            for group in self._client(self.admin)
            .get(self._catalogue_url(), self._assert())
            .data["data"]
            for entry in group["permissions"]
        ]

        response = self._client(self.admin).patch(
            f"{self._role_url('assistant-bursar')}?tenant={self.tenant.slug}",
            {"permission_keys": offered},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_platform_tenant_sees_the_whole_registry(self):
        """The narrowing is the tenant's, not the endpoint's.

        CodeX may hold both scopes, so filtering its own view would be a
        different lie in the other direction.
        """
        platform = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        platform_role = make_role(
            platform, name="Platform Admin", key="platform_admin",
        )
        make_role_permission(platform_role, self.view_perm)
        staff = make_school_admin(
            None, email="cx@codex.example.com", tenant=platform,
        )
        make_assignment(platform, staff, platform_role, branch=None)

        response = self._client(staff).get(
            reverse(
                "rbac-tenant-permission-catalogue",
                kwargs={"tenant_slug": platform.slug},
            ),
            {"tenant": platform.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        offered = {
            entry["key"]
            for group in response.data["data"]
            for entry in group["permissions"]
        }
        self.assertIn("platform.impersonation.start_school", offered)

    # ── Who, and whose ───────────────────────────────────────────────────────

    def test_a_branch_admin_holds_none_of_this(self):
        """Which is why the checklist card hides its button rather than 403s."""
        for url in (self._roles_url(), self._catalogue_url()):
            response = self._client(self.branch_admin).get(url, self._assert())
            self.assertEqual(response.status_code, 403, url)

    def test_another_schools_roles_are_not_reachable_by_slug(self):
        """404, not 403, so the path cannot be used to enumerate schools."""
        for url in (
            self._roles_url(self.other.tenant),
            self._catalogue_url(self.other.tenant),
        ):
            response = self._client(self.admin).get(url, self._assert())
            self.assertEqual(response.status_code, 404, url)

    def test_a_school_cannot_grant_itself_a_platform_key(self):
        """The guard that makes opening this surface safe, stated out loud.

        Bright Star's admin holds ``school.roles.update``, so she can edit her
        own roles. If that reached ``platform.impersonation.start_school``, she
        could grant herself the ability to act as users in other schools by
        editing a row in her own tenant.
        """
        make_role(self.school, name="Assistant Bursar", key="assistant-bursar")

        response = self._client(self.admin).patch(
            f"{self._role_url('assistant-bursar')}?tenant={self.tenant.slug}",
            {"permission_keys": ["platform.impersonation.start_school"]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(
            Permission.objects.filter(
                key="platform.impersonation.start_school",
                tenant_role_permissions__role__tenant=self.tenant,
            ).exists(),
        )


class PermissionCatalogueCapabilityTests(TestCase):
    """Which product each permission belongs to, and whether the school has it.

    Two vocabularies had never been joined: what a school BUYS
    (``vs_config.Capability``) and what a permission is FILED UNDER
    (``vs_rbac.PermissionModule``). ``vs_rbac.capability_map`` joins them, and
    these are the claims that join makes.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="bright-star", name="Bright Star")
        make_branch(cls.school, name="Main Branch")
        cls.tenant = cls.school.tenant

        cls.view_perm = make_permission(
            "school.roles.view", scope=PermissionScope.TENANT,
        )
        # One core permission, one governed per resource, one per module.
        make_permission("school.branches.view", scope=PermissionScope.TENANT)
        make_permission("school.students.view", scope=PermissionScope.TENANT)
        make_permission("finance.invoice.view", scope=PermissionScope.TENANT)
        make_permission("procurement.vendor.view", scope=PermissionScope.TENANT)

        role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(role, cls.view_perm)
        cls.admin = make_school_admin(
            None, email="admin@bright-star.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, role, branch=None)

    def _catalogue(self):
        token = str(CodeXRefreshToken.for_user(self.admin).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = client.get(
            reverse(
                "rbac-tenant-permission-catalogue",
                kwargs={"tenant_slug": self.tenant.slug},
            ),
            {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return {
            entry["key"]: entry
            for group in response.data["data"]
            for entry in group["permissions"]
        }

    def test_each_permission_says_which_product_it_belongs_to(self):
        rows = self._catalogue()
        # Core: every school has its own branches whatever it bought.
        self.assertIsNone(rows["school.branches.view"]["capability"])
        # Governed per resource: students are sold separately from the school.
        self.assertEqual(rows["school.students.view"]["capability"], "students")
        # Governed per module.
        self.assertEqual(rows["finance.invoice.view"]["capability"], "finance")
        self.assertEqual(
            rows["procurement.vendor.view"]["capability"], "procurement",
        )

    def test_a_school_with_nothing_switched_on_is_offered_everything(self):
        """"Not provisioned" and "not bought" are different facts.

        Entitlements are not granted at provisioning yet, so today every school
        answers no to every capability. Treating that as "bought nothing" would
        empty this screen for every school on the platform - so a tenant with no
        capability on at all is offered the lot, and flagged as nothing.
        """
        rows = self._catalogue()
        self.assertTrue(all(row["available"] for row in rows.values()))

    def test_once_a_school_has_a_module_the_others_are_flagged(self):
        """The flags become real the moment provisioning grants anything."""
        from vs_config.models import Capability, CapabilityEntitlement

        # Built here rather than read from the seeded catalogue: a test that
        # skips itself when the catalogue is absent proves nothing on the run
        # that matters, and the two capabilities this asserts on are named in
        # ``capability_map`` anyway.
        finance, _ = Capability.objects.get_or_create(
            key="finance",
            defaults={"label": "Finance", "kind": Capability.Kind.MODULE},
        )
        Capability.objects.get_or_create(
            key="procurement",
            defaults={"label": "Procurement", "kind": Capability.Kind.MODULE},
        )
        Capability.objects.get_or_create(
            key="students",
            defaults={"label": "Students", "kind": Capability.Kind.MODULE},
        )
        CapabilityEntitlement.objects.create(
            tenant=self.tenant,
            capability=finance,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
        )

        rows = self._catalogue()
        self.assertTrue(rows["finance.invoice.view"]["available"])
        self.assertFalse(rows["procurement.vendor.view"]["available"])
        # Core is never flagged: the school still runs its own branches.
        self.assertTrue(rows["school.branches.view"]["available"])
        # And students are separately sold, so they follow their own capability.
        self.assertFalse(rows["school.students.view"]["available"])


class ConfigIsPlatformOnlyTests(TestCase):
    """What a school is offered, after migration 0008.

    The roles screen was the first surface to show a school administrator the
    permission registry, and it showed her two things it should not have. These
    are both halves, including the half that turned out NOT to be a scope
    problem.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="bright-star", name="Bright Star")
        make_branch(cls.school, name="Main Branch")
        cls.tenant = cls.school.tenant

        view = make_permission("school.roles.view", scope=PermissionScope.TENANT)
        update = make_permission(
            "school.roles.update", scope=PermissionScope.TENANT,
        )
        role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(role, view)
        make_role_permission(role, update)
        cls.admin = make_school_admin(
            None, email="admin@bright-star.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, role, branch=None)

    def _client(self):
        token = str(CodeXRefreshToken.for_user(self.admin).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def test_the_migration_moves_config_off_the_tenant_surface(self):
        """CodeX decides what a school HAS; a school does not decide for itself.

        A tenant able to hold ``config.entitlement.manage`` is one row of
        enforcement away from granting itself the modules it has not paid for.

        The migration's own function is run here rather than the seeded state
        asserted, because a test database runs migrations and not seeders - so
        asserting on seeded rows would pass by being vacuous.
        """
        from django.apps import apps as registry

        from vs_rbac.models import Permission

        migration = import_module(
            "vs_rbac.migrations.0008_config_is_platform_only",
        )

        make_permission("config.entitlement.manage", scope=PermissionScope.TENANT)
        make_permission("config.value.view", scope=PermissionScope.TENANT)

        migration.forward(registry, None)

        self.assertFalse(
            Permission.objects.filter(module_id="config")
            .exclude(scope=PermissionScope.PLATFORM)
            .exists(),
            "a config permission is still holdable by a tenant",
        )

        # And it goes back cleanly, which is what makes it safe to deploy.
        migration.backward(registry, None)
        self.assertFalse(
            Permission.objects.filter(module_id="config")
            .exclude(scope=PermissionScope.TENANT)
            .exists(),
        )

    def test_a_school_is_not_offered_config_permissions(self):
        response = self._client().get(
            reverse(
                "rbac-tenant-permission-catalogue",
                kwargs={"tenant_slug": self.tenant.slug},
            ),
            {"tenant": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn(
            "config", {group["module"] for group in response.data["data"]},
        )

    def test_a_school_cannot_grant_itself_a_config_permission(self):
        """The listing is a courtesy; this is the rule."""
        make_permission("config.entitlement.manage", scope=PermissionScope.PLATFORM)
        make_role(self.school, name="Assistant Bursar", key="assistant-bursar")

        response = self._client().patch(
            reverse(
                "rbac-role-detail",
                kwargs={
                    "tenant_slug": self.tenant.slug,
                    "key": "assistant-bursar",
                },
            )
            + f"?tenant={self.tenant.slug}",
            {"permission_keys": ["config.entitlement.manage"]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_the_migration_leaves_the_staff_keys_holdable_by_a_school(self):
        """Deliberately NOT reclassified, unlike config.

        ``vs_user.account_scope`` states the rule: a school administrator holds
        ``platform.team.suspend`` because that is how she suspends her own
        leavers, and the tenant boundary is drawn by ``administrable_users``
        rather than by the key. Reclassifying these would have locked a school
        out of administering its own staff, which is why the fix for that family
        was the caption instead.
        """
        from django.apps import apps as registry

        from vs_rbac.models import Permission

        migration = import_module(
            "vs_rbac.migrations.0008_config_is_platform_only",
        )
        made = make_permission(
            "platform.team.create", scope=PermissionScope.TENANT,
        )
        Permission.objects.filter(pk=made.pk).update(
            description="Invite new Vision team members",
        )

        migration.forward(registry, None)

        made.refresh_from_db()
        self.assertEqual(made.scope, PermissionScope.TENANT)
        # The real defect in that family: a school admin choosing what her
        # bursar may do was reading a caption about CodeX's own staff console.
        self.assertNotIn("Vision", made.description)
        self.assertEqual(made.description, "Invite new staff members")


class GlobalTableWritesAreNotTenantHoldableTests(TestCase):
    """A write key on a table with no tenant column belongs to CodEx alone.

    The registry has a handful of these - one row set shared by every school on
    the platform. A tenant able to hold a write key on one of them can change
    what every other school sees.

    The notification-template case was live, not theoretical. Its ViewSet scopes
    nothing and carries no platform guard, so a school that granted itself the
    key could read and rewrite the message templates every other school
    receives. It was reproduced against a running tenant before this was
    written: 55 templates listed, and a PATCH returned 200.
    """

    #: Reads on the same tables stay tenant-holdable: a school has to see the
    #: currency list and the template list in order to use either.
    STILL_TENANT = [
        "finance.currency.view",
        "finance.fxrate.view",
        "import.templates.view",
    ]

    def setUp(self):
        self.school = make_school(slug="bright-star", name="Bright Star")
        make_branch(self.school, name="Main Branch")
        self.role = make_role(self.school, name="School Admin", key="school_admin")

    def test_the_migration_moves_every_global_write_key(self):
        from django.apps import apps as registry

        from vs_rbac.models import Permission

        migration = import_module(
            "vs_rbac.migrations.0008_config_is_platform_only",
        )
        for key in migration.GLOBAL_WRITE_KEYS + self.STILL_TENANT:
            make_permission(key, scope=PermissionScope.TENANT)

        migration.forward(registry, None)

        for key in migration.GLOBAL_WRITE_KEYS:
            self.assertEqual(
                Permission.objects.get(key=key).scope,
                PermissionScope.PLATFORM,
                f"{key} writes a global table and must not be tenant-holdable",
            )
        for key in self.STILL_TENANT:
            self.assertEqual(
                Permission.objects.get(key=key).scope,
                PermissionScope.TENANT,
                f"{key} is a read a school genuinely needs",
            )

    def test_a_school_role_cannot_hold_a_global_write_key(self):
        """Refused at the grant model, which every path runs through.

        Not at the serializer and not in the view: overrides, group
        attachments, prebuilt defaults and role assignments all reach the same
        guard, so there is one place this can be got wrong rather than five.
        """
        from django.core.exceptions import ValidationError

        from vs_rbac.models import TenantRolePermission

        permission = make_permission(
            "communication.notification_templates.configure",
            scope=PermissionScope.PLATFORM,
        )
        with self.assertRaises(ValidationError):
            TenantRolePermission.objects.create(
                role=self.role, permission=permission, granted=True,
            )

    def test_the_platform_may_still_hold_them(self):
        """The narrowing is the tenant's; CodeX authors these for everybody."""
        from vs_rbac.models import TenantRolePermission

        platform = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
        cx_role = make_role(platform, name="Platform Admin", key="platform_admin")
        permission = make_permission(
            "communication.notification_templates.configure",
            scope=PermissionScope.PLATFORM,
        )
        row = TenantRolePermission.objects.create(
            role=cx_role, permission=permission, granted=True,
        )
        self.assertTrue(row.pk)
