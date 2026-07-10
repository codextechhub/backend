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

    def test_resolution_uses_branch_school_platform_default_precedence(self):
        value, source = resolve_value(self.definition, school=self.school, branch=self.branch)
        self.assertEqual(value, "UTC")
        self.assertIsNone(source)

        platform = set_value(
            definition=self.definition, value="Africa/Accra", actor=self.actor
        )
        school = set_value(
            definition=self.definition, value="Africa/Lagos", actor=self.actor,
            school=self.school,
        )
        branch = set_value(
            definition=self.definition, value="Europe/London", actor=self.actor,
            school=self.school, branch=self.branch,
        )

        self.assertEqual(resolve_value(self.definition)[0], platform.value)
        self.assertEqual(resolve_value(self.definition, school=self.school)[0], school.value)
        value, source = resolve_value(self.definition, school=self.school, branch=self.branch)
        self.assertEqual(value, branch.value)
        self.assertEqual(source.scope_key, f"branch:{self.branch.pk}")

    def test_branch_scope_populates_school_and_rejects_mismatch(self):
        row = ConfigurationValue(
            definition=self.definition, branch=self.branch, value="Africa/Lagos"
        )
        row.save()
        self.assertEqual(row.school, self.school)

        other = make_school(slug="other-config-school")
        invalid = ConfigurationValue(
            definition=self.definition, school=other, branch=self.branch, value="UTC"
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
        self.branch = make_branch(self.school)
        self.actor = make_vision_user(email="capability-actor@example.com")
        self.finance, _ = Capability.objects.update_or_create(
            key="finance",
            defaults={"label": "Finance", "requires_entitlement": True, "default_enabled": True},
        )

    def test_runtime_override_cannot_bypass_entitlement(self):
        self.assertFalse(effective_capability(self.finance, school=self.school))
        with self.assertRaises(CapabilityNotEntitled):
            set_override(
                capability=self.finance, state=CapabilityOverride.State.ENABLED,
                actor=self.actor, school=self.school,
            )

    def test_entitlement_and_most_specific_override_are_separate(self):
        set_entitlement(
            capability=self.finance, school=self.school,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE, actor=self.actor,
        )
        self.assertTrue(effective_capability(self.finance, school=self.school, branch=self.branch))
        set_override(
            capability=self.finance, state=CapabilityOverride.State.DISABLED,
            actor=self.actor, school=self.school, branch=self.branch,
        )
        self.assertTrue(effective_capability(self.finance, school=self.school))
        self.assertFalse(effective_capability(self.finance, school=self.school, branch=self.branch))

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
        self.assertFalse(effective_capability(procurement, school=self.school))
        set_entitlement(
            capability=self.finance, school=self.school,
            state="GRANTED", source="MANUAL", actor=self.actor,
        )
        self.assertTrue(effective_capability(procurement, school=self.school))

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
