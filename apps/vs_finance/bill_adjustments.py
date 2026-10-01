"""What an adjustment of a bill takes back, and whose books it comes out of.

A credit note, concession or write-off is raised at the branch that holds the
bill now. On a bill that never moved, that is the branch that booked all its
income, and everything the adjustment debits is that branch's own, exactly as it
always has been. A receivable move
(:func:`vs_finance.inter_branch.transfer_open_receivables`) changes that: the
bill and its months still to come go to the new branch, while the revenue and
output tax its invoice journal booked, and any month already earned, stay at the
old branch.

**A credit note or concession cancels the service, so the branch that booked the
income gives it back.** Tunde's 100k textbook bill (plus 7.5k VAT) is raised at
Ikeja on 10 January and moves to Lekki with him on 25 January; the books never
arrive and Lekki credits the bill in full:

* Lekki: ``Dr inter-branch [Ikeja] 107.5k, Cr receivable 107.5k``. Its revenue
  is untouched, and what it owes Ikeja for the bill drops to nothing.
* Ikeja: ``Dr revenue 100k, Dr output VAT 7.5k, Cr inter-branch [Lekki]
  107.5k``. Its revenue and its share of the VAT return both come down.

The parts of an adjustment, latest service first, and where each is debited:

1. a waiting deferred-income share: the deferred income of the branch holding
   the share;
2. a share already released, on a moved bill only
   (:func:`vs_finance.deferred_income.plan_released_takeback`): the adjustment's
   own account (revenue for a credit note, the allowance for a concession) at
   the branch that released it;
3. the rest, and a credit note's output tax: the same accounts at the branch
   whose journal booked the bill (:func:`booked_elsewhere`).

Every part a branch other than the adjusting one bears is a line on the
adjusting journal debiting the inter-branch account naming that branch, and one
journal of that branch through an ``INCOME_GIVEN_BACK`` transfer
(:func:`vs_finance.inter_branch.book_income_given_back`), so each journal touches
only its own branch's books and every pair agrees.

**A write-off does not cancel the service; it fails to collect for it.** The
branch holding the debt bears that, whatever happened before. Lekki writes off
Tunde's 400k term on 28 January or on 3 February: either way only the months
Lekki holds (February to April, 300k) leave its deferred income, Lekki books the
other 100k as a bad debt, January stays earned at Ikeja (released there on its
usual schedule if it has not been), and Lekki still owes Ikeja 100k. So a
write-off takes back only the waiting shares its own branch holds
(:func:`vs_finance.deferred_income.plan_unwind` with ``held_by``), and nothing
crosses branches.

An adjustment not tied to a bill (a credit note with no invoice, such as a
returned deposit) is its own branch's in full.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .deferred_income import (
    apply_unwind,
    held_elsewhere,
    plan_released_takeback,
    plan_unwind,
    shares_elsewhere,
)


@dataclass
class Adjustment:
    """Where each part of one adjustment of a bill is debited.

    ``deferred_here``, ``here`` (``{(account_id, cost_center_id): kobo}``, in the
    order the adjustment listed its debits) and ``tax_here`` (``{account_id:
    kobo}``) are the adjusting journal's own debits. ``elsewhere`` holds, per
    other branch, the deferred income and the ``{(account_id, cost_center_id):
    kobo}`` that branch debits in its own journal; the adjusting journal debits
    the inter-branch account naming it for their sum (:attr:`inter_branch`).
    ``plan`` is the deferred-income shares taken, recorded once the adjusting
    journal posts (:func:`give_back`).
    """

    branch_id: int | None
    plan: list = field(default_factory=list)
    deferred_here: int = 0
    here: dict = field(default_factory=dict)
    tax_here: dict = field(default_factory=dict)
    elsewhere: dict = field(default_factory=dict)

    def _side(self, branch_id):
        return self.elsewhere.setdefault(branch_id, {"deferred": 0, "lines": defaultdict(int)})

    @property
    def inter_branch(self) -> list[tuple[int, int]]:
        """``[(other branch id, kobo)]``: the adjusting journal's inter-branch debits."""
        totals = [
            (branch_id, side["deferred"] + sum(side["lines"].values()))
            for branch_id, side in sorted(self.elsewhere.items())
        ]
        return [(branch_id, total) for branch_id, total in totals if total]


def booked_elsewhere(invoice, branch_id):
    """The branch whose journal booked ``invoice``, when it is not ``branch_id``; else ``None``.

    Both must name a branch, as for a share
    (:func:`vs_finance.deferred_income.held_elsewhere`): an unbranched journal or
    adjustment is taken as the adjusting branch's own.
    """
    if invoice is None or branch_id is None or invoice.journal_id is None:
        return None
    booked = invoice.journal.branch_id
    return booked if booked is not None and booked != branch_id else None


def plan_adjustment(invoice, *, branch_id, debits, tax=None) -> Adjustment:
    """Split an adjustment of ``invoice`` raised at ``branch_id`` by whose books bear each part.

    ``debits`` is ``[((account_id, cost_center_id), kobo)]``: what the
    adjustment would debit for the bill's income were every part its own, in
    order (a credit note's revenue lines, a concession's allowance). ``tax`` is a
    credit note's output tax by account. Deferred shares are taken latest first
    and cover the debits in order; see the module docstring for where each part
    goes. A write-off does not come here: it takes only its own branch's
    waiting shares (:func:`vs_finance.deferred_income.plan_unwind` with
    ``held_by``) and bears the rest itself.

    Locks the bill's shares. Nothing is written.
    """
    adjustment = Adjustment(branch_id=branch_id)
    pending = [[key, int(amount)] for key, amount in debits if amount]
    total = sum(amount for _key, amount in pending)
    tax = {account_id: int(amount) for account_id, amount in (tax or {}).items() if amount}

    def take(amount):
        """Consume ``amount`` from the debits in order: ``[(key, kobo)]``."""
        parts = []
        while amount > 0 and pending:
            key, left = pending[0]
            part = min(left, amount)
            parts.append((key, part))
            amount -= part
            pending[0][1] -= part
            if pending[0][1] == 0:
                pending.pop(0)
        return parts

    def debit(where, key, amount):
        if where is None:
            adjustment.here[key] = adjustment.here.get(key, 0) + amount
        else:
            adjustment._side(where)["lines"][key] += amount

    if invoice is None:
        for key, amount in take(total):
            debit(None, key, amount)
        adjustment.tax_here = tax
        return adjustment

    plan = plan_unwind(invoice, total)
    booked = booked_elsewhere(invoice, branch_id)
    if booked is not None or shares_elsewhere(invoice, branch_id):
        plan += plan_released_takeback(
            invoice, total - sum(step.take for step in plan), branch_id=branch_id)
    adjustment.plan = plan

    for step in plan:
        holder = step.entry.branch_id if held_elsewhere(step.entry, branch_id) else None
        parts = take(step.take)
        if not step.after_release:
            if holder is None:
                adjustment.deferred_here += step.take
            else:
                adjustment._side(holder)["deferred"] += step.take
            continue
        for key, amount in parts:
            debit(holder, key, amount)
    for key, amount in take(total):
        debit(booked, key, amount)
    for account_id, amount in tax.items():
        if booked is None:
            adjustment.tax_here[account_id] = amount
        else:
            debit(booked, (account_id, None), amount)
    return adjustment


def give_back(adjustment, *, adjustment_entry, invoice, label="", actor_user=None) -> None:
    """Record what the posted adjusting journal took, and book each other branch's side.

    The shares taken are recorded against the journal
    (:func:`vs_finance.deferred_income.apply_unwind`), and every branch in
    ``adjustment.elsewhere`` posts its own journal through an ``INCOME_GIVEN_BACK``
    transfer linked to it. Voiding the adjusting document undoes both
    (:func:`vs_finance.deferred_income.restore_unwinds`). ``label`` names the
    document on the transfer, so a refusal to void the transfer alone can say
    what to void instead.
    """
    if adjustment.plan:
        apply_unwind(adjustment.plan, adjustment_entry=adjustment_entry)
    if not adjustment.elsewhere:
        return
    from .inter_branch import book_income_given_back

    for branch_id, side in sorted(adjustment.elsewhere.items()):
        book_income_given_back(
            adjustment_entry, holder_branch_id=branch_id, invoice=invoice,
            deferred=side["deferred"], lines=dict(side["lines"]), label=label,
            actor_user=actor_user,
        )
