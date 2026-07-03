# finance_tax_remittance

Statutory tax **remittance**: a `TaxObligation` maps a tax type (VAT / WHT / PAYE /
pension) to the GL **liability control account** that accumulates it; a `TaxFiling`
is one return for one period — **prepared** from the GL, **filed** (frozen, with VAT
netting / penalties), then **paid** (`Dr liability, Cr bank`). The liabilities were
parked by the source flows (sales VAT, payroll PAYE/pension, WHT); this slice is how
they leave the books.

Routes (mounted at `/v1/finance/`): `tax-obligations/…`, `tax-obligations/outstanding/`,
`tax-filings/…`, `tax-filings/{summary}/`, `tax-filings/<pk>/{file,pay}/`.

---

## 1. What it is (and what it is NOT)

- **`TaxObligation`** (`models/ops.py:531`): master data — tax type → liability
  account (+ optional `recoverable_account`, input VAT, netted at filing), authority,
  frequency, `filing_day`. Configurable data, not hard-coded (`unique(entity, code)`).
- **`TaxFiling`** (`models/ops.py:588`): one return, `DRAFT → FILED → PAID`.
  `amount_due = gross_liability − recoverable_amount + adjustment`.

**This does NOT:**
- **Accrue tax.** The liability already sits in the control account from source
  postings (perpetual). `prepare` only *reads* the GL movement; the only journals here
  are the VAT-netting/penalty entry at filing and the remittance at pay.
- **Auto-compute `due_date`.** The obligation's `filing_day` is informational master
  data (exposed/edited, seeded) — `prepare` takes `due_date` from the caller.
- **Un-file a return that's been paid.** `unfile/` reverts a FILED return to DRAFT
  (reversing its netting/penalty journal) — but only while **no** remittance has been
  recorded; a paid return needs its payment reversed first (§4).

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `TaxObligation` | `models/ops.py:531` | `code`, `obligation_type`, `liability_account`, `recoverable_account?` (VAT input, usually 1300), `authority_name`, `frequency`, `filing_day`, `is_active` |
| `TaxFiling` | `:588` | `obligation`, `period_start/end`, `due_date?`, `filing_status`, `gross_liability`, `recoverable_amount`, `adjustment_amount`(+`adjustment_account`), `amount_due`, `amount_paid`, `payment_status`, `filing_reference`, `filed_at`, `filing_journal` |

Money is kobo. `balance_due = amount_due − amount_paid`; `payment_status` reuses
`InvoicePaymentStatus` (UNPAID/PARTIAL/PAID), derived.

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET/POST /tax-obligations/` | `finance.tax.view` / `.manage` | Obligation list / create | `code`, `name`, `obligation_type`, `liability_account`, `recoverable_account?`, `authority_name?`, `frequency?`, `filing_day?` | obligation |
| `GET/PATCH /tax-obligations/<pk>/` | `finance.tax.view` / `.manage` | One obligation / edit | — | obligation |
| `GET /tax-obligations/outstanding/` | `finance.tax.view` | Per-obligation **unremitted balance** now sitting in each control account (all-time GL net, less recoverable) | — | rows |
| `GET/POST /tax-filings/` | `finance.tax.view` / `.file` | Filings list (paginated) / **prepare** a draft from the GL | `obligation`, `period_start`, `period_end`, `due_date?` | filing |
| `GET /tax-filings/summary/` | `finance.tax.view` | KPIs over all filings | — | summary |
| `GET /tax-filings/<pk>/` | `finance.tax.view` | One filing | — | detail |
| `POST /tax-filings/<pk>/file/` | `finance.tax.file` | **File**: freeze, net input VAT, book penalty | `filed_date`, `filing_reference?`, `adjustment_amount?`, `adjustment_account?` | filing |
| `POST /tax-filings/<pk>/unfile/` | `finance.tax.file` | **Un-file**: FILED → DRAFT, reversing the netting journal; refused once any payment made | — | filing |
| `POST /tax-filings/<pk>/pay/` | `finance.tax.pay` | **Remit** (full/partial) | `bank_account`, `pay_date`, `amount?` | filing |

## 4. Lifecycle / state machine

```
DRAFT ──file──▶ FILED ──pay (×N, partial ok)──▶ PAID
  ▲ ◀──unfile── ┘  (only while amount_paid == 0; netting journal reversed)
  (re-prepare refreshes the same draft; overlapping windows are rejected)
```
- **Prepare** (`prepare_filing`, `tax_filing.py:72`): derives the amount owed from GL
  movement; re-running for the same `(obligation, period_start, period_end)` updates
  the existing DRAFT. A **new** draft whose window overlaps any other filing for the
  obligation is **rejected** (names the clashing return) — which also blocks
  re-drafting an already-FILED period.
- **File** (`file_filing`): DRAFT-only; refuses a zero amount-due; freezes figures,
  posts the netting/penalty journal (only if needed).
- **Unfile** (`unfile_filing`): FILED-only and only while `amount_paid == 0`; reverses
  the netting/penalty journal, clears `filed_at`/`filing_reference`, back to DRAFT
  (audited as `TAX_FILING_UNFILED`).
- **Pay** (`pay_filing`): FILED (or PAID with balance) only; capped at `balance_due`;
  flips to PAID at full remittance.

## 5. Calculations

**Prepare** — GL movement over the period (`_account_movement`, `tax_filing.py:46`,
POSTED lines only, bounded by `entry__date`):
```
gross       = max(credit − debit, 0)  on the liability account   (credit-normal payable)
recoverable = min(max(debit − credit, 0), gross)  on the recoverable account (never nets below 0)
amount_due  = gross − recoverable + adjustment
```
**Outstanding** (`outstanding_obligations`, `:340`): same movement, **all-time**, per
active obligation — "what would be owed if a return were filed for everything to date."

## 6. What posting does to the ledger

**Prepare posts nothing** (a draft worksheet).

**File** (`_file_filing_atomic`, `tax_filing.py:175`) — a journal **only if** there is
input VAT to net or a penalty, `source=CLOSING`:
```
Dr  liability account        recoverable   ← net input VAT off the output payable
Cr  recoverable account      recoverable   ← clear input VAT (1300)
Dr  adjustment (expense)     penalty       ← late-filing penalty / interest
Cr  liability account        penalty       ← penalty increases the payable
```
After filing, the liability account holds exactly `amount_due`.

**Pay** (`_pay_filing_atomic`, `:283`):
```
Dr  liability account   pay
Cr  bank (GL cash)      pay
```
All via `post_journal` (the `finance_journals_posting` guards); durable rejection
audits (`TAX_FILING_REJECTED`) on failure.

## 7. Worked example

VAT for June: output VAT (2200) accrued ₦75,000 Cr, input VAT (1300) ₦20,000 Dr.
`POST /tax-filings/ {obligation: VAT, period_start: 2026-06-01, period_end: 2026-06-30}`
→ DRAFT: gross 75,000, recoverable 20,000, due ₦55,000. `file/ {filed_date,
filing_reference: "FIRS-123"}` → nets `Dr 2200 20,000 / Cr 1300 20,000`; 2200 now
holds ₦55,000. `pay/ {bank_account, pay_date}` → `Dr 2200 55,000 / Cr bank 55,000`,
filing → PAID.

## 8. Gotchas / known limitations

- ✅ **Un-file exists** (`unfile/`) — FILED → DRAFT with the netting journal reversed;
  refused once any remittance is recorded (reverse the payment first). PAID remains
  final by design.
- ✅ **Overlapping periods are rejected** at prepare time (any other filing for the
  obligation whose window straddles the new one, exact-draft refresh excluded) — the
  double-remittance hole is closed.
- **`filing_day` is informational** — it doesn't compute `due_date`; the caller
  passes one.
- **`outstanding/` scans all-time GL movement** per obligation (two aggregates each)
  — fine at this scale.
- `pay/` accepts a filing already PAID *only* to reject on `balance_due ≤ 0`; partials
  keep `filing_status=FILED` until fully remitted.

## 9. Permissions & tenant isolation

- Verbs: `finance.tax.{view, manage (obligations), file, pay}` — filing and paying
  are separate, `pay` is CRITICAL in the seed.
- Entity-scoped resolution everywhere (`filter(entity=…, pk=…)`); `pay_filing`
  rejects a bank account from another entity. ✅

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `TaxObligation`, `TaxFiling` |
| `tax_filing.py` | `_account_movement`, `prepare_filing` (+ overlap guard), `file_filing`, `unfile_filing`, `pay_filing`, `outstanding_obligations` |
| `views_ops/tax.py` | obligation CRUD, outstanding, filing list/prepare/summary/detail/file/pay |
| `seed.py` | seeded default obligations (VAT/WHT/PAYE/pension → 2200/2300/2310/2320) |
| `constants.py` | `TaxObligationType`, `TaxFilingFrequency`, `TaxFilingStatus` |

## 11. Test coverage & gaps

Existing (`TaxFilingTests`): prepare derives from GL (incl. VAT netting), file posts
the netting journal, pay remits and flips status.

Worth asserting: 403 per verb; cross-tenant ids → 404; re-prepare refreshes (no
duplicate drafts); penalty requires an expense account; partial-then-full remittance;
overlapping-period double-count (documented risk); empty lists.
