# todo_review_requests

The module's one outward integration: when a person ticks off their own task,
their reviewer is told. This slice owns the grace window, the Celery task and its
three guards, how the reviewer is chosen, and the notification event that carries
it. The toggle that starts it is `todo_tasks` §6; the tree that finds the
reviewer is `todo_hierarchy_scoping` §4.

---

## 1. What it is (and what it is NOT)

- **It is a review *request*, not a review.** Nothing in the data model records
  that a review happened, was accepted or was rejected. The reviewer is emailed
  and given an in-app row; what they then do leaves no trace on the task.
- **It fires only on self-completion.** `set_done` queues it when the actor is
  the assignee and the task was not already done
  (`services/tasks.py:96-98`). A manager ticking a report's task tells nobody -
  correctly, since the manager is the person who would have been told.
- **It is deliberately late.** The Celery task is queued with a countdown and
  re-reads the row at send time (`tasks.py:57-61`), so the delay is not a
  performance trick: it is an undo window, and undoing inside it cancels the
  email rather than sending a correction.
- **The window is five seconds, and zero wherever Celery is eager.**
  `REVIEW_GRACE_SECONDS = 5` (`constants.py:46`), and eager mode ignores
  `countdown` altogether (§8).
- **Being *given* a task tells nobody.** `EVENT_TASK_ASSIGNED`
  (`constants.py:41`) is declared and never used; there is no `todo.task_assigned`
  in the notification registry and no code path that fires one (§8).
- **A task going overdue tells nobody either.** There is no sweep, no periodic
  task, and no deadline notification anywhere in the module.
- **The notification is stamped with the platform tenant**, which is the right
  answer, and it gets there by falling through the dispatcher's fallback rather
  than by asking for it (§5).

## 2. Domain model

None. This slice owns no model and writes no row of its own. It reads `Task`
(`todo_tasks` §2) and produces `vs_notifications.Notification` rows through the
engine.

Two constants define it:

```python
EVENT_TASK_COMPLETED = "todo.task_completed"   # constants.py:42
REVIEW_GRACE_SECONDS = 5                        # constants.py:46
```

and one registry entry, in `vs_notifications`:

| Field | Value |
|---|---|
| `key` | `todo.task_completed` (`vs_notifications/constants.py:634-641`) |
| `label` | "Task completed - review requested" |
| `source_module` | `vs_todo` |
| `supported_channels` | `IN_APP`, `EMAIL` |
| `default_enabled` | `True` |
| `is_transactional` | not set, so the event is configurable |

Templates for both channels are seeded at
`vs_notifications/services/seed.py:1207-1230`. The in-app body ends with
"Kindly review it under Tasks → My Team", in words, because the notification has
no click destination (§8).

## 3. Entry point

There is no endpoint. The flow starts inside `set_done`:

```python
# services/tasks.py:96-118
is_self_completion = (
    done and not was_done and actor is not None and actor.pk == task.assignee_id
)
if is_self_completion:
    stamp = task.completed_at.isoformat() if task.completed_at else ""
    task_pk, actor_pk, title = task.pk, actor.pk, task.title
    transaction.on_commit(
        lambda: send_completion_review_request.apply_async(
            kwargs={
                "task_id": task_pk,
                "completed_at": stamp,
                "_job_owner_id": str(actor_pk),
                "_job_label": f"Review request: {title}"[:255],
                "_job_kind": "email",
            },
            countdown=REVIEW_GRACE_SECONDS,
        )
    )
```

Three details worth noticing:

- **`on_commit`**, so nothing is queued if the toggle's transaction rolls back.
- **Scalars are captured before the lambda**, not the `task` object, so the
  closure cannot hold a stale instance or a live DB connection.
- **The three `_job_*` kwargs** are queue-row attribution consumed by the
  platform's job monitor (`docs/console/console_task_monitor.md`), which is what
  makes a review request visible on the View Queues page with an owner and a
  human label.

The only other caller of `set_done` is `TaskViewSet.toggle` (`views.py:141`).
`PATCH {"is_done": true}` bypasses `set_done` entirely and therefore this whole
slice - see `todo_code_issues.md` §1.

## 4. Lifecycle

```text
POST /tasks/<pk>/toggle/ {"done": true}   (by the assignee)
        │
        ├─ is_done = True, completed_at = now        [committed]
        │
        └─ on_commit ──► apply_async(countdown=5)
                                │
                      ── 5 seconds ──
                                │
                                ▼
            send_completion_review_request(task_id, completed_at)
                                │
             ┌──────────────────┼───────────────────┬────────────────────┐
             ▼                  ▼                   ▼                    ▼
      task deleted?      not done / no stamp?   stamp changed?     reviewer is None
      skip:              skip:                  skip:              or is the assignee?
      task-deleted       reopened-within-grace  superseded-…       skip: no-reviewer
                                │
                                ▼  (all guards passed)
                   send_notification("todo.task_completed", [reviewer])
                                │
                    in-app row (SENT) + email row (PENDING → delivery task)
```

The guards are re-checks, not locks: the task re-reads the row at send time
rather than trusting what was true when it was queued (`tasks.py:54-61`).

## 5. Derivations

- **The reviewer** is `task.assigned_by or TodoHierarchy.direct_manager(assignee)`
  (`tasks.py:63`). For a handed-down task it is the manager who handed it down;
  for a self-set task it is the assignee's line manager. `direct_manager` walks
  **past vacant seats** (`services/hierarchy.py:133-151`), specifically so a
  missing middle manager does not swallow the escalation - the opposite rule from
  the breadcrumb, and deliberately so.
- **A reviewer who is the assignee is refused**, and so is `None`
  (`tasks.py:64-65`): you are not asked to review your own work, and a person at
  the top of the tree with a self-set task simply produces no request.
- **The undo check is the stamp, not the flag.** Guard two catches a reopen
  (`not task.is_done or task.completed_at is None`); guard three catches an
  undo-and-redo, because the second completion re-stamps `completed_at` and
  queues a *fresher* request, which makes the older one's captured stamp stale
  (`tasks.py:59-61`). Both queued jobs run; exactly one sends.
- **Re-completing without reopening does not re-stamp.** `mark_done` is a no-op
  when the task is already done (`models.py:115-119`), so a duplicate toggle
  cannot produce a second request.
- **The context is fully rendered at send time** (`tasks.py:68-81`): the
  reviewer's first name, the assignee's name, and the task's title, description,
  metric, target, priority display, deadline, completion timestamp and
  department. `_fmt_dt` localises the completion time (`tasks.py:27-28`), and
  metric/target fall back to `-` rather than an empty line in the email.
- **The tenant is not passed, and lands correctly anyway.** The call is
  `send_notification(..., school=None)` (`tasks.py:83-88`) with no `tenant=`, so
  the dispatcher falls back to the first recipient carrying a tenant
  (`vs_notifications/services/dispatch.py:118-122`) - the reviewer, who is CX
  staff on the platform tenant - and to the `codex` tenant if even that is
  absent. Either way the row is stamped `codex`, which is the tenant the reviewer
  asserts, so the in-app feed shows it. This module is therefore **not** exposed
  to the cross-tenant feed defect that bites `vs_tickets`
  (`error/notifications/notification_code_issues.md` §1), but it escapes by
  fallback rather than by intent.
- **An unseeded event is a skip, not a crash.** `UnknownEventTypeError` is
  caught, logged at ERROR and returned as `{"skipped": "event-not-seeded"}`
  (`tasks.py:89-95`), so a missing seed degrades to silence.
- **Every exit returns a dict**, and the dicts are the contract the job monitor
  displays: `task-deleted`, `reopened-within-grace`,
  `superseded-by-newer-completion`, `no-reviewer`, `event-not-seeded`, or
  `{"reviewer": <pk>, "notifications": [ids]}`.

## 6. What it writes

| Target | Written |
|---|---|
| `Task` | nothing - the task is only read |
| `vs_notifications.Notification` | one in-app row (SENT) and one email row (PENDING, then handed to the delivery task) for the reviewer |
| Job queue | one row per queued request, attributed to the completing user with the label `Review request: <title>` |
| `vs_audit` | nothing |
| Log | INFO on dispatch, ERROR when the event is not seeded |

Nothing records on the task that a review was requested, so the same completion
toggled off and on twice produces two requests and no history of either.

## 7. Worked example

Tobi finishes the task Chidi gave him:

```text
POST /v1/todo/tasks/208/toggle/?tenant=codex   {"done": true}
```

The response returns immediately with `status: "COMPLETED"` and
`completed_at: "2026-08-21T11:40:07Z"`. Five seconds later Chidi receives:

```text
Subject: Review requested: "Close the Q3 renewal list" marked as done

Hello Chidi,

Tobi Member has marked the task below as completed and it is
awaiting your review.

  Task       : Close the Q3 renewal list
  Details    : All 14 accounts.
  Metric     : Renewals closed
  Target     : 14
  Priority   : High
  Deadline   : 30 Sep 2026
  Completed  : 21 Aug 2026, 12:40
  Department : Sales

Review it on the console under Tasks → My Team → Tobi.
```

and an in-app row reading "Tobi Member marked "Close the Q3 renewal list" as
done. Kindly review it under Tasks → My Team." That row's `action_url` is `""`,
which is why the body has to say where to go in words (§8).

Now the undo. If Tobi realises within the window:

```text
POST /v1/todo/tasks/208/toggle/?tenant=codex   {"done": false}
  → the queued job fires, finds is_done False, returns {"skipped": "reopened-within-grace"}
```

Chidi is never told. If instead he undoes and re-completes:

```text
{"done": false} then {"done": true}
  → job A: completed_at no longer matches its stamp → "superseded-by-newer-completion"
  → job B: sends
```

Exactly one email, for the second completion. That second case is pinned by
`test_skips_when_completion_superseded` (`tests.py:235-243`).

And what makes all of this theoretical on staging: `CELERY_TASK_ALWAYS_EAGER`
defaults to `True` there (`apps/settings/staging.py:45`), and eager mode runs
`apply_async` inline and ignores `countdown` entirely. Tobi's email is on its way
before the toggle's response reaches his browser.

## 8. Gotchas / known limitations

Full evidence in **`error/todo/todo_code_issues.md`**.

- **`PATCH {"is_done": true}` completes a task without ever calling `set_done`**,
  so no review request is queued and `completed_at` is not stamped. The whole
  mechanism in this file can be walked around with one field
  (`todo_code_issues.md` §1).
- **The undo window is five seconds** (`constants.py:46`) - not long enough for
  a person to notice a mis-click, let alone act on it - **and zero wherever
  Celery is eager**, which is local, CI, test and staging by default
  (`todo_code_issues.md` §4).
- **Staging propagates eager failures into the request.**
  `CELERY_TASK_EAGER_PROPAGATES` follows `ALWAYS_EAGER`
  (`apps/settings/staging.py:46`), and the task catches only
  `UnknownEventTypeError`. Anything else the notification engine raises surfaces
  out of the `on_commit` callback as a `500` on a toggle that has already been
  saved (`todo_code_issues.md` §4).
- **Being handed a task notifies nobody.** `EVENT_TASK_ASSIGNED` is declared
  (`constants.py:41`), absent from the registry, and fired from nowhere - so the
  one moment the design is about, a manager handing accountability down, is
  silent (`todo_code_issues.md` §3).
- **The in-app row has no click destination.**
  `vs_notifications`'s router has no `todo.` prefix and no ticket-style special
  case (`vs_notifications/services/routing.py:13-41`), so `action_url` is `""`
  for every review request. This is one instance of
  `error/notifications/notification_code_issues.md` §4
  (`todo_code_issues.md` §13).
- **The event is not transactional**, so a platform admin can switch review
  requests off for the whole company through notification settings. Reasonable
  for an internal tool, but worth knowing it is a setting and not a guarantee.
- **`school=None` is passed explicitly** by an engine app
  (`tasks.py:87`) where `tenant=` is the current parameter name. It works
  through a fallback rather than by intent (`todo_code_issues.md` §16 item 3).
- **Nothing records that a review was requested**, so there is no way to ask
  "which completions are awaiting review" - the question the feature exists to
  raise.
- **Justified by design:** re-reading the row at send time rather than
  cancelling a queued job. Cancellation is unreliable; a re-check is not.
- **Justified by design:** capturing scalars rather than the `Task` instance in
  the `on_commit` closure.
- **Justified by design:** `direct_manager` walking past a vacant seat, so an
  escalation is not swallowed by an unfilled middle role.

## 9. Permissions & tenant isolation

There is no permission to hold: the flow is triggered by the assignee's own
toggle, which is already gated (`todo_hierarchy_scoping` §5), and the recipient
is derived rather than requested. Nobody can name a reviewer, and nobody can
address a review request to a third party.

What leaves the module is the assignee's name, the task's title, description,
metric, target, priority, deadline, completion time and department, sent to one
person - the manager who assigned it or the assignee's line manager. Both are
people who can already read every one of those fields on the dashboard.

Tenant isolation: the notification lands on the platform tenant, because the
reviewer is always CX staff (§5). A school user can never be a reviewer, since
they cannot hold a CX seat (`vs_user/services/organogram.py:56-60`) and therefore
cannot be anyone's `assigned_by` or `direct_manager`.

## 10. Code map

| File | Responsibility |
|---|---|
| `services/tasks.py:79-118` | `set_done` - the self-completion test and the `on_commit` hand-off |
| `tasks.py:31-100` | `send_completion_review_request` - the guards, the reviewer, the context, the dispatch |
| `tasks.py:23-28` | `_first_name`, `_fmt_dt` |
| `constants.py:41-46` | The two event keys and `REVIEW_GRACE_SECONDS` |
| `services/hierarchy.py:133-151` | `direct_manager` - the reviewer fallback |
| `vs_notifications/constants.py:630-641` | The `todo.task_completed` registry entry |
| `vs_notifications/services/seed.py:1207-1230` | The in-app and email templates |
| `vs_notifications/services/dispatch.py:118-122` | The tenant fallback this module relies on |

## 11. Test coverage & gaps

- `ReviewRequestDispatchTests.test_dispatch_creates_in_app_and_email_for_reviewer`
  (`tests.py:206-233`) - the best test in the module. It seeds the real event
  registry and templates, runs the task, and asserts the reviewer is the
  assigner, that exactly one in-app and one email row exist, the event key, and
  that the rendered subject and body carry the task title and the assignee's
  name.
- `ReviewRequestDispatchTests.test_skips_when_completion_superseded`
  (`tests.py:235-243`) - guard three, with a deliberately stale stamp.

What the suite does not cover:

1. **The other three guards**: a deleted task, a reopen inside the window, and a
   `None` reviewer.
2. **The self-set path** - every test task is handed down, so
   `TodoHierarchy.direct_manager` is never the reviewer in a test. The vacant-seat
   walk-past that makes it interesting is untested end to end.
3. **`set_done`'s trigger condition.** Nothing asserts that a *manager*
   completing a report's task queues nothing, or that a repeat completion queues
   nothing - both are one-line conditions doing real work.
4. **`transaction.on_commit`.** The dispatch tests call the Celery task directly;
   no test goes through `set_done` and `captureOnCommitCallbacks`, so nothing
   pins that the job is queued at all.
5. **The `_job_*` attribution kwargs** reaching the queue monitor.
6. **`event-not-seeded`** - the `UnknownEventTypeError` branch.
7. **The tenant the notification is stamped with**, which is what makes this
   module's feed work where `vs_tickets`'s does not.
