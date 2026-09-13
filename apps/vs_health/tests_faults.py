"""Deduplication rules for configuration-fault incidents.

A fault detector runs on the path the fault breaks, which is usually a request
path, so these tests assert the two properties that keep one broken
configuration to one line on the console: a repeat report of an unresolved fault
writes nothing at all, not even a timeline entry, and an operator's edits to the
incident cannot break the match that makes that true.
"""
from __future__ import annotations

from django.test import TestCase

from vs_health.faults import report_configuration_fault
from vs_health.models import Incident, Severity


class ConfigurationFaultTests(TestCase):
    def setUp(self):
        self.key = "fal.ambiguous-primary-entity.tenant-7"

    def _report(self, **overrides):
        payload = {
            "fault_key": self.key,
            "title": "Two sets of books for Corona Secondary",
            "summary": "Corona Secondary has two active tenant-kind entities.",
        }
        payload.update(overrides)
        return report_configuration_fault(**payload)

    def test_a_first_report_opens_an_incident_with_its_opening_note(self):
        incident = self._report(affected_tenant_count=1)

        self.assertIsNotNone(incident)
        self.assertEqual(incident.fault_key, self.key)
        self.assertEqual(incident.source, Incident.Source.AUTO)
        self.assertEqual(incident.status, Incident.Status.INVESTIGATING)
        self.assertEqual(incident.severity, Severity.SEV2)
        self.assertEqual(incident.affected_tenant_count, 1)
        self.assertEqual(incident.timeline.count(), 1)
        self.assertEqual(incident.timeline.get().kind, "opened")

    def test_a_repeat_writes_nothing_at_all(self):
        first = self._report()

        for _ in range(5):
            self.assertIsNone(self._report())

        self.assertEqual(Incident.objects.count(), 1)
        first.refresh_from_db()
        # A timeline entry per report costs a write per request and buries the
        # opening note that says what to do.
        self.assertEqual(first.timeline.count(), 1)

    def test_an_operator_editing_the_incident_does_not_break_the_match(self):
        incident = self._report()
        incident.title = "Corona: looking into the duplicate ledger"
        incident.summary = "Spoke to the bursar."
        incident.status = Incident.Status.IDENTIFIED
        incident.save(update_fields=["title", "summary", "status", "updated_at"])

        self.assertIsNone(self._report())
        self.assertEqual(Incident.objects.count(), 1)

    def test_resolving_lets_a_later_occurrence_open_a_fresh_incident(self):
        first = self._report()
        first.status = Incident.Status.RESOLVED
        first.save(update_fields=["status", "updated_at"])

        second = self._report()

        self.assertIsNotNone(second)
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(Incident.objects.filter(fault_key=self.key).count(), 2)

    def test_different_subjects_are_different_incidents(self):
        self._report()
        other = self._report(fault_key="fal.ambiguous-primary-entity.tenant-8")

        self.assertIsNotNone(other)
        self.assertEqual(Incident.objects.count(), 2)

    def test_an_alert_driven_incident_is_not_mistaken_for_a_fault(self):
        Incident.objects.create(title="API error rate breached")

        opened = self._report()

        # Incidents from the alert engine and from operators carry no fault key,
        # so they never satisfy a fault's deduplication check.
        self.assertIsNotNone(opened)
        self.assertEqual(Incident.objects.count(), 2)

    def test_a_fault_without_a_key_is_refused(self):
        with self.assertRaises(ValueError):
            self._report(fault_key="")

        self.assertEqual(Incident.objects.count(), 0)

    def test_an_overlong_title_is_trimmed_rather_than_refused(self):
        incident = self._report(title="T" * 400)

        self.assertEqual(len(incident.title), 255)
