"""Guard: every RBAC key enforced by a finance/procurement/payments view must
be registered by its app permission seed.

This catches the class of bug where a view gates on ``rbac_permission =
"finance.foo.bar"`` but no seed ever creates that Permission row — leaving the
endpoint reachable only by the Vision super admin (who bypasses RBAC) and
ungrantable through normal role administration.
"""
from __future__ import annotations

import pathlib
import re
from importlib import import_module

from django.core.management import call_command
from django.test import TestCase

# Matches a quoted "module.resource.action" key for the three finance apps.
# Many views compute rbac_permission dynamically from request.method (a
# @property), so static class introspection can't see both branches — scanning
# the view source for the literal keys is the reliable way to enumerate them.
_KEY_RE = re.compile(r"""["']((?:finance|procurement|payments)\.[a-z_]+\.[a-z_]+)["']""")


def _keys_for(app_module_name, prefix):
    """Collect every rbac key literal referenced in an app's view modules."""
    mod = import_module(app_module_name)
    app_dir = pathlib.Path(mod.__file__).resolve().parent
    keys: set[str] = set()
    for path in sorted(app_dir.glob("views*.py")):
        for match in _KEY_RE.finditer(path.read_text(encoding="utf-8")):
            if match.group(1).startswith(prefix):
                keys.add(match.group(1))
    return keys


class AppPermissionSeedCoverageTest(TestCase):
    """Each app seed must register every key its own views enforce."""

    @classmethod
    def setUpTestData(cls):
        # Actions first (key generation needs the PermissionAction rows), then
        # the three app seeds. Platform roles are absent in the test DB, so the
        # grant step no-ops with a warning — registration still happens.
        call_command("seed_actions", verbosity=0)
        call_command("seed_finance_permissions", verbosity=0)
        call_command("seed_procurement_permissions", verbosity=0)
        call_command("seed_payments_permissions", verbosity=0)

    def _assert_keys_registered(self, app_module_name, expected_module_prefix):
        from vs_rbac.models import Permission

        view_keys = _keys_for(app_module_name, expected_module_prefix)
        self.assertTrue(
            view_keys,
            f"No rbac_permission keys discovered in {app_module_name} views.",
        )
        registered = set(Permission.objects.values_list("key", flat=True))
        missing = sorted(k for k in view_keys if k not in registered)
        self.assertEqual(
            missing, [],
            f"{expected_module_prefix} view keys missing from the seeded registry: {missing}",
        )

    def test_finance_keys_registered(self):
        self._assert_keys_registered("vs_finance", "finance.")

    def test_procurement_keys_registered(self):
        self._assert_keys_registered("vs_procurement", "procurement.")

    def test_payments_keys_registered(self):
        self._assert_keys_registered("vs_payments", "payments.")

    def test_seeds_are_idempotent(self):
        from vs_rbac.models import Permission

        before = Permission.objects.count()
        call_command("seed_finance_permissions", verbosity=0)
        call_command("seed_procurement_permissions", verbosity=0)
        call_command("seed_payments_permissions", verbosity=0)
        self.assertEqual(Permission.objects.count(), before)

    def test_money_movement_keys_are_restricted(self):
        """Money-out / ledger-irreversible keys must carry a non-NORMAL sensitivity."""
        from vs_rbac.models import Permission

        critical = [
            "finance.journal.post", "finance.period.close", "finance.payrollrun.pay",
            "finance.tax.pay", "finance.refund.post",
            "procurement.vendor_payment.post", "procurement.goods_receipt.post",
            "payments.payout.create", "payments.collection.create",
        ]
        for key in critical:
            perm = Permission.objects.filter(key=key).first()
            self.assertIsNotNone(perm, f"{key} not registered.")
            self.assertTrue(perm.is_restricted, f"{key} should be restricted.")
            self.assertNotEqual(perm.sensitivity_level, "NORMAL", f"{key} should not be NORMAL.")
