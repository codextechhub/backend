# vs_workflow — The Story Guide

*Everything vs_workflow does, explained through the life of Temi.*

---

## Meet Temi

Temi is 15. He goes to Greenfield Secondary School in Lagos during the day. After school, he runs a small phone accessories business — he sells phone cases, earphones, and phone chargers from a bag he carries around the estate. Business is booming.

One day, Temi realises he needs a system. When he wants to buy new stock worth over ₦20,000, his mum wants to approve it first. When one of his "delivery guys" (his younger cousins) carries stock to sell, his aunty at the gate has to sign off. And when a customer in school wants to pay with a cheque (yes, one actually tried), his teacher and the school bursar both have to agree before Temi hands anything over.

Temi's life is basically a workflow engine. He just doesn't know it yet.

---

## Part 1 — The Big Picture (What the Engine Does)

Every time something important needs to happen in the platform, it has to pass through a chain of approvals. Someone submits it. Someone reviews it. Someone else may also review it. Eventually, it either gets approved or it doesn't.

Without a workflow engine, every single feature — leave requests, purchase orders, school admissions — would need to build its own approval chain from scratch. That would be chaos.

**vs_workflow is the one engine everyone shares.**

Think of it like Temi's mum. She doesn't need to understand earphones or chargers. She just needs to know: *"Is this a reasonable request?"* and *"Who else needs to agree?"*. Once she has a template for how she makes these decisions, every request Temi brings follows the same path.

Here is the big picture:

```
Your Feature (e.g. Leave Requests, Purchase Orders)
        │
        │  "Hey engine, I need approval for this."
        ▼
   vs_workflow Engine
        │  Looks up the blueprint (WorkflowTemplate)
        │  Finds the right approvers
        │  Waits for votes
        │  Notifies everyone
        │
        └─► Calls back your feature: "Approved!" or "Rejected."
```

---

## Part 2 — The Blueprint (WorkflowTemplate)

When Temi started his business, his mum sat him down and said:

> "Any purchase over ₦5,000: come to me first. Over ₦20,000: come to me AND your dad. Under ₦5,000: you decide."

She wrote this down on a piece of paper and stuck it on the fridge. That paper is a **WorkflowTemplate**.

A `WorkflowTemplate` is the master plan for how a particular type of request gets approved. It belongs to:

- A **document type** — what kind of request is this? (`"procurement.purchase_order"`, `"leave.request"`)
- A **code** — which flavour of template? (`"standard"`, `"high_value"`) — because Temi's mum might have different rules for electronics vs. accessories
- A **school** (optional) — is this rule just for Greenfield School, or for Temi's whole estate?

The template contains **stages** (the people who have to check) and **routes** (the rules for which stage comes next).

When you publish a template, you are writing the rule on the fridge. If you need to change the rule, you just update it — no new paper, no version numbers. The old instances running with the old rules are already mid-journey; only new submissions use the updated template.

---

## Part 3 — The Steps (WorkflowStage)

Each step in Temi's approval chain is a `WorkflowStage`.

Temi's ₦20,000 purchase chain has two stages:
1. **Mum** — checks if the amount is reasonable
2. **Dad** — gives final financial sign-off

Each stage has rules:

**Who can approve?**
Every stage has an `approver_permission_key` — a role name like `"procurement.approve.finance"`. The engine finds all users who have that role and puts them on the approver list.

**How many people need to say yes?** (`advance_rule`)
- `ANY` — first person to approve, it moves on. Like Temi's mum — if she approves first, done.
- `UNANIMOUS` — everyone must approve. Used for high-stakes decisions.
- `QUORUM` — a set number must approve. E.g. "2 out of 3 Finance Officers".

**What happens if someone says no?** (`on_rejection`)
- `TERMINAL` — request is dead. Hard stop.
- `RETURN_TO_REQUESTER` — send it back to Temi to fix and resubmit.

**Who counts as an approver?** (`approver_scope`)
This narrows down the search for people with the right permission:
- `SCHOOL` — only people from Temi's school
- `BRANCH` — only people from Temi's specific branch or department
- `PLATFORM` — anyone on the entire platform (for platform-wide roles)

**What if nobody has the role?** (`skip_if_no_approvers`)
If `True`, the stage is quietly skipped. Like if Temi's dad is travelling — mum handles it alone.

**Should this stage even run?** (`inclusion_condition`)
A condition that checks the document first. If it returns `False`, the stage is skipped entirely. E.g. "only run the Finance Director stage if amount >= ₦500,000."

---

## Part 4 — The Road Map (WorkflowRoutePath)

Sometimes approval isn't a straight line. Temi's mum might say:

> "If it's under ₦5,000, just go to Uncle Chidi. If it's over ₦5,000, go to me AND your dad."

This branching is handled by `WorkflowRoutePath` — a directed edge between stages.

```
                       ┌──── Route A: amount < 5000 ─────► Uncle Chidi
  [Decision Point] ────┤
                       └──── Route B: amount >= 5000 ────► Mum → Dad
```

The decision point is a special stage with `kind = BRANCH`. It has no approvers — the engine zips through it instantly and just uses it to evaluate which route to take.

If no routes are defined, the engine runs stages in numeric order — simple and linear, like Temi's standard chain.

---

## Part 5 — The Conditions (How the Engine Decides)

Routes and stages can have **conditions** — JSON rules that the engine evaluates against the actual document.

Temi writes a note to the engine:

**Simple check:**
```json
{ "op": "gte", "field": "amount", "value": 5000 }
```
*"Is the amount ≥ 5,000?"*

**All of these must be true (AND):**
```json
{
  "all": [
    { "op": "gte", "field": "amount", "value": 5000 },
    { "op": "eq",  "field": "category", "value": "ELECTRONICS" }
  ]
}
```

**Custom function:**
```json
{ "fn": "procurement.is_urgent", "args": { "threshold_days": 2 } }
```
Your module can register custom condition functions using `@register_condition("procurement.is_urgent")` in a file called `workflow_conditions.py`. The engine auto-discovers it on startup.

---

## Part 6 — The Running Job (WorkflowInstance)

Every time Temi submits a request, the engine creates a `WorkflowInstance`. This is the live version — the actual journey of one specific request through the approval chain.

Think of it like a file in a school registry. The template is the form design. The instance is the completed, stamped, signed form currently sitting on someone's desk.

**The States of a WorkflowInstance:**

```
Temi writes request (DRAFT)
        │
        ▼
Temi submits it (SUBMITTED)
        │
        ▼
Engine activates first stage (IN_PROGRESS)
        │
        ├──► Mum says YES, Dad says YES → APPROVED ✓ (done, celebrate)
        │
        ├──► Mum says NO → REJECTED ✗ (done, sad)
        │
        ├──► Mum says "Fix this" → RETURNED
        │         │
        │         └──► Temi fixes it → resubmits → back to IN_PROGRESS
        │
        ├──► Temi changes his mind → WITHDRAWN (he cancelled it himself)
        │
        └──► Admin kills it for some reason → CANCELLED
```

`APPROVED`, `REJECTED`, `WITHDRAWN`, and `CANCELLED` are **terminal** — once an instance reaches any of these, it is done forever. Nothing can change it.

---

## Part 7 — The Desk Record (WorkflowStageInstance)

For every stage that the engine **reaches** during a run, it creates a `WorkflowStageInstance`. This is like a "currently on your desk" slip.

If the request gets RETURNED and Temi resubmits it, the engine creates a brand new desk slip for the same stage — this time with `attempt = 2`. The old votes from attempt 1 are still in the system for auditing, but they don't count for the new attempt.

---

## Part 8 — The Eligible Voters (WorkflowStageApprover)

When a stage activates, the engine runs a search: *"Who has the required RBAC role right now?"* It takes a snapshot of that list and stores it as `WorkflowStageApprover` records.

Why a snapshot? Because roles can change. If Temi's dad gets "removed" from his role the day after the stage activated, the system still knows he was eligible *at the time*. The audit trail is honest.

---

## Part 9 — The Vote (WorkflowStageAction)

When an eligible approver makes a decision — APPROVED, REJECTED, or RETURNED — the engine writes a `WorkflowStageAction` row.

If an admin later needs to undo a vote (maybe Mum approved by mistake), the original row is stamped with `reversed_at` and a new row is created that says "this is a reversal of row X." Nothing is deleted.

---

## Part 10 — The Substitute (ApprovalDelegation)

Temi's mum is travelling for two weeks. She tells his aunty:

> "While I'm away, you have my authority to approve Temi's requests."

That is an `ApprovalDelegation`. User A grants User B their approval authority for a date range. It can be scoped to a specific document type or left open for all types.

`exclusive = True` means Mum is removed from the approver list while the delegation is active — only Aunty can approve, not both.

---

## Part 11 — The Ledger (WorkflowAuditLog)

Every material thing that happens to an instance is written to the `WorkflowAuditLog`. Submitted. Stage activated. Vote recorded. Approved. Everything.

The ledger is **append-only**. It is never updated. It is never deleted. It is the permanent receipt for everything that happened.

---

## Part 12 — The Handler (How Your Feature Listens)

The engine doesn't know what a leave request or a purchase order is. It only knows about documents. Your feature tells the engine what to do at each lifecycle event by writing a **Handler**.

```python
@register_handler("leave.request")
class LeaveRequestHandler(BaseWorkflowHandler):

    def on_approved(self, instance, context):
        # Engine called us — update the document
        LeaveRequest.objects.filter(pk=instance.document_object_id).update(status="APPROVED")

    def on_rejected(self, instance, context):
        LeaveRequest.objects.filter(pk=instance.document_object_id).update(status="REJECTED")
```

The engine auto-discovers this file (`workflow_handlers.py`) in your app on startup. You don't register it anywhere manually.

---

## Part 13 — Wiring It All Together

Here is how Temi would plug his phone accessories business into the engine if it were a Django app:

**Step 1 — Mark the document:**
```python
class PurchaseRequest(models.Model):
    workflow_document_type = "accessories.purchase"  # class attribute
    amount = models.DecimalField(...)
    category = models.CharField(...)
    status = models.CharField(...)
    school = None  # platform-level, not tied to a school
```

**Step 2 — Write the handler:**
```python
# workflow_handlers.py
@register_handler("accessories.purchase")
class PurchaseRequestHandler(BaseWorkflowHandler):

    def resolve_default_template_code(self, document):
        return "high_value" if document.amount >= 20000 else "standard"

    def validate_document(self, document, requested_by):
        if document.status != "DRAFT":
            raise InvalidInstanceStateError("Only DRAFT requests can be submitted.")

    def on_approved(self, instance, context):
        PurchaseRequest.objects.filter(pk=instance.document_object_id).update(status="APPROVED")

    def on_rejected(self, instance, context):
        PurchaseRequest.objects.filter(pk=instance.document_object_id).update(status="REJECTED")

    def on_returned(self, instance, context):
        PurchaseRequest.objects.filter(pk=instance.document_object_id).update(status="NEEDS_AMENDMENT")
```

**Step 3 — Publish the template (once, or update when rules change):**
```
POST /v1/workflow/templates/publish/
{
  "document_type": "accessories.purchase",
  "code": "standard",
  "name": "Standard Purchase Approval",
  "stages": [
    {
      "code": "mum-check",
      "label": "Mum's Approval",
      "kind": "APPROVAL",
      "order": 1,
      "approver_permission_key": "accessories.approve.parent",
      "approver_scope": "SCHOOL",
      "advance_rule": "ANY",
      "on_rejection": "RETURN_TO_REQUESTER"
    }
  ],
  "routes": []
}
```

**Step 4 — Submit a request:**
```python
from vs_workflow.services.submission import submit_for_approval

instance = submit_for_approval(document=purchase_request, requested_by=temi)
```

That's it. The engine takes over. It finds the template, resolves the approvers, activates the stage, notifies them, waits for their votes, and calls your handler when the decision is made.

---

## Part 14 — The API Cheat Sheet

All under `/v1/workflow/`.

| What you want to do | Method + URL |
|---|---|
| List all templates | `GET /templates/` |
| Publish or update a template | `POST /templates/publish/` |
| Submit a document for approval | `POST /instances/` |
| See all running instances | `GET /instances/` |
| See one instance in detail | `GET /instances/{id}/` |
| Vote on a stage | `POST /instances/{id}/actions/` |
| Withdraw your own submission | `POST /instances/{id}/withdraw/` |
| Fix and resubmit after RETURNED | `POST /instances/{id}/resubmit/` |
| Admin cancel a stuck instance | `POST /instances/{id}/cancel/` |
| Admin undo a vote | `POST /actions/{id}/reverse/` |
| See what needs my approval | `GET /dashboard/pending/` |
| See what I've submitted | `GET /dashboard/submitted/` |
| Create a delegation | `POST /delegations/` |
| Revoke a delegation | `POST /delegations/{id}/revoke/` |

---

## Part 15 — Errors Temi Might See

| Error | What happened |
|---|---|
| `TEMPLATE_NOT_FOUND` | No template published for this document type yet. Temi forgot to write the rules on the fridge. |
| `INVALID_INSTANCE_STATE` | Tried to do something at the wrong time — like resubmit an already-approved request. |
| `NOT_ELIGIBLE_APPROVER` | Someone tried to vote but they're not on the eligible list for this stage. |
| `REQUESTER_CANNOT_APPROVE` | Temi tried to approve his own request. Nice try. |
| `DUPLICATE_APPROVER_ACTION` | Already voted. You can't vote twice. |
| `STAGE_NOT_ACTIVE` | Tried to vote but no stage is currently waiting for input. |
| `INSTANCE_TERMINAL` | The request is already done — can't touch a finished one. |
| `REVERSAL_NOT_ALLOWED` | Tried to reverse a vote that's already been reversed, or a reversal row itself. |
| `UNKNOWN_CONDITION_FUNCTION` | A route condition used a `fn` key that nobody registered. Check `workflow_conditions.py`. |

---

*The end. Temi's request got approved. He bought the earphones. Business is good.*
