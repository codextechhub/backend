"""The operator-facing half of the ambiguous-books refusal.

A school holding two active tenant-kind entities is a configuration fault the FAL
refuses on every read it serves, so these tests are as much about what it must
*not* write (a second incident, a timeline entry per request) as about what it
writes once. They also pin the two properties that make the reporting safe to sit
on a read path: it never changes the refusal a caller sees, and it survives the
rollback of the provisioning transaction that raised it.
"""

from __future__ import annotations

from unittest.mock import patch

from schools.core.fal.adapters.django_finance import (
    _AMBIGUOUS_PRIMARY_FAULT,
    DjangoEntityResolverAdapter,
    DjangoFinanceReadAdapter,
    DjangoProcurementReadAdapter,
)
from schools.core.fal.exceptions import AmbiguousPrimaryEntity, EntityNotProvisioned
from vs_health.models import Incident

from .base import FALFixture


class AmbiguousPrimaryReportingTests(FALFixture):
    """Corona is given a spare set of books; Greenfield stays correctly set up."""

    def setUp(self):
        super().setUp()
        self.reader = DjangoFinanceReadAdapter()
        self.resolver = DjangoEntityResolverAdapter()

    # ----- fixtures -------------------------------------------------------- #
    def _spare_books(self, school):
        from vs_finance.models import LedgerEntity

        return LedgerEntity.objects.create(
            tenant=school.tenant, name=f"{school.name} Second Books",
            code=f"{school.code}-2", kind=LedgerEntity.Kind.TENANT,
            base_currency_id="NGN", is_active=True,
        )

    def _fault_key(self, school):
        return f"{_AMBIGUOUS_PRIMARY_FAULT}.tenant-{school.tenant_id}"

    def _incidents_for(self, school):
        return Incident.objects.filter(fault_key=self._fault_key(school))

    # ----- the read path --------------------------------------------------- #
    def test_a_read_raising_three_times_opens_exactly_one_incident(self):
        self._spare_books(self.corona)

        for _ in range(3):
            with self.assertRaises(AmbiguousPrimaryEntity):
                self.reader.collections(self.corona.pk)

        incidents = self._incidents_for(self.corona)
        self.assertEqual(incidents.count(), 1)
        # A repeat must not append to the timeline either: one entry per request
        # buries the opening note as surely as a second incident does.
        self.assertEqual(incidents.get().timeline.count(), 1)

    def test_the_refusal_keeps_its_message_while_being_reported(self):
        self._spare_books(self.corona)

        with self.assertRaises(AmbiguousPrimaryEntity) as raised:
            self.reader.collections(self.corona.pk)

        self.assertIn("more than one active entity", str(raised.exception))
        self.assertIn(self.corona.slug, str(raised.exception))

    def test_the_incident_names_the_school_and_what_to_do(self):
        self._spare_books(self.corona)

        with self.assertRaises(AmbiguousPrimaryEntity):
            self.reader.collections(self.corona.pk)

        incident = self._incidents_for(self.corona).get()
        self.assertEqual(incident.source, Incident.Source.AUTO)
        self.assertEqual(incident.status, Incident.Status.INVESTIGATING)
        self.assertTrue(incident.is_active)
        self.assertIn(self.corona.name, incident.title)
        self.assertIn(self.corona.slug, incident.summary)
        self.assertIn("deactivated", incident.summary)

    def test_procurement_reads_report_the_same_fault_once(self):
        self._spare_books(self.corona)

        with self.assertRaises(AmbiguousPrimaryEntity):
            self.reader.collections(self.corona.pk)
        with self.assertRaises(AmbiguousPrimaryEntity):
            DjangoProcurementReadAdapter().snapshot(self.corona.pk)

        # Both read ports resolve the school the same way, so they are one fault.
        self.assertEqual(self._incidents_for(self.corona).count(), 1)

    def test_two_broken_schools_get_an_incident_each(self):
        self._spare_books(self.corona)
        self._spare_books(self.greenfield)

        for school in (self.corona, self.greenfield):
            with self.assertRaises(AmbiguousPrimaryEntity):
                self.reader.collections(school.pk)

        self.assertEqual(self._incidents_for(self.corona).count(), 1)
        self.assertEqual(self._incidents_for(self.greenfield).count(), 1)

    def test_resolving_the_incident_lets_the_next_occurrence_reopen(self):
        self._spare_books(self.corona)
        with self.assertRaises(AmbiguousPrimaryEntity):
            self.reader.collections(self.corona.pk)

        opened = self._incidents_for(self.corona).get()
        opened.status = Incident.Status.RESOLVED
        opened.save(update_fields=["status", "updated_at"])

        with self.assertRaises(AmbiguousPrimaryEntity):
            self.reader.collections(self.corona.pk)

        # Resolving is how an operator says "I have dealt with it": a fault that
        # is still there afterwards must be able to say so again.
        self.assertEqual(self._incidents_for(self.corona).count(), 2)

    # ----- reporting must never mask the fault ----------------------------- #
    def test_a_failing_reporter_changes_neither_the_error_nor_its_message(self):
        self._spare_books(self.corona)

        with patch(
            "vs_health.faults.report_configuration_fault",
            side_effect=RuntimeError("health is down"),
        ):
            with self.assertRaises(AmbiguousPrimaryEntity) as raised:
                self.reader.collections(self.corona.pk)

        self.assertIn("more than one active entity", str(raised.exception))
        self.assertEqual(self._incidents_for(self.corona).count(), 0)

    # ----- provisioning ---------------------------------------------------- #
    def test_provisioning_reports_outside_its_own_transaction(self):
        """The refusal rolls the onboarding transaction back; the incident stays.

        ``provision_entity`` raises from inside ``transaction.atomic`` while
        holding a lock on the tenant row, so an incident written in there would
        be rolled back with the refusal and the fault would be silent exactly
        where a human is already watching an onboarding fail.
        """
        self._spare_books(self.corona)

        with self.assertRaises(AmbiguousPrimaryEntity):
            self.resolver.provision_entity(
                self.corona.pk, code="CORONA", name="Corona Secondary",
            )

        self.assertEqual(self._incidents_for(self.corona).count(), 1)

    def test_resolve_entity_reports_the_same_single_fault(self):
        self._spare_books(self.corona)

        with self.assertRaises(AmbiguousPrimaryEntity):
            self.resolver.resolve_entity(self.corona.pk)
        with self.assertRaises(AmbiguousPrimaryEntity):
            self.resolver.resolve_entity(self.corona.pk)

        self.assertEqual(self._incidents_for(self.corona).count(), 1)

    # ----- everything that is not this fault ------------------------------- #
    def test_a_correctly_provisioned_school_opens_nothing(self):
        self.reader.collections(self.corona.pk).unwrap()
        self.reader.collections(self.greenfield.pk).unwrap()

        self.assertEqual(Incident.objects.count(), 0)

    def test_a_school_with_no_books_opens_nothing(self):
        from schools.vs_schools.models import School

        school = School.objects.create(
            slug="no-books", name="No Books Academy", code="NB-1", status="ACTIVE",
        )

        with self.assertRaises(EntityNotProvisioned):
            self.reader.collections(school.pk)

        # Unprovisioned books are an onboarding step nobody has run, not a
        # configuration fault an operator has to unpick.
        self.assertEqual(Incident.objects.count(), 0)

    def test_the_in_memory_fake_refuses_without_writing_health_rows(self):
        from schools.core.fal.testing import FakeEntityResolver

        fake = FakeEntityResolver(ambiguous_schools={self.corona.pk})

        with self.assertRaises(AmbiguousPrimaryEntity):
            fake.resolve_entity(self.corona.pk)

        self.assertEqual(Incident.objects.count(), 0)
