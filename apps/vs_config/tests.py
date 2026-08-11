from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)

from .exceptions import CapabilityNotEntitled, InvalidConfigurationValue
from .models import (
    Capability,
    CapabilityDependency,
    CapabilityEntitlement,
    CapabilityOverride,
    ConfigurationAuditEvent,
    ConfigurationAuditExportJob,
    ConfigurationAuditSavedView,
    ConfigurationDefinition,
    ConfigurationValue,
)
from .services.capabilities import (
    bulk_effective_capabilities,
    effective_capability,
    set_entitlement,
    set_override,
)
from .services.audit_exports import execute_configuration_audit_export
from .services.resolution import resolve_value, set_value


class ConfigurationResolutionTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="config-school")
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school)
        self.actor = make_vision_user(email="config-actor@example.com")
        self.definition = ConfigurationDefinition.objects.create(
            key="display.timezone",
            label="Timezone",
            description="Display timezone.",
            value_type=ConfigurationDefinition.ValueType.STRING,
            default_value="UTC",
            allowed_scopes=["platform", "school", "branch"],
        )

    def test_resolution_uses_branch_tenant_platform_default_precedence(self):
        value, source = resolve_value(self.definition, tenant=self.tenant, branch=self.branch)
        self.assertEqual(value, "UTC")
        self.assertIsNone(source)

        platform = set_value(
            definition=self.definition, value="Africa/Accra", actor=self.actor
        )
        tenant = set_value(
            definition=self.definition, value="Africa/Lagos", actor=self.actor,
            tenant=self.tenant,
        )
        branch = set_value(
            definition=self.definition, value="Europe/London", actor=self.actor,
            tenant=self.tenant, branch=self.branch,
        )

        self.assertEqual(resolve_value(self.definition)[0], platform.value)
        self.assertEqual(resolve_value(self.definition, tenant=self.tenant)[0], tenant.value)
        value, source = resolve_value(self.definition, tenant=self.tenant, branch=self.branch)
        self.assertEqual(value, branch.value)
        self.assertEqual(source.scope_key, f"branch:{self.branch.pk}")

    def test_branch_scope_populates_tenant_and_rejects_mismatch(self):
        row = ConfigurationValue(
            definition=self.definition, branch=self.branch, value="Africa/Lagos"
        )
        row.save()
        self.assertEqual(row.tenant, self.tenant)
        self.assertEqual(row.scope_key, f"branch:{self.branch.pk}")

        other = make_school(slug="other-config-school")
        invalid = ConfigurationValue(
            definition=self.definition, tenant=other.tenant, branch=self.branch, value="UTC"
        )
        with self.assertRaises(ValidationError):
            invalid.save()

    def test_typed_values_are_enforced(self):
        integer_definition = ConfigurationDefinition.objects.create(
            key="security.retry_limit", label="Retry limit", description="Retry limit.",
            value_type=ConfigurationDefinition.ValueType.INTEGER,
            default_value=3, allowed_scopes=["platform"],
        )
        with self.assertRaises(InvalidConfigurationValue):
            set_value(definition=integer_definition, value="three", actor=self.actor)

    def test_min_max_rules_with_mismatched_types_fail_cleanly(self):
        decimal_definition = ConfigurationDefinition.objects.create(
            key="finance.rate_cap", label="Rate cap", description="Rate cap.",
            value_type=ConfigurationDefinition.ValueType.DECIMAL,
            validation_rules={"min": 1}, allowed_scopes=["platform"],
        )
        set_value(definition=decimal_definition, value="2.5", actor=self.actor)
        with self.assertRaises(InvalidConfigurationValue):
            set_value(definition=decimal_definition, value="0.5", actor=self.actor)

        string_definition = ConfigurationDefinition.objects.create(
            key="security.token_prefix", label="Token prefix", description="Prefix.",
            value_type=ConfigurationDefinition.ValueType.STRING,
            validation_rules={"min": 3}, allowed_scopes=["platform"],
        )
        with self.assertRaises(InvalidConfigurationValue):
            set_value(definition=string_definition, value="abc", actor=self.actor)

    def test_get_config_public_api(self):
        from .conf import get_config

        self.assertEqual(get_config("display.timezone"), "UTC")
        set_value(
            definition=self.definition, value="Africa/Lagos", actor=self.actor
        )
        self.assertEqual(get_config("display.timezone"), "Africa/Lagos")
        self.assertEqual(get_config("missing.key", default=7), 7)

    def test_secret_references_are_redacted_in_audit(self):
        secret = ConfigurationDefinition.objects.create(
            key="payments.secret", label="Payment secret", description="Secret reference.",
            value_type=ConfigurationDefinition.ValueType.SECRET_REFERENCE,
            sensitivity=ConfigurationDefinition.Sensitivity.SECRET_REFERENCE,
            allowed_scopes=["platform"],
        )
        set_value(
            definition=secret, value="env://PAYMENTS_SECRET", actor=self.actor
        )
        event = ConfigurationAuditEvent.objects.get(action="config.value.updated")
        self.assertEqual(event.after_data["value"], "[REDACTED]")
        self.assertNotIn("PAYMENTS_SECRET", str(event.after_data))

    def test_audit_events_cannot_be_changed_or_deleted(self):
        event = ConfigurationAuditEvent(action="test", target_type="Test", target_id="1")
        event.save()
        event.reason = "changed"
        with self.assertRaises(ValueError):
            event.save()
        with self.assertRaises(ValueError):
            event.delete()


class CapabilityEvaluationTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="capability-school")
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school)
        self.actor = make_vision_user(email="capability-actor@example.com")
        self.finance, _ = Capability.objects.update_or_create(
            key="finance",
            defaults={"label": "Finance", "requires_entitlement": True, "default_enabled": True},
        )

    def test_runtime_override_cannot_bypass_entitlement(self):
        self.assertFalse(effective_capability(self.finance, tenant=self.tenant))
        with self.assertRaises(CapabilityNotEntitled):
            set_override(
                capability=self.finance, state=CapabilityOverride.State.ENABLED,
                actor=self.actor, tenant=self.tenant,
            )

    def test_entitlement_and_most_specific_override_are_separate(self):
        set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE, actor=self.actor,
        )
        self.assertTrue(effective_capability(self.finance, tenant=self.tenant, branch=self.branch))
        set_override(
            capability=self.finance, state=CapabilityOverride.State.DISABLED,
            actor=self.actor, tenant=self.tenant, branch=self.branch,
        )
        self.assertTrue(effective_capability(self.finance, tenant=self.tenant))
        self.assertFalse(effective_capability(self.finance, tenant=self.tenant, branch=self.branch))

    def test_dependencies_must_be_effective(self):
        procurement, _ = Capability.objects.update_or_create(
            key="procurement",
            defaults={
                "label": "Procurement", "requires_entitlement": False,
                "default_enabled": True,
            },
        )
        CapabilityDependency.objects.get_or_create(
            capability=procurement, requires=self.finance
        )
        self.assertFalse(effective_capability(procurement, tenant=self.tenant))
        set_entitlement(
            capability=self.finance, tenant=self.tenant,
            state="GRANTED", source="MANUAL", actor=self.actor,
        )
        self.assertTrue(effective_capability(procurement, tenant=self.tenant))

    def test_dependency_cycles_are_rejected(self):
        second = Capability.objects.create(
            key="cycle-feature", label="Cycle feature", requires_entitlement=False
        )
        CapabilityDependency.objects.create(capability=second, requires=self.finance)
        with self.assertRaises(ValidationError):
            CapabilityDependency.objects.create(capability=self.finance, requires=second)

    def test_scheduled_entitlement_activates_and_expires_at_evaluation_time(self):
        now = timezone.now()
        set_entitlement(
            capability=self.finance,
            tenant=self.tenant,
            state="GRANTED",
            source="MANUAL",
            actor=self.actor,
            starts_at=now + timedelta(hours=1),
            ends_at=now + timedelta(days=2),
        )
        self.assertFalse(effective_capability(self.finance, tenant=self.tenant))

        row = CapabilityEntitlement.all_objects.get(
            capability=self.finance, tenant=self.tenant,
        )
        row.starts_at = now - timedelta(hours=1)
        row.save(update_fields=["starts_at", "scope_key"])
        self.assertTrue(effective_capability(self.finance, tenant=self.tenant))

        row.ends_at = now - timedelta(seconds=1)
        row.save(update_fields=["ends_at", "scope_key"])
        self.assertFalse(effective_capability(self.finance, tenant=self.tenant))

    def test_bulk_evaluation_matches_single_evaluation_with_bounded_queries(self):
        set_entitlement(
            capability=self.finance, tenant=self.tenant, state="GRANTED",
            source="MANUAL", actor=self.actor,
        )
        feature = Capability.objects.create(
            key="bulk-child", label="Bulk child", requires_entitlement=False,
            default_enabled=True,
        )
        CapabilityDependency.objects.create(capability=feature, requires=self.finance)

        with self.assertNumQueries(4):
            result = bulk_effective_capabilities(tenant=self.tenant, branch=self.branch)

        states = {item["key"]: item["enabled"] for item in result}
        self.assertEqual(states[self.finance.key], effective_capability(self.finance, tenant=self.tenant))
        self.assertEqual(states[feature.key], effective_capability(feature, tenant=self.tenant))


class ConfigurationAPISecurityTests(TestCase):
    def setUp(self):
        self.school = make_school(slug="api-config-school")
        self.branch = make_branch(self.school)
        self.admin = make_school_admin(
            self.branch, email="config-school-admin@example.com"
        )
        self.client = APIClient()

    def test_school_admin_user_type_does_not_bypass_rbac(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get("/v1/config/definitions/")
        self.assertEqual(response.status_code, 403)

    def test_school_permission_allows_read_but_not_platform_mutation(self):
        permission = make_permission("config.definition.view")
        role = make_role(self.school, name="Configuration Reader")
        make_role_permission(role, permission)
        make_assignment(self.school, self.admin, role)
        ConfigurationDefinition.objects.create(
            key="api.setting", label="API setting", description="API setting.",
            value_type="STRING", allowed_scopes=["school"],
        )
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/v1/config/definitions/").status_code, 200)
        self.assertEqual(
            self.client.post("/v1/config/definitions/", {
                "key": "forbidden.setting", "label": "Forbidden",
                "description": "Forbidden.", "value_type": "STRING",
                "allowed_scopes": ["platform"],
            }, format="json").status_code,
            403,
        )

    def test_platform_super_admin_can_create_definition(self):
        user = make_vision_user(
            email="config-super-admin@example.com", super_admin=True
        )
        self.client.force_authenticate(user)
        response = self.client.post("/v1/config/definitions/", {
            "key": "platform.setting", "label": "Platform setting",
            "description": "Platform setting.", "value_type": "BOOLEAN",
            "default_value": True, "allowed_scopes": ["platform", "school"],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_cross_tenant_branch_scope_returns_not_found(self):
        permission = make_permission("config.value.view")
        role = make_role(self.school, name="Value Reader")
        make_role_permission(role, permission)
        make_assignment(self.school, self.admin, role)
        # A branch under another tenant must never resolve for this caller: the
        # scope tenant comes from request.tenant, and branch validation rejects
        # any branch whose owning school belongs to a different tenant.
        other = make_school(slug="other-tenant-school")
        other_branch = make_branch(other)
        self.client.force_authenticate(self.admin)
        response = self.client.get(f"/v1/config/values/?branch={other_branch.pk}")
        self.assertEqual(response.status_code, 404)

    def test_tenant_scoped_value_write_lists_and_audits(self):
        role = make_role(self.school, name="Config Writer")
        for key in ("config.value.update", "config.value.view", "config.audit.view"):
            make_role_permission(role, make_permission(key))
        make_assignment(self.school, self.admin, role)
        ConfigurationDefinition.objects.create(
            key="ui.theme", label="Theme", description="Theme.",
            value_type="STRING", allowed_scopes=["school"],
        )
        self.client.force_authenticate(self.admin)

        # A school admin's write lands at their own tenant scope (resolved from
        # the request, not the payload) and the response exposes ``tenant``.
        post = self.client.post(
            "/v1/config/values/", {"key": "ui.theme", "value": "dark"}, format="json"
        )
        self.assertEqual(post.status_code, 201, post.data)
        self.assertEqual(post.data["data"]["tenant"], self.school.tenant_id)

        # List + audit endpoints must serialize cleanly under the tenant shape.
        listing = self.client.get("/v1/config/values/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["data"][0]["tenant"], self.school.tenant_id)
        audit = self.client.get("/v1/config/audit-events/")
        self.assertEqual(audit.status_code, 200)

    def test_unmapped_http_method_returns_405(self):
        user = make_vision_user(
            email="config-method-admin@example.com", super_admin=True
        )
        self.client.force_authenticate(user)
        response = self.client.put("/v1/config/values/", {}, format="json")
        self.assertEqual(response.status_code, 405)

    def test_capability_archive_is_idempotent(self):
        user = make_vision_user(
            email="config-archive-admin@example.com", super_admin=True
        )
        Capability.objects.create(
            key="archive-target", label="Archive target", requires_entitlement=False
        )
        self.client.force_authenticate(user)
        self.assertEqual(
            self.client.delete("/v1/config/capabilities/archive-target/").status_code, 200
        )
        self.assertEqual(
            self.client.delete("/v1/config/capabilities/archive-target/").status_code, 200
        )
        self.assertEqual(
            ConfigurationAuditEvent.all_objects.filter(
                action="config.capability.archived"
            ).count(),
            1,
        )

    def test_bulk_value_update_is_atomic(self):
        user = make_vision_user(
            email="config-bulk-admin@example.com", super_admin=True
        )
        first = ConfigurationDefinition.objects.create(
            key="bulk.first", label="First", description="First.",
            value_type="INTEGER", allowed_scopes=["platform"],
        )
        ConfigurationDefinition.objects.create(
            key="bulk.second", label="Second", description="Second.",
            value_type="INTEGER", allowed_scopes=["platform"],
        )
        self.client.force_authenticate(user)
        response = self.client.post("/v1/config/values/", {
            "values": [
                {"key": "bulk.first", "value": 1},
                {"key": "bulk.second", "value": "invalid"},
            ]
        }, format="json")
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ConfigurationValue.all_objects.filter(definition=first).exists())


class PlatformSettingsAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.platform_user = make_vision_user(
            email="platform-settings-admin@example.com", super_admin=True
        )

    @override_settings(PLATFORM_ISSUER={"name": "Environment CodeX", "email": "env@example.com"})
    def test_get_uses_environment_profile_and_product_onboarding_defaults(self):
        self.client.force_authenticate(self.platform_user)
        response = self.client.get("/v1/config/platform-settings/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["profile"]["name"], "Environment CodeX")
        self.assertEqual(response.data["data"]["sources"]["profile"]["name"], "environment")
        self.assertEqual(response.data["data"]["onboarding"]["currency"], "NGN")

    def test_authenticated_platform_request_defaults_to_home_tenant_without_query_param(self):
        login = self.client.post(
            "/v1/user/auth/login/",
            {"email": self.platform_user.email, "password": "testpass123"},
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.data)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access']}"
        )

        response = self.client.get("/v1/config/platform-settings/")

        self.assertEqual(response.status_code, 200, response.data)

    def test_school_user_cannot_read_platform_settings_even_with_value_permission(self):
        school = make_school(slug="platform-settings-school")
        branch = make_branch(school)
        admin = make_school_admin(branch, email="platform-settings-school@example.com")
        role = make_role(school, name="Settings Reader")
        make_role_permission(role, make_permission("config.value.view"))
        make_assignment(school, admin, role)
        self.client.force_authenticate(admin)

        response = self.client.get("/v1/config/platform-settings/")

        self.assertEqual(response.status_code, 403)

    def test_patch_saves_typed_values_and_records_audit(self):
        self.client.force_authenticate(self.platform_user)
        response = self.client.patch(
            "/v1/config/platform-settings/",
            {
                "profile": {"name": "CodeX Africa", "email": "billing@codex.example"},
                "onboarding": {"currency": "USD", "branch_country": "Ghana"},
                "reason": "Platform rollout update",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["profile"]["name"], "CodeX Africa")
        self.assertEqual(response.data["data"]["onboarding"]["currency"], "USD")
        self.assertEqual(
            ConfigurationValue.all_objects.filter(scope_key="platform").count(), 4
        )
        self.assertEqual(
            ConfigurationAuditEvent.all_objects.filter(
                action="config.value.updated", reason="Platform rollout update"
            ).count(),
            4,
        )

    @override_settings(PLATFORM_ISSUER={"name": "Environment CodeX"})
    def test_blank_optional_profile_value_clears_database_override(self):
        definition = ConfigurationDefinition.objects.get(key="platform.profile.tagline")
        set_value(
            definition=definition,
            value="Saved tagline",
            actor=self.platform_user,
        )
        self.client.force_authenticate(self.platform_user)

        response = self.client.patch(
            "/v1/config/platform-settings/",
            {"profile": {"tagline": ""}, "reason": "Use fallback"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(
            ConfigurationValue.all_objects.filter(definition=definition).exists()
        )
        self.assertTrue(
            ConfigurationAuditEvent.all_objects.filter(
                action="config.value.cleared", reason="Use fallback"
            ).exists()
        )

    def test_curated_definition_schema_and_lifecycle_are_product_owned(self):
        self.client.force_authenticate(self.platform_user)
        patch = self.client.patch(
            "/v1/config/definitions/platform.profile.name/",
            {"value_type": "BOOLEAN"},
            format="json",
        )
        archive = self.client.delete(
            "/v1/config/definitions/platform.profile.name/",
            format="json",
        )

        self.assertEqual(patch.status_code, 400, patch.data)
        self.assertEqual(archive.status_code, 400, archive.data)
        definition = ConfigurationDefinition.objects.get(key="platform.profile.name")
        self.assertEqual(definition.value_type, "STRING")
        self.assertTrue(definition.is_active)


class GenericValueResetAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_vision_user(email="config-reset@example.com", super_admin=True)
        self.definition = ConfigurationDefinition.objects.create(
            key="display.reset_example",
            label="Reset example",
            value_type="STRING",
            default_value="product default",
            allowed_scopes=["platform", "school"],
        )
        set_value(definition=self.definition, value="platform override", actor=self.user)
        self.client.force_authenticate(self.user)

    def test_delete_clears_only_the_selected_scope_and_reports_effective_value(self):
        response = self.client.delete(
            "/v1/config/values/display.reset_example/",
            {"reason": "Return to product default"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["cleared"])
        self.assertEqual(response.data["data"]["effective_value"], "product default")
        self.assertEqual(response.data["data"]["source"], "default")
        self.assertFalse(
            ConfigurationValue.all_objects.filter(definition=self.definition).exists()
        )

    def test_delete_is_idempotent(self):
        self.client.delete("/v1/config/values/display.reset_example/", format="json")
        response = self.client.delete(
            "/v1/config/values/display.reset_example/", format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["cleared"])


class RuntimeSettingsAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_vision_user(email="special-config@example.com")

    def grant(self, *permission_keys):
        role = make_role(self.user.tenant, name=f"Special config {len(permission_keys)}")
        for key in permission_keys:
            make_role_permission(role, make_permission(key))
        make_assignment(self.user.tenant, self.user, role)

    def test_security_requires_dedicated_permissions_and_no_approval(self):
        self.grant("config.security.view", "config.security.manage")
        self.client.force_authenticate(self.user)

        response = self.client.patch(
            "/v1/config/security-settings/",
            {"failed_login_threshold": 8, "reason": "Security baseline"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["settings"]["failed_login_threshold"], 8)
        self.assertEqual(response.data["data"]["sources"]["failed_login_threshold"], "database")
        self.assertTrue(
            ConfigurationAuditEvent.all_objects.filter(reason="Security baseline").exists()
        )

    def test_view_permission_cannot_save_security(self):
        self.grant("config.security.view")
        self.client.force_authenticate(self.user)

        self.assertEqual(self.client.get("/v1/config/security-settings/").status_code, 200)
        self.assertEqual(
            self.client.patch(
                "/v1/config/security-settings/",
                {"account_lock_minutes": 20},
                format="json",
            ).status_code,
            403,
        )

    def test_school_user_can_set_only_a_stricter_security_override(self):
        school = make_school(slug="runtime-settings-school")
        branch = make_branch(school)
        admin = make_school_admin(branch, email="runtime-settings-school@example.com")
        role = make_role(school, name="Runtime settings manager")
        for key in ("config.security.view", "config.security.manage"):
            make_role_permission(role, make_permission(key))
        make_assignment(school, admin, role)
        self.client.force_authenticate(admin)

        allowed = self.client.patch(
            "/v1/config/security-settings/",
            {"failed_login_threshold": 4, "reason": "Tighten school baseline"},
            format="json",
        )
        rejected = self.client.patch(
            "/v1/config/security-settings/",
            {"failed_login_threshold": 9},
            format="json",
        )

        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.assertEqual(allowed.data["data"]["settings"]["failed_login_threshold"], 4)
        self.assertTrue(allowed.data["data"]["overrides"]["failed_login_threshold"])
        self.assertEqual(rejected.status_code, 400, rejected.data)

    def test_branch_security_override_cannot_weaken_school_override(self):
        school = make_school(slug="runtime-branch-school")
        branch = make_branch(school)
        admin = make_school_admin(branch, email="runtime-branch@example.com")
        role = make_role(school, name="Branch security manager")
        for key in ("config.security.view", "config.security.manage"):
            make_role_permission(role, make_permission(key))
        make_assignment(school, admin, role)
        self.client.force_authenticate(admin)
        self.client.patch(
            "/v1/config/security-settings/",
            {"failed_login_threshold": 4},
            format="json",
        )

        rejected = self.client.patch(
            f"/v1/config/security-settings/?branch={branch.pk}",
            {"failed_login_threshold": 5},
            format="json",
        )
        allowed = self.client.patch(
            f"/v1/config/security-settings/?branch={branch.pk}",
            {"failed_login_threshold": 3},
            format="json",
        )

        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.assertEqual(allowed.data["data"]["source_scopes"]["failed_login_threshold"], "branch")

    def test_generic_value_permission_cannot_bypass_special_permission(self):
        self.grant("config.value.update")
        self.client.force_authenticate(self.user)

        response = self.client.post(
            "/v1/config/values/",
            {"key": "security.failed_login_threshold", "value": 9},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)

    @override_settings(
        DEFAULT_FROM_EMAIL="Deployment Sender <deploy@example.com>",
        EMAIL_HOST="smtp.example.com",
        EMAIL_HOST_USER="smtp-user",
        PAYSTACK_SECRET_KEY="secret",
        PAYSTACK_PUBLIC_KEY="public",
    )
    def test_integration_save_changes_runtime_sender_and_redacts_status(self):
        from core.mail import build_from_email

        self.grant("config.integration.view", "config.integration.manage")
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            "/v1/config/integration-settings/",
            {
                "email_sender_name": "Platform Mail",
                "email_sender_address": "mail@example.com",
                "email_max_retries": 5,
                "reason": "Mail operations update",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(build_from_email(), "Platform Mail <mail@example.com>")
        status_data = response.data["data"]["status"]
        self.assertTrue(status_data["email"]["configured"])
        self.assertTrue(status_data["payments"]["configured"])
        self.assertNotIn("secret", str(status_data).lower())

    def test_null_resets_special_value_to_default(self):
        self.grant("config.security.view", "config.security.manage")
        self.client.force_authenticate(self.user)
        self.client.patch(
            "/v1/config/security-settings/",
            {"failed_login_threshold": 8},
            format="json",
        )

        response = self.client.patch(
            "/v1/config/security-settings/",
            {"failed_login_threshold": None, "reason": "Restore baseline"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["settings"]["failed_login_threshold"], 5)
        self.assertEqual(response.data["data"]["sources"]["failed_login_threshold"], "default")

    @patch("vs_config.views.test_email_connection")
    def test_connection_test_is_permission_controlled_safe_and_audited(self, test_email):
        cache.clear()
        self.grant("config.integration.view", "config.integration.manage")
        self.client.force_authenticate(self.user)

        response = self.client.post(
            "/v1/config/integration-settings/test/",
            {"connection": "email"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["connected"])
        test_email.assert_called_once_with(actor_id=self.user.pk)
        self.assertTrue(
            ConfigurationAuditEvent.all_objects.filter(
                action="config.integration.connection_tested",
                metadata__connection="email",
            ).exists()
        )


class EntitlementManagementAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_vision_user(email="entitlement-api@example.com", super_admin=True)
        self.school = make_school(slug="entitlement-api-school")
        self.capability = Capability.objects.create(
            key="scheduled-module", label="Scheduled module", requires_entitlement=True,
        )
        self.client.force_authenticate(self.user)

    def test_entitlement_accepts_schedule_and_reset_restores_parent(self):
        set_entitlement(
            capability=self.capability, tenant=None, state="GRANTED",
            source="PLATFORM", actor=self.user,
        )
        self.client.force_authenticate(user=None)
        login = self.client.post(
            "/v1/user/auth/login/",
            {"email": self.user.email, "password": "testpass123"},
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access']}")
        starts_at = timezone.now() + timedelta(hours=1)
        ends_at = timezone.now() + timedelta(days=30)
        response = self.client.post(
            f"/v1/config/entitlements/?tenant={self.school.slug}",
            {
                "capability": self.capability.key,
                "state": "GRANTED",
                "source": "MANUAL",
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["data"]["tenant"], self.school.tenant_id, response.data)
        self.assertEqual(response.data["data"]["starts_at"], starts_at.isoformat().replace("+00:00", "Z"))

        reset = self.client.delete(
            f"/v1/config/entitlements/{self.capability.key}/?tenant={self.school.slug}",
            {"reason": "Return to platform plan"},
            format="json",
        )
        self.assertEqual(reset.status_code, 200, reset.data)
        self.assertTrue(reset.data["data"]["cleared"])
        self.assertTrue(
            reset.data["data"]["effective"],
            {
                "response": reset.data,
                "rows": list(
                    CapabilityEntitlement.all_objects.filter(capability=self.capability)
                    .values("scope_key", "tenant_id", "state")
                ),
            },
        )
        self.assertEqual(reset.data["data"]["source"], "platform")

    def test_past_expiry_is_rejected(self):
        response = self.client.post(
            "/v1/config/entitlements/",
            {
                "capability": self.capability.key,
                "state": "GRANTED",
                "source": "MANUAL",
                "ends_at": (timezone.now() - timedelta(minutes=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)


class ConfigurationAuditDetailAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_vision_user(email="audit-detail@example.com", super_admin=True)
        self.definition = ConfigurationDefinition.objects.create(
            key="audit.detail_example",
            label="Audit detail example",
            value_type="INTEGER",
            default_value=1,
            allowed_scopes=["platform"],
        )
        set_value(
            definition=self.definition,
            value=2,
            actor=self.user,
            reason="First audit event",
        )
        self.event = ConfigurationAuditEvent.all_objects.get(reason="First audit event")
        self.client.force_authenticate(self.user)

    def test_detail_returns_redacted_snapshots_and_list_filters(self):
        detail = self.client.get(f"/v1/config/audit-events/{self.event.pk}/")
        filtered = self.client.get(
            "/v1/config/audit-events/?action=config.value.updated&target_type=ConfigurationValue"
        )

        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["data"]["after_data"], {"value": 2})
        self.assertEqual(filtered.status_code, 200, filtered.data)
        self.assertEqual(len(filtered.data["data"]), 1)

    def test_invalid_date_filter_is_rejected(self):
        response = self.client.get("/v1/config/audit-events/?created_before=not-a-date")
        self.assertEqual(response.status_code, 400)

    def test_invalid_actor_filter_is_rejected(self):
        response = self.client.get("/v1/config/audit-events/?actor=not-a-uuid")
        self.assertEqual(response.status_code, 400)

    def test_facets_and_filtered_export_share_actor_and_target_filters(self):
        facets = self.client.get("/v1/config/audit-events/facets/")
        exported = self.client.get(
            f"/v1/config/audit-events/export/?actor={self.user.pk}"
            f"&target_type={self.event.target_type}&target_id={self.event.target_id}"
        )

        self.assertEqual(facets.status_code, 200, facets.data)
        self.assertIn(str(self.user.pk), [item["id"] for item in facets.data["data"]["actors"]])
        self.assertIn(self.event.target_id, [item["id"] for item in facets.data["data"]["targets"]])
        self.assertEqual(exported.status_code, 200, getattr(exported, "data", None))
        self.assertEqual(exported["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("First audit event", exported.content.decode())

    def test_detail_id_cannot_escape_tenant_scope(self):
        first_school = make_school(slug="audit-detail-first")
        scoped_definition = ConfigurationDefinition.objects.create(
            key="audit.tenant_detail",
            label="Tenant audit detail",
            value_type="STRING",
            allowed_scopes=["school"],
        )
        set_value(
            definition=scoped_definition,
            value="first tenant",
            actor=self.user,
            tenant=first_school.tenant,
            reason="Tenant-only audit event",
        )
        scoped_event = ConfigurationAuditEvent.all_objects.get(
            reason="Tenant-only audit event"
        )

        other_school = make_school(slug="audit-detail-other")
        other_branch = make_branch(other_school)
        other_admin = make_school_admin(other_branch, email="other-audit@example.com")
        role = make_role(other_school, name="Audit reader")
        make_role_permission(role, make_permission("config.audit.view"))
        make_assignment(other_school, other_admin, role)
        self.client.force_authenticate(other_admin)

        response = self.client.get(f"/v1/config/audit-events/{scoped_event.pk}/")

        self.assertEqual(response.status_code, 404)


class EntitlementOperationsAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_vision_user(email="renewals@example.com", super_admin=True)
        self.first = make_school(slug="renewal-first")
        self.second = make_school(slug="renewal-second")
        self.capability = Capability.objects.create(
            key="renewal-feature",
            label="Renewal feature",
            requires_entitlement=True,
        )
        set_entitlement(
            capability=self.capability,
            tenant=self.first.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.MANUAL,
            actor=self.user,
            ends_at=timezone.now() + timedelta(days=5),
        )
        set_entitlement(
            capability=self.capability,
            tenant=self.second.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.MANUAL,
            actor=self.user,
            ends_at=timezone.now() + timedelta(days=20),
        )
        self.client.force_authenticate(self.user)

    def test_calendar_reports_expiry_warnings_across_schools(self):
        response = self.client.get(
            "/v1/config/entitlements/calendar/?all_tenants=true&window_days=90"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["summary"]["expiring_7_days"], 1)
        warnings = {
            row["tenant_slug"]: row["warning"]
            for row in response.data["data"]["entries"]
        }
        self.assertEqual(warnings[self.first.slug], "critical")
        self.assertEqual(warnings[self.second.slug], "warning")

    def test_bulk_schedule_updates_every_selected_grant_atomically(self):
        expiry = timezone.now() + timedelta(days=75)
        response = self.client.post(
            "/v1/config/entitlements/bulk-schedule/",
            {
                "items": [
                    {"capability": self.capability.key, "tenant": self.first.slug},
                    {"capability": self.capability.key, "tenant": self.second.slug},
                ],
                "ends_at": expiry.isoformat(),
                "reason": "Renew both school plans",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        rows = CapabilityEntitlement.all_objects.filter(capability=self.capability)
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(abs((row.ends_at - expiry).total_seconds()) < 1 for row in rows))
        self.assertEqual(
            ConfigurationAuditEvent.all_objects.filter(
                action="config.entitlement.updated",
                metadata__bulk_schedule=True,
            ).count(),
            2,
        )

    def test_school_operator_cannot_bulk_schedule_other_schools(self):
        branch = make_branch(self.first)
        admin = make_school_admin(branch, email="school-renewal@example.com")
        role = make_role(self.first, name="Entitlement manager")
        make_role_permission(role, make_permission("config.entitlement.manage"))
        make_assignment(self.first, admin, role)
        self.client.force_authenticate(admin)

        response = self.client.post(
            "/v1/config/entitlements/bulk-schedule/",
            {
                "items": [{"capability": self.capability.key, "tenant": self.second.slug}],
                "ends_at": (timezone.now() + timedelta(days=60)).isoformat(),
                "reason": "Cross-school attempt",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 403)


class ConfigurationAuditSavedViewsAndExportsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_vision_user(email="saved-audit@example.com", super_admin=True)
        self.other = make_vision_user(email="other-saved-audit@example.com", super_admin=True)
        self.definition = ConfigurationDefinition.objects.create(
            key="audit.saved_view_example",
            label="Saved view example",
            value_type="INTEGER",
            default_value=1,
            allowed_scopes=["platform"],
        )
        set_value(
            definition=self.definition,
            value=2,
            actor=self.user,
            reason="Exported setting change",
        )
        self.client.force_authenticate(self.user)

    def test_saved_views_are_personal_and_duplicate_names_are_rejected(self):
        payload = {
            "name": "Entitlement changes",
            "filters": {"window_days": 90, "action": "config.entitlement.updated"},
        }
        created = self.client.post(
            "/v1/config/audit-events/saved-views/", payload, format="json",
        )
        duplicate = self.client.post(
            "/v1/config/audit-events/saved-views/", payload, format="json",
        )

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(duplicate.status_code, 400, duplicate.data)
        self.assertEqual(ConfigurationAuditSavedView.objects.filter(owner=self.user).count(), 1)
        self.client.force_authenticate(self.other)
        listed = self.client.get("/v1/config/audit-events/saved-views/")
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(listed.data["data"], [])

    @patch("vs_config.tasks.run_configuration_audit_export_task.delay")
    def test_export_is_queued_generated_and_downloaded_only_by_owner(self, delay):
        queued = self.client.post(
            "/v1/config/audit-events/export-jobs/",
            {
                "filters": {"action": "config.value.updated"},
                "client_key": "saved-audit-export-1",
            },
            format="json",
        )
        self.assertEqual(queued.status_code, 201, queued.data)
        job = ConfigurationAuditExportJob.objects.get(pk=queued.data["data"]["id"])
        delay.assert_called_once_with(str(job.pk))

        with TemporaryDirectory() as directory:
            storage = FileSystemStorage(location=directory)
            with patch("vs_config.services.audit_exports.default_storage", storage), patch(
                "vs_config.views.default_storage", storage,
            ):
                execute_configuration_audit_export(job.pk)
                job.refresh_from_db()
                self.assertEqual(job.status, job.Status.COMPLETED)
                self.assertGreaterEqual(job.row_count, 1)

                downloaded = self.client.get(
                    f"/v1/config/audit-events/export-jobs/{job.pk}/download/"
                )
                self.assertEqual(downloaded.status_code, 200)
                self.assertIn(b"Exported setting change", b"".join(downloaded.streaming_content))

                self.client.force_authenticate(self.other)
                forbidden = self.client.get(
                    f"/v1/config/audit-events/export-jobs/{job.pk}/download/"
                )
                self.assertEqual(forbidden.status_code, 404)

    @patch("vs_config.tasks.run_configuration_audit_export_task.delay")
    def test_export_queue_is_idempotent_and_limited_to_three_active_jobs(self, delay):
        first = None
        for index in range(3):
            response = self.client.post(
                "/v1/config/audit-events/export-jobs/",
                {
                    "filters": {"action": "config.value.updated"},
                    "client_key": f"queue-limit-{index}",
                },
                format="json",
            )
            self.assertEqual(response.status_code, 201, response.data)
            first = first or response

        duplicate = self.client.post(
            "/v1/config/audit-events/export-jobs/",
            {
                "filters": {"action": "config.value.updated"},
                "client_key": "queue-limit-0",
            },
            format="json",
        )
        fourth = self.client.post(
            "/v1/config/audit-events/export-jobs/",
            {
                "filters": {"action": "config.value.updated"},
                "client_key": "queue-limit-3",
            },
            format="json",
        )

        self.assertEqual(duplicate.status_code, 200, duplicate.data)
        self.assertEqual(duplicate.data["data"]["id"], first.data["data"]["id"])
        self.assertEqual(fourth.status_code, 400, fourth.data)
        self.assertEqual(delay.call_count, 3)
