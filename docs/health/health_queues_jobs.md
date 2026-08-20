# health_queues_jobs

The async half of the Command Center: the per-minute snapshot of every Celery
queue's depth, throughput, failures and worker pool; the depth-trend history it
accumulates; the `celery` service card it drives; and the task table, which is
not a `vs_health` model at all but a read over `core.BackgroundJob`.

Routes covered by this slice, mounted at `/v1/health/` (`apps/urls.py:39`):
`queues/`, `tasks/`.

Request metrics are `health_signal_collection`; probes and SLOs are
`health_uptime_availability`; alerts and incidents are `health_incidents_alerts`.

---

## 1. What it is (and what it is NOT)

- **The task table is not a health model.** `QueueSnapshot` stores only the
  queue-level aggregate; the row-level task list reads `core.BackgroundJob`
  directly, because every tracked task already writes one there
  (`models/queues.py:3-7`, `views.py:231-258`). There is no duplicated task
  record and there should not be.
- **A snapshot is a point in time, not a rolling window.** One row per queue per
  minute (`apps/celery.py:133-136`), each carrying the broker depth at that
  instant plus a one-minute aggregate of job outcomes. The depth trend on the
  screen is the last 40 of those rows (`services.py:511-514`).
- **Depth comes from Redis, not from the database.** `_broker_depths` runs
  `LLEN` against the broker for each known queue name
  (`tasks.py:122-134`); throughput and failure counts come from `BackgroundJob`.
  Two different sources in one row, which is why they can disagree.
- **A queue with no snapshot is absent, not healthy.** `queue_overview` skips a
  queue with no rows rather than inventing a zero
  (`services.py:507-510`). This is the correct instinct and it is the one place
  in the module that applies it consistently.
- **`KNOWN_QUEUES` is a list of names, not of queues that exist.** Nothing in
  the repo routes a task to any queue: `apps/celery.py` defines a beat schedule
  and no `task_routes`, so every task runs on Celery's default `celery` queue.
  Five of the six names are permanently empty by construction.
- **The `celery` service card is the one honest signal here.** Workers online →
  HEALTHY, broker reachable but no workers → CRITICAL, broker unreachable →
  UNKNOWN (`tasks.py:200-212`). It refuses to claim anything in the third case.
- **The queue card's labels do not match what it measures.** Throughput,
  retrying, dead and retry-storm each report something other than their name
  (§5, §8).
- **The task table is unreachable in production.** `/v1/health/tasks/` returns a
  500 on the only value of `?tenant=` the auth layer accepts.
- **This is not the console's task monitor.** `vs_admin_console` has its own
  BackgroundJob screen with its own gate (`is_staff`) and its own serializer that
  exposes `result`, `error` and `traceback`. This one exposes neither, and is
  gated on `platform.health.view`.

## 2. Domain model

### `QueueSnapshot` (`models/queues.py:18`)

| Field | Meaning | Source |
|---|---|---|
| `captured_at` | Instant of capture, indexed | `timezone.now()` default |
| `queue_name` | Indexed, max 64 | one row per name in `KNOWN_QUEUES` |
| `depth` | "Messages waiting in the broker" | Redis `LLEN` |
| `throughput_per_min` | "Tasks completed in the trailing minute" | count of `BackgroundJob`s **created** in the last minute that are now SUCCEEDED |
| `failed` | | count created in the last minute now FAILED |
| `retrying` | "retrying" | count created in the last minute now **RUNNING** |
| `dead` | | hardcoded `0` |
| `avg_duration_sec` | | written by nothing; always null |
| `workers_active` / `workers_idle` | | Celery `inspect` |
| `retry_storm` | "Abnormal retry spike detected" | `failed >= 50` |
| `status` | | derived from depth and failures |

Indexed on `(queue_name, -captured_at)` (`models/queues.py:32`), ordered newest
first. Pruned at three days (`tasks.py:407-408`) - the shortest retention in the
module, and correct, since a minute-resolution snapshot has no long-term value.

`KNOWN_QUEUES` (`constants.py:84`):

```python
["imports", "exports", "notifications", "provisioning", "reports", "celery"]
```

`KIND_TO_QUEUE` (`tasks.py:20-27`) maps a `BackgroundJob.kind` onto one of those
names, with everything unmapped falling through to `celery`:

```text
import → imports    export → exports    email → notifications
notification → notifications             provision → provisioning
report → reports    (anything else) → celery
```

### `core.BackgroundJob` - read, never written here

The task table's source (`core/models.py:31-102`). The fields this slice reads:

| Field | Used for |
|---|---|
| `kind`, `status` | The one-minute aggregate and the table's filters |
| `tenant` (FK, PROTECT) | The `tenant` column, rendered as a name |
| `task_name`, `label`, `worker` | Table columns |
| `created_at`, `started_at`, `finished_at` | Table columns and the computed duration |

`BackgroundJob` has **no custom manager**, so `BackgroundJob.objects` is a plain
manager and the task table sees every tenant's jobs. That is intended here - the
gate is a PLATFORM-scoped key - and it is worth stating because most models on
this platform are tenant-aware.

The three fields this slice deliberately does **not** expose are `result`,
`error` and `traceback`.

## 3. Endpoint map

| Method + path | Permission | View | Paginated |
|---|---|---|---|
| `GET /queues/` | `platform.health.view` | `QueuesView` (`views.py:223-227`) | no |
| `GET /tasks/` | `platform.health.view` | `TaskListView` (`views.py:231-258`) | yes - `XVSPagination`, page 25 |

Both inherit `HealthViewMixin` (`views.py:49-52`), so `?tenant=<slug>` is
mandatory.

### Query parameters actually read

`GET /queues/` reads none.

`GET /tasks/` reads four (`views.py:243-257`):

| Parameter | Behaviour |
|---|---|
| `status` | Upper-cased and matched exactly against `BackgroundJob.Status` |
| `tenant` | **Fed to `tenant_id` as-is.** A slug here is a `ValueError` and therefore a 500 |
| `kind` | Exact match on `BackgroundJob.kind` |
| `queue` | Reverse-mapped through `KIND_TO_QUEUE`; an unknown queue name deliberately matches nothing (`filter(kind="__none__")`) |

The `queue` reverse map is the neat part: it collects every `kind` whose mapped
queue equals the requested name, so `?queue=notifications` matches both `email`
and `notification` kinds.

### Response shapes

`GET /queues/` returns `queue_overview()` directly (`services.py:525-529`):

```jsonc
{"queues": [
   {"name": "celery", "depth": 3, "status": "healthy",
    "throughput_per_min": 2.0, "failed": 0, "retrying": 1, "dead": 0,
    "retry_storm": false, "avg_duration_sec": null,
    "depth_trend": [0, 1, 4, 3, ...up to 40 points, oldest first],
    "captured_at": "2026-08-20T09:19:00Z"},
   ...],
 "workers": {"active": 2, "idle": 6, "total": 8}}
```

`workers` is the **maximum** across the queues' latest snapshots
(`services.py:515-516`), not a sum - every snapshot in a tick carries the same
cluster-wide numbers, so a max is the right way to collapse them.

`GET /tasks/` returns `TaskRowSerializer` rows in the pagination envelope
(`serializers.py:135-157`):

```jsonc
{"id": "…", "task_name": "vs_exports.tasks.run_export",
 "label": "Export: Invoices (August)", "kind": "export", "queue": "exports",
 "status": "SUCCEEDED", "tenant": "Corona Secondary School",
 "duration_sec": 42.7, "worker": "celery@srv-1",
 "created_at": "…", "started_at": "…", "finished_at": "…"}
```

`queue` is derived per row through `KIND_TO_QUEUE`
(`serializers.py:150-152`); `tenant` is the tenant's **name**, not its id
(`serializers.py:154-155`); `duration_sec` is `finished_at - started_at` rounded
to one decimal, or null when either is missing
(`serializers.py:157-160`).

## 4. Lifecycle / state machine

There is no state machine here - a snapshot is written and never changes. The
cycle, every minute (`apps/celery.py:133-136`):

```text
capture_queue_snapshot_task
   │
   ├─ _broker_depths()            LLEN per KNOWN_QUEUES name, {} if the broker
   │                              is not redis or is unreachable
   ├─ _worker_counts()            Celery inspect, 2s timeout, (0,0) on any failure
   │
   ├─ aggregate BackgroundJobs created in the trailing 60 seconds,
   │  bucketed by KIND_TO_QUEUE
   │
   ├─ write one QueueSnapshot per KNOWN_QUEUES name  (always six rows)
   │
   └─ set the celery service card
         workers_active + workers_idle > 0   →  HEALTHY
         no workers but depths were readable →  CRITICAL
         depths empty (broker unreachable)   →  UNKNOWN
```

and, daily at 03:00, `prune_health_metrics_task` deletes snapshots older than
three days (`tasks.py:407-408`).

Both helper functions are written to fail soft: `_broker_depths` returns `{}` on
any exception with a DEBUG log (`tasks.py:132-134`), and `_worker_counts`
returns `(0, 0)` (`tasks.py:148-149`). Neither can stop the snapshot from being
written, which is right - a snapshot with zeros and a known-bad broker is more
useful than a missing minute.

## 5. Derivations

- **Depth** (`tasks.py:122-134`). `LLEN <queue_name>` on the broker Redis, for
  every name in `KNOWN_QUEUES`. Non-Redis brokers return `{}` and every depth
  falls back to `0` (`tasks.py:179`), which is indistinguishable from an empty
  queue.

- **Worker counts** (`tasks.py:138-149`):

  ```text
  total = sum of every worker's pool max-concurrency   (inspect.stats())
  busy  = sum of len(active task list) per worker      (inspect.active())
  active = busy
  idle   = max(0, total - busy)
  ```

  A two-second inspect timeout, and any failure - including no workers at all -
  collapses to `(0, 0)`.

- **The one-minute job aggregate** (`tasks.py:161-175`):

  ```python
  recent = BackgroundJob.objects.filter(created_at__gte=window_start)
  ```

  bucketed by `KIND_TO_QUEUE`, counting SUCCEEDED into `throughput`, FAILED into
  `failed`, RUNNING into `running`. Note the window is on **`created_at`**: a job
  that started four minutes ago and finished in this minute is in no tick's
  window at all.

- **The three assignments that do not match their labels**
  (`tasks.py:181-183`, `:192-196`):

  ```text
  throughput_per_min = jobs CREATED in the last minute that are now SUCCEEDED
  retrying           = jobs CREATED in the last minute that are now RUNNING
  dead               = 0, always
  retry_storm        = failed >= 50   (a failure count, not a retry measure)
  ```

- **Queue status** (`tasks.py:184-190`):

  | Condition | Status |
  |---|---|
  | `depth >= 5000` or `retry_storm` | CRITICAL |
  | `depth >= 2000` or `failed >= 10` | WARNING |
  | otherwise | HEALTHY |

  All four thresholds are literals in the task body, not configuration.

- **The depth trend** (`services.py:511-514`). The newest 40 snapshots for the
  queue, depth only, reversed to oldest-first for the chart. At one snapshot a
  minute that is a 40-minute window, whatever range the rest of the screen is
  showing - the queue card does not honour `?range=`.

- **Worker totals** (`services.py:515-516`). `max()` across queues rather than
  `sum()`, because every snapshot in a tick carries the same cluster figures.

- **Task queue label** (`serializers.py:150-152`). `KIND_TO_QUEUE.get(kind.lower(),
  "celery")` - so an unmapped kind is labelled `celery`, which happens to be
  true today for every kind, mapped or not.

- **Task duration** (`serializers.py:157-160`). `finished_at - started_at`, so a
  queued-but-not-started job and a running job both report null rather than
  their age.

## 6. What writing writes

| Action | Written by | Rows |
|---|---|---|
| Capture a tick | `capture_queue_snapshot_task` (`tasks.py:191-198`) | six `QueueSnapshot` rows, one per known queue name |
| Set the async service card | same (`tasks.py:203-212`) | `MonitoredService.current_status` for `celery`, only on change |
| Retention | `prune_health_metrics_task` (`tasks.py:407-408`) | deletes snapshots older than 3 days |

Both endpoints in this slice are reads and write nothing at all - no audit row,
no access log, no last-viewed stamp.

**Nothing in this slice writes a `BackgroundJob`.** The task table is strictly a
reader of another app's table, which is the right boundary: `core.tasks_base`
owns that lifecycle.

## 7. Worked example

At 09:19 the snapshot task runs on a system where an export is halfway through.

`_broker_depths` returns `{"imports": 0, "exports": 0, "notifications": 0,
"provisioning": 0, "reports": 0, "celery": 3}` - five zeroes because nothing
routes tasks to those queues, and `3` because the default queue really does have
three messages waiting.

`_worker_counts` returns `(2, 6)`.

The trailing-minute aggregate finds one `BackgroundJob` created in the last
sixty seconds, kind `email`, status SUCCEEDED. Six rows are written:

```text
imports        depth=0  throughput=0  failed=0  retrying=0  status=healthy
exports        depth=0  throughput=0  failed=0  retrying=0  status=healthy
notifications  depth=0  throughput=1  failed=0  retrying=0  status=healthy
provisioning   depth=0  throughput=0  failed=0  retrying=0  status=healthy
reports        depth=0  throughput=0  failed=0  retrying=0  status=healthy
celery         depth=3  throughput=0  failed=0  retrying=0  status=healthy
```

Workers are present, so the `celery` service card is set HEALTHY.

```text
GET /v1/health/queues/?tenant=codex
```

returns all six cards. Five of them describe queues that no producer has ever
written to (`health_code_issues.md` §10), and the export that has been running
for four minutes appears in none of them: it was created before the window, so
it is not counted as throughput, and it will not be counted when it finishes
either.

Now the task table, which is the screen an operator actually wants:

```text
GET /v1/health/tasks/?tenant=codex&status=failed
```

```text
500  ValueError: Field 'id' expected a number but got 'codex'.
```

`?tenant=` is mandatory - the auth layer requires it and rejects any slug but
the caller's own - and `TaskListView` passes that slug straight into
`filter(tenant_id=...)` against a numeric primary key
(`views.py:247-249`). The full matrix, verified against a real JWT:

```text
GET /v1/health/tasks/                  -> 400   "A 'tenant' query parameter is required."
GET /v1/health/tasks/?tenant=codex     -> 500   ValueError
GET /v1/health/tasks/?tenant=all       -> 404   "No tenant matches the requested context."
GET /v1/health/tasks/?tenant=1         -> 404   "No tenant matches the requested context."
```

There is no request that reaches the table. The other health screens survive the
same collision only because `_tenant_id` swallows it
(`views.py:72-79`); this one does not go through `_tenant_id` at all.

## 8. Gotchas / known limitations

Full evidence for each is in **`error/health/health_code_issues.md`**. The items
belonging to this slice:

- **`/v1/health/tasks/` 500s on the only `?tenant=` value the auth layer
  accepts**, so the Background Jobs table is unreachable by any request
  (`views.py:247-249`, issues §1). Confirmed by execution against a real JWT.
- **Five of the six monitored queues do not exist.** There is no `task_routes`
  anywhere in the repo, so every task runs on `celery` and the other five names
  are permanently zero - as is the seeded "Notifications backlog" alert rule,
  which can therefore never fire (`constants.py:84`, `seed.py:146`, issues §10).
- **Throughput, retrying, dead and retry_storm each measure something other
  than their label** (`tasks.py:165`, `:182`, `:183`, `:194`): throughput windows
  on `created_at` so a long job is never counted, `retrying` is really "running",
  `dead` is a hardcoded zero, and `retry_storm` is a failure count (issues §11).
- **`avg_duration_sec` is a column nothing writes**
  (`models/queues.py:29`), so it is null on every snapshot and null on every
  queue card.
- **`GET /queues/` is unpaginated and ignores `?range=`.** The depth trend is
  always the last 40 snapshots regardless of the window the rest of the screen is
  showing (`services.py:511-514`).
- **`queue_overview` issues two queries per queue name** - one for the latest
  snapshot, one for the trend (`services.py:507-514`) - so twelve queries for six
  cards, on a payload the Command Center already embeds
  (`views.py:117`) (issues §18).
- **A non-Redis or unreachable broker is indistinguishable from an empty
  queue.** `_broker_depths` returns `{}` and every depth is written as `0`
  (`tasks.py:179`), with the failure visible only in a DEBUG log. The `celery`
  service card does encode the difference (`tasks.py:209-212`); the queue cards
  do not.
- **Six known-shape rows are written every minute regardless of need** -
  8,640 rows a day, of which 7,200 describe queues that do not exist
  (issues §10).
- **`?status=` on the task table is upper-cased and matched exactly**
  (`views.py:245-247`), so a value that is not a `BackgroundJob.Status` member
  silently returns an empty page rather than a 400.
- **Justified by design:** the task table reads `core.BackgroundJob` rather than
  duplicating it (`models/queues.py:3-7`). Every tracked task already writes
  there; a second copy would drift.
- **Justified by design:** a queue with no snapshot is omitted rather than shown
  as healthy (`services.py:507-510`). This is exactly the discipline the uptime
  aggregation lacks.
- **Justified by design:** `result`, `error` and `traceback` are not exposed
  (`serializers.py:135-160`), unlike the console's task monitor. A health screen
  needs to know that a job failed, not what was inside it.
- **Justified by design:** an unknown `?queue=` matches nothing explicitly
  (`views.py:256-257`) rather than falling through to an unfiltered list.
- **Justified by design:** `_broker_depths` and `_worker_counts` both fail soft,
  so a broker problem cannot stop the snapshot from being written.

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Scope | Seeded to |
|---|---|---|---|---|
| `GET /queues/` | `platform.health.view` | NORMAL | PLATFORM | nobody but `xvs_super_admin` |
| `GET /tasks/` | `platform.health.view` | NORMAL | PLATFORM | nobody but `xvs_super_admin` |

`QueueSnapshot` has no tenant column and needs none - a queue is
platform-wide.

`BackgroundJob` **does** have one, and `TaskListView` deliberately does not
scope by it: `BackgroundJob.objects.select_related("tenant").all()`
(`views.py:242`) returns every tenant's jobs, and the `tenant` column on the
table is the point of the screen. The only thing keeping that safe is that
`platform.health.view` is `PermissionScope.PLATFORM` (`seed.py:71`) and
therefore cannot be attached to a school role
(`vs_rbac/models.py:91-110`). That boundary is declared correctly here, which is
not true of every cross-tenant screen on this platform.

Worth contrasting with the console's own task monitor, which shows the same
underlying rows: that one is gated on Django's `is_staff` flag - a flag every CX
staff account carries by construction - and exposes `result`, `error` and
`traceback`. This screen is the stricter of the two on both counts.

`?tenant=` is mandatory and, on `/tasks/`, fatal (§8).

## 10. Code map

| File | Responsibility |
|---|---|
| `models/queues.py:18-42` | `QueueSnapshot` |
| `constants.py:84` | `KNOWN_QUEUES` |
| `tasks.py:20-27` | `KIND_TO_QUEUE` |
| `tasks.py:122-134` | `_broker_depths` - Redis `LLEN`, fails soft |
| `tasks.py:138-149` | `_worker_counts` - Celery inspect, fails soft |
| `tasks.py:153-213` | `capture_queue_snapshot_task` - the aggregate, the six rows, the celery card |
| `tasks.py:407-408` | Snapshot retention, 3 days |
| `services.py:499-529` | `queue_overview` - latest per queue, trend, worker totals |
| `views.py:223-227` | `QueuesView` |
| `views.py:231-258` | `TaskListView` and its four filters |
| `serializers.py:135-160` | `TaskRowSerializer` |
| `core/models.py:31-102` | `BackgroundJob` - the table this slice reads |
| `apps/celery.py:133-136` | The per-minute snapshot beat entry |

## 11. Test coverage & gaps

Baseline: **`Ran 27 tests in 2.139s` - OK**.

What this slice covers:

**Nothing.** Not one of the 27 tests touches `QueueSnapshot`,
`capture_queue_snapshot_task`, `_broker_depths`, `_worker_counts`,
`queue_overview`, `KIND_TO_QUEUE`, `QueuesView`, `TaskListView` or
`TaskRowSerializer`. The one API test in the module asserts that
`GET /overview/` returns a `queues` key (`tests.py:445-446`) and nothing about
what is inside it.

This is the least-tested slice in `vs_health` and it contains the module's only
Critical defect, which is not a coincidence: a single request test against
`/v1/health/tasks/` with a realistic `?tenant=` would have caught issue §1 the
day it was written.

What is needed, in order:

1. **A request test for `/v1/health/tasks/`** using a real JWT (not
   `force_authenticate`, which skips the tenant assertion entirely and is why
   the existing API test cannot see the bug), asserting 200 and the row shape
   for each of the four filters.
2. **A request test for `/v1/health/queues/`** asserting the envelope, the
   `workers` block and that a queue with no snapshot is absent rather than
   present-and-healthy.
3. **`capture_queue_snapshot_task` with the broker mocked**: depths present,
   depths empty, and no workers - the three branches that set the `celery`
   service card (`tasks.py:203-212`), none of which is exercised today.
4. **The one-minute aggregate**, with jobs of several kinds in several statuses,
   asserting which queue each lands in and - once issue §11 is fixed - that a
   long-running job is counted as throughput in the minute it finishes.
5. **`KIND_TO_QUEUE` round-tripping**: `?queue=notifications` must match both
   `email` and `notification` kinds, and an unknown queue name must return an
   empty page rather than everything.
