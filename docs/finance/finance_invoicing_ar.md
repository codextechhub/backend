# finance_invoicing_ar

Accounts-Receivable core: **customers** (the billable parties), **invoices** raised
against them, **receipts/payments** that settle those invoices, and **fee
structures** that mass-generate invoices. This is the sales/billing side of the
ledger - money owed *to* the entity and the cash that clears it.

Routes covered (mounted at `/v1/finance/`): `customers/`, `customers/<pk>/`,
`customers/<pk>/receipt/`, `customers/opening/`, `invoices/`, `invoices/summary/`,
`invoices/<pk>/`, `invoices/<pk>/pay/`, `invoices/<pk>/remind/`, `payments/`,
`payments/<pk>/`, `payments/<pk>/allocate/`, `fee-structures/…`,
`fee-structures/<pk>/items/<item>/assignments/`, `deferred-income/`,
`deferred-income/release/`, `deferred-income/reverse/`, `deposits/`,
`deposits/release/`, `deposits/forfeit/`, `settings/receivables/`.

> **Adjacent slices** (not here): credit notes, refunds, write-offs, concessions →
> `finance_ar_adjustments`; installment plans → `finance_payment_plans`; reminders
> →`finance_dunning` (the `remind/` action just calls into it).

---

## 1. What it is (and what it is NOT)

- A **`Customer`** (`models/ar.py:31`) is the AR sub-ledger party for one entity.
  Generic: a parent/student, a client, an internal counterparty. Its
  `receivable_account` is the AR **control** account its balance rolls into; the
  customer is the detail behind that control.
- An **`Invoice`** (`models/ar.py:85`) is a sales document. Posting raises the AR
  journal **Dr receivable / Cr revenue / Cr output tax** and links it via
  `journal`. A line billed before its **service period** starts credits
  **deferred income** instead of revenue, and a **refundable-deposit** line
  credits **deposits held** (§6).
- A **payer** is a customer billed for somebody else's service: a sponsor or an
  employer. The invoice is raised against the payer and names the other
  customer as its `beneficiary`; revenue is earned and collected from the payer,
  and the payer's statement names the beneficiary on the line. A tenant's own
  scholarship stays a concession (a discount, `finance_ar_adjustments`).
- A **`Payment`** (`models/ar.py:219`) is a customer **receipt** - money in,
  settling one or more **open AR items** (invoices **and** posted DEBIT notes, which
  debit AR the same way); overflow becomes customer credit.

**This does NOT:**
- **Settle another customer's bill, or a bill that is not posted.** Every
  settlement target (receipt, stored credit, credit note, concession, gateway
  confirmation) must be a posted document of the paying customer and of the
  paying document's branch (`_require_settlable_targets`). Money moves between
  customers only by an approved customer credit transfer (`finance_ar_adjustments`).
- **Bill a customer twice for one fee structure and period** (§6, fee runs).
- **Let AR carry a credit balance.** Overpayments are split at source into a
  **customer-credit liability** (`2140`), never left as a negative receivable
  (§6).
- **Edit a posted invoice's amounts.** Corrections are credit notes / write-offs
  (`finance_ar_adjustments`), not edits.
- **Move cash on allocation.** Allocating a *posted* receipt only reclassifies
  customer-credit → AR; no bank line moves (§6).

## 2. Domain model

| Model | File | Key fields | Notes |
|---|---|---|---|
| `Customer` | `models/ar.py` | `code`, `name`, billing\_*, `receivable_account`, `opening_balance`, `source_type`/`source_id` (loose strings, **not** FKs), `is_active` | `unique(entity, code)`; `unique(entity, source_type, source_id)` where a source is set |
| `Invoice` | `models/ar.py` | `customer`, `invoice_date`, `due_date`, `source`, `subtotal`/`tax_total`/`total`, `amount_paid`, `amount_credited`, `status`, `payment_status`, `journal`, `billing_key`, `billing_period`, `billing_period_label` | **two status axes** (below); check constraints keep `0 <= amount_paid + amount_credited <= total`; `billing_key` unique per customer among live invoices |
| `InvoiceLine` | `models/ar.py` | `revenue_account`, `quantity`, `unit_price`, `tax_code`, `net_amount`, `tax_amount`, `cost_center`, `dimensions`, `kind` (`CHARGE`/`DEPOSIT`), `service_start`/`service_end` | net/tax stored, not re-derived; a service period is both dates or neither and never ends before it starts (check constraint) |
| `Invoice.beneficiary` | `models/ar.py` | optional FK to `Customer` | set when the customer billed pays for somebody else |
| `DeferredIncomeEntry` | `models/accruals.py` | `invoice`, `line`, `revenue_account`, `cost_center`, `recognition_date`, `amount`, `unwound_amount`, `released_amount`, `status` (`PENDING`/`RELEASED`/`CANCELLED`), `release`, `void_journal` | one month's share of a deferred line |
| `DeferredIncomeRelease` | `models/accruals.py` | `branch`, `journal`, `amount`, `reversed_at` | one release journal (one branch, one month) |
| `DeferredIncomeUnwind` | `models/accruals.py` | `entry`, `adjustment_entry`, `amount`, `restored` | what a credit note, concession or write-off took back before release |
| `CustomerDeposit` | `models/accruals.py` | `customer`, `invoice`, `line`, `branch`, `amount`, `status` (`HELD`/`RELEASED`/`FORFEITED`/`CANCELLED`), `claim_opened_on`, `release_note`, `forfeiture` | one refundable deposit billed |
| `DepositForfeiture` | `models/accruals.py` | `branch`, `journal`, `amount` | one branch's unclaimed deposits taken to income by one run |
| `FinanceReceivablesPolicy` | `models/accruals.py` | `revenue_recognition`, `provision_bands`, `deposits_offset_unpaid_bills`, `unclaimed_deposit_years` | one per entity; defaults when absent |
| `Payment` | `models/ar.py` | `customer`, `payment_date`, `method`, `amount`, `allocated_amount`, `refunded_amount`, `transferred_amount`, `deposit_account`, `journal` | receipt; `allocated + refunded + transferred <= amount` is a check constraint; method `CREDIT_TRANSFER` marks a credit-transfer receipt |
| `PaymentAllocation` | `models/ar.py:268` | `payment`, `invoice`, `amount` | the receipt↔invoice link |
| `DebitNoteAllocation` | `models/adjustments.py:182` | `payment`, `note`, `amount` | the receipt↔DEBIT-note link (bumps `CreditNote.amount_paid`) |
| `FeeStructure` / `FeeItem` | `models/ar.py` | billing catalogue → invoices | `applies_to` gates AR generation; `FeeItem.is_optional` bills only assigned customers; `FeeItem.kind` `DEPOSIT` bills a refundable deposit |
| `FeeItemAssignment` | `models/ar.py` | `item`, `customer` | who takes an optional item; `unique(item, customer)` |

- **Money is kobo.** `total = subtotal + tax_total`; `settled = amount_paid +
  amount_credited`; `balance_due = total − settled` (`models/ar.py:140`,`:147`).
- **Two status axes on an invoice** (`models/ar.py:97`):
  - document `status` - ledger lifecycle: `DRAFT → POSTED → CANCELLED`.
  - `payment_status` - `UNPAID / PARTIAL / PAID`, *derived* from settled-vs-total
    by `refresh_payment_status` (`models/ar.py:162`), never set by hand.

## 3. Endpoint map

All require `?entity=<id|code>`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /customers/` | `finance.customer.view` | List + computed `balance`/`account_status` (**paginated**). Query: `search`, `is_active` | - | paginated `CustomerSerializer` + balance |
| `POST /customers/` | `finance.customer.create` | Create. AR control **defaults to the receivable mapping** and must be a non-cash asset; a source record already held by another customer is refused; if `opening_balance>0`, posts an **opening invoice** (§6), backdatable via `opening_date` | `code`, `name`, `receivable_account?`, billing\_*?, `opening_balance?`, `opening_date?`, `source_type?/source_id?` | `201` `CustomerSerializer` |
| `PATCH /customers/<pk>/` | `finance.customer.update` | Edit, audited (`CUSTOMER_UPDATED` with before/after). `source_type`/`source_id` and `receivable_account` are fixed once the customer has posted documents (422); `opening_balance` never changes after create (400); `is_active:false` deactivates (§4) | editable fields | `CustomerSerializer` |
| `POST /customers/opening/` | `finance.customer.import_opening` | Carry in unpaid bills from before go-live, one opening invoice per bill (§6), all or nothing, at most 500 rows | `invoices:[{customer, invoice_date, due_date?, amount, reference?, period_label?, narration?, branch?}]` | `201` invoices |
| `GET /customers/<pk>/` | `finance.customer.view` | Customer detail + ledger | - | detail |
| `POST /customers/<pk>/receipt/` | `finance.payment.create` | Record a receipt, auto-allocate | `amount`, `payment_date`, `deposit_account`, `method?`, `auto_allocate?`, `allocation_strategy?` (`oldest`\|`largest`) | `201` `{allocated, unallocated}` |
| `GET /invoices/` | `finance.invoice.view` | List. Query: `status`, `payment_status`, `bucket` (draft/issued/partial/paid/overdue), `search`, `customer` | - | paginated `InvoiceSerializer` |
| `POST /invoices/` | `finance.invoice.create` | Manual invoice; **posts** unless `post=false` (priced draft). With `beneficiary`, the customer is billed as the payer for that customer; the invoice takes the payer's branch, else the beneficiary's, else the raiser's | `customer`, `beneficiary?`, `invoice_date`, `lines:[{revenue_account, quantity?, unit_price, tax_code?, cost_center?, kind?, service_start?, service_end?}]`, `post?` | `201` `InvoiceSerializer` (with `beneficiary_id/code/name`) |
| `GET /invoices/summary/` | `finance.invoice.view` | KPIs, status counts, 12-month series | - | `success_response` |
| `GET /invoices/<pk>/` | `finance.invoice.view` | Full invoice: lines (with `kind` and service period), `deferred_income` (`pending`, `released`), allocations, GL, reminders | - | detail |
| `POST /invoices/<pk>/pay/` | `finance.payment.create` | Receipt settling **this** invoice | `amount`, `payment_date`, `deposit_account`, `method?`, … | `201` `InvoiceSerializer` |
| `POST /invoices/<pk>/remind/` | `finance.dunning.send` | Raise a dunning reminder → `finance_dunning` | `message?` | `DunningNoticeSerializer` |
| `GET /payments/` | `finance.payment.view` | Posted receipts + allocation state (**paginated**). Query: `status` (ALLOCATED/PARTIAL/UNALLOCATED, filtered in-DB), `method`, `customer`, `search` | - | paginated `PaymentSerializer` |
| `GET /payments/<pk>/` | `finance.payment.view` | Receipt + allocations + open-invoice **and open-debit-note** candidates + GL | - | detail |
| `POST /payments/<pk>/allocate/` | `finance.payment.allocate` | Apply stored customer credit to open AR items | `allocations:[{invoice\|debit_note, amount}]` **or** `auto_allocate:true` (+ `allocation_strategy?`) | `PaymentSerializer` |
| `GET/POST /fee-structures/…` | `finance.feestructure.view`/`.create` | Billing catalogue CRUD | - | `FeeStructureSerializer` |
| `POST /fee-structures/<pk>/generate/` | `finance.feestructure.generate` | One **posted** invoice per customer; skips a customer already billed from the structure, and an inactive one (`all_active` never selects them). A service period is stamped on every charge line | `customers:[…]` or `all_active:true`, `invoice_date?`, `due_date?`, `service_start?` + `service_end?` | `201` invoices |
| `GET /deferred-income/` | `finance.deferredincome.view` | Deferred income waiting, released, and due per month, in the caller's branches | - | `{pending, released, by_month}` |
| `POST /deferred-income/release/` | `finance.deferredincome.run` | Release every share due by `up_to` (default today, never later); idempotent. Whole-tenant callers only (403 `SHARED_RECORD_READ_ONLY` otherwise) | `up_to?` | releases |
| `POST /deferred-income/reverse/` | `finance.deferredincome.reverse` | Reverse the releases dated in an **open** period. Whole-tenant only | `period` (id) | `{reversed}` |
| `GET /deposits/` | `finance.deposit.view` | Refundable deposits in the caller's branches (**paginated**). Query: `customer`, `status` | - | paginated `CustomerDepositSerializer` |
| `POST /deposits/release/` | `finance.deposit.settle` | Release a customer's held deposits as credit, or with `offset` against their unpaid bills (policy permitting). The caller must reach every branch holding one | `customer`, `offset?` | credit notes |
| `POST /deposits/forfeit/` | `finance.deposit.run` | Take deposits unclaimed past the policy's limit to income. Whole-tenant only | `as_of?` | forfeitures + skipped |
| `GET/PATCH /settings/receivables/` | `finance.settings.view`/`.update` | The receivables policy; writes whole-tenant only | any of `revenue_recognition`, `provision_bands`, `deposits_offset_unpaid_bills`, `unclaimed_deposit_years` | settings, consumers, history |
| `GET/POST/DELETE /fee-structures/<pk>/items/<item>/assignments/` | `finance.feestructure.view`/`.edit` | Who takes one optional item; a required item takes no assignments (400) | `customers:[…]` | assigned customers |

> **Field note:** invoice/receipt creation reads `unit_price`×`quantity` and
> `amount` (kobo) - there is no separate `amount` on invoice *lines*. A line's
> `cost_center` now survives posting (see `finance_cost_centers` §6).

## 4. Lifecycle / state machine

**Invoice:** `DRAFT` (priced) → `POSTED` (`post_invoice`, raises AR journal) →
settled over time as receipts allocate (`payment_status` walks
`UNPAID→PARTIAL→PAID`). Created via `POST /invoices/` (posts unless `post=false`)
or `fee-structures/<pk>/generate/` (always posts).

As an invoice posts, any unapplied credit the customer holds in the invoice's
branch pays it, oldest credit first (`apply_customer_credit`, §6), unless the
entity's `auto_apply_customer_credit` document setting is off.

**Payment/receipt:** `DRAFT` → `POSTED` (`post_payment`: books cash, settles
invoices, parks overflow as credit). A posted receipt with leftover credit can be
applied later via `allocate/` (`allocate_payment`), and pays the customer's next
bill as it posts.

**Customer:** active, or deactivated (`vs_finance.customers.set_customer_active`,
audited). A deactivated customer is billed by no fee run and no `all_active`
selection; their documents, balance and debtor-list entry stay as they are. The
owner layer deactivates a child's account when the child leaves the roll and
reactivates it on readmission. Deactivating opens the claim on the customer's
held deposits (`claim_opened_on`, the start of the unclaimed-deposit clock) and,
where the policy sets deposits against unpaid bills and the customer leaves
owing, releases them against those bills at once (§6). Reactivating stops the
clock.

**Deferred income share:** `PENDING` → `RELEASED` (by a release run or the period
close) → back to `PENDING` if that month's releases are reversed while it is open.
`PENDING` → `CANCELLED` when an adjustment takes all of it back or the invoice is
voided.

**Deposit:** `HELD` → `RELEASED` (returned as credit or set against bills) /
`FORFEITED` (unclaimed past the limit) / `CANCELLED` (its invoice voided). Voiding
the release credit note puts it back to `HELD`.

## 5. Calculations

Pricing - `receivables.py`, all integer-exact `Decimal` then `ROUND_HALF_UP` to
kobo:
```
net = quantity × unit_price                      # compute_line_net (receivables.py:41)
tax = net × tax_code.rate_bps / 10000            # compute_tax        (receivables.py:47)
invoice.subtotal/tax_total/total = Σ over lines  # price_invoice / recompute_totals
```
Example: `quantity=1, unit_price=100000, VAT rate_bps=750` → net `100000`, tax
`7500`, total `107500`.

Allocation cap - `_apply_payment_subledger` (`receivables.py:247`):
```
apply = min(requested, invoice.balance_due, remaining_cash)   # per invoice
excess = payment.amount − Σ apply                              # → customer credit
```
Derived reads: `balance_due = total − amount_paid − amount_credited`;
`unallocated_amount = amount − allocated_amount` (`models/ar.py:263`);
`collection_rate = collected × 100 / invoiced` in the summary.

## 6. What posting does to the ledger

**Invoice posting** - `_post_invoice_atomic` (`receivables.py`), atomic, only a
`DRAFT` with a positive total and a customer that has an AR control:
```
Dr  receivable (AR control)        invoice.total          ← gross, unallocated
Cr  revenue (per account+cost_centre)  Σ net              ← P&L, carries cost centre
Cr  deferred income (2160)         Σ net of lines billed before their service period
Cr  deposits held (2170)           Σ net of DEPOSIT lines
Cr  output tax (per tax account)       Σ tax
```
A `CHARGE` line whose `service_start` is after the invoice date is **deferred**: its
net goes to the deferred-income mapping and `schedule_line`
(`deferred_income.py`) writes its monthly shares under the entity's
`revenue_recognition`: `SPREAD_MONTHLY` (the default) gives each calendar month the
service period touches an equal whole-kobo share, the remainder in the last month,
the first recognised on the service start and each later one on the first of its
month; `AT_PERIOD_START` recognises all of it on the service start. There is no "on
billing" option: a line with no service period, or one whose period has already
begun, is revenue on the invoice date. Output tax is due on the invoice and is
never deferred. A `DEPOSIT` line credits the deposits-held mapping whatever its
`revenue_account` says, carries no tax (refused if it would) and no service period,
and opens a `CustomerDeposit`.

**Releasing deferred income** - `release_deferred_income` moves every share
recognised on or before a date to revenue, one journal per branch per month, dated
at that month's end (or the run's date in the current month; a share whose month
is closed is released in the run's month):
```
Dr  deferred income (2160)         Σ open shares
Cr  revenue (per account+cost_centre)  per share
```
It runs on demand (`deferred-income/release/`) and as a step of period close, and a
month cannot close while a share recognised in it is unreleased (the
`deferred_income_released` close check). It is idempotent. The releases dated in a
period are reversed with `reverse_deferred_release` while that period is open;
their shares wait to be released again. Greenfield bills Second Term (6 January to
4 April 2027) on 10 December 2026 at N150,000: December holds a N150,000 liability
and no income, and January to April each release N37,500.

**Deposits** - `release_deposits` (`deposits.py`) raises one CREDIT note per branch
against the deposits-held account (`Dr 2170`). It settles the part of each deposit
its own bill never collected first (a deposit never paid is cancelled, not
refunded), then, with `offset`, the customer's other unpaid bills of that branch,
oldest first; the rest is customer credit (`2140`) that the refund route pays out.
`offset` needs the policy's `deposits_offset_unpaid_bills` (off by default).
`forfeit_unclaimed_deposits` takes deposits still held `unclaimed_deposit_years`
(default 6) after their customer left to income, `Dr 2170 / Cr 4820`, one journal
per branch; a deposit whose own bill still owes money is skipped and listed.
Then `post_journal` (all the `finance_journals_posting` guards apply), link
`invoice.journal`, stamp `POSTED`, `refresh_payment_status`, audit. A
`FinanceError` writes a **durable rejection** row and re-raises (`receivables.py:78`).

**Receipt posting** - `_post_payment_atomic` (`receivables.py:276`), the
**split-at-source** design so AR never goes credit:
```
Dr  deposit (bank/cash)            payment.amount
Cr  receivable (AR control)        applied            (only if applied > 0)
Cr  customer credit (2140)         excess             (only if overpaid)
```
`applied` is what the allocation plan settled (explicit `[(target, amount)]` or
oldest-first open items); each row is written and the target's `amount_paid` bumped
*before* the GL line, capped at balance/remaining. A target is an **invoice**
(`PaymentAllocation`, bumps `Invoice.amount_paid`) or a posted **DEBIT note**
(`DebitNoteAllocation`, bumps `CreditNote.amount_paid`). Both debit AR when raised, so
the single `Cr AR` for `applied` settles either - no debit-note-specific GL.

**Applying stored credit** - `allocate_payment` on an already-posted receipt
reclassifies, **no cash moves**:
```
Dr  customer credit (2140)         applied
Cr  receivable (AR control)        applied
```

**Opening balance** - `post_opening_balance` (`receivables.py`) raises a posted
opening invoice (`source=OPENING`) when a customer is created with
`opening_balance>0`, so the figure shows in both the GL and the (invoice-derived)
outstanding. The offset is the opening-balance equity mapping (retained earnings),
never revenue:
```
Dr  receivable (AR control)        opening_balance
Cr  retained earnings              opening_balance
```

**Opening import** - `import_opening_customer_invoices`
(`opening_balances.py`) carries in one opening invoice per unpaid bill, dated as
the original and due when it fell due (its invoice date when none is given), in
its customer's branch, so ageing is true from day one. Each posts an `OPENING`
journal, dated on the invoice date when a period covers it and on the first day
of the books otherwise, with the same `Dr AR / Cr retained earnings` as the supplier
side's opening bills. A bill dated on or after the day the books went live
(`opening_balances.books_went_live`, shared with procurement) is refused.

**Applying credit to a new invoice** - `apply_customer_credit` (`receivables.py`)
runs as every invoice posts: each of the customer's credit lots in the invoice's
branch, oldest first, is applied through `allocate_payment` /
`allocate_credit_note`, so the journal (`Dr 2140 / Cr AR`, dated at the later of
credit and bill) and audit row are those of a manual allocation.

**Settlement guards** - every settlement writer locks its targets in a fixed order
(`lock_settlement_targets`, `select_for_update(of=("self",))` by model then pk) and
judges them after locking (`_require_settlable_targets`): posted, the paying
customer's, the paying document's branch, and an invoice when a credit note pays.
Concessions and write-offs lock their invoice and their own row, so a double click
posts once. The database refuses settlement beyond an invoice's total and a
receipt or credit note spending more than it holds. A gateway confirmation for an
invoice that was voided, or belongs to another customer, parks the money as credit
and records `RECEIPT_PARKED_AS_CREDIT` for the bursar.

**Fee runs** - `generate_invoices` (`fees.py`) locks the fee structure for the run,
so two runs queue. Each invoice carries `billing_key` (`FEE:<code>`, plus
`@<period>` when the owner layer names a billing period) and the period's key and
label; a customer holding a live invoice with the same key is skipped, and the
database's unique key refuses a second one outright. Without a period a structure
bills a customer once; with one, once per period. The period stamp is fixed once
the invoice posts (`Invoice.save` refuses a change), and period reports read it.

**Auto-allocation order** - `_build_invoice_plan` (`receivables.py:272`) settles
either `oldest` (document date, default) or `largest` (biggest balance) first, across
open invoices **and** open DEBIT notes together (`include_debit_notes=True` on the
receipt paths; the credit-note sub-ledger keeps its own invoice-only plan);
`post_payment`/`allocate_payment` take a `strategy` arg, exposed as
`allocation_strategy` on the receipt/allocate endpoints. Explicit allocations may name
a debit note with `{debit_note, amount}` instead of `{invoice, amount}`, and the
payment-detail view lists the customer's `open_debit_notes` alongside `open_invoices`.

## 7. Worked example

`POST /v1/finance/invoices/?entity=LEKKI`:
```json
{ "customer": "CUST-0001", "invoice_date": "2026-06-26",
  "lines": [ { "revenue_account": "4100", "quantity": 1, "unit_price": 100000,
               "tax_code": "VAT", "cost_center": "PRI" } ] }
```
→ priced (net 100000, tax 7500, total 107500), posted: Dr `1200` 107500 / Cr
`4100` 100000 (cost_centre PRI) / Cr `2200` 7500. `201` `InvoiceSerializer` with
`status:"POSTED"`, `payment_status:"UNPAID"`, `balance_due:107500`.

`POST /v1/finance/invoices/<id>/pay/` `{amount:107500, payment_date, deposit_account:"1100"}`
→ receipt Dr `1100` 107500 / Cr `1200` 107500; invoice `payment_status:"PAID"`,
`balance_due:0`. Overpay 120000 instead → Cr `1200` 107500 + Cr `2140` 12500, and
the receipt shows `allocation_status:"PARTIAL"` (`unallocated_amount` 12500).

## 8. Gotchas / known limitations

- **A service period decides when income is earned, not the invoice date.** The
  owner layer stamps a term's (or a whole-year fee's session's) dates on every fee
  line it bills, so a term billed before it starts is deferred. A manual invoice or
  a fee run from the finance screen is deferred only when it is given a service
  period.
- **The recognition method applies to lines posted after it changes.** A line
  already scheduled keeps its schedule.
- **A deferred invoice is owed in full from its invoice date.** Deferral changes
  where the credit goes, not the receivable: ageing, dunning and collections read it
  as before.
- **Deposits are held, not earned.** A deposit item's account is always the
  deposits-held liability; a revenue account cannot be picked for it.

- **Credit pays new bills by default.** An entity that holds money on account on
  purpose turns `auto_apply_customer_credit` off in its document settings; credit
  then waits for a manual allocation, and dunning still does not chase what it
  covers.
- **A customer's source record and receivable account are fixed once money has
  moved.** Open a new customer for a different record; correct a balance with a
  credit or debit note.

- **`opening_balance` posting is atomic with customer-create** - if no open period
  covers today (or `4100` is missing), the opening invoice fails and the whole
  customer create rolls back with a clear error. Intended (loud > silent), but
  worth knowing.
- ✅ **The opening invoice is backdatable** - pass an optional `opening_date` on
  customer create to seat migrated balances in their historical period (defaults to
  today; a closed/missing period still rolls the whole create back, loudly).
- **Invoice create defaults `post=True`** - omitting `post` posts immediately;
  pass `post:false` for a draft.
- **`pay/` requires a POSTED invoice** (`views_ar.py`); paying a draft → 400.
- **AR control defaults to `1200`** on customer create if omitted - verify the
  chart has it (it's in the seeded `DEFAULT_CHART`).
- ✅ **Fixed 2026-07-05: receipts settle DEBIT notes.** A DEBIT note debits AR like an
  invoice, but receipts used to allocate only to invoices - so a debit note raised with
  no invoice sat unsettled and the whole receipt fell to `2140`, overstating customer
  credit. Debit notes are now open AR items in allocation, the customer ledger/statement,
  and the refund cap (`customer_credit_balance` nets unsettled DN balances). See
  `finance_ar_adjustments` §8.

_Fixed (were flagged here): customer/receipt lists now paginate via XVSPagination
(no 500-cap truncation); `opening_balance` posts an opening invoice; auto-allocation
supports `oldest`|`largest`; receipts settle DEBIT notes (2026-07-05)._

## 9. Permissions & tenant isolation

- Verbs are split by action: `finance.customer.{view,create,update,import_opening}`,
  `finance.invoice.{view,create}`, `finance.payment.{view,create,allocate}`,
  `finance.feestructure.{view,create,edit,generate}`; `remind/` uses
  `finance.dunning.send`. The document settings `auto_apply_customer_credit` and
  `concession_second_person_threshold` are written only by a whole-tenant caller
  (`finance.settings.update`).
- Every view resolves the entity first then `filter(entity=…, pk=…)`
  (e.g. `views_ar.py:1159`, `:494`), so another tenant's invoice/payment id → 404.
  `_resolve_customer`/`_resolve_account` are entity-scoped → no cross-tenant
  attach. ✅
- `InvoiceSerializer` exposes no secrets (ids, codes, money, dates, status). FLS
  not required here; billing PII (`billing_email/phone/address`) lives on
  `CustomerSerializer` - review if those become sensitive.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ar.py` | `Customer`, `Invoice`, `InvoiceLine`, `Payment`, `PaymentAllocation`, `FeeStructure`/`FeeItem` |
| `receivables.py` | pricing (`compute_line_net`/`compute_tax`/`price_invoice`), `post_invoice`, `apply_customer_credit`, `post_payment`, `allocate_payment`, the settlement guards |
| `fees.py` | `generate_invoices` (fee structure → posted invoices, service period on charge lines), billing keys |
| `deferred_income.py` | schedules, release and its reversal, unwinding by adjustments and voids, the close check |
| `deposits.py` | deposits opened at posting, release, the leaver path, forfeiture |
| `receivables_policy.py` | the receivables policy: read, validate, update |
| `views_accruals.py` | deferred income, deposits, provisions, write-off recovery and the policy screen |
| `customers.py` | customer edits (fixed fields, audit), deactivation |
| `opening_balances.py` | `books_went_live`, opening customer invoices and their import |
| `collected.py` | the one definition of billed and collected, shared with the owner layer |
| `views.py` | `InvoiceListCreateView`, `InvoiceSummaryView`, `InvoiceDetailView` |
| `views_ar.py` | customer/receipt/payment/allocate/pay/remind/fee-structure views |
| `serializers.py` | `InvoiceSerializer`, `CustomerSerializer`, `PaymentSerializer`, `FeeStructureSerializer` |
| `constants.py` | `CUSTOMER_CREDIT_CODE` (`2140`), `InvoicePaymentStatus`, `InvoiceSource` |

## 11. Test coverage & gaps

Existing (see `tests.py`): `InvoicePostingTests` (balanced AR journal + tax,
closed-period rejection), `PaymentAllocationTests`, `InvoiceCreateEndpointTests`,
`ReceiptAllocationEndpointTests`, `CustomerEndpointTests`, `InvoicePayRemindEndpointTests`.

Added with the §8 fixes (in `FinanceAPITests`): opening-balance posts the
`Dr 1200 / Cr 4100` opening invoice and surfaces in the paginated customer list;
largest-first receipt clears the bigger invoice first; an unknown
`allocation_strategy` → 400.

`tests_accruals.py` covers deferred income (posting, the spread and its remainder,
start-of-period recognition, release per branch per month and its idempotency, the
close step and check, reversal with the open month, unwinding by credit notes,
concessions, write-offs and voids), deposits (held, returned, cancelled when never
paid, offset against bills with the policy, a bill carrying the deposit settled
once, forfeited after the limit, voids), the payer and beneficiary, and the
whole-tenant gates on the runs and the policy, at a two-branch and a one-branch
school. `schools.core.fal.tests.test_service_period` covers the owner layer
stamping a term's or a session's dates.

`tests_ar_guards.py` covers the receivables guards: settlement targets and
constraints, credit transfers, credit applied to new bills, dunning cover, credit
notes naming a bill, fee-run idempotency, optional items and period stamps,
deactivation, cumulative approval and the second person, the customer master record,
opening imports, the collections definition, gateway parking and payroll share
journals.

Worth asserting if not already:
- **403** per verb; **cross-tenant** invoice/payment/customer id → 404.
- Overpayment → customer-credit (`2140`) line + `allocation_status:"PARTIAL"`;
  later `allocate/` moves credit → AR with no cash line.
- `post=false` saves a priced draft (no journal); paying a draft invoice → 400.
- Empty-list shape on a fresh entity.
- Fee-structure generate rejects non-`CUSTOMER` `applies_to`.
