# finance_dunning

Automated **overdue-invoice reminders**: a `DunningPolicy` is a ladder of escalating
`DunningStage` rungs; a dunning *run* raises a `DunningNotice` for each overdue
invoice at the highest rung it qualifies for. Like payment plans, this is a
**communications overlay — it never posts to the GL**; it tracks *intent and
outcome* and leaves the actual emailing to an outer notifications service.

Routes (mounted at `/v1/finance/`): `dunning-policies/…`, `dunning/generate/`,
`dunning/summary/`, `dunning-notices/…`. (The invoice drawer's *Send reminder* —
`invoices/<pk>/remind/` — lives in `finance_invoicing_ar` but calls in here.)

---

## 1. What it is (and what it is NOT)

- **`DunningPolicy`** (`models/dunning.py:27`): a named ladder per entity. At most
  **one default** per entity (conditional unique constraint); a run uses the named
  policy, else the active default.
- **`DunningStage`** (`models/dunning.py:58`): one rung — `level`, `min_days_overdue`,
  `channel` (comma-separated `DunningChannel`), and a `message` template.
- **`DunningNotice`** (`models/dunning.py:91`): one reminder for one overdue invoice
  at one level. Keyed **unique per `(invoice, level)`** so re-running never
  duplicates a rung the customer already got.

**This does NOT:**
- **Post to the GL.** No journal — it's a comms overlay (`models/dunning.py:97`).
- **Actually send email/in-app.** `vs_finance` records the *intent* (a `PENDING`
  notice) and the *outcome* (`SENT`); an **outer notifications service reads
  `PENDING` notices and dispatches them** (`dunning.py:11`, `DunningChannel`
  docstring `constants.py:240`). "Send" here = flip to `SENT` + audit (§6/§8).
- **Hit every rung.** A run picks the **highest** qualifying stage, not each one in
  turn — infrequent runs can skip levels (§8).

## 2. Domain model

| Model | File | Key fields | Constraints |
|---|---|---|---|
| `DunningPolicy` | `models/dunning.py:27` | `name`, `is_active`, `is_default` | `unique(entity, name)`; **≤1 default/entity** (partial unique) |
| `DunningStage` | `:58` | `level` (1-based), `name`, `min_days_overdue`, `channel`, `message` | `unique(policy, level)`; ordered by `level` |
| `DunningNotice` | `:91` | `customer`, `invoice`, `policy`, `stage`, `level`, `notice_date`, `days_overdue`, `amount_due`, `channel`, `message`, `notice_status`, `sent_at` | `unique(invoice, level)` |

- Money is kobo (`amount_due` snapshots the invoice balance at generation).
- **`DunningNoticeStatus`** (`constants.py:249`): `PENDING → SENT`; `RESOLVED`
  (invoice settled after the notice) / `CANCELLED` (withdrawn).
- **`DunningChannel`** (`constants.py:239`): `EMAIL`, `IN_APP` (no SMS — see
  [[feedback-no-sms]]); recorded, not acted on, by this app.
- **Default ladder** (`DEFAULT_STAGES`, `dunning.py:33`): L1 *Friendly reminder*
  ≥1d, L2 *Second reminder* ≥14d, L3 *Final notice* ≥30d, all EMAIL.

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /dunning-policies/` | `finance.dunning.view` | List policies (+ stages), un-paginated | — | `DunningPolicySerializer[]` |
| `POST /dunning-policies/` | `finance.dunning.manage` | Create a policy; `use_default:true` seeds the standard ladder | `name`, `is_active?`, `is_default?`, `stages:[{level,name,min_days_overdue,channel,message}]?` **or** `use_default:true` | `201` policy |
| `GET /dunning-policies/<pk>/` | `finance.dunning.view` | One policy | — | detail |
| `PATCH /dunning-policies/<pk>/` | `finance.dunning.manage` | Update name/active/default; `stages` **replaces** the ladder | `name?`, `is_active?`, `is_default?`, `stages?` | policy |
| `POST /dunning/generate/` | `finance.dunning.generate` | Run a policy → raise notices | `as_of?`, `policy?` (id/name), `customer?` | `{created, notices[]}` |
| `GET /dunning/summary/` | `finance.dunning.view` | Open-receivable aging buckets | — | `{due_soon, overdue_1_30, overdue_31_60, overdue_60_plus}` |
| `GET /dunning-notices/` | `finance.dunning.view` | List notices (paginated). Query: `status`, `customer`, `invoice` | — | paginated `DunningNoticeSerializer` |
| `GET /dunning-notices/<pk>/` | `finance.dunning.view` | One notice | — | detail |
| `POST /dunning-notices/<pk>/send/` | `finance.dunning.send` | Mark a `PENDING` notice `SENT` | — | notice |
| `POST /dunning-notices/<pk>/cancel/` | `finance.dunning.send` | Withdraw a notice | `reason?` | notice |

## 4. Lifecycle / state machine

```
Notice:  (generate/remind) ─▶ PENDING ──send──▶ SENT
                               │  │                │
                               │  └── invoice settled ──▶ RESOLVED
                               └────── cancel ─────────▶ CANCELLED
```
- **Run** (`generate_dunning`): for each posted, not-fully-paid invoice with a
  positive balance, measure days-overdue and raise a `PENDING` notice at the
  highest qualifying stage — **unless** a notice already exists for that
  `(invoice, level)`.
- **Auto-resolve:** every run first flips any `PENDING`/`SENT` notice whose invoice
  is now settled to `RESOLVED` (`_resolve_settled`, `dunning.py:208`).
- **Per-invoice reminder** (`remind_invoice`): reuses the `(invoice, level)` notice
  (creating or **reactivating** a `CANCELLED`/`RESOLVED` one back to `PENDING`) and
  sends it by default.
- **Send/cancel** are idempotent on their terminal states.

## 5. Calculations

```
days_overdue = (as_of − (invoice.due_date or invoice.invoice_date)).days   # >0 = overdue
stage        = highest stage with min_days_overdue ≤ days_overdue           # _stage_for, dunning.py:84
```
`generate_dunning` skips invoices with `days_overdue ≤ 0`; `remind_invoice` clamps
to `0` and falls back to the **gentlest** stage (`stages[0]`) when not yet overdue.

**Summary buckets** (`DunningSummaryView`), by days past `due_date`:
`due_soon` (−7…0), `overdue_1_30`, `overdue_31_60`, `overdue_60_plus` — each
`{amount, count}` of outstanding balance.

## 6. What posting does to the ledger

**Nothing.** Dunning never raises a journal. The only "side effects" are the notice
rows, status transitions, and `FinanceAuditLog` events
(`DUNNING_RUN_GENERATED`, `DUNNING_NOTICE_SENT`, `DUNNING_NOTICE_CANCELLED`).

**"Sent" ≠ delivered.** `mark_notice_sent` (`dunning.py:222`) flips
`PENDING → SENT`, stamps `sent_at`, writes an audit row — it does **not** call any
mail/notification service. Delivery is the job of an external notifications worker
that reads `PENDING` (or `SENT`) notices and dispatches per `channel`. So `SENT`
means "handed off / marked dispatched", not "the customer received it".

## 7. Worked example

Seed + run:
```
POST /v1/finance/dunning-policies/?entity=LEKKI   { "use_default": true }
POST /v1/finance/dunning/generate/?entity=LEKKI   { "as_of": "2026-06-30" }
```
An invoice 40 days overdue → one `PENDING` `DunningNotice` at **L3 Final notice**
(highest rung ≥30d), `amount_due` = its balance, `channel:"EMAIL"`. Re-running the
same day creates nothing (the `(invoice, L3)` notice exists). After the customer
pays, the next run flips that notice to `RESOLVED`.

`POST /dunning-notices/<id>/send/` → `SENT` + `sent_at`; the notifications worker
then emails it.

## 8. Gotchas / known limitations

- ⚠️ **`send` doesn't email** (§6) — it records dispatch intent; an external service
  does the actual sending. Don't treat `SENT` as proof of delivery.
- **Runs raise only the highest qualifying rung.** An invoice that goes 0 → 40 days
  between runs jumps straight to L3; it never gets an L1/L2 notice. Frequent
  (e.g. daily) runs avoid skips; the `(invoice, level)` uniqueness is what prevents
  duplicates, not a "fire every rung" loop.
- **`generate_dunning` and `dunning/summary/` iterate posted invoices in Python**
  (`balance_due` is a property) — fine for typical AR, but O(open invoices) per
  call; watch it on very large ledgers.
- **Policy list is un-paginated** (few rows, acceptable); notices list **is**
  paginated.
- **PATCH `stages` replaces the whole ladder** (delete + recreate) — not a merge;
  send the full set.
- A `CANCELLED`/`RESOLVED` notice can be **reactivated** to `PENDING` by
  `remind_invoice` — re-running `generate` will not (it only skips on existence).

## 9. Permissions & tenant isolation

- Verbs: `finance.dunning.view` (reads/summary), `finance.dunning.manage`
  (policy create/update), `finance.dunning.generate` (run), `finance.dunning.send`
  (send/cancel a notice, and the invoice `remind/` action).
- Every view resolves the entity then `filter(entity=…, pk=…)` (`_policy`/`_notice`
  bases); `_resolve_customer`/`_resolve_invoice` are entity-scoped → another
  tenant's policy/notice/invoice id → 404. ✅
- Serializers expose ids/codes/money/dates/message — the `message` is operator-
  authored reminder text, not secrets.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/dunning.py` | `DunningPolicy`, `DunningStage`, `DunningNotice` |
| `dunning.py` | `ensure_default_policy`, `generate_dunning`, `remind_invoice`, `mark_notice_sent`, `cancel_notice`, `_resolve_settled`, `_stage_for` |
| `views_ar.py` | policy / generate / summary / notice views (+ `InvoiceRemindView`) |
| `serializers.py` | `DunningPolicySerializer`, `DunningStageSerializer`, `DunningNoticeSerializer` |
| `constants.py` | `DunningChannel`, `DunningNoticeStatus`; `DEFAULT_STAGES` in `dunning.py` |

## 11. Test coverage & gaps

Existing (`tests.py`, `DunningTests`): generate is idempotent per `(invoice, level)`;
not-yet-due invoices are skipped; a settled invoice flips its notice to `RESOLVED`
and no new one is raised.

Worth asserting if not already:
- **403** per verb; **cross-tenant** policy/notice id → 404.
- `_stage_for` picks the highest qualifying rung; the level-skip behaviour (§8) is
  intended.
- `≤1 default policy/entity` enforced; `use_default` seeds the standard ladder
  idempotently.
- `send` is idempotent once `SENT`; `cancel` idempotent on terminal; `remind_invoice`
  reactivates a `CANCELLED`/`RESOLVED` notice to `PENDING`.
- Summary bucket boundaries (−7…0 / 1-30 / 31-60 / 60+).
- Empty-list shape on a fresh entity.
