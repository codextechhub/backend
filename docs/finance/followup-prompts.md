# Follow-up prompts — finance / payments / procurement

Five self-contained prompts, written 2026-07-30 after the accounting-date class-fix
(backend `8b576eb`, console-fe `f8038a8`, payments follow-up `d8eaeb9`).

Each one is meant to be **pasted whole into a fresh session**. They assume no memory
of the conversation that produced them, and each ends by removing its own entry from
the root `todo.md`.

Suggested order: **3 → 4 → 1 → 5 → 2**. Prompt 3 is small and closes a live hole;
prompt 4 protects the books from silent drift; prompt 1 is the largest; prompt 5 is
operational; prompt 2 can wait until you actually pay a supplier in advance.

| # | Title | Size | Why now |
|---|-------|------|---------|
| 1 | AR "as at" reporting | Large | Historical reports are wrong; needed before any audit or board pack |
| 2 | Vendor advances | Small | Latent — zero prepayments in the books today |
| 3 | Backdated expense / payroll / tax settlements | Small | Live hole, same class as the refund bug |
| 4 | Journal reversal desynchronises the sub-ledger | Medium | Silent, unclearable ledger drift |
| 5 | Unbookable gateway receipts are invisible | Medium | Real customer money can vanish from view |

---

## Prompt 1 — Make AR "as at" reports actually mean "as at"

```
Fix the AR "as at" reporting gap recorded as the first item under "## Undone" in the
backend repo's root todo.md.

BACKGROUND — what is wrong

A report headed "as at 30 June" must show the books as they stood on 30 June. Today
these reports show current numbers with a June date printed on them.

Concretely: a customer is invoiced ₦500,000 on 1 June, does not pay, and settles on
20 September. Run "AR Aging as at 30 June" in October and that customer is absent
entirely — ar_aging (apps/vs_finance/reports.py, ar_aging) excludes any invoice whose
payment_status is currently PAID, and buckets off the CURRENT balance_due. It also
runs the other way: an invoice dated 10 September appears on the June report, because
nothing compares invoice_date to the cutoff. `as_of` is used only to compute
days_overdue. The same shape affects customer_statement in the same module, and
generate_dunning (apps/vs_finance/dunning.py) where a later-dated settlement can
suppress or resolve a reminder for an earlier as-of date.

The posting side of this problem is already fixed — see apps/vs_finance/chronology.py
and its docstring. Do not re-do that work. This is the reporting side only.

THERE IS ALREADY A CORRECT IMPLEMENTATION IN THIS CODEBASE — COPY IT

apps/vs_procurement/reports.py solved exactly this for AP. Read it before writing
anything:
  * _ap_snapshot(entity, as_of=...) reconstructs settlement from posted journals with
    journal__date__lte=as_of, and correctly handles reversals via
    journal__reversed_by__date__gt=as_of;
  * _account_gl_net_as_of(account, as_of) filters journal lines by entry__date__lte;
  * ap_aging passes the cutoff as BOTH the aging clock and the effectiveness cutoff;
  * reconcile_ap passes as_of to BOTH sides, so the control report stays consistent.

Mirror that structure on the AR side. Keep the naming parallel so the two read as one
system.

THE ONE PLACE AR CANNOT SIMPLY COPY AP

AP can reconstruct per-invoice settlement from journals because each vendor payment
journals against its bills. AR cannot: _post_payment_atomic credits AR for the applied
TOTAL in a single line, and allocate_payment journals Dr 2140 / Cr AR at customer
level. There is no per-invoice journal, so per-invoice aging buckets cannot be
rebuilt from the GL alone.

So this half needs a stored date. Add an effective date to the three allocation
tables — PaymentAllocation and CreditNoteAllocation (apps/vs_finance/models/ar.py and
models/adjustments.py) and DebitNoteAllocation — set to
chronology.effective_allocation_date(credit_date, [target_date]), i.e. the later of
the crediting document and the document it settles. receivables.allocate_payment and
credit_notes.allocate_credit_note ALREADY compute exactly this value (they use it to
date the reclassification journal) and then discard it; persist it instead of
recomputing. _apply_payment_subledger / _apply_creditnote_subledger are where the rows
are written.

Backfill in the migration: for existing rows use max(payment_date/note_date,
invoice_date/note_date of the target). State in the migration docstring that this is
a reconstruction, not a record — the true date was never captured.

WHY THIS MUST BE DONE AS ONE PIECE

reconcile_ar compares ar_aging's total_net against _account_gl_net, which sums
AccountBalance across ALL periods with no cutoff. If you date-filter the aging side
alone, the two disagree whenever a future-dated document exists and the control report
starts crying wolf — worse than the current state, because a control report people
learn to ignore is no control at all. Give reconcile_ar the same cutoff, using
procurement's _account_gl_net_as_of as the model.

SCOPE

  * ar_aging: exclude documents dated after the cutoff; rebuild balance at the cutoff
    from allocations effective on or before it, rather than reading balance_due.
    Keep the no-cutoff call path behaving exactly as today (current state) — check how
    _ap_snapshot preserves that contract.
  * customer_statement: same treatment.
  * reconcile_ar: cutoff on both sides.
  * generate_dunning: measure overdue against the state at as_of, and do not resolve a
    notice on the strength of a settlement dated after the run date.
  * Frontend: if any screen presents these as historical, make sure the date it sends
    and the basis it displays now agree. Check
    console-fe/src/pages/protected/finance for the aging/statement/dunning screens.

TESTS

Follow the style of AccountingDateIntegrityTests in apps/vs_finance/tests.py. At
minimum: the ₦500,000 June/September case above; an invoice raised after the cutoff
absent from the earlier report; a partially-allocated receipt aging correctly at two
different cutoffs; reconcile_ar balanced at a historical cutoff with a future-dated
document present; a dunning run for a past date unaffected by a later settlement.

VERIFY

Run the vs_finance, vs_procurement and vs_payments suites (cd apps &&
../cx/bin/python manage.py test vs_finance vs_procurement vs_payments --noinput).
They were 723 green plus 64 payments at the time of writing. If you touch a screen,
run the /verify-design skill and LOOK at the screenshots.

FINALLY

Delete the "As at reports still read current mutable balances" bullet from the
"## Undone" list in the backend root todo.md, and add a "# ..." line to the "## Done"
section following the style of the entries already there. Commit to main (do not
push), staging files explicitly — never `git add -A`.
```

---

## Prompt 2 — Give vendor prepayments somewhere to live

```
Fix the vendor-prepayment gap recorded under "## Undone" in the backend repo's root
todo.md ("Vendor prepayments drive AP negative").

BACKGROUND

Pay a supplier ₦500,000 on 1 March as a deposit; their invoice arrives on 10 March.
_post_vendor_payment_atomic (apps/vs_procurement/payables.py) debits Accounts Payable
for the full gross regardless of what the payment settles, so on 1 March the books
record Dr AP ₦500,000 / Cr Bank ₦500,000. AP is a liability — it exists to show what
you owe suppliers — and it now shows minus ₦500,000. The balance sheet is asserting
that suppliers owe you money, which is not something AP can mean.

The truth on 1 March is that you are ₦500,000 out of pocket and the vendor owes you
goods. That is an ASSET — a prepayment — and there is no account in the seeded chart
to hold it.

THE AR SIDE ALREADY SOLVES THIS — MIRROR IT, DO NOT COPY IT

When a customer pays before their invoice exists, _post_payment_atomic in
apps/vs_finance/receivables.py splits at source: the settled part credits AR and the
excess credits customer credit 2140, so AR never carries a credit balance. Read that
function and the "split at source" comments in it.

The mirror is NOT identical. Customer credit is a LIABILITY (you owe the customer
their money back). A vendor advance is an ASSET (the vendor owes you goods). Seed a
new asset control account — vendor advances / prepayments — in
apps/vs_finance/seed.py alongside the existing chart, with the right IFRS mapping and
parent, and make the seed idempotent like the rest of that module.

SCOPE

  * Seed the vendor-advance account.
  * Split at source in _post_vendor_payment_atomic: debit AP only for what the payment
    actually settles, and debit vendor advances for the unallocated remainder.
  * When a later bill is settled from that advance, reclassify Dr AP / Cr vendor
    advances, dated at the later of the two documents — reuse
    vs_finance.chronology.effective_allocation_date, exactly as
    receivables.allocate_payment does. allocate_vendor_payment is the place.
  * Check reconcile_ap and ap_aging in apps/vs_procurement/reports.py still hold —
    they currently assume AP carries the whole payment.
  * VendorPayment.unallocated_amount and any screen showing it should now mean "sitting
    in vendor advances", the way credit_remaining does on the AR side.

ALREADY DONE, DO NOT REDO

allocate_vendor_payment is already date-guarded: it will not settle a bill dated after
the payment (auto-allocation skips it, an explicitly named bill is refused). That
makes the prepayment visible rather than mis-settled; it does not give it a home.

DATA CHECK FIRST

Before writing code, run this and report what it finds — as at 2026-07-30 the answer
was 2 posted vendor payments, 0 predating their bill, 0 with unallocated gross, which
is why this was deprioritised:

  cd apps && ../cx/bin/python manage.py shell -c "
  from vs_procurement.models import VendorPayment, VendorPaymentAllocation
  from vs_finance.constants import DocumentStatus
  print('posted:', VendorPayment.objects.filter(status=DocumentStatus.POSTED).count())
  print('unallocated:', len([p for p in VendorPayment.objects.filter(status=DocumentStatus.POSTED) if p.unallocated_amount > 0]))
  "

If there is existing data with an unallocated balance, the migration must reclassify
it out of AP into the new account — say so explicitly and show the journal you intend
to raise before raising it.

TESTS

Vendor payment before the bill lands in vendor advances, not AP; AP is untouched on
that date; the later bill draws the advance down with a reclassification dated to the
bill; reconcile_ap balances throughout; a normal same-day payment behaves exactly as
before.

VERIFY

cd apps && ../cx/bin/python manage.py test vs_procurement vs_finance --noinput

FINALLY

Delete the "Vendor prepayments drive AP negative" bullet from "## Undone" in the
backend root todo.md and add a "# ..." entry to "## Done" in the existing style.
Commit to main (do not push), staging files explicitly.
```

---

## Prompt 3 — Stop expense, payroll and tax settlements predating their obligation

```
Fix the third "## Undone" item in the backend repo's root todo.md — expense, payroll
and tax settlements can be dated before the obligation they settle.

BACKGROUND

apps/vs_finance/chronology.py already holds the shared guard for this class of bug.
Read its module docstring first — it explains why "is this period open?" and "could
this have happened by then?" are different questions, and why only the first was ever
being asked. The AR side (refunds, write-offs, concessions, allocations) is fixed. The
audit that produced this item found three more services with the identical shape: each
takes a caller-supplied pay date, validates only current status/balance plus an open
period, and then debits a liability on that date.

  1. _settle_expense_claim_atomic — apps/vs_finance/expenses.py:179
     Never compares pay_date to claim.claim_date. Reimbursing before the claim was
     accrued debits accrued-reimbursement before anything credited it.
  2. _pay_payroll_atomic — apps/vs_finance/payroll.py:280
     Never compares pay_date to run.pay_date. A disbursement can predate the accrual
     it clears, so net-wages-payable goes debit for the gap.
  3. _pay_filing_atomic — apps/vs_finance/tax_filing.py:403
     Never compares pay_date to filing.filed_date. A remittance can predate the
     return being filed.

WHAT TO DO

Call chronology.ensure_on_or_after in each, passing a subject/source pair that reads
as a sentence and a `remedy` that names the date the user should pick instead. Follow
exactly how credit_notes._write_off_invoice_atomic and
installments._post_concession_atomic already call it — same phrasing style, same level
of helpfulness in the message. It raises BackdatedPostingError (409,
POSTING_BACKDATED).

Then check for a matching workflow preflight for each document type in
apps/vs_finance/workflow_handlers.py and mirror the guard there, the way WriteOffHandler
does — a preflight that disagrees with the posting service means an approval queue
fills with items that cannot post.

Also check the API layer for each (apps/vs_finance/views.py and views_ops/) and decide
whether a batch or list endpoint should pre-validate per item rather than failing
mid-loop, the way ARAdjustmentBatchView does for write-offs.

FRONTEND

console-fe's PostingDateField already supports this constraint: pass `notBefore` (and
`notBeforeLabel`) and the calendar stops offering earlier days, with a message naming
the floor. See how refunds-tab.tsx and concessions-tab.tsx use it. Apply it to the
expense-claim settle, payroll pay and tax remit screens under
console-fe/src/pages/protected/finance — settle/pay dates only, never to due dates or
report filters.

TESTS

Follow AccountingDateIntegrityTests in apps/vs_finance/tests.py — one rejection test
and one "on the boundary date it is allowed" test per service, asserting the document
is left untouched on rejection.

VERIFY

cd apps && ../cx/bin/python manage.py test vs_finance --noinput
For the frontend, npx tsc --noEmit && npx vitest run, then the /verify-design skill on
any screen you change, and LOOK at the screenshots.

FINALLY

Delete the "Expense, payroll and tax settlements can be dated before the obligation"
bullet from "## Undone" in the backend root todo.md and add a "# ..." entry to
"## Done". Commit to main in both repos (do not push), staging files explicitly.
```

---

## Prompt 4 — Stop journal reversal from silently desynchronising the sub-ledger

```
Fix the "Reversing a sub-ledger-backed journal silently desynchronises the sub-ledger"
item under "## Undone" in the backend repo's root todo.md.

BACKGROUND — verified, not theoretical

POST /finance/journals/<id>/reverse/ (JournalReverseView, apps/vs_finance/views.py:1253,
gated by finance.journal.reverse) accepts ANY posted journal belonging to the entity —
including the journal a receipt, invoice, credit note, concession or refund raised.

reverse_journal (apps/vs_finance/posting.py:351) is purely GL-level: it mirrors the
lines into a new entry and marks the original REVERSED. Nothing else reacts. There are
no signals on JournalEntry, and no document-level void service exists anywhere in
vs_finance — the only sub-ledger reversal in the codebase is
vs_procurement.payables.reverse_vendor_payment.

So after reversing a receipt's journal:
  * the GL says the cash never arrived;
  * Payment stays POSTED with its allocated_amount intact;
  * the PaymentAllocation rows stand;
  * Invoice.amount_paid is unchanged, so the invoice still looks paid;
  * for a refund, refunded_amount stays consumed on the credit lot, so the customer's
    credit is still shown as spent.

reconcile_ar then reports a difference that nobody can clear, because there is no
operation that would clear it.

DECIDE, THEN IMPLEMENT

Two defensible answers. Pick one, state which and why in the commit message, and check
with the user first if you think the choice is theirs:

  (a) Refuse. Make reverse_journal (or the view) reject a journal that a sub-ledger
      document points at, with an error naming the document and directing the user to
      a document-level void. Cheap, immediately stops the drift, but leaves a genuine
      need unmet — people do mis-key receipts and must be able to undo them.

  (b) Add real void services per document type — void_payment, void_invoice,
      void_credit_note, void_refund, void_concession — each unwinding the sub-ledger
      and raising the reversal in ONE transaction, then refuse (a) for anything that
      has one. This is the honest answer.

If (b): reverse_vendor_payment is the closest existing model — read it for the lock
ordering (payment, then allocation rows by invoice id, then invoices by pk) and for
the convention that allocation rows remain as history while the authoritative
settlement totals are rolled back. Note what a refund void additionally has to do:
release RefundAllocation rows and decrement refunded_amount on each source lot, or the
customer's credit stays permanently consumed. See apps/vs_finance/chronology.py and
credit_notes._attribute_refund_to_lots for how that attribution is built.

Also decide what a void does about DATES. The reversal must not be dated before the
document it reverses — use chronology.ensure_on_or_after, and note that reverse_journal
already falls back to today when the original period has since closed.

TESTS

Per document type: void unwinds both halves; reconcile_ar balances afterwards; a
voided receipt's invoice returns to unpaid; a voided refund returns the credit to its
lot and the receipt reports it as available again; voiding twice is refused; whichever
of (a)/(b) you chose, reversing the raw journal of a sub-ledger document behaves as
decided.

VERIFY

cd apps && ../cx/bin/python manage.py test vs_finance vs_procurement vs_payments --noinput

Also check the frontend: console-fe may expose a reverse action on the journal screen
(search for journals/.../reverse). If your answer is (a), that action needs to explain
the refusal rather than surface a raw 409; if (b), the document screens likely want a
Void action.

FINALLY

Delete the "Reversing a sub-ledger-backed journal" bullet from "## Undone" in the
backend root todo.md and add a "# ..." entry to "## Done". Commit to main (do not
push), staging files explicitly.
```

---

## Prompt 5 — Surface gateway receipts that could not be booked

```
Fix the "A gateway receipt that cannot be booked disappears silently" item under
"## Undone" in the backend repo's root todo.md.

BACKGROUND — verified

_book_receipt (apps/vs_payments/services.py:268) dates the customer receipt
datetime.date.today() and posts it. During a period close — when no fiscal period is
open for today — ensure_period_open raises, _dispatch fails, and
process_webhook_event (apps/vs_payments/webhooks.py:105) marks the WebhookEvent FAILED
and DELIBERATELY swallows the exception.

The swallow is correct for the PSP: it has already been acked, and confirm_* are
idempotent, so re-raising would only produce a spurious 500. The problem is what
happens next — nothing. apps/vs_payments/urls.py exposes collections, payouts,
batches, settlement reconciliation, transactions, movements and the webhook receiver.
There is NO endpoint that lists WebhookEvent at all, no FAILED filter, no alert, and
no replay action.

So real customer money can arrive, fail to book, and be visible only to someone
querying the table by hand.

SCOPE

  1. A permission-gated list of webhook events — at minimum the FAILED ones — showing
     provider, reference, received time, the stored error string, and the resolved
     collection/payout if one was matched. Follow the conventions of the existing
     views in apps/vs_payments/views.py: rbac_permission, resolve_entity scoping,
     XVSPagination, success_response. Add the permission key to the payments seed and
     to the console's permission constants; the repo convention is to update
     PERMISSIONS_AUDIT.md in console-fe whenever permission keys change.
  2. A replay action that re-runs process_webhook_event for one event. It is already
     idempotent by design — confirm the guard at the top of that function covers a
     replay of a FAILED (not PROCESSED) row before relying on it.
  3. Consider letting a receipt whose date has no open period fall to the nearest open
     date instead of failing at all. apps/vs_finance/banking.py already has
     resolve_adjustment_date (line ~648) doing exactly this for bank adjustments, and
     its docstring explains the reasoning — a legitimate movement in a closed month
     must still be recordable. If you adopt it, the receipt must keep a record of the
     true value date, and posting.posting_window is the read-side helper for choosing.
     This is a judgement call with accounting consequences — put the recommendation to
     the user before implementing it.
  4. A frontend surface for (1) and (2). The payments console lives at
     console-fe/src/pages/protected/payments. A small "Needs attention" panel or tab
     is enough; it must not be buried. Follow the house page anatomy and the
     responsive rules in the console CLAUDE.md.

TESTS

A webhook whose booking fails leaves the event FAILED with the error recorded and no
Payment row; the failed list returns it and is entity-scoped and permission-gated
(assert the 403); a replay after the period is reopened books exactly one receipt;
replaying a PROCESSED event is a no-op.

VERIFY

cd apps && ../cx/bin/python manage.py test vs_payments --noinput   (64 green at time of writing)
Frontend: npx tsc --noEmit && npx vitest run, then the /verify-design skill on the new
screen plus the mobile overflow probe, and LOOK at the screenshots.

FINALLY

Delete the "A gateway receipt that cannot be booked disappears silently" bullet from
"## Undone" in the backend root todo.md and add a "# ..." entry to "## Done". Commit
to main in both repos (do not push), staging files explicitly.
```
