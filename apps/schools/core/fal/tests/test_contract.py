"""The contract itself: the envelope, the registry seam, and the fakes.

These are the tests a consuming module relies on without knowing it. If the
fakes and the Django adapters disagree, every consumer test written against a
fake is worthless, so several of these assert the two behave alike.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from schools.core.fal import FAL_CONTRACT_VERSION, contracts, ports, registry
from schools.core.fal.contracts import (
    Availability,
    BillLine,
    EntityHandle,
    FinanceResult,
    ProcDocRef,
    ProcDocType,
    ProcApprovalState,
    Unavailable,
)
from schools.core.fal.exceptions import (
    ApprovalNotParkedError,
    ApprovalTemplateMissingError,
    CrossBranchError,
    FALNotConfiguredError,
    OverrideNotPermittedError,
    ProcurementStateError,
)
from schools.core.fal.testing import (
    FakeEntityResolver,
    FakeFinanceReader,
    FakeProcurementActions,
    unavailable_finance_reader,
    unavailable_parent_payment_bridge,
    unavailable_procurement_reader,
)

LINES = (BillLine(description="Books", quantity=10, unit_price=1_000),)


class ContractShapeTests(SimpleTestCase):
    def test_the_version_is_declared(self):
        self.assertEqual(FAL_CONTRACT_VERSION, "1.1.3")

    def test_every_dto_is_frozen(self):
        """A consumer holds a snapshot, and cannot mutate finance through it."""
        mutable = [
            name for name, obj in vars(contracts).items()
            if dataclasses.is_dataclass(obj) and isinstance(obj, type)
            and not obj.__dataclass_params__.frozen
        ]
        self.assertEqual(mutable, [])

    def test_a_returned_handle_cannot_be_edited(self):
        handle = EntityHandle(
            entity_ref=1, school_ref=2, code="CORONA", name="Corona",
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            handle.code = "SOMETHING_ELSE"

    def test_an_unavailable_result_carries_no_value(self):
        result = FinanceResult.unavailable(Unavailable.BACKEND_TIMEOUT)

        self.assertIs(result.availability, Availability.UNAVAILABLE)
        self.assertIsNone(result.value)
        self.assertFalse(result.is_available)
        with self.assertRaises(ValueError):
            result.unwrap()

    def test_a_zero_is_available_and_not_confused_with_unavailable(self):
        """The distinction the whole envelope exists for."""
        result = FinanceResult.available(0)

        self.assertTrue(result.is_available)
        self.assertEqual(result.unwrap(), 0)

    def test_the_deferred_payment_types_are_not_part_of_the_surface(self):
        import schools.core.fal as fal

        for name in ("PaymentPort", "ApplyPaymentCommand", "PaymentApplication",
                     "Allocation", "AppliedInvoice"):
            self.assertNotIn(name, fal.__all__)
            self.assertFalse(hasattr(fal, name), name)

    def test_the_deferred_types_still_exist_for_the_v1_2_work(self):
        self.assertTrue(hasattr(ports, "PaymentPort"))
        self.assertTrue(hasattr(contracts, "ApplyPaymentCommand"))

    def test_the_contract_imports_without_django(self):
        """Ports and contracts are pure Python, so a consumer can type-check them.

        Run in a subprocess with no settings module, because importing Django
        models anywhere in that chain would only fail there.
        """
        code = (
            "import sys; sys.path.insert(0, 'apps');"
            "import schools.core.fal.contracts as c;"
            "import schools.core.fal.ports as p;"
            "import schools.core.fal.testing as t;"
            "assert 'django.db.models' not in sys.modules;"
            "print('clean')"
        )
        # The repository root, derived rather than written down. This was an
        # absolute path to one developer's machine, so the test passed there
        # and raised FileNotFoundError on every CI runner - green locally and
        # red in the only place that checks every branch.
        #
        # tests -> fal -> core -> schools -> apps -> the root, which is the
        # directory `sys.path.insert(0, "apps")` above is relative to.
        repo_root = Path(__file__).resolve().parents[5]
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            cwd=repo_root,
        )
        self.assertEqual(result.stdout.strip(), "clean", result.stderr)


class RegistryTests(TestCase):
    def setUp(self):
        super().setUp()
        registry.reset()
        self.addCleanup(registry.reset)

    def test_it_resolves_the_django_adapters_by_default(self):
        from schools.core.fal.adapters.django_finance import (
            DjangoEntityResolverAdapter,
        )

        self.assertIsInstance(registry.get_entity_resolver(),
                              DjangoEntityResolverAdapter)

    def test_the_guardian_link_now_resolves_from_the_student_roll(self):
        """The one setting that opened the parent portal.

        It defaulted to a resolver that refused every question while no student
        roll existed. Module 11 landed and this is the change that turned the
        payment bridge from shut to live.
        """
        from schools.core.fal.adapters.django_finance import (
            DjangoGuardianLinkAdapter,
        )

        self.assertIsInstance(registry.get_guardian_link(),
                              DjangoGuardianLinkAdapter)

    @override_settings(
        FAL_GUARDIAN_LINK=(
            "schools.core.fal.adapters.django_finance.DenyAllGuardianLinkAdapter"
        ),
    )
    def test_a_deployment_without_a_roll_can_still_fail_closed(self):
        """The old resolver is kept, and still refuses rather than saying no."""
        from schools.core.fal.exceptions import GuardianLinkNotConfigured

        with self.assertRaises(GuardianLinkNotConfigured):
            registry.get_guardian_link().owns("g-1", "s-1")

    def test_a_fake_can_be_injected_and_taken_back_out(self):
        fake = FakeFinanceReader(outstanding=120_000)
        registry.set_finance_reader(fake)

        self.assertIs(registry.get_finance_reader(), fake)

        registry.reset()
        self.assertIsNot(registry.get_finance_reader(), fake)

    @override_settings(FAL_FINANCE_READER="nowhere.at.all.Reader")
    def test_a_bad_setting_is_a_typed_error_not_an_import_error(self):
        with self.assertRaises(FALNotConfiguredError):
            registry.get_finance_reader()

    def test_there_is_no_payment_port_key(self):
        """PaymentPort is deferred to v1.2 and must not be wireable."""
        self.assertNotIn("FAL_PAYMENT_PORT", registry._DEFAULTS)


class FakeParityTests(SimpleTestCase):
    """The fakes reproduce the behaviour a consumer has to handle."""

    def test_provisioning_fakes_are_idempotent(self):
        fake = FakeEntityResolver()

        first = fake.provision_entity(1, code="CORONA", name="Corona").unwrap()
        second = fake.provision_entity(1, code="CORONA", name="Corona").unwrap()

        self.assertTrue(first.was_created)
        self.assertFalse(second.was_created)
        self.assertEqual(first.entity_ref, second.entity_ref)

    def test_submission_parks_when_nobody_holds_the_role(self):
        fake = FakeProcurementActions(seeded_entities={7})
        document = fake.raise_requisition(
            entity_ref=7, raiser_ref=1, lines=LINES,
        ).unwrap()

        submission = fake.submit_for_approval(
            document.ref, actor_ref=1,
        ).unwrap()

        self.assertTrue(submission.is_parked)
        self.assertIs(submission.approval_state, ProcApprovalState.PENDING)

    def test_appointing_an_approver_releases_without_resubmission(self):
        fake = FakeProcurementActions(seeded_entities={7})
        document = fake.raise_requisition(
            entity_ref=7, raiser_ref=1, lines=LINES,
        ).unwrap()
        fake.submit_for_approval(document.ref, actor_ref=1)
        self.assertTrue(fake.is_parked(document.ref.doc_ref))

        fake.grant_approver(7)

        self.assertFalse(fake.is_parked(document.ref.doc_ref))

    def test_a_school_with_no_rules_is_refused(self):
        fake = FakeProcurementActions()
        document = fake.raise_requisition(
            entity_ref=7, raiser_ref=1, lines=LINES,
        ).unwrap()

        with self.assertRaises(ApprovalTemplateMissingError):
            fake.submit_for_approval(document.ref, actor_ref=1)

    def test_the_override_refuses_the_same_three_ways_as_the_adapter(self):
        fake = FakeProcurementActions(seeded_entities={7}, override_users={9})
        document = fake.raise_requisition(
            entity_ref=7, raiser_ref=1, lines=LINES,
        ).unwrap()
        fake.submit_for_approval(document.ref, actor_ref=1)

        with self.assertRaises(OverrideNotPermittedError):
            fake.approve_without_review(document.ref, actor_ref=1, reason="Please.")
        with self.assertRaises(OverrideNotPermittedError):
            fake.approve_without_review(document.ref, actor_ref=9, reason="  ")

        fake.approve_without_review(document.ref, actor_ref=9, reason="Term starts.")
        self.assertEqual(len(fake.overrides), 1)

        with self.assertRaises(ApprovalNotParkedError):
            fake.approve_without_review(document.ref, actor_ref=9, reason="Again.")

    def test_a_branch_bound_user_cannot_raise_for_another_branch(self):
        fake = FakeProcurementActions(seeded_entities={7}, user_branches={1: 55})

        with self.assertRaises(CrossBranchError):
            fake.raise_requisition(
                entity_ref=7, raiser_ref=1, lines=LINES, branch_ref=66,
            )

    def test_a_school_level_user_raises_with_no_branch(self):
        fake = FakeProcurementActions(seeded_entities={7})

        document = fake.raise_requisition(
            entity_ref=7, raiser_ref=1, lines=LINES,
        ).unwrap()

        self.assertIsNone(document.ref.branch_ref)

    def test_a_bill_is_not_posted_before_it_is_approved(self):
        """Parity with the adapter, which the engine forces to work this way."""
        fake = FakeProcurementActions(seeded_entities={7})
        order = fake._new(ProcDocType.PURCHASE_ORDER, 7, None, 10_000)

        bill = fake.record_supplier_bill(
            order.ref, vendor_ref=3, actor_ref=1, lines=LINES,
            invoice_date=None,
        ).unwrap()

        self.assertEqual(bill.status, "DRAFT")
        with self.assertRaises(ProcurementStateError):
            fake.post_to_ledger(bill.ref, actor_ref=1)

    def test_another_entitys_document_is_refused(self):
        from schools.core.fal.exceptions import CrossTenantError

        fake = FakeProcurementActions(seeded_entities={7})
        document = fake.raise_requisition(
            entity_ref=7, raiser_ref=1, lines=LINES,
        ).unwrap()
        foreign = ProcDocRef(
            doc_ref=document.ref.doc_ref, doc_type=ProcDocType.REQUISITION,
            entity_ref=8,
        )

        with self.assertRaises(CrossTenantError):
            fake.submit_for_approval(foreign, actor_ref=1)

    def test_combined_balance_refuses_a_mixed_sibling_set(self):
        from schools.core.fal.exceptions import CrossTenantError

        reader = FakeFinanceReader(
            balances={"a": 100, "b": 200},
            student_schools={"a": 1, "b": 2},
        )

        with self.assertRaises(CrossTenantError):
            reader.combined_balance(("a", "b"))


class UnavailableFakeTests(SimpleTestCase):
    """A consumer must render an outage state, not a zero."""

    def test_every_reader_method_is_unavailable(self):
        reader = unavailable_finance_reader()

        for call in (
            lambda: reader.collections(1), lambda: reader.outstanding(1),
            lambda: reader.collection_rate(1), lambda: reader.debtor_count(1),
            lambda: reader.payment_trend(1), lambda: reader.ar_ageing(1),
            lambda: reader.fee_liability(1), lambda: reader.debtors(1),
            lambda: reader.fee_invoices(1), lambda: reader.payments(1),
            lambda: reader.fee_status("s"), lambda: reader.invoices_for("s"),
            lambda: reader.combined_balance(("s",)),
        ):
            result = call()
            self.assertFalse(result.is_available)
            self.assertEqual(result.reason, Unavailable.BACKEND_UNAVAILABLE)

    def test_the_procurement_reader_and_payment_bridge_too(self):
        self.assertFalse(unavailable_procurement_reader().snapshot(1).is_available)
        self.assertEqual(
            unavailable_parent_payment_bridge().receipt_for(1, guardian_ref="g").reason,
            Unavailable.GATEWAY_UNAVAILABLE,
        )
