"""Period-close services - the controlled sealing of an accounting period.

Closing a period is the control that stops the past being silently rewritten. Before a
period is sealed, the close runs a **checklist** of integrity checks and posts the
auto-entries a period needs (depreciation), then transitions the period's status
through the 4-state lock (OPEN → SOFT_CLOSED → CLOSED → LOCKED).

Decoupling note: this lives in ``vs_finance`` and therefore knows only finance-native
invariants (trial balance balanced, AR sub-ledger == control, all due depreciation
posted). The AP / GR-IR reconciliations live in ``vs_procurement`` - which depends on
finance, not the other way round - so finance must never import them.

They reach the close through :func:`register_close_check`, which a dependent app calls
from its ``AppConfig.ready``. The registry inverts the dependency the same way the
workflow handlers and export datasets already do. It replaced an ``extra_checks``
argument that only a test ever passed: the seam existed, no caller used it, so in
practice *every* close ran without the payables checks and could seal a period over an
unreconciled AP position while reporting success. ``extra_checks`` is still accepted
for a caller that needs a one-off check, and the registry is applied on top of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from django.db import transaction
from django.utils import timezone

from vs_config.display import format_date

from .audit import record
from .account_mappings import resolve_mapped_account
from .constants import (
    AR_PLAIN,
    AssetStatus,
    AccountMappingKey,
    DocumentStatus,
    FinanceAuditAction,
    PeriodStatus,
)
from .exceptions import PeriodCloseError
from .money import format_naira
from .wording import counted, period_label, period_status_word

#: Checks contributed by dependent apps, in registration order. Populated at startup
#: from each app's ``ready()``; see :func:`register_close_check`.
_REGISTERED_CHECKS: list = []


def register_close_check(check):
    """Register a close check contributed by an app that depends on finance.

    ``check`` is called as ``check(entity, period)`` and returns a
    :class:`ChecklistItem`, an iterable of them, or ``None`` when it has nothing to say
    for that entity. A check that can reconcile one branch sets ``supports_branch``
    and accepts ``branch=``; branch close omits tenant-only checks rather than showing
    another branch's result. Registration is idempotent, so a module imported twice
    does not double the check.

    A check that raises is reported as a *failed* blocking item rather than being
    allowed to escape. A close is a control: a check that cannot answer is not the same
    as a check that passed, and silently dropping it would recreate the defect this
    registry exists to fix.
    """
    if check not in _REGISTERED_CHECKS:
        _REGISTERED_CHECKS.append(check)
    return check


def registered_close_checks() -> list:
    """The registered checks, for tests and diagnostics."""
    return list(_REGISTERED_CHECKS)


#: Checks the fiscal-year close runs before it posts anything, in registration order.
#: Populated at startup from each app's ``ready()``; see :func:`register_year_close_check`.
_REGISTERED_YEAR_CHECKS: list = []


def register_year_close_check(check):
    """Register a check that :func:`close_fiscal_year` runs before sealing a year.

    ``check`` is called as ``check(entity, fiscal_year)`` and answers the way a
    period check does (:func:`register_close_check`): a :class:`ChecklistItem`, an
    iterable of them, or ``None``. Branch-aware checks use the same
    ``supports_branch`` and ``branch=`` contract. Registration is idempotent, and a
    check that raises fails the close as a blocking item.

    A year close has checks of its own because some things can only go wrong at the
    year boundary: a charge dated in a closed year can never post afterwards. Finance
    registers its own year checks here from ``AppConfig.ready`` through the same
    seam a dependent app uses, so the year close never lists them by hand.
    """
    if check not in _REGISTERED_YEAR_CHECKS:
        _REGISTERED_YEAR_CHECKS.append(check)
    return check


def registered_year_close_checks() -> list:
    """The registered year-close checks, for tests and diagnostics."""
    return list(_REGISTERED_YEAR_CHECKS)


def _run_registered_check(check, *args, **kwargs) -> list:
    """Run one registered check and return its items; a raising check fails, blocking."""
    try:
        result = check(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a broken check must not pass silently.
        return [ChecklistItem(
            name=getattr(check, "check_name", getattr(check, "__name__", "registered_check")),
            passed=False,
            detail=f"check raised {type(exc).__name__}: {exc}",
        )]
    if result is None:
        return []
    return [result] if isinstance(result, ChecklistItem) else list(result)


@dataclass
class ChecklistItem:
    """One pre-close check: whether it passed, and a line saying why.

    ``blocking`` decides whether a failure stops the close unless it is forced; a
    non-blocking item is a warning, shown so a figure is seen before the close.

    ``close_settles`` is set by a check whose outstanding work the close does itself
    before it checks again: due depreciation, which the close posts, and deferred
    income falling due, which it releases. It is the line the preview shows instead
    of ``detail`` (say "6 depreciation charges are due; closing the period posts
    them."). The preview (:func:`close_checklist` with ``preview``) then reports the
    item as passed with ``done_by_close`` set, because pressing Close will settle it,
    while the close itself still evaluates the item after its own steps, and fails
    it when the caller told the close not to take that step.
    """

    name: str
    passed: bool
    blocking: bool = True
    detail: str = ""
    done_by_close: bool = False
    close_settles: str = ""
    title: str = ""

    @property
    def label(self) -> str:
        """The check's name on the close checklist: "AR reconciled (what customers owe)".

        The checklist is an accountant's screen, so a check named by an accounting
        term carries the plain words beside it. ``name`` stays the machine code a
        screen keys on (``ar_reconciled``) and is never put in a sentence. A check
        contributed by another app passes its own ``title``; finance's own checks
        are named in :data:`CHECK_TITLES`.
        """
        return (
            self.title or CHECK_TITLES.get(self.name)
            or self.name.replace("_", " ").capitalize()
        )


#: How finance's own close checks read on the checklist, by their machine name.
#: An accounting term is paired with its plain words; a title already in plain
#: words stands alone.
CHECK_TITLES = {
    "trial_balance_balanced": "Trial balance agrees (debits equal credits)",
    "no_draft_journals": "No draft journals left in the month",
    "ar_reconciled": f"AR reconciled ({AR_PLAIN})",
    "depreciation_posted": "Depreciation posted",
    "earlier_periods_closed": "Earlier months closed",
    "deferred_income_released": "Deferred income released (fees billed ahead, now earned)",
    "inter_branch_balanced": "Inter-branch balances agree (what branches owe each other)",
    "sealed_figures_unchanged": "Closed figures unchanged",
    "depreciation_posted_for_year": "The year's depreciation posted",
}


def failures_sentence(items) -> str:
    """The failed checks of a refusal as one sentence: each one's title and what it found."""
    parts = []
    for item in items:
        detail = str(item.detail or "").strip().rstrip(".")
        parts.append(f"{item.label}: {detail}" if detail else item.label)
    return "; ".join(parts)


@dataclass
class CloseChecklist:
    """The checks for one period, and whether they let it close."""

    period_id: int
    items: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when no *blocking* check failed (non-blocking warnings are allowed)."""
        return all(i.passed for i in self.items if i.blocking)

    @property
    def failures(self) -> list:
        """The blocking checks that failed, as a close refusal names them."""
        return [i for i in self.items if i.blocking and not i.passed]


def _plural(count, one, many):
    return one if count == 1 else many


def _as_done_by_close(item):
    """``item`` as the preview shows it: settled by the close when the close can settle it."""
    if item.passed or not item.close_settles:
        return item
    return replace(item, passed=True, done_by_close=True, detail=item.close_settles)


def close_checklist(entity, period, *, branch=None, extra_checks=None, preview=False,
                    soft=False) -> CloseChecklist:
    """Run the pre-close integrity checks for ``period`` and return the results.

    Every check registered through :func:`register_close_check` runs, plus anything
    passed in ``extra_checks`` for a one-off. ``extra_checks`` entries are zero-arg
    callables returning a :class:`ChecklistItem` (or ``(name, passed, detail)`` tuple);
    registered checks are called with ``(entity, period)``.

    ``preview`` asks what pressing Close would find, which is not what the ledger
    shows today: the close posts due depreciation and releases due deferred income
    before it checks, so the preview reports those as done by the close
    (:class:`ChecklistItem`), assuming the close's own steps are left on as they are
    by default. The preview also carries ``earlier_periods_closed`` while the school
    keeps its months in order (:func:`earlier_period_in_the_way`), for a hard close
    or, with ``soft``, a soft close. The close itself refuses that case outright
    rather than through the checklist, so a forced close cannot override it.
    """
    from vs_rbac.scoping import BranchScope

    from .models import DepreciationSchedule, JournalEntry
    from .reports import reconcile_ar, trial_balance

    branch_id = getattr(branch, "pk", branch)
    scope = BranchScope(frozenset((branch_id,)), include_shared=False) if branch_id else None

    items: list[ChecklistItem] = []

    # Trial balance balances: a tripwire for corruption, since posting keeps it so.
    tb = trial_balance(entity, period=period, scope=scope)
    items.append(ChecklistItem(
        name="trial_balance_balanced", passed=tb.is_balanced,
        detail=(
            "Debits equal credits." if tb.is_balanced
            else f"Debits and credits differ by {format_naira(abs(tb.difference))}."
        ),
    ))

    # Draft journals dated in the period: unposted work left behind, a warning.
    drafts = JournalEntry.objects.filter(
        entity=entity, status=DocumentStatus.DRAFT,
        date__gte=period.start_date, date__lte=period.end_date,
    )
    draft_count = drafts.filter(branch_id=branch_id).count() if branch_id else drafts.count()
    items.append(ChecklistItem(
        name="no_draft_journals", passed=draft_count == 0, blocking=False,
        detail=f"{counted(draft_count, 'draft journal')} dated in period",
    ))

    # AR sub-ledger reconciles to the AR control account.
    ar = reconcile_ar(entity, scope=scope)
    items.append(ChecklistItem(
        name="ar_reconciled", passed=ar.is_reconciled,
        detail=(
            f"Customers' balances total {format_naira(ar.subledger_total)}; the "
            f"receivables account in the ledger holds {format_naira(ar.control_total)}."
        ),
    ))

    # Depreciation due by the period's end has posted; the close posts it first.
    due = DepreciationSchedule.objects.filter(
        asset__entity=entity, asset__asset_status=AssetStatus.ACTIVE,
        is_posted=False, depreciation_date__lte=period.end_date,
    )
    if branch_id:
        due = due.filter(asset__branch_id=branch_id)
    unposted = due.count()
    items.append(ChecklistItem(
        name="depreciation_posted", passed=unposted == 0,
        detail=(
            "Every depreciation charge due by the period's end is posted." if not unposted
            else f"{unposted} due depreciation {_plural(unposted, 'charge is', 'charges are')} "
                 f"not yet posted."
        ),
        close_settles=(
            f"{unposted} depreciation {_plural(unposted, 'charge is', 'charges are')} due; "
            f"closing the period posts {_plural(unposted, 'it', 'them')}."
            if unposted else ""
        ),
    ))

    for check in (extra_checks or []):
        result = check() if callable(check) else check
        if isinstance(result, ChecklistItem):
            items.append(result)
        else:  # (name, passed, detail) tuple
            name, passed, *rest = result
            items.append(ChecklistItem(name=name, passed=passed,
                                       detail=rest[0] if rest else ""))

    # Checks contributed by dependent apps (procurement's AP and GR/IR reconciliations,
    # for one). A check that raises fails the close rather than vanishing from it.
    for check in _REGISTERED_CHECKS:
        if branch_id:
            if not getattr(check, "supports_branch", False):
                continue
            items.extend(_run_registered_check(check, entity, period, branch=branch_id))
            continue
        items.extend(_run_registered_check(check, entity, period))

    if preview:
        items = [_as_done_by_close(item) for item in items]
        order = _close_order_item(entity, period, branch_id=branch_id, soft=soft)
        if order is not None:
            items.insert(0, order)

    return CloseChecklist(period_id=period.id, items=items)


@dataclass
class YearCloseChecklist:
    """The registered year-close checks' results for one fiscal year."""

    fiscal_year_id: int
    items: list = field(default_factory=list)

    passed = CloseChecklist.passed
    failures = CloseChecklist.failures


def year_close_checklist(entity, fiscal_year, *, branch=None) -> YearCloseChecklist:
    """Run every registered year-close check for ``fiscal_year`` (no side effects)."""
    items: list[ChecklistItem] = []
    for check in _REGISTERED_YEAR_CHECKS:
        if branch is not None:
            if not getattr(check, "supports_branch", False):
                continue
            items.extend(_run_registered_check(check, entity, fiscal_year, branch=branch))
        else:
            items.extend(_run_registered_check(check, entity, fiscal_year))
    return YearCloseChecklist(fiscal_year_id=fiscal_year.pk, items=items)


# --------------------------------------------------------------------------- #
# The order periods close and reopen in                                        #
# --------------------------------------------------------------------------- #
#: Statuses that count as closed in the walk. A locked month is no more closed
#: than a closed one for this purpose: both refuse postings.
_SHUT = (PeriodStatus.CLOSED, PeriodStatus.LOCKED)
_SOFT_OR_SHUT = (PeriodStatus.SOFT_CLOSED,) + _SHUT


def periods_close_in_order(entity) -> bool:
    """Whether ``entity``'s school keeps its periods closing in date order (on by default)."""
    from .calendar_settings import resolve_finance_calendar_settings

    return resolve_finance_calendar_settings(entity).periods_close_in_order


def _unit(period, plural=False) -> str:
    """"month" for a period a month long or shorter, else "period"."""
    word = "month" if (period.end_date - period.start_date).days <= 31 else "period"
    return word + ("s" if plural else "")


def _walk(entity, branch_id=None):
    """``entity``'s periods in the order walk, each annotated with ``walk_status``.

    The year's closing period is left out: it opens and closes with its fiscal year
    and is not a month anybody closes. For a branch, a period's status is the
    branch's own row where it has one, and the tenant period's status where it has
    none, as :func:`vs_finance.branch_close.branch_period_state` reads it.
    """
    from django.db.models import F, OuterRef, Subquery
    from django.db.models.functions import Coalesce

    from .models import BranchFiscalPeriod, FiscalPeriod

    rows = FiscalPeriod.objects.filter(entity=entity, is_closing=False)
    if branch_id is None:
        return rows.annotate(walk_status=F("status"))
    own = BranchFiscalPeriod.objects.filter(
        period=OuterRef("pk"), branch_id=branch_id,
    ).values("status")[:1]
    return rows.annotate(walk_status=Coalesce(Subquery(own), F("status")))


def earlier_period_in_the_way(entity, period, *, soft=False, branch=None):
    """The earliest period before ``period`` that is not closed enough for it to close.

    Walking a ledger entity's periods in date order, across fiscal years, statuses
    never get more open going back in time. So a soft close needs every earlier
    period at least SOFT_CLOSED, and a hard close (or a lock) needs every earlier
    period CLOSED or LOCKED. The fiscal year itself need not be closed: December
    2026 must be closed before January 2027 closes, while FY2026 stays open for the
    auditors. ``branch`` walks that branch's own period states.

    Returns ``None`` when nothing is in the way or the school has turned the order
    off (:func:`periods_close_in_order`).
    """
    if not periods_close_in_order(entity):
        return None
    return (
        _walk(entity, getattr(branch, "pk", branch))
        .filter(end_date__lt=period.start_date)
        .exclude(walk_status__in=_SOFT_OR_SHUT if soft else _SHUT)
        .order_by("start_date", "pk")
        .first()
    )


def later_period_in_the_way(entity, period, *, branch=None):
    """The latest period after ``period`` that is not OPEN, so ``period`` cannot reopen.

    Periods reopen from the latest back: reopening August while September is closed
    would leave a closed month after an open one. The latest is named because it is
    the one to reopen first. Returns ``None`` when every later period is OPEN or the
    school has turned the order off.
    """
    if not periods_close_in_order(entity):
        return None
    return (
        _walk(entity, getattr(branch, "pk", branch))
        .filter(start_date__gt=period.end_date)
        .exclude(walk_status=PeriodStatus.OPEN)
        .order_by("-start_date", "-pk")
        .first()
    )


def _branch_name(entity, branch):
    """The branch to name in an order refusal, or ``None`` when naming one adds nothing.

    A school with one branch never hears its branch named: the dimension recedes.
    """
    from vs_rbac.scoping import only_branch_id

    if branch is None or only_branch_id(entity.tenant) is not None:
        return None
    if hasattr(branch, "name"):
        return branch.name
    from vs_tenants.models import Branch

    return Branch.all_objects.filter(pk=branch).values_list("name", flat=True).first()


def _close_order_message(entity, period, blocker, *, soft=False, branch=None, act="close"):
    """The refusal a bursar reads when ``blocker`` must close before ``period`` can.

    ``act`` is "close" or "lock". Names the month in the way and says why, in the
    school's own month names, and at a branch, which branch.
    """
    tenant = entity.tenant
    target, first = period_label(period, tenant), period_label(blocker, tenant)
    units, unit = _unit(period, plural=True), _unit(period)
    where = _branch_name(entity, branch)
    at = f" at {where}" if where else ""
    still_soft = blocker.walk_status == PeriodStatus.SOFT_CLOSED
    verb = "Soft-close or close" if soft else "Close"
    lead = f"{verb} {first}{at} first" + (": it is only soft-closed." if still_soft else ".")
    if act == "lock":
        can = f"{target} can be locked"
    elif soft:
        can = f"{target} can be soft-closed"
    else:
        can = f"{target} can close"
    if where:
        can = f"{where} can {'lock' if act == 'lock' else 'soft-close' if soft else 'close'} {target}"
    need = "soft-closed or closed" if soft else "closed"
    return f"{lead} {units.capitalize()} close in order, so {can} once every earlier {unit} is {need}."


def refuse_out_of_order_close(entity, period, *, soft=False, branch=None, act="close"):
    """Raise :class:`PeriodCloseError` when an earlier period must close first.

    Called by every close and lock, forced or not: a forced close overrides the
    checklist, never the order. Only the school's setting turns it off.
    """
    blocker = earlier_period_in_the_way(entity, period, soft=soft, branch=branch)
    if blocker is not None:
        raise PeriodCloseError(
            _close_order_message(entity, period, blocker, soft=soft, branch=branch, act=act),
            failures=["earlier_periods_closed"], blocking_period=blocker.pk,
        )


def refuse_out_of_order_reopen(entity, period, *, branch=None):
    """Raise :class:`PeriodCloseError` when a later period must reopen first.

    A LOCKED later period never reopens, so the refusal says the period can no
    longer be reopened rather than sending the bursar to reopen it.
    """
    blocker = later_period_in_the_way(entity, period, branch=branch)
    if blocker is None:
        return
    tenant = entity.tenant
    target, latest = period_label(period, tenant), period_label(blocker, tenant)
    units, unit = _unit(period, plural=True), _unit(period)
    where = _branch_name(entity, branch)
    at = f" at {where}" if where else ""
    if blocker.walk_status == PeriodStatus.LOCKED:
        message = (
            f"{latest}{at} is locked, so {target} can no longer be reopened. "
            f"{units.capitalize()} reopen from the latest back, and a locked {unit} never reopens."
        )
    else:
        can = f"{where} can reopen {target}" if where else f"{target} can reopen"
        message = (
            f"Reopen {latest}{at} first. {units.capitalize()} reopen from the latest back, "
            f"so {can} once every later {unit} is open."
        )
    raise PeriodCloseError(message, failures=["later_periods_open"], blocking_period=blocker.pk)


def _close_order_item(entity, period, *, branch_id=None, soft=False):
    """The preview's ``earlier_periods_closed`` item, or ``None`` when the order is off.

    A blocking item, so the preview says before Close is pressed what the close
    would refuse. When the month in the way is only soft-closed and this period is
    still open, a soft close would go ahead, and the item says so.
    """
    if not periods_close_in_order(entity):
        return None
    if branch_id is None:
        from vs_rbac.scoping import only_branch_id

        if only_branch_id(entity.tenant) is None:
            return _whole_school_order_item(entity, period, soft=soft)
    blocker = earlier_period_in_the_way(entity, period, soft=soft, branch=branch_id)
    if blocker is None:
        return ChecklistItem(
            name="earlier_periods_closed", passed=True,
            detail=f"Every earlier {_unit(period)} is {'soft-closed or closed' if soft else 'closed'}.",
        )
    detail = _close_order_message(entity, period, blocker, soft=soft, branch=branch_id)
    if (not soft and blocker.walk_status == PeriodStatus.SOFT_CLOSED
            and period.status == PeriodStatus.OPEN):
        detail += f" {period_label(period, entity.tenant)} can be soft-closed now."
    return ChecklistItem(name="earlier_periods_closed", passed=False, detail=detail)


def _whole_school_order_item(entity, period, *, soft=False):
    """``earlier_periods_closed`` for the All branches view of a school with several branches.

    Each branch closes its own months in order (:func:`refuse_out_of_order_close`
    with its branch), and the school's month closes once every branch has closed
    it. So the whole-school view answers branch by branch, from each branch's own
    months, never from the school's month (which stays open until the last branch
    closes, and would report Lekki's open August as everybody's).

    Bright Star has closed August at Ikeja but not at Lekki. Viewing September
    under All branches:

    * the item names Lekki's August as the month in Lekki's way, says Ikeja can
      close September now, and is a warning, not a blocker: Ikeja's close, and a
      forced close of it, are still open to the bursar exactly as they are when
      Ikeja is chosen;
    * once Lekki's August is closed too, it passes;
    * it blocks only when no branch still to close September can close it, which
      is when every close the bursar could start would be refused.

    A branch that has already closed the month (soft-closed, for a soft close)
    is left out: it has nothing left to wait for.
    """
    from vs_tenants.models import Branch

    from .models import BranchFiscalPeriod

    tenant = entity.tenant
    target = period_label(period, tenant)
    done = _SOFT_OR_SHUT if soft else _SHUT
    own = dict(BranchFiscalPeriod.objects.filter(period=period).values_list("branch_id", "status"))
    ready, blocked = [], []
    for branch in Branch.all_objects.filter(tenant_id=entity.tenant_id).order_by("name", "pk"):
        if own.get(branch.pk, period.status) in done:
            continue
        blocker = earlier_period_in_the_way(entity, period, soft=soft, branch=branch.pk)
        if blocker is None:
            ready.append(branch.name)
        else:
            blocked.append((branch.name, period_label(blocker, tenant)))
    need = "soft-closed or closed" if soft else "closed"
    if not blocked:
        return ChecklistItem(
            name="earlier_periods_closed", passed=True,
            detail=f"Every earlier {_unit(period)} is {need} at every branch.",
        )
    act = "soft-close" if soft else "close"
    waiting = "; ".join(
        f"{name} has not {act}d {first} yet, so {name} can {act} {target} only after it"
        for name, first in blocked
    )
    if ready:
        detail = f"{waiting}. {_joined(ready)} can {act} {target} now."
    else:
        detail = (
            f"{waiting}. {_unit(period, plural=True).capitalize()} close in order at "
            f"each branch, so no branch can {act} {target} yet."
        )
    return ChecklistItem(
        name="earlier_periods_closed", passed=False, blocking=not ready, detail=detail,
    )


def _joined(names) -> str:
    """"Ikeja", "Ikeja and Lekki", "Abuja, Ikeja and Lekki"."""
    names = list(names)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def lock_periods_in_date_order(entity, period, *, before):
    """Lock the periods before (or after) ``period``, oldest first, until commit.

    Two bursars closing August and September at the same moment, or one closing
    September while another reopens August, could otherwise each read the other's
    month in its old state and leave the books out of order. Every writer of a
    period's status holds FOR UPDATE on that period's row, so a close takes
    FOR NO KEY UPDATE on every earlier period and a reopen on every later one before
    reading their statuses: whoever comes second waits for the first to commit and
    then reads what it wrote. The rows are taken in date order, with the closer's
    own row last and the reopener's first, so two of them always queue in the same
    order and never deadlock. NO KEY UPDATE does not conflict with the KEY SHARE a
    posting takes (:func:`vs_finance.posting.read_key_shared`), so postings into
    those months carry on. The year's closing period takes no part.
    """
    from .models import FiscalPeriod

    rows = FiscalPeriod.objects.filter(entity=entity, is_closing=False)
    rows = (
        rows.filter(end_date__lt=period.start_date) if before
        else rows.filter(start_date__gt=period.end_date)
    )
    list(rows.order_by("start_date", "pk").select_for_update(no_key=True)
         .values_list("pk", flat=True))


@transaction.atomic
# Post due depreciation during close.
def run_period_depreciation(entity, period, *, actor_user=None, branch=None):
    """Post all depreciation due on/before this period's end (a close auto-posting).

    Posts into the period even when it is SOFT_CLOSED (``allow_restricted``), which is
    exactly the privileged auto-posting the soft-close state exists for. Returns the
    number of charges posted.
    """
    from .assets import post_depreciation
    from .models import FixedAsset

    count = 0  # Count depreciation schedule rows posted by this close run.
    assets = FixedAsset.objects.filter(entity=entity, asset_status=AssetStatus.ACTIVE)
    if branch is not None:
        assets = assets.filter(branch_id=getattr(branch, "pk", branch))
    for asset in assets:  # Check each active asset for due depreciation.
        if asset.schedule.filter(is_posted=False, depreciation_date__lte=period.end_date).exists():
            posted = post_depreciation(  # Post all due depreciation up to period end.
                asset, up_to_date=period.end_date,  # Period end is the depreciation cutoff.
                actor_user=actor_user, allow_restricted=True,  # Allow close auto-posting in restricted periods.
            )
            count += len(posted)  # Add posted schedule count.
    return count  # Return total charges posted.


#: Longest reason a close override may carry; it is stored on the audit row.
REASON_MAX_LENGTH = 500


def require_reason(reason, *, act):
    """Return ``reason`` stripped, or refuse the act for want of one.

    Forcing a close over its checklist, reopening a period and reopening a year each
    undo a control. The reason is what the audit trail shows the next reader, so a
    blank one is refused as a 400 on ``reason`` rather than recorded as nothing.
    ``act`` completes the sentence ("A reason is required to <act>.").
    """
    from rest_framework.exceptions import ValidationError

    text = str(reason or "").strip()
    if not text:
        raise ValidationError({"reason": f"A reason is required to {act}."})
    if len(text) > REASON_MAX_LENGTH:
        raise ValidationError({
            "reason": f"Keep the reason to {REASON_MAX_LENGTH} characters or fewer.",
        })
    return text


# Apply and audit a period status transition.
def _transition(period, new_status, *, actor_user, action, message, **metadata):
    """Move ``period`` to ``new_status``, seal it when it closes or locks, and audit it.

    A CLOSED or LOCKED period's figures are sealed in the same transaction
    (:func:`vs_finance.seals.seal_period`), and the seal's checksum is written
    on the audit row, so the figures that were closed can be proven later.
    """
    period.status = new_status  # Set the new lifecycle status.
    fields = ["status", "updated_at"]  # Base fields changed by every transition.
    if new_status in (PeriodStatus.SOFT_CLOSED, PeriodStatus.CLOSED, PeriodStatus.LOCKED):  # Closing statuses capture actor/time.
        period.closed_at = timezone.now()
        period.closed_by = actor_user  # Store the user who closed/locked the period.
        fields += ["closed_at", "closed_by"]  # Persist close metadata too.
    period.save(update_fields=fields)
    if new_status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        from .seals import seal_period

        seal = seal_period(
            period, locked=new_status == PeriodStatus.LOCKED, actor_user=actor_user,
        )
        metadata["seal_checksum"] = seal.seal_checksum
    record(  # Audit the transition.
        entity=period.entity, action=action, actor_user=actor_user, target=period,  # Entity, action, actor, target.
        message=message, target_type="FiscalPeriod",  # Human message and explicit target type.
        period=period.name, period_status=new_status,  # Structured period metadata.
        **metadata,
    )
    return period  # Return transitioned period.


def _sync_branch_periods(period, status, actor_user=None):
    """Keep legacy tenant-wide transitions compatible with branch close state."""
    from .models import BranchFiscalPeriod
    from vs_tenants.models import Branch

    closing = status != PeriodStatus.OPEN
    for branch in Branch.all_objects.filter(tenant_id=period.entity.tenant_id):
        BranchFiscalPeriod.objects.update_or_create(
            period=period, branch=branch,
            defaults={
                "status": status,
                "closed_at": timezone.now() if closing else None,
                "closed_by": actor_user if closing else None,
            },
        )


@transaction.atomic
# Handle the close period workflow.
def close_period(entity, period, *, actor_user=None, soft=False, force=False,
                 run_depreciation=True, extra_checks=None, reason=None,
                 release_deferred=True):  # Close or soft-close a fiscal period.
    """Close ``period`` after running (and optionally enforcing) the checklist.

    ``soft`` transitions OPEN → SOFT_CLOSED (auto-postings still allowed); otherwise it
    transitions OPEN/SOFT_CLOSED → CLOSED. ``run_depreciation`` posts due depreciation
    first, and ``release_deferred`` releases the deferred income due by the period's
    end (:func:`vs_finance.deferred_income.release_deferred_income`), both before the
    checklist runs. Blocking checklist failures raise :class:`PeriodCloseError` unless
    ``force``. Returns the period.

    ``force`` needs a ``reason`` (:func:`require_reason`), asked for up front so a
    forced request without one is refused before depreciation posts anything. The
    reason lands on the PERIOD_CLOSED audit row beside the checklist it overrode.

    While the school keeps its periods in order (:func:`periods_close_in_order`),
    every earlier period must be closed first, at least soft-closed for a soft close
    (:func:`refuse_out_of_order_close`). That refusal comes before anything posts,
    and ``force`` does not override it.

    The period's row is locked FOR UPDATE for the whole close, and its status re-read
    under the lock. A posting takes KEY SHARE on the same row in its guard
    (:func:`vs_finance.posting.ensure_period_open`), so a posting in flight finishes
    before the month closes and a later one sees it closed. The year's row is
    share-locked first, the order the posting guard and the year close both use, so
    a month close and a year close cannot deadlock on each other. Between the two,
    the earlier periods are locked oldest first (:func:`lock_periods_in_date_order`),
    so a reopen of one of them cannot slip in between the order check and the close.
    """
    from .models import FiscalPeriod, FiscalYear
    from .posting import read_key_shared

    said = period_label(period, entity.tenant)
    if period.is_closing:
        raise PeriodCloseError(
            f"{said} is the year's closing period; it opens and closes with its "
            f"fiscal year, not on its own.")
    if force:
        reason = require_reason(reason, act=f"force-close {said}")
    read_key_shared(FiscalYear, period.fiscal_year_id, ("status",))  # Year before month.
    in_order = periods_close_in_order(entity)
    if in_order:
        lock_periods_in_date_order(entity, period, before=True)
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    period.refresh_from_db(fields=["status"])
    if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(f"{said} is already {period_status_word(period.status)}.")
    if in_order:
        refuse_out_of_order_close(entity, period, soft=soft)

    if run_depreciation:  # Close can auto-post due depreciation.
        run_period_depreciation(entity, period, actor_user=actor_user)  # Post depreciation before checklist.
    if release_deferred:  # Income earned in the month leaves deferred income.
        from .deferred_income import release_deferred_income

        release_deferred_income(
            entity, up_to=period.end_date, actor_user=actor_user, allow_restricted=True,
        )

    checklist = close_checklist(entity, period, extra_checks=extra_checks)  # Run close integrity checks.
    if not checklist.passed and not force:  # Blocking failures stop the close unless forced.
        raise PeriodCloseError(
            f"{said} is not ready to close. {failures_sentence(checklist.failures)}.",
            failures=[i.name for i in checklist.failures],  # Machine names, for a screen to key on.
        )

    new_status = PeriodStatus.SOFT_CLOSED if soft else PeriodStatus.CLOSED  # Choose requested close strength.
    _transition(  # Apply the period status transition and audit it.
        period, new_status, actor_user=actor_user,  # Target status and actor.
        action=FinanceAuditAction.PERIOD_CLOSED,  # Audit action for close.
        message=f"{'Soft-closed' if soft else 'Closed'} {said}"
                + ("" if checklist.passed else " over failed checks"),  # Flag forced closes.
        **({"forced": True, "reason": reason} if force else {}),
    )
    _sync_branch_periods(period, new_status, actor_user)
    return period, checklist  # Return updated period and checklist details.


@transaction.atomic
# Re-open a closed or soft-closed period.
def reopen_period(entity, period, *, actor_user=None, reason=None):
    """Re-open a CLOSED or SOFT_CLOSED period back to OPEN (audited). LOCKED can't reopen.

    A ``reason`` is required (:func:`require_reason`) and stored on the audit row.

    A month of a CLOSED or LOCKED fiscal year stays shut. The year's result is
    already in Retained Earnings, so anything posted into a reopened month of it
    would sit outside that result for good; the year has to be reopened first
    (:func:`reopen_fiscal_year`), which a LOCKED year never is.

    While the school keeps its periods in order (:func:`periods_close_in_order`),
    every later period must be OPEN first (:func:`refuse_out_of_order_reopen`).

    The year's row is share-locked and the period's locked FOR UPDATE, year first as
    every close and posting takes them, so a year close in flight finishes before
    this reads the year's status. The later periods are locked after this one,
    oldest first (:func:`lock_periods_in_date_order`), so a close of one of them
    cannot slip in between the order check and the reopen.
    """
    from .models import FiscalPeriod, FiscalYear
    from .posting import read_key_shared

    said = period_label(period, entity.tenant)
    if period.is_closing:
        raise PeriodCloseError(
            f"{said} is the year's closing period; reopen the fiscal year instead.")
    reason = require_reason(reason, act=f"reopen {said}")
    year = read_key_shared(FiscalYear, period.fiscal_year_id, ("year", "status"))
    FiscalPeriod.objects.select_for_update().only("pk").get(pk=period.pk)
    in_order = periods_close_in_order(entity)
    if in_order:
        lock_periods_in_date_order(entity, period, before=False)
    period.refresh_from_db(fields=["status"])
    if year is not None and year[1] == PeriodStatus.LOCKED:
        raise PeriodCloseError(
            f"{said} belongs to FY{year[0]}, which is locked; neither the year nor "
            f"its months can be reopened.")
    if year is not None and year[1] == PeriodStatus.CLOSED:
        raise PeriodCloseError(
            f"{said} belongs to FY{year[0]}, which is closed. Reopen the fiscal year "
            f"first, then reopen the month.")
    if period.status == PeriodStatus.LOCKED:  # Locked periods are irreversible.
        raise PeriodCloseError(f"{said} is locked and can never be reopened.")
    if period.status == PeriodStatus.OPEN:  # Open periods do not need reopening.
        raise PeriodCloseError(f"{said} is already open.")
    if in_order:
        refuse_out_of_order_reopen(entity, period)
    period.status = PeriodStatus.OPEN  # Restore open lifecycle status.
    period.closed_at = None  # Clear close timestamp.
    period.closed_by = None  # Clear close actor.
    period.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    _sync_branch_periods(period, PeriodStatus.OPEN)
    record(  # Audit the reopen.
        entity=entity, action=FinanceAuditAction.PERIOD_REOPENED,  # Audit action for reopening.
        actor_user=actor_user, target=period, target_type="FiscalPeriod",  # Actor and target context.
        message=f"Reopened {said}.", period=period.name,  # Human and structured period text.
        reason=reason,
    )
    return period  # Return reopened period.


@transaction.atomic
# Permanently lock a closed fiscal period.
def lock_period(entity, period, *, actor_user=None):
    """Permanently seal a CLOSED period (e.g. after statutory filing). Irreversible.

    While the school keeps its periods in order, every earlier period must be
    CLOSED or LOCKED, as for a hard close; they need not be LOCKED themselves.
    Locking September while August is merely CLOSED keeps the order, because the
    walk counts both as closed, and it leaves August where the reopen rule already
    holds it: August reopens only once September is OPEN, which a LOCKED September
    never is again. Requiring the earlier periods locked as well would force every
    lock to run oldest first for nothing. No neighbouring rows are locked here: this
    period is already CLOSED, and an earlier period cannot reopen while a later one
    is not OPEN, so nothing the check reads can change under it.
    """
    said = period_label(period, entity.tenant)
    if period.status != PeriodStatus.CLOSED:  # Only fully closed periods may be locked.
        raise PeriodCloseError(
            f"Only a closed month can be locked; {said} is "
            f"{period_status_word(period.status)}.",
        )
    refuse_out_of_order_close(entity, period, act="lock")
    if (
        period.end_date == period.fiscal_year.end_date
        and period.fiscal_year.status == PeriodStatus.OPEN
    ):
        raise PeriodCloseError(
            f"Close fiscal year {period.fiscal_year.year} before locking its final period.",
        )
    _transition(  # Apply irreversible lock transition and audit it.
        period, PeriodStatus.LOCKED, actor_user=actor_user,  # Target locked status and actor.
        action=FinanceAuditAction.PERIOD_LOCKED,  # Audit action for lock.
        message=f"Locked {said} for good.",  # Human-readable audit message.
    )
    _sync_branch_periods(period, PeriodStatus.LOCKED, actor_user)
    return period  # Return locked period.


def _closing_buckets(entity, fiscal_year):
    """The year's income and expense movement, grouped by the branch it closes under.

    Returns ``{branch_id: {account_id: (debit, credit)}}`` built from the ledger's
    journal lines, with accounts that are already flat left out and branches with
    nothing to close left out altogether. Every key is a real branch.

    An entry with no branch is filed under the tenant's only branch when it has
    exactly one. At a tenant with several, nothing says which branch's result it
    belongs to, so the close is refused and the count of such entries named. A
    tenant that owns no branch raises
    :class:`~vs_finance.exceptions.BranchlessTenantError`
    (:func:`vs_finance.branch_ledger.only_branch_id_or_several`): no closing
    journal is ever written without a branch.
    """
    from django.db.models import Sum

    from .branch_ledger import branches_in_year, ledger_lines, only_branch_id_or_several
    from .constants import AccountType

    pl_types = (AccountType.INCOME, AccountType.EXPENSE)
    only = only_branch_id_or_several(entity.tenant_id)
    shape = branches_in_year(entity, fiscal_year, account_types=pl_types)
    if shape.has_unbranched and only is None:
        count = shape.unbranched_entries
        raise PeriodCloseError(
            f"FY{fiscal_year.year} cannot close yet: {count} journal "
            f"{'entry carries' if count == 1 else 'entries carry'} income or expense "
            f"with no branch. The year is closed branch by branch, so give "
            f"{'it' if count == 1 else 'each of them'} a branch first.",
            failures=["unbranched_entries"], unbranched_entries=count,
        )

    rows = (
        ledger_lines(entity)
        .filter(entry__period__fiscal_year=fiscal_year,
                account__is_postable=True,  # Header accounts never take a line.
                account__account_type__in=pl_types)
        .values("entry__branch_id", "account_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    buckets: dict = {}
    for row in rows:
        branch_id = row["entry__branch_id"]
        if branch_id is None:
            branch_id = only
        accounts = buckets.setdefault(branch_id, {})
        debit, credit = accounts.get(row["account_id"], (0, 0))
        accounts[row["account_id"]] = (debit + int(row["d"] or 0), credit + int(row["c"] or 0))

    closing = {}
    for branch_id, accounts in buckets.items():
        live = {acc: (d, c) for acc, (d, c) in accounts.items() if d != c}
        if live:
            closing[branch_id] = live
    return closing


def _refuse_year_close_out_of_order(entity, fiscal_year, *, branch_id=None):
    """Refuse a year close whose hard close of soft-closed months would break the order.

    Only a year with a SOFT_CLOSED month to hard-close can break it, and only when a
    period before the year is not yet CLOSED or LOCKED. ``branch_id`` reads that
    branch's own period states.
    """
    from .models import FiscalPeriod

    months = FiscalPeriod.objects.filter(fiscal_year=fiscal_year, is_closing=False)
    first = months.order_by("start_date").first()
    if first is None:
        return
    if branch_id is None:
        soft = months.filter(status=PeriodStatus.SOFT_CLOSED).exists()
    else:
        soft = _walk(entity, branch_id).filter(
            fiscal_year=fiscal_year, walk_status=PeriodStatus.SOFT_CLOSED,
        ).exists()
    if not soft:
        return
    lock_periods_in_date_order(entity, first, before=True)
    blocker = earlier_period_in_the_way(entity, first, branch=branch_id)
    if blocker is None:
        return
    where = _branch_name(entity, branch_id)
    raise PeriodCloseError(
        f"Close {period_label(blocker, entity.tenant)}{f' at {where}' if where else ''} "
        f"first. {_unit(first, plural=True).capitalize()} close in order, and closing "
        f"FY{fiscal_year.year} closes its soft-closed {_unit(first, plural=True)}, so "
        f"every {_unit(first)} before the year must be closed first.",
        failures=["earlier_periods_closed"], blocking_period=blocker.pk,
    )


def _lock_fiscal_year(fiscal_year):
    """Take the year's row lock and bring ``fiscal_year`` up to date under it.

    Closing and reopening a year each read its status and then change it; the lock
    makes two requests queue rather than both acting on the same stale status. It
    also serialises the year change with postings: the posting guard holds KEY SHARE
    on this row until its transaction commits
    (:func:`vs_finance.posting.read_key_shared`), which FOR UPDATE waits for, so no
    posting can land in the year between the close's reading of the ledger and its
    sealing of the year.
    """
    from .models import FiscalYear

    FiscalYear.objects.select_for_update().only("pk").get(pk=fiscal_year.pk)
    fiscal_year.refresh_from_db()
    return fiscal_year


@transaction.atomic
# Post the year-end closing journals and seal the fiscal year.
def close_fiscal_year(entity, fiscal_year, *, actor_user=None, closing_date=None,
                      require_periods_closed=True, reason=None, branch=None):
    """Post the year-end closing journals and mark ``fiscal_year`` CLOSED.

    The year's result is worked out per branch. With ``branch`` supplied, only that
    branch closes and the tenant year closes after every branch year is closed.
    Without it, all branches close together for compatibility. Each
    branch with income or expense in the year gets its own closing journal, carrying
    that branch, which zeroes the branch's own income and expense accounts and rolls
    its net profit or loss into Retained Earnings (3200). A branch-scoped call posts
    that branch's journal in one transaction. A compatibility call without a branch
    posts every branch journal together. See :func:`_closing_buckets` for how an
    entry with no branch is treated.

    Each closing journal names the year in ``closes_fiscal_year``, which makes the
    year its owner: the journal screen offers no raw reverse, and only
    :func:`reopen_fiscal_year` undoes it.

    * The closing journals post into the year's closing period
      (:func:`vs_finance.seed.ensure_closing_period`), dated the year's last day,
      never into the last month. Every month and the full year keep their real
      income and expense; the statements of profit leave the closing period out,
      and the balance sheet and trial balance include it, so Retained Earnings
      shows the profit moved. The closing period is CLOSED, which the close's
      escape hatch covers; a LOCKED closing period refuses.
    * ``closing_date`` may be left out, or given as the year's ``end_date``. Any
      other date is refused: outside the year it would carry the close into
      another year, and inside it there is nowhere to put it but the closing period,
      which has one date.
    * ``require_periods_closed`` (default) refuses while any period in the year is still
      OPEN - draft/late entries should be posted and the months soft-/closed first.
      Passing ``False`` forces the close over OPEN months and needs a ``reason``
      (:func:`require_reason`), stored on the FISCAL_YEAR_CLOSED audit row. A forced
      close leaves those months reading OPEN; the posting guard refuses them anyway,
      because their year is closed.
    * Every SOFT_CLOSED month of the year is hard-closed as part of the close. A
      soft-closed month still takes privileged postings such as depreciation, and
      nothing may post into a closed year. While the school keeps its periods in
      order (:func:`periods_close_in_order`), that hard close obeys the order like
      any other: every period before the year must already be CLOSED or LOCKED, or
      the close is refused naming the earliest one in the way, forced or not. The
      year's own months already step down from closed to soft-closed to open when
      the order is kept, so hard-closing the soft ones keeps it. The earlier periods
      are locked oldest first after the year's row
      (:func:`lock_periods_in_date_order`).
    * Every registered year-close check runs first (:func:`register_year_close_check`),
      such as depreciation dated in the year that has not posted. A blocking failure
      refuses the close unless it is forced with a reason; a forced close records the
      checks it overrode on the audit row.
    * The year's row is locked FOR UPDATE before anything is read
      (:func:`_lock_fiscal_year`), so postings into the year already in flight
      finish first and later ones see the year closed.

    * Once sealed, the year's figures are recorded with a checksum
      (:func:`vs_finance.seals.seal_fiscal_year`), so the closed year can later be
      proven unchanged.

    Refuses a year already CLOSED/LOCKED. Returns ``(journals, net_income)``: the
    closing journals, one per branch with something to close (an empty list when
    the year had no P&L activity), and the net result across all of them in kobo
    (positive = profit).
    """
    from .constants import JournalSource
    from .models import (
        Account, BranchFiscalPeriod, BranchFiscalYear, FiscalPeriod,
        JournalEntry, JournalLine,
    )
    from .posting import _period_accepts_posting, post_journal
    from .seed import ensure_closing_period
    from vs_tenants.models import Branch

    _lock_fiscal_year(fiscal_year)
    branch_id = getattr(branch, "pk", branch)
    branch_row = None
    if branch_id is not None:
        owned_branch = Branch.all_objects.filter(
            pk=branch_id, tenant_id=entity.tenant_id,
        ).first()
        if owned_branch is None:
            raise PeriodCloseError("Branch year does not belong to these books.")
        branch_row, _ = BranchFiscalYear.objects.get_or_create(
            fiscal_year=fiscal_year, branch=owned_branch,
            defaults={"status": fiscal_year.status},
        )
        branch_row = BranchFiscalYear.objects.select_for_update().get(pk=branch_row.pk)
    current_status = branch_row.status if branch_row is not None else fiscal_year.status
    if current_status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise PeriodCloseError(
            f"FY{fiscal_year.year} is already {period_status_word(current_status)}.")

    closing_date = closing_date or fiscal_year.end_date  # Default to the last day of the year.
    if closing_date != fiscal_year.end_date:
        from rest_framework.exceptions import ValidationError

        start = format_date(fiscal_year.start_date, entity.tenant)
        end = format_date(fiscal_year.end_date, entity.tenant)
        raise ValidationError({
            "closing_date": (
                f"The closing date must fall inside FY{fiscal_year.year} "
                f"({start} to {end}) on its last day: "
                f"the closing entry is dated {end}, in the year's closing "
                f"period. {format_date(closing_date, entity.tenant)} is not that day. "
                f"Leave it out to close on {end}."
            ),
        })

    forced = not require_periods_closed
    if forced:
        reason = require_reason(reason, act=f"force-close FY{fiscal_year.year}")
    else:  # Months must be settled before the year is sealed.
        if branch_row is None:
            open_count = FiscalPeriod.objects.filter(
                fiscal_year=fiscal_year, status=PeriodStatus.OPEN, is_closing=False,
            ).count()
        else:
            branch_states = dict(BranchFiscalPeriod.objects.filter(
                period__fiscal_year=fiscal_year, branch_id=branch_id,
                period__is_closing=False,
            ).values_list("period_id", "status"))
            open_count = sum(
                branch_states.get(period_id, status) == PeriodStatus.OPEN
                for period_id, status in FiscalPeriod.objects.filter(
                    fiscal_year=fiscal_year, is_closing=False,
                ).values_list("pk", "status")
            )
        if open_count:  # Refuse while any month is still OPEN.
            raise PeriodCloseError(
                f"{open_count} {'month' if open_count == 1 else 'months'} of "
                f"FY{fiscal_year.year} {'is' if open_count == 1 else 'are'} still open. "
                f"Close or soft-close {'it' if open_count == 1 else 'them'} before closing "
                f"the year, or force the close with a reason.")

    if periods_close_in_order(entity):
        _refuse_year_close_out_of_order(entity, fiscal_year, branch_id=branch_id)

    checklist = year_close_checklist(entity, fiscal_year, branch=branch_id)
    if not checklist.passed and not forced:  # Blocking failures stop the close unless forced.
        raise PeriodCloseError(
            f"FY{fiscal_year.year} is not ready to close. "
            f"{failures_sentence(checklist.failures)}.",
            failures=[i.name for i in checklist.failures],
        )

    buckets = _closing_buckets(entity, fiscal_year)
    if branch_id is not None:
        buckets = {branch_id: buckets[branch_id]} if branch_id in buckets else {}

    period = None
    if buckets:
        period = ensure_closing_period(fiscal_year)  # The closing entries' own period.
        if not _period_accepts_posting(  # Formal close may use CLOSED, but never LOCKED.
            period, branch=branch_id, allow_restricted=True, allow_closed=True,
        ):
            raise PeriodCloseError(
                f"The closing period of FY{fiscal_year.year} is locked, so no closing "
                f"entry can post into it.")

    if branch_row is None:
        soft_closed = (
            FiscalPeriod.objects.select_for_update()
            .filter(fiscal_year=fiscal_year, status=PeriodStatus.SOFT_CLOSED, is_closing=False)
            .order_by("period_no")
        )
        for month in soft_closed:
            _transition(
                month, PeriodStatus.CLOSED, actor_user=actor_user,
                action=FinanceAuditAction.PERIOD_CLOSED,
                message=f"Closed period to {PeriodStatus.CLOSED} with the FY{fiscal_year.year} close.",
                fiscal_year=fiscal_year.year,
            )
    else:
        BranchFiscalPeriod.objects.filter(
            period__fiscal_year=fiscal_year, branch_id=branch_id,
            status=PeriodStatus.SOFT_CLOSED,
        ).update(status=PeriodStatus.CLOSED, closed_at=timezone.now(), closed_by=actor_user)

    journals = []
    net_income = 0  # Σ(credit − debit) over P&L = revenue minus expense = profit.
    net_by_branch = {}
    if buckets:
        retained = resolve_mapped_account(
            entity, AccountMappingKey.RETAINED_EARNINGS, label="retained earnings",
        )
        account_ids = {acc for accounts in buckets.values() for acc in accounts}
        accounts = Account.objects.in_bulk(account_ids)
        branches = Branch.all_objects.in_bulk(list(buckets))
        for branch_id in sorted(buckets):
            closing_lines = []  # (account, debit, credit) - each line zeroes one P&L account.
            branch_net = 0
            for acc_id, (d, c) in sorted(buckets[branch_id].items()):
                if c > d:  # Net credit balance (typical revenue) → debit it flat.
                    closing_lines.append((accounts[acc_id], c - d, 0))
                else:  # Net debit balance (typical expense / contra-revenue) → credit it flat.
                    closing_lines.append((accounts[acc_id], 0, d - c))
                branch_net += c - d
            if branch_net > 0:  # Profit → credit Retained Earnings.
                closing_lines.append((retained, 0, branch_net))
            elif branch_net < 0:  # Loss → debit Retained Earnings.
                closing_lines.append((retained, -branch_net, 0))

            branch = branches[branch_id]
            narration = f"Year-end close FY{fiscal_year.year}"
            entry = JournalEntry.objects.create(  # One closing journal per branch.
                entity=entity, branch=branch, date=closing_date, period=period,
                source=JournalSource.CLOSING, created_by=actor_user,
                narration=f"{narration} - {branch.name}",
                closes_fiscal_year=fiscal_year,
            )
            for i, (acc, debit, credit) in enumerate(closing_lines, start=1):
                JournalLine.objects.create(
                    entry=entry, account=acc, debit=debit, credit=credit,
                    description=narration, line_no=i,
                )
            post_journal(  # Privileged formal-close posting; the only CLOSED-month bypass.
                entry, actor_user=actor_user, allow_restricted=True, allow_closed=True,
            )
            journals.append(entry)
            net_income += branch_net
            net_by_branch[str(branch_id)] = branch_net

    if branch_row is not None:
        branch_row.status = PeriodStatus.CLOSED
        branch_row.closed_at = timezone.now()
        branch_row.closed_by = actor_user
        branch_row.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
        branch_ids = set(Branch.all_objects.filter(
            tenant_id=entity.tenant_id,
        ).values_list("pk", flat=True))
        closed_ids = set(BranchFiscalYear.objects.filter(
            fiscal_year=fiscal_year, status__in=(PeriodStatus.CLOSED, PeriodStatus.LOCKED),
        ).values_list("branch_id", flat=True))
        seal = None
        if branch_ids == closed_ids:
            fiscal_year.status = PeriodStatus.CLOSED
            fiscal_year.save(update_fields=["status", "updated_at"])
            from .seals import seal_fiscal_year
            seal = seal_fiscal_year(fiscal_year, actor_user=actor_user)
    else:
        for tenant_branch in Branch.all_objects.filter(tenant_id=entity.tenant_id):
            BranchFiscalYear.objects.update_or_create(
                fiscal_year=fiscal_year, branch=tenant_branch,
                defaults={"status": PeriodStatus.CLOSED, "closed_at": timezone.now(),
                          "closed_by": actor_user},
            )
        fiscal_year.status = PeriodStatus.CLOSED
        fiscal_year.save(update_fields=["status", "updated_at"])
        from .seals import seal_fiscal_year
        seal = seal_fiscal_year(fiscal_year, actor_user=actor_user)
    record(  # Audit the close with the net result and every closing journal.
        entity=entity, action=FinanceAuditAction.FISCAL_YEAR_CLOSED,
        actor_user=actor_user, target=fiscal_year, target_type="FiscalYear",
        message=(
            f"Closed FY{fiscal_year.year}: {'profit' if net_income >= 0 else 'loss'} of "
            f"{format_naira(abs(net_income))} rolled to retained earnings."
            if journals else f"Closed FY{fiscal_year.year} (no P&L activity)."
        ),
        journal_ids=[j.pk for j in journals], net_by_branch=net_by_branch,
        fiscal_year=fiscal_year.year, net_income=net_income,
        **({"seal_checksum": seal.seal_checksum} if seal is not None else {}),
        **({"branch_id": branch_id} if branch_id is not None else {}),
        **({"forced": True, "reason": reason,
            "overridden_checks": [i.name for i in checklist.failures]} if forced else {}),
    )
    return journals, net_income


@transaction.atomic
# Reverse a year's closing journals and set it back to OPEN.
def reopen_fiscal_year(entity, fiscal_year, *, actor_user=None, reason=None, branch=None):
    """Reopen a CLOSED fiscal year so it can be corrected and closed again.

    Every closing journal in scope is reversed. With ``branch`` supplied, that
    branch's year reopens while the other branches stay closed. Without it, every
    closing journal the year still has in force (one per branch) is reversed
    through :func:`vs_finance.posting.reverse_journal`, as the year's own act, on the
    closing journal's own date and in the year's closing period. The reversal
    therefore lands inside the year it undoes and outside its months: the income and
    expense accounts read their full-year totals again, Retained Earnings loses the
    result it was given, and no month's figures move. Dated today it would pour a
    whole year's income and expense into the current month.

    The year is set OPEN before the reversals post, so the posting guard admits
    them; the closing period is CLOSED, which ``allow_closed`` covers. Months keep
    their own status: re-open the one that needs a correction,
    post it, close it, and close the year again, which rolls the corrected result
    into Retained Earnings.

    Needs a ``reason`` (:func:`require_reason`), stored on the FISCAL_YEAR_REOPENED
    audit row with the journals reversed. Refuses an archived year (unarchive it
    first), a LOCKED year, a year that is not closed, and a year whose closing
    journal sits in a LOCKED period, where no reversal can post. Returns
    ``(fiscal_year, reversals)``.
    """
    from .constants import DocumentStatus
    from .models import BranchFiscalYear
    from .posting import reverse_journal
    from vs_tenants.models import Branch

    reason = require_reason(reason, act=f"reopen FY{fiscal_year.year}")
    _lock_fiscal_year(fiscal_year)
    branch_id = getattr(branch, "pk", branch)
    branch_row = None
    if branch_id is not None:
        owned_branch = Branch.all_objects.filter(
            pk=branch_id, tenant_id=entity.tenant_id,
        ).first()
        if owned_branch is None:
            raise PeriodCloseError("Branch year does not belong to these books.")
        branch_row = BranchFiscalYear.objects.select_for_update().filter(
            fiscal_year=fiscal_year, branch=owned_branch,
        ).first()
        if branch_row is None or branch_row.status != PeriodStatus.CLOSED:
            status = branch_row.status if branch_row is not None else PeriodStatus.OPEN
            raise PeriodCloseError(
                f"FY{fiscal_year.year} is {period_status_word(status)} at {owned_branch.name}; "
                f"only a closed year can be reopened.")
    if fiscal_year.archived_at is not None:
        raise PeriodCloseError(
            f"Fiscal year {fiscal_year.year} is archived. Unarchive it before re-opening it.")
    if fiscal_year.status == PeriodStatus.LOCKED:
        raise PeriodCloseError(
            f"FY{fiscal_year.year} is locked and can never be reopened.")
    if branch_row is None and fiscal_year.status != PeriodStatus.CLOSED:
        raise PeriodCloseError(
            f"FY{fiscal_year.year} is {period_status_word(fiscal_year.status)}; only a "
            f"closed year can be reopened.")

    journals = list(
        fiscal_year.closing_journals.filter(status=DocumentStatus.POSTED)
        .filter(**({"branch_id": branch_id} if branch_id is not None else {}))
        .select_related("period").order_by("pk")
    )
    for journal in journals:
        if journal.period is None or journal.period.status == PeriodStatus.LOCKED:
            raise PeriodCloseError(
                f"FY{fiscal_year.year} cannot be re-opened: its closing journal "
                f"{journal.document_number or journal.pk} sits in "
                f"{period_label(journal.period, entity.tenant) if journal.period else format_date(journal.date, entity.tenant)}, "
                f"which is locked, so it cannot be reversed.")

    if fiscal_year.status == PeriodStatus.CLOSED:
        fiscal_year.status = PeriodStatus.OPEN
        fiscal_year.save(update_fields=["status", "updated_at"])
    if branch_row is not None:
        branch_row.status = PeriodStatus.OPEN
        branch_row.closed_at = None
        branch_row.closed_by = None
        branch_row.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    else:
        BranchFiscalYear.objects.filter(fiscal_year=fiscal_year).update(
            status=PeriodStatus.OPEN, closed_at=None, closed_by=None,
        )

    reversals = [
        reverse_journal(
            journal, actor_user=actor_user, date=journal.date,
            allow_restricted=True, allow_closed=True, document_owner=fiscal_year,
        )
        for journal in journals
    ]

    record(
        entity=entity, action=FinanceAuditAction.FISCAL_YEAR_REOPENED,
        actor_user=actor_user, target=fiscal_year, target_type="FiscalYear",
        message=(
            f"Re-opened FY{fiscal_year.year}; reversed {counted(len(reversals), 'closing journal')}."
        ),
        fiscal_year=fiscal_year.year, reason=reason,
        journal_ids=[j.pk for j in journals], reversal_ids=[r.pk for r in reversals],
        **({"branch_id": branch_id} if branch_id is not None else {}),
    )
    return fiscal_year, reversals
