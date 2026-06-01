# vs_workflow — Study Guide

## Table of Contents
1. [What is a Workflow Engine?](#1-what-is-a-workflow-engine)
2. [Big Picture — How the Pieces Fit](#2-big-picture--how-the-pieces-fit)
3. [Data Models](#3-data-models)
4. [Lifecycle & State Machine](#4-lifecycle--state-machine)
5. [How Decisions Are Made — Routing & Conditions](#5-how-decisions-are-made--routing--conditions)
6. [Integration Guide — Wiring Up a New Module](#6-integration-guide--wiring-up-a-new-module)
7. [API Endpoints](#7-api-endpoints)
8. [Errors & What They Mean](#8-errors--what-they-mean)

---

## 1. What is a Workflow Engine?

A workflow engine automates the question: *"Who needs to approve this, and in what order?"*

Instead of hardcoding approval logic inside each feature (e.g. procurement, leave requests, staff onboarding), you define reusable **templates** that describe the stages and route a document through them automatically.

**Example scenario:**
> A staff member raises a purchase requisition. It must first be approved by a Line Manager, then by the Finance Officer. If the amount exceeds ₦500k, it also requires the School Principal. The engine handles all of this — including notifying approvers, recording votes, and calling back into procurement when a final decision is reached.

`vs_workflow` is that engine for the entire platform. Any module can plug in.

---

## 2. Big Picture — How the Pieces Fit

```
┌─────────────────────────────────────────────────────────┐
│                     Your Module                         │
│   (e.g. vs_procurement, vs_hr, vs_leave)                │
│                                                         │
│  1. Document model declares workflow_document_type      │
│  2. Registers a Handler (workflow_handlers.py)          │
│  3. Optionally registers Conditions (workflow_conditions│
└──────────────────────────┬──────────────────────────────┘
                           │  submit_for_approval(document, user)
                           ▼
┌─────────────────────────────────────────────────────────┐
│                   vs_workflow Engine                    │
│                                                         │
│  WorkflowTemplate  ──►  WorkflowInstance               │
│     (blueprint)            (one running job)            │
│                                                         │
│  WorkflowStage  ──►  WorkflowStageInstance             │
│     (one step)          (that step in this job)         │
│                                                         │
│  Routes + Conditions decide which stage comes next      │
│  Approvers vote → engine tallies → advances or ends     │
└──────────────────────────┬──────────────────────────────┘
                           │  on_approved(instance) / on_rejected(instance)
                           ▼
┌─────────────────────────────────────────────────────────┐
│                     Your Module                         │
│   (handler callback fires — update your document)       │
└─────────────────────────────────────────────────────────┘
```

There are three layers:

| Layer | What it is |
|---|---|
| **Template** | The blueprint — defines stages, order, rules. Created once by an admin. |
| **Instance** | One live execution of a template against one document. Created per submission. |
| **Handler** | Your module's code. The engine calls it at lifecycle events (submitted, approved, etc.). |

---

## 3. Data Models

There are 9 models. Here is how they relate:

```
WorkflowTemplate
    │
    ├── WorkflowStage (many)          ← steps in the blueprint
    └── WorkflowRoutePath (many)      ← directed edges between stages

WorkflowInstance (one per submission)
    │  points to → WorkflowTemplate
    │  points to → document (via GenericForeignKey)
    │
    └── WorkflowStageInstance (one per stage reached)
            │
            ├── WorkflowStageApprover (snapshot: who was eligible)
            └── WorkflowStageAction   (actual votes recorded)

ApprovalDelegation                    ← User A delegates to User B
WorkflowAuditLog                      ← append-only event log per instance
```

---

### WorkflowTemplate

The **blueprint**. Think of it like a form template — it defines the shape, not the data.

| Field | Meaning |
|---|---|
| `document_type` | Dotted string like `"procurement.purchase_order"`. Links to your module. |
| `code` | Slug like `"standard"` or `"high_value"`. Multiple templates can exist per document_type. |
| `school` | Optional. If set, this template belongs to a specific school. `null` = platform-wide. |
| `notification_events` | Dict of event keys → bool. Controls which events trigger notifications. |

---

### WorkflowStage

One **step** in a template. There are two kinds:

| `kind` | What it does |
|---|---|
| `APPROVAL` | Waits for approvers to vote. The engine pauses here until the advance rule is satisfied. |
| `BRANCH` | Never shown to approvers. Exists only as a routing decision point — the engine passes through it instantly to evaluate routes. |

Key fields:

| Field | Meaning |
|---|---|
| `approver_permission_key` | RBAC permission key used to resolve who can approve this stage. |
| `approver_scope` | `BRANCH`, `SCHOOL`, or `PLATFORM` — narrows the RBAC query to branch-level, school-level, or all users platform-wide. |
| `advance_rule` | `UNANIMOUS` (everyone must approve), `QUORUM` (N of M), or `ANY` (first approver wins). |
| `quorum_count` | Only used when `advance_rule = QUORUM`. Minimum approvals needed. |
| `on_rejection` | What happens when someone rejects: `TERMINAL` (ends the workflow) or `RETURN_TO_REQUESTER`. |
| `skip_if_no_approvers` | If `True` and no eligible approvers are found, the stage is auto-skipped. |
| `inclusion_condition` | JSON condition. If it evaluates to `False` for this document, the stage is skipped entirely. |

---

### WorkflowRoutePath

A **directed edge** between stages. If a template has no routes, the engine uses linear order (stage.order ascending).

| Field | Meaning |
|---|---|
| `from_stage` | Source stage. `null` means this is an entry edge (fires first). |
| `to_stage` | Destination stage. `null` means exit — instance terminates as APPROVED. |
| `condition` | JSON condition evaluated against the document. First matching route wins. |
| `order` | Routes are evaluated in ascending order until one matches. |

---

### WorkflowInstance

**One live execution** — created the moment a document is submitted.

```
DRAFT ──► SUBMITTED ──► IN_PROGRESS ──► APPROVED   (terminal)
                     │              └──► REJECTED   (terminal)
                     └──► RETURNED  ──► WITHDRAWN   (terminal — requester gave up)
                          (requester amends & resubmits)
                                     └──► CANCELLED  (terminal — admin killed it)
```

| Field | Meaning |
|---|---|
| `document_content_type` + `document_object_id` | Generic FK to your document (any model). |
| `document_type` | Denormalised copy of the type string — for fast filtering without a join. |
| `current_stage` | The stage the engine is waiting on right now. `null` when terminal. |
| `status` | See state machine above. |
| `state_version` | Incremented on every transition. Useful for detecting stale reads. |
| `school` / `branch` | Optional scoping. Null = platform-level. |

---

### WorkflowStageInstance

Created for each stage the engine **reaches** during a given instance. If the same stage is revisited (after a RETURN → resubmit), a new row is created with a higher `attempt` number.

| Field | Meaning |
|---|---|
| `status` | `PENDING` → `ACTIVE` → `APPROVED` / `REJECTED` / `SKIPPED` |
| `attempt` | 1 for the first pass. 2 if the stage was returned and re-entered, etc. |
| `skip_reason` | Set when `status = SKIPPED`. E.g. `"inclusion_condition_false"`, `"zero_eligible_approvers"`. |

---

### WorkflowStageApprover

A **snapshot** of who was eligible to act when a stage was activated. Never updated — historical record.

> Why a snapshot? Because RBAC roles can change. The audit trail must reflect who was eligible *at the time*, not who has the role today.

---

### WorkflowStageAction

Every **vote** an approver records. Also used for admin reversals.

| `action` | Meaning |
|---|---|
| `APPROVED` | Positive vote. Counted toward the advance rule. |
| `REJECTED` | Negative vote. Triggers `on_rejection` behaviour on the stage. |
| `RETURNED` | Sends the instance back to the requester for amendment. |

Reversals: when an admin reverses a vote, the original row gets `reversed_at` stamped and a new `WorkflowStageAction` row is created with `is_reversal_of` pointing to the original.

---

### ApprovalDelegation

User A grants User B authority to approve on their behalf for a date range.

- `exclusive = True` means User A is removed from the eligible list for the duration.
- Can be scoped to a specific `document_type` or left blank for all types.

---

### WorkflowAuditLog

Append-only. Every material engine event writes a row. Never updated or deleted. Used for auditing, debugging, and notification dispatch.

---

## 4. Lifecycle & State Machine

### The full journey of one document

```
  [User submits document]
          │
          ▼
  submit_for_approval()
    - reads document.school, document.workflow_document_type
    - finds WorkflowTemplate matching (school, document_type, code)
    - creates WorkflowInstance (status=SUBMITTED)
    - calls handler.on_submitted()
    - calls advance_instance()
          │
          ▼
  advance_instance()  ◄─────────────────────────────────────┐
    - picks next stage via _pick_next_stage()                │
    - if no next stage → APPROVED (done)                     │
    - if BRANCH stage → skip, loop back                      │
    - if inclusion_condition fails → skip, loop back         │
    - if no eligible approvers + skip_if_no_approvers → skip │
    - else: activate stage, set status=IN_PROGRESS, STOP     │
          │                                                  │
          ▼                                                  │
  [Stage ACTIVE — waiting for votes]                         │
          │                                                  │
    approver calls record_action(APPROVED/REJECTED/RETURNED) │
          │                                                  │
          ├── RETURNED ──────────────────────────────────────────► status=RETURNED
          │                                                  │      requester amends
          │                                                  │      resubmit() resumes
          │                                                  │      from same stage, attempt+1
          ├── REJECTED                                       │
          │     └── on_rejection=TERMINAL ──────────────────────► status=REJECTED (done)
          │     └── on_rejection=RETURN_TO_REQUESTER ────────────► status=RETURNED
          │                                                  │
          └── APPROVED (advance rule satisfied) ─────────────┘
                stage.status=APPROVED, loop back to advance_instance()
```

---

### Stage advance rules

```
Advance rule: UNANIMOUS
  All 3 eligible approvers must APPROVE before the stage resolves.
  ┌──────────┬────────────────────────────────────────┐
  │ Approver │ Vote                                   │
  ├──────────┼────────────────────────────────────────┤
  │  Alice   │ APPROVED ✓                             │
  │  Bob     │ APPROVED ✓                             │
  │  Carol   │ (waiting...)                           │
  └──────────┴────────────────────────────────────────┘
  → Stage stays ACTIVE until Carol votes.

Advance rule: ANY
  First APPROVE resolves the stage immediately.

Advance rule: QUORUM (quorum_count = 2)
  First 2 APPROVEs resolve the stage.
```

---

### What "attempt" means

```
Attempt 1:
  Stage activated → Bob votes RETURNED → instance goes to RETURNED.

Requester fixes document and calls resubmit().

Attempt 2:
  Same stage re-activated with a fresh approver snapshot.
  Previous votes are still visible in audit logs but do not count.
```

---

## 5. How Decisions Are Made — Routing & Conditions

### Linear routing (no routes defined)

Stages run in `order` ascending. Simple, most common.

```
Stage 1 (order=1) → Stage 2 (order=2) → Stage 3 (order=3) → APPROVED
```

### Route-based routing (routes defined)

Routes are evaluated in `order` ascending. First route whose condition matches wins.

```
                    ┌─── Route A: condition={amount < 500000} ──► Stage: Line Manager
  [BRANCH stage] ───┤
                    └─── Route B: condition={amount >= 500000} ─► Stage: Finance + Principal
```

A `BRANCH` stage is just a decision point — it is always skipped and never shown to approvers. Its purpose is to give the route evaluator a `from_stage` to branch from.

### Conditions

Conditions are JSON objects stored on `WorkflowRoutePath.condition` and `WorkflowStage.inclusion_condition`.

**Simple operator check:**
```json
{ "op": "gte", "field": "amount", "value": 500000 }
```
`field` is a dot-path resolved against the document object.

**Compound logic:**
```json
{
  "all": [
    { "op": "gte", "field": "amount", "value": 500000 },
    { "op": "eq",  "field": "category", "value": "CAPITAL" }
  ]
}
```

Supported: `all` (AND), `any` (OR), `not`.

**Custom function:**
```json
{ "fn": "procurement.is_urgent", "args": { "threshold_days": 3 } }
```
The function must be registered via `@register_condition("procurement.is_urgent")` in your module's `workflow_conditions.py`.

**No condition (`null`):** always matches — used for unconditional routes.

### Inclusion conditions

`WorkflowStage.inclusion_condition` controls whether a stage is included at all for a given document.

```json
{ "op": "gte", "field": "amount", "value": 500000 }
```
If this evaluates to `False`, the stage is **skipped** entirely — not shown to any approver.

---

## 6. Integration Guide — Wiring Up a New Module

To connect a new module (e.g. `vs_leave`), you need four things:

### Step 1 — Declare `workflow_document_type` on your model

```python
# vs_leave/models.py
class LeaveRequest(models.Model):
    ...
    workflow_document_type = "leave.request"  # class attribute, not a DB field
```

### Step 2 — Create `workflow_handlers.py` in your app

```python
# vs_leave/workflow_handlers.py
from vs_workflow.handlers import register_handler
from vs_workflow.handlers.base import BaseWorkflowHandler

@register_handler("leave.request")
class LeaveRequestHandler(BaseWorkflowHandler):

    def resolve_default_template_code(self, document):
        # Return the template code to use. Can vary per document.
        return "standard"

    def validate_document(self, document, requested_by):
        # Raise InvalidInstanceStateError if the document is not ready.
        if document.status != "DRAFT":
            from vs_workflow.exceptions import InvalidInstanceStateError
            raise InvalidInstanceStateError("Only DRAFT leave requests can be submitted.")

    def on_submitted(self, instance, context):
        # Called the moment the instance is created.
        pass

    def on_approved(self, instance, context):
        # Called when the workflow fully approves. Update your document here.
        LeaveRequest.objects.filter(pk=instance.document_object_id).update(status="APPROVED")

    def on_rejected(self, instance, context):
        LeaveRequest.objects.filter(pk=instance.document_object_id).update(status="REJECTED")

    def on_returned(self, instance, context):
        LeaveRequest.objects.filter(pk=instance.document_object_id).update(status="NEEDS_AMENDMENT")

    def on_withdrawn(self, instance, context):
        pass

    def on_cancelled(self, instance, context):
        pass
```

The engine auto-discovers `workflow_handlers.py` in every installed app on startup (via `autodiscover_modules("workflow_handlers")` in `VsWorkflowConfig.ready()`).

### Step 3 — Optionally create `workflow_conditions.py`

Only needed if your routes or stages use custom `fn` conditions.

```python
# vs_leave/workflow_conditions.py
from vs_workflow.conditions import register_condition

@register_condition("leave.is_long_leave")
def is_long_leave(document, args):
    threshold = args.get("days", 10)
    return document.duration_days >= threshold
```

### Step 4 — Publish or update a template via the API

Templates are created-or-updated in place — calling publish again with the same `(school, document_type, code)` key updates the existing template rather than creating a new version. There is no versioning. Stages are upserted by `code`; routes are replaced entirely.

```
POST /v1/workflow/templates/publish/
{
  "document_type": "leave.request",
  "code": "standard",
  "name": "Standard Leave Approval",
  "stages": [
    {
      "code": "line-manager",
      "label": "Line Manager Approval",
      "kind": "APPROVAL",
      "order": 1,
      "approver_permission_key": "leave.approve.line_manager",
      "approver_scope": "SCHOOL",
      "advance_rule": "ANY",
      "on_rejection": "RETURN_TO_REQUESTER"
    },
    {
      "code": "hr",
      "label": "HR Final Approval",
      "kind": "APPROVAL",
      "order": 2,
      "approver_permission_key": "leave.approve.hr",
      "approver_scope": "SCHOOL",
      "advance_rule": "ANY",
      "on_rejection": "TERMINAL"
    }
  ],
  "routes": []
}
```

No `routes` needed for a simple linear flow — stages run in `order` order. Calling this endpoint again with the same `document_type` + `code` will update the template in place.

### Step 5 — Submit a document

```python
from vs_workflow.services.submission import submit_for_approval

instance = submit_for_approval(document=leave_request, requested_by=request.user)
```

Or via the API:
```
POST /v1/workflow/instances/
{
  "content_type_id": 42,
  "object_id": "uuid-of-leave-request"
}
```

---

## 7. API Endpoints

All endpoints are under `/v1/workflow/`.

### Templates

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `GET` | `/templates/` | `workflow.template.view` | List all templates (scoped to school if set). |
| `GET` | `/templates/{id}/` | `workflow.template.view` | Retrieve a single template with stages and routes. |
| `POST` | `/templates/publish/` | `workflow.template.manage` | Create or update a template in place. |

### Instances

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `GET` | `/instances/` | `workflow.instance.view` | List instances. Supports `?document_type=`, `?status=`, `?requested_by=`, `?template_code=`. |
| `GET` | `/instances/{id}/` | `workflow.instance.view` | Full detail including stage history and audit log. |
| `POST` | `/instances/` | `workflow.instance.submit` | Submit a document for approval. |
| `POST` | `/instances/{id}/withdraw/` | Authenticated | Requester withdraws their own submission. |
| `POST` | `/instances/{id}/resubmit/` | Authenticated | Requester resubmits after RETURNED. |
| `POST` | `/instances/{id}/cancel/` | `workflow.instance.cancel` | Admin cancels a stuck instance. Body: `{ "reason": "..." }`. |
| `POST` | `/instances/{id}/actions/` | Authenticated | Approver records a vote. Body: `{ "action": "APPROVED" \| "REJECTED" \| "RETURNED", "comment": "..." }`. |

### Reverse Action

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `POST` | `/actions/{id}/reverse/` | `workflow.action.reverse` | Admin reverses a recorded vote. Re-activates the stage. Body: `{ "reason": "..." }`. |

### Dashboards

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `GET` | `/dashboard/pending/` | Authenticated | Instances where the current user is an eligible approver. |
| `GET` | `/dashboard/submitted/` | Authenticated | Instances the current user has submitted. |
| `GET` | `/dashboard/team-load/` | `workflow.instance.view` | Active instance count grouped by document type and stage. |

### Delegations

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| `GET` | `/delegations/` | Authenticated | Lists delegations. Admins see all; others see only their own. |
| `POST` | `/delegations/` | Authenticated | Create a delegation. Requester is automatically the delegator. |
| `PUT/PATCH` | `/delegations/{id}/` | Authenticated | Update a delegation (own only, or admin). |
| `DELETE` | `/delegations/{id}/` | Authenticated | Delete a delegation. |
| `POST` | `/delegations/{id}/revoke/` | Authenticated | Revoke (soft-delete) a delegation. |

---

## 8. Errors & What They Mean

All errors come back as:
```json
{ "error_code": "SOME_CODE", "message": "...", "field": null, "meta": {} }
```

| Error code | When it happens |
|---|---|
| `TEMPLATE_NOT_FOUND` | No active template exists for this `(school, document_type, code)` combination. |
| `TEMPLATE_INVALID` | Template configuration is broken — e.g. no stages, a cycle, or unmatched routes. |
| `UNKNOWN_DOCUMENT_TYPE` | `submit_for_approval` was called but no handler was registered for this document type. |
| `INVALID_INSTANCE_STATE` | Action attempted on an instance in the wrong status (e.g. resubmit on a non-RETURNED instance). |
| `INSTANCE_TERMINAL` | Action attempted on an instance that already finished (APPROVED, REJECTED, etc.). |
| `STAGE_NOT_ACTIVE` | Vote recorded but no stage is currently ACTIVE on this instance. |
| `NOT_ELIGIBLE_APPROVER` | The user trying to vote is not on the eligible approver snapshot. |
| `REQUESTER_CANNOT_APPROVE` | The user who submitted is trying to approve their own document. |
| `DUPLICATE_APPROVER_ACTION` | This user already voted on the current attempt of this stage. |
| `REVERSAL_NOT_ALLOWED` | Tried to reverse a row that is already reversed, or a reversal row itself. |
| `CANCELLATION_NOT_ALLOWED` | Cancel was called without a reason, or on a terminal instance. |
| `UNKNOWN_OPERATOR` | A condition JSON used an operator not in the supported set. |
| `UNKNOWN_CONDITION_FUNCTION` | A `fn` condition referenced a key not registered in the condition registry. |
