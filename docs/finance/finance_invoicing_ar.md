# finance_invoicing_ar

Accounts-Receivable core: **customers** (the billable parties), **invoices** raised
against them, **receipts/payments** that settle those invoices, and **fee
structures** that mass-generate invoices. This is the sales/billing side of the
ledger — money owed *to* the entity and the cash that clears it.

Routes covered (mounted at `/v1/finance/`): `customers/`, `customers/<pk>/`,
`customers/<pk>/receipt/`, `invoices/`, `invoices/summary/`, `invoices/<pk>/`,
`invoices/<pk>/pay/`, `invoices/<pk>/remind/`, `payments/`, `payments/<pk>/`,
`payments/<pk>/allocate/`, `fee-structures/…`.

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
  `journal`.
- A **`Payment`** (`models/ar.py:219`) is a customer **receipt** — money in,
  settling one or more invoices; overflow becomes customer credit.

**This does NOT:**
- **Auto-post `opening_balance`.** A customer's `opening_balance` is informational
  only (`models/ar.py:64`); seat real opening AR via a direct entry
  (`finance_journals_posting`).
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
| `Customer` | `models/ar.py:31` | `code`, `name`, billing\_*, `receivable_account`, `opening_balance`, `source_type`/`source_id` (loose strings, **not** FKs), `is_active` | `unique(entity, code)` |
| `Invoice` | `models/ar.py:85` | `customer`, `invoice_date`, `due_date`, `source`, `subtotal`/`tax_total`/`total`, `amount_paid`, `amount_credited`, `status`, `payment_status`, `journal` | **two status axes** (below) |
| `InvoiceLine` | `models/ar.py:176` | `revenue_account`, `quantity`, `unit_price`, `tax_code`, `net_amount`, `tax_amount`, `cost_center`, `dimensions` | net/tax stored, not re-derived |
| `Payment` | `models/ar.py:219` | `customer`, `payment_date`, `method`, `amount`, `allocated_amount`, `deposit_account`, `journal` | receipt |
| `PaymentAllocation` | `models/ar.py:268` | `payment`, `invoice`, `amount` | the receipt↔invoice link |
| `FeeStructure` / `FeeItem` | `models/ar.py:302`/`:364` | billing catalogue → invoices | `applies_to` gates AR generation |

- **Money is kobo.** `total = subtotal + tax_total`; `settled = amount_paid +
  amount_credited`; `balance_due = total − settled` (`models/ar.py:140`,`:147`).
- **Two status axes on an invoice** (`models/ar.py:97`):
  - document `status` — ledger lifecycle: `DRAFT → POSTED → CANCELLED`.
  - `payment_status` — `UNPAID / PARTIAL / PAID`, *derived* from settled-vs-total
    by `refresh_payment_status` (`models/ar.py:162`), never set by hand.

## 3. Endpoint map

All require `?entity=<id|code>`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /customers/` | `finance.customer.view` | List + computed `balance`/`account_status` (capped 500). Query: `search`, `is_active` | — | list of `CustomerSerializer` + balance |
| `POST /customers/` | `finance.customer.create` | Create. AR control **defaults to `1200`** | `code`, `name`, `receivable_account?`, billing\_*?, `opening_balance?`, `source_type?/source_id?` | `201` `CustomerSerializer` |
| `GET /customers/<pk>/` | `finance.customer.view` | Customer detail + ledger | — | detail |
| `POST /customers/<pk>/receipt/` | `finance.payment.create` | Record a receipt, auto-allocate oldest-first | `amount`, `payment_date`, `deposit_account`, `method?`, `auto_allocate?` | `201` `{allocated, unallocated}` |
| `GET /invoices/` | `finance.invoice.view` | List. Query: `status`, `payment_status`, `bucket` (draft/issued/partial/paid/overdue), `search`, `customer` | — | paginated `InvoiceSerializer` |
| `POST /invoices/` | `finance.invoice.create` | Manual invoice; **posts** unless `post=false` (priced draft) | `customer`, `invoice_date`, `lines:[{revenue_account, quantity?, unit_price, tax_code?, cost_center?}]`, `post?` | `201` `InvoiceSerializer` |
| `GET /invoices/summary/` | `finance.invoice.view` | KPIs, status counts, 12-month series | — | `success_response` |
| `GET /invoices/<pk>/` | `finance.invoice.view` | Full invoice: lines, allocations, GL, reminders | — | detail |
| `POST /invoices/<pk>/pay/` | `finance.payment.create` | Receipt settling **this** invoice | `amount`, `payment_date`, `deposit_account`, `method?`, … | `201` `InvoiceSerializer` |
| `POST /invoices/<pk>/remind/` | `finance.dunning.send` | Raise a dunning reminder → `finance_dunning` | `message?` | `DunningNoticeSerializer` |
| `GET /payments/` | `finance.payment.view` | Posted receipts + allocation state. Query: `status` (ALLOCATED/PARTIAL/UNALLOCATED), `method`, `customer`, `search` | — | list of `PaymentSerializer` |
| `GET /payments/<pk>/` | `finance.payment.view` | Receipt + allocations + open-invoice candidates + GL | — | detail |
| `POST /payments/<pk>/allocate/` | `finance.payment.allocate` | Apply stored customer credit to invoices | `allocations:[{invoice, amount}]` **or** `auto_allocate:true` | `PaymentSerializer` |
| `GET/POST /fee-structures/…` | `finance.feestructure.view`/`.create` | Billing catalogue CRUD | — | `FeeStructureSerializer` |
| `POST /fee-structures/<pk>/generate/` | `finance.feestructure.generate` | One **posted** invoice per customer | `customers:[…]` or `all_active:true`, `invoice_date?`, `due_date?` | `201` invoices |

> **Field note:** invoice/receipt creation reads `unit_price`×`quantity` and
> `amount` (kobo) — there is no separate `amount` on invoice *lines*. A line's
> `cost_center` now survives posting (see `finance_cost_centers` §6).

## 4. Lifecycle / state machine

**Invoice:** `DRAFT` (priced) → `POSTED` (`post_invoice`, raises AR journal) →
settled over time as receipts allocate (`payment_status` walks
`UNPAID→PARTIAL→PAID`). Created via `POST /invoices/` (posts unless `post=false`)
or `fee-structures/<pk>/generate/` (always posts).

**Payment/receipt:** `DRAFT` → `POSTED` (`post_payment`: books cash, settles
invoices, parks overflow as credit). A posted receipt with leftover credit can be
applied later via `allocate/` (`allocate_payment`).

## 5. Calculations

Pricing — `receivables.py`, all integer-exact `Decimal` then `ROUND_HALF_UP` to
kobo:
```
net = quantity × unit_price                      # compute_line_net (receivables.py:41)
tax = net × tax_code.rate_bps / 10000            # compute_tax        (receivables.py:47)
invoice.subtotal/tax_total/total = Σ over lines  # price_invoice / recompute_totals
```
Example: `quantity=1, unit_price=100000, VAT rate_bps=750` → net `100000`, tax
`7500`, total `107500`.

Allocation cap — `_apply_payment_subledger` (`receivables.py:247`):
```
apply = min(requested, invoice.balance_due, remaining_cash)   # per invoice
excess = payment.amount − Σ apply                              # → customer credit
```
Derived reads: `balance_due = total − amount_paid − amount_credited`;
`unallocated_amount = amount − allocated_amount` (`models/ar.py:263`);
`collection_rate = collected × 100 / invoiced` in the summary.

## 6. What posting does to the ledger

**Invoice posting** — `_post_invoice_atomic` (`receivables.py:95`), atomic, only a
`DRAFT` with a positive total and a customer that has an AR control:
```
Dr  receivable (AR control)        invoice.total          ← gross, unallocated
Cr  revenue (per account+cost_centre)  Σ net              ← P&L, carries cost centre
Cr  output tax (per tax account)       Σ tax
```
Then `post_journal` (all the `finance_journals_posting` guards apply), link
`invoice.journal`, stamp `POSTED`, `refresh_payment_status`, audit. A
`FinanceError` writes a **durable rejection** row and re-raises (`receivables.py:78`).

**Receipt posting** — `_post_payment_atomic` (`receivables.py:276`), the
**split-at-source** design so AR never goes credit:
```
Dr  deposit (bank/cash)            payment.amount
Cr  receivable (AR control)        applied            (only if applied > 0)
Cr  customer credit (2140)         excess             (only if overpaid)
```
`applied` is what the allocation plan settled (explicit `[(invoice, amount)]` or
oldest-first open invoices); each `PaymentAllocation` row is written and the
invoice's `amount_paid` bumped *before* the GL line, capped at balance/remaining.

**Applying stored credit** — `allocate_payment` (`receivables.py:345`) on an
already-posted receipt reclassifies, **no cash moves**:
```
Dr  customer credit (2140)         applied
Cr  receivable (AR control)        applied
```

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

- **`opening_balance` is decorative** — never posts. Easy to assume it seeds AR.
- **Customer list & receipts are capped at 500** (`views_ar.py:222`, `:474`) and
  **un-paginated** (`success_response` list) — a large entity silently truncates.
- **Invoice create defaults `post=True`** — omitting `post` posts immediately;
  pass `post:false` for a draft.
- **`pay/` requires a POSTED invoice** (`views_ar.py:1163`); paying a draft → 400.
- **AR control defaults to `1200`** on customer create if omitted — verify the
  chart has it (it's in the seeded `DEFAULT_CHART`).
- Allocation order is `due_date, invoice_date, id` (oldest-first); no
  "largest-first" option.

## 9. Permissions & tenant isolation

- Verbs are split by action: `finance.customer.{view,create,update}`,
  `finance.invoice.{view,create}`, `finance.payment.{view,create,allocate}`,
  `finance.feestructure.{view,create,generate}`; `remind/` uses
  `finance.dunning.send`.
- Every view resolves the entity first then `filter(entity=…, pk=…)`
  (e.g. `views_ar.py:1159`, `:494`), so another tenant's invoice/payment id → 404.
  `_resolve_customer`/`_resolve_account` are entity-scoped → no cross-tenant
  attach. ✅
- `InvoiceSerializer` exposes no secrets (ids, codes, money, dates, status). FLS
  not required here; billing PII (`billing_email/phone/address`) lives on
  `CustomerSerializer` — review if those become sensitive.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ar.py` | `Customer`, `Invoice`, `InvoiceLine`, `Payment`, `PaymentAllocation`, `FeeStructure`/`FeeItem` |
| `receivables.py` | pricing (`compute_line_net`/`compute_tax`/`price_invoice`), `post_invoice`, `post_payment`, `allocate_payment` |
| `fees.py` | `generate_invoices` (fee structure → posted invoices) |
| `views.py` | `InvoiceListCreateView`, `InvoiceSummaryView`, `InvoiceDetailView` |
| `views_ar.py` | customer/receipt/payment/allocate/pay/remind/fee-structure views |
| `serializers.py` | `InvoiceSerializer`, `CustomerSerializer`, `PaymentSerializer`, `FeeStructureSerializer` |
| `constants.py` | `CUSTOMER_CREDIT_CODE` (`2140`), `InvoicePaymentStatus`, `InvoiceSource` |

## 11. Test coverage & gaps

Existing (see `tests.py`): `InvoicePostingTests` (balanced AR journal + tax,
closed-period rejection), `PaymentAllocationTests`, `InvoiceCreateEndpointTests`,
`ReceiptAllocationEndpointTests`, `CustomerEndpointTests`, `InvoicePayRemindEndpointTests`.

Worth asserting if not already:
- **403** per verb; **cross-tenant** invoice/payment/customer id → 404.
- Overpayment → customer-credit (`2140`) line + `allocation_status:"PARTIAL"`;
  later `allocate/` moves credit → AR with no cash line.
- `post=false` saves a priced draft (no journal); paying a draft invoice → 400.
- Empty-list shape on a fresh entity (`[]` → `{}`).
- Fee-structure generate rejects non-`CUSTOMER` `applies_to`.
- The 500-cap truncation on customer/receipt lists (document or paginate).
