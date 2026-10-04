"""Branch scope reaches both sides of procurement's period-close checks."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .close_checks import ap_reconciled, grir_explained


class BranchCloseCheckScopeTests(SimpleTestCase):
    def test_ap_check_scopes_the_subledger_and_control_to_the_branch(self):
        result = SimpleNamespace(is_reconciled=True, subledger_total=0, control_total=0)
        with patch("vs_procurement.models.Vendor.objects.filter") as vendors, \
                patch("vs_procurement.reports.reconcile_ap", return_value=result) as reconcile:
            vendors.return_value.exists.return_value = True
            ap_reconciled(SimpleNamespace(), SimpleNamespace(), branch=17)
        self.assertEqual(reconcile.call_args.kwargs["branch_scope"].branch_ids, frozenset({17}))

    def test_grir_check_scopes_the_control_to_the_branch(self):
        with patch("vs_procurement.models.Vendor.objects.filter") as vendors, \
                patch("vs_procurement.reports.grir_balance", return_value=0) as balance:
            vendors.return_value.exists.return_value = True
            grir_explained(SimpleNamespace(), SimpleNamespace(), branch=23)
        self.assertEqual(balance.call_args.kwargs["branch_scope"].branch_ids, frozenset({23}))
