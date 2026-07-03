# finance_fixed_assets

The fixed-asset register: **acquire** (capitalise `Dr PP&E, Cr bank/payable`), lay
down a monthly **depreciation schedule** (straight-line or declining-balance), post
depreciation per asset or as one **period run**, and **dispose** (clear cost +
accumulated depreciation, book proceeds and the gain/loss).

Routes (mounted at `/v1/finance/`): `fixed-assets/`, `fixed-assets/summary/`,
`fixed-assets/run-depreciation/`, `fixed-assets/<pk>/{acquire,depreciate,dispose}/`.

---

## 1. What it is (and what it is NOT)

- **`FixedAsset`** (`models/ops.py:986`): a depreciable asset —
  `cost`, `salvage_value`, `useful_life_months`, `method`, three posting accounts
  (defaults: PP&E `1500`, accumulated depreciation `1900` contra, expense `5400`),
  `asset_status` (`DRAFT → ACTIVE → FULLY_DEPRECIATED / DISPOSED`).
- **`DepreciationSchedule`** (`:1053`): one planned (then posted) monthly charge;
  `unique(asset, seq)`; rows always sum to `depreciable_base` exactly.

**This does NOT:**
- **Re-plan a part-depreciated asset silently.** `build_depreciation_schedule`
  refuses once any row has posted (`assets.py:103`).
- **Let depreciation overshoot.** Charges are integer-exact: the final month absorbs
  rounding; declining-balance never takes book value below salvage.
- **Un-dispose / reverse depreciation** via API — corrections are journal reversals.

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `FixedAsset` | `models/ops.py:986` | `name`, `category`, 3 accounts, `acquisition_date`, `cost`, `salvage_value`, `useful_life_months`, `method`, `asset_status`, `accumulated_depreciation`, `acquisition_journal`, `disposal_date/journal` |
| `DepreciationSchedule` | `:1053` | `seq`, `depreciation_date`, `amount`, `is_posted`, `journal`, `posted_at` |

Derived: `depreciable_base = max(cost − salvage, 0)`; `net_book_value = cost −
accumulated_depreciation`. Money is kobo.

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /fixed-assets/` | `finance.fixedasset.view` | Register list (paginated) | — | assets |
| `POST /fixed-assets/` | `finance.fixedasset.create` | Create a **DRAFT** asset | `name`, `acquisition_date`, `cost`, `salvage_value?`, `useful_life_months`, `method?`, `category?`, accounts? | `201` asset |
| `GET /fixed-assets/summary/` | `finance.fixedasset.view` | Register KPIs (over all rows) | — | summary |
| `GET /fixed-assets/<pk>/` | `finance.fixedasset.view` | Asset + schedule | — | detail |
| `POST /fixed-assets/<pk>/acquire/` | `finance.fixedasset.acquire` | Capitalise + build schedule (DRAFT → ACTIVE) | `bank_account` **or** `credit_account` | asset |
| `POST /fixed-assets/<pk>/depreciate/` | `finance.fixedasset.depreciate` | Post this asset's due charges (one journal per row) | `up_to_date` | rows posted |
| `POST /fixed-assets/run-depreciation/` | `finance.fixedasset.depreciate` (GET preview: `.view`) | One compound journal **per fiscal period** covering every due charge across the entity | `up_to_date` | `{journal_id, journal_ids[], period_count, total, …}` |
| `POST /fixed-assets/<pk>/dispose/` | `finance.fixedasset.dispose` | Retire/sell (→ DISPOSED) | `disposal_date`, `proceeds?`, `bank_account?`, `gain_loss_account?` | asset |

## 4. Lifecycle / state machine

```
DRAFT ──acquire──▶ ACTIVE ──(last schedule row posts)──▶ FULLY_DEPRECIATED
                     │                                        │
                     └───────────── dispose ──────────────────┴──▶ DISPOSED
```
Depreciation posts from **ACTIVE only** (a DRAFT asset — not yet capitalised — is
refused) until the schedule is exhausted; disposal is allowed from ACTIVE or
FULLY_DEPRECIATED.

## 5. Calculations

**Straight line** (`_straight_line_amounts`, `assets.py:65`):
```
per_month = base // months ;  final month += remainder   (Σ == base exactly)
```
**Declining balance** (`_declining_balance_amounts`, `:72`) — double-declining with
the textbook switch to straight-line:
```
charge = max(book_value × 2/months, (book_value − salvage)/months_left)
         capped at (book_value − salvage);  final month lands exactly on salvage
```
**Schedule dates**: `acquisition_date + seq` months, day clamped to month length
(Jan 31 → Feb 28). **Disposal**: `gain_loss = proceeds − net_book_value`.

## 6. What posting does to the ledger

**Acquire** (`_acquire_asset_atomic`, `assets.py:162`):
`Dr PP&E (1500) cost / Cr bank-or-payable cost`, then builds the schedule.

**Depreciate (per asset)** (`_post_depreciation_atomic`, `:240`) — for each due row:
`Dr depreciation expense (5400) / Cr accumulated depreciation (1900)`, each in its
**own** journal dated on the row's `depreciation_date` (so charges land in their own
periods); rolls `accumulated_depreciation`; flips FULLY_DEPRECIATED when done.

**Run (entity-wide)** (`_run_period_depreciation_atomic`) — due charges are grouped
by their **fiscal period**, and each period gets its own compound journal (Dr per
expense account / Cr per accum account) dated at the latest charge date within that
period — so a backlog never collapses into one month. A CLOSED period in the range
raises cleanly (re-open it via `periods/<id>/reopen/`, then re-run); a date with no
fiscal period raises a typed `DepreciationError`. Period close still auto-posts due
depreciation with `allow_restricted` (into a SOFT_CLOSED period) via
`close.run_period_depreciation`.

**Dispose** (`_dispose_asset_atomic`, `:424`):
```
Dr accumulated depreciation (written back)   Dr bank (proceeds)   Dr loss | Cr gain
Cr asset cost (PP&E)
```

## 7. Worked example

Asset: cost ₦1,200,000, salvage ₦0, 12 months, straight-line. `acquire/
{bank_account}` → `Dr 1500 / Cr bank`, 12 rows of ₦100,000. After 3 months,
`depreciate/ {up_to_date: 2026-10-01}` → three `Dr 5400 100,000 / Cr 1900 100,000`
journals; NBV ₦900,000. Sell for ₦950,000: `dispose/ {proceeds: 95000000,
bank_account, gain_loss_account: "4900…"}` → `Dr 1900 300,000, Dr bank 950,000 /
Cr gain 50,000, Cr 1500 1,200,000`.

## 8. Gotchas / known limitations

- ✅ **The run posts per-period** (was: one lump dated `up_to_date`) — a backlog now
  lands in its own months, matching the per-asset path. Response keeps `journal_id`
  (first/earliest) and adds `journal_ids` + `period_count`. A CLOSED period in the
  backlog raises — by design; re-open, then re-run.
- ✅ **Depreciation is ACTIVE-only** (was: DRAFT allowed) — an un-capitalised asset
  can no longer accrue depreciation.
- **Disposal doesn't warn about unposted due charges** — NBV at disposal uses only
  what's been *booked*; pending schedule rows are silently orphaned (they drop out of
  `_due_depreciation` once DISPOSED). Accounting-acceptable, but a "you have unposted
  depreciation" warning would help.
- **No un-dispose / no schedule edit** after posting starts — corrections via journal
  reversal only.

## 9. Permissions & tenant isolation

- Verbs: `finance.fixedasset.{view, create, acquire, depreciate, dispose}` —
  `dispose` is CRITICAL in the seed.
- Entity-scoped resolution throughout; bank/gain-loss accounts resolved within the
  entity. ✅

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `FixedAsset`, `DepreciationSchedule` |
| `assets.py` | schedule builders, `acquire_asset`, `post_depreciation`, `preview/run_period_depreciation`, `dispose_asset` |
| `views_ops/assets.py` | register CRUD, summary, acquire/depreciate/dispose, run-depreciation (GET preview / POST run) |
| `close.py` | `run_period_depreciation(entity, period)` — the close-time auto-posting |
| `constants.py` | `AssetCategory`, `AssetStatus`, `DepreciationMethod`; account codes 1500/1900/5400 |

## 11. Test coverage & gaps

Existing (fixed-asset tests): schedule maths (both methods), acquire, depreciation
posting, disposal gain/loss.

Added with the fixes: a two-period backlog posts two per-period journals; the
single-period run still returns one (`journal_id == journal_ids[0]`); DRAFT
depreciation rejected; a schedule date with no fiscal period raises a typed error.

Worth asserting: 403 per verb; cross-tenant → 404; rebuild-after-posting refused;
month-end clamping; empty register.
