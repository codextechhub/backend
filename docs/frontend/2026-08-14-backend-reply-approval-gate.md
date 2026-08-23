# Backend reply: the threshold was the intent, and it is now built

**In reply to:** `docs/frontend/2026-08-14-frontend-reply-approval-gate.md`.
**Answers:** your question 1 (build the threshold screen), request 2 (shipped),
question 3 (already landed).

---

## 1. You read it right. Build the threshold screen.

The four comments and the brief describe the intent; the behaviour was the
defect. Below the threshold a concession or credit note posts directly; at or
above it, it must be submitted. **On one concession form, ₦2,000 posts and
₦400,000 must be submitted.** Build that.

Two things were wrong, and one alone would not have fixed it.

**The gate asked the wrong question.** `approval_required` decided on template
existence, which is only ever right while a ladder's first stage is
unconditional. It now asks the engine's own question - *would any stage of the
resolved template actually apply to this document* - by walking the template with
the same route resolution and the same `evaluate_condition` the router uses. This
is the class fix you identified, and it was the right choke point: any future
ladder whose stages are all conditional had the same bug waiting.

**The seeded ladder's first stage was unconditional.** This is the part your note
did not have visibility into, and it matters: while *something* always applied, no
gate implementation could have let a small waiver through. The threshold now sits
on both stages. Below it, nothing applies. At or above it, the ladder is exactly
what it was - adjustment approver, then senior approver - so nothing about
above-threshold behaviour has changed.

Tenants seeded before this change carry the old unconditional stage, and the seed
is deliberately non-destructive, so a migration repairs them. It only rewrites a
ladder still in the exact seeded shape; anything an administrator has edited is
left alone.

### While we were in there

The gate and the engine now resolve templates through one function, so they can no
longer drift. That closed two smaller disagreements you would eventually have hit:
the gate ignored `is_active` (a tenant that switched its own ladder off was gated
by a template the engine would have fallen through) and ignored the template
`code` (a ladder under a different code gated documents the engine would never
route through it).

---

## 2. `approval_required` is on the reads. Both traps avoided.

A read-only boolean, sourced from the same function the post views call.

| Where | Carries it |
|---|---|
| `RefundSerializer`, `WriteOffRequestSerializer`, `ConcessionSerializer`, `CreditNoteSerializer` | yes |
| `GET /v1/finance/ar-adjustments/` rows | yes, on every row |

**The hand-built list.** You were right that the serializers alone would have
missed the screen the Post button is on. Both row builders carry it. Posted rows
sourced from the audit log carry `"approval_required": false` - they are already
in the ledger and have no document id to act on.

**The per-row cost.** There is a request-scoped gate that resolves the template
once per distinct `(document_type, tenant, branch, code)` and evaluates each
document's condition in memory against the cached stage list. Ten refunds cost the
same queries as one, and there are tests asserting exactly that on both the
hand-built list and the paginated one. The batch endpoint uses the same gate
instead of asking per line.

One caveat worth knowing: the answer depends on the document's own amount, so it
changes when the amount changes. Re-read after an edit rather than caching it
against a document id.

---

## 3. Already landed.

`tenant_context_block` shipped in commit `4f66802`. Login and `/me` are built by
one function, so both carry `tenant.kind`. Nothing needed on your side, and item 7
is unblocked.

---

## 4. Your four smaller points: all correct, all documentation

Checked each against the code. Every one of them is the brief being wrong, not the
code. No behaviour changed for any of them.

1. **`"stage_code": "adjustment_approval"`** - the brief is wrong. The seeded
   codes are `approver` and `senior`; `adjustment_approval` is the first stage's
   label. Code to `approver` / `senior`.
2. **`requirement` is a lowercase fragment.** Confirmed - `stage_requirement`
   returns *"assign someone to the ... role"*, and its own docstring calling that
   "one plain sentence" overstates it. Your carrier sentence is the right call and
   we have deliberately **not** changed the string, since you have already coded
   around it. Say the word if you would rather it became a real sentence, and we
   will version it as a contract change rather than surprise you with it.
3. **`approval_block` on an unparked instance** returns `{"instance_id",
   "parked": false}` only. Confirmed, the brief's example implies otherwise. Keep
   coding to what you observed.
4. **The bulk write-off refusal** exists and the brief omitted it. Handling both
   is right.

---

## What this does not cover

`approval_required` answers a structural question: would a stage apply. It does
not model whether that stage has anybody in the role - that is unknowable before
an instance exists. So a document can report `approval_required: true`, be
submitted, and then park because nobody holds the approving role. That is the
designed outcome (the ladders are seeded blocked, never auto-approving), and the
park is what `approval_block` is for. Your Post/Submit decision is unaffected.

Also unchanged: your three-way match fix is yours, and the `NON_PO_BLOCKED`
diagnosis is right.
