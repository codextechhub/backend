# workflow_actions_lifecycle

The decisions. How a vote is recorded, what makes a stage advance, and the four
operations that move an instance without a vote: withdraw, cancel, reverse and
resubmit. Plus the instance API and the three dashboards built on it. The engine
that reacts to these is `workflow_engine_routing`; who is allowed to vote is
`workflow_approvers`.

Routes: `instances/`, `actions/<id>/reverse/`, `dashboard/pending/`,
`dashboard/submitted/`, `dashboard/team-load/`.

---

## 1. What it is (and what it is NOT)

- **Every mutating operation takes a row lock first.**
  `_lock_instance` (`services/actions.py:52-60`) is
  `WorkflowInstance.objects.select_for_update().get(pk=...)`, so two approvers
  voting in the same millisecond, or a withdraw racing a cancel, serialise
  instead of producing split advance-rule counts.
- **Votes are append-only.** A reversal never edits or deletes the original: it
  stamps `reversed_at` on it and writes a new row with `is_reversal_of` pointing
  back (`services/actions.py:245-254`).
- **One live vote per actor per attempt**, enforced twice: a query before the
  insert (`actions.py:142-145`) and a conditional unique constraint in the
  database (`models.py:698-704`).
- **Eligibility is read from the frozen snapshot, never re-resolved.**
  `_check_eligibility` (`actions.py:83-95`) looks the actor up in
  `WorkflowStageApprover` for this attempt. A role change mid-review cannot
  retroactively invalidate somebody already deciding.
- **A requester can never approve their own document**, checked again at vote
  time (`actions.py:135-136`) even though resolution already excluded them.
- **`RETURNED` is not a rejection.** It pauses the instance, remembers the stage,
  and waits for `resubmit` to re-activate it on a fresh attempt.
- **Withdraw is the requester's; cancel is an admin's.** Both are terminal, and
  neither can be undone - a requester must submit a fresh document.
- **A terminal rejection cannot be reversed.** Reversal is allowed on a
  non-terminal instance or an APPROVED one, and on nothing else (§8).

## 2. Domain model

### `WorkflowStageAction` (`models.py:659`)

| Field | Notes |
|---|---|
| `stage_instance` | `PROTECT` |
| `actor` | `PROTECT` - who clicked |
| `on_behalf_of` | `PROTECT`, nullable - copied from the snapshot row when the actor is a delegate |
| `action` | `APPROVED`, `REJECTED`, `RETURNED` (the enum also carries `WITHDRAWN`, unused here) |
| `comment` | Mandatory for `REJECTED` and `RETURNED` |
| `attempt` | Mirrors the stage instance's attempt |
| `reversed_at`, `reversed_by`, `reversal_reason` | Stamped on the original when reversed |
| `is_reversal_of` | `OneToOneField("self")`, `PROTECT` - set on the reversal row |

The unique constraint is conditional:
`(stage_instance, actor, attempt)` where `is_reversal_of IS NULL`, so a reversal
row does not collide with the vote it reverses.

### `WorkflowStageApprover` (`models.py:632`)

Written once at activation, never updated. `user`, `on_behalf_of`, `attempt`.
This is the list `_check_eligibility` and `_stage_fully_approved` read.

## 3. Endpoint map

| Method + path | Key | Body |
|---|---|---|
| `GET instances/` | `workflow.instance.view` | `?document_type=`, `?status=`, `?requested_by=`, `?template_code=` |
| `GET instances/<id>/` | `workflow.instance.view` | - |
| `POST instances/` | `workflow.instance.submit` | `{content_type_id, object_id, template_code?}` |
| `POST instances/<id>/actions/` | **none** | `{action, comment}` |
| `POST instances/<id>/withdraw/` | **none** | - |
| `POST instances/<id>/resubmit/` | **none** | - |
| `POST instances/<id>/cancel/` | `workflow.instance.cancel` | `{reason}` |
| `POST instances/<id>/continue-without-approval/` | **none** | `{reason?}` - see `workflow_parking_release` |
| `POST actions/<action_id>/reverse/` | `workflow.action.reverse` | `{reason}` |
| `GET dashboard/pending/` | authentication only | - |
| `GET dashboard/submitted/` | authentication only | `?status=` |
| `GET dashboard/team-load/` | `workflow.instance.view` | - |

"none" means `get_permissions` falls through to `[IsAuthenticatedAndActive()]`
(`views.py:483-484`), with the comment "actor-level actions are guarded by
ownership/eligibility in the service layer" - which is accurate: the vote is
gated by the snapshot, withdraw and resubmit by `requested_by`.

**The asymmetry that follows is real.** `workflow.instance.view` is seeded to
platform roles only, so in a school tenant an approver can vote on an instance
they cannot open, and the team-load board is `403` for everybody
(`workflow_code_issues.md` §8).

### Response shapes

Four different ones in this slice alone:

| Endpoint | Shape |
|---|---|
| `GET instances/` | `{success, message, pagination, data}` (XVSPagination) |
| `GET instances/<id>/`, every action | a bare serialized object |
| `GET dashboard/pending/` | `{"results": [...], "count": N}` |
| `GET dashboard/submitted/`, `dashboard/team-load/` | a bare list |

None of the module's success responses uses `core.response.success_response`
(`workflow_code_issues.md` §13).

## 4. Recording a vote (`services/actions.py:122`)

```text
validate action ∈ {APPROVED, REJECTED, RETURNED}
comment required for REJECTED and RETURNED
┌ transaction ────────────────────────────────────────────────┐
│ lock the instance                                            │
│ refuse if terminal, or if status is RETURNED                 │
│ refuse if the actor is the requester                         │
│ find the ACTIVE stage instance (highest attempt)             │
│ check the actor is in the frozen snapshot                    │
│ refuse a second live vote on this attempt                    │
│ write the action row + APPROVER_ACTED audit                  │
│                                                              │
│ RETURNED  → stage RETURNED, _return_to_requester             │
│ REJECTED  → stage REJECTED, STAGE_REJECTED audit,            │
│             then TERMINAL → _terminate_rejected              │
│                  or RETURN_TO_REQUESTER → _return_to_requester│
│ APPROVED  → if threshold met: stage APPROVED,                │
│             STAGE_APPROVED audit, advance_instance           │
└──────────────────────────────────────────────────────────────┘
```

### The threshold (`services/actions.py:99-118`)

```python
approved = live APPROVED actions on this attempt
eligible = snapshot rows on this attempt
ANY       → approved >= 1
QUORUM    → approved >= (quorum_count or 1)
UNANIMOUS → eligible > 0 and approved >= eligible
```

Only non-reversed, non-reversal approvals count, which is what lets a reversal
correctly re-open a stage that had already crossed the line. `eligible > 0` on
the unanimous branch is what stops an empty snapshot from being vacuously
unanimous - and it is why the parking repair can never change a stage's
arithmetic retroactively.

**`eligible` counts snapshot rows, and a non-exclusive delegation adds a row
without removing one.** That is the deadlock in `workflow_approvers` §7.

## 5. The four operations without a vote

### `withdraw` (`actions.py:185`)

Requester only, permitted until terminal. Sets `WITHDRAWN`, clears
`current_stage`, stamps `completed_at`, writes `INSTANCE_WITHDRAWN`, calls
`on_withdrawn`.

### `cancel` (`actions.py:206`)

Requires a non-empty reason. Sets `CANCELLED` the same way and calls
`on_cancelled` with the reason. The key is `workflow.instance.cancel`
(`SENSITIVE`); the service itself checks nothing about who the admin is, so the
view's tenant-scoped `get_object()` is the boundary.

Both call `_run_handler_callback` (`actions.py:32-48`) rather than `get_handler`
directly, so a `document_type` whose handler has been renamed away cannot stop an
admin cancelling or a requester withdrawing. Genuine handler bugs still
propagate.

### `reverse_action` (`actions.py:229`)

Requires a reason. Refuses a reversal of a reversal and a double reversal.
Refuses any terminal instance **except** APPROVED. Stamps the original, writes
the reversal row and an `ACTION_REVERSED` audit entry, and - if the stage had
resolved APPROVED or REJECTED - puts it back to ACTIVE, restores
`current_stage`, and moves an APPROVED instance back to IN_PROGRESS.

Two things it does not do: it does **not** re-notify the approvers whose decision
is once again awaited, and it does **not** undo `handler.on_approved`. A
reversed final approval leaves the workflow IN_PROGRESS over a document the
handler has already posted, dispatched or issued (§8).

The tenant check lives in the view, not the service
(`views.py:608-627`), with a comment saying why: `WorkflowStageAction` has no
tenant column and no tenant-aware manager, so the explicit comparison is the only
thing between a reverse-capable admin and another tenant's approval history.

### `resubmit` (`actions.py:278`)

Requester only, `RETURNED` only. Computes the next attempt from the highest
existing one, writes `INSTANCE_RESUBMITTED`, then either advances past the
returning stage if it has since been retired, or re-activates it with a fresh
approver snapshot. A stage that resolves to nobody on the new attempt auto-skips
if its policy allows.

## 6. The dashboards

### `dashboard/pending/` (`views.py:629-649` → `services/my_queue.py:31`)

Starts from **snapshots**, not instances, so delegated approvals - where the
actor is not the original approver - are included. Three filters make a snapshot
actionable:

1. live work only: an ACTIVE stage on an IN_PROGRESS instance;
2. not already voted: no unreversed action by this actor for this attempt;
3. not stale: `snap.attempt == snap.stage_instance.attempt`, which matters after
   a return-and-resubmit.

It runs a **parking repair pass first**, scoped to the tenant being read, so a
newly appointed approver finds the waiting work without the requester having to
resubmit (`workflow_parking_release` §5).

`pending_approval_count` deliberately materialises the same list rather than
issuing a `COUNT(*)`: filters 2 and 3 cannot be expressed in the query, so a
cheaper count would report work the user cannot action.

### `dashboard/submitted/` (`views.py:651-667`)

The caller's own submissions in this tenant, newest first, optional `?status=`.
Unpaginated.

### `dashboard/team-load/` (`views.py:669-698`)

Active stage instances grouped by `(document_type, stage_code)` with a count. The
scope is `instance__tenant=self.get_tenant()`, written unconditionally because
`WorkflowStageInstance` has no tenant-aware manager and the board would otherwise
report every tenant's workload.

## 7. Worked example

Bright Star's PO ladder, one stage, `QUORUM` of 2, snapshot [Adaeze, Chidi,
Femi].

```text
POST /v1/workflow/instances/PO7a2f/actions/?tenant=bright-star
{"action": "APPROVED"}                                  (as Chidi)
  → action row, APPROVER_ACTED audit
  → threshold 1 of 2 → stage stays ACTIVE, instance unchanged

POST … {"action": "APPROVED"}                           (as Femi)
  → threshold 2 of 2 → stage APPROVED, STAGE_APPROVED audit
  → advance_instance → no next stage → _terminate_approved
       on_approved fires: the purchase order issues
       workflow.final_approved → the requester
```

Now the reversal. Somebody notices Femi should not have voted:

```text
POST /v1/workflow/actions/<Femi's action id>/reverse/?tenant=bright-star
{"reason": "Voted on the wrong batch."}
  → Femi's row stamped reversed_at/reversed_by/reversal_reason
  → a reversal row created, ACTION_REVERSED audit
  → stage back to ACTIVE, instance back to IN_PROGRESS
  → the purchase order is still issued
  → Adaeze and Chidi are told nothing
```

Both of those last two lines are §8 items.

And the refusal that has no exit:

```text
POST /v1/workflow/actions/<a rejecting vote>/reverse/?tenant=bright-star
  → 422 REVERSAL_NOT_ALLOWED  "Cannot reverse on instance in status REJECTED."
```

A mis-clicked terminal rejection cannot be undone by anybody. The requester
starts again.

## 8. Gotchas / known limitations

Full evidence in **`error/workflow/workflow_code_issues.md`**.

- **Reversing an action reopens the stage and notifies nobody**
  (`actions.py:260-273`). The parking repair notifies in exactly this situation
  and explains why; reversal does the same thing silently
  (`workflow_code_issues.md` §6).
- **A terminal rejection can never be reversed** (`actions.py:241-243`), so a
  mis-clicked rejection is unrecoverable (`workflow_code_issues.md` §7).
- **Reversing a final approval does not undo the handler.** `on_approved` has
  already run - a payout dispatched, a purchase order issued - and nothing calls
  it back (`workflow_code_issues.md` §7).
- **`workflow.instance.view` is seeded to nobody outside the platform**, so an
  approver in a school tenant can vote on an instance they cannot open, and
  `dashboard/team-load/` is `403` for the whole tenant
  (`workflow_code_issues.md` §8).
- **The instance detail payload is unbounded**: every stage instance with every
  action and every eligible approver, plus every audit row with its raw context
  (`workflow_code_issues.md` §10).
- **Four response shapes in one module**, none of them the platform envelope
  (`workflow_code_issues.md` §13).
- **`dashboard/submitted/` and `dashboard/team-load/` are unpaginated.**
- **The list filters are unvalidated strings.** `?requested_by=abc` reaches the
  ORM as a pk filter (`views.py:499`).
- **`WorkflowStageAction.WITHDRAWN` is declared and never written**
  (`constants.py:35`): withdrawal is an instance transition, not a stage vote.
- **Justified by design:** every mutation takes `select_for_update` on the
  instance first.
- **Justified by design:** eligibility is read from the frozen snapshot rather
  than re-resolved.
- **Justified by design:** `_run_handler_callback` swallows only
  `UnknownDocumentTypeError`, so a renamed document type cannot trap a cancel.

## 9. Permissions & tenant isolation

| Operation | Gate | Where enforced |
|---|---|---|
| Vote | in the frozen snapshot for this attempt | `_check_eligibility` |
| Vote (negative) | not the requester, not terminal, not RETURNED, not a repeat | `record_action` |
| Withdraw, resubmit | `requested_by == actor` | the service |
| Cancel | `workflow.instance.cancel` + tenant-scoped `get_object()` | view + queryset |
| Reverse | `workflow.action.reverse` + explicit tenant comparison | `ReverseActionView` |
| List, retrieve | `workflow.instance.view` + `filter(tenant=…)` | view |
| Pending, submitted | authentication + `filter(tenant=…)` / `requested_by=user` | view and service |
| Team load | `workflow.instance.view` + `instance__tenant=` join | view |

The instance queryset filters the tenant **before** any user-supplied filter
(`views.py:490-503`), so no filter parameter can widen it.

The one place a tenant check is written by hand rather than derived from a
queryset is `ReverseActionView`, and the code says why. It answers `404` for a
cross-tenant action id, hiding existence.

## 10. Code map

| File | Responsibility |
|---|---|
| `services/actions.py:32-48` | `_run_handler_callback` |
| `services/actions.py:52-60` | `_lock_instance` |
| `services/actions.py:64-95` | `_active_stage_instance`, `_check_eligibility` |
| `services/actions.py:99-118` | `_stage_fully_approved` - the three advance rules |
| `services/actions.py:122-181` | `record_action` |
| `services/actions.py:185-225` | `withdraw`, `cancel` |
| `services/actions.py:229-274` | `reverse_action` |
| `services/actions.py:278-321` | `resubmit` |
| `services/my_queue.py` | `pending_approval_snapshots`, `pending_approval_count` |
| `views.py:471-601` | `WorkflowInstanceViewSet` |
| `views.py:603-627` | `ReverseActionView` |
| `views.py:629-698` | the three dashboards |
| `models.py:632-706` | `WorkflowStageApprover`, `WorkflowStageAction` |
| `exceptions.py` | the typed error codes every refusal carries |

## 11. Test coverage & gaps

- `RecordActionTests` (`tests/test_actions.py:110-185`) - the action row, the
  audit row, mandatory comments, the requester refusal, a non-eligible actor, a
  duplicate vote, a terminal instance, terminal rejection, and the return path.
- `StageFullyApprovedTests` (`187-237`) - `ANY`, `UNANIMOUS`, `QUORUM`, and that a
  reversed vote does not count.
- `WithdrawTests` (`239-276`) and `CancelTests` (`278-307`) - including that a
  missing handler does not raise for either.
- `ReverseActionTests` (`309-360`) - marks the original and creates the reversal,
  refuses a double reversal and a reversal of a reversal, requires a reason, and
  re-activates an approved stage.
- `ResubmitTests` (`362-393`) - restores IN_PROGRESS, refuses a non-returned
  instance and a non-requester, and increments the attempt.
- `ReverseActionScopingTests`, `TeamLoadScopingTests`, `InstanceScopingTests`
  (`tests/test_tenant_scoping.py:107-173`) - another tenant's action, instance
  list, instance detail and submissions are all invisible.

What it does not cover:

1. **Notification after a reversal** - nothing asserts what the reopened stage's
   approvers are told, which is why §8's first item is invisible.
2. **Reversing a rejection**, in either direction: neither the refusal nor a
   route around it.
3. **What happens to the document after a reversed final approval** - the
   handler side of §8's third item.
4. **`dashboard/pending/`** as an endpoint. `my_queue`'s three filters are the
   heart of the approvals inbox and no test exercises the stale-attempt filter or
   the already-voted filter through the view.
5. **`on_behalf_of` reaching the action row** - delegation is tested at
   resolution, not at vote time.
6. **Concurrency.** `select_for_update` is described as the guard for two
   simultaneous votes and nothing tests two.
7. **The list filters**, valid or malformed.
