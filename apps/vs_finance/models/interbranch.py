"""Inter-branch transfers: who owes whom between a tenant's branches.

A tenant keeps one set of books, but every entry belongs to one branch. When
something passes between two branches (money, a customer's open balance, a share
of a cost, goods), each branch books its own side in its own journal, and the two
journals are linked through one :class:`InterBranchTransfer`. The rule "one entry,
one branch" therefore never has an exception, and the inter-branch balances
account nets to zero across all branches.

The services that write these rows live in :mod:`vs_finance.inter_branch`.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from ..constants import (
    DocType,
    InterBranchLegRole,
    InterBranchTransferKind,
    PaymentMethod,
    ReceivableMoveItemKind,
    RechargeBasis,
    SharedCostTreatment,
)
from ..money import MoneyField, format_naira
from .core import FinanceDocument, LedgerEntity, TimeStampedModel
from .gl import Account

__all__ = [
    "HeldForBranchReceipt",
    "InterBranchRecharge",
    "InterBranchRechargeLine",
    "InterBranchTransfer",
    "InterBranchTransferLeg",
    "ReceivableTransferItem",
    "SharedCostRule",
    "SharedCostRuleShare",
]


class HeldForBranchReceipt(FinanceDocument):
    """Money received into one branch's bank that belongs to another branch.

    Mrs Adeyemi pays 400k of her son's Lekki fees into Ikeja's account. Ikeja
    records it here, against the held-for-other-branches liability naming Lekki
    (``Dr Ikeja's bank, Cr held for other branches [Lekki]``). Ikeja cannot apply
    it to the Lekki invoice, because a receipt settles only its own branch's
    documents; a forwarded-receipt transfer moves the money to Lekki, where it is
    receipted against the customer and settles the invoice.

    ``branch`` is the branch whose bank received the money; ``for_branch`` the
    branch it belongs to. ``customer`` is who paid, and must be ``for_branch``'s
    customer or one shared by every branch.
    """

    DOC_TYPE = DocType.HELD_RECEIPT

    bank_account = models.ForeignKey(
        "BankAccount", on_delete=models.PROTECT, related_name="held_receipts",
    )
    for_branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="held_receipts_for",
    )
    customer = models.ForeignKey(
        "Customer", on_delete=models.PROTECT, related_name="held_receipts",
    )
    amount = MoneyField(help_text="Kobo received, always positive.")
    receipt_date = models.DateField()
    method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER,
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="held_receipts",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        constraints = FinanceDocument.Meta.constraints + [
            models.CheckConstraint(
                check=models.Q(amount__gt=0), name="ck_finance_heldreceipt_amount_positive",
            ),
            models.CheckConstraint(
                check=~models.Q(branch=models.F("for_branch")),
                name="ck_finance_heldreceipt_other_branch",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "receipt_date"]),
            models.Index(fields=["for_branch", "receipt_date"]),
        ]
        ordering = ["-receipt_date", "-id"]

    def __str__(self) -> str:
        return f"{self.document_number or self.pk}: {format_naira(int(self.amount))} for branch {self.for_branch_id}"


class SharedCostRule(TimeStampedModel):
    """The tenant's choice for one kind of cost a branch pays on behalf of others.

    The audit fee, the group insurance, the head office's software licence: one
    branch pays each from its own bank, and the tenant decides once per cost
    whether that branch **absorbs** it or **recharges** it to the others. A
    recharge splits by per-branch counts supplied when it is run (the default,
    for a cost that follows headcount) or by fixed percentages kept here
    (:class:`SharedCostRuleShare`, totalling 100), for a cost that does not.

    Configuration shared by every branch, so only a caller who reaches the whole
    tenant may change it.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="shared_cost_rules",
    )
    name = models.CharField(max_length=120)
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="shared_cost_rules",
        null=True, blank=True,
        help_text="The account the cost is booked to, offered as the recharge's default.",
    )
    treatment = models.CharField(
        max_length=10, choices=SharedCostTreatment.choices,
        default=SharedCostTreatment.ABSORB,
    )
    basis = models.CharField(
        max_length=12, choices=RechargeBasis.choices, default=RechargeBasis.COUNTS,
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="+", null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "name"], name="uniq_finance_sharedcostrule_entity_name",
            ),
        ]
        ordering = ["entity", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.treatment})"


class SharedCostRuleShare(TimeStampedModel):
    """One branch's fixed share of a rule's cost, in basis points (10000 is 100%)."""

    rule = models.ForeignKey(
        SharedCostRule, on_delete=models.CASCADE, related_name="shares",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="+",
    )
    percent_bps = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "branch"], name="uniq_finance_sharedcostshare_rule_branch",
            ),
            models.CheckConstraint(
                check=models.Q(percent_bps__lte=10000),
                name="ck_finance_sharedcostshare_bps_max",
            ),
        ]
        ordering = ["rule", "branch"]


class InterBranchRecharge(FinanceDocument):
    """A cost one branch paid, split across branches as inter-branch balances.

    ``branch`` is the paying branch. Each other branch's share becomes a RECHARGE
    :class:`InterBranchTransfer` from the paying branch to it: the paying branch
    takes the share off its expense and is owed it, and the owing branch books
    the expense and owes it. No cash moves; the owing branch settles later with
    a cash transfer. The paying branch's own share stays where it is.
    """

    DOC_TYPE = DocType.RECHARGE

    rule = models.ForeignKey(
        SharedCostRule, on_delete=models.PROTECT, related_name="recharges",
        null=True, blank=True,
    )
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="inter_branch_recharges",
    )
    amount = MoneyField(help_text="The whole cost being split, in kobo.")
    recharge_date = models.DateField()
    basis = models.CharField(max_length=12, choices=RechargeBasis.choices)
    narration = models.CharField(max_length=255)
    reference = models.CharField(max_length=64, blank=True, default="")

    class Meta(FinanceDocument.Meta):
        constraints = FinanceDocument.Meta.constraints + [
            models.CheckConstraint(
                check=models.Q(amount__gt=0), name="ck_finance_recharge_amount_positive",
            ),
        ]
        indexes = [models.Index(fields=["entity", "recharge_date"])]
        ordering = ["-recharge_date", "-id"]

    def __str__(self) -> str:
        return f"{self.document_number or self.pk}: {format_naira(int(self.amount))}"


class InterBranchRechargeLine(TimeStampedModel):
    """One branch's weight and share of a recharge.

    ``weight`` is the count supplied for the branch (pupils, staff, square
    metres) or its percentage in basis points. The paying branch's own line has
    no ``transfer``: its share is the part of the cost it keeps.
    """

    recharge = models.ForeignKey(
        InterBranchRecharge, on_delete=models.CASCADE, related_name="lines",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="+",
    )
    weight = models.BigIntegerField()
    amount = MoneyField(help_text="This branch's share, in kobo.")
    transfer = models.OneToOneField(
        "InterBranchTransfer", on_delete=models.PROTECT, related_name="recharge_line",
        null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["recharge", "branch"], name="uniq_finance_rechargeline_branch",
            ),
        ]
        ordering = ["recharge", "branch"]


class InterBranchTransfer(FinanceDocument):
    """Something passing between two branches, booked once on each side.

    ``branch`` is the sending branch, the one that gives (money, goods, a
    customer's open balance, a share of a cost), and ``to_branch`` the receiving
    one. Except for a forwarded receipt, the receiving branch then owes the
    sending branch ``amount``; a cash transfer the other way repays it. There is
    no interest: it is one company, and interest would only move profit between
    branches for show. ``repay_by`` is an optional date the branches agree.

    A receivable move carries a customer's whole position, which can owe either
    way: open bills the new branch now collects, against unapplied credit and
    unearned income the new branch now owes the customer. Its ``amount`` is
    everything it carried, added up without sign, and its
    :class:`ReceivableTransferItem` rows and journals say what each part was and
    who owes whom.

    Income given back is booked by a credit note or concession
    (``adjustment_entry``) that cancels a moved bill's income another branch
    booked. ``branch`` is the adjusting document's branch, whose side is a line
    of that document's own journal, so its leg carries no journal of its own;
    ``to_branch`` booked the income and books its side on its receiving leg.

    Every other kind posts two journals, one per branch, through its two
    :class:`InterBranchTransferLeg` rows. The cash kind on the sending side is
    ``Dr inter-branch [receiving], Cr sending bank``; on the receiving side
    ``Dr receiving bank, Cr inter-branch [sending]``. Both branches must be open
    on ``transfer_date``.

    A cash transfer starts one of two ways: the receiving branch requests an
    amount (``requested_by``), or the sending branch sends without one. Either
    way the sending branch approves and sends it through its own workflow route
    (``finance.inter_branch_transfer``, held at the sending branch), and the
    receiving branch confirms arrival (``received_by``). A request the sending
    branch will not meet is declined (CANCELLED).

    Visible to anyone whose reach includes either branch.
    """

    DOC_TYPE = DocType.INTER_BRANCH_TRANSFER
    workflow_document_type = "finance.inter_branch_transfer"
    workflow_amount_field = "amount"

    kind = models.CharField(max_length=20, choices=InterBranchTransferKind.choices)
    to_branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="inter_branch_transfers_in",
    )
    amount = MoneyField(help_text="Kobo passing from the sending to the receiving branch.")
    transfer_date = models.DateField()
    purpose = models.CharField(max_length=255)
    reference = models.CharField(max_length=64, blank=True, default="")
    repay_by = models.DateField(null=True, blank=True)
    from_bank_account = models.ForeignKey(
        "BankAccount", on_delete=models.PROTECT, related_name="inter_branch_transfers_out",
        null=True, blank=True,
    )
    to_bank_account = models.ForeignKey(
        "BankAccount", on_delete=models.PROTECT, related_name="inter_branch_transfers_in",
        null=True, blank=True,
    )
    customer = models.ForeignKey(
        "Customer", on_delete=models.PROTECT, related_name="inter_branch_transfers",
        null=True, blank=True,
        help_text="Whose money or open balance moved, for a forwarded receipt or a receivable move.",
    )
    held_receipt = models.ForeignKey(
        HeldForBranchReceipt, on_delete=models.PROTECT, related_name="forwards",
        null=True, blank=True,
    )
    receipt = models.OneToOneField(
        "Payment", on_delete=models.PROTECT, related_name="inter_branch_forward",
        null=True, blank=True,
        help_text="The receipt a forwarded receipt raised at the receiving branch.",
    )
    recharge = models.ForeignKey(
        InterBranchRecharge, on_delete=models.PROTECT, related_name="transfers",
        null=True, blank=True,
    )
    move_key = models.CharField(
        max_length=96, blank=True, default="",
        help_text="The caller's key for a receivable move, so a repeated call moves nothing twice.",
    )
    adjustment_entry = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="income_given_back",
        null=True, blank=True,
        help_text="For income given back: the credit note or concession journal that took it.",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
        null=True, blank=True,
    )
    requested_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
        null=True, blank=True,
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
        null=True, blank=True,
    )
    received_at = models.DateTimeField(null=True, blank=True)
    arrival_date = models.DateField(null=True, blank=True)
    declined_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+",
        null=True, blank=True,
    )
    declined_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta(FinanceDocument.Meta):
        constraints = FinanceDocument.Meta.constraints + [
            models.CheckConstraint(
                check=models.Q(amount__gt=0), name="ck_finance_ibt_amount_positive",
            ),
            models.CheckConstraint(
                check=~models.Q(branch=models.F("to_branch")),
                name="ck_finance_ibt_two_branches",
            ),
            # A voided move gives its key up, so the same move can be made again.
            models.UniqueConstraint(
                fields=["entity", "move_key"],
                condition=~models.Q(move_key="") & ~models.Q(status="REVERSED"),
                name="uniq_finance_ibt_live_move_key",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "transfer_date"]),
            models.Index(fields=["entity", "kind"]),
            models.Index(fields=["to_branch", "transfer_date"]),
        ]
        ordering = ["-transfer_date", "-id"]

    def __str__(self) -> str:
        return f"{self.document_number or self.pk}: {self.kind} {format_naira(int(self.amount))}"

    @property
    def stage(self) -> str:
        """Where the transfer stands, in the words its two branches use.

        ``REQUESTED`` (asked for, not yet sent), ``PENDING_APPROVAL``, ``SENT``
        (booked, arrival not yet confirmed), ``RECEIVED``, ``DECLINED`` or
        ``VOIDED``. A kind that moves no money is ``SENT`` once booked.
        """
        from ..constants import DocumentStatus

        if self.status == DocumentStatus.CANCELLED:
            return "DECLINED"
        if self.status == DocumentStatus.REVERSED:
            return "VOIDED"
        if self.status == DocumentStatus.PENDING_APPROVAL:
            return "PENDING_APPROVAL"
        if self.status in (DocumentStatus.DRAFT, DocumentStatus.APPROVED):
            return "REQUESTED"
        return "RECEIVED" if self.received_at else "SENT"


class InterBranchTransferLeg(TimeStampedModel):
    """One branch's side of an inter-branch transfer, and the journal it posted.

    A transfer has a SENDING and a RECEIVING leg. Each leg carries its own branch
    and the journal that branch's books hold, so a journal of the receiving side
    is filed under the receiving branch rather than the sending one the
    transfer itself names. ``journal`` is blank on the receiving leg of a
    forwarded receipt, whose receiving side is the receipt it raised
    (:attr:`InterBranchTransfer.receipt`), and on the sending leg of income given
    back, whose sending side is the adjusting document's own journal
    (:attr:`InterBranchTransfer.adjustment_entry`).
    """

    transfer = models.ForeignKey(
        InterBranchTransfer, on_delete=models.CASCADE, related_name="legs",
    )
    role = models.CharField(max_length=10, choices=InterBranchLegRole.choices)
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="inter_branch_legs",
    )
    counterparty_branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="+",
    )
    bank_account = models.ForeignKey(
        "BankAccount", on_delete=models.PROTECT, related_name="inter_branch_legs",
        null=True, blank=True,
    )
    journal = models.OneToOneField(
        "JournalEntry", on_delete=models.PROTECT, related_name="inter_branch_leg",
        null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "role"], name="uniq_finance_ibtleg_role",
            ),
        ]
        ordering = ["transfer", "role"]


class ReceivableTransferItem(TimeStampedModel):
    """One document a receivable move carried from one branch to another.

    * ``INVOICE``: an open invoice, or one whose income is not all earned yet.
      ``amount`` is what it still owed and ``deferred_amount`` the income not yet
      earned that moved with it; its revenue journal stays at ``from_branch``,
      and the invoice itself names the new branch.
    * ``DEBIT_NOTE``: an open debit note; ``amount`` is what it still owed.
    * ``RECEIPT_CREDIT`` / ``NOTE_CREDIT``: unapplied credit drawn from a receipt
      or a credit note of the old branch; ``amount`` is what was drawn. The
      source document stays where it is and records the draw in its
      ``transferred_amount``; the credit reappears at the new branch as the
      move's receipt (:attr:`InterBranchTransfer.receipt`).
    """

    transfer = models.ForeignKey(
        InterBranchTransfer, on_delete=models.CASCADE, related_name="moved_items",
    )
    kind = models.CharField(max_length=16, choices=ReceivableMoveItemKind.choices)
    invoice = models.ForeignKey(
        "Invoice", on_delete=models.PROTECT, related_name="receivable_moves",
        null=True, blank=True,
    )
    note = models.ForeignKey(
        "CreditNote", on_delete=models.PROTECT, related_name="receivable_moves",
        null=True, blank=True,
    )
    payment = models.ForeignKey(
        "Payment", on_delete=models.PROTECT, related_name="receivable_moves",
        null=True, blank=True,
    )
    from_branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="+",
    )
    amount = MoneyField(help_text="Open balance moved, or credit drawn, in kobo.")
    deferred_amount = MoneyField(help_text="Income not yet earned that moved with an invoice, in kobo.")

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(kind="INVOICE", invoice__isnull=False, note__isnull=True, payment__isnull=True)
                    | models.Q(kind__in=["DEBIT_NOTE", "NOTE_CREDIT"], invoice__isnull=True,
                               note__isnull=False, payment__isnull=True)
                    | models.Q(kind="RECEIPT_CREDIT", invoice__isnull=True, note__isnull=True,
                               payment__isnull=False)
                ),
                name="ck_finance_receivablemove_one_document",
            ),
        ]
        ordering = ["transfer", "kind", "id"]
