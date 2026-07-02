# finance_expense_claims

Staff **expense claims** — an employee acts as a one-off "vendor" to be reimbursed.
Posting recognises the cost and a liability owed to them; settling pays them from a
bank account. A mini accounts-payable cycle that never involves the procurement
vendor tables.

Routes (mounted at `/v1/finance/`): `expense-claims/`, `expense-claims/summary/`,
`expense-claims/<pk>/`, `expense-claims/<pk>/{post,reject,settle}/`,
`expense-claims/<pk>/lines/<line_id>/receipt/`.

---

## 1. What it is (and what it is NOT)

- An **`ExpenseClaim`** (`models/ops.py:251`) is a `FinanceDocument` with expense
  **lines** (`ExpenseClaimLine`, `:329`). The claimant is either a platform user
  (`claimant` FK) or free text (`claimant_name`).
- **Posting** (`post_expense_claim`) raises `Dr expense(s) (+ Dr input VAT), Cr
  accrued reimbursement (2400)` — the liability owed to the employee. **Settling**
  (`settle_expense_claim`) pays it: `Dr accrued reimbursement, Cr bank`.
- Two status axes, like an invoice: document `status` (`DRAFT/POSTED/CANCELLED`) and
  `payment_status` (`UNPAID/PARTIAL/PAID`) for how much has been reimbursed.

**This does NOT:**
- **Touch the procurement vendor ledger.** The claimant is not a `Vendor`; the
  credit goes to the shared accrued-reimbursement liability (`2400`), not AP.
- **Reject a posted claim.** `reject/` only cancels a **DRAFT**; a *posted* claim is
  undone with **`void/`** (reverses its journal → CANCELLED), and only while it hasn't
  been reimbursed yet (§4).
- **Validate the claimant is a real employee.** `claimant` is optional; a free-text
  `claimant_name` is accepted as-is.

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `ExpenseClaim` | `models/ops.py:251` | `claimant?`/`claimant_name`, `claim_date`, `title`, `reimbursement_account` (default `2400`), `subtotal`/`tax_total`/`total`, `amount_paid`, `status`, `payment_status`, `journal` |
| `ExpenseClaimLine` | `:329` | `expense_account`, `quantity`, `unit_price`, `tax_code`, `net_amount`, `tax_amount`, `cost_center`, `receipt` (file) |

- Money is kobo. `total = subtotal + tax_total`; `balance_due = total − amount_paid`.
- `payment_status` is **derived** from `amount_paid` vs `total`
  (`refresh_payment_status`), never set by hand.
- Line `receipt` is a DB-backed `FileField` (a supporting document per line).

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /expense-claims/` | `finance.expenseclaim.view` | List (paginated). Query: `status`, `payment_status`, `display_status` (DRAFT/APPROVED/PAID/REJECTED), `q` | — | paginated `ExpenseClaimSerializer` |
| `POST /expense-claims/` | `finance.expenseclaim.create` | Create a **DRAFT** (priced) | `claimant_name?`, `claim_date`, `title?`, `lines:[{expense_account, quantity?, unit_price, tax_code?, cost_center?}]` | `201` claim |
| `GET /expense-claims/summary/` | `finance.expenseclaim.view` | Header KPIs over **all** claims | — | `success_response` |
| `GET /expense-claims/<pk>/` | `finance.expenseclaim.view` | Claim + lines (+ receipt urls) | — | detail |
| `POST /expense-claims/<pk>/post/` | `finance.expenseclaim.post` | DRAFT → POSTED (raise the liability journal) | — | claim |
| `POST /expense-claims/<pk>/reject/` | `finance.expenseclaim.post` | DRAFT → CANCELLED (approver's call) | — | claim |
| `POST /expense-claims/<pk>/settle/` | `finance.expenseclaim.settle` | Reimburse (full or partial) | `bank_account`, `pay_date`, `amount?` | claim |
| `POST /expense-claims/<pk>/void/` | `finance.expenseclaim.post` | Void a **posted, un-reimbursed** claim (reverses its journal → CANCELLED) | — | claim |
| `POST /expense-claims/<pk>/lines/<line_id>/receipt/` | `finance.expenseclaim.create` | Attach a receipt (multipart `file`) | `file` | `201` claim |
| `DELETE …/lines/<line_id>/receipt/` | `finance.expenseclaim.create` | Remove a receipt | — | claim |

> **Field note:** create reads `unit_price`×`quantity` (there is **no** `amount`
> field on a line). `cost_center` on a line now survives posting (see
> `finance_cost_centers` §6).

## 4. Lifecycle / state machine

```
DRAFT ──post──▶ POSTED ──settle (×N, partial ok)──▶ payment_status PAID
  │               │                                   (UNPAID→PARTIAL→PAID)
  │               └──void (only if un-reimbursed)──▶ CANCELLED (posting journal REVERSED)
  └──reject──▶ CANCELLED
```
- **Create** makes a priced DRAFT. **post/** raises the liability; **reject/** cancels
  a DRAFT only. **settle/** reimburses (repeatable until `balance_due` hits 0).
- **void/** undoes a *posted* claim booked in error: it reverses the posting journal
  and marks the claim CANCELLED — but **only while `amount_paid == 0`**; once cash has
  been reimbursed you must reverse that reimbursement first.
- Approve-vs-reject is one decision by one role: both `post/` and `reject/` use
  `finance.expenseclaim.post` (§9).

## 5. Calculations

Pricing reuses the shared AR helpers (`receivables.compute_line_net`/`compute_tax`,
`ROUND_HALF_UP` to kobo) via `price_expense_claim`:
```
line.net = quantity × unit_price ;  line.tax = net × tax_code.rate_bps / 10000
claim.subtotal/tax_total/total = Σ over lines
settle.pay = min(amount or balance_due, balance_due)   # capped; partials allowed
```

## 6. What posting does to the ledger

**Post** — `_post_expense_claim_atomic` (`expenses.py:63`), atomic, DRAFT + positive total:
```
Dr  expense  (per (account, cost centre))   Σ net     ← P&L, carries the cost centre
Dr  input tax (per tax account)             Σ tax     ← recoverable VAT (1300-type)
Cr  accrued reimbursement (2400)            total     ← liability owed to the employee
```
Then `post_journal` (all the `finance_journals_posting` guards), link `journal`,
stamp POSTED, refresh payment status, audit. A `FinanceError` writes a **durable
rejection** row and re-raises.

**Settle** — `_settle_expense_claim_atomic` (`expenses.py:174`):
```
Dr  accrued reimbursement (2400)   pay
Cr  bank (the bank account's GL cash account)   pay
```
`pay` defaults to the full `balance_due`, or a smaller `amount` for a partial;
`amount_paid` bumps and `payment_status` re-derives.

> Cost centres: expense lines carry the cost centre to the GL (the propagation fix);
> the input-tax and reimbursement lines do not — they're not P&L analytics.

## 7. Worked example

`POST /v1/finance/expense-claims/?entity=LEKKI`:
```json
{ "claimant_name": "Ada N.", "claim_date": "2026-07-01", "title": "Client trip",
  "lines": [ { "expense_account": "5300", "quantity": 1, "unit_price": 150000,
               "cost_center": "ADM" } ] }
```
→ priced DRAFT (net 150000, total 150000). `post/` → `Dr 5300 150000 (cost centre ADM)
/ Cr 2400 150000`, status POSTED, payment UNPAID. `settle/` `{bank_account:"GTB-OPS",
pay_date:"2026-07-03"}` → `Dr 2400 150000 / Cr <bank GL> 150000`, payment_status PAID.
A receipt PDF attaches to the line via `lines/<id>/receipt/` (multipart `file`).

## 8. Gotchas / known limitations

- **`reject/` reuses `finance.expenseclaim.post`** — no separate reject verb; the
  approver who can post can also reject (approve-or-reject is one decision).
- **Receipt endpoints use `finance.expenseclaim.create`** — the creator/claimant
  attaches receipts, not the approver.
- ✅ **Posted claims can be voided** (`void/`) — but only while un-reimbursed; it
  reverses the posting journal and cancels the claim. Once reimbursed, the cash has
  left, so the reimbursement must be reversed first (guarded).
- **Free-text claimant** — `claimant` FK is optional; `claimant_name` is unvalidated,
  so reporting "by employee" needs the FK to be set.
- **Receipt files use capability URLs.** The media endpoint (`/media/<name>`,
  `core.views.MediaView`) authenticates the caller but can't authorise per file — so
  every stored file's name now carries a high-entropy token
  (`core.storage.DatabaseStorage.get_available_name`), making receipt URLs
  unguessable and only handed to callers already allowed to see the claim. Note this
  is capability-based, not object-level auth — a *leaked* URL is still fetchable by
  any authenticated user. (Files uploaded before this change keep their old,
  guessable names.)

## 9. Permissions & tenant isolation

- Verbs: `finance.expenseclaim.{view, create, post, settle}` — note `post` also
  gates **reject**, and `create` also gates **receipt** upload/delete.
- Every action resolves the entity then `filter(entity=…, pk=…)` (`_claim`), and the
  receipt sub-resource filters the line by `claim.lines.filter(pk=line_id)`, so a
  cross-tenant claim or line id → 404. ✅
- `bank_account`/accounts are entity-scoped resolvers → no cross-tenant settlement.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `ExpenseClaim`, `ExpenseClaimLine` |
| `expenses.py` | `price_expense_claim`, `post_expense_claim`, `settle_expense_claim`, `void_expense_claim` |
| `views_ops/expenses.py` | list/create (+ `display_status`/`q`), post, reject, settle, receipt, summary |
| `serializers.py` | `ExpenseClaimSerializer`, `ExpenseClaimLineSerializer` (receipt urls) |
| `constants.py` | `ACCRUED_REIMBURSEMENT_CODE` (2400); reuses `InvoicePaymentStatus`, `DocumentStatus` |

## 11. Test coverage & gaps

Existing (`tests.py`, `ExpenseClaimTests`): post raises the liability with input VAT;
partial-then-full settle; cannot post an empty claim; and the cost-centre propagation
regression (`CostCenterPropagationTests`).

Worth asserting if not already:
- **403** per verb (view/create/post/settle); **cross-tenant** claim/line id → 404.
- `reject/` only from DRAFT; posting a non-draft → error.
- Settle caps at `balance_due`; partial then full moves `payment_status`
  UNPAID→PARTIAL→PAID; settling a fully-paid claim → error.
- `display_status` filter maps to the right (status × payment_status) sets;
  `summary/` aggregates over all rows (accurate under pagination).
- Receipt attach/remove round-trip; empty-list shape on a fresh entity.
