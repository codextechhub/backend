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

**This does NOT (corrections to the old draft):**
- ❌ **It is NOT a replacement for the chart of accounts.** (This part of the old
  draft was right — keep it.)
- ❌ **Invoice cost centers do NOT survive posting.** The old draft claimed "the
  generated journal line keeps the same cost center". It does not — see §6.
  `post_invoice` aggregates invoice lines **by `revenue_account` only** and
  creates revenue GL lines with **no** cost center (`receivables.py:134`,
  `:151`). Two invoice lines on the same account but different cost centers are
  merged and the split is discarded.
- ❌ **More broadly, NO sub-ledger posting propagates the cost center to the
  General Ledger.** Expense claims, payroll, depreciation and adjustments all
  aggregate by account and drop the cost center too (§6). So a cost center set on
  a document is **document-level analytics only** today; it does not slice the
  posted ledger.

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

None. A cost center carries no amount; it's a tag. The arithmetic that *would*
slice by cost center lives in reports — but those read GL lines, which don't carry
one (§6), so cost-center P&L slicing is not currently functional through posting.

## 6. What posting does to the ledger  ← the corrected core

**Cost centers attached to documents are dropped when the document posts to the
GL.** Every sub-ledger posting service aggregates lines by *account* and creates
GL `JournalLine`s **without** a `cost_center`:

| Posting | Aggregation key | Carries cost center to GL? | Evidence |
|---|---|---|---|
| Invoice (`post_invoice`) | `revenue_account_id` (+ tax account) | **No** | `receivables.py:134`, `:151` |
| Expense claim | `expense_account_id` (+ tax) | **No** | `expenses.py:95`, `:111` |
| Payroll run | control accounts | **No** (cost center stays on `PayrollLine` only) | `payroll.py:125` sets it on the *sub-ledger* line, not the GL line |
| Reversal (`reverse_journal`) | mirrors original lines | **Yes — copies `cost_center`** | `posting.py:272` |

The **only** code path in `vs_finance` that writes `cost_center` onto a GL
`JournalLine` is `reverse_journal` (`posting.py:272`), and it merely copies
whatever the original line had. Since no forward posting sets one, in practice GL
lines have no cost center. `direct-entries` can't set one either — its line
serializer accepts only `account/debit/credit` (`serializers.py:263`).

**Consequence:** `AccountDetailView` activity shows a `cost_center` column
(`views.py:325`), but it will be empty for posted activity. Any "spend by cost
center" report built off journal lines returns nothing meaningful today. Where
cost centers *are* genuinely used is the **budget** sub-system (BudgetLine carries
one and budget variance/heatmap read budgeted vs actual at the document level) and
as descriptive metadata on the source documents themselves.

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

- 🔴 **Cost centers don't reach the General Ledger** (§6). Document-level only.
  "Spend by cost center" off the GL doesn't work. This is an architectural gap,
  not a doc nuance — flagged for the backlog.
- The GL `JournalLine` and `AccountDetailView` expose a `cost_center` field that
  is effectively always empty for posted activity → misleading to readers/clients.
- POST is an **upsert**, not strict create — a typo'd re-POST silently mutates an
  existing center rather than 409-ing.
- `is_active=false` is a soft filter only; nothing stops a service resolving an
  inactive center if a caller passes its code.

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
| `receivables.py` / `expenses.py` / `payroll.py` | postings that **drop** the cost center at the GL |
| `posting.py` | `reverse_journal` — only path that carries it onto a GL line |

## 11. Test coverage & gaps

- `403` for `finance.costcenter.view`/`.create` missing.
- **Cross-tenant:** attaching `?entity=A` with a cost-center code that exists only
  in entity B → rejected.
- Upsert semantics: second POST of same `code` returns `200` and updates.
- `parent` resolves by code and by pk.
- **Regression lock for the corrected behavior:** a test asserting that after
  `post_invoice` the revenue GL line's `cost_center` is `None` (documents the
  current drop) — and a separate failing/xfail test if/when the gap is fixed to
  propagate it.
- Empty-list shape on a fresh entity.
