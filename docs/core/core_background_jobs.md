# core_background_jobs

Every Celery task on this platform is tracked, automatically, in one table. This
slice owns `BackgroundJob`, the `TrackedTask` base class that writes it, the
reserved kwargs callers use to attribute a job to a person, the completion
notification, and the nightly prune.

The two screens built on it are documented elsewhere:
`docs/user/user_accounts.md`'s `/v1/user/me/tasks/` and
`docs/console/console_task_monitor.md`'s admin view.

---

## 1. What it is (and what it is NOT)

- **Tracking is not opt-in.** `Celery("apps", task_cls="core.tasks_base:TrackedTask")`
  (`apps/celery.py:10`) makes `TrackedTask` the base of every task in the repo,
  including every `@shared_task`. A new task is tracked without its author doing
  anything.
- **The owner is the ACTOR, never the subject.** An invitation email to Jane
  queued by admin Ada is owned by Ada: she triggered it, she sees it in View
  Queues, she is the one told when it lands. Passing the subject there hands a
  stranger someone else's queue row and completion notification
  (`core/tasks_base.py:18-23`).
- **Attribution rides on reserved kwargs, not on the task signature.**
  `_job_owner_id`, `_job_tenant_id`, `_job_label`, `_job_kind`, `_job_notify`
  are stripped in `apply_async` before the task runs
  (`core/tasks_base.py:45-48,76-77`), so adopting tracking never changes a task's
  parameters.
- **Tracking is best-effort, everywhere.** Every one of the four write points is
  wrapped in a bare `except` that logs and swallows
  (`core/tasks_base.py:102,126,184,206`). A database problem while writing the
  job row must never block the work.
- **A job row is not a Celery result.** It records status, timing, worker, and a
  JSON-safe return value - not the task's arguments, and not a retry chain.
- **It is a user-facing queue, and it is also where beat lives.** Every scheduled
  task in `apps/celery.py` writes a row on every run, owned by nobody
  (§8).
- **There is no cancel.** `Status.CANCELLED` exists in the enum
  (`core/models.py:45`) and nothing in `core` ever sets it.

## 2. Domain model

### `BackgroundJob` (`core/models.py:31`)

| Field | Notes |
|---|---|
| `owner` | `SET_NULL`, nullable. The actor. Null = system/scheduled |
| `tenant` | `PROTECT`, **not null** - derived from the owner, or `codex` |
| `kind` | Free string for filtering: `import`, `export`, `email`, `system` |
| `label` | The human description the queue UI shows |
| `task_name`, `celery_task_id` | `celery_task_id` is **unique** - the join key |
| `status` | `QUEUED` → `RUNNING` → `SUCCEEDED` / `FAILED` (`CANCELLED` unused) |
| `progress` | 0-100 when the task reports it; forced to 100 on success |
| `result` | `JSONField` - the return value, when JSON-safe |
| `error`, `traceback` | Truncated to 2,000 and 10,000 characters |
| `worker` | The hostname that ran it |
| `notify_owner` | Whether the owner gets a bell on completion |
| `created_at`, `started_at`, `finished_at` | |

Ordered newest first, with three indexes: `(owner, -created_at)`,
`(status, -created_at)`, `(kind, -created_at)` - matching the three ways the two
screens filter.

## 3. The lifecycle hooks

`TrackedTask` overrides five Celery hooks:

| Hook | What it does |
|---|---|
| `apply_async` | Strips the five reserved kwargs; mints a `task_id` if absent; writes a `QUEUED` row **only when `_job_owner_id` or `_job_label` is present** (`tasks_base.py:75-83`) |
| `before_start` | `get_or_create` on `celery_task_id`, then sets `RUNNING`, `started_at` and `worker` (`108-128`) |
| `__call__` | Catches an exception in **eager** mode and records the failure there, because eager propagation re-raises before `on_failure` fires (`130-144`) |
| `on_success` | `_finish(succeeded=True, retval=…)` |
| `on_failure` | `_finish(succeeded=False, error=…, traceback=…)` |

`before_start`'s `get_or_create` is what makes an unattributed task appear at
all: a beat schedule or an internal fan-out that passed no reserved kwargs gets
its row created here, with `owner=None`.

`_finish` (`tasks_base.py:157-185`) is **terminal-state guarded**: if the row is
already `SUCCEEDED` or `FAILED` it returns. That is what stops the eager path in
`__call__` and the worker path in `on_failure` from double-writing.

## 4. Lifecycle

```text
  caller: task.delay(..., _job_owner_id=…, _job_label=…)
     │
     ├─ apply_async strips the kwargs
     │     _job_owner_id or _job_label present?
     │        yes → QUEUED row
     │        no  → nothing yet
     ▼
  worker picks it up
     │
     ├─ before_start: get_or_create, then RUNNING + started_at + worker
     ▼
  the task body runs
     │
     ├─ success → on_success  → SUCCEEDED, progress 100, result
     └─ failure → on_failure  → FAILED, error, traceback
                  (eager mode: __call__ records it first)
     │
     └─ _notify_owner: an in-app "task.completed" / "task.failed"
                       if owner, label and notify_owner are all set
```

Then, nightly at 02:30 (`apps/celery.py:39-42`),
`prune_background_jobs_task` deletes `SUCCEEDED` and `FAILED` rows older than 90
days. `QUEUED` and `RUNNING` rows are kept regardless of age, because a stuck row
is a signal, not noise (`core/tasks.py:22-24`).

## 5. Derivations

- **The tenant is derived, not required.** `_resolve_job_tenant_id`
  (`tasks_base.py:51-59`): an explicit `_job_tenant_id` wins; else the owner's
  tenant, via one `User.objects.only("tenant_id").get(...)`; else the `codex`
  platform tenant. The docstring tells callers to omit it unless it must differ
  from the owner's.
- **`kind` is guessed when not given.** `_short_kind` (`tasks_base.py:62-67`)
  matches on the task name: `"import"` in it → `import`; `"email"` or
  `"notification"` → `email`; otherwise `system`.
- **`notify_owner` defaults to True by absence.**
  `meta["_job_notify"] is not False` (`tasks_base.py:99`), so only an explicit
  `False` opts out. The reason is per-recipient fan-out: one invitation email job
  per imported row would otherwise ring the actor's bell once per row.
- **`result` is stored only for JSON-safe scalars and containers** -
  `dict, list, str, int, float, bool` (`tasks_base.py:175-176`). A task returning
  a model instance records nothing rather than failing.
- **A notification needs all three of owner, label and `notify_owner`**
  (`tasks_base.py:191`), which is why system rows notify nobody: they have no
  owner and no label.
- **The notification is best-effort even by this module's standards**: its own
  `except` swallows `UnknownEventTypeError` from an unseeded event registry
  (`tasks_base.py:206-209`), so a fresh environment simply sends nothing.

## 6. What it writes

| Point | Writes |
|---|---|
| `apply_async` | a `QUEUED` `BackgroundJob`, for attributed tasks only |
| `before_start` | the row if absent; `RUNNING`, `started_at`, `worker` |
| `_finish` | terminal status, `finished_at`, `progress`, `result` or `error`+`traceback` |
| `_notify_owner` | one `task.completed` / `task.failed` notification through `vs_notifications`, with `tenant=job.tenant` |
| `prune_background_jobs_task` | deletes terminal rows older than 90 days |

Four log lines, all `logger.warning` with `exc_info`, one per swallowed failure
point.

Note the notification passes `tenant=job.tenant` explicitly - the modern
parameter, correctly - so the row lands on the tenant whose queue the job belongs
to.

## 7. Worked example

A school admin starts a student import:

```python
execute_import_batch_task.delay(
    import_batch_id=str(batch.id),
    _job_owner_id=str(request.user.id),
    _job_label=f"Import: {batch.file_name}",
    _job_kind="import",
)
```

```text
apply_async  → BackgroundJob(celery_task_id=…, owner=Ada, tenant=bright-star,
                             kind="import", label="Import: students-jss2.xlsx",
                             status=QUEUED, notify_owner=True)
before_start → RUNNING, started_at=…, worker="celery@web-1"
on_success   → SUCCEEDED, progress=100,
               result={"created": 214, "skipped": 3, "errors": 0}
_notify_owner→ "task.completed" to Ada: "Import: students-jss2.xlsx"
```

Ada sees the row at `/v1/user/me/tasks/` throughout, and gets a bell when it
lands.

The same day, unattributed:

```text
beat fires vs_health.tasks.capture_queue_snapshot_task
   apply_async → nothing (no owner, no label)
   before_start→ BackgroundJob(owner=None, tenant=codex, kind="system",
                               task_name="vs_health.tasks.capture_queue_snapshot_task",
                               status=RUNNING)
   on_success  → SUCCEEDED
   _notify_owner → returns immediately (no owner, no label)
```

That row is written **every minute**, as is `evaluate_alert_rules_task`. Add the
five-minute and half-hourly schedules and the table gains roughly four thousand
system rows a day before any person does anything, all of them retained for
ninety days (`core_code_issues.md` §10).

## 8. Gotchas / known limitations

Full evidence in **`error/core/core_code_issues.md`**.

- **Beat floods the user-facing queue.** Two tasks run every minute and every run
  writes a row; the 90-day prune leaves the table holding hundreds of thousands
  of system rows that no person triggered (`core_code_issues.md` §10).
- **`CANCELLED` is a status nothing sets.** There is no cancel path in `core`,
  so the enum promises an operation that does not exist.
- **`error` and `traceback` are stored and shown.** `job.error[:300]` goes into
  the owner's notification (`tasks_base.py:201`), and the admin task monitor
  exposes `result`, `error` and `traceback` for every tenant - already recorded
  against `vs_admin_console`. A task whose exception message carries a value
  puts that value in front of a user.
- **`_resolve_job_tenant_id` issues a query per unattributed task start**
  (`tasks_base.py:58-59`), looking up the `codex` tenant by slug every time.
- **A task that is queued but never runs leaves a `QUEUED` row forever.** The
  prune deliberately keeps it, and nothing else reports it - there is no
  stuck-job sweeper in `core` (each module writes its own, e.g.
  `mark_stuck_import_jobs_task`).
- **`progress` is never written by `core`.** The field exists, the docstring
  promises "0-100 when the task reports progress", and only `_finish`'s
  `progress = 100` ever sets it - a task that wants to report progress has to
  update the row itself.
- **Tracking failures are invisible in the product.** They are `logger.warning`
  only, so a job that was never recorded looks to the user like a task that
  never ran.
- **Justified by design:** every write point swallows its exceptions - tracking
  must never block the work.
- **Justified by design:** `_finish` is terminal-state guarded, so eager and
  worker paths cannot double-write.
- **Justified by design:** the prune keeps `QUEUED`/`RUNNING` rows regardless of
  age.
- **Justified by design:** the owner is the actor, stated at length in the module
  docstring because getting it wrong hands a stranger somebody else's queue.

## 9. Permissions & tenant isolation

`core` enforces neither - it only writes rows. The two consumers do:

| Surface | Scope |
|---|---|
| `/v1/user/me/tasks/` | `owner=request.user`, with an admin `?scope=all` toggle |
| `/v1/admin/tasks/` | the admin console's own gate |

`BackgroundJob.tenant` is non-null and derived, so the rows carry the scope even
though `core` never reads it. The model has no tenant-aware manager, so every
consumer writes its own filter - the same shape as `vs_workflow`'s stage models.

One consequence worth naming: a job's tenant follows its **owner**, not its
subject. A CX agent who triggers work on a school's behalf gets a row on the
platform tenant, so it appears in the platform's queue and not the school's.

## 10. Code map

| File | Responsibility |
|---|---|
| `core/models.py:31-102` | `BackgroundJob` |
| `core/tasks_base.py:45-67` | The reserved kwargs, `_resolve_job_tenant_id`, `_short_kind` |
| `core/tasks_base.py:75-103` | `apply_async`, `_record_queued` |
| `core/tasks_base.py:108-155` | `before_start`, `__call__`, `on_success`, `on_failure` |
| `core/tasks_base.py:157-185` | `_finish` |
| `core/tasks_base.py:190-209` | `_notify_owner` |
| `core/tasks.py` | `prune_background_jobs_task` |
| `apps/celery.py:10` | `task_cls` - what makes it universal |
| `apps/celery.py:18-149` | The beat schedule that flows through it |
| `vs_user/views/jobs.py` | `/me/tasks/` |
| `vs_admin_console/views_tasks.py` | the admin monitor |

## 11. Test coverage & gaps

`core/test_jobs.py` is focused and good:

- `TrackedTaskTests` (`test_jobs.py:48-116`) - an owned task's full lifecycle
  plus its notification; a failure records the error; `_job_notify=False` tracks
  the job but stays silent; a system task is recorded without an owner.
- `MyTasksAPITests` (`118-168`) - the `mine` scope shows only my jobs, the `all`
  scope requires an admin, the filters work, and the summary flags the admin
  toggle.

What it does not cover:

1. **`prune_background_jobs_task`** - neither that it deletes terminal rows past
   the cutoff nor, more importantly, that it leaves `QUEUED`/`RUNNING` rows
   alone.
2. **`_resolve_job_tenant_id`'s three branches** - explicit id, owner's tenant,
   and the `codex` fallback.
3. **`_short_kind`'s guessing**, and `_job_kind` overriding it.
4. **The terminal-state guard in `_finish`** - the property that stops the eager
   and worker paths double-writing.
5. **`result` filtering** - that a non-JSON-safe return value is dropped rather
   than crashing the finish.
6. **The truncation of `error` and `traceback`.**
7. **That tracking failures are swallowed** - the promise every one of the four
   bare excepts makes.
