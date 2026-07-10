# Finance Approval Workflow — Design Doc

**Status:** Draft for review · **Author:** finance audit follow-up · **Date:** 2026-07-06
**Scope:** How money-moving finance documents get a maker-checker / multi-level
approval gate by plugging into the platform's **existing** `vs_workflow` engine —
*not* by building a bespoke finance approval system.

> TL;DR — Finance does **not** get its own approval code. Every approvable finance
> document (a) exposes `workflow_document_type`, (b) is submitted through
> `vs_workflow.submit_for_approval`, and (c) posts to the GL only inside the
> engine's `on_approved` callback. The `DocumentStatus` enum already carries the
> `DRAFT → PENDING_APPROVAL → APPROVED → POSTED` states this needs. Approver
> resolution, thresholds, delegation, SoD, audit and notifications are all the
> engine's job already.

---

## 1. Why reuse `vs_workflow` (and not build our own)

`vs_workflow` (`apps/vs_workflow/`, see `guide.md`) is a complete, tested approval
engine already used by procurement. It gives us, for free, everything the audit
flagged as missing:

| Need (from the audit) | `vs_workflow` feature |
|---|---|
| Maker-checker / segregation of duties | `REQUESTER_CANNOT_APPROVE` is enforced in `resolve_approvers` (requester always excluded) |
| Multi-level approval | Ordered `WorkflowStage` rows, linear or route-based |
| Threshold routing (approve over ₦X) | `WorkflowRoutePath.condition` / `WorkflowStage.inclusion_condition` JSON, e.g. `{op: "gte", field: "total", value: 50000000}` |
| Quorum / unanimity | `advance_rule` = `ANY` / `QUORUM` / `UNANIMOUS` |
| Delegation (approver on leave) | `ApprovalDelegation` |
| Immutable approval audit | `WorkflowAuditLog` (append-only) + `WorkflowStageAction` |
| Approver notifications | `template.notification_events` → `dispatch_notification` task → `vs_notifications` |
| Approval inbox / dashboards | `/v1/workflow/dashboard/pending`, `/submitted`, `/team-load` |

Building a second approval system in finance would duplicate all of this and
diverge over time. **Decision: integrate, don't reinvent.**

---

## 2. The one hard problem: Entity ↔ School/Branch scoping

This is the crux of the whole design and must be settled first.

- `vs_workflow` and `vs_notifications` are **school-scoped**. The engine reads
  `document.school` and `document.branch` (`services/submission.py:36-45`) and
  resolves approvers with `resolve_users_with_permission(school, branch, key)`
  (`services/approvers.py:36-66`).
- Finance is **entity-scoped**. `FinanceDocument` has `entity` + `branch`, no
  `school` (`apps/vs_finance/models/core.py:175-211`). A `LedgerEntity` has a
  **nullable** `source_school` (`core.py:99-104`), and one school may own several
  entities.

### Resolution

1. **Add a `school` property to `FinanceDocument`** that returns
   `self.entity.source_school` (already the canonical entity→school link). Branch
   already exists. No new columns:

   ```python
   # apps/vs_finance/models/core.py  (FinanceDocument)
   @property
   def school(self):
       """School that owns this document's ledger entity (None for platform/product books)."""
       return self.entity.source_school
   ```

   This makes every finance document satisfy the engine's `document.school` /
   `document.branch` contract with zero schema change.

2. **Platform/product entities have `source_school = None`.** The submission
   cascade already falls back to a **platform-wide** template
   (`school=None, branch=None`) — `submission.py:41-45` — and approver scope
   `PLATFORM` passes `school=None` to RBAC. So platform books (CODEX) approve
   against platform-level roles. This is correct and needs no special-casing.

3. **Accepted limitation:** approver eligibility is resolved at the *school*
   level, so two entities under the same school share one approver pool for a
   given permission key. That is fine — approvers are people at a school, and the
   *entity* remains the ledger/isolation boundary. If per-entity approver
   isolation is ever needed, it is a future `approver_scope = ENTITY` extension in
   the engine, **out of scope here.** → **Confirm this is acceptable (Q1).**

---

## 3. The approval-gated posting model

Today "post" is one atomic action. We interpose approval **between draft and
post**, reusing the states already in the enum
(`apps/vs_finance/constants.py:44-49`):

```
 DRAFT ──submit──► PENDING_APPROVAL ──engine approves──► APPROVED ──post──► POSTED
   │                      │                                   │
   │                      └── engine rejects ────────────────┴──► back to DRAFT
   │                                                                (or CANCELLED)
   └── (no template configured) ──────────── post directly ─────► POSTED   [rollout gate, §7]
```

**The golden rule: the GL posting happens inside the engine's `on_approved`
callback, never before.** So the money cannot hit the ledger until approval
completes. Concretely, for a journal:

1. User creates the journal (`DRAFT`) — unchanged.
2. User calls the new `POST /journals/<id>/submit/` → `submit_for_approval(journal, user)`.
   Handler's `validate_document` runs the **posting preconditions** (period open,
   balanced, accounts active) *now*, so a doomed document is rejected before it
   ever enters the queue. Document → `PENDING_APPROVAL`.
3. Approvers vote via the standard `/v1/workflow/instances/<id>/actions/`.
4. On final approval, the engine calls `FinanceApprovalHandler.on_approved`, which
   calls the **existing** `post_journal(entry, actor_user=<original requester or
   system>)`. Document → `POSTED`. (`on_rejected` → `DRAFT`; `on_returned` →
   `DRAFT` with the requester notified to amend.)

Nothing in the posting engine changes — we are only moving *when* `post_journal`
is called and *who* is allowed to trigger it. The balanced-or-rejected, closed-
period, audit-row guarantees remain exactly as they are.

### Who is recorded as the poster?

`post_journal(actor_user=...)` stamps `posted_by`. Options: (a) the final
approver, (b) the original requester, (c) a system user. **Recommendation: stamp
the original requester as `created_by` (already so) and the final approver as
`posted_by`** — this is the cleanest maker-checker trail. The `WorkflowAuditLog`
holds the full vote history regardless. → **Confirm (Q2).**

---

## 4. Document coverage & default templates

Every path the audit flagged as single-actor money movement gets a
`workflow_document_type`. Proposed matrix (thresholds are examples in **kobo** and
must be set per your policy):

| Document | `workflow_document_type` | Submit endpoint | Default approval policy |
|---|---|---|---|
| Journal entry (manual) | `finance.journal` | `/journals/<id>/submit/` | 1 checker over ₦0; + controller ≥ ₦5m |
| Direct entry | `finance.direct_entry` | `/direct-entries/<id>/submit/` | Always 1 checker (openings/capital are sensitive) |
| Credit note | `finance.credit_note` | `/credit-notes/<id>/submit/` | 1 checker; + finance mgr ≥ ₦1m |
| Debit note | `finance.debit_note` | same endpoint, kind-routed | 1 checker |
| Refund (cash out to customer) | `finance.refund` | `/refunds/<id>/submit/` | 1 checker always (cash leaves) |
| Bad-debt write-off | `finance.write_off` | `/write-offs/<id>/submit/` | Finance mgr; + controller ≥ ₦1m — **needs a new `WriteOffRequest` document (see below)** |
| Concession (discount/waiver/scholarship) | `finance.concession` | `/concessions/<id>/submit/` | 1 checker; policy-holder ≥ threshold |
| Expense claim | `finance.expense_claim` | replaces current post flow | Line mgr → finance (already half-built) |
| Payroll run | `finance.payroll_run` | `/payroll-runs/<id>/submit/` | HR/finance mgr — high value, always |
| Payout batch (money out via PSP) | `payments.payout_batch` | `/payout-batches/<id>/submit/` | 1 checker + controller ≥ threshold |
| Budget | `finance.budget` | migrate existing `approve/` to engine | 1 approver (today it's a single call) |

### Cut-1 scope adjustments (confirmed 2026-07-06)

Two "money-out" documents don't fit the base handler as-is:

- **Write-offs** have no document today — `write_off_invoice` is an *action* on an
  invoice, recorded only in `FinanceAuditLog`. **Decision:** add a lightweight
  `WriteOffRequest` document (`DRAFT → APPROVED → POSTED`, `entity`+`branch`+`invoice`
  +`amount`+`write_off_account`) that the workflow attaches to; `on_approved` calls
  the existing `write_off_invoice` service. This is the correct ERP shape (a
  write-off *should* be an approvable, first-class record) and is the **one
  sanctioned new table** — it supersedes §10's "no new finance tables" for this doc
  type. The `ARAdjustmentListView`/`_writeoff_rows` reader should later prefer the
  new document over the audit-log reconstruction (backward-compatible: keep reading
  the log for historical write-offs).
- **Payout batches** (`vs_payments`, gate the PSP *submission* not a GL post) —
  **now built** (2026-07-07): `PayoutBatch` gains `workflow_document_type =
  "payments.payout_batch"` + a `school`/`branch` bridge; a
  `vs_payments/workflow_handlers.py` handler whose `on_approved` calls
  `submit_payout_batch`. The batch has no approval states in its own enum, so the
  approval phase is tracked in `metadata["approval_status"]` and the batch stays
  `DRAFT` until approved+submitted (→ `PROCESSING`). New
  `/payout-batches/<id>/submit-for-approval/` endpoint; direct submit refused when
  gated. RBAC: `payments.payout_batch.{submit,approve,approve_high_value}`.

So cut-1 approval coverage = **journals → refunds → write-offs (via
`WriteOffRequest`) → payout batches** (all done).

**Invoices, customer receipts, and fee-run generation are intentionally NOT
gated** — they are revenue capture, not disbursement, and gating them would break
day-to-day billing. (Invoices instead get the *notification* track — separate
doc.) → **Confirm the matrix & which docs are in the first cut (Q3).**

Threshold routing uses a `BRANCH` stage + two routes:

```json
{
  "document_type": "finance.journal",
  "code": "standard",
  "stages": [
    { "code": "gate", "kind": "BRANCH", "order": 1 },
    { "code": "checker", "kind": "APPROVAL", "order": 2,
      "approver_permission_key": "finance.journal.approve",
      "approver_scope": "SCHOOL", "advance_rule": "ANY",
      "on_rejection": "RETURN_TO_REQUESTER" },
    { "code": "controller", "kind": "APPROVAL", "order": 3,
      "approver_permission_key": "finance.journal.approve_high_value",
      "approver_scope": "SCHOOL", "advance_rule": "ANY",
      "on_rejection": "TERMINAL",
      "inclusion_condition": { "op": "gte", "field": "total_debit_kobo", "value": 500000000 } }
  ],
  "routes": []
}
```

`field` is a dot-path resolved against the document, so the handler must expose a
plain attribute/property the condition can read (e.g. a `total_debit_kobo`
property on `JournalEntry`, `total` on notes/invoices, `amount` on refunds).

---

## 5. The finance workflow handler(s)

One handler per `document_type`, all sharing a thin base that does the common
work (resolve the concrete model, run posting preconditions in `validate_document`,
call the right `post_*` service in `on_approved`). New file:
`apps/vs_finance/workflow_handlers.py` (auto-discovered by the engine on startup).

```python
# apps/vs_finance/workflow_handlers.py  (sketch — not final)
from vs_workflow.handlers import register_handler
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.exceptions import InvalidInstanceStateError
from .constants import DocumentStatus

class _FinancePostOnApprove(BaseWorkflowHandler):
    """Shared: submit sets PENDING_APPROVAL; approve posts; reject/return → DRAFT."""
    status_field = "status"

    def _doc(self, instance):
        return self.document_model.objects.get(pk=instance.document_object_id)

    def resolve_default_template_code(self, document):
        return "standard"

    def validate_document(self, document, requested_by):
        if getattr(document, self.status_field) != DocumentStatus.DRAFT:
            raise InvalidInstanceStateError("Only a draft can be submitted for approval.")
        self.preflight(document)          # run the posting guards early (no write)

    def get_document_summary(self, document):
        return self.summary(document)     # {title, subtitle, fields:[...], link}

    def on_submitted(self, instance, context):
        self._set_status(instance, DocumentStatus.PENDING_APPROVAL)

    def on_approved(self, instance, context):
        doc = self._doc(instance)
        self._set_status_obj(doc, DocumentStatus.APPROVED)
        self.post(doc, actor_user=instance.requested_by)   # the real GL posting

    def on_rejected(self, instance, context):
        self._set_status(instance, DocumentStatus.DRAFT)

    def on_returned(self, instance, context):
        self._set_status(instance, DocumentStatus.DRAFT)

@register_handler("finance.journal")
class JournalHandler(_FinancePostOnApprove):
    from .models import JournalEntry as document_model
    def preflight(self, doc):  ...   # ensure_period_open, ensure_balanced, accounts active
    def post(self, doc, *, actor_user):
        from .posting import post_journal; post_journal(doc, actor_user=actor_user)
    def summary(self, doc): ...
# ... one small subclass per document_type in the §4 matrix
```

### Failure inside `on_approved` (important)

If `post_*` raises after approval (e.g. the period closed *while the doc sat in the
queue*), we must not leave a half-approved document. Two options:

- **(A) Preferred — re-validate at approval time and fail the transition.** The
  engine records the action inside a transaction; if `on_approved` raises, the
  whole approval action rolls back and the stage stays ACTIVE with an error
  surfaced to the approver ("cannot post: period JAN-2026 is closed"). The
  document stays `APPROVED`-pending and a durable finance rejection audit row is
  written (the existing `record_rejection` path). Approver retries after the block
  clears, or the requester cancels. **This needs confirmation that the engine's
  action transaction propagates a handler exception** — see Q4; if it swallows
  exceptions, we add an explicit `POSTING_FAILED` state.
- (B) Fallback — split "approve" from "post": `on_approved` only sets `APPROVED`,
  and a separate `/journals/<id>/post/` (now gated on `status == APPROVED`)
  performs the posting. More clicks, but bulletproof.

→ **Decide A vs B (Q4).** Recommendation: A, with B's `APPROVED→post` endpoint kept
as the manual retry path.

---

## 6. Segregation of duties & RBAC keys to seed

SoD is **free**: `resolve_approvers` always excludes `instance.requested_by`, so a
maker can never be their own checker. What we must add is the permission
vocabulary. New keys in `seed_finance_permissions.py` (one resource per document
class, mirroring the existing split convention):

- `finance.journal.submit`, `finance.journal.approve`, `finance.journal.approve_high_value`
- `finance.refund.approve`, `finance.write_off.approve`, `finance.credit_note.approve`,
  `finance.concession.approve`, `finance.payroll.approve`, `payments.payout.approve`, …
- Existing `*.post` keys are **retired from direct API exposure** for gated
  documents — posting becomes an engine-only side effect. (Keep them internally;
  the views that exposed `/post/` either 404 or become the §5-B manual retry,
  gated on `APPROVED`.)

The **workflow** keys (`workflow.instance.submit`, `.view`, `workflow.action.*`)
already exist and gate the shared endpoints.

---

## 7. Rollout & backward compatibility (no big-bang)

`submit_for_approval` raises `TEMPLATE_NOT_FOUND` when no template exists. We turn
that into the **opt-in switch**:

- A finance document type is "approval-gated" **iff a `WorkflowTemplate` exists**
  for it at the document's `(school, branch)` (with platform fallback).
- The submit/post views check: *is there a template?* If **yes** → must go through
  approval (direct `/post/` is refused). If **no** → today's direct-post behaviour
  is preserved unchanged.

This lets you enable approvals **one document type and one school at a time** by
publishing a template, with zero migration and zero disruption to entities that
aren't ready. A small helper `approval_required(entity, document_type) -> bool`
(checks template existence with the same cascade) centralises the gate. →
**Confirm this opt-in-by-template model (Q5).**

---

## 8. Notifications (ties into the broader "everything through vs_notifications" goal)

Approval notifications are **already** engine-native: set `notification_events`
on each template (e.g. `{"stage_activated": true, "instance_approved": true,
"instance_rejected": true, "instance_returned": true}`) and the engine's
`dispatch_notification` Celery task calls `vs_notifications` — *no finance code
sends anything directly*, which is exactly the "route all notifications through the
notification system" rule you set.

Prerequisites (tracked separately, but noted here because they gate the notify
step):

1. **Register the finance notification event types** in `vs_notifications`
   (`NotificationEventType` rows + per-channel templates), e.g.
   `finance.approval_requested`, `finance.approval_granted`,
   `finance.approval_rejected`. The engine event keys must map to these.
2. **The school-scope caveat again:** `NotificationService.send` requires a
   `school`. Platform-entity documents (`school=None`) can't dispatch school-scoped
   notifications — approver notice for platform books needs either a platform
   notifications channel or in-app-only. → **Q6.**
3. Recipients = the current stage's eligible approvers (for "requested") and the
   requester (for approved/rejected/returned). The engine already knows both.

Dunning and invoice-issued emails are the **same spine** but a different track —
covered in the next doc; they will call `NotificationService.send` with
`billing`/`finance` event keys rather than going through the workflow engine.

---

## 9. Frontend (console-fe)

- **Reuse the workflow approval inbox** rather than building a finance-specific
  one: `/v1/workflow/dashboard/pending` already returns everything a user must
  approve across modules. Finance screens add a **"Submit for approval"** button
  (replacing "Post" where a template exists) and show the new
  `PENDING_APPROVAL`/`APPROVED` status pills on journal/refund/credit-note/etc.
  lists and drawers.
- The document drawer links to its `WorkflowInstance` detail (stage history + audit)
  via `get_document_summary(...).link`.
- Gate the Submit button on `finance.<doc>.submit`; the Approve/Reject actions are
  already gated by the workflow endpoints. FE gating stays advisory — the backend
  engine is the real gate.

---

## 10. Data / migration impact

- **No new finance tables.** `WorkflowInstance` links via GenericFK, so finance
  documents need no `workflow_instance_id` column (optional denormalised FK later
  if we want fast reverse lookups).
- One tiny model change: the `school` **property** on `FinanceDocument` (no
  migration — it's a Python property).
- New permission rows (seed command update, no schema migration).
- `WorkflowTemplate` rows are **data**, published via the existing
  `/v1/workflow/templates/publish/` API or a seed command — not migrations.
- The `DocumentStatus` values already exist, so **no enum migration**.

---

## 11. Testing plan (security-first, per house rules)

1. **SoD:** requester cannot approve own document (`REQUESTER_CANNOT_APPROVE`) —
   assert 403 on self-approval for each document type.
2. **Gate integrity:** with a template present, direct `/post/` is refused; the GL
   is untouched until `on_approved` fires (assert no `JournalEntry` POSTED, no
   `AccountBalance` movement pre-approval).
3. **Threshold routing:** a ₦4m journal needs 1 approver; a ₦6m journal also needs
   the controller stage (`inclusion_condition` fires).
4. **Reject/return → DRAFT**, and re-submit works (attempt 2).
5. **Posting failure at approval time** (closed period) rolls the approval back
   (option A) or lands `POSTED` via the retry endpoint (option B).
6. **Cross-tenant:** a user of entity B cannot see/approve entity A's instances
   (school scoping + `resolve_entity` on the finance side).
7. **Backward compat:** an entity with *no* template still direct-posts exactly as
   today (regression guard).
8. **Notification events** fire on submit/approve/reject (assert `dispatch_notification`
   enqueued with the right event key; mock `vs_notifications`).

Baseline: run `vs_finance` + `vs_workflow` + `vs_payments` suites green before and
after.

---

## 12. Decisions (confirmed 2026-07-06)

- **Q1 — Approver scoping:** ✅ Accept school-level scoping (default). Entity-level
  approver isolation is a future engine extension, out of scope.
- **Q2 — Poster identity:** ✅ Default — `posted_by` = final approver,
  `created_by` = requester.
- **Q3 — First cut:** ✅ **Money-out documents** — journals, refunds, bad-debt
  write-offs, and payout batches ship together in cut 1 (the highest-risk cash-out
  paths). Remaining types (credit/debit notes, concessions, expense claims,
  payroll, budget) follow on the same base handler.
- **Q4 — Post-failure behaviour:** ✅ **Option A** — a posting failure inside
  `on_approved` rolls the approval action back and leaves the stage ACTIVE with the
  error surfaced to the approver, who retries once the block clears. *Build
  prerequisite:* confirm `vs_workflow`'s action transaction propagates a handler
  exception (see §5); if it swallows it, add an explicit `POSTING_FAILED` state and
  keep the §5-B manual retry endpoint as the fallback path.
- **Q5 — Rollout:** ✅ **Opt-in by template** — a document type is approval-gated
  only when a `WorkflowTemplate` exists for it; otherwise it direct-posts as today.
  Enable one document type + one school at a time.
- **Q6 — Platform-entity notifications:** ✅ Default — in-app only for CODEX (no
  school). A platform notifications channel is a later enhancement.
- **Q7 — Thresholds:** ✅ Ship with placeholders; tune the real ₦ figures per
  document type after cut 1 lands.

---

## 13. Suggested build order (once decisions land)

1. `school` property on `FinanceDocument` + `approval_required()` helper + tests.
2. `workflow_handlers.py` with the base + **journal** handler only (thinnest
   vertical slice), a published `finance.journal` template, and the
   `/journals/<id>/submit/` view. Prove the whole loop end-to-end.
3. Seed the approval RBAC keys.
4. Roll the remaining document types onto the base handler one at a time (§4).
5. Wire `notification_events` + register finance event types in `vs_notifications`.
6. Console-fe: Submit button + status pills + link to the workflow instance.

This keeps each PR small and independently shippable, and never leaves the GL
posting path in a half-migrated state.
