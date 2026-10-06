"""Money filters take naira, and the catalogue converts them to kobo.

Money is stored as integer kobo, but nobody types kobo: a bursar looking for
invoices over ₦50,000 types 50000. :func:`vs_exports.catalogue.compile_filter`
is the one place every dataset's filters are read, so the conversion lives
there, and a filter is a money filter because its :class:`FilterDef` says so.
These tests hold that choke point, the 400 a malformed amount earns at every
door a filter comes in by, every money filter in the registry, and the data
migration that moves saved recipes from kobo to naira.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import SimpleTestCase, TestCase, tag

from core.migration_testing import RewoundSchemaTestCase
from core.test_utils import TenantAPIClient

from vs_exports.catalogue import (
    FILTER_NUMBER_RANGE,
    KIND_MONEY,
    KIND_NUMBER,
    Dataset,
    Field,
    FilterDef,
    FilterError,
    all_datasets,
    compile_filter,
    describe_filter,
    register,
)
from vs_exports.constants import ExportFormat, ValuesMode
from vs_exports.models import ExportDefinition, ExportRun
from vs_exports.tests import COLUMNS, DATASET, _ExportFixture


def _dataset(*filters, fields=None):
    """A throwaway dataset, never registered, carrying *filters*."""
    return Dataset(
        key="test.money", module="Test", name="Money test", description="",
        base=lambda scope: None,
        fields=fields if fields is not None else (
            Field("total", "Total", "Amounts", KIND_MONEY),
            Field("quantity", "Quantity", "Amounts", KIND_NUMBER),
        ),
        filters=filters,
    )


MONEY = FilterDef("total", "Total", FILTER_NUMBER_RANGE, money=True)
COUNT = FilterDef("quantity", "Quantity", FILTER_NUMBER_RANGE)


class MoneyFilterConversionTests(SimpleTestCase):
    """The conversion itself: naira in, integer kobo out, nothing else accepted."""

    def setUp(self):
        self.dataset = _dataset(MONEY, COUNT)

    def compile(self, **spec):
        return compile_filter(self.dataset, {"id": "total", **spec})

    def test_whole_naira_becomes_kobo(self):
        self.assertEqual(self.compile(min=50000), Q(total__gte=5_000_000))

    def test_both_bounds_convert(self):
        self.assertEqual(
            self.compile(min=50000, max="125000"),
            Q(total__gte=5_000_000) & Q(total__lte=12_500_000),
        )

    def test_up_to_two_decimal_places_are_exact(self):
        self.assertEqual(self.compile(min="50000.5"), Q(total__gte=5_000_050))
        self.assertEqual(self.compile(min="1250.75"), Q(total__gte=125_075))
        # A JSON number with a fraction arrives as a float and stays exact.
        self.assertEqual(self.compile(min=0.1), Q(total__gte=10))
        self.assertEqual(self.compile(min=Decimal("19.99")), Q(total__gte=1_999))
        # Trailing zeros past the second place change nothing.
        self.assertEqual(self.compile(min="50000.500"), Q(total__gte=5_000_050))

    def test_more_than_two_decimal_places_is_refused_without_mentioning_kobo(self):
        with self.assertRaises(FilterError) as caught:
            self.compile(min="50000.505")
        message = str(caught.exception)
        self.assertIn("Total", message)
        self.assertIn("two", message)
        self.assertNotIn("kobo", message.lower())
        self.assertEqual(caught.exception.filter_id, "total")

    def test_a_value_that_is_not_a_number_is_refused(self):
        for bad in ("abc", "50,000", True, "NaN", "Infinity", [5], {"n": 5}):
            with self.subTest(bad=bad), self.assertRaises(FilterError) as caught:
                self.compile(min=bad)
            self.assertIn("naira", str(caught.exception))
            self.assertNotIn("kobo", str(caught.exception).lower())

    def test_an_absent_bound_is_no_bound(self):
        self.assertEqual(self.compile(), Q())
        self.assertEqual(self.compile(min=None, max=None), Q())
        self.assertEqual(self.compile(min="", max="  "), Q())

    def test_a_number_filter_that_is_not_money_is_untouched(self):
        self.assertEqual(
            compile_filter(self.dataset, {"id": "quantity", "min": 5, "max": 50}),
            Q(quantity__gte=5) & Q(quantity__lte=50),
        )

    def test_the_review_sentence_reads_naira(self):
        self.assertEqual(
            describe_filter(self.dataset, {"id": "total", "min": 50000, "max": "75000.5"}),
            "Total is between ₦50,000.00 and ₦75,000.50",
        )
        self.assertEqual(
            describe_filter(self.dataset, {"id": "total", "min": 50000}),
            "Total is between ₦50,000.00 and any",
        )
        # A stored value that no longer parses is shown as it is, never raised.
        self.assertEqual(
            describe_filter(self.dataset, {"id": "total", "min": "lots"}),
            "Total is between lots and any",
        )

    def test_the_catalogue_publishes_which_filters_are_money(self):
        described = {f["id"]: f for f in self.dataset.describe()["filters"]}
        self.assertTrue(described["total"]["money"])
        self.assertEqual(
            described["total"]["description"],
            "Enter amounts in naira, e.g. 50000 for ₦50,000.00.",
        )
        self.assertFalse(described["quantity"]["money"])
        self.assertEqual(described["quantity"]["description"], "")


class MoneyFilterDeclarationTests(SimpleTestCase):
    """A number filter over a money column must say it is money."""

    def test_a_plain_number_filter_on_a_money_column_is_refused_at_registration(self):
        with self.assertRaises(ImproperlyConfigured):
            register(_dataset(FilterDef("total", "Total", FILTER_NUMBER_RANGE)))

    def test_a_money_filter_must_be_a_number_range(self):
        from vs_exports.catalogue import FILTER_TEXT

        with self.assertRaises(ImproperlyConfigured):
            register(_dataset(FilterDef("total", "Total", FILTER_TEXT, money=True)))

    def test_every_registered_money_filter_is_declared_and_says_naira(self):
        """Every dataset in every app: a number filter over a money column is a
        money filter, and its hint asks for naira."""
        seen = 0
        for dataset in all_datasets():
            money_paths = {f.path for f in dataset.fields if f.kind == KIND_MONEY}
            for fdef in dataset.filters:
                if fdef.kind != FILTER_NUMBER_RANGE:
                    continue
                with self.subTest(dataset=dataset.key, filter=fdef.id):
                    if fdef.path in money_paths:
                        self.assertTrue(fdef.money)
                    if fdef.money:
                        seen += 1
                        hint = fdef.describe()["description"]
                        self.assertIn("naira", hint)
                        self.assertNotIn("kobo", hint.lower())
                        self.assertNotIn("hundredths", hint)
        # The invoice and expense-claim totals, at least.
        self.assertGreaterEqual(seen, 2)


class MoneyFilterApiTests(_ExportFixture, TestCase):
    """The fixture's invoices total ₦1,240,000, ₦318,500, ₦2,004,750 and ₦96,200."""

    def setUp(self):
        self.client = TenantAPIClient(user=self.admin)

    def _filters(self, **total):
        return [
            {
                "id": "invoice_date",
                "start": (self.today - datetime.timedelta(days=30)).isoformat(),
                "end": (self.today + datetime.timedelta(days=1)).isoformat(),
            },
            {"id": "total", **total},
        ]

    def _preview(self, **total):
        return self.client.post(
            f"/v1/exports/preview/?entity={self.entity.code}",
            {"dataset_key": DATASET, "columns": COLUMNS, "filters": self._filters(**total),
             "format": ExportFormat.CSV, "values_mode": ValuesMode.SYSTEM},
            format="json",
        )

    def test_typing_naira_finds_the_invoices_over_that_many_naira(self):
        response = self._preview(min=500000)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["matching_rows"], 2)
        self.assertEqual(
            sorted(row[-1] for row in data["sample"]["rows"]),
            ["1240000.00", "2004750.00"],
        )

    def test_a_decimal_bound_is_exact(self):
        response = self._preview(min="96200.01", max="1240000")
        self.assertEqual(response.json()["data"]["matching_rows"], 2)
        response = self._preview(min="96200.00", max="96200.00")
        self.assertEqual(response.json()["data"]["matching_rows"], 1)

    def test_the_review_sentence_reads_naira(self):
        reads_as = self._preview(min=500000).json()["data"]["reads_as"]
        self.assertIn("₦500,000.00", reads_as)

    def test_a_malformed_amount_is_a_plain_400_on_preview(self):
        response = self._preview(min="50000.505")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("kobo", response.content.decode().lower())

    def test_a_malformed_amount_is_a_plain_400_on_a_quick_export(self):
        response = self.client.post(
            f"/v1/exports/quick/?entity={self.entity.code}",
            {"dataset_key": DATASET, "columns": COLUMNS,
             "filters": self._filters(min="fifty thousand"),
             "format": ExportFormat.CSV, "values_mode": ValuesMode.SYSTEM},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("naira", response.content.decode())
        self.assertFalse(ExportRun.objects.exists())

    def test_a_malformed_amount_is_a_plain_400_when_saving_an_export(self):
        response = self.client.post(
            f"/v1/exports/definitions/?entity={self.entity.code}",
            {"name": "Big invoices", "dataset_key": DATASET, "columns": COLUMNS,
             "filters": self._filters(min="1.234"), "format": ExportFormat.CSV},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("kobo", response.content.decode().lower())
        self.assertFalse(ExportDefinition.objects.filter(name="Big invoices").exists())

    def test_a_saved_export_keeps_the_naira_it_was_given_and_runs_on_kobo(self):
        response = self.client.post(
            f"/v1/exports/definitions/?entity={self.entity.code}",
            {"name": "Big invoices", "dataset_key": DATASET, "columns": COLUMNS,
             "filters": self._filters(min=500000), "format": ExportFormat.CSV},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        definition = ExportDefinition.objects.get(name="Big invoices")
        self.assertEqual(definition.filters[1], {"id": "total", "min": 500000})

        run = self.client.post(f"/v1/exports/definitions/{definition.pk}/run/", {}, format="json")
        self.assertIn(run.status_code, (200, 201), run.content)
        self.assertEqual(ExportRun.objects.get(definition=definition).row_count, 2)

    def test_renaming_an_export_does_not_re_judge_filters_it_was_not_sent(self):
        """A stored value is judged when it is written, not on every later edit:
        a rename must not be blocked by a filter the caller never touched."""
        definition = self.make_definition(filters=self._filters(min="not a number"))
        response = self.client.patch(
            f"/v1/exports/definitions/{definition.pk}/?entity={self.entity.code}",
            {"name": "Renamed"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)


@tag("slow")
class MoneyFiltersToNairaMigrationTests(RewoundSchemaTestCase):
    """0006 rewrites stored money-filter bounds from kobo to naira, and back.

    Built through the historical models at 0005. A recipe and a run on a dataset
    with a money filter are converted; a filter that is not money, and a filter
    with the same id on a dataset whose ``total`` is not money, are left alone.
    """

    APP = "vs_exports"
    BEFORE = "0005_exportdownload_access_kind"
    AFTER = "0006_money_filters_in_naira"

    def _seed(self):
        from django.contrib.auth import get_user_model

        from vs_tenants.models import Tenant

        tenant = Tenant.objects.get(slug="codex")
        owner = get_user_model().objects.create_user(
            email="migrate@test.com", password="testpass123", status="ACTIVE",
            first_name="Migrate", last_name="Tester", tenant=tenant,
        )
        old = self.historical_apps(self.BEFORE)
        Definition = old.get_model("vs_exports", "ExportDefinition")
        Run = old.get_model("vs_exports", "ExportRun")
        filters = [
            {"id": "invoice_date", "start": "2026-01-01", "end": "2026-01-31"},
            {"id": "total", "min": 5_000_000, "max": 5_000_050},
            {"id": "status", "values": ["POSTED"]},
        ]
        recipe = Definition.objects.create(
            tenant_id=tenant.pk, dataset_key="finance.customer_invoices",
            name="Big invoices", columns=["document_number"], filters=filters,
            owner_id=owner.pk,
        )
        claims = Definition.objects.create(
            tenant_id=tenant.pk, dataset_key="finance.expense_claims",
            name="Big claims", columns=["document_number"],
            filters=[{"id": "total", "max": 99}], owner_id=owner.pk,
        )
        elsewhere = Definition.objects.create(
            tenant_id=tenant.pk, dataset_key="admin.users",
            name="Not money", columns=["email"],
            filters=[{"id": "total", "min": 5_000_000}], owner_id=owner.pk,
        )
        run = Run.objects.create(
            reference="XR-MIGRATE-1", tenant_id=tenant.pk, definition_id=recipe.pk,
            frozen_config={"dataset_key": "finance.customer_invoices", "filters": filters},
            requested_by_id=owner.pk,
        )
        return recipe.pk, claims.pk, elsewhere.pk, run.pk

    def test_money_bounds_move_to_naira_and_back(self):
        recipe, claims, elsewhere, run = self._seed()

        self.migrate_to(self.AFTER)
        new = self.historical_apps(self.AFTER)
        Definition = new.get_model("vs_exports", "ExportDefinition")
        Run = new.get_model("vs_exports", "ExportRun")
        converted = Definition.objects.get(pk=recipe).filters
        self.assertEqual(converted[1], {"id": "total", "min": 50000, "max": "50000.50"})
        self.assertEqual(converted[0]["start"], "2026-01-01")
        self.assertEqual(converted[2], {"id": "status", "values": ["POSTED"]})
        self.assertEqual(Definition.objects.get(pk=claims).filters,
                         [{"id": "total", "max": "0.99"}])
        self.assertEqual(Definition.objects.get(pk=elsewhere).filters,
                         [{"id": "total", "min": 5_000_000}])
        self.assertEqual(Run.objects.get(pk=run).frozen_config["filters"][1],
                         {"id": "total", "min": 50000, "max": "50000.50"})

        self.migrate_to(self.BEFORE)
        old = self.historical_apps(self.BEFORE)
        Definition = old.get_model("vs_exports", "ExportDefinition")
        Run = old.get_model("vs_exports", "ExportRun")
        self.assertEqual(Definition.objects.get(pk=recipe).filters[1],
                         {"id": "total", "min": 5_000_000, "max": 5_000_050})
        self.assertEqual(Definition.objects.get(pk=claims).filters,
                         [{"id": "total", "max": 99}])
        self.assertEqual(Definition.objects.get(pk=elsewhere).filters,
                         [{"id": "total", "min": 5_000_000}])
        self.assertEqual(Run.objects.get(pk=run).frozen_config["filters"][1],
                         {"id": "total", "min": 5_000_000, "max": 5_000_050})
