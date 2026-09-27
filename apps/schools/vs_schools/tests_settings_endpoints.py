"""``/v1/i/me/settings/security/`` and ``/v1/i/me/settings/payroll-scope/``.

A school's own door to the two configuration values that are its to set. The
configuration console's endpoints are closed to every school, because every
``config.*`` key is platform-only, so these endpoints are gated on the school's
own ``school.settings.view`` / ``school.settings.update`` and bound to the
caller's tenant. The tests below lead with who is refused and whose values a
write can reach, then cover what the school can actually do.

Two live schools throughout, because a single-branch test proves nothing about
a multi-branch one: Bright Star runs Ikeja (main) and Lekki; Green Field runs
one branch.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase

from core.test_utils import TenantAPIClient
from vs_config.models import ConfigurationAuditEvent, ConfigurationDefinition
from vs_config.runtime_settings import resolve_security_settings
from vs_config.services.resolution import resolve_value, set_value
from vs_finance.models import EmployeeSalary, LedgerEntity
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
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

from .models import SchoolStatus

SECURITY_URL = "/v1/i/me/settings/security/"
PAYROLL_URL = "/v1/i/me/settings/payroll-scope/"


class _SchoolSettingsFixture(TestCase):
    """Two live schools, one pending, and the people who call these endpoints."""

    @classmethod
    def setUpTestData(cls):
        cls.view_perm = make_permission("school.settings.view")
        cls.update_perm = make_permission("school.settings.update")
        cls.profile_perm = make_permission("school.profile.view")

        # Bright Star: two branches.
        cls.bright = make_school(slug="bright-star", name="Bright Star")
        cls.ikeja = make_branch(cls.bright, name="Ikeja Branch")
        cls.lekki = make_branch(cls.bright, name="Lekki Branch", is_main=False)
        cls.bright_admin = cls._school_admin(cls.bright, cls.ikeja, "admin@bright.example.com")

        # A branch admin pinned to Ikeja: may read, may not write.
        branch_role = make_role(cls.bright, name="Branch Admin", key="branch_admin")
        make_role_permission(branch_role, cls.view_perm)
        cls.ikeja_admin = make_school_admin(cls.ikeja, email="ikeja@bright.example.com")
        make_assignment(cls.bright, cls.ikeja_admin, branch_role, branch=cls.ikeja)

        # A teacher: holds neither settings key.
        teacher_role = make_role(cls.bright, name="Teacher", key="teacher")
        make_role_permission(teacher_role, cls.profile_perm)
        cls.teacher = make_staff_user(cls.ikeja, email="teacher@bright.example.com")
        make_assignment(cls.bright, cls.teacher, teacher_role, branch=cls.ikeja)

        # Green Field: one branch.
        cls.green = make_school(slug="green-field", name="Green Field")
        cls.green_main = make_branch(cls.green, name="Main Branch")
        cls.green_admin = cls._school_admin(cls.green, cls.green_main, "admin@green.example.com")

        # Sunrise: not live yet.
        cls.sunrise = make_school(
            slug="sunrise", name="Sunrise", status=SchoolStatus.PENDING,
        )
        cls.sunrise_main = make_branch(cls.sunrise, name="Main Branch")
        cls.sunrise_admin = cls._school_admin(
            cls.sunrise, cls.sunrise_main, "admin@sunrise.example.com",
        )

    @classmethod
    def _school_admin(cls, school, branch, email):
        """A school admin posted school-wide and holding both settings keys."""
        role = make_role(school, name="School Admin", key="school_admin")
        make_role_permission(role, cls.view_perm)
        make_role_permission(role, cls.update_perm)
        user = make_school_admin(branch, email=email)
        make_assignment(school, user, role, branch=None)
        return user

    def client_for(self, user, tenant_slug=None):
        return TenantAPIClient(user=user, tenant_slug=tenant_slug)

    def patch(self, user, url, body, **query):
        client = self.client_for(user)
        suffix = "".join(f"&{key}={value}" for key, value in query.items())
        return client.patch(
            f"{url}?tenant={user.tenant.slug}{suffix}", body, format="json",
        )


class SchoolSecuritySettingsAccessTests(_SchoolSettingsFixture):
    """Who may reach the security form, and whose values a write can touch."""

    def test_a_teacher_is_refused_both_verbs(self):
        client = self.client_for(self.teacher)
        self.assertEqual(client.get(SECURITY_URL).status_code, 403)
        refused = self.patch(self.teacher, SECURITY_URL, {"failed_login_threshold": 4})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertFalse(
            resolve_security_settings(tenant=self.bright.tenant)["overrides"]["failed_login_threshold"]
        )

    def test_a_branch_admin_may_read_but_not_write(self):
        read = self.client_for(self.ikeja_admin).get(SECURITY_URL)
        self.assertEqual(read.status_code, 200, read.data)
        refused = self.patch(self.ikeja_admin, SECURITY_URL, {"failed_login_threshold": 4})
        self.assertEqual(refused.status_code, 403, refused.data)

    def test_a_branch_admin_cannot_read_a_branch_they_are_not_posted_to(self):
        """Pinned to Ikeja, Lekki is not theirs, and the answer is the same 404."""
        own = self.client_for(self.ikeja_admin).get(SECURITY_URL, {"branch": self.ikeja.pk})
        other = self.client_for(self.ikeja_admin).get(SECURITY_URL, {"branch": self.lekki.pk})
        self.assertEqual(own.status_code, 200, own.data)
        self.assertEqual(other.status_code, 404, other.data)

    def test_one_school_writing_never_moves_another_schools_values(self):
        before = resolve_security_settings(tenant=self.green.tenant)

        response = self.patch(self.bright_admin, SECURITY_URL, {
            "failed_login_threshold": 3, "account_lock_minutes": 60,
        })
        self.assertEqual(response.status_code, 200, response.data)

        self.assertEqual(resolve_security_settings(tenant=self.green.tenant), before)
        self.assertEqual(
            resolve_security_settings(tenant=self.bright.tenant)["settings"]["failed_login_threshold"],
            3,
        )

    def test_a_branch_of_another_school_is_a_404(self):
        response = self.patch(
            self.bright_admin, SECURITY_URL, {"failed_login_threshold": 3},
            branch=self.green_main.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)
        read = self.client_for(self.bright_admin).get(
            SECURITY_URL, {"branch": self.green_main.pk},
        )
        self.assertEqual(read.status_code, 404, read.data)
        self.assertFalse(
            ConfigurationAuditEvent.all_objects.filter(branch=self.green_main).exists()
        )

    def test_asserting_another_schools_tenant_is_a_404(self):
        client = self.client_for(self.bright_admin, tenant_slug=self.green.tenant.slug)
        self.assertEqual(client.get(SECURITY_URL).status_code, 404)

    def test_a_school_that_is_not_live_is_refused(self):
        response = self.client_for(self.sunrise_admin).get(SECURITY_URL)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")

    def test_a_platform_caller_acting_as_itself_cannot_write_the_platform_baseline(self):
        """The platform tenant has no school profile, so this door stays shut.

        Without the check, the scope resolver would read the platform tenant as
        the platform layer and a save here would move every school's baseline.
        """
        operator = make_vision_user(email="operator@codex.example.com", super_admin=True)
        response = self.patch(operator, SECURITY_URL, {"failed_login_threshold": 3})
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(
            resolve_security_settings()["settings"]["failed_login_threshold"], 5,
        )

    def test_the_console_endpoint_stays_closed_to_a_school_admin(self):
        """``config.*`` is platform-only, and holding the school keys changes nothing.

        The keys are built with the scope production gives them, and a school
        role cannot even be granted one: the grant itself is refused.
        """
        for key in ("config.security.view", "config.security.update"):
            permission = make_permission(key, scope=PermissionScope.PLATFORM)
            self.assertEqual(permission.scope, PermissionScope.PLATFORM)
            role = make_role(self.bright, name=f"Tries {key}")
            with self.assertRaises(DjangoValidationError):
                make_role_permission(role, permission)

        client = self.client_for(self.bright_admin)
        self.assertEqual(client.get("/v1/config/security-settings/").status_code, 403)
        refused = client.patch(
            f"/v1/config/security-settings/?tenant={self.bright.tenant.slug}",
            {"failed_login_threshold": 3}, format="json",
        )
        self.assertEqual(refused.status_code, 403, refused.data)


class SchoolSecuritySettingsBehaviourTests(_SchoolSettingsFixture):
    """What a school admin can do with the form once they are through the door."""

    def test_the_body_is_the_console_shape(self):
        response = self.client_for(self.bright_admin).get(SECURITY_URL)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["success"])
        self.assertEqual(
            response.data["data"], resolve_security_settings(tenant=self.bright.tenant),
        )
        self.assertEqual(
            set(response.data["data"]),
            {"settings", "configured", "sources", "source_scopes", "overrides",
             "compliance", "scope"},
        )
        self.assertEqual(response.data["data"]["scope"], {
            "type": "school", "tenant": str(self.bright.tenant.pk), "branch": None,
        })

    def test_a_school_can_tighten_and_it_is_audited(self):
        response = self.patch(self.bright_admin, SECURITY_URL, {
            "failed_login_threshold": 4, "reason": "Tighten sign-in",
        })
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["settings"]["failed_login_threshold"], 4)
        self.assertTrue(data["overrides"]["failed_login_threshold"])
        self.assertEqual(data["source_scopes"]["failed_login_threshold"], "school")

        event = ConfigurationAuditEvent.all_objects.get(
            action="config.value.updated", reason="Tighten sign-in",
        )
        self.assertEqual(event.tenant_id, self.bright.tenant.pk)
        self.assertEqual(event.actor, self.bright_admin)
        self.assertEqual(event.after_data, {"value": 4})

    def test_a_school_cannot_loosen_past_the_platform(self):
        """Five failed sign-ins is the platform ceiling; nine is refused on the field."""
        response = self.patch(self.bright_admin, SECURITY_URL, {"failed_login_threshold": 9})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("failed_login_threshold", response.data["error"]["detail"])
        self.assertFalse(
            resolve_security_settings(tenant=self.bright.tenant)["overrides"]["failed_login_threshold"]
        )

    def test_a_refused_field_saves_nothing_from_the_same_form(self):
        response = self.patch(self.bright_admin, SECURITY_URL, {
            "account_lock_minutes": 60, "failed_login_threshold": 9,
        })
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(
            resolve_security_settings(tenant=self.bright.tenant)["overrides"]["account_lock_minutes"]
        )

    def test_null_resets_to_the_platform_value(self):
        self.patch(self.bright_admin, SECURITY_URL, {"failed_login_threshold": 4})
        response = self.patch(self.bright_admin, SECURITY_URL, {"failed_login_threshold": None})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["settings"]["failed_login_threshold"], 5)
        self.assertFalse(data["overrides"]["failed_login_threshold"])
        self.assertTrue(
            ConfigurationAuditEvent.all_objects.filter(
                action="config.value.cleared", tenant=self.bright.tenant,
            ).exists()
        )

    def test_an_empty_form_is_refused(self):
        response = self.patch(self.bright_admin, SECURITY_URL, {"reason": "nothing"})
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_branch_override_on_a_multi_branch_school(self):
        """Lekki may be stricter than Bright Star, never looser, and Ikeja is untouched."""
        self.patch(self.bright_admin, SECURITY_URL, {"failed_login_threshold": 4})

        looser = self.patch(
            self.bright_admin, SECURITY_URL, {"failed_login_threshold": 5},
            branch=self.lekki.pk,
        )
        stricter = self.patch(
            self.bright_admin, SECURITY_URL, {"failed_login_threshold": 3},
            branch=self.lekki.pk,
        )
        self.assertEqual(looser.status_code, 400, looser.data)
        self.assertEqual(stricter.status_code, 200, stricter.data)
        data = stricter.data["data"]
        self.assertEqual(data["source_scopes"]["failed_login_threshold"], "branch")
        self.assertEqual(data["scope"]["branch"], str(self.lekki.pk))
        self.assertEqual(data["compliance"]["failed_login_threshold"]["parent_scope"], "school")

        ikeja = self.client_for(self.bright_admin).get(SECURITY_URL, {"branch": self.ikeja.pk})
        self.assertEqual(ikeja.data["data"]["settings"]["failed_login_threshold"], 4)
        school = self.client_for(self.bright_admin).get(SECURITY_URL)
        self.assertEqual(school.data["data"]["settings"]["failed_login_threshold"], 4)

    def test_a_single_branch_school_can_set_its_own_values(self):
        """Account lock is a minimum: longer is stricter, shorter is refused."""
        stricter = self.patch(self.green_admin, SECURITY_URL, {"account_lock_minutes": 30})
        looser = self.patch(self.green_admin, SECURITY_URL, {"account_lock_minutes": 10})
        self.assertEqual(stricter.status_code, 200, stricter.data)
        self.assertEqual(stricter.data["data"]["settings"]["account_lock_minutes"], 30)
        self.assertEqual(looser.status_code, 400, looser.data)
        self.assertIn("account_lock_minutes", looser.data["error"]["detail"])


class SchoolPayrollScopeTests(_SchoolSettingsFixture):
    """Central or per-branch payroll, chosen by the school, guarded by finance."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bright_books = LedgerEntity.objects.create(
            name="Bright Star Books", code="BSB", kind=LedgerEntity.Kind.TENANT,
            tenant=cls.bright.tenant,
        )
        cls.green_books = LedgerEntity.objects.create(
            name="Green Field Books", code="GFB", kind=LedgerEntity.Kind.TENANT,
            tenant=cls.green.tenant,
        )
        cls.definition = ConfigurationDefinition.objects.get(key="payroll.scope")

    def salary(self, entity, name, branch):
        return EmployeeSalary.objects.create(
            entity=entity, name=name, branch=branch, gross_amount=50_000_00,
        )

    def effective(self, school):
        return resolve_value(self.definition, tenant=school.tenant)[0]

    # -- security first -------------------------------------------------------- #

    def test_a_teacher_is_refused_both_verbs(self):
        self.assertEqual(self.client_for(self.teacher).get(PAYROLL_URL).status_code, 403)
        refused = self.patch(self.teacher, PAYROLL_URL, {"scope": "PER_BRANCH"})
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.effective(self.bright), "CENTRAL")

    def test_a_branch_admin_may_read_but_not_switch(self):
        self.assertEqual(self.client_for(self.ikeja_admin).get(PAYROLL_URL).status_code, 200)
        refused = self.patch(self.ikeja_admin, PAYROLL_URL, {"scope": "PER_BRANCH"})
        self.assertEqual(refused.status_code, 403, refused.data)

    def test_one_school_switching_never_moves_another(self):
        response = self.patch(self.bright_admin, PAYROLL_URL, {"scope": "PER_BRANCH"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.effective(self.bright), "PER_BRANCH")
        self.assertEqual(self.effective(self.green), "CENTRAL")

    def test_a_school_that_is_not_live_is_refused(self):
        response = self.patch(self.sunrise_admin, PAYROLL_URL, {"scope": "PER_BRANCH"})
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")

    # -- behaviour ------------------------------------------------------------- #

    def test_the_default_reads_central_with_its_options(self):
        response = self.client_for(self.bright_admin).get(PAYROLL_URL)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["scope"], "CENTRAL")
        self.assertEqual(data["source"], "default")
        self.assertEqual(
            [option["value"] for option in data["options"]], ["CENTRAL", "PER_BRANCH"],
        )
        for option in data["options"]:
            self.assertTrue(option["label"])
            self.assertTrue(option["description"])
            self.assertNotIn("campus", (option["label"] + option["description"]).lower())

    def test_a_multi_branch_school_switches_when_everyone_has_a_branch(self):
        self.salary(self.bright_books, "Ada Obi", self.ikeja)
        self.salary(self.bright_books, "Bola Lawal", self.lekki)

        response = self.patch(self.bright_admin, PAYROLL_URL, {
            "scope": "PER_BRANCH", "reason": "Branches pay their own",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["scope"], "PER_BRANCH")
        self.assertEqual(response.data["data"]["source"], "school")

        event = ConfigurationAuditEvent.all_objects.get(
            action="config.value.updated", reason="Branches pay their own",
        )
        self.assertEqual(event.tenant_id, self.bright.tenant.pk)
        self.assertEqual(event.actor, self.bright_admin)
        self.assertEqual(event.before_data, {"value": None})
        self.assertEqual(event.after_data, {"value": "PER_BRANCH"})

    def test_a_single_branch_school_can_switch_and_back(self):
        self.salary(self.green_books, "Chidi Eze", self.green_main)
        on = self.patch(self.green_admin, PAYROLL_URL, {"scope": "PER_BRANCH"})
        off = self.patch(self.green_admin, PAYROLL_URL, {"scope": "CENTRAL"})
        self.assertEqual(on.status_code, 200, on.data)
        self.assertEqual(off.status_code, 200, off.data)
        self.assertEqual(off.data["data"]["scope"], "CENTRAL")
        self.assertEqual(off.data["data"]["source"], "school")

    def test_the_guard_refusal_is_a_400_keyed_on_scope(self):
        """Two staff have no branch, so the switch is refused and names them."""
        self.salary(self.bright_books, "Ada Obi", self.ikeja)
        self.salary(self.bright_books, "Chioma Nwosu", None)
        self.salary(self.bright_books, "Tunde Bello", None)

        response = self.patch(self.bright_admin, PAYROLL_URL, {"scope": "PER_BRANCH"})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["error"]["code"], "INVALID_CONFIGURATION_VALUE")
        self.assertEqual(response.data["error"]["detail"], {"scope": [response.data["message"]]})
        self.assertIn("Chioma Nwosu, Tunde Bello", response.data["message"])
        self.assertNotIn("Ada Obi", response.data["message"])

        self.assertEqual(self.effective(self.bright), "CENTRAL")
        self.assertFalse(
            ConfigurationAuditEvent.all_objects.filter(tenant=self.bright.tenant).exists()
        )

    def test_an_unknown_scope_is_refused(self):
        response = self.patch(self.bright_admin, PAYROLL_URL, {"scope": "REGIONAL"})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("scope", response.data["error"]["detail"])

    def test_a_platform_value_reads_as_platform(self):
        """Not a value the definition allows today, but the read must still say so."""
        from vs_config.models import ConfigurationValue

        ConfigurationValue.all_objects.create(
            definition=self.definition, scope_key="platform", value="CENTRAL",
        )
        response = self.client_for(self.green_admin).get(PAYROLL_URL)
        self.assertEqual(response.data["data"]["source"], "platform")

    def test_the_console_write_and_this_read_agree(self):
        set_value(
            definition=self.definition, value="PER_BRANCH", actor=None,
            tenant=self.green.tenant, reason="console",
        )
        response = self.client_for(self.green_admin).get(PAYROLL_URL)
        self.assertEqual(response.data["data"]["scope"], "PER_BRANCH")
        self.assertEqual(response.data["data"]["source"], "school")
