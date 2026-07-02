# finance_petty_cash

An **imprest petty-cash float**: a physical cash tin, mapped 1:1 to its own GL
account. You **establish** the float from a bank account, spend it via **vouchers**
(each posting `Dr expense, Cr petty cash`), and **replenish** it back to the float
when it runs low. It runs perpetually — the fund's `current_balance` mirrors the GL
petty-cash account's balance at all times.

Routes (mounted at `/v1/finance/`): `petty-cash-funds/…`,
`petty-cash-funds/<pk>/{establish,replenish}/`, `petty-cash-status/`,
`petty-cash-vouchers/…`, `petty-cash-vouchers/<pk>/post/`.

---

## 1. What it is (and what it is NOT)

- A **`PettyCashFund`** (`models/ops.py:370`) is master data — like a `BankAccount` —
  mapped **1:1** to a petty-cash GL account (`gl_account`, OneToOne). It holds the
  custodian, the `float_amount` (the imprest it's restored to), and a live
  `current_balance` (cash on hand).
- A **`PettyCashVoucher`** (`models/ops.py:435`) is one small disbursement; posting
  raises `Dr expense(s) (+ Dr input VAT), Cr petty cash` and lowers the fund.
- The cycle: **establish** (open/top up the float from bank) → **voucher** (spend) →
  **replenish** (restore to float from bank).

**This does NOT:**
- **Hold a second source of truth.** `current_balance` is a maintained mirror of the
  GL petty-cash account — "money only ever moves via journals against `gl_account`"
  (`models/ops.py:370`). It's kept in step *by these services*, not by every posting
  to that account (§8).
- **Let you overspend the tin.** A voucher whose total exceeds `current_balance` is
  **rejected** (`PettyCashOverdrawError`); a row lock stops two concurrent vouchers
  both slipping under the guard.
- **Void/reject a posted voucher.** A voucher is `DRAFT → POSTED` only; the cash left
  the tin when posted, so a mistake is fixed by reversing the journal (no `void` here).

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `PettyCashFund` | `models/ops.py:370` | `gl_account` (1:1), `name`, `custodian?`/`custodian_name`, `float_amount`, `current_balance`, `last_replenished_at`, `is_active` |
| `PettyCashVoucher` | `:435` | `fund`, `voucher_date`, `payee`, `spent_by?`, `subtotal`/`tax_total`/`total`, `status`, `journal` |
| `PettyCashVoucherLine` | `:493` | `expense_account`, `quantity`, `unit_price`, `tax_code`, `net_amount`, `tax_amount`, `cost_center` |

- Money is kobo. `shortfall = max(float_amount − current_balance, 0)`. `unique(entity, name)`.
- A voucher reuses `DocumentStatus` (`DRAFT/POSTED`); the fund has no status beyond
  `is_active`.

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /petty-cash-funds/` | `finance.pettycash.view` | List funds. Query: `is_active` | — | `PettyCashFundSerializer[]` (un-paginated) |
| `POST /petty-cash-funds/` | `finance.pettycash.manage` | Create a fund (maps to a GL account) | `name`, `gl_account`, `custodian?`/`custodian_name?`, `float_amount?`, `currency?` | `201` fund |
| `GET /petty-cash-funds/<pk>/` | `finance.pettycash.view` | Fund + week spend + movement register | — | detail |
| `PATCH /petty-cash-funds/<pk>/` | `finance.pettycash.manage` | Edit fund settings | `name?`, `custodian?`, `float_amount?`, `is_active?` | fund |
| `POST /petty-cash-funds/<pk>/establish/` | `finance.pettycash.replenish` | Move cash bank → tin (open/increase the float) | `bank_account`, `amount`, `date` | fund |
| `POST /petty-cash-funds/<pk>/replenish/` | `finance.pettycash.replenish` | Top the tin back to its float (or by `amount`) | `bank_account`, `date`, `amount?` | fund |
| `GET /petty-cash-status/` | `finance.pettycash.view` | Per-fund position + low-balance flags | Query: `threshold_bps` (default 2500 = 25%) | `{rows[]}` |
| `GET /petty-cash-vouchers/` | `finance.pettycash.view` | List vouchers (paginated). Query: `fund`, `status` | — | paginated `PettyCashVoucherSerializer` |
| `POST /petty-cash-vouchers/` | `finance.pettycash.create` | Create a **DRAFT** voucher + lines | `fund`, `voucher_date`, `payee?`, `spent_by?`, `lines:[{expense_account, quantity?, unit_price, tax_code?, cost_center?}]` | `201` voucher |
| `GET /petty-cash-vouchers/<pk>/` | `finance.pettycash.view` | One voucher | — | detail |
| `POST /petty-cash-vouchers/<pk>/post/` | `finance.pettycash.post` | Post it (relieve the tin) | — | voucher |

> **Permission verbs are mixed here:** fund create/edit uses `manage`, voucher create
> uses `create`, establish **and** replenish both use `replenish`, posting a voucher
> uses `post` (§9) — worth care when assigning roles.

## 4. Lifecycle / state machine

```
Fund:    (create) ──establish──▶ float on hand ──voucher posts (spend)──▶ balance ↓
                                        ▲                                     │
                                        └──────── replenish (to float) ◀──────┘
Voucher: DRAFT ──post──▶ POSTED   (lowers the fund; no void/reject)
```
- **Establish** seeds (or permanently increases) the float. **Vouchers** spend it
  down. **Replenish** tops it back up (defaults to the exact `shortfall`).
- `current_balance` moves in the *same transaction* as each posting (establish +total,
  voucher −total, replenish +top_up).

## 5. Calculations

```
shortfall       = max(float_amount − current_balance, 0)
voucher line    net = quantity × unit_price ;  tax = net × rate_bps / 10000   (ROUND_HALF_UP)
overdraw guard  reject if voucher.total > current_balance
replenish top_up = shortfall (default)  or  amount
needs_replenish = current_balance ≤ float_amount × threshold_bps / 10000   (default 25%)
```
Voucher pricing reuses the shared AR helpers (`receivables.compute_line_net`/
`compute_tax`).

## 6. What posting does to the ledger

Each action posts a journal **and** adjusts `current_balance` atomically.

**Establish** (`_establish_fund_atomic`, `petty_cash.py:66`):
```
Dr  petty cash (fund gl_account)   amount
Cr  bank (bank account's GL cash)  amount        → current_balance += amount
```
**Voucher** (`_post_voucher_atomic`, `petty_cash.py:139`) — DRAFT, positive total, fund
active, and **total ≤ current_balance** (row-locked):
```
Dr  expense  (per (account, cost centre))   Σ net   ← P&L, carries the cost centre
Dr  input tax (per tax account)             Σ tax
Cr  petty cash (fund gl_account)            total   → current_balance −= total
```
**Replenish** (`_replenish_fund_atomic`, `petty_cash.py:257`):
```
Dr  petty cash (fund gl_account)   top_up
Cr  bank                           top_up          → current_balance += top_up,
                                                     last_replenished_at = date
```
All go through `post_journal` (the `finance_journals_posting` guards); a `FinanceError`
writes a durable rejection row. Cost centres ride the expense lines only (the
propagation fix); tax and the petty-cash credit don't.

## 7. Worked example

`POST /petty-cash-funds/?entity=LEKKI` `{name:"Front desk", gl_account:"1110",
float_amount:5000000}` → fund, balance ₦0.
`establish/` `{bank_account:"GTB-OPS", amount:5000000, date}` → `Dr 1110 / Cr <bank>`,
balance ₦50,000. Spend: `POST /petty-cash-vouchers/` `{fund, voucher_date,
lines:[{expense_account:"5300", unit_price:120000}]}` → DRAFT; `post/` → `Dr 5300 1200
/ Cr 1110 1200`, balance ₦48,800. A voucher for ₦60,000 → **`400` overdraw** (only
₦48,800 in the tin). `replenish/` `{bank_account, date}` → tops up the ₦1,200
shortfall back to ₦50,000.

## 8. Gotchas / known limitations

- **`current_balance` is a mirror maintained only by these services.** A journal
  posted *directly* to the petty-cash GL account (e.g. a manual `direct-entry` or an
  adjustment) moves the GL but **not** `current_balance`, so the two would drift.
  Trust the GL as truth; treat `current_balance` as a convenience mirror.
- **No void for a posted voucher** — `DRAFT → POSTED` only; correct a mistake by
  reversing the voucher's journal (unlike expense claims, there's no `void/` action).
- **Establish uses the `replenish` permission** (no separate establish verb), and fund
  master-data uses `manage` while voucher create uses `create` — mixed verbs (§9).
- **`custodian`/`spent_by` are optional** (free-text `custodian_name` fallback), so
  "who spent it" reporting needs the FK set.
- Fund list and the status endpoint are **un-paginated** (funds are few — fine); the
  voucher list *is* paginated.

## 9. Permissions & tenant isolation

- Verbs: `finance.pettycash.{view, manage (fund CRUD), create (voucher), post
  (voucher), replenish (establish + replenish)}`.
- Every action resolves the entity then `filter(entity=…, pk=…)` (`_fund`/`_voucher`),
  and `establish`/`replenish` reject a `bank_account` from another entity
  (`petty_cash.py`) → no cross-tenant cash movement. ✅
- `_resolve_account`/`_resolve_bank_account`/`_resolve_user` are entity/platform
  scoped.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `PettyCashFund`, `PettyCashVoucher`, `PettyCashVoucherLine` |
| `petty_cash.py` | `establish_fund`, `price_voucher`, `post_voucher`, `replenish_fund`, `fund_status` |
| `views_ops/pettycash.py` | fund CRUD (+ register), establish, replenish, status, voucher list/create/post |
| `serializers.py` | `PettyCashFundSerializer`, `PettyCashVoucherSerializer`(+`Line`) |
| `constants.py` | `PETTY_CASH_CODE` (1110); reuses `DocumentStatus`; `PettyCashOverdrawError` in `exceptions.py` |

## 11. Test coverage & gaps

Existing (`tests.py`, `PettyCashTests`): establish moves cash bank→tin and rejects
non-positive; voucher posts + lowers balance; overdraw blocked and audited; replenish
restores the float (and rejects when nothing to top up); status flags low balance.

Worth asserting if not already:
- **403** per verb (view/manage/create/post/replenish); **cross-tenant** fund/voucher
  id → 404; establish/replenish reject another entity's bank account.
- Concurrent vouchers can't both overdraw (the `select_for_update` lock).
- `current_balance` stays equal to the GL account balance across establish → spend →
  replenish; and the **drift** case (a direct journal to the account) is documented.
- `fund_status` threshold boundary (`needs_replenish`); empty-list on a fresh entity.
