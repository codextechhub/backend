from django.core.exceptions import ValidationError
from django.test import TestCase
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
    ConfigurationDefinition,
    ConfigurationValue,
)
from .services.capabilities import effective_capability, set_entitlement, set_override
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
