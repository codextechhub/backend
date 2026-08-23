# workflow_notifications_audit

What the engine tells people, and what it writes down. The ten declared event
keys and the four the engine actually emits, the template opt-in that decides
whether any of them fire, the append-only audit log, and the Export Centre
dataset. The transitions that trigger all of this are
`workflow_engine_routing` and `workflow_actions_lifecycle`.

No endpoints of its own: the audit log is read through the instance detail
payload, and the dataset through the Export Centre.

---

## 1. What it is (and what it is NOT)

- **Notifications are queued after commit and never block a transition.**
  `notify` (`services/routing.py:40-70`) wraps `dispatch_notification.delay` in
  `transaction.on_commit`, so a rolled-back transition never notifies, and the
  Celery task swallows its own failures (`tasks.py:45-46`).
- **The engine emits four of the ten keys it declares.**
  `NOTIF_WIRED_EVENT_KEYS` (`constants.py:153-158`) is honest about which:
  `stage_activated` to the activated stage's approvers, and `returned`,
  `rejected` and `final_approved` to the requester. The other six are reserved.
- **Notification copy is built in one place on purpose.** `notify` is public
  precisely so the parking repair can use it: when a role is finally staffed the
  repair fills an already-ACTIVE stage's snapshot, and without a notification the
  newly eligible approver would only learn of the waiting document by opening the
  queue on spec (`services/routing.py:34-39`).
- **The template decides whether anything fires at all.** `notification_events`
  is an opt-in dict on the template, and an untouched `{}` means "every wired
  event" while a configured dict is exact intent (`tasks.py:21-26`).
- **The audit log is the engine's own, not `vs_audit`.** `WorkflowAuditLog` rows
  are keyed on an instance, written inside the transition's transaction, and
  never updated or deleted. Nothing in this module writes to the platform audit
  stream.
- **The audit log is a debugging record as much as a compliance one.** It carries
  full condition traces, per-route evaluations and dynamic-rule decisions -
  which is what makes "why did this go to the Bursar" answerable, and what makes
  its `context` field the exposure recorded in §8.
- **There is no notification when a reversal reopens a stage**, and no
  escalation or reminder of any kind.

## 2. The ten keys, and where each one lives

| Key (`constants.py:133-142`) | In the registry? | Emitted by the engine? |
|---|---|---|
| `workflow.stage_activated` | active | **yes** - `_activate_stage`, and the parking repair |
| `workflow.returned` | active | **yes** - `_return_to_requester` |
| `workflow.rejected` | active | **yes** - `_terminate_rejected` |
| `workflow.final_approved` | active | **yes** - `_terminate_approved` |
| `workflow.submitted` | registered `is_active: False` | no - superseded by the first stage activation |
| `workflow.approved` | registered `is_active: False` | no - superseded by the next stage activation |
| `workflow.stage_approved` | **absent** | no |
| `workflow.stage_rejected` | **absent** | no |
| `workflow.withdrawn` | **absent** | no |
| `workflow.cancelled` | **absent** | no |

And one the registry carries that `vs_workflow` does not declare:
`workflow.escalated` (`vs_notifications/constants.py:359-367`), registered
inactive because the engine has no escalation emitter. `dispatch_notification`
would refuse it anyway - it is not in `NOTIF_EVENT_KEYS`.

So the two lists disagree in both directions: four keys the engine declares do
not exist in the registry, and one registry key the engine cannot emit
(`workflow_code_issues.md` §16).

**Nobody is told about a withdrawal or a cancellation.** An approver with a
document in their queue that the requester withdrew, or an admin cancelled,
finds out by opening it.

## 3. Recipients and context

| Event | Recipients | Extra context |
|---|---|---|
| `stage_activated` | every distinct user in the freshly written snapshot | `stage_name`, `stage_label`, `submitter_name` |
| `returned` | the requester | `returned_by_name`, `return_comment` |
| `rejected` | the requester | `rejected_by_name`, `rejection_reason` |
| `final_approved` | the requester | `final_approver_name` |

`notify` adds three keys to every message before dispatch
(`services/routing.py:46-66`):

- **`document_title`** - `"{subtitle}: {title}"` from the handler's
  `document_summary`, falling back to
  `"{document_type} #{first 8 of the object id}"`. Internal tokens such as
  `PLATFORM_USER_CREATION` are implementation details, not useful notification
  copy, which is why the domain snapshot is preferred.
- **`document_type`** - the summary's subtitle, else the document type with
  underscores replaced and title-cased.
- **`document_type_title`** - the same, capitalised word by word but leaving
  already-uppercase words alone.

`final_approver_name` (`routing.py:363-385`) reads back the most recent live
`APPROVED` action to name the vote that completed the workflow, and answers
**"the system"** when there is none - a fully automatic ladder has no action row,
and rendering an empty string into "Approved by " is worse than saying so.

## 4. The template opt-in (`tasks.py:21-26`)

```python
events = instance.template.notification_events or {}
if events and not events.get(event_key, False):
    return
```

Three cases:

| `notification_events` | Behaviour |
|---|---|
| `{}` (never configured) | every wired event fires |
| `{"workflow.returned": true}` | **only** `returned` fires; the other three are off |
| `{"workflow.stage_activate": true}` (typo) | **nothing** fires, ever |

The third case is the defect: `WorkflowTemplatePublishSerializer` types the field
as `DictField(child=BooleanField())` with no key validation
(`serializers.py:147-148`), and the dispatcher treats any non-empty dict as exact
intent. One mistyped key silences a template's whole notification surface,
including the parking repair's, and nothing anywhere reports it
(`workflow_code_issues.md` §4).

## 5. Dispatch (`tasks.py:10-46`)

```text
dispatch_notification(instance_id, event_key, recipient_user_ids, context)
  refuse an event_key outside NOTIF_EVENT_KEYS      ← callers cannot emit arbitrary types
  load the instance (gone → return)
  apply the template opt-in
  load the recipients (none → return)
  NotificationService.send(event_key, recipients,
                           school=instance.school,
                           metadata={"workflow_instance_id": …},
                           context={workflow_instance_id, document_type,
                                    document_id, **context})
  ImportError → log and skip;  Exception → log and continue
```

Two details worth knowing:

- **`metadata["workflow_instance_id"]` is what makes the notification
  clickable.** `vs_notifications`'s router turns a `workflow.` key plus that id
  into `/workflow/approvals/<id>` for the two approval events and
  `/workflow/my-submissions/<id>` for everything else
  (`vs_notifications/services/routing.py:30-33`), and `acknowledge-route` reads
  it back to mark exactly that instance's rows read. This module is one of only
  three whose in-app notifications have a real destination.
- **The tenant is passed as `school=instance.school`**, not as `tenant=`
  (`tasks.py:38`). `WorkflowInstance.school` is `tenant.school_profile`
  (`models.py:588-590`), so for a school tenant the dispatcher recovers the right
  tenant through `school.tenant`; for a platform or health tenant it is `None`
  and the tenant is decided by the dispatcher's fallback to the first recipient
  carrying one. Every recipient is contained to the instance's tenant, so the
  fallback lands correctly - but by accident rather than by intent
  (`workflow_code_issues.md` §12).

## 6. The audit log

`WorkflowAuditLog` (`models.py:764`), written through `audit_service.write`
(`services/audit.py:13`), which resolves the audit identity through
`vs_tenants.context` so an impersonated actor is recorded with the real one
behind them.

Fourteen event types (`constants.py:55-70`), covering three families:

| Family | Events |
|---|---|
| Instance lifecycle | `INSTANCE_SUBMITTED`, `_WITHDRAWN`, `_CANCELLED`, `_APPROVED`, `_REJECTED`, `_RETURNED`, `_RESUBMITTED` |
| Stage lifecycle | `STAGE_ACTIVATED`, `STAGE_APPROVED`, `STAGE_REJECTED`, `STAGE_SKIPPED_NO_APPROVER`, `STAGE_SKIPPED_CONDITION` |
| Decisions | `APPROVER_ACTED`, `ACTION_REVERSED`, `ROUTE_EVALUATED` |

Each skip passes a **distinct** reason event so the log is queryable by skip
cause without parsing free text (`services/routing.py:258-263`).

Two events are reused with a marker rather than duplicated:
`STAGE_ACTIVATED` carries `{"warning": "stage_active_with_no_approvers"}` when a
stage parks, and `{"repair": "approver_snapshot_refilled"}` when the parking
repair fills it. `APPROVER_ACTED` carries
`{"action": "RELEASED_NO_APPROVER", "override": true, …}` for a release.

The docstring on `write` carries one rule worth repeating: **never call it inside
a rollback-only savepoint**, because the audit entry disappears with the
savepoint.

### What `context` holds, and who sees it

`context` is a raw `JSONField`, serialized raw by
`WorkflowAuditLogReadSerializer` (`serializers.py:255-260`), and every audit row
of an instance is embedded in its detail payload with no limit
(`serializers.py:277-286`).

That payload therefore includes the full `ROUTE_EVALUATED` traces, and a trace
carries `_safe(left)` - whatever the condition's dotted `field` path resolved to
on the business document. Since `_extract_field` is an unbounded `getattr` walk
(`conditions/evaluator.py:15-24`), a template author can point a condition at any
attribute reachable from the document and have its value stringified into an
audit row that every holder of `workflow.instance.view` can read
(`workflow_code_issues.md` §3).

## 7. The Export Centre

Registered from `AppConfig.ready()` (`apps.py:14-23`), never from `vs_exports`,
so the engine never imports the export app.

### Dataset `workflow.approvals` (`export_datasets.py:33-68`)

- **Scope** `DatasetScope.TENANT`; base
  `WorkflowInstance.objects.filter(tenant=scope.tenant)`.
- **Permission** `workflow.instance.view`. Row cap 200,000.
- **Columns**: `instance_id` (locked), `document_type`, `document_reference`,
  `template_name`, `status`, `current_stage`, `requested_by`
  (`requested_by__email`), `submitted_at`, `completed_at`, `created_at`.
- **Filters**: a **required** `submitted_at` date range, plus `status` and
  `document_type` as text.

It is the dataset that answers "what is sitting unapproved, and with whom"
without anybody opening the workflow console. Two notes: `requested_by` exposes
an email address and is **not** marked `sensitive`, unlike the equivalent column
on the support-ticket dataset; and the base queryset uses the tenant-aware
`objects` manager where every sibling engine dataset uses `all_objects`
(`workflow_code_issues.md` §16).

### Screen binding (`export_datasets.py:88-99`)

Binds the instance list so a filtered table becomes a one-click export.
`_translate_instances` maps `status` and `document_type` - and the comment above
it calls this "the rare happy case: every filter the screen offers has an exact
counterpart on the dataset". The screen actually offers four filters
(`views.py:496-502`); `requested_by` and `template_code` are neither handled nor
reported as unmapped (`workflow_code_issues.md` §16).

## 8. Gotchas / known limitations

Full evidence in **`error/workflow/workflow_code_issues.md`**.

- **One typo in `notification_events` silences a template's whole notification
  surface**, including the parking repair's, with no error and no report
  (`workflow_code_issues.md` §4).
- **A condition's field path is an unbounded attribute walk, and what it finds is
  copied into the audit trace** that the instance detail returns raw
  (`workflow_code_issues.md` §3).
- **The instance detail embeds every audit row, unpaginated**, alongside every
  stage instance, action and approver snapshot
  (`workflow_code_issues.md` §10).
- **Reversing an action notifies nobody**, although the stage is once again
  awaiting a decision (`workflow_code_issues.md` §6).
- **Nobody is told about a withdrawal or a cancellation**, and the four keys that
  would carry that news are declared in `vs_workflow` and absent from the
  notification registry (`workflow_code_issues.md` §16).
- **The dispatcher is handed `school=`, not `tenant=`**, and lands on the right
  tenant through a recipient fallback (`workflow_code_issues.md` §12).
- **Template changes are not audited at all** - `WorkflowAuditLog` is keyed on an
  instance, so there is nowhere for a template edit to be recorded, and nothing
  writes one to `vs_audit` either (`workflow_code_issues.md` §12).
- **`workflow.escalated` is registered and unreachable**, and
  `NOTIF_EVENT_KEYS` and the registry disagree in both directions.
- **Justified by design:** notifications are queued on commit and their failures
  are swallowed - the workflow state is already committed and a notification
  must never roll it back.
- **Justified by design:** `dispatch_notification` refuses any key outside
  `NOTIF_EVENT_KEYS`, so a caller cannot emit an arbitrary notification type.
- **Justified by design:** each skip cause is its own audit event rather than a
  free-text reason.
- **Justified by design:** `notify` is public so the parking repair sends the
  same message a real activation does.

## 9. Permissions & tenant isolation

| Surface | Gate |
|---|---|
| Receiving a notification | being a resolved recipient - the snapshot, or the requester |
| Reading the audit log | `workflow.instance.view`, through the instance detail |
| Exporting `workflow.approvals` | `workflow.instance.view` in the Export Centre |

The audit log has no permission of its own and no endpoint of its own: it is
readable exactly when the instance is, which means in practice by platform roles
only, since `workflow.instance.view` is seeded nowhere else
(`workflow_code_issues.md` §8).

Tenant isolation is inherited: recipients are always contained to the instance's
tenant by `resolve_approvers`, or are the requester; the audit rows travel with
the instance; the dataset filters `tenant=scope.tenant`.

## 10. Code map

| File | Responsibility |
|---|---|
| `constants.py:133-158` | The ten keys and the four wired ones |
| `services/routing.py:40-70` | `notify` - copy, document title, on-commit queueing |
| `tasks.py` | `dispatch_notification` - the key guard, the template opt-in, the send |
| `services/audit.py` | `write` - the append-only entry point |
| `models.py:764-796` | `WorkflowAuditLog` |
| `constants.py:55-70` | `AuditEventType` |
| `serializers.py:255-260` | `WorkflowAuditLogReadSerializer` |
| `serializers.py:277-290` | `WorkflowInstanceDetailSerializer` - where the log is embedded |
| `export_datasets.py:26-68` | The `workflow.approvals` dataset |
| `export_datasets.py:78-99` | `_translate_instances` and the screen binding |
| `vs_notifications/constants.py:303-367` | The seven registry entries |
| `vs_notifications/services/routing.py:30-33` | Where a workflow notification clicks through to |

## 11. Test coverage & gaps

- `WorkflowNotificationTests` (`tests/test_notifications.py:46-212`) - stage
  activation notifies the approvers; returned, terminal rejection and final
  approval each notify the requester; the final-approval message names the human
  approver; and a template opt-out suppresses a notification.
- `ParkedRepairNotificationTests` (`214-320`) - the repair notifies, a repair
  that staffs nobody notifies nobody, and a second pass does not re-notify.
- Every audit assertion is incidental: `test_approved_writes_audit_log`
  (`tests/test_actions.py:122`) and `test_withdraw_writes_audit_log` (`258`).

What it does not cover:

1. **A malformed `notification_events` key** - the typo case in §4, which is the
   one that turns everything off.
2. **The audit `context` payload.** Nothing asserts what a `ROUTE_EVALUATED` row
   contains, which is why §8's second item is invisible.
3. **The tenant a workflow notification is stamped with** - the `school=`
   argument in §5 is asserted nowhere.
4. **`document_title` / `document_type_title`** - the copy-building in `notify`,
   including the fallback when the handler supplies no summary.
5. **The four unregistered keys.** Nothing asserts that `NOTIF_EVENT_KEYS` and
   the notification registry agree, in either direction.
6. **The `workflow.approvals` dataset** - no test runs it, and none checks that
   its rows match what the API would show the same caller.
7. **`_translate_instances`** and the two screen filters it silently drops.
