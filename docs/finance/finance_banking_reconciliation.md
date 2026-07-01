# finance_banking_reconciliation

Bank reconciliation: import a bank statement, then **pair each statement line to the
matching GL cash-account journal line** so the books agree with what the bank
reported. The GL remains the single source of truth — a `BankAccount` is just
metadata mapped 1:1 to a cash account, and reconciliation is a *pairing overlay*
that posts nothing to the ledger **except** when it books a charge/credit the books
didn't know about.

Routes (mounted at `/v1/finance/`): `bank-accounts/…`,
`bank-accounts/<pk>/{statement-lines,auto-reconcile,book-lines,reconcile/complete}/`,
`statement-lines/<pk>/{match,adjust,unmatch}/`.

---

## 1. What it is (and what it is NOT)

- **`BankAccount`** (`models/ops.py:42`): a real account mapped **1:1** to a GL cash
  account (`gl_account`, OneToOne). Adds bank name/number; `is_primary` (≤1/entity).
- **`BankStatementLine`** (`models/ops.py:86`): one imported line, `amount` **signed
  from our side** — `+inflow` (a GL **debit** to cash), `−outflow` (a GL credit).
- **`BankStatement`** (`models/ops.py:149`): the imported batch (opening → closing).
- **`BankReconciliation`** (`models/ops.py:187`): a snapshot of book vs statement
  balance at a point in time.

**This does NOT:**
- **Hold a second balance.** The bank account is not a source of truth — "money only
  ever moves via journals against `gl_account`" (`models/ops.py:42`). The book
  balance is always the GL cash account's posted balance.
- **Post to the GL when you match.** Import, auto-match, manual match, complete — all
  just set pairings/snapshots. The **only** ledger-moving action is
  `post_bank_adjustment` (booking a missing charge/interest), and `unmatch` reverses
  that adjustment if the match created one (§6).
- **Do fuzzy/partial matching.** Auto and manual match require the **exact signed
  amount** to be equal; a bank line that aggregates several receipts won't
  auto-match (§8).

## 2. Domain model

| Model | File | Key fields | Notes |
|---|---|---|---|
| `BankAccount` | `models/ops.py:42` | `gl_account` (1:1), `name`, `bank_name`, `account_number`, `currency`, `is_active`, `is_primary` | `unique(entity, name)` |
| `BankStatementLine` | `:86` | `txn_date`, `amount` (**signed** kobo), `status`, `matched_line` (→JournalLine), `match_source`, `adjusting_journal`, `external_id`, `reconciled_at` | `unique(bank_account, external_id)` when set |
| `BankStatement` | `:149` | `statement_date`, `opening_balance`, `closing_balance`, `status` | batch of lines |
| `BankReconciliation` | `:187` | `as_of_date`, `book_balance`, `statement_balance`, `difference`, `matched_count`, `status` | history snapshot |

- **Enums** (`constants.py:277`): line `UNMATCHED / MATCHED / IGNORED`; match source
  `AUTO / MANUAL / ADJUSTMENT`; statement `UPLOADED / RECONCILED`; recon
  `BALANCED / OUT_OF_BALANCE`.
- Money is kobo; statement `amount` and GL `debit−credit` are both **signed** so they
  compare directly.

## 3. Endpoint map

All require `?entity=`. Gate: `IsAuthenticatedAndActive & HasRBACPermission`.

| Method + path | permission key | what it does | request body | response |
|---|---|---|---|---|
| `GET /bank-accounts/` | `finance.bankaccount.view` | List. Query: `is_active` | — | `BankAccountSerializer[]` (un-paginated) |
| `POST /bank-accounts/` | `finance.bankaccount.create` | Create (maps to a GL cash account) | `name`, `gl_account`, `bank_name?`, `account_number?`, `currency?`, `is_primary?` | `201` account |
| `GET /bank-accounts/<pk>/` | `finance.bankaccount.view` | Detail + metrics (book/stmt balance, unreconciled), recent txns, statements, recon history | — | detail |
| `PATCH /bank-accounts/<pk>/` | `finance.bankaccount.update` | Edit settings | `name?`, `bank_name?`, `account_number?`, `currency?`, `is_active?`, `is_primary?` | account |
| `GET /bank-accounts/<pk>/statement-lines/` | `finance.bankaccount.view` | List lines (**paginated**). Query: `status` | — | paginated `BankStatementLineSerializer` |
| `POST /bank-accounts/<pk>/statement-lines/` | `finance.bankaccount.import` | **Import** a batch. De-dups on `external_id`; rows without one that match an existing line are held back as *suspected duplicates* unless `force` | `lines:[{txn_date, amount (signed), description?, reference?, external_id?}]`, `force?`, `statement_date?`, `period_label?`, `opening_balance?`, `closing_balance?` | `201` `{imported[], suspected_duplicates[]}` |
| `POST /bank-accounts/<pk>/auto-reconcile/` | `finance.bankaccount.reconcile` | Auto-match by amount+date | `tolerance_days?` (default 4) | matched lines |
| `GET /bank-accounts/<pk>/book-lines/` | `finance.bankaccount.view` | Unmatched GL cash lines (the "book" side), **paginated** | — | paginated `{id, date, description, reference, amount(signed)}` |
| `POST /bank-accounts/<pk>/reconcile/complete/` | `finance.bankaccount.reconcile` | Record a reconciliation snapshot | — | `201` `BankReconciliationSerializer` |
| `POST /statement-lines/<pk>/match/` | `finance.bankaccount.reconcile` | Manually pair to a cash journal line | `journal_line` (id) | line |
| `POST /statement-lines/<pk>/adjust/` | `finance.bankaccount.reconcile` | **Book** an unrecorded line + match it | `counter_account?`, `counter_code?`, `narration?` | `201` line |
| `POST /statement-lines/<pk>/unmatch/` | `finance.bankaccount.reconcile` | Undo a match (reverses adjustment if any) | — | line |

## 4. Lifecycle / state machine

```
Statement line:  UNMATCHED ──match / auto-match──▶ MATCHED
                      │  └──adjust (book + match)──▶ MATCHED (source=ADJUSTMENT)
                      └── (IGNORED — defined, not set by any endpoint yet)
                 MATCHED ──unmatch──▶ UNMATCHED  (adjusting journal reversed if present)

Statement: UPLOADED ──(no unmatched lines left)──▶ RECONCILED
Recon run:  BALANCED | OUT_OF_BALANCE   (per snapshot)
```
- **Import** creates `UNMATCHED` lines under a new `UPLOADED` statement.
- **auto_reconcile / match / adjust** flip lines to `MATCHED` (with the source).
- A statement auto-flips to `RECONCILED` once it has lines and none are `UNMATCHED`
  (`_record_reconciliation`, `banking.py:184`).

## 5. Calculations

**Signed amounts** — a statement line and a cash journal line compare directly:
```
statement.amount : +inflow (GL debit to cash) / −outflow (GL credit)
_signed_gl(line) : line.debit − line.credit                (banking.py:108)
```
**Auto-match rule** — `auto_reconcile` (`banking.py:130`), greedy & conservative:
```
match sline ↔ first unconsumed posted cash line where
    _signed_gl(gl) == sline.amount               (exact signed amount)
    AND |gl.entry.date − sline.txn_date| ≤ tolerance_days   (default 4)
each GL line consumed at most once; ambiguous → left for a human
```
**Balances** — `book_balance = gl_account_balance(gl_account)` (posted GL, signed to
normal side, `banking.py:39`); `statement_balance` = latest statement's
`closing_balance` (`banking.py:178`); `difference = book − statement` →
`BALANCED` if 0 else `OUT_OF_BALANCE`. On import, `closing = opening + Σ amounts`
when not supplied.

## 6. What posting does to the ledger

**Reconciliation itself posts nothing.** Import, auto-match, manual match, and
complete only write pairings and snapshot rows — the GL is untouched (the money was
already booked when the receipt/payment posted). Matching just *asserts* "this bank
line is that ledger movement."

**The one ledger-moving action** — `post_bank_adjustment` (`banking.py:287`), for a
line the books don't yet know about (a bank charge or interest), direction by sign:
```
outflow (amount < 0):  Dr counter (default 5500 Bank Charges)   Cr cash
inflow  (amount > 0):  Dr cash                                  Cr counter
```
It posts the adjusting journal, then matches the statement line to the **new** cash
line with `match_source = ADJUSTMENT` and stores `adjusting_journal`.

**`unmatch`** (`banking.py:250`): a plain match just drops the pairing (no ledger
effect); a match that booked an adjustment **reverses that journal** (mirror entry
that nets to zero) so unmatching never leaves the ledger out of step.

## 7. Worked example

Import + reconcile a ₦500 bank charge the books missed:
```
POST /bank-accounts/12/statement-lines/?entity=LEKKI
  { "lines": [ { "txn_date": "2026-06-30", "amount": -50000,
                 "description": "Account maintenance fee" } ] }
```
→ one `UNMATCHED` line, `amount −50000`. `POST /auto-reconcile/` finds no GL line
(the charge was never booked) → still unmatched. `POST /statement-lines/<id>/adjust/`
→ books `Dr 5500 Bank Charges 50000 / Cr 1100 Cash 50000`, flips the line to
`MATCHED (ADJUSTMENT)`. `POST /reconcile/complete/` snapshots book vs statement.
Later `POST /statement-lines/<id>/unmatch/` → reverses that adjusting journal and
returns the line to `UNMATCHED`.

## 8. Gotchas / known limitations

- **Exact-amount matching only** — auto and manual match require `_signed_gl ==
  statement.amount`. A single bank deposit that lumps several receipts (or a partial
  clearing) won't auto-match; you'd match manually against an equal line or book an
  adjustment. No split/many-to-one matching.
- ✅ **Ambiguous auto-match now skipped** (was greedy first-match) — `auto_reconcile`
  only auto-pairs when **exactly one** GL candidate fits amount+date; ties are left
  for a human.
- ✅ **Re-import guard** (was silent duplication) — a row without an `external_id`
  that matches an existing line on `(txn_date, amount, description, reference)` is
  held back as a *suspected duplicate* and returned in the response; `force=true`
  imports anyway. Two identical rows in one *fresh* batch are both kept.
- **`IGNORED` is defined but no endpoint sets it** — there's no "ignore this line"
  action yet; lines are either UNMATCHED or MATCHED via the API.
- ✅ **`book-lines/` now paginates** (was capped at 200).
- **Import doesn't validate** opening/closing against the GL — it records what the
  bank said; the difference surfaces later in the reconciliation snapshot.

## 9. Permissions & tenant isolation

- Verbs: `finance.bankaccount.{view, create, update, import, reconcile}` — importing
  and reconciling are distinct from viewing/editing.
- Views resolve the entity then `filter(entity=…, pk=…)`; statement-line actions
  filter `bank_account__entity=entity`, and `match/` validates the journal line is in
  this entity **and** on this bank account's `gl_account` with an equal signed amount
  (`banking.py:227`) → no cross-tenant or wrong-account pairing. ✅
- Serializers expose ids/codes/money/dates — `account_number` is bank metadata (not a
  secret in this model), but review if it's ever treated as sensitive (FLS).

## 10. Code map

| File | Responsibility |
|---|---|
| `models/ops.py` | `BankAccount`, `BankStatementLine`, `BankStatement`, `BankReconciliation` |
| `banking.py` | `import_statement_lines`, `auto_reconcile`, `match_line`, `unmatch_line`, `post_bank_adjustment`, `gl_account_balance`, `statement_balance`, `_record_reconciliation`, `complete_reconciliation` |
| `views_ops/banking.py` | bank-account CRUD + statement-line / reconcile / match / adjust / unmatch views |
| `serializers.py` | `BankAccountSerializer`, `BankStatementSerializer`, `BankStatementLineSerializer`, `BankReconciliationSerializer` |
| `constants.py` | `BankLineStatus`, `BankMatchSource`, `BankStatementStatus`, `BankReconStatus`; `BANK_CHARGES_CODE` (5500) |

## 11. Test coverage & gaps

Existing (`tests.py`, `BankReconciliationTests`): a bank-charge adjustment books
`Dr 5500 / Cr cash` and matches; unmatch drops the pairing and reverses the
adjusting journal.

Worth asserting if not already:
- **403** per verb (esp. `import` vs `reconcile` vs `view`); **cross-tenant**
  bank-account / statement-line / journal-line id → 404 / rejected.
- Import idempotency on `external_id`; re-import without ids duplicates (document it).
- `auto_reconcile`: matches on exact signed amount within tolerance; leaves
  ambiguous/partial lines unmatched; each GL line consumed once.
- `match_line` rejects a wrong-account or amount-mismatched journal line.
- Adjustment direction by sign (inflow vs outflow) and the `unmatch` reversal.
- Statement flips to `RECONCILED` when its last unmatched line is matched.
- Empty-list shape on a fresh entity.
