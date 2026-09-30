"""What "billed" and "collected" mean, said once for every screen that reports them.

The finance dashboard, the receivables tab and the owner layer's own dashboards
(through the FAL) all answer "how much of this period's fees is in?". They answer
from these definitions, so the proprietor and the bursar read the same figure:

* **Billed** is what the period's invoices ask for after concessions, credit notes
  and write-offs: ``total - amount_credited``. A scholarship is income never
  charged, not income owed and unpaid.
* **Collected** is money applied to the period's invoices: ``amount_paid``, the sum
  of the receipt allocations that settled them. A receipt that pays two periods'
  bills counts in each for the part it paid there and once in all, and money left
  over as credit counts in no period until it pays a bill.
* **Received money** is a receipt that brought money in. A credit-transfer receipt
  (:class:`~vs_finance.models.CustomerCreditTransfer`) moves credit a customer
  already held, so it is not takings, though what it settles is collected on the
  bill it settles.
"""
from __future__ import annotations

from django.db.models import F, Q, Sum

from .constants import CREDIT_TRANSFER_METHOD


def received_money_q(prefix: str = "") -> Q:
    """Receipts that brought money in, as a filter over ``Payment`` (or a relation to it)."""
    return ~Q(**{f"{prefix}method": CREDIT_TRANSFER_METHOD})


def billed_and_collected(invoices) -> tuple[int, int]:
    """``(billed, collected)`` in kobo over a queryset of posted invoices."""
    agg = invoices.aggregate(
        billed=Sum(F("total") - F("amount_credited")), collected=Sum("amount_paid"),
    )
    return int(agg["billed"] or 0), int(agg["collected"] or 0)


def collection_rate_bps(billed: int, collected: int) -> int:
    """Collected over billed in basis points; nothing billed reads as fully collected."""
    return 10000 if billed <= 0 else (collected * 10000) // billed
