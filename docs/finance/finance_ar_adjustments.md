# finance_ar_adjustments

The non-invoice ways a receivable changes after an invoice is posted: **credit /
debit notes**, **customer refunds**, **bad-debt write-offs**, and **concessions**
(`DISCOUNT` / `WAIVER` / `SCHOLARSHIP` - the last being the domain-neutral name a
school tenant reads as a bursary). Each either gives value back to a customer,
charges them more, or recognises that a balance won't be collected - without
editing the original invoice (which is immutable once posted).

Routes (mounted at `/v1/finance/`): `credit-notes/…`, `refunds/…`,
`invoices/<pk>/write-off/`, `ar-adjustments/`, `concessions/…`,
`credit-transfers/…`.

> **Adjacent:** the AR core (customers, invoices, receipts) is `finance_invoicing_ar`;
> installment plans are `finance_payment_plans`. Concessions live in the same model
> file as payment plans but belong here conceptually.

---

## 1. What it is (and what it is NOT)

- **`CreditNote`** (`models/adjustments.py:33`): `kind=CREDIT` reduces AR (gives
  value back); `kind=DEBIT` increases AR (a supplementary charge). Doc-number
  token tracks the kind - `CRN` vs `DRN` (`save()`, `models/adjustments.py:104`).
- **`Refund`** (`models/adjustments.py:182`): pays **cash** back out of a
  customer's **credit balance** (the `2140` liability) - *not* off an invoice.
- **`Concession`** (`models/adjustments.py:228`): a non-cash reduction of a
  *specific* invoice's balance; `kind` ∈ {DISCOUNT, WAIVER, SCHOLARSHIP}.
- **Write-off**: recognising bad debt on an invoice, raised as a
  `WriteOffRequest` document and posted by `write_off_invoice` (§6).
- **`CustomerCreditTransfer`** (`models/adjustments.py`): moves one customer's
  unapplied credit to another customer, the only way money crosses between
  customers. Always approved by a second person; there is no direct post (§6).

**This does NOT:**
- **Apply one customer's money to another customer's bill.** A credit note,
  concession or write-off reduces only its own customer's posted bill; a credit
  transfer moves credit between customers.
- **Refund against an invoice.** A refund draws down customer credit (`2140`);
  there must be credit available, and it's **capped** at it (§5). To reverse an
  invoice's revenue, use a CREDIT note.
- **Edit posted invoices.** All four mechanisms post *new* journals; the invoice's
  `amount_credited` / `amount_paid` move, never its lines.
- **Carry cost centres on refunds/write-offs/concessions.** Those are
  single-account postings with no analytics by design. (Credit/debit **notes** do
  now carry the line's cost centre to the GL - §6.)

## 2. Domain model

| Model | File | Key fields | Notes |
|---|---|---|---|
| `CreditNote` | `models/adjustments.py:33` | `customer`, `kind` (CREDIT/DEBIT), `note_date`, `invoice?`, `subtotal`/`tax_total`/`total`, `allocated_amount`, **`amount_paid`, `settlement_status`**, `journal` | `unallocated_amount = total − allocated` (CREDIT only); `balance_due = total − amount_paid` (DEBIT only) |
| `CreditNoteLine` | `:113` | `revenue_account`, `quantity`, `unit_price`, `tax_code`, `net_amount`, `tax_amount`, `cost_center` | priced like an invoice line |
| `CreditNoteAllocation` | `:150` | `note`, `invoice`, `amount` | CREDIT note → invoice; bumps `Invoice.amount_credited`; `unique(note, invoice)` |
| `DebitNoteAllocation` | `:182` | `payment`, `note`, `amount` | receipt → DEBIT note; bumps `CreditNote.amount_paid`; `unique(payment, note)` |
| `Refund` | `:182` | `customer`, `refund_date`, `method`, `amount`, `bank_account?`/`deposit_account?`, `journal` | pays out credit |
| `Concession` | `:228` | `customer`, `invoice`, `kind`, `amount`, `allowance_account?`, `journal` | single amount, no lines; the invoice must be the customer's |
| `CustomerCreditTransfer` | `models/adjustments.py` | `from_customer`, `to_customer`, `transfer_date`, `amount`, `reason`, `receipt` | the destination's credit-transfer receipt carries the GL effect |
| `CustomerCreditTransferDraw` | `models/adjustments.py` | `transfer`, `payment` or `note`, `amount` | which source lots a transfer drew; bumps their `transferred_amount` |

- Money is kobo. `CreditNote`/`Refund`/`Concession` all extend `FinanceDocument`
  (entity scope, numbered, `status`, `created_by`).
- **No `WriteOff` model** - write-offs exist only as a journal and a
  `FinanceAuditLog` row (`action=INVOICE_WRITTEN_OFF`); the AR-adjustments list
  reconstructs them from that log (`_writeoff_rows`, `views_ar.py:1058`).

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /credit-notes/` | `finance.creditnote.view` | List (paginated). Query: `kind`, `customer`, `search`, `status` (draft/issued/applied) | - | paginated `CreditNoteSerializer` |
| `POST /credit-notes/` | `finance.creditnote.create` | Create a **draft** (priced) note | `customer`, `kind?`, `note_date`, `invoice?`, `reason?`, `lines:[{revenue_account, quantity?, unit_price, tax_code?, cost_center?}]` | `201` `CreditNoteSerializer` |
| `GET /credit-notes/<pk>/` | `finance.creditnote.view` | One note | - | detail |
| `POST /credit-notes/<pk>/post/` | `finance.creditnote.post` | Post it; CREDIT notes may auto/explicitly allocate | `allocations:[{invoice, amount}]?`, `auto_allocate?` | `CreditNoteSerializer` |
| `POST /credit-notes/<pk>/allocate/` | `finance.creditnote.allocate` | Apply a posted CREDIT note's stored credit | `allocations:[{invoice, amount}]?` | `CreditNoteSerializer` |
| `GET /refunds/` | `finance.refund.view` | Paginated list. Query: `status`, `customer` | - | paginated `RefundSerializer` |
| `GET /refunds/availability/` | `finance.refund.create` | Active customers with unreserved refundable credit. Query: `search`, `page`, `page_size` | - | paginated `{customer_id, customer_code, customer_name, refundable_credit}` |
| `POST /refunds/` | `finance.refund.create` | Create a **draft** refund, capped at currently unreserved credit | `customer`, `refund_date`, `amount`, `method?`, `bank_account?` | `201` `RefundSerializer` |
| `GET /refunds/<pk>/` | `finance.refund.view` | One refund | - | detail |
| `POST /refunds/<pk>/post/` | `finance.refund.post` | Pay it out (capped at customer credit) | - | `RefundSerializer` |
| `POST /invoices/<pk>/write-off/` | `finance.invoice.writeoff` | Write off bad debt | `amount?` (default full balance), `write_off_account?`, `write_off_date?`, `narration?` | `InvoiceSerializer` |
| `GET /ar-adjustments/` | `finance.refund.view` | Unified refunds + write-offs + KPIs (paginated) | - | `{rows, kpis, pagination}` |
| `GET /concessions/` | `finance.concession.view` | List (paginated). Query: `kind`, `customer`, `search` | - | paginated `ConcessionSerializer` |
| `POST /concessions/` | `finance.concession.create` | Create a **draft** concession | `customer`, `invoice`, `kind?`, `concession_date`, `amount`, `allowance_account?`, `reason?` | `201` `ConcessionSerializer` |
| `GET /concessions/summary/` | `finance.concession.view` | KPI totals | - | `success_response` |
| `GET /concessions/<pk>/` | `finance.concession.view` | One concession | - | detail |
| `POST /concessions/<pk>/post/` | `finance.concession.post` | Post it (reduces the invoice); refused to its own author above the second-person threshold | - | `ConcessionSerializer` |
| `GET /credit-transfers/` | `finance.credittransfer.view` | List (paginated). Query: `status`, `customer` (either side) | - | paginated `CustomerCreditTransferSerializer` |
| `POST /credit-transfers/` | `finance.credittransfer.create` | Create a **draft** transfer; refused if the source lacks the credit | `from_customer`, `to_customer`, `amount`, `transfer_date`, `reason?`, `branch?` | `201` |
| `GET /credit-transfers/<pk>/` | `finance.credittransfer.view` | One transfer | - | detail |
| `POST /credit-transfers/<pk>/submit/` | `finance.credittransfer.submit` | Submit for approval; posts only on final approval | - | transfer + `approval` block |
| `POST /credit-transfers/<pk>/void/` | `finance.credittransfer.reverse` | Void: voids the destination receipt and restores the source lots | `date?` | detail |

## 4. Lifecycle / state machine

- **Credit/debit note:** `DRAFT` (priced) → `POSTED` (`post_credit_note`). A
  POSTED **CREDIT** note can then `allocate/` its stored credit onto invoices. A
  **DEBIT** note cannot `allocate/` (it raised AR, not credit) - but because it
  debits AR like an invoice, it **is settled by receipts**: `post_payment` /
  `allocate_payment` treat open DEBIT notes as AR open items and bump their
  `amount_paid` / `settlement_status` (UNPAID → PARTIAL → PAID) via
  `DebitNoteAllocation`. See `finance_invoicing_ar` §receipts.
- **Refund / concession:** `DRAFT` → `POSTED` (`post_refund` / `post_concession`).
  A concession and its invoice are locked and re-read before posting, so a double
  click posts once.
- **Write-off:** a `WriteOffRequest` `DRAFT` → `POSTED`, locked the same way.
- **Customer credit transfer:** `DRAFT` → `PENDING_APPROVAL` (submit) →
  `APPROVED` → `POSTED` (`post_customer_credit_transfer`, run by the approval) →
  `REVERSED` (void). The seeded route (`finance.customer_credit_transfer`) has one
  always-on step; a tenant's books carry the route empty until it adds approvers,
  and an empty route refuses submission.

**Approval weight of concessions and credit notes.** A threshold step weighs
`cumulative_amount`: the document with every other live reduction (concession or
CREDIT note pending approval, approved or posted) of the same bill and of the same
customer's billing period (the period stamped on the bill, or the fiscal period of
the document's date), whichever total is larger
(`approvals.cumulative_adjustment_amount`). Three discounts of ₦49,000 on one bill
weigh ₦49,000, ₦98,000 and ₦147,000, so the second and third meet the ₦50,000 step.
Separately, above the entity's `concession_second_person_threshold` (document
setting, default ₦10,000, weighed the same way) the person who raised a concession
may not post it, directly or as its approver (`approvals.require_second_person`).

## 5. Calculations

Notes reuse the invoice pricing (`receivables.compute_line_net`/`compute_tax`,
`ROUND_HALF_UP` to kobo) via `price_credit_note` (`credit_notes.py:48`).

Caps that protect the books:
```
refund.amount   ≤ customer_credit_balance(customer)   # credit_notes.py:309 → receivables.py
write_off amount = balance_due if unset; must be 0 < amount ≤ balance_due
concession.amount must be 0 < amount ≤ invoice.balance_due
credit-note allocation: apply = min(requested, invoice.balance_due, remaining)
```
`customer_credit_balance` = unapplied receipts + unapplied CREDIT notes − refunds
already paid − **unsettled DEBIT-note balances**, floored at 0 (`receivables.py`).
An open DEBIT note is a supplementary charge still to collect, so it offsets what a
refund may pay out. `customer_refund_available_balance` additionally subtracts
`PENDING_APPROVAL` refunds (excluding the current document during revalidation), so
two requests cannot promise the same credit. Drafts do not reserve value.

## 6. What posting does to the ledger

**CREDIT note** (`_post_credit_note_atomic`, `credit_notes.py:93`) - give value back;
split-at-source so AR never goes credit:
```
Dr  revenue / returns (per account)   Σ net
Dr  output-tax reversal (per account) Σ tax
Cr  receivable (AR control)           applied        (settles invoices)
Cr  customer credit (2140)            excess          (unapplied remainder)
```
**DEBIT note** - supplementary charge (debits AR just like an invoice):
```
Dr  receivable (AR control)   total
Cr  revenue (per account)     Σ net
Cr  output tax (per account)  Σ tax
```
A later receipt settles it with no new GL beyond the receipt's own `Cr AR` for the
applied portion - the DEBIT note's AR debit and the receipt's AR credit net off, and
only the true excess lands in `2140`. The sub-ledger record is a `DebitNoteAllocation`.
**Refund** (`_post_refund_atomic`, `credit_notes.py:327`):
```
Dr  customer credit (2140)   amount
Cr  bank / deposit           amount
```
**Write-off** (`_write_off_invoice_atomic`, `credit_notes.py:413`):
```
Dr  bad-debt expense (5300)  amount
Cr  receivable (AR control)  amount        + invoice.amount_credited += amount
```
**Concession** (`_post_concession_atomic`, `installments.py`):
```
Dr  discounts & allowances (4910)  amount
Cr  receivable (AR control)        amount  + invoice.amount_credited += amount
```
The allowance account must be revenue (usually contra) or expense, and a write-off
account an expense or contra-revenue account (`accounts.require_account_kind`).

**CREDIT note naming a bill** - the named invoice is settled first, up to its
balance, on the direct and the approval path alike; any explicit `allocations`
settle after it, and the remainder is customer credit, which pays the customer's
next bill as it posts. A note naming no bill keeps the old behaviour (explicit
allocations, or oldest-first on the direct path). The named invoice must be the
note's customer's (refused at create).

**Customer credit transfer** (`credit_transfers.py`) - the source's credit lots in
the transfer's branch are drawn oldest first (`CustomerCreditTransferDraw`), and the
destination gets a receipt with method `CREDIT_TRANSFER` whose deposit account is
the customer-credit liability. That receipt's one journal is the transfer's GL
effect, with no bank line:
```
Dr  customer credit (2140)   amount          (the source's credit)
Cr  receivable (AR control)  applied         (the destination's open bills)
Cr  customer credit (2140)   excess          (the destination's new credit)
```
The receipt settles the destination's open bills at once when
`auto_apply_customer_credit` is on. It is not takings: collections and receipt
totals leave it out (`collected.received_money_q`). It is voided only through its
transfer, and a source receipt or credit note that funded a posted transfer is
voided only after the transfer.
**Applying stored credit** (`allocate_credit_note`, `credit_notes.py:250`) - no cash:
`Dr customer credit (2140) · Cr AR`. All paths run `post_journal` (the
`finance_journals_posting` guards) and write a durable rejection audit on failure.

> **Cost centres survive credit/debit-note posting** - `_post_credit_note_atomic`
> groups revenue by `(account, cost_center)` (`credit_notes.py:125`), so a line's
> `cost_center` reaches the GL on both the CREDIT (Dr revenue/returns) and DEBIT
> (Cr revenue) lines. Tax stays aggregated by account. See `finance_cost_centers` §6.

## 7. Worked example

**Overpayment → refund.** Customer has ₦5,000 unapplied credit (sitting in `2140`
from an earlier overpayment). `POST /v1/finance/refunds/` `{customer, refund_date,
amount: 500000, bank_account}` → draft; `POST /refunds/<id>/post/` →
`Dr 2140 500000 / Cr <bank> 500000`. Trying to refund ₦6,000 → `400`
("exceeds … available credit").

**Concession on an invoice.** Invoice balance ₦20,000; grant a ₦5,000 scholarship:
`POST /concessions/` `{customer, invoice, kind:"SCHOLARSHIP", amount:500000,
concession_date}` → draft; `post/` → `Dr 4910 500000 / Cr 1200 500000`, invoice
`amount_credited += 500000`, `balance_due` now ₦15,000, `payment_status` → PARTIAL.

## 8. Gotchas / known limitations

- ✅ **Refund list paginates** (covered by the ops pagination sweep) - this gotcha was
  stale; all four adjustment lists now use the standard `{pagination, data}` envelope.
- **A refund needs existing credit** - you can't refund a customer who only has open
  invoices; settle/credit first so `2140` holds the balance.
- **A credit transfer needs its route staffed.** Books arrive with the transfer
  route empty; submission is refused until the tenant adds approvers (or runs
  `seed_finance_approvals`).
- **DEBIT note `allocate/` → 400** ("a debit note increases the receivable"). This
  only blocks the credit-note *allocate* verb (which reduces another invoice). A DEBIT
  note is instead **settled by a receipt** - `post_payment`/`allocate_payment` pick it
  up as an open AR item, or target it explicitly with
  `allocations:[{debit_note, amount}]`.
- ✅ **Fixed 2026-07-05: receipts now settle DEBIT notes.** Previously a receipt could
  only allocate to invoices, so a DEBIT note with no invoice sat unsettled forever and
  the whole receipt fell to `2140` - the customer's credit balance was overstated by
  the note amount and the note was unpayable. DEBIT notes are now first-class AR open
  items across allocation, the customer ledger, statement, and refund cap.

## 9. Permissions & tenant isolation

- Verbs split per action: `finance.creditnote.{view,create,post,allocate}`,
  `finance.refund.{view,create,post}`, `finance.invoice.writeoff`,
  `finance.concession.{view,create,post}`,
  `finance.credittransfer.{view,create,submit,reverse}` (no post key: approval is
  the only route). The combined `ar-adjustments/` reuses `finance.refund.view`.
- Every action resolves the entity then `filter(entity=…, pk=…)` (e.g.
  `_note`/`_refund`/`_concession` bases), and `_resolve_customer`/`_resolve_invoice`
  are entity-scoped → another tenant's note/invoice/customer id → 404. ✅
- Serializers expose ids/codes/money/dates/reason only - no secrets.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/adjustments.py` | `CreditNote`(+`Line`,`Allocation`), `DebitNoteAllocation`, `Refund`, `Concession` |
| `credit_notes.py` | price/post/allocate credit notes, `post_refund`, `write_off_invoice` |
| `receivables.py` | receipt allocation over invoices **+ DEBIT notes** (`_build_invoice_plan`, `_apply_payment_subledger`), `customer_credit_balance` |
| `installments.py` | `post_concession` |
| `credit_transfers.py` | `check_transfer`, `post_customer_credit_transfer`, `void_customer_credit_transfer` |
| `approvals.py` | `cumulative_adjustment_amount`, `require_second_person`, the seeded routes |
| `views_ar.py` | credit-note / refund / write-off / ar-adjustments / concession views |
| `serializers.py` | `CreditNoteSerializer`, `RefundSerializer`, `ConcessionSerializer` |
| `constants.py` | `CreditNoteKind`, `ConcessionKind`, `CUSTOMER_CREDIT_CODE` (2140), `BAD_DEBT_EXPENSE_CODE` (5300), `DISCOUNTS_ALLOWED_CODE` (4910) |

## 11. Test coverage & gaps

Existing (`tests.py`): `CreditNoteTests` (CREDIT note reverses AR + applies to
invoice), `ConcessionTests` (discount reduces invoice, posts to allowances).

`tests_ar_guards.py`: settlement targets, credit transfers (posting, voiding, the
approval route, the missing post route), credit notes naming a bill, cumulative
approval and the second person.

Worth asserting if not already:
- **403** per verb; **cross-tenant** note/refund/concession id → 404.
- Refund **capped** at customer credit (over-refund → 400) and books `Dr 2140 / Cr bank`.
- DEBIT note increases AR and **cannot** be allocated via `allocate/` (→ 400), but a
  receipt **settles** it - `test_receipt_settles_standalone_debit_note` (the reported
  bug: DN 20k + receipt 40k → DN PAID, 20k customer credit),
  `test_explicit_receipt_allocation_to_debit_note`, `test_stored_credit_settles_debit_note`,
  `test_receipt_allocates_across_invoice_and_debit_note_oldest_first`.
- Write-off `Dr 5300 / Cr AR`, bumps `amount_credited`, full-balance default + the
  "exceeds balance" guard; appears in `ar-adjustments/`.
- Concession exceeding `balance_due` → 400; refreshes the invoice's payment plan.
- Empty-list shape on a fresh entity.
- A credit-note line's cost centre reaches the GL
  (`CreditNoteTests.test_credit_note_revenue_line_carries_cost_centre_to_gl`).
