# finance_ar_adjustments

The non-invoice ways a receivable changes after an invoice is posted: **credit /
debit notes**, **customer refunds**, **bad-debt write-offs**, and **concessions**
(`DISCOUNT` / `WAIVER` / `SCHOLARSHIP` — the last being the domain-neutral name a
school tenant reads as a bursary). Each either gives value back to a customer,
charges them more, or recognises that a balance won't be collected — without
editing the original invoice (which is immutable once posted).

Routes (mounted at `/v1/finance/`): `credit-notes/…`, `refunds/…`,
`invoices/<pk>/write-off/`, `ar-adjustments/`, `concessions/…`.

> **Adjacent:** the AR core (customers, invoices, receipts) is `finance_invoicing_ar`;
> installment plans are `finance_payment_plans`. Concessions live in the same model
> file as payment plans but belong here conceptually.

---

## 1. What it is (and what it is NOT)

- **`CreditNote`** (`models/adjustments.py:33`): `kind=CREDIT` reduces AR (gives
  value back); `kind=DEBIT` increases AR (a supplementary charge). Doc-number
  token tracks the kind — `CRN` vs `DRN` (`save()`, `models/adjustments.py:104`).
- **`Refund`** (`models/adjustments.py:182`): pays **cash** back out of a
  customer's **credit balance** (the `2140` liability) — *not* off an invoice.
- **`Concession`** (`models/adjustments.py:228`): a non-cash reduction of a
  *specific* invoice's balance; `kind` ∈ {DISCOUNT, WAIVER, SCHOLARSHIP}.
- **Write-off**: an *action* on an invoice (`write_off_invoice`), recognising bad
  debt. **It has no model** — only a journal + an audit-log row (§6).

**This does NOT:**
- **Refund against an invoice.** A refund draws down customer credit (`2140`);
  there must be credit available, and it's **capped** at it (§5). To reverse an
  invoice's revenue, use a CREDIT note.
- **Edit posted invoices.** All four mechanisms post *new* journals; the invoice's
  `amount_credited` / `amount_paid` move, never its lines.
- **Carry cost centres to the GL on these postings.** Credit/debit notes
  aggregate revenue **by account only** (cost centre dropped, §6) — unlike the
  invoice/expense/payroll/petty-cash path. Concessions/refunds/write-offs are
  single-account and carry none by design.

## 2. Domain model

| Model | File | Key fields | Notes |
|---|---|---|---|
| `CreditNote` | `models/adjustments.py:33` | `customer`, `kind` (CREDIT/DEBIT), `note_date`, `invoice?`, `subtotal`/`tax_total`/`total`, `allocated_amount`, `journal` | `unallocated_amount = total − allocated` (CREDIT only) |
| `CreditNoteLine` | `:113` | `revenue_account`, `quantity`, `unit_price`, `tax_code`, `net_amount`, `tax_amount`, `cost_center` | priced like an invoice line |
| `CreditNoteAllocation` | `:150` | `note`, `invoice`, `amount` | bumps `Invoice.amount_credited`; `unique(note, invoice)` |
| `Refund` | `:182` | `customer`, `refund_date`, `method`, `amount`, `bank_account?`/`deposit_account?`, `journal` | pays out credit |
| `Concession` | `:228` | `customer`, `invoice`, `kind`, `amount`, `allowance_account?`, `journal` | single amount, no lines |

- Money is kobo. `CreditNote`/`Refund`/`Concession` all extend `FinanceDocument`
  (entity scope, numbered, `status`, `created_by`).
- **No `WriteOff` model** — write-offs exist only as a journal and a
  `FinanceAuditLog` row (`action=INVOICE_WRITTEN_OFF`); the AR-adjustments list
  reconstructs them from that log (`_writeoff_rows`, `views_ar.py:1058`).

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /credit-notes/` | `finance.creditnote.view` | List (paginated). Query: `kind`, `customer`, `search`, `status` (draft/issued/applied) | — | paginated `CreditNoteSerializer` |
| `POST /credit-notes/` | `finance.creditnote.create` | Create a **draft** (priced) note | `customer`, `kind?`, `note_date`, `invoice?`, `reason?`, `lines:[{revenue_account, quantity?, unit_price, tax_code?, cost_center?}]` | `201` `CreditNoteSerializer` |
| `GET /credit-notes/<pk>/` | `finance.creditnote.view` | One note | — | detail |
| `POST /credit-notes/<pk>/post/` | `finance.creditnote.post` | Post it; CREDIT notes may auto/explicitly allocate | `allocations:[{invoice, amount}]?`, `auto_allocate?` | `CreditNoteSerializer` |
| `POST /credit-notes/<pk>/allocate/` | `finance.creditnote.allocate` | Apply a posted CREDIT note's stored credit | `allocations:[{invoice, amount}]?` | `CreditNoteSerializer` |
| `GET /refunds/` | `finance.refund.view` | List (un-paginated, `[:200]`). Query: `status`, `customer` | — | list of `RefundSerializer` |
| `POST /refunds/` | `finance.refund.create` | Create a **draft** refund | `customer`, `refund_date`, `amount`, `method?`, `bank_account?` | `201` `RefundSerializer` |
| `GET /refunds/<pk>/` | `finance.refund.view` | One refund | — | detail |
| `POST /refunds/<pk>/post/` | `finance.refund.post` | Pay it out (capped at customer credit) | — | `RefundSerializer` |
| `POST /invoices/<pk>/write-off/` | `finance.invoice.writeoff` | Write off bad debt | `amount?` (default full balance), `write_off_account?`, `write_off_date?`, `narration?` | `InvoiceSerializer` |
| `GET /ar-adjustments/` | `finance.refund.view` | Unified refunds + write-offs + KPIs (paginated) | — | `{rows, kpis, pagination}` |
| `GET /concessions/` | `finance.concession.view` | List (paginated). Query: `kind`, `customer`, `search` | — | paginated `ConcessionSerializer` |
| `POST /concessions/` | `finance.concession.create` | Create a **draft** concession | `customer`, `invoice`, `kind?`, `concession_date`, `amount`, `allowance_account?`, `reason?` | `201` `ConcessionSerializer` |
| `GET /concessions/summary/` | `finance.concession.view` | KPI totals | — | `success_response` |
| `GET /concessions/<pk>/` | `finance.concession.view` | One concession | — | detail |
| `POST /concessions/<pk>/post/` | `finance.concession.post` | Post it (reduces the invoice) | — | `ConcessionSerializer` |

## 4. Lifecycle / state machine

- **Credit/debit note:** `DRAFT` (priced) → `POSTED` (`post_credit_note`). A
  POSTED **CREDIT** note can then `allocate/` its stored credit; a **DEBIT** note
  cannot allocate (it raised AR, not credit).
- **Refund / concession:** `DRAFT` → `POSTED` (`post_refund` / `post_concession`).
- **Write-off:** no draft — a single posted action on a POSTED invoice.

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
already paid (`receivables.py`).

## 6. What posting does to the ledger

**CREDIT note** (`_post_credit_note_atomic`, `credit_notes.py:93`) — give value back;
split-at-source so AR never goes credit:
```
Dr  revenue / returns (per account)   Σ net
Dr  output-tax reversal (per account) Σ tax
Cr  receivable (AR control)           applied        (settles invoices)
Cr  customer credit (2140)            excess          (unapplied remainder)
```
**DEBIT note** — supplementary charge:
```
Dr  receivable (AR control)   total
Cr  revenue (per account)     Σ net
Cr  output tax (per account)  Σ tax
```
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
**Concession** (`_post_concession_atomic`, `installments.py:266`):
```
Dr  discounts & allowances (4910)  amount
Cr  receivable (AR control)        amount  + invoice.amount_credited += amount
```
**Applying stored credit** (`allocate_credit_note`, `credit_notes.py:250`) — no cash:
`Dr customer credit (2140) · Cr AR`. All paths run `post_journal` (the
`finance_journals_posting` guards) and write a durable rejection audit on failure.

> **Cost centres are dropped on credit/debit notes** — `_post_credit_note_atomic`
> groups revenue `by_account` only (`credit_notes.py:128`), so a line's
> `cost_center` does not reach the GL. This flow was **not** in the cost-centre
> propagation fix (which covered invoice/expense/payroll/petty-cash). See
> `finance_cost_centers` §6.

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

- 🔴 **Stale model docstring:** `Refund` (`models/adjustments.py:182`) says posting
  is "Dr AR control, Cr bank" — the code actually does **Dr customer-credit (2140),
  Cr bank** (`credit_notes.py:327`). The 2140 version is correct (a refund returns
  *credit*, not an open receivable). Docstring should be corrected.
- **Refund list is un-paginated** (`[:200]`, `views_ar.py`) — unlike credit notes /
  concessions / ar-adjustments which paginate. Inconsistent; large entities truncate.
- **Cost centres dropped on credit/debit notes** (§6) — a known gap vs the fixed
  sub-ledger postings.
- **A refund needs existing credit** — you can't refund a customer who only has open
  invoices; settle/credit first so `2140` holds the balance.
- **Write-offs have no document** to list/detail — they're audit-log entries only;
  the only "list" is `ar-adjustments/`.
- **DEBIT note `allocate/` → 400** ("a debit note increases the receivable").

## 9. Permissions & tenant isolation

- Verbs split per action: `finance.creditnote.{view,create,post,allocate}`,
  `finance.refund.{view,create,post}`, `finance.invoice.writeoff`,
  `finance.concession.{view,create,post}`. The combined `ar-adjustments/` reuses
  `finance.refund.view`.
- Every action resolves the entity then `filter(entity=…, pk=…)` (e.g.
  `_note`/`_refund`/`_concession` bases), and `_resolve_customer`/`_resolve_invoice`
  are entity-scoped → another tenant's note/invoice/customer id → 404. ✅
- Serializers expose ids/codes/money/dates/reason only — no secrets.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/adjustments.py` | `CreditNote`(+`Line`,`Allocation`), `Refund`, `Concession` |
| `credit_notes.py` | price/post/allocate credit notes, `post_refund`, `write_off_invoice` |
| `installments.py` | `post_concession` |
| `views_ar.py` | credit-note / refund / write-off / ar-adjustments / concession views |
| `serializers.py` | `CreditNoteSerializer`, `RefundSerializer`, `ConcessionSerializer` |
| `constants.py` | `CreditNoteKind`, `ConcessionKind`, `CUSTOMER_CREDIT_CODE` (2140), `BAD_DEBT_EXPENSE_CODE` (5300), `DISCOUNTS_ALLOWED_CODE` (4910) |

## 11. Test coverage & gaps

Existing (`tests.py`): `CreditNoteTests` (CREDIT note reverses AR + applies to
invoice), `ConcessionTests` (discount reduces invoice, posts to allowances).

Worth asserting if not already:
- **403** per verb; **cross-tenant** note/refund/concession id → 404.
- Refund **capped** at customer credit (over-refund → 400) and books `Dr 2140 / Cr bank`.
- DEBIT note increases AR and **cannot** be allocated (→ 400).
- Write-off `Dr 5300 / Cr AR`, bumps `amount_credited`, full-balance default + the
  "exceeds balance" guard; appears in `ar-adjustments/`.
- Concession exceeding `balance_due` → 400; refreshes the invoice's payment plan.
- Empty-list shape on a fresh entity.
- (Regression) credit-note GL line carries **no** cost centre until that gap is fixed.
