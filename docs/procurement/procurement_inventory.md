# procurement_inventory

Procurement inventory is the entity-scoped perpetual stock ledger: stock-item
masters carry live quantity and value, immutable movements explain every receipt,
issue, and adjustment, and moving-average costing connects that sub-ledger to the
general ledger. Routes are mounted at `/v1/procurement/`; every route in this slice
requires `?entity=<id|code>`.

---

## 1. What it is (and what it is NOT)

A `StockItem` represents a physically held good and carries its current quantity,
integer-kobo value, accounting defaults, and reorder policy. A `StockMovement`
records each signed change and its post-movement balance. Stock-backed GRNs increase
inventory at purchase cost; issues relieve it at moving-average cost; adjustments
record count gains or shrinkage (`models.py:332-417,420-479`;
`stock.py:47-107,114-301`).

**This is not the purchasing catalog, a warehouse-management system, or a stock
reservation engine.** `CatalogItem` stores reusable buying defaults and may describe
services; its link to `StockItem` is optional. There are no warehouse/location,
bin, lot, serial-number, reservation, transfer, or pick/pack entities in this slice
(`models.py:332-369`). Procurement also does not bypass finance: issues and
adjustments create journals through the finance posting engine, which enforces
balanced entries and open accounting periods (`stock.py:165-197,261-301`).

## 2. Domain model

Money is integer **kobo**. Stock-master policy quantities are `Decimal(14,4)`;
live balances and movement quantities are `Decimal(16,4)` (`models.py:371-388,
438-449`).

| Model | Key fields | Tenant/relationship rules |
|---|---|---|
| `StockItem` | immutable API code, name/description/unit, optional catalog link, required inventory asset account, optional issue expense account, reorder level/quantity, ledger-owned `on_hand_qty` and `stock_value`, active flag | Every row belongs to a protected `LedgerEntity`; code is unique by `(entity, code)` through `uniq_proc_stockitem_entity_code`; account rows are protected and the catalog link is `SET_NULL` (`models.py:348-400`; `views/stock.py:145-187`) |
| `StockMovement` | `RECEIPT`, `ISSUE`, or `ADJUSTMENT`; date; signed quantity/value; resulting balance snapshots; optional GRN/journal; reference/narration/actor | Every row carries its entity and protected stock item; journal is protected, GRN is `SET_NULL`; indexed by entity/date, item/date, and movement type; newest movement date/id first (`constants.py:186-198`; `models.py:420-476`) |
| `GoodsReceivedNoteLine.stock_item` | optional link that changes a receipt line from expense recognition to inventory capitalisation | Protected link to a stock item; accepted quantity and ex-tax value enter stock, while rejected quantity does not (`models.py:1075-1119`; `purchasing.py:283-338,374-386`) |

The API never accepts writes to `on_hand_qty` or `stock_value`; only the stock
services change them and append the corresponding movement (`views/stock.py:145-190`;
`stock.py:47-69`). The catalog list/detail adds `stock_status` only when the caller
also has `procurement.stock.view`; otherwise it returns `null`, so catalog visibility
alone cannot disclose quantities (`views/catalog.py:148-190`).

## 3. Endpoint map

Request bodies below list only fields actually read by the view. Lists use the
standard paginated `{pagination, data}` envelope; report rows do not
(`views/base.py:281-298`; `views/stock.py:332-381`).

| Method + path | permission key | what it does | request body / query fields actually read | response shape |
|---|---|---|---|---|
| `GET /stock-items/` | `procurement.stock.view` | List inventory masters | Query `is_active=true|false`, `q`, `needs_reorder=true` | Paginated rows with accounting/catalog ids and codes, reorder data, live quantity/value, derived unit cost/reorder flag, active flag (`views/stock.py:77-88`; `serializers.py:343-377`) |
| `POST /stock-items/` | `procurement.stock.manage` | Create a zero-balance stock master | `code`, `name`, `description?`, `unit_of_measure?`, `catalog_item?`, `inventory_account`, `default_expense_account?`, `reorder_level?`, `reorder_qty?`, `is_active?` | `201` detail item with `movements` and `activity` (`views/stock.py:90-121`; `serializers.py:380-399`) |
| `GET /stock-items/summary/` | `procurement.stock.view` | Return entity-wide stock KPIs | — | `{tracked, active, low_stock, out_of_stock, total_value, total_value_naira}` (`views/stock.py:273-308`) |
| `GET /stock-items/<pk>/` | `procurement.stock.view` | Read one item and recent history | — | Item fields plus newest 50 movements and finance-audit activity (`views/stock.py:136-143`; `serializers.py:380-440`) |
| `PATCH /stock-items/<pk>/` | `procurement.stock.manage` | Change master defaults, never balances | `code?` as same-code no-op, `name?`, `description?`, `unit_of_measure?`, `catalog_item?`, `inventory_account?`, `default_expense_account?`, `reorder_level?`, `reorder_qty?`, `is_active?` | Updated detail item (`views/stock.py:145-190`) |
| `POST /stock-items/<pk>/issue/` | `procurement.stock.issue` | Consume stock at moving-average cost | `quantity`, `movement_date?`, `expense_account?`, `reference?`, `narration?` | `201 {movement, stock_item}` with the posted journal id and new balances (`views/stock.py:193-228`; `serializers.py:402-440`) |
| `POST /stock-items/<pk>/adjust/` | `procurement.stock.adjust` | Apply a signed physical-count correction | `quantity_delta`, `movement_date?`, `adjustment_account?`, `unit_cost?` in integer kobo, `reference?`, `narration?` | `201 {movement, stock_item}` (`views/stock.py:231-270`; `serializers.py:402-440`) |
| `GET /stock-movements/` | `procurement.stock.view` | List the entity movement ledger | Query `stock_item=<id|exact code>`, `movement_type` | Paginated signed movements with balance snapshots, source ids, actor, and raw/formatted values (`views/stock.py:311-329`; `serializers.py:402-440`) |
| `GET /reports/stock-reorder/` | `procurement.report.view` | List active items at/below their reorder point | — | `{entity, rows[]}`; quantity strings and `unit_cost: {kobo, naira}` (`views/stock.py:332-355`) |
| `GET /reports/stock-valuation/` | `procurement.report.view` | Value all active and inactive entity stock | — | `{entity, rows[], total_value}`; money values are `{kobo, naira}` (`views/stock.py:358-381`; `stock.py:333-350`) |

Stock enters through the existing GRN routes. The GRN create/replace view reads
`po_line`, `line_no`, `description`, `expense_account`, `cost_center`,
`accepted_qty`, `rejected_qty`, and `unit_price` for each line—but **does not read
`stock_item`**. Consequently the public API cannot currently create a stock-backed
receipt even though the model and posting service support one (`views/receiving.py:
56-120,146-176,200-225`; `models.py:1093-1097`; `purchasing.py:319-386`).

## 4. Lifecycle / state machine

```text
Stock master:
create (zero qty/value) ─manage─▶ active or inactive master

Perpetual balance:
stock-backed GRN DRAFT ─post─▶ RECEIPT  (+ quantity, + purchase value)
held stock             ─issue─▶ ISSUE    (− quantity, − moving-average value)
physical count         ─adjust +▶ ADJUSTMENT (+ quantity, + valued write-up)
physical count         ─adjust −▶ ADJUSTMENT (− quantity, − moving-average value)
```

Movement rows have no update/delete endpoint and serve as the historical ledger.
Deactivation does not remove held stock or history: inactive items stay in valuation
but are excluded from reorder suggestions (`models.py:420-479`;
`stock.py:308-350`). The API still permits an issue or adjustment against an inactive
item, which allows an obsolete item to be run down or corrected
(`views/stock.py:201-220,239-262`).

## 5. Calculations

- Receipt value is `round_half_up(accepted_qty × unit_price)` kobo through
  `compute_line_net`; rejected units contribute zero. Example: `10 × 125,000 =
  1,250,000` kobo (`purchasing.py:283-325`).
- Post-receipt balances are `new_qty = old_qty + accepted_qty` and `new_value =
  old_value + receipt_value`; the derived weighted-average unit cost is
  `round(total stock_value ÷ on_hand_qty)` kobo. Example: 10 units worth
  `1,000,000` plus 10 worth `2,000,000` produces 20 units worth `3,000,000`,
  average `150,000` kobo (`stock.py:47-69,91-107`; `models.py:403-409`).
- Issue value is `round(stock_value × issue_qty ÷ on_hand_qty)` kobo. Full
  depletion returns the entire carried value, preventing a residual balance.
  Example: 4 of 20 units carrying `3,000,000` costs `600,000` kobo
  (`stock.py:72-84,145-163`).
- A negative adjustment uses the same moving-average relief formula. A positive
  adjustment uses `unit_cost × quantity_delta`, or the current average when stock
  already exists; an empty item requires an explicit unit cost. Example: `+2 ×
  150,000 = +300,000` kobo (`stock.py:237-259`).
- A movement snapshots `balance_qty = previous balance + signed quantity` and
  `balance_value = previous balance + signed value` after the change
  (`stock.py:47-69`).
- `needs_reorder` is inclusive: `on_hand_qty <= reorder_level`. The reorder report
  applies that rule only to active items and returns the configured suggestion
  quantity; it does not calculate an economic order quantity (`models.py:411-414`;
  `stock.py:308-330`).
- Valuation is `Σ StockItem.stock_value` across every entity item, including
  inactive ones (`stock.py:333-350`). The summary separately counts low stock as
  active with `0 < on_hand <= reorder_level`, and out of stock as active with
  `on_hand <= 0` (`views/stock.py:281-307`).

All stock valuation paths now use the same explicit `ROUND_HALF_UP` integer-kobo
helper. Full depletion still takes the exact remaining value rather than rounding a
ratio (`models.py:403-412`; `stock.py:47-50,110-124,292-301`).

## 6. What posting does to the ledger

### Stock receipt

For accepted stock-backed GRN lines, grouped by inventory account:

```text
Dr stock_item.inventory_account (normally 1400 Inventory)   accepted ex-tax value
    Cr 2150 GR/IR clearing                                  accepted ex-tax value
```

The inventory and GR/IR control lines drop the source cost center. The same
transaction posts the GL journal, raises the stock quantity/value, writes a
`RECEIPT` movement linked to the GRN and journal, advances PO received quantity,
and marks the GRN POSTED. `receive_stock` does not post a second journal
(`purchasing.py:319-386`). Account 1400 is seeded as a postable asset
(`vs_finance/seed.py:23-31`).

### Stock issue

```text
Dr supplied expense account or item.default_expense_account   moving-average value
    Cr item.inventory_account                                 moving-average value
```

Neither line carries a cost center because the issue API/service accepts no
dimension field. The journal and negative `ISSUE` movement commit together; finance
rejects a closed period or invalid journal (`stock.py:135-197`).

### Stock adjustment

For a positive count/write-up:

```text
Dr item.inventory_account                                   adjustment value
    Cr supplied adjustment account or 5150                  adjustment value
```

For shrinkage/write-down:

```text
Dr supplied adjustment account or 5150                     moving-average value
    Cr item.inventory_account                               moving-average value
```

No cost center is carried. Account 5150 `Inventory Adjustments` is the seeded
postable expense default (`constants.py:201-206`; `vs_finance/seed.py:53-57`;
`stock.py:225-301`).

## 7. Worked example

Assume item 42 holds 20 chairs with a total stock value of `3,000,000` kobo, so
its derived moving-average cost is `150,000` kobo. A user issues four chairs:

```json
POST /v1/procurement/stock-items/42/issue/?entity=LEKKI
{
  "quantity": "4.0000",
  "movement_date": "2026-07-26",
  "expense_account": "5100",
  "reference": "OPS-CHAIRS-04",
  "narration": "Chairs issued to operations"
}
```

The service calculates `3,000,000 × 4 ÷ 20 = 600,000` kobo and posts:

```text
Dr 5100 Cost of Sales       600,000
    Cr 1400 Inventory       600,000
```

The response contains the signed movement and refreshed item:

```json
{
  "movement": {
    "movement_type": "ISSUE",
    "quantity": "-4.0000",
    "value_amount": -600000,
    "balance_qty": "16.0000",
    "balance_value": 2400000,
    "reference": "OPS-CHAIRS-04",
    "journal_id": 901
  },
  "stock_item": {
    "id": 42,
    "code": "CHAIR",
    "on_hand_qty": "16.0000",
    "stock_value": 2400000,
    "unit_cost": 150000,
    "needs_reorder": false
  }
}
```

This follows the real response composition and serializers; formatted naira mirrors
are additive and omitted above for brevity (`views/stock.py:201-228`;
`serializers.py:343-440`).

## 8. Gotchas / known limitations

### Fixed automatically

- ✅ **Concurrent movements now preserve the GL/sub-ledger tie.** Receipt, issue,
  and adjustment re-read the authoritative stock row under `SELECT ... FOR UPDATE`.
  GRN posting locks all referenced stock items once in sorted primary-key order before
  PO counters and journal construction, preventing stale overwrite, over-issue races,
  and inverted multi-item lock order (`stock.py:52-82,131-149,177-240,268-340`;
  `purchasing.py:299-327,384-395`).
- ✅ **A carried balance pins its inventory account.** PATCH locks the master and
  rejects a different inventory account while either quantity or value is nonzero.
  Sending the same account remains a valid no-op; an empty item can be remapped
  (`views/stock.py:167-226`).
- ✅ **Every fractional-kobo stock calculation rounds half up.** One helper now owns
  derived unit cost, proportional relief, explicit-cost write-ups, and
  average-cost write-ups; full depletion takes the exact residue
  (`models.py:403-412`; `stock.py:47-50,110-124,292-301`).
- ✅ **Malformed master/movement payloads now return stable 400 errors.** Duplicate
  normalized codes map the database constraint race to `code`; unit, reference, and
  narration lengths are bounded; signed adjustments enforce four decimal places; and
  `is_active` requires a real JSON boolean (`views/base.py:178-196`;
  `views/stock.py:48-54,98-143,167-226,237-256,275-298`).

### Recommend-fix

- **The normal GRN API cannot select a stock item.** The database/service support
  stock receipts, but create/PATCH silently ignore `stock_item`, so stock can enter
  only through direct service/ORM callers (`views/receiving.py:56-120,146-176,
  200-225`; `models.py:1093-1097`).
- **Item detail fetches the entire movement history before returning 50.** The
  serializer slices a prefetched Python list; a long-lived item therefore loads every
  movement on each detail request (`views/stock.py:50-63`;
  `serializers.py:380-395`).
- **Receipt audit activity is missing from the item timeline.** `STOCK_RECEIVED`
  exists as an audit action, but GRN posting records only `GRN_POSTED` against the GRN.
  The movement ledger shows receipts, while `StockItem.activity` shows only issues and
  adjustments (`vs_finance/constants.py:461-465`; `purchasing.py:393-398`;
  `serializers.py:397-399`).

### Judgment calls

- **Issue and adjustment journals have no cost center.** This keeps inventory and
  adjustment control postings simple, but stock consumption cannot be attributed to
  a department through these endpoints (`views/stock.py:201-220,239-262`;
  `stock.py:173-180,276-283`).
- **Reorder and valuation reports are unpaginated.** Their response is convenient
  for whole-entity exports but grows linearly with the stock catalog
  (`stock.py:308-350`; `views/stock.py:332-381`).

### Justified by design

- **Movements are append-only through the public API.** There is no PATCH/DELETE
  route; corrections create a signed adjustment and keep the original evidence
  (`urls.py:101-108`; `stock.py:204-301`).
- **Inactive stock remains issuable/adjustable and appears in valuation.** This lets
  operations run down or correct obsolete held value; inactive items are excluded only
  from replenishment (`views/stock.py:201-262`; `stock.py:308-350`).
- **Control lines drop cost center.** Stock-backed receipt debits and GR/IR are
  balance-sheet controls, so they remain unallocated; non-stock receipt expenses carry
  the line dimension (`purchasing.py:319-361`).
- **One catalog item may map to multiple stock items.** The model does not make
  `catalog_item` unique, which permits separate physical stock records while the
  catalog overlay derives a combined status (`models.py:356-360`;
  `views/catalog.py:160-190`).

## 9. Permissions & tenant isolation

The stock surface separates ordinary visibility (`procurement.stock.view`) from
sensitive master maintenance, issue, and adjustment verbs. The two stock reports use
the broader normal-sensitivity `procurement.report.view`; the seed matrix registers
all five keys (`management/commands/seed_procurement_permissions.py:28-50`).

Every view resolves the caller's `LedgerEntity`, then filters item/movement rows by
that entity. Account and catalog references are also resolved inside it, so changing
a path `pk` or supplying an account id from another entity returns 404/400 rather
than crossing the tenant boundary (`views/stock.py:77-88,90-119,136-190,
201-267,281-329`; `views/base.py:45-98`). The tests exercise denied verbs,
cross-entity item ids, and cross-entity account references
(`tests.py:6070-6170`).

There is no field-level masking on stock serializers: anyone with stock-view
permission receives raw quantity, value, account ids/codes, movement journal ids,
references, and narrations (`serializers.py:343-440`). Catalog-only users do not
receive the stock-status overlay unless separately granted stock view
(`views/catalog.py:148-190`).

## 10. Code map

| File | Responsibility |
|---|---|
| `apps/vs_procurement/models.py:332-479,1075-1119` | Stock masters, immutable movement records, and the GRN stock link |
| `apps/vs_procurement/constants.py:186-206` | Movement choices and inventory/adjustment control account codes |
| `apps/vs_procurement/stock.py:42-350` | Balance mutation, moving-average valuation, issue/adjustment journals, reorder and valuation reports |
| `apps/vs_procurement/purchasing.py:255-399` | Stock-backed GRN valuation, GL posting, PO receipt counters, and receipt movements |
| `apps/vs_procurement/views/stock.py:50-381` | Entity-scoped CRUD, issue/adjust actions, summary, movement feed, and stock reports |
| `apps/vs_procurement/views/base.py:129-211` | Quantity and integer-kobo request validation |
| `apps/vs_procurement/views/catalog.py:148-190` | Permission-gated aggregate stock status on catalog items |
| `apps/vs_procurement/serializers.py:343-440` | Stock item/detail/movement response contracts |
| `apps/vs_procurement/urls.py:101-108,129-130` | Inventory and stock-report routes |
| `apps/vs_procurement/management/commands/seed_procurement_permissions.py:28-50` | RBAC key registration and sensitivity |
| `apps/vs_finance/seed.py:23-31,53-57` | Default 1400 Inventory and 5150 Inventory Adjustments accounts |
| `apps/vs_procurement/tests.py:5818-6628` | Stock service/accounting, concurrency, and REST security/contract coverage |

## 11. Test coverage & gaps

Current tests cover stock-backed GRN capitalisation, concurrent same-item receipts,
dropped control-line cost center, two-lot moving average, stale-instance issue and
adjustment locking, issue journal sides and insufficient-stock rejection, half-up
rounding and exact full depletion, positive/negative adjustment journals, opening-stock
cost requirement, reorder/valuation results, every endpoint's 403 gate, distinct
permission verbs, cross-entity item/account isolation, immutable codes and
ledger-owned balances, carried-balance account protection, duplicate/UOM/boolean/text/
precision validation, summary states, report values, receipt movement visibility, and
the empty-movement response shape (`tests.py:1247-1277,5818-6628`). The complete
procurement suite is 242 tests green after the fixes.

Still missing before this slice is ship-ready:

- GRN API stock-item assignment and cross-entity/inactive-stock-item rejection;
- a query-count or bounded-prefetch test for the newest-50 detail contract;
- receipt audit activity on the stock item;
- cost-center behavior if dimensional issues/adjustments are adopted;
- large-catalog pagination/response-shape coverage if report pagination is adopted.
