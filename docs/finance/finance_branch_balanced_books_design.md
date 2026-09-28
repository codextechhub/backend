# finance_branch_balanced_books_design

**Status:** design, not built. Every decision below is agreed except the one open
question in section 17.

A tenant with more than one branch chooses how its books treat those branches.
There are two modes, and the tenant picks one for all of its branches:

| Mode | What a branch is | Who it suits |
| --- | --- | --- |
| **Shared books** (default, and what runs today) | A label on documents. One pool of cash, one set of balances; branch reports are a filter. | Schools run centrally, where the branches do not hold their own money. |
| **Branch-balanced books** | The owner of every money record. Each branch banks, spends and closes on its own, and its statements stand on their own. Branches never pay for each other; they lend to each other through inter-branch transfers. | Schools where each branch banks its own fees, has its own bursar, and the owner wants to see each branch's true position. |

Both modes keep **one** `LedgerEntity`: one chart of accounts, one tax number,
one numbering series, one set of fiscal years. Branches are never separate legal
companies, so there is no multi-entity consolidation and no elimination step.

A tenant with one branch never sees the choice. The setting, the transfer screens
and the per-branch close controls are absent, not disabled.

---

## 1. Decisions

**The model**

1. The mode is one tenant-wide setting. There is no per-branch choice.
2. Branches are not separate legal entities. No sub-entities, no consolidation.
3. In branch-balanced mode **everything that moves money belongs to exactly one
   branch**: every invoice, receipt, bill, payment, journal, bank account and
   petty cash fund. A head office, where a school has one, is a branch like any
   other.
4. **All branches is a view, not a set of books.** It has no bank account and no
   records of its own. It shows everything every branch did, added up.
5. **No branch pays for another branch's goods or services.** When a branch is
   short, another branch lends to it through an **inter-branch transfer**, and the
   books record who owes whom until it is repaid.
6. A bursar sees the transfers their own branches are party to. A caller who
   reaches every branch sees all of them.

**Money in**

7. Every branch has its own collection bank account and its own payment-provider
   sub-account, so fees paid online or by transfer land in the pupil's own branch.
8. Money that still lands in the wrong branch is recorded as **held for** the
   right branch and forwarded by transfer. The pupil's invoice is paid when the
   money reaches their branch.

**Closing**

9. Each branch closes its own month. A transfer needs both branches open on its
   date.
10. All branches closes the tenant's period, and only after every branch has closed.

**Moving balances between branches**

11. When a pupil moves branch, their unpaid balance moves with them: the new branch
    takes over the receivable and owes the old branch that amount. Their unpaid
    invoices move to the new branch's lists; the revenue stays with the old branch.
12. Tax balances move each month to the tenant's **filing branch** (the main branch
    unless the tenant names another), which files and pays every return. Each
    branch then sends the cash for its share by transfer.
13. A cost one branch pays on behalf of all (the audit fee) is either absorbed by
    that branch or **recharged** to the others by pupil numbers or fixed
    percentages, whichever the tenant chooses for that cost.

**Buying and pay**

14. Buying together means one negotiated price and one order per branch at that
    price. Each branch receives and pays for its own goods.
15. Goods sent from one branch's store to another's are a goods transfer: the
    receiving branch owes their cost.
16. Payroll runs per branch. Each staff member is paid in full by one branch; there
    is no salary split.

**Working in it**

17. The entity selector also chooses **All branches** or one branch. It is hidden
    whenever the caller has only one option.
18. A new money record under All branches must name its branch. There is no
    school-wide option, because there is no school-wide book.
19. Shared-books schools get a per-screen branch filter on finance lists and
    reports instead of the selector.
20. The mode switches on only at the start of a period, through a setup step, and
    switches off only at the start of a fiscal year, once every transfer balance is
    settled to zero.

---

## 2. How a month runs (worked example)

Bright Star School runs Head Office, Ikeja, Lekki and Abuja in branch-balanced
mode. Head Office is the main branch and the filing branch.

| Event | What is recorded | Effect |
| --- | --- | --- |
| Lekki collects 12m of fees, online and by transfer | Lekki: Dr Bank, Cr Receivable | Lekki only. The provider sub-account settled into Lekki's bank. |
| Lekki is short for diesel and asks Ikeja for 1m | Transfer. Ikeja: Dr Owed by Lekki, Cr Bank. Lekki: Dr Bank, Cr Owed to Ikeja | Lekki owes Ikeja 1m. |
| Lekki pays its diesel supplier 800k | Lekki: Dr Diesel, Cr Bank | Lekki's own cost, from Lekki's own bank. |
| Mrs Adeyemi pays 400k of Lekki fees into Ikeja's account by mistake | Ikeja: Dr Bank, Cr Held for Lekki. Then a transfer forwards it | Lekki's invoice is paid when the 400k reaches Lekki. |
| Head Office pays the 3m audit fee and recharges it by pupil numbers | Head Office: Dr Audit fee, Cr Bank; then a recharge | Lekki owes Head Office 600k, and so on. |
| Month end: tax balances move to Head Office | Lekki: Dr VAT payable, Cr Owed to Head Office. Head Office: the reverse | Head Office files one return; Lekki sends the cash by transfer. |
| 5 Oct: Lekki closes September | Lekki's September closes | Abuja keeps posting. |
| 9 Oct: Abuja, the last branch, closes | All branches closes September | The tenant's September is closed. |

The All branches view shows every bank account, every branch's figures and every
transfer. Its "owed between branches" total is zero, because every transfer is
recorded on both sides.

---

## 3. The one-branch rule

In branch-balanced mode:

1. Every money document and every journal entry has a branch. A blank branch is
   refused.
2. Every line of an entry belongs to the entry's branch. A bank account or petty
   cash fund used on a document must belong to the document's branch, so Ikeja's
   bank cannot pay a Lekki bill.
3. A receipt or payment may only settle documents of its own branch. Today a
   receipt settling invoices from two branches resolves to "shared"
   (`vs_rbac.scoping.inherited_branch_id`); in this mode it is refused instead.
4. The only records that touch two branches are transfers (section 4). Each
   transfer posts **two** entries, one per branch, linked to each other, so the
   rule "one entry, one branch" never has an exception.
5. Lists that are not money may still be shared across branches: suppliers,
   catalogue items and fee templates. A shared supplier is a name every branch can
   buy from, not a place money sits.

The rule is enforced once, where every entry already passes
(`vs_finance.posting.post_journal`), and at the document write paths where the
branch is chosen (`raised_branch` and `inherited_branch_id`).

Because no entry mixes branches, branch statements need no new plumbing:
`vs_finance.branch_ledger` already builds them from the entry's branch, and in
this mode they become complete. The "cash is not money this branch holds" label
on narrowed statements goes away, because it becomes true.

---

## 4. Inter-branch transfers

A transfer is one document with a sending and a receiving branch. It comes in
kinds, which share one balance between each pair of branches:

| Kind | What moves | Example |
| --- | --- | --- |
| **Cash** | Money, bank to bank | Ikeja lends Lekki 1m for diesel; Lekki repays later. |
| **Forwarded receipt** | Money that arrived at the wrong branch | Ikeja forwards Mrs Adeyemi's 400k to Lekki. |
| **Receivable** | A pupil's unpaid balance | Tunde moves from Ikeja to Lekki owing 150k. |
| **Tax** | A branch's tax balance | Lekki's VAT for September moves to Head Office. |
| **Recharge** | A share of a cost one branch paid for all | Head Office bills Lekki 600k of the audit fee. |
| **Goods** | Stock, at cost | Head Office's store sends Lekki 300 textbooks. |

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
`FinanceAccountMapping` key, for example code 1950), carries every transfer. Each
line records its counterparty branch, which is what the pair balances and the
register read. Across all branches the account always nets to zero.

**Visibility.** A transfer is visible to anyone whose reach includes either of its
two branches. This is a new scoping shape: every other finance row has one branch,
a transfer has two, so its query is "sending branch in reach **or** receiving
branch in reach".

---

## 5. Money in

- The setup step requires every branch to have a collection bank account and a
  payment-provider sub-account before the mode switches on. Invoices print the
  pupil's own branch's account as "pay to".
- Money arriving at the wrong branch is receipted there against a "Held for other
  branches" liability, naming the branch it belongs to. The receiving branch
  cannot apply it to another branch's invoice. A forwarded-receipt transfer moves
  it, and the invoice is settled on arrival.

---

## 6. Sweeps

A sweep moves a branch's money to another branch on a schedule. The tenant chooses
one of four options:

| Option | When a sweep is recorded |
| --- | --- |
| **Manual** | When a bursar makes one. |
| **Daily** | Once a day, for the day's movement. |
| **Month end** | Once, at the branch's close. |
| **As it happens** | With each transaction, as it is made. |

What a sweep moves, and to where, is the open question in section 17.

---

## 7. Per-branch close

- A per-branch period status sits beside `FiscalPeriod`: one row per period per
  branch. `FiscalPeriod.status` stays the tenant-level state.
- A branch closes, soft-closes, reopens and locks on its own, with the existing
  close checklist run against that branch's entries.
- All branches closes the tenant's period only once every branch row is closed.
  Year-end closing requires that for every period.
- A transfer is refused unless both branches are open on its date, so neither side
  of a pair can be written into a closed month.
- In shared-books mode the per-branch rows do not exist and close runs as it does
  now.

---

## 8. Tax

- VAT and withholding tax stay tenant-wide: one tax number, one return.
- At month end each branch's tax balances move to the filing branch as tax
  transfers. The filing branch files and pays; each branch settles its share with
  a cash transfer.
- Payroll tax (PAYE) is owed to the state where the staff member works, so its
  obligations group by the branch's state (`Branch.state`). The filing branch
  files for every state.

---

## 9. Shared costs and recharges

- A cost paid on behalf of every branch is paid by one branch, from its own bank.
- For each such cost the tenant chooses: the paying branch **absorbs** it, or
  **recharges** it to the others.
- A recharge splits the cost by a weight per branch, and the finance engine takes
  the weights as plain numbers from one of two sources:
  - **per-branch counts** (the default): the schools product supplies pupil
    numbers per branch through the FAL, so the engine never learns what a pupil
    is;
  - **fixed percentages** set by the tenant, for a cost that does not follow
    headcount. Bright Star Abuja holds 20% of the pupils but its boarding house
    drives most of the insurance bill, so the owner sets Abuja at 40%.
  Fixed percentages must total 100.

---

## 10. A pupil changing branch

- There is no branch-move flow for pupils today (the student's branch is read-only
  after admission), so this arrives with one. The students app owns the move and
  calls a new FAL operation; the finance engine sees only "move this customer to
  that branch".
- In branch-balanced mode the move posts a receivable transfer for the open
  balance: the new branch takes over the receivable and owes the old one.
- The pupil's unpaid invoices move to the new branch's lists, so its bursar sees
  and chases them and can match a payment to them. The old branch's revenue
  reports, which read entries, still show the fees earned there.
- In shared-books mode the customer's branch simply changes.
- Either way the finance customer's branch must follow the pupil. Today it is set
  once, when the pupil is first billed, and never updated.

---

## 11. Buying

1. Branches raise requisitions against their own budgets. The branch head approves
   up to a limit; above it, approval goes to a caller who reaches every branch.
2. Buying together is one negotiation: requisitions from several branches can go
   out as one request for quotation, and the agreed price then raises **one
   purchase order per branch**. Each branch receives its own goods, is invoiced
   for them, and pays the supplier from its own bank.
3. Stock moved from one branch's store to another's is a goods transfer at cost.
   The movement type does not exist today (there are only receipt, issue and
   adjustment).
4. Suppliers are shared across branches by default (section 3, rule 5).

---

## 12. Payroll

- Branch-balanced mode requires per-branch payroll, the existing `PER_BRANCH`
  payroll scope: each branch runs and pays its own staff from its own bank.
- Each staff member is paid in full by one branch. A teacher who works at two
  branches is paid by one of them.

---

## 13. Working branch by branch

A finance admin who reaches every branch still needs to work on one branch at a
time.

### Branch-balanced mode: the selector

The entity selector (hidden today when a tenant has one entity) also offers the
branches:

| Option | What the caller gets |
| --- | --- |
| **All branches** | Everything added up: every branch's lists, statements, bank accounts and transfers. |
| **Head Office / Ikeja / Lekki / Abuja** | That branch's own lists, statements, bank accounts, transfers and close. |

- The options are the branches in the caller's reach. A caller who covers only
  Lekki has one option and sees no selector. A caller who covers Ikeja and Lekki
  sees each of them and "All my branches".
- **Creating a record.** With one branch selected, the form shows that branch and
  sends it with the record. Under All branches the form asks for the branch and
  preselects none: there is no school-wide book to fall back to.
- The selection lives in the page address beside `?entity=`, never in stored
  preferences, so two tabs can sit on two branches without interfering, and the
  branch a record is filed under is always the one visible on its form.

### Shared-books mode: the filter

- No selector. Finance lists and reports take a `?branch=` filter per screen,
  with the procurement meanings: a branch id, or `none` for school-wide rows only.
- A filtered report says so in its title and in its export ("filtered to Lekki"),
  because on shared books Lekki has no books of its own and the figures are a
  slice, not a statement.

### Backend

- `resolve_entity` is the one place every finance, procurement and payments view
  (about 300 call sites) resolves `?entity=`. A branch resolver beside it reads
  `?branch=`, ANDs it with the caller's grants (`vs_rbac.scoping`), and returns
  the scope every list and report narrows by. One rule, reached from every screen,
  rather than added screen by screen as payroll's `_filter_by_branch` was.
- A branch outside the caller's reach is reported exactly like an unknown one, so
  the parameter cannot be used to enumerate a tenant's branches.
- The entities list returns, per entity, the selector options this caller may use
  (none in shared-books mode), so the frontend never works out reach itself.
- Writes never take their branch from `?branch=`. The branch travels in the body,
  as it does today.

---

## 14. Switching modes

- **On:** at the start of a period only. The setup step requires:
  - every bank account, petty cash fund and store assigned to a branch;
  - every open invoice, bill and customer with a blank branch assigned to one
    (the main branch is offered as the default, and the bursar confirms);
  - a collection account and payment-provider sub-account for every branch;
  - payroll switched to per-branch.
  Opening balances per branch are then posted, one entry per branch.
- **Off:** at the start of a fiscal year only, and only once every pair balance is
  settled to zero. Nothing is deleted; entries keep their branches.

---

## 15. Reports and access

**Reports**

- Branch trial balance, income statement and balance sheet, complete in this mode.
- A transfer register, and a grid of pair balances (who owes whom), with
  drill-down to each transfer.
- All branches statements: every branch added up. The inter-branch account nets
  to zero.
- `AccountBalance` stays per account per period. Branch statements are built from
  entries, as `vs_finance.branch_ledger` already does; a per-branch aggregate is
  added only if that proves slow.

**Access**

- A branch-scoped user records money only in their own branches, and sees the
  transfers their branches are party to.
- New permission keys: switch the mode, request a transfer, approve a transfer,
  close a branch period, close All branches, run a recharge. Branch-scoped grants
  apply to the transfer and branch-close keys.

**Known effect outside finance**

A head-office branch is a branch like any other, so it appears in the pupil-facing
branch lists (admissions, classes), and a school with one teaching branch plus a
head office counts as multi-branch there. This is accepted as it stands.

---

## 16. Build order

1. The branch resolver beside `resolve_entity` and the `?branch=` filter on every
   finance list and report (both modes; useful to shared-books schools at once).
2. The mode setting and the one-branch rule on every money write path.
3. Bank, petty cash and collection accounts per branch; payment-provider
   sub-accounts per branch.
4. Cash transfers: the document, approval, pair balances, the register, and bank
   matching.
5. Per-branch close and the All branches close.
6. Held-for-another-branch receipts and forwarded-receipt transfers.
7. The switch-on setup step.
8. Tax transfers and the filing branch.
9. Recharges, with count and fixed-percentage weights.
10. Pupil branch move (students app, FAL operation, customer branch sync) and
    receivable transfers.
11. Goods transfers and one-order-per-branch buying.
12. Sweeps, once section 17 is settled.

---

## 17. Open question

**What does a sweep move, and to where?** All branches holds no money, so a sweep
cannot move money "to the main account". The two readings are:

1. **Money to a collecting branch.** The tenant names a branch (usually Head
   Office) that gathers the others' money, and a sweep is a cash transfer to it on
   the chosen schedule. Head Office then owes Lekki what it swept.
2. **Records to the All branches view.** Nothing moves between banks. The option
   decides when branch activity shows up in the All branches view.
