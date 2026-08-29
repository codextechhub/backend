"""Two things that are easy to assume and worth pinning.

First, that an outage comes back as an unavailable *value* while a caller error
still raises: the split the whole availability envelope exists for, and the one a
dashboard gets wrong by rendering a zero.

Second, that the real caller genuinely goes through the FAL. School creation
provisions books, and it did so before the FAL existed too, so a test that only
checks "the school has books" would pass either way and prove nothing about the
wiring.
"""

from __future__ import annotations

from unittest import mock

from django.db import OperationalError

from schools.core.fal import registry
from schools.core.fal.adapters.django_finance import (
    DjangoEntityResolverAdapter,
    DjangoFinanceReadAdapter,
)
from schools.core.fal.contracts import Unavailable
from schools.core.fal.exceptions import CrossTenantError, EntityNotProvisioned
from schools.core.fal.testing import FakeEntityResolver

from .base import FALFixture


class AvailabilityEnvelopeTests(FALFixture):
    def test_a_database_outage_is_unavailable_and_not_an_exception(self):
        """The dashboard renders "we cannot reach finance", never N0.00."""
        reader = DjangoFinanceReadAdapter()

        with mock.patch(
            "schools.core.fal.adapters.django_finance._school",
            side_effect=OperationalError("server has gone away"),
        ):
            result = reader.outstanding(self.corona.pk)

        self.assertFalse(result.is_available)
        self.assertEqual(result.reason, Unavailable.BACKEND_UNAVAILABLE)
        self.assertIsNone(result.value)

    def test_an_invariant_violation_still_raises_through_the_envelope(self):
        """An outage and a bug must not arrive looking the same."""
        with self.assertRaises(EntityNotProvisioned):
            DjangoEntityResolverAdapter().resolve_entity(
                self._school_without_books().pk,
            )

    def test_a_cross_tenant_reference_raises_rather_than_reading_empty(self):
        with self.assertRaises(CrossTenantError):
            DjangoEntityResolverAdapter().resolve_entity(9_999_999)

    def test_an_integrity_error_is_never_dressed_up_as_an_outage(self):
        """Only connection-level failures are outages; the rest are bugs."""
        from django.db import IntegrityError

        reader = DjangoFinanceReadAdapter()
        with mock.patch(
            "schools.core.fal.adapters.django_finance._school",
            side_effect=IntegrityError("something is genuinely wrong"),
        ):
            with self.assertRaises(IntegrityError):
                reader.outstanding(self.corona.pk)

    def _school_without_books(self):
        from schools.vs_schools.models import School

        return School.objects.create(
            slug="bookless", name="Bookless Academy", code="BK-1", status="ACTIVE",
        )


class SchoolCreationGoesThroughTheFALTests(FALFixture):
    """The real caller: ``schools.vs_schools.services.books``."""

    def test_it_asks_the_registry_rather_than_importing_finance(self):
        """Swap the port and the caller's behaviour changes, which is the proof.

        Without the FAL in the path this test cannot fail: the school would get
        its books from ``provision_books`` directly and the injected fake would
        never be consulted.
        """
        from schools.vs_schools.models import School
        from schools.vs_schools.services.books import provision_books_for_school

        fake = FakeEntityResolver()
        registry.set_entity_resolver(fake)
        school = School.objects.create(
            slug="wired-school", name="Wired Academy", code="WA-1", status="ACTIVE",
        )

        provision_books_for_school(school)

        self.assertIn(school.pk, fake.entities)
        self.assertEqual(fake.entities[school.pk].code, "WIREDSCHOOL")
        self.assertEqual(fake.entities[school.pk].name, "Wired Academy")

    def test_it_returns_the_entity_the_fal_resolved(self):
        from schools.vs_schools.models import School
        from schools.vs_schools.services.books import provision_books_for_school

        school = School.objects.create(
            slug="real-school", name="Real Academy", code="RA-1", status="ACTIVE",
        )

        entity = provision_books_for_school(school)

        self.assertIsNotNone(entity)
        self.assertEqual(entity.tenant_id, school.tenant_id)
        self.assertEqual(entity.code, "REALSCHOOL")

    def test_a_school_that_already_has_books_is_not_given_a_second_set(self):
        from schools.vs_schools.services.books import provision_books_for_school
        from vs_finance.models import LedgerEntity

        again = provision_books_for_school(self.corona)

        self.assertEqual(again.pk, self.corona_books.entity_ref)
        self.assertEqual(
            LedgerEntity.objects.filter(tenant=self.corona.tenant).count(), 1,
        )

    def test_a_fal_failure_costs_the_books_and_not_the_school(self):
        """Books are worth less than the school, so the failure stays local."""
        from schools.vs_schools.models import School
        from schools.vs_schools.services.books import provision_books_for_school

        school = School.objects.create(
            slug="doomed-school", name="Doomed Academy", code="DA-1", status="ACTIVE",
        )
        with mock.patch(
            "schools.core.fal.adapters.django_finance._school",
            side_effect=OperationalError("finance is down"),
        ):
            result = provision_books_for_school(school)

        self.assertIsNone(result)
        school.refresh_from_db()
        self.assertEqual(school.slug, "doomed-school")

    def test_the_school_keeps_its_own_currency(self):
        from schools.vs_schools.models import School
        from schools.vs_schools.services.books import provision_books_for_school

        school = School.objects.create(
            slug="dollar-school", name="Dollar Academy", code="DS-1",
            status="ACTIVE", currency="USD",
        )

        entity = provision_books_for_school(school)

        self.assertEqual(entity.base_currency_id, "USD")
