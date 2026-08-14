# console_task_monitor

The **engine room**: every tracked Celery run, its status counts, and the beat
schedule as configured in code. Routes are at `/v1/admin/tasks/`,
`/v1/admin/tasks/stats/` and `/v1/admin/tasks/schedule/`.

---

## 1. What it is (and what it is NOT)

- Three reads over `core.BackgroundJob`, the row `core.tasks_base.TrackedTask`
  writes for every run it wraps (`views_tasks.py:1-18,62-94`).
- The **owner-facing slice of the same table** lives elsewhere:
  `/v1/user/me/tasks/` returns the caller's own jobs and is documented in
  `docs/user/user_security_monitoring.md`. This surface returns everyone's, in
  every tenant, plus the fields the owner view withholds - `result`, `error`
  and the full `traceback` (`views_tasks.py:41-48`).
- **Nothing here can act on a job.** There is no cancel, no retry, no requeue -
  `TaskMonitorViewSet` is `ListModelMixin` plus two read actions
  (`views_tasks.py:62`).
- `schedule/` reports the beat schedule **as configured in code**, read off
  `celery_app.conf.beat_schedule` at request time. It is not a live view of what
  beat is actually running (`views_tasks.py:133-154`).

## 2. Domain model

This app owns no table for this slice. One model matters:

| Model | Where | Notes |
|---|---|---|
| `core.BackgroundJob` | `core/models.py:31-102` | One row per tracked Celery run |

Fields the console reads: `owner` (the **actor** who triggered it, null for
system runs), `tenant` (non-nullable), `kind`, `label`, `task_name`,
`celery_task_id` (unique), `status`, `progress`, `result` (JSON), `error`,
`traceback`, `worker`, `created_at`, `started_at`, `finished_at`
(`core/models.py:47-91`).

Five statuses: `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`
(`core/models.py:40-45`). Nothing in this codebase writes `CANCELLED`.

Meta: default ordering `-created_at`, plus three composite indexes -
`(owner, -created_at)`, `(status, -created_at)`, `(kind, -created_at)`
(`core/models.py:93-99`).

**Who writes the rows.** `TrackedTask.apply_async` creates a `QUEUED` row only
when the caller passed `_job_owner_id` or `_job_label`
(`core/tasks_base.py:75-103`); `before_start` then `get_or_create`s
unconditionally, so a scheduled run with neither still gets a row - it simply
appears as `RUNNING` and never as `QUEUED`
(`core/tasks_base.py:108-128`). `_finish` writes the terminal state, is guarded
against double-writing, truncates `error` at 2,000 characters and `traceback` at
10,000 (`core/tasks_base.py:158-185`).

**Retention.** `prune_background_jobs_task` deletes `SUCCEEDED`/`FAILED` rows
older than 90 days at 02:30 daily. `QUEUED`/`RUNNING` rows are never pruned,
because a stuck row is a signal rather than noise
(`core/tasks.py:11-24`; `apps/celery.py:39-42`).

## 3. Endpoint map

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /tasks/` | `IsVisionStaff` (Django `is_staff` flag) | `status`, `task`, `kind`, `since` | Paginated jobs, newest first (`views_tasks.py:72-94`) |
| `GET /tasks/stats/` | same | - | `{by_status, last_24h, by_task, recent_failures, total}` (`views_tasks.py:97-130`) |
| `GET /tasks/schedule/` | same | - | `{eager_mode, broker_configured, entries}` (`views_tasks.py:133-154`) |

Filter semantics (`views_tasks.py:77-93`):

- `?status=` is upper-cased before matching, so `?status=failed` works.
- `?task=` is an `icontains` substring of the **task name**, e.g. `?task=import`.
- `?kind=` is lower-cased and matched exactly: `import`, `export`, `email`,
  `system`.
- `?since=YYYY-MM-DD` filters `created_at__date__gte`.

The viewset does not set `tenant_param_required = False`, so
`TenantJWTAuthentication` requires `?tenant=<slug>` on all three routes
(`vs_rbac/authentication.py:123-126`) - even though nothing in the queryset uses
it.

## 4. Lifecycle / state machine

The console observes a lifecycle it does not drive
(`core/tasks_base.py:75-185`):

```text
apply_async  ──► QUEUED     (only when _job_owner_id or _job_label was passed)
before_start ──► RUNNING    started_at + worker stamped; row created if missing
on_success   ──► SUCCEEDED  progress = 100, JSON-safe return value stored
on_failure   ──► FAILED     error (2 000 chars) + traceback (10 000 chars)

_finish is terminal-guarded: a row already SUCCEEDED/FAILED is never rewritten,
which is what stops eager mode double-writing (core/tasks_base.py:130-145).

prune (daily 02:30) ──► SUCCEEDED/FAILED rows older than 90 days deleted
                        QUEUED/RUNNING rows kept regardless of age
```

## 5. Derivations

- **`runtime_seconds`**: `finished_at - started_at` rounded to 3 decimals, and
  `None` while the job is queued or running - a running job has no stable
  runtime yet (`views_tasks.py:54-58`).
- **`owner_name`**: `owner.full_name`, or `None` for a system-owned run
  (`views_tasks.py:50-52`).
- **`by_status` / `last_24h`**: `values_list("status").annotate(Count("id"))`
  wrapped in `dict()`, all-time and over the trailing 24 hours, so a regression
  stands out against the baseline (`views_tasks.py:103-109`).
- **`by_task`**: run count per `task_name`, the 20 busiest
  (`views_tasks.py:110-114`).
- **`recent_failures`**: the 5 most recent `FAILED` rows by `finished_at`,
  reduced to `task_name`, `label`, `finished_at`, `celery_task_id` - not the
  traceback (`views_tasks.py:115-120`).
- **`eager_mode`**: `settings.CELERY_TASK_ALWAYS_EAGER`. When true, tasks ran
  inline in the web process and nothing was queued at all - the first thing to
  check when the list looks wrong (`views_tasks.py:150`).
- **`broker_configured`**: truthiness of `CELERY_BROKER_URL`
  (`views_tasks.py:151`).
- **`entries`**: `name`, `task` and `str(schedule)` per beat entry. The schedule
  objects are stringified because `crontab` is not JSON-native
  (`views_tasks.py:138-146`).

## 6. What posting does to the ledger

Nothing posts and, unlike the other two slices in this module, nothing is
written at all: three `GET`s, no audit event, no side effect
(`views_tasks.py:62-154`). Note the asymmetry with `console_impersonation`,
where listing sweeps rows.

Reading this surface leaves no trace. Every row exposed here carries another
person's `result` payload, error text and traceback, and no audit event records
that anyone looked (compare `vs_audit`, whose own reads are key-gated).

## 7. Worked example

```text
GET /v1/admin/tasks/?tenant=codex&kind=import&status=failed
```

```json
{ "success": true, "message": "Data retrieved successfully",
  "pagination": { "currentPage": 1, "pageSize": 25, "totalItems": 2,
                  "totalPages": 1, "next": null, "previous": null },
  "data": [
    { "id": 8814, "celery_task_id": "6f0c…", 
      "task_name": "vs_import_data.tasks.execute_import_batch_task",
      "kind": "import", "label": "Import students - Lekki Campus",
      "owner": 411, "owner_name": "Ngozi Eze", "tenant": 3,
      "status": "FAILED", "progress": 62, "worker": "celery@web-1",
      "created_at": "2026-08-14T07:02:11Z", "started_at": "2026-08-14T07:02:13Z",
      "finished_at": "2026-08-14T07:04:48Z", "runtime_seconds": 155.219,
      "result": null,
      "error": "IntegrityError: duplicate key value violates unique constraint …",
      "traceback": "Traceback (most recent call last): …" }
  ] }
```

The list route returns the `XVSPagination`
`{success, message, pagination, data}` envelope
(`views_tasks.py:70`; `core/pagination.py:12-31`), while `stats/` and
`schedule/` return `success_response`'s `{success, message, data}` without the
`pagination` block (`views_tasks.py:121-130,147-153`).

## 8. Gotchas / known limitations

- **The gate is Django's `is_staff` flag, not RBAC, and every CX staff account
  has it.** `IsVisionStaff` checks nothing but
  `user.is_authenticated and user.is_staff` (`permissions.py:7-18`), and
  `UserService` sets `is_staff=True` for every account created with
  `user_type == "CX_STAFF"` (`vs_user/services/user.py:86`). So a brand new CX
  hire holding no role and no permission whatsoever can read **every tenant's**
  job history including each job's `result` JSON, `error` string and full
  `traceback` (`views_tasks.py:41-48`). Those payloads are whatever the task
  returned or blew up on: import summaries, export row counts, database error
  text quoting the offending values. There is no `rbac_permission` on the
  viewset at all, no tenant filter in the queryset
  (`views_tasks.py:72-94`), and no audit row recording the read. This is the
  same shape as the lockout list finding in
  `docs/user/user_security_monitoring.md` §8, and it is the item to fix first
  here: the surface needs a seeded key (the platform catalogue has no
  `tasks`/`jobs` resource today) and, for anything but a platform actor, a
  tenant filter.
- **`?since=` returns a 500 on a malformed date.** The raw string goes straight
  into `created_at__date__gte` (`views_tasks.py:90-92`); `?since=yesterday`
  raises a Django `ValidationError`, which is not a DRF exception and so falls
  through to the unhandled branch of the handler as
  `500 SERVER_ERROR` (`core/exceptions.py:158-168`). The same class of defect is
  recorded against the security views in
  `docs/user/user_security_monitoring.md` §8; the helper that fixes it is
  `vs_user/views/accounts.py:51-60`.
- **`?status=` and `?kind=` are unvalidated free text.** `?status=BOGUS` and
  `?kind=nonsense` both return an empty page rather than a `400`
  (`views_tasks.py:77-89`), so a frontend typo reads as "no jobs" rather than as
  an error.
- **Two response shapes on one viewset.** `list` returns the pagination
  envelope; `stats` and `schedule` return `success_response` with no
  `pagination` block. Any client has to branch on the route
  (`views_tasks.py:70,121,147`).
- **`stats/` runs five unbounded aggregates on every call.** All-time
  `by_status`, 24-hour `by_status`, `by_task` grouped over the whole table,
  `recent_failures`, and a full `COUNT(*)` (`views_tasks.py:103-129`). Three of
  the five are covered by the `(status, -created_at)` index; `by_task` groups on
  an unindexed `task_name` and `total` counts the table. With 90 days of
  retention that is survivable, but it is a screen built to be polled.
- **`?tenant=` is required and then ignored.** The viewset does not opt out of
  the tenant assertion, so every call must carry a slug
  (`vs_rbac/authentication.py:123-126`) that the queryset never uses
  (`views_tasks.py:72-94`). It reads as tenant scoping and provides none.
- **`schedule/` reports intent, not reality.** It reads the in-process
  `beat_schedule` dict (`views_tasks.py:136-146`), so it answers identically
  whether or not a beat process is running, and it cannot show last-run or
  next-run times. `eager_mode` and `broker_configured` are the only two signals
  here that say anything about the deployment.
- **`CANCELLED` is dead vocabulary.** The status exists
  (`core/models.py:45`) and is documented as a filter value
  (`views_tasks.py:12`), but nothing writes it and nothing here can cancel a
  job.
- **Justified by design:** `owner` is the actor who triggered the task, never
  the subject it acts on - an invitation email to Jane queued by Ada is Ada's
  row (`core/models.py:50-54`). It is the right choice for a queue view, and it
  is why `owner_name` sometimes looks unrelated to `label`.
- **Justified by design:** `recent_failures` returns four scalar fields and not
  the traceback (`views_tasks.py:115-120`). The dashboard card does not need it,
  even though the list route beside it hands it over in full.

## 9. Permissions & tenant isolation

| Surface | Gate | Seeded? |
|---|---|---|
| Task list | `IsVisionStaff` - Django `is_staff` only | n/a - no RBAC key exists |
| Task stats | same | n/a |
| Beat schedule | same | n/a |

**There is no tenant isolation on this slice.** `BackgroundJob` carries a
`tenant` FK and uses the plain default manager (`core/models.py:56-59,93-99`),
the queryset applies no tenant condition (`views_tasks.py:74`), and `tenant` is
serialised as a bare id with no filter to match it (`views_tasks.py:41-48`).
That is defensible for a platform operations console; it is not defensible
behind a flag that every CX account carries by construction.

`StaffReadOnlyOrSuperuserWrite` (`permissions.py:22-37`) is defined in the same
file and used nowhere - there is nothing to write here.

## 10. Code map

| File | Responsibility |
|---|---|
| `views_tasks.py` | `AdminJobSerializer`, the list queryset and its four filters, `stats`, `schedule` |
| `permissions.py` | `IsVisionStaff` - the only gate on this slice |
| `urls.py:16` | Router registration under the `tasks` basename |
| `core/models.py` | `BackgroundJob` - fields, statuses, indexes, and the owner-is-the-actor rule |
| `core/tasks_base.py` | `TrackedTask` - what actually writes and finalises every row |
| `core/tasks.py` | `prune_background_jobs_task` - the 90-day retention sweep |
| `apps/celery.py` | The beat schedule `schedule/` reports |
| `vs_user/views/jobs.py` | The owner-facing view of the same table (`/v1/user/me/tasks/`) |

## 11. Test coverage & gaps

`tests_tasks.py` is four tests (`tests_tasks.py:31-78`):

- `test_list_and_filters` (`tests_tasks.py:36`) - the list plus the `status`,
  `task` and `kind` filters.
- `test_stats` (`tests_tasks.py:55`) - the `by_status` counts and the payload
  keys.
- `test_schedule_lists_beat_entries` (`tests_tasks.py:66`) - two known beat
  entry names and the presence of `eager_mode`.
- `test_non_staff_denied` (`tests_tasks.py:75`) - an **anonymous** client gets
  `401`.

This is the thinnest coverage in the module, and two of the gaps are the reason
§8 reads the way it does:

1. **"Non-staff denied" tests nobody.** It uses a bare `APIClient()` with no
   credentials, so it asserts that anonymous requests are rejected - not that an
   authenticated non-staff user is. There is no test that a school user, or a CX
   user with no role, is refused.
2. **The whole file uses `force_authenticate`** (`tests_tasks.py:34`), which
   bypasses `TenantJWTAuthentication` entirely. The `?tenant=` requirement, the
   ambient tenant contextvar, and therefore any tenant scoping this surface
   might grow are all unexercised. The pattern that does drive the real auth
   path is in this module already: `vs_admin_console/tests.py:83-88`.
3. **No cross-tenant test.** Every job in the fixtures is created against the
   `codex` tenant (`tests_tasks.py:23-28`), so nothing would fail if a tenant
   filter were added, and nothing fails today for its absence.
4. Also uncovered: `?since=` in any form including the malformed value that
   500s, `runtime_seconds` for a running job, `owner_name` for a system-owned
   run, `recent_failures`, and the empty-table response shape.
