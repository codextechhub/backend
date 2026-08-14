# Frontend reply: one defect, one request

**In reply to:** `docs/frontend/2026-08-14-frontend-adjustments.md` (commit `24293fd`).
**Verified against:** `main` at `24293fd`.
**Blocks:** the concession / credit-note approval UI (brief items 1, 2 and 4).

Two things, both on the finance adjustment gate. The first is a behaviour defect and
the brief documents the intended behaviour rather than the built one. The second is a
read we need and cannot compute on the client without reimplementing your scope
cascade.

---

## 1. Defect: concessions and credit notes are gated at every amount

### What the code does

`approval_required` (`apps/vs_finance/approvals.py:17`) decides the gate purely on
whether a `WorkflowTemplate` exists for the document's type at its `(tenant, branch)`
scope. There is no amount in it. `ConcessionPostView`
(`apps/vs_finance/views_ar.py:2597`) and `CreditNotePostView`
(`apps/vs_finance/views_ar.py:1400`) both refuse on that boolean alone.

The seeded ladder (`_stages_payload`, `apps/vs_finance/approvals.py:84`) gives a
threshold-gated type **two** stages:

| Stage | `code` | `inclusion_condition` |
|---|---|---|
| Adjustment approval | `approver` | none - always applies |
| Senior adjustment approval | `senior` | `{"op": "gte", "field": <amount>, "value": 5000000}` |

The threshold sits on the **second** stage. The first is unconditional. So once
`ensure_tenant_approval_templates` has run for a tenant - which provisioning now does
for every tenant - `approval_required` answers `True` for a concession of any size, and
`POST /v1/finance/concessions/<id>/post/` returns the 400 refusal for a ₦2,000 waiver
exactly as it does for a ₦400,000 one.

### Why we read that as a defect rather than the intent

Four places in the same commit state the opposite, in terms too specific to be loose
wording:

- `apps/vs_finance/constants.py:697` - *"Kobo at or above which a concession or credit
  note needs a second person... Small goodwill allowances stay frictionless, which is
  the only reason not to gate everything."*
- `apps/vs_finance/approvals.py:68` (`_ADJUSTMENT_TEMPLATES`) - *"Concessions and credit
  notes are gated only above a threshold, because a ₦2,000 goodwill allowance should not
  need a meeting and a ₦400,000 waiver should."*
- `apps/vs_finance/management/commands/seed_finance_permissions.py:61-63` - *"the submit
  key is the ordinary route for a large waiver or note and the post key only reaches the
  ledger below it."* The post key currently reaches the ledger nowhere.
- The brief itself, §1: *"Below the threshold they still post directly... on one
  concession form, ₦2,000 posts and ₦400,000 must be submitted."*

### Why the tests did not catch it

`test_a_waiver_is_gated_only_above_the_threshold` (`apps/vs_finance/tests.py:9958`)
asserts the *stage structure* - two stages, first unconditional, second carrying the
threshold - and never calls the post endpoint. The assertions pass, and the name
describes behaviour that is never exercised.

### Where we think the fix belongs

Not in the seed. Moving the threshold onto the first stage would make
`approval_required` still answer `True` below the threshold (the template exists either
way), so `/post/` would keep refusing, and a submitted small concession would enter an
instance in which every stage is skipped by `inclusion_condition` and terminate APPROVED
immediately. That is a stranger outcome than today's.

The choke point is `approval_required` itself. It should answer *"would any stage of the
resolved template actually apply to this document"*, evaluating stage inclusion with the
same `evaluate_condition` the router already uses at
`apps/vs_workflow/services/routing.py:296`. That keeps the module's own stated invariant
- gate and engine resolving identically - which today they do not: the gate says
"approval required" for a document the engine would route past every approval stage on.

It is also a class fix rather than a case fix. Any future ladder whose stages are all
conditional inherits the same bug; journals, payouts and procurement are unaffected only
because their first stage happens to be unconditional.

If you conclude the current behaviour is the intent after all, that is a fine answer -
but then the four comments above and the brief need correcting, and we will build "every
concession needs approval" instead. **We need to know which, because the two produce
different screens.** We have not built either yet.

### Smaller things in the same area, for the same pass

- The brief's example `approval` block shows `"stage_code": "adjustment_approval"`. The
  seeded codes are `approver` and `senior`; `adjustment_approval` is the first stage's
  *label*.
- The brief says to render `requirement` verbatim as a finished sentence and gives
  *"Appoint somebody to the Finance adjustment approver role."* `stage_requirement`
  returns a lowercase fragment - *"assign someone to the finance-adjustment-approver
  role"* - which reads wrong standing alone. We will embed it in a carrier sentence. No
  change needed on your side unless you would rather it be a sentence; just flagging
  that the doc and the function disagree.
- `approval_block` for an unparked instance returns `{"instance_id", "parked": false}`
  only, not the full block with `parked` flipped. We are coding to that; noting it
  because the brief's example implies otherwise.
- The brief gives the bulk **refund** batch refusal but not the bulk **write-off** one
  (`apps/vs_finance/views_ar.py:2240`). We are handling both.

---

## 2. Request: expose whether a document is gated

### The problem

Nothing in the API tells a client whether a given adjustment is approval-gated.
`approval_required` is not on any read serializer, so the console cannot decide whether
to show **Post** or **Submit for approval** without guessing.

We can only avoid guessing by fetching `/v1/workflow/templates/` per document type and
reimplementing the branch → tenant → platform cascade on the client. That puts a second
implementation of your gate in our codebase, and it will drift from yours the first time
the cascade changes. We would rather not.

### What we are asking for

A read-only `approval_required` boolean on the four adjustment reads, sourced from the
same function the post views call:

| Serializer | File |
|---|---|
| `RefundSerializer` | `apps/vs_finance/serializers.py:515` |
| `WriteOffRequestSerializer` | `apps/vs_finance/serializers.py:532` |
| `ConcessionSerializer` | `apps/vs_finance/serializers.py:593` |
| `CreditNoteSerializer` | `apps/vs_finance/serializers.py:492` |

### Two things that will bite if it is done the obvious way

**The screen with the Post button does not use those serializers.** The Refunds &
write-offs list is `ARAdjustmentListView` (`apps/vs_finance/views_ar.py:2329`), which
hand-builds its row dicts and never touches `RefundSerializer` or
`WriteOffRequestSerializer`. Adding the field to the serializers alone would leave the
one list our Post action is rendered on without it. The row builders at
`views_ar.py:2352` and `_writeoff_rows` (`views_ar.py:2272`) need it too.

**A naive per-row call is a query per row.** That view pulls up to 1000 refunds plus
write-offs into memory before paginating, so a per-document `approval_required` is 1000+
`WorkflowTemplate` existence checks on one request. The answer only varies by
`(document_type, tenant_id, branch_id)`, so it wants resolving once per distinct scope
and reusing - a small cache inside the request, or a helper that takes the document type
and scope rather than a document.

### What we do with it

Show one primary action instead of two guesses, and stop offering a Post button that
returns 400. Nothing else - we are not branching business logic on it.

---

## 3. Smaller: the login response omits `tenant.kind`, and that is the one we read

Brief item 9 says `/me` now returns `tenant.kind`, and it does. The **login** response
does not: at `24293fd`, `LoginService` returns `{'slug', 'name'}` only
(`apps/vs_user/services/auth.py:142`).

That is the copy the console actually holds. We treat a fresh login as equivalent to a
`/me` sync and only re-fetch `/me` after a *token refresh*, so `tenant.kind` is
`undefined` for the whole first session and our `selectIsPlatformTenant` answers `false`
for a platform operator who has just signed in. The visible effect today is in the
workflow template builder, which tells a platform operator editing a shared template
that their edit will fork a tenant copy - the opposite of what will happen.

It also blocks brief item 7: you asked us to use `tenant.kind` to decide whether to show
the adoption and compare endpoints, and right after login we cannot.

We think a fix is already in progress - the working tree carries a `tenant_context_block`
helper shared by both callers, which is exactly the right shape. Please land it. If you
would rather we defend against it on our side instead, say so and we will re-sync `/me`
after login, but one builder feeding both responses is the better fix and we would
rather not paper over it.

---

## What we are doing meanwhile

Building the parts that do not depend on any of the above: the `NON_PO_BLOCKED` match
fix, the stock-location work (brief item 6) and the close-checklist severity styling
(item 5). The concession and credit-note approval UI is parked on question 1; the
platform template adoption view (item 7) is parked on question 3.

One thing worth knowing on your side: our three-way match screen was treating only
`UNDER_RECEIVED` and `OVER_BILLED` as blocking, so with `allow_non_po_invoices` now
defaulting off, every non-PO bill currently renders as a passed match with a Post button
that 409s. That is ours to fix and we are fixing it - flagging it only because it is the
most visible symptom of item 3 in the field right now.

---

## Addendum (same day): one more inconsistency, in the stock reorder report

Found while building the reorder and valuation report screens. Not urgent, and
we have shipped around it, but the two reports disagree with each other.

`valuation_row` (`apps/vs_procurement/stock.py:569`) deliberately accepts either a
stock master or a location balance, so `?location=` narrows both *which* rows come
back **and** what they report - the store's own quantity and its own weighted
average. That is what makes the store-scoped valuation correct.

`reorder_row` (`apps/vs_procurement/stock.py:533`) always reads the stock master.
So `?location=` narrows only *which* rows come back: `reorder_items` selects items
low at that store via the `EXISTS` subquery, but `on_hand_qty`, `reorder_level` and
`unit_cost` remain the entity roll-up.

The visible result is a row that contradicts itself. On our seeded data, LTO-9 at
`ANNEX` (which holds 10 against a reorder level of 20) is correctly listed as
needing reorder, and then reports **On hand 25** against **Reorder point 20** -
because 25 is the entity total across both stores. A reader sees an item flagged as
short while the number beside it looks healthy.

We have labelled the column "On hand (entity)" and added a line explaining it when
a store is selected, which is honest but is not the answer anyone wants. Passing
the balance to `reorder_row` the way `valuation_row` already does would fix it at
the same choke point, and the report would then mean the same thing in both scopes.

No response needed if you would rather leave it; we are not blocked. If you do
change it, tell us, because our column label and explanatory line should come back
out in the same release.
