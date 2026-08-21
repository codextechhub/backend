# workflow_code_issues

Everything wrong with `vs_workflow`, in one place, ordered by how much it costs.
Each item states the defect, the evidence, what actually happens to a person, and
the fix. The six slice reports (`workflow_templates`,
`workflow_engine_routing`, `workflow_approvers`, `workflow_actions_lifecycle`,
`workflow_parking_release`, `workflow_notifications_audit`) point here rather
than repeating it.

Baseline: the `vs_workflow` suite is **`Ran 253 tests in 68.765s` - OK**
(`cd apps && DB_NAME=cx_workflow_doc ../cx/Scripts/python.exe manage.py test
vs_workflow --settings=apps.settings.local --noinput`). Every item below is
therefore something those 253 tests do not catch. Every claim is traced to a file
and line. Nothing here is speculative.

**Worth saying before the list.** This is the most carefully reasoned module
documented so far. The tenant-containment rules in `resolve_approvers`, the
freeze-then-repair design for parked stages, the shared `readonly_next_stage`
that stops a preview describing a route the engine will not take, and the
decision to terminate a release through the engine's own `advance_instance` are
all right, and several of them carry comments explaining a defect that was found
and closed. The findings below are what is left.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in the
code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | Submitting a document for approval never checks it belongs to your tenant | **Critical** |
| 2 | The maker-checker bypass has no test in this module at all | **High** |
| 3 | A route condition can read any attribute reachable from the document, and the value lands in the audit trail | **High** |
| 4 | One typo in `notification_events` silences a template's entire notification surface | **High** |
| 5 | A non-exclusive delegation deadlocks a UNANIMOUS stage | **High** |
| 6 | Reversing an approval reopens the stage and tells nobody | **Medium** |
| 7 | A terminal rejection can never be reversed, and a reversed approval does not undo the document | **Medium** |
| 8 | No school role holds `workflow.instance.view`, so approvers can vote on documents they cannot open | **Medium** |
| 9 | Approvers are resolved twice on every stage activation | **Medium** |
| 10 | The instance detail payload is unbounded | **Medium** |
| 11 | Nothing stops a tenant flattening its own approval ladder | **Medium** |
| 12 | Template changes are not audited, and the notifier is handed `school=` | **Medium** |
| 13 | Four response shapes, none of them the platform envelope | **Low** |
| 14 | A stage about to be skipped for want of approvers is activated first | **Low** |
| 15 | `all` and `any` conditions do not short-circuit | **Low** |
| 16 | Smaller defects and dead code | **Low** |

---

## 1. Submitting a document for approval never checks it belongs to your tenant

**Critical.**

### The defect

```python
# views.py:504-514
def create(self, request):
    p = SubmitForApprovalSerializer(data=request.data)
    p.is_valid(raise_exception=True)
    d = p.validated_data
    try:
        ct = ContentType.objects.get(pk=d["content_type_id"])
        document = ct.model_class().objects.get(pk=d["object_id"])
    except Exception:
        return Response({... "DOCUMENT_NOT_FOUND" ...}, status=404)
    instance = submission_svc.submit_for_approval(
        document=document, requested_by=request.user, ...)
```

The document is loaded by content type and primary key with **no tenant filter
and no ownership check**. `request.tenant` is not consulted. `submit_for_approval`
then calls `document_scope(document, default_tenant=requested_by.tenant)`
(`services/submission.py:46`), which reads the tenant off the **document**
(`services/resolution.py:121-126`) - so the resulting `WorkflowInstance` is filed
under the document's tenant, not the caller's.

The only accidental protection is the manager on the target model. Some
submittable models are tenant-aware and would raise `DoesNotExist` under the
ambient tenant; `vs_payments.PayoutBatch` declares no custom manager at all
(`vs_payments/models.py:176-257`), so `objects` is a plain `models.Manager` and
the lookup succeeds across tenants.

No handler closes the gap either. `validate_document` is the domain's own guard
and every implementation checks only document state: the payout handler checks
`status == DRAFT` and that pending instructions exist
(`vs_payments/workflow_handlers.py:74-86`); the finance handlers check `DRAFT`
and run the posting preflight (`vs_finance/workflow_handlers.py:90-100`). None
compares the requester's tenant with the document's.

### What actually happens

Kemi is a CX platform engineer holding `workflow.instance.submit`. Bright Star
has a draft payout batch, id 4471, for ₦8.2m to seventeen vendors, sitting in
their finance office unsubmitted.

```text
POST /v1/workflow/instances/?tenant=codex
{"content_type_id": 91, "object_id": "4471"}
  → 201
```

A `WorkflowInstance` now exists **in Bright Star's tenant**, running Bright
Star's payout ladder, with `requested_by = Kemi`. Consequences, in order of
how bad they get:

- Bright Star's payout approvers are emailed and belled about a batch nobody at
  Bright Star submitted.
- Kemi is the requester, so she can `withdraw` it - and the handler's
  `on_withdrawn` runs against Bright Star's batch.
- Kemi is the requester, so `may_release` returns True for her
  (`services/release.py:281-282`). If Bright Star has nobody in `payout-approver`
  - which is the seeded state for a new tenant (`services/roles.py:1-14`) - the
  stage parks and **Kemi can continue it without approval**, which runs
  `_terminate_approved` → `handler.on_approved` → **the ₦8.2m dispatches**.

Object ids are sequential integers and the content-type id is discoverable, so
nothing here requires guessing anything hard.

The exposure is bounded today by `workflow.instance.submit` being seeded to
`xvs_super_admin` and `xvs_platform_admin` only
(`management/commands/seed_workflow_permissions.py:50-51`). But the key is
`PermissionScope.TENANT` (107), so a school role may legally hold it, and the
whole point of the payout ladder is that platform staff are not supposed to be
able to move a tenant's money alone.

### Why it exists

`submit_for_approval` is written for the service callers - finance, payments and
procurement each call it from code that has already resolved and authorised the
document - and `document_scope` reading the tenant off the document is correct
for them. The REST route was added on top and inherited the assumption without
inheriting the check.

### The fix

1. **Scope the lookup, then verify the scope.** After loading the document,
   compare `document_scope(document)[0]` with `request.tenant` and answer the
   same `404 DOCUMENT_NOT_FOUND` when they differ - the response already exists,
   so a cross-tenant id becomes indistinguishable from a missing one.
2. **Have the base handler assert it too**, so a service caller cannot skip it
   either: `BaseWorkflowHandler.validate_document` is the natural home for
   "requested_by must belong to the document's tenant".
3. Add the test: a caller in tenant A submitting tenant B's document gets a
   `404`, and no instance is written.

---

## 2. The maker-checker bypass has no test in this module at all

**High.**

### The defect

`services/release.py` is 282 lines whose entire subject is stepping past an
approval nobody can give. Its own docstring states the trade
(`services/release.py:12-22`):

> Releasing a stage without a vote means the document reaches its terminal state
> with no second pair of eyes. On a payout batch or a purchase order that is the
> whole maker-checker control, and the only thing standing in its place
> afterwards is the record this module writes.

Grep the module's 253 tests for `release_parked_stage`, `may_release`,
`describe_park`, `stage_requirement`, `approval_block` or
`continue-without-approval`: nothing. `vs_procurement` tests its own stricter
sibling (`vs_procurement.approval_override`, behind a `CRITICAL` key and a typed
justification); the engine's own bypass, which is ungated by any key and
available on every document type the engine serves, is covered by nothing here.

What is untested, specifically:

| Behaviour | Consequence if it regresses |
|---|---|
| The refusal when somebody can decide (`NotParkedError`) | the dialog becomes a self-approval button on a document that has a reviewer |
| `may_release` | an unrelated user releases somebody else's parked spend |
| The re-check under `lock_parked_stage` | a release lands on top of a vote or a repair |
| The audit row written *before* the move | a payout dispatches with no record of why |
| Termination through `advance_instance` | the document does not actually issue, or issues twice |
| `_clean_reason`'s 500-character ceiling and type check | a `ValueError` reaches the client as a 500 |

### What actually happens

Nothing, today - the code reads correct. The cost is that it is the one part of
the engine where a regression is both silent and expensive, and nothing would
catch one. Combined with §1, the same untested path is what dispatches another
tenant's money.

### The fix

Write the tests, in this order: the refusal when the stage has an approver; the
`may_release` matrix (submitter yes, unrelated tenant user no, super admin yes);
the audit row's contents; and an end-to-end release that asserts
`handler.on_approved` ran. The fixtures already exist -
`OrganogramSourceResolutionTests` builds parked instances
(`tests/test_services.py:1341-1396`) and `ParkedRepairNotificationTests` builds
them again (`tests/test_notifications.py:246-260`).

---

## 3. A route condition can read any attribute reachable from the document, and the value lands in the audit trail

**High.**

### The defect

```python
# conditions/evaluator.py:15-24
def _extract_field(document, path):
    current = document
    for segment in path.split("."):
        ...
        current = getattr(current, segment, _MISSING)
    return current
```

An unbounded `getattr` walk. `validate_condition` checks that `field` is
non-empty (`evaluator.py:157-158`) and nothing else - not a prefix, not a depth
limit, not an allowlist.

Whatever it lands on is then stringified into the trace:

```python
# conditions/evaluator.py:50-54, 108-109
def _safe(v):
    if isinstance(v, Decimal): return str(v)
    try: json.dumps(v); return v
    except (TypeError, ValueError): return str(v)
...
return result, {"kind": "op", "op": op, "field": field_path,
                "left": _safe(left), "right": _safe(value), "result": result}
```

and the trace is written into a `ROUTE_EVALUATED` audit row
(`services/routing.py:98-101`), which `WorkflowAuditLogReadSerializer` returns
raw (`serializers.py:255-260`), embedded unpaginated in every instance detail
(`serializers.py:277-286`).

### What actually happens

A template author writes a route condition against a purchase order:

```json
{"op": "eq", "field": "created_by.password", "value": "never-matches"}
```

It evaluates false, the route is not taken, and the workflow proceeds normally.
It also writes, into the instance's audit log:

```json
{"kind": "op", "op": "eq", "field": "created_by.password",
 "left": "pbkdf2_sha256$870000$…", "right": "never-matches", "result": false}
```

Anybody who can open that instance reads the hash. The same trick reaches
`entity.tenant.slug`, a vendor's bank details through a relation, a staff
profile's `account_number` - anything the document object graph touches.

The bar is `workflow.template.manage`, which is platform-only today and whose
holder can already read a great deal. What makes it worth fixing anyway is that
it turns a *configuration* permission into a *data-exfiltration* one, in a place
nobody inspects: the value ends up in an append-only log that survives the
condition being corrected.

### The fix

Fix the class, not the case:

1. **Bound the walk.** Refuse a `field` path deeper than two or three segments at
   `validate_condition`, or require the first segment to be in a per-handler
   allowlist the document type publishes - the same shape `vs_tickets` uses for
   its context registry.
2. **Stop copying values into the trace.** The trace exists to answer "why did
   this route fire"; the operator, the field path and the result answer that. If
   the value is genuinely needed, record it only for scalar types and truncate
   it.
3. **Do not serialize raw `context`.** The audit serializer should project the
   keys it means to expose rather than the whole blob.

---

## 4. One typo in `notification_events` silences a template's entire notification surface

**High.**

### The defect

```python
# tasks.py:21-26
events = instance.template.notification_events or {}
if events and not events.get(event_key, False):
    return
```

An untouched `{}` means "every wired event"; a non-empty dict is treated as
exact intent, so any key not in it is off. And the field accepts anything:

```python
# serializers.py:147-148
notification_events = serializers.DictField(child=serializers.BooleanField(),
                                            required=False, default=dict)
```

No validation against `NOTIF_EVENT_KEYS` (`constants.py:143-148`), which exists
three files away.

### What actually happens

A platform admin publishes the payout ladder and wants approvers notified. They
write:

```json
"notification_events": {"workflow.stage_activate": true}
```

one character short of `stage_activated`. The publish succeeds. From that moment:

- no approver is ever emailed or belled about a payout awaiting them;
- no requester is told their payout was returned, rejected or approved;
- **the parking repair's notification is silenced too** - the one whose whole
  purpose is telling a newly appointed approver that work is waiting
  (`services/parking.py:275-296`).

The payouts still park correctly and the queue still lists them. They simply
wait until somebody opens the inbox on spec. Nothing errors, nothing is logged,
and the template screen shows the key the author typed, which looks right.

### The fix

1. **Validate the keys at publish.** `validate_notification_events` rejecting any
   key outside `NOTIF_EVENT_KEYS` is four lines, and it is the same shape as
   `validate_stages`'s enum checking directly above it.
2. **Consider naming only the wired ones.** Six of the ten keys are reserved
   (`constants.py:150-158`), so enabling `workflow.approved` today is a no-op the
   author cannot distinguish from a working setting.
3. Add the test: a bad key is a `400`, and a good key fires.

---

## 5. A non-exclusive delegation deadlocks a UNANIMOUS stage

**High.**

### The defect

Resolution adds the delegate **without removing the delegator** unless the
delegation is exclusive:

```python
# services/approvers.py:476-494
excluded_delegators = {d.delegator_id for d in delegations if d.exclusive}
for u in base_users:
    if u.pk in excluded_delegators: continue
    result.append(EligibleApprover(user=u, on_behalf_of=None))
for d in delegations:
    result.append(EligibleApprover(user=d.delegate, on_behalf_of=d.delegator))
```

Both rows are written into the frozen snapshot. Unanimity then counts snapshot
rows:

```python
# services/actions.py:111-118
eligible_count = WorkflowStageApprover.objects.filter(
    stage_instance=stage_instance, attempt=stage_instance.attempt).count()
...
return eligible_count > 0 and approved_count >= eligible_count
```

and the unique constraint is per **actor**, so the delegator and the delegate are
two votes that both have to arrive.

### What actually happens

Bright Star's PO stage is `UNANIMOUS` with two role holders, Adaeze and Chidi.
Adaeze goes on maternity leave and delegates to Femi - non-exclusively, because
the form's default is not exclusive and "let Femi help" is what she meant.

Tunde submits a PO. The snapshot is three rows: Adaeze, Chidi, and Femi on
behalf of Adaeze. `eligible_count` is 3.

Chidi approves. Femi approves. `approved_count` is 2. **The stage does not
advance.** The third approval has to come from Adaeze, who is on leave, which is
the reason the delegation exists. The PO sits until she returns, or until an
admin cancels it and Tunde starts again.

Nobody involved can see why. The queue shows the PO as awaiting a decision; the
detail shows two approvals; nothing says the third row is a person who is away.

### Why it exists

Delegation was modelled as "add somebody", which is right for `ANY` and for
`QUORUM`. Unanimity is the rule that counts heads, and nothing reconciled the
two.

### The fix

Decide what a delegation means for a threshold, and apply it in one place:

1. **Count authorities, not rows.** `_stage_fully_approved` should treat a
   delegator and their delegates as one eligible authority: group the snapshot by
   `COALESCE(on_behalf_of_id, user_id)` and require an approval per group. That
   also fixes `QUORUM` counting, which today can be satisfied by one authority
   voting through two delegates.
2. Or **make delegation exclusive by default** and say so on the form - a
   narrower fix that leaves the arithmetic wrong for anybody who unticks it.
3. Add the test: a `UNANIMOUS` stage with a non-exclusive delegation advances
   when every authority has approved.

---

## 6. Reversing an approval reopens the stage and tells nobody

**Medium.**

```python
# services/actions.py:260-273
si = original.stage_instance
if si.status in {APPROVED, REJECTED}:
    si.status = ACTIVE
    si.resolved_at = None
    si.save(...)
    instance.current_stage = si.stage
    if instance.status == APPROVED:
        instance.status = IN_PROGRESS
        instance.completed_at = None
    instance.save(...)
```

The stage is ACTIVE again and the instance is back in review. No notification is
sent.

The engine knows this matters. `services/parking.py:275-296` sends one in
precisely this situation, with a comment explaining why:

> Until this existed a repaired document waited silently: the stage was already
> ACTIVE, so no activation notification had ever fired for it, and the audit row
> nobody reads was the only trace.

A reversal produces exactly that state, and did not get the same treatment.

**What actually happens.** An admin reverses Femi's vote on a purchase order that
had been fully approved. The PO drops back into Adaeze's and Chidi's queues with
no bell, no email, and no change they would notice unless they refresh the
approvals inbox. The order has already issued (§7), and the people who now need
to re-decide it do not know they do.

**The fix.** Call the same `notify(instance, NOTIF_EVENT_STAGE_ACTIVATED, …)`
the repair calls, to the stage's existing snapshot, when a reversal re-activates
a stage. Four lines, and `notify` is already public for this reason.

---

## 7. A terminal rejection can never be reversed, and a reversed approval does not undo the document

**Medium. Two halves of one asymmetry.**

```python
# services/actions.py:241-243
if instance.is_terminal and instance.status != WorkflowInstanceStatus.APPROVED:
    raise ReversalNotAllowedError(
        f"Cannot reverse on instance in status {instance.status}.")
```

So reversal works on a live instance and on an APPROVED one, and is refused on
REJECTED, WITHDRAWN and CANCELLED.

**Half one: a mis-clicked rejection is unrecoverable.** A finance manager
rejects the wrong purchase order; the stage's `on_rejection` is `TERMINAL`, so
the instance is REJECTED and `handler.on_rejected` has run. Nobody - not the
manager, not an admin, not a super admin - can reverse it. The requester creates
the document again from scratch, and the audit trail records a rejection that
everybody agrees was a mistake.

**Half two: reversing an approval does not undo what it caused.**
`_terminate_approved` fires `handler.on_approved` inside the transition
(`services/routing.py:360-361`), so by the time anybody reverses, the payout has
dispatched or the purchase order has issued. `reverse_action` moves the instance
back to IN_PROGRESS and calls no handler at all. The result is a workflow that
says "awaiting decision" over a document the domain has already acted on.

**The fix.**

1. **Allow reversal of a terminal rejection**, symmetrically with APPROVED, and
   restore the instance to IN_PROGRESS the same way. WITHDRAWN and CANCELLED are
   the requester's and the admin's own decisions and can stay terminal.
2. **Add an `on_reversed` callback** to `BaseWorkflowHandler` and call it when a
   reversal un-approves an instance, so the domain can refuse (a dispatched
   payout cannot be recalled) or compensate. Refusing inside the transaction is
   the honest answer for money.
3. At minimum, **document the one-way door** in the reversal response, so an
   admin reversing a final approval knows the document has already moved.

---

## 8. No school role holds `workflow.instance.view`, so approvers vote on documents they cannot open

**Medium.**

`seed_workflow_permissions` grants all eight workflow keys to `xvs_super_admin`
and `xvs_platform_admin` on the `codex` tenant
(`management/commands/seed_workflow_permissions.py:50-51,120-129`). Nothing else
in the repo grants a `workflow.*` key to anything - the only other mention
anywhere is `vs_rbac/views.py:62`, which reads `workflow.template.manage` to
decide who may list roles.

Meanwhile the instance viewset gates by action (`views.py:475-485`):

| Action | Key | Reachable in a school tenant? |
|---|---|---|
| `list`, `retrieve` | `workflow.instance.view` | **no** |
| `create` | `workflow.instance.submit` | **no** |
| `cancel` | `workflow.instance.cancel` | **no** |
| `actions` (vote), `withdraw`, `resubmit`, `continue-without-approval` | none | yes |
| `dashboard/pending/`, `dashboard/submitted/` | none | yes |
| `dashboard/team-load/` | `workflow.instance.view` | **no** |

**What actually happens.** Chidi is Bright Star's purchase-order approver. His
approvals inbox lists the PO - `PendingApprovalsView` needs only authentication
and serialises the instance inline (`views.py:636-649`). He taps it. The detail
page calls `GET /v1/workflow/instances/<id>/` and gets `403`. He can approve it
from the list without ever seeing its stage history, its audit trail or its
document summary; he cannot open it to look.

The team-load board, which exists so a manager can see where work is piling up,
is `403` for every school user in the platform.

**The fix.** Decide whether the workflow REST surface is meant for tenants at
all. If it is, seed `workflow.instance.view` (and probably `.submit`) onto the
school prebuilts the way `vs_tickets` seeds its view key. If it is not, remove
the dashboards and voting endpoints from the tenant surface too, so the module
is consistently platform-only rather than half-open.

---

## 9. Approvers are resolved twice on every stage activation

**Medium.**

```python
# services/routing.py:325-327
_activate_stage(instance, next_stage, current_attempt)   # resolves + snapshots + notifies
eligible = approvers_service.resolve_approvers(next_stage, instance)   # resolves again
if not eligible:
```

`_activate_stage` already called `resolve_approvers` (`routing.py:222`) and wrote
the result into the snapshot. The caller then throws that away and resolves from
scratch to decide whether to skip. `actions.resubmit` does the same
(`actions.py:309` then `314`).

Resolution is not cheap: a role lookup plus an assignment query, or a group's
mixed membership, or an organogram climb, plus the delegation query and two
containment passes.

Worse than the cost: **the two calls can disagree.** A delegation whose
`starts_at` falls between them, or a role assignment committed by another
transaction, changes the answer - and the snapshot says one thing while the skip
decision uses another.

**The fix.** Have `_activate_stage` return the eligible list it already
resolved, and use that. Two lines, and it removes the divergence as well as the
query.

---

## 10. The instance detail payload is unbounded

**Medium.**

```python
# serializers.py:277-286
class WorkflowInstanceDetailSerializer(WorkflowInstanceListSerializer):
    stage_instances = WorkflowStageInstanceReadSerializer(many=True, read_only=True)
    audit_logs      = WorkflowAuditLogReadSerializer(many=True, read_only=True)
```

and each stage instance embeds **all** its actions and **all** its eligible
approvers (`serializers.py:242-243`).

A long-running instance - a procurement chain returned twice, resubmitted twice,
with five stages and route conditions on each - accumulates a stage instance per
stage per attempt, an action row per vote plus reversals, an approver row per
eligible person per attempt, and an audit row per activation, skip, route
evaluation, vote and transition. All of it comes back on every read of the
detail, including every `ROUTE_EVALUATED` trace (§3).

There is no pagination, no `?include=` and no cap. The same payload is returned
by `create`, `withdraw`, `resubmit`, `cancel`, `record_action` and
`continue-without-approval`, so every vote re-serialises the entire history.

**The fix.** Paginate `audit_logs` behind its own sub-resource, cap the embedded
actions to the current attempt, and return the full history only when asked for.

---

## 11. Nothing stops a tenant flattening its own approval ladder

**Medium.**

A tenant-scoped template overrides the platform's for its `(document_type,
code)` (`services/resolution.py:144-160`), and `publish` accepts any
`document_type` string with no allowlist and no handler check
(`serializers.py:143`).

`validate_stages` requires at least one stage (`serializers.py:167-168`), but a
`BRANCH` stage is a stage. So a template whose only stage is a routing-only
`BRANCH` node - or whose approval stages all carry an `inclusion_condition` the
document fails - routes straight to APPROVED. And the read-side gate agrees:

```python
# services/resolution.py:212-219
if nxt.retired_at is not None or nxt.kind == StageKind.BRANCH:
    cursor = nxt; continue
if nxt.kind == StageKind.APPROVAL and nxt.inclusion_condition:
    matches, _ = evaluate_condition(...)
    if not matches: cursor = nxt; continue
```

so `template_requires_approval` answers False, and `vs_finance.approvals`
lets the document take the direct-post path with no workflow at all.

**What actually happens.** A tenant admin holding `workflow.template.manage`
publishes, for `payments.payout_batch`, a template whose one stage is
`{"code": "noop", "label": "Noop", "kind": "BRANCH"}`. From that moment their
payouts post without any approval, and the platform's ladder no longer applies
to them. The platform can see it - `adoption` reports who has their own version
- but nothing prevents it and nothing alerts anybody.

The bar today is the key being seeded to platform roles only. It is
`PermissionScope.TENANT`, so a school admin who can mint a role can put it in
one - the same escalation shape recorded in `error/audit/audit_code_issues.md`.

**The fix.** A platform floor: a list of document types whose ladder a tenant may
adjust but not remove, checked at publish - "this document type requires at least
one unconditional APPROVAL stage". `payments.payout_batch` and the finance
document types are the obvious members. Belt and braces: have
`template_requires_approval` treat a floor-protected type with no approval stage
as True rather than False, so a template that slipped through still gates.

---

## 12. Template changes are not audited, and the notifier is handed `school=`

**Medium. Two gaps in the record.**

### 12a. Nobody can say who changed an approval path

`WorkflowAuditLog` is keyed on an **instance** (`models.py:777`), so there is
nowhere in this module for a template edit to be recorded, and
`publish_template` writes nothing to `vs_audit` either
(`services/templates.py:156-275`).

A stage's `advance_rule` can go from `UNANIMOUS` to `ANY`, `skip_if_no_approvers`
from `False` to `True`, or an entire approval stage can be dropped from the
payload and soft-retired - and the only trace is that the row now says something
different, with `updated_at` and `created_by` overwritten. There is no before, no
diff and no actor history.

For a module whose subject is controlled approval, that is the one record most
worth having: a document approved under a ladder that was quietly loosened last
Tuesday looks identical to one approved under the original.

**The fix.** Emit a `vs_audit` event from `publish_template` and from
`use_platform_version`, with the stage configuration before and after -
`services/comparison.py` already computes exactly that diff, for the platform
oversight screen.

### 12b. The notifier is told the school, not the tenant

```python
# tasks.py:35-42
NotificationService.send(
    event_key=event_key, recipients=recipients,
    school=instance.school, ...)
```

`WorkflowInstance.school` is `tenant.school_profile` (`models.py:588-590`), one
of five school-vocabulary properties in this engine app
(`models.py:117-124, 260-268, 588-596, 748-754`, plus
`WorkflowInstanceQuerySet.for_school` at `models.py:506-507`). `CLAUDE.md` is
explicit that outside `apps/schools/` the word is **tenant**.

Here it is load-bearing rather than cosmetic. The dispatcher does
`tenant = tenant or getattr(school, "tenant", None)`
(`vs_notifications/services/dispatch.py:118`), so for a school tenant it recovers
the right tenant; for a platform or a health tenant `school_profile` does not
exist, `school` is None, and the tenant is decided by the fallback to the first
recipient carrying one. That fallback happens to land correctly, because
`resolve_approvers` contains every recipient to the instance's tenant - so the
row is stamped right by accident, not by intent, and the accident holds only as
long as containment does.

**The fix.** `tenant=instance.tenant`. One argument, and it removes the
dependency on a fallback. Then drop the five `school` properties, or move them
behind the school FAL where school vocabulary belongs.

---

## 13. Four response shapes, none of them the platform envelope

**Low, and it is an API contract.**

`vs_workflow/views.py` imports neither `core.mixins` nor `core.response`. It uses
DRF's `mixins.ListModelMixin`, `mixins.RetrieveModelMixin` and `ModelViewSet`
(`views.py:8,13`), so success responses are bare.

| Endpoint | Shape |
|---|---|
| `GET templates/`, `GET instances/` | `{success, message, pagination, data}` - from `XVSPagination` |
| `GET instances/<id>/`, every action, every group/override/delegation write | a bare serialized object |
| `GET dashboard/pending/` | `{"results": [...], "count": N}` |
| `GET dashboard/submitted/`, `dashboard/team-load/`, `preview-approvers` | a bare list or dict |
| Every hand-built error in the module | `{success, message, error: {code, detail}}` |

So the error shape is the platform's and the success shape is not, within the
same view. A frontend written against `response.data.data` for tickets, finance
and exports has to special-case workflow, and a shared client helper cannot be
used.

**The fix.** Swap the DRF mixins for `core.mixins`' equivalents, which exist for
exactly this and are already used by `vs_tickets` and `vs_todo`, and wrap the
four `APIView` responses in `success_response`.

---

## 14. A stage about to be skipped for want of approvers is activated first

**Low.**

```python
# services/routing.py:325-332
_activate_stage(instance, next_stage, current_attempt)
eligible = approvers_service.resolve_approvers(next_stage, instance)
if not eligible:
    if next_stage.skip_if_no_approvers:
        _skip_stage(instance, next_stage, current_attempt,
                    AuditEventType.STAGE_SKIPPED_NO_APPROVER, "zero_eligible_approvers")
```

`_activate_stage` has already set the stage instance ACTIVE, pointed
`instance.current_stage` at it, written a `STAGE_ACTIVATED` audit row and called
`notify` (which returns early on an empty recipient list, so at least nobody is
told). `_skip_stage` then flips the same row to SKIPPED.

The audit log therefore reads "stage activated, eligible_count 0" immediately
followed by "stage skipped, zero_eligible_approvers" for a stage nobody ever saw,
and `instance.current_stage` briefly points at it.

Harmless in a single transaction; misleading to anybody reading the log, and it
is the same duplicate resolution as §9 seen from the other side.

**The fix.** Resolve first, decide, then activate - which is also §9's fix.

---

## 15. `all` and `any` conditions do not short-circuit

**Low.**

```python
# conditions/evaluator.py:62-77
if "all" in condition:
    for child in children:
        r, t = evaluate_condition(child, document)
        child_traces.append(t)
        if not r: result = False        # keeps going
if "any" in condition:
    for child in children:
        ...
        if r: result = True             # keeps going
```

Every child is evaluated even once the answer is settled. For `op` children that
is a few attribute reads. For `fn` children - registered predicates that may
query the database (`conditions/registry.py`) - it is real work, and the
evaluator runs on every route transition of every instance.

There is a defensible reason to keep going: the trace shows every child, which is
what makes route decisions explicable. If that is the intent it should be said,
because the code reads like an oversight.

**The fix.** Either short-circuit and record the remaining children as
`{"skipped": true}`, or add a sentence saying the completeness of the trace is
worth the evaluations.

---

## 16. Smaller defects and dead code

**Low, individually.**

1. **`_filter_by_branch` (`views.py:54-66`) is dead.** Its logic was inlined into
   `WorkflowTemplateViewSet.get_queryset` (209-213) and the helper is called from
   nowhere.
2. **`NOTIF_EVENT_KEYS` and the notification registry disagree in both
   directions.** `workflow.stage_approved`, `workflow.stage_rejected`,
   `workflow.withdrawn` and `workflow.cancelled` are declared in
   `constants.py:133-142` and absent from the registry entirely;
   `workflow.escalated` is in the registry
   (`vs_notifications/constants.py:359-367`) and not in `NOTIF_EVENT_KEYS`, so
   `dispatch_notification` would refuse it.
3. **Nobody is told about a withdrawal or a cancellation.** The two keys exist
   (item 2) and no code path emits them, so an approver whose queued document was
   withdrawn or cancelled discovers it by opening the item.
4. **`WorkflowRoutePath` has no unique constraint on `(from_stage, order)`**
   (`models.py:487-489`), so two edges can claim the same evaluation position and
   the tie is decided by insertion order.
5. **`MAX_HOPS = 50` is written three times** - `services/routing.py:286`,
   `services/routing.py:180`, `services/resolution.py:106` - with a comment in
   two of them saying it mirrors the third.
6. **The instance list filters are unvalidated strings.**
   `?requested_by=abc` reaches the ORM as a pk filter (`views.py:499`); the
   others are `document_type`, `status` and `template_code` (`496-502`).
7. **`preview-approvers` accepts an arbitrary requester id** and resolves
   approvers in *that user's* tenant (`views.py:230-234`). Gated on
   `workflow.template.view`, platform-only today, so the cross-tenant read is
   deliberate - but a school role granted that key would gain a people lookup
   into every tenant.
8. **Delegations have no permission key and no bound.**
   `ApprovalDelegationViewSet` is `IsAuthenticatedAndActive` only
   (`views.py:874`); nothing checks that the delegator holds any approval
   authority, and nothing caps `ends_at`, so a delegation can run for a decade.
9. **A group deactivated under a live stage resolves to nobody, silently.**
   Combined with `skip_if_no_approvers=True`, deactivating a group makes every
   stage naming it auto-approve. There is no coverage report for groups
   equivalent to `workflow_role_coverage` for roles.
10. **The export dataset uses the tenant-aware manager** where every sibling
    engine dataset uses `all_objects` (`export_datasets.py:26-29` versus
    `vs_tickets/export_datasets.py:30-32`). It works because a Celery worker has
    no ambient tenant; under eager mode with an ambient tenant set it would
    return nothing.
11. **`requested_by` on the export dataset is an email address and is not marked
    `sensitive`** (`export_datasets.py:56-57`), unlike the equivalent column on
    the support-ticket dataset.
12. **`_translate_instances` claims a completeness it does not have.** Its
    comment calls it "the rare happy case: every filter the screen offers has an
    exact counterpart", while the screen offers four filters and the binding
    handles two - `requested_by` and `template_code` are neither mapped nor
    reported as unmapped (`export_datasets.py:74-99`).
13. **`WorkflowStageAction.WITHDRAWN` is declared and never written**
    (`constants.py:35`): withdrawal is an instance transition, not a stage vote.
14. **`dashboard/submitted/` and `dashboard/team-load/` are unpaginated**
    (`views.py:658-698`).
15. **`parked_object_ids` casts `document_object_id` to an integer**
    (`services/parking.py:409-424`), hard-coding an assumption the generic FK does
    not make - `document_object_id` is a `CharField(max_length=64)`.
16. **`submit_for_approval` swallows every exception from
    `get_document_summary`** (`services/submission.py:55-61`). Correct - a display
    snapshot must not fail an approval - but a handler bug there is invisible,
    with no log line.

---

## What the test suite does not know

253 tests, all green, and the coverage of resolution and tenant scoping is
genuinely strong: `resolve_approvers` is exercised across all four sources with
containment, delegation and override cases, and `test_tenant_scoping.py` walks
every read surface from a second tenant. What that leaves:

1. **No test posts to `POST /v1/workflow/instances/`.** Submission is tested at
   the service layer only, which is exactly where §1's missing check would have
   been caught.
2. **No test touches `services/release.py`** - §2, and the reason it is graded
   High on its own.
3. **No test asserts the content of an audit `context`**, so §3's traces are
   invisible.
4. **No test publishes a `notification_events` dict**, valid or invalid - §4.
5. **No test combines delegation with a `UNANIMOUS` threshold** - §5.
6. **No test asserts what a reversal notifies** - §6 - or what happens to the
   document afterwards - §7.
7. **No test asserts a response shape**, so §13 is invisible.
8. **No test publishes or follows routes.** Every routing test uses linear stage
   order, so `_pick_next_stage`'s route branch, the `ROUTE_EVALUATED` audit row
   and the "no route matched" refusal are all unexercised.
