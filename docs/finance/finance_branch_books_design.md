# finance_branch_books_design

**Status:** design, partly built. Every decision below is agreed. Inter-branch
transfers (section 4), money held for another branch (section 5), recharges
(section 8), the receivable transfer of a pupil move (section 9, the finance
service) and goods transfers between branches' stores (section 10) are built;
section 15 says where.

Every tenant keeps its books by branch. There is no mode to choose and nothing
that is "school-wide" in the books: every transaction belongs to a real branch,
and every tenant has at least one. A tenant with one branch keeps exactly the
same books, and the branch dimension recedes on its screens because there is
only one answer to "which branch".

The tenant keeps **one** `LedgerEntity`: one chart of accounts, one tax number,
one numbering series, one set of fiscal years. Branches are never separate legal
companies, so there is no multi-entity consolidation and no elimination step.
CodeX's own books follow the same rule, with one branch named after its city
(for example "Lagos").

---

## 1. Decisions

**The model**

1. **Every transaction names a real branch**: every invoice, receipt, bill,
   payment, journal, payroll journal, budget, bank record and petty cash fund.
   A blank branch on a transaction is refused.
2. No branch is called "Head Office" in the system. A tenant that runs a central
   office names it as the branch it is.
3. **All branches is a view, not a set of books.** It has no bank account and no
   records of its own. It shows everything every branch did, added up.
4. **No branch pays for another branch's goods or services.** When a branch is
   short, another branch lends to it through an **inter-branch transfer**, and the
   books record who owes whom until it is repaid.
5. A bursar sees only their own branches' records and the transfers their branches
   are party to. An Ikeja-only bursar cannot see or pay from an account with no
   branch, because no such account exists. A caller who reaches every branch sees
   all of them.
6. **There is no central collecting account and no sweep.** Money leaves a branch
   only when that branch spends it or sends it by transfer.
7. **Lists that are not transactions may be shared** across branches: customers,
   suppliers, fee structures, catalogue items and the chart of accounts.
   Configuration such as role grants and workflow templates keeps a blank branch
   meaning "all branches".

**Banks and money in**

8. One physical bank account used by two branches becomes one bank record and one
   ledger account per branch.
9. Every branch has its own collection account and its own payment-provider
   sub-account, so fees paid online or by transfer land in the pupil's own branch.
10. Money that still lands in the wrong branch is recorded as **held for** the
    right branch and forwarded by transfer. The pupil's invoice is paid when the
    money reaches their branch.

**Closing, tax and payroll**

11. Each branch closes its own month. A transfer needs both branches open on its
    date. All branches closes the tenant's period only after every branch has
    closed.
12. VAT, withholding tax and the year-end close are worked out per branch and
    filed or closed together, as one tenant (one tax number).
13. Payroll stays one run for the tenant, and posts one journal per branch, each
    paid from that branch's own bank. Each staff member is paid in full by one
    branch.

**Moving balances between branches**

14. When a pupil moves branch, their unpaid balance moves with them: the new branch
    takes over the receivable and owes the old branch that amount. Their unpaid
    invoices move to the new branch's lists; the revenue stays with the old branch.
15. A supplier advance settles only bills of its own branch.
16. A cost one branch pays on behalf of others (the audit fee) is either absorbed by
    that branch or **recharged** to the others by pupil numbers or fixed
    percentages, whichever the tenant chooses for that cost.

**Buying and stock**

17. Buying together means one negotiated price and one order per branch at that
    price. Each branch receives and pays for its own goods.
18. A central store belongs to one branch. Other branches requisition from it, and
    the goods issued to them are a goods transfer: the receiving branch owes their
    cost.
19. Every budget belongs to a branch.

**Working in it**

20. The entity selector also chooses **All branches** or one branch. It is hidden
    whenever the caller has only one option.
21. A new transaction under All branches must name its branch. At a one-branch
    tenant it takes the only branch; at a multi-branch tenant a caller who names
    none is refused.

---

## 2. How a month runs (worked example)

Bright Star School runs Ikeja, Lekki and Abuja. Ikeja is the main branch.

| Event | What is recorded | Effect |
| --- | --- | --- |
| Lekki collects 12m of fees, online and by transfer | Lekki: Dr Bank, Cr Receivable | Lekki only. The provider sub-account settled into Lekki's bank. |
| Lekki is short for diesel and asks Ikeja for 1m | Transfer. Ikeja: Dr Owed by Lekki, Cr Bank. Lekki: Dr Bank, Cr Owed to Ikeja | Lekki owes Ikeja 1m. |
| Lekki pays its diesel supplier 800k | Lekki: Dr Diesel, Cr Bank | Lekki's own cost, from Lekki's own bank. |
| Mrs Adeyemi pays 400k of Lekki fees into Ikeja's account by mistake | Ikeja: Dr Bank, Cr Held for Lekki. Then a transfer forwards it | Lekki's invoice is paid when the 400k reaches Lekki. |
| Ikeja pays the 3m audit fee and recharges it by pupil numbers | Ikeja: Dr Audit fee, Cr Bank; then a recharge | Lekki owes Ikeja 900k, and so on. |
| Payroll runs once for the school | One journal per branch, each from its own bank | Each branch carries its own staff cost. |
| Month end: VAT worked out per branch | Each branch's VAT on its own books; one return filed for the school | The tax office sees one filing. |
| 5 Oct: Lekki closes September | Lekki's September closes | Abuja keeps posting. |
| 9 Oct: Abuja, the last branch, closes | All branches closes September | The tenant's September is closed. |

The All branches view shows every bank account, every branch's figures and every
transfer. Its "owed between branches" total is zero, because every transfer is
recorded on both sides.

---

## 3. The one-branch rule

1. Every transaction and every journal entry has a branch. A blank branch is
   refused.
2. Every line of an entry belongs to the entry's branch. A bank account or petty
   cash fund used on a document must belong to the document's branch, so Ikeja's
   bank cannot pay a Lekki bill.
3. A receipt or payment may only settle documents of its own branch. A chain whose
   sources come from two branches is refused
   (`vs_rbac.scoping.inherited_branch_id`).
4. The only records that touch two branches are transfers (section 4). Each
   transfer posts **two** entries, one per branch, linked to each other, so the
   rule "one entry, one branch" never has an exception.
5. A new transaction takes its branch through `vs_rbac.scoping.raised_branch`: a
   caller bound to one branch files at it; at a one-branch tenant every caller
   files at the only branch; at a multi-branch tenant a caller who covers several
   branches must name one.

Because no entry mixes branches, branch statements need no new plumbing:
`vs_finance.branch_ledger` builds them from the entry's branch, and they are
complete statements, not slices.

---

## 4. Inter-branch transfers

A transfer is one document with a sending and a receiving branch. It comes in
kinds, which share one balance between each pair of branches:

| Kind | What moves | Example |
| --- | --- | --- |
| **Cash** | Money, bank to bank | Ikeja lends Lekki 1m for diesel; Lekki repays later. |
| **Forwarded receipt** | Money that arrived at the wrong branch | Ikeja forwards Mrs Adeyemi's 400k to Lekki. |
| **Receivable** | A pupil's unpaid balance | Tunde moves from Ikeja to Lekki owing 150k. |
| **Recharge** | A share of a cost one branch paid for others | Ikeja bills Lekki 900k of the audit fee. |
| **Goods** | Stock, at cost | Ikeja's central store issues Lekki 300 textbooks. |

Only a cash transfer (and a forwarded receipt) moves money between banks. The
others move a balance and leave cash where it is; the owing branch settles later
with a cash transfer.

**Cash transfer steps**

1. The receiving branch may request an amount, or the sending branch may send
   without a request.
2. The sending branch approves and sends it from one of its bank accounts to one of
   the receiving branch's bank accounts. Approval is maker-checker, as a payout is.
3. The receiving branch confirms arrival, and both sides are matched in bank
   reconciliation.

**Balances.** Each pair of branches keeps one running balance ("Lekki owes Ikeja
1m"). A transfer the other way reduces it. There is no interest: it is one
company, and interest would only move profit between branches for show. A transfer
may carry an optional "repay by" date.

**Books.** One well-known account, "Inter-branch balances" (a
`FinanceAccountMapping` key), carries every transfer. Each line records its
counterparty branch, which is what the pair balances and the register read.
Across all branches the account always nets to zero.

**Visibility.** A transfer is visible to anyone whose reach includes either of its
two branches: "sending branch in reach **or** receiving branch in reach".

**Built.** `vs_finance.inter_branch` and `InterBranchTransfer` (with one
`InterBranchTransferLeg` per branch, each holding that branch's journal). The
inter-branch account is the `INTER_BRANCH` mapping (starter code 1260
Inter-branch Balances), locked against hand-typed journals; its counterparty is a
column on the journal line (`JournalLine.counterparty_branch`), so the pair
balances are one query over the ledger and a reversal carries it with the line.
A cash transfer is requested by the receiving branch or sent unprompted, sent
through the sending branch's `finance.inter_branch_transfer` approval route, and
confirmed by the receiving branch; both journals post on the transfer date, and
each reaches its own bank's reconciliation. It is voided (both sides reversed)
by somebody who works in both branches, while neither bank side is matched. The
both-branches-open check is `ensure_branches_open`, which reads the tenant's
period today and is where a per-branch close will answer. At a tenant with one
branch every write refuses ("there is only one branch") and nothing touches the
account.

---

## 5. Money in

- Every branch has a collection bank account and a payment-provider sub-account.
  Invoices print the pupil's own branch's account as "pay to".
- Money arriving at the wrong branch is receipted there against a "Held for other
  branches" liability, naming the branch it belongs to. The receiving branch
  cannot apply it to another branch's invoice. A forwarded-receipt transfer moves
  it, and the invoice is settled on arrival.
- **Built.** `HeldForBranchReceipt` (`/finance/held-receipts/`) books
  `Dr bank, Cr held for other branches [Lekki]` (mapping
  `HELD_FOR_OTHER_BRANCHES`, starter code 2190). Forwarding it is a
  `FORWARDED_RECEIPT` transfer on the same approval route as cash: Ikeja books
  `Dr held for other branches [Lekki], Cr bank`, and Lekki receives an ordinary
  receipt against the customer in its own bank, which settles their Lekki
  invoices oldest first. The receipt is voided only through the transfer.

---

## 6. Per-branch close

- A per-branch period status sits beside `FiscalPeriod`: one row per period per
  branch. `FiscalPeriod.status` stays the tenant-level state.
- A branch closes, soft-closes, reopens and locks on its own, with the close
  checklist run against that branch's entries.
- All branches closes the tenant's period only once every branch row is closed.
  The year-end close runs per branch, and the tenant's year closes when every
  branch's has.
- A transfer is refused unless both branches are open on its date.
- **Built.** The close check `inter_branch_balanced` (`vs_finance.inter_branch.
  inter_branch_close_check`) reads the inter-branch account up to the period's
  end: across all branches it must net to zero, and each pair's two sides must
  agree. It blocks the tenant's close; a single branch's close runs it for the
  pairs that branch is part of, as a warning.

---

## 7. Tax

- The tenant has one tax number and files one return per tax per period.
- Each branch works out its own VAT and withholding tax on its own books. The
  return adds the branches together, and each branch pays its own share from its
  own bank.
- Payroll tax (PAYE) is owed to the state where the staff member works, so its
  obligations group by the branch's state (`Branch.state`).

---

## 8. Shared costs and recharges

- A cost paid on behalf of several branches is paid by one branch, from its own
  bank.
- For each such cost the tenant chooses: the paying branch **absorbs** it, or
  **recharges** it to the others.
- A recharge splits the cost by a weight per branch, from one of two sources:
  - **per-branch counts** (the default): the schools product supplies pupil
    numbers per branch through the FAL, so the engine never learns what a pupil
    is;
  - **fixed percentages** set by the tenant, for a cost that does not follow
    headcount. Bright Star Abuja holds 20% of the pupils but its boarding house
    drives most of the insurance bill, so the owner sets Abuja at 40%.
  Fixed percentages must total 100.
- **Built.** The choice per cost is a `SharedCostRule` (absorb by default,
  counts by default; fixed percentages kept as its shares), written only by a
  whole-tenant caller. A recharge (`/finance/recharges/`) splits the cost
  exactly (largest remainder) and books each other branch's share as a
  `RECHARGE` transfer: `Dr inter-branch [owing], Cr expense` at the paying
  branch and `Dr expense, Cr inter-branch [paying]` at the owing one. The counts
  are plain numbers in the request; the schools product supplies pupil numbers.

---

## 9. A pupil changing branch

- The students app owns the move and calls a FAL operation; the finance engine sees
  only "move this customer to that branch".
- The move posts a receivable transfer for the open balance: the new branch takes
  over the receivable and owes the old one.
- The pupil's unpaid invoices move to the new branch's lists, so its bursar sees
  and chases them. The old branch's revenue reports, which read entries, still
  show the fees earned there.
- The finance customer's branch follows the pupil.
- **Built (finance side).** `vs_finance.inter_branch.transfer_open_receivables`
  moves the customer's whole position at the old branch, so the new branch holds
  everything and the old branch keeps only the revenue it has already earned:
  - every open invoice and open debit note is given the new branch in its own
    branch column, so the new branch's bursar sees and chases it, and every
    later receipt, credit note or write-off on it is the new branch's; a live
    payment plan on a moved invoice moves too. The revenue journals stay at the
    old branch;
  - the customer's unapplied credit (receipts and credit notes not yet spent,
    less what a pending refund reserves) is drawn from its source documents,
    which stay at the old branch, and reappears at the new branch as one receipt
    applied to the customer's bills there;
  - income not yet earned moves: a moved invoice's deferred shares recognised
    after the move date are given the new branch, so later releases recognise
    the revenue there. Shares already released, or recognised by the move date,
    stay at the old branch, which earned them.

  Each branch posts one journal: the old branch credits the receivable and
  debits customer credit and deferred income, the new branch books the mirror
  image, and the difference is the inter-branch balance. It is idempotent per
  move, runs inside the caller's transaction, and is audited under both
  branches. A moved invoice or debit note, and a receipt or credit note whose
  credit moved, cannot be voided on their own; the move is voidable until
  anything it moved is paid, credited or released at the new branch. The FAL's
  account re-filing calls it right after re-filing the customer; it is also a
  whole-tenant endpoint (`/finance/inter-branch-transfers/receivable-moves/`).

---

## 10. Buying and stock

1. Branches raise requisitions against their own budgets.
2. Buying together is one negotiation: requisitions from several branches can go
   out as one request for quotation, and the agreed price then raises **one
   purchase order per branch**. Each branch receives its own goods, is invoiced
   for them, and pays the supplier from its own bank.
3. A central store belongs to one branch. Another branch requisitions from it, and
   the goods issued are a goods transfer at cost.
4. A supplier advance is its branch's asset and settles only that branch's bills.
5. **Built.** A store-to-store move is `vs_procurement.stock.transfer_stock`
   (`/procurement/stock-items/<id>/transfer/`), a TRANSFER movement out of one
   store and into the other at the sending store's moving average. Between two
   branches' stores it books a `GOODS` transfer (`Dr inter-branch [receiving],
   Cr inventory` and `Dr inventory, Cr inter-branch [sending]`); between two
   stores of one branch it posts nothing. Goods are returned by a transfer back,
   never by a void.

---

## 11. Working branch by branch

### The selector

Finance uses the branch and session selector the rest of the product already
has in its menu, not the entity selector. A tenant keeps one set of books, so
the entity selector offers a school a single option and finance does not show
it.

| Option | What the caller gets |
| --- | --- |
| **All branches** | Everything added up: every branch's lists, statements, bank accounts and transfers. |
| **Ikeja / Lekki / Abuja** | That branch's own lists, statements, bank accounts, transfers and close. |

- The options are the branches in the caller's reach. A caller with one option
  sees no branch choice; at a one-branch tenant nobody does.
- **The session applies to fee screens only.** Bills, collections, debtors, fee
  runs and concessions follow the selected session or term, which the schools
  product maps to its billing periods; the engine never learns what a session
  is. The accounting screens (journals, trial balance, income statement,
  balance sheet, tax, payroll, bank and close) keep their own financial year and
  period picker and hide the session: a session runs September to July and
  spans two financial years, so a session view would match neither the closed
  accounts nor the returns filed.
- **Creating a record.** With one branch selected, the form shows that branch and
  sends it with the record. Under All branches the form asks for the branch and
  preselects none.
- **Each tab keeps its own selection** (owner decision of 2026-10-01). The
  branch and session live in the page address (`?branch=`, and the session on
  fee screens), never in stored preferences, so Mrs Bello can keep Ikeja in one
  tab and Lekki in another. A link into finance from another part of the
  product carries the branch selected there, so opening finance starts where
  the caller already was.

### Backend

- `resolve_entity` is the one place every finance, procurement and payments view
  resolves `?entity=`. A branch resolver beside it reads `?branch=`, ANDs it with
  the caller's grants (`vs_rbac.scoping`), and returns the scope every list and
  report narrows by.
- A branch outside the caller's reach is reported exactly like an unknown one.
- The entities list returns, per entity, the selector options this caller may use.
- Writes never take their branch from `?branch=`. The branch travels in the body.

---

## 12. Reports and access

**Reports**

- Branch trial balance, income statement and balance sheet.
- A transfer register, and a grid of pair balances (who owes whom), with
  drill-down to each transfer.
- All branches statements: every branch added up. The inter-branch account nets
  to zero.

**Access**

- A branch-bound user records money only in their own branches, and sees the
  transfers their branches are party to.
- Tenant-level configuration (settings, the fiscal calendar, tax setup, master
  data) needs a caller who reaches every branch. At a one-branch tenant, a grant
  pinned to the only branch reaches every branch.
- Permission keys for transfers: request, approve, and run a recharge.
  Branch-scoped grants apply. Built as `finance.interbranch.view`, `.request`
  (the receiving branch asks), `.transfer` (the sending branch sends, declines,
  forwards a held receipt or moves a customer's balance), `.confirm` (the
  receiving branch confirms arrival), `.recharge` and `.reverse`. Who may act:
  the receiving branch requests and confirms, the sending branch sends and
  declines, a void needs both branches, and a receivable move or a shared-cost
  rule needs the whole tenant.

---

## 13. Moving today's data

Existing books were kept with blank branches. Moving them:

- every transaction with a blank branch is given the tenant's main branch;
- a bank account used by several branches is split into one bank record and
  ledger account per branch, with opening balances agreed by the bursar. Where a
  branch's own entries on the shared account differ from its agreed share, the
  person splitting chooses per split: by default the difference is a debt between
  branches, booked as a `BANK_SPLIT` inter-branch transfer (largest deficit
  against largest surplus first, so every pair is explicit); or it moves
  permanently through retained earnings. Ikeja's entries total 500k and Lekki's
  minus 100k on a shared 400k; they agree 250k and 150k; by default Lekki owes
  Ikeja 250k (`vs_finance.bank_splits`);
- blank-branch budgets and petty cash funds are assigned to a branch.

---

## 14. Build order

1. The rule that every transaction names a branch, `raised_branch` and
   `inherited_branch_id` enforcing it, and the data migration (section 13).
2. Bank, petty cash and collection accounts per branch; payment-provider
   sub-accounts per branch.
3. Cash transfers: the document, approval, pair balances, the register, and bank
   matching.
4. Per-branch close, year-end per branch, and the All branches close.
5. Held-for-another-branch receipts and forwarded-receipt transfers.
6. Tax worked per branch and filed together; payroll one run with one journal per
   branch.
7. Recharges, with count and fixed-percentage weights.
8. Pupil branch move (students app, FAL operation, customer branch sync) and
   receivable transfers.
9. Goods transfers from a central store, and one-order-per-branch buying.
10. The branch selector (the product's own branch and session selector, section 11)
    and the transfer and pair-balance reports.

Built so far from this order: 3 (cash transfers, approval, pair balances
`/finance/inter-branch-balances/`, the register `/finance/inter-branch-transfers/`,
bank matching), 5, 7, the finance half of 8, and the goods half of 9.

---

## 15. Code map for the inter-branch work

| Piece | Where |
| --- | --- |
| Services: cash, held and forwarded receipts, voids, receivable moves, recharges, goods, pair balances | `apps/vs_finance/inter_branch.py` |
| Models | `apps/vs_finance/models/interbranch.py`; `JournalLine.counterparty_branch` |
| Endpoints | `apps/vs_finance/views_ops/interbranch.py`; `vs_procurement.views.stock.StockTransferView` |
| Approval route | `finance.inter_branch_transfer` in `apps/vs_finance/workflow_handlers.py` |
| Store-to-store moves | `vs_procurement.stock.transfer_stock` |
| Tests | `vs_finance.tests_inter_branch` (three branches, and the one-branch tenant where it recedes) |
