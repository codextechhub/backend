"""The configuration audit trail shows a value only to a reader who may read it.

Every change to a configuration value, and every grant a school's plan wrote,
keeps its before and after. Two things in those snapshots belong to a Field
Access switch: a setting stored as a configuration value that restates a
guarded field (bound with ``register_setting_field``), and the subscription
dates of a plan-written grant. The event list, the detail, the CSV download and
a queued export all serve the snapshots through the reader's own switches, and
the stored event is never changed.
"""
import csv
import io

from vs_config import audit_access
from vs_config.models import ConfigurationAuditEvent, ConfigurationDefinition
from vs_config.services.audit_exports import (
    create_configuration_audit_export,
    execute_configuration_audit_export,
)
from vs_config.services.resolution import set_value
from vs_rbac.tests.helpers import (
    make_permission,
    make_platform_assignment,
    make_platform_role,
    make_platform_role_permission,
    make_vision_user,
    set_field_access,
)

from .tests_entitlement_access import EXPIRES, STARTS, _EntitlementAccess

AUDIT_KEYS = ("config.audit.view", "config.audit.export")
BOUND = "audit.access.example"


class _AuditAccess(_EntitlementAccess):
    """The entitlement fixture, plus a bound setting that Corona has changed twice."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.definition = ConfigurationDefinition.objects.create(
            key=BOUND, label="Example", description="A setting that restates a guarded field.",
            value_type=ConfigurationDefinition.ValueType.STRING, default_value="none",
            allowed_scopes=["platform", "school"],
        )
        set_value(definition=cls.definition, value="blue", actor=cls.admin,
                  tenant=cls.corona.tenant)
        set_value(definition=cls.definition, value="green", actor=cls.admin,
                  tenant=cls.corona.tenant)
        cls.reader = cls.auditor("reader")
        cls.blind = cls.auditor("blind", EXPIRES)
        cls.blind_to_dates = cls.auditor("dates", STARTS, EXPIRES)

    @classmethod
    def auditor(cls, label, *closed):
        """CodeX staff who read the audit trail, with the named switches off."""
        user = make_vision_user(email=f"audit-access-{label}@codex.test")
        role = make_platform_role(name=f"Audit access {label}")
        for key in AUDIT_KEYS:
            make_platform_role_permission(role, make_permission(key))
        make_platform_assignment(user, role)
        if closed:
            set_field_access(role, *closed, read=False, write=False)
        return user

    def setUp(self):
        audit_access.register_setting_field(BOUND, EXPIRES)
        self.addCleanup(audit_access._SETTING_FIELDS.pop, BOUND, None)

    def events(self, user, **query):
        parts = "".join(f"&{k}={v}" for k, v in query.items())
        response = self.api(user).get(
            f"/v1/config/audit-events/?tenant={self.corona.slug}&page_size=100{parts}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]


class SnapshotAccessTests(_AuditAccess):

    def value_events(self, user):
        return [e for e in self.events(user, target_type="ConfigurationValue")
                if e["action"] == "config.value.updated"]

    def grant_events(self, user):
        return [e for e in self.events(user, target_type="CapabilityEntitlement")
                if e["after_data"].get("source") == "PACKAGE"]

    def test_an_open_reader_sees_every_value_and_date(self):
        values = self.value_events(self.reader)
        self.assertEqual(sorted(e["after_data"]["value"] for e in values), ["blue", "green"])
        (grant,) = self.grant_events(self.reader)
        self.assertIn("starts_at", grant["after_data"])
        self.assertIn("ends_at", grant["after_data"])

    def test_a_setting_bound_to_a_hidden_field_loses_its_value_but_not_its_event(self):
        values = self.value_events(self.blind)

        self.assertEqual(len(values), 2)
        for event in values:
            self.assertNotIn("value", event["before_data"])
            self.assertNotIn("value", event["after_data"])
            self.assertNotIn("value", event["field_labels"])
            self.assertEqual(event["target_label"], "Example")

    def test_an_unbound_setting_is_served_whole(self):
        audit_access._SETTING_FIELDS.pop(BOUND)
        values = self.value_events(self.blind)
        self.assertEqual(sorted(e["after_data"]["value"] for e in values), ["blue", "green"])

    def test_a_plan_written_grant_loses_the_dates_the_reader_may_not_read(self):
        (grant,) = self.grant_events(self.blind)
        self.assertNotIn("ends_at", grant["after_data"])
        self.assertIn("starts_at", grant["after_data"])
        (none,) = self.grant_events(self.blind_to_dates)
        self.assertNotIn("starts_at", none["after_data"])
        self.assertNotIn("ends_at", none["after_data"])
        self.assertEqual(none["after_data"]["state"], "GRANTED")

    def test_a_hand_written_grant_keeps_its_own_window(self):
        manual = [e for e in self.events(self.blind_to_dates, target_type="CapabilityEntitlement")
                  if e["after_data"].get("source") == "MANUAL"]
        self.assertTrue(manual)
        for event in manual:
            self.assertIn("ends_at", event["after_data"])

    def test_the_detail_is_filtered_like_the_list(self):
        event = ConfigurationAuditEvent.objects.filter(
            action="config.value.updated", target_type="ConfigurationValue",
        ).first()
        response = self.api(self.blind).get(
            f"/v1/config/audit-events/{event.pk}/?tenant={self.corona.slug}")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("value", response.data["data"]["after_data"])
        full = self.api(self.reader).get(
            f"/v1/config/audit-events/{event.pk}/?tenant={self.corona.slug}")
        self.assertIn("value", full.data["data"]["after_data"])

    def test_the_stored_event_is_never_changed(self):
        self.value_events(self.blind)
        stored = ConfigurationAuditEvent.objects.filter(
            action="config.value.updated", target_type="ConfigurationValue",
        )
        self.assertEqual(sorted(e.after_data["value"] for e in stored), ["blue", "green"])

    def test_another_schools_events_are_not_served_to_a_tenant_reader(self):
        response = self.api(self.reader).get(
            f"/v1/config/audit-events/?tenant={self.sunrise.slug}&page_size=100")
        self.assertEqual(response.status_code, 200, response.data)
        for event in response.data["data"]:
            self.assertNotEqual(event["tenant"], self.corona.pk)


class ExportAccessTests(_AuditAccess):

    def rows(self, content):
        return list(csv.reader(io.StringIO(content)))[1:]

    def test_the_csv_download_follows_the_reader(self):
        url = f"/v1/config/audit-events/export/?tenant={self.corona.slug}"
        open_file = self.api(self.reader).get(url).content.decode()
        blind_file = self.api(self.blind).get(url).content.decode()

        self.assertIn("green", open_file)
        self.assertNotIn("green", blind_file)
        self.assertNotIn("blue", blind_file)
        self.assertEqual(len(self.rows(open_file)), len(self.rows(blind_file)))

    def test_a_queued_export_is_written_as_the_person_who_asked(self):
        from django.core.files.storage import default_storage

        written = {}
        for label, user in (("open", self.reader), ("blind", self.blind)):
            job, _ = create_configuration_audit_export(
                actor=user, tenant=self.corona.tenant, branch=None, filters={},
            )
            done = execute_configuration_audit_export(job.pk)
            with default_storage.open(done.storage_name, "rb") as handle:
                written[label] = handle.read().decode()
            self.addCleanup(default_storage.delete, done.storage_name)

        self.assertIn("green", written["open"])
        self.assertNotIn("green", written["blind"])
        self.assertNotIn("blue", written["blind"])


class RegistrationTests(_AuditAccess):
    def test_a_definition_is_bound_to_one_field(self):
        audit_access.register_setting_field(BOUND, STARTS)
        event = ConfigurationAuditEvent.objects.filter(
            action="config.value.updated", target_type="ConfigurationValue").first()
        keys = audit_access.setting_keys([event])
        shown = audit_access.visible_snapshots(
            event, lambda key: key != STARTS, keys)
        self.assertNotIn("value", shown[1])
        shown = audit_access.visible_snapshots(event, lambda key: key == STARTS, keys)
        self.assertIn("value", shown[1])

    def test_no_binding_costs_no_query(self):
        audit_access._SETTING_FIELDS.pop(BOUND)
        event = ConfigurationAuditEvent.objects.first()
        with self.assertNumQueries(0):
            self.assertEqual(audit_access.setting_keys([event]), {})

