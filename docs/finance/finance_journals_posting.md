# finance_journals_posting

The **posting engine** — the heart of the ledger. A `JournalEntry` is a balanced
double-entry transaction; **posting** is the single act that makes it affect
account balances. Every other slice (invoices, payroll, depreciation, bank
charges …) ultimately moves money by handing a draft journal to this engine, so
the rules here are enforced *once* and cannot be bypassed.

Routes covered (mounted at `/v1/finance/`):
`journals/`, `journals/summary/`, `journals/<id>/`, `journals/<id>/post/`,
`journals/<id>/reverse/`, `direct-entries/`.

---

## 1. What it is (and what it is NOT)

- A **`JournalEntry`** (`models/gl.py:359`) is a numbered, entity-scoped document
  (`CFX-<ENTITY>-JNL-<year>-<seq>`) whose **`JournalLine`** rows must net to zero
  (Σdebit = Σcredit) before it can post.
- **Posting** (`posting.post_journal`, `posting.py:148`) is the *only* sanctioned
  way to make a journal affect balances. It runs the guards, updates the
  denormalised per-period balances, stamps the entry `POSTED`, and writes an
  audit row — **all in one transaction**.
- **Reversal** (`posting.reverse_journal`, `posting.py:229`) is the only
  sanctioned way to undo: it raises a *new* mirror-image journal, leaving the
  original permanently on record marked `REVERSED`.

**This slice does NOT:**
- **Create draft journals from raw lines via a public CRUD endpoint.** There is
  no `POST /journals/`. A normal journal is a *side effect* of a sub-ledger
  action (invoice/payment/payroll). The one place a caller hands in raw lines is
  `POST /direct-entries/` (openings/capital/adjustments).
- **Let you post by flipping `status`.** `status` is stamped only inside the
  posting service; setting it by hand bypasses balance maintenance and audit
  (`posting.py:106` warns this explicitly).
- **Edit posted history.** Lines are immutable once posted; corrections are
  reversals (`models/gl.py:425` docstring; enforced by the reverse-only flow).
- **Tolerate rounding.** Amounts are integer **kobo**; balance equality is exact,
  "off by one kobo is wrong" (`posting.py:74`).

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `JournalEntry` | `models/gl.py:359` | `date` (drives the period), `period` (FK), `source` (`JournalSource`), `currency`, `fx_rate`, `narration`, `reference`, `status`, `posted_at/by`, `reverses` (self-O2O → the entry it cancels) |
| `JournalLine` | `models/gl.py:425` | `account` (FK), `debit`, `credit` (kobo, **one-sided**), `description`, `cost_center` (FK, optional), `dimensions` (JSON), `line_no` |
| `AccountBalance` | `models/gl.py:478` | denormalised `(account, period)` running totals — the fast aggregate posting maintains |

- **DB-enforced invariants** on `JournalLine` (`models/gl.py:460`):
  `ck_finance_line_one_sided` (`debit=0 OR credit=0`) and
  `ck_finance_line_non_negative`. The balance guard catches the zero/zero case at
  post time.
- `JournalEntry` inherits entity scoping + numbering from `FinanceDocument`
  (see `finance_chart_of_accounts` §1); `DOC_TYPE = JOURNAL`.
- **`source`** (`JournalSource`, `constants.py:136`) is `MANUAL`, `SALES`,
  `PURCHASE`, `BANK`, `SYSTEM` (reversals), `OPENING` (direct entries), etc. —
  for filtering/audit only, never for posting logic.

## 3. Endpoint map

All require `?entity=<id|code>`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /journals/?entity=` | `finance.journal.view` | List entries. Query: `status`, `source`, `date_from`, `date_to`, `search` (doc#/narration/reference). Annotates `_total_debit` (1 query) | — | paginated `JournalEntryListSerializer` |
| `GET /journals/summary/?entity=` | `finance.journal.view` | Status counts + posted/reversed totals (status-tab footer). Honours `source`/`date`/`search` | — | `success_response` (`total`, `by_status`, `posted_total`, `reversed_total`) |
| `GET /journals/<id>/?entity=` | `finance.journal.view` | One entry **with its lines** | — | `JournalEntryDetailSerializer` |
| `POST /journals/<id>/post/?entity=` | `finance.journal.post` | Post an existing **draft** | — | posted `JournalEntryDetailSerializer` |
| `POST /journals/<id>/reverse/?entity=` | `finance.journal.reverse` | Reverse a **posted** entry | — | `201` the **reversing** entry |
| `POST /direct-entries/?entity=` | `finance.directentry.post` | Create **and post** a journal from raw lines | `date?`, `narration?`, `reference?`, `lines:[{account (code), debit?, credit?, cost_center? (code/id), dimensions? ({axis: value})}]` (kobo) | `201` posted `JournalEntryDetailSerializer` |

> **Field notes (verified against the serializers):**
> - Direct-entry `lines[].account` is an **account code string** resolved within
>   the entity (`DirectEntryLineSerializer`, `serializers.py:263`) — *not* a pk.
> - A line is **one-sided**: setting both `debit` and `credit` is rejected by the
>   serializer (`serializers.py:270`) *and* by a DB check constraint.
> - The serializer pre-validates the entry **balances and is non-zero**
>   (`serializers.py:285`) before the engine ever runs.
> - A line may also carry **`cost_center`** (code/id) and **`dimensions`**
>   (`{axis: value}`); both are validated against the entity (the dimension value
>   must be in the axis's allow-list) and carried onto the GL line. See
>   `finance_cost_centers` §6.

## 4. Lifecycle / state machine

```
DRAFT ──post_journal──▶ POSTED ──reverse_journal──▶ REVERSED   (original)
                            └─ creates new SYSTEM journal ─▶ POSTED   (the reversal)
```
- A draft is produced by a sub-ledger service or by `direct-entries` (which
  posts immediately). `POST /journals/<id>/post/` is for drafts that were saved
  un-posted (e.g. an invoice created with `post=false`).
- `post_journal` is **idempotent-guarded**: re-posting a `POSTED` entry raises
  rather than double-counting (`posting.py:188`). A `REVERSED`/`CANCELLED` entry
  cannot be posted (`posting.py:192`).
- `reverse_journal` refuses anything not `POSTED`, and refuses to reverse twice
  (`reversed_by` already set) (`posting.py:242`, `:247`).

## 5. Calculations & guards

All in `posting.py`. There is no "rate" maths here — the engine's job is
**integrity**, computed by three guards + the balance roll-forward.

**(a) Balance guard** — `ensure_balanced` (`posting.py:74`):
```
Σ debit_kobo == Σ credit_kobo      # exact integer equality, no tolerance
```
Totals via `sum_sides` (`posting.py:84`).

**(b) Period guard** — `ensure_period_open` (`posting.py:44`), using
`constants.py:27`:
```
period is None                       -> reject (nothing posts without a period)
status ∈ {CLOSED, LOCKED}            -> reject (PERIOD_POSTING_BLOCKED)
status == SOFT_CLOSED                -> reject UNLESS allow_restricted (close auto-entries)
status != OPEN (unknown/unset)       -> reject (fail closed, never guess)
```

**(c) Account guard** — every line's account must be `is_active AND is_postable`,
else `InactiveAccountError` (`posting.py:206`).

**(d) Balance roll-forward** — `_apply_to_balances` (`posting.py:127`), the only
write to the read-model:
```
for each line:
  AccountBalance(account, period)  # select_for_update, get_or_create
  debit_total  += sign * line.debit     # sign=+1 posting, −1 unposting
  credit_total += sign * line.credit
```
Truth stays in the immutable lines; `AccountBalance` is the denormalised
aggregate kept in step **inside the same transaction**.

## 6. What posting does to the ledger

This *is* the posting step — the canonical sequence `_post_journal_atomic`
(`posting.py:174`), all under `@transaction.atomic`:

1. Reject if already `POSTED` / `REVERSED` / `CANCELLED`.
2. `ensure_period_open(entry.period)`.
3. Require ≥1 line; `ensure_balanced(Σdebit, Σcredit)`.
4. Every line's account active + postable.
5. `_apply_to_balances(sign=+1)` — update `AccountBalance`.
6. Stamp `status=POSTED`, `posted_at`, `posted_by`.
7. Write the `JOURNAL_POSTED` audit row (`audit.record`, `audit.py:45`) — **same
   commit** as 5–6. A posting can never commit without its audit row.

**Rejections are durable.** `post_journal` (the wrapper, `posting.py:148`) catches
any `FinanceError`, and *outside* the rolled-back transaction writes a
`JOURNAL_POST_REJECTED` audit row, then re-raises — so a blocked attempt is still
on the record even though the posting itself rolled back.

**Reversal** (`reverse_journal`, `posting.py:229`) carries **everything** to the
mirror line — account, swapped debit/credit, `cost_center`, `dimensions`,
`line_no` (`posting.py:265`) — so it is a true inverse including analytics. Sub-
ledger postings likewise carry `cost_center` onto their P&L lines (see the
`finance_cost_centers` slice §6). The reversal posts with `source=SYSTEM` into the
original's period unless a `date` is given.

## 7. Worked example

**Direct entry — opening capital** (`POST /v1/finance/direct-entries/?entity=LEKKI`):
```json
{
  "narration": "Owner capital injection",
  "lines": [
    { "account": "1100", "debit": 5000000 },
    { "account": "3000", "credit": 5000000 }
  ]
}
```
→ serializer checks `5,000,000 == 5,000,000` (kobo) and non-zero →
`post_direct_entry` resolves codes `1100`/`3000` to accounts, creates the entry
`source=OPENING`, posts it. Response `201`:
```json
{
  "success": true,
  "message": "Direct entry posted as CFX-LEKKI-JNL-2026-00012.",
  "data": {
    "id": 312, "document_number": "CFX-LEKKI-JNL-2026-00012",
    "date": "2026-06-01", "period": "Jun 2026", "source": "OPENING",
    "status": "POSTED", "narration": "Owner capital injection",
    "total_debit": 5000000, "total_credit": 5000000,
    "lines": [
      { "line_no": 1, "account_code": "1100", "account_name": "Cash & Bank",
        "debit": 5000000, "credit": 0, "debit_naira": "₦50,000.00", "cost_center": null, "dimensions": {} },
      { "line_no": 2, "account_code": "3000", "account_name": "Owner's Equity",
        "debit": 0, "credit": 5000000, "credit_naira": "₦50,000.00", "cost_center": null, "dimensions": {} }
    ]
  }
}
```
**Unbalanced** (`debit 5000000` vs `credit 4000000`) → `400` from the serializer:
`"Entry must balance: debits 5000000 ≠ credits 4000000 (kobo)."` — the engine is
never reached.

## 8. Gotchas / known limitations

- **No draft-create endpoint.** To get a draft you can `POST /journals/<id>/post/`
  on, a sub-ledger flow must have produced it (e.g. invoice `post=false`). You
  can't `POST /journals/` raw lines — only `direct-entries`, which posts at once.
- **`direct-entries` posts immediately** — there is no "save as draft" variant.
- **`source` is not authorization.** It's a label; don't gate logic on it.
- **`fx_rate`/multi-currency** fields exist on the model but this slice's
  endpoints don't compute FX — multi-currency revaluation is a separate concern.
- **`post_journal` re-raises** on a closed period etc.; the typed exception is
  rendered to the standard error envelope by `core.exceptions` — callers see a
  clean `400`/`409`, and a rejection audit row exists.

## 9. Permissions & tenant isolation

- Distinct verbs per action: `finance.journal.view` / `.post` / `.reverse` and
  `finance.directentry.post` — posting and reversing are **not** implied by view.
- Every action resolves the entity first, then `filter(entity=…, id=…)`
  (`views.py:828`, `:854`) → another tenant's journal id returns `NotFound`. The
  list/detail/summary all run through `resolve_entity` (CX-staff-all,
  else `source_school`-scoped). A `?entity=` swap → `404`. ✅
- Audit actor is `request.user` (`views.py:836`), so who-posted/who-reversed is
  attributed.

## 10. Code map

| File | Responsibility |
|---|---|
| `posting.py` | `ensure_period_open`, `ensure_balanced`, `sum_sides`, `resolve_period`, `_apply_to_balances`, `post_journal`/`_post_journal_atomic`, `reverse_journal`, `post_direct_entry` |
| `views.py` | `JournalEntryList/Summary/DetailView`, `JournalPost/ReverseView`, `DirectEntryCreateView` |
| `serializers.py` | `JournalLine/EntryList/EntryDetailSerializer`, `DirectEntryLine/CreateSerializer` |
| `audit.py` | `record` (success row), `record_rejection` (durable reject row) |
| `models/gl.py` | `JournalEntry`, `JournalLine`, `AccountBalance` |
| `constants.py` | `DocumentStatus`, `JournalSource`, `PERIOD_POSTING_BLOCKED/RESTRICTED` |

## 11. Test coverage & gaps

Security-critical first:
- `403` for each verb missing (`view`/`post`/`reverse`/`directentry.post`).
- **Cross-tenant:** posting/reversing/viewing another entity's journal id → `404`.

Engine correctness:
- Balanced vs unbalanced direct entry (exact-kobo guard).
- Post into `OPEN` ✅; into `CLOSED`/`LOCKED` → rejected; into `SOFT_CLOSED` →
  rejected unless `allow_restricted`.
- Re-post a `POSTED` entry → raises (no double count); post a `REVERSED` → raises.
- Inactive/non-postable account line → `InactiveAccountError`.
- `_apply_to_balances` actually moves `AccountBalance`, and reversal restores it
  to net-zero (post then reverse → balances back to start).
- **Durable rejection audit row** written on a blocked post (rolled back txn).
- Reversal carries `cost_center`/`dimensions` (mirror is a true inverse).
- Empty-list shape for `GET /journals/` on a fresh entity (`[]` → `{}`).

> Check `apps/vs_finance/tests.py` for existing coverage before adding.
