# finance_chart_of_accounts

Foundations slice: the **ledger entity** (the tenant of every finance document),
its **chart of accounts** (CoA), and its **fiscal calendar** (years + periods).
Everything else in finance - invoices, journals, payroll, budgets - hangs off
these three.

Routes covered (mounted at `/v1/finance/`):
`entities/`, `accounts/`, `accounts/<pk>/`, `periods/`, `fiscal-years/`.

---

## 1. What it is (and what it is NOT)

- A **`LedgerEntity`** is a distinct *set of books* - the accounting entity that
  owns finance documents. Numbering is owned one level higher by its canonical
  tenant: several entities belonging to the same tenant share each document-code
  series (`models/core.py:220-227`; `vs_tenants/numbering.py:11-39`).
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
  creation - those would reclassify already-posted history, so PATCH refuses
  them (`views.py:355`, only `name/subtype/description/is_active/is_postable`).
- Scope the **entity list** to the caller's school - `GET /entities/` returns
  *all* sets of books on the platform (see §9).

## 2. Domain model

| Model | File | Key fields | Scoping / constraints |
|---|---|---|---|
| `LedgerEntity` | `models/core.py:83` | `code` (uppercase lookup/display code; **not** in new document numbers), `name`, `kind` (PLATFORM/TENANT/PRODUCT/OTHER), `tenant`, `base_currency` (FK→Currency, default NGN), `is_active` | `code` **globally unique**; one tenant may own many entities |
| `Account` | `models/gl.py:103` | `code`, `name`, `account_type`, `normal_balance` (derived), `is_contra`, `is_postable`, `parent` (self-FK tree), `subtype`, `ifrs_line` | `unique(entity, code)` - two entities can both run a `1000` |
| `FiscalYear` | `models/gl.py:182` | `year` (accounting label; document numbers use their allocation date instead), `start_date`, `end_date`, `status` | `unique(entity, year)` |
| `FiscalPeriod` | `models/gl.py:212` | `period_no` (1–12, 13+ adjustment), `name`, `start/end_date`, `status`, `closed_at/by` | `unique(fiscal_year, period_no)` |

- **New document-number format:** `<CODE>-<tenant_id><YYMMDD><daily_sequence>`,
  for example `IV-12607221`. The sequence is unpadded, starts at 1 each local
  calendar day, and is independent per `(tenant, document code, date)`. The row
  is protected by a unique constraint plus `select_for_update`
  (`vs_tenants/models.py:82-110`; `vs_tenants/numbering.py:10-39`).
- Existing stored numbers are not rewritten. The former entity/fiscal-year
  `DocumentSequence` remains only as legacy counter metadata
  (`models/core.py:169-184`).

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

### Account roles (mappings)

Services never hard-code an account for a role. They resolve it through
`account_mappings.resolve_mapped_account(entity, key)`: the entity's
`FinanceAccountMapping` override when it has one (set on
`settings/account-mappings/`, whole-tenant writes only), else the seeded default
code. The account found must be active, postable and of the expected type, or the
posting is refused with the missing account named.

| Key | Default | Type | Used for |
|---|---|---|---|
| `CASH_BANK` | 1100 Cash & Bank | asset | primary cash account |
| `ACCOUNTS_RECEIVABLE` | 1200 Accounts Receivable | asset | customer AR control |
| `DOUBTFUL_DEBT_ALLOWANCE` | 1290 Allowance for Doubtful Debts | asset (contra, credit balance) | set by provision runs, used by write-offs |
| `VENDOR_ADVANCE` | 1240 Vendor Advances | asset | money paid to a vendor before their bill |
| `INVENTORY_ASSET` | 1400 Inventory | asset | stock |
| `INTER_BRANCH` | 1260 Inter-branch Balances | asset (a branch that owes more than it is owed carries a credit balance) | what each branch is owed by or owes to each other branch; every line names its counterparty, and it nets to zero across all branches |
| `HELD_FOR_OTHER_BRANCHES` | 2190 Held for Other Branches | liability | money one branch received that belongs to another, until forwarded |
| `ACCOUNTS_PAYABLE` | 2100 Accounts Payable | liability | vendor AP control |
| `CUSTOMER_CREDIT` | 2140 Customer Credit | liability | unapplied receipts, overpayments, credit notes |
| `GRIR_CLEARING` | 2150 GR/IR Clearing | liability | goods received, not yet billed |
| `DEFERRED_INCOME` | 2160 Deferred Income | liability (current) | invoice lines billed before their service period, until released |
| `DEPOSITS_HELD` | 2170 Customer Deposits Held | liability | refundable deposits until returned or forfeited |
| `OUTPUT_VAT` | 2200 Output VAT | liability | sales tax |
| `WHT_PAYABLE` | 2300 WHT Payable | liability | supplier withholding |
| `RETAINED_EARNINGS` | 3200 Retained Earnings | equity | year-end close, opening balances |
| `BAD_DEBT_RECOVERED` | 4810 Bad Debts Recovered | income | written-off debt later paid |
| `FORFEITED_DEPOSIT_INCOME` | 4820 Forfeited Deposits | income | deposits unclaimed past the entity's limit |
| `INVENTORY_ADJUSTMENT` | 5150 Inventory Adjustments | expense | stock-count differences |
| `PURCHASE_PRICE_VARIANCE` | 5160 Purchase Price Variance | expense | receipt-to-bill price differences |
| `BAD_DEBT_EXPENSE` | 5350 Bad Debts | expense | write-offs beyond the allowance, provision movements |
| `BANK_CHARGES` | 5500 Bank Charges | expense | bank reconciliation adjustments |

1260 and 2190 are part of the starter chart and are kept by the inter-branch
ledger, so a typed journal cannot touch them. Books seeded before them receive
them from migration `0053_inter_branch_accounts`; where a code is already taken,
the first free code in the range (1261-1299, 2191-2199) is created and mapped to
the role.

The six accounts behind the receivables accruals (1290, 2160, 2170, 4810, 4820,
5350) are part of the starter chart. Books whose chart was seeded before them
receive them from migration `0044_receivables_accruals_data`, except where the
books already use one of those codes for something else: there the role must be
pointed at the right account on the mappings screen before it is first used.
2160 presents on the statement of financial position as **deferred income**, a
current liability (`IFRSLine.DEFERRED_INCOME`); 1290 nets into trade receivables.

## 3. Endpoint map

All require `?entity=<id|code>` **except** `GET/POST /entities/` (the entity list
is the thing that enumerates entities). Permission gate is
`IsAuthenticatedAndActive & HasRBACPermission`, except `GET /entities/`,
which uses `HasAnyModuleAccess` on the `finance` module.

| Method + path | permission key | what it does | request body (fields actually read) | response |
|---|---|---|---|---|
| `GET /entities/` | any `finance.*` key | List the caller's own tenant's sets of books (the picker every finance screen opens with). Query: `kind`, `is_active` | - | paginated `LedgerEntitySerializer` |
| `POST /entities/` | `finance.entity.create` | **Provision** a new entity *and* seed currencies + starter CoA + 12 periods | `code`, `name`, `kind?`, `base_currency?` (3-letter code), `source_school?`, `fiscal_year?`, `fiscal_start_month?` | `201` `LedgerEntitySerializer` |
| `GET /accounts/?entity=` | `finance.account.view` | CoA. `?with_balance=true` → **full tree, un-paginated**, with `balance` + `tag`. Else paginated picker list. Query: `account_type` (single or `A,B`), `is_postable` | - | paginated **or** `success_response` tree of `AccountSerializer` |
| `POST /accounts/?entity=` | `finance.account.create` | Create one CoA node | `code`, `name`, `account_type`, `parent?` (**by pk**), `is_contra?`, `is_postable?`, `subtype?`, `description?` | `201` `AccountSerializer` |
| `GET /accounts/<pk>/?entity=` | `finance.account.view` | Account + balance summary + posted-line activity (running balance) | - | `success_response` (see §7) |
| `PATCH /accounts/<pk>/?entity=` | `finance.account.update` | Edit **safe** fields only | `name?`, `subtype?`, `description?`, `is_active?`, `is_postable?` | `AccountSerializer` |
| `GET /periods/?entity=` | `finance.period.view` | List fiscal periods. Query: `status`, `year` | - | paginated `FiscalPeriodSerializer` |
| `GET /fiscal-years/?entity=` | `finance.period.view` | List fiscal years. Query: `status` | - | paginated `FiscalYearSerializer` |

> **Field gotcha (the kind that broke earlier docs):** account create reads
> `parent` as a **pk** (`views.py:189`), *not* a code - unlike cost centers,
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

**(a) Derived normal balance** - `Account.default_normal_balance()`
(`models/gl.py:166`):
```
base = NORMAL_BALANCE_BY_TYPE[account_type]      # ASSET→DEBIT, INCOME→CREDIT, …
normal_balance = flip(base) if is_contra else base
```
Example: `account_type=ASSET, is_contra=True` (accumulated depreciation) → base
`DEBIT`, flipped → **CREDIT**.

**(b) Account balance, signed to normal side** - two code paths that must agree:

- *Chart column* - `AccountSerializer.get_balance()` (`serializers.py:163`) off
  the view's annotations `_bal_dr = Σ(opening_debit + debit_total)`,
  `_bal_cr = Σ(opening_credit + credit_total)` over `AccountBalance`
  (`views.py:206`):
  ```
  net = _bal_dr - _bal_cr
  if normal_balance != DEBIT: net = -net          # credit accounts read positive
  ```
- *Detail running balance* - `AccountDetailView.get()` (`views.py:300`) walks
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
aggregate (all periods), while the **detail activity** re-sums raw posted lines -
two representations of the same truth, by design.

## 6. What posting does to the ledger

Nothing - **this slice never posts.** It is the *target* of postings made
elsewhere: other slices resolve control accounts out of this chart **by code**
through `resolve_account(entity, code, …)` (`accounts.py:14`), which returns only
an **active, postable** account and otherwise raises `MissingAccountError` - a
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
(`balance` is `null` here because picker/non-tree responses aren't annotated -
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
        "dimensions": {"FUND": "GRANT-A"},
        "debit": {"kobo": 2500000, "naira": "₦25,000.00"}, "credit": {"kobo": 0, "naira": "₦0.00"},
        "running_balance": {"kobo": 4500000, "naira": "₦45,000.00"} }
    ]
  }
}
```
(`activity` is newest-first; money is always the `{kobo, naira}` pair via
`_money()`, `views.py:961`.)

## 8. Gotchas / known limitations

- ✅ **`GET /entities/` is now tenancy-scoped** - CX staff see every set of books;
  a school-scoped user sees only entities sourced from their school (none without a
  school), matching `resolve_entity`'s rule. Defence in depth on top of the key being
  seeded to platform admins only.
- ✅ **`parent` on account create accepts a code or a pk** (code tried first, like
  the cost-center resolver). Account create still silently ignores any
  `currency`/`ifrs_line` in the body.
- **`with_balance=true` returns the whole tree un-paginated** - fine for a CoA
  (bounded), but it also runs a `Sum` over `AccountBalance` per node; large
  charts pay for it.
- `_resolve_period` treats a numeric `?period=` as a **pk only when > 12**,
  otherwise as a `period_no` (`views.py:95`) - a quirk to remember when reusing
  it.

## 9. Permissions & tenant isolation

- **Account/period/year reads** go through `EntityScopedListMixin` →
  `resolve_entity()` (`views.py:46`): non-`CX_STAFF` callers are filtered to
  `source_school=<their school>`, and unknown **or** forbidden entities both
  return `NotFound` so an outsider can't probe which codes exist. A `?entity=`
  swap to another tenant's books → `404`. ✅
- **Account detail/PATCH** resolve the entity first, then `filter(entity=…, pk=…)`
  (`views.py:271`), so a `pk` from another tenant → `NotFound`. ✅
- **Entity list (`GET /entities/`)** applies the same rule in its queryset:
  non-CX users are filtered to `source_school`, no school → empty. ✅

## 10. Code map

| File | Responsibility |
|---|---|
| `models/core.py` | `LedgerEntity`, `FinanceDocument` numbering adapter; legacy `DocumentSequence` metadata |
| `vs_tenants/models.py`, `vs_tenants/numbering.py` | Shared tenant/code/day counter and concurrency-safe formatter |
| `models/gl.py` | `Account`, `FiscalYear`, `FiscalPeriod`, `Currency`, balances |
| `views.py` | `EntityListCreateView`, `AccountListCreateView`, `AccountDetailView`, `FiscalPeriod/YearListView`, `resolve_entity` |
| `serializers.py` | `LedgerEntity(Create)Serializer`, `AccountSerializer`, `FiscalPeriod/YearSerializer` |
| `accounts.py` | `resolve_account()` - control-account lookup by code (used by *other* slices) |
| `seed.py` | `DEFAULT_CHART`, `seed_chart_of_accounts`, `seed_currencies`, `seed_fiscal_year` |
| `constants.py` | `AccountType`, `NormalBalance`, `NORMAL_BALANCE_BY_TYPE`, `PeriodStatus` |

## 11. Test coverage & gaps

To assert (security-critical first):
- `403` for a caller missing `finance.account.view` / `finance.entity.create`.
- **Cross-tenant:** school-A user hitting `?entity=<school-B>` and
  `accounts/<school-B-pk>/` → `404`.
- **`GET /entities/` scoping** (covered by `EntityListScopingTests`): CX staff see
  all; a school user sees only their own; a school-less user sees none.
- Happy path: create account → `normal_balance` derived correctly for each type
  and for `is_contra`; PATCH cannot change `account_type`.
- Empty-list shape: `GET /accounts/` for a fresh entity (`success_response`
  coerces `[]` → `{}`).
- Balance agreement: chart `balance` == detail `current_balance` for the same
  account after some postings.

> Check `apps/vs_finance/tests.py` for which of these already exist before
> writing new ones.
