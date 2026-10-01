"""Which branch an unbranched transaction belongs to: the engine behind ``branch_audit`` and ``branch_backfill``.

Every transaction names a branch. Rows written before that rule may have none,
and at a tenant with several branches the year-end close refuses while any
income or expense journal is unbranched. This module works out, for each
unbranched row, the branch it belongs to from facts already on the books, and
says which fact it read.

How a branch is derived
-----------------------
Each transaction model is a :class:`Target` holding an ordered tuple of
:class:`Source` objects, and the first source that answers wins. A source only
ever answers with a branch of the row's own tenant; a branch of any other tenant
is treated as no answer. A source that reads several related rows (a payment's
allocated invoices, a bill run's bills) answers only when every one of them has
a branch and they all agree. When they disagree, the disagreement becomes the
row's flag reason.

When no source answers:

* a tenant with exactly one branch files the row under it, because there is
  nowhere else it can belong;
* a tenant with several leaves the row blank and flags it, with the reason, for
  an administrator. The main branch is never assumed: a Lekki pupil's invoice
  filed under Ikeja because Ikeja is the main branch would move Lekki's fee
  income into Ikeja's year result, and nothing afterwards would say so;
* a tenant that owns no branch at all (the platform's own books) is reported as
  blocked, and nothing is derived until it has one.

Derivations chain. An RFQ reads its requisition's branch, including a branch
the same run has derived but not yet written, which is what makes a dry run
report exactly what an applied run writes.

A model with no branch column of its own may be registered with
``has_branch_column=False``: every row is planned and reported, and nothing is
written.

Registration
------------
Each app contributes its own targets from a module named ``branch_backfill``,
discovered on first use the way workflow handlers are. Finance declares its
documents, procurement and payments declare theirs, so finance never imports
either. The school layer contributes :func:`register_customer_source` (the pupil
on the roll), which lets the engine reach a school fact without importing a
school app.

Writing
-------
:func:`apply_plan` writes one transaction per batch. It locks the batch's rows,
writes only those still blank, and records every change in the central audit
trail inside the same transaction. A change whose audit row cannot be written is
rolled back with its batch, so no branch is ever set without its record. A row
that gained a branch after it was planned is left exactly as it is, which also
makes a second run a no-op.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import autodiscover_modules

#: How a row is described when the tenant's only branch is the answer.
ONLY_BRANCH = "the tenant's only branch"

#: Rows read per query; keeps ``IN`` lists bounded on a large tenant.
READ_CHUNK = 1000

#: Rows written per transaction by :func:`apply_plan`.
DEFAULT_BATCH_SIZE = 500


class BackfillAuditError(RuntimeError):
    """A branch change whose audit row could not be written; its batch rolls back."""


# --------------------------------------------------------------------------- #
# Declarations
# --------------------------------------------------------------------------- #
@dataclass
class Answer:
    """What one source says about a chunk of rows.

    ``found`` maps a row to ``(branch_id, how)``; ``how`` names the fact read,
    for the audit trail. ``split`` maps a row to the several branches its
    related rows disagree on. ``pending`` holds the rows that do point at
    something, which has no branch itself: a flag reason says so, because
    giving that upstream row a branch and running again settles both.
    """

    found: dict[int, tuple[int, str]] = field(default_factory=dict)
    split: dict[int, set[int]] = field(default_factory=dict)
    pending: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class Source:
    """One way of reading a row's branch. ``derive(ctx, model_label, pks)`` returns an :class:`Answer`."""

    label: str
    derive: Callable[["Context", str, list[int]], Answer]


@dataclass(frozen=True)
class Target:
    """A transaction model the backfill covers, and the sources it tries in order.

    ``order`` places it in the run: a model reading another's branch comes after
    it. ``audit_module`` is the ``vs_audit`` module key its changes are filed
    under. ``no_source_note`` is the flag reason for a model with no source at
    all, where only an administrator can say which branch a row is.

    ``whole_tenant`` (a ``Q``) picks out the rows that name no branch by design
    and are neither planned nor flagged: a document covering every branch that
    is booked per branch through branch shares of its own, such as a central
    payroll run or the tenant's tax return. Giving one of them a branch would
    hand that branch's staff every other branch's shares, so it is not a gap
    the backfill fills. It may instead be a function returning the ``Q``, called
    when an entity is planned, for a target whose rule reads what the other
    targets registered (the finance audit trail's does).

    ``scope_field`` names how a row reaches the books being planned: ``entity``
    (the default) for a row of one set of books, ``tenant`` for a row kept per
    tenant rather than per set of books, such as the platform audit trail's
    copy of a finance entry. A tenant row is planned with each of its tenant's
    books, and a row written by an earlier one is simply no longer blank.
    """

    model_label: str
    sources: tuple[Source, ...]
    order: int
    audit_module: str = "FINANCE"
    has_branch_column: bool = True
    no_source_note: str = ""
    whole_tenant: object = None
    scope_field: str = "entity"

    @property
    def model(self):
        return apps.get_model(self.model_label)


@dataclass(frozen=True)
class JournalOwner:
    """A model whose ``journal_field`` points at the journal it raised.

    The owner carries the branch itself unless ``via`` names the foreign key to
    the row that does (a depreciation charge reads its asset's), with
    ``via_label`` naming that row's model.
    """

    model_label: str
    journal_field: str
    via: str | None = None
    via_label: str | None = None


_TARGETS: dict[str, Target] = {}
_JOURNAL_OWNERS: dict[tuple[str, str, str | None], JournalOwner] = {}
_AUDIT_REFERENCES: dict[str, tuple[str, str]] = {}
_CUSTOMER_SOURCES: dict[str, Callable[["Context", set[int]], dict[int, int]]] = {}
_discovered = False


def register_target(target: Target) -> None:
    """Declare a transaction model to the backfill. Re-registering replaces it."""
    _TARGETS[target.model_label] = target


def register_journal_owner(owner: JournalOwner) -> None:
    """Declare a journal-raising model, read by the journal entry's first source."""
    _JOURNAL_OWNERS[(owner.model_label, owner.journal_field, owner.via)] = owner


def register_audit_reference(target_type: str, metadata_key: str, model_label: str) -> None:
    """Declare where an audit entry about ``target_type`` names the document it concerns.

    For an entry whose target carries no branch of its own, the id of the row
    that does sits in the entry's ``metadata[metadata_key]``, a row of
    ``model_label``: a stock movement's entry names the item and keeps its store
    in ``location_id``. The finance audit trail's backfill reads it there.
    """
    _AUDIT_REFERENCES[target_type] = (metadata_key, model_label)


def audit_references() -> dict[str, tuple[str, str]]:
    """Every registered audit reference, by the ``target_type`` it applies to."""
    _discover()
    return dict(_AUDIT_REFERENCES)


def register_customer_source(label: str, resolve) -> None:
    """Declare a fallback for a customer whose own record carries no branch.

    ``resolve(ctx, customer_ids)`` returns ``{customer_id: branch_id}`` for the
    customers it can place. The school layer registers the pupil on the roll
    here; the engine names no domain.
    """
    _CUSTOMER_SOURCES[label] = resolve


def _discover() -> None:
    global _discovered
    if not _discovered:
        autodiscover_modules("branch_backfill")
        _discovered = True


def targets() -> list[Target]:
    """Every registered target, in run order."""
    _discover()
    return sorted(_TARGETS.values(), key=lambda t: (t.order, t.model_label))


def journal_owners() -> list[JournalOwner]:
    _discover()
    return list(_JOURNAL_OWNERS.values())


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #
def _chunks(items: Iterable, size: int = READ_CHUNK):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _has_branch(model) -> bool:
    try:
        model._meta.get_field("branch")
    except FieldDoesNotExist:
        return False
    return True


def _values(model_label: str, pks: Iterable[int], path: str) -> dict[int, int]:
    """``{pk: value}`` of one field or path for these rows, skipping blanks."""
    model = apps.get_model(model_label)
    out: dict[int, int] = {}
    for chunk in _chunks(pks):
        for pk, value in model._base_manager.filter(pk__in=chunk).values_list("pk", path):
            if value is not None:
                out[pk] = value
    return out


class Context:
    """One ledger entity's run: its tenant's branches, and the branches planned so far.

    Branches are counted over every status, the same count
    :func:`vs_rbac.scoping.only_branch_id` and the year-end close use, so a
    tenant with a suspended second branch is not mistaken for a one-branch one.
    """

    def __init__(self, entity):
        from vs_tenants.models import Branch

        self.entity = entity
        self.tenant = entity.tenant
        self.branch_names: dict[int, str] = dict(
            Branch.all_objects.filter(tenant_id=entity.tenant_id)
            .order_by("pk").values_list("pk", "name")
        )
        self.planned: dict[str, dict[int, int]] = defaultdict(dict)

    @property
    def only_branch_id(self) -> int | None:
        return next(iter(self.branch_names)) if len(self.branch_names) == 1 else None

    def valid(self, branch_id) -> bool:
        return branch_id in self.branch_names

    def branches_of(self, model_label: str, ids: Iterable) -> dict[int, int]:
        """The branch of each row: written, or planned earlier in this run.

        Only branches of this tenant are returned. A model without a branch
        column answers from the plan alone.
        """
        ids = {i for i in ids if i is not None}
        if not ids:
            return {}
        planned = self.planned.get(model_label, {})
        out = {i: planned[i] for i in ids if i in planned}
        model = apps.get_model(model_label)
        if _has_branch(model):
            for chunk in _chunks(ids - out.keys()):
                out.update(
                    model._base_manager.filter(pk__in=chunk, branch__isnull=False)
                    .values_list("pk", "branch_id")
                )
        return {k: v for k, v in out.items() if self.valid(v)}


def _agree(ctx: Context, related: dict[int, list[tuple[str, int]]], how: str) -> Answer:
    """One branch per row when every related row has one and they all match."""
    wanted: dict[str, set[int]] = defaultdict(set)
    for items in related.values():
        for label, ref in items:
            wanted[label].add(ref)
    known = {label: ctx.branches_of(label, refs) for label, refs in wanted.items()}
    answer = Answer()
    for pk, items in related.items():
        got = [known[label].get(ref) for label, ref in items]
        seen = {b for b in got if b is not None}
        if len(seen) > 1:
            answer.split[pk] = seen
        elif len(seen) == 1 and None not in got:
            answer.found[pk] = (seen.pop(), how)
        else:
            answer.pending.add(pk)
    return answer


# --------------------------------------------------------------------------- #
# Source builders
# --------------------------------------------------------------------------- #
def via(label: str, path: str, target_label: str) -> Source:
    """The branch of the row ``path`` points at, of model ``target_label``."""

    def derive(ctx, model_label, pks):
        links = _values(model_label, pks, path)
        branches = ctx.branches_of(target_label, links.values())
        return Answer(
            found={pk: (branches[ref], label) for pk, ref in links.items() if ref in branches},
            pending={pk for pk, ref in links.items() if ref not in branches},
        )

    return Source(label, derive)


def agreeing(label: str, link_label: str, own_field: str, *refs: tuple[str, str]) -> Source:
    """The branch every related row agrees on, reached through ``link_label``.

    ``own_field`` is the link's foreign key back to the row being derived;
    each of ``refs`` is ``(field on the link, model it names)``.
    """

    def derive(ctx, model_label, pks):
        link = apps.get_model(link_label)
        fields = [f for f, _ in refs]
        related: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for chunk in _chunks(pks):
            rows = link._base_manager.filter(**{f"{own_field}__in": chunk}).values_list(own_field, *fields)
            for row in rows:
                for (_, target_label), ref in zip(refs, row[1:]):
                    if ref is not None:
                        related[row[0]].append((target_label, ref))
        return _agree(ctx, related, label)

    return Source(label, derive)


def bank_behind(label: str, path: str) -> Source:
    """The branch of the bank account whose ledger account ``path`` names."""

    def derive(ctx, model_label, pks):
        from .models import BankAccount

        links = _values(model_label, pks, path)
        banks: dict[int, int] = {}
        for chunk in _chunks(set(links.values())):
            banks.update(
                BankAccount._base_manager.filter(gl_account_id__in=chunk)
                .values_list("gl_account_id", "pk")
            )
        branches = ctx.branches_of("vs_finance.BankAccount", banks.values())
        found = {}
        for pk, account_id in links.items():
            bank = banks.get(account_id)
            if bank in branches:
                found[pk] = (branches[bank], label)
        pending = {pk for pk, account_id in links.items() if account_id in banks and pk not in found}
        return Answer(found=found, pending=pending)

    return Source(label, derive)


def banks_on_journal(label: str, path: str) -> Source:
    """The branch every bank account on the journal at ``path`` agrees on."""

    def derive(ctx, model_label, pks):
        from .models import JournalLine

        journals = _values(model_label, pks, path)
        banks_by_journal: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for chunk in _chunks(set(journals.values())):
            rows = (
                JournalLine.objects.filter(entry_id__in=chunk, account__bank_account__isnull=False)
                .values_list("entry_id", "account__bank_account").distinct()
            )
            for entry_id, bank_id in rows:
                banks_by_journal[entry_id].append(("vs_finance.BankAccount", bank_id))
        related = {pk: banks_by_journal[j] for pk, j in journals.items() if banks_by_journal.get(j)}
        return _agree(ctx, related, label)

    return Source(label, derive)


def user_branch(label: str, path: str) -> Source:
    """The home branch of the person at ``path``, when it is one of this tenant's."""

    def derive(ctx, model_label, pks):
        links = _values(model_label, pks, path)
        users: dict[int, int] = {}
        for chunk in _chunks(set(links.values())):
            users.update(
                get_user_model()._base_manager.filter(pk__in=chunk, branch__isnull=False)
                .values_list("pk", "branch_id")
            )
        found = {
            pk: (users[ref], label) for pk, ref in links.items()
            if ref in users and ctx.valid(users[ref])
        }
        return Answer(found=found, pending=set(links) - found.keys())

    return Source(label, derive)


def customer(path: str = "customer") -> Source:
    """The customer's branch, then each registered customer source in turn.

    The customer record's own branch is read first; a customer with none is
    offered to the registered fallbacks (the pupil on the roll), and the row's
    ``how`` names whichever answered.
    """
    label = "the customer"

    def derive(ctx, model_label, pks):
        links = _values(model_label, pks, path)
        placed = {cid: (b, label) for cid, b in ctx.branches_of("vs_finance.Customer", links.values()).items()}
        for source_label, resolve in _CUSTOMER_SOURCES.items():
            missing = set(links.values()) - placed.keys()
            if not missing:
                break
            for cid, branch_id in resolve(ctx, missing).items():
                if cid in missing and ctx.valid(branch_id):
                    placed[cid] = (branch_id, source_label)
        return Answer(
            found={pk: placed[cid] for pk, cid in links.items() if cid in placed},
            pending={pk for pk, cid in links.items() if cid not in placed},
        )

    return Source(label, derive)


def journal_owner() -> Source:
    """The branch of the document that raised the journal, from every registered owner."""
    label = "the document that raised it"

    def derive(ctx, model_label, pks):
        related: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for owner in journal_owners():
            model = apps.get_model(owner.model_label)
            ref_field = owner.via or "pk"
            ref_label = owner.via_label or owner.model_label
            for chunk in _chunks(pks):
                rows = (
                    model._base_manager.filter(**{f"{owner.journal_field}__in": chunk})
                    .values_list(owner.journal_field, ref_field)
                )
                for journal_id, ref in rows:
                    if ref is not None:
                        related[journal_id].append((ref_label, ref))
        return _agree(ctx, related, label)

    return Source(label, derive)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
@dataclass
class Flag:
    """A row left blank for an administrator, and why."""

    pk: int
    label: str
    reason: str


@dataclass
class TargetPlan:
    """One model's unbranched rows at one entity, and what becomes of each."""

    target: Target
    blank: int = 0
    assign: dict[int, tuple[int, str]] = field(default_factory=dict)
    flags: list[Flag] = field(default_factory=list)
    blocked: bool = False

    @property
    def derived(self) -> Counter:
        return Counter(how for _, how in self.assign.values() if how != ONLY_BRANCH)

    @property
    def fallback(self) -> int:
        return sum(1 for _, how in self.assign.values() if how == ONLY_BRANCH)


@dataclass
class EntityPlan:
    entity: object
    ctx: Context
    targets: list[TargetPlan]

    @property
    def owns_no_branch(self) -> bool:
        return not self.ctx.branch_names


def _row_labels(model, pks: Iterable[int]) -> dict[int, str]:
    """A human handle per row: its document number, reference, name or code."""
    names = {f.name for f in model._meta.concrete_fields}
    handle = next((f for f in ("document_number", "reference", "name", "code") if f in names), None)
    if handle is None:
        return {}
    out: dict[int, str] = {}
    for chunk in _chunks(pks):
        out.update(model._base_manager.filter(pk__in=chunk).values_list("pk", handle))
    return {k: v for k, v in out.items() if v}


def _reason(ctx: Context, target: Target, split, pending: list[str]) -> str:
    """Why a row is left for an administrator, in the words the audit prints."""
    if split is not None:
        source_label, branches = split
        names = ", ".join(sorted(ctx.branch_names[b] for b in branches))
        return f"{source_label} disagree: {names}"
    if not target.sources:
        return target.no_source_note or "nothing on the row says which branch it is"
    if pending:
        named = pending[0] if len(pending) == 1 else f"{', '.join(pending[:-1])} or {pending[-1]}"
        return f"no branch on {named}"
    tried = ", ".join(s.label for s in target.sources)
    return f"nothing links it to a branch (tried {tried})"


def plan_entity(entity) -> EntityPlan:
    """Work out every unbranched row's branch at ``entity``. Reads only."""
    ctx = Context(entity)
    plans: list[TargetPlan] = []
    for target in targets():
        model = target.model
        owner = entity.tenant if target.scope_field == "tenant" else entity
        rows = model._base_manager.filter(**{target.scope_field: owner})
        if target.has_branch_column:
            rows = rows.filter(branch__isnull=True)
        if target.whole_tenant is not None:
            whole = target.whole_tenant
            rows = rows.exclude(whole() if callable(whole) else whole)
        pks = list(rows.order_by("pk").values_list("pk", flat=True))
        plan = TargetPlan(target=target, blank=len(pks))
        plans.append(plan)
        if not pks:
            continue
        if not ctx.branch_names:
            plan.blocked = True
            continue

        remaining = set(pks)
        splits: dict[int, tuple[str, set[int]]] = {}
        pending: dict[int, list[str]] = defaultdict(list)
        for source in target.sources:
            if not remaining:
                break
            for chunk in _chunks(sorted(remaining)):
                answer = source.derive(ctx, target.model_label, chunk)
                for pk, (branch_id, how) in answer.found.items():
                    if pk in remaining and ctx.valid(branch_id):
                        plan.assign[pk] = (branch_id, how)
                        remaining.discard(pk)
                for pk, branches in answer.split.items():
                    splits.setdefault(pk, (source.label, branches))
                for pk in answer.pending:
                    pending[pk].append(source.label)
            ctx.planned[target.model_label].update({pk: b for pk, (b, _) in plan.assign.items()})

        if remaining and ctx.only_branch_id is not None:
            for pk in remaining:
                plan.assign[pk] = (ctx.only_branch_id, ONLY_BRANCH)
            ctx.planned[target.model_label].update({pk: ctx.only_branch_id for pk in remaining})
        elif remaining:
            labels = _row_labels(model, remaining)
            plan.flags = [
                Flag(pk=pk, label=labels.get(pk, ""), reason=_reason(ctx, target, splits.get(pk), pending.get(pk, [])))
                for pk in sorted(remaining)
            ]
    return EntityPlan(entity=entity, ctx=ctx, targets=plans)


@dataclass(frozen=True)
class Gate:
    """A check that refuses at a multi-branch tenant while its inputs are unbranched.

    ``count`` is how many unbranched ``unit`` s feed it today; ``resolved`` is
    how many of them the plan gives a branch, through the journal entry that
    carries them.
    """

    name: str
    unit: str
    count: int
    resolved: int


def gates(plan: EntityPlan) -> list[Gate]:
    """What stands between this tenant and its year close and tax returns.

    The year-end close refuses while any income or expense entry in an open year
    has no branch, and a tax return refuses while any line it would declare has
    none. Both read an entry's branch, so both are cleared by the journal
    entries this plan resolves. Counted with the close's and the tax module's own
    helpers, so the numbers are the ones those checks will see. A tenant with one
    branch, or none, is never refused, and has no gates.
    """
    from vs_config.clock import tenant_today

    from .branch_ledger import branches_in_year, ledger_lines
    from .constants import AccountType, PeriodStatus
    from .models import FiscalYear, JournalLine, TaxObligation
    from .tax_filing import branch_breakdown, branch_rule, collect_source_lines

    ctx, entity = plan.ctx, plan.entity
    if len(ctx.branch_names) < 2:
        return []
    planned = set(ctx.planned.get("vs_finance.JournalEntry", {}))
    result: list[Gate] = []

    pl_types = (AccountType.INCOME, AccountType.EXPENSE)
    open_years = (
        FiscalYear.objects.filter(entity=entity)
        .exclude(status__in=[PeriodStatus.CLOSED, PeriodStatus.LOCKED]).order_by("year")
    )
    for year in open_years:
        shape = branches_in_year(entity, year, account_types=pl_types)
        if not shape.unbranched_entries:
            continue
        entry_ids = set(
            ledger_lines(entity)
            .filter(entry__period__fiscal_year=year, account__account_type__in=pl_types,
                    entry__branch__isnull=True)
            .values_list("entry_id", flat=True)
        )
        result.append(Gate(
            name=f"year close {year.year}", unit="entry",
            count=shape.unbranched_entries, resolved=len(entry_ids & planned),
        ))

    rule = branch_rule(entity)
    today = tenant_today(entity.tenant)
    for obligation in TaxObligation.objects.filter(entity=entity).order_by("code"):
        lines = collect_source_lines(obligation, period_end=today, rule=rule)
        pending = sum(share.line_count for share in branch_breakdown(lines).values() if share.pending)
        if not pending:
            continue
        pending_ids = [line.id for line in lines if line.pending]
        entry_of = dict(JournalLine.objects.filter(pk__in=pending_ids).values_list("pk", "entry_id"))
        result.append(Gate(
            name=f"{obligation.code} return", unit="line", count=pending,
            resolved=sum(1 for line_id in pending_ids if entry_of.get(line_id) in planned),
        ))
    return result


def entities(*, tenant_slug: str | None = None, entity_code: str | None = None):
    """The ledger entities a run covers, platform books included."""
    from .models import LedgerEntity

    rows = LedgerEntity.objects.select_related("tenant").order_by("tenant__name", "code")
    if tenant_slug:
        rows = rows.filter(tenant__slug=tenant_slug)
    if entity_code:
        rows = rows.filter(code=entity_code)
    return list(rows)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
@dataclass
class ApplyResult:
    written: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)


def apply_plan(plan: EntityPlan, *, batch_size: int = DEFAULT_BATCH_SIZE, on_change=None) -> ApplyResult:
    """Write ``plan``'s assignments in batches, auditing each change.

    ``on_change(target_plan, pk, label, branch_id, how)`` is called for each
    row written, after its batch commits.
    """
    result = ApplyResult()
    for target_plan in plan.targets:
        target = target_plan.target
        if not target.has_branch_column or not target_plan.assign:
            continue
        model = target.model
        stamp = "updated_at" in {f.name for f in model._meta.concrete_fields}
        for batch in _chunks(sorted(target_plan.assign.items()), batch_size):
            written = _write_batch(plan, target, model, batch, stamp)
            result.written[target.model_label] += len(written)
            result.skipped[target.model_label] += len(batch) - len(written)
            if on_change:
                for pk, label, branch_id, how in written:
                    on_change(target_plan, pk, label, branch_id, how)
    return result


def _write_batch(plan: EntityPlan, target: Target, model, batch, stamp: bool):
    """One transaction: lock, write the rows still blank, audit each one."""
    ids = [pk for pk, _ in batch]
    with transaction.atomic():
        blank = set(
            model._base_manager.select_for_update()
            .filter(pk__in=ids, branch__isnull=True).values_list("pk", flat=True)
        )
        by_branch: dict[int, list[int]] = defaultdict(list)
        for pk, (branch_id, _) in batch:
            if pk in blank:
                by_branch[branch_id].append(pk)
        extra = {"updated_at": timezone.now()} if stamp else {}
        for branch_id, pks in by_branch.items():
            model._base_manager.filter(pk__in=pks, branch__isnull=True).update(branch_id=branch_id, **extra)
        labels = _row_labels(model, blank)
        written = []
        for pk, (branch_id, how) in batch:
            if pk not in blank:
                continue
            label = labels.get(pk, "")
            _audit(plan, target, pk, label, branch_id, how)
            written.append((pk, label, branch_id, how))
    return written


def _audit(plan: EntityPlan, target: Target, pk, label, branch_id, how) -> None:
    from vs_audit.models import AuditActionType
    from vs_audit.services import emit_audit_event

    branch_name = plan.ctx.branch_names[branch_id]
    event = emit_audit_event(
        module_key=target.audit_module,
        action_type=AuditActionType.UPDATE,
        entity_type=target.model_label,
        entity_id=str(pk),
        entity_label=label or str(pk),
        tenant=plan.ctx.tenant,
        summary=f"Branch set to {branch_name} by the branch backfill, from {how}",
        before_data={"branch_id": None},
        diff_data={"branch_id": {"before": None, "after": branch_id}},
        metadata={
            "change": "branch_backfill",
            "derived_from": how,
            "branch_name": branch_name,
            "ledger_entity": plan.entity.code,
        },
        branch=branch_id,
    )
    if event is None:
        raise BackfillAuditError(
            f"Could not record the branch change on {target.model_label} {pk}; "
            f"its batch was rolled back."
        )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def describe(plan: EntityPlan, *, flagged_limit: int = 0) -> list[str]:
    """The plan as plain lines: one table per entity, then the flagged rows.

    ``flagged_limit`` caps the flagged rows printed per model; 0 prints them all.
    """
    entity, tenant = plan.entity, plan.ctx.tenant
    count = len(plan.ctx.branch_names)
    shape = "owns no branch" if not count else f"{count} branch{'es' if count != 1 else ''}"
    lines = [f"{tenant.name} [{tenant.slug}, {tenant.kind}] books {entity.code}: {shape}"]

    written = [p for p in plan.targets if p.target.has_branch_column and p.blank]
    reported = [p for p in plan.targets if not p.target.has_branch_column and p.blank]
    if not written and not reported:
        lines.append("  no unbranched rows")
        return lines
    if plan.owns_no_branch:
        total = sum(p.blank for p in written)
        lines.append(
            f"  BLOCKED: {total} unbranched row(s) and no branch to give them. "
            f"Prerequisite: create this tenant's branch first."
        )
        for p in written:
            lines.append(f"    {p.target.model_label:<40} {p.blank:>6}")
        return lines

    header = f"  {'model':<40} {'blank':>6} {'derived':>8} {'one-branch':>11} {'needs admin':>12}"
    if written:
        lines.append(header)
        for p in written:
            lines.append(
                f"  {p.target.model_label:<40} {p.blank:>6} {sum(p.derived.values()):>8} "
                f"{p.fallback:>11} {len(p.flags):>12}"
            )
    if reported:
        lines.append("  no branch column yet (reported, never written):")
        lines.append(header)
        for p in reported:
            lines.append(
                f"  {p.target.model_label:<40} {p.blank:>6} {sum(p.derived.values()):>8} "
                f"{p.fallback:>11} {len(p.flags):>12}"
            )
    blocking = gates(plan)
    if blocking:
        lines.append("  refused at a multi-branch tenant until these have a branch:")
        for gate in blocking:
            left = gate.count - gate.resolved
            lines.append(
                f"    {gate.name}: {gate.count} unbranched {gate.unit}(s); the backfill "
                f"resolves {gate.resolved}, {left} need an administrator"
            )
    derived = [p for p in plan.targets if p.derived]
    if derived:
        lines.append("  derived from:")
        for p in derived:
            parts = ", ".join(f"{how} {n}" for how, n in p.derived.most_common())
            lines.append(f"    {p.target.model_label}: {parts}")
    flagged = [p for p in plan.targets if p.flags]
    if flagged:
        lines.append("  flagged for an administrator:")
        for p in flagged:
            shown = p.flags if not flagged_limit else p.flags[:flagged_limit]
            for flag in shown:
                handle = f" {flag.label}" if flag.label else ""
                lines.append(f"    {p.target.model_label} #{flag.pk}{handle}: {flag.reason}")
            if len(shown) < len(p.flags):
                lines.append(f"    {p.target.model_label}: {len(p.flags) - len(shown)} more")
    return lines
