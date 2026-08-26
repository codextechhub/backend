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
  `docs/user/user_security_monitoring.md`.
- **The surface is split three ways by what it costs to read.** The list is
  metadata only - no `result`, no `error`, no `traceback`. The detail route
  adds the **redacted** `result` and `error`. The raw, unredacted text lives
  on `<id>/diagnostics/`, behind a separate CRITICAL key, and every read of it
  writes an audit event. Before M-sec this was one list route serving all
  three at once to any Django staff account.
- **Nothing here can act on a job.** There is no cancel, no retry, no requeue -
  `TaskMonitorViewSet` is `ListModelMixin` + `RetrieveModelMixin` plus three
  read actions.
- `schedule/` reports the beat schedule **as configured in code**, read off
  `celery_app.conf.beat_schedule` at request time. It is not a live view of what
  beat is actually running (`views_tasks.py:133-154`).

## 2. Domain model

This app owns no table for this slice. One model matters:

| Model | Where | Notes |
|---|---|---|
| `core.BackgroundJob` | `core/models.py` | One row per tracked Celery run. `result`/`error`/`traceback` are stored **redacted** |
| `core.TaskDiagnostic` | `core/models.py` | The raw failure text for one job. Never listed; 400-day retention |

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
10,000.

**Redaction happens here, once, for the whole platform.** `_finish` passes the
error, traceback and result through `core.redaction` before saving, so what
reaches `BackgroundJob` is `Key (email)=([redacted])` rather than the address
Postgres put there. This is the choke point every task passes through, which is
why the scrub is here and not in the tasks: a task cannot know whether its own
exception text carries personal data, because it did not write that text.

For a failure, `_record_diagnostic` then writes the **unredacted** original to
`core.TaskDiagnostic`. Successes keep no raw copy - a success result is already
summary data, and storing every one would rebuild the store this design exists
to empty.

**Retention.** Two windows, deliberately different lengths.
`prune_background_jobs_task` deletes `SUCCEEDED`/`FAILED` rows older than 90
days at 02:30 daily; `QUEUED`/`RUNNING` rows are never pruned, because a stuck
row is a signal rather than noise. `prune_task_diagnostics_task` runs at 02:45
and deletes `TaskDiagnostic` rows past their own `expires_at`, stamped at write
time from `TASK_DIAGNOSTIC_RETENTION_DAYS` (default 400).

The operational queue is read in days and the audit record in quarters, so the
diagnostic outlives the job row it describes. Stamping `expires_at` per row
rather than computing it from the current setting means shortening the setting
later cannot retroactively extend rows already on disk.

## 3. Endpoint map

All routes require a PLATFORM-tenant caller (`vs_rbac.permissions.IsVisionStaff`)
**and** the key below. The two are separate questions: the tenant kind says who
may stand at the platform level at all, the key says what they may then read.

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /tasks/` | `platform.tasks.view` | `status`, `task`, `kind`, `since`, `for_tenant` | Paginated jobs, newest first. Metadata only |
| `GET /tasks/<id>/` | `platform.tasks.view` | - | One job + **redacted** `result` and `error` |
| `GET /tasks/<id>/diagnostics/` | `platform.tasks.view_sensitive` | - | Raw `raw_error`, `raw_traceback`, `raw_result`. Audited |
| `GET /tasks/stats/` | `platform.tasks.view` | same filters | `{by_status, last_24h, by_task, recent_failures, total}`, tenant-scoped |
| `GET /tasks/schedule/` | `platform.tasks.view` | - | `{eager_mode, broker_configured, entries}` |

`platform.tasks.view_all` is a fourth key and gates no route of its own: it
widens the queryset from the caller's own tenant to every tenant. Both it and
`view_sensitive` are CRITICAL and seeded to `xvs_super_admin` only
(`seed_platform_permissions.py`).

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

Nothing posts, and the reads that cost nothing write nothing: the list, the
detail, `stats/` and `schedule/` are plain `GET`s with no side effect.

**`diagnostics/` is the exception, and deliberately so.** It emits a
`TASK_DIAGNOSTIC_VIEWED` audit event at `WARNING` severity before returning,
naming the reader, the job and the tenant. Reading a raw traceback means
reading whatever personal data the failing row carried, so the read is itself
the auditable act - without it, "who looked at Corona's failed guardian
import" has no answer.

The event is filed against the **job's** tenant, not the reader's. An auditor
at Corona Secondary School asking who read their data is asking about Corona's
trail; filing it under Codex would put the answer where they cannot see it.

## 7. Worked example

```text
GET /v1/admin/tasks/?tenant=codex&for_tenant=corona&kind=import&status=failed
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
      "has_diagnostic": true }
  ] }
```

No payload fields on a list row. `has_diagnostic` says a raw record exists to
open without being the record - one scroll of this page used to render every
failing row's full traceback, and a Postgres duplicate-key error carries the
duplicated value.

Opening the one row that matters:

```text
GET /v1/admin/tasks/8814/
```

```json
{ "success": true, "message": "Task run retrieved.",
  "data": { "id": 8814, "status": "FAILED", "has_diagnostic": true,
            "result": null,
            "error": "duplicate key value violates unique constraint \"vs_user_user_email_key\"\nDETAIL:  Key (email)=([redacted]) already exists." } }
```

The shape of the failure survives; the person in it does not. The address is
one key and one audit event away, at `/v1/admin/tasks/8814/diagnostics/`.

The list route returns the `XVSPagination`
`{success, message, pagination, data}` envelope (`core/pagination.py`), while
the detail route, `stats/`, `schedule/` and `diagnostics/` return
`success_response`'s `{success, message, data}` without the `pagination` block.
`retrieve` is overridden to do this rather than returning DRF's bare serializer
body, which would have made it the only endpoint in the console outside the
envelope.

## 8. Gotchas / known limitations

- **Tenant scoping is coarse: platform-wide or own-tenant, with nothing in
  between.** `visible_tenant_ids` returns every tenant for a holder of
  `platform.tasks.view_all` and the caller's own tenant for everyone else. The
  shape an operations team actually wants - "this support operator covers
  Corona and Greenfield" - cannot be expressed, because `platform.tasks.*` is
  PLATFORM-scoped and `vs_rbac.models.assert_tenant_may_hold` refuses a
  platform-scoped key granted inside a tenant role. Building it means either
  relaxing that guard, which exists to stop a school granting itself platform
  powers, or adding a second "which schools does this operator cover" table
  alongside RBAC. That is a feature, not a fix, and it is not attempted here.
  The practical consequence today: anyone who needs one school's task rows
  needs `view_all`, which gives them all of them.
- **The tenant filter is `?for_tenant=`, and it cannot be `?tenant=`.**
  `?tenant=` is the assertion `TenantJWTAuthentication` requires, naming the
  tenant the caller is acting *in*; this viewset does not set
  `platform_cross_tenant_param`, so a CodeX operator must assert `codex` or be
  refused with 404. The filter shipped reading that same parameter, which meant
  a Super Admin holding `platform.tasks.view_all` sent `?tenant=codex` like
  everybody else and watched the platform-wide list they hold a CRITICAL key
  for collapse to Codex's own system jobs, showing no school at all. The same
  conflation was a 500 on `/v1/health/tasks/`, which filtered `tenant_id` (an
  integer) by the asserted slug and so answered 500 to every caller it ever
  had. Both now go through `core.tenant_filters`, which owns the name.
- **Redaction is pattern-based, so it over-redacts by design.** A 12-digit
  invoice reference is masked alongside a bank account number, and part of a
  UUID inside a traceback can be too. The trade is deliberate - masking a
  reference costs an operator one click into `diagnostics/`; missing an
  account number leaves it in a backup for a year - but it does mean the
  redacted `error` is a triage aid rather than a faithful transcript.
- **`schedule/` is not tenant-scoped and does not need to be**, but it is worth
  saying out loud: it describes the platform's own internals, so it sits behind
  `platform.tasks.view` on that basis rather than on any customer-data basis.
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
  the traceback. The dashboard card does not need it, and the list route beside
  it no longer hands one over either.
- **Justified by design:** `stats/` aggregates are built from `get_queryset()`
  rather than the bare manager, so the counts an operator reads describe the
  rows they can list. Unscoped totals would have leaked the size of tenants
  they cannot see.

## 9. Permissions & tenant isolation

| Surface | Gate | Seeded to |
|---|---|---|
| Task list / detail / stats / schedule | `IsVisionStaff` (PLATFORM tenant) + `platform.tasks.view` | `xvs_super_admin`, `xvs_platform_admin` |
| Cross-tenant widening | `platform.tasks.view_all` (CRITICAL) | `xvs_super_admin` only |
| Raw diagnostics | `platform.tasks.view_sensitive` (CRITICAL) | `xvs_super_admin` only |

**Tenant isolation.** `get_queryset` filters on `BackgroundJob.tenant` against
`visible_tenant_ids(user)`, so a caller without `view_all` sees their own
tenant only, and `?for_tenant=<slug|id>` narrows within whatever that leaves. An
unknown slug narrows to nothing rather than being ignored: silently returning
every tenant for a typo is how a filter becomes a leak.

**Why `IsVisionStaff` sits beside the key.** A permission key living in the
`platform` module is a naming convention, not a boundary - `vs_rbac/views.py`
already reckons with "a school role that somehow carried a platform key". Here
the scope column closes that door first (`assert_tenant_may_hold` refuses the
grant outright), and the tenant-kind check closes it again at the endpoint.

**Historical note.** This slice was previously gated on a *shadow*
`IsVisionStaff` defined in `vs_admin_console/permissions.py`, which checked
Django's `is_staff` flag rather than the caller's tenant kind. It shared a name
with `vs_rbac.permissions.IsVisionStaff` - the real gate, composed by roughly
twenty views elsewhere - so the call site read as the platform boundary while
asking a much weaker question. Both that class and the unused
`StaffReadOnlyOrSuperuserWrite` beside it have been deleted rather than fixed,
so the name resolves to one thing across the codebase.

## 10. Code map

| File | Responsibility |
|---|---|
| `views_tasks.py` | List/detail serializers, `visible_tenant_ids`, the filters, `diagnostics`, `stats`, `schedule` |
| `permissions.py` | `IsPlatformActor` only - the gate now comes from `vs_rbac.permissions` |
| `urls.py` | Router registration under the `tasks` basename |
| `core/models.py` | `BackgroundJob` and `TaskDiagnostic` |
| `core/redaction.py` | `redact_text`, `redact_payload`, `RedactingLogFilter` - the scrub both the DB and the log stream call |
| `core/log_format.py` | `JSONFormatter` - one JSON object per log line |
| `core/tasks_base.py` | `TrackedTask._finish` - the choke point that redacts and forks the raw copy |
| `core/tasks.py` | `prune_background_jobs_task` (90 days), `prune_task_diagnostics_task` (400) |
| `apps/settings/base.py` | `LOGGING`, `TASK_DIAGNOSTIC_RETENTION_DAYS` |
| `core/management/commands/seed_platform_permissions.py` | The three `platform.tasks.*` keys and their grants |
| `apps/celery.py` | The beat schedule `schedule/` reports |
| `vs_user/views/jobs.py` | The owner-facing view of the same table (`/v1/user/me/tasks/`) |

## 11. Test coverage & gaps

`tests_tasks.py` is five classes. The security cases come first because they
are the ones this surface got wrong.

**Access (`TaskMonitorAccessTests`)** - anonymous gets `401`; a Codex account
with `is_staff` and no role gets `403` (the finding, as a test); `is_superuser`
alone is not a grant; a school user is refused by tenant kind; and a school
role *cannot even be granted* `platform.tasks.view`, which proves the scope
column stops the grant a layer before the endpoint.

**Redaction (`TaskMonitorRedactionTests`)** - list rows carry no `result`,
`error` or `traceback`; the detail route shows a redacted `error` and still no
`traceback`.

**Diagnostics (`TaskDiagnosticAccessTests`)** - `platform.tasks.view` alone
cannot read raw text; `view_sensitive` can; the read emits
`TASK_DIAGNOSTIC_VIEWED` against the **job's** tenant naming the actor; a run
with no diagnostic is a `404` rather than an empty body.

**Tenant scope (`TaskMonitorTenantScopeTests`)** - `view_all` sees every
tenant; without it no customer rows are visible; `?for_tenant=` narrows; an unknown
slug returns nothing rather than everything; an out-of-scope row is not
reachable by id; `stats/` counts are scoped too.

**Listing (`TaskMonitorListingTests`)** - the four filters, the `by_status`
grouping fix, the malformed and well-formed `?since=`, the empty-list response
shape, and the beat entries including `prune-task-diagnostics`.

`core/tests_redaction.py` covers the other half - the choke point itself:
the Postgres DETAIL payload, a value no other rule matches (a guardian's
name), the SMTP refusal, bank account numbers, that ordinary diagnostics and
`task_name`-style keys survive, nested payloads, the log filter including
**exception text**, what `_finish` writes to each of the two tables, that
`/v1/user/me/tasks/` serves the redacted column, and the retention prune.

Still uncovered:

1. **`force_authenticate` throughout**, which bypasses
   `TenantJWTAuthentication`. The `?tenant=` assertion and the ambient tenant
   contextvar are unexercised; `vs_admin_console/tests.py` has the pattern that
   drives the real auth path.
2. **`runtime_seconds` for a running job** and **`owner_name` for a
   system-owned run**.
3. **No test that a *newly added* task's result is redacted** - the guarantee
   is structural (everything routes through `_finish`) rather than asserted
   per task.
4. **`?status=` and `?kind=` remain unvalidated free text** - still a silent
   empty page rather than a `400`, and still untested as such.
