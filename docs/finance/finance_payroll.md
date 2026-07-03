# finance_payroll

Batch **payroll** on the classic two-step accrual-then-disburse model: **post** a run
to recognise the cost and park each liability (`Dr salary expense, Cr PAYE / pension /
net-wages payable`), then **pay** it to clear net wages (`Dr net-wages payable, Cr
bank`). Runs are typed by hand or **generated from an employee-salary roster**, whose
figures a **salary structure** can derive from gross.

Routes (mounted at `/v1/finance/`): `payroll-runs/…`, `payroll-runs/{summary,generate}/`,
`payroll-runs/<pk>/{post,pay}/`, `employee-salaries/…`, `salary-structures/…`.

---

## 1. What it is (and what it is NOT)

- A **`PayrollRun`** (`models/ops.py:678`) is a batch of **`PayrollLine`** rows (one
  per employee: `gross`, `paye`, `pension`, `net = gross − paye − pension`).
- An **`EmployeeSalary`** (`models/ops.py:865`) is the recurring roster a run is
  *generated* from; a **`SalaryStructure`** + **`SalaryComponent`** (`:794`/`:823`)
  is a reusable template that *derives* an employee's PAYE/pension/net from gross.
- Two postings: **accrual** (`post_payroll`) then **disbursement** (`pay_payroll`).

**This does NOT:**
- **Post from the roster or a structure.** `EmployeeSalary`/`SalaryStructure` never
  hit the GL — they only shape the numbers a run copies into its lines
  (`models/ops.py:800`, `:872`).
- **Show individual salaries to everyone.** Per-employee names and pay figures are
  **FLS-masked** — only holders of `finance.payrollrun.view_sensitive` see them; plain
  `view` sees the run and its **totals** but not who earns what (§9).
- **One-click undo a *paid* run.** `cancel/` voids a DRAFT or POSTED (un-paid) run
  (§4), but a **PAID** run is refused — the net wages already left, so the disbursement
  must be reversed (a real clawback) first.

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `PayrollRun` | `models/ops.py:678` | `pay_date`, `period_label`, `run_status`, the four posting accounts, `gross/paye/pension/net_total`, `journal` (accrual), `disbursement_journal` |
| `PayrollLine` | `:760` | `employee?`/`employee_name`, `gross/paye/pension/net_amount`, `components` (payslip snapshot), `cost_center` |
| `SalaryStructure` | `:794` | `name`, `is_active`; `unique(entity, name)` |
| `SalaryComponent` | `:823` | `kind` (EARNING/DEDUCTION), `calc_method`, `rate_bps`, `amount`, `is_basic`, `statutory_type` (NONE/PAYE/PENSION), `sequence` |
| `EmployeeSalary` | `:865` | `name`, `employee?`, `structure?`, `gross_amount`, flat `paye/pension_amount`, `cost_center`, `is_active` |

- Money is kobo. **`run_status`** (`PayrollRunStatus`): `DRAFT → POSTED → PAID`, plus
  `CANCELLED` (from `cancel/`). The run also carries a `DocumentStatus` (set to POSTED
  on accrual, CANCELLED on cancel).
- Default posting accounts: salary `5200`, PAYE `2310`, pension `2320`, net wages
  `2330` (overridable per run).

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /payroll-runs/` | `finance.payrollrun.view` | List runs (paginated). Query: `run_status` | — | paginated `PayrollRunSerializer` (lines FLS-masked) |
| `POST /payroll-runs/` | `finance.payrollrun.create` | Create a **DRAFT** run + lines by hand | `pay_date`, `period_label?`, `bank_account?`, `lines:[{employee_name, gross_amount, paye_amount?, pension_amount?, cost_center?}]` | `201` run |
| `POST /payroll-runs/generate/` | `finance.payrollrun.create` | Draft a run from the **active roster** | `pay_date`, `period_label?`, `narration?` | `201` run |
| `GET /payroll-runs/summary/` | `finance.payrollrun.view` | KPIs over all runs | — | `success_response` |
| `GET /payroll-runs/<pk>/` | `finance.payrollrun.view` | Run + lines | — | detail |
| `POST /payroll-runs/<pk>/post/` | `finance.payrollrun.post` | **Accrue** (DRAFT → POSTED) | — | run |
| `POST /payroll-runs/<pk>/pay/` | `finance.payrollrun.pay` | **Disburse** net wages (POSTED → PAID) | `bank_account?`, `pay_date?` | run |
| `POST /payroll-runs/<pk>/cancel/` | `finance.payrollrun.post` | Cancel a DRAFT, or **void** a POSTED (un-paid) run — reverses the accrual → CANCELLED. Refused once PAID | — | run |
| `GET/POST /employee-salaries/` | `finance.payrollrun.view` / `.create` | Roster list / add | `name`, `gross_amount`, `structure?`, flat `paye/pension?`, `cost_center?` | salary |
| `PATCH/DELETE /employee-salaries/<pk>/` | `finance.payrollrun.create` | Edit / remove a roster row | — | salary |
| `GET/POST /salary-structures/` | `finance.payrollrun.view` / `.create` | Structure list / create | `name`, `components:[…]` | structure |
| `GET /salary-structures/<pk>/` | `finance.payrollrun.view` | One structure + components | — | detail |

> **Note:** the roster (`employee-salaries`) and templates (`salary-structures`)
> reuse the **`payrollrun`** permission family — there's no separate resource, so
> managing them needs `finance.payrollrun.create`.

## 4. Lifecycle / state machine

```
Roster (EmployeeSalary) ──generate──▶ DRAFT run
                          (or POST /payroll-runs/ by hand)
DRAFT ──post (accrue)──▶ POSTED ──pay (disburse)──▶ PAID
  │                         │  │                        │
cancel                    journal │              disbursement_journal
  ▼                          cancel (reverse accrual)
CANCELLED  ◀─────────────────┘   (PAID can't be cancelled — reverse the disbursement first)
```
- **post** requires a DRAFT with ≥1 line, positive gross, and **no negative net** on
  any line; it stamps `run_status=POSTED` and `status=POSTED`, and freezes the four
  posting accounts on the run.
- **pay** requires POSTED and a bank account; it clears net wages and sets `PAID`.
- **cancel** (`cancel_payroll_run`): a DRAFT is just marked CANCELLED; a POSTED run is
  **voided** by reversing its accrual journal → CANCELLED; a PAID run is **refused**
  (the cash left the bank — reverse the disbursement first). Idempotent when already
  cancelled.

## 5. Calculations

**Structure application** — `apply_structure` (`payroll.py:41`), integer kobo:
```
value(component) = amount                         if FIXED
                 = base × rate_bps / 10000          (base = gross, or basic for %-of-basic)
basic  = Σ value(earning components flagged is_basic)
paye   = Σ value(deduction where statutory_type == PAYE)
pension= Σ value(deduction where statutory_type == PENSION)
net    = gross − paye − pension
```
Earnings are an *informational* split of gross; only PAYE/pension deductions reduce
net (so the accrual always balances). An `EmployeeSalary` **with a structure derives
paye/pension** (its flat fields are ignored); **without one** it uses the flat
`paye_amount`/`pension_amount`.

**Run totals** — `recompute_totals` sums the lines; `compute_payroll` re-derives each
`net = gross − paye − pension` first.

## 6. What posting does to the ledger

**Accrual** — `_post_payroll_atomic` (`payroll.py:168`):
```
Dr  salary expense (5200, split by cost centre)   Σ gross   ← P&L, carries cost centre
Cr  PAYE payable (2310)                           Σ paye
Cr  pension payable (2320)                         Σ pension
Cr  net wages payable (2330)                       Σ net
```
The gross line is **split by cost centre** (each employee's gross grouped by their
cost centre) so the P&L slices by department; the three liabilities are balance-sheet
control accounts and stay aggregated. `Σ(gross by cost centre) == gross_total`, so it
balances (`gross = paye + pension + net`).

**Disbursement** — `_pay_payroll_atomic` (`payroll.py:272`):
```
Dr  net wages payable (2330)   net_total
Cr  bank (the bank account's GL cash)   net_total
```
PAYE and pension **stay parked** as liabilities until remitted (a separate tax-filing
/ AP payment — see `finance_tax_remittance`). Both postings run `post_journal` (the
`finance_journals_posting` guards) and write durable rejection rows on failure.

## 7. Worked example

Roster of 2 with a "Senior" structure (Basic 60% of gross, PAYE 10% of gross, Pension
8% of gross). `POST /payroll-runs/generate/ {pay_date:"2026-07-25"}` → DRAFT with
per-line gross/paye/pension/net derived. `post/` on gross `₦1,000,000` total →
`Dr 5200 1,000,000 (by cost centre) / Cr 2310 100,000 / Cr 2320 80,000 / Cr 2330
820,000`. `pay/ {bank_account:"GTB-OPS"}` → `Dr 2330 820,000 / Cr <bank> 820,000`;
run → PAID. A viewer with only `payrollrun.view` sees the run and the ₦1,000,000
gross total, but each employee line's name/amounts are stripped.

## 8. Gotchas / known limitations

- ✅ **Cancel/void a run** (`cancel/`) — a DRAFT is cancelled; a POSTED run is voided
  by reversing its accrual. **PAID runs are refused** (reverse the disbursement first);
  there's no `unpay`/clawback action, so a paid run in error still needs manual journal
  reversal.
- **Totals are visible with plain `view`; only individual salaries are FLS-masked.**
  `payrollrun.view` exposes `gross_total`/`net_total` etc. on the run — deliberate
  (aggregate cost for finance) — but treat run-level totals as *not* secret.
- **A structure silently overrides the flat PAYE/pension** on an `EmployeeSalary` — if
  a row has both a structure and typed figures, the typed ones are ignored.
- **Roster + structures need `payrollrun.create`** (no dedicated resource) — HR-style
  roster edits share the payroll-run create privilege.
- **`generate` copies the roster at that moment** — later roster edits don't touch an
  already-generated draft; regenerate for a fresh copy.

## 9. Permissions & tenant isolation

- Verbs (one resource): `finance.payrollrun.{view, create, post, pay,
  view_sensitive}`. `view` and `view_sensitive` are both **SENSITIVE** in the seed —
  payroll is sensitive by nature.
- **Field-level security:** `PayrollLineSerializer` and `EmployeeSalarySerializer` use
  `FieldSecurityMixin` — `gross/paye/pension/net_amount`, `components` (and the line's
  `employee_name`) are stripped unless the caller holds
  `finance.payrollrun.view_sensitive` (`serializers.py:963`, `:1044`). The roster keeps
  **names** visible (it's the roster) but hides the amounts.
- Every action resolves the entity then `filter(entity=…, pk=…)` (`_run`/`_resolve_salary`)
  → cross-tenant run/salary id → 404. `bank_account`/accounts are entity-scoped. ✅

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `PayrollRun`, `PayrollLine`, `SalaryStructure`, `SalaryComponent`, `EmployeeSalary` |
| `payroll.py` | `apply_structure`, `compute_payroll`, `generate_run_from_roster`, `_accounts_for`, `post_payroll`, `pay_payroll`, `cancel_payroll_run` |
| `views_ops/payroll.py` | run list/create/generate/summary/post/pay, employee-salary + salary-structure CRUD |
| `serializers.py` | `PayrollRun/LineSerializer` (FLS), `EmployeeSalarySerializer` (FLS), `SalaryStructure/ComponentSerializer` |
| `constants.py` | `PayrollRunStatus`, `SalaryComponentKind`, `SalaryCalcMethod`, `StatutoryType`; `SALARIES_EXPENSE_CODE`/`PAYE_PAYABLE_CODE`/`PENSION_PAYABLE_CODE`/`NET_WAGES_PAYABLE_CODE` |

## 11. Test coverage & gaps

Existing (`tests.py`, `PayrollTests`): accrual posts balanced with statutory
liabilities; gross salary splits by cost centre; disbursement clears net payable;
negative net rejected; can't pay an unposted run.

Worth asserting if not already:
- **403** per verb; **FLS**: a caller without `view_sensitive` gets line amounts
  stripped but still sees run totals; **cross-tenant** run/salary id → 404.
- `apply_structure`: FIXED / %-of-gross / %-of-basic, and that a structure overrides
  flat PAYE/pension; `net = gross − paye − pension`.
- `generate_run_from_roster` uses only active rows and errors on an empty roster.
- Post guards (draft, ≥1 line, positive gross, no negative net); empty-list shape.
- **Cancel/void** (added): a DRAFT is cancelled with no GL; a POSTED run's accrual is
  reversed → CANCELLED; a PAID run is refused.
