# finance_branch_balanced_books_design

**Status:** design, not built. Every decision below is agreed.

A tenant with more than one branch chooses how its books treat those branches.
There are two modes, and the tenant picks one for all of its branches:

| Mode | What a branch is | Who it suits |
| --- | --- | --- |
| **Shared books** (default, and what runs today) | A label on documents. One pool of cash, one set of balances; branch reports are a filter. | Schools run centrally, where the branches do not hold their own money. |
| **Branch-balanced books** | A label that also balances. Every branch has a trial balance and balance sheet that stand on their own, and money one branch spends or holds for another is recorded as owed between them. | Schools where each branch banks its own fees, has its own bursar, and the owner wants to see each branch's true position. |

Both modes keep **one** `LedgerEntity`: one chart of accounts, one tax number,
one numbering series, one set of fiscal years. Branches are never separate legal
companies, so there is no multi-entity consolidation and no elimination step.
Because it is one set of books, every "owed between branches" amount cancels out
in the tenant total by construction.

A tenant with one branch never sees the choice. The setting, the inter-branch
reports and the per-branch close controls are absent, not disabled.

---

## 1. Decisions

1. The mode is one tenant-wide setting. There is no per-branch choice.
2. Branches are not separate legal entities. No sub-entities, no consolidation.
3. Owners see who owes whom between branches, pair by pair, not only branch profit.
4. Each branch closes its own month.
5. A blank branch keeps its meaning: shared across the tenant. In branch-balanced
   mode the shared pot balances like a branch does (shown as **School-wide** in
   the school product). Shared costs can optionally be split across branches at
   month end.
6. An entry that touches several branches needs every one of them open on its
   date. The tenant's year closes only when every branch and the shared pot have
   closed.
7. The mode switches on only at the start of a period, through a setup step, and
   switches off only at the start of a fiscal year, once inter-branch balances are
   settled.
8. When a pupil moves branch, their unpaid balance moves with them: the new branch
   takes over the receivable and owes the old branch that amount at once.
9. Buying: branches request, head office may combine requests into one order,
   each order line names the branch it is for, and cost lands on that branch.
   Suppliers are shared across the tenant by default.
10. A moved pupil's unpaid invoices move to the new branch's lists. The revenue
    they earned stays in the old branch's figures.
11. The shared-cost split uses pupil numbers per branch by default, or fixed
    percentages the school sets instead.
12. A staff member's pay is charged to one branch by default, with an optional
    percentage split across branches on their salary record.

---

## 2. How a month runs (worked example)

Bright Star School runs Ikeja (main), Lekki and Abuja in branch-balanced mode.

| Event | Lines (branch in brackets) | Effect |
| --- | --- | --- |
| Lekki collects 12m of fees into Lekki GTBank | Dr Bank [Lekki] 12m, Cr Receivable [Lekki] 12m | Lekki only. |
| Head office pays Lekki's diesel 800k from Ikeja Zenith | Dr Diesel expense [Lekki] 800k, Cr Bank [Ikeja] 800k, plus added: Dr Inter-branch [Ikeja, owed by Lekki] 800k, Cr Inter-branch [Lekki, owed to Ikeja] 800k | Lekki owes Ikeja 800k. |
| Lekki sends 10m to Ikeja's account (branch transfer) | Dr Bank [Ikeja] 10m, Cr Bank [Lekki] 10m, plus the inter-branch pair | Net: Ikeja owes Lekki 9.2m. |
| Mrs Adeyemi pays 600k into Ikeja for children at Lekki (400k) and Abuja (200k) | Dr Bank [Ikeja] 600k, Cr Receivable [Lekki] 400k, Cr Receivable [Abuja] 200k, plus pairs | Ikeja owes Lekki 400k and Abuja 200k. |
| Annual audit fee 3m | Dr Audit fee [School-wide] 3m, Cr Payables [School-wide] 3m | Sits in the shared pot; an optional split run spreads it by pupil numbers. |
| 5 Oct: Lekki closes September | Lekki's September is closed | Abuja keeps posting to September. |

The tenant balance sheet shows 0 on the inter-branch account, because every pair
nets to zero.

---

## 3. The posting rules

Every journal already passes through `vs_finance.posting.post_journal`. That is
the single choke point, so the whole mode is enforced there and nowhere else.

1. **Each line has a branch.** Today only the entry has one. A line's branch is
   resolved in this order, so most posting callers change nothing:
   1. the branch the caller set on the line explicitly;
   2. the branch of the bank account or petty cash fund whose GL account the line
      hits (both models already carry a branch);
   3. the entry's own branch.
2. **Shared mode:** lines carry a branch for reporting, and nothing else changes.
3. **Branch-balanced mode:** after resolving, lines are grouped by branch (blank
   is its own group). Any group that does not net to zero gets an added line on
   the inter-branch account, paired against the other side:
   - when one side of the imbalance has a single branch, every other branch is
     paired against it (the Adeyemi receipt: Ikeja pays out to Lekki and Abuja);
   - when both sides have several branches, each is paired against the shared
     pot, which keeps the rule deterministic. This is rare.
   Each added line records its branch **and** its counterparty branch, which is
   what the "who owes whom" report reads.
4. **Period guard:** the entry is refused unless every branch it touches (after
   the added lines) is open for its date.
5. **Reversal** mirrors the original lines, added lines included, so a reversal
   never has to rediscover the pairing.

The inter-branch account is a new well-known account provisioned with the starter
chart (a key in `FinanceAccountMapping`, for example code 1950 "Inter-branch
balances"), so a tenant that remaps it is honoured.

---

## 4. Per-branch close

- A new per-branch period status sits beside `FiscalPeriod`: one row per period
  per branch, plus one for the shared pot. `FiscalPeriod.status` stays the
  tenant-level state.
- A branch closes, soft-closes, reopens and locks on its own, with the existing
  close checklist run against that branch's lines.
- The tenant-level period closes when every branch row and the shared pot have
  closed. Year-end closing requires that for every period.
- In shared mode the per-branch rows do not exist and close runs as it does now.

---

## 5. Money and banks

- Every bank account and petty cash fund belongs to a branch or to the shared pot.
  The setup step makes the bursar assign every existing one.
- Each branch has its own collection account, printed as "pay to" on invoices for
  its pupils. Today there is one per entity (`is_primary_collection`).
- Online payments may settle into any account. When Paystack pays a Lekki fee into
  Ikeja's account, rule 3 records Ikeja holding Lekki's money. No per-branch
  Paystack setup is required, though a school may add one.
- A **branch transfer** is a new document: move cash from one branch's bank to
  another's, with maker-checker approval like a payout.

---

## 6. Shared costs and the split run

- Blank-branch entries sit in the shared pot and stay there unless split.
- The optional month-end split run moves shared costs to branches by a weight per
  branch. The finance engine takes the weights as plain numbers and supports two
  sources:
  - **per-branch counts** (the default): the schools product supplies pupil
    numbers per branch through the FAL, so the engine never learns what a pupil
    is;
  - **fixed percentages** set by the tenant, for a cost that does not follow
    headcount. Bright Star Abuja holds 20% of the pupils but its boarding house
    drives most of the insurance bill, so the owner sets Abuja at 40%.
  Fixed percentages must total 100 across the branches that take a share.
- The split posts one entry per run, reversible like any other.

---

## 7. A pupil changing branch

- There is no branch-move flow for pupils today (the student's branch is read-only
  after admission), so this arrives with one. The students app owns the move and
  calls a new FAL operation; the finance engine sees only "move this customer to
  that branch".
- In branch-balanced mode the operation posts `Dr Receivable [new] / Cr
  Receivable [old]` for the open balance, and rule 3 records that the new branch
  owes the old one. Revenue already earned stays in the old branch's figures.
- The pupil's unpaid invoices move to the new branch's lists, so the new branch's
  bursar sees and chases them and can match a payment to them. The old branch's
  staff no longer see those invoices; their revenue reports, which read journal
  lines, still show the fees earned there.
- In shared mode the customer's branch simply changes.
- Either way the finance customer's branch must follow the pupil. Today it is set
  once, when the pupil is first billed, and never updated.

---

## 8. Buying

1. Branches raise requisitions against their own budgets. The branch head approves
   up to a limit; head office approves above it.
2. Head office may combine requisitions from several branches into one purchase
   order. Each order line keeps the branch it is for.
3. Goods are received into that branch's store where possible, so cost lands on
   the right branch from the start.
4. Goods received into a central store reach a branch by a **stock transfer**,
   a new movement type (today there are only receipt, issue and adjustment). In
   branch-balanced mode the transfer carries its cost to the receiving branch and
   rule 3 records the debt.
5. Head office pays the supplier once; each branch owes its share through rule 3.
6. Suppliers are shared across the tenant by default.

---

## 9. Switching modes

- **On:** at the start of a period only. The setup step assigns every bank account,
  petty cash fund and store to a branch or the shared pot; open receivables follow
  each customer's current branch; whatever cannot be placed stays shared. Opening
  per-branch balances are posted as one entry, which rule 3 balances.
- **Off:** at the start of a fiscal year only, and only once every inter-branch
  pair is settled to zero. Nothing is deleted; the lines keep their branches.

---

## 10. Reports

- Branch trial balance, income statement and balance sheet read line branches.
  In branch-balanced mode the "cash is not money this branch holds" label on
  narrowed statements goes away, because it becomes true.
- A new inter-branch report: a grid of who owes whom, with drill-down to entries.
- The tenant statements are unchanged: the inter-branch account nets to zero.
- `AccountBalance` stays per account per period. Branch statements are built from
  lines, as `vs_finance.branch_ledger` already does; a per-branch aggregate is
  added only if that proves slow.

---

## 11. Access

- A branch-scoped user posts only within their branches. An entry that touches a
  branch outside the user's reach is refused, so a Lekki bursar cannot pay Lekki
  bills out of Ikeja's bank.
- New permission keys: switch the mode, close a branch period, create and approve
  a branch transfer, run the shared-cost split. Branch-scoped grants apply to the
  close and transfer keys.

---

## 12. Staff at more than one branch

- A salary record is charged to one branch by default, as it is today.
- It may instead carry a percentage split across branches (Mr Eze teaches at
  Ikeja three days and Lekki two, so 60/40). The split must total 100.
- A payroll run posts each person's cost lines to their branches by that split.
  In branch-balanced mode, when one branch's bank pays the whole run, rule 3
  records what each other branch owes it.

---

## 13. Tax

This holds in both modes. VAT and withholding tax stay tenant-wide (one tax
number). Payroll tax (PAYE) is owed to the state where the staff member works, so
PAYE obligations group by the branch's state (`Branch.state`).

---

## 14. Build order

1. Line-level branch and the resolution rule (both modes). Carries every later step.
2. Inter-branch account, the balancing rule in `post_journal`, reversal mirroring.
3. Per-branch period status and close.
4. Bank, petty cash and collection accounts per branch; branch transfer document.
5. Mode setting and the switch-on setup step.
6. Line-branch reports and the inter-branch grid.
7. Pupil branch move (students app, FAL operation, customer branch sync).
8. Stock transfer and consolidated purchase orders.
9. Shared-cost split run, with count and fixed-percentage weights.
10. Salary split across branches.
