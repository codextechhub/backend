# finance_reports_statements

The read-only reporting surface — financial statements, AR analyses, the dashboard,
exports — plus **period close** (the one write in this slice). Reports are plain
dataclasses rendered straight to JSON (no ModelSerializer); most read the
denormalised `AccountBalance` aggregates, a few (statements of record, analytics)
re-read posted `JournalLine`s.

Routes (mounted at `/v1/finance/`): `reports/{dashboard, trial-balance,
income-statement, balance-sheet, cash-flow, changes-in-equity, statutory-pack,
ar-aging, ar-reconciliation, customer-statement, analytics-slice}/`,
`periods/<id>/checklist/`, `periods/<id>/close/`.

---

## 1. What it is (and what it is NOT)

- One `reports.py` module of pure functions → dataclasses; each report view wraps
  one, all gated on **`finance.report.view`**, all exportable (`?export=csv|xlsx|pdf`).
- **Period close** (`close.py`) is the exception that writes: it runs a checklist,
  optionally posts due depreciation, then transitions the period's status.

**This does NOT:**
- **Recompute the ledger.** Statements read `AccountBalance` (kept in step by the
  posting engine); they're views over truth, not re-derivations — except AR aging /
  customer statements / analytics-slice, which walk documents/lines directly.
- **Leave close as the only period control.** Re-opening a mis-closed period
  (`periods/<id>/reopen/`) and permanently sealing one (`periods/<id>/lock/`) are
  first-class endpoints, each behind its own CRITICAL key (§3/§4).

## 2. Domain model

No models of its own — dataclasses only (`TrialBalance`, `IncomeStatement`(+Compare),
`BalanceSheet`(+Sections), `CashFlowStatement`, `StatementOfChangesInEquity`,
`StatutoryPack`, `AgingReport`, `ARReconciliation`, `CustomerStatement`,
`AnalyticsSlice`, `BudgetVarianceReport`, `CloseChecklist`). `ReportTable`
(`exports.py:26`) is the export envelope.

## 3. Endpoint map

All require `?entity=`; all reads use `finance.report.view` (dashboard included);
close uses `finance.period.close`.

| Endpoint | Report | Notes |
|---|---|---|
| `reports/trial-balance/` | `trial_balance` | `?period=`; cumulative as-of (latest period row per account) |
| `reports/income-statement/` | `income_statement(_compare)` | period P&L; compare adds budget + prior-year columns |
| `reports/balance-sheet/` | `balance_sheet(_sections)` | as-of; assets = liabilities + equity check |
| `reports/cash-flow/` | `cash_flow_statement` | indirect-ish classification via `_classify_cash_flow` heuristics |
| `reports/changes-in-equity/` | `statement_of_changes_in_equity` | equity movements + net income |
| `reports/statutory-pack/` | `statutory_pack` | IFRS-for-SMEs lines via `Account.ifrs_line` (blank falls back to type default) |
| `reports/ar-aging/` | `ar_aging` | buckets by days overdue (document-level) |
| `reports/ar-reconciliation/` | `reconcile_ar` | AR sub-ledger vs control account |
| `reports/customer-statement/` | `customer_statement` | `?customer=&start=&end=`; running balance |
| `reports/analytics-slice/` | `analytics_slice` | `?axis=cost_center|<dimension>`; reads posted JournalLines |
| `reports/dashboard/` | `dashboard.py` | KPI cards + sparklines + AR blocks in one payload |
| `GET periods/<id>/checklist/` | `close_checklist` | preview, no side effects |
| `POST periods/<id>/close/` | `close_period` | body: `soft?`, `force?`, `run_depreciation?` (default true); key `finance.period.close` |
| `POST periods/<id>/reopen/` | `reopen_period` | CLOSED/SOFT_CLOSED → OPEN (LOCKED refused); key `finance.period.reopen` (CRITICAL) |
| `POST periods/<id>/lock/` | `lock_period` | CLOSED → LOCKED, **irreversible**; key `finance.period.lock` (CRITICAL) |

**Exports:** every tabular report accepts `?export=csv|xlsx|pdf` (`_maybe_export`,
`views.py:994` → `exports.render`); the parameter is `export`, **not** `format`
(DRF reserves `?format=`). Unknown format → 400.

## 4. Lifecycle / state machine (period close)

```
OPEN ──close(soft)──▶ SOFT_CLOSED ──close──▶ CLOSED ──lock──▶ LOCKED (irreversible)
  ▲                        │                    │
  └────────── reopen ◀─────┴────────────────────┘
```
`close_period` (`close.py:160`): refuses CLOSED/LOCKED; posts due depreciation first
(`run_depreciation=true`, into SOFT_CLOSED via `allow_restricted`); runs the
checklist; **blocking failures raise unless `force`** (a forced close is audited as
such).

**Checklist** (`close_checklist`, `close.py:60`): ① trial balance balances
(blocking), ② no draft journals in the period (warning only), ③ AR sub-ledger
reconciles to control (blocking), ④ all due depreciation posted (blocking) — plus
injectable `extra_checks` from other apps (procurement AP/GR-IR).

## 5. Calculations (the ones that bite)

- **Trial balance**: one row per account = its **latest period's** cumulative
  closing (`opening + movement`) — summing opening+movement across every period
  would double-count roll-forwards (`reports.py:60`).
- **Net income** for BS/SoCE: income minus expense movement signed to normal
  balances; the balance sheet folds current-year P&L into equity so it balances.
- **Cash-flow classification** (`_classify_cash_flow`, `reports.py:1162`): heuristic
  by account type/code (operating/investing/financing) — a custom chart may need its
  codes to follow the seeded ranges to classify well.
- **Statutory pack**: groups by `Account.ifrs_line`, falling back to a per-type
  default line when blank.

## 6. What posting does to the ledger

Reports: **nothing**. Close: the depreciation auto-posting (one compound journal —
see `finance_fixed_assets` §6) and the period-status transition + audit rows
(`PERIOD_CLOSED` / `PERIOD_REOPENED` / `PERIOD_LOCKED`). A forced close is recorded
as "(forced over checklist failures)".

## 7. Worked example

`GET reports/trial-balance/?entity=LEKKI&export=xlsx` → an xlsx attachment of the
TB. `GET periods/8/checklist/` → `{passed: false, items: [... ar_reconciled: false
(sub-ledger 64,500 vs control 60,000)]}` → fix, then `POST periods/8/close/ {}` →
depreciation posted, checklist green, period CLOSED.

## 8. Gotchas / known limitations

- ✅ **Reopen and lock are now routed** (`periods/<id>/reopen/` and `…/lock/`), each
  behind its own CRITICAL key (`finance.period.reopen` / `.lock`). Lock remains
  deliberately irreversible; treat both keys as top-tier privileges.
- **`force` close is powerful** — it overrides *blocking* integrity failures
  (unbalanced TB included). It's audited, but treat `finance.period.close` as a
  highly privileged key.
- **Dashboard + several AR reports are Python-heavy** (walk documents/lines);
  acceptable at current scale, same O(n) caveat as dunning had.
- **Cash-flow buckets are heuristic** — verify against a customised chart.
- Reports return live JSON; nothing is cached/persisted (a snapshot per close could
  be a future need for auditors).

## 9. Permissions & tenant isolation

- Reads: `finance.report.view` (one key for the whole surface — no per-statement
  granularity). Close: `finance.period.close` (CRITICAL).
- Every view resolves the entity first; report functions all filter by entity. ✅
- Exports render the same rows the JSON returns — no extra fields leak.

## 10. Code map

| File | Responsibility |
|---|---|
| `reports.py` | every statement/report dataclass + function |
| `dashboard.py` | the composed Finance-overview payload |
| `exports.py` | `ReportTable`, csv/xlsx/pdf renderers, `render` |
| `close.py` | `close_checklist`, `close_period`, `run_period_depreciation`, `reopen_period`, `lock_period` |
| `views.py` | the report views + `_maybe_export` + `PeriodCloseView` |

## 11. Test coverage & gaps

Existing: statement endpoints match service output; TB export formats; unknown
export format rejected; period-close checklist flow; income-statement compare.

Worth asserting: 403 (report.view vs period.close); cross-tenant; forced-close audit
trail; checklist blocking vs warning behaviour; the reopen/lock endpoint gap
(documented); statutory-pack fallback lines.
