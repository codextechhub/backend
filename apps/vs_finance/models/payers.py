"""One payer paying for several customers.

A customer is the account a bill sits on. Somebody else often pays it: a parent
pays for each of their children, a company for its staff, a trust for the people
it sponsors. That somebody is a **payer**, and in these books a payer is itself a
:class:`~vs_finance.models.Customer` (an account of its own, which may or may not
be billed), linked to the customers it pays for by :class:`PayerLink`.

When the payer sends one sum to cover several of those customers, a
:class:`PayerPayment` records it once and splits it into one ordinary receipt per
customer, each settling only that customer's own bills of its own branch. A share
whose bills sit at another branch than the bank account that received the money
is booked as money held for that branch
(:class:`~vs_finance.models.HeldForBranchReceipt`) and reaches the bills when it
is forwarded there. The services that write these rows live in
:mod:`vs_finance.payer_payments`.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from ..constants import DocType, PayerPaymentSplit, PaymentMethod
from ..money import MoneyField
from .core import FinanceDocument, LedgerEntity, TimeStampedModel

__all__ = ["PayerLink", "PayerPayment", "PayerPaymentShare"]


class PayerLink(TimeStampedModel):
    """A payer and one customer it pays for.

    Mr Okafor's account pays for Ada, Emeka and Chidi: three links, one per child.
    A customer may have several payers (a mother and a father, or a parent and a
    sponsor), and a payer as many customers as it likes. The link is what entitles
    a payment from the payer to settle the customer's bills; a customer the payer
    is not linked to is refused.

    A shared record, like the customers it joins: it carries no branch, because
    one family pays at every branch its children attend. ``source_type`` and
    ``source_id`` name the owner's record the link mirrors, as plain strings, so a
    product that keeps its own relationships (the schools product's fee-paying
    guardians) can keep these in step without the ledger importing it. A link that
    ends is switched off rather than deleted, so the payments made under it still
    read.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="payer_links",
    )
    payer = models.ForeignKey(
        "Customer", on_delete=models.PROTECT, related_name="pays_for_links",
        help_text="The account that pays.",
    )
    customer = models.ForeignKey(
        "Customer", on_delete=models.PROTECT, related_name="paid_by_links",
        help_text="The account whose bills the payer pays.",
    )
    is_active = models.BooleanField(default=True)
    source_type = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Loose reference to the owner's record this link mirrors, as 'app_label.Model'.",
    )
    source_id = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_payer_links_created", null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payer", "customer"], name="uniq_finance_payer_link",
            ),
            models.CheckConstraint(
                check=~models.Q(payer=models.F("customer")),
                name="ck_finance_payer_link_not_self",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "payer", "is_active"]),
            models.Index(fields=["customer", "is_active"]),
        ]
        ordering = ["payer", "customer"]

    def __str__(self) -> str:
        return f"{self.payer_id} pays for {self.customer_id}"


class PayerPayment(FinanceDocument):
    """One payment from a payer, split into one receipt per customer it pays for.

    Mr Okafor sends N450,000 into Ikeja's bank for Ada (N180,000) and Emeka
    (N150,000), billed at Ikeja, and Chidi (N120,000), billed at Lekki. This
    document records the N450,000 once, at the branch whose bank received it
    (``branch``, always the bank account's), and its shares
    (:class:`PayerPaymentShare`) say where each part went: Ada's and Emeka's are
    Ikeja receipts settling their own Ikeja bills, and Chidi's is held at Ikeja
    for Lekki until it is forwarded there.

    ``split`` is how the amounts were decided: one of
    :class:`~vs_finance.constants.PayerPaymentSplit`, or ``EXPLICIT`` when the
    bursar typed them. Its receipts and held receipts are voided only with it, all
    together.
    """

    DOC_TYPE = DocType.PAYER_PAYMENT

    #: The split recorded when the bursar typed each customer's amount.
    EXPLICIT = "EXPLICIT"
    SPLIT_CHOICES = [*PayerPaymentSplit.choices, (EXPLICIT, "Entered per customer")]

    payer = models.ForeignKey(
        "Customer", on_delete=models.PROTECT, related_name="payer_payments",
    )
    bank_account = models.ForeignKey(
        "BankAccount", on_delete=models.PROTECT, related_name="payer_payments",
        help_text="Where the money arrived; its branch is this document's.",
    )
    amount = MoneyField(help_text="Kobo received, always positive.")
    payment_date = models.DateField()
    method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER,
    )
    split = models.CharField(max_length=16, choices=SPLIT_CHOICES)
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")

    class Meta(FinanceDocument.Meta):
        constraints = FinanceDocument.Meta.constraints + [
            models.CheckConstraint(
                check=models.Q(amount__gt=0), name="ck_finance_payerpayment_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "payment_date"]),
            models.Index(fields=["payer"]),
        ]
        ordering = ["-payment_date", "-id"]

    def __str__(self) -> str:
        return f"{self.document_number or self.pk}: {self.amount} from {self.payer_id}"


class PayerPaymentShare(TimeStampedModel):
    """The part of a :class:`PayerPayment` that went to one customer at one branch.

    ``branch`` is the branch of the bills the share pays, which decides how it is
    booked. At the payment's own branch it is an ordinary receipt (``receipt``)
    settling that customer's bills there. At another branch it is money held for
    that branch (``held_receipt``), forwarded there by the usual forwarded-receipt
    transfer, where it settles the customer's bills of that branch.

    ``amount`` is the whole share; ``surplus`` is the part of it no bill took when
    it was split, left as the customer's credit there.
    """

    payer_payment = models.ForeignKey(
        PayerPayment, on_delete=models.CASCADE, related_name="shares",
    )
    customer = models.ForeignKey(
        "Customer", on_delete=models.PROTECT, related_name="payer_payment_shares",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="payer_payment_shares",
    )
    amount = MoneyField(help_text="Kobo of the payment given to this customer at this branch.")
    surplus = MoneyField(help_text="Kobo of the share no bill took, left as credit.")
    receipt = models.OneToOneField(
        "Payment", on_delete=models.PROTECT, related_name="payer_payment_share",
        null=True, blank=True,
    )
    held_receipt = models.OneToOneField(
        "HeldForBranchReceipt", on_delete=models.PROTECT, related_name="payer_payment_share",
        null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payer_payment", "customer", "branch"], name="uniq_finance_payer_share",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gt=0) & models.Q(surplus__gte=0)
                & models.Q(surplus__lte=models.F("amount")),
                name="ck_finance_payer_share_amounts",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(receipt__isnull=False, held_receipt__isnull=True)
                    | models.Q(receipt__isnull=True, held_receipt__isnull=False)
                ),
                name="ck_finance_payer_share_one_document",
            ),
        ]
        ordering = ["payer_payment", "id"]

    def __str__(self) -> str:
        return f"{self.payer_payment_id}: {self.amount} to {self.customer_id} at {self.branch_id}"
