# finance_budgets

Planning: a **`Budget`** is an entity's plan of P&L amounts for a fiscal year, broken
into **`BudgetLine`** cells of `(account, cost centre, period 1–12)`. It **never posts
to the ledger** — it's the yardstick actuals are measured against. The one rule with
teeth: **approval locks the plan**, so a disappointing variance can't be quietly fixed
by rewriting the budget after the fact.

Routes (mounted at `/v1/finance/`): `budgets/`, `budgets/<pk>/`,
`budgets/<pk>/lines/(<line_id>/)`, `budgets/<pk>/{approve,variance,heatmap}/`.

---

## 1. What it is (and what it is NOT)

- **`Budget`** (`models/ops.py:909`): `fiscal_year`, `name`, `status` — two states
  only, `DRAFT → APPROVED` (approval *is* the lock) — auto code
  `CFX-<entity>-BDG-<year>-NNNNN` from the shared document sequence.
- **`BudgetLine`** (`:956`): one budgeted cell; `unique(budget, account, cost_center,
  period_no)`.

**This does NOT:**
- **Post anything, ever** — read-only against the ledger (`models/ops.py:912`).
- **Budget balance-sheet accounts.** Lines are **income/expense only**
  (`_ensure_pl_account`, `budgets.py:25`) — variance is P&L movement.
- **Allow edits after approval.** Every mutating service runs `_ensure_editable`
  (`budgets.py:18`); approval is **one-way** (no un-approve endpoint).

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `Budget` | `models/ops.py:909` | `code`, `fiscal_year`, `name`, `status`, `approved_at/by`; `unique(entity, fiscal_year, name)`; `is_locked = status == APPROVED` |
| `BudgetLine` | `:956` | `account` (P&L only), `cost_center?`, `period_no` (1–12), `amount` (kobo) |

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /budgets/` | `finance.budget.view` | List (paginated, page-scoped enrichment) | — | budgets |
| `POST /budgets/` | `finance.budget.create` | Create a DRAFT (optionally with lines) | `name`, `fiscal_year`, `lines?` | `201` budget |
| `GET/PATCH/DELETE /budgets/<pk>/` | `finance.budget.view` / `.edit` / `.delete` | Detail / rename (draft-only; `lines` replaces wholesale) / delete a **draft** | `name?`, `lines?` | budget |
| `POST /budgets/<pk>/lines/` | `finance.budget.edit` | Add/update one cell (upsert on the cell key) | `account`, `period_no`, `amount`, `cost_center?` | line |
| `PATCH/DELETE /budgets/<pk>/lines/<line_id>/` | `finance.budget.edit` | Edit / remove a cell (draft-only) | — | — |
| `POST /budgets/<pk>/approve/` | `finance.budget.approve` | DRAFT → APPROVED (locks) | — | budget |
| `GET /budgets/<pk>/variance/` | `finance.budget.view` | Budget-vs-actual per account (`?period_no` scopes) | — | rows + totals |
| `GET /budgets/<pk>/heatmap/` | `finance.budget.view` | Per-account × per-period matrix (bare kobo cells) | — | matrix |

## 4. Lifecycle / state machine

```
DRAFT ──approve──▶ APPROVED  (locked — approval is the lock; there is no third state)
```
Draft: create/rename/line edits (add, wholesale replace, delete). Approved: frozen —
only variance/heatmap reads. No un-approve, no delete-budget endpoint.

## 5. Calculations

**Variance** — `budget_vs_actual` (`reports.py:586`):
```
budget[account] = Σ BudgetLine.amount        (summed across cost centres + periods)
actual[account] = Σ AccountBalance movement  (debit_total − credit_total, signed to the
                                              account's normal balance; opening excluded)
variance        = actual − budget            (per row and in total)
```
- `?period_no` scopes **both** sides to one period.
- Unbudgeted accounts appear **only if P&L** with non-zero movement (the cash/AR contra
  side of postings is deliberately excluded as noise).
- **Cost-centre budgets are summed away here** — variance is per *account* only,
  because actuals come from `AccountBalance`, which carries no cost centre (§8).

**Heatmap** — `budget_monthly_matrix` (`reports.py:703`): the same comparison as a
12-column per-account grid, bare kobo (FE colours by actual/budget ratio).

## 6. What posting does to the ledger

**Nothing — by design.** Budgets are compared against the denormalised
`AccountBalance` read model; no journal is ever raised by any budget action.

## 7. Worked example

`POST /budgets/ {name: "FY26 Plan", fiscal_year: <id>}` → DRAFT with code
`CFX-LEKKI-BDG-2026-00001`. Add a cell: `POST /budgets/<pk>/lines/
{account: "6100", period_no: 1, amount: 5000000, cost_center: "PRI"}`. `approve/` →
locked. Post ₦45,000 of salaries in period 1 → `variance/?period_no=1` shows budget
₦50,000, actual ₦45,000, variance −₦5,000 (under-spend).

## 8. Gotchas / known limitations

- ✅ **The dead `LOCKED` enum was removed** (migration `0026`; no data existed with
  that value). `BudgetStatus` is now honestly two states, and
  `income_statement_compare`'s budget lookup was updated with it.
- **Approval is one-way** — no un-approve. A wrong approved budget can only be
  superseded by a new budget (the `unique(entity, year, name)` means a new name).
- **No cost-centre variance.** Budget cells carry cost centres, but the variance and
  heatmap collapse them per account (actuals from `AccountBalance` have no cost
  centre). Now that GL lines carry cost centres, a line-level actuals query (as in
  `analytics_slice`) could close this.
- **`PATCH lines` replaces wholesale** (delete + recreate) — send the full set.
- ✅ **Draft budgets can be deleted** (`DELETE /budgets/<pk>/`, key
  `finance.budget.delete`, audited as `BUDGET_DELETED`); an approved budget still
  refuses — the lock survives.

## 9. Permissions & tenant isolation

- Verbs: `finance.budget.{view, create, edit, approve}` — editing and approving are
  separated (maker/checker).
- Entity-scoped resolution; `_resolve_lines` resolves accounts/cost-centres within
  the entity → no cross-tenant cells. ✅

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `Budget`, `BudgetLine` |
| `budgets.py` | `create/update_budget`, `add_budget_line`, `set_budget_lines`, `delete_budget_line`, `approve_budget`, `_ensure_editable`, `_ensure_pl_account` |
| `views_ops/budgets.py` | list/create (paginated), detail, line CRUD, approve, variance, heatmap |
| `reports.py` | `budget_vs_actual`, `budget_monthly_matrix` |
| `constants.py` | `BudgetStatus` |

## 11. Test coverage & gaps

Existing (`BudgetTests`): approve locks lines against edits; variance maths.

Worth asserting: 403 per verb; cross-tenant → 404; P&L-only rejection; duplicate-cell
rejection in `set_budget_lines`; `?period_no` scoping both sides; unbudgeted
balance-sheet accounts excluded; empty lists.
