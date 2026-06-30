# finance_payment_plans

Installment **payment plans** — a scheduling overlay that spreads a receivable over
dated installments so reminders, dunning and progress tracking have a schedule to
measure against. **A plan never touches the General Ledger**: the invoice already
sits in AR; the plan only records *when* and *how much* the customer is expected to
pay, and mirrors settlement that was posted elsewhere.

Routes (mounted at `/v1/finance/`): `payment-plans/`, `payment-plans/<pk>/`,
`payment-plans/<pk>/{activate,refresh,cancel}/`.

> **Adjacent:** receipts that actually settle invoices are `finance_invoicing_ar`;
> reminders are `finance_dunning`; concessions live in `finance_ar_adjustments`
> (posting a concession also refreshes a linked plan — §4).

---

## 1. What it is (and what it is NOT)

- A **`PaymentPlan`** (`models/adjustments.py:276`) spreads `total_amount` over
  `installment_count` dated **`PaymentPlanInstallment`** rows
  (`models/adjustments.py:333`). It usually references an `invoice` (optional — a
  standalone plan is allowed).
- It is a **pure overlay**: `plan_status` walks `DRAFT → ACTIVE → COMPLETED /
  CANCELLED`; settlement is *reflected*, never *caused*, by the plan.

**This does NOT:**
- **Post to the GL.** No journal, ever (`models/adjustments.py:277` docstring;
  `refresh_plan_progress` "No GL effect"). Don't look for a `journal` FK — there
  isn't one.
- **Move money or settle invoices.** Paying an installment is just paying the
  underlying invoice via a receipt (`finance_invoicing_ar`); the plan only
  *displays* that progress.
- **Cause settlement.** But it *does* now **track** it automatically: any change to
  the linked invoice's settled amount — a receipt, credit-note allocation or
  write-off — pushes a refresh of the plan (§4/§8). The manual `refresh/` endpoint
  remains for standalone plans and explicit overrides.

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `PaymentPlan` | `models/adjustments.py:276` | `customer`, `invoice?`, `plan_status`, `start_date`, `frequency`, `installment_count`, `total_amount`, `notes` |
| `PaymentPlanInstallment` | `:333` | `seq_no` (1-based), `due_date`, `amount`, `amount_settled`, `status` |

- Money is kobo. Derived reads on the plan: `scheduled_total = Σ installment
  amount`, `settled_total = Σ amount_settled`, `outstanding_total = total −
  settled` (`models/adjustments.py:318`). On an installment: `balance = amount −
  amount_settled`, plus `is_overdue(as_of)`.
- Enums (`constants.py:193`): **frequency** `WEEKLY / FORTNIGHTLY / MONTHLY /
  QUARTERLY`; **plan_status** `DRAFT / ACTIVE / COMPLETED / CANCELLED`;
  **installment status** `PENDING / PARTIAL / PAID` (derived from settled vs due).
- `unique(plan, seq_no)` on installments.

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /payment-plans/` | `finance.paymentplan.view` | List (paginated). Query: `status`, `customer`, `search` | — | paginated `PaymentPlanSerializer` |
| `POST /payment-plans/` | `finance.paymentplan.create` | Create a **DRAFT** + build the schedule | `customer`, `invoice?`, `total_amount?` (defaults to invoice `balance_due`), `installment_count`, `start_date`, `frequency?`, `amounts?`, `notes?` | `201` `PaymentPlanSerializer` |
| `GET /payment-plans/<pk>/` | `finance.paymentplan.view` | Plan + its installments | — | detail |
| `POST /payment-plans/<pk>/activate/` | `finance.paymentplan.activate` | DRAFT → ACTIVE (validates schedule, syncs settlement) | — | `PaymentPlanSerializer` |
| `POST /payment-plans/<pk>/refresh/` | `finance.paymentplan.activate` | Re-distribute settlement across installments | `settled_amount?` (override) | `PaymentPlanSerializer` |
| `POST /payment-plans/<pk>/cancel/` | `finance.paymentplan.cancel` | → CANCELLED (idempotent on terminal) | — | `PaymentPlanSerializer` |

> **Field/authz notes:** `refresh/` reuses the **`finance.paymentplan.activate`**
> key (no dedicated `refresh` verb). `total_amount` defaults to the invoice's
> `balance_due` when omitted and an `invoice` is given. `amounts` (optional) is an
> explicit per-installment kobo list — must match `installment_count` and sum to
> `total_amount`.

## 4. Lifecycle / state machine

```
DRAFT ──activate──▶ ACTIVE ──(settlement reaches total)──▶ COMPLETED
  │                   │
  └── build schedule  └── refresh / cancel
        (DRAFT only)
any non-terminal ──cancel──▶ CANCELLED
```
- **Create** (`POST /payment-plans/`) makes a DRAFT and immediately
  `build_installments` (`installments.py:98`) — which **replaces** any existing
  rows and only works on a DRAFT.
- **Activate** (`installments.py:156`): requires installments exist and
  `scheduled_total == total_amount`, else rejects; then runs `refresh_plan_progress`.
- **Refresh** distributes settlement (below). **Cancel** is idempotent on
  COMPLETED/CANCELLED.
- **Settlement auto-syncs the plan.** Every sub-ledger path that moves the linked
  invoice's settled amount calls `refresh_plans_for_invoice` (`installments.py:244`):
  a cash receipt (`receivables.py:_apply_payment_subledger`), credit-note allocation
  (`credit_notes.py:_apply_creditnote_subledger`), and a write-off
  (`credit_notes.py`). A concession posting refreshes directly
  (`installments.py:342`). So an ACTIVE plan advances on its own; `refresh/` is only
  needed for standalone plans or explicit overrides.

## 5. Calculations

**Even split** — `split_amount` (`installments.py:81`), integer-exact kobo:
```
base = round_half_up(total / count)
each of the first count−1 installments = base
last installment = total − base × (count−1)      # absorbs the remainder
```
Example: ₦100,000 over 3 → `[33333, 33333, 33334]` (sums back to 100000).

**Due dates** — `_due_date` (`installments.py:70`): installment 0 is on
`start_date`; thereafter `WEEKLY=+7d`, `FORTNIGHTLY=+14d` per index, or
`MONTHLY=+1mo`, `QUARTERLY=+3mo` via `_add_months`, which **clamps the day** to the
target month's last day (31 → 30/28).

**Progress distribution** — `refresh_plan_progress` (`installments.py:199`),
oldest-first, no GL:
```
remaining = settled_amount (override) or invoice.settled_amount (cash + credits)
for each installment in seq order:
    applied = min(installment.amount, remaining)
    status  = PAID (applied==amount) | PARTIAL (>0) | PENDING (0)
    remaining -= applied
if settled_total >= total_amount: plan → COMPLETED
```
A standalone plan (no invoice) with no `settled_amount` override is left as-is.

## 6. What posting does to the ledger

**Nothing — by design.** Payment plans never raise a journal; `build_installments`,
`activate_payment_plan`, `refresh_plan_progress` and `cancel_payment_plan` only
write the plan/installment rows and audit events. The money they track was (or
will be) posted by the invoice and its receipts (`finance_invoicing_ar`). The
plan's job is purely *when/how-much* reporting.

(For contrast, the one thing in this code file that *does* post is `post_concession`
— documented in `finance_ar_adjustments`, not here.)

## 7. Worked example

`POST /v1/finance/payment-plans/?entity=LEKKI`:
```json
{ "customer": "CUST-0001", "invoice": 312, "installment_count": 3,
  "start_date": "2026-07-01", "frequency": "MONTHLY" }
```
→ DRAFT plan, `total_amount` defaulted to the invoice's ₦100,000 balance, schedule
built: `#1 2026-07-01 ₦33,333`, `#2 2026-08-01 ₦33,333`, `#3 2026-09-01 ₦33,334`.

`activate/` → ACTIVE, then progress synced from the invoice's settled amount (₦0 so
far). Customer later pays ₦40,000 against the invoice (a receipt) → **call
`refresh/`** → `#1` PAID, `#2` PARTIAL (₦6,667), `#3` PENDING. Once the invoice is
fully settled, a final `refresh/` flips the plan to COMPLETED.

## 8. Gotchas / known limitations

- ✅ **Progress now auto-syncs on settlement** (was a gotcha): receipts, credit-note
  allocations and write-offs push `refresh_plans_for_invoice`; concession posting
  refreshes directly. `refresh/` is no longer required after a normal payment — it
  remains for standalone plans and explicit `settled_amount` overrides.
- **`refresh/` is gated on `finance.paymentplan.activate`**, not a `refresh` verb —
  worth knowing when assigning permissions.
- **Schedule edits are DRAFT-only** — `build_installments` rejects a non-DRAFT plan;
  to re-shape an ACTIVE plan you cancel and recreate.
- **`amounts` must sum exactly** to `total_amount` and match `installment_count`
  (all positive), else `400`.
- **Standalone plans don't self-progress** — with no invoice you must pass
  `settled_amount` to `refresh/`.
- No "skip/defer installment" or partial-reschedule action; it's create / activate
  / refresh / cancel only.

## 9. Permissions & tenant isolation

- Verbs: `finance.paymentplan.{view, create, activate, cancel}` (activate also
  covers refresh).
- Every action resolves the entity then `filter(entity=…, pk=…)` (`_plan`,
  `views_ar.py:1540`); `_resolve_customer`/`_resolve_invoice` are entity-scoped →
  another tenant's plan/customer/invoice id → 404. ✅
- `PaymentPlanSerializer` exposes ids/codes/money/dates/status only — no secrets.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/adjustments.py` | `PaymentPlan`, `PaymentPlanInstallment` (+ derived totals) |
| `installments.py` | `split_amount`, `_due_date`/`_add_months`, `build_installments`, `activate_payment_plan`, `refresh_plan_progress`, `cancel_payment_plan` |
| `views_ar.py` | `PaymentPlanListCreateView` + activate/refresh/cancel views |
| `serializers.py` | `PaymentPlanSerializer`, `PaymentPlanInstallmentSerializer` |
| `constants.py` | `PaymentPlanFrequency`, `PaymentPlanStatus`, `InstallmentStatus` |

## 11. Test coverage & gaps

Existing (`tests.py`, `PaymentPlanTests`): schedule build/split and
`refresh_plan_progress` distribution.

Worth asserting if not already:
- **403** per verb; **cross-tenant** plan id → 404.
- `split_amount` remainder on the last installment; `amounts` validation (sum /
  count / positivity → 400).
- Due-date spacing for each frequency, incl. month-end clamping (Jan-31 → Feb-28).
- Activate rejects when `scheduled_total != total_amount` or no installments built.
- `refresh/` after a partial invoice settlement marks installments
  PAID/PARTIAL/PENDING oldest-first and flips COMPLETED at full settlement.
- **Auto-sync** (`PaymentPlanTests.test_receipt_auto_refreshes_linked_plan`): a
  receipt advances the plan with no manual `refresh/` call.
- Empty-list shape on a fresh entity.
