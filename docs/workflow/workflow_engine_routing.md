# workflow_engine_routing

The state machine. How a document enters approval, which template catches it,
how the engine walks from stage to stage, what makes it skip, and how it
terminates. The blueprint being walked is `workflow_templates`; who is allowed
to vote at each stop is `workflow_approvers`; the votes themselves are
`workflow_actions_lifecycle`.

This slice has no endpoints of its own. It is reached through every domain
module that calls `submit_for_approval` directly. There is no generic submit
endpoint: `POST /v1/workflow/instances/` existed until it was removed, because
the engine cannot know which rows a caller may see (§8).

---

## 1. What it is (and what it is NOT)

- **The engine is document-agnostic.** It knows a `document_type` token, a
  `ContentType` and a primary key. Everything domain-specific is delegated to a
  handler the owning app registers (`handlers/base.py`), so `vs_workflow`
  imports no domain module.
- **Submission is a service call, not an endpoint, for most callers.** Finance,
  payments and procurement call `submit_for_approval` from their own code. The
  REST route exists as well, and its document lookup is the module's worst
  defect (§8).
- **Routing is "first match wins", twice over.** Routes from a stage are
  evaluated in `order` and the first whose condition matches is followed
  (`services/routing.py:81-108`); with no routes at all the engine falls back to
  linear `WorkflowStage.order` (111-120).
- **Skipping is normal, not exceptional.** Four things make the engine step past
  a stage without a decision: it is retired, it is a `BRANCH` node, its
  `inclusion_condition` is false for this document, or it has no eligible
  approvers and `skip_if_no_approvers` is true. Each writes its own audit event.
- **A stage with no approvers and `skip_if_no_approvers = False` does not
  fail. It parks** - ACTIVE, IN_PROGRESS, empty snapshot, nobody able to act.
  That is the deliberate outcome for every ladder over money, and it has its own
  slice (`workflow_parking_release`).
- **`advance_instance` is not idempotent by accident, it is idempotent by
  design.** `_activate_stage` and `_skip_stage` both use `get_or_create` on
  `(instance, stage, attempt)`, so a double call on the same attempt writes one
  row.
- **The engine never re-evaluates eligibility.** Approvers are resolved once, at
  activation, and frozen into `WorkflowStageApprover`. Every later read consults
  the snapshot. That guarantee is why parking needs a repair rather than a
  retry.
- **There is no timer, no escalation and no reminder.** A stage that nobody acts
  on stays ACTIVE forever. `workflow.escalated` exists in the notification
  registry and nothing in this engine emits it.

## 2. Domain model

Three models belong to a running instance. None of them carries a tenant column,
and none has a tenant-aware manager - a fact the module is explicit about
(`views.py:603-607`, `views.py:677-682`): every scope check on them is a join
through `instance__tenant` that the caller must remember to write.

### `WorkflowInstance` (`models.py:523`)

| Field | Notes |
|---|---|
| `tenant`, `branch` | `PROTECT`. Copied from the document at submission |
| `template` | `PROTECT` - the reason a used template can never be deleted |
| `document_content_type` + `document_object_id` | Generic FK; `document` resolves it |
| `document_type` | Denormalised so filtering needs no contenttypes join |
| `document_summary` | The handler's display snapshot, frozen at submission |
| `status` | `WorkflowInstanceStatus`, default `DRAFT` |
| `requested_by` | `PROTECT` |
| `current_stage` | `PROTECT`, null when terminal |
| `state_version` | Incremented on every status transition |

`objects` is `TenantAwareManager.from_queryset(WorkflowInstanceQuerySet)()`, so
the domain helpers (`for_tenant`, `for_document`, `active`) survive the tenant
scoping; `all_objects` is the unscoped view every view uses with an explicit
filter.

### `WorkflowStageInstance` (`models.py:600`)

One row per `(instance, stage, attempt)`. `status` runs
`PENDING → ACTIVE → APPROVED | REJECTED | RETURNED | SKIPPED`, with
`activated_at`, `resolved_at`, `skip_reason` and `attempt`.

### `WorkflowAuditLog` (`models.py:764`)

Append-only, `PROTECT` on both the instance and the stage instance, ordered
newest first, indexed on `(instance, occurred_at)` and `(instance, event_type)`.
`context` is a raw `JSONField` and is serialized raw (§8).

## 3. Entry points

| Caller | Path |
|---|---|
| Domain modules | `vs_workflow.services.submission.submit_for_approval` directly |
| Read-side gates | `vs_workflow.services.resolution.template_requires_approval` |

`submit_for_approval` (`services/submission.py:19`), in order:

1. read `document.workflow_document_type`, or `InvalidInstanceStateError`;
2. `get_handler(document_type)`, or `UnknownDocumentTypeError`;
3. `handler.validate_document(document, requested_by)` - the domain's own submit
   guards;
4. `code = template_code or handler.resolve_default_template_code(document)`;
5. `document_scope(document, default_tenant=requested_by.tenant)`;
6. `resolve_template(...)`, or `TemplateNotFoundError`;
7. `handler.get_document_summary(document)`, wrapped in a bare `except` because
   a display snapshot must never fail an approval (55-61);
8. inside one transaction: create the instance, write
   `INSTANCE_SUBMITTED`, call `handler.on_submitted`, and
   `advance_instance(instance, current_attempt=1)`.

Everything from step 8 commits together: if the first stage's activation raises,
no instance exists.

## 4. Lifecycle / state machine

```text
                    submit_for_approval
                            │
                            ▼
                       SUBMITTED
                            │  advance_instance
        ┌───────────────────┴───────────────────────────┐
        │ walk: pick next stage, skip what must be      │
        │ skipped, activate the first real approval     │
        ▼                                               ▼
   IN_PROGRESS  ◄── resubmit ──  RETURNED        (ran off the end)
        │  ▲                          ▲                 │
        │  │ reverse_action           │ RETURNED vote   ▼
        │  └──────────────────────────┤ or rejection    APPROVED
        │                             │ policy
        ├── REJECTED  (on_rejection = TERMINAL)
        ├── WITHDRAWN (requester)
        └── CANCELLED (admin)
```

`WORKFLOW_TERMINAL_STATUSES` is `{APPROVED, REJECTED, WITHDRAWN, CANCELLED}`
(`constants.py:16-19`). `RETURNED` is deliberately not terminal: it is a pause
with the returning stage remembered so `resubmit` can resume from it on a fresh
attempt.

Per stage attempt:

```text
   PENDING ─► ACTIVE ─┬─► APPROVED   (threshold met)
                      ├─► REJECTED   (a rejecting vote)
                      ├─► RETURNED   (a returning vote)
                      └─► SKIPPED    (retired / branch / condition / no approvers)
```

## 5. Derivations

### The template cascade (`services/resolution.py:131-161`)

```text
{tenant, branch}  →  {tenant, None}  →  {None, None}
```

with `is_active=True` **inside** the lookup rather than checked afterwards, so a
tenant that switched its own version off falls *through* to the platform
template instead of finding the inactive one and stopping. Ordered by
`("code", "id")` so a codeless lookup is deterministic.

`document_scope` (109-127) decides what `tenant` means for a document: a direct
`tenant` attribute wins **including when it is explicitly None** (a
platform-scoped document must gate on the platform template only); otherwise
`document.entity.tenant` for finance-shaped documents; otherwise the caller's
default.

### Picking the next stage (`services/routing.py:74-120`)

If the template has any routes at all, the routes **from this stage** are
evaluated in order and the first match is followed. Every evaluation is written
to a `ROUTE_EVALUATED` audit row carrying the full per-route trace. If routes
exist from this stage and none matched, that is `TemplateInvalidError` - the
engine refuses to guess. If no routes leave this stage, it falls back to linear
order.

`readonly_next_stage` (124-154) is the pure sibling: same stepping, no audit
writes. Two read-only walks share it - the next-stage label preview and
`template_requires_approval` - so a preview can never describe a route the
engine will not take.

### The skip ladder (`services/routing.py:290-341`)

In order, per hop, with `MAX_HOPS = 50` as the cycle guard:

| Test | Audit event | `skip_reason` |
|---|---|---|
| `retired_at is not None` | `STAGE_SKIPPED_CONDITION` | `stage_retired` |
| `kind == APPROVAL` and `inclusion_condition` false | `STAGE_SKIPPED_CONDITION` | `inclusion_condition_false` |
| `kind == BRANCH` | `STAGE_SKIPPED_CONDITION` | `branch_node` |
| no eligible approvers and `skip_if_no_approvers` | `STAGE_SKIPPED_NO_APPROVER` | `zero_eligible_approvers` |

The fourth test is the one that costs: the stage is **activated first**
(`routing.py:325`), then approvers are resolved **a second time** (326) to
decide whether to skip it. So a skipped-for-no-approvers stage leaves a
`STAGE_ACTIVATED` audit row followed by a `STAGE_SKIPPED_NO_APPROVER` one, and
every activation resolves its approvers twice (`workflow_code_issues.md` §9).

When there are no approvers and the stage does **not** auto-skip, the engine
writes a second `STAGE_ACTIVATED` row carrying
`{"warning": "stage_active_with_no_approvers"}` and leaves the instance
`IN_PROGRESS`. That is parking.

### Activation (`services/routing.py:203-252`)

`get_or_create` the stage instance, force it ACTIVE, resolve approvers, bulk
insert the frozen `WorkflowStageApprover` snapshot, point
`instance.current_stage` at the stage, write `STAGE_ACTIVATED` with the eligible
count, and notify the snapshot. For a `DYNAMIC_ROLE` stage the audit context
additionally records **which rule chose the role** and the full evaluation
trace, because a dynamic stage is exactly where "why did this go to the Bursar"
gets asked.

### Termination

- **`_terminate_approved` (345-390)** sets APPROVED, clears `current_stage`,
  stamps `completed_at`, writes `INSTANCE_APPROVED`, and calls
  `handler.on_approved` **inside the same atomic block**, so a handler that
  raises rolls the whole transition back. It then names the vote that completed
  the workflow for the notification, falling back to "the system" for a fully
  automatic ladder rather than rendering an empty string.
- **`_terminate_rejected` (394-415)** is the same shape with `on_rejected`.
- **`_return_to_requester` (419-439)** sets RETURNED and calls `on_returned`.
  It deliberately does **not** clear `current_stage`: the pointer is how
  `resubmit` knows where to resume.

### The read-only twin (`services/resolution.py:165-223`)

`template_requires_approval(template, document)` walks from ENTRY with
`readonly_next_stage` and the same three skips, and answers True at the first
stage that would genuinely activate. It exists so a domain gate such as
`vs_finance.approvals.approval_required` and the engine cannot disagree - they
used to, from two hand-copied cascades, and a ladder whose only stage was
conditional left a document with no route to the ledger at all.

Two deliberate properties, both worth knowing before touching it:

- It **fails closed**: a malformed condition or an undecidable route answers
  True, because a broken template must not become an approval bypass.
- It **does not model staffing**: a stage that would auto-skip for want of an
  approver still counts as requiring approval, which is the safe direction.
- It answers **False for a template with no stages** (195-202), on the reasoning
  that blocking the direct post would leave the document nowhere to go. That
  reasoning is sound and its consequence is recorded as
  `workflow_code_issues.md` §11.

### Conditions (`conditions/evaluator.py`)

Five forms: `all`, `any`, `not`, `fn` (a registered named predicate), and `op`
(one of nine operators over a dotted `field` path). `_normalise` coerces numbers
to `Decimal` so a JSON number compares cleanly with a model's `DecimalField`. A
missing field compares as `None` and a `TypeError` becomes `False` with the
error preserved in the trace, so a bad comparison never takes an approval down.

`validate_condition` runs at publish time and rejects the shapes that would only
surface mid-approval: an unsupported operator, a missing `field`, an `in`
without a list value, an unregistered `fn` key.

Two things it does not do: `all`/`any` **do not short-circuit**, so every child
runs even once the answer is known; and `field` is an **unbounded attribute
walk** whose result is copied into the audit trace
(`workflow_code_issues.md` §3).

## 6. What the engine writes

Per hop, inside the caller's transaction:

| Event | Written by |
|---|---|
| `INSTANCE_SUBMITTED` | `submit_for_approval` |
| `ROUTE_EVALUATED` | `_pick_next_stage`, once per stage transition when routes exist |
| `STAGE_ACTIVATED` | `_activate_stage`, and again as a warning when a stage activates unstaffed |
| `STAGE_SKIPPED_CONDITION` / `STAGE_SKIPPED_NO_APPROVER` | `_skip_stage` |
| `INSTANCE_APPROVED` / `INSTANCE_REJECTED` / `INSTANCE_RETURNED` | the three terminal helpers |

Plus the frozen approver snapshot, the stage instance rows, and the instance's
own `status`/`current_stage`/`state_version`.

`audit_service.write` (`services/audit.py:13`) resolves the audit identity
through `vs_tenants.context`, so an impersonated actor is recorded with the real
one behind them. Its docstring carries one rule worth repeating: never call it
inside a rollback-only savepoint, because the audit entry disappears with the
savepoint.

Notifications are queued on `transaction.on_commit`, so a rolled-back transition
never notifies (`services/routing.py:67-70`). See
`workflow_notifications_audit`.

## 7. Worked example

A Bright Star bursar submits a journal. The tenant runs the platform template,
whose stages are: `branch-check` (a `BRANCH` node), `finance-review` (approval,
`inclusion_condition` = amount ≥ ₦100,000), `bursar-signoff` (approval).

The journal is for ₦40,000.

```text
submit_for_approval(document=journal, requested_by=bursar)
  cascade: {bright-star, ikeja} miss → {bright-star, None} miss → {None, None} hit
  INSTANCE_SUBMITTED
  advance_instance:
    hop 1  next = branch-check     → BRANCH        → SKIPPED (branch_node)
    hop 2  next = finance-review   → condition false
                                    (40000 >= 100000 → False)
                                   → SKIPPED (inclusion_condition_false)
    hop 3  next = bursar-signoff   → APPROVAL, activate
             resolve_approvers → [Adaeze]
             snapshot written, STAGE_ACTIVATED, notify Adaeze
    status = IN_PROGRESS
```

The instance detail then shows three `stage_instances` - two SKIPPED with their
reasons, one ACTIVE - and an audit log whose `STAGE_SKIPPED_CONDITION` row for
`finance-review` carries the condition trace:

```json
{"kind": "op", "op": "gte", "field": "amount",
 "left": "40000.00", "right": "100000", "result": false}
```

Adaeze approves. `_stage_fully_approved` is true (one eligible, one approval),
so the stage resolves APPROVED and `advance_instance` runs again from
`bursar-signoff`, finds no next stage, and `_terminate_approved` fires
`handler.on_approved`, which posts the journal to the ledger - inside the same
transaction, so a posting failure un-approves the workflow.

Had the same journal been for ₦400,000, hop 2 would have activated
`finance-review` instead, and the ladder would have taken two decisions.

## 8. Gotchas / known limitations

Full evidence in **`error/workflow/workflow_code_issues.md`**.

- ~~**`POST /v1/workflow/instances/` loads the document by content type and pk
  with no tenant scoping**~~ **- fixed.** The endpoint is removed (a generic
  submitter cannot answer "which rows may this caller see"), and
  `submit_for_approval` now calls `_assert_own_tenant` on the scope
  `document_scope` returns, before the handler's `on_submitted` can touch the
  record. Every module submits through its own scoped queryset
  (`workflow_code_issues.md` §1).
- **An unstaffed stage now parks by default.** ``skip_if_no_approvers``
  defaulted to True on both the model and the publish service, so a stage
  published without the field auto-skipped when nobody could approve it. A
  tenant may publish its own full version of a central ladder, and an editor
  changing one threshold does not resend the fields it is not changing, so the
  dangerous answer arrived by omission. Both defaults are now False
  (``vs_workflow.0010``); existing stage rows are untouched.
- **A route condition can read any attribute reachable from the document**, and
  whatever it finds is stringified into the `ROUTE_EVALUATED` audit trace, which
  the instance detail returns raw (`workflow_code_issues.md` §3).
- **Approvers are resolved twice per activation** (`routing.py:222` then `326`,
  and again in `actions.resubmit`) (`workflow_code_issues.md` §9).
- **A stage that is about to be skipped for want of approvers is activated
  first**, so the audit log shows it activating and then being skipped, and
  `current_stage` briefly points at a stage nobody ever saw
  (`workflow_code_issues.md` §14).
- **`all` and `any` do not short-circuit** (`conditions/evaluator.py:62-77`), so
  a `fn` condition that queries the database runs even when the answer is already
  settled (`workflow_code_issues.md` §15).
- **The instance detail payload is unbounded**: every stage instance, every
  action, every eligible approver and every audit row, including full traces
  (`workflow_code_issues.md` §10).
- **There is no timer, reminder or escalation.** A stage nobody votes on waits
  forever, and nothing counts how long.
- **`MAX_HOPS = 50` is duplicated** in three places
  (`routing.py:286`, `routing.py:180`, `resolution.py:106`) rather than shared.
- **Justified by design:** `template_requires_approval` fails closed on an
  undecidable template.
- **Justified by design:** `_terminate_approved` runs `handler.on_approved`
  inside the transition's transaction, so a handler failure un-approves rather
  than leaving a workflow that says approved over a document that did not post.
- **Justified by design:** `_return_to_requester` leaves `current_stage` set.

## 9. Permissions & tenant isolation

The engine itself has no permission layer - it is a service. The gates are:

| Surface | Key |
|---|---|
| `POST instances/` | `workflow.instance.submit` |
| `GET instances/`, `GET instances/<id>/` | `workflow.instance.view` |
| Everything a domain module calls directly | that module's own gate |

Every workflow key is seeded to `xvs_super_admin` and `xvs_platform_admin` only.
No school role holds `workflow.instance.view`, so in a school tenant the instance
list and detail are `403` for everybody while the pending queue and the voting
endpoints are open (`workflow_code_issues.md` §8).

Isolation on the read paths is `WorkflowInstance.all_objects.filter(tenant=...)`
(`views.py:491`), applied before any user-supplied filter. On the write path it
is `document_scope`, which reads the tenant off the **document** and not off the
request - correct for a service call from a domain module, and the hole in §8's
first item for the REST route.

`WorkflowStageInstance`, `WorkflowStageApprover`, `WorkflowStageAction` and
`WorkflowAuditLog` have no tenant column and no tenant-aware manager. Every
place that reads them therefore carries an explicit join, and the module says so
where it matters (`views.py:603-607`, `677-682`).

## 10. Code map

| File | Responsibility |
|---|---|
| `services/submission.py` | `submit_for_approval` - the eight steps |
| `services/resolution.py:109-127` | `document_scope` |
| `services/resolution.py:131-161` | `resolve_template` - the cascade |
| `services/resolution.py:165-223` | `template_requires_approval` |
| `services/routing.py:74-120` | `_pick_next_stage` |
| `services/routing.py:124-154` | `readonly_next_stage` |
| `services/routing.py:158-199` | `preview_next_approval_stage` |
| `services/routing.py:203-252` | `_activate_stage` |
| `services/routing.py:256-278` | `_skip_stage` |
| `services/routing.py:282-341` | `advance_instance` |
| `services/routing.py:345-439` | the three terminal transitions |
| `services/audit.py` | `write` |
| `conditions/evaluator.py` | `evaluate_condition`, `validate_condition` |
| `conditions/registry.py` | `register_condition`, `get_condition_function` |
| `handlers/base.py`, `handlers/registry.py` | the document contract and its registry |
| `constants.py:5-75` | statuses, terminal set, audit vocabulary, stage kinds |

## 11. Test coverage & gaps

- `AdvanceInstanceTests` (`tests/test_services.py:77-164`) - a terminal instance
  is returned unchanged; no stages raises `TemplateInvalidError`; a single stage
  activates; a stage with no approvers and the skip flag is skipped; a retired
  stage is skipped; two stages advance in order; running out of stages
  terminates APPROVED.
- `TemplateCascadeTests` (`tests/test_submission.py:22-103`) - branch first,
  then tenant, then platform; a tenant document never picks up another branch's
  template; a platform document only matches the platform scope; a switched-off
  tenant template falls through; a different code at a nearer scope does not
  shadow; and no template anywhere resolves to None.
- `DocumentScopeTests` (`105-143`) - a direct `tenant` attribute wins even when
  it is None, an entity-scoped document scopes through its entity, and a document
  with neither takes the default.
- `SubmissionGuardTests` (`145-161`) - `TemplateNotFoundError` when all scopes
  miss, and a missing `workflow_document_type`.
- `ConditionEvaluatorTests` (`tests/test_conditions.py`) - the five forms, plus a
  named function whose error returns False.
- `test_registry.py` - handler and condition-function duplicate registration.

What the suite does not cover:

1. ~~**`POST /v1/workflow/instances/`.**~~ **- closed.** The endpoint is gone,
   and its absence is asserted through the router
   (`tests/test_tenant_scoping.py::InstanceScopingTests`). Cross-tenant refusal
   is covered at the service layer
   (`tests/test_submission.py::CrossTenantSubmissionTests`) and end to end on a
   real payout batch (`vs_payments/tests.py::PayoutBatchApprovalTests`).
2. **`template_requires_approval`** - the read-only twin the finance direct-post
   gate depends on has no test in this module: not the conditional-ladder case it
   was written for, not the fail-closed branch, not the no-stages branch.
3. **Route evaluation.** Every routing test uses linear order; no test publishes
   routes and asserts which edge was taken, the `ROUTE_EVALUATED` audit row, or
   the "no route matched" refusal.
4. **`preview_next_approval_stage`** - the `next_stage` field on every instance
   detail response.
5. **The `MAX_HOPS` cycle guard**, in any of its three copies.
6. **`inclusion_condition` on a live stage** - conditions are tested in isolation
   and through dynamic rules, but no test routes a document past a stage on one.
7. **`document_summary`** - neither the handler call nor the bare-except fallback
   when it raises.
