# finance_chart_of_accounts

Foundations slice: the **ledger entity** (the tenant of every finance document),
its **chart of accounts** (CoA), and its **fiscal calendar** (years + periods).
Everything else in finance — invoices, journals, payroll, budgets — hangs off
these three.

Routes covered (mounted at `/v1/finance/`):
`entities/`, `accounts/`, `accounts/<pk>/`, `periods/`, `fiscal-years/`.

---

## 1. What it is (and what it is NOT)

- A **`LedgerEntity`** is a distinct *set of books* — the accounting entity that
  owns documents and numbering. It is **the tenant, not a School**: a school may
  own several entities, Codex's own platform books are an entity with no school,
  and future products plug in the same way (`models/core.py:62`).
- An **`Account`** is one node in that entity's chart-of-accounts tree. **Header**
  accounts (`is_postable=False`) only roll up totals; only **leaf, postable**
  accounts take journal lines (`models/gl.py:103`).
- A **`FiscalYear`** contains **`FiscalPeriod`** rows (normally one per calendar
  month). A period's `status` is what the posting engine checks before it will
  write into it (`models/gl.py:182`, `:212`).

**This slice does NOT:**
- Post anything. Creating/editing accounts and listing periods never touches
  balances. Posting lives in `finance_journals_posting`; closing a period lives
  in `finance_period_close`.
- Let you edit an account's `account_type`, `normal_balance`, or `parent` after
  creation — those would reclassify already-posted history, so PATCH refuses
  them (`views.py:355`, only `name/subtype/description/is_active/is_postable`).
- Scope the **entity list** to the caller's school — `GET /entities/` returns
  *all* sets of books on the platform (see §9).

## 2. Domain model

| Model | File | Key fields | Scoping / constraints |
|---|---|---|---|
| `LedgerEntity` | `models/core.py:62` | `code` (uppercase, in doc numbers), `name`, `kind` (PLATFORM/TENANT/PRODUCT/OTHER), `source_school` (nullable, **non-unique**), `base_currency` (FK→Currency, default NGN), `is_active` | `code` **globally unique**; `source_school` 1-tenant→many-entities |
| `Account` | `models/gl.py:103` | `code`, `name`, `account_type`, `normal_balance` (derived), `is_contra`, `is_postable`, `parent` (self-FK tree), `subtype`, `ifrs_line` | `unique(entity, code)` — two entities can both run a `1000` |
| `FiscalYear` | `models/gl.py:182` | `year` (label used in doc numbers), `start_date`, `end_date`, `status` | `unique(entity, year)` |
| `FiscalPeriod` | `models/gl.py:212` | `period_no` (1–12, 13+ adjustment), `name`, `start/end_date`, `status`, `closed_at/by` | `unique(fiscal_year, period_no)` |

- **Money is kobo** everywhere (integer minor units); no floats. `Currency` is
  **global** reference data, not entity-scoped (`models/gl.py:34`).
- **`account_type` → `normal_balance`** is derived, not free-typed
  (`constants.py:127` `NORMAL_BALANCE_BY_TYPE`):
  - `ASSET`, `EXPENSE` → **DEBIT**
  - `LIABILITY`, `EQUITY`, `INCOME` → **CREDIT**
  - `is_contra=True` **flips** it (accumulated depreciation, sales returns).
    Computed by `Account.default_normal_balance()` and filled in `Account.save()`
    when left blank (`models/gl.py:166`, `:175`).
- **`PeriodStatus`** (`constants.py:11`): `OPEN` (postings allowed) →
  `SOFT_CLOSED` (admins/auto only) → `CLOSED` (reversible re-open) → `LOCKED`
  (sealed, e.g. after statutory filing).

## 3. Endpoint map

All require `?entity=<id|code>` **except** `GET/POST /entities/` (the entity list
is the thing that enumerates entities). Permission gate is
`IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body (fields actually read) | response |
|---|---|---|---|---|
| `GET /entities/` | `finance.entity.view` | List **all** sets of books. Query: `kind`, `is_active` | — | paginated `LedgerEntitySerializer` |
| `POST /entities/` | `finance.entity.create` | **Provision** a new entity *and* seed currencies + starter CoA + 12 periods | `code`, `name`, `kind?`, `base_currency?` (3-letter code), `source_school?`, `fiscal_year?`, `fiscal_start_month?` | `201` `LedgerEntitySerializer` |
| `GET /accounts/?entity=` | `finance.account.view` | CoA. `?with_balance=true` → **full tree, un-paginated**, with `balance` + `tag`. Else paginated picker list. Query: `account_type` (single or `A,B`), `is_postable` | — | paginated **or** `success_response` tree of `AccountSerializer` |
| `POST /accounts/?entity=` | `finance.account.create` | Create one CoA node | `code`, `name`, `account_type`, `parent?` (**by pk**), `is_contra?`, `is_postable?`, `subtype?`, `description?` | `201` `AccountSerializer` |
| `GET /accounts/<pk>/?entity=` | `finance.account.view` | Account + balance summary + posted-line activity (running balance) | — | `success_response` (see §7) |
| `PATCH /accounts/<pk>/?entity=` | `finance.account.update` | Edit **safe** fields only | `name?`, `subtype?`, `description?`, `is_active?`, `is_postable?` | `AccountSerializer` |
| `GET /periods/?entity=` | `finance.period.view` | List fiscal periods. Query: `status`, `year` | — | paginated `FiscalPeriodSerializer` |
| `GET /fiscal-years/?entity=` | `finance.period.view` | List fiscal years. Query: `status` | — | paginated `FiscalYearSerializer` |

> **Field gotcha (the kind that broke earlier docs):** account create reads
> `parent` as a **pk** (`views.py:189`), *not* a code — unlike cost centers,
> which resolve `parent` by code. `currency`, `ifrs_line`, and `normal_balance`
> are **not** settable on create; `normal_balance` is always derived.

## 4. Lifecycle / state machine

- **Entity:** created `is_active=True`, `activated_at=now` in one transaction
  that also seeds currencies, a starter chart, and a fiscal year of 12 open
  monthly periods (`serializers.py:117` → `seed_*`). No deactivation endpoint in
  this slice.
- **Account:** `is_active` / `is_postable` toggled via PATCH; `account_type`,
  `normal_balance`, `parent` are immutable after creation by design.
- **FiscalPeriod:** `OPEN → SOFT_CLOSED → CLOSED → LOCKED`. This slice only
  **reads** status; the transitions are driven by `finance_period_close`.

## 5. Calculations

This slice has no money *movement*, but it derives two displayed numbers.

**(a) Derived normal balance** — `Account.default_normal_balance()`
(`models/gl.py:166`):
```
base = NORMAL_BALANCE_BY_TYPE[account_type]      # ASSET→DEBIT, INCOME→CREDIT, …
normal_balance = flip(base) if is_contra else base
```
Example: `account_type=ASSET, is_contra=True` (accumulated depreciation) → base
`DEBIT`, flipped → **CREDIT**.

**(b) Account balance, signed to normal side** — two code paths that must agree:

- *Chart column* — `AccountSerializer.get_balance()` (`serializers.py:163`) off
  the view's annotations `_bal_dr = Σ(opening_debit + debit_total)`,
  `_bal_cr = Σ(opening_credit + credit_total)` over `AccountBalance`
  (`views.py:206`):
  ```
  net = _bal_dr - _bal_cr
  if normal_balance != DEBIT: net = -net          # credit accounts read positive
  ```
- *Detail running balance* — `AccountDetailView.get()` (`views.py:300`) walks
  **posted** lines oldest-first:
  ```
  sign = +1 if normal_balance == DEBIT else -1
  net_of_line   = sign * (debit - credit)         # both kobo
  running      += net_of_line                     # accumulated, then list reversed (newest first)
  opening       = Σ net_of_line for lines dated before the current FY start
  ```
  The headline `current_balance` uses `_account_gl_net(acc)` from `reports.py`
  (same source as the chart column) so the two never disagree; the activity
  list's running total is rebuilt from the actual lines.

Worth noting: the **chart** balance reads the denormalised `AccountBalance`
aggregate (all periods), while the **detail activity** re-sums raw posted lines —
two representations of the same truth, by design.

## 6. What posting does to the ledger

Nothing — **this slice never posts.** It is the *target* of postings made
elsewhere: other slices resolve control accounts out of this chart **by code**
through `resolve_account(entity, code, …)` (`accounts.py:14`), which returns only
an **active, postable** account and otherwise raises `MissingAccountError` — a
misconfigured chart fails loudly instead of posting into the wrong place. The
seeded control codes those services expect include `1100` Cash & Bank, `1200` AR,
`2100` AP, `2150` GR/IR, `2200`/`1300` Output/Input VAT, `2300` WHT Payable,
`2310` PAYE, `2320` Pension, `1900` Accumulated Depreciation (`seed.py:23`,
`DEFAULT_CHART`).

## 7. Worked example

**Create a child account** (`POST /v1/finance/accounts/?entity=LEKKI`):
```json
{ "code": "6100", "name": "Salaries", "account_type": "EXPENSE", "parent": 42 }
```
→ `normal_balance` derived to `DEBIT`; `201` with `AccountSerializer` data
(`balance` is `null` here because picker/non-tree responses aren't annotated —
`serializers.py:171`).

**Account detail** (`GET /v1/finance/accounts/55/?entity=LEKKI`) response shape:
```json
{
  "success": true,
  "message": "Account detail retrieved.",
  "data": {
    "account": { "id": 55, "code": "1100", "name": "Cash & Bank", "account_type": "ASSET",
                 "normal_balance": "DEBIT", "is_postable": true, "tag": null, "balance": null },
    "type_label": "Asset",
    "summary": { "current_balance": {"kobo": 4500000, "naira": "₦45,000.00"},
                 "opening_balance": {"kobo": 0, "naira": "₦0.00"},
                 "line_count": 12, "journal_count": 9 },
    "activity": [
      { "date": "2026-06-20", "journal_no": "CFX-LEKKI-JNL-2026-00009", "source": "Manual",
        "status": "POSTED", "description": "Term-2 tuition receipt", "cost_center": "PRI",
        "debit": {"kobo": 2500000, "naira": "₦25,000.00"}, "credit": {"kobo": 0, "naira": "₦0.00"},
        "running_balance": {"kobo": 4500000, "naira": "₦45,000.00"} }
    ]
  }
}
```
(`activity` is newest-first; money is always the `{kobo, naira}` pair via
`_money()`, `views.py:961`.)

## 8. Gotchas / known limitations

- **`GET /entities/` is not school-scoped** (`views.py:138`) — it lists every set
  of books. Safe only if `finance.entity.view` is granted to platform admins
  only. **Verify the role grant** before treating this as fine (see §9).
- **`parent` is a pk on account create**, a code on cost-center create — easy to
  document wrong. Account create also silently ignores any `currency`/`ifrs_line`
  in the body.
- **`with_balance=true` returns the whole tree un-paginated** — fine for a CoA
  (bounded), but it also runs a `Sum` over `AccountBalance` per node; large
  charts pay for it.
- `_resolve_period` treats a numeric `?period=` as a **pk only when > 12**,
  otherwise as a `period_no` (`views.py:95`) — a quirk to remember when reusing
  it.

## 9. Permissions & tenant isolation

- **Account/period/year reads** go through `EntityScopedListMixin` →
  `resolve_entity()` (`views.py:46`): non-`CX_STAFF` callers are filtered to
  `source_school=<their school>`, and unknown **or** forbidden entities both
  return `NotFound` so an outsider can't probe which codes exist. A `?entity=`
  swap to another tenant's books → `404`. ✅
- **Account detail/PATCH** resolve the entity first, then `filter(entity=…, pk=…)`
  (`views.py:271`), so a `pk` from another tenant → `NotFound`. ✅
- **Entity list (`GET /entities/`)** is the exception — **no per-school filter**
  (§8). This is the one isolation question in the slice; resolve it by confirming
  who holds `finance.entity.view`.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/core.py` | `LedgerEntity`, `DocumentSequence`, `FinanceDocument` base (numbering) |
| `models/gl.py` | `Account`, `FiscalYear`, `FiscalPeriod`, `Currency`, balances |
| `views.py` | `EntityListCreateView`, `AccountListCreateView`, `AccountDetailView`, `FiscalPeriod/YearListView`, `resolve_entity` |
| `serializers.py` | `LedgerEntity(Create)Serializer`, `AccountSerializer`, `FiscalPeriod/YearSerializer` |
| `accounts.py` | `resolve_account()` — control-account lookup by code (used by *other* slices) |
| `seed.py` | `DEFAULT_CHART`, `seed_chart_of_accounts`, `seed_currencies`, `seed_fiscal_year` |
| `constants.py` | `AccountType`, `NormalBalance`, `NORMAL_BALANCE_BY_TYPE`, `PeriodStatus` |

## 11. Test coverage & gaps

To assert (security-critical first):
- `403` for a caller missing `finance.account.view` / `finance.entity.create`.
- **Cross-tenant:** school-A user hitting `?entity=<school-B>` and
  `accounts/<school-B-pk>/` → `404`.
- **`GET /entities/` exposure:** confirm a school-scoped user with
  `finance.entity.view` cannot see other tenants' books (or that the key is
  platform-only).
- Happy path: create account → `normal_balance` derived correctly for each type
  and for `is_contra`; PATCH cannot change `account_type`.
- Empty-list shape: `GET /accounts/` for a fresh entity (`success_response`
  coerces `[]` → `{}`).
- Balance agreement: chart `balance` == detail `current_balance` for the same
  account after some postings.

> Check `apps/vs_finance/tests.py` for which of these already exist before
> writing new ones.
