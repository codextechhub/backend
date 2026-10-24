# field_access_remaining_fields

Ledger of every user-visible business field that has no Field Access decision
yet, every field the platform deliberately leaves without a switch, every
product question still open, and every place where a registered switch is
bypassed by another payload.

Recorded: 12 October 2026, from an audit of the code changed between
13 September and 11 October 2026. Companion to `rbac_field_access_design.md`
(how switches work) and to the `field_access.py` module of each app (what is
registered).

Area F (messages, section 7A) was added on 21 October 2026 with the Module 32
build. It records a resource that registered its fields with the feature, so it
adds no open work.

---

## 1. Purpose and how to use this ledger

Field Access is part of every feature contract (see `CLAUDE.md`). Most of the
platform already has switches. This ledger holds what remains, in three parts:

1. **Fields without a decision** (sections 3 to 7). One table per resource or
   endpoint group. Each row names the response field or fields, what the value
   is, whether a person types it (stored) or the system derives it (computed),
   the paths that can write it, the recommended `resource.field` switch with its
   human label, whether it is Read-only (RO) or Read plus Write (RW), whether it
   ships open or sensitive and why, and its status.
2. **Open product questions** (section 8). Decisions that belong to the product
   owner and that block or shape a registration.
3. **Leaks** (section 9). A leak is a value governed by a registered switch that
   another payload serves without asking that switch: an export, a nested
   payload, an audit row, an approval card, a message, a dashboard, a branch
   scope that is too wide.

### Row statuses

| Status | Meaning |
| --- | --- |
| `recorded` | The field needs a switch and has none. A later registration change adds it. Nothing is enforced today. |
| `registered` | A switch already exists. The row is shown only where the table needs it for context or where the recommended decision changes. |
| `no switch: identifier` | A code, label, number or reference that identifies a record. Never switched. |
| `no switch: transport` | Ids, pagination, window echoes, options lists, provenance, counts of children, action flags. Never switched. |
| `no switch: owner decision` | Left without a switch by an explicit product decision (section 2). |

### Leak statuses

| Status | Meaning |
| --- | --- |
| `to be fixed in this wave` | The leak concerns a switch that exists today, or a branch scope that is wider than the caller's reach. The leak-closing change that accompanied this ledger closes it. |
| `recorded` | The leak concerns fields that have no switch yet, or depends on a registration in a later change. It closes when the field is registered. |
| `fixed: <test>` | The registered switch (or the caller's branch reach) is now applied at the shared source of the payload, and the named test proves it for a hidden and an open reader. |
| `exempt: <reason>` | The payload is governed by another rule on purpose, such as CodeX staff following platform keys, or the copy being a system write. |

"This wave" means that one leak-closing change. When it lands, change each
`to be fixed in this wave` row to `fixed`, with the commit. When a field is
registered, change its row from `recorded` to `registered` and remove it from
the open questions it settled. Keep the counts in section 10 current.

### Writing a registration from a row

* Put the switch in the owning app's `field_access.py` with the label, group,
  scope and every API alias shown in the row (the rows name the aliases).
* A row marked "shown only if X and Y are readable" is a derived value: it
  disappears unless every figure it is built from is readable.
* Apply the switch on every surface that carries the value: serializer,
  nested payload, export, document, email, approval card, audit text, search
  hit, dashboard block. The leaks in section 9 are the known gaps.
* Every stored field needs `assert_writable` on every write path in its row,
  including alternate paths (import, bulk, action endpoint).
* Hand-built dictionaries become small read-only serializers that carry
  `FieldAccessMixin`, or pass through `visible()`.
* Add a deep-payload case, an alternate-read-path case, an alternate-write-path
  case and a default-access case for each new resource, and test a one-branch
  and a multi-branch school.

---

## 2. Rules that shape every row

These product decisions are settled. They override the recommendations in the
audit and they answer several of the questions the audit raised.

1. **Identifiers have no switch.** Branch names, vendor names, customer names
   and codes, bank account names and document numbers are identifiers. They
   are never registered. Every existing branch switch and every existing
   document-number switch is removed (the audit found branch switches on
   `finance.customer`, `finance.bankaccount`, `finance.banktransaction`,
   `finance.banktransfer` and `finance.interbranch`, and document-number
   switches on bank documents and inter-branch documents). The write key
   `branch` on create has no switch either: where a record may be raised is
   decided by branch reach alone. Wording that names a document number or a
   branch is therefore never a leak.
2. **Customer contact details are open by default.** `billing_email`,
   `billing_phone` and `billing_address` ship `sensitive=False`.
3. **When one figure is governed by two switches, the stricter wins.** A reader
   needs both. A balance restated on an invoice screen needs the customer
   balance switch and the invoice switch. A payout beneficiary account needs
   the payout switch and the vendor bank switch. A stock transfer value needs
   the stock switch and `finance.interbranch.amount`.
4. **Documents printed or emailed to an outside party stay unfiltered.** The
   invoice, receipt (including its balance lines), statement, credit and debit
   note, dunning notice, payslip, purchase order PDF and email, and the public
   pay page are addressed to the payer, vendor or employee, not to the person
   who presses Print or Send. They are declared system surfaces
   (`SYSTEM_SURFACES` in `vs_finance/documents.py`). The parts of the printed
   receipt that are not yet declared (`customer_balance_after`, per-bill
   `invoice_balance_after`) are added to the declaration.
5. **Unit prices on invoice and credit-note lines have their own switches**
   (`finance.invoice.unit_price`, `finance.creditnote.unit_price`), apart from
   the line set.
6. **AR aging has its own switches, apart from customer balances.** A role can
   read a customer's balance without reading how late it is.
7. **Statements, budget against actual, and report exports use section switches.**
   The Area A switches are registered and enforced on JSON and exports. Other
   areas retain their own pending decisions.
8. **Defaults.** A stored or submitted field gets Read and Write. A computed,
   generated, status, audit or server-owned field gets Read only
   (`writable=False`). Transport values get nothing. Normal business fields ship
   open (`sensitive=False`); a field ships sensitive only for a stated reason.
9. **Derived values follow their parts.** A ratio, difference, net or check flag
   is shown only if every figure it restates is readable.

Resource names in the tables follow the permission key that guards the endpoint
(`finance.invoice` for `finance.invoice.view`), because the role editor walks
Module, Resource, Field.

---

## 3. Area A: budgets and financial statements

The 8 `finance.budget` and 17 `finance.report` fields in this area are registered.
Budgets are written by people; every statement is
computed from the ledger. Statements have no per-branch columns: a branch-bound
reader sees only their own branches' journals (`narrowed`).

Statement figures are Read-only, open switches (nobody types them), in the
`finance.report` resource, one switch per statement section or column. Every
report view also serves `?export=csv|xlsx|pdf` from the same data; the filter
sits before the table builder so the export cannot show what the JSON hides.

### 3.1 Budgets (`finance.budget`)

Routes under `/finance/`: `budgets/` (`BudgetListCreateView`, `finance.budget.view`
and `.create`), `budgets/rollup/` (`.view`), `budgets/<pk>/` (`.view`, `.edit`,
`.delete`), `budgets/<pk>/lines/` and `lines/<line_id>/` (`.edit`),
`budgets/<pk>/approve/` (`.approve`), `budgets/<pk>/variance/` and `heatmap/`
(`.view`). Serializers: `BudgetSerializer`, `BudgetLineSerializer`; the
variance, heatmap, roll-up and list-extras payloads are hand-built in
`views_ops/budgets.py`. Frontend: `FinPro/src/pages/finance/budgets/budgets-tab.tsx`,
`FinPro/src/redux/services/finance/ops-api.ts`, `ops-types.ts`. A budget has no
import path and no approval-inbox handler.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `lines[].id`, `budget_id`, `fiscal_year_id` | record, line and year ids | Transport | none | - | - | - | - | no switch: transport |
| `code`, `budgets[].code` | budget number | Computed | none | - | - | - | - | no switch: owner decision |
| `name`, `budget_name`, `budgets[].name`, names inside response and error sentences | budget title, free text typed per budget | Stored | create `name`; PATCH `name` | `finance.budget.name` | Budget name | RW | open; the free-text title has its own switch (Q-A6 answered) | registered |
| `fiscal_year` | fiscal year the plan covers | Stored, create only | create `fiscal_year` | `finance.budget.fiscal_year` | Fiscal year | RW (open on create) | open | registered |
| `status`, `is_locked` | draft or approved | Computed | approve action | `finance.budget.status` | Status | RO | open | registered |
| `approved_at` | date the plan was approved | Computed | approve action | `finance.budget.approved_at` | Approved on | RO | open (`approved_by` is not serialized and stays out) | registered |
| `lines` | the set of budget lines | Stored, nested | create `lines`; `budgets/<pk>/lines/` POST, PUT, DELETE | `finance.budget.lines` | Budget lines (account, cost centre, period) | RW | open; coordinates have a separate switch from amounts (Q-A5 answered) | registered |
| `lines[].account`, `lines[].account_id`, `lines[].cost_center`, `lines[].period_no` | cell coordinates | Stored | with `lines` | - | - | - | - | no switch: identifier |
| `lines[].amount`, `budgeted_total`, variance `budget`, heatmap `cells[].budget`, `budget_total`, `total_budget`, roll-up `total_budget`, income statement `budget`, dashboard `plan` | planned amount | Stored | create `lines`; lines endpoints | `finance.budget.amount` | Planned amount | RW | sensitive (Q-A4 answered); separate from line coordinates (Q-A5 answered) | registered |
| `actual_ytd`, variance `actual`, `total_actual`, heatmap `cells[].actual`, `actual_total`, dashboard `used` | ledger actual against the plan | Computed | none | `finance.budget.actual` | Actual against the plan | RO | open; shown only if the matching statement section (revenue or expense lines) is readable | registered |
| `consumed_pct`, `variance`, `total_variance`, dashboard `pct`, `pct_of_plan`, `delta_pct`, heatmap colour class | share of plan used and the gap | Computed | none | `finance.budget.variance` | Variance and share used | RO | open; shown only if `amount` and `actual` are readable (a ratio plus one side reveals the other) | registered |
| `branch_id`, `branch_name`, create `branch` | branch the plan belongs to | Stored, create only | create `branch` | - | - | - | - | no switch: owner decision |
| `can_manage` | permission metadata, recomputed false for a field the caller may not write | Computed | none | - | - | - | - | no switch: transport |
| `narrowed`, `filing.branches[]`, `next`, `previous`, `count`, `results` | reach flag, branch choices, pagination | Transport | none | - | - | - | - | no switch: transport |
| `rows[].account_id`, `rows[].code`, `rows[].name`, `rows[].account_type`, `periods[]`, `rows[].cells[].period_no`, `period_no`, roll-up `fiscal_year` | chart labels, calendar labels, window echoes | Computed | none | - | - | - | - | no switch: identifier |

A variance row is dropped together with the figures it labels. The heatmap is
coloured by actual over budget, so a hidden `amount` or `actual` removes the
cell, not just the number.

### 3.2 Financial statements (`finance.report`)

All views are GET, guarded by `finance.report.view`, take `?entity=` and (except
the pack, aging and reconciliation) `?period=`. Routes under `/finance/reports/`:
`trial-balance/`, `income-statement/`, `balance-sheet/`, `cash-flow/`,
`analytics-slice/`, `changes-in-equity/`, `statutory-pack/`, `ar-aging/`,
`ar-reconciliation/` (views in `vs_finance/views.py`, builders in
`vs_finance/reports.py`). Frontend: `FinPro/src/pages/finance/reports/`
(`trial-balance-tab.tsx`, `income-statement-tab.tsx`, `balance-sheet-tab.tsx`,
`cash-flow-tab.tsx`, `equity-tab.tsx`, `analytics-slice-tab.tsx`,
`statutory-pack-tab.tsx`). The AR aging report has no screen today and is
reachable by API and by export. Money values are `{kobo, naira}` pairs. All rows
are computed with no write path. The grouping is settled (Q-A1): one switch per
statement section or column.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| trial balance `rows[].debit`, `rows[].credit`, `total_debit`, `total_credit`, `is_balanced` | trial balance figures and its footing check | Computed | none | `finance.report.trial_balance` | Trial balance | RO | open | registered |
| income statement `income[].amount`, `totals.income.amount` | revenue lines | Computed | none | `finance.report.income_revenue` | Revenue lines | RO | open | registered |
| income statement `expense[].amount`, `totals.expense.amount` | expense lines | Computed | none | `finance.report.income_expense` | Expense lines | RO | open | registered |
| income statement `totals.net.amount` | net income | Computed | none | `finance.report.income_net` | Net income | RO | open; shown only if revenue and expense lines are readable | registered |
| income statement `budget`, `variance`, `has_budget` on lines and totals | plan columns, roll-up of branch plans | Computed | none | `finance.report.income_budget` | Budget and variance columns | RO | open; shown only if `finance.budget.amount` is readable | registered |
| income statement `prior_year`, `has_prior_year`, `prior_fiscal_year` | prior-year column | Computed | none | `finance.report.income_prior_year` | Prior-year column | RO | open; also needs the matching revenue or expense switch | registered |
| balance sheet sections `non_current_assets`, `current_assets` (`total`, `groups[].amount`, `accounts[].amount`), `total_assets` | assets | Computed | none | `finance.report.balance_assets` | Assets | RO | open | registered |
| balance sheet sections `non_current_liabilities`, `current_liabilities`, `total_liabilities` | liabilities | Computed | none | `finance.report.balance_liabilities` | Liabilities | RO | open | registered |
| balance sheet section `equity`, `total_equity`, top-level `retained_earnings` (carries `current_year_earnings`) | equity and current-year earnings | Computed | none | `finance.report.balance_equity` | Equity and current-year earnings | RO | open; both keys go together | registered |
| balance sheet `is_balanced`, `difference` | whether the sheet balances | Computed | none | `finance.report.balance_check` | Whether the balance sheet balances | RO | open; shown only if assets, liabilities and equity are readable | registered |
| cash flow `opening_cash`, `closing_cash` | opening and closing cash | Computed | none | `finance.report.cashflow_cash` | Opening and closing cash | RO | open | registered |
| cash flow `by_activity.{operating,investing,financing}`, `activity_lines.*[].amount`, `net_change`, `is_reconciled` | cash by activity | Computed | none | `finance.report.cashflow_activities` | Cash by activity | RO | open; `is_reconciled` needs both cash switches | registered |
| changes in equity `columns[].opening`, `profit`, `contributions`, `transfers`, `closing`, `total_*`, `balance_sheet_equity`, `is_reconciled` | changes in equity | Computed | none | `finance.report.equity_changes` | Changes in equity | RO | open; the synthetic column's profit and transfers need `income_net` | registered |
| analytics slice `rows[].debit`, `credit`, `net`, `bucket_totals`, `total_net` | cost and dimension analysis | Computed | none | `finance.report.analytics_slice` | Cost and dimension analysis | RO | open (Q-A7 answered); a cost centre with one salaried person may reveal that person's pay, so the slice has its own switch | registered |
| AR aging `rows[].buckets.*`, `outstanding`, `unallocated_credit`, `net` | amounts owed by each customer, by age | Computed | none | `finance.report.aging_customers` | Amounts owed by each customer | RO | open (Q-A3); a row is dropped, not blanked, when closed | registered |
| AR aging `bucket_totals.*`, `total_net` | receivables by age, totals | Computed | none | `finance.report.aging_totals` | Receivables by age, totals | RO | open (Q-A3) | registered |
| AR reconciliation `subledger_total`, `control_total`, `difference`, `is_reconciled` | receivables sub-ledger against control account | Computed | none | `finance.report.ar_reconciliation` | Receivables reconciliation | RO | open | registered |
| statutory pack: every figure of the four statements and the trial balance | the filing pack, whole school only | Computed | none | - | - | - | follows the section switches above; a pack with a closed section is refused (Q-A2) | no switch: owner decision |
| every report `entity`, `narrowed`, `period`, `period_label`, `fiscal_year`, `as_of`, `headings`, `axis`, `rows[].account_id`, `code`, `name`, `account_type`, `rows[].customer_id`, `code`, `name`, section, group and component labels, `rows[].bucket`, export subtitle | context, window echoes and reference labels | Computed | none | - | - | - | - | no switch: identifier |

The AR aging switches are the single decision for per-customer aging wherever it
appears: the aging report, the `aging` block of the customer statement and the
bucket figures of the dunning summary (section 4.1).

---

## 4. Area B: receivables documents, ledger and banking documents

Resources in this area: customers (`finance.customer`, registered), invoices,
credit notes, refunds, concessions, write-offs, credit transfers, payment plans,
chart of accounts, cost centres, journals, and the statements and
reconciliations of bank accounts (extending `finance.bankaccount`, registered).
Every route is mounted under `/finance/`. The statement and reconciliation
routes are guarded by `finance.bankaccount.view`, `.import` and `.reconcile`, so
they extend that resource; there is no `finance.bankstatement`.

Frontend for the whole area: `FinPro/src/pages/finance/receivables/`
(`customers-tab.tsx`, `customer-detail-drawer.tsx`, `new-customer-drawer.tsx`,
`invoices-tab.tsx`, `invoice-detail-drawer.tsx`, `new-invoice-drawer.tsx`,
`credit-notes-tab.tsx`, `refunds-tab.tsx`, `concessions-tab.tsx`,
`credit-transfers-tab.tsx`, `payment-plans-tab.tsx`, `batch-adjustment-drawer.tsx`,
`income-given-back.tsx`), `FinPro/src/pages/finance/setup/accounts-tab.tsx`,
`cost-centers-tab.tsx`, `FinPro/src/pages/finance/ledger/`, `banking.tsx`,
`bank-reconciliation.tsx`, with API and types in
`FinPro/src/redux/services/finance/` (`ar-api.ts`, `ar-types.ts`, `gl-api.ts`,
`gl-types.ts`, `setup-api.ts`, `ops-api.ts`, `ops-types.ts`).

### 4.1 Customers (`finance.customer`, registered)

Routes: `customers/` (GET `.view`, POST `.create`), `customers/summary/`,
`customers/<pk>/` (GET, PATCH `.update`), `customers/opening/`
(`finance.customer.import_opening`), `customers/<pk>/statement-email/`
(`.email_statement`), `reports/customer-statement/` (`finance.report.view`),
`refunds/availability/`. Serializers: `CustomerSerializer`,
`CustomerListSerializer`, `CustomerAccountSerializer`,
`CustomerStandingSerializer`, `CustomerSummaryTotalsSerializer`,
`FinanceDocumentDeliverySerializer`, `DocumentEmailPreviewSerializer`. The school
to finance synchronisation (`ensure_customer`, `platform_customer.py`) writes
contact fields and `branch` as a system write and stays outside `assert_writable`.

The master record is registered. The table lists it for context and for the
decisions that change; the statement rows are registered already, and the aging
row uses the Area A report switch.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | record id | Transport | none | - | - | - | - | no switch: transport |
| `code`, `name`, `customer_code`, `customer_name` | customer identity | Stored | create; PATCH `name` | - | - | - | - | no switch: owner decision |
| `billing_email` (also `recipients`, `to`), `billing_phone`, `billing_address` | contact details | Stored | create, PATCH | `finance.customer.billing_email`, `billing_phone`, `billing_address` | Billing email, phone, address | RW | open (contact details open by default); the address is open on create (Q-B10 answered) | registered |
| `receivable_account_id`, `receivable_account_code`, `receivable_account_name` | control account | Stored | create, PATCH (fixed after activity) | `finance.customer.receivable_account` | Receivable account | RW | open | registered |
| `opening_balance`, `opening_balance_naira` | opening position | Stored, create only | create (with `opening_date`, `opening_branch`) | `finance.customer.opening_balance` | Opening balance | RW (open on create) | open | registered |
| `source_type`, `source_id` | originating record kind and id | Stored | create, PATCH (fixed after activity) | `finance.customer.source_type`, `source_id` | Source | RW | sensitive; `source_id` is exported as "Customer tax ID" (Q-B4) | registered |
| `is_active` | active flag | Stored | create, PATCH | `finance.customer.is_active` | Active | RW | open | registered |
| `branch_id`, `branch_name` | customer's branch, null for shared | Stored, create only | create `branch` | - | - | - | The existing `finance.customer.branch` switch is removed | no switch: owner decision |
| `created_at` | creation date | Computed | none | `finance.customer.created_at` | Created | RO | open | registered |
| `balance`, `balance_naira` (also `current_balance`, `receivable`, `on_credit`, statement `opening_balance`, `closing_balance`, `total_debits`, `total_credits`, `entries[].balance`, refund availability `refundable_credit`, AR adjustments `kpis.refundable_credit`, credit note `credit_remaining`) | signed outstanding minus credit, scoped to the reader's branches | Computed | none | `finance.customer.balance` | Balance | RO | open (Q-B4 answered) | registered |
| `account_status` (also `overdue`, `status_counts`), `pays_for_count` | standing and payer count | Computed | none | `finance.customer.account_status`, `pays_for_count` | Account standing, Pays for | RO | open | registered |
| detail `lifetime_paid`, `open_invoice_count`, `open_invoices`, `open_debit_notes`, `transactions`, `statement` | account detail blocks | Computed | none | `finance.customer.lifetime_paid`, `open_invoice_count`, `open_invoices`, `open_debit_notes`, `transactions`, `statement` | as labelled in the registry | RO | open; where a block restates an invoice or receipt total the stricter switch wins | registered |
| customer statement `aging.*` (per-customer buckets) | the customer's age analysis | Computed | none | `finance.report.aging_customers` | Amounts owed by each customer | RO | open (Q-A3); shown only if `balance` is also readable | registered in area A |
| customer statement `entries[]` (`date`, `doc_type`, `description`, `debit`, `credit`) and its export columns | statement of account lines | Computed | none | `finance.customer.statement` | Statement of account | RO | open | registered |

`entries[].document_number`, `transactions[].reference` and
`statement[].description` name document numbers; document numbers are
identifiers, so no filter follows them.

### 4.2 Invoices (`finance.invoice`)

Routes: `invoices/` (GET `.view`, POST `.create`), `invoices/summary/`,
`invoices/<pk>/`, `invoices/<pk>/document/` (printed, system surface),
`invoices/<pk>/pay/` (`finance.payment.create`), `invoices/<pk>/remind/`
(`finance.dunning.send`), `invoices/<pk>/revoke-pay-link/` (`.update`),
`invoices/<pk>/email/` (`.email`), `invoices/<pk>/void/` (`.reverse`),
`invoices/<pk>/write-off/`, `fee-structures/<pk>/generate/` (fee run),
`customers/opening/` (opening import). `InvoiceSerializer` is reused by create,
void, pay, write-off, fee run and opening import. There is no invoice PATCH.
Create body: `customer`, `beneficiary`, `invoice_date`, `due_date`, `currency`,
`reference`, `narration`, `branch`, `post`, `lines[]`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `customer_id`, `invoice_id` | ids | Transport | create `customer` | - | - | - | - | no switch: transport |
| `document_number`, `customer_code`, `customer_name`, `branch_id`, `branch_name`, create `branch` | invoice number, customer and branch identity | Computed or stored | create, fee run, opening import | - | - | - | - | no switch: owner decision |
| `beneficiary_id`, `beneficiary_code`, `beneficiary_name` | second customer the bill is for (a child billed to a sponsor) | Stored | create `beneficiary` | `finance.invoice.beneficiary` | Beneficiary (who the bill is for) | RW (open on create) | open | registered |
| `invoice_date` | invoice date | Stored | create, fee run, opening import | `finance.invoice.invoice_date` | Invoice date | RW (open on create) | open | registered |
| `due_date`, `summary.due_date` | due date | Stored | create, fee run, opening import | `finance.invoice.due_date` | Due date | RW (open on create) | open | registered |
| `status`, `payment_status`, `screen_tab` | state of the invoice | Computed | post, void, pay actions | `finance.invoice.status` | Status | RO | open | registered |
| `subtotal`, `tax_total`, `total`, `total_naira`, `summary.subtotal`, `summary.tax`, `summary.total` | amounts from the lines | Computed | none | `finance.invoice.total` | Invoice amounts | RO | open | registered |
| `amount_paid`, `amount_credited`, `settled_amount`, `balance_due`, `summary.paid`, `credited`, `settled`, `balance` | paid, credited and owed | Computed | none | `finance.invoice.settlement` | Paid, credited and balance due | RO | open; shown only if `total` is readable | registered |
| `reference` | free-text reference | Stored | create | `finance.invoice.reference` | Reference | RW | open | registered |
| `narration` | free-text narration | Stored | create | `finance.invoice.narration` | Narration | RW | open | registered |
| `billing_period`, `billing_period_label` | period set by the fee run | Computed | fee run | `finance.invoice.billing_period` | Billing period | RO | open | registered |
| `currency` (create only, never returned) | invoice currency | Stored | create | `finance.invoice.currency` | Currency | RW | open; register only if it is ever returned | registered |
| `post` (create only) | action flag | Transport | create | - | - | - | - | no switch: transport |
| detail `lines[]`: `description`, `account_code`, `account_name`, `quantity`, `tax_code`, `kind`, `service_start`, `service_end` | what is billed | Stored | create `lines` | `finance.invoice.lines` | Invoice lines | RW | open | registered |
| detail `lines[].unit_price` | price of one unit | Stored | create `lines` | `finance.invoice.unit_price` | Unit price | RW | open (own switch by decision) | registered |
| detail `lines[].tax_amount`, `lines[].line_total` | line amounts | Computed | none | `finance.invoice.total` | Invoice amounts | RO | open | registered |
| detail `deferred_income` | pending and released deferred income | Computed | none | `finance.deferredincome` fields | as registered | RO | open | registered |
| detail `payments[]`, `settlements[]` (`type`, `date`, `reference`, `method`, `amount`) | receipts, credit notes, concessions and write-offs applied | Computed | none | `finance.invoice.settlements` | Payments, credits and write-offs applied | RO | open; the stricter of this and the receipt switches (section 7.1) wins | registered |
| detail `gl_postings[]`, `gl_journals[]` | ledger lines of the invoice and its settlements | Computed | none | `finance.invoice.postings` | Ledger postings | RO | open; account code and name are chart labels | registered |
| detail `reminders[]` | reminders sent | Computed | none | `finance.invoice.reminders` | Reminders sent | RO | open | registered |
| detail `activity[]` | timeline; labels embed document numbers and amounts | Computed | none | `finance.invoice.activity` | Activity timeline | RO | open; built from the readable parts only | registered |
| summary `kpis` (`total_invoiced`, `total_collected`, `collection_rate`, `overdue_balance`), `totals` (`total`, `outstanding`), `monthly[]` (`invoiced`, `collected`) | list-screen figures | Computed | none | `finance.invoice.summary` | Invoice summary figures | RO | open; collected figures also need the receipt amount switch | registered |
| summary `by_status`, `totals.count` | counts of documents the reader may list | Computed | none | - | - | - | - | no switch: transport |

Messages "Invoice INV-0042 posted" and "Receipt R-0004 recorded against
INV-0042" name numbers and stay as they are. The printed invoice, the emailed
invoice and the public pay page are system surfaces.

### 4.3 Credit and debit notes (`finance.creditnote`)

Routes: `credit-notes/` (GET `.view`, POST `.create`), `credit-notes/<pk>/`
(GET, PATCH `.update`), and the `submit`, `post` (`.post`), `allocate`
(`.allocate`) and `void` (`.reverse`) actions. Serializers: `CreditNoteSerializer`,
`CreditNoteLineSerializer`. Create body: `customer`, `invoice`, `kind`,
`note_date`, `currency`, `reason`, `reference`, `branch`, `lines[]`. `customer`,
`invoice` and `kind` are fixed after create.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `customer_id`, `invoice_id` | ids | Transport | create | - | - | - | - | no switch: transport |
| `document_number`, `customer_code`, `customer_name`, create `branch` | identity | Computed or stored | create | - | - | - | - | no switch: owner decision |
| `kind` | credit or debit note | Stored | create | `finance.creditnote.kind` | Credit or debit note | RW (open on create) | open | registered |
| `invoice_id`, `invoice_number` | invoice adjusted | Stored | create `invoice` | `finance.creditnote.invoice` | Invoice adjusted | RW (open on create) | open | registered |
| `note_date` | note date | Stored | create, PATCH | `finance.creditnote.note_date` | Note date | RW | open | registered |
| `status`, `approval_state`, `approval_returned`, `approval_required` | state and approval words | Computed | submit, post, void actions | `finance.creditnote.status` | Status | RO | open | registered |
| `subtotal`, `tax_total`, `total`, `total_naira` | amounts | Computed | none | `finance.creditnote.total` | Credit note amounts | RO | open | registered |
| `allocated_amount`, `unallocated_amount`, `refunded_amount`, `transferred_amount`, `credit_remaining` | how the credit has been used | Computed | allocate action | `finance.creditnote.credit_use` | How the credit has been used | RO | open; `credit_remaining` also needs `finance.customer.balance` | registered |
| `reason` | reason for the note | Stored | create, PATCH | `finance.creditnote.reason` | Reason | RW | open | registered |
| `reference` | reference | Stored | create, PATCH | `finance.creditnote.reference` | Reference | RW | open | registered |
| `lines[]` (`line_no`, `description`, `revenue_account`, `quantity`, `tax_code`, `net_amount`, `tax_amount`, `cost_center`) | what is credited | Stored | create, PATCH | `finance.creditnote.lines` | Credited lines | RW | open | registered |
| `lines[].unit_price` | price of one unit | Stored | create, PATCH | `finance.creditnote.unit_price` | Unit price | RW | open (own switch by decision) | registered |
| `income_given_back[]` (`id`, `to_branch_id`, `to_branch_name`, `amount`, `status`; also on concessions) | inter-branch transfers that gave the income back | Computed | none | `finance.creditnote.income_given_back` | Income given back to other branches | RO | open; each row also passes `finance.interbranch` `amount` and `status` (stricter wins) | registered |
| `income_given_back[].document_number` | transfer number | Computed | none | - | - | - | - | no switch: owner decision |

### 4.4 Refunds (`finance.refund`)

Routes: `refunds/` (GET `.view`, POST `.create`), `refunds/<pk>/` and the
`submit`, `post`, `void` actions, `refunds/availability/` (`.create`),
`ar-adjustments/` (`finance.refund.view` or `finance.writeoff.view`),
`ar-adjustments/batch/` (creates refunds and write-offs in bulk; an alternate
write path that goes through the same checks). Serializer: `RefundSerializer`.
Create body: `customer`, `branch`, `bank_account`, `refund_date`, `method`,
`amount`, `currency`, `reference`, `narration`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `customer_id` | ids | Transport | create | - | - | - | - | no switch: transport |
| `document_number`, `customer_code`, `customer_name`, create `branch`, availability `branch_id`, `branch_name` | identity | Computed or stored | create, batch | - | - | - | - | no switch: owner decision |
| `refund_date` | refund date | Stored | create, batch | `finance.refund.refund_date` | Refund date | RW (open on create) | open | registered |
| `method` | payment method | Stored | create, batch | `finance.refund.method` | Refund method | RW | open | registered |
| `status`, `approval_state`, `approval_returned`, `approval_required` | state and approval words | Computed | submit, post, void | `finance.refund.status` | Status | RO | open | registered |
| `amount`, `amount_naira` | amount refunded | Stored | create, batch | `finance.refund.amount` | Refund amount | RW | open; restates customer credit (Q-B4) | registered |
| `bank_account_id` | bank account the refund is paid from | Stored | create, batch | `finance.refund.bank_account` | Paid from bank account | RW | open (Q-C4 answered) | registered |
| `reference`, `narration` | free text | Stored | create, batch | `finance.refund.reference`, `finance.refund.narration` | Reference, Narration | RW | open | registered |
| `currency` (create only, never returned) | currency | Stored | create | - | - | - | - | no switch: transport |
| availability `refundable_credit`, `refundable_credit_naira` | credit per customer and branch | Computed | none | `finance.customer.balance` | Balance | RO | the row is dropped when `balance` is hidden | registered |
| `ar-adjustments/` `rows[]` (`kind`, `reference`, `date`, `reason`, `amount`, `amount_naira`, `status`, `refund_id`, `write_off_id`, `approval_*`), `kinds` | unified refund and write-off list | Computed | none | `finance.refund.*` and `finance.writeoff.*` fields of the row's kind | as the document | RO | rows follow their document switches; `kpis.written_off_ytd`, `pending`, `sent_back` follow the amount and status switches | registered |

### 4.5 Concessions, write-offs and credit transfers (same pattern)

Serializers: `ConcessionSerializer`, `WriteOffRequestSerializer`,
`CustomerCreditTransferSerializer`. Each follows the invoice rules: typed values
are RW, derived values are RO, document numbers, customer identity and branch are
not switched. Money is open. Approval words travel with the status field.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| concession `kind` | waiver kind | Stored | create | `finance.concession.kind` | Concession kind | RW | open | registered |
| concession `invoice` | invoice the concession applies to | Stored | create | `finance.concession.invoice` | Invoice | RW | open | registered |
| concession `concession_date` | date | Stored | create | `finance.concession.concession_date` | Concession date | RW | open | registered |
| concession `amount` | amount waived | Stored | create | `finance.concession.amount` | Amount | RW | open | registered |
| concession `allowance_account` | account the waiver is charged to | Stored | create | `finance.concession.allowance_account` | Allowance account | RW | open | registered |
| concession `reason`, `reference` | free text | Stored | create | `finance.concession.reason`, `finance.concession.reference` | Reason, Reference | RW | open | registered |
| concession `status`, `approval_*`, `income_given_back` | state, approval words, inter-branch rows | Computed | actions | `finance.concession.status`, `finance.concession.income_given_back` | Status, Income given back | RO | open | registered |
| write-off `invoice` | invoice written off | Stored | create | `finance.writeoff.invoice` | Invoice | RW | open | registered |
| write-off `amount` | amount | Stored | create | `finance.writeoff.amount` | Amount | RW | open | registered |
| write-off `write_off_account` | account charged | Stored | create | `finance.writeoff.write_off_account` | Write-off account | RW | open | registered |
| write-off `write_off_date` | date | Stored | create | `finance.writeoff.write_off_date` | Write-off date | RW | open | registered |
| write-off `narration`, `reason` | free text | Stored | create | `finance.writeoff.narration`, `finance.writeoff.reason` | Narration, Reason | RW | open | registered |
| write-off `status`, `approval_*`, `journal_id`, `allowance_used`, `recovered_amount` | state, ledger link and recovery | Computed | actions | `finance.writeoff.status`, `finance.writeoff.journal`, `finance.writeoff.recovery` | Status, Journal, Recovery | RO | open | registered |
| credit transfer `from_customer`, `to_customer` | customers the credit moves between | Stored | create | `finance.credittransfer.customers` | From and to customer | RW | open | registered |
| credit transfer `transfer_date`, `amount`, `reason` | typed values | Stored | create | `finance.credittransfer.transfer_date`, `finance.credittransfer.amount`, `finance.credittransfer.reason` | Transfer date, Amount, Reason | RW | open | registered |
| credit transfer `status`, `approval_*`, `receipt_id`, `receipt_number` | state and receipt link | Computed | actions | `finance.credittransfer.status`, `finance.credittransfer.receipt` | Status, Receipt | RO | open | registered |
| credit transfer `branch_id` | branch | Stored | create | - | - | - | - | no switch: owner decision |

---

### 4.6 Payment plans (`finance.paymentplan`)

Routes: `payment-plans/` (GET `.view`, POST `.create`), `payment-plans/<pk>/`
(`.view`), and the `activate` and `refresh` (`.activate`) and `cancel`
(`.cancel`) actions. Serializers: `PaymentPlanSerializer`,
`PaymentPlanInstallmentSerializer`; all five responses use the same payload.
Create body: `customer`, `invoice` (optional), `start_date`, `frequency`,
`installment_count`, `total_amount`, `notes`, `amounts[]`, `branch`. Refresh body:
`settled_amount`. Frontend: `FinPro/src/pages/finance/receivables/payment-plans-tab.tsx`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `customer_id`, `invoice_id` | ids | Transport | create | - | - | - | - | no switch: transport |
| `document_number`, `customer_code`, `customer_name`, `branch_id`, `branch_name`, create `branch` | identity | Computed or stored | create | - | - | - | - | no switch: owner decision |
| `invoice_id`, `invoice_number` | invoice the plan spreads | Stored | create `invoice` | `finance.paymentplan.invoice` | Invoice spread | RW (open on create) | open | registered |
| `plan_status` | state of the plan | Computed | activate, refresh, cancel | `finance.paymentplan.status` | Status | RO | open | registered |
| `start_date` | first instalment date | Stored | create | `finance.paymentplan.start_date` | Start date | RW (open on create) | open | registered |
| `frequency` | spacing | Stored | create | `finance.paymentplan.frequency` | Frequency | RW (open on create) | open | registered |
| `installment_count` | number of instalments | Stored | create | `finance.paymentplan.installment_count` | Number of instalments | RW (open on create) | open | registered |
| `total_amount`, `total_naira` | amount spread | Stored | create | `finance.paymentplan.total_amount` | Amount spread | RW | open | registered |
| `baseline_settled`, `scheduled_total`, `settled_total`, `outstanding_total`, `installments[].amount_settled`, `installments[].balance`, `installments[].status` | paid so far and outstanding | Computed | refresh action | `finance.paymentplan.progress` | Paid so far and outstanding | RO | open; the stricter of this and `finance.invoice.settlement` wins | registered |
| `notes` | free text | Stored | create | `finance.paymentplan.notes` | Notes | RW | open | registered |
| `installments[]` (`id`, `seq_no`, `due_date`, `amount`) | the schedule; amounts typed through create `amounts[]` | Stored | create `amounts` | `finance.paymentplan.installments` | Instalment schedule | RW | open | registered |
| refresh body `settled_amount` | settled amount applied on refresh | Stored | refresh action | `finance.paymentplan.settled_amount` | Settled amount applied on refresh | RW | open; asserted in `PaymentPlanRefreshView` | registered |

### 4.7 Chart of accounts (`finance.account`)

Routes: `accounts/` (GET open to any finance member, `?with_balance=true` needs
`finance.account.view`, POST `.create` with whole-tenant reach),
`accounts/<pk>/` (GET `.view`, PATCH `.update`), `accounts/<pk>/activity/`
(`.view`). Serializer: `AccountSerializer`. Create body: `code`, `name`, `parent`,
`is_contra`, `is_postable`, `subtype`, `description`. PATCH body: `name`,
`subtype`, `description`, `is_active`, `is_postable`. Frontend:
`FinPro/src/pages/finance/setup/accounts-tab.tsx`, `account-detail-tabs.ts`,
`FinPro/src/components/finance-ui/pickers.tsx`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `parent_id` | ids | Transport | create | - | - | - | - | no switch: transport |
| `code`, `name` | account identity | Stored | create; PATCH `name` | - | - | - | - | no switch: identifier |
| `account_type`, `normal_balance`, `type_label` | structural classification derived from the code and contra flag | Computed | none | - | - | - | - | no switch: identifier |
| `is_contra` | contra account | Stored, create only | create | `finance.account.is_contra` | Contra account | RW (open on create) | open | registered |
| `is_postable` | accepts postings | Stored | create, PATCH | `finance.account.is_postable` | Accepts postings | RW (open on create) | open | registered |
| `is_active` | active flag | Stored | PATCH | `finance.account.is_active` | Active | RW | open | registered |
| `parent_id`, `parent_code` | parent account | Stored | create `parent` | `finance.account.parent` | Parent account | RW (open on create) | open | registered |
| `subtype` | account subtype | Stored | create, PATCH | `finance.account.subtype` | Account subtype | RW | open | registered |
| `description` (write only, never returned) | free text | Stored | create, PATCH | `finance.account.description` | Description | RW | open; either return it and register, or document it as write-only | registered |
| `balance`, detail `summary.current_balance`, `summary.opening_balance`, activity `running_balance`, `totals.debit`, `totals.credit`, `totals.net_movement` | net ledger balance signed to the normal balance; a branch reader sees their branches' share | Computed | none | `finance.account.balance` | Account balance | RO | open (Q-B4); for a bank ledger account, and for a group account with any bank ledger account among its descendants, it also needs `finance.bankaccount.book_balance` (B-L11) | registered |
| `tag` (`CONTROL`, `CASH`, null) | sub-ledger role | Computed | none | `finance.account.tag` | Sub-ledger role | RO | open | registered |
| `bank_account_id` | the bank account behind a ledger account | Computed | none | `finance.bankaccount.ledger_account` | Ledger account | RW | stricter wins with the bank switch; a role that cannot read the link must not learn it from the chart | registered |
| `bank_branch_id` | branch of that bank account | Computed | none | - | - | - | stays open; the deposit picker narrows by it | no switch: owner decision |
| detail `summary.line_count`, `summary.journal_count`, `summary.fiscal_year_start`, `summary.as_of` | counts and window echoes | Computed | none | - | - | - | - | no switch: transport |
| detail `activity[]` (`date`, `journal_no`, `source`, `status`, `description`, `cost_center`, `dimensions`, `debit`, `credit`) | ledger activity | Computed | none | `finance.account.activity` | Ledger activity | RO | open; its money follows `balance` | registered |

### 4.8 Cost centres and setup data (`finance.costcenter`)

Route: `cost-centers/` (POST creates or updates by code, `finance.costcenter.create`;
GET open to any finance key or a procurement requisition or issue grant).
Serializer: `CostCenterSerializer`. Frontend: `FinPro/src/pages/finance/setup/cost-centers-tab.tsx`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `parent_id` | ids | Transport | create | - | - | - | - | no switch: transport |
| `code`, `name` | cost centre identity | Stored | POST | - | - | - | - | no switch: identifier |
| `parent_code` | parent cost centre | Stored | POST `parent` | `finance.costcenter.parent` | Parent cost centre | RW | open | registered |
| `is_active` | active flag | Stored | POST | `finance.costcenter.is_active` | Active | RW | open | registered |
| `DimensionSerializer` (`code`, `name`, `allowed_values`, `is_active`), `TaxCodeSerializer`, `CurrencySerializer`, `FxRateSerializer` | setup data with the same shape | Stored | their create and update endpoints | to be named when the setup-data resources register | - | RW | code and name get no switch; the rest get RW switches | registered |

### 4.9 Journals (`finance.journal`)

Routes: `journals/` (`finance.journal.view`), `journals/summary/`,
`journals/<id>/` (GET `.view`, PATCH `finance.directentry.post`),
`journals/<id>/submit/`, `post/`, `reverse/`, `direct-entries/`
(`finance.directentry.post`). Serializers: `JournalEntryListSerializer`,
`JournalEntryDetailSerializer`, `JournalLineSerializer`,
`DirectEntryCreateSerializer`. Every other journal is a side effect of a
document. `JournalEntryDetailSerializer` is also embedded in the fiscal-year
close result (`closing_journals`, `closing_journal`) and re-open result
(`reversals`). Frontend: `FinPro/src/pages/finance/ledger/`
(`journal-detail-drawer.tsx`, `direct-entry-drawer.tsx`, `direct-entry-edit.ts`).

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `reverses_id`, `created_by_id`, `workflow_instance_id`, `approval` block | ids and approval transport | Transport | none | - | - | - | - | no switch: transport |
| `document_number`, `reversal_action.document_number`, create `branch` | journal number, branch | Computed or stored | direct entry | - | - | - | - | no switch: owner decision |
| `date` | journal date | Stored | direct entry, PATCH, reverse `date` | `finance.journal.date` | Journal date | RW (open on create) | open | registered |
| `period`, `period_label` | accounting period | Computed | none | `finance.journal.period` | Accounting period | RO | open | registered |
| `source` | where the journal came from | Computed | none | `finance.journal.source` | Source | RO | open | registered |
| `status`, `approval_state`, `approval_returned`, `reversal_action.kind`, `document_type`, `document_id` | state and reversal link | Computed | submit, post, reverse | `finance.journal.status` | Status | RO | open | registered |
| `narration` | free text | Stored | direct entry, PATCH | `finance.journal.narration` | Narration | RW | open | registered |
| `reference` | free text | Stored | direct entry, PATCH | `finance.journal.reference` | Reference | RW | open | registered |
| `posted_at` | posting time | Computed | none | `finance.journal.posted_at` | Posted on | RO | open | registered |
| `total_debit`, `total_credit` | journal totals | Computed | none | `finance.journal.total` | Journal total | RO | open (Q-B4 answered) | registered |
| `created_by`, `created_by_is_exited` | person who raised it (a staff name) | Computed | none | `finance.journal.created_by` | Raised by | RO | open; personal data | registered |
| `lines[]` (`line_no`, `account_id`, `account_code`, `account_name`, `cost_center`, `dimensions`, `debit`, `credit`, `debit_naira`, `credit_naira`, `description`) | journal lines | Stored | direct entry, PATCH | `finance.journal.lines` | Journal lines | RW | open (Q-B5: one switch for the whole line) | registered |
| create `opening_balance` | marks the entry as an opening balance | Stored, create only | direct entry | `finance.journal.opening_balance` | Opening balance entry | RW (open on create) | open | registered |
| create `confirm_without_approval`, `reason` | action flags; `reason` is stored text on the audit row | Transport | direct entry | - | - | - | - | no switch: transport |
| summary `posted_total`, `reversed_total` | list-screen money | Computed | none | `finance.journal.summary` | Posted and reversed totals | RO | open | registered |
| summary `total`, `by_status`, `sent_back` | counts | Computed | none | - | - | - | - | no switch: transport |
| close result `closing_journals`, `closing_journal`, `reversals` | closing journals embedded in the fiscal-year result | Computed | none | `finance.fiscalyear.net_income` | Net result | RO | their P&L lines restate the registered net result (leak B-L10) | registered |

### 4.10 Bank accounts, statements and reconciliations (`finance.bankaccount`, registered)

Routes: `bank-accounts/` (GET `finance.bankaccount.view`, POST `.create`),
`bank-accounts/<pk>/` (GET, PATCH `.update`), `split-by-branch/` (registered),
`statement-lines/` (GET `.view`, POST `.import`), `statements/<statement_id>/`
(GET `.view`, PATCH `.import`), `statement-imports/` template and wizard
(`.import`), `auto-reconcile/`, `split-match/`, `reconcile/complete/` (`.reconcile`),
`book-lines/` (`.view`), and `statement-lines/<pk>/` match, match-group, adjust,
unmatch, ignore (`.reconcile`) and DELETE (`.import`). Serializers:
`BankAccountSerializer`, `BankAccountDetailSerializer`, `BankStatementSerializer`,
`BankStatementDetailSerializer`, `BankStatementLineSerializer`,
`BankReconciliationSerializer`. Frontend: `FinPro/src/pages/finance/banking.tsx`,
`bank-reconciliation.tsx`, `bank-split-drawer.tsx`.

The account itself is registered. Statements, statement lines, reconciliations
and book lines are not, and they restate the registered `book_balance`,
`statement_balance` and `unreconciled_diff` (leaks B-L4 to B-L7).

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, statement and line ids and links (`bank_account_id`, `statement_id`, `matched_line_id`, `adjusting_journal_id`, `group_line_ids`) | ids and links | Transport | none | - | - | - | - | no switch: transport |
| `name`, `gl_account_name` | bank account and ledger account names | Stored | create, PATCH | - | - | - | - | no switch: owner decision |
| `bank_name`, `account_number`, `currency`, `is_active`, `is_primary`, `is_primary_collection`, `gl_account`, `book_balance`, `unreconciled_count`, `last_reconciled_at`, `gateway_subaccount_*`, `settlement_bank_code`, `metrics.*`, `transactions[]` | the bank account | Stored or computed | create, PATCH, payments endpoint | `finance.bankaccount.*` | as registered | per field | `account_number` is the only sensitive field | registered |
| `branch_id` | bank account's branch | Stored, create only | create `branch` | - | - | - | The existing `finance.bankaccount.branch` switch is removed | no switch: owner decision |
| statement `statement_date` | statement date | Stored | import, PATCH | `finance.bankaccount.statement_date` | Statement date | RW | open | registered |
| statement `period_label` | statement period | Stored | import, PATCH | `finance.bankaccount.statement_period` | Statement period | RW | open | registered |
| statement `opening_balance`, `opening_balance_naira` | opening balance on the statement; the first statement's is the account's opening position | Stored | import, PATCH | `finance.bankaccount.statement_opening_balance` | Statement opening balance | RW | Write open; Read requires sensitive `statement_balance` (Q-B4 and Q-B8 answered) | registered |
| statement `closing_balance`, `closing_balance_naira` | closing balance on the statement; the latest statement's is the account's statement balance | Stored on import, recomputed on correction | import | `finance.bankaccount.statement_closing_balance` | Statement closing balance | RW | Write open; Read requires sensitive `statement_balance` (Q-B4 and Q-B8 answered) | registered |
| statement `status`, `status_display`, `can_edit`, `edit_block_reason`, `import_rollback` | state of the statement and why it is locked | Computed | none | `finance.bankaccount.statement_state` | Statement status | RO | open; the reason sentence goes neutral when hidden | registered |
| statement `line_count` | count of lines | Computed | none | - | - | - | - | no switch: transport |
| statement `lines[]`, line `txn_date`, `description`, `reference`, `amount`, `amount_naira`, `external_id` | the bank's own entries | Stored | JSON import, statement PATCH, DELETE | `finance.bankaccount.statement_lines` | Statement lines | RW | open | registered |
| line `status`, `match_source`, `match_source_display`, `matched_reference`, `reconciled_at`, `can_delete`, `delete_block_reason` | match state of a line | Computed | match, group match, split match, adjust, unmatch, ignore | `finance.bankaccount.line_state` | Match status of a line | RO | open | registered |
| adjust body `counter_account`, `counter_code`, `narration`, `posting_date` | adjustment booked from a statement line | Stored | adjust action | `finance.bankaccount.reconcile_adjustment` | Adjustment booked from a statement line | RW | open | registered |
| ignore body `ignored`, `reason`; auto-reconcile body `tolerance_days`, `group` | action inputs; defaults come from the banking settings | Transport | ignore, auto-reconcile | - | - | - | - | no switch: transport |
| reconciliation `status`, `status_display`, `matched_count`, `as_of_date`, `created_at`, `performed_by_name` | reconciliation history (also returned by `reconcile/complete/`) | Computed | complete action | `finance.bankaccount.reconciliation_history` | Reconciliation history | RO | open; `performed_by_name` is a staff name | registered |
| reconciliation `book_balance`, `statement_balance`, `difference` and naira twins | reconciliation figures | Computed | none | `finance.bankaccount.book_balance`, `finance.bankaccount.statement_balance`, `finance.bankaccount.unreconciled_diff` | as registered | RO | `difference` shown only if book and statement balances are readable | registered |
| book lines `id`, `date`, `description`, `reference`, `amount` | unmatched ledger lines of the account | Computed | none | `finance.bankaccount.book_lines` | Unmatched ledger lines | RO | open; a description may name a customer, which is an identifier | registered |
| import batch `domain_context` (`bank_account_name`, `statement_date`, `period_label`, `opening_balance`, `closing_balance`, `published_statement_id`) | bank statement upload summary under `import.batches` | Computed | none | `finance.bankaccount.statement_opening_balance`, `finance.bankaccount.statement_closing_balance` | as above | RO | the balances follow the statement switches (leak B-L7) | registered |

---

## 5. Area C: procurement

Only `procurement.vendor` and `procurement.vendor_contact` are registered. Every
other payload below is not: stock and transfers, supplier credit notes, goods
returns and receipts, vendor payments with withholding tax (WHT), requisitions,
RFQs and shared sourcing, quotations, purchase orders, vendor bills, contracts,
catalogue, categories, assessments, settings, reports, dashboards.

Routes are under `/v1/procurement/`, the entity comes from `?entity=`, and reach
is the branch of the document or store. The vendor portal routes (`public-rfq-*`,
token and verified session, no RBAC) show a vendor only its own submission and
sit outside Field Access. There is no WHT certificate anywhere in the backend:
WHT exists as three payment fields, a line in the approval summary and two audit
sentences. Frontend: `FinPro/src/pages/procurement/` (`inventory.tsx`,
`stock-locations.tsx`, `vendor-credit-notes.tsx`, `goods-receipts.tsx`,
`goods-return-drawer.tsx`, `vendor-payments.tsx`, `withholding-tax.ts`,
`requisitions.tsx`, `sourcing/`, `purchase-orders.tsx`, `vendor-invoices.tsx`,
`vendors/`, `contracts.tsx`, `analytics/`, `approvals.tsx`, `settings.tsx`,
`dashboard*.tsx`), API in `FinPro/src/redux/services/procurement/`.

Conventions for this area: document numbers, vendor code and name, branch id and
name, item code and name, store code and name, cost centre name, approval
workflow ids, `approval_returned`, `is_parked`, `*_is_exited` flags and
`created_at` on masters are not switched. `*_naira` display mirrors travel with
the integer they mirror. Stored money that a person types is open, with the two
sensitive exceptions in Q-C3.

### 5.1 Stock (`procurement.stock`)

Routes: `stock-locations/` (GET `.view`, POST `.create`),
`stock-locations/transfer-destinations/` (`.issue`), `stock-locations/<pk>/`,
`stock-balances/`, `stock-items/` (GET `.view`, POST `.create`),
`stock-items/summary/`, `stock-items/restock-requisition/`
(`procurement.requisition.create`), `stock-items/<pk>/` (GET `.view`, PATCH
`.update`), `stock-items/<pk>/issue/` and `transfer/` (`.issue`),
`stock-items/<pk>/adjust/` (`.adjust`), `stock-movements/`. Serializers:
`StockItemListSerializer`, `StockItemDetailSerializer`, `StockLocationSerializer`,
`StockBalanceSerializer`, `StockMovementSerializer`; the transfer response, the
destination row and the summary are hand-built (`views/stock.py`). List filters
`?needs_reorder=` and `?location=` select by a quantity and are refused with 403
when the quantity is unreadable. A transfer between two branches' stores hands
finance the value, narration, reference and date (`book_goods_transfer`).

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `stock_item_id`, `location_id`, `updated_at`, `created_at`, `grn_id` | ids and metadata | Transport | none | - | - | - | - | no switch: transport |
| item `code`, `name`, `stock_item_code`, `stock_item_name`, `location_code`, store `code`, `name`, destination row `code`, `name` | item and store identity | Stored | create; PATCH `name` | - | - | - | - | no switch: identifier |
| `branch_id`, `branch_name`, `to_location` branch labels | store's branch | Stored | create, PATCH `branch` | - | - | - | - | no switch: owner decision |
| item `description` | item description | Stored | create, PATCH | `procurement.stock.description` | Item description | RW | open | recorded |
| `unit_of_measure` | unit | Stored | create, PATCH | `procurement.stock.unit_of_measure` | Unit of measure | RW | open | recorded |
| `catalog_item_id`, `catalog_item_code` | catalogue link | Stored | create, PATCH `catalog_item` | `procurement.stock.catalog_item` | Catalogue item | RW | open | recorded |
| `inventory_account_id`, `inventory_code` | inventory ledger account | Stored | create, PATCH `inventory_account` | `procurement.stock.inventory_account` | Inventory account | RW | open | recorded |
| `default_expense_account_id`, `expense_code` | issue expense account | Stored | create, PATCH `default_expense_account` | `procurement.stock.default_expense_account` | Issue expense account | RW | open | recorded |
| `reorder_level` | reorder level | Stored | create, PATCH | `procurement.stock.reorder_level` | Reorder level | RW | open | recorded |
| `reorder_qty` | reorder quantity | Stored | create, PATCH | `procurement.stock.reorder_qty` | Reorder quantity | RW | open | recorded |
| `on_hand_qty` | quantity on hand | Computed | none (movements only) | `procurement.stock.on_hand_qty` | Quantity on hand | RO | open | recorded |
| `stock_value`, `stock_value_naira`, summary `total_value`, `total_value_naira` | stock value | Computed | none | `procurement.stock.stock_value` | Stock value | RO | open (Q-C2) | recorded |
| `unit_cost`, `unit_cost_naira` | moving-average cost | Computed | none | `procurement.stock.unit_cost` | Unit cost | RO | open (Q-C2) | recorded |
| `needs_reorder`, summary `low_stock`, `out_of_stock`, catalogue `stock_status` | reorder state | Computed | none | `procurement.stock.needs_reorder` | Needs reorder | RO | open; shown only if `on_hand_qty` and `reorder_level` are readable | recorded |
| `is_active` (item) | item active | Stored | create, PATCH | `procurement.stock.item_is_active` | Item active | RW | open | recorded |
| item detail `movements` | movement rows | Computed | none | - | - | - | rows follow the movement switches; the list is dropped if none is readable | recorded |
| item detail `activity` | item timeline; carries amounts and store codes in the message | Computed | none | `procurement.stock.item_activity` | Item activity | RO | open; must be narrowed to the caller's branches (leak C-L3) | recorded |
| store `description` | store description | Stored | create, PATCH | `procurement.stock.location_description` | Store description | RW | open | recorded |
| store `is_default` | default store | Stored | create, PATCH | `procurement.stock.location_is_default` | Default store | RW | open | recorded |
| store `is_active` | store active | Stored | create, PATCH | `procurement.stock.location_is_active` | Store active | RW | open | recorded |
| movement `movement_type` | issue, transfer, adjustment, receipt, return | Computed | set by the action | `procurement.stock.movement_type` | Movement kind | RO | open | recorded |
| movement `movement_date` | date | Stored | issue, transfer, adjust bodies | `procurement.stock.movement_date` | Movement date | RW | open | recorded |
| movement `quantity` (body `quantity`, `quantity_delta`) | quantity moved | Stored | issue, transfer, adjust | `procurement.stock.movement_quantity` | Movement quantity | RW | open | recorded |
| movement `value_amount`, `value_amount_naira` | cost times quantity | Computed | none | `procurement.stock.movement_value` | Movement value | RO | open (Q-C2); on a transfer leg between branches the stricter of this and `finance.interbranch.amount` wins | recorded |
| movement `balance_qty` | quantity after the movement | Computed | none | `procurement.stock.balance_qty` | Quantity after movement | RO | open | recorded |
| movement `balance_value`, `balance_value_naira` | stock value after the movement | Computed | none | `procurement.stock.balance_value` | Stock value after movement | RO | open (Q-C2) | recorded |
| movement `journal_id`, `grn_id` | ledger and receipt links | Computed | none | `procurement.stock.ledger_links` | Ledger and receipt links | RO | open; on a transfer leg the stricter of this and `finance.interbranch.journals` wins | recorded |
| movement `reference` | reference | Stored | issue, transfer, adjust | `procurement.stock.movement_reference` | Reference | RW | open; on a transfer it is copied into `finance.interbranch.reference` | recorded |
| movement `narration` | narration | Stored | issue, transfer, adjust | `procurement.stock.movement_narration` | Narration | RW | open; on a transfer it becomes `finance.interbranch.purpose` | recorded |
| movement `cost_center_id`, `cost_center_name` | cost centre issued to | Stored | issue `cost_center` | `procurement.stock.movement_cost_center` | Issued to cost centre | RW | open | recorded |
| adjust body `unit_cost` (input only) | cost of stock found in a count | Stored | adjust | `procurement.stock.adjust_unit_cost` | Cost of written-up stock | RW | open; write-only, kept apart from the read-only `unit_cost` | recorded |
| movement `created_by_name`, `created_at` | who recorded it | Computed | none | `procurement.stock.movement_recorded_by` | Recorded by | RO | open | recorded |
| transfer response `movement` | sending-side leg | Computed | none | - | - | - | rendered through the movement switches | recorded |
| transfer response `received` | receiving-side leg with that store's balance | Computed | none | `procurement.stock.transfer_received` | Receiving store movement | RO | open; returned only to a caller who can read the receiving store (leak C-L1) | recorded |
| transfer response `inter_branch_transfer_id` | link to the finance document | Computed | none | `procurement.stock.transfer_document` | Inter-branch document | RO | shown only if a `finance.interbranch` field is readable | recorded |
| transfer body `to_location` | destination store | Stored | transfer | `procurement.stock.transfer_destination` | Destination store | RW | open | recorded |
| summary `tracked`, `active` | counts | Computed | none | - | - | - | - | no switch: transport |

### 5.2 Supplier credit notes (`procurement.vendor_credit_note`)

Routes: `vendor-credit-notes/` (GET `.view`, POST `.create`),
`vendor-credit-notes/<pk>/` (GET, PATCH `.update`, including the returned-document
correction path), `submit/`, `post/`, `allocate/` (`.allocate`), `void/`
(`.reverse`). Serializers: `VendorCreditNoteSerializer`,
`VendorCreditNoteListSerializer`, detail overlay `_serialize_note_detail`
(`views/corrections.py`). Create body: `vendor_invoice`, `note_date`, `reason`,
`vendor_reference`, and exactly one of `full`, `amount`, `lines[]`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `workflow_instance_id`, `approval_returned`, `lines[].id`, `lines[].line_no` | ids and workflow metadata | Transport | none | - | - | - | - | no switch: transport |
| `document_number`, `branch_id`, `branch_name`, `vendor_id`, `vendor_code`, `vendor_name` | identity, inherited from the bill | Computed | none | - | - | - | - | no switch: owner decision |
| `status`, `approval_state` | state | Computed | actions | `procurement.vendor_credit_note.status` | Status | RO | open | recorded |
| `vendor_invoice_id`, `vendor_invoice_number` | bill credited | Stored, create only | create `vendor_invoice` | `procurement.vendor_credit_note.vendor_invoice` | Bill credited | RW (open on create) | open | recorded |
| `note_date` | note date | Stored | create, PATCH | `procurement.vendor_credit_note.note_date` | Credit note date | RW | open | recorded |
| `vendor_reference` | supplier's credit memo number | Stored | create, PATCH | `procurement.vendor_credit_note.vendor_reference` | Supplier's credit memo number | RW | open | recorded |
| `reason` (also the approval queue title) | free text | Stored | create, PATCH | `procurement.vendor_credit_note.reason` | Reason | RW | open | recorded |
| `subtotal`, `tax_total`, `total` | credit amounts | Computed | none | `procurement.vendor_credit_note.amounts` | Credit amounts | RO | open (Q-C3) | recorded |
| `allocated_amount`, `advance_remaining` | applied and remaining credit | Computed | allocate action | `procurement.vendor_credit_note.settlement` | Applied and remaining credit | RO | open; `advance_remaining` shown only if total and allocated are readable | recorded |
| `journal_id` | ledger link | Computed | none | `procurement.vendor_credit_note.journal` | Journal link | RO | open | recorded |
| `lines[]` (`invoice_line_id`, `description`, `quantity`, `net_amount`, `tax_amount`); create `lines`, `amount`, `full` | credited lines | Stored | create, PATCH | `procurement.vendor_credit_note.credit_lines` | Credited lines | RW | open; one switch for the set | recorded |
| `allocations[]` (`vendor_invoice_id`, `amount`, `effective_date`) | where the credit went | Computed | allocate action | `procurement.vendor_credit_note.applications` | Where the credit went | RO | open | recorded |

### 5.3 Goods receipts and goods returns (`procurement.goods_receipt`)

A goods return has no list or detail route; it is created by reversing a receipt
(`goods-receipts/<pk>/reverse/`, `.reverse`) and read inside the receipt as
`returns`. Receipts: `goods-receipts/` (GET `.view`, POST `.create`),
`goods-receipts/<pk>/` (GET, PATCH `.update`), `post/` (`.post`). Serializers:
`GoodsReceivedNoteSerializer`, `GoodsReceivedNoteListSerializer`,
`GRNLineSerializer`, `GoodsReturnSerializer`, `GoodsReturnLineSerializer`. A
return also appears in the stock ledger as a `RETURN` movement.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `grn_id`, `purchase_order_id`, `lines[].id`, `line_no`, `po_line_id`, `stock_item_id` | ids and links | Transport | none | - | - | - | - | no switch: transport |
| `document_number`, `grn_number`, `purchase_order_number`, `vendor_*`, `branch_*` | identity | Computed | none | - | - | - | - | no switch: owner decision |
| receipt `status`, `receipt_status` | state | Computed | post, reverse | `procurement.goods_receipt.status` | Status | RO | open | recorded |
| `received_by_name` | receiver | Computed | none | `procurement.goods_receipt.received_by` | Received by | RO | open | recorded |
| `received_date` | received date | Stored | create, PATCH | `procurement.goods_receipt.received_date` | Received date | RW | open | recorded |
| receipt `reference`, `narration` | free text | Stored | create, PATCH | `procurement.goods_receipt.reference`, `procurement.goods_receipt.narration` | Reference, Narration | RW | open | recorded |
| `received_item_count`, `ordered_item_count`, `purchase_order_*_item_count`, `purchase_order_fulfilment_status`, `lines[].expected_qty`, `lines[].returned_qty` | received and ordered quantities | Computed | none | `procurement.goods_receipt.quantities` | Received and ordered quantities | RO | open; the order counts also follow the order's fulfilment switch; `returned_qty` reveals a return | recorded |
| `total_value`, `total_value_naira` | receipt value | Computed | none | `procurement.goods_receipt.total_value` | Receipt value | RO | open (Q-C3) | recorded |
| `journal_id` | ledger link | Computed | none | `procurement.goods_receipt.journal` | Journal link | RO | open | recorded |
| `lines[]` (`description`, `expense_account`, `cost_center`, `stock_item_code`, `stock_item_name`, `accepted_qty`, `rejected_qty`) | receipt lines | Stored | create, PATCH | `procurement.goods_receipt.receipt_lines` | Receipt lines | RW | open | recorded |
| `lines[].unit_price` | price on the receipt line | Stored | create, PATCH | `procurement.goods_receipt.receipt_unit_price` | Receipt unit price | RW | open (Q-C3); a storekeeper receiving goods needs no prices | recorded |
| `lines[].value_amount` | line value | Computed | none | `procurement.goods_receipt.receipt_line_value` | Receipt line value | RO | open; shown only if the unit price is readable | recorded |
| return `status` | return state | Computed | reverse action | `procurement.goods_receipt.return_status` | Return status | RO | open | recorded |
| return `return_date` | return date | Stored | reverse body | `procurement.goods_receipt.return_date` | Return date | RW | open | recorded |
| return `reason` | free text | Stored | reverse body (required) | `procurement.goods_receipt.return_reason` | Return reason | RW | open | recorded |
| return `total_value` | value returned | Computed | none | `procurement.goods_receipt.return_value` | Value returned | RO | open (Q-C3) | recorded |
| return `journal_id` | ledger link | Computed | none | `procurement.goods_receipt.return_journal` | Return journal link | RO | open | recorded |
| return `lines[]` (`grn_line_id`, `description`, `quantity`) | returned lines | Stored | reverse body `lines` | `procurement.goods_receipt.return_lines` | Returned lines | RW | open | recorded |
| return `lines[].value_amount` | value of returned lines | Computed | none | `procurement.goods_receipt.return_line_values` | Value of returned lines | RO | open; shown only if the receipt unit price is readable | recorded |

### 5.4 Vendor payments and withholding tax (`procurement.vendor_payment`)

Routes: `vendor-payments/` (GET `.view`, POST `.create`),
`vendor-payments/eligible-invoices/` (`.view`), `vendor-payments/<pk>/` (GET,
PATCH `.update`, including the returned-document correction path), `submit/`,
`post/`, `allocate/` (`.allocate`), `cancel/`, `reverse/`, `attachments/`
(per-verb keys). Serializers: `VendorPaymentSerializer`,
`VendorPaymentListSerializer`, `VendorPaymentAllocationSerializer`, detail overlay
`_serialize_detail` (`views/vendor_payments.py`). Create body: `vendor`,
`bank_account`, `allocations[]`, `payment_date`, `method`, `wht_amount` (kobo, or
null to recompute), `wht_tax_code`, `reference`, `narration`. A gateway payout
line resolves WHT through the same `payables.resolve_wht` (`payments.payout`).
No payment payload carries a vendor email, phone, address, tax id, bank detail,
category, payment terms, KYC state, risk or hold flag. There is no WHT rate in
any procurement payload; the rate lives on the finance tax code.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `workflow_instance_id`, `approval_returned`, `vendor_id`, `payment_account_id`, `bank_account_id` | ids | Transport | none | - | - | - | - | no switch: transport |
| `document_number`, `vendor_code`, `vendor_name`, `branch_id`, `branch_name` | identity | Computed | create `vendor` | - | - | - | - | no switch: owner decision |
| `status`, `approval_state` | state | Computed | actions | `procurement.vendor_payment.status` | Status | RO | open | recorded |
| `payment_date` | payment date | Stored | create, PATCH | `procurement.vendor_payment.payment_date` | Payment date | RW | open | recorded |
| `method` | payment method | Stored | create, PATCH | `procurement.vendor_payment.method` | Payment method | RW | open | recorded |
| `gross_amount` | sum of the allocations | Computed | none | `procurement.vendor_payment.gross_amount` | Amount settled (gross) | RO | open (Q-C3) | recorded |
| `net_amount`, `net_naira` | gross minus WHT | Computed | none | `procurement.vendor_payment.net_amount` | Amount paid out (net) | RO | open (Q-C3) | recorded |
| `wht_amount` | withholding tax, typed or computed | Stored | create, PATCH | `procurement.vendor_payment.wht_amount` | Withholding tax | RW | open | recorded |
| `wht_tax_code_id`, `wht_tax_code_value` | tax code, defaults to the vendor's code | Stored | create, PATCH `wht_tax_code` | `procurement.vendor_payment.wht_tax_code` | Withholding tax code | RW | open; reveals the vendor's default WHT code | recorded |
| `wht_source` | computed or entered (a typed value is a hand-set exemption) | Computed | none | `procurement.vendor_payment.wht_source` | How withholding tax was set | RO | open; shown only if `wht_amount` is readable | recorded |
| `allocated_amount`, `unallocated_amount`, `advance_remaining`, `allocation_status` | applied to bills | Computed | allocate action | `procurement.vendor_payment.allocated_amount` | Applied to bills | RO | open | recorded |
| `allocations[]` (`vendor_invoice_id`, `amount`) | bills settled | Stored | create, PATCH, allocate | `procurement.vendor_payment.allocations` | Bills settled | RW | open | recorded |
| `allocations[].invoice_number`, `invoice_date`, `due_date`, `invoice_total`, `invoice_balance`, `invoice_payment_status` | the bill's own figures | Computed | none | `procurement.vendor_payment.allocations` | Bills settled | RO | the stricter of this and the bill amount switches wins | recorded |
| `payment_code`, `payment_account_name`, `bank_account_name` | bank account chosen to pay from | Stored | create, PATCH `bank_account` | `procurement.vendor_payment.paid_from_account` | Paid from account | RW | open (Q-C4) | recorded |
| `reference` | reference | Stored | create, PATCH | `procurement.vendor_payment.reference` | Reference | RW | open | recorded |
| `narration` | narration | Stored | create, PATCH | `procurement.vendor_payment.narration` | Narration | RW | open | recorded |
| `journal_id` | ledger link | Computed | none | `procurement.vendor_payment.journal` | Journal link | RO | open | recorded |
| `created_at`, `created_by_name` | who recorded it | Computed | none | `procurement.vendor_payment.recorded_by` | Recorded by | RO | open | recorded |
| `attachments[]` (`name`, `url`, `caption`, `uploaded_by_name`, `superseded_*`) | uploaded files | Stored | `vendor-payments/<pk>/attachments/` | `procurement.vendor_payment.attachments` | Attachments | RW | open (Q-C7); the uploader email fallback is personal data | recorded |
| detail `posting_lines[]` (`account_code`, `account_name`, `debit`, `credit`) | journal lines including the WHT payable and bank legs | Computed | none | `procurement.vendor_payment.posting_lines` | Journal lines | RO | open; dropped if gross, net or WHT is unreadable | recorded |
| detail `activity[]` | timeline; messages quote net and WHT | Computed | none | `procurement.vendor_payment.activity` | Activity | RO | open; built from the readable parts only | recorded |
| eligible bills row `invoice_date`, `due_date`, `subtotal`, `tax_total`, `total`, `amount_paid`, `balance_due`, `payment_status` | the bill picker | Computed | none | `procurement.vendor_invoice.total` | Bill amount | RO | follows the bill switches; `subtotal` and `tax_total` are the WHT base, so they also need `wht_amount` | recorded |
| eligible bills row `id`, `vendor_id`, `document_number`, `vendor_code`, `branch_id` | identity | Computed | none | - | - | - | - | no switch: owner decision |
| submit response `approval` block | approval transport | Transport | none | - | - | - | - | no switch: transport |

### 5.5 Requisitions, shared sourcing, RFQs and quotations

Routes: `requisitions/` (GET `.view`, POST `.create`), `requisitions/<pk>/`
(GET, PATCH `.update`), `requisitions/<pk>/submit/`, `requisitions/summary/`,
`requisitions/budget-availability/`, `rfqs/` (GET `.view`, POST `.create`),
`rfqs/free-requisition-lines/`, `rfqs/<pk>/` (GET, PATCH), `issue/`, `close/`,
`cancel/`, `rfqs/<pk>/amendments/`, invitation resend and extend (`.issue`),
`rfqs/summary/`, `quotations/` (GET `.view`, POST `.create`), `quotations/<pk>/`,
`submit/`, `award/`. Serializers: `RequisitionSerializer`,
`RequisitionLineSerializer`, `FreeRequisitionLineSerializer`,
`RfqListSerializer`, `RfqDetailSerializer`, `RfqLineSerializer`,
`QuotationListSerializer`, `QuotationDetailSerializer`,
`VendorQuotationLineSerializer`; the shared-sourcing payload is hand-built
(`_shared_sourcing_payload`). Shared RFQs and their quotations are hidden from a
buyer who does not reach every participant branch. Frontend:
`FinPro/src/pages/procurement/requisitions.tsx`, `sourcing/`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `line_no`, `is_parked`, `approved_by_override`, `workflow_instance_id`, `approval_returned`, `requisition_id`, `rfq_id`, `awarded_po_id`, `currency_id` (as an id) | ids and workflow metadata | Transport | none | - | - | - | - | no switch: transport |
| `document_number`, `requisition_number`, `rfq_number`, `awarded_po_number`, `vendor_*`, `branch_id`, `branch_name`, `participant_branches[]` | identity | Computed or stored | create | - | - | - | - | no switch: owner decision |
| requisition `status`, `approval_state` | state | Computed | submit | `procurement.requisition.status` | Status | RO | open | recorded |
| requisition `title` | title | Stored | create, PATCH | `procurement.requisition.title` | Title | RW | open | recorded |
| requisition `request_date` | request date | Stored | create, PATCH | `procurement.requisition.request_date` | Request date | RW | open | recorded |
| requisition `needed_by` | date needed | Stored | create, PATCH | `procurement.requisition.needed_by` | Needed by | RW | open | recorded |
| `requested_by_id`, `requested_by_name` | requester | Computed | none | `procurement.requisition.requested_by` | Requested by | RO | open | recorded |
| requisition `cost_center_id`, `cost_center_code`, `cost_center_name` | cost centre | Stored | create, PATCH | `procurement.requisition.cost_center` | Cost centre | RW | open | recorded |
| requisition `justification` | free text, also searched by `?search=` | Stored | create, PATCH | `procurement.requisition.justification` | Justification | RW | open | recorded |
| `estimated_total`, `estimated_total_naira`, summary `pending_approval`, `approved_mtd`, `draft`, `sent_back`, `total_value_mtd` amounts | estimated total | Computed | none | `procurement.requisition.estimated_total` | Estimated total | RO | open (Q-C3) | recorded |
| requisition `lines[]` (`description`, `quantity`, `unit`) | requested items | Stored | create, PATCH | `procurement.requisition.lines` | Requested items | RW | open | recorded |
| `lines[].catalog_item_id` | catalogue link | Stored | create, PATCH | `procurement.requisition.line_catalog_item` | Line catalogue item | RW | open | recorded |
| `lines[].estimated_unit_price` | budget holder's estimate | Stored | create, PATCH | `procurement.requisition.estimated_unit_price` | Estimated unit price | RW | open (Q-C3) | recorded |
| `lines[].estimated_line_total` | line total | Computed | none | `procurement.requisition.estimated_line_total` | Estimated line total | RO | open; shown only if quantity and price are readable | recorded |
| `lines[].expense_account_id`, `expense_code` | accounting coding | Stored | create, PATCH | `procurement.requisition.line_expense_account` | Line expense account | RW | open | recorded |
| `lines[].tax_code_id` | tax code | Stored | create, PATCH | `procurement.requisition.line_tax_code` | Line tax code | RW | open | recorded |
| free-line extras `request_date`, `requisition_number`, `branch_name` | labels and dates of the source requisition | Computed | none | - | - | - | - | no switch: identifier |
| budget availability `has_budget`, `period` (budget name), `budget`, `committed`, `available` | plan against commitments at a cost centre | Computed | none | `finance.budget.amount` | Planned amount | RO | the plan figures need the budget switches (leaks A-L18, C-L10, C-L11) | registered in area A |
| `shared_sourcing` (null for an ordinary RFQ) | branch shares of a shared RFQ | Computed | none | `procurement.rfq.shared_sourcing` | Branch shares | RO | open | recorded |
| `allocations[]` (`rfq_line_id`, `requisition_line_id`, `quantity`) | quantity per branch on a shared RFQ | Stored | create `lines[].allocations` (PATCH refuses) | `procurement.rfq.branch_allocations` | Branch quantities | RW (create only) | open | recorded |
| RFQ `rfq_status`, `version` | state | Computed | issue, close, cancel | `procurement.rfq.status` | Status | RO | open | recorded |
| RFQ `title` | title | Stored | create, PATCH | `procurement.rfq.title` | Title | RW | open | recorded |
| RFQ `issue_date` | issue date | Stored | create, PATCH | `procurement.rfq.issue_date` | Issue date | RW | open | recorded |
| RFQ `response_due_date`, `response_due_at` | response deadline | Stored | create, PATCH, amendment | `procurement.rfq.response_due` | Response deadline | RW | open | recorded |
| RFQ `budget_estimate` | the school's ceiling for the purchase | Stored | create, PATCH | `procurement.rfq.budget_estimate` | Budget estimate | RW | sensitive (recommended, Q-C3): a buyer who reads the ceiling can tip a vendor to bid just under it | recorded |
| RFQ `notes` | free text | Stored | create, PATCH | `procurement.rfq.notes` | Notes | RW | open | recorded |
| RFQ `line_count`, `response_count`, `invited_count` | counts | Computed | none | - | - | - | - | no switch: transport |
| RFQ `lines[]` (`description`, `quantity`, `requisition_line_id`, `expense_account_id`, `expense_code`, `tax_code_id`, `version`, `is_active`) | quotation request lines | Stored | create, PATCH, amendment | `procurement.rfq.lines` | Quotation request lines | RW | open | recorded |
| RFQ `invitations[]` (`vendor_id`, `status`, `deadline`) | invited vendors | Stored (through `invited_vendors` ids) | create, PATCH | `procurement.rfq.invited_vendors` | Invited vendors | RW | open | recorded |
| invitation `opened_at`, `draft_started_at`, `submitted_at`, `declined_at`, `decline_reason`, `responded` | vendor behaviour | Computed | none | `procurement.rfq.invitation_activity` | Invitation activity | RO | open | recorded |
| invitation `quotation_id`, `quotation_status`, `quotation_total` | the vendor's response | Computed | none | `procurement.quotation.total` | Quoted amount | RO | follows the quotation switches | recorded |
| invitation `recipients[]` (`name`, `email`) | snapshot of contacts | Computed | none | `procurement.vendor_contact.name`, `procurement.vendor_contact.email` | as registered | RO | open | registered |
| RFQ `quotations[]` summary (`quotation_status`, `total`, `lead_time_days`, `quote_date`, `valid_until`, `is_expired`) | quotation summary | Computed | none | `procurement.quotation.total` | Quoted amount | RO | follows the quotation switches | recorded |
| RFQ `amendments[]` (`version`, `summary`, `response_required`, `published_at`) | amendments | Stored | `rfqs/<pk>/amendments/` | `procurement.rfq.amendments` | Amendments | RW | open | recorded |
| RFQ `activity[]` | timeline; messages quote submitted amounts | Computed | none | `procurement.rfq.activity` | Activity | RO | open | recorded |
| quotation `quotation_status` | state | Computed | submit, award | `procurement.quotation.status` | Status | RO | open | recorded |
| quotation `quote_date` | quote date | Stored | create, PATCH | `procurement.quotation.quote_date` | Quote date | RW | open | recorded |
| quotation `valid_until`, `is_expired` | validity | Stored, computed | create, PATCH | `procurement.quotation.valid_until` | Valid until | RW | open | recorded |
| quotation `lead_time_days` | lead time | Stored | create, PATCH | `procurement.quotation.lead_time_days` | Lead time | RW | open | recorded |
| quotation `currency` | quoted currency | Stored | create, PATCH | `procurement.quotation.currency` | Currency | RW | open | recorded |
| quotation `reference`, `notes` | free text | Stored | create, PATCH | `procurement.quotation.reference`, `procurement.quotation.notes` | Reference, Notes | RW | open | recorded |
| quotation `subtotal`, `tax_total`, `total`, `total_naira` | quoted amount | Computed | none | `procurement.quotation.total` | Quoted amount | RO | open (Q-C3): a vendor's bid seen by a requester before award | recorded |
| quotation `lines[].unit_price` | quoted unit price typed by the buyer | Stored | create, PATCH | `procurement.quotation.unit_price` | Quoted unit price | RW | open (Q-C3) | recorded |
| quotation `lines[]` (`description`, `rfq_line_id`, `expense_account_id`, `expense_code`, `quantity`, `tax_code_id`, `net_amount`, `tax_amount`, `response_type`, `alternative_for_id`) | quotation lines | Stored | create, PATCH | `procurement.quotation.quote_lines` | Quotation lines | RW | open | recorded |
| quotation `attachments[]` (`name`, `content_type`, `size`, `revision`, `url`) | files the vendor uploaded | Computed | portal only | `procurement.quotation.attachments` | Attachments | RO | open | recorded |
| quotation `submissions[]` (`revision`, `rfq_version`, `submitted_at`) | submission history | Computed | none | `procurement.quotation.submissions` | Submissions | RO | open | recorded |
| quotation `submissions[].submitted_by_email` | a vendor contact's email | Computed | none | `procurement.vendor_contact.email` | Email | RO | follows the registered contact switch (leak C-L7) | registered |
| quotation `activity[]` | timeline | Computed | none | `procurement.quotation.activity` | Activity | RO | open | recorded |

### 5.6 Purchase orders (`procurement.purchase_order`)

Routes: `purchase-orders/` (GET `.view`, POST `.create`),
`purchase-orders/<pk>/` (GET, PATCH `.update`), `submit/` (`.submit`; with
`auto_email_vendor` needing `.email_vendor`), `cancel/`, `summary/`,
`email-preview/`, `email/`, `email-retry/` (`.email_vendor`). Serializers:
`PurchaseOrderSerializer`, `PurchaseOrderListSerializer`, `POLineSerializer`,
`PurchaseOrderVendorDeliverySerializer`. The PO PDF and email go to the vendor
and are system surfaces. Create body: `requisition`, `vendor`, `contract`,
`order_date`, `expected_date`, `delivery_address`, `payment_terms` (defaults to
the vendor's), `currency`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `workflow_instance_id`, `approval_returned`, `can_email_vendor`, `contract_id`, `requisition_id`, `receipt_documents[]`, `invoice_documents[]` links | ids and links | Transport | none | - | - | - | - | no switch: transport |
| `document_number`, `quotation_number`, `requisition_number`, `contract_reference`, `vendor_*`, `branch_id`, `branch_name` | identity | Computed or stored | create | - | - | - | - | no switch: owner decision |
| `status`, `approval_state`, `display_status` | state | Computed | actions | `procurement.purchase_order.status` | Status | RO | open | recorded |
| `order_date` | order date | Stored | create, PATCH | `procurement.purchase_order.order_date` | Order date | RW | open | recorded |
| `expected_date` | expected delivery | Stored | create, PATCH | `procurement.purchase_order.expected_date` | Expected date | RW | open | recorded |
| `delivery_address` | delivery address, also sent to the vendor | Stored | create, PATCH | `procurement.purchase_order.delivery_address` | Delivery address | RW | open | recorded |
| `payment_terms` | the order's agreed terms, copied from the vendor when omitted | Stored | create, PATCH | `procurement.purchase_order.payment_terms` | Payment terms | RW | open; the create default must not echo the vendor's terms when `procurement.vendor.payment_terms` is unreadable (leak C-L8e) | recorded |
| `narration` | narration (comes from the requisition or award; no write path in the views) | Computed | none | `procurement.purchase_order.narration` | Narration | RO | open | recorded |
| `subtotal`, `tax_total`, `total`, `total_naira` | order amount | Computed | none | `procurement.purchase_order.total` | Order amount | RO | open (Q-C3) | recorded |
| `received_pct`, `invoiced_pct`, `lines[].received_qty`, `lines[].invoiced_qty` | fulfilment | Computed | none | `procurement.purchase_order.fulfilment` | Fulfilment | RO | open | recorded |
| `contract_id` (as a choice) | contract the order sits under | Stored | create, PATCH `contract` | `procurement.purchase_order.contract` | Contract | RW | open | recorded |
| `lines[]` (`description`, `expense_account_id`, `expense_code`, `quantity`, `tax_code_id`, `net_amount`, `tax_amount`) | snapshot of the requisition or award | Computed | none | `procurement.purchase_order.order_lines` | Order lines | RO | open | recorded |
| `lines[].unit_price` | order unit price | Computed | none | `procurement.purchase_order.unit_price` | Order unit price | RO | open (Q-C3) | recorded |
| `receipt_documents[]` (`document_number`, `received_date`, `status`, `item_count`) | link list of receipts | Computed | none | - | - | - | follows goods receipt visibility | no switch: transport |
| `invoice_documents[]` (`invoice_date`, `total`, `status`, `match_status`) | link list of bills | Computed | none | `procurement.vendor_invoice.total` | Bill amount | RO | follows the bill amount switch | recorded |
| `email_deliveries[]` (`source`, `status`, `requested_by_name`, `buyer_message`, `queued_at`, `sent_at`, `cancelled_at`, `created_at`, `parent_id`) | vendor email history | Computed | none | `procurement.purchase_order.email_history` | Vendor email history | RO | open | recorded |
| `email_deliveries[].recipient_count`, `bcc_count`, `failure_reason` | counts of contacts holding routing flags; the gateway's text may quote an address | Computed | none | `procurement.vendor_contact.is_active` | Active contact | RO | shown only if the contact routing flags and email are readable (leak C-L8d) | registered |

### 5.7 Vendor bills (`procurement.vendor_invoice`)

Routes: `vendor-invoices/` (GET `.view`, POST `.create`),
`vendor-invoices/<pk>/` (GET, PATCH `.update`), `reference-check/` (`.view`),
`summary/`, `opening/` (`.import_opening`, up to 500 bills), `match/` (`.match`),
`submit/`, `post/` (`allow_variance` needs `.override_variance`), `void/`
(`.reverse`), `attachments/`. Serializers: `VendorInvoiceSerializer`,
`VendorInvoiceListSerializer`, `VendorInvoiceLineSerializer`, detail overlay
`_serialize_invoice_detail`. The opening import body is `bills[{vendor,
invoice_date, due_date, vendor_reference, amount, branch, narration}]`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `workflow_instance_id`, `approval_returned`, `purchase_order_id`, `lines[].po_line_id`, `lines[].grn_line_id` | ids | Transport | none | - | - | - | - | no switch: transport |
| `document_number`, `purchase_order_number`, `vendor_*`, `branch_*` | identity | Computed or stored | create | - | - | - | - | no switch: owner decision |
| `status`, `approval_state`, `match_status`, `payment_status`, `display_status`, `is_overdue`, `is_opening` | state | Computed | actions | `procurement.vendor_invoice.status` | Status | RO | open | recorded |
| `invoice_date` | bill date | Stored | create, PATCH, opening import | `procurement.vendor_invoice.invoice_date` | Bill date | RW | open | recorded |
| `due_date` | due date | Stored | create, PATCH, opening import | `procurement.vendor_invoice.due_date` | Due date | RW | open | recorded |
| `vendor_reference` | supplier's invoice number | Stored | create, PATCH, opening import | `procurement.vendor_invoice.vendor_reference` | Supplier's invoice number | RW | open | recorded |
| `narration` | narration | Stored | create, PATCH, opening import | `procurement.vendor_invoice.narration` | Narration | RW | open | recorded |
| `subtotal`, `tax_total`, `total`, `total_naira` | bill amount | Computed | none | `procurement.vendor_invoice.total` | Bill amount | RO | open (Q-C3) | recorded |
| opening import body `amount` | opening balance owed | Stored | opening import | `procurement.vendor_invoice.opening_amount` | Opening balance owed | RW | open; write-only | recorded |
| `amount_paid`, `amount_credited`, `balance_due` | paid, credited and owed | Computed | none | `procurement.vendor_invoice.settlement` | Paid, credited and owed | RO | open | recorded |
| `journal_id` | ledger link | Computed | none | `procurement.vendor_invoice.journal` | Journal link | RO | open | recorded |
| `lines[]` (`description`, `expense_*`, `cost_center_*`, `quantity`, `tax_code_id`) | bill lines | Stored | create, PATCH | `procurement.vendor_invoice.bill_lines` | Bill lines | RW | open | recorded |
| `lines[].unit_price` | bill unit price | Stored | create, PATCH | `procurement.vendor_invoice.bill_unit_price` | Bill unit price | RW | open (Q-C3) | recorded |
| `lines[].net_amount`, `lines[].tax_amount` | line amounts | Computed | none | `procurement.vendor_invoice.bill_line_amounts` | Bill line amounts | RO | open | recorded |
| `attachments[]` | uploaded files | Stored | `vendor-invoices/<pk>/attachments/` | `procurement.vendor_invoice.attachments` | Attachments | RW | open (Q-C7) | recorded |
| detail `match_comparisons[]` (quantities, `po_unit_price`, `invoice_unit_price`, `grn_number`, `grn_accepted_quantity`) | three-way match | Computed | none | `procurement.vendor_invoice.match_comparisons` | Three-way match | RO | open; each column follows the source document's switch | recorded |
| detail `payments[]` (`document_number`, `payment_date`, `amount`, `status`) | payments applied | Computed | none | `procurement.vendor_invoice.payments` | Payments | RO | `amount` follows the vendor payment amount switches | recorded |
| detail `posting_lines[]` | journal lines | Computed | none | `procurement.vendor_invoice.posting_lines` | Journal lines | RO | open; dropped if `total` is unreadable | recorded |
| detail `activity[]` | timeline; messages quote the amount | Computed | none | `procurement.vendor_invoice.activity` | Activity | RO | open | recorded |
| reference check `same_vendor_duplicate`, `other_vendor_matches[]` (`document_number`, `vendor_code`, `vendor_name`, `invoice_date`, `total`, `status`) | duplicate-bill check | Computed | none | `procurement.vendor_invoice.total` | Bill amount | RO | must be narrowed to the caller's branches (leak C-L2) | recorded |
| summary `under_review`, `approved`, `overdue.count`, `disputed`, `sent_back` | counts | Computed | none | - | - | - | - | no switch: transport |
| summary `overdue.amount` | overdue amount | Computed | none | `procurement.vendor_invoice.total` | Bill amount | RO | follows `total`, `amount_paid`, `amount_credited` | recorded |

### 5.8 Vendors (unswitched keys), categories, catalogue, contracts, assessments, settings

Vendors: `VendorSerializer` and `VendorListSerializer` carry 13 registered vendor
fields (`procurement.vendor`) and the keys below, which are open and unswitchable
until registered. Categories: `categories/` (GET `procurement.category.view`,
POST `.create`), `categories/<pk>/`, `categories/insights/`
(`procurement.report.view`). Catalogue: `catalog-items/` (GET `.view`, POST
`.create`), `catalog-items/<pk>/`, `catalog-items/<pk>/insights/`
(`procurement.report.view`); catalogue items are shared records. Contracts:
`contracts/` (GET `.view`, POST `.create`), `<pk>/`, `summary/`, `renewals/`,
`linked-pos/` (`procurement.purchase_order.view`), `activate/`, `terminate/`,
`renew/`, `milestone-complete/`. Assessments: `vendor-assessments/` (GET
`procurement.analytics.view`, POST `procurement.vendor_assessment.create`).
Settings: `settings/` (GET `procurement.settings.view`, PATCH `.update` with
whole-tenant reach), `settings/history/`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| vendor `id`, `code`, `name`, `branch_id`, `branch_name` | vendor identity | Stored | create; PATCH `name` | - | - | - | - | no switch: owner decision |
| vendor `is_active` | active flag | Stored | create, PATCH | `procurement.vendor.is_active` | Vendor active | RW | open | recorded |
| vendor `payable_account_id`, `payable_code` | AP control account | Stored | create, PATCH `payable_account` | `procurement.vendor.payable_account` | Payable account | RW | open (Q-C6) | recorded |
| vendor `default_expense_account_id`, `default_expense_code` | default expense account | Stored | create, PATCH `default_expense_account` | `procurement.vendor.default_expense_account` | Default expense account | RW | open (Q-C6) | recorded |
| vendor `default_wht_tax_code_id`, `default_wht_tax_code_value` | decides the WHT on every payment to the vendor | Stored | create, PATCH `default_wht_tax_code` | `procurement.vendor.default_wht_tax_code` | Default withholding tax code | RW | open; moves money (Q-C6) | recorded |
| vendor list `active_po_count` | active orders | Computed | none | `procurement.vendor.active_po_count` | Active orders | RO | open; or dropped when order access is closed | recorded |
| category `id`, `code`, `name`, `parent_*`, `level`, `child_count`, `catalog_item_count` | category identity and structure | Stored | create, PATCH | - | - | - | - | no switch: identifier |
| category `default_expense_account_id`, `default_expense_code` | default expense account | Stored | create, PATCH | `procurement.category.default_expense_account` | Default expense account | RW | open | recorded |
| category `is_active` | active flag | Stored | create, PATCH | `procurement.category.is_active` | Category active | RW | open | recorded |
| category `vendor_count`; insights `spend_mtd`, `spend_prior_month`, `spend_ytd` | vendors and spend per category | Computed | none | `procurement.vendor.category` | Category | RW | registered switch; both must be dropped when it is hidden (leak C-L8a, C-L8b) | registered |
| catalogue `id`, `code`, `name` | item identity | Stored | create, PATCH | - | - | - | - | no switch: identifier |
| catalogue `description` | description | Stored | create, PATCH | `procurement.catalog_item.description` | Description | RW | open | recorded |
| catalogue `unit_of_measure` | unit | Stored | create, PATCH | `procurement.catalog_item.unit_of_measure` | Unit of measure | RW | open | recorded |
| catalogue `category_id`, `category_code`, `category_name`, `category_level`, `category_path` | the item's category (a different field from the vendor's) | Stored | create, PATCH `category` | `procurement.catalog_item.category` | Category | RW | open | recorded |
| catalogue `preferred_vendor_id`, `preferred_vendor_code`, `preferred_vendor_name` | preferred vendor | Stored | create, PATCH | `procurement.catalog_item.preferred_vendor` | Preferred vendor | RW | open | recorded |
| catalogue `default_expense_account_id`, `expense_code`, `expense_name` | default expense account | Stored | create, PATCH | `procurement.catalog_item.default_expense_account` | Default expense account | RW | open | recorded |
| catalogue `default_tax_code_id`, `tax_code`, `tax_name` | default tax code | Stored | create, PATCH | `procurement.catalog_item.default_tax_code` | Default tax code | RW | open | recorded |
| catalogue `lead_time_days` | lead time | Stored | create, PATCH | `procurement.catalog_item.lead_time_days` | Lead time | RW | open | recorded |
| catalogue `standard_unit_price`, `standard_unit_price_naira` | standard price | Stored | create, PATCH | `procurement.catalog_item.standard_unit_price` | Standard unit price | RW | open (Q-C3) | recorded |
| catalogue `is_active` | active flag | Stored | create, PATCH | `procurement.catalog_item.is_active` | Item active | RW | open | recorded |
| catalogue `stock_status` | stock state, only for stock viewers | Computed | none | `procurement.stock.needs_reorder` | Needs reorder | RO | shown only if `on_hand_qty` is readable | recorded |
| catalogue insights `usage`, `vendor_pricing[]`, `purchase_history[]` | prices paid, by vendor and order | Computed | none | `procurement.catalog_item.price_history` | Purchase price history | RO | open; derived from the order unit price; must be branch narrowed (leak C-L12) | recorded |
| contract `id`, `reference`, `vendor_*`, `milestone_count`, `renews_*`, `renewed_by_reference` | identity and counts | Stored | create | - | - | - | - | no switch: owner decision |
| contract `title`, `notes` | free text | Stored | create, PATCH | `procurement.contract.title`, `procurement.contract.notes` | Title, Notes | RW | open | recorded |
| contract `status`, `is_expired` | state | Computed | activate, terminate, renew | `procurement.contract.status` | Status | RO | open | recorded |
| contract `start_date`, `end_date`, `renewal_notice_days`, `auto_renew`; `renewal_window_start` (computed) | contract term | Stored | create, PATCH | `procurement.contract.term` | Contract term | RW | open | recorded |
| contract `contract_value`, `contract_value_naira`; summary `total_active_value` | contract value | Stored | create, PATCH | `procurement.contract.contract_value` | Contract value | RW | open (Q-C3) | recorded |
| contract `payment_terms` | the contract's own terms, defaults from a setting | Stored | create, PATCH | `procurement.contract.payment_terms` | Payment terms | RW | open | recorded |
| contract `milestones[]` (`name`, `due_date`, `amount`, `status`, `completed_date`, `note`) | milestones | Stored | create, PATCH, milestone-complete | `procurement.contract.milestones` | Milestones | RW | open | recorded |
| contract `activity[]`; summary `active`, `expiring_soon`, `expired` | timeline and counts | Computed | none | `procurement.contract.activity` | Activity | RO | open | recorded |
| contract linked orders `total`, `total_naira` | order totals | Computed | none | `procurement.purchase_order.total` | Order amount | RO | must be branch narrowed (leak C-L12) | recorded |
| assessment `assessment_date` | date | Stored | create | `procurement.vendor_assessment.assessment_date` | Assessment date | RW | open | recorded |
| assessment `on_time_delivery`, `quality_acceptance`, `invoice_accuracy`, `responsiveness` | the four 0 to 100 criterion scores | Stored | create | `procurement.vendor_assessment.scores` | Criterion scores | RW | open; one set; also restated in vendor performance (leak C-L13) | recorded |
| assessment `overall_score`, `grade` | derived rating | Computed | none | `procurement.vendor_assessment.grade` | Grade | RO | open; shown only if `scores` is readable | recorded |
| assessment `notes` | free-text opinion about a supplier | Stored | create | `procurement.vendor_assessment.notes` | Assessment notes | RW | sensitive (recommended, Q-C3): the most sensitive text in the area | recorded |
| assessment `assessor`, `assessor_is_exited` | the person assessing | Computed | none | `procurement.vendor_assessment.assessor` | Assessor | RO | open | recorded |
| settings `default_payment_terms`, `default_payment_terms_label` | default payment terms | Stored | PATCH | `procurement.settings.default_payment_terms` | Default payment terms | RW | open | recorded |
| settings `default_delivery_address` | default delivery address | Stored | PATCH | `procurement.settings.default_delivery_address` | Default delivery address | RW | open | recorded |
| settings `quantity_tolerance_bps` | quantity tolerance | Stored | PATCH | `procurement.settings.quantity_tolerance_bps` | Quantity tolerance | RW | open | recorded |
| settings `price_tolerance_bps` | price tolerance | Stored | PATCH | `procurement.settings.price_tolerance_bps` | Price tolerance | RW | open | recorded |
| settings `allow_non_po_invoices` | allow bills without an order | Stored | PATCH | `procurement.settings.allow_non_po_invoices` | Allow bills without an order | RW | open | recorded |
| settings `vendor_purchase_kyc_requirement`, `vendor_purchase_kyc_requirement_label` | KYC needed before buying | Stored | PATCH | `procurement.settings.vendor_purchase_kyc_requirement` | KYC requirement for purchases | RW | open | recorded |
| settings `require_purchase_order_for_receipts` | order needed before receiving | Stored | PATCH | `procurement.settings.require_purchase_order_for_receipts` | Require an order for receipts | RW | open | recorded |
| settings `default_requisition_lead_days` | default lead days | Stored | PATCH | `procurement.settings.default_requisition_lead_days` | Default requisition lead days | RW | open | recorded |
| settings `contract_renewal_notice_days` | contract renewal notice | Stored | PATCH | `procurement.settings.contract_renewal_notice_days` | Contract renewal notice days | RW | open | recorded |
| settings `default_rfq_response_days` | default response window | Stored | PATCH | `procurement.settings.default_rfq_response_days` | Default RFQ response days | RW | open | recorded |
| settings `rfq_closing_soon_days` | closing-soon window | Stored | PATCH | `procurement.settings.rfq_closing_soon_days` | RFQ closing-soon days | RW | open | recorded |
| settings `minimum_rfq_invited_vendors` | minimum invited vendors | Stored | PATCH | `procurement.settings.minimum_rfq_invited_vendors` | Minimum invited vendors | RW | open | recorded |
| settings `minimum_submitted_quotations_before_award` | minimum quotations before award | Stored | PATCH | `procurement.settings.minimum_submitted_quotations_before_award` | Minimum quotations before award | RW | open | recorded |
| settings `non_po_spend_limit_pct` | limit on spend without an order | Stored | PATCH | `procurement.settings.non_po_spend_limit_pct` | Non-order spend limit | RW | open | recorded |
| settings `history[]` | before and after per setting | Computed | none | `procurement.settings.history` | Settings history | RO | each line follows its setting's switch | recorded |
| settings `updated_at`, `updated_by` | last change; `updated_by` is the editor's email, to become a display name | Computed | none | `procurement.settings.updated_by` | Last changed by | RO | open; personal data (leak C-L14) | recorded |
| settings `consumers` | where a setting is used | Computed | none | - | - | - | - | no switch: transport |

### 5.9 Reports, dashboards, exports and approvals (restating paths)

These payloads hold no stored fields of their own. They restate the fields above,
so each shows a value only to a caller who can read the switch it restates.

| Surface | Restated values | Switch family | Status |
| --- | --- | --- | --- |
| `reports/ap-aging/` (guarded `procurement.analytics.view`), `ap-aging/vendor/`, `ap-cash-requirements`, `grir*`, `spend-analysis`, `vendor-performance`, `vendor-summary` (`procurement.report.view`), `vendor-insights` | bill, payment, credit note and receipt amounts, `payment_terms` (already filtered), `by_category` (already trimmed), assessment scores, `total_spend_ytd`, `average_payment_days` | follows `procurement.vendor_invoice.*`, `procurement.vendor_payment.*`, `procurement.goods_receipt.*`, `procurement.vendor_assessment.*` | recorded |
| `reports/ap-reconciliation/` | `subledger_total`, `control_total`, `difference`, `is_reconciled` | a read-only report switch in the family of `finance.report`; name to be chosen at registration | recorded |
| `reports/cycle-time/` | stage averages and counts, no money | - | no switch: transport |
| overview dashboard | `kpis.spend`, `spend_by_category`, `top_vendors`, `committed_vs_spent`, `kpis.open_purchase_orders`, `pipeline`, `bills_due`, `overdue_invoices`, exceptions, `contracts_ending`, `approvals_awaiting_user` | `procurement.dashboard`, one read-only switch per block, as `finance.dashboard` | recorded |
| suppliers dashboard | `spend.plan` (`against_plan`), `by_branch`, `open_rfqs[].budget`, `savings`, `scorecard`, `vendor_base` | `procurement.dashboard`, one switch per block | recorded |
| stock dashboard | `position`, `by_store`, `issued`, `adjustments`, `running_low`, `movements`, `turns` | `procurement.dashboard`, one switch per block; derived from the stock switches | recorded |
| overview `recent_activity` | action label and document number only | - | no switch: transport |
| export `procurement.purchase_orders` | `payment_terms`, `narration`, `expected_date` | `access=` keys on every column once registered | recorded |
| export `procurement.vendor_invoices` | `vendor_reference`, `narration`, `due_date`, `match_status`, `payment_status` | `access=` keys | recorded |
| export `procurement.requisitions` | `estimated_total`, `cost_center`, `justification` | `access=` keys | recorded |
| export `procurement.vendors` | 13 vendor columns carry `access=`; the `payable_account` column does not | `procurement.vendor.payable_account` | recorded |
| approval handlers (requisition, purchase order, bill, payment, credit note) and the approval queue row | amounts, unit prices, WHT, payment terms, bill totals, vendor-free titles | the document switches; fixed at the inbox choke point (section 7.5) | recorded |

---

## 6. Area D: school settings and finance settings

Every school setting is a `vs_config` definition written through
`vs_config.services.resolution.set_value` and audited as `config.value.updated`.
Each settings screen has one endpoint pair, and control today is one key,
`school.settings.update`, plus a whole-school reach check
(`assert_caller_may_configure`). A role holding that key can change every rule
below. No settings serializer carries `FieldAccessMixin`, `visible()`,
`can_read()` or `assert_writable()`.

Recommended registration: one permission resource, `school.settings`, one group
per screen, each rules body rendered through a new read-only mixin serializer.
Where two screens share a key name, the switch carries a group prefix. Fields
whose value is a school choice default open; the security fields and the
starting role ship sensitive. Whether to switch per field or per screen is open
(Q-D1), and whether vocabulary and display values get a Read switch is open
(Q-D2).

| Route | Read | Write | Frontend |
| --- | --- | --- | --- |
| `/v1/academics/rules/` (`AcademicRulesView`) | any member | `school.settings.update`, PUT | `school-fe/src/pages/protected/settings/sections/academics-section.tsx`, `hooks/use-school-words.ts` |
| `/v1/academics/calendar/rules/` | any member | `school.settings.update`, PUT | `settings/sections/calendar-section.tsx`, `hooks/use-school-week.ts` |
| `/v1/students/enrolment-rules/`, `guardian-rules/`, `admission-rules/`, `promotion-rules/` | `school.students.view` | `school.settings.update`, PUT | `settings/sections/enrolment-section.tsx`, `guardians-section.tsx`, `admissions-section.tsx`, `promotion-section.tsx` |
| `/v1/students/admission-number-policy/` | `school.students.view` | `school.students.update`, PUT, DELETE (`?branch=`) | `settings/sections/admission-numbers-section.tsx`, `number-rule-section.tsx` |
| `/v1/i/me/staff/rules/`, `staff/number-policy/` | `school.staff.view` | `school.settings.update`, PUT, DELETE (`?branch=`) | `settings/sections/staff-section.tsx`, `staff-numbers-section.tsx` |
| `/v1/i/me/staff/<pk>/leave-group/` | none | `school.settings.update`, PUT | staff record drawer |
| `/v1/i/me/settings/display/`, `security/`, `payroll-scope/` | `school.settings.view` | `school.settings.update`, PATCH, DELETE (`?branch=`) | `settings/sections/display-section.tsx`, `security-section.tsx`, `payroll-section.tsx` |
| `/v1/i/me/settings/staff-profiles/` | `school.settings.view` | `school.field_access.update`, PUT | `settings/sections/staff-profiles-section.tsx` |
| `/v1/attendance/settings/`, `reason-codes/` | register keys | `assert_may_configure`, PATCH, POST | attendance settings tab |
| `/v1/config/values/`, `effective-values/`, `export/`, `security-settings/`, `platform-settings/`, `audit-events/*` | `config.value.view`, `config.export.create`, `config.audit.view` (platform) | `config.value.update`, `config.security.update` | console-fe config screens |

The platform value endpoint, `PATCH /v1/config/security-settings/`, the school
create flow, `reset-config` and `seed_config_catalogue` are alternate write paths
for every setting. They are platform-owned and unreachable by school roles; they
do not bind CodeX staff to a school's switches (Q-D6, answered in 8.1).

### 6.1 School settings (`school.settings`)

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| academics `term_word` | what the school calls a term (Term or Semester) | Stored | PUT rules; config values | `school.settings.term_word` | What the school calls a term | RW | open; Read locked open (Q-D2) | recorded |
| academics `term_names` | names given to a new year's terms | Stored | PUT rules; config values | `school.settings.term_names` | Names given to a new year's terms | RW | open | recorded |
| academics `default_arms` | arms new classes are generated with | Stored | PUT rules; config values | `school.settings.default_arms` | Arms new classes are generated with | RW | open | recorded |
| academics `term_word_options`; every rules body `reason` | choices; audit reason text | Transport | none; PUT | - | - | - | - | no switch: transport |
| calendar `teaching_days` | teaching days of the week | Stored | PUT; config values | `school.settings.teaching_days` | Teaching days | RW | open; Read locked open (Q-D2) | recorded |
| calendar `week_starts_on` | first day of the week | Stored | PUT | `school.settings.week_starts_on` | Week starts on | RW | open; Read locked open (Q-D2) | recorded |
| calendar `closes_school_by_type` | which event types close the school | Stored | PUT | `school.settings.closes_school_by_type` | Event types that close the school | RW | open | recorded |
| calendar `room_required_to_publish` | a timetable needs rooms before it is published | Stored | PUT | `school.settings.room_required_to_publish` | Room required to publish | RW | open | recorded |
| calendar `teacher_duty_match` | how duties match teachers | Stored | PUT | `school.settings.teacher_duty_match` | Teacher duty matching | RW | open | recorded |
| calendar `invigilator_roles` | role keys that may invigilate | Stored | PUT | `school.settings.invigilator_roles` | Roles that may invigilate | RW | open | recorded |
| calendar `default_period_minutes` | default lesson length | Stored | PUT | `school.settings.default_period_minutes` | Default period length | RW | open | recorded |
| calendar `event_types`, `teacher_duty_match_options`, `invigilator_role_options` | choices | Transport | none | - | - | - | - | no switch: transport |
| enrolment `min_age_years` | youngest admissible age | Stored | PUT; config values | `school.settings.min_age_years` | Minimum age | RW | open | recorded |
| enrolment `max_age_years` | oldest admissible age | Stored | PUT; config values | `school.settings.max_age_years` | Maximum age | RW | open | recorded |
| enrolment `required_documents` | documents required to enrol | Stored | PUT | `school.settings.enrolment_required_documents` | Documents required to enrol | RW | open | recorded |
| enrolment `required_fields` | student fields made mandatory | Stored | PUT | `school.settings.required_fields` | Fields required to enrol | RW | open; a role without Write on a named student field is not bound by the rule (Q-D7, answered in 8.1) | recorded |
| enrolment `capacity_mode` | how class capacity is enforced | Stored | PUT | `school.settings.enrolment_capacity_mode` | Class capacity mode | RW | open | recorded |
| enrolment `default_capacity` | default class capacity | Stored | PUT | `school.settings.default_capacity` | Default class capacity | RW | open | recorded |
| enrolment `document_types`, `optional_fields` | choices; `optional_fields` echoes student field labels | Transport | none | - | - | - | filter `optional_fields` to the student fields the caller can read (leak D-L3) | no switch: transport |
| guardians `min_per_student` | guardians required per student | Stored | PUT | `school.settings.min_per_student` | Guardians required per student | RW | open | recorded |
| guardians `email_required` | guardian email mandatory | Stored | PUT | `school.settings.email_required` | Guardian email required | RW | open; same subject as `school.guardians.email`; the rule yields to a caller who cannot write it (Q-D7, answered in 8.1) | recorded |
| guardians `matching` | how guardians match existing people | Stored | PUT | `school.settings.matching` | Guardian matching | RW | open | recorded |
| guardians `extra_relationships`, `relationships` (built-in plus extra) | relationship kinds | Stored; computed alias | PUT | `school.settings.extra_relationships` | Relationships | RW | open | recorded |
| guardians `matching_options` | choices | Transport | none | - | - | - | - | no switch: transport |
| admissions `stages[]` (`name`, `is_offer`, `offer_valid_days`, `position`) | ordered admission stages, replaced as a whole | Stored | PUT admission-rules | `school.settings.stages` | Admission stages | RW | open | recorded |
| admissions `stages[].applicants`, `stages[].id` | applicant count (follows student scope); stage id | Computed | none | - | - | - | - | no switch: transport |
| admissions `required_documents_to_confirm` | documents to confirm an admission | Stored | PUT | `school.settings.required_documents_to_confirm` | Documents to confirm | RW | open | recorded |
| promotion `suspended` | handling of suspended students | Stored | PUT promotion-rules | `school.settings.promotion_suspended` | Promotion of suspended students | RW | open | recorded |
| promotion `not_placed` | handling of students not placed | Stored | PUT | `school.settings.promotion_not_placed` | Promotion of students not placed | RW | open | recorded |
| promotion `arms` | arms used in promotion | Stored | PUT | `school.settings.promotion_arms` | Promotion arms | RW | open | recorded |
| promotion `capacity_mode` | capacity handling in promotion | Stored | PUT | `school.settings.promotion_capacity_mode` | Promotion capacity mode | RW | open | recorded |
| admission number rule `required`, `pattern`, `hint`, `auto_issue` | school or per-branch admission number rule | Stored | PUT, DELETE (`?branch=`) | `school.settings.admission_number_required`, `school.settings.admission_number_pattern`, `school.settings.admission_number_hint`, `school.settings.admission_number_auto_issue` | Admission number rule | RW | open | recorded |
| admission number rule `source` | where the rule comes from (branch, school, default) | Computed | none | `school.settings.admission_number_source` | Rule source | RO | open | recorded |
| admission number rule `suggestion` | next admission number; reveals the highest issued number and so the school's size | Computed | none | `school.settings.admission_number_suggestion` | Next admission number | RO | open (Q-D4) | recorded |
| staff rules `starting_role` | role new staff start with | Stored | PUT; grant-ceiling check on save | `school.settings.starting_role` | Starting role for new staff | RW | sensitive: it decides what new staff can do; Write limited to school administrators | recorded |
| staff rules `required_documents` | documents required of staff | Stored | PUT | `school.settings.staff_required_documents` | Documents required of staff | RW | open | recorded |
| staff rules `self_editable_fields` | staff profile fields staff may edit themselves; option list already drawn from `school.teachers` | Stored | PUT | `school.settings.self_editable_fields` | Fields staff may edit themselves | RW | open | recorded |
| staff rules `hire_requires_approval` | hire needs approval | Stored | PUT | `school.settings.hire_requires_approval` | Hire requires approval | RW | open | recorded |
| staff rules `starting_role_options`, `document_types`, `self_editable_options`, `self_editable_locked` | choices (role names only) | Transport | none | - | - | - | - | no switch: transport |
| leave `allowances` | days per leave type | Stored | PUT staff rules | `school.settings.leave_allowances` | Leave allowances | RW | open | recorded |
| leave `groups` | named leave groups | Stored | PUT staff rules | `school.settings.leave_groups` | Leave groups | RW | open | recorded |
| leave `overrides` | days by branch, group and type | Stored | PUT staff rules | `school.settings.leave_overrides` | Leave overrides | RW | open | recorded |
| leave `working_days`, `exclude_closures` | how leave days are counted | Stored | PUT staff rules | `school.settings.leave_working_days`, `school.settings.leave_exclude_closures` | Leave day counting | RW | open | recorded |
| leave `branch_options`, `leave_types` | choices; branch id and name | Transport | none | - | - | - | - | no switch: transport |
| per-person `leave_group` (`StaffLeaveGroupView`, also shown on the staff record) | leave group assigned to a person | Stored | PUT `staff/<pk>/leave-group/` | `school.teachers.leave_group` | Leave group | RW | open; registered under `school.teachers`, not `school.settings` | recorded |
| staff number rule `required`, `pattern`, `hint`, `auto_issue` | school or per-branch staff number rule | Stored | PUT, DELETE (`?branch=`) | `school.settings.staff_number_required`, `school.settings.staff_number_pattern`, `school.settings.staff_number_hint`, `school.settings.staff_number_auto_issue` | Staff number rule | RW | open | recorded |
| staff number rule `source` | where the rule comes from | Computed | none | `school.settings.staff_number_source` | Rule source | RO | open | recorded |
| staff number rule `suggestion` | next staff number; restates the registered `school.teachers.staff_number` and shows headcount | Computed | none | `school.settings.staff_number_suggestion` | Next staff number | RO | open (Q-D4); the stricter of this and `staff_number` wins | recorded |
| display `timezone` | school time zone | Stored | PATCH display; config values | `school.settings.timezone` | School time zone | RW | open; Read locked open (Q-D2) | recorded |
| display `source` | where the zone comes from | Computed | none | `school.settings.timezone_source` | Time zone source | RO | open | recorded |
| display `date_format` | date format | Stored | PATCH display | `school.settings.date_format` | Date format | RW | open; Read locked open (Q-D2) | recorded |
| display `clock` | 12 or 24 hour clock | Stored | PATCH display | `school.settings.clock` | Clock | RW | open; Read locked open (Q-D2) | recorded |
| display `branches[].timezone` | a branch's own zone | Stored | PATCH `?branch=`, DELETE | `school.settings.branch_timezone` | Branch time zone | RW | open | recorded |
| display `branches[].id`, `branches[].name`, `branches[].source`, `options`, `date_format_options`, `clock_options` | branch identity, provenance, choices | Transport | none | - | - | - | - | no switch: owner decision |
| security `failed_login_threshold` | failed logins before lock | Stored | PATCH school security (`?branch=`), console, config values | `school.settings.failed_login_threshold` | Failed logins before lock | RW | sensitive: reveals how many guesses an attacker gets; `read_by` school administrator | recorded |
| security `account_lock_minutes` | lock duration | Stored | same | `school.settings.account_lock_minutes` | Account lock minutes | RW | sensitive, as above | recorded |
| security `self_reset_expiry_hours` | self-service reset link life | Stored | same | `school.settings.self_reset_expiry_hours` | Self-reset link expiry | RW | sensitive: reveals how long a reset link lives | recorded |
| security `admin_reset_expiry_hours` | administrator reset link life | Stored | same | `school.settings.admin_reset_expiry_hours` | Administrator reset link expiry | RW | sensitive, as above | recorded |
| security `invitation_expiry_days` | invitation link life | Stored | same | `school.settings.invitation_expiry_days` | Invitation expiry | RW | sensitive, as above | recorded |
| security `proxy_idle_timeout_minutes` | idle timeout for proxy sessions | Stored | same | `school.settings.proxy_idle_timeout_minutes` | Proxy idle timeout | RW | sensitive, as above | recorded |
| security `sources`, `source_scopes`, `overrides`, `compliance`, `scope` | provenance of each of the six values | Computed | none | `school.settings.security_provenance` | Security setting sources | RO | follows the six switches; a nested block is filtered with the field it describes | recorded |
| payroll scope `scope` | central payroll or one per branch | Stored | PATCH payroll-scope; config values | `school.settings.payroll_scope` | Payroll scope | RW | open; Write is the consequential switch (Q-D3) | recorded |
| payroll scope `source` | provenance | Computed | none | `school.settings.payroll_scope_source` | Payroll scope source | RO | follows `payroll_scope` | recorded |
| payroll scope `options` | choices | Transport | none | - | - | - | - | no switch: transport |
| attendance `guardian_notices` | guardian absence notices | Stored | PATCH attendance settings | `school.settings.guardian_notices` | Guardian absence notices | RW | open | recorded |
| attendance `chronic_threshold` | chronic absence threshold | Stored | PATCH | `school.settings.chronic_threshold` | Chronic absence threshold | RW | open | recorded |
| attendance `lesson_marking_levels` | levels that mark lesson registers | Stored | PATCH | `school.settings.lesson_marking_levels` | Lesson marking levels | RW | open | recorded |
| attendance `edit_window_days` | days a register stays editable | Stored | PATCH | `school.settings.edit_window_days` | Register edit window | RW | open | recorded |
| reason code `name` | label of a reason code | Stored | POST, PATCH reason-codes | `school.settings.reason_code_name` | Reason code name | RW | open | recorded |
| reason code `counts_as_authorised` | counts as authorised | Stored | POST, PATCH | `school.settings.counts_as_authorised` | Counts as authorised | RW | open | recorded |
| reason code `requires_note` | a note is required | Stored | POST, PATCH | `school.settings.requires_note` | Requires a note | RW | open | recorded |
| reason code `retired` | retired flag | Stored | POST, PATCH | `school.settings.retired` | Retired | RW | open | recorded |
| reason code `used_this_term` | count of marks (follows register counts) | Computed | none | `school.settings.used_this_term` | Used this term | RO | open | recorded |
| staff profile visibility `policy`, `default_policy`, `groups`, `audiences` | the staff profile access policy, written under `school.field_access.update`; it is itself an access control and must not be hideable by the switches it intersects | Stored | PUT staff-profiles | - | - | - | - | no switch: owner decision |

### 6.2 Finance-owned settings (`finance.settings`)

All are `finance.settings.view` to read and `finance.settings.update` to write
(whole-tenant writer, `views_settings.py`), with history from
`FinanceSettingsHistoryView`. Every value on the Finance settings screens is a
`finance.settings` field with its own Read and Write switch, labelled as the
settings history labels it (`vs_finance.field_access`). A hidden value leaves
its section, its consumer entry, the section and combined history and the audit
trail; a read-only value is named in `_read_only_fields` (`read_only_keys` for
account mappings) and a change to it is refused with 403 `field_write_denied`
before anything is saved. The `@xvs/finance` screens follow the same switches.
Routes are under `/finance/settings/`: `payroll/` (`payroll_settings.py`),
`account-mappings/`, `documents/`, `banking/`, `calendar/`, `records/`
(`views_records.py`), `history/`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| payroll `paye_method` (with `_label`, `_options`), `tax_country` | how PAYE is worked out | Stored | PATCH payroll | `finance.settings.paye_method`, `finance.settings.tax_country` | Where PAYE comes from, Tax table country | RW | open | registered |
| payroll `payment_route` (with `_label`, `_options`) | how staff are paid | Stored | PATCH payroll | `finance.settings.payment_route` | How staff are paid | RW | open | registered |
| payroll `employee_pension_enabled`, `employer_pension_enabled`, `nhf_enabled`, `nsitf_enabled`, `itf_enabled` | statutory deductions on or off | Stored | PATCH payroll | one switch each, `finance.settings.<name>` | as the settings history names each | RW | open | registered |
| payroll `employee_pension_rate_bps`, `employer_pension_rate_bps`, `nhf_rate_bps`, `nsitf_rate_bps`, `itf_rate_bps` | statutory rates | Stored | PATCH payroll | one switch each, `finance.settings.<name>` | as the settings history names each | RW | open | registered |
| payroll `payslip_in_app`, `payslip_email`, `previous_pay_required`, `payroll_moved_here_on` | payslip delivery and earlier pay | Stored | PATCH payroll | one switch each, `finance.settings.<name>` | as the settings history names each | RW | open | registered |
| payroll `paystack_route_available`, `paystack_route_unavailable_reason` | whether the provider route is open: the books' state, not a setting | Computed | none | - | - | - | - | no switch: state of the books |
| account mappings, one row per role key (`CASH_BANK`) | role to ledger account map | Stored | PATCH account-mappings | `finance.settings.mapping_<key>`, travelling under the role key | the role's own label | RW | open | registered |
| documents `default_invoice_due_days`, `default_invoice_narration`, `auto_post_manual_invoices`, `allow_customer_opening_balances`, `term_collection_target_pct`, `auto_apply_customer_credit`, `concession_second_person_threshold` | document and receivables defaults | Stored | PATCH documents | one switch each, `finance.settings.<name>` | as the settings history names each | RW | open | registered |
| documents `primary_collection_bank_account`, `primary_collection_bank_accounts`, `bank_account_options` | the collection account and its options | Stored | PATCH documents | follows `finance.bankaccount` (`is_primary_collection` to change it; bank name and currency to read them) | - | - | - | no `finance.settings` switch: follows the bank account switches |
| banking `default_bank_reconciliation_tolerance_days`, `default_group_reconciliation_matches`, `default_receipt_allocation_strategy`, `apply_transfers_automatically`, `petty_cash_low_balance_threshold_bps` | banking defaults | Stored | PATCH banking | one switch each, `finance.settings.<name>` | as the settings history names each | RW | open | registered |
| calendar `next_year_mode` (with `_label`, `_options`), `next_year_lead_days`, `periods_close_in_order` | fiscal calendar behaviour | Stored | PATCH calendar | one switch each, `finance.settings.<name>` | as the settings history names each | RW | open | registered |
| records `retention_years` (with `effective_retention_years`), `archive_min_age_years` | record retention; the period in force travels with the school's own | Stored | PATCH records | `finance.settings.retention_years`, `finance.settings.archive_min_age_years` | Years financial records are kept, Years before records may be archived | RW | open | registered |
| records `statutory_years` | CodeX's statutory floor | Computed | none | - | - | - | - | no switch: CodeX's floor, the same for every school |
| every section's `updated_at`, `updated_by` | when a setting last changed, and who changed it | Computed | none | `finance.settings.last_changed` | Last changed | RO | open; audit value | registered |
| settings history for the sections above | before and after per setting | Computed | none | - | - | - | filtered per field: a change shows only to a caller who may read that setting | registered |

### 6.3 Not switched in this area

Catalogue depth metadata on the school plan serializer (`PackagePlanSerializer`)
is deliberately unswitched: it is the catalogue, not the school's choice.
`PostingWindowView` is open by design because every date picker needs it.

---

## 7. Area E: receipts, petty cash, issuer profile, entitlements, audit trails, approvals, exports

### 7.1 Receipts (`finance.payment`)

`finance.payment` exists as a permission resource only (view, create, allocate,
reverse, email). The module docstring of `vs_finance/field_access.py` says an
ordinary receipt keeps the switches of `finance.payment`; no such switches are
registered, so the same receipt is switched when it arrives as a payer payment
(`finance.payer_payment`) and open on `GET /finance/payments/`.

Routes: `customers/<pk>/receipt/` (`finance.payment.create`), `payments/` (list),
`payments/summary/`, `payments/<pk>/`, `payments/<pk>/receipt/` (printed, system
surface), `payments/<pk>/allocate/` (`.allocate`), `payments/<pk>/void/`
(`.reverse`), `payments/<pk>/email/` (`.email`). Held receipts and payer payments
are already registered under `finance.interbranch` and `finance.payer_payment`.
Other creators of a receipt row (payer payment shares, forwarded held receipts,
credit transfers, gateway settlement) are system-raised and need no write
assertion. `PaymentSerializer` carries no `FieldAccessMixin`, and
`PaymentDetailView` passes no request. Create body: `amount`, `payment_date`,
`method`, `deposit_account`, `reference`, `narration`, `auto_allocate`,
`allocation_strategy`. List filters `?method=`, `?customer=`, `?search=` and
`?status=` must be refused when the field they select on is unreadable.
Frontend: `FinPro/src/pages/finance/receivables/receipts-allocation-tab.tsx`,
`payment-allocation-drawer.tsx`, `record-receipt-drawer.tsx`,
`record-payment-modal.tsx`, `customer-receipt-modal.tsx`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `id`, `journal_id` as a link target | ids | Transport | none | - | - | - | - | no switch: transport |
| `document_number`, `customer_id`, `customer_code`, `customer_name`, `branch_id`, `branch_name` (absent from the payload today) | receipt number, customer and branch identity | Computed or stored | create (customer from the URL) | - | - | - | - | no switch: owner decision |
| `payment_date` | receipt date | Stored | create | `finance.payment.payment_date` | Receipt date | RW | open | recorded |
| `method` | payment method | Stored | create | `finance.payment.method` | Payment method | RW | open | recorded |
| `amount`, `amount_naira` | amount received; also printed for the payer | Stored | create | `finance.payment.amount` | Amount | RW | open (follows `payer_payment.amount`, which is not sensitive) | recorded |
| `allocated_amount`, `allocation_status`, detail `allocations[]` (`invoice`, `debit_note`, `amount`) | applied to bills | Computed | allocate action; `auto_allocate` on create | `finance.payment.allocated_amount` | Applied to bills | RO | open; applying a receipt stays a business action behind `finance.payment.allocate` | recorded |
| `unallocated_amount`, `credit_remaining` | credit still held | Computed | none | `finance.payment.credit_remaining` | Credit still held | RO | open | recorded |
| `refunded_amount`, `transferred_amount` | paid back or moved on | Computed | refund and credit-transfer side effects | `finance.payment.refunded_amount` | Paid back or moved on | RO | open | recorded |
| `deposit_account_code`, `deposit_account_name` | bank account the money was deposited to | Stored | create `deposit_account` | `finance.payment.deposit_account` | Deposit account | RW | open (Q-C4) | recorded |
| `reference` | reference | Stored | create | `finance.payment.reference` | Reference | RW | open | recorded |
| `narration` | narration | Stored | create | `finance.payment.narration` | Narration | RW | open | recorded |
| `journal_id`, detail `gl_postings[]` | ledger link and ledger lines | Computed | none | `finance.payment.journal` | Journal | RO | open; `gl_postings` shown only if `amount` and `deposit_account` are readable | recorded |
| `status` | state | Computed | void | `finance.payment.status` | Status | RO | open | recorded |
| summary `today`, `week`, `unallocated`, `refunded`, `status_counts` | list-screen totals | Computed | none | `finance.payment.summary_totals` | Receipt totals | RO | open; each total is hidden with the field it sums | recorded |
| detail `open_invoices[]`, `open_debit_notes[]` | the customer's open documents | Computed | none | `finance.customer.open_invoices`, `finance.customer.open_debit_notes` | as registered | RO | follow the customer switches (leak E-L2) | registered |
| create response `payment`, `allocated`, `unallocated` and message | created receipt | Computed | none | `finance.payment.allocated_amount` | Applied to bills | RO | rendered through a read-only serializer | recorded |
| create body `auto_allocate`, `allocation_strategy` | behaviour, decides `allocated_amount` | Transport | create, allocate | - | - | - | - | no switch: transport |
| email `note` | free text typed into the receipt email | Stored | email | - | - | - | - | no switch: transport |
| printed receipt `customer_balance_after`, per-bill `invoice_balance_after`; receipt email body | addressed to the payer | Computed | none | - | - | - | unfiltered by decision; declared system surface | no switch: owner decision |

### 7.2 Petty cash fund and vouchers

`finance.pettycash_return` is registered (the count and the cash banked back). The
fund and the voucher are not. The fund gets its own switches under
`finance.pettycash`; the return's `new_float_amount` ("Float after") stays a
separate switch.

Routes: `petty-cash-funds/` (GET `.view`, POST `.create`), `petty-cash-funds/<pk>/`
(GET, PATCH `.update`), `establish/` (`finance.pettycash.establish`), `replenish/`
(`.replenish`), `reduce/` and `close/` (asserted under `pettycash_return`),
`reopen/` (`.reopen`), `petty-cash-status/` (`.view`, `?threshold_bps`),
`petty-cash-vouchers/` (`finance.pettycashvoucher.view`, `.create`, `.post`).
Serializers: `PettyCashFundSerializer` (no mixin, no request in context),
`PettyCashVoucherSerializer`, `PettyCashVoucherLineSerializer`. Frontend:
`FinPro/src/pages/finance/expenses/petty-cash-tab.tsx`,
`petty-cash-return-drawers.tsx`, `FinPro/src/pages/finance/dashboard-spend.tsx`.
Writes with no `assert_writable` today: fund create, fund PATCH, establish,
replenish, reopen (`float_amount`), voucher create; fund list filter
`?is_active=`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fund `id`, voucher `fund_id` | ids | Transport | none | - | - | - | - | no switch: transport |
| fund `name`, status row `gl_code`, voucher `document_number` | fund and voucher identity | Stored | create, PATCH `name` | - | - | - | - | no switch: identifier |
| fund `branch_id`, create `branch` | fund's branch | Stored, create only | create | - | - | - | - | no switch: owner decision |
| fund `currency` | tenant-wide currency, not a business choice | Stored | create | - | - | - | - | no switch: transport |
| fund `gl_account`, `gl_account_id` | petty cash ledger account | Stored, create only | create | `finance.pettycash.gl_account` | Petty cash ledger account | RW (open on create) | open | recorded |
| fund `custodian_id`, `custodian_name`, `custodian_label`, `custodian_is_exited` | person holding the tin | Stored | create, PATCH `custodian`, `custodian_name` | `finance.pettycash.custodian` | Custodian | RW | open; a staff name already printed on screens | recorded |
| fund `float_amount`, `float_amount_naira`, status row `float_amount` | float (imprest) | Stored | create, PATCH, reopen `float_amount` | `finance.pettycash.float_amount` | Float (imprest) | RW | open (Q-E12) | recorded |
| fund `current_balance`, `current_balance_naira`, status row `current_balance` | cash on hand | Computed | establish, replenish, voucher, return (side effects) | `finance.pettycash.cash_on_hand` | Cash on hand | RO | open (Q-E12); the stricter of this and the return's `book_balance` wins | recorded |
| fund `shortfall`, status rows `shortfall`, `needs_replenish` | amount to top up | Computed | none | `finance.pettycash.shortfall` | To replenish | RO | open; shown only if float and cash on hand are readable | recorded |
| fund `last_replenished_at`, status row `last_replenished_at` | last top-up | Computed | establish, replenish | `finance.pettycash.last_replenished_at` | Last topped up | RO | open | recorded |
| fund `is_active` | active flag | Stored | create, PATCH | `finance.pettycash.is_active` | Active | RW | open | recorded |
| fund `state` | active, inactive or closed | Computed | close | `finance.pettycash.state` | Fund status | RO | open | recorded |
| fund `closed_on`, `closed_by_id`, `closed_by_name`, `closed_by_is_exited` | who closed it and when | Computed | close | `finance.pettycash.closed` | Closed on and by | RO | open | recorded |
| detail `spent_this_week` | spending this week | Computed | none | `finance.pettycash.spent_this_week` | Spent this week | RO | open | recorded |
| detail `register[]` (`date`, `description`, `category`, `in`, `out`, `balance`) | movement register; `category` is the expense account name | Computed | none | `finance.pettycash.movement_register` | Movement register | RO | open; hidden with `cash_on_hand` | recorded |
| establish and replenish bodies `bank_account`, `amount`, `date` | inputs of a transaction | Transport | establish, replenish | - | - | - | gated by `finance.pettycash.establish` and `.replenish` (Q-E5) | no switch: transport |
| reopen body `reason` | audited free text | Transport | reopen | - | - | - | - | no switch: transport |
| voucher `status` | state | Computed | post, cancel | `finance.pettycashvoucher.status` | Status | RO | open | recorded |
| voucher `voucher_date` | voucher date | Stored | create | `finance.pettycashvoucher.voucher_date` | Voucher date | RW | open | recorded |
| voucher `payee` | who was paid, a third party's name | Stored | create | `finance.pettycashvoucher.payee` | Paid to | RW | open (Q-E4) | recorded |
| voucher `spent_by_id` | person who spent | Stored | create | `finance.pettycashvoucher.spent_by` | Spent by | RW | open | recorded |
| voucher `narration`, `reference` | free text | Stored | create | `finance.pettycashvoucher.narration`, `finance.pettycashvoucher.reference` | Narration, Reference | RW | open | recorded |
| voucher `lines[]` (`description`, `expense_account`, `quantity`, `unit_price`, `tax_code`, `cost_center`) | what was bought | Stored | create | `finance.pettycashvoucher.lines` | Voucher lines | RW | open | recorded |
| voucher `subtotal`, `tax_total`, `total`, `total_naira`, line `net_amount`, `tax_amount`, `line_total` | amounts | Computed | none | `finance.pettycashvoucher.totals` | Voucher totals | RO | open | recorded |
| voucher `expense_account` (first line's code and name, "+N") | category summary | Computed | none | `finance.pettycashvoucher.category` | Category | RO | open | recorded |
| voucher `journal_id` | ledger link | Computed | none | `finance.pettycashvoucher.journal` | Journal | RO | open | recorded |
| return `previous_float_amount`, `book_balance` | restate the fund's float and cash on hand | Computed | none | `finance.pettycash_return.previous_float_amount`, `finance.pettycash_return.book_balance` | as registered | RO | stricter wins with the fund switches | registered |
| return `fund_id`, `fund_name` | the fund a return belongs to | Stored label | none | - | - | - | - | no switch: identifier (a name is a label, like a bank account's name; the earlier `finance.pettycash_return.fund` switch is removed) |

### 7.3 CodeX issuer profile, onboarding defaults and entitlements

Seven `platform.profile.*` configuration keys and four `platform.onboarding.*`
defaults hold CodeX's own letterhead and the defaults for new schools
(`vs_config/platform_settings.py`; `PLATFORM_ISSUER` in Django settings is the
fallback). No bank, tax identification or invoice-footer setting exists for the
issuer: the pay-to bank on a CodeX invoice is the platform ledger entity's
collection `BankAccount`, governed by `finance.bankaccount`, and the printed
footers are static text. Nothing is registered. Route:
`/config/platform-settings/` (`PlatformSettingsView`, platform tenant only,
`config.value.view` and `config.value.update`). Alternate paths that write or
read the same rows: `POST /config/values/`, `DELETE /config/values/<key>/`,
`/config/effective-values/`; `PROTECTED_SETTING_KEYS` refuses security,
integration and provider keys but not these. Frontend:
`console-fe/src/pages/protected/settings/index.tsx`,
`console-fe/src/redux/services/config-api.ts`.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `profile.name` | issuer name | Stored | PATCH; `/config/values/` | - | - | - | - | no switch: identifier |
| `profile.tagline` | letterhead tagline | Stored | PATCH; `/config/values/`, reset | `platform.profile.tagline` | Tagline | RW | open (printed on every CodeX invoice and receipt) | recorded |
| `profile.address` | issuer address | Stored | PATCH; `/config/values/`, reset | `platform.profile.address` | Address | RW | open | recorded |
| `profile.email` | issuer email | Stored | PATCH; `/config/values/`, reset | `platform.profile.email` | Email | RW | open | recorded |
| `profile.phone` | issuer phone | Stored | PATCH; `/config/values/`, reset | `platform.profile.phone` | Phone | RW | open | recorded |
| `profile.website` | issuer website | Stored | PATCH; `/config/values/`, reset | `platform.profile.website` | Website | RW | open | recorded |
| `profile.logo_url` | issuer logo | Stored | PATCH; `/config/values/`, reset | `platform.profile.logo_url` | Logo | RW | open (also the brand mark of every product email) | recorded |
| `onboarding.ownership_type` | default ownership type for new schools | Stored | PATCH; `/config/values/` | `platform.profile.default_ownership_type` | Default ownership type | RW | open | recorded |
| `onboarding.term_structure` | default term structure | Stored | PATCH; `/config/values/` | `platform.profile.default_term_structure` | Default term structure | RW | open | recorded |
| `onboarding.currency` | default billing currency of new schools | Stored | PATCH; `/config/values/` | `platform.profile.default_currency` | Default currency | RW | open | recorded |
| `onboarding.branch_country` | default branch country | Stored | PATCH; `/config/values/` | `platform.profile.default_branch_country` | Default branch country | RW | open | recorded |
| `sources.profile.*`, `sources.onboarding.*`, `options.*`, `reason` | provenance, choices, audit text | Computed | none; PATCH | - | - | - | each source drops with its value | no switch: transport |
| entitlement `starts_at`, `ends_at` (`/config/entitlements/`, `bulk-schedule/`, school plan uplift) | window of a capability grant; `ends_at` repeats the subscription expiry | Stored | `EntitlementListSetView` POST, bulk schedule | `platform.entitlements.window` | Subscription window | RW | open; the stricter of this and `platform.schools.subscription_expires_at` wins | recorded |
| entitlement calendar entries (`tenant_slug`, `tenant_name`, `starts_at`, `ends_at`, `status`, `warning`, `days_until_expiry`), `summary` (`expired`, `expiring_7`, `expiring_30`, `expiring_90_days`, `scheduled`) | renewal calendar | Computed | none | `platform.entitlements.calendar` | Subscription expiry | RO | open; a row shows only to a role who may read the school's expiry | recorded |

The other keys of a capability grant (`state`, `source`, `capability`,
`updated_by`) carry grant state, not the subscription date, and are outside this
ledger.

### 7.4 Audit trails

Only nine protected-word rules exist, from two modules (schools, students):
`school.students.status_move`, `school.students.stage_move`,
`schools.plan_price`, `schools.billing_email`, `schools.billing_phone`,
`schools.profile_address`, `schools.profile_registration_id`,
`schools.branch_address`, `schools.branch_email`. None touches finance,
procurement, payments, staff, user, config or import. A rule cuts only
`summary`, `metadata["reason"]` and top-level keys of `before_data` and
`diff_data`; it cannot cut other metadata keys, which is where finance values
travel, and it matches `entity_type` exactly while finance mirrors write
`vs_finance.<Target>`.

| Viewer | Route and permission | What it serves | Filtered today |
| --- | --- | --- | --- |
| Finance trail | `finance/audit-logs/`, `audit-logs/<pk>/`, `facets/`; `finance.audit.view` | `action`, `status`, `actor`, `target_type`, `target_id`, `document_number`, `message`, `before`, `after`, branch labels; not `metadata` | `before` and `after` for four targets only (`EmployeeSalary`, `FinanceReceivablesPolicy`, `Customer`, `BankAccount`, via `AUDIT_SNAPSHOT_FIELDS`); `message` and every other target are unfiltered |
| Settings history | finance and procurement settings screens; `finance.settings.view` | `changes[]` built from the filtered before and after | filtered (receivables policy only registered) |
| Document activity feeds | vendor contract, RFQ, quotation, stock item, vendor bill, vendor payment | `entry_sentence` (figure-free; an older entry is rebuilt from its action and document number) and `reason` | the sentence carries no registered value; `reason` is open unless the field is registered |
| Procurement approval queue | approval detail `activity[]` | `WorkflowAuditLog.message`, written by the workflow engine | nothing to filter: the sentences carry a person's label only (E-L40) |
| Central trail | `/audit/events/`, `/audit/events/<id>/`, entity trails, `/audit/me/activity/`, export, Export Centre dataset `audit.events`, dashboard; `platform.audit.view`, `platform.audit.export` | list `summary`, `entity_label`, `ip_address`; detail `before_data`, `diff_data`, `metadata` | the nine protected-word rules only |
| Config audit | `/config/audit-events/...`; `config.audit.view` | `before_data`, `after_data`, `reason`, metadata of every configuration value | none |

The central trail mirrors every finance and procurement entry (`_mirror_to_central`
in `vs_finance/audit.py`): `summary` is the finance message and `metadata` is
`finance_action` plus every metadata argument. Because the tenant is set, a row
is visible to the school's own audit officers, narrowed to their branches; a
platform-tenant reader is not narrowed by tenant or branch.

Registered fields whose values reach an audit row (message, `before`, `after` or
metadata) without a Field Access check:

| Registered field | Audit target and what is stored | Protected today |
| --- | --- | --- |
| `finance.bankaccount` balances | reconciliation `difference`, amounts in bank charge messages | no |
| `finance.bankaccount` account number and bank name | branch-split row `before`; bank account create and edit write no audit row | finance `before` and `after`: yes; the gap is a missing trail |
| `finance.salary` figures and identifiers | `EmployeeSalary` before and after; messages name the employee; metadata `reason`, `effective_from`, `version_id` | `before` and `after`: yes; message and central metadata: no |
| `finance.payrollrun` totals | PAYROLL_POSTED message and metadata (`gross`, `paye`, `pension`, `other_deductions`, `employer_contributions`, `net`); PAYROLL_PAID | no |
| `procurement.vendor` and `procurement.vendor_contact` email | PURCHASE_ORDER_EMAIL_* metadata `recipients`, `bcc`, `notification_ids`; vendor create and update write no audit row | no |
| `finance.interbranch` | held-receipt, recharge and transfer messages and metadata (`amount`, `share`, `basis`), customer name, narration | no |
| `finance.deposit` | DEPOSIT_RELEASED and DEPOSITS_FORFEITED messages and metadata (customer code, amounts, per-customer list) | no |
| `finance.deferredincome` | released amount, invoice ids | no |
| `finance.provision`, `finance.settings.provision_bands` | PROVISION_POSTED message and metadata (`required`, per-branch lines, `bands`) | no |
| `finance.settings` | `FinanceReceivablesPolicy` before and after | `before` and `after`: yes; other settings targets are unregistered |
| `finance.customer` | CUSTOMER_UPDATED `before` and `after`; the message lists the changed field names; deactivation `reason` in central metadata | finance `before` and `after`: yes; central trail: no |
| `finance.payer_link`, `finance.payer_payment` | payer and customer names, amounts, receiving bank, per-customer shares | no |
| `finance.pettycash_return` | PETTY_CASH_RETURN_POSTED and RETURN_VOIDED message and metadata (`counted`, `book_balance`, `shortage`, `overage`, `new_float`) | no (`PettyCashReturn` is not in `AUDIT_SNAPSHOT_FIELDS`) |
| `finance.banktransaction`, `finance.banktransfer` | BANK_TRANSACTION_EDITED and BANK_TRANSFER_EDITED `before` and `after`; posted-message amount, bank name, narration | no (neither target is in `AUDIT_SNAPSHOT_FIELDS`) |
| `finance.fiscalyear.net_income`, `finance.period` | FISCAL_YEAR_CLOSED message and metadata (`net_income`, `net_by_branch`, `overridden_checks[]`); forced-close `reason` | no |
| `school.students.*` | Student UPDATE `before_data` is the whole detail payload as the editor could read it; `diff_data` lists old and new values; `entity_label` and summary carry the full name | status and stage reason words only |
| `school.guardians.*` | link and unlink summaries name guardian and student | identity only; low |
| `school.teachers.employment_status`, `exit_date` | StaffProfile status moves: metadata `from_status`, `to_status`, `reason`, `effective_date`, `last_working_day` | no |
| `platform.schools` agreement fields | `agreed_price_per_student`, `minimum_billable_students`, `package_plan`, `subscription_starts_at`, `subscription_expires_at`, `notes`, uplift: no rule | no (five of the school fields are covered) |
| `finance.settings` and all configuration values | `ConfigurationValue` events: `before_data`, `after_data`, mirror metadata | no |

Rejection rows (`record_rejection`) store `str(exc)[:255]`, so a refusal such as
"Refund of N exceeds C001's credit available on 3 Sep (N)" becomes a stored
message with amounts and balances.

Recommended fix shape: stop mirroring values into the central trail (send a
neutral summary and identifier metadata only); give the finance trail a message
policy (figure-free messages with figures in `before` and `after` under registered
names, or a template plus structured values rendered per reader); extend
`AUDIT_SNAPSHOT_FIELDS` to every registered target that writes `before` and
`after`; pass the request into the procurement activity feeds; write the Student
audit snapshot from the registered key list; close the bank account and vendor
audit gaps. The first two are decided (Q-E10 and Q-E11, answered in 8.1).

### 7.5 Approval inbox

At submission the engine stores the handler's `document_summary` and
`document_details` on the `WorkflowInstance`. `document_details` supports a
per-item Field Access marker that `for_reader` applies; only the payout batch
handler uses it. `document_summary` has no marker support at all and is served to
every approver exactly as written; the same text becomes the list title, the
inbox search target and the subject and body of the workflow notification. An
approver needs no finance key to read a finance document's summary, so a
registered field closed to an approver is still read in the card.

| Document type (handler) | Copied into the unfilterable summary | Copied into details (marker available, unused) | Registered source | Status |
| --- | --- | --- | --- | --- |
| `finance.bank_transaction` | Date, Amount, Bank account | Direction, Other side, Narration, Reference | `finance.banktransaction` | recorded |
| `finance.bank_transfer` | Date, Amount, From, To | Narration, Reference | `finance.banktransfer` | recorded |
| `finance.petty_cash_return` | Date, Banked, Fund | Kind, Counted, On the books, Short, Over, Why the count differs, New float, Into | `finance.pettycash_return` | recorded |
| `finance.inter_branch_transfer` | Date, Amount, From, To | Kind, bank accounts, Purpose, Repay by, Reference | `finance.interbranch` | recorded |
| `finance.doubtful_debt_provision` | Aged to, Allowance required, Change to the allowance | Narration; table By branch (Required, Held, Change) | `finance.provision` | recorded |
| `finance.refund` | Date, Customer, Amount | Method, Reference, Narration, Payment account | `finance.refund` (recorded) | recorded |
| `finance.write_off`, `finance.concession`, `finance.credit_note`, `finance.customer_credit_transfer`, `finance.journal` | amounts, customer codes | reasons, narrations, lines | unregistered | recorded |
| `finance.expense_claim` | Claimant (marked with `finance.expenseclaim.claimant`); Date, Total | Purpose, Narration, Subtotal, Tax, claim items | `finance.expenseclaim` (claimant only) | registered for the claimant; recorded for the rest |
| `procurement.requisition`, `.purchase_order`, `.vendor_invoice`, `.vendor_payment`, `.vendor_credit_note` | Amount, Vendor | prices, payment terms, WHT, net payment, bill total and balance | unregistered (vendor name is an identifier) | recorded |
| `payments.payout_batch` | Items, Total | Provider, Source account; Beneficiaries table with markers on `beneficiary` and `account` | `payments.payout` (the model to copy) | registered |
| `school.leave` | title is the staff name; subtitle is "Leave request"; Type (marked `school.leave.leave_type`), Dates (`school.leave.dates`), Over allowance (`school.leave.balance`) | Days (`school.leave.days`), Resumption date (`school.leave.dates`), Job title (a label, unmarked), Reason (`school.leave.note`), Over allowance by (`school.leave.balance`) | `school.leave` | registered |
| `school.hire` | title is the person's name; subtitle is the job title; Posting, Starting role | Email, Staff ID, Employment type, Hire date | `school.teachers` | recorded |
| `PLATFORM_USER_CREATION` | title is the full name or email; Role | Email, Phone | `platform.team` registers other fields, not these (phone: X-L15) | recorded |
| `rbac` role change and grant | Raised by, Reach | permission lists | none | no switch: transport |

Payroll runs, deposits and deferred income have no approval handler, so they have
no inbox path. Finance document summaries carry a document number and kind only
in the notification title; no finance figure reaches the subject today.
Recommended fix at the choke point: let summary `fields` carry an `access` key,
pass the request into `summary_for_reader`, mark every registered figure in the
finance handlers (about forty entries) and in `school.hire` and `school.leave`,
and keep titles to a document number and kind, except that a person's name or job title in a title is a label and always shows (Q-E9, answered in 8.1).

### 7.5a Expense claims (`finance.expenseclaim`, claimant registered)

The claimant is picked from the staff list (`claimant`, an account id in the
entity's own tenant) or typed for somebody not on it (`claimant_name` alone).
Both are one fact, so they share one switch. Every other claim field is still
unregistered and open.

| Response field(s) | What it is | Kind | Write paths | Switch | Label | RO/RW | Default | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `claimant_id`, `claimant_name`; body key `claimant` (write alias); export `claimant_name`, `claimant_email`; approval card Claimant; spending dashboard `claims.oldest[].claimant`; list search `q` on the claimant | who the claim reimburses | Stored | create (`assert_writable`, not open on create) | `finance.expenseclaim.claimant` | Claimant | RW | open | registered |
| `title`, `narration`, `claim_date`, totals, `lines[]`, `payment_status` | the rest of the claim | Stored and computed | create | - | - | - | - | recorded |

Tests: `vs_finance.tests_expense_claim_claimant` (hidden, read-only and
writable on the list, detail, search, create, `/me` map, approval card and
dashboard; the export link), `ExpenseClaimDeepPayloadTests` in
`vs_finance.tests_field_access_deep_payload`. Screen:
`FinPro/src/pages/finance/expenses/claimant-field.tsx`.

### 7.6 Other restating paths: exports, search, overview

| Path | What it restates | Status |
| --- | --- | --- |
| Export `finance.customer_receipts` and the receipts quick export | receipt number, customer name, date, amount, allocated, refunded, status, method, reference, deposit account code, narration; only `finance.payment.view` gates it | recorded: every column needs `access="finance.payment.<name>"` the day the resource registers |
| Export `finance.gl_postings` | per-line account, debit, credit, description; rebuilds statements, bank movement, petty cash, payroll and deposits | recorded: no field key exists for a ledger line |
| Export `platform.schools` | `address`, `registration_id` (marked sensitive, no `access=`), `website`, `motto` | exempt: the columns follow the platform key of the export, not `school.profile` switches (Q-E13, answered in 8.1) |
| Export `procurement.purchase_orders` | `payment_terms` | recorded |
| Export `audit.events` and `write_audit_export_file` | `summary`, `entity_label` | the nine rules only |
| Export `workflow.approvals` | `document_reference` only | clean |
| Export `payments.collections` | `payer_email`, `payer_name` (sensitive, no `access=`) | recorded: no registered source |
| Document emails: invoice, receipt, statement, credit and debit note, dunning, payslips | amounts, running balance, issuer identity | system surface by decision |
| Settings search (`vs_finance/search_places.py`) | label of every finance setting, including the seven registered `finance.settings` fields (for example "Doubtful-debt age bands") | `Place` carries a permission key but no Field Access key |
| People search | student, guardian, staff hits | clean: rendered through the registered hit serializers |
| Console overview signals (`vs_admin_console/overview.py`) | `fiscal_runway`, `overdue_invoices.count`, `unallocated_credit.count`, `vendor_invoices_unpaid.count` | counts across the whole tenant with no branch narrowing |
| Month and fiscal-year close refusals | failed check names | clean: named only if readable |

The issuer identity has two sources: the printed document uses
`platform.profile.name`, while email subjects, payslips and credit-note emails
use the ledger entity name, and the email brand name is the fixed word "CodeX".
A CodeX customer record copies the school's email, phone and address on every
school save (`platform_customer.py`), so the same value sits under
`finance.customer.billing_*` and `platform.schools.billing_*`; the stricter wins.

---

## 7A. Area F: messages (`school.messages`, registered with the feature)

Module 32 (`schools.vs_messages`) registered its Field Access decisions in the
same change that built it, so nothing in this area is open work: every row is
`registered` or deliberately left without a switch. The resource is
`school.messages`, declared in `schools/vs_messages/field_access.py`, tenant
scope, every field open by default, across ten serializer surfaces. Routes:
`/v1/messages/` (staff), `/v1/messages/oversight/` (inspection, which follows the
same switches) and the portal routes `/v1/portal/parent/messages/` and
`/v1/portal/student/messages/`. Design: `docs/messaging/messaging-design.md`,
sections 12 and 14.2.

| Response fields | What it is | Kind | Write paths | Switch | Label | Access | Open or sensitive | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `body`, `preview`, `last_message.preview`, the export's `message` column, body search | the text of a message | Stored | POST `conversations/<id>/messages/`, the first message of POST `conversations/`, `groups/` and `broadcasts/`, POST `broadcasts/<id>/reply/` | `school.messages.body` | Message | RW | open | registered |
| `title`, `name`, `display_name` where it is a title, the alert's `conversation_label` | what a group or a broadcast is called | Stored | POST `groups/` and `broadcasts/`, PATCH `groups/<id>/` | `school.messages.title` | Group or broadcast name | RW | open; open on create | registered |
| `counterpart`, `participants`, `sender`, `sender_name`, `members`, `display_name` where it is a person, the export's `sender` column, the alert's `sender_label`, name search, `recipient` and `guardian` on write | who is in a conversation and who wrote | Stored (chosen on write), Computed (read) | POST `conversations/` (`recipient`, `guardian`), POST `groups/` (`members`), POST `groups/<id>/members/`, DELETE `groups/<id>/members/<user_id>/` for another member, POST `.../owner/` | `school.messages.participants` | People in the conversation | RW | open; open on create | registered |
| `recipients`, `recipient_count` | who a broadcast went to, and how many | Stored | POST `broadcasts/` | `school.messages.recipients` | Broadcast recipients | RW | open | registered |
| `child`, `student` on write, child-name search | the pupil a family or pupil conversation is about | Stored | POST `conversations/` (`student`) | `school.messages.child` | Pupil the conversation is about | RW | open | registered |
| `reply_mode` | whether a broadcast takes private replies | Stored | POST `broadcasts/` | `school.messages.reply_mode` | Broadcast reply setting | RW | open | registered |
| `sent_at`, `last_activity_at`, `last_message.sent_at`, the export's `sent_at` column | when messages were sent | Computed | none | `school.messages.sent_at` | Sent times | RO | open | registered |
| `unread_count`, the totals of `unread-count/` | unread messages per conversation and in total | Computed | none (`read/` moves server state, never a client value) | `school.messages.read_status` | Read status and unread counts | RO | open | registered |
| `receipt` on each of the caller's own messages (`state`, `read_count`, `recipient_count`), the polling route's `receipts` list, the detail's `receipts` flag | whether the people who could see a message the caller sent have read it (one tick sent, two read; "Read by n of m" in a group or broadcast) | Computed | none (worked out from the readers' read watermarks) | `school.messages.read_receipts` | Read receipts | RO | open; absent everywhere while the school setting `messages.read_receipts` is off, and never shown to an inspector or a watching parent | registered |
| `kind`, `kind_label` | what sort of conversation | Stored | POST `conversations/` (`kind`) | - | - | - | - | no switch: identifier |
| `branch` on a group or broadcast row (a multi-branch school only), `branch` on create | the branch a group or broadcast belongs to | Stored | POST `groups/` and `broadcasts/` | - | - | - | decided by branch reach alone (owner decision of 12 October 2026: no branch switches) | no switch: owner decision |
| `id`, `user_id`, the cursor (`has_more`, `next_before`), page fields, `member_count` (a count of children), `role`, `mine`, `can_send`, `read_only`, `read_only_reason`, `inspection` | ids, paging, counts of children and action flags | Computed | none | - | - | - | - | no switch: transport |
| every field of the portal routes `/v1/portal/parent/messages/` and `/v1/portal/student/messages/` | the parent's and the pupil's own payloads | Computed | the portals' send and start routes | - | - | - | parents and pupils hold no role, so a switch cannot reach them; the payload is a fixed narrow shape (`portal_serializers.py`), and its input serializers are on the write-path allowlist of `vs_rbac.tests.test_field_registry` with that reason | no switch: owner decision |

Rules the registration settles:

* Write off on Message refuses sending, on Broadcast recipients refuses
  addressing a broadcast, on Child refuses naming a child on a new family or
  pupil conversation, and on Broadcast reply setting refuses choosing it (a
  broadcast sent without one is one way). Title and People are open on create,
  so whoever may make a group names it and chooses its first members; the Write
  switch then decides who may rename it or change its members.
* `display_name` has no switch of its own. It follows Group or broadcast name
  where it is a title and People in the conversation where it is a person,
  decided in code by the serializers.
* A label never carries a hidden name: a guardian reads "Mother of Tunde Bello"
  only while the Child switch is readable, otherwise "Mother".
* Leaving a group is not a change of members the Write switch can refuse;
  removing another member, adding members and making an owner are.
* Alerts follow each staff recipient's own switches: hidden People gives "a
  colleague", "a parent" or "a pupil", and a hidden group or broadcast name gives
  "a staff group" or "a broadcast". Tests: `schools.vs_messages.tests.test_field_access`,
  `test_field_access_deep_payload` and `test_alerts` (`FieldAccessInAlertsTests`).

---

## 8. Open product questions

Each question is one the registration cannot settle alone. The example shows
what each answer does on a real day; the recommendation is the audit's, where it
gave one. A question is closed by recording the answer in section 2 or in the
answered table (8.1) and updating the rows that cite it.

### 8.1 Already answered

| Audit question | Answer |
| --- | --- |
| Branch switches on customers, bank accounts, bank documents, inter-branch (Q-B1), refund branch, hiding the chart's bank branch id (Q-B9) | Branch has no switch. `bank_branch_id` stays open; `bank_account_id` follows `finance.bankaccount.ledger_account`. Reach alone decides where a record may be raised. |
| Document numbers get a Read switch or not (Q-B2) | They are identifiers; no switch. |
| Blocks shared with receipts (Q-B3), one number under two switches (the petty cash fund against the return, CodeX's customer record against the school record, entitlement windows against the school expiry), vendor bank details on a payout (Q-C5), stock transfer value against inter-branch amount (Q-C1) | The stricter switch wins. |
| Customer contact details sensitive by default (Q-B4, first part) | Open by default. |
| Invoice and credit-note unit prices grouped with the lines or apart (Q-B5, first part) | Apart: their own switches. |
| Printed or emailed documents filtered by the sender (Q-B6, receipt balance line) | Unfiltered; system surfaces. |
| Aging apart from the balance (Q-B7) | Apart: its own switches. |
| Customer on a receipt a field or a label (Q-E1) | A label; no switch. |
| Do the switches bind CodeX staff on the generic configuration endpoints (Q-D6) | No. CodeX staff are governed by platform keys, not by a school's switches. The generic configuration endpoints (`/v1/config/values/`, `effective-values/`, `export/`) and the CodeX school export are exempt for tenant switches. A `PLATFORM` field is judged in the operator's own tenant (`vs_rbac.field_evaluator.get_acting_field_access`, X-L10). |
| A mandatory-field rule for a caller who cannot write the field (Q-D7) | The rule yields: a caller who cannot supply the field is not bound by it, and somebody with Write completes it later. Built (D-L4, D-L11). |
| Person names and job titles on approval cards and notifications (Q-E9) | Labels; they always show. Figures on the card follow the switches. Built. |
| Does the central audit mirror keep copying values (Q-E10) | No. The mirror stops copying values and carries who, what, which document, branch and time only. Built (E-L14). |
| How finance trail sentences hide a figure (Q-E11) | The sentence is figure-free everywhere; the figures sit in the per-field filtered `before` and `after`. Built. A row written before the rule is rebuilt on read from the action and the document number; its stored text is untouched (E-L26). |
| Do CodeX roles follow the school's profile switches (Q-E13) | No. CodeX roles follow platform keys only. The address and registration columns of the `platform.schools` export are not governed by `school.profile` switches. |
| Document numbers, petty cash fund names and bank account names | Identifiers; no switch. |
| Statement figure grouping (Q-A1) | One switch per statement section. Revenue lines, expense lines and net income are separate decisions; every JSON response and export follows the same section switches. |
| Statutory pack with a closed section (Q-A2) | Refuse the pack with 403. A partial filing must not look complete. |
| Customer aging rows and totals (Q-A3) | Separate `aging_customers` and `aging_totals` switches. |
| Budget amount default (Q-A4) | Sensitive. Planned amounts start closed for both Read and Write; each grant is separate. |
| Budget lines and amounts (Q-A5) | Separate switches for the line set and planned amounts. |
| Budget title (Q-A6) | Switch the free-text name; the budget code remains an identifier. |
| Aggregates of switched source figures (Q-A7) | Report section switches govern aggregates. Individual pay switches do not remove aggregate salary accounts from statements; `analytics_slice` has its own switch. |
| Choosing the bank account for a payment or refund (Q-C4) | Give the account choice its own Read and Write switch. Branch reach still limits which account can be selected. The refund switch is registered; receipt and procurement switches remain for their areas. |

### 8.2 Area A: budgets and statements

All Area A questions are answered in section 8.1.

### 8.3 Area B: receivables, ledger and banking (answered)

Q-B4: customer source references and bank statement balances are sensitive.
Customer balances, invoice amounts and journal totals stay open. Q-B5: one
switch governs each complete journal line, including its debit and credit.
Q-B8: a statement balance has a writable switch of its own, and its Read
requires the account's statement-balance switch. Q-B10: a customer's billing
address is open on create like the other contact fields.

### 8.4 Area C: procurement

**Q-C2. Does stock valuation ship open or sensitive?** Tunde, Ikeja Branch's
storekeeper, issues 20 reams of paper and sees a unit cost of N4,200 and a stock
value of N84,000. Open: every role with `procurement.stock.view` sees cost, and a
school closes it per role. Sensitive: Tunde sees quantities only until the bursar
opens cost and value. Recommendation: open.

**Q-C3. Does entered money on procurement documents ship open or sensitive?**
Ikeja's bursar sets a N2.5m ceiling on an RFQ for generators. Open: a buyer who
reads the ceiling can tip a vendor to bid just under it. Sensitive: buyers do not
see the ceiling until the bursar grants it, and no other price field works this
way. The same question covers unit prices, estimated totals, quoted prices,
contract value, standard price and the free-text assessment of a supplier.
Recommendation: open for all, with two sensitive exceptions: RFQ `budget_estimate`
and vendor assessment `notes`.

**Q-C6. Are a vendor's accounting defaults one switch or three?** Mrs Eze, Ikeja
Branch's purchasing clerk, may pick the stationery vendor's expense account. One
switch "Accounting defaults": she also changes its withholding tax code, which
alters the tax withheld on every payment to that vendor. Three switches (payable
account, default expense account, default WHT code): she keeps the expense account
and loses the WHT code, which only the bursar changes. Recommendation: three.

**Q-C7. Do attachments on bills and payments ship open?** Ada's role hides
`procurement.vendor.bank_account_number`, yet she opens Adex's invoice scan, which
prints "GTBank 0123456789". An uploaded file cannot be filtered. One RW
`attachments` switch per document, open by default: a school closes it for roles
whose bank details are hidden. Sensitive by default: nobody reads scans until the
school grants it. Recommendation: open, one switch per document.

### 8.5 Area D: settings

**Q-D1. Per-field switches on settings, or one key per screen?** Bright Star
School's registrar holds `school.settings.update` to change the admission-number
pattern. Per field: she keeps editing admission numbers while staff leave
allowances, the starting role and payroll scope stay closed to her role. Per
screen: she edits all of Staff or none of it, and a school that wants her on
admission numbers but off leave must split the key anyway. Leave as it is: the
same registrar can set the starting role for new staff to Principal.
Recommendation: per field, with consequential fields on their own Write switch.

**Q-D2. Do vocabulary and display values get a Read switch?** Greenfield's teacher
sees "Semester" and dates as 12/10/2026 on every screen because the login tenant
block and the open GET carry them. No Read switch (Write only through the key):
hiding "Semester" from a role would make that role's calendar and registers print
the wrong word. A Read switch: the tenant block and open GETs also drop the value
for that role, which blanks term names on their register. The registry cannot
express "Write only", so the rows register these as RW with Read locked open.
Recommendation: no Read switch.

**Q-D3. Is payroll scope a pay field?** Lekki Branch's bursar can already read
`finance.salary` figures. Switching Bright Star School from one payroll to one per
branch changes whose pay runs where. Register it with Write closed to branch roles
(the guard already needs a whole-school caller); or no switch (one key already
controls it); or move it under the payroll resource. No recommendation.

**Q-D4. Is the "next number" suggestion identifying?** The admission number rule
tells any registrar at Greenfield that the next number is GF/2026/0842, so the
school has about 842 pupils this year; the staff number rule does the same for
headcount. Read-only and open: the enrolment form needs it. Sensitive: the form
falls back to showing only the pattern hint. Recommendation: open.

**Q-D5. Do the finance-owned settings join the school-settings change or the
finance change?** Bright Star's bursar edits the concession second-person
threshold and the pension rates on the finance settings screen. In the school
settings change, those switches sit under `school.settings` beside admission
numbers. In the finance change, they sit under `finance.settings` beside the
receivables policy already closed to the Ikeja clerk. Recommendation: the finance
change, because they carry money. Settled: they are `finance.settings` fields
(section 6.2).

### 8.6 Area E: receipts, petty cash, issuer, audit and approvals

**Q-E4. Do petty cash voucher payee and spent-by ship open or sensitive?** Ikeja
Branch records "Paid to Mama Tunde, market woman, N4,500 for chalk". Open: every
finance role reads who was paid. Sensitive: only a role that was switched on reads
the payee, because it is a third party's name typed by a user. The audit left the
fields open; the rows ship them open.

**Q-E5. Are the establish and replenish amounts writes of "Cash on hand"?** Lekki's
custodian may replenish the tin with N50,000 but must not see the bank balance it
came from. Action inputs gated by `finance.pettycash.establish` and `.replenish`:
she tops up and never needs to read the balance. Writes of the read-only Cash on
hand: nobody could ever top up a tin. Recommendation: action inputs.

**Q-E12. Do a petty cash fund's float and cash on hand ship open or sensitive?**
Greenfield's front-desk tin holds N150,000. Open (the convention of the return
resource): the secretary who runs it and a new auditor both see it. Sensitive: the
auditor sees nothing until the school switches it on. Recommendation: open.

---

## 9. Leaks found

A leak is a value governed by a registered switch (or a branch scope narrower
than the payload) that another payload serves without asking. IDs are stable:
`A-` to `E-` follow the five areas, `X-` marks items found outside them.

How a status was chosen:

* `to be fixed in this wave`: the leak concerns a switch that exists today, or a
  branch scope wider than the caller's reach, and a check against the existing
  switch or scope closes it. Where the right closure depends on an open question,
  the row says which, and the row stays in the wave because the leak is real
  either way.
* `recorded`: the leak concerns fields that have no switch yet, or it is a
  statement, budget-against-actual or report export (the report change handles
  those with their own switches, under the stricter-wins rule), or an owner
  decision in section 2 removed it. It closes when the field or report is
  registered.

### 9.1 Area A: budgets and statements

| ID | Leak | Status |
| --- | --- | --- |
| A-L1 | The income statement carries budget and variance columns, JSON and export, under `finance.report.view` only; no budget permission is needed. | fixed: report budget columns require the report section and budget amount switches |
| A-L2 | Budget variance, heatmap, roll-up and list carry actual income and expense per account, including unbudgeted P&L accounts, under `finance.budget.view` only. | fixed: actual figures require budget actual and the matching income section switch |
| A-L3 | Budget list, variance, heatmap and roll-up serve the figures of the registered `finance.dashboard` budget cards under a separate guard. | fixed: budget figures use the budget switches across these paths |
| A-L4 | Budget name, branch and status reappear in dashboards, the roll-up, response messages, error text and audit rows. | fixed for switched values: the free-text name follows its switch; branch remains an identifier |
| A-L5 | Reports restate eleven registered `finance.dashboard` cards (net income, receivables, payables, cash, bank accounts, cash movement, aging, fixed assets, tax, spending, branch comparison). | resolved by Q-A7: report section switches govern these aggregates |
| A-L6 | The income statement, changes in equity, balance sheet and pack restate the registered `finance.fiscalyear.net_income`. | resolved by Q-A7: report section switches govern these aggregates |
| A-L7 | Trial balance, AR reconciliation, balance sheet, cash flow, equity and pack flags restate the registered `finance.period` close checks. | resolved by Q-A7: report section switches govern these aggregates |
| A-L8 | Statements show live balances every day; the registered `finance.seal` serves the comparison against the sealed balances. | resolved by Q-A7: report section switches govern these aggregates |
| A-L9 | Balance sheet, trial balance, pack and the ledger dataset restate the registered `finance.deferredincome` balances. | resolved by Q-A7: report section switches govern these aggregates |
| A-L10 | Balance sheet, P&L and trial balance restate the registered `finance.provision` allowance. | resolved by Q-A7: report section switches govern these aggregates |
| A-L11 | Balance sheet and trial balance restate the registered `finance.deposit` liability. | resolved by Q-A7: report section switches govern these aggregates |
| A-L12 | Balance sheet and trial balance restate the registered `finance.interbranch` due-to and due-from balances. | resolved by Q-A7: report section switches govern these aggregates |
| A-L13 | Balance sheet cash lines, cash flow opening and closing cash and the trial balance restate the registered bank balances. | resolved by Q-A7: report section switches govern these aggregates |
| A-L14 | Statements restate aggregate pay (`finance.salary`, `finance.payrollrun`); a cost centre of one person in the analytics slice reads as that person's pay (Q-A7). | resolved by Q-A7: report section switches govern these aggregates |
| A-L15 | Every report's export (CSV, XLSX or PDF) rebuilds the same figures with no field awareness. | fixed: exports use the filtered report payload |
| A-L16 | The `finance.gl_postings` dataset (schedulable) rebuilds every statement from raw ledger lines. | pending with the Area B journal-field work; raw ledger lines have their own field boundary |
| A-L17 | `GET accounts/?with_balance=true` restates the trial balance per account (the bank-ledger part is B-L11). | fixed for trial balance figures: account balances require `finance.report.trial_balance`; B-L11 remains pending |
| A-L18 | Procurement requisition budget availability returns a plan amount and budget name under `procurement.requisition.view`, and picks the latest approved plan of any branch, not the requester's. | fixed: branch reach and the budget name, amount, actual and variance switches govern the procurement payload |
| A-L19 | The customer statement `aging` and balances, the dunning summary buckets and the customer list `balance` restate customer balance and aging with no switch check. | fixed: `vs_finance.tests_receivables_restating_paths.CustomerStatementAccessTests`, `DunningSummaryAccessTests`, `CustomerCreditAccessTests.test_the_customer_list_balance_follows_the_balance_switch` |
| A-L20 | Budget `filing.branches` offered while the caller may not set `branch`. | exempt: branch is an identifier and its write scope follows branch reach |

A-L20 is voided by the branch rule in section 2.

### 9.2 Area B: receivables, ledger and banking

| ID | Leak | Status |
| --- | --- | --- |
| B-L1 | `CUSTOMER_UPDATED` audit rows carry contact, source and receivable-account values; the central trail has no snapshot rule for the registered customer fields. | fixed: the central copy of a finance entry carries no value, `vs_finance.tests_audit_figures.CentralMirrorTests`; the finance trail filters customer snapshots through `finance.customer` |
| B-L2 | `income_given_back[]` on credit notes and concessions re-serves registered `finance.interbranch` fields (amount, status) with no check. | fixed: `vs_finance.tests_receivables_restating_paths.IncomeGivenBackAccessTests` |
| B-L3 | The chart's `bank_account_id` restates `finance.bankaccount.ledger_account`. | fixed: `vs_finance.tests_bank_restating_paths.BankLedgerAccountAccessTests.test_a_hidden_ledger_account_removes_the_link_from_the_chart` |
| B-L4 | Bank account detail `statements[]` restates the registered `statement_balance`. | fixed: `vs_finance.tests_bank_restating_paths.BankStatementAccessTests` |
| B-L5 | Bank account detail `reconciliations[]` and the `reconcile/complete/` response restate `book_balance`, `statement_balance` and `unreconciled_diff`. | fixed: `vs_finance.tests_bank_restating_paths.BankReconciliationAccessTests` |
| B-L6 | Statement detail GET and PATCH and the statement-lines import and list serve the same statement balances and line amounts outside the account detail. | fixed: `vs_finance.tests_bank_restating_paths.BankStatementAccessTests` (detail GET and PATCH); statement lines carry no balance, and their amounts are the recorded `statement_lines` switch. The correction (PATCH) takes `opening_balance` as optional, keeps the figure on file when it is left out, and refuses a submitted one with 403 `field_write_denied` for a caller who cannot read `statement_balance`, whatever the value (accepting an unchanged value would let the caller find the figure by trying values): `test_a_correction_without_an_opening_balance_keeps_the_one_on_file`, `test_a_caller_who_cannot_read_the_balance_may_not_write_it_whatever_the_value`. The `BANK_STATEMENT_CORRECTED` entry's `before`/`after` drop `opening_balance` and `closing_balance` for the same caller (`AUDIT_SNAPSHOT_GUARDS`): `test_the_correction_entry_hides_the_balances_from_a_caller_who_cannot_read_them` |
| B-L7 | Import batch `domain_context` repeats a bank statement's opening and closing balance under `import.batches`. | fixed: `vs_finance.tests_import_batch_restating.ImportBatchDomainContextAccessTests` |
| B-L8 | Customer balance is restated by the customer statement JSON and export, refund availability, the AR-adjustments KPI and credit note `credit_remaining`; AR aging and AR reconciliation are the report change. | fixed: `vs_finance.tests_receivables_restating_paths.CustomerStatementAccessTests`, `CustomerCreditAccessTests` (refund availability, credit note `credit_remaining`); AR aging and AR reconciliation stay with the report change |
| B-L9 | Typed reasons and narrations can reach the audit viewer in snapshots or as a separate `reason` value. | recorded: registered journal, credit-note and refund snapshot fields follow their Read switches, but metadata reasons without a registered field, including a statement-line ignore reason, remain open by rule. `vs_finance.audit.entry_reason` serves that value beside the figure-free sentence; see `vs_finance.tests_audit_figures.OlderEntryAndReasonTests` and `vs_procurement.tests_audit_figures`. |
| B-L10 | Fiscal-year close and re-open responses embed closing journals whose P&L lines restate the hidden `finance.fiscalyear.net_income`. | fixed: `vs_finance.tests_year_close_restating.YearCloseJournalAccessTests` |
| B-L11 | Chart `balance`, account detail `summary` and activity `running_balance` for a bank ledger account restate the registered `finance.bankaccount.book_balance`. | fixed: `vs_finance.tests_bank_restating_paths.BankLedgerAccountAccessTests`. A group account adds up its descendants, so the stricter rule applies: when any account a figure sums backs a bank account and the caller cannot read the book balance, the group's `summary.current_balance`, `summary.opening_balance` and activity `totals` are omitted (`vs_finance.accounts.book_balance_hidden`; also when `?account=` picks the bank out of a group): `test_a_group_over_a_bank_ledger_account_follows_the_book_balance_switch`, `test_one_bank_ledger_account_among_ordinary_ones_is_enough_to_withhold_the_group`, `test_the_activity_totals_of_a_group_over_a_bank_follow_the_switch`, `test_picking_the_bank_out_of_a_group_does_not_bring_the_totals_back`, `test_a_group_in_a_single_branch_school_follows_the_same_switch` |
| B-L12 | Invoice detail `payments[]`, `settlements[]`, `gl_journals[]` and `activity[]` re-serve receipt data. | fixed: `vs_finance.tests_invoice_field_access.InvoiceFieldAccessTests`; settlement rows and their journals follow the source amount switches, and journal lines follow `finance.journal.lines` |
| B-L13 | Payment plan progress restates the invoice's paid and balance figures. | fixed: `vs_finance.tests_field_access_deep_payload.PaymentPlanDeepPayloadTests`; invoice-linked progress follows the invoice settlement switch |
| B-L14 | Invoice summary KPIs and monthly series restate receipts. | fixed: `vs_finance.tests_invoice_field_access.InvoiceFieldAccessTests`; receipt figures follow `finance.payment.amount`, and invoice totals and derived rates follow the invoice switches |
| B-L15 | Export datasets for invoices (money columns) and GL postings carry no `access=` keys. | fixed: `vs_finance.tests_invoice_field_access.FinanceExportFieldAccessTests` and `vs_exports.tests_filter_field_access`; columns, filters, sorts and searches follow their keys, including both switches of a derived amount |
| B-L16 | Refund `bank_account_id` and the availability branch rows name a bank account and a branch; both are identifiers. | exempt: identifiers under section 2 |
| B-L17 | Success messages name documents ("Invoice INV-0042 posted"); document numbers are identifiers. | exempt: identifiers under section 2 |

B-L16 and B-L17 follow the identifier rule in section 2.

### 9.3 Area C: procurement

| ID | Leak | Status |
| --- | --- | --- |
| C-L1 | The stock transfer response returns the receiving store's movement, with that store's balance quantity and value, to a sender who cannot read the other branch's stock. | fixed: `vs_procurement.tests_stock_transfer_restated.StockTransferRestatedTests.test_a_sender_outside_the_receiving_branch_is_not_told_its_balance` |
| C-L2 | `VendorInvoiceReferenceCheckView` returns other branches' bills (number, total, date, status, vendor), scoped by entity only. | fixed: `vs_procurement.tests_restated_reach.VendorInvoiceReferenceCheckReachTests` |
| C-L3 | The stock item `activity` feed is entity-wide, not narrowed to the caller's branches, and carries store codes and values. | fixed: `vs_procurement.tests_stock_transfer_restated.StockTransferRestatedTests.test_the_activity_feed_holds_only_the_callers_branches_entries` |
| C-L4 | Stock transfer value, narration, reference, date, journal link and `inter_branch_transfer_id` are served by procurement regardless of the registered `finance.interbranch` amount, purpose, reference, transaction date, journals and related-documents switches. | fixed: `vs_procurement.tests_stock_transfer_restated.StockTransferRestatedTests.test_each_switch_takes_out_only_the_values_it_governs` |
| C-L5 | A vendor payment's `posting_lines`, `activity`, audit sentence and eligible-bills WHT base restate gross, net and WHT outside any amount switch. | recorded |
| C-L6 | A gateway payout line copies the vendor's bank account name, number and bank code into `payments.payout` beneficiary fields governed by a different switch than `procurement.vendor.bank_*`. | fixed: `vs_payments.tests_payout_vendor_banking` (`PayoutVendorBankingTests`, `MovementsFeedTests`, `ApprovalDocumentTests`, `ExportTests`) |
| C-L7 | `QuotationDetailSerializer.submissions[].submitted_by_email` serves a vendor contact's email outside `procurement.vendor_contact.email`. | fixed: `vs_procurement.tests_restated_vendor_values.SubmittedByEmailTests` |
| C-L8 | Registered vendor values still leak: (a) `VendorCategorySerializer.vendor_count`, (b) `VendorCategoryInsightsView` spend per category, (c) refusal sentences and the `?purchase_eligible=true` filter state hold and KYC, (d) the PO delivery history `recipient_count`, `bcc_count`, `failure_reason`, (e) a new order echoes the vendor's `payment_terms`. | fixed in parts: (a) `vs_procurement.tests_restated_vendor_values.VendorCategoryCountTests`; (b) `CategorySpendFieldAccessTests` (the insights answer 403 when the category is hidden); (c) exempt by owner decision: business-rule refusal sentences and the `?purchase_eligible=true` verdict stay; (d) `DeliveryHistoryFieldAccessTests`; (e) no change: payment terms copied onto an order are the order's own field |
| C-L9 | Double-sourcing refusals name a shared RFQ number the buyer cannot open; document numbers are identifiers. | recorded |
| C-L10 | A plan amount is readable under a procurement key only: budget availability and the suppliers dashboard `spend.plan`. | fixed: both paths require `finance.budget.amount` |
| C-L11 | Requisition budget availability `committed` sums every branch's PO lines at a cost centre, not the caller's branches. | fixed: `vs_procurement.tests_restated_reach.RequisitionBudgetReachTests` |
| C-L12 | `CatalogItemInsightsView` (vendor pricing, purchase history) and `ContractLinkedPurchaseOrdersView` (order totals) are not branch narrowed. | fixed: `vs_procurement.tests_restated_reach.CatalogItemInsightsReachTests`, `ContractLinkedOrdersReachTests` |
| C-L13 | Assessment scores and grade are restated in vendor performance and the suppliers dashboard scorecard. | recorded |
| C-L14 | Personal data in unregistered payloads: `updated_by` email in procurement settings, the uploader's email fallback in attachment rows, requisition `?search=` matching the requester's email. | recorded |
| C-L15 | Procurement approval summaries, details and queue rows restate amounts, unit prices, WHT, payment terms and bill totals (no registered source yet). | recorded |
| C-L16 | Procurement dashboards restate unregistered values block by block (bill and order amounts, RFQ `budget_estimate`, stock value, issued value). | recorded |
| C-L17 | Export datasets for requisitions (`estimated_total`) and orders (`payment_terms`) have no `access=` keys. | recorded |
| C-L18 | Finance audit rows written by procurement carry values and addresses: stock transfer value, PO email recipients and bcc, payment net and WHT, credit note totals, return value. Only the recipient addresses concern a registered field; the amounts are recorded. | fixed: the sentences carry no figure or address and the central copy none either, `vs_procurement.tests_audit_figures.PurchaseOrderEmailAuditTests`, `vs_procurement.tests_audit_figures.ProcurementSentenceTests` |
| C-L19 | The inter-branch `purpose` of a goods transfer carries the item code, quantity and both store codes, readable with `finance.interbranch.purpose` while `procurement.stock` is closed. | recorded |

### 9.4 Area D: settings

| ID | Leak | Status |
| --- | --- | --- |
| D-L1 | `/v1/config/values/`, `effective-values/` and `export/` return every setting, including registered `finance.settings` receivables values, and `POST /v1/config/values/` writes any key, with no Field Access (platform scope: Q-D6, answered in 8.1). | exempt: CodeX staff follow platform keys, not tenant switches |
| D-L2 | The configuration audit viewer, detail, saved views and export jobs carry `before_data` and `after_data` unfiltered. | fixed: the configuration audit list, detail and both CSV downloads filter snapshots per reader, `vs_config.tests_audit_access.SnapshotAccessTests`, `vs_config.tests_audit_access.ExportAccessTests`; a definition is bound to a field with `vs_config.audit_access.register_setting_field` (none is bound yet: no registered setting is stored as a configuration value) |
| D-L3 | Enrolment `optional_fields` lists student field labels regardless of the caller's student switches (labels only). | fixed: `schools.vs_students.tests.test_field_access_settings_leaks.OptionalFieldsTests` |
| D-L4 | `required_fields` and guardian `email_required` can make a field mandatory that the caller cannot write or read (Q-D7, answered in 8.1). | fixed: the rule yields to a caller who cannot supply the field (`binding_required_fields`, `binding_email_required`); `schools.vs_students.tests.test_field_access_settings_leaks.RequirementsYieldTests`. The same yield on the spreadsheet imports is D-L11. |
| D-L6 | The staff number rule `suggestion` restates the registered `school.teachers.staff_number`. | fixed: `schools.vs_staff.tests.test_field_access_settings_leaks.StaffNumberSuggestionTests` |
| D-L8 | `school.profile` registers only address and registration number; `currency`, `term_structure`, `website`, `motto` and `ownership_type` on `SchoolProfileSerializer` are unregistered. | recorded |
| D-L9 | The history rows of the other finance settings sections will carry old and new values unfiltered once those settings are registered. | recorded |
| D-L11 | Found while fixing D-L4: the student roll and household list spreadsheet imports applied no Field Access, so a caller without Write on a registered `school.students` or `school.guardians` field could load values into it, and the school's required fields and guardian email rule bound callers who cannot supply them. Both imports now refuse a row (the roll) or the whole file (the household list) naming the fields, in the standard `FieldWriteDenied` wording, honouring `open_on_create` and dropping blank cells; the preview reports the same refusals per row and column; the requirements yield (`binding_required_fields`, `GuardianRules.binding`). The roll's one `address` column is judged under both `school.students.address` and `school.guardians.address`, because it also becomes a new guardian's address. | fixed: `schools.vs_students.tests.test_import_field_access` (`RollWriteSwitchTests`, `RollRuleYieldTests`, `HouseholdWriteSwitchTests`, `HouseholdRuleYieldTests`) |
| D-L12 | Found while fixing D-L3: `optional_fields` hides student fields the caller cannot read, so the enrolment rules PUT from a settings admin who cannot read blood group dropped a school's requirement for it (the screen never offered it back), and the guardian rules PUT let an admin who cannot read guardians' email change `email_required`. A save now keeps every stored requirement for a field the caller cannot read and ignores one they add (`EnrolmentRules.required_fields_after`, `GuardianRules.email_required_after`). The generic `/v1/config/values/` write of the same keys is D-L1 (platform scope). | fixed: `schools.vs_students.tests.test_import_field_access.EnrolmentRulesSaveTests`, `GuardianRulesSaveTests` |

Reviewed and clear, not counted: the finance document settings carry bank name
and currency but no account number (D-L5); no settings payload carries branch
address or email (D-L7); settings search returns labels and anchors, never values
for these screens (D-L10).

### 9.5 Area E: receipts, petty cash, issuer, audit, approvals and exports

| ID | Leak | Status |
| --- | --- | --- |
| E-L1 | `finance.payment` is registered nowhere, though the `finance.payer_payment` docstring says an ordinary receipt keeps its switches; the same receipt is switched through a payer payment and open on `GET /finance/payments/`. The docstring is corrected with the registration. | recorded |
| E-L2 | `PaymentDetailView` serves `open_invoices` and `open_debit_notes` under `finance.payment.view`; they are the registered `finance.customer.open_invoices` and `open_debit_notes`. | fixed: `vs_finance.tests_receivables_restating_paths.ReceiptOpenItemsAccessTests` |
| E-L3 | `PaymentSummaryView` totals (`today`, `week`, `unallocated`, `refunded`, `status_counts`) are sums of unregistered receipt fields. | recorded |
| E-L4 | The printed receipt prints `customer_balance_after` and `invoice_balance_after`; these are system surfaces by decision and are added to `SYSTEM_SURFACES`. | recorded |
| E-L5 | `RefundAvailabilityView` and the `ARAdjustmentListView` rows restate customer credit (the registered `finance.customer.balance`) and refund amounts. | fixed: `vs_finance.tests_receivables_restating_paths.CustomerCreditAccessTests` (availability and the adjustments credit total); the rows carry the refund and write-off amounts, which are the recorded `finance.refund` and `finance.writeoff` fields, not a customer balance |
| E-L6 | The petty cash fund has no switches, and fund create, fund PATCH, establish, replenish, reopen and voucher create have no `assert_writable`. | recorded |
| E-L7 | `PettyCashStatusView` and `fund_status` are not narrowed by branch: a branch-bound bursar reads other branches' float and cash on hand. | fixed: `vs_finance.tests_petty_cash_status_reach.PettyCashStatusReachTests` |
| E-L8 | Fund detail `register[]` and `spent_this_week` restate cash on hand with no switch. | recorded |
| E-L9 | The dashboard petty cash card and its "floats running low" attention item restate fund float and balance and hide only on `finance.dashboard.petty_cash`. | recorded |
| E-L10 | The CodeX issuer profile has no registration; `POST /config/values/`, `DELETE /config/values/<key>/` and `effective-values/` write and read the same keys. | recorded |
| E-L11 | Issuer identity has two sources (printed documents use `platform.profile.name`; email subjects, payslips and credit-note emails use the ledger entity name). | recorded |
| E-L12 | Entitlement `starts_at` and `ends_at` (list, calendar, bulk schedule, uplift) restate the subscription expiry guarded by `platform.schools.subscription_expires_at`. | fixed for the entitlement list, the grant and schedule answers and the renewal calendar: `vs_config.tests_entitlement_access`; the uplift window on the school plan screen: `schools.vs_schools.tests_plan_uplift_window_access` (a plan-written uplift, `source` `PACKAGE`, follows the subscription start and expiry switches; a hand-written one is open) |
| E-L13 | CodeX's customer record copies a school's email, phone and address on every school save: two switch families for one value (stricter wins). | exempt: the CodeX customer record is read through the CodeX books' own `finance.customer` switches, and the copy from the school is a system write |
| E-L14 | The central audit mirror sends the finance `message` as `summary` and every metadata argument as `metadata`, with no Field Access (Q-E10 and Q-E11, answered in 8.1). | fixed: the mirror writes who, what, which document, branch and time and nothing else; rows already written are rebuilt on read (the finance trail's own old rows too: see E-L26), `vs_finance.tests_audit_figures.CentralMirrorTests`, `vs_audit.tests_protected_words_values.RecordOnlyTests` |
| E-L15 | Payroll run audit rows carry gross, PAYE, pension, other deductions and net totals in message and metadata. | fixed: `vs_finance.tests_audit_figures.PayrollAuditTests` |
| E-L16 | Salary-change audit messages name the employee and the change reason travels in central metadata, while the employee name is a sensitive registered field. | fixed: the reason no longer reaches the central trail (`vs_finance.tests_audit_figures.CentralMirrorTests`); the employee's name in the finance sentence is a label |
| E-L17 | Bank transaction and transfer corrections store before and after of account, amount, narration and dates; `BankTransaction` and `BankTransfer` are missing from `AUDIT_SNAPSHOT_FIELDS`. | fixed: `BankTransaction` and `BankTransfer` are in `AUDIT_SNAPSHOT_FIELDS`, `vs_finance.tests_audit_figures.BankAuditFigureTests` |
| E-L18 | Petty cash return audit rows carry counted cash, books balance, shortage and new float. | fixed: `vs_finance.tests_audit_figures.PettyCashReturnAuditTests` |
| E-L19 | Inter-branch, held-receipt and recharge audit rows name amounts, customer and narration in message and metadata. | fixed: `vs_finance.tests_audit_figures.InterBranchAuditTests` |
| E-L20 | Deposit release and forfeit audit rows name customer codes and per-customer amounts. | fixed: `vs_finance.tests_audit_figures.AccrualAuditFigureTests` |
| E-L21 | Deferred income release and reversal audit rows carry the released amount and invoice ids. | fixed: `vs_finance.tests_audit_figures.AccrualAuditFigureTests` |
| E-L22 | Provision audit rows carry the required total, per-branch lines and the `bands` policy snapshot (`finance.settings.provision_bands`). | fixed: `vs_finance.tests_audit_figures.AccrualAuditFigureTests` |
| E-L23 | Payer link and payer payment audit rows carry payer and customer names, amounts, receiving bank and per-customer shares. | fixed: `vs_finance.tests_audit_figures.PayerPaymentAuditTests` |
| E-L24 | Purchase order email audit rows carry vendor and contact email addresses (`recipients`, `bcc`) into central metadata (same as C-L18). | fixed: `vs_procurement.tests_audit_figures.PurchaseOrderEmailAuditTests` |
| E-L25 | The fiscal-year close audit message carries the net result; forced closes store a free-text `reason` and the names of the failed checks. | fixed: `vs_finance.tests_audit_figures.YearCloseAuditTests`; the free-text reason stays in metadata |
| E-L26 | Procurement activity feeds (contract, sourcing documents, vendor bill, vendor payment, approval queue) return the raw finance audit `message` with no request in context. | fixed: every finance trail read (list, detail, settings history) and every procurement document activity feed serves `vs_finance.audit.entry_sentence`. An entry written now carries `sentence_without_values` in its metadata (set in `record()`, the only writer) and its stored sentence is figure-free; an entry without the marker predates the rule and shows the action label and document number ("Invoice posted: INV-0042", as the central mirror does) whatever its stored text says, so the stored row is never rewritten and no screen shows its text. The presence of the marker, not the shape of `before`/`after`, is the cut-off. The free-text reason behind an act is served beside the sentence as `reason` (`entry_reason`, `activity_entry`): open when unregistered, from the reader's filtered `after` for an inter-branch decline (`finance.interbranch.decline_reason`), and only to a reader of a pay switch for a salary change. `vs_finance.tests_audit_figures.EntrySentenceTests`, `OlderEntryAndReasonTests`, `vs_procurement.tests_audit_figures.ProcurementSentenceTests` |
| E-L27 | A student update stores the whole detail payload as `before_data` and old and new values in `diff_data`; no Student snapshot rule exists, and `entity_label` and summary carry the full name. | fixed: snapshot rules per registered field (`STUDENT_SNAPSHOT_KEYS`); `schools.vs_students.tests.test_field_access_settings_leaks.StudentUpdateAuditTests`, `vs_audit.tests_protected_words_values.SnapshotPathTests`. The pupil's name stays as the row's label. |
| E-L28 | Staff employment status moves store `reason`, `effective_date` and `last_working_day` in central metadata with no rule for `school.teachers.employment_status` and `exit_date`. | fixed: `schools.vs_staff.tests.test_field_access_settings_leaks.EmploymentMoveAuditTests`, `vs_audit.tests_protected_words_values.MetadataValueTests` |
| E-L29 | The plan agreement fields of `platform.schools` (agreed price, minimum billable students, package plan, subscription dates, notes, uplift) have no protected-word rule. | fixed: `schools.vs_schools.tests_agreement_audit_words` (entitlement, uplift, withdrawn-grant and not-agreement cases) |
| E-L30 | `/config/audit-events/` serves raw before and after of every configuration value (same as D-L2). | fixed: see D-L2, `vs_config.tests_audit_access.SnapshotAccessTests` |
| E-L31 | Audit gap: creating or editing a bank account (account number included) and a vendor (bank details, tax id, email) writes no audit row, so there is nothing to protect. | recorded |
| E-L32 | Finance approval summaries (bank transaction, transfer, petty cash return, inter-branch, provision) copy amounts, accounts, counted cash and branch splits into the unfilterable summary and unmarked details. | fixed: `vs_finance.tests_approval_field_access`, `vs_workflow.tests.test_summary_access` |
| E-L33 | `school.hire` and `school.leave` approval cards copy name, job title, email, staff id, hire date and leave kind into title and summary, ignoring the `school.teachers` switches (the name and job title in the title are labels and always show, Q-E9, answered in 8.1; the other values follow the switches). | fixed: `schools.vs_staff.tests.test_approval_field_access` (the registered `school.teachers` fields other than name and job title are gated; a leave card now restates `school.leave` fields, so its type, dates, days, reason and over-allowance figure each follow their switch, and its subtitle names no type). Who filed a request, when, and who last changed it carry no `school.leave` switch and are recorded as an open product question |
| E-L34 | Workflow notification subjects and in-app text are built from the stored title and subtitle: staff name and job title (hire), name and "Leave request" (leave; the type of leave is a marked field and not part of the subtitle), platform user name or email (labels, Q-E9, answered in 8.1). | no change: names are labels |
| E-L35 | Export datasets without `access=`: `platform.schools` exports `address` and `registration_id` (registered under `school.profile`; the export follows platform keys, Q-E13, answered in 8.1); `finance.customer_receipts`, `finance.gl_postings` and `procurement.purchase_orders.payment_terms` have no registered source yet and are recorded. | exempt: CodeX staff follow platform keys, not tenant switches |
| E-L36 | Settings search offers hidden `finance.settings.*` fields as hits (`Place` has no Field Access key). | belongs to the search feature another session is building |
| E-L37 | Console overview signals `unallocated_credit`, `overdue_invoices` and `fiscal_runway` count across the whole tenant with no branch narrowing. | fixed: `vs_admin_console.tests_overview_access.OverviewFinanceSignalAccessTests` |
| E-L38 | Service-level refusals raised without a request name amounts and balances ("holds N, more than a float of N"; "Refund of N exceeds C001's credit"). | recorded |
| E-L39 | The gateway log (`PaymentEvent`, `payments/transactions/`) wrote a virtual account's number into its sentence; amounts appear in collection, payout and settlement sentences. | fixed: a sentence names no value of `payments.virtual_account` or `payments.payout` (the beneficiary's name, account number and bank code, and the account's number and name); events written now carry `sentence_without_values`, and an event without it still has the number cut from its sentence for a reader who cannot read it. Amounts are unregistered in payments and stay in the sentences, as recorded. A refusal's text comes from the provider or a service message and no code path interpolates a beneficiary value; a provider's own failure text (`PayoutInstruction.failure_reason`, a rejection message) is the provider's words and is recorded, not filtered. `vs_payments.tests.PaymentEventTests.test_no_sentence_or_metadata_carries_a_switched_value`, `vs_payments.tests_transactions_log.TransactionsLogTests` |
| E-L40 | The procurement approval queue detail serves `WorkflowAuditLog.message` as written. | exempt: the workflow engine writes only fixed sentences, and the one value it interpolates is the label of the person who acted (`vs_workflow/services/release.py`, `parking.py`); no registered field's value is in them. Reasons and comments travel in the row's `context`, which the queue does not serve. `vs_workflow.tests.test_audit_sentences` reads every writer's `message=` from source and refuses any other interpolation |

### 9.6 Found outside the five areas

| ID | Leak | Status |
| --- | --- | --- |
| X-L1 | `policy_snapshot` on a provision carries the provision bands while the registered `finance.settings.provision_bands` is hidden. | fixed: `vs_finance.tests_policy_restating_paths.ProvisionPolicyBandsAccessTests` (the snapshot and each line's age bands) |
| X-L2 | The month-close checklist names the deferred income count and total regardless of the registered `finance.deferredincome` switches. | fixed: `vs_finance.tests_dashboard_field_access.ChecklistFieldAccessTests.test_a_deferred_income_check_needs_the_deferred_income_figure` |
| X-L3 | `payer_payments.split_choices` returns the default split to payment recorders even when `finance.settings.payer_payment_split` is hidden. | fixed: `vs_finance.tests_policy_restating_paths.PayerPaymentSplitChoicesAccessTests` |
| X-L4 | The budgets tab's fiscal-year picker sends `?status=OPEN`, which answers 403 when `finance.fiscalyear.close_state` is hidden; the picker drops the filter or uses the posting window. | fixed: FinPro no longer sends `?status=` when the `finance.fiscalyear` close state is hidden, `FinPro/src/pages/finance/budgets/budgets-tab.test.tsx` |
| X-L5 | The hidden vendor category alias `category` is not listed in a record's `_read_only_fields` (only in `/me`). | fixed: `vs_procurement.tests_restated_vendor_values.VendorCategoryWriteAliasTests` |
| X-L6 | `reverse_blocked_reason` answers a generic "The month is not open" when the period is hidden. | recorded |
| X-L7 | A non-admin release date picker answers "No fiscal period is open" (a missing period permission is possible; unconfirmed). | recorded |
| X-L8 | A refused contact write names `email` and `phone`, the same keys as a vendor's own; the refusal detail could prefix them with `contacts.`. | recorded |
| X-L9 | An RFQ with no recipients is ambiguous: no contacts, or contact flags the caller cannot read. | recorded |
| X-L10 | A CodeX operator acts on a school by asserting it as the request tenant, and `_access_for`, `field_access_payload` and the export engine judged their switches in that school, where a platform role does not exist: every `PLATFORM` scope field (`platform.schools.*`, `platform.staff_profile.*`, `platform.team.*`) read closed for the operator. `vs_config.entitlement_access` carried a private workaround. | fixed at the shared door: `vs_rbac.field_evaluator.get_acting_field_access` judges a `PLATFORM` field in the operator's own tenant and every other field in the school; `vs_rbac.tests.test_acting_field_access`, `vs_config.tests_entitlement_access`; the workaround is removed |
| X-L11 | The staff spreadsheet import applied no Field Access: a caller without Write on a registered `school.teachers` field (phone, job title, staff ID, employment type, hire date, gender, middle name) could load values into it, although the Add form refuses the same body. Each non-blank cell is now judged against the runner's switches with `open_on_create` honoured (the name and the sign-in address), blank cells dropped; the preview reports a refusal per row and column with `metadata.field_write_denied`; execution fails the row with the standard `FieldWriteDenied` wording and `error_details.field_write_denied`. The shared judge is `vs_import_data.field_writes`. | fixed: `schools.vs_staff.tests.test_import_field_access` (`StaffWriteSwitchTests`, `StaffPreviewTests`, `StaffEngineTests`) |
| X-L12 | Adding a member of CodeX staff set `platform.staff_profile` details (name, employee ID, job title, employment type, date joined, date of birth, marital status, nationality, state of origin) with no Write check, on the Add form, the draft save and the CX users spreadsheet alike: only editing an existing profile was judged. The account serializer now judges them for any account created on the platform tenant (`vs_user.serializers.refuse_unwritable_staff_profile_fields`); the name is declared open on create because nobody can be added without one. The CX users preview reports the refusals per row and column. | fixed: `vs_import_data.tests_cx_users_field_access` (`CxImportWriteSwitchTests`, `CxImportPreviewTests`, `CxImportEngineTests`, `CxAddFormTests`) |
| X-L13 | Reviewed, no leak: the `schools` import is judged by `SchoolCreateSerializer` (`platform.schools`, covered by `schools.vs_schools.tests_platform_schools_field_access`); the `branches` import writes the branch address and email that `school.branches` declares read-only because CodeX sets them when provisioning, and the dataset is platform-only; the `bank_statements` import writes `BankStatementLine` rows, which no registered resource covers (`finance.banktransaction` is the posted bank transaction document); `org_units`, `positions`, `matrix_reports`, `calendar_events`, `academic_structure` and `subjects` write no registered field. | reviewed, no change |
| X-L14 | The student roll and household-list imports carried their own copy of the import write judge (`import_request`, `write_refusal`, `write_refusal_issue`). | fixed: both read the shared `vs_import_data.field_writes`, which takes an optional `names_set` for a column that fills several fields only in part (a guardian's one-line name); behaviour and messages are unchanged. `vs_import_data.tests_field_writes`, `schools.vs_students.tests.test_import_field_access` |
| X-L15 | A platform account's phone (`User.phone`) has no switch. It shows on the user list and on the CodeX user-creation approval card (`PLATFORM_USER_CREATION`). Candidate: `platform.team.phone`, Read and Write, in the next wave. | recorded |
| X-L16 | The `/me` field map listed create-open write aliases only under `read_only`. `opening_date` and `opening_branch` now follow `finance.customer.opening_balance` into `open_on_create`; `receivable_account` remains read-only because its governing field is not open on create. | fixed: `vs_rbac.tests.test_field_enforcement.TheMapAUserIsHandedTests`, `vs_finance.tests_customer_field_access.CustomerWriteTests` |
| X-L17 | A cancelled purchase order's reason also appears in the delivery's `failure_reason` ("Purchase order cancelled: <reason>"). The reason is unregistered, so the text is open. | recorded: open, the reason is unregistered |

### 9.7 Deployment checks that belong with the registrations

* `seed_finance_permissions` must mint the `finance.dashboard` permission-resource
  row (`FIELD_ONLY_RESOURCES`) so a deploy does not depend on a hand-made row,
  and it must run before `sync_field_registry` (`seed_all_permissions` already
  orders them).
* Approval requests sent before the per-field markers existed carry none and show
  every value to every approver. After deploying, run
  `manage.py rebuild_approval_snapshots --dry-run`, then
  `manage.py rebuild_approval_snapshots`: it retakes the summary and details of
  every open (not yet approved, rejected, withdrawn or cancelled) request through
  its handler, writes only what changed, and is safe to run again.
* Document numbers and branch labels that remain in wording are not leaks.
* The generic per-field 403 wording and tenant hard-coding in the design
  preflight script are tooling and framework items outside Field Access.

---

## 10. Counts

Counted from the rows of this ledger. They differ from the audit's own totals
because the rules in section 2 removed the branch and document-number switches
and merged duplicate switches. Keep them current when a row changes status.

### 10.1 Fields

A switch is counted once, wherever it is served. Two switches
(`finance.report.aging_customers`, `finance.budget.amount`) appear in more than
one area, so the total is two lower than the sum of the areas.

| Area | New switches recorded | Rows already registered | Rows with no switch: identifier | Rows with no switch: transport | Rows with no switch: owner decision |
| --- | --- | --- | --- | --- | --- |
| A. Budgets and financial statements | 0 | 25 | 3 | 3 | 3 |
| B. Receivables, ledger and banking documents | 0 | 119 | 3 | 17 | 12 |
| C. Procurement | 193 | 4 | 4 | 13 | 10 |
| D. School settings and finance settings | 107 | 0 | 0 | 8 | 2 |
| E. Receipts, petty cash, issuer, entitlements | 45 | 3 | 3 | 8 | 3 |
| F. Messages (registered with the feature) | 0 | 8 | 1 | 1 | 2 |
| Total | 343 | 159 | 14 | 50 | 32 |

Where the new switches sit:

* Area A: `finance.budget` 8, `finance.report` 17.
* Area B: 103 switches registered in this change. The original plan was `finance.invoice` 17, `finance.creditnote` 11, `finance.refund` 8,
  `finance.concession` 9, `finance.writeoff` 9, `finance.credittransfer` 6,
  `finance.paymentplan` 10, `finance.account` 9, `finance.costcenter` 2,
  `finance.journal` 12, `finance.bankaccount` 10 (statements and reconciliations),
  plus `finance.report.aging_customers` counted in area A.
* Area C: `procurement.stock` 31, `procurement.vendor_credit_note` 10,
  `procurement.goods_receipt` 18, `procurement.vendor_payment` 18,
  `procurement.requisition` 14, `procurement.rfq` 13, `procurement.quotation` 13,
  `procurement.purchase_order` 12, `procurement.vendor_invoice` 17,
  `procurement.vendor` 5, `procurement.category` 2, `procurement.catalog_item` 10,
  `procurement.contract` 8, `procurement.vendor_assessment` 5,
  `procurement.settings` 16.
* Area D: `school.settings` 70, `school.teachers.leave_group` 1,
  `finance.settings` 36.
* Area E: `finance.payment` 12, `finance.pettycash` 11,
  `finance.pettycashvoucher` 10, `platform.profile` 10, `platform.entitlements` 2.
* Area F: none recorded. `school.messages` registers 9 switches with the feature
  (`body`, `title`, `participants`, `recipients`, `child`, `reply_mode`, `sent_at`,
  `read_status`, and `read_receipts` from 22 October 2026), counted under the
  registered column.

Sensitive by owner decision: `finance.budget.amount` (Q-A4),
`finance.customer.source_id` and `finance.bankaccount.statement_balance`
(Q-B4). Sensitive by
recommendation: `procurement.rfq.budget_estimate`,
`procurement.vendor_assessment.notes`, the six security settings and
`school.settings.starting_role`. Everything else ships open, several pending the
questions in section 8.

### 10.2 Leaks

| Area | Leaks | Fixed | Still to be fixed | Recorded | Exempt | Reviewed or elsewhere |
| --- | --- | --- | --- | --- | --- | --- |
| A. Budgets and financial statements | 20 | 8 | 0 | 1 | 11 | 0 |
| B. Receivables, ledger and banking documents | 17 | 14 | 0 | 1 | 2 | 0 |
| C. Procurement | 19 | 11 | 0 | 8 | 0 | 0 |
| D. School settings | 9 | 6 | 0 | 2 | 1 | 0 |
| E. Receipts, petty cash, issuer, audit, approvals, exports | 40 | 25 | 0 | 10 | 3 | 2 |
| X. Found outside the five areas | 17 | 10 | 0 | 6 | 0 | 1 |
| Total | 122 | 74 | 0 | 28 | 17 | 3 |

Area A's eleven exemptions follow
the owner's aggregate and branch decisions. "Reviewed or elsewhere" is E-L34
(names are labels, no change), E-L36 (belongs to the search feature) and X-L13
(reviewed, no leak). C-L8 is counted as fixed although its part (c) is exempt by
owner decision. Three reviewed paths in area D were clear and are not rows
(D-L5, D-L7, D-L10).

### 10.3 Open product questions

12 questions: area A 0, area B 0,
area C 4 (Q-C2, Q-C3, Q-C6, Q-C7), area D 5 (Q-D1 to Q-D5),
area E 3 (Q-E4, Q-E5, Q-E12). Fourteen more (Q-A1 to Q-A7, Q-D6,
Q-D7, Q-E9, Q-E10, Q-E11, Q-E13, Q-C4) are answered in 8.1; Q-B4, Q-B5, Q-B8 and Q-B10 are answered in 8.3.
