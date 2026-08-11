# Follow-up prompts - finance / payments / procurement

Self-contained prompts, each meant to be **pasted whole into a fresh session**. They
assume no memory of the conversation that produced them, and each ends by removing
its own entry from the root `todo.md`.

Started 2026-07-30 after the accounting-date class-fix (backend `8b576eb`, console-fe
`f8038a8`, payments follow-up `d8eaeb9`). Four are now done and their prompts have
been removed: backdated expense/payroll/tax settlements and the sub-ledger
journal-reversal drift (both 2026-08-01), then AR "as at" reporting and voided
documents in statement history (both 2026-08-11). See the `## Done` section of
`todo.md` for what each one actually did.

| # | Title | Size | Why now |
|---|-------|------|---------|
| 1 | Vendor advances | Small | Latent - still zero vendor prepayments in the books |
| 2 | Unbookable gateway receipts are invisible | Medium | Real customer money can vanish from view |

Suggested order: **2 → 1**. Prompt 2 is operational; prompt 1 can wait until you
actually pay a supplier in advance.

---

## Prompt 1 - Give vendor prepayments somewhere to live

```
Fix the vendor-prepayment gap recorded under "## Undone" in the backend repo's root
todo.md ("Vendor prepayments drive AP negative").

BACKGROUND

Pay a supplier ₦500,000 on 1 March as a deposit; their invoice arrives on 10 March.
_post_vendor_payment_atomic (apps/vs_procurement/payables.py) debits Accounts Payable
for the full gross regardless of what the payment settles, so on 1 March the books
record Dr AP ₦500,000 / Cr Bank ₦500,000. AP is a liability - it exists to show what
you owe suppliers - and it now shows minus ₦500,000. The balance sheet is asserting
that suppliers owe you money, which is not something AP can mean.

The truth on 1 March is that you are ₦500,000 out of pocket and the vendor owes you
goods. That is an ASSET - a prepayment - and there is no account in the seeded chart
to hold it.

THE AR SIDE ALREADY SOLVES THIS - MIRROR IT, DO NOT COPY IT

When a customer pays before their invoice exists, _post_payment_atomic in
apps/vs_finance/receivables.py splits at source: the settled part credits AR and the
excess credits customer credit 2140, so AR never carries a credit balance. Read that
function and the "split at source" comments in it.

The mirror is NOT identical. Customer credit is a LIABILITY (you owe the customer
their money back). A vendor advance is an ASSET (the vendor owes you goods). Seed a
new asset control account - vendor advances / prepayments - in
apps/vs_finance/seed.py alongside the existing chart, with the right IFRS mapping and
parent, and make the seed idempotent like the rest of that module.

SCOPE

  * Seed the vendor-advance account.
  * Split at source in _post_vendor_payment_atomic: debit AP only for what the payment
    actually settles, and debit vendor advances for the unallocated remainder.
  * When a later bill is settled from that advance, reclassify Dr AP / Cr vendor
    advances, dated at the later of the two documents - reuse
    vs_finance.chronology.effective_allocation_date, exactly as
    receivables.allocate_payment does. allocate_vendor_payment is the place.
  * Check reconcile_ap and ap_aging in apps/vs_procurement/reports.py still hold -
    they currently assume AP carries the whole payment.
  * VendorPayment.unallocated_amount and any screen showing it should now mean "sitting
    in vendor advances", the way credit_remaining does on the AR side.

ALREADY DONE, DO NOT REDO

allocate_vendor_payment is already date-guarded: it will not settle a bill dated after
the payment (auto-allocation skips it, an explicitly named bill is refused). That
makes the prepayment visible rather than mis-settled; it does not give it a home.

DATA CHECK FIRST

Before writing code, run this and report what it finds - as at 2026-07-30 the answer
was 2 posted vendor payments, 0 predating their bill, 0 with unallocated gross, which
is why this was deprioritised:

  cd apps && ../cx/bin/python manage.py shell -c "
  from vs_procurement.models import VendorPayment, VendorPaymentAllocation
  from vs_finance.constants import DocumentStatus
  print('posted:', VendorPayment.objects.filter(status=DocumentStatus.POSTED).count())
  print('unallocated:', len([p for p in VendorPayment.objects.filter(status=DocumentStatus.POSTED) if p.unallocated_amount > 0]))
  "

If there is existing data with an unallocated balance, the migration must reclassify
it out of AP into the new account - say so explicitly and show the journal you intend
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

## Prompt 2 - Surface gateway receipts that could not be booked

```
Fix the "A gateway receipt that cannot be booked disappears silently" item under
"## Undone" in the backend repo's root todo.md.

BACKGROUND - verified

_book_receipt (apps/vs_payments/services.py:268) dates the customer receipt
datetime.date.today() and posts it. During a period close - when no fiscal period is
open for today - ensure_period_open raises, _dispatch fails, and
process_webhook_event (apps/vs_payments/webhooks.py:105) marks the WebhookEvent FAILED
and DELIBERATELY swallows the exception.

The swallow is correct for the PSP: it has already been acked, and confirm_* are
idempotent, so re-raising would only produce a spurious 500. The problem is what
happens next - nothing. apps/vs_payments/urls.py exposes collections, payouts,
batches, settlement reconciliation, transactions, movements and the webhook receiver.
There is NO endpoint that lists WebhookEvent at all, no FAILED filter, no alert, and
no replay action.

So real customer money can arrive, fail to book, and be visible only to someone
querying the table by hand.

SCOPE

  1. A permission-gated list of webhook events - at minimum the FAILED ones - showing
     provider, reference, received time, the stored error string, and the resolved
     collection/payout if one was matched. Follow the conventions of the existing
     views in apps/vs_payments/views.py: rbac_permission, resolve_entity scoping,
     XVSPagination, success_response. Add the permission key to the payments seed and
     to the console's permission constants; the repo convention is to update
     PERMISSIONS_AUDIT.md in console-fe whenever permission keys change.
  2. A replay action that re-runs process_webhook_event for one event. It is already
     idempotent by design - confirm the guard at the top of that function covers a
     replay of a FAILED (not PROCESSED) row before relying on it.
  3. Consider letting a receipt whose date has no open period fall to the nearest open
     date instead of failing at all. apps/vs_finance/banking.py already has
     resolve_adjustment_date (line ~648) doing exactly this for bank adjustments, and
     its docstring explains the reasoning - a legitimate movement in a closed month
     must still be recordable. If you adopt it, the receipt must keep a record of the
     true value date, and posting.posting_window is the read-side helper for choosing.
     This is a judgement call with accounting consequences - put the recommendation to
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
