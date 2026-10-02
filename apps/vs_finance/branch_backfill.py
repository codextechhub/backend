"""Finance's transaction models, as the branch backfill derives them.

Discovered by :mod:`vs_finance.branch_derivation` on first use. Each target's
sources are tried in the order written here and the first answer wins; see that
module for what happens when none answers. The order numbers put a model after
every model it reads: bank accounts first, because payments and refunds read
them; invoices before everything that reads an invoice; journals last, because
they read the documents that raised them.

Where a model has no source at all, ``no_source_note`` is the reason an
administrator is shown, and a one-branch tenant still files the row under its
only branch.

The finance audit trail comes last, after every document it can be about: an
entry takes the branch of the document it records, including a branch this
same run has only planned for that document. An entry about something that
belongs to the whole tenant (a setting, a fiscal year, a central payroll run,
the tenant's tax return) is neither planned nor flagged, and stays visible to
whole-school readers only. The platform audit trail's copies of those entries
follow them the same way; that trail is kept per tenant, not per set of books.
"""
from __future__ import annotations

from collections import defaultdict

from django.db.models import CharField, Q
from django.db.models.functions import Cast

from .branch_derivation import (
    Answer,
    JournalOwner,
    Source,
    Target,
    _chunks,
    agreeing,
    audit_references,
    bank_behind,
    banks_on_journal,
    customer,
    journal_owner,
    register_audit_reference,
    register_journal_owner,
    register_target,
    targets,
    user_branch,
    via,
)

INVOICE = "vs_finance.Invoice"
PAYMENT = "vs_finance.Payment"
CREDIT_NOTE = "vs_finance.CreditNote"
BANK_ACCOUNT = "vs_finance.BankAccount"
FIXED_ASSET = "vs_finance.FixedAsset"
JOURNAL = "vs_finance.JournalEntry"
AUDIT_LOG = "vs_finance.FinanceAuditLog"
PLATFORM_AUDIT = "vs_audit.AuditEvent"

#: An emailed document's audit entry names the delivery; the delivery names the document.
_DELIVERED = {"InvoiceDelivery": INVOICE, "ReceiptDelivery": PAYMENT}


def _document_labels() -> dict[str, str]:
    """``{class name: model label}`` of every backfilled document, as an entry's ``target_type`` names it.

    A class name two apps share names neither, so such an entry is left for the
    whole tenant rather than guessed.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for target in targets():
        if target.has_branch_column and target.model_label not in _AUDIT_FIELDS:
            seen[target.model.__name__].add(target.model_label)
    return {name: labels.pop() for name, labels in seen.items() if len(labels) == 1}


#: How each audit trail names what an entry is about: ``(type field, id field)``.
_AUDIT_FIELDS = {
    AUDIT_LOG: ("target_type", "target_id"),
    PLATFORM_AUDIT: ("entity_type", "entity_id"),
}


def _entries(model_label, pks):
    """``(pk, class name, id, metadata)`` of audit rows of either trail.

    The finance trail names its target by class (``Invoice``); the platform
    trail's copy by label (``vs_finance.Invoice``), and the backfill's own events
    by their model's label (``vs_procurement.PurchaseOrder``). All read as the
    class name, which is what :func:`_document_labels` is keyed by.
    """
    from django.apps import apps

    type_field, id_field = _AUDIT_FIELDS[model_label]
    model = apps.get_model(model_label)
    for chunk in _chunks(pks):
        rows = model._base_manager.filter(pk__in=chunk).values_list("pk", type_field, id_field, "metadata")
        for pk, target_type, target_id, metadata in rows:
            yield pk, str(target_type or "").rsplit(".", 1)[-1], target_id, metadata or {}


def _answer(ctx, links: dict[int, tuple[str, int]], label: str) -> Answer:
    """Each entry's branch from the row ``links`` names, pending where that row has none."""
    wanted: dict[str, set[int]] = defaultdict(set)
    for model_label, ref in links.values():
        wanted[model_label].add(ref)
    known = {model_label: ctx.branches_of(model_label, refs) for model_label, refs in wanted.items()}
    found = {
        pk: (known[model_label][ref], label)
        for pk, (model_label, ref) in links.items() if ref in known[model_label]
    }
    return Answer(found=found, pending=set(links) - found.keys())


def _as_id(value):
    return int(value) if str(value).isdigit() else None


def audited_document() -> Source:
    """The branch of the document an entry's ``target_type``/``target_id`` names."""
    label = "the document it is about"

    def derive(ctx, model_label, pks):
        documents = _document_labels()
        links = {}
        for pk, target_type, target_id, _metadata in _entries(model_label, pks):
            ref = _as_id(target_id)
            if target_type in documents and ref is not None:
                links[pk] = (documents[target_type], ref)
        return _answer(ctx, links, label)

    return Source(label, derive)


def audited_reference() -> Source:
    """The branch of the row an entry's metadata names (:func:`register_audit_reference`)."""
    label = "the record its details name"

    def derive(ctx, model_label, pks):
        references = audit_references()
        links = {}
        for pk, target_type, _target_id, metadata in _entries(model_label, pks):
            if target_type not in references:
                continue
            key, reference_label = references[target_type]
            ref = _as_id(metadata.get(key))
            if ref is not None:
                links[pk] = (reference_label, ref)
        return _answer(ctx, links, label)

    return Source(label, derive)


def delivered_document() -> Source:
    """The branch of the invoice or receipt an emailed document's entry concerns."""
    label = "the document it emailed"

    def derive(ctx, model_label, pks):
        from .models import FinanceDocumentDelivery

        delivery_of = {}
        for pk, target_type, target_id, _metadata in _entries(model_label, pks):
            ref = _as_id(target_id)
            if target_type in _DELIVERED and ref is not None:
                delivery_of[pk] = (_DELIVERED[target_type], ref)
        documents = dict(
            FinanceDocumentDelivery.objects.filter(pk__in={ref for _, ref in delivery_of.values()})
            .values_list("pk", "document_id")
        ) if delivery_of else {}
        links = {
            pk: (document_label, _as_id(documents.get(ref)))
            for pk, (document_label, ref) in delivery_of.items()
            if _as_id(documents.get(ref)) is not None
        }
        return _answer(ctx, links, label)

    return Source(label, derive)


def _unplaceable_entries() -> Q:
    """The audit entries the backfill leaves alone: those about the whole tenant.

    Everything whose ``target_type`` no source reads (a setting, a fiscal year,
    the ledger entity, a vendor contract, a customer's statement), an entry whose
    reference is missing from its metadata, and every entry about a central
    payroll run or the tenant's tax return, which are booked per branch share and
    so are nobody's one branch.
    """
    from .models import PayrollRun, TaxFiling

    references = audit_references()
    readable = set(_document_labels()) | set(references) | set(_DELIVERED)
    whole = ~Q(target_type__in=sorted(readable))
    for target_type, (key, _label) in references.items():
        whole |= Q(target_type=target_type) & ~Q(metadata__has_key=key)
    for model, target_type in ((PayrollRun, "PayrollRun"), (TaxFiling, "TaxFiling")):
        whole |= Q(
            target_type=target_type,
            target_id__in=model._base_manager.filter(branch__isnull=True)
            .annotate(ref=Cast("pk", CharField())).values("ref"),
        )
    return whole


def _unplaceable_events() -> Q:
    """The platform trail's events the backfill leaves alone.

    Every event of a module other than finance and procurement, and among
    those, the same entries :func:`_unplaceable_entries` leaves alone, named the
    way the platform trail names them: ``vs_finance.Invoice`` for the finance
    trail's copy, the model's label for the backfill's own events.
    """
    from .models import PayrollRun, TaxFiling

    documents = _document_labels()
    references = audit_references()
    readable = (
        {f"vs_finance.{name}" for name in documents} | set(documents.values())
        | {f"vs_finance.{name}" for name in references} | {f"vs_finance.{name}" for name in _DELIVERED}
    )
    whole = ~Q(module_key__in=["FINANCE", "PROCUREMENT"]) | ~Q(entity_type__in=sorted(readable))
    for target_type, (key, _label) in references.items():
        whole |= Q(entity_type=f"vs_finance.{target_type}") & ~Q(metadata__has_key=key)
    for model, label in ((PayrollRun, "vs_finance.PayrollRun"), (TaxFiling, "vs_finance.TaxFiling")):
        whole |= Q(
            entity_type=label,
            entity_id__in=model._base_manager.filter(branch__isnull=True)
            .annotate(ref=Cast("pk", CharField())).values("ref"),
        )
    return whole


_TARGETS = (
    Target(
        BANK_ACCOUNT, (), order=5,
        no_source_note=(
            "a bank account shared by several branches becomes one record per "
            "branch; an administrator decides which branch this record is"
        ),
    ),
    Target(INVOICE, (customer(),), order=10),
    Target(
        PAYMENT,
        (
            agreeing("the allocated invoices", "vs_finance.PaymentAllocation", "payment", ("invoice", INVOICE)),
            bank_behind("the deposit bank account", "deposit_account"),
            customer(),
        ),
        order=20,
    ),
    Target(CREDIT_NOTE, (via("the invoice", "invoice", INVOICE), customer()), order=30),
    Target("vs_finance.BankTransaction", (via("the bank account", "bank_account", BANK_ACCOUNT),), order=20),
    Target("vs_finance.BankTransfer", (via("the sending bank account", "from_account", BANK_ACCOUNT),), order=20),
    # Inter-branch documents are raised with both branches named and never blank;
    # they are listed so a row written blank by hand is still placed or flagged.
    Target(
        "vs_finance.InterBranchTransfer",
        (via("the sending bank account", "from_bank_account", BANK_ACCOUNT),), order=20,
        no_source_note="an inter-branch transfer names its sending branch; an administrator decides which",
    ),
    Target(
        "vs_finance.HeldForBranchReceipt",
        (via("the bank account that received it", "bank_account", BANK_ACCOUNT),), order=20,
    ),
    Target(
        "vs_finance.PayerPayment",
        (via("the bank account that received it", "bank_account", BANK_ACCOUNT),), order=20,
    ),
    Target(
        "vs_finance.InterBranchRecharge", (), order=20,
        no_source_note="a recharge names the branch that paid the cost; an administrator decides which",
    ),
    Target(
        "vs_finance.WriteOffRequest",
        (via("the invoice", "invoice", INVOICE), customer("invoice__customer")),
        order=30,
    ),
    Target("vs_finance.Concession", (via("the invoice", "invoice", INVOICE), customer()), order=30),
    Target("vs_finance.PaymentPlan", (via("the invoice", "invoice", INVOICE), customer()), order=30),
    Target(
        "vs_finance.CustomerCreditTransfer",
        (via("the receipt it gave", "receipt", PAYMENT), customer("from_customer")),
        order=30,
    ),
    Target("vs_finance.DunningNotice", (via("the invoice", "invoice", INVOICE), customer()), order=30),
    Target(
        "vs_finance.Refund",
        (
            agreeing(
                "the refunded notes and payments", "vs_finance.RefundAllocation", "refund",
                ("payment", PAYMENT), ("note", CREDIT_NOTE),
            ),
            via("the bank account", "bank_account", BANK_ACCOUNT),
            customer(),
        ),
        order=40,
    ),
    Target(
        "vs_finance.PettyCashFund",
        (
            user_branch("the custodian", "custodian"),
            agreeing("the fund's vouchers", "vs_finance.PettyCashVoucher", "fund", ("id", "vs_finance.PettyCashVoucher")),
        ),
        order=50,
    ),
    Target("vs_finance.PettyCashVoucher", (via("the fund", "fund", "vs_finance.PettyCashFund"),), order=55),
    Target("vs_finance.PettyCashReturn", (via("the fund", "fund", "vs_finance.PettyCashFund"),), order=55),
    Target("vs_finance.ExpenseClaim", (user_branch("the claimant", "claimant"),), order=50),
    Target(FIXED_ASSET, (banks_on_journal("the funding bank account", "acquisition_journal"),), order=50),
    # A run with no branch is a central run, booked one journal per branch.
    Target("vs_finance.PayrollRun", (), order=50, whole_tenant=Q(branch__isnull=True)),
    # A return with no branch is the tenant's one return, booked per branch share.
    Target("vs_finance.TaxFiling", (), order=50, whole_tenant=Q(branch__isnull=True)),
    # A provision run is raised for every branch and booked one journal per branch line.
    Target("vs_finance.DoubtfulDebtProvision", (), order=50, whole_tenant=Q(branch__isnull=True)),
    Target(
        "vs_finance.Budget", (), order=50,
        no_source_note="every budget belongs to a branch; an administrator decides which",
    ),
    Target(
        JOURNAL,
        (
            journal_owner(),
            via("the entry it reverses", "reverses", JOURNAL),
            banks_on_journal("the bank accounts on its lines", "pk"),
        ),
        order=900,
    ),
    Target(
        AUDIT_LOG,
        (audited_document(), audited_reference(), delivered_document()),
        order=1000, whole_tenant=_unplaceable_entries,
    ),
    Target(
        PLATFORM_AUDIT,
        (audited_document(), audited_reference(), delivered_document()),
        order=1010, whole_tenant=_unplaceable_events, scope_field="tenant",
    ),
)

#: Audit entries whose target carries no branch, and where their details name the row that does.
_AUDIT_REFERENCES = (
    ("BankStatement", "bank_account_id", BANK_ACCOUNT),
    ("BankStatementLine", "bank_account_id", BANK_ACCOUNT),
)

#: Every finance model holding a foreign key to the journal it raised.
_JOURNAL_OWNERS = (
    JournalOwner(INVOICE, "journal"),
    JournalOwner(PAYMENT, "journal"),
    JournalOwner(CREDIT_NOTE, "journal"),
    JournalOwner("vs_finance.Refund", "journal"),
    JournalOwner("vs_finance.WriteOffRequest", "journal"),
    JournalOwner("vs_finance.Concession", "journal"),
    JournalOwner("vs_finance.ExpenseClaim", "journal"),
    JournalOwner("vs_finance.PettyCashVoucher", "journal"),
    JournalOwner("vs_finance.PettyCashReturn", "journal"),
    JournalOwner("vs_finance.BankTransaction", "journal"),
    JournalOwner("vs_finance.BankTransfer", "journal"),
    # Each side of an inter-branch transfer is its own branch's journal; the leg
    # always carries that branch, so it needs no target of its own.
    JournalOwner("vs_finance.InterBranchTransferLeg", "journal"),
    JournalOwner("vs_finance.HeldForBranchReceipt", "journal"),
    JournalOwner("vs_finance.TaxFiling", "filing_journal"),
    JournalOwner("vs_finance.TaxFilingShare", "filing_journal"),
    JournalOwner("vs_finance.TaxRemittance", "journal"),
    JournalOwner("vs_finance.TaxRemittance", "reversal_journal"),
    JournalOwner("vs_finance.PayrollRun", "journal"),
    JournalOwner("vs_finance.PayrollRun", "disbursement_journal"),
    JournalOwner("vs_finance.PayrollRunBranch", "journal"),
    JournalOwner("vs_finance.PayrollRunBranch", "disbursement_journal"),
    JournalOwner(FIXED_ASSET, "acquisition_journal"),
    JournalOwner(FIXED_ASSET, "disposal_journal"),
    JournalOwner("vs_finance.DepreciationSchedule", "journal", via="asset", via_label=FIXED_ASSET),
    JournalOwner("vs_finance.BankStatementLine", "adjusting_journal", via="bank_account", via_label=BANK_ACCOUNT),
    JournalOwner("vs_finance.CustomerCreditAllocationJournal", "journal", via="payment", via_label=PAYMENT),
    JournalOwner("vs_finance.CustomerCreditAllocationJournal", "journal", via="note", via_label=CREDIT_NOTE),
    JournalOwner("vs_finance.DeferredIncomeRelease", "journal"),
    JournalOwner("vs_finance.DeferredIncomeEntry", "void_journal"),
    # The adjusting journal belongs to its credit note, concession or write-off. An
    # unbranched one predates receivable moves, so its shares are its own branch's.
    JournalOwner("vs_finance.DeferredIncomeUnwind", "adjustment_entry",
                 via="entry", via_label="vs_finance.DeferredIncomeEntry"),
    JournalOwner("vs_finance.DoubtfulDebtProvisionLine", "journal"),
    JournalOwner("vs_finance.WriteOffRecovery", "journal",
                 via="write_off", via_label="vs_finance.WriteOffRequest"),
    JournalOwner("vs_finance.DepositForfeiture", "journal"),
)

for _target in _TARGETS:
    register_target(_target)
for _owner in _JOURNAL_OWNERS:
    register_journal_owner(_owner)
for _reference in _AUDIT_REFERENCES:
    register_audit_reference(*_reference)
