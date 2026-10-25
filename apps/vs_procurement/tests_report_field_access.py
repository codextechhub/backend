"""Regression coverage for business values restated by procurement reports."""

from unittest.mock import patch

from django.test import SimpleTestCase

from .views.reports import _ap_totals, _omit_closed, _trim_dashboard


class ProcurementReportFieldAccessTests(SimpleTestCase):
    def test_ap_balances_remove_all_money_aliases(self):
        data = {
            "buckets": {"current": 100}, "total": 100, "net_total": 90,
            "outstanding": 100, "unallocated_credit": 10, "vendor_id": 1,
        }
        with patch("vs_procurement.views.reports.can_read", return_value=False):
            result = _ap_totals(None, data)
        self.assertEqual(result, {"vendor_id": 1})

    def test_derived_clearing_values_require_both_source_amounts(self):
        data = {"received_value": 100, "invoiced_value": 90,
                "open_value": 10, "reference": "GRN-1"}
        rules = {
            "procurement.goods_receipt.total_value": (
                "received_value", "invoiced_value", "open_value"),
            "procurement.vendor_invoice.total": ("invoiced_value", "open_value"),
        }
        with patch("vs_procurement.views.reports.can_read",
                   side_effect=lambda _request, key: key != "procurement.vendor_invoice.total"):
            result = _omit_closed(None, data, rules)
        self.assertEqual(result, {"received_value": 100, "reference": "GRN-1"})

    def test_dashboard_nested_values_follow_their_source_switches(self):
        data = {
            "kpis": {"spend": {"amount": 100}, "active_vendors": {"on_hold_count": 2}},
            "open_rfqs": {"items": [{"title": "Generator", "budget": 2500000}]},
            "scorecard": [{"name": "Adex", "score": 91, "grade": "A", "spend": 100}],
            "vendor_base": {"on_hold": 2, "awaiting_kyc": 3},
        }
        closed = {
            "procurement.vendor_invoice.total", "procurement.rfq.budget_estimate",
            "procurement.vendor_assessment.scores", "procurement.vendor.on_hold",
        }
        with patch("vs_procurement.views.reports.can_read",
                   side_effect=lambda _request, key: key not in closed):
            result = _trim_dashboard(None, data)
        self.assertNotIn("spend", result["kpis"])
        self.assertNotIn("on_hold_count", result["kpis"]["active_vendors"])
        self.assertNotIn("budget", result["open_rfqs"]["items"][0])
        self.assertEqual(result["scorecard"], [{"name": "Adex"}])
        self.assertNotIn("on_hold", result["vendor_base"])

    def test_stock_dashboard_drops_valuation_and_quantity_aliases(self):
        data = {
            "position": {"value": 84000, "items": 1, "stores": 1,
                         "below_reorder": 1, "idle_value": 84000},
            "by_store": [{"store": "Ikeja", "value": 84000}],
            "running_low": [{"name": "Paper", "on_hand": 20}],
            "issued": {"total": 84000, "items": []},
            "turns": {"times": 2.0},
            "adjustments": {"count": 1, "value": 84000},
            "movements": [{"item": "Paper", "quantity": -20, "reference": "ISS-1"}],
            "unbilled": {"count": 1, "amount": 84000},
        }
        closed = {
            "procurement.stock.stock_value", "procurement.stock.on_hand_qty",
            "procurement.stock.movement_value", "procurement.stock.movement_quantity",
            "procurement.goods_receipt.total_value",
        }
        with patch("vs_procurement.views.reports.can_read",
                   side_effect=lambda _request, key: key not in closed):
            result = _trim_dashboard(None, data)
        self.assertNotIn("value", result["position"])
        self.assertNotIn("items", result["position"])
        self.assertNotIn("running_low", result)
        self.assertNotIn("by_store", result)
        self.assertNotIn("issued", result)
        self.assertNotIn("turns", result)
        self.assertNotIn("value", result["adjustments"])
        self.assertNotIn("quantity", result["movements"][0])
        self.assertNotIn("amount", result["unbilled"])
