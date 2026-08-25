"""Procurement exports read a null branch exclusively, as its screens do.

The opposite of finance, and both are right for what they describe: a purchase
belongs to one place, so an entity-wide purchase is a scope of its own that a
branch-pinned buyer is not in rather than a row shared with them. That is what
``vs_procurement/views/base.py`` already does on the screens these datasets
mirror, and an export that disagreed with its own screen would be the bug this
narrowing exists to prevent.

These live here rather than in ``vs_exports`` because the engine may not import
a domain app - ``CatalogueRegistrationTests.test_the_engine_never_imports_a_domain_app``
enforces it - and because this is where somebody changing procurement's reading
will look.
"""
from __future__ import annotations

from django.test import TestCase

from vs_exports.catalogue import ScopeContext, get_dataset
from vs_finance.models import LedgerEntity
from vs_procurement.models import Vendor
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)


class ProcurementExportBranchScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        role = make_role(cls.school, name="Storekeeper", key="branch_admin")
        make_role_permission(
            role,
            make_permission("procurement.vendor.view", scope=PermissionScope.TENANT),
        )
        cls.buyer = make_school_admin(
            None, email="store@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.buyer, role, branch=cls.lekki)
        cls.head_office = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.head_office, role, branch=None)

        cls.entity = LedgerEntity.objects.filter(tenant=cls.tenant).first()
        if cls.entity is None:
            cls.entity = LedgerEntity.objects.create(
                tenant=cls.tenant, name="Brightfield Books",
                code="BFBOOKS", number_code="BFB",
            )

    def setUp(self):
        Vendor.objects.create(
            entity=self.entity, name="Shared Vendor", code="V-SHARED", branch=None,
        )
        Vendor.objects.create(
            entity=self.entity, name="Lekki Vendor", code="V-LEKKI", branch=self.lekki,
        )
        Vendor.objects.create(
            entity=self.entity, name="Ikeja Vendor", code="V-IKEJA", branch=self.ikeja,
        )

    def names(self, key, user):
        return {
            v.name for v in get_dataset(key).base(
                ScopeContext(tenant=self.tenant, entity=self.entity, user=user),
            )
        }

    def test_an_entity_wide_vendor_is_not_the_branch_buyers_to_export(self):
        self.assertEqual(self.names("procurement.vendors", self.buyer), {"Lekki Vendor"})

    def test_head_office_still_exports_everything(self):
        self.assertEqual(
            self.names("procurement.vendors", self.head_office),
            {"Shared Vendor", "Lekki Vendor", "Ikeja Vendor"},
        )

    def test_a_buyer_never_sees_another_branchs_rows(self):
        """The case the whole change exists for.

        Before this, a Lekki storekeeper who could not open Ikeja's purchase
        screens could still export a file containing every one of their orders,
        with quantities, prices and vendors in it.
        """
        self.assertNotIn("Ikeja Vendor", self.names("procurement.vendors", self.buyer))

    def test_every_procurement_dataset_narrows(self):
        """Enumerated rather than sampled, so one added later is caught here."""
        import inspect

        from vs_procurement import export_datasets as proc

        for name, fn in vars(proc).items():
            if not name.startswith("_") or not inspect.isfunction(fn):
                continue
            if name.startswith("_translate") or "scope" not in inspect.signature(fn).parameters:
                continue
            source = inspect.getsource(fn)
            self.assertIn(
                "narrow_to_caller_branches", source,
                f"vs_procurement.export_datasets.{name} does not narrow by branch",
            )
            self.assertIn(
                "inclusive=False", source,
                f"{name} must read a null branch exclusively, as its screens do",
            )
