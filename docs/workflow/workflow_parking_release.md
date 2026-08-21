# workflow_parking_release

The failure mode the engine chooses on purpose, and the two ways out of it. A
stage that activates while nobody holds its approving role **parks**: ACTIVE,
IN_PROGRESS, empty approver snapshot, no human able to decide it. This slice owns
detecting that, repairing it when somebody is appointed, and the submitter's
escape hatch when nobody ever will be.

The engine that creates the state is `workflow_engine_routing`; the resolver that
returns nobody is `workflow_approvers`.

Route: `POST instances/<id>/continue-without-approval/`.

---

## 1. What it is (and what it is NOT)

- **Parking is the safe failure, not a bug.** It is what
  `skip_if_no_approvers = False` means, and every ladder over money sets it:
  spend must never approve itself (`services/parking.py:1-8`).
- **Parking on its own would be a trap.** Eligibility is resolved exactly once,
  at activation, and every read path afterwards consults that frozen snapshot. A
  stage activated with **zero** approvers is therefore permanently unreachable for
  that attempt: granting somebody the role afterwards changes nothing, and only a
  return-plus-resubmit would re-snapshot. The repair exists for exactly that
  (9-16).
- **The repair fills an empty snapshot. It does not move a workflow.** It never
  approves, advances, skips or terminates anything - it restores reachability and
  the decision still needs a human (26-34).
- **The repair never touches a populated snapshot.** Emptiness is re-checked
  *inside* the row lock and is the hard precondition for any write, because the
  freeze guarantee protects an approver who is mid-review from having their
  eligibility rewritten under them.
- **The release is a maker-checker bypass, and the module says so.**
  `services/release.py:12-22` states the trade plainly: releasing a stage without
  a vote means the document reaches its terminal state with no second pair of
  eyes, and on a payout batch that is the whole control. It is a deliberate
  product decision, and the record it writes is what stands in the control's
  place.
- **A release may not bypass a human who exists.** The stage must be genuinely
  parked, re-checked after a repair pass and again under a row lock. If anybody
  at all can decide it - including somebody appointed one second ago - the
  release is refused (25-30).
- **A release frees one stage, not a ladder.** If the next stage has approvers
  the document goes back into ordinary review; if it parks too, it needs its own
  release and its own record.
- **It lives in the engine because the defect does.** The repair was first
  written inside `vs_procurement` and fenced to that app's four document types,
  which left the same trap open on the other five the engine serves - including
  `payments.payout_batch`, the path that sends money to a bank (18-23).

## 2. Domain model

None of its own. Parking is a *shape* in existing rows:

```text
WorkflowStageInstance.status   == ACTIVE
WorkflowInstance.status        == IN_PROGRESS
no WorkflowStageApprover rows for (stage_instance, attempt)
```

`empty_active_stages` (`services/parking.py:65-97`) is that predicate as one
indexed query with a `NOT EXISTS` subquery. On healthy data it matches nothing
and every caller short-circuits before resolving a single role holder.

Its `select_related` list is load-bearing and commented: `instance__tenant` in
particular, because both the override lookup and the role-holder lookup take the
tenant **object**, not its id, so omitting it is a descriptor query per stage -
the exact per-row cost the module's cache and its query-count test exist to
prevent.

## 3. Entry points

| Caller | Purpose |
|---|---|
| `services/my_queue.pending_approval_snapshots` | Repairs the tenant's parked work before reading anybody's inbox |
| `services/release.parked_stage` | Repairs one instance before offering the bypass |
| `parked_object_ids`, `parked_stage_instance`, `parked_id_subquery` | The read-side helpers domain list views use to flag parked documents |
| `POST instances/<id>/continue-without-approval/` | The bypass itself |
| `vs_procurement.approval_override` | The stricter sibling: same mechanics behind a `CRITICAL` key and a mandatory typed justification |

The endpoint (`views.py:546-590`) carries **no RBAC key** - `get_permissions`
falls through to `IsAuthenticatedAndActive` - and is gated by
`release_svc.may_release` instead. Its docstring says why: this is the
submitter's own escape from their own stuck submission, and a permission key
would be the wrong shape for that.

## 4. The repair

```text
repair_workflows(tenant=…, instance_id=…, document_types=…)
   └─ empty_active_stages()            one indexed query
      └─ repair_stages(rows)
           ResolutionCache()           memo shared across the batch
           for each row:
              has_candidates()?        cheap: could anybody resolve at all?
                 no  → skip, take no lock
                 yes → _repair_one()
                          ┌ transaction ──────────────────────────┐
                          │ lock_parked_stage()  re-check under    │
                          │   the row lock: still ACTIVE,          │
                          │   still IN_PROGRESS, still empty       │
                          │ resolve → nobody? return 0             │
                          │ bulk_create the snapshot rows          │
                          │ STAGE_ACTIVATED audit,                 │
                          │   context.repair = …refilled           │
                          │ notify the newly eligible approvers    │
                          └────────────────────────────────────────┘
```

`ResolutionCache` (`services/parking.py:145-231`) memoises the expensive part -
"who holds this role in this scope for this tenant and branch" - on
`(source, role key, scope, tenant, branch)`, so a page of parked documents
sharing one stage costs one RBAC lookup.

The memo is **opt-in per source, not opt-out**, and the direction matters
(159-164): only a `ROLE` stage carrying a key can be answered from a role-holder
lookup. Every other source falls through to live resolution. The alternative
default - treating an unrecognised source as "provably nobody" - would make the
repair silently skip those stages, and a stage the repair skips is a document
that parks and never un-parks.

`has_candidates` can answer outright because `resolve_approvers` excludes the
requester **before** expanding delegations: an empty holder set after removing
the requester provably yields no approvers, so a sole approver who is also the
requester means parked.

**The notification is the point of the repair.** Until it existed a repaired
document waited silently: the stage was already ACTIVE, so no activation
notification had ever fired, and the audit row nobody reads was the only trace.
Whoever was appointed discovered the work by opening the queue on spec, which can
be days later (`services/parking.py:275-296`).

`repair_stages` wraps each row in a bare `except` (321-329) on purpose: this runs
on the read path over whatever happens to be parked, so one broken template - a
stage naming a source the resolver raises on - must not take down the whole
approvals inbox for the tenant. The resolver is right to raise; the sweep
degrades to leaving the stage parked, which is where it already was.

## 5. The release

`release_parked_stage` (`services/release.py:195`), one transaction:

1. `_clean_reason` - strip whitespace, default to a stock sentence, refuse
   anything over 500 characters or anything that is not text. The text is never
   summarised or truncated silently: it is the actor's own words on an
   append-only record.
2. `parked_stage(instance)` - runs a **repair pass first**, so an instance that
   only looked parked correctly reports None and the answer becomes "get them to
   decide it".
3. `lock_parked_stage` - re-assert the same precondition under a row lock,
   because between the read and here a repair could have staffed the stage or a
   vote could have landed.
4. Write the `APPROVER_ACTED` audit row with
   `action = "RELEASED_NO_APPROVER"`, `override = True`, the stage code and
   attempt, the approver source, the role key, the requirement sentence and the
   reason - **before** the workflow moves, so if the engine's terminal callbacks
   run (a payout dispatches, a purchase order issues) the evidence of why already
   exists.
5. `routing._skip_stage(... STAGE_SKIPPED_NO_APPROVER, RELEASE_SKIP_REASON)` -
   resolved the way the engine resolves a stage nobody ran, with a reason that
   distinguishes "no approver, and somebody chose to continue" from an ordinary
   skip.
6. `routing.advance_instance(...)` - the engine's own public advance, so the
   document terminates through the same `_terminate_approved` →
   `handler.on_approved` path a real final vote takes. That is what makes a
   released payout dispatch. Hand-rolling it would be the more dangerous choice.

Every refusal leaves nothing written: the whole release is one transaction.

### Who may release (`services/release.py:269-283`)

```python
if user.is_superuser or is_vision_super_admin(user):  return True
return instance.requested_by_id == user.pk
```

The submitter, or platform staff cleaning up on a tenant's behalf. Deliberately
**not** "any authenticated user in the tenant": letting an unrelated user release
somebody else's parked spend would be a different thing entirely, and a real
hole.

### What the dialog is told (`describe_park`, `stage_requirement`)

`describe_park` returns `{"parked": False}` or the stage code, label, approver
source, role key (blank unless the stage really resolves by a named role), a
`requirement` sentence, and the document type.

`stage_requirement` (`services/release.py:114-159`) composes that sentence per
source - "assign someone to the payout-approver role", "add someone to the
Exam Board approver group", "appoint a head for the department of the person who
raised this" - and falls back to a truthful generic line for a source it has not
been taught, because a vague sentence is recoverable and a blank space where the
instruction should be is not.

`approval_block` (257-266) is the same payload shaped for a submit response, so
every module's "submit for approval" endpoint can tell the client in the submit
response itself that nobody can approve what it just submitted.

## 6. What it writes

| Operation | Writes |
|---|---|
| Detection (`empty_active_stages`, `parked_object_ids`) | nothing |
| Repair | `WorkflowStageApprover` rows, a `STAGE_ACTIVATED` audit row marked `repair: approver_snapshot_refilled`, and a `workflow.stage_activated` notification |
| Release | an `APPROVER_ACTED` audit row marked `RELEASED_NO_APPROVER`, a `SKIPPED` stage instance with `RELEASE_SKIP_REASON`, and whatever `advance_instance` then does |

The repair reuses `STAGE_ACTIVATED` with an explicit marker rather than inventing
an event, mirroring how routing records its
`stage_active_with_no_approvers` warning: the stage did not re-activate, only its
eligibility snapshot was filled in.

Note what the release does **not** write: nothing on the business document itself
says it was released rather than approved. The distinction lives in the audit
log and in the skipped stage's `skip_reason`.

## 7. Worked example

Bright Star has just been provisioned. `seed_payout_approvals` created the
`payout-approver` role and, correctly, assigned nobody
(`services/roles.py:1-14`). Their bursar Adaeze submits a payout batch of
₦2.4m.

```text
submit_for_approval
  → stage payout-approval activates
  → resolve_approvers → []            (nobody holds payout-approver)
  → skip_if_no_approvers is False     → no skip
  → STAGE_ACTIVATED { warning: stage_active_with_no_approvers }
  → instance IN_PROGRESS, snapshot empty            ← parked

response: { …batch…, "approval": {
    "instance_id": "aB3xY9kM", "parked": true,
    "stage_code": "payout-approval", "stage_label": "Payout approval",
    "approver_source": "ROLE", "role_key": "payout-approver",
    "requirement": "assign someone to the payout-approver role",
    "document_type": "payments.payout_batch" } }
```

Two endings.

**Somebody is appointed.** The school admin assigns Chidi to `payout-approver`.
The next time anyone opens an approvals inbox in that tenant,
`pending_approval_snapshots` runs a repair pass, `_repair_one` fills the
snapshot, writes the marked audit row, and notifies Chidi. The batch appears in
his queue and he decides it normally. Nothing was bypassed.

**Nobody will be.** Adaeze, told at submission that nobody can approve this,
clicks through:

```text
POST /v1/workflow/instances/aB3xY9kM/continue-without-approval/?tenant=bright-star
{"reason": "Single-person finance office; the vendor cut-off is today."}
```

The repair runs again (still nobody), the lock re-asserts it, the audit row is
written with her name, her reason and the requirement, the stage is SKIPPED with
`RELEASE_SKIP_REASON`, `advance_instance` finds no next stage, and
`_terminate_approved` fires `on_approved` - **the payout dispatches**. If Chidi
had been appointed between the dialog opening and the click, the answer would
have been `409 NOT_PARKED` with the fresh park description, not a bypass.

## 8. Gotchas / known limitations

Full evidence in **`error/workflow/workflow_code_issues.md`**.

- **`services/release.py` has no test in this module at all.** The module's most
  dangerous operation - the one that dispatches money without a second pair of
  eyes - is covered by nothing here (`workflow_code_issues.md` §2).
- **The release is only as safe as who counts as the submitter**, and the
  submitter is whoever created the instance. Combined with the unscoped document
  lookup on `POST instances/` (`workflow_code_issues.md` §1), a caller can make
  themselves the requester of another tenant's document and then hold release
  rights over it.
- **A release is a permanent bypass with no review afterwards.** Nothing flags
  the resulting document for retrospective sign-off, and no report lists
  released instances - the evidence is one audit row inside one instance.
- **The repair runs on every approvals-inbox read**, tenant-wide
  (`services/my_queue.py:37-39`). On healthy data that is one indexed query; on a
  tenant with many parked documents it is a repair pass per inbox open.
- **`parked_object_ids` casts `document_object_id` to an integer**
  (`services/parking.py:409-424`) - safe because the content-type filter restricts
  it to that model's integer pks, but it hard-codes an assumption the generic FK
  does not make (`document_object_id` is a `CharField`).
- **The bare `except Exception` in `repair_stages`** is justified and logged, but
  it means a template whose approver source the resolver cannot handle degrades
  silently for every parked document in the tenant.
- **Justified by design:** the emptiness precondition is re-checked inside the
  lock, so a populated snapshot is never rewritten.
- **Justified by design:** the memo is opt-in per source, so an unknown source
  costs a query rather than a lost document.
- **Justified by design:** the release terminates through `advance_instance` and
  the handler's own `on_approved`, rather than hand-rolling the terminal
  transition.
- **Justified by design:** the audit row is written before the workflow moves.

## 9. Permissions & tenant isolation

| Surface | Gate |
|---|---|
| `continue-without-approval` | `IsAuthenticatedAndActive` + `may_release` (submitter, superuser, or vision super admin) + tenant-scoped `get_object()` |
| The repair | none - it is a service, invoked on read paths |
| `vs_procurement.approval_override` | a `CRITICAL` permission key and a typed justification |

The repair's own scoping is the thing to watch: `repair_workflows(tenant=…)` is
always called with a tenant by the read paths
(`services/my_queue.py:37-39` carries the comment "one person opening their
queue must not do work on behalf of other tenants"), and
`repair_workflows(instance_id=…)` narrows to one instance. Called with neither,
it would sweep every tenant - nothing in the engine does that, but the function
allows it.

The release's tenant boundary is inherited entirely from the view's
`get_object()`, which reads the tenant-filtered instance queryset.

## 10. Code map

| File | Responsibility |
|---|---|
| `services/parking.py:65-97` | `empty_active_stages` - the detection predicate |
| `services/parking.py:100-138` | `lock_parked_stage` - the in-transaction definition of parked |
| `services/parking.py:145-231` | `ResolutionCache` |
| `services/parking.py:238-297` | `_repair_one` |
| `services/parking.py:300-345` | `repair_stages`, `repair_workflows` |
| `services/parking.py:358-424` | `parked_object_ids`, `parked_stage_instance`, `parked_id_subquery` |
| `services/release.py:76-92` | `_actor_label`, `_clean_reason` |
| `services/release.py:95-111` | `parked_stage` |
| `services/release.py:114-192` | `stage_requirement`, `describe_park` |
| `services/release.py:195-254` | `release_parked_stage` |
| `services/release.py:257-283` | `approval_block`, `may_release` |
| `views.py:546-590` | `continue_without_approval` |
| `services/my_queue.py:31-45` | the repair call on the inbox read path |

## 11. Test coverage & gaps

- `ParkedRepairNotificationTests` (`tests/test_notifications.py:214-320`) - the
  repair notifies the newly eligible approver; a repair that staffs nobody
  notifies nobody; a second pass neither restaffs nor renotifies.
- `OrganogramSourceResolutionTests` (`tests/test_services.py:1341-1396`) - an
  unstaffed organogram stage parks instead of auto-approving, and filling the
  manager seat lets the repair staff it.
- `test_an_unavailable_organogram_degrades_to_parking_not_to_approval` (`1397`)
  and `test_a_stage_emptied_by_containment_parks_instead_of_auto_approving`
  (`1515`).
- `RoleCoverageTests` (`tests/test_role_coverage.py`) - the administrative report
  that finds unstaffed roles before they park anything, including that creating
  the role grants nobody anything.

What it does not cover, and the first item is the serious one:

1. **The release, entirely.** No test in `vs_workflow` calls
   `release_parked_stage`, `may_release`, `describe_park`, `stage_requirement` or
   the `continue-without-approval` endpoint. Not the refusal when somebody can
   decide, not the reason validation, not the audit row, not the termination
   through `on_approved`. `vs_procurement` tests its own stricter sibling; the
   engine's own bypass is untested here.
2. **`lock_parked_stage`'s re-check under contention** - the property the whole
   design rests on.
3. **`parked_object_ids` / `parked_id_subquery`** - the list-view helpers.
4. **`ResolutionCache`'s memo** - that a page of parked documents sharing a stage
   costs one lookup, which the module's own docstring calls out as the reason the
   cache exists.
5. **A repair on a `WORKFLOW_GROUP` or `DYNAMIC_ROLE` stage** - the sources that
   deliberately fall through the memo to live resolution.
