"""Every machine value the configuration API returns travels with words to show.

The settings console once printed ``ConfigurationValue`` under "Target type",
``config.value.updated`` as a change, ``SECRET_REFERENCE`` in a picker and
``tenant:3f2c...`` in a toast. These tests hold the API to its side of the
contract: each such value has a ``*_label`` beside it, and the label reads as
words, never as the code identifier it names.
"""
import re
from datetime import timedelta
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_branch,
    make_school,
    make_school_admin,
    make_vision_user,
)

from . import labels
from .models import (
    Capability,
    CapabilityDependency,
    CapabilityEntitlement,
    ConfigurationAuditEvent,
    ConfigurationDefinition,
)
from .services.capabilities import set_entitlement, set_override
from .services.resolution import set_value

_CODE_SHAPES = (
    re.compile(r"_"),                  # snake_case and SCREAMING_CASE
    re.compile(r"\w\.\w"),             # dotted keys and module paths
    re.compile(r"[a-z][A-Z]"),         # class names
    re.compile(r"\b[A-Z]{5,}\b"),      # bare enum members
    re.compile(r"^(?:tenant|branch):"),  # scope keys
)


class HumanLabelAssertions:
    """``assertHuman``: the text is non-empty and shaped like words, not code."""

    def assertHuman(self, text, context=""):
        self.assertIsInstance(text, str, context)
        self.assertTrue(text.strip(), f"empty label {context}")
        for shape in _CODE_SHAPES:
            self.assertIsNone(
                shape.search(text), f"{text!r} looks like a code identifier {context}",
            )


class LabelTablesTests(HumanLabelAssertions, SimpleTestCase):
    def test_every_label_and_fallback_reads_as_words(self):
        tables = [
            labels.AUDIT_ACTION_LABELS, labels.AUDIT_TARGET_TYPE_LABELS,
            labels.AUDIT_FIELD_LABELS, labels.VALUE_TYPE_LABELS,
            labels.SENSITIVITY_LABELS, labels.ALLOWED_SCOPE_LABELS,
            labels.CAPABILITY_KIND_LABELS, labels.CAPABILITY_DEPTH_LABELS,
            labels.ENTITLEMENT_STATE_LABELS, labels.ENTITLEMENT_SOURCE_LABELS,
            labels.OVERRIDE_STATE_LABELS, labels.CALENDAR_STATUS_LABELS,
            labels.EXPORT_JOB_STATUS_LABELS, labels.INTEGRATION_CONNECTION_LABELS,
            labels.SETTING_GROUP_LABELS,
        ]
        for table in tables:
            for value, label in table.items():
                self.assertHuman(label, f"for {value!r}")
        for fallback in (
            labels.AUDIT_ACTION_FALLBACK, labels.AUDIT_TARGET_TYPE_FALLBACK,
            labels.AUDIT_FIELD_FALLBACK, labels.SETTING_GROUP_FALLBACK,
            labels.INTEGRATION_CONNECTION_FALLBACK,
        ):
            self.assertHuman(fallback)

    def test_an_unknown_value_falls_back_to_a_phrase_not_to_itself(self):
        self.assertEqual(labels.audit_action_label("config.thing.done"), "Configuration change")
        self.assertEqual(labels.audit_target_type_label("SomeNewModel"), "Configuration record")
        self.assertEqual(labels.audit_field_label("brand_new_field"), "Other recorded detail")
        self.assertEqual(labels.setting_group_label("brandnew.setting"), "Other settings")
        self.assertEqual(labels.setting_group_label("undotted"), "Other settings")

    def test_every_action_the_app_records_has_its_own_label(self):
        """A new ``config.*`` audit action must arrive with its words."""
        root = Path(__file__).resolve().parent
        recorded = set()
        for path in root.rglob("*.py"):
            if "tests" in path.name or "migrations" in path.parts:
                continue
            recorded.update(
                re.findall(r'action="(config\.[a-z_]+\.[a-z_]+)"', path.read_text())
            )
        self.assertTrue(recorded)
        self.assertEqual(recorded - set(labels.AUDIT_ACTION_LABELS), set())


class ConfigurationLabelsAPITests(HumanLabelAssertions, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = make_vision_user(email="labels-operator@example.com", super_admin=True)
        cls.school = make_school(slug="labels-school")
        cls.branch = make_branch(cls.school)
        cls.school_admin = make_school_admin(cls.branch, email="labels-admin@example.com")
        cls.definition = ConfigurationDefinition.objects.create(
            key="display.labels_example",
            label="Labels example",
            value_type="INTEGER",
            default_value=1,
            allowed_scopes=["platform", "school", "branch"],
        )
        cls.base = Capability.objects.create(
            key="labels-base", label="Labels base", requires_entitlement=True,
        )
        cls.feature = Capability.objects.create(
            key="labels-feature", label="Labels feature", kind="FEATURE",
            requires_entitlement=True,
        )
        CapabilityDependency.objects.create(capability=cls.feature, requires=cls.base)
        set_value(definition=cls.definition, value=2, actor=cls.operator, reason="Labels")
        set_entitlement(
            capability=cls.base, tenant=None, state="GRANTED", source="MANUAL",
            actor=cls.operator,
        )
        set_override(capability=cls.base, state="DISABLED", actor=cls.operator)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.operator)

    def test_a_definition_names_its_group_and_its_enum_values(self):
        response = self.client.get("/v1/config/definitions/?page_size=100")

        self.assertEqual(response.status_code, 200, response.data)
        row = next(r for r in response.data["data"] if r["key"] == self.definition.key)
        self.assertEqual(row["group_label"], "Dates and times")
        self.assertEqual(row["value_type_label"], "Whole number")
        self.assertEqual(row["allowed_scope_labels"], ["Platform-wide", "Each school", "Each branch"])
        for field in ("group_label", "value_type_label", "sensitivity_label"):
            self.assertHuman(row[field], field)

    def test_the_choice_endpoints_pair_each_value_with_words(self):
        for url, lists in (
            ("/v1/config/definition-choices/", ("value_types", "sensitivities", "allowed_scopes")),
            ("/v1/config/capability-choices/", (
                "kinds", "entitlement_states", "entitlement_sources", "override_states",
            )),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            for name in lists:
                self.assertTrue(response.data["data"][name], name)
                for option in response.data["data"][name]:
                    self.assertEqual(set(option), {"value", "label"})
                    self.assertHuman(option["label"], f"{url} {name}")

    def test_the_choice_endpoints_need_the_catalogue_permission(self):
        self.client.force_authenticate(self.school_admin)
        for url in ("/v1/config/definition-choices/", "/v1/config/capability-choices/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_an_audit_event_names_its_action_its_target_kind_and_its_fields(self):
        event = ConfigurationAuditEvent.all_objects.get(reason="Labels")

        listed = self.client.get("/v1/config/audit-events/?action=config.value.updated")
        detail = self.client.get(f"/v1/config/audit-events/{event.pk}/")

        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(detail.status_code, 200, detail.data)
        for row in (listed.data["data"][0], detail.data["data"]):
            self.assertEqual(row["action"], "config.value.updated")
            self.assertEqual(row["target_type"], "ConfigurationValue")
            self.assertEqual(row["action_label"], "Setting changed")
            self.assertEqual(row["target_type_label"], "Setting value")
        self.assertEqual(detail.data["data"]["field_labels"], {"value": "Value"})

    def test_facets_offer_labelled_actions_and_target_kinds(self):
        response = self.client.get("/v1/config/audit-events/facets/")

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual([o["value"] for o in data["action_options"]], data["actions"])
        self.assertEqual([o["value"] for o in data["target_type_options"]], data["target_types"])
        for option in data["action_options"] + data["target_type_options"]:
            self.assertHuman(option["label"], option["value"])
        for target in data["targets"]:
            self.assertHuman(target["label"], target["type"])
            self.assertHuman(target["type_label"], target["type"])

    def test_a_deleted_target_is_named_by_its_kind_not_its_class(self):
        ConfigurationAuditEvent.all_objects.create(
            action="config.definition.archived", target_type="ConfigurationDefinition",
            target_id="00000000-0000-0000-0000-000000000000", actor=self.operator,
        )

        response = self.client.get("/v1/config/audit-events/facets/")

        labels_by_type = {t["type"]: t["label"] for t in response.data["data"]["targets"]}
        self.assertEqual(labels_by_type["ConfigurationDefinition"], "Setting")

    def test_a_reset_names_the_value_that_now_applies(self):
        response = self.client.delete(
            f"/v1/config/values/{self.definition.key}/", {"reason": "Labels reset"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["source"], "default")
        self.assertEqual(response.data["data"]["source_label"], "Built-in default")

    def test_a_scoped_source_is_named_after_its_school_or_branch(self):
        tenant_row = set_value(
            definition=self.definition, value=3, actor=self.operator,
            tenant=self.school.tenant,
        )
        branch_row = set_value(
            definition=self.definition, value=4, actor=self.operator,
            tenant=self.school.tenant, branch=self.branch,
        )

        self.assertEqual(labels.value_source_label(tenant_row), f"{self.school.tenant.name} value")
        self.assertEqual(labels.value_source_label(branch_row), f"{self.branch.name} branch value")

    def test_entitlements_overrides_and_capabilities_carry_their_words(self):
        entitlements = self.client.get("/v1/config/entitlements/")
        overrides = self.client.get("/v1/config/overrides/")
        capabilities = self.client.get("/v1/config/capabilities/?page_size=100")

        self.assertEqual(entitlements.status_code, 200, entitlements.data)
        grant = next(r for r in entitlements.data["data"] if r["capability_key"] == self.base.key)
        self.assertEqual((grant["state_label"], grant["source_label"]), ("Granted", "Set by an operator"))
        forced = next(r for r in overrides.data["data"] if r["capability_key"] == self.base.key)
        self.assertEqual(forced["state_label"], "Forced off")
        feature = next(r for r in capabilities.data["data"] if r["key"] == self.feature.key)
        self.assertEqual(feature["kind_label"], "Feature")
        self.assertEqual(feature["dependencies"], [self.base.key])
        self.assertEqual(
            feature["dependency_details"], [{"key": self.base.key, "label": "Labels base"}],
        )

    def test_the_payment_provider_is_named_not_coded(self):
        response = self.client.get("/v1/config/integration-settings/")

        self.assertEqual(response.status_code, 200, response.data)
        payments = response.data["data"]["status"]["payments"]
        self.assertHuman(payments["provider_label"])

    def test_the_renewal_calendar_names_each_status(self):
        CapabilityEntitlement.all_objects.filter(capability=self.base).update(
            ends_at=timezone.now() + timedelta(days=20),
        )

        response = self.client.get("/v1/config/entitlements/calendar/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["entries"])
        for entry in response.data["data"]["entries"]:
            self.assertEqual(entry["status_label"], labels.CALENDAR_STATUS_LABELS[entry["status"]])
