"""Row narrowing per caller: the half of the boundary the engine was missing.

The Export Centre already narrowed *columns* per person - ``resolve_columns``
runs at build time to shape the picker and again at run time to shape the file.
It did not narrow *rows*, so a branch-pinned caller holding an export key could
export sites whose screens answer 404 for them. These pin the fix at the engine,
because the gap was never one module's: every branch-scoped dataset had it.

Only ``vs_finance`` appears here, and deliberately. It is the engine's reference
integration and the one domain ``tests.py`` already reads, so using it keeps
this file inside the rule
:meth:`CatalogueRegistrationTests.test_the_engine_never_imports_a_domain_app`
enforces. Procurement and academics assert their own readings in their own
suites, which is also where somebody changing them will look.
"""
from __future__ import annotations

from django.test import TestCase

from vs_exports.catalogue import ScopeContext, narrow_to_caller_branches
from vs_finance.models import Customer, LedgerEntity
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


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        cls.role = make_role(cls.school, name="Branch Admin", key="branch_admin")
        make_role_permission(
            cls.role,
            make_permission("finance.invoice.view", scope=PermissionScope.TENANT),
        )
        cls.school_level = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.school_level, cls.role, branch=None)
        cls.lekki_head = make_school_admin(
            None, email="head@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_head, cls.role, branch=cls.lekki)

        # Built rather than looked up. An earlier cut asked for whichever entity
        # the school happened to have and skipped when there was none, so the
        # two cases that matter most never actually ran.
        cls.entity = LedgerEntity.objects.filter(tenant=cls.tenant).first()
        if cls.entity is None:
            cls.entity = LedgerEntity.objects.create(
                tenant=cls.tenant, name="Brightfield Books",
                code="BFBOOKS", number_code="BFB",
            )

    def setUp(self):
        self.shared = Customer.objects.create(
            entity=self.entity, name="Shared Payer", code="C-SHARED", branch=None,
        )
        self.mine = Customer.objects.create(
            entity=self.entity, name="Lekki Payer", code="C-LEKKI", branch=self.lekki,
        )
        self.theirs = Customer.objects.create(
            entity=self.entity, name="Ikeja Payer", code="C-IKEJA", branch=self.ikeja,
        )

    def names(self, user, **kwargs):
        qs = narrow_to_caller_branches(
            Customer.objects.filter(entity=self.entity),
            ScopeContext(tenant=self.tenant, entity=self.entity, user=user),
            **kwargs,
        )
        return {c.name for c in qs}


class NarrowingTests(_Base):
    def test_a_branch_caller_gets_the_shared_rows_plus_their_own(self):
        """The inclusive reading, and never another branch's."""
        self.assertEqual(
            self.names(self.lekki_head), {"Shared Payer", "Lekki Payer"},
        )

    def test_a_school_level_caller_gets_everything(self):
        """Narrowing must not restrict the people who need it least."""
        self.assertEqual(
            self.names(self.school_level),
            {"Shared Payer", "Lekki Payer", "Ikeja Payer"},
        )

    def test_the_exclusive_reading_drops_the_shared_rows(self):
        """What a document dataset wants, and a catalogue must not have.

        Offering both readings here is what stops procurement writing a second
        copy of this rule when it adopts it.
        """
        self.assertEqual(
            self.names(self.lekki_head, inclusive=False), {"Lekki Payer"},
        )

    def test_no_caller_means_no_narrowing_rather_than_no_rows(self):
        """A missing person is not a person with no branches.

        Conflating them would silently empty a system-triggered estimate, which
        reads as "this school has nothing" rather than as a bug.
        """
        self.assertEqual(
            self.names(None),
            {"Shared Payer", "Lekki Payer", "Ikeja Payer"},
        )

    def test_it_narrows_through_a_parent_when_given_a_prefix(self):
        """For rows that reach branch through a relation, not a column."""
        import datetime as dt

        from vs_finance.models import Invoice

        for customer in (self.mine, self.theirs, self.shared):
            Invoice.objects.create(
                entity=self.entity, customer=customer, branch=customer.branch,
                invoice_date=dt.date(2026, 9, 1),
            )
        # Narrowed through the customer rather than the invoice's own column,
        # which is the case a line or a posting is in: it reaches branch through
        # its parent and has no column of its own to filter.
        qs = narrow_to_caller_branches(
            Invoice.objects.filter(entity=self.entity),
            ScopeContext(
                tenant=self.tenant, entity=self.entity, user=self.lekki_head,
            ),
            prefix="customer__",
        )
        self.assertEqual(
            {i.customer.name for i in qs}, {"Lekki Payer", "Shared Payer"},
        )


class ScopeContextTests(_Base):
    def test_scope_context_defaults_user_to_none(self):
        """So every construction site that predates this keeps working."""
        self.assertIsNone(ScopeContext(tenant=self.tenant).user)

    def test_a_run_carries_the_person_it_executes_as(self):
        """Not whoever happened to click, when the two differ.

        A definition's owner is who a run reads as, and the engine already
        refuses to start if they are no longer active. Branch narrowing has to
        follow that same person or a file could hold rows its owner could not
        have asked for.
        """
        from vs_exports.models import ExportRun

        field_names = {f.name for f in ExportRun._meta.get_fields()}
        self.assertIn("requested_by", field_names)
        self.assertIn("definition", field_names)


class FinanceReadingTests(_Base):
    """Finance reads a null branch as shared, at every one of its call sites."""

    def rows(self, key, user):
        from vs_exports.catalogue import get_dataset

        return list(get_dataset(key).base(
            ScopeContext(tenant=self.tenant, entity=self.entity, user=user),
        ))

    def test_a_school_wide_customer_stays_visible_to_a_branch_caller(self):
        """A payer the school holds centrally is still the branch's to read.

        The exclusive reading here would hand a bursar a nearly empty file
        whenever the school posts its fees once for everybody, which is normal.
        """
        names = {c.name for c in self.rows("finance.customers", self.lekki_head)}
        self.assertEqual(names, {"Shared Payer", "Lekki Payer"})

    def test_every_finance_dataset_narrows(self):
        """Enumerated rather than sampled, so one added later is caught here."""
        import inspect

        from vs_finance import export_datasets as fin

        for name, fn in vars(fin).items():
            if not name.startswith("_") or not inspect.isfunction(fn):
                continue
            if name.startswith("_translate") or "scope" not in inspect.signature(fn).parameters:
                continue
            self.assertIn(
                "narrow_to_caller_branches", inspect.getsource(fn),
                f"vs_finance.export_datasets.{name} does not narrow by branch",
            )
