#!/usr/bin/env python3
"""Cut M19 v1.12: branch books, three dashboards, open reference lists and governed pay fields.

Every queue entry naming Module 19, and therefore the document:

* D2 (fa23e06d). A school holding two active sets of books is still refused on
  every finance and procurement read the finance abstraction layer makes, and
  the refusal now opens exactly one system-health incident per school. The
  one-primary rule has no column and no constraint behind it.
* D5 (3aab9000). Provisioned books carry one empty approval route per finance
  document type and no approver groups; for finance the empty route is the gate.
  Migration 0028 removes the groups the earlier provisioning minted.
* D6 (9557ad6e) and D10 (4767f637). The bank account number and the payroll-line
  and salary figures are registered fields, governed by the role's switches.
* D12, D14, D23, D24 (415953a7, f6359e25, a94d2d36, 22d45821). The lists every
  finance picker and report filter reads open to any finance key; balances and
  creation keep their keys; the chart has a tagged shape without balances.
* D15, D26, D27, D28 (278583e9, 10d047f1, fb3ccf16, 2371f364). The overview,
  receivables and spend views open to the module, each block behind its key and
  the reader's branches, read over a month, a quarter, the year or the term.
* D16, D17, D21 (dcc692e2, 4e3f3456, 03928649). AR aging narrows; the statements
  answer a branch-bound reader from their own journals; the statutory pack is
  the school's filing and refused to that reader. The refunds and write-offs
  list opens on either key with rows per kind.
* D20, D22 (c6dfe7ab, 8ca7af04). A branch keeps its own budget; the school's
  plan shows a branch reader the plan without the school's actuals.
* D32 (bf517f8b). A school chooses its payroll scope at its own settings door.
* D47 (a5c36920). The chart with balances, the account drawer and the account
  activity read a branch-bound reader's own journals, as the statements do.
* D48 (8cd97cae). The engine fee run's all_active form bills only the caller's
  reach and holds the branch price-list rule; a branch reader's dunning run and
  adjustment batch lines stay in reach; batches confirm like single posts.
* D39 (324401bc). A fee structure with a branch is that branch's price list on
  the school's cohort billing route; the structure read carries branch_id.
* D50 (a8e0e4d0, d51ccc97, e1f42256). A bursar rolls back a statement she imported, on
  her statement key alone, from the statement's own row: each statement in a
  bank account's list carries import_rollback, the batch and job to roll back,
  or null where there is nothing the rollback would accept.
* D57 (61836114, 966e5770, 1fb5bec5, 07baab0a). A bank account a posting names
  stays within the caller's branches (404 outside), a ledger account behind a
  bank answers to the same list wherever a write names it, a branch's document
  is paid from its own branch's account or a school-wide one, and bank
  accounts, claims, floats, assets and refund-availability rows carry
  branch_id. Section 9's bank gap leaves.
* D58 (d7a700a8, 88873c1c). Journal post, expense-claim post and direct entry
  confirm an empty approval route; a direct entry at a school whose journal
  route has steps waits for approval. The direct-post gap leaves.
* Found while folding those: the dashboard's blocks also ask the school's
  plan (D56, 4af5d331) and its approvals block counts credit and debit notes
  (D59, d6260eec), which FR-022 records.
* D62 (0c59e23d, merged 864468ba). The same-branch rule reaches the ledger
  account behind a bank account: a branch's document names its own branch's
  bank ledger or a school-wide one, in the wording of its side ("Pay it from",
  "Deposit it into", "Book it against").

The MRD and the other FRDs are not revised here. The traceability follows MRD
v2.94, whose Module 19 note records the statements narrowed and which closes its
P2 gap on branch financial statements.

    python tools/patch_finance_dashboards_and_branch_books_docs.py
"""
from __future__ import annotations

from docx import Document

import patch_mrd_v2_79_docs as mrd_tools
from patch_backend_owned_permission_registry_docs import keep_rows_whole
from patch_document_type_labels_docs import require_newest
from patch_record_history_docs import (
    ROOT,
    append_rows,
    change_log_table,
    finish,
    frd_path,
    keep_format,
    log_change,
    set_control,
    set_cover_version,
)
from patch_restricted_grant_ladder_docs import (
    append_to,
    assert_absent_outside_log,
    edit_box,
    insert_row_after,
    row_labelled,
    table_headed,
)
from patch_staff_id_and_auth_events_docs import (
    edit_cell,
    edit_paragraph,
    edit_value,
    fr_table,
    normalise_change_log,
    repair_ooxml,
    row,
    set_status,
    set_value,
)

import patch_record_history_docs
from patch_record_history_docs import add_fr

REVIEW_DATE = "28 September 2026"
CODE_BASELINE = (
    "Backend main at 7860ea86, 28 September 2026, carrying the branch bank ledgers "
    "(0c59e23d, merged at 864468ba), the finance pass (merge "
    "32194627: bank reach 61836114, 966e5770, 1fb5bec5 and 07baab0a; empty-route "
    "confirmation d7a700a8 and 88873c1c; dashboards 4af5d331 and d6260eec) and the statement "
    "rollback (a8e0e4d0, d51ccc97, e1f42256)"
)
MRD_VERSION = "2.94"
SOURCE_MRD = f"XVS Module Requirements Document v{MRD_VERSION}"
TEST_EVIDENCE = (
    "Read against the code at 0fb6cfea, the statement rollback at e1f42256 and the finance "
    "pass at 6d3251c6. Test evidence is what each change recorded, the last "
    "full vs_finance run at 872 tests passing, with the tests the chart-of-accounts and "
    "bulk-run changes added; the suites were not rerun for this revision. "
    "The bank-ledger rule rests on LedgerOfAnotherBranchOnABranchDocumentTests, read at "
    "7860ea86 and not rerun. "
    "Backend evidence only; nothing here claims deployment."
)

patch_record_history_docs.REVIEW_DATE = REVIEW_DATE

M19_DIR = "19-finance-and-accounting"
M19_STEM = "XVS_M19_Finance_and_Accounting_Functional_Requirements_Document"
M19_SOURCE, M19_TARGET = "1.11", "1.12"

M19_SUMMARY = (
    "Minor revision. The branch rule reaches the books. A reader whose grants pin them to "
    "branches gets the trial balance, income statement, balance sheet, cash flow, changes in "
    "equity, cost and dimension analysis and AR reconciliation built from the journals of "
    "their branches and the school-wide entries, and every journal balances, so those "
    "statements still balance; the chart of accounts' balances, an account's detail and its "
    "activity read the same journals, so the chart agrees with the reader's trial balance; "
    "AR aging narrows as its lists do; the statutory pack is the "
    "school's filing and answers 403 to that reader (FR-006). A branch may keep a budget of "
    "its own, measured against its own journals, and the school's plan shows branch staff "
    "the plan without the school's actuals and is read-only to them (FR-012). The finance "
    "dashboard becomes three views, overview, receivables and spend, opening to the module "
    "with each block behind its key and the reader's branches, and read over a month, a "
    "quarter, the year or the school's term, whose fees are counted by the term they were "
    "billed for through the school layer's provider (new FR-022). The lists every picker "
    "and report filter reads open to any finance key while balances and creation keep "
    "theirs, the chart gains a tagged shape without balances, and the refunds and write-offs "
    "list opens on either key with each kind of row to its own holder (new FR-023). The bank "
    "account number and the pay figures are governed by the role's field switches (new "
    "FR-024). FR-016 records that a school holding two sets of books is reported to the "
    "console as one incident, and that the one-primary rule has no constraint behind it; "
    "FR-018 that books arrive with empty approval routes and no approver groups, the empty "
    "route being finance's gate; FR-021 the school's own payroll-scope door; FR-020 that a "
    "fee structure with a branch is that branch's price list, on the school's cohort route and "
    "the engine's generate alike. FR-019 records the bulk runs held to the caller's branches: "
    "the engine fee run's all_active form, a branch reader's dunning run, and adjustment batch "
    "lines, which answer 404 outside the reach. The bank account a posting names is held to "
    "the caller's reach on every money route, another branch's answering 404 as an unknown "
    "one does, and a ledger account behind a bank answers to the same list wherever a write "
    "names it, refused as unknown; a branch's document is paid only from its own branch's "
    "account or a school-wide one, a 400 naming the branch; bank accounts, expense claims, "
    "petty-cash funds, fixed assets and refund-availability rows carry branch_id (61836114, "
    "966e5770, 1fb5bec5, 07baab0a; FR-019, FR-020, Section 7). Journal post, expense-claim "
    "post and direct entry now confirm an empty approval route as the adjustments do: 409 "
    "APPROVAL_NOT_CONFIGURED until confirmed and then recorded as made without approval, a "
    "route with steps sending the caller to submit, or for a direct entry submitting it into "
    "the route (d7a700a8, 88873c1c; FR-003, FR-008). FR-022 records that a dashboard block "
    "also needs the school's plan to reach its key, and that the approvals block counts "
    "credit and debit notes (4af5d331, d6260eec). Needs Attention drops the statements gap "
    "and the direct-post confirmation gap, both closed, and names no branch gap in their "
    "place, since the bank account a posting names is held to the reach as well; two items "
    "are added, the two-sets-of-books incident notifying nobody "
    "and the export builder offering a choice of one set of books. A bursar who imported the "
    "wrong statement rolls it back herself, from the statement's own row, and imports it "
    "again: finance.bankaccount.import now covers the rollback of a statement batch "
    "(a8e0e4d0, Module 10), and each statement in a bank account's list carries "
    "import_rollback, the batch and job to roll back, or null for a statement keyed in by "
    "hand, a reconciled one, one with a line already acted on or one whose rollback has "
    "started and not finished (d51ccc97, e1f42256; FR-007, the actors, Section 7 and the "
    "Module 10 dependency). The rollback is one delete and finishes inside the request "
    "however many lines the statement has (e1f42256). The same-branch rule reaches the ledger "
    "account behind a bank account: an asset purchase's credit account, a customer receipt's "
    "and an invoice payment's deposit account, a bank adjustment's counter account, and a "
    "Module 18 payment request's and virtual account's deposit account follow the document's "
    "branch, and another branch's bank ledger is refused 400 in the wording of the document's "
    "side, \"Pay it from\", \"Deposit it into\" or \"Book it against\"; payouts and payout "
    "batches stay school-wide (0c59e23d; FR-020, Section 7). Traceability is retraced against MRD "
    f"v{MRD_VERSION}, still 26 entries, whose Module 19 note records the statements narrowed "
    "and closes its branch-statements gap. " + TEST_EVIDENCE
)


# ── control, scope and context ───────────────────────────────────────────────


def patch_control(doc) -> None:
    set_cover_version(doc, M19_SOURCE, M19_TARGET)
    set_control(doc, "Version", M19_TARGET)
    set_control(doc, "Review date", REVIEW_DATE)
    set_control(doc, "Code baseline", CODE_BASELINE)
    set_control(doc, "Source MRD", SOURCE_MRD)


def patch_scope(doc) -> None:
    """Section 1.1: the reporting and budget rows name what now exists."""
    keep_format(
        row_labelled(doc, "Reporting").cells[-1],
        "Trial balance, income statement, balance sheet, cash flow, changes in equity, the "
        "statutory pack, analytics slices, AR aging and reconciliation, and the finance "
        "dashboard's three views. Each answers a branch-bound reader for their branches, "
        "except the statutory pack, which is the school's filing alone.",
    )
    keep_format(
        row_labelled(doc, "Budgets and assets").cells[-1],
        "School and branch budget plans with an approval lock and variance reporting, and "
        "fixed assets with straight-line depreciation and disposal.",
    )


def patch_branch_context(doc) -> None:
    """Section 2.3: the two exclusive readings finance makes, each on purpose."""
    boxes = [t for t in doc.tables
             if t.rows[0].cells[0].text.strip().startswith("THE NULL BRANCH")]
    if len(boxes) != 1:
        raise ValueError(f"The null-branch box was found {len(boxes)} times")
    edit_box(boxes[0].rows[0].cells[0], [
        ("append",
         "• Finance itself reads a branch exclusively in two places, each argued where it is "
         "made. A branch payroll run pays exactly its branch's roster, because an inclusive "
         "reading would pay an unassigned person once per branch (FR-021). A branch budget "
         "is measured against its branch's journals alone, because the school-wide entries "
         "are the school plan's to measure (FR-012)."),
    ])


def patch_actors(doc) -> None:
    """Section 3: the module-wide read rule and the field switches beside the keys."""
    edit_paragraph(
        doc, "Access is governed by RBAC permission keys",
        "Every actor below should therefore be read as this role, at the branches it was "
        "granted for.",
        "Every actor below should therefore be read as this role, at the branches it was "
        "granted for. Two rules sit beside the keys. The lists every finance screen builds "
        "its pickers and filters from open to anyone holding any finance key (FR-023). And "
        "the bank account number and the pay figures are read and written field by field, "
        "as the role's switches allow (FR-024).",
    )
    actors = table_headed(doc, "Actor", "May do", "Governed by")
    keep_format(
        row(actors, "Payroll officer").cells[-1],
        "finance.payrollrun.*, finance.salary.*; the pay figures by field switch (FR-024)",
    )
    keep_format(
        row(actors, "Bank reconciler").cells[-1],
        "finance.bankaccount.*; the account number by field switch (FR-024)",
    )
    insert_row_after(row(actors, "Payroll officer"), [
        "School administrator",
        "Choose whether the school runs one payroll or one per branch.",
        "school.settings.view, school.settings.update (FR-021)",
    ])


# ── requirements ─────────────────────────────────────────────────────────────


def patch_reports(doc) -> None:
    """FR-006: statements from the reader's own journals; the statutory pack stays whole."""
    fr006 = fr_table(doc, "FR-006")
    set_value(
        fr006, "Requirement",
        "The standard statements must be cheap to produce, must always balance, and must "
        "answer a reader whose grants pin them to branches for those branches.",
    )
    set_value(
        fr006, "Current evidence",
        "A whole-school reader's reports read the denormalised account balances that posting "
        "maintains rather than re-summing the journal: trial balance, income statement, "
        "balance sheet, cash flow, changes in equity, the statutory pack, analytics slices, "
        "and the AR reconciliation. A reader whose grants pin them to branches gets the same "
        "statements built from the journal lines of the entries in their branches plus the "
        "school-wide entries, through vs_finance.branch_ledger, summed into rows of the same "
        "shape; the statutory pack is the exception below. Every such payload carries "
        "narrowed, and a narrowed export is subtitled as covering the reader's branches and "
        "school-wide entries only. AR aging is built from the invoices, debit notes, receipts "
        "and credit notes themselves, so it narrows as their lists do, CSV, XLSX and PDF "
        "export included. The statutory pack is what the school files as one entity, so a "
        "branch-bound reader is refused it with 403 and the reason, rather than handed a "
        "branch-shaped pack that looks like a filing and is not one. A narrowed income "
        "statement carries no budget column, because the school's plan set against one "
        "branch's actuals reads as a shortfall that is only the other branches' share; a "
        "whole-school statement's budget column reads the school's plan only, never a "
        "branch's.",
    )
    append_to(
        fr006, "Acceptance",
        " Every journal balances on its own, so any set of whole journals balances too. "
        "BranchStatementTests in tests_branch_ledger.py proves a branch trial balance "
        "balances, a branch balance sheet satisfies its equation, a branch cash flow "
        "reconciles to its own cash, a reach over every branch gives the whole-school "
        "figures, and a filter the branch ledger does not understand fails loudly rather "
        "than reading the whole entity. BranchReportEndpointTests proves every statement "
        "endpoint says it is narrowed, a narrowed export says whose figures it holds, and the "
        "statutory pack is refused to a branch. ARAgingBranchTests proves aging and its "
        "export keep to the reader's branches and the school-wide customers.",
    )
    set_value(
        fr006, "Current limit",
        "A balance corrupted outside the posting service would be believed by the "
        "whole-school reports; the balances are a cache maintained at the one write point. A "
        "branch statement's cash is what its own journals moved rather than money the branch "
        "holds apart, because a school banks in shared accounts.",
    )


def patch_budgets(doc) -> None:
    """FR-012: a branch keeps its own plan; the school's is read-only to branch staff."""
    fr012 = fr_table(doc, "FR-012")
    set_value(
        fr012, "Requirement",
        "A plan that actuals are measured against must not be rewritten after the fact, and a "
        "branch may keep a plan of its own beside the school's.",
    )
    set_value(
        fr012, "Current evidence",
        "Budgets hold lines per account and period and never post to the ledger. Approving "
        "one locks it. Variance and heatmap views compare it against actuals. A budget with "
        "no branch is the school's plan, measured against the whole ledger; a budget with a "
        "branch is that branch's plan, measured against that branch's journals alone, since "
        "counting school-wide entries in every branch's plan would charge each branch the "
        "whole school's shared spend. Names are unique per school or per branch within a "
        "fiscal year, and a clash is refused naming whose name it is. The list shows a reader "
        "their branches' plans and the school's, each with branch_id, branch_name and "
        "can_manage, and says in filing which plans the reader may create. What a branch-bound "
        "reader creates is filed to their branch, naming another branch is refused, and a "
        "reader covering several branches names one of theirs. Another branch's plan answers "
        "404, and every write to the school's plan by a branch-bound reader answers 403 "
        "SHARED_RECORD_READ_ONLY. Such a reader sees the school's plan but not its actuals: "
        "the actual, variance and consumed figures are null, narrowed is true, and variance "
        "and heatmap rows for accounts the plan does not cover are left out, since their "
        "presence alone reports other branches' postings. A branch plan's actuals are shown "
        "in full to whoever can read it. The finance dashboard's revenue against budget and "
        "the income statement's budget column read the school's plan only.",
    )
    append_to(
        fr012, "Acceptance",
        " BudgetActualsForBranchReadersTests and BranchBudgetTests in tests_budget_branch.py "
        "(16 tests) prove each rule above, among them that a branch plan counts only its "
        "branch's journals, that a name is unique within one school or branch each year, and "
        "that the income statement compares against the school's plan alone.",
    )


def patch_entity_scope(doc) -> None:
    """FR-016: the picker's rule, and two sets of books reported as well as refused."""
    fr016 = fr_table(doc, "FR-016")
    append_to(
        fr016, "Current evidence",
        " Reading the list needs any finance key: resolve_entity, which every entity-scoped "
        "endpoint calls, asks nothing beyond tenant membership, so a list gated on a key of "
        "its own was stricter than opening the books it lists (FR-023). The finance "
        "abstraction layer finds a school's primary books among the tenant's active "
        "tenant-kind entities. There is no primary flag and no database constraint behind "
        "that rule, so the query is the rule, and a school holding two is refused on every "
        "finance and procurement read the layer makes. The refusal is reported as well as "
        "raised: the envelope decorator both raise sites share opens exactly one incident on "
        "the console's system-health board (Module 30), keyed on the school's tenant, and it "
        "sits outside the provisioning transaction, so the incident survives that rollback.",
    )
    append_to(
        fr016, "Acceptance",
        " AmbiguousPrimaryReportingTests proves a read raising three times opens one "
        "incident, each misconfigured school gets its own, resolving it lets the next "
        "occurrence open a fresh one, a failing reporter changes neither the error nor its "
        "message, and provisioning reports outside its own transaction.",
    )
    append_to(
        fr016, "Current limit",
        " The two-sets-of-books incident reaches the console only: no notification is sent, "
        "and it stays open after the school is fixed until an operator resolves it, unlike an "
        "incident an alert raised.",
    )


def patch_provisioning(doc) -> None:
    """FR-018: books arrive with empty approval routes and no approver groups."""
    fr018 = fr_table(doc, "FR-018")
    set_value(
        fr018, "Current evidence",
        "register_entity_provisioner runs registered callables inside the transaction that "
        "creates a ledger entity, alongside the currencies, chart of accounts and fiscal "
        "periods. Finance, procurement and payments each publish one approval route per "
        "approvable document type for the tenant, carrying no steps, and create no approver "
        "groups: who approves a school's money is read from the organogram it builds, and a "
        "ladder invented at creation would be a guess at who signs off and at what amount. "
        "Finance's routes are the four adjustment types and expense claims. For finance the "
        "empty route is the gate itself, because nothing seeds a platform-wide finance route "
        "behind it: with no route at all a refund would post straight to the ledger with "
        "nobody told, and with the empty one a direct post is refused until somebody confirms "
        "it and is recorded against it. A tenant that asks for the default ladders gets them "
        "from seed_finance_approvals, which fills an empty route and leaves a route with live "
        "steps alone. Migration 0028 removes the three finance approver groups the earlier "
        "provisioning minted, and the steps naming them, sparing a group with members, a "
        "group whose steps have run and a group something is waiting on. The finance "
        "abstraction layer provisions a school's books through this path rather than creating "
        "a ledger entity itself, which is the difference between books a school can post to "
        "and a row that fails at the first posting.",
    )
    append_to(
        fr018, "Acceptance",
        " ProvisionedBooksCarryNoLadderTests and SeededApproverGroupCleanupTests in "
        "tests_stageless_provisioning.py prove every finance type gets an empty route of its "
        "own and no group, a tenant's existing ladder keeps every step, the seeding command "
        "still publishes the full ladder, and the cleanup spares a group with a member, one "
        "whose step has run and one another app seeded.",
    )


def patch_branch_reads(doc) -> None:
    """FR-019: the reports, chart, dashboards, budgets and bulk runs are held to the rule."""
    fr019 = fr_table(doc, "FR-019")
    append_to(
        fr019, "Current evidence",
        " The statements are held to it from the reader's own journals (FR-006), and so are "
        "the chart of accounts with balances, an account's detail and its activity, which read "
        "the same journals so a branch reader's chart agrees with her trial balance to the "
        "kobo; the account itself is the school's and reads the same for everyone. The "
        "dashboards are held to it block by block (FR-022), and budgets by whose plan each is "
        "(FR-012). The bulk runs stay inside the reach too: the engine's fee generation with "
        "all_active bills only the active customers the caller reaches, a branch reader's "
        "dunning run chases only the invoices in their reach while the scheduled daily run "
        "stays school-wide, and refund and write-off batch lines resolve their customers and "
        "invoices within the reach, so another branch's row answers 404 as an unknown one "
        "does.",
    )
    append_to(
        fr019, "Acceptance",
        " ChartOfAccountsNarrowsTests in tests_branch_ledger.py proves a branch reader's "
        "chart, drawer and activity show her reach and agree with her trial balance, and that "
        "a whole-school reader's are unchanged. FeeRunBillsOnlyWhatTheCallerReachesTests and "
        "BulkRunsNarrowToTheCallersReachTests in tests_branch_reference.py prove the fee run, "
        "the dunning run and the batch lines keep to the caller's reach.",
    )
    set_value(
        fr019, "Current limit",
        "None.",
    )


def patch_branch_writes(doc) -> None:
    """FR-020: a fee structure with a branch is that branch's price list."""
    fr020 = fr_table(doc, "FR-020")
    append_to(
        fr020, "Current evidence",
        " A fee structure that carries a branch is that branch's price list: the school's "
        "cohort billing route refuses to bill a child at another branch with 409 WRONG_BRANCH, "
        "even for a caller who sees every branch, while a school-wide structure bills every "
        "branch (Module 17). The engine's own fee generation holds the same rule: a branch "
        "structure refuses a named customer filed under any other branch, school-wide "
        "included, with 409 WRONG_BRANCH, and all_active from it selects that branch's "
        "customers only. The fee structure read carries branch_id, null for the shared "
        "template.",
    )


def patch_payroll(doc) -> None:
    """FR-021: the school's own settings door, and the ten-name refusal."""
    fr021 = fr_table(doc, "FR-021")
    append_to(
        fr021, "Current evidence",
        " A school chooses the scope itself at GET and PATCH /v1/i/me/settings/payroll-scope/, "
        "under school.settings.view to read and school.settings.update to change it, live "
        "schools only and always the caller's own school; the answer names its source "
        "(school, platform or default) and offers the two choices in the school's words. The "
        "write goes through the configuration engine, so finance's guard runs exactly as it "
        "does from the console, and a refusal is a 400 INVALID_CONFIGURATION_VALUE keyed on "
        "scope. No config.* key is one a school can hold.",
    )
    edit_value(
        fr021, "Acceptance",
        "the refusal names those people rather than counting them,",
        "the refusal names up to ten of those people in its sentence rather than counting "
        "them,",
    )


def add_dashboard(doc) -> None:
    """New FR-022: three dashboard views, each block behind its key and the reader's branches."""
    add_fr(doc, "FR-022 Show Each Reader the Finance Dashboard Their Keys Allow",
           "FR-022 | Implemented", [
               ("Requirement",
                "The finance landing page must open to everybody working in finance, show each "
                "reader only the figures their keys and branches allow, and read over the "
                "window the books' owner actually bills by."),
               ("Current evidence",
                "Three views share one opening rule and the same ?window= and ?period=: the "
                "overview at /reports/dashboard/, open to any finance or payments key, and the "
                "receivables and spend views beside it, open to any finance key. Each block is "
                "computed only for a reader holding the key behind it and is null otherwise, so "
                "nothing about it leaves the server, and each payload carries narrowed. "
                "Document blocks answer under the reader's branches and the school-wide rows, "
                "the rows their lists show: receivables, aging and top overdue on "
                "finance.invoice.view, the trend's issued and collected series on the invoice "
                "and payment view keys, recent journals on finance.journal.view, and vendor "
                "bills and approval counts on their own keys, read exclusively as procurement "
                "reads them. Ledger blocks (cash, receivables, payables, net income) read the "
                "reader's own journals through the branch ledger, so a branch bursar's cards "
                "agree with her own income statement; an invoice reader without "
                "finance.report.view gets her open-invoice total as the receivables card. "
                "Revenue against budget, the period close, bank balances, the per-branch "
                "comparison, cash movement, runway, reconciliation, payroll and tax, including "
                "their items among what needs attention, are the school's money as a whole and "
                "go only to a reader who sees the whole school. The window is this month, this "
                "quarter, the fiscal year to date, or the billing period of the books' owner: "
                "the provider named by FINANCE_BILLING_PERIOD_PROVIDER, which the school layer "
                "answers with the current term and the fee invoices linked to it. Payers are "
                "grouped by the provider named by FINANCE_PAYER_GROUP_PROVIDER, which the "
                "school layer answers with each child's current class. Books with no provider "
                "get calendar windows and no grouping, so finance itself holds no school "
                "concept."),
               ("Acceptance",
                "This term is counted by the fee's term, not by dates: it counts the fees billed "
                "for that term and what was paid against them whenever it arrived, so a parent "
                "who pays First Term fees in the holiday has paid this term's fees; calendar "
                "windows count by date, and the spend view reads every window by date. The "
                "collection curve compares like with like, this window's fees against the "
                "previous comparable window's at the same week, with the school's own target "
                "(term_collection_target_pct, 1 to 100, default 90) as the line both aim at; "
                "the term provider reads archived sessions but never a draft one, so last term "
                "is found for the comparison and next year's calendar never takes over early. "
                "The runway is cash today over the average monthly outflow of the last 90 "
                "days, averaged over the history the books have when that is shorter, and "
                "absent under two weeks. Cash movement sorts each journal by the accounts "
                "opposite its cash lines and leaves out transfers between the school's own "
                "accounts. FinanceDashboardAccessTests and the overview, receivables and spend "
                "test modules (51 tests) prove the block keys, the branch rule and each "
                "computation."),
               ("Current limit",
                "A school-wide budget's plan, used and percentage on the spend view are null for "
                "a branch-bound reader, as on the budgets list, so such a reader sees no budget "
                "progress until their branch keeps a plan of its own (FR-012)."),
           ])


def add_reference_lists(doc) -> None:
    """New FR-023: the lists pickers and filters read open to any finance key."""
    add_fr(doc, "FR-023 Open the Shared Lists to Those Working in Finance",
           "FR-023 | Implemented", [
               ("Requirement",
                "The lists every finance screen builds its pickers and report filters from must "
                "open to anybody working in finance, while balances and creation keep their own "
                "keys, and a list serving two kinds of document must show each reader the kinds "
                "their keys allow."),
               ("Current evidence",
                "GET /entities/ (the set-of-books picker every finance screen opens with), "
                "/fiscal-years/, /periods/, /accounts/ without balances, /cost-centers/, "
                "/dimensions/, /tax-codes/ and /currencies/ answer anyone holding any finance "
                "key, the rule /posting-window/ follows for finance and procurement alike. Each "
                "is a code and a name, or a period's dates and status, for books the caller is "
                "already entitled to; which rows appear is unchanged, the caller's own entity "
                "only. Cost centres also open to procurement.requisition.create, "
                "procurement.requisition.update and procurement.stock.issue, whose forms pick "
                "one. The chart of accounts has three shapes: the plain paginated list behind "
                "the pickers; ?with_tags=true, the whole tree un-paginated with each account's "
                "tag (CONTROL or CASH) and balance null, for a form that must know which account "
                "is a control or cash account; and ?with_balance=true, which alone still needs "
                "finance.account.view and answers a branch-bound reader from their own journals "
                "(FR-019). Creating any of these keeps its create key, and closing, "
                "reopening or locking a period keeps its own. finance.entity.view, "
                "finance.period.view, finance.costcenter.view and finance.dimension.view no "
                "longer gate these reads. GET /ar-adjustments/, the refunds and write-offs "
                "list, opens on finance.refund.view or finance.writeoff.view: refund rows and "
                "the refundable-credit figure go only to a refund-key holder, write-off rows "
                "and the written-off year-to-date figure only to a write-off-key holder, "
                "pending counts the kinds the reader sees, kinds lists them, and refundable "
                "credit counts only customers in the reader's branches."),
               ("Acceptance",
                "Reading these lists follows finance module membership; the chart's balances do "
                "not. FinanceReferenceListAccessTests (10 tests) proves a caller with no finance "
                "key is refused every list, another tenant's books stay unreachable, a report "
                "reader and a budget author read what their screens need, balances still need "
                "the chart-of-accounts key, and creating still needs its own. "
                "AdjustmentListAccessTests proves a reader with neither key is refused, each key "
                "holder sees their own kind only, and a branch holder never sees another "
                "branch's rows."),
               ("Current limit",
                "Any finance key is a weaker gate than a named one, so these lists carry no "
                "amounts; the entity boundary is still enforced in each view, and the dashboards "
                "that open the same way gate each block on its own key (FR-022)."),
           ])


def add_field_access(doc) -> None:
    """New FR-024: the bank account number and the pay figures follow the role's switches."""
    add_fr(doc, "FR-024 Govern Bank and Pay Figures Field by Field",
           "FR-024 | Implemented", [
               ("Requirement",
                "Who reads and who changes a bank account number and a person's pay must be "
                "decided per role, field by field, and a role must never change a figure it "
                "cannot see."),
               ("Current evidence",
                "Finance declares its fields to the platform's field registry, all sensitive "
                "and so closed until a school turns them on: account_number on "
                "finance.bankaccount; employee_name, gross_amount, paye_amount, pension_amount, "
                "net_amount and components on a payroll line (finance.payrollrun); and "
                "gross_amount, paye_amount, pension_amount, net_amount and components on a "
                "salary row (finance.salary). Net pay and the pay breakdown are derived, so they "
                "are read-only. The bank account, payroll line and salary serializers apply the "
                "role's switches through the platform's field enforcement (Module 4): a field "
                "the caller cannot read is absent, with no placeholder; one they can read but "
                "not change is present and named in _read_only_fields; and a submitted field "
                "they cannot write is refused with 403 field_write_denied naming every such "
                "field, before anything is saved. The switches replaced the permission keys "
                "once checked inside these serializers, and the conversion gave each role the "
                "access its keys had given it, with one deliberate difference: because write "
                "implies read, a role that cannot read four payroll-line figures, three salary "
                "figures or the bank account number cannot write them either. An invoice's pay-to block is a declared system surface: it "
                "is addressed to the payer, so the collection account number on it is not "
                "filtered by whoever sends the invoice."),
               ("Acceptance",
                "Reaching the payroll screens is finance.payrollrun.view; seeing what each "
                "person is paid is a second decision the school makes per role. "
                "FinanceDeepPayloadTests renders every declared surface of the three resources "
                "on a real record, as a caller with every field switched off, in a school with "
                "two branches and one with a single branch, and fails on a registered name at "
                "any depth."),
               ("Current limit", "None."),
           ])


# ── lifecycle, data and contracts ────────────────────────────────────────────


def patch_branch_origin(doc) -> None:
    """Section 5.4: where a budget's branch comes from."""
    origin = table_headed(doc, "Situation", "Branch on the new row", "Why")
    anchor = next(r for r in origin.rows
                  if r.cells[0].text.startswith("A fee structure, a bank account"))
    budget = insert_row_after(anchor, [
        "A fee structure that carries a branch.",
        "That branch, stamped when it was raised.",
        "It is that branch's price list: the school's cohort route and the engine's fee "
        "generation both refuse to bill a family filed elsewhere (409 WRONG_BRANCH), while a "
        "school-wide structure bills every branch.",
    ])
    insert_row_after(budget, [
        "A budget.",
        "The branch-bound reader's branch, the branch a school-wide reader names, or none for "
        "the school's plan.",
        "A branch keeps its own plan, measured against its own journals; the school's plan is "
        "read-only to branch staff.",
    ])


def patch_data_model(doc) -> None:
    """Section 6: budgets carry a branch; the primary books have no flag; two new rows."""
    models = table_headed(doc, "Model", "Holds", "Notes")
    ledger = row(models, "LedgerEntity")
    keep_format(
        ledger.cells[-1],
        ledger.cells[-1].text.rstrip()
        + " It has no primary flag: the finance abstraction layer reads a school's one "
          "active tenant-kind entity as its books, with no constraint behind that (FR-016).",
    )
    keep_format(
        row(models, "AccountBalance").cells[-1],
        "Maintained by posting; read by every whole-school report. A branch-bound reader's "
        "statements sum the journal lines of their branches instead (FR-006).",
    )
    budget = row(models, "Budget, BudgetLine")
    keep_format(budget.cells[1], "The school's plan, or one branch's.")
    keep_format(
        budget.cells[-1],
        "Never posts; frozen by approval. Branch nullable, none being the school's plan; a "
        "name is unique per school or per branch within a fiscal year (FR-012).",
    )
    append_rows(models, [
        ["FinanceDocumentSettings.term_collection_target_pct",
         "The share of a billing period's fees the owner aims to collect, 1 to 100, default "
         "90.",
         "Read and written through /settings/documents/; drawn as the collection curve's "
         "target (FR-022)."],
        ["Finance field declarations",
         "The bank account number and the pay figures a role's switches govern.",
         "Declared in vs_finance/field_access.py and written to the registry; all sensitive "
         "(FR-024)."],
    ])


def patch_api(doc) -> None:
    """Section 7: the reads' keys, the dashboard views, budgets and the new refusals."""
    ledger = [t for t in doc.tables
              if t.rows[0].cells[0].text.strip() == "Method and path"
              and any(r.cells[0].text.strip() == "GET, POST /entities/, /accounts/" for r in t.rows)]
    if len(ledger) != 1:
        raise ValueError(f"The ledger table was found {len(ledger)} times")
    ledger = ledger[0]
    keep_format(
        row(ledger, "GET, POST /entities/, /accounts/").cells[-1],
        "Books and the chart of accounts. Reads open on any finance key; ?with_tags=true gives "
        "the tree with each account's tag and no balance, and ?with_balance=true the balances, "
        "which need finance.account.view and come from a branch-bound reader's own journals "
        "(FR-019, FR-023).",
    )
    keep_format(
        row(ledger, "POST /direct-entries/").cells[-1],
        "Raise and post in one CRITICAL action, or, where the journal route has steps, raise "
        "and submit it for approval (FR-003).",
    )
    keep_format(
        row(ledger, "GET /accounts/{id}/activity/").cells[-1],
        "Movement on one account, and in the account drawer its balance, from a branch-bound "
        "reader's own journals (FR-019).",
    )
    keep_format(
        row(ledger, "GET /periods/, /fiscal-years/, /posting-window/").cells[-1],
        "The calendar and what is currently postable, readable on any finance key.",
    )
    keep_format(
        row(ledger, "GET, POST /currencies/, /fx-rates/, /tax-codes/, /cost-centers/, "
                    "/dimensions/").cells[-1],
        "Ledger configuration. Currencies, tax codes, cost centres and dimensions list on any "
        "finance key; each create keeps its key.",
    )

    reports = [t for t in doc.tables
               if t.rows[0].cells[0].text.strip() == "Method and path"
               and any(r.cells[0].text.strip() == "GET /reports/dashboard/" for r in t.rows)]
    if len(reports) != 1:
        raise ValueError(f"The reporting table was found {len(reports)} times")
    reports = reports[0]
    dashboard = row(reports, "GET /reports/dashboard/")
    keep_format(
        dashboard.cells[-1],
        "The finance overview, open to any finance or payments key, with ?window= and "
        "?period=. Adds books, window, windows, reader_first_name, collections, channels, "
        "branches, bank_accounts, budget, top_payers, receivables_summary, attention, "
        "upcoming and payables_due to the KPIs, aging, trend and lists (FR-022).",
    )
    insert_row_after(dashboard, [
        "GET /reports/dashboard/spend/",
        "Cash, spend and compliance, open to any finance key: runway, cash_movement, spend, "
        "spending, reconciliation, unmatched, budgets, payroll, claims, petty_cash, tax_owed, "
        "tax_calendar and assets, every figure by date.",
    ])
    insert_row_after(dashboard, [
        "GET /reports/dashboard/receivables/",
        "Receivables and collections, open to any finance key: collections, days_to_pay, "
        "receivables_summary, credit, curve, plans, groups, dunning, concessions, adjustments "
        "and largest.",
    ])
    keep_format(
        row(reports, "GET /reports/trial-balance/").cells[-1],
        "The cardinal balanced view, from the reader's own journals when narrowed (FR-006).",
    )
    keep_format(
        row(reports, "GET /reports/income-statement/, /balance-sheet/, /cash-flow/, "
                     "/changes-in-equity/").cells[-1],
        "The primary statements, from the reader's own journals when narrowed; a narrowed "
        "income statement has no budget column (FR-006).",
    )
    keep_format(
        row(reports, "GET /reports/statutory-pack/").cells[-1],
        "The statements as one filing pack, for a whole-school reader only.",
    )
    insert_row_after(row(reports, "GET /reports/statutory-pack/"), [
        "GET /reports/ar-aging/, /reports/ar-reconciliation/, /ar-adjustments/",
        "Receivables by age, with CSV, XLSX and PDF export; the sub-ledger against its "
        "control; and the refunds and write-offs list, open on either view key. All narrowed "
        "to the reader's branches (FR-006, FR-023).",
    ])

    ops = [t for t in doc.tables
           if t.rows[0].cells[0].text.strip() == "Method and path"
           and any(r.cells[0].text.strip().startswith("GET, POST /budgets/") for r in t.rows)]
    if len(ops) != 1:
        raise ValueError(f"The operations table was found {len(ops)} times")
    ops = ops[0]
    budgets = next(r for r in ops.rows if r.cells[0].text.strip().startswith("GET, POST /budgets/"))
    keep_format(
        budgets.cells[-1],
        "Plan, freeze, and compare. Each listed plan carries branch_id, branch_name and "
        "can_manage, and the list says in filing which plans the reader may create; POST takes "
        "an optional branch (FR-012).",
    )
    append_rows(ops, [
        ["GET, PATCH /v1/i/me/settings/payroll-scope/",
         "The school's own choice of central or per-branch payroll, under school.settings.view "
         "and school.settings.update. Served outside /v1/finance/, by the school records "
         "module, and guarded by finance (FR-021)."],
    ])

    errors = table_headed(doc, "Condition", "Answer")
    keep_format(
        row(errors, "Switching a school to per-branch payroll while an active employee has no "
                    "branch").cells[-1],
        "400 INVALID_CONFIGURATION_VALUE keyed on scope, naming up to ten of the staff still "
        "unassigned.",
    )
    append_rows(errors, [
        ["A bank account a posting names that is unknown, another entity's, or outside the "
         "caller's branches", "404, identical for all three."],
        ["A ledger account behind a bank account outside the caller's branches, named on any "
         "write", "400 \"No account '<ref>' in this entity.\", as for an unknown account; 404 "
         "on the account's own edit route."],
        ["Paying a branch's document from another branch's bank account",
         "400 naming the branch: \"This refund belongs to Ikeja Branch. Pay it from an Ikeja "
         "Branch account or a school-wide one.\""],
        ["Posting a journal or expense claim, or raising a direct entry, against an empty "
         "approval route without confirm_without_approval",
         "409 APPROVAL_NOT_CONFIGURED; nothing is written. Confirmed, it posts and is recorded "
         "as made without approval."],
        ["Posting a journal or expense claim whose route has steps",
         "400, sending the caller to submit."],
        ["Raising a direct entry where the journal route has steps",
         "201, and the entry waits for approval."],
        ["A branch-bound reader asking for the statutory pack",
         "403, saying the pack covers the whole school."],
        ["A budget name the same school or branch already uses in that fiscal year",
         "422 BUDGET_ERROR, naming whose name it is."],
        ["Opening another branch's budget", "404, identical to an unknown id."],
        ["A branch-bound reader changing the school's budget", "403 SHARED_RECORD_READ_ONLY."],
        ["Submitting a bank account number or pay figure the caller's role may not change",
         "403 field_write_denied, naming every such field; nothing is saved."],
        ["Naming a customer or invoice outside the caller's branches on a fee run or an "
         "adjustment batch line", "404, identical to an unknown code."],
        ["A branch fee structure billing a customer filed under any other branch, school-wide "
         "included", "409 WRONG_BRANCH."],
        ["A refund or write-off batch posted against an empty approval route without "
         "confirm_without_approval", "Refused as the single post is, before any document posts; "
         "a confirmed batch records one unapproved post per document."],
        ["A finance or procurement read for a school holding two active sets of books",
         "A server error, and one system-health incident for that school (FR-016)."],
    ])


def patch_dependencies(doc) -> None:
    """Section 8: the school layer's providers, the empty routes, the settings door, health."""
    deps = table_headed(doc, "Dependency", "Contract")
    keep_format(
        row(deps, "Finance abstraction layer").cells[-1],
        "The school layer's only route into these books. It provisions a school's entity "
        "through provision_books, reads the receivable position for the dashboards and "
        "portals, and evaluates finance permission keys in the school that owns the entity. It "
        "answers two questions finance cannot: the billing period, the current term and the fee "
        "invoices linked to it (FINANCE_BILLING_PERIOD_PROVIDER), and the payer grouping, each "
        "child's current class (FINANCE_PAYER_GROUP_PROVIDER). A school raises its fee invoices "
        "through the layer's cohort billing route; the engine's own generate still serves the "
        "console (Module 17). It holds no ledger logic and posts nothing itself.",
    )
    append_to(
        deps, "Module 4, Roles & Permissions",
        " The fields a role reads and writes on bank accounts, payroll lines and salary rows "
        "are decided by the role's field switches (FR-024).",
    )
    append_to(
        deps, "Module 7, Workflow & Approval Engine",
        " A tenant's books arrive with an empty route per finance document type and no "
        "approver groups, and the tenant publishes its own steps through the generic workflow "
        "surface (FR-018). Every finance document with a route and a direct post now asks "
        "the empty-route question on that post, journals, expense claims and direct entries "
        "among them (FR-003, FR-008).",
    )
    append_to(
        deps, "Module 6, Configuration",
        " A school reaches the setting through its own settings door, "
        "/v1/i/me/settings/payroll-scope/, under school.settings.view and .update (FR-021).",
    )
    append_to(
        deps, "Seeding",
        " seed_finance_approvals publishes the default ladders for a tenant that asks for "
        "them. seed_finance_dashboard_demo, seed_finance_receivables_demo and "
        "seed_finance_spend_demo fill a development school's books with demo activity and "
        "refuse to run outside DEBUG.",
    )
    insert_row_after(row(deps, "Module 6, Configuration"), [
        "Module 30, System Health",
        "Receives the one configuration-fault incident the finance abstraction layer opens "
        "for a school holding two active sets of books (FR-016).",
    ])


# ── needs attention and traceability ─────────────────────────────────────────


def patch_needs_attention(doc) -> None:
    """Section 9: the statements gap is closed; the bank account picker leads what is open."""
    # The statements gap v1.11 led with is closed, and so is the bank-account gap that
    # followed it, so the box leaves with nothing to replace it.
    gap = [t for t in doc.tables
           if t.rows[0].cells[0].text.strip().startswith("CURRENT GAP - THE FINANCIAL")]
    if len(gap) != 1:
        raise ValueError(f"The statements gap box was found {len(gap)} times")
    following = gap[0]._tbl.getnext()
    gap[0]._tbl.getparent().remove(gap[0]._tbl)
    if (following is not None and following.tag.endswith("}p")
            and not "".join(following.itertext()).strip()):
        following.getparent().remove(following)
    gaps = [t for t in doc.tables if t.rows[0].cells[0].text.strip().startswith("FURTHER GAPS")]
    if len(gaps) != 1:
        raise ValueError(f"FURTHER GAPS heads {len(gaps)} tables")
    edit_box(gaps[0].rows[0].cells[0], [
        ("sub", "• School domain integration",
         ", which FR-019 to FR-021 record and whose one open gap is stated above.",
         ", which FR-019 to FR-021 record."),
        ("append",
         "• A school holding two active sets of books is reported to the console as one "
         "incident, but nobody is notified, and the incident stays open after the school is "
         "fixed until an operator resolves it (FR-016)."),
        ("append",
         "• The export builder in the shared finance package still shows its Books step when a "
         "school has a single set of books, a choice of one, where the finance screens' own "
         "switcher is hidden in that case. That is frontend package work."),
    ])


def patch_traceability(doc) -> None:
    """Section 10: retraced against MRD v2.94; the new requirements map to existing entries."""
    edit_paragraph(doc, "Module 19 carries 26 capability entries", "MRD v2.76",
                   f"MRD v{MRD_VERSION}")
    trace = table_headed(doc, "MRD capability", "Requirements")
    for label, value in (
        ("Finance entities and books", "FR-001, FR-016, FR-019, FR-020, FR-023"),
        ("Chart of accounts", "FR-001, FR-023"),
        ("Fiscal years and periods", "FR-002, FR-005, FR-023"),
        ("Trial balance and financial statements", "FR-006, FR-019"),
        ("Finance dashboard and summaries", "FR-022, FR-019"),
        ("Bank accounts and reconciliation", "FR-007, FR-019, FR-020, FR-024"),
        ("Cost centers and dimensions", "FR-001, FR-016, FR-023"),
        ("Payroll processing records", "FR-010, FR-019, FR-021, FR-024"),
        ("Budgets and budget controls", "FR-012, FR-019"),
        ("One primary set of books per tenant", "FR-016, FR-018"),
    ):
        keep_format(row(trace, label).cells[-1], value)
    closing = [p for p in doc.paragraphs if p.text.strip().startswith("All 26 entries are traced")]
    if len(closing) != 1:
        raise ValueError(f"The traceability closing note was found {len(closing)} times")
    mrd_tools.retitle(
        closing[0],
        "All 26 entries are traced above. Module number, name, phase, backend and in-use "
        f"states and code ownership agree with MRD v{MRD_VERSION}, and so does the branch "
        "rule: its Module 19 note records a branch-bound reader's statements, chart balances "
        "and account activity built from their branches' journals and the school-wide "
        "entries (FR-006), and the bank accounts and bank ledgers a posting names kept within "
        "the caller's branches (FR-019), and its P2 gap on branch financial statements is "
        "closed, so no branch gap is left open for Module 19.",
    )


def drop_closed_direct_post_gap(doc) -> None:
    """Section 9: journals and expense claims now confirm an empty route (FR-003, FR-008)."""
    gaps = [t for t in doc.tables if t.rows[0].cells[0].text.strip().startswith("FURTHER GAPS")]
    if len(gaps) != 1:
        raise ValueError(f"FURTHER GAPS heads {len(gaps)} tables")
    cell = gaps[0].rows[0].cells[0]
    lines = [line.strip() for p in cell.paragraphs for line in p.text.split("\n") if line.strip()]
    kept = [line for line in lines
            if not line.startswith("• The direct-post routes for journals and expense claims")]
    if len(kept) != len(lines) - 1:
        raise ValueError("The direct-post gap is not exactly one line of FURTHER GAPS")
    if len(cell.paragraphs) == 1 and len(cell.paragraphs[0].runs) == 1:
        cell.paragraphs[0].runs[0].text = "\n".join(kept)
    else:
        mrd_tools.set_lines(cell, kept)


def patch_finance_pass(doc) -> None:
    """FR-003, FR-008, FR-019, FR-020 and FR-022: the finance pass merged at 32194627."""
    fr003 = fr_table(doc, "FR-003")
    edit_value(fr003, "Current evidence",
               "Journals carry a submit, post, and reverse endpoint; direct entries post in "
               "one step behind a CRITICAL key.",
               "Journals carry a submit, post, and reverse endpoint. Posting a journal "
               "directly answers to the school's journal route: a route with steps refuses "
               "with 400 and sends the caller to submit; an empty route answers 409 "
               "APPROVAL_NOT_CONFIGURED and writes nothing until the caller sends "
               "confirm_without_approval, and the post is then recorded as made without "
               "approval against her name and reason; no route at all posts as before. A "
               "direct entry, raised and posted in one action behind the CRITICAL "
               "finance.directentry.post, follows the same route: where the journal route has "
               "steps the entry is created and submitted into it, 201 with \"Direct entry "
               "<number> is waiting for approval. It reaches the books once it is approved.\" "
               "and the approval block the journal submit returns; an empty route answers 409 "
               "as above; no route posts it directly. Each direct-entry line names its ledger "
               "account under the caller's reach (FR-019).")
    append_to(fr003, "Acceptance",
              " JournalDirectPostTests and DirectEntryApprovalRouteTests in "
              "tests_direct_post_confirmation.py prove an empty route refuses and writes "
              "nothing until confirmed, a confirmed post goes through recorded against the "
              "confirmer, a route with steps sends the journal to submit and holds the direct "
              "entry for approval, and no route posts without asking.")
    fr008 = fr_table(doc, "FR-008")
    append_to(fr008, "Current evidence",
              " Posting a claim directly asks the same question as every finance document: a "
              "route with steps refuses with 400 and sends the claimant's approver to submit, "
              "and the empty route every school's books arrive holding answers 409 "
              "APPROVAL_NOT_CONFIGURED until somebody confirms, then posts and records the "
              "post as made without approval. Reimbursement is paid from a bank account in the "
              "caller's reach and, for a claim that belongs to a branch, from that branch's "
              "account or a school-wide one (FR-020).")
    append_to(fr008, "Acceptance",
              " ExpenseClaimDirectPostTests proves the starting route refuses until confirmed "
              "and writes nothing, a confirmed post is recorded against the confirmer, and a "
              "route with steps sends the claim to submit.")

    fr019 = fr_table(doc, "FR-019")
    append_to(fr019, "Current evidence",
              " The bank account a movement is paid from or into is held to the same rule on "
              "every money route: refunds singly and in batches, expense-claim reimbursement, "
              "tax payment, fixed-asset purchase and disposal, petty-cash float and top-up, "
              "payroll runs and their payment, and vendor payments, which go through the same "
              "resolver. The account named in the body resolves among the caller's branches' "
              "accounts and the school-wide ones, the bank list's own rule, so another "
              "branch's account answers 404 exactly as an unknown one does, and an unknown "
              "account answers 404 rather than 400 everywhere. A ledger account that backs a "
              "bank account is that bank's money under another code, so it answers to the same "
              "list wherever a write names it (accounts_a_caller_may_name): a receipt's deposit "
              "account, an asset's credit account, a bank adjustment's counter_code, a direct "
              "entry's line, a payout's source and a collection's deposit account (Module 18), "
              "a vendor's accounts and every procurement account field, a school default and a "
              "new account's parent. Another branch's bank ledger is refused as an unknown "
              "account is, 400 \"No account '<ref>' in this entity.\", or 404 on the account's "
              "own edit route. A ledger account behind no bank and a caller bound to no branch "
              "are untouched, and reads are not narrowed by it: a report or list filtered by "
              "account already returns the reader's own rows, and the chart lists every "
              "account.")
    append_to(fr019, "Acceptance",
              " BankAccountNamedInAPostingTests in tests_bank_account_reach.py proves each "
              "money route refuses another branch's account, by id and by name, with the 404 of "
              "an unknown one; ResolverTests and LedgerNamedOnAWriteTests in "
              "tests_ledger_reach.py prove another branch's bank ledger is unknown by code and "
              "by id on a receipt, a direct-entry line, an asset, a bank adjustment, a payout, a "
              "vendor, a school default and the account's own edit, while a caller bound to no "
              "branch names any.")
    set_status(doc, fr019, "Implemented")

    fr020 = fr_table(doc, "FR-020")
    append_to(fr020, "Current evidence",
              " A document that belongs to a branch is paid only from that branch's bank "
              "account or a school-wide one, because an Ikeja refund paid from Lekki's account "
              "leaves Ikeja owing and Lekki short even for a caller who covers both. Another "
              "branch's account is refused 400 naming the branch: \"This refund belongs to "
              "Ikeja Branch. Pay it from an Ikeja Branch account or a school-wide one.\" The "
              "rule applies to a refund and to each line of a refund batch, expense-claim "
              "reimbursement, payroll runs and their payment, petty-cash float and top-up, "
              "asset purchase and disposal, and vendor payments, checked once the bills a "
              "payment settles fix its branch, and on edit. A school-wide document may use any "
              "account in reach. A tax filing is created without a branch and so pays from any "
              "reachable account. Bank accounts, expense claims, petty-cash funds and fixed "
              "assets return branch_id, null for school-wide, and each refund-availability row "
              "returns its customer's branch_id, which the refund inherits, so a screen offers "
              "a document only the accounts it may be paid from.")
    append_to(fr020, "Acceptance",
              " DocumentPaidFromItsOwnBranchTests proves a caller covering two branches is "
              "refused another branch's account on each route with the sentence naming the "
              "branch, and that a school-wide refund may use any account she reaches; "
              "BranchOnThePickersTests proves the accounts and documents carry their branch.")

    fr022 = fr_table(doc, "FR-022")
    append_to(fr022, "Current evidence",
              " A block also needs the school's plan to reach its key: the reader's keys are "
              "passed through the plan gate's keys_within_plan (Module 6), so a block whose key "
              "sits above the school's band is absent, as the screen behind it would refuse it, "
              "while the plan gate switched off, a school with no plan and a platform super "
              "administrator are unaffected. Approvals waiting on the reader count every "
              "finance and payments workflow type, credit and debit notes among them under one "
              "label.")
    append_to(fr022, "Acceptance",
              " FinanceDashboardPlanTests proves recent journals are absent below their band "
              "and present at it; ApprovalsWaitingOnYouTests proves a credit note at the "
              "reader's stage is counted and that the counted types cover every registered "
              "finance and payments workflow type.")


def patch_bank_ledger_branch(doc) -> None:
    """FR-020 and Section 7: a branch's document names its own branch's bank ledger (D62)."""
    fr020 = fr_table(doc, "FR-020")
    append_to(fr020, "Current evidence",
              " The rule holds for the ledger account behind a bank account as it does for the "
              "bank account, because naming Lekki's bank ledger code moves Lekki's money exactly "
              "as choosing Lekki's account does. Where a branch's document names the ledger "
              "account it pays from, deposits into or books against, the account resolver takes "
              "the document's branch, and a ledger account behind a bank account must be that "
              "branch's or a school-wide one. Another branch's is refused with the bank rule's "
              "own 400, worded for the document's side: \"Pay it from\" on an asset purchase's "
              "credit_account; \"Deposit it into\" on a customer receipt's and an invoice "
              "payment's deposit_account, and on a Module 18 payment request's and virtual "
              "account's; and \"Book it against\" on a bank adjustment's counter_account or "
              "counter_code, as in \"This bank adjustment belongs to Ikeja Branch. Book it "
              "against an Ikeja Branch account or a school-wide one.\" Each route finds the "
              "branch where the money belongs: an asset purchase from the asset; a customer "
              "receipt from the customer and an invoice payment from the invoice, worked out "
              "before the account is resolved; a bank adjustment from its statement's bank "
              "account; and a payment request or virtual account from the customer it names. "
              "Payouts and payout batches pay through a vendor payment that carries no branch, so "
              "they are school-wide and name any account in reach. A ledger account behind no "
              "bank account, and a school-wide document, are unaffected.")
    append_to(fr020, "Acceptance",
              " LedgerOfAnotherBranchOnABranchDocumentTests in tests_ledger_reach.py proves Mrs "
              "Okafor, who covers Ikeja and Lekki, is refused Lekki's bank ledger on an Ikeja "
              "asset purchase, customer receipt, invoice payment, bank adjustment, payment "
              "request and virtual account, with the sentence naming the branch, while Ikeja's "
              "and the school-wide bank ledgers are accepted; a ledger behind no bank account is "
              "unaffected, and a school-wide customer's receipt may land in any bank she "
              "reaches.")

    errors = table_headed(doc, "Condition", "Answer")
    insert_row_after(row(errors, "Paying a branch's document from another branch's bank account"), [
        "Naming the ledger account behind another branch's bank account on a branch's document",
        "The same 400 naming the branch, worded for the document's side: \"Pay it from\" on an "
        "asset purchase, \"Deposit it into\" on a receipt, invoice payment, payment request or "
        "virtual account, \"Book it against\" on a bank adjustment. Nothing is written.",
    ])


def patch_statement_rollback(doc) -> None:
    """FR-007 and around it: a bursar rolls back her own statement import from its row."""
    scope = table_headed(doc, "Area", "Responsibility")
    edit_value(scope, "Banking", "statement import, matching,",
               "statement import and its rollback, matching,")
    actors = table_headed(doc, "Actor", "May do", "Governed by")
    edit_cell(row(actors, "Bank reconciler").cells[1], "Import statements, match,",
              "Import statements, roll back a statement import nothing has been done with, "
              "match,")
    fr007 = fr_table(doc, "FR-007")
    append_to(fr007, "Current evidence", (
        " A bulk-imported statement is never edited line by line, because its lines must stay "
        "the ones the file carried: it is corrected by rolling back the import that published "
        "it and importing the file again, and finance's rollback refuses once any line has "
        "been matched, ignored or adjusted. The bursar who imported it does this herself: "
        "finance.bankaccount.import, the key that took the file through the wizard, also "
        "rolls back a bank-statement batch, and nothing else past the wizard (Module 10). "
        "Each statement in a bank account's list (GET /bank-accounts/{id}/) and the "
        "statement's own read carry import_rollback, the batch_id and job_id of the import "
        "that published it, or null for a statement keyed in by hand, a reconciled one, one "
        "with a line already acted on, which the rollback would refuse, or one whose rollback "
        "has started and not finished, which the rollback route refuses as already running. "
        "The rollback deletes the statement and its lines in one step, so it finishes inside "
        "the request however many lines the statement has, and the bursar is answered once the "
        "statement is gone. The list reads it "
        "for every statement through two subqueries (annotate_statement_rollback) rather than "
        "one lookup per row, and the rollback route still checks the lines and the caller's "
        "key itself. The finance banking screen offers Roll back on the statement's own row "
        "from that field, and the school app's import wizard offers it to the statement key's "
        "holder; both are committed and pending release (FinPro c3cac8b, school-fe a855385)."
    ))
    append_to(fr007, "Acceptance", (
        " test_a_bulk_statement_names_the_import_that_rolls_it_back proves the list and the "
        "detail name the same batch and job, and that rolling that job back takes the "
        "statement off the list; test_a_statement_keyed_in_by_hand_names_no_import and "
        "test_a_bulk_statement_with_an_acted_on_line_names_no_import prove the null, and "
        "test_a_statement_being_rolled_back_names_no_import that a rollback under way offers "
        "none; test_a_long_statement_rolls_back_in_the_request proves a sixty-line statement "
        "is gone when the rollback answers. "
        "BankKeyRollsBackItsStatementTests proves a holder of finance.bankaccount.import alone "
        "rolls a published statement back, lines and all, and is refused once a line has been "
        "ignored; BankKeyStopsAtTheRollbackTests that the same key deletes no batch, reads no "
        "rollback history or feed, and rolls back no students batch."
    ))
    banking = [r for t in doc.tables for r in t.rows
               if r.cells[0].text.strip() == "GET, POST /bank-accounts/ and its statement routes"]
    if len(banking) != 1:
        raise ValueError(f"The bank account route row was found {len(banking)} times")
    cell = banking[0].cells[-1]
    keep_format(cell, cell.text.rstrip() + (
        " An account's read lists up to fifty of its statements, each with import_rollback: "
        "{batch_id, job_id} of the import that rolls it back, or null (FR-007). The rollback "
        "itself is Module 10's POST /v1/import/batches/{id}/jobs/{id}/rollback/, open to "
        "finance.bankaccount.import on a statement batch."
    ))
    deps = table_headed(doc, "Dependency", "Contract")
    append_to(deps, "Module 10, Import & Data", (
        " Its rollback route is how a bulk-imported statement is corrected, and it admits "
        "finance.bankaccount.import on a statement batch; finance's own rollback decides "
        "whether the statement may go."
    ))


def keep_needs_attention_heading_with_its_first_box(doc) -> None:
    """Section 9's heading and lead line travel with the first box below them.

    Without it the heading and its one-line lead can end a page on their own
    while every box they introduce starts the next.
    """
    heading = [p for p in doc.paragraphs if p.text.strip() == "9. Needs Attention"]
    lead = [p for p in doc.paragraphs
            if p.text.strip() == "These are current risks and gaps, not history."]
    if len(heading) != 1 or len(lead) != 1:
        raise ValueError("Section 9's heading or lead line was not found exactly once")
    for paragraph in (heading[0], lead[0]):
        paragraph.paragraph_format.keep_with_next = True
    # Section 7's lead line travels with 7.1 and its table, never ending a page alone.
    api_lead = [p for p in doc.paragraphs
                if p.text.strip().startswith("All routes are mounted under /v1/finance/")]
    if len(api_lead) != 1:
        raise ValueError("Section 7's lead line was not found exactly once")
    api_lead[0].paragraph_format.keep_with_next = True


def keep_requirement_rows_whole(doc) -> None:
    """No row of a requirement, the change log or any other table breaks across a page.

    A requirement's status bar and Requirement row also keep with the evidence
    below them, so a page never ends on a requirement's heading and its first
    line alone.
    """
    for table in doc.tables:
        if table.rows[0].cells[0].text.strip().startswith("FR-"):
            keep_rows_whole(table)
            for table_row in table.rows[:2]:
                for cell in table_row.cells:
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.keep_with_next = True
    keep_rows_whole(change_log_table(doc))
    for table in doc.tables:
        if len(table.rows[0].cells) > 1:
            keep_rows_whole(table)


def keep_last_rows_together(doc) -> None:
    """A requirement's or the dependency table's last row never stands alone on a page.

    A requirement's second-last row keeps with its last, so its limit travels
    with its acceptance, and a short requirement moves whole; the dependency
    table keeps its last three rows together.
    """
    for table in doc.tables:
        first = table.rows[0].cells[0].text.strip()
        if first.startswith("FR-"):
            short = sum(len(c.text) for r in table.rows for c in r.cells) < 1500
            tail = table.rows[:-1] if short else table.rows[-2:-1]
        elif first == "Dependency":
            tail = table.rows[-3:-1]
        else:
            continue
        for table_row in tail:
            for cell in table_row.cells:
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.keep_with_next = True


def patch_m19() -> None:
    require_newest(str(ROOT / "functional-requirements" / M19_DIR / f"{M19_STEM}_v*.docx"),
                   M19_SOURCE)
    doc = Document(str(frd_path(M19_DIR, M19_STEM, M19_SOURCE)))
    patch_control(doc)
    patch_scope(doc)
    patch_branch_context(doc)
    patch_actors(doc)
    patch_reports(doc)
    patch_budgets(doc)
    patch_entity_scope(doc)
    patch_provisioning(doc)
    patch_branch_reads(doc)
    patch_branch_writes(doc)
    patch_payroll(doc)
    add_dashboard(doc)
    add_reference_lists(doc)
    add_field_access(doc)
    patch_branch_origin(doc)
    patch_data_model(doc)
    patch_api(doc)
    patch_dependencies(doc)
    patch_statement_rollback(doc)
    patch_finance_pass(doc)
    patch_bank_ledger_branch(doc)
    patch_needs_attention(doc)
    drop_closed_direct_post_gap(doc)
    patch_traceability(doc)

    log_change(doc, M19_TARGET, M19_SUMMARY)
    assert_absent_outside_log(
        doc,
        "Version: 1.11",
        "MRD v2.76",
        "none of these statements is narrowed",
        "still aggregate ledger lines across branches",
        "whose stages name approver groups the same call creates empty",
        "THE FINANCIAL STATEMENTS ARE NOT NARROWED",
        "AN ACCOUNT'S BALANCE AND ACTIVITY",
        "all_active bills every active customer",
        "PICKING A BANK ACCOUNT",
        "direct entries post in one step",
        "The direct-post routes for journals and expense claims",
        "The finance landing aggregate.",
        "the MRD is behind",
        "MRD v2.93",
    )
    keep_requirement_rows_whole(doc)
    keep_last_rows_together(doc)
    keep_needs_attention_heading_with_its_first_box(doc)
    # The traceability note stays whole and on the page of the table's last rows.
    note = [p for p in doc.paragraphs if p.text.strip().startswith("All 26 entries are traced")]
    if len(note) != 1:
        raise ValueError("The traceability note was not found exactly once")
    note[0].paragraph_format.keep_together = True
    for table_row in table_headed(doc, "MRD capability", "Requirements").rows[-2:]:
        for cell in table_row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next = True
    repair_ooxml(doc)
    normalise_change_log(doc)
    finish(doc, frd_path(M19_DIR, M19_STEM, M19_TARGET),
           f"{M19_STEM.replace('_', ' ')} v{M19_TARGET}", M19_TARGET)


def main() -> None:
    patch_m19()


if __name__ == "__main__":
    main()
