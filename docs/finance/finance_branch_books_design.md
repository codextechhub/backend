# finance_branch_books_design

**Status:** design, partly built. Every decision below is agreed.

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

---

## 5. Money in

- Every branch has a collection bank account and a payment-provider sub-account.
  Invoices print the pupil's own branch's account as "pay to".
- Money arriving at the wrong branch is receipted there against a "Held for other
  branches" liability, naming the branch it belongs to. The receiving branch
  cannot apply it to another branch's invoice. A forwarded-receipt transfer moves
  it, and the invoice is settled on arrival.

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

---

## 11. Working branch by branch

### The selector

The entity selector also offers the branches:

| Option | What the caller gets |
| --- | --- |
| **All branches** | Everything added up: every branch's lists, statements, bank accounts and transfers. |
| **Ikeja / Lekki / Abuja** | That branch's own lists, statements, bank accounts, transfers and close. |

- The options are the branches in the caller's reach. A caller with one option
  sees no selector; at a one-branch tenant nobody does.
- **Creating a record.** With one branch selected, the form shows that branch and
  sends it with the record. Under All branches the form asks for the branch and
  preselects none.
- The selection lives in the page address beside `?entity=`, never in stored
  preferences, so two tabs can sit on two branches without interfering.

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
  Branch-scoped grants apply.

---

## 13. Moving today's data

Existing books were kept with blank branches. Moving them:

- every transaction with a blank branch is given the tenant's main branch;
- a bank account used by several branches is split into one bank record and
  ledger account per branch, with opening balances agreed by the bursar;
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
10. The branch selector and the transfer and pair-balance reports.
