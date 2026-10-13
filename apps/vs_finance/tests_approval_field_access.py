"""Field Access on the finance approval cards.

An approval document is snapshotted when it is submitted and read later by
whoever approves it, so what a given approver is shown of a bank transaction,
a transfer between accounts, a petty cash return, an inter-branch transfer or a
doubtful-debt provision is decided on the way out, from that approver's own
role switches (:func:`vs_workflow.presentation.summary_for_reader` and
:func:`vs_workflow.presentation.for_reader`).

Corona Group runs Ikeja, Lekki and Yaba; Single Site runs one branch; Rival
Group is a third school. Every case drives the real instance endpoints as an
approver who holds one role, with exactly one registered field closed, and
checks both what disappears (the row and its figure, wherever the card restates
it) and what stays (the bank account, fund and branch names, document numbers).
"""
from __future__ import annotations

import itertools
import json

from django.contrib.contenttypes.models import ContentType

from core.test_utils import TenantAPIClient
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import install_declared_fields, set_field_access
from vs_workflow.constants import WorkflowInstanceStatus
from vs_workflow.handlers import get_handler
from vs_workflow.models import WorkflowInstance, WorkflowTemplate
from vs_workflow.services.submission import document_snapshot

from .constants import InterBranchTransferKind, PettyCashReturnKind
from .models import (
    Account, BankTransaction, BankTransfer, DoubtfulDebtProvision, DoubtfulDebtProvisionLine,
    InterBranchTransfer, PettyCashFund, PettyCashReturn,
)
from .money import format_naira
from .tests_inter_branch import JAN_15, _InterBranchFixture

_seq = itertools.count(1)

RESOURCES = (
    "finance.banktransaction", "finance.banktransfer", "finance.pettycash_return",
    "finance.interbranch", "finance.provision",
)


def naira(kobo: int) -> str:
    return format_naira(kobo)


# What each card restates from a registered field: the field, the rows or
# columns it governs, and the figures or words that must not survive its
# closing. ``always`` are the labels that show whatever the switches say.
SPECS = {
    "banktransaction": {
        "fields": {
            "amount": (["Amount"], ["amount"]),
            "direction": (["Direction"], []),
            "counter_account": (["Other side"], ["counter"]),
            "transaction_date": (["Date"], []),
            "narration": (["Narration"], ["narration"]),
            "reference": (["Reference"], ["reference"]),
        },
        "always": ["Bank account"],
    },
    "banktransfer": {
        "fields": {
            "amount": (["Amount"], ["amount"]),
            "transfer_date": (["Date"], []),
            "narration": (["Narration"], ["narration"]),
            "reference": (["Reference"], ["reference"]),
        },
        "always": ["From", "To"],
    },
    "pettycash_return": {
        "fields": {
            "kind": (["Kind"], []),
            "counted_amount": (["Counted"], ["counted"]),
            "book_balance": (["On the books"], ["book"]),
            "difference": (["Short", "Over"], ["shortage"]),
            "difference_reason": (["Why the count differs"], ["why"]),
            "amount": (["Banked"], ["amount"]),
            "new_float_amount": (["New float"], ["float"]),
            "return_date": (["Date"], []),
            "reference": (["Reference"], ["reference"]),
        },
        "always": ["Fund", "Into"],
    },
    "interbranch": {
        "fields": {
            "kind": (["Kind"], []),
            "amount": (["Amount"], ["amount"]),
            "transaction_date": (["Date"], []),
            "purpose": (["Purpose"], ["purpose"]),
            "repay_by": (["Repay by"], []),
            "reference": (["Reference"], ["reference"]),
        },
        "always": ["From", "To"],
    },
    "provision": {
        "fields": {
            "as_of": (["Aged to"], []),
            "required_total": (["Allowance required"], ["required_total"]),
            "movement_total": (["Change to the allowance"], ["movement_total"]),
            "narration": (["Narration"], ["narration"]),
            "required": (["Required"], ["line_required"]),
            "current": (["Held"], ["line_current"]),
            "movement": (["Change"], ["line_movement"]),
        },
        "always": ["Branch"],
    },
}


class _ApprovalCardFixture(_InterBranchFixture):
    """One document of each kind in each shape of school, each with a request open on it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.field_keys = install_declared_fields(*RESOURCES)
        cls.requester = cls.user_for(cls.tenant, "requester@approvals.test")
        cls.solo_requester = cls.user_for(cls.solo_tenant, "requester@solo-approvals.test")
        cls.rival_requester = cls.user_for(cls.rival_tenant, "requester@rival-approvals.test")
        # kind -> {shape: (document, {token: text that must vanish with its field})}
        cls.cards = {}
        for shape, books, branch, bank, tenant in (
            ("multi", cls.books, cls.ikeja, cls.ikeja_bank, cls.tenant),
            ("solo", cls.solo_books, cls.solo_main, cls.solo_bank, cls.solo_tenant),
        ):
            cls.build_cards(shape, books, branch, bank, tenant)
        cls.build_interbranch()

    @classmethod
    def ledger(cls, books, code):
        return Account.objects.get(entity=books, code=code)

    @classmethod
    def second_bank(cls, books, branch, code="1190"):
        return cls.bank(books, code, f"{branch.name} Savings", branch)

    @classmethod
    def build_cards(cls, shape, books, branch, bank, tenant):
        savings = cls.second_bank(books, branch)
        txn = BankTransaction.objects.create(
            entity=books, branch=branch, bank_account=bank, direction="IN",
            amount=4_123_456, counter_account=cls.ledger(books, "3100"),
            transaction_date=JAN_15, narration="Owner capital drive", reference="TXN-REF-77",
        )
        transfer = BankTransfer.objects.create(
            entity=books, branch=branch, from_account=bank, to_account=savings,
            amount=2_345_678, transfer_date=JAN_15, narration="Move to savings",
            reference="BTR-REF-12",
        )
        fund = PettyCashFund.objects.create(
            entity=books, branch=branch, name=f"{branch.name} front desk",
            gl_account=cls.ledger(books, "1110"), float_amount=10_000_000,
        )
        petty = PettyCashReturn.objects.create(
            entity=books, branch=branch, fund=fund, kind=PettyCashReturnKind.REDUCE,
            bank_account=bank,
            return_date=JAN_15, counted_amount=9_654_321, book_balance=9_876_543,
            amount=5_432_100, previous_float_amount=10_000_000, new_float_amount=4_567_890,
            difference_reason="Two receipts went missing", reference="PCR-REF-5",
        )
        provision = DoubtfulDebtProvision.objects.create(
            entity=books, as_of=JAN_15, narration="Termly allowance review",
            required_total=3_141_592, movement_total=1_618_033,
        )
        if shape == "multi":
            lines = [(branch, 2_000_001, 1_000_002, 999_999),
                     (cls.lekki, 1_141_591, 523_557, 618_034)]
        else:
            lines = [(branch, 3_141_592, 1_523_559, 1_618_033)]
        for line_branch, required, current, movement in lines:
            DoubtfulDebtProvisionLine.objects.create(
                provision=provision, branch=line_branch, required=required,
                current=current, movement=movement,
            )
        first = lines[0]
        cls.cards.setdefault("banktransaction", {})[shape] = (txn, tenant, {
            "amount": naira(4_123_456), "counter": txn.counter_account.name,
            "narration": "Owner capital drive", "reference": "TXN-REF-77",
        })
        cls.cards.setdefault("banktransfer", {})[shape] = (transfer, tenant, {
            "amount": naira(2_345_678), "narration": "Move to savings",
            "reference": "BTR-REF-12",
        })
        cls.cards.setdefault("pettycash_return", {})[shape] = (petty, tenant, {
            "counted": naira(9_654_321), "book": naira(9_876_543), "shortage": naira(222_222),
            "why": "Two receipts went missing", "amount": naira(5_432_100),
            "float": naira(4_567_890), "reference": "PCR-REF-5",
        })
        cls.cards.setdefault("provision", {})[shape] = (provision, tenant, {
            "required_total": naira(3_141_592), "movement_total": naira(1_618_033),
            "narration": "Termly allowance review", "line_required": naira(first[1]),
            "line_current": naira(first[2]), "line_movement": naira(first[3]),
        })

    @classmethod
    def build_interbranch(cls):
        transfer = InterBranchTransfer.objects.create(
            entity=cls.books, kind=InterBranchTransferKind.CASH, branch=cls.ikeja, to_branch=cls.lekki,
            from_bank_account=cls.ikeja_bank, to_bank_account=cls.lekki_bank,
            amount=7_654_321, transfer_date=JAN_15, purpose="Generator diesel",
            reference="IBT-REF-9",
        )
        cls.cards["interbranch"] = {"multi": (transfer, cls.tenant, {
            "amount": naira(7_654_321), "purpose": "Generator diesel", "reference": "IBT-REF-9",
        })}

    # -- people and requests -------------------------------------------------- #

    @classmethod
    def approver(cls, tenant, *, hide=(), restrict=True):
        """A user who may view approvals, with the named registered fields closed.

        ``restrict=False`` leaves the role without any field row, which is a
        school that never opened Field Access: every unregistered-by-sensitivity
        field is open to every role.
        """
        name = f"wfa-{next(_seq)}"
        user = cls.grant(
            cls.user_for(tenant, f"{name}@approvals.test"), "workflow.instance.view",
            tenant=tenant, role_key=name,
        )
        if restrict:
            role = TenantRoleTemplate.objects.get(tenant=tenant, key=name)
            set_field_access(role, *cls.field_keys, read=True, write=True)
            if hide:
                set_field_access(role, *hide, read=False, write=False)
        return user

    def request_for(self, document, tenant, requester):
        handler = get_handler(document.workflow_document_type)
        summary, details = document_snapshot(handler, document, document.workflow_document_type)
        template = WorkflowTemplate.objects.create(
            tenant=tenant, branch=None, document_type=document.workflow_document_type,
            code=f"card-{next(_seq)}", name="Card route")
        return WorkflowInstance.all_objects.create(
            tenant=tenant, branch=document.branch, template=template,
            document_content_type=ContentType.objects.get_for_model(type(document)),
            document_object_id=str(document.pk), document_type=document.workflow_document_type,
            status=WorkflowInstanceStatus.IN_PROGRESS, requested_by=requester,
            document_summary=summary, document_details=details)

    def requester_of(self, tenant):
        return {self.tenant: self.requester, self.solo_tenant: self.solo_requester,
                self.rival_tenant: self.rival_requester}[tenant]

    def read(self, user, instance):
        response = TenantAPIClient(user=user).get(f"/v1/workflow/instances/{instance.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    @staticmethod
    def labels(card) -> set:
        """Every row label and table column label on an approval card."""
        found = {field["label"] for field in card["document_summary"].get("fields", [])}
        for section in card["document_details"].get("sections", []):
            if section["kind"] == "fields":
                found.update(item["label"] for item in section["items"])
            elif section["kind"] == "table":
                found.update(column["label"] for column in section["columns"])
        return found

    @staticmethod
    def dump(card) -> str:
        return json.dumps(card, default=str, ensure_ascii=False)


class ApprovalCardFieldAccessTests(_ApprovalCardFixture):
    """Closing one registered field removes that value from the card, and nothing else."""

    def test_an_approver_with_every_field_open_sees_the_whole_card(self):
        for kind, shapes in self.cards.items():
            for shape, (document, tenant, tokens) in shapes.items():
                with self.subTest(kind=kind, shape=shape):
                    instance = self.request_for(document, tenant, self.requester_of(tenant))
                    card = self.read(self.approver(tenant), instance)
                    spec = SPECS[kind]
                    expected = set(spec["always"])
                    for labels, _ in spec["fields"].values():
                        expected.update(labels)
                    self.assertTrue(expected <= self.labels(card), expected - self.labels(card))
                    for text in tokens.values():
                        self.assertIn(text, self.dump(card))

    def test_a_role_with_no_field_rows_at_all_sees_the_whole_card(self):
        document, tenant, tokens = self.cards["banktransfer"]["multi"]
        instance = self.request_for(document, tenant, self.requester)
        card = self.read(self.approver(tenant, restrict=False), instance)
        for text in tokens.values():
            self.assertIn(text, self.dump(card))

    def test_closing_a_field_removes_its_rows_and_figures_and_nothing_else(self):
        for kind, shapes in self.cards.items():
            for shape, (document, tenant, tokens) in shapes.items():
                instance = self.request_for(document, tenant, self.requester_of(tenant))
                baseline = self.labels(self.read(self.approver(tenant), instance))
                for field, (governed, token_names) in SPECS[kind]["fields"].items():
                    with self.subTest(kind=kind, shape=shape, field=field):
                        hidden = self.approver(tenant, hide=[f"finance.{kind}.{field}"])
                        card = self.read(hidden, instance)
                        self.assertEqual(self.labels(card), baseline - set(governed))
                        dump = self.dump(card)
                        # A figure two fields restate (a one-branch school's
                        # only line equals its total) stays while the other shows.
                        still_shown = {
                            text for name, text in tokens.items() if name not in token_names
                        }
                        for name in token_names:
                            if tokens[name] not in still_shown:
                                self.assertNotIn(tokens[name], dump)
                        for text in still_shown:
                            self.assertIn(text, dump)

    def test_closing_every_field_leaves_only_the_identifying_labels(self):
        for kind, shapes in self.cards.items():
            for shape, (document, tenant, tokens) in shapes.items():
                with self.subTest(kind=kind, shape=shape):
                    instance = self.request_for(document, tenant, self.requester_of(tenant))
                    every = [f"finance.{kind}.{name}" for name in SPECS[kind]["fields"]]
                    card = self.read(self.approver(tenant, hide=every), instance)
                    self.assertEqual(self.labels(card), set(SPECS[kind]["always"]))
                    dump = self.dump(card)
                    for text in tokens.values():
                        self.assertNotIn(text, dump)

    def test_the_stored_snapshot_keeps_the_values_and_names_the_key_that_governs_each(self):
        document, tenant, tokens = self.cards["pettycash_return"]["multi"]
        instance = self.request_for(document, tenant, self.requester)
        every = [f"finance.pettycash_return.{name}" for name in SPECS["pettycash_return"]["fields"]]
        self.read(self.approver(tenant, hide=every), instance)
        stored = WorkflowInstance.all_objects.get(pk=instance.pk)
        self.assertIn(tokens["amount"], self.dump(stored.document_summary))
        keyed = [f for f in stored.document_summary["fields"] if f.get("access")]
        self.assertIn("finance.pettycash_return.amount", {f["access"] for f in keyed})
        self.assertEqual(stored.document_summary["subtitle"], "Petty cash return")

    def test_names_show_whatever_the_switches_say(self):
        """A bank account, a fund, a branch and a document number identify the document."""
        for kind, shape, names in (
            ("banktransaction", "multi", ["Ikeja GTBank"]),
            ("banktransfer", "multi", ["Ikeja GTBank", "Ikeja Branch Savings"]),
            ("pettycash_return", "multi", ["Ikeja Branch front desk", "Ikeja GTBank"]),
            ("interbranch", "multi", ["Ikeja Branch", "Lekki Branch", "Ikeja GTBank", "Lekki Access"]),
            ("provision", "multi", ["Ikeja Branch"]),
            ("banktransaction", "solo", ["Main GTBank"]),
            ("pettycash_return", "solo", ["Main Branch front desk"]),
            ("provision", "solo", ["Main Branch"]),
        ):
            document, tenant, _ = self.cards[kind][shape]
            instance = self.request_for(document, tenant, self.requester_of(tenant))
            every = [f"finance.{kind}.{name}" for name in SPECS[kind]["fields"]]
            card = self.read(self.approver(tenant, hide=every), instance)
            dump = self.dump(card)
            with self.subTest(kind=kind, shape=shape):
                for name in names:
                    self.assertIn(name, dump)
                self.assertEqual(card["document_title"], str(document.document_number or document.pk))

    def test_closing_the_bank_account_fields_does_not_hide_the_account_name(self):
        document, tenant, _ = self.cards["interbranch"]["multi"]
        instance = self.request_for(document, tenant, self.requester)
        hidden = self.approver(tenant, hide=[
            "finance.interbranch.source_bank_account",
            "finance.interbranch.destination_bank_account",
        ])
        dump = self.dump(self.read(hidden, instance))
        self.assertIn("Ikeja GTBank", dump)
        self.assertIn("Lekki Access", dump)

    def test_the_queue_row_never_carries_a_figure(self):
        """The row an approver scans names the document, and holds nothing a switch governs."""
        for kind, shapes in self.cards.items():
            document, tenant, tokens = shapes["multi"]
            self.request_for(document, tenant, self.requester)
        listing = TenantAPIClient(user=self.approver(self.tenant)).get("/v1/workflow/instances/")
        self.assertEqual(listing.status_code, 200, listing.data)
        dump = self.dump(listing.data)
        for kind, shapes in self.cards.items():
            for text in shapes["multi"][2].values():
                self.assertNotIn(text, dump)

    def test_the_card_subtitle_never_names_a_registered_value(self):
        document, tenant, _ = self.cards["pettycash_return"]["multi"]
        for kind in (PettyCashReturnKind.REDUCE, PettyCashReturnKind.CLOSE):
            document.kind = kind
            summary, _ = document_snapshot(
                get_handler("finance.petty_cash_return"), document, "finance.petty_cash_return")
            self.assertEqual(summary["subtitle"], "Petty cash return")
            self.assertEqual(summary["title"], str(document.document_number or document.pk))


class ApprovalCardTenantIsolationTests(_ApprovalCardFixture):
    """One school's switches never reach another's card, and its cards are never served."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        rival_bank = cls.bank(cls.rival_books, "1151", "Rival GTBank", cls.rival_branch)
        rival_savings = cls.second_bank(cls.rival_books, cls.rival_branch)
        cls.rival_transfer = BankTransfer.objects.create(
            entity=cls.rival_books, branch=cls.rival_branch, from_account=rival_bank,
            to_account=rival_savings, amount=2_345_678, transfer_date=JAN_15,
            narration="Move to savings",
        )

    def test_a_closed_switch_in_one_school_does_not_hide_the_figure_in_another(self):
        mine, tenant, _ = self.cards["banktransfer"]["multi"]
        mine_instance = self.request_for(mine, tenant, self.requester)
        rival_instance = self.request_for(self.rival_transfer, self.rival_tenant, self.rival_requester)
        closed = self.approver(self.tenant, hide=["finance.banktransfer.amount"])
        open_rival = self.approver(self.rival_tenant)
        self.assertNotIn(naira(2_345_678), self.dump(self.read(closed, mine_instance)))
        self.assertIn(naira(2_345_678), self.dump(self.read(open_rival, rival_instance)))

    def test_an_approver_never_reaches_another_schools_card(self):
        rival_instance = self.request_for(self.rival_transfer, self.rival_tenant, self.rival_requester)
        response = TenantAPIClient(user=self.approver(self.tenant)).get(
            f"/v1/workflow/instances/{rival_instance.pk}/")
        self.assertEqual(response.status_code, 404, response.data)
