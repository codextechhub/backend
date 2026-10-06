"""Tax returns: what each return declares, how it is filed, and how it is paid.

The platform's source transactions already park what is owed to the authorities in
liability control accounts: sales credit **Output VAT** (2200), purchases debit
recoverable **Input VAT** (1300), vendor payments credit **WHT Payable** (2300), and
payroll credits **PAYE** (2310) and **Pension** (2320). A
:class:`~vs_finance.models.TaxObligation` names one such payable account (and, for
VAT, the recoverable account netted against it); a
:class:`~vs_finance.models.TaxFiling` is one return.

**A return declares source lines, not a date window.** The source lines of an
obligation are the ledger lines on its accounts that this module did not write: the
netting, penalty and remittance journals posted here carry
:attr:`JournalSource.TAX <vs_finance.constants.JournalSource.TAX>`, and they and
their reversals are never source lines. A return collects every source line dated on
or before its ``period_end`` that no filed return has declared yet, and filing
stamps each one with a :class:`~vs_finance.models.TaxFilingLine`. So June's filing
and payment, both dated in July, never reduce July's tax, and a June line recorded
after June was filed lands in the next open return as a late item, shown under the
month it is dated in ("from June"). A filed return is never amended.

**One return, booked per branch.** A tenant files one return per obligation, and
the figures are broken down by the branch of each line's entry
(:func:`branch_breakdown`). An entry with no branch counts as the tenant's only
branch where it has exactly one; at a tenant with several it forms its own "no
branch yet" group, which a draft shows and filing refuses until the entries are
given a branch. Each branch share gets its own netting/penalty journal at filing
and its own remittance journals when paid, all carrying that branch; the return is
PAID when every share is. A share is paid only from its own branch's bank account:
an account with no branch pays nothing at a tenant with several branches, and
counts as the branch's at a tenant with one. Every tenant owns at least one
branch, so every share that is filed and paid names one; a tenant with none is a
broken invariant and raises :class:`~vs_finance.exceptions.BranchlessTenantError`
(:func:`branch_rule`). A branch-bound reader sees and pays only the shares of
their own branches.

**Credits carry forward.** When recoverable input tax (and any credit brought
forward) exceeds the tax, the return is due nil and the excess is carried to the
next return of the same obligation, which uses it before anything is paid. Between
branches, one branch's surplus covers another's tax inside the same return; the
cover stays visible as the two branches' opposite balances on the payable account,
which net to zero for the tenant.

Lifecycle, mirroring ``DRAFT → FILED → PAID``:

* :func:`prepare_filing` works the figures out and writes them on a draft. Nothing
  posts and no line is claimed.
* :func:`file_filing` stamps the lines, posts the per-branch journals and freezes
  the figures; it refuses when the lines have moved since the draft was prepared.
  A nil return files.
* :func:`pay_filing` remits one or more branch shares; :func:`reverse_remittance`
  undoes one recorded in error and puts the return back to FILED.
* :func:`unfile_filing` reverts a FILED return with no payment to DRAFT, reversing
  its journals and releasing its lines.

:func:`outstanding_obligations` is a read-only view of each control account's
running balance. All amounts are integer kobo; every mutating call records a durable
rejection audit on a :class:`FinanceError` and re-raises.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count, Sum

from vs_config.display import format_date, format_date_range

from .audit import record, record_rejection
from .constants import (
    FinanceAuditAction,
    InvoicePaymentStatus,
    JournalSource,
    TaxFilingStatus,
    TaxSourceRole,
)
from .exceptions import FinanceError, TaxFilingError
from .money import format_naira
from .posting import post_journal, resolve_period
from .wording import state_word

#: Journal sources whose lines are never a return's source lines: the journals the
#: tax module writes for itself. Their reversals are left out through ``reverses``.
TAX_MODULE_SOURCES = (JournalSource.TAX,)

#: Statuses of a return whose lines are declared and whose credit can carry forward.
FILED_STATUSES = (TaxFilingStatus.FILED, TaxFilingStatus.PAID)

#: Marks "no branch named" for :func:`pay_filing` and :func:`file_filing`, apart from a
#: branch argument passed as ``None``.
ANY_SHARE = object()


# --------------------------------------------------------------------------- #
# Dates                                                                        #
# --------------------------------------------------------------------------- #

def _default_due_date(period_end, filing_day):
    """Day ``filing_day`` of the month *after* ``period_end``, clamped to that month's length.

    Matches the obligation's ``filing_day`` help_text ("Day of the month after period
    end the return is due").
    """
    year = period_end.year + (1 if period_end.month == 12 else 0)
    month = 1 if period_end.month == 12 else period_end.month + 1
    if month == 12:
        next_month_first = datetime.date(year + 1, 1, 1)
    else:
        next_month_first = datetime.date(year, month + 1, 1)
    last_day = (next_month_first - datetime.timedelta(days=1)).day
    return datetime.date(year, month, min(int(filing_day), last_day))


def _account_movement(entity, account, *, period_start=None, period_end=None,
                      branch_ids=None, rule=None):
    """Return ``(debit_sum, credit_sum)`` of ``account``'s lines in the ledger.

    Lines come from :func:`vs_finance.branch_ledger.ledger_lines`, so a reversed
    entry and its reversal both count and cancel when both are dated inside the
    window. Bounded by entry date to ``[period_start, period_end]`` when given;
    otherwise the all-time movement (the account's current balance components).
    Used for the running balance in :func:`outstanding_obligations`; a return's own
    figures come from its source lines instead.

    ``branch_ids`` narrows to the lines those branches own under ``rule`` (a
    :class:`BranchRule`), the way a return counts them: an entry's own branch,
    and an entry with no branch only at a tenant whose one branch is among them.
    """
    from django.db.models import Q

    from .branch_ledger import ledger_lines

    qs = ledger_lines(entity).filter(account=account)
    if branch_ids is not None:
        own = Q(entry__branch_id__in=tuple(sorted(branch_ids)))
        if rule is not None and rule.only_branch_id in branch_ids:
            own |= Q(entry__branch_id__isnull=True)
        qs = qs.filter(own)
    if period_start is not None:
        qs = qs.filter(entry__date__gte=period_start)
    if period_end is not None:
        qs = qs.filter(entry__date__lte=period_end)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return int(agg["d"] or 0), int(agg["c"] or 0)


# --------------------------------------------------------------------------- #
# Source lines and how they group                                              #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BranchRule:
    """How a line's entry branch becomes the branch it is counted under.

    ``only_branch_id`` is the tenant's one branch when it has exactly one, and
    ``None`` when it has several. A line whose entry names a branch keeps it. One
    with none counts as the only branch; at a tenant with several it is pending.
    """

    only_branch_id: int | None

    def resolve(self, branch_id):
        """``(branch_id, pending)`` for a line whose entry carries ``branch_id``."""
        if branch_id is not None:
            return branch_id, False
        if self.only_branch_id is not None:
            return self.only_branch_id, False
        return None, True

    def bank_branch_id(self, bank_account):
        """The branch whose share ``bank_account`` may pay, or ``None``.

        An account's own branch; at a tenant with one branch, an account with
        none is that branch's. ``None`` means the account pays no share: an
        account not yet given a branch at a tenant with several.
        """
        return bank_account.branch_id or self.only_branch_id


def branch_rule(entity) -> BranchRule:
    """The :class:`BranchRule` of ``entity``'s tenant.

    Raises :class:`~vs_finance.exceptions.BranchlessTenantError` when the tenant
    owns no branch (:func:`vs_finance.branch_ledger.only_branch_id_or_several`).
    """
    from .branch_ledger import only_branch_id_or_several

    return BranchRule(only_branch_id=only_branch_id_or_several(entity.tenant_id))


@dataclass(frozen=True)
class SourceLine:
    """One undeclared source line, with the branch it counts under.

    ``amount`` is signed the way the tax reads it: credit minus debit on the
    payable account, debit minus credit on the recoverable one.
    """

    id: int
    role: str
    date: datetime.date
    branch_id: int | None
    pending: bool
    debit: int
    credit: int

    @property
    def amount(self) -> int:
        if self.role == TaxSourceRole.PAYABLE:
            return self.credit - self.debit
        return self.debit - self.credit

    @property
    def key(self):
        return (self.branch_id, self.pending)


def source_roles(obligation) -> dict:
    """``{account_id: TaxSourceRole}``: the accounts whose lines are ``obligation``'s source."""
    roles = {obligation.liability_account_id: TaxSourceRole.PAYABLE}
    if obligation.recoverable_account_id:
        roles[obligation.recoverable_account_id] = TaxSourceRole.RECOVERABLE
    return roles


def undeclared_source_lines(obligation, *, period_end):
    """The ledger lines a return of ``obligation`` up to ``period_end`` would declare now.

    A queryset of :class:`~vs_finance.models.JournalLine`, oldest first, read from
    the ledger every money reader uses (reversed entries and their reversals both
    count). Lines the tax module wrote, and reversals of them, are left out, as
    are lines a filed return has already declared. The one definition of a
    return's source lines: :func:`collect_source_lines` works the figures out from
    it, and a draft's line listing reads it.
    """
    from .branch_ledger import ledger_lines

    return (
        ledger_lines(obligation.entity)
        .filter(account_id__in=list(source_roles(obligation)), entry__date__lte=period_end,
                tax_declaration__isnull=True)
        .exclude(entry__source__in=TAX_MODULE_SOURCES)
        .exclude(entry__reverses__source__in=TAX_MODULE_SOURCES)
        .order_by("entry__date", "id")
    )


def collect_source_lines(obligation, *, period_end, branch_id=None, rule=None) -> list:
    """Every undeclared source line of ``obligation`` dated on or before ``period_end``.

    The lines :func:`undeclared_source_lines` reads, each with the branch it
    counts under. ``branch_id`` narrows to one branch, for a return raised for a
    single branch.
    """
    rule = rule or branch_rule(obligation.entity)
    roles = source_roles(obligation)
    rows = undeclared_source_lines(obligation, period_end=period_end).values_list(
        "id", "account_id", "entry__date", "entry__branch_id", "debit", "credit",
    )
    lines = []
    for line_id, account_id, date, entry_branch, debit, credit in rows:
        resolved, pending = rule.resolve(entry_branch)
        if branch_id is not None and resolved != branch_id:
            continue
        lines.append(SourceLine(
            id=line_id, role=roles[account_id], date=date, branch_id=resolved,
            pending=pending, debit=int(debit), credit=int(credit),
        ))
    return lines


def return_lines(filing, *, branch_ids=None):
    """The ledger lines ``filing`` declares, for listing them line by line.

    A queryset of :class:`~vs_finance.models.JournalLine`, oldest first, each
    annotated with ``tax_role`` (PAYABLE or RECOVERABLE), ``counted_branch_id``
    (the branch it counts under), ``is_late`` (dated before the return's period)
    and ``tax_amount`` (signed the way the tax reads it, as
    :attr:`SourceLine.amount`).

    A filed or paid return lists the lines it stamped, exactly as filed. A draft
    lists what it would declare if filed now, read the way
    :func:`work_out_return` reads it (:func:`undeclared_source_lines`), so the
    list adds up to the draft's figures until the ledger moves under it. A
    cancelled return declares nothing. ``branch_ids`` narrows to the lines those
    branches count, which is what a branch-bound reader is shown: Ngozi at Lekki
    sees Lekki's lines of the tenant's March VAT return, never Ikeja's.
    """
    from django.db.models import BigIntegerField, BooleanField, Case, CharField, F, Value, When
    from django.db.models.functions import Coalesce

    from .models import JournalLine

    if filing.filing_status in FILED_STATUSES:
        lines = JournalLine.objects.filter(tax_declaration__filing=filing).annotate(
            tax_role=F("tax_declaration__role"),
            counted_branch_id=F("tax_declaration__branch_id"),
            is_late=F("tax_declaration__is_late"),
        )
    elif filing.filing_status == TaxFilingStatus.DRAFT:
        rule = branch_rule(filing.entity)
        counted = (
            Coalesce(F("entry__branch_id"), Value(rule.only_branch_id))
            if rule.only_branch_id is not None else F("entry__branch_id")
        )
        lines = undeclared_source_lines(filing.obligation, period_end=filing.period_end).annotate(
            tax_role=Case(
                *[When(account_id=account_id, then=Value(role))
                  for account_id, role in source_roles(filing.obligation).items()],
                output_field=CharField(),
            ),
            counted_branch_id=counted,
            is_late=Case(
                When(entry__date__lt=filing.period_start, then=Value(True)),
                default=Value(False), output_field=BooleanField(),
            ),
        )
        if filing.branch_id is not None:
            lines = lines.filter(counted_branch_id=filing.branch_id)
    else:
        return JournalLine.objects.none()
    if branch_ids is not None:
        lines = lines.filter(counted_branch_id__in=tuple(sorted(branch_ids)))
    return lines.annotate(
        tax_amount=Case(
            When(tax_role=TaxSourceRole.PAYABLE, then=F("credit") - F("debit")),
            default=F("debit") - F("credit"),
            output_field=BigIntegerField(),
        ),
    ).select_related("entry", "account").order_by("entry__date", "id")


@dataclass
class ShareFigures:
    """One branch's figures on a return, before and after the branches settle."""

    branch_id: int | None
    pending: bool = False
    gross: int = 0
    recoverable: int = 0
    brought_forward: int = 0
    adjustment: int = 0
    amount_due: int = 0
    carried_forward: int = 0
    line_count: int = 0

    @property
    def key(self):
        return (self.branch_id, self.pending)

    @property
    def net(self) -> int:
        """Tax less credits, signed: positive is owed, negative is surplus credit."""
        return self.gross - self.recoverable - self.brought_forward


def _key_order(key):
    branch_id, pending = key
    return (pending, branch_id is None, branch_id or 0)


def branch_breakdown(lines) -> dict:
    """A return's source lines grouped by the branch they count under.

    Returns ``{(branch_id, pending): ShareFigures}`` with gross, recoverable and
    line count filled in, ordered by branch with the "no branch yet" group last.
    This is the one place a return's lines are grouped by branch.
    """
    groups: dict = {}
    for line in lines:
        share = groups.setdefault(line.key, ShareFigures(branch_id=line.branch_id, pending=line.pending))
        if line.role == TaxSourceRole.PAYABLE:
            share.gross += line.amount
        else:
            share.recoverable += line.amount
        share.line_count += 1
    return {key: groups[key] for key in sorted(groups, key=_key_order)}


def _month_label(month_start, period_start):
    """"from March", or "from December 2025" when the month is in another year."""
    if month_start.year == period_start.year:
        return f"from {month_start:%B}"
    return f"from {month_start:%B %Y}"


def late_items(lines, period_start) -> list:
    """Source lines dated before ``period_start``, grouped by the month they are dated in.

    Each item is ``{"month", "label", "gross", "recoverable", "net", "line_count",
    "branches"}``, oldest month first. ``branches`` holds the same figures per
    branch, keyed by the branch id as a string ("" for lines with no branch), so a
    branch-bound reader is shown only their branches' part of each month. A late
    line was recorded after the return for its own month was filed (or before any
    return existed), so it is declared here and named.
    """
    def figures():
        return {"gross": 0, "recoverable": 0, "line_count": 0}

    def add(item, line):
        if line.role == TaxSourceRole.PAYABLE:
            item["gross"] += line.amount
        else:
            item["recoverable"] += line.amount
        item["line_count"] += 1

    months: dict = {}
    for line in lines:
        if line.date >= period_start:
            continue
        month = line.date.replace(day=1)
        item = months.setdefault(month, {**figures(), "branches": {}})
        add(item, line)
        key = "" if line.branch_id is None else str(line.branch_id)
        add(item["branches"].setdefault(key, figures()), line)
    return [
        {
            "month": f"{month:%Y-%m}", "label": _month_label(month, period_start),
            "gross": item["gross"], "recoverable": item["recoverable"],
            "net": item["gross"] - item["recoverable"], "line_count": item["line_count"],
            "branches": {
                key: {**part, "net": part["gross"] - part["recoverable"]}
                for key, part in sorted(item["branches"].items())
            },
        }
        for month, item in sorted(months.items())
    ]


def _allocate(total, weights) -> dict:
    """Split ``total`` over the positive ``weights`` in proportion, to the kobo.

    Largest remainder, ties broken by branch order, so the split is deterministic
    and always sums to ``total``.
    """
    keys = [k for k, w in weights.items() if w > 0]
    if total <= 0 or not keys:
        return {}
    weight_sum = sum(weights[k] for k in keys)
    parts = {k: total * weights[k] // weight_sum for k in keys}
    left = total - sum(parts.values())
    by_remainder = sorted(keys, key=lambda k: (-(total * weights[k] % weight_sum), _key_order(k)))
    for k in by_remainder[:left]:
        parts[k] += 1
    return parts


# --------------------------------------------------------------------------- #
# Working a return out                                                         #
# --------------------------------------------------------------------------- #

@dataclass
class ReturnFigures:
    """Everything a return declares, before it is written onto the filing."""

    lines: list
    shares: list
    credit_from: object = None
    late_items: list = field(default_factory=list)

    @property
    def gross(self) -> int:
        return sum(s.gross for s in self.shares)

    @property
    def recoverable(self) -> int:
        return sum(s.recoverable for s in self.shares)

    @property
    def brought_forward(self) -> int:
        return sum(s.brought_forward for s in self.shares)

    @property
    def late_line_count(self) -> int:
        return sum(item["line_count"] for item in self.late_items)

    @property
    def pending(self):
        """The "no branch yet" share, when its lines move any money."""
        for share in self.shares:
            if share.pending and (share.gross or share.recoverable):
                return share
        return None


def _credit_source(filing):
    """The filed return whose carried-forward credit ``filing`` brings forward, if any.

    The latest filed return of the same obligation that ends before this one
    starts and whose credit no other filed return has taken. Credits chain: a
    return that uses a credit carries forward whatever it leaves, so at most one
    return holds an untaken credit at a time.
    """
    from .models import TaxFiling

    return (
        TaxFiling.objects.filter(
            obligation=filing.obligation, filing_status__in=FILED_STATUSES,
            carried_forward_credit__gt=0, period_end__lt=filing.period_start,
            credit_carried_into__isnull=True,
        )
        .exclude(pk=filing.pk)
        .prefetch_related("shares")
        .order_by("-period_end", "-id")
        .first()
    )


def _settle(shares) -> None:
    """Work each share's amount due and carried credit out from the branches' nets.

    Branches with surplus credit cover the others inside the one return, shared in
    proportion to what each owes; whatever surplus is left after that is carried
    forward, taken from each surplus branch in proportion to its surplus.
    """
    owing = {s.key: s.net for s in shares if s.net > 0}
    surplus = {s.key: -s.net for s in shares if s.net < 0}
    total_owing, total_surplus = sum(owing.values()), sum(surplus.values())
    by_key = {s.key: s for s in shares}
    for share in shares:
        share.amount_due = 0
        share.carried_forward = 0
    if total_owing >= total_surplus:
        covered = _allocate(total_surplus, owing)
        for key, owed in owing.items():
            by_key[key].amount_due = owed - covered.get(key, 0)
    else:
        used = _allocate(total_owing, surplus)
        for key, spare in surplus.items():
            by_key[key].carried_forward = spare - used.get(key, 0)


def _spread_adjustment(shares, adjustment, *, adjustment_branch_id, rule) -> list:
    """Add a penalty to the shares and return the (possibly extended) share list.

    Named branch first; otherwise in proportion to each branch's tax; otherwise the
    one share there is, or the tenant's only branch. A tenant with several branches
    and no tax to go by must name the branch.
    """
    if adjustment <= 0:
        return shares
    by_key = {s.key: s for s in shares}

    def share_for(branch_id):
        key = (branch_id, False)
        if key not in by_key:
            by_key[key] = ShareFigures(branch_id=branch_id)
            shares.append(by_key[key])
        return by_key[key]

    if adjustment_branch_id is None:
        raise TaxFilingError(
            "A penalty is booked to one branch, so name the branch that bears it.",
            failures=["adjustment_branch"],
        )
    if adjustment_branch_id is not ANY_SHARE:
        share_for(adjustment_branch_id).adjustment += adjustment
        return shares
    weights = {s.key: s.gross for s in shares if not s.pending and s.gross > 0}
    if weights:
        for key, part in _allocate(adjustment, weights).items():
            by_key[key].adjustment += part
        return shares
    settled = [s for s in shares if not s.pending]
    if len(settled) == 1:
        settled[0].adjustment += adjustment
        return shares
    if rule.only_branch_id is not None:
        share_for(rule.only_branch_id).adjustment += adjustment
        return shares
    raise TaxFilingError(
        "The return has no tax to share the penalty by, so name the branch that bears it.",
        failures=["adjustment_branch"],
    )


def work_out_return(filing, *, adjustment=0, adjustment_branch_id=ANY_SHARE) -> ReturnFigures:
    """The figures ``filing`` declares if it is filed now.

    Collects the obligation's undeclared source lines up to the period end (for a
    return raised for one branch, that branch's only), groups them by branch,
    brings forward the previous return's credit branch by branch, settles the
    branches against each other and spreads any penalty. Reads only; nothing is
    written.
    """
    rule = branch_rule(filing.entity)
    lines = collect_source_lines(
        filing.obligation, period_end=filing.period_end,
        branch_id=filing.branch_id, rule=rule,
    )
    groups = branch_breakdown(lines)

    source = _credit_source(filing)
    if source is not None:
        credit = {(s.branch_id, s.branch_pending): int(s.carried_forward_credit)
                  for s in source.shares.all() if s.carried_forward_credit > 0}
        if not credit:
            branch_id, _ = rule.resolve(source.branch_id)
            credit = {(branch_id, False): int(source.carried_forward_credit)}
        for key, amount in credit.items():
            share = groups.setdefault(key, ShareFigures(branch_id=key[0], pending=key[1]))
            share.brought_forward += amount

    shares = [groups[key] for key in sorted(groups, key=_key_order)]
    _settle(shares)
    shares = _spread_adjustment(
        shares, int(adjustment or 0), adjustment_branch_id=adjustment_branch_id, rule=rule,
    )
    for share in shares:
        share.amount_due += share.adjustment
    shares.sort(key=lambda s: _key_order(s.key))
    return ReturnFigures(
        lines=lines, shares=shares, credit_from=source,
        late_items=late_items(lines, filing.period_start),
    )


def _apply_figures(filing, figures, *, adjustment) -> None:
    """Copy the return-level totals from ``figures`` onto ``filing`` (not saved)."""
    filing.gross_liability = figures.gross
    filing.recoverable_amount = figures.recoverable
    filing.brought_forward_credit = figures.brought_forward
    filing.adjustment_amount = int(adjustment or 0)
    filing.declared_line_count = len(figures.lines)
    filing.late_line_count = figures.late_line_count
    filing.late_items = figures.late_items
    filing.recompute_due(save=False)


def _write_shares(filing, figures) -> dict:
    """Make ``filing``'s share rows match ``figures``; returns them by key.

    A share that no longer has figures is deleted, unless remittances were once
    recorded against it, in which case it is kept at nil so its history stays.
    """
    from .models import TaxFilingShare

    existing = {(s.branch_id, s.branch_pending): s for s in filing.shares.all()}
    written = {}
    for fig in figures.shares:
        share = existing.pop(fig.key, None) or TaxFilingShare(
            filing=filing, branch_id=fig.branch_id, branch_pending=fig.pending,
        )
        share.gross_liability = fig.gross
        share.recoverable_amount = fig.recoverable
        share.brought_forward_credit = fig.brought_forward
        share.adjustment_amount = fig.adjustment
        share.amount_due = fig.amount_due
        share.carried_forward_credit = fig.carried_forward
        share.line_count = fig.line_count
        share.refresh_payment_status()
        share.save()
        written[fig.key] = share
    for share in existing.values():
        if share.remittances.exists():
            share.gross_liability = share.recoverable_amount = 0
            share.brought_forward_credit = share.adjustment_amount = 0
            share.amount_due = share.carried_forward_credit = share.line_count = 0
            share.refresh_payment_status()
            share.save()
        else:
            share.delete()
    return written


def _audit_per_share(filing, shares, *, action, actor_user, describe):
    """Audit ``action`` on ``filing`` once per branch share, each under its branch.

    A return is booked per branch, so its trail is too: Lagoon View's VAT
    return has an Ikeja share and a Lekki share, and Lekki's bursar reads
    Lekki's N30,000 without ever seeing Ikeja's figures or the return's total.
    The whole-school total is the sum of the shares' entries. ``describe(share)``
    returns ``(message, metadata)`` for one share. A return with no shares is
    audited once, under its own branch, with ``describe(None)``.
    """
    shares = list(shares)
    for share in shares or [None]:
        message, metadata = describe(share)
        record(
            entity=filing.entity, action=action, actor_user=actor_user, target=filing,
            branch=share.branch_id if share is not None else filing.branch_id,
            message=message[:255], **metadata,
        )


def _whose(share) -> str:
    """How an entry names a share: "Lekki Branch's share", or "the return" for none."""
    if share is None:
        return "the return"
    if share.branch_id is None:
        return "the share with no branch yet" if share.branch_pending else "the return"
    return f"{_branch_name(share.branch_id)}'s share"


def _share_figures(share, filing) -> dict:
    """The figures an entry carries: one share's, or the whole return's when it has none."""
    source = share if share is not None else filing
    return {
        "total": source.amount_due, "tax": source.recoverable_amount,
        "carried_forward": source.carried_forward_credit,
        "lines": share.line_count if share is not None else filing.declared_line_count,
    }


# --------------------------------------------------------------------------- #
# Prepare                                                                      #
# --------------------------------------------------------------------------- #

def prepare_filing(obligation, *, period_start, period_end, due_date=None,
                   currency=None, actor_user=None):
    """Create (or refresh) a DRAFT :class:`TaxFiling` from the undeclared source lines.

    Re-running for the same obligation/period refreshes the existing draft rather
    than duplicating it. Records a durable rejection audit on any
    :class:`FinanceError`.
    """
    try:
        return _prepare_filing_atomic(
            obligation, period_start=period_start, period_end=period_end,
            due_date=due_date, currency=currency, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=obligation.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=obligation,
        )
        raise


@transaction.atomic
def _prepare_filing_atomic(obligation, *, period_start, period_end, due_date,
                           currency, actor_user):
    """Find or start the draft, refuse an overlapping period, and write its figures.

    A new draft may not overlap any other return of the obligation; the exact
    draft for the same period is the refresh path. The due date defaults to the
    obligation's filing day of the month after the period, re-derived on every
    refresh unless the caller names one. A draft carries no penalty: that is
    decided at filing.
    """
    from .models import TaxFiling

    if not obligation.is_active:
        raise TaxFilingError(f"Tax obligation '{obligation.code}' is inactive.")
    if period_end < period_start:
        raise TaxFilingError("The filing period end cannot precede its start.")

    entity = obligation.entity
    filing = TaxFiling.objects.filter(
        entity=entity, obligation=obligation,
        period_start=period_start, period_end=period_end,
        filing_status=TaxFilingStatus.DRAFT,
    ).first()
    if filing is None:
        clash = (
            TaxFiling.objects.filter(
                entity=entity, obligation=obligation,
                period_start__lte=period_end, period_end__gte=period_start,
            )
            .exclude(
                period_start=period_start, period_end=period_end,
                filing_status=TaxFilingStatus.DRAFT,
            )
            .order_by("period_start")
            .first()
        )
        if clash is not None:
            tenant = entity.tenant
            raise TaxFilingError(
                f"Filing period {format_date_range(period_start, period_end, tenant)} "
                f"overlaps existing {obligation.code} filing "
                f"{clash.document_number or clash.pk} "
                f"({format_date_range(clash.period_start, clash.period_end, tenant)}).",
            )
        filing = TaxFiling(
            entity=entity, obligation=obligation,
            period_start=period_start, period_end=period_end,
        )
    filing.due_date = (
        due_date if due_date is not None
        else _default_due_date(period_end, obligation.filing_day)
    )
    filing.currency = currency or filing.currency

    figures = work_out_return(filing)
    _apply_figures(filing, figures, adjustment=0)
    filing.adjustment_account = None
    filing.payment_status = InvoicePaymentStatus.UNPAID
    filing.save()
    shares = _write_shares(filing, figures)

    period = format_date_range(period_start, period_end, entity.tenant)
    _audit_per_share(
        filing, shares.values(), action=FinanceAuditAction.TAX_FILING_PREPARED,
        actor_user=actor_user,
        describe=lambda share: (
            f"Prepared {obligation.code} filing for {period}: {_whose(share)}, "
            f"{format_naira((share or filing).amount_due)} due.",
            _share_figures(share, filing),
        ),
    )
    return filing


# --------------------------------------------------------------------------- #
# File                                                                         #
# --------------------------------------------------------------------------- #

def file_filing(filing, *, filed_date, filing_reference="", adjustment_amount=0,
                adjustment_account=None, adjustment_branch=ANY_SHARE, actor_user=None):
    """Submit a draft return: stamp its lines, post the branch journals, freeze the figures.

    ``adjustment_amount`` (kobo) is a late-filing penalty / interest added to the
    amount due, posted ``Dr adjustment_account (expense), Cr payable`` in the
    branch that bears it (``adjustment_branch``, a Branch or its id, or else spread
    over the branches' tax). Records a durable rejection audit on any
    :class:`FinanceError`.
    """
    try:
        _file_filing_atomic(
            filing, filed_date=filed_date, filing_reference=filing_reference,
            adjustment_amount=adjustment_amount, adjustment_account=adjustment_account,
            adjustment_branch=adjustment_branch, actor_user=actor_user,
        )
        filing.refresh_from_db()
        return filing
    except FinanceError as exc:
        record_rejection(
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=filing,
        )
        raise


def _lock(filing):
    """Lock the obligation, then the return, so one obligation files one return at a time."""
    from .models import TaxFiling, TaxObligation

    TaxObligation.objects.select_for_update().get(pk=filing.obligation_id)
    return TaxFiling.objects.select_for_update(of=("self",)).select_related("obligation").get(pk=filing.pk)


def _branch_id(branch):
    return getattr(branch, "pk", branch)


@transaction.atomic
def _file_filing_atomic(filing, *, filed_date, filing_reference, adjustment_amount,
                        adjustment_account, adjustment_branch, actor_user):
    """Re-collect the lines, check them against the draft, then declare and post.

    The figures a person reviewed on the draft are the figures filed: when the
    source lines have moved since it was prepared (a late invoice, a reversal),
    filing is refused and the draft must be prepared again. A "no branch yet"
    group that moves money is refused too, since its netting and payment would
    belong to no branch.
    """
    from .models import JournalEntry, JournalLine, TaxFilingLine

    filing = _lock(filing)
    if filing.filing_status != TaxFilingStatus.DRAFT:
        raise TaxFilingError(
            f"Filing {filing.document_number or filing.pk} is {state_word(filing, 'filing_status')}, "
            f"only a draft can be filed.",
        )

    adjustment_amount = int(adjustment_amount or 0)
    if adjustment_amount < 0:
        raise TaxFilingError("A penalty/interest adjustment cannot be negative.")
    if adjustment_amount and adjustment_account is None:
        raise TaxFilingError("A penalty/interest adjustment needs an expense account.")

    figures = work_out_return(
        filing, adjustment=adjustment_amount,
        adjustment_branch_id=(
            ANY_SHARE if adjustment_branch is ANY_SHARE else _branch_id(adjustment_branch)
        ),
    )
    drafted = (filing.gross_liability, filing.recoverable_amount, filing.brought_forward_credit)
    now = (figures.gross, figures.recoverable, figures.brought_forward)
    if drafted != now:
        raise TaxFilingError(
            f"Return {filing.document_number or filing.pk} has changed since it was "
            f"prepared (tax {format_naira(drafted[0])}, input {format_naira(drafted[1])}, "
            f"credit {format_naira(drafted[2])} then; {format_naira(now[0])}, "
            f"{format_naira(now[1])}, {format_naira(now[2])} now). "
            f"Prepare it again, check it, then file.",
            failures=["stale_draft"],
        )
    pending = figures.pending
    if pending is not None:
        raise TaxFilingError(
            f"{pending.line_count} line{'s' if pending.line_count != 1 else ''} on this "
            f"return {'come' if pending.line_count != 1 else 'comes'} from entries with no "
            f"branch. The return is booked branch by branch, so give "
            f"{'them' if pending.line_count != 1 else 'it'} a branch first.",
            failures=["unbranched_lines"], unbranched_lines=pending.line_count,
        )

    obligation = filing.obligation
    _apply_figures(filing, figures, adjustment=adjustment_amount)
    filing.adjustment_account = adjustment_account if adjustment_amount else None
    filing.credit_from = figures.credit_from if figures.brought_forward else None

    TaxFilingLine.objects.bulk_create([
        TaxFilingLine(
            filing=filing, journal_line_id=line.id, role=line.role,
            branch_id=line.branch_id, is_late=line.date < filing.period_start,
        )
        for line in figures.lines
    ])
    shares = _write_shares(filing, figures)

    period = resolve_period(filing.entity, filed_date)
    for fig in figures.shares:
        recoverable, penalty = fig.recoverable, fig.adjustment
        if not recoverable and not penalty:
            continue
        entry = JournalEntry.objects.create(
            entity=filing.entity, branch_id=fig.branch_id, date=filed_date, period=period,
            source=JournalSource.TAX, currency=filing.currency,
            narration=f"Tax filing {filing.document_number or ''}: {obligation.code}".strip(),
            created_by=actor_user,
        )
        legs = []
        if recoverable > 0:
            legs += [(obligation.liability_account, recoverable, 0, "Net input tax against output"),
                     (obligation.recoverable_account, 0, recoverable, "Clear recoverable input tax")]
        elif recoverable < 0:
            legs += [(obligation.recoverable_account, -recoverable, 0, "Clear net input tax reversal"),
                     (obligation.liability_account, 0, -recoverable, "Input tax reversal added to payable")]
        if penalty > 0:
            legs += [(adjustment_account, penalty, 0, "Tax penalty / interest"),
                     (obligation.liability_account, 0, penalty, "Penalty added to payable")]
        for line_no, (account, debit, credit, description) in enumerate(legs, start=1):
            JournalLine.objects.create(
                entry=entry, account=account, debit=debit, credit=credit,
                description=description, line_no=line_no,
            )
        post_journal(entry, actor_user=actor_user)
        share = shares[fig.key]
        share.filing_journal = entry
        share.save(update_fields=["filing_journal", "updated_at"])

    filing.filing_reference = filing_reference
    filing.filed_at = filed_date
    filing.filing_status = TaxFilingStatus.FILED
    filing.refresh_payment_status(save=False)
    filing.save()

    label = filing.document_number or filing.pk
    _audit_per_share(
        filing, shares.values(), action=FinanceAuditAction.TAX_FILING_FILED,
        actor_user=actor_user,
        describe=lambda share: (
            f"Filed {obligation.code} return {label}: {_whose(share)}, "
            f"{format_naira((share or filing).amount_due)} due.",
            {**_share_figures(share, filing),
             "journal_id": share.filing_journal_id if share is not None else None},
        ),
    )
    return filing


# --------------------------------------------------------------------------- #
# Unfile                                                                       #
# --------------------------------------------------------------------------- #

def unfile_filing(filing, *, actor_user=None):
    """Revert a FILED return to DRAFT: reverse its journals and release its lines.

    The undo for a return filed in error, before any remittance stands. A return
    with a payment recorded is refused (reverse the remittance first), and so is
    one whose carried-forward credit a later filed return has used. Records a
    durable rejection audit on any :class:`FinanceError`.
    """
    try:
        _unfile_filing_atomic(filing, actor_user=actor_user)
        filing.refresh_from_db()
        return filing
    except FinanceError as exc:
        record_rejection(
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=filing,
        )
        raise


@transaction.atomic
def _unfile_filing_atomic(filing, *, actor_user=None):
    from .models import TaxFiling, TaxFilingShare
    from .posting import reverse_journal

    filing = _lock(filing)
    label = filing.document_number or filing.pk
    if filing.filing_status == TaxFilingStatus.PAID:
        raise TaxFilingError(
            f"Filing {label} is PAID; reverse the remittance before un-filing it.",
        )
    if filing.filing_status != TaxFilingStatus.FILED:
        raise TaxFilingError(
            f"Filing {label} is {state_word(filing, 'filing_status')}, only a filed return can be un-filed.",
        )
    if int(filing.amount_paid or 0) > 0:
        raise TaxFilingError(
            "This filing carries a remittance; reverse the payment before un-filing it.",
        )
    later = TaxFiling.objects.filter(credit_from=filing).first()
    if later is not None:
        raise TaxFilingError(
            f"Return {later.document_number or later.pk} uses the credit this return "
            f"carried forward; un-file it first.",
        )

    reversed_by_branch = {}
    for share in filing.shares.exclude(filing_journal=None).select_related("filing_journal"):
        reverse_journal(share.filing_journal, actor_user=actor_user, document_owner=share)
        reversed_by_branch[share.branch_id] = share.filing_journal_id
        share.filing_journal = None
        share.save(update_fields=["filing_journal", "updated_at"])
    if filing.filing_journal_id is not None:
        reverse_journal(filing.filing_journal, actor_user=actor_user, document_owner=filing)
        reversed_by_branch.setdefault(filing.filing_journal.branch_id, filing.filing_journal_id)
        filing.filing_journal = None

    released = dict(
        filing.declared_lines.order_by().values("branch_id")
        .annotate(n=Count("id")).values_list("branch_id", "n")
    )
    filing.declared_lines.all().delete()
    filing.credit_from = None
    filing.filed_at = None
    filing.filing_reference = ""
    filing.filing_status = TaxFilingStatus.DRAFT

    figures = work_out_return(filing)
    _apply_figures(filing, figures, adjustment=0)
    filing.adjustment_account = None
    filing.payment_status = InvoicePaymentStatus.UNPAID
    filing.save()
    shares = list(_write_shares(filing, figures).values())
    for branch_id in (reversed_by_branch.keys() | released.keys()) - {s.branch_id for s in shares}:
        shares.append(TaxFilingShare(filing=filing, branch_id=branch_id))

    def describe(share):
        branch_id = share.branch_id if share is not None else filing.branch_id
        return (
            f"Un-filed {filing.obligation.code} return {label} back to draft: {_whose(share)}.",
            {"journal_id": reversed_by_branch.get(branch_id),
             "released_lines": released.get(branch_id, 0)},
        )

    _audit_per_share(
        filing, shares, action=FinanceAuditAction.TAX_FILING_UNFILED,
        actor_user=actor_user, describe=describe,
    )
    return filing


# --------------------------------------------------------------------------- #
# Pay (one remittance journal per branch share)                                #
# --------------------------------------------------------------------------- #

def pay_filing(filing, *, bank_account, pay_date, amount=None, branch=ANY_SHARE,
               actor_user=None, reach=None):
    """Remit one branch share of a filed return: ``Dr payable, Cr bank``.

    Every share is paid only from its own branch's bank account
    (:meth:`BranchRule.bank_branch_id`): at a tenant with several branches an
    account with no branch pays no share; at a tenant with one branch its
    accounts are that branch's.

    ``branch`` (a Branch or its id) names the share to pay. Without it: the only unpaid share,
    else the unpaid share of the bank account's branch. ``amount`` defaults to the
    share's balance. ``reach`` (a set of branch ids, or ``None`` for the whole
    tenant) refuses a share outside the caller's branches. Records a durable
    rejection audit on any :class:`FinanceError`.
    """
    try:
        _pay_filing_atomic(
            filing, bank_account=bank_account, pay_date=pay_date,
            amount=amount, branch=branch, actor_user=actor_user, reach=reach,
        )
        filing.refresh_from_db()
        return filing
    except FinanceError as exc:
        record_rejection(
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=filing,
        )
        raise


def pay_filing_shares(filing, *, payments, pay_date, actor_user=None, reach=None):
    """Several branch payments of one return on one date, recorded together or not at all.

    ``payments`` is a list of ``(branch, bank_account, amount)`` read as
    :func:`pay_filing` reads its arguments (``branch`` may be :data:`ANY_SHARE`,
    ``amount`` may be ``None``), and ``reach`` is applied to each. Records a
    durable rejection audit on any :class:`FinanceError`.
    """
    try:
        with transaction.atomic():
            for branch, bank_account, amount in payments:
                _pay_filing_atomic(
                    filing, bank_account=bank_account, pay_date=pay_date,
                    amount=amount, branch=branch, actor_user=actor_user, reach=reach,
                )
        filing.refresh_from_db()
        return filing
    except FinanceError as exc:
        record_rejection(
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=filing,
        )
        raise


def _shares_to_pay(filing, rule, bank_account):
    """The return's shares, with one made for a return written without any.

    A return created outside :func:`prepare_filing` has figures and no shares; it
    is paid as one share under its own branch, else the tenant's only branch, else
    the paying account's branch.
    """
    from .models import TaxFilingShare

    shares = list(filing.shares.select_for_update(of=("self",)).order_by("id"))
    if shares or filing.amount_due <= 0:
        return shares
    branch_id = filing.branch_id or rule.only_branch_id or bank_account.branch_id
    share = TaxFilingShare.objects.create(
        filing=filing, branch_id=branch_id,
        gross_liability=filing.gross_liability, recoverable_amount=filing.recoverable_amount,
        brought_forward_credit=filing.brought_forward_credit,
        adjustment_amount=filing.adjustment_amount, amount_due=filing.amount_due,
        amount_paid=filing.amount_paid, carried_forward_credit=filing.carried_forward_credit,
        payment_status=filing.payment_status,
    )
    return [share]


def _branch_name(branch_id):
    from vs_tenants.models import Branch

    if branch_id is None:
        return "the tenant"
    branch = Branch.all_objects.filter(pk=branch_id).only("name").first()
    return branch.name if branch else f"branch {branch_id}"


@transaction.atomic
def _pay_filing_atomic(filing, *, bank_account, pay_date, amount, branch, actor_user,
                       reach=None):
    from .chronology import ensure_on_or_after
    from .models import JournalEntry, JournalLine, TaxRemittance

    filing = _lock(filing)
    label = filing.document_number or filing.pk
    if filing.filing_status not in FILED_STATUSES:
        raise TaxFilingError("Only a filed return can be remitted.")
    if bank_account.entity_id != filing.entity_id:
        raise TaxFilingError("The bank account belongs to a different entity.")
    ensure_on_or_after(
        subject=f"Tax remittance for {label}",
        subject_date=pay_date,
        source=f"tax filing {label}",
        source_date=filing.filed_at,
        remedy=f"Date the remittance {format_date(filing.filed_at, filing.entity.tenant)} or later.",
        tenant=filing.entity.tenant,
    )

    rule = branch_rule(filing.entity)
    bank_branch = rule.bank_branch_id(bank_account)
    if bank_branch is None:
        raise TaxFilingError(
            f"{bank_account.name} is not any one branch's account. Each branch's share of "
            f"the return is paid from that branch's own account.",
            failures=["bank_account"],
        )
    open_shares = [s for s in _shares_to_pay(filing, rule, bank_account) if s.balance_due > 0]
    if not open_shares:
        raise TaxFilingError("This filing has no outstanding balance to remit.")

    def share_branch(share):
        return share.branch_id or rule.only_branch_id or bank_branch

    if branch is not ANY_SHARE:
        wanted = _branch_id(branch)
        targets = [s for s in open_shares if s.branch_id == wanted]
        if not targets:
            raise TaxFilingError(f"{_branch_name(wanted)} owes nothing more on return {label}.")
    elif len(open_shares) == 1:
        targets = open_shares
    else:
        targets = [s for s in open_shares if share_branch(s) == bank_branch][:1]
        if not targets:
            raise TaxFilingError(
                f"{_branch_name(bank_branch)} owes nothing more on return {label}; "
                f"name the branch whose share this pays.",
            )

    obligation = filing.obligation
    period = resolve_period(filing.entity, pay_date)
    paid = []
    for share in targets:
        journal_branch = share_branch(share)
        if journal_branch != bank_branch:
            raise TaxFilingError(
                f"This share of the return belongs to {_branch_name(journal_branch)}. "
                f"Pay it from {_branch_name(journal_branch)}'s own account.",
                failures=["bank_account"],
            )
        if reach is not None and journal_branch not in reach:
            raise TaxFilingError(
                f"This share of the return belongs to {_branch_name(journal_branch)}, "
                f"which is outside your branches.",
                failures=["branch"],
            )
        pay = share.balance_due if amount is None else min(int(amount), share.balance_due)
        if pay <= 0:
            raise TaxFilingError("Remittance amount must be positive.")
        entry = JournalEntry.objects.create(
            entity=filing.entity, branch_id=journal_branch, date=pay_date, period=period,
            source=JournalSource.TAX, currency=filing.currency,
            narration=f"Remit {obligation.code} {filing.document_number or ''}".strip(),
            created_by=actor_user,
        )
        JournalLine.objects.create(
            entry=entry, account=obligation.liability_account, debit=pay, credit=0,
            description=f"{obligation.code} remitted to {obligation.authority_name or 'authority'}",
            line_no=1,
        )
        JournalLine.objects.create(
            entry=entry, account=bank_account.gl_account, debit=0, credit=pay,
            description="Tax remittance paid", line_no=2,
        )
        post_journal(entry, actor_user=actor_user)
        TaxRemittance.objects.create(
            filing=filing, share=share, branch_id=journal_branch, bank_account=bank_account,
            pay_date=pay_date, amount=pay, journal=entry, created_by=actor_user,
        )
        share.amount_paid = int(share.amount_paid) + pay
        share.refresh_payment_status()
        share.save(update_fields=["amount_paid", "payment_status", "updated_at"])
        paid.append((share, pay, entry))

    _refresh_filing_payment(filing)

    for share, pay, entry in paid:
        record(
            entity=filing.entity, action=FinanceAuditAction.TAX_FILING_PAID,
            actor_user=actor_user, target=filing, branch=entry.branch_id,
            message=(
                f"Remitted {format_naira(pay)} of {obligation.code} filing {label}: {_whose(share)}."
            )[:255],
            journal_id=entry.pk, amount=pay, payment_status=share.payment_status,
        )
    return filing


def _refresh_filing_payment(filing) -> None:
    """Total the shares' payments onto the return; PAID when every share is, else FILED."""
    paid = filing.shares.aggregate(p=Sum("amount_paid"))["p"]
    filing.amount_paid = int(paid or 0)
    filing.refresh_payment_status(save=False)
    if filing.payment_status == InvoicePaymentStatus.PAID and filing.amount_due > 0:
        filing.filing_status = TaxFilingStatus.PAID
    elif filing.filing_status == TaxFilingStatus.PAID:
        filing.filing_status = TaxFilingStatus.FILED
    filing.save(update_fields=["amount_paid", "payment_status", "filing_status", "updated_at"])


# --------------------------------------------------------------------------- #
# Reverse a remittance                                                         #
# --------------------------------------------------------------------------- #

def reverse_remittance(remittance, *, reason, date=None, actor_user=None):
    """Undo a tax payment recorded in error; the return goes back to FILED.

    Reverses the remittance journal (on its own date where that month is open,
    else today, or on ``date``), keeps the remittance row marked reversed, and
    takes the amount off its share and its return. Records a durable rejection
    audit on any :class:`FinanceError`.
    """
    try:
        return _reverse_remittance_atomic(
            remittance, reason=reason, date=date, actor_user=actor_user,
        )
    except FinanceError as exc:
        record_rejection(
            entity=remittance.filing.entity, action=FinanceAuditAction.TAX_FILING_REJECTED,
            exc=exc, actor_user=actor_user, target=remittance.filing,
        )
        raise


@transaction.atomic
def _reverse_remittance_atomic(remittance, *, reason, date, actor_user):
    from .models import TaxFilingShare, TaxRemittance
    from .posting import reverse_journal

    reason = str(reason or "").strip()
    if not reason:
        raise TaxFilingError("Give the reason the payment is being reversed.", failures=["reason"])
    filing = _lock(remittance.filing)
    remittance = TaxRemittance.objects.select_for_update(of=("self",)).get(pk=remittance.pk)
    if remittance.is_reversed:
        raise TaxFilingError("This remittance has already been reversed.")

    reversal = reverse_journal(
        remittance.journal, actor_user=actor_user, date=date, document_owner=remittance,
    )
    remittance.reversal_journal = reversal
    remittance.reversed_at = reversal.date
    remittance.reversal_reason = reason[:255]
    remittance.save(update_fields=[
        "reversal_journal", "reversed_at", "reversal_reason", "updated_at",
    ])

    share = TaxFilingShare.objects.select_for_update(of=("self",)).get(pk=remittance.share_id)
    share.amount_paid = max(int(share.amount_paid) - int(remittance.amount), 0)
    share.refresh_payment_status()
    share.save(update_fields=["amount_paid", "payment_status", "updated_at"])
    _refresh_filing_payment(filing)

    record(
        entity=filing.entity, action=FinanceAuditAction.TAX_REMITTANCE_REVERSED,
        actor_user=actor_user, target=filing, branch=remittance.branch_id,
        message=(
            f"Reversed a {format_naira(remittance.amount)} remittance of "
            f"{filing.obligation.code} return {filing.document_number or filing.pk}: {reason}"
        )[:255],
        journal_id=remittance.journal_id, reversal_id=reversal.pk,
        amount=int(remittance.amount), branch_id=remittance.branch_id, reason=reason,
    )
    return remittance


# --------------------------------------------------------------------------- #
# Read-only - what each obligation currently owes                              #
# --------------------------------------------------------------------------- #

def outstanding_obligations(entity, branch_ids=None) -> list:
    """Per-obligation snapshot of the unremitted balance sitting in each control account.

    The running net credit balance of each active obligation's ``liability_account`` (less
    any recoverable input balance), i.e. what would be owed if a return were filed for all
    activity to date. Returns one dict per active obligation.

    ``branch_ids`` (a branch-bound reader's reach; ``None`` for the whole tenant)
    counts only the lines of those branches, by entry branch as a return counts
    them (:class:`BranchRule`): lines with no branch belong to a branch only at a
    tenant with exactly one.
    """
    from .models import TaxObligation

    rule = branch_rule(entity) if branch_ids is not None else None
    narrow = {"branch_ids": branch_ids, "rule": rule}
    rows = []
    qs = (
        TaxObligation.objects
        .filter(entity=entity, is_active=True)
        .select_related("liability_account", "recoverable_account")
        .order_by("code")
    )
    for ob in qs:
        debit, credit = _account_movement(entity, ob.liability_account, **narrow)
        payable = credit - debit
        recoverable = 0
        if ob.recoverable_account_id:
            rdebit, rcredit = _account_movement(entity, ob.recoverable_account, **narrow)
            recoverable = rdebit - rcredit
        net = max(payable - max(recoverable, 0), 0)
        rows.append({
            "obligation_id": ob.id, "code": ob.code, "name": ob.name,
            "obligation_type": ob.obligation_type,
            "authority_name": ob.authority_name,
            "liability_code": ob.liability_account.code,
            "payable_balance": payable,
            "recoverable_balance": recoverable,
            "net_outstanding": net,
        })
    return rows
