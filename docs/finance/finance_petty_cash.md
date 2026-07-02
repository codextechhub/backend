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
- **Hold a second source of truth.** The **GL petty-cash account is truth**;
  `current_balance` is a denormalised mirror **re-synced from the GL after every
  operation**, and the overdraw guard reads the GL live — so the mirror can't drift or
  mis-authorise a payout (§6/§8).
- **Let you overspend the tin.** A voucher whose total exceeds the **live GL** cash on
  hand is **rejected** (`PettyCashOverdrawError`); a row lock stops two concurrent
  vouchers both slipping under the guard.
- **Reject a posted voucher** — but a posted voucher **can be voided** (`void/`):
  it reverses the journal (cash back to the tin) and marks the voucher CANCELLED (§4).

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
| `POST /petty-cash-funds/` | `finance.pettycash.create` | Create a fund (maps to a GL account) | `name`, `gl_account`, `custodian?`/`custodian_name?`, `float_amount?`, `currency?` | `201` fund |
| `GET /petty-cash-funds/<pk>/` | `finance.pettycash.view` | Fund + week spend + movement register | — | detail |
| `PATCH /petty-cash-funds/<pk>/` | `finance.pettycash.update` | Edit fund settings | `name?`, `custodian?`, `float_amount?`, `is_active?` | fund |
| `POST /petty-cash-funds/<pk>/establish/` | `finance.pettycash.establish` | Move cash bank → tin (open/increase the float) | `bank_account`, `amount`, `date` | fund |
| `POST /petty-cash-funds/<pk>/replenish/` | `finance.pettycash.replenish` | Top the tin back to its float (or by `amount`) | `bank_account`, `date`, `amount?` | fund |
| `GET /petty-cash-status/` | `finance.pettycash.view` | Per-fund position + low-balance flags | Query: `threshold_bps` (default 2500 = 25%) | `{rows[]}` |
| `GET /petty-cash-vouchers/` | `finance.pettycashvoucher.view` | List vouchers (paginated). Query: `fund`, `status` | — | paginated `PettyCashVoucherSerializer` |
| `POST /petty-cash-vouchers/` | `finance.pettycashvoucher.create` | Create a **DRAFT** voucher + lines | `fund`, `voucher_date`, `payee?`, `spent_by?`, `lines:[{expense_account, quantity?, unit_price, tax_code?, cost_center?}]` | `201` voucher |
| `GET /petty-cash-vouchers/<pk>/` | `finance.pettycashvoucher.view` | One voucher | — | detail |
| `POST /petty-cash-vouchers/<pk>/post/` | `finance.pettycashvoucher.post` | Post it (relieve the tin) | — | voucher |
| `POST /petty-cash-vouchers/<pk>/void/` | `finance.pettycashvoucher.post` | Void a **posted** voucher (reverses journal, cash back to tin → CANCELLED) | — | voucher |

> **Two resources:** the **fund** (`finance.pettycash.*` — `view/create/update/
> establish/replenish`) and the **voucher** (`finance.pettycashvoucher.*` —
> `view/create/post`) are separate RBAC resources, mirroring how every other finance
> document gets its own resource, so each verb is unambiguous (§9).

## 4. Lifecycle / state machine

```
Fund:    (create) ──establish──▶ float on hand ──voucher posts (spend)──▶ balance ↓
                                        ▲                                     │
                                        └──────── replenish (to float) ◀──────┘
Voucher: DRAFT ──post──▶ POSTED ──void──▶ CANCELLED   (journal reversed, cash back to tin)
```
- **Establish** seeds (or permanently increases) the float. **Vouchers** spend it
  down. **Replenish** tops it back up (defaults to the exact `shortfall`).
- **Void** undoes a posted voucher: reverses its journal (restoring the tin) and marks
  it CANCELLED.
- `current_balance` is **re-synced from the GL** in the *same transaction* as each
  posting (establish / voucher / replenish / void), so it always matches truth.

## 5. Calculations

```
on_hand         = gl_cash_on_hand(fund)   # LIVE GL balance of the petty-cash account
shortfall       = max(float_amount − on_hand, 0)
voucher line    net = quantity × unit_price ;  tax = net × rate_bps / 10000   (ROUND_HALF_UP)
overdraw guard  reject if voucher.total > on_hand          # live GL, not the mirror
replenish top_up = shortfall (default)  or  amount          # shortfall from live GL
needs_replenish = on_hand ≤ float_amount × threshold_bps / 10000   (default 25%)
```
Voucher pricing reuses the shared AR helpers (`receivables.compute_line_net`/
`compute_tax`).

## 6. What posting does to the ledger

Each action posts a journal, then **re-syncs `current_balance` from the GL**
(`gl_cash_on_hand`) in the same transaction.

**Establish** (`_establish_fund_atomic`):
```
Dr  petty cash (fund gl_account)   amount
Cr  bank (bank account's GL cash)  amount
```
**Voucher** (`_post_voucher_atomic`) — DRAFT, positive total, fund active, and
**total ≤ live GL cash on hand** (row-locked):
```
Dr  expense  (per (account, cost centre))   Σ net   ← P&L, carries the cost centre
Dr  input tax (per tax account)             Σ tax
Cr  petty cash (fund gl_account)            total
```
**Replenish** (`_replenish_fund_atomic`):
```
Dr  petty cash (fund gl_account)   top_up
Cr  bank                           top_up          → last_replenished_at = date
```
**Void** (`void_voucher`) — reverses a posted voucher's journal (a mirror
`Dr petty cash, Cr expense`), so the cash returns to the tin and the voucher is
CANCELLED.

All go through `post_journal`/`reverse_journal` (the `finance_journals_posting`
guards); a `FinanceError` writes a durable rejection row. Cost centres ride the
expense lines only (the propagation fix); tax and the petty-cash credit don't.

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

- ✅ **`current_balance` can't drift** — it's re-synced from the GL after every op and
  the overdraw guard reads the GL live (`gl_cash_on_hand`), so a stray direct journal to
  the account no longer leaves the mirror or the guard stale (they self-heal on the next
  op / on any read via `fund_status`).
- ✅ **A posted voucher can be voided** (`void/`) — reverses the journal, returns the
  cash to the tin, marks CANCELLED.
- ✅ **Permission verbs cleaned up** — the fund and voucher are now separate RBAC
  resources with unambiguous verbs (§9).
- **`custodian`/`spent_by` are optional** (free-text fallback), so "who spent it"
  reporting needs the FK set. *(Left intentional.)*
- Fund list and the status endpoint are **un-paginated** (funds are few — fine); the
  voucher list *is* paginated.

## 9. Permissions & tenant isolation

- **Two resources, unambiguous verbs:**
  - `finance.pettycash.{view, create, update, establish, replenish}` — the **fund**
    (master data + cash-in movements).
  - `finance.pettycashvoucher.{view, create, post}` — the **voucher** (`void/` reuses
    `post`, like other undo-an-approval actions).
- Every action resolves the entity then `filter(entity=…, pk=…)` (`_fund`/`_voucher`),
  and `establish`/`replenish` reject a `bank_account` from another entity → no
  cross-tenant cash movement. ✅
- `_resolve_account`/`_resolve_bank_account`/`_resolve_user` are entity/platform scoped.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `PettyCashFund`, `PettyCashVoucher`, `PettyCashVoucherLine` |
| `petty_cash.py` | `gl_cash_on_hand`, `establish_fund`, `price_voucher`, `post_voucher`, `void_voucher`, `replenish_fund`, `fund_status` |
| `views_ops/pettycash.py` | fund CRUD (+ register), establish, replenish, status, voucher list/create/post/void |
| `management/commands/seed_finance_permissions.py` | the `pettycash` + `pettycashvoucher` RBAC resources |
| `serializers.py` | `PettyCashFundSerializer`, `PettyCashVoucherSerializer`(+`Line`) |
| `constants.py` | `PETTY_CASH_CODE` (1110); reuses `DocumentStatus`; `PettyCashOverdrawError` in `exceptions.py` |

## 11. Test coverage & gaps

Existing (`tests.py`, `PettyCashTests`): establish moves cash bank→tin and rejects
non-positive; voucher posts + lowers balance; overdraw blocked and audited; replenish
restores the float (and rejects when nothing to top up); status flags low balance.
Plus (added with the fixes): the guard reads the **live GL** and re-syncs a drifted
mirror (`test_overdraw_guard_uses_live_gl_and_resyncs_mirror`); **void** reverses the
journal + returns cash + CANCELLED, and is refused on a draft.

Worth asserting if not already:
- **403** per verb (fund `view/create/update/establish/replenish`, voucher
  `view/create/post`); **cross-tenant** fund/voucher id → 404; establish/replenish
  reject another entity's bank account.
- Concurrent vouchers can't both overdraw (the `select_for_update` lock).
- `fund_status` threshold boundary (`needs_replenish`); empty-list on a fresh entity.
