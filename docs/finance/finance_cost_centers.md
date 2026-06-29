# finance_cost_centers

> **This file replaces the earlier cost-centers write-up, which was wrong on the
> two claims that matter most** (cost centers surviving invoice posting; an
> expense-claim `amount` field). Both are corrected below and the underlying
> behavior is traced to code. Read §1 and §6 first — they overturn the old doc.

A **`CostCenter`** is the finance module's analytical bucket for slicing activity
by department / project / branch unit (Primary, Secondary, Admin, Sports). It is
**independent of the chart of accounts**: keep accounts generic (`Salaries`,
`Tuition Income`) and attach a cost center to a *line* to answer "how much did
Primary spend on salaries?".

Route covered (mounted at `/v1/finance/`): `cost-centers/`.

---

## 1. What it is (and what it is NOT)

- A `CostCenter` (`models/gl.py:300`) is an entity-scoped, optionally-hierarchical
  tag (`parent` self-FK for rollups like `PRI` → `PRI-Y1`). `unique(entity, code)`.
- It is attachable to **source-document and budget lines** that carry a
  `cost_center` FK: `InvoiceLine`, `ExpenseClaimLine`, `PayrollLine`,
  `BudgetLine`, and the GL `JournalLine` itself.

**This does NOT:**
- ❌ **It is NOT a replacement for the chart of accounts.** Keep accounts generic
  and split them with cost centers, not by minting per-department accounts.

> **History — what the old draft got wrong, and the fix.** The earlier write-up
> claimed an invoice's cost center "keeps the same cost center" on the posted
> journal. At the time that was *false in the other direction*: `post_invoice`
> (and every other sub-ledger posting) aggregated lines **by account only** and
> **dropped** the cost center, so cost centers never reached the GL at all. That
> gap has since been **fixed** — P&L lines now split by `(account, cost center)`
> and carry the tag into the ledger (§6). Balance-sheet control and tax lines
> still (correctly) do not.

## 2. Domain model

| Model | File | Key fields | Constraints |
|---|---|---|---|
| `CostCenter` | `models/gl.py:300` | `code`, `name`, `parent` (self-FK), `is_active` | `unique(entity, code)` |

Lines that *can* hold a `cost_center` FK (nullable): `JournalLine`
(`models/gl.py:444`), plus `InvoiceLine`, `ExpenseClaimLine`, `PayrollLine`,
`BudgetLine`. Setting it is optional; omitting it leaves the line unallocated.

> Sibling concept: **`Dimension`** (`models/gl.py:331`) — user-defined extra axes
> carried as a JSON map on journal lines. Same "captured on the line" idea; same
> caveat about reaching the GL.

## 3. Endpoint map

Requires `?entity=<id|code>`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /cost-centers/?entity=` | `finance.costcenter.view` | List cost centers. Query: `is_active=true\|false` | — | `success_response` list of `CostCenterSerializer` (un-paginated) |
| `POST /cost-centers/?entity=` | `finance.costcenter.create` | **Upsert** a cost center (`update_or_create` on `(entity, code)`) | `code` (required), `name?` (defaults to `code`), `parent?` (**code or pk**), `is_active?` | `201` if created / `200` if updated, `CostCenterSerializer` |

`CostCenterSerializer` exposes: `id, code, name, parent_id, parent_code,
is_active` (`serializers.py:619`). No sensitive fields.

> **Field note:** `parent` is resolved by `_resolve_cost_center`
> (`views_ops/base.py:59`), which tries **code first, then numeric pk** — unlike
> account-create, where `parent` is a pk only. POSTing the same `code` again
> **updates** it (`update_or_create`, `masterdata.py:197`); it does not error.

## 4. Lifecycle / state machine

No workflow — cost centers are master data. `is_active=false` stops new
allocations conceptually without deleting historical references (there is no DB
guard preventing selection of an inactive one; it's a list filter). Created/edited
only via the upsert POST.

## 5. Calculations

None. A cost center carries no amount; it's a tag. The arithmetic that slices by
cost center lives in reports, which read GL lines — and those GL lines now carry
the cost center (§6), so cost-center P&L slicing works.

## 6. What posting does to the ledger  ← the core behavior (now fixed)

**P&L lines carry the cost center into the GL; balance-sheet control and tax lines
do not.** Each sub-ledger posting groups its P&L lines by `(account, cost_center)`
and emits one GL line per group, passing `cost_center=` through. Two source lines
on the same account but different cost centers produce **two** GL lines, so the
split survives.

| Posting | P&L line | Grouping key | Carries cost center to GL? | Evidence |
|---|---|---|---|---|
| Invoice (`post_invoice`) | revenue (Cr) | `(revenue_account, cost_center)` | **Yes** | `receivables.py:133` |
| Expense claim (`post_expense_claim`) | expense (Dr) | `(expense_account, cost_center)` | **Yes** | `expenses.py:89` |
| Petty-cash voucher | expense (Dr) | `(expense_account, cost_center)` | **Yes** | `petty_cash.py:170` |
| Payroll accrual (`post_payroll`) | gross salary (Dr) | `cost_center` (single salary account) | **Yes** | `payroll.py:198` |
| Reversal (`reverse_journal`) | mirrors original | per original line | **Yes — copies it** | `posting.py:272` |

What stays **un**-allocated (by design — these are not P&L analytics): the AR/AP
control line, output/input **tax** liability lines, the accrued-reimbursement
liability, and the PAYE/pension/net-wages payables. **`direct-entries` accept an
optional per-line `cost_center`** (code or id), resolved within the entity and
carried onto the GL line (`serializers.py:263` field → `views.py:879` resolves →
`posting.py:334` writes it); an unknown code is a `400`.

**Consequence:** the `cost_center` column on `AccountDetailView` activity
(`views.py:325`) and any "spend by cost center" report off journal lines now
returns real values for postings made after the fix. (Journals posted *before* the
fix have no cost center on their GL lines — historical only.)

## 7. Worked examples (corrected)

**Create a cost center** (`POST /v1/finance/cost-centers/?entity=SCHOOL-001`):
```json
{ "code": "PRI", "name": "Primary School", "is_active": true }
```
→ `201`:
```json
{ "success": true, "message": "Cost centre PRI created.",
  "data": { "id": 12, "code": "PRI", "name": "Primary School",
            "parent_id": null, "parent_code": null, "is_active": true } }
```
Re-POSTing `code: "PRI"` returns `200` `"Cost centre PRI updated."`.

**Child cost center** — `parent` by code:
```json
{ "code": "PRI-Y1", "name": "Primary Year 1", "parent": "PRI" }
```

**Attach to an invoice line** (request the API *does* read — corrected):
```json
{ "customer": "CUST-0001", "invoice_date": "2026-06-26", "reference": "INV-DEMO-001",
  "lines": [ { "revenue_account": "4000", "description": "Primary tuition",
               "quantity": "1", "unit_price": 25000000, "cost_center": "PRI" } ] }
```
The cost center is stored on the **`InvoiceLine`**. ⚠️ But when the invoice posts,
the revenue GL line is created **without** it (§6) — do **not** claim it survives
to the journal.

**Expense-claim line — use `unit_price`, NOT `amount`** (the old draft's bug):
```json
{ "claimant_name": "EMP-001", "claim_date": "2026-06-26",
  "lines": [ { "expense_account": "6100", "description": "Teaching supplies",
               "quantity": 1, "unit_price": 150000, "cost_center": "PRI" } ] }
```
`ExpenseClaimListCreateView` prices each line from `quantity` × `unit_price`
(`views_ops/expenses.py:85`); an `amount` key is **ignored**, so the old example
recorded a **0-kobo** line. Same drop on posting applies.

## 8. Gotchas / known limitations

- ✅ **Cost centers reach the GL as of the §6 fix** — but only on the **P&L**
  lines. Don't expect them on AR/AP/tax/payable lines (intentional).
- **Pre-fix history is unallocated.** Journals posted before the fix have no cost
  center on their GL lines; reports spanning that boundary will show a gap.
- POST is an **upsert**, not strict create — a typo'd re-POST silently mutates an
  existing center rather than 409-ing.
- `is_active=false` is a soft filter only; nothing stops a service resolving an
  inactive center if a caller passes its code.
- `direct-entries` now tag a cost center per line (optional); the balancing
  contra leg (e.g. cash) is typically left unallocated by the caller.

## 9. Permissions & tenant isolation

- `finance.costcenter.view` (GET) / `finance.costcenter.create` (POST) — separate
  verbs.
- `resolve_entity` scopes every call (CX-staff-all, else `source_school`);
  `_resolve_cost_center` filters `CostCenter.objects.filter(entity=entity)`
  (`views_ops/base.py:62`), so a cost-center code/pk from another tenant →
  `ValidationError` ("No cost centre … in this entity"), never a cross-tenant
  attach. ✅

## 10. Code map

| File | Responsibility |
|---|---|
| `models/gl.py` | `CostCenter`, `Dimension`, `JournalLine.cost_center` |
| `views_ops/masterdata.py` | `CostCenterListCreateView` (upsert) |
| `views_ops/base.py` | `_resolve_cost_center` (code-then-pk, entity-scoped) |
| `serializers.py` | `CostCenterSerializer` |
| `receivables.py` / `expenses.py` / `petty_cash.py` / `payroll.py` | postings that **split P&L lines** by `(account, cost_center)` and carry it to the GL |
| `posting.py` | `reverse_journal` — mirrors the cost center on a reversal |

## 11. Test coverage & gaps

- `403` for `finance.costcenter.view`/`.create` missing.
- **Cross-tenant:** attaching `?entity=A` with a cost-center code that exists only
  in entity B → rejected.
- Upsert semantics: second POST of same `code` returns `200` and updates.
- `parent` resolves by code and by pk.
- **Regression locks for the §6 fix** (in `CostCenterPropagationTests` +
  `PayrollTests`): invoice revenue splits by cost center into two GL lines while
  the AR control line stays unallocated; expense-claim and petty-cash expense
  lines carry it; payroll gross salary splits by cost center while PAYE/pension/
  net payables stay aggregated; every journal still balances.
- Empty-list shape on a fresh entity.
