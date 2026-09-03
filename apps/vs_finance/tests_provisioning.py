"""Provisioning a set of books as a service, and the one-primary-entity guard.

The sequence that makes books usable is a function any caller can reach, not
something reachable only through ``LedgerEntityCreateSerializer.create`` and an
HTTP POST. These tests exercise it as such, with the guard sitting on the
function rather than on the endpoint.
"""
from __future__ import annotations

from django.test import TestCase

from vs_tenants.models import Tenant

from .exceptions import PrimaryEntityExistsError
from .models import Account, FiscalPeriod, LedgerEntity
from .provisioning import primary_entity_for, provision_books


def _school_tenant(slug="books-tenant", name="Books Tenant"):
    """A customer tenant. Deliberately built from vs_tenants alone: the finance
    engine must not know that a school is what usually sits behind one."""
    return Tenant.objects.create(name=name, slug=slug, kind=Tenant.Kind.SCHOOL)


class ProvisionBooksServiceTests(TestCase):
    """The extracted service, called directly rather than through the API."""

    def test_provision_books_creates_a_usable_set_of_books(self):
        tenant = _school_tenant()

        entity = provision_books(tenant=tenant, name="Books Tenant", code="BOOKSA")

        self.assertEqual(entity.tenant_id, tenant.id)
        self.assertEqual(entity.code, "BOOKSA")
        self.assertEqual(entity.kind, LedgerEntity.Kind.TENANT)
        self.assertTrue(entity.is_active)
        self.assertIsNotNone(entity.activated_at)
        # A short reporting code is derived even though none was supplied.
        self.assertTrue(entity.number_code)

        # Usable means: a chart to post to and open periods to post into.
        codes = set(Account.objects.filter(entity=entity).values_list("code", flat=True))
        self.assertTrue({"1100", "1200", "3100"}.issubset(codes))
        periods = FiscalPeriod.objects.filter(entity=entity)
        self.assertEqual(periods.count(), 12)
        self.assertTrue(all(p.status == "OPEN" for p in periods))

    def test_provision_books_honours_the_fiscal_anchors(self):
        """A Sept-Aug school year, opened by a direct call."""
        tenant = _school_tenant(slug="school-year", name="School Year")

        entity = provision_books(
            tenant=tenant, name="School Year", code="SCHOOLYR",
            fiscal_year=2026, fiscal_start_month=9,
        )

        names = list(
            FiscalPeriod.objects
            .filter(entity=entity).order_by("period_no")
            .values_list("name", flat=True)
        )
        self.assertEqual(names[0], "2026-09")
        self.assertEqual(names[-1], "2027-08")

    def test_base_currency_accepts_the_code_or_the_row(self):
        from .models import Currency

        by_code = provision_books(
            tenant=_school_tenant(slug="cur-code", name="By Code"),
            name="By Code", code="BYCODE", base_currency="USD",
        )
        by_row = provision_books(
            tenant=_school_tenant(slug="cur-row", name="By Row"),
            name="By Row", code="BYROW", base_currency=Currency.objects.get(code="USD"),
        )

        self.assertEqual(by_code.base_currency_id, "USD")
        self.assertEqual(by_row.base_currency_id, "USD")

    def test_registered_provisioners_run_and_a_failure_takes_the_entity_with_it(self):
        """Books without their approval ladders are the door this closes."""
        from . import provisioning

        seen = []

        def _boom(entity):
            seen.append(entity.code)
            raise RuntimeError("provisioner exploded")

        original = list(provisioning._PROVISIONERS)
        provisioning._PROVISIONERS.append(_boom)
        try:
            with self.assertRaises(RuntimeError):
                provision_books(
                    tenant=_school_tenant(slug="rollback", name="Rollback"),
                    name="Rollback", code="ROLLBACK",
                )
        finally:
            provisioning._PROVISIONERS[:] = original

        self.assertEqual(seen, ["ROLLBACK"])
        self.assertFalse(LedgerEntity.objects.filter(code="ROLLBACK").exists())


class SecondTenantEntityGuardTests(TestCase):
    """A tenant gets one primary set of books, and only one.

    The model docstring says a tenant *may* keep several entities, and that is
    still true for PLATFORM / PRODUCT / OTHER kinds. What is refused is a second
    ``TENANT``-kind entity, because the finance abstraction layer resolves one
    primary entity per tenant and two candidates make that ambiguous.
    """

    def setUp(self):
        self.tenant = _school_tenant(slug="guarded", name="Guarded School")
        self.first = provision_books(
            tenant=self.tenant, name="Guarded School", code="GUARDED",
        )

    def test_a_second_tenant_entity_is_refused(self):
        with self.assertRaises(PrimaryEntityExistsError) as caught:
            provision_books(tenant=self.tenant, name="Guarded Again", code="GUARDED2")

        self.assertEqual(caught.exception.error_code, "PRIMARY_ENTITY_EXISTS")
        self.assertEqual(caught.exception.http_status, 409)
        self.assertEqual(caught.exception.entity_code, "GUARDED")
        # Refused before anything was written.
        self.assertFalse(LedgerEntity.objects.filter(code="GUARDED2").exists())
        self.assertEqual(
            LedgerEntity.objects.filter(tenant=self.tenant).count(), 1,
        )

    def test_the_refusal_renders_into_the_platform_envelope(self):
        from core.exceptions import custom_exception_handler

        response = custom_exception_handler(
            PrimaryEntityExistsError(entity_code="GUARDED"), {},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["error"]["code"], "PRIMARY_ENTITY_EXISTS")

    def test_reuse_existing_returns_the_books_the_tenant_already_has(self):
        """The idempotent direction school creation and the backfill rely on."""
        again = provision_books(
            tenant=self.tenant, name="Different Name", code="DIFFERENT",
            reuse_existing=True,
        )

        self.assertEqual(again.pk, self.first.pk)
        self.assertEqual(again.name, "Guarded School")  # left alone, not renamed
        self.assertEqual(LedgerEntity.objects.filter(tenant=self.tenant).count(), 1)

    def test_other_kinds_are_not_blocked(self):
        """The rule is about the TENANT kind only; the docstring's several
        entities per tenant stays possible."""
        for kind in (
            LedgerEntity.Kind.PRODUCT,
            LedgerEntity.Kind.OTHER,
            LedgerEntity.Kind.PLATFORM,
        ):
            with self.subTest(kind=kind):
                entity = provision_books(
                    tenant=self.tenant, name=f"Extra {kind}",
                    code=f"EXTRA{kind[:4]}", kind=kind,
                )
                self.assertEqual(entity.kind, kind)

    def test_an_inactive_entity_does_not_block_a_replacement(self):
        """A deactivated set of books is not a primary set of books, so a
        tenant whose books were retired can be given new ones."""
        self.first.is_active = False
        self.first.save(update_fields=["is_active"])

        replacement = provision_books(
            tenant=self.tenant, name="Guarded School", code="GUARDEDNEW",
        )

        self.assertNotEqual(replacement.pk, self.first.pk)

    def test_the_platform_tenant_may_keep_several_sets_of_books(self):
        """Codex's own tenant is exempt: it keeps platform, product and test
        books side by side and is never resolved through the primary lookup."""
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)

        first = provision_books(tenant=codex, name="Codex A", code="CODEXA")
        second = provision_books(tenant=codex, name="Codex B", code="CODEXB")

        self.assertNotEqual(first.pk, second.pk)
        self.assertIsNone(primary_entity_for(codex))

    def test_primary_entity_for_answers_the_resolution_question(self):
        self.assertEqual(primary_entity_for(self.tenant).pk, self.first.pk)
        self.assertIsNone(primary_entity_for(_school_tenant(slug="bookless", name="Bookless")))
        self.assertIsNone(primary_entity_for(None))
