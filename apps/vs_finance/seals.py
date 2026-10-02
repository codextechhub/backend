"""Proof that a closed period's figures are the ones that were closed.

Closing a month stops new postings, but on its own it proves nothing years
later: if somebody changed a journal line in 2030 by hand, the 2027 statements
would simply read differently and nobody could say which version was locked.
So every close and lock writes a :class:`~vs_finance.models.LedgerSeal`: the
closing balance of each account per branch, and a checksum over the ledger
lines the period covers, chained to the entity's previous seal.

:func:`verify_entity` recomputes both from the ledger and compares. It is read
by the read-only verify endpoint, the ``verify_ledger_seals`` command (which
can open a health incident) and a close-checklist warning, so a difference is
reported wherever somebody is looking at the books.

Example. Bright Star School locks March 2027 with Cash at Ikeja reading
N4,200,000. In 2030 a support engineer edits one of March's receipt lines from
the database shell. Nothing refuses it, but the next verification says March
2027's lines no longer match their checksum, and that Cash at Ikeja read
N4,200,000 when it was sealed and reads N4,150,000 now.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from django.db.models import Q, Sum

from .constants import PeriodStatus

#: The statuses in which a period or year is sealed and its latest seal must hold.
SEALED_STATUSES = (PeriodStatus.CLOSED, PeriodStatus.LOCKED)


def _sole_branch_id(entity):
    """The tenant's only branch, which a line naming no branch belongs to; else ``None``."""
    from vs_tenants.models import Branch

    ids = list(Branch.all_objects.filter(tenant_id=entity.tenant_id).values_list("pk", flat=True)[:2])
    return ids[0] if len(ids) == 1 else None


def _branch_key(branch_id, sole) -> str:
    branch_id = branch_id if branch_id is not None else sole
    return "" if branch_id is None else str(branch_id)


def _period_lines(entity, period):
    from .branch_ledger import ledger_lines

    return ledger_lines(entity).filter(entry__period=period)


def _year_lines(entity, fiscal_year):
    from .branch_ledger import ledger_lines

    return ledger_lines(entity).filter(entry__period__fiscal_year=fiscal_year)


def _period_cumulative(entity, period):
    """Every ledger line through ``period``'s last day, without year-end closing entries."""
    from .branch_ledger import ledger_lines

    return ledger_lines(entity).filter(
        entry__period__end_date__lte=period.end_date, entry__period__is_closing=False,
    )


def _year_cumulative(entity, fiscal_year):
    """Every ledger line through the year's last day, its closing entries included."""
    from .branch_ledger import ledger_lines

    return ledger_lines(entity).filter(entry__period__end_date__lte=fiscal_year.end_date)


def lines_digest(lines, sole) -> tuple[int, str]:
    """``(count, sha256)`` over ``lines`` in id order, one canonical row per line."""
    digest = hashlib.sha256()
    count = 0
    rows = lines.order_by("pk").values_list(
        "pk", "entry_id", "entry__branch_id", "counterparty_branch_id",
        "account_id", "debit", "credit",
    )
    for pk, entry_id, branch_id, counterparty_id, account_id, debit, credit in rows.iterator(
        chunk_size=5000,
    ):
        digest.update(
            f"{pk}|{entry_id}|{_branch_key(branch_id, sole)}|{counterparty_id or ''}|"
            f"{account_id}|{int(debit)}|{int(credit)}\n".encode()
        )
        count += 1
    return count, digest.hexdigest()


def balances_of(lines, sole) -> dict:
    """``{branch: {account: [debit, credit]}}`` summed over ``lines``, zero rows left out."""
    out: dict = {}
    rows = lines.values("entry__branch_id", "account_id").annotate(d=Sum("debit"), c=Sum("credit"))
    for row in rows:
        debit, credit = int(row["d"] or 0), int(row["c"] or 0)
        if not debit and not credit:
            continue
        branch = out.setdefault(_branch_key(row["entry__branch_id"], sole), {})
        current = branch.get(str(row["account_id"]), [0, 0])
        branch[str(row["account_id"])] = [current[0] + debit, current[1] + credit]
    return out


def seal_checksum(*, entity_id, fiscal_year_id, period_id, kind, line_count,
                  lines_checksum, balances, previous_checksum) -> str:
    """SHA-256 over a seal's figures and the checksum of the seal before it."""
    payload = json.dumps(
        {
            "entity": entity_id, "fiscal_year": fiscal_year_id, "period": period_id,
            "kind": kind, "line_count": line_count, "lines_checksum": lines_checksum,
            "balances": balances, "previous": previous_checksum or "",
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _write_seal(entity, *, fiscal_year, period, kind, lines, cumulative, actor_user):
    from .models import LedgerEntity, LedgerSeal

    # One chain at a time; NO KEY UPDATE leaves postings free to reference the entity.
    LedgerEntity.objects.select_for_update(no_key=True).only("pk").get(pk=entity.pk)
    sole = _sole_branch_id(entity)
    count, checksum = lines_digest(lines, sole)
    balances = balances_of(cumulative, sole)
    previous = LedgerSeal.objects.filter(entity=entity).order_by("-pk").first()
    return LedgerSeal.objects.create(
        entity=entity, fiscal_year=fiscal_year, period=period, kind=kind,
        sealed_by=actor_user if getattr(actor_user, "is_authenticated", False) else None,
        line_count=count, lines_checksum=checksum, balances=balances, previous=previous,
        seal_checksum=seal_checksum(
            entity_id=entity.pk, fiscal_year_id=fiscal_year.pk,
            period_id=getattr(period, "pk", None), kind=kind, line_count=count,
            lines_checksum=checksum, balances=balances,
            previous_checksum=previous.seal_checksum if previous else "",
        ),
    )


def seal_period(period, *, locked=False, actor_user=None):
    """Seal ``period``'s figures as they stand; called inside the close or lock."""
    from .models import LedgerSeal

    entity = period.entity
    return _write_seal(
        entity, fiscal_year=period.fiscal_year, period=period,
        kind=LedgerSeal.Kind.PERIOD_LOCKED if locked else LedgerSeal.Kind.PERIOD_CLOSED,
        lines=_period_lines(entity, period), cumulative=_period_cumulative(entity, period),
        actor_user=actor_user,
    )


def seal_fiscal_year(fiscal_year, *, actor_user=None):
    """Seal a closed year's figures, closing entries included; called inside the close."""
    from .models import LedgerSeal

    entity = fiscal_year.entity
    return _write_seal(
        entity, fiscal_year=fiscal_year, period=None, kind=LedgerSeal.Kind.YEAR_CLOSED,
        lines=_year_lines(entity, fiscal_year),
        cumulative=_year_cumulative(entity, fiscal_year), actor_user=actor_user,
    )


# --------------------------------------------------------------------------- #
# Verification                                                                 #
# --------------------------------------------------------------------------- #

@dataclass
class SealCheck:
    """One seal recomputed against today's ledger."""

    seal: object
    lines_match: bool = True
    seal_intact: bool = True
    line_count_now: int = 0
    differences: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.lines_match and self.seal_intact and not self.differences

    @property
    def label(self) -> str:
        seal = self.seal
        if seal.period_id:
            return f"{seal.period.name} (FY{seal.fiscal_year.year})"
        return f"FY{seal.fiscal_year.year}"


def _diff(sealed: dict, now: dict) -> list:
    out = []
    for branch in sorted(set(sealed) | set(now)):
        then_accounts, now_accounts = sealed.get(branch, {}), now.get(branch, {})
        for account in sorted(set(then_accounts) | set(now_accounts), key=int):
            before = then_accounts.get(account, [0, 0])
            after = now_accounts.get(account, [0, 0])
            if list(before) != list(after):
                out.append({
                    "branch_id": int(branch) if branch else None,
                    "account_id": int(account),
                    "sealed": {"debit": before[0], "credit": before[1]},
                    "now": {"debit": after[0], "credit": after[1]},
                })
    return out


def verify_seal(seal, *, balances=True) -> SealCheck:
    """Recompute ``seal`` from the ledger: its own checksum, its lines, its balances.

    ``balances=False`` skips the cumulative balances, the costly part on a long
    ledger. The lines checksum alone detects any change to a sealed period's
    own lines; the balances say what the change did to each account.
    """
    entity = seal.entity
    sole = _sole_branch_id(entity)
    check = SealCheck(seal=seal)
    check.seal_intact = seal.seal_checksum == seal_checksum(
        entity_id=seal.entity_id, fiscal_year_id=seal.fiscal_year_id,
        period_id=seal.period_id, kind=seal.kind, line_count=seal.line_count,
        lines_checksum=seal.lines_checksum, balances=seal.balances,
        previous_checksum=seal.previous.seal_checksum if seal.previous_id else "",
    )
    if seal.period_id:
        lines = _period_lines(entity, seal.period)
        cumulative = _period_cumulative(entity, seal.period)
    else:
        lines = _year_lines(entity, seal.fiscal_year)
        cumulative = _year_cumulative(entity, seal.fiscal_year)
    check.line_count_now, digest = lines_digest(lines, sole)
    check.lines_match = digest == seal.lines_checksum and check.line_count_now == seal.line_count
    if balances:
        check.differences = _diff(seal.balances or {}, balances_of(cumulative, sole))
    return check


def current_seals(entity, *, fiscal_years=None):
    """The latest seal of every period and year of ``entity`` that is still sealed."""
    from .models import LedgerSeal

    seals = (
        LedgerSeal.objects.filter(entity=entity)
        .filter(
            Q(period__isnull=False, period__status__in=SEALED_STATUSES)
            | Q(period__isnull=True, fiscal_year__status__in=SEALED_STATUSES)
        )
        .select_related("period", "fiscal_year", "previous", "entity")
        .order_by("-pk")
    )
    if fiscal_years is not None:
        seals = seals.filter(fiscal_year__in=fiscal_years)
    latest, seen = [], set()
    for seal in seals:
        key = ("period", seal.period_id) if seal.period_id else ("year", seal.fiscal_year_id)
        if key not in seen:
            seen.add(key)
            latest.append(seal)
    return sorted(latest, key=lambda s: s.pk)


def chain_breaks(entity) -> list:
    """Seals whose ``previous`` is not the seal written just before them."""
    from .models import LedgerSeal

    breaks, prior = [], None
    for seal in LedgerSeal.objects.filter(entity=entity).order_by("pk").only("pk", "previous_id"):
        if seal.previous_id != (prior.pk if prior else None):
            breaks.append(seal.pk)
        prior = seal
    return breaks


@dataclass
class EntityVerification:
    """Every current seal of one set of books, checked."""

    checks: list
    chain_breaks: list

    @property
    def ok(self) -> bool:
        return not self.chain_breaks and all(c.ok for c in self.checks)

    @property
    def mismatches(self) -> list:
        return [c for c in self.checks if not c.ok]


def verify_entity(entity, *, fiscal_years=None, balances=True) -> EntityVerification:
    """Check every current seal of ``entity`` (or of ``fiscal_years``) and the chain."""
    checks = [
        verify_seal(seal, balances=balances)
        for seal in current_seals(entity, fiscal_years=fiscal_years)
    ]
    return EntityVerification(checks=checks, chain_breaks=chain_breaks(entity))


def describe(check: SealCheck) -> str:
    """One sentence a person reads about a failed check."""
    parts = []
    if not check.seal_intact:
        parts.append("the seal's own figures were altered")
    if not check.lines_match:
        parts.append(
            f"its ledger lines no longer match their checksum "
            f"({check.seal.line_count} sealed, {check.line_count_now} now)"
        )
    if check.differences:
        parts.append(f"{len(check.differences)} account balance(s) differ from the sealed figures")
    return f"{check.label}: " + "; ".join(parts) + "."


def sealed_figures_close_check(entity, period):
    """Close-checklist warning: the earlier seals of this year and last still hold.

    Covers the seals of the period's own fiscal year and the year before, the
    window in which corrections are normally made, so a close never re-reads a
    decade of ledger, and compares line checksums only, which is enough to see
    a change; ``verify_ledger_seals`` and the verify endpoint also say what each
    change did to the balances. A warning, not
    a block: a mismatch in a LOCKED month can never be resealed, so blocking on
    it would stop every later close for good; it is reported for the
    accountant to investigate.
    """
    from .close import ChecklistItem
    from .models import FiscalYear

    years = FiscalYear.objects.filter(
        entity=entity, start_date__lte=period.fiscal_year.start_date,
    ).order_by("-start_date")[:2]
    result = verify_entity(entity, fiscal_years=list(years), balances=False)
    if result.ok:
        return ChecklistItem(
            name="sealed_figures_unchanged", passed=True, blocking=False,
            detail=f"{len(result.checks)} sealed period(s) and year(s) still match the ledger",
        )
    details = [describe(c) for c in result.mismatches]
    if result.chain_breaks:
        details.append(f"{len(result.chain_breaks)} seal(s) do not follow the seal before them.")
    return ChecklistItem(
        name="sealed_figures_unchanged", passed=False, blocking=False,
        detail=" ".join(details),
    )
