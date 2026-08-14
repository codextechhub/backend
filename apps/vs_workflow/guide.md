# vs_workflow - Study Guide

## Table of Contents
1. [What is a Workflow Engine?](#1-what-is-a-workflow-engine)
2. [Big Picture - How the Pieces Fit](#2-big-picture--how-the-pieces-fit)
3. [Data Models](#3-data-models)
4. [Lifecycle & State Machine](#4-lifecycle--state-machine)
5. [How Decisions Are Made - Routing & Conditions](#5-how-decisions-are-made--routing--conditions)
6. [Integration Guide - Wiring Up a New Module](#6-integration-guide--wiring-up-a-new-module)
7. [API Endpoints](#7-api-endpoints)
8. [Errors & What They Mean](#8-errors--what-they-mean)

---

## 1. What is a Workflow Engine?

A workflow engine automates the question: *"Who needs to approve this, and in what order?"*

Instead of hardcoding approval logic inside each feature (e.g. procurement, leave requests, staff onboarding), you define reusable **templates** that describe the stages and route a document through them automatically.

**Example scenario:**
> A staff member raises a purchase requisition. It must first be approved by a Line Manager, then by the Finance Officer. If the amount exceeds ₦500k, it also requires the School Principal. The engine handles all of this - including notifying approvers, recording votes, and calling back into procurement when a final decision is reached.

`vs_workflow` is that engine for the entire platform. Any module can plug in.

---

## 2. Big Picture - How the Pieces Fit

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
│   (handler callback fires - update your document)       │
└─────────────────────────────────────────────────────────┘
```

There are three layers:

| Layer | What it is |
|---|---|
| **Template** | The blueprint - defines stages, order, rules. Created once by an admin. |
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

The **blueprint**. Think of it like a form template - it defines the shape, not the data.

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
| `BRANCH` | Never shown to approvers. Exists only as a routing decision point - the engine passes through it instantly to evaluate routes. |

Key fields:

| Field | Meaning |
|---|---|
| `approver_source` | How approvers are resolved: `ROLE` (default - holders of a named role), `WORKFLOW_GROUP` (a named approver group), `DYNAMIC_ROLE` (role chosen by the document), or `ORGANOGRAM` (climbs the CX org chart relative to the requester). |
| `approver_role_key` | Role key (e.g. `bursar`) whose active holders approve this stage (`ROLE` source only). Resolved **inside the tenant that raised the request**, so one central template serves every tenant. On a tenant-scoped template the publish fails if the role does not exist; on a central template it cannot be checked, so `workflow_role_coverage` reports gaps instead. |
| `approver_group_code` | Approver group code (e.g. `po-approvers`) whose resolved membership approves this stage (`WORKFLOW_GROUP` source only). Publish fails if no active group with that code exists in the tenant. |
| `dynamic_role_rules` | Ordered "when this, then that role" rules (`DYNAMIC_ROLE` source only). See below. |
| `approver_scope` | `BRANCH`, `SCHOOL`, or `PLATFORM` - narrows the role query to branch-level, tenant-level, or all users. For a group, it narrows that group's `ROLE` members only. |
| `advance_rule` | `UNANIMOUS` (everyone must approve), `QUORUM` (N of M), or `ANY` (first approver wins). |
| `quorum_count` | Only used when `advance_rule = QUORUM`. Minimum approvals needed. |
| `on_rejection` | What happens when someone rejects: `TERMINAL` (ends the workflow) or `RETURN_TO_REQUESTER`. |
| `skip_if_no_approvers` | If `True` and no eligible approvers are found, the stage is auto-skipped. |
| `inclusion_condition` | JSON condition. If it evaluates to `False` for this document, the stage is skipped entirely. |

---

### WorkflowApproverGroup

A **named, reusable pool of approvers** owned by one tenant - "PO Approvers",
"Exam Board", "Leave Committee". A stage with `approver_source = WORKFLOW_GROUP`
points at one by code, and the group's membership is read live at every stage
activation: editing a group changes who approves next time, with no republish.

Membership is deliberately mixed. Each member row is one of:

| `kind` | Points at | Resolves to |
|---|---|---|
| `USER` | A specific person | That person. Static. |
| `ROLE` | A tenant role | Everyone actively assigned that role, right now. |
| `POSITION` | An organogram seat | Whoever currently holds that seat. |

`ROLE` and `POSITION` members are computed at resolution time, so staff joining
or leaving a role or a seat flow through with no group edit. Every resolved
person is filtered to the group's own tenant, so a group can never route
approval authority outside the tenant that owns it - organogram positions in
particular are platform-global seats.

A group that resolves to nobody (empty, deactivated, or every member vacant)
behaves like any other empty stage: `skip_if_no_approvers` decides whether the
stage is skipped or the workflow waits.

**Managing groups** (the "Workflow Approver" screen):

| Method | Path | Permission |
|---|---|---|
| `GET` / `POST` | `/approver-groups/` | `workflow.group.view` / `workflow.group.manage` |
| `GET` / `PATCH` / `DELETE` | `/approver-groups/{id}/` | view / manage |
| `POST` | `/approver-groups/{id}/members/` | manage |
| `DELETE` | `/approver-groups/{id}/members/{member_id}/` | manage |
| `GET` | `/approver-groups/{id}/resolve/` | view |

`resolve/` returns the live per-member breakdown ("this role resolves to 3
people") plus the de-duplicated union of everyone the group currently reaches.
It runs the engine's own resolution, so the screen can never disagree with what
an activation will do. Pass `?branch=<id>` to preview branch narrowing.

Deleting a group that an active stage still references returns `409` with
`APPROVER_GROUP_IN_USE`; deactivate it instead, which keeps the stage resolvable
(to nobody) and preserves audit history. A group's `code` is immutable once
created, because published templates reference it.

---

### Platform templates and a tenant's own version

A template published with **no tenant** is the *platform* template: one shared
definition every tenant runs until it adjusts its own. Publishing it is a
platform act, so the publish payload carries `scope`:

| `scope` | Writes | Who may |
|---|---|---|
| `TENANT` (default) | a template owned by the calling tenant | anyone with `workflow.template.manage` |
| `PLATFORM` | the shared, tenant-less template | only an actor whose tenant is `PLATFORM` |

This distinction is load-bearing. The platform (Codex) is itself a tenant, so
without `scope=PLATFORM` every "master" it published would have been its own
private template that no other tenant inherits, and the cascade's last step -
`tenant=None` - would never be reached.

When a tenant publishes over a shared template's `(document_type, code)`, it
gets its own version, which the cascade prefers from then on. That is the
intended flexibility, not an accident: the tenant adjusts the flow it runs
without touching anybody else's. The consequence to be honest about on screen
is that later changes to the platform template no longer reach that tenant.
`GET /templates/` says where each one stands:

| Field | Meaning |
|---|---|
| `is_platform` | This row is the shared definition. |
| `tenant_has_own` | On a platform row: this tenant is running its own version instead. |
| `platform_updated_at` | On a tenant row: when the shared version it came from last changed. |
| `platform_changed_since` | On a tenant row: the shared version moved on after this tenant last saved. |

**Seeing who runs it** (platform actors only, read-only):

| Method | Path | Answers |
|---|---|---|
| `GET` | `/templates/{id}/adoption/` | How many tenants run this as published, and which ones run their own. |
| `GET` | `/templates/{id}/compare/?with=<template id>` | How one tenant's version differs from the shared one. |

Both refuse a caller whose own tenant is not `PLATFORM` (`PLATFORM_ONLY`) and
refuse a subject that is not the shared template (`NOT_PLATFORM_TEMPLATE`).
`compare` additionally checks that the other template is an active tenant
version of the *same* `(document_type, code)`, so it cannot be used to read an
arbitrary tenant's template by guessing an id, and it answers the same 404 for
"no such template" and "not a version of this one". It returns configuration
only - stages, approvers, rules, routing - never documents, approvals or people.
`adoption` counts tenants rather than templates: a tenant with both a
branch-level and a tenant-level version has still adjusted the path once.

**Going back to the shared version**: `POST /templates/{id}/use-platform-version/`
switches the tenant's own version off (`is_active=False`) rather than deleting
it - instances PROTECT the template they ran under, so the version that has
actually been used is precisely the one that cannot be deleted. Inactive
templates are skipped by the cascade, so the next request falls through to the
platform template; publishing again brings the tenant's version back. The call
returns the platform template now in force. It refuses on a platform template
(`ALREADY_PLATFORM`) and when no platform version exists to fall back to
(`NO_PLATFORM_VERSION`, 409) - switching off in that case would leave the
document type with no template at all.

---

### Central templates and tenant overrides

A template published with **no tenant** is *central*: one definition, shared by
every tenant, selected by the usual cascade (branch → tenant → central). Its
stages name approvers by **role key**, and the key is resolved inside whichever
tenant raised the document. So a central "Spend Approval" step that names
`procurement-approver` reaches a different set of people in every tenant, which
is the point.

The consequence to plan for: a tenant with no role of that key resolves to
nobody. Where the stage is set to `skip_if_no_approvers`, the request then
passes through unapproved. Run this before relying on a central template:

```
python manage.py workflow_role_coverage           # report gaps
python manage.py workflow_role_coverage --create  # create the missing roles
```

`--create` makes the role but assigns nobody, so the report keeps flagging it
until an admin assigns someone. It will not invent approval authority.

**A tenant can repoint any central step** without cloning the template:

| Method | Path |
|---|---|
| `GET` / `POST` | `/stage-approvers/` |
| `GET` / `PATCH` / `DELETE` | `/stage-approvers/{id}/` |

An override names either a role key or one of the tenant's approver groups, and
the engine consults it before the stage's own configuration. Only *who approves*
changes; advance rule, rejection policy and routing stay with the template.
Deleting the override restores the template's own approver. Overrides need
`workflow.template.manage`, because repointing an approval step is a
template-level decision.

---

### WorkflowStageDynamicRule

Rules for a stage with `approver_source = DYNAMIC_ROLE`, where **the document
picks the role and the role picks the people**. One stage covers what would
otherwise need two templates or an extra branch stage:

```json
{
  "code": "spend-approval",
  "label": "Spend Approval",
  "approver_source": "DYNAMIC_ROLE",
  "dynamic_role_rules": [
    {"condition": {"op": "lt",  "field": "amount", "value": 100000},
     "role_key": "finance-officer"},
    {"condition": {"op": "lt",  "field": "amount", "value": 1000000},
     "role_key": "bursar"},
    {"condition": null, "role_key": "principal"}
  ]
}
```

Rules are evaluated in order and **the first match wins**, the same contract as
route paths. A rule with `"condition": null` always matches, so it is the
fallback and must be last; publishing rejects anything after it, because those
rules could never fire. Conditions use the full condition language (`op`,
`all`, `any`, `not`, `fn`) that routes and `inclusion_condition` already use.

Once a rule wins, its role resolves exactly like the `ROLE` source: active
assignees, `approver_scope` narrowing, requester excluded, delegation applied.
No rule matching and no fallback means no approvers, which
`skip_if_no_approvers` then decides on.

**Publishing validates conditions.** A typo like `"op": "greater_than"`, a
missing `field`, an `in` without a list value, or an unregistered `fn` key now
fails the publish. This applies to route and inclusion conditions too - previously
a bad operator saved happily and only raised mid-approval, leaving the workflow
stuck.

**Why did this go to the Bursar?** Two answers:

- Before publishing: `POST /templates/preview-approvers/` with
  `approver_source: "DYNAMIC_ROLE"`, the rules, and a `sample_document`
  (e.g. `{"amount": 250000}`). It returns the matched role, the resolved
  people, and a per-rule trace showing what each condition compared.
- After the fact: the `STAGE_ACTIVATED` audit entry carries a `dynamic_role`
  block with the matched rule and the same trace.

---

### WorkflowRoutePath

A **directed edge** between stages. If a template has no routes, the engine uses linear order (stage.order ascending).

| Field | Meaning |
|---|---|
| `from_stage` | Source stage. `null` means this is an entry edge (fires first). |
| `to_stage` | Destination stage. `null` means exit - instance terminates as APPROVED. |
| `condition` | JSON condition evaluated against the document. First matching route wins. |
| `order` | Routes are evaluated in ascending order until one matches. |

---

### WorkflowInstance

**One live execution** - created the moment a document is submitted.

```
DRAFT ──► SUBMITTED ──► IN_PROGRESS ──► APPROVED   (terminal)
                     │              └──► REJECTED   (terminal)
                     └──► RETURNED  ──► WITHDRAWN   (terminal - requester gave up)
                          (requester amends & resubmits)
                                     └──► CANCELLED  (terminal - admin killed it)
```

| Field | Meaning |
|---|---|
| `document_content_type` + `document_object_id` | Generic FK to your document (any model). |
| `document_type` | Denormalised copy of the type string - for fast filtering without a join. |
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

A **snapshot** of who was eligible to act when a stage was activated. Never updated - historical record.

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
  [Stage ACTIVE - waiting for votes]                         │
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

## 5. How Decisions Are Made - Routing & Conditions

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

A `BRANCH` stage is just a decision point - it is always skipped and never shown to approvers. Its purpose is to give the route evaluator a `from_stage` to branch from.

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

**No condition (`null`):** always matches - used for unconditional routes.

### Inclusion conditions

`WorkflowStage.inclusion_condition` controls whether a stage is included at all for a given document.

```json
{ "op": "gte", "field": "amount", "value": 500000 }
```
If this evaluates to `False`, the stage is **skipped** entirely - not shown to any approver.

---

## 6. Integration Guide - Wiring Up a New Module

To connect a new module (e.g. `vs_leave`), you need four things:

### Step 1 - Declare `workflow_document_type` on your model

```python
# vs_leave/models.py
class LeaveRequest(models.Model):
    ...
    workflow_document_type = "leave.request"  # class attribute, not a DB field
```

### Step 2 - Create `workflow_handlers.py` in your app

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

### Step 3 - Optionally create `workflow_conditions.py`

Only needed if your routes or stages use custom `fn` conditions.

```python
# vs_leave/workflow_conditions.py
from vs_workflow.conditions import register_condition

@register_condition("leave.is_long_leave")
def is_long_leave(document, args):
    threshold = args.get("days", 10)
    return document.duration_days >= threshold
```

### Step 4 - Publish or update a template via the API

Templates are created-or-updated in place - calling publish again with the same `(school, document_type, code)` key updates the existing template rather than creating a new version. There is no versioning. Stages are upserted by `code`; routes are replaced entirely.

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
      "approver_role_key": "line-manager",
      "approver_scope": "SCHOOL",
      "advance_rule": "ANY",
      "on_rejection": "RETURN_TO_REQUESTER"
    },
    {
      "code": "hr",
      "label": "HR Final Approval",
      "kind": "APPROVAL",
      "order": 2,
      "approver_role_key": "hr-manager",
      "approver_scope": "SCHOOL",
      "advance_rule": "ANY",
      "on_rejection": "TERMINAL"
    }
  ],
  "routes": []
}
```

No `routes` needed for a simple linear flow - stages run in `order` order. Calling this endpoint again with the same `document_type` + `code` will update the template in place.

### Step 5 - Submit a document

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
| `TEMPLATE_INVALID` | Template configuration is broken - e.g. no stages, a cycle, or unmatched routes. |
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
