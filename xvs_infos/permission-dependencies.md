# Permission Dependencies

A `PermissionDependency` record says: **"holding permission A is only valid if you also hold permission B."**

The system does not auto-grant B when you assign A — you must grant both explicitly.
The dependency is a validation rule: a role that holds A without B is misconfigured.

---

## The mental model

Before writing any `PermissionDependency`, ask one question per non-view action:

> **"Can a user meaningfully perform this action without being able to see the resource first?"**

The answer is almost always no. That is the core dependency.

Then ask a second question for higher-privilege actions:

> **"Does this action logically require that a lower-privilege action on the same resource already succeeded?"**

---

## Standard rules (apply to every resource automatically)

These cover 80 % of all dependencies. Apply them mechanically for every resource
before thinking about anything custom.

| If the resource has… | It depends on…         | Reason                                          |
|----------------------|------------------------|-------------------------------------------------|
| `create`             | `view`                 | You see the list before adding to it.           |
| `update`             | `view`                 | You find and open a record before editing it.   |
| `delete`             | `view`                 | You see a record before you can remove it.      |
| `export`             | `view`                 | You see data before you download it.            |
| `import`             | `create`               | Bulk import creates records; implies create.    |
| `approve`            | `view`                 | You read a submission before approving it.      |
| `reject`             | `view`                 | Same as approve.                                |
| `publish`            | `view`                 | You review content before publishing it.        |
| `assign`             | `view`                 | You see the resource before linking it.         |
| `transfer`           | `view`                 | You see the record before moving it.            |
| `suspend`            | `view`                 | You read the record before suspending it.       |
| `reactivate`         | `view`                 | You see it is suspended before restoring it.    |
| `archive`            | `view`                 | You see the record before archiving it.         |
| `verify`             | `view`                 | You read the submission before verifying it.    |
| `reverse`            | `view` + `record`      | You see a transaction before reversing it.      |
| `track`              | `view`                 | You see the event before marking attendance.    |
| `generate`           | `view`                 | You access the record before generating a doc.  |
| `report`             | `view`                 | You access the data before summarising it.      |

**`manage` and `view` never have dependencies** — they are the base level.
`manage` implies full control; `view` is the foundation everything else builds on.

---

## Mapping a resource — worksheet

Use this for every resource you create. Fill in each row before writing any
`PermissionDependency` records for that resource.

```
Module:   <module>
Resource: <resource>

Action        | Exists? | Depends on (same resource unless noted)
--------------+---------+------------------------------------------
view          |         | — (no deps)
create        |         | view
update        |         | view
delete        |         | view
manage        |         | — (no deps)
approve       |         | view
reject        |         | view
export        |         | view
import        |         | create
reverse       |         | view, record
              |         |
<custom>      |         | <what you concluded from the two questions above>
```

Only fill in rows for actions that actually exist on this resource.
Leave actions blank if you did not create a Permission record for them.

---

## Cross-resource dependencies

These are the ones people forget. They arise when an action on resource A
requires access to resource B — usually in the same module.

**How to spot them:**

Work through the UI flow for the action. If the user must navigate through
or read another resource to reach this one, there is a cross-resource dependency.

**Common patterns:**

| Action                          | Also depends on                         |
|---------------------------------|-----------------------------------------|
| `finance.payment.record`        | `finance.invoice.view`                  |
| `finance.payment.verify`        | `finance.invoice.view`                  |
| `finance.invoice.approve`       | `finance.invoice.view` *(intra, std)*   |
| `assessments.scores.approve`    | `assessments.scores.enter`              |
| `assessments.results.publish`   | `assessments.scores.approve`            |
| `assessments.results.export`    | `assessments.results.publish`           |
| `students.class.transfer`       | `students.profile.view`                 |
| `admissions.enrollment.confirm` | `admissions.application.approve`        |
| `staff.leave.approve`           | `staff.leave.apply`                     |
| `staff.appraisal.manage`        | `staff.appraisal.view`                  |

**How to build cross-resource deps:**

Draw the user's workflow step by step:
```
User clicks "Record Payment"
  → must select an invoice first       → finance.invoice.view is a dep
  → fills in amount and reference
  → submits
```

Every resource the user must see or interact with in the flow = a dependency.

---

## Chain dependencies

Some actions form a chain where each step depends on the previous.
Always map the full chain, not just the immediate parent.

```
assessments.scores.enter
  └── (no dep beyond view)

assessments.scores.approve
  └── assessments.scores.enter   ← someone must have entered scores first

assessments.results.publish
  └── assessments.scores.approve ← scores must be approved before results are published

assessments.results.export
  └── assessments.results.publish ← results must be published before exporting
```

When you see a chain, each step depends only on the step directly above it —
not the whole chain. The dependency graph is transitive by implication.

---

## Audit checklist — run this after defining all permissions for a module

For each resource in the module:

```
[ ] Every non-view, non-manage action has at least a dep on <resource>.view
[ ] import (if present) has a dep on <resource>.create
[ ] reverse (if present) has deps on view AND record
[ ] approve/reject (if present) both dep on view
[ ] Any action with a UI flow that reads another resource has a cross-resource dep
[ ] Chain actions (enter → approve → publish → export) each dep on the step above
[ ] No circular deps introduced (A → B → A)
```

---

## Writing the records

After completing the worksheet, write deps in a single pass per resource —
all intra-resource deps first, then cross-resource:

```python
from vs_rbac.models import Permission, PermissionDependency

def dep(a_key, b_key):
    a = Permission.objects.get(key=a_key)
    b = Permission.objects.get(key=b_key)
    PermissionDependency.objects.get_or_create(permission=a, depends_on=b)

# ── finance.invoice ───────────────────────────────────────────────
dep("finance.invoice.create",  "finance.invoice.view")
dep("finance.invoice.approve", "finance.invoice.view")
dep("finance.invoice.export",  "finance.invoice.view")

# ── finance.payment ───────────────────────────────────────────────
dep("finance.payment.record",  "finance.payment.view")
dep("finance.payment.verify",  "finance.payment.view")
dep("finance.payment.verify",  "finance.invoice.view")   # cross-resource
dep("finance.payment.reverse", "finance.payment.view")
dep("finance.payment.reverse", "finance.payment.record") # chain
```

Always call deps in one place per module — not scattered across multiple scripts.
This makes it easy to audit the full dependency map for a module in one read.
