# health_uptime_availability

The half of `vs_health` that answers "is it up": the service registry an
operator configures, the recurring probes that test each service, the raw
results and the daily rollups that compress them, the SLO targets computed on
top, and the two screens they feed - the Command Center's service grid and
posture banner, and the Uptime & Availability page.

Routes covered by this slice, mounted at `/v1/health/` (`apps/urls.py:39`):
`services/`, `services/<key>/`, `uptime/monitors/`, `uptime/monitors/<key>/`,
`slos/`.

Request metrics and the golden signals are `health_signal_collection`; alerts
and incidents are `health_incidents_alerts`; queues are `health_queues_jobs`.

---

## 1. What it is (and what it is NOT)

- **A service is a card, not a process.** `MonitoredService` is a registry row
  an operator configures (`models/registry.py:24-60`): a key, a display name, a
  group, a tier, a kind, and a denormalised `current_status` kept fresh by the
  probe task so the grid renders without recomputing anything.
- **Three of the twelve seeded services cannot be probed at all.** `schools`,
  `billing` and `reports` are *route groups of the monolith*, not separate
  processes. Their status is derived from real request metrics on their route
  prefixes (`constants.py:86-93`, `tasks.py:73-114`), which is the honest answer
  and is documented as such in both places.
- **A check is configuration; a result is evidence.** `UptimeCheck` holds the
  target, the type and the thresholds (`models/uptime.py:29-48`);
  `UptimeCheckResult` holds one execution (`models/uptime.py:51-72`). Results are
  pruned at seven days and the daily rollup carries the long view.
- **A probe never raises.** Every `run_*` in `probes.py` wraps its work and
  converts any exception into a CRITICAL result with the message captured
  (`probes.py:60-62`, `:76-78`, `:116-118`, `:133-135`, `:163-165`). A probe that
  cannot run returns UNKNOWN with a reason, not a failure
  (`probes.py:41-42`, `:86-91`, `:186-187`).
- **UNKNOWN is a first-class answer.** "Broker is not redis", "requests not
  installed", "no probe for internal check" all produce UNKNOWN rather than a
  claimed failure. The module is careful about this in the probe layer and much
  less careful about it in the aggregation layer (§8).
- **`interval_sec` is configuration nothing reads.** Every active check runs on
  every task tick (`tasks.py:43`), five minutes apart
  (`apps/celery.py:129-132`), regardless of what the field says.
- **Uptime is measured, availability is inferred, and the two are conflated.**
  The daily rollup counts a WARNING result as a failed check
  (`tasks.py:376-377`), so "slow" and "down" produce the same uptime number.
- **This is not external monitoring.** The probes execute inside the platform's
  own Celery worker against the platform's own public URL. If the platform is
  down, so is the thing that would notice.
- **Seeding writes configuration only.** `seed.py:1-11` states it outright and
  the code holds to it: services, checks, alert rules, SLO targets and
  permissions, and not one row of telemetry. Screens are honestly empty until
  real probes have run. That is the right decision and it is unusual enough to
  be worth naming.

## 2. Domain model

### `MonitoredService` (`models/registry.py:24`)

| Field | Meaning |
|---|---|
| `id` | UUID primary key |
| `key` | Stable slug, unique, max 40 - `api`, `redis`, `dns` |
| `name`, `group`, `tier` | Display only - "API · DRF", "Core", "Tier 1" |
| `kind` | `internal` / `datastore` / `external` (`models/registry.py:18-21`) |
| `is_active` | Retired services are deactivated, never deleted (`seed.py:88-93`) |
| `sort_order` | Grid ordering, 10-120 in the seed |
| `current_status` | Denormalised cache, indexed, default UNKNOWN |
| `status_changed_at` | Stamped only when the status actually changes |
| `config` | Free-form JSON, written and read by nothing today |

`set_status` (`models/registry.py:55-60`) writes only on a real transition,
which keeps the beat task cheap and is also why a frozen card is
indistinguishable from a fresh one (§8).

The twelve seeded services (`seed.py:22-37`):

| Key | Name | Group | Tier | Kind | How its status is set |
|---|---|---|---|---|---|
| `web` | Web Frontend | Edge | 1 | internal | HTTP probe against `FRONTEND_BASE_URL` |
| `api` | API · DRF | Core | 1 | internal | HTTP probe against `{PROBE_BASE}/v1/` |
| `auth` | Auth / JWT | Core | 1 | internal | HTTP probe against `{PROBE_BASE}/v1/user/` |
| `schools` | Schools & Onboarding | Modules | 2 | internal | derived from `/v1/i/` request metrics |
| `billing` | Billing & Fees | Modules | 2 | internal | derived from `/v1/finance/` + `/v1/payments/` |
| `reports` | Report Engine | Modules | 3 | internal | derived from `/v1/finance/reports/` |
| `celery` | Celery Workers | Async | 2 | internal | set by the queue snapshot task |
| `postgres` | PostgreSQL | Data | 1 | datastore | `SELECT 1` probe |
| `redis` | Redis | Data | 1 | datastore | `PING` + memory probe |
| `smtp` | Zoho SMTP | External | Ext | external | TCP connect to `EMAIL_HOST:EMAIL_PORT` |
| `payments` | Payment Gateway | External | Ext | external | HTTP probe against `{PROBE_BASE}/v1/payments/` |
| `dns` | DNS / SSL | External | Ext | external | TLS certificate expiry probe |

### `UptimeCheck` (`models/uptime.py:29`)

| Field | Meaning |
|---|---|
| `service` | FK, CASCADE |
| `name` | Unique-per-service in practice; the seeder upserts on `(service, name)` |
| `check_type` | `http` / `tcp` / `redis` / `postgres` / `ssl` / `internal` |
| `target` | URL, `host:port`, or domain - empty for redis/postgres |
| `interval_sec` | **Ignored** (`tasks.py:43`) |
| `region` | Written by nothing |
| `expected` | Per-check tuning: `{"status": 200, "warn_ms": 800, "timeout": 10}` |
| `is_active` | Only active checks run |

### `UptimeCheckResult` (`models/uptime.py:51`)

One row per execution: `status`, `response_ms`, `status_code`, `error`, and a
`meta` JSON blob that carries `ssl_days_left` / `domain` / `expires_at` from the
SSL probe (`probes.py:157`) and `used_memory` / `maxmemory` / `mem_pct` from the
Redis probe (`probes.py:102-106`). `mem_pct` is the platform's only saturation
signal.

Indexed on `(service, -checked_at)` and `(uptime_check, -checked_at)`
(`models/uptime.py:66-69`). Pruned at seven days (`tasks.py:405-406`).

### `UptimeDailyRollup` (`models/uptime.py:75`)

One row per `(service, day)`, unique together (`models/uptime.py:88`):
`uptime_pct`, `worst_status`, `total_checks`, `failed_checks`,
`avg_response_ms`. Never pruned - this is the durable record and the reason the
90-day bar survives the seven-day retention on raw results.

### `SLO` (`models/registry.py:86`)

`service` FK, a `name` (unique per service), `target_pct` (default 99.900),
`window_days` (default 30), `is_active`. Attainment and error budget are
computed live, never stored.

Four seeded objectives (`seed.py:184`): `api` 99.9%, `auth` 99.95%,
`payments` 99.5%, `reports` 99.0%, all over 30 days.

## 3. Endpoint map

All five routes inherit `HealthViewMixin` (`views.py:49-52`): read-only,
`platform.health.view`, `?tenant=<slug>` mandatory.

| Method + path | Permission | Notes |
|---|---|---|
| `GET /services/` | `platform.health.view` | Worst-first grid, active services only |
| `GET /services/<slug:key>/` | `platform.health.view` | 404 on unknown key; **does not filter `is_active`** |
| `GET /uptime/monitors/` | `platform.health.view` | Every active service, 90-day window |
| `GET /uptime/monitors/<slug:key>/` | `platform.health.view` | Computes the whole grid, then picks one |
| `GET /slos/` | `platform.health.view` | Every active SLO |

None of these read a query parameter beyond the mandatory `?tenant=`. None of
them are paginated.

### Response shapes

Hand-built dicts, not serializers. One grid card (`services.py:453-457`):

```jsonc
{"key": "redis", "name": "Redis", "group": "Data", "tier": "Tier 1",
 "kind": "datastore", "status": "healthy",
 "status_changed_at": "2026-08-19T22:04:11Z"}
```

One uptime monitor (`services.py:563-573`):

```jsonc
{"key": "api", "name": "API · DRF", "status": "warning",
 "uptime_24h": 99.6528, "uptime_7d": 99.8214, "uptime_30d": 99.9012, "uptime_90d": 99.9104,
 "segments": [{"day": "2026-05-23", "status": "healthy", "uptime": 100.0}, ...],
 "response_series": [{"t": "2026-08-20T09:15:00Z", "ms": 214.6}, ...],
 "avg_response_ms": 228.4,
 "ssl": {"days_left": 61, "domain": "api.codexng.com"}}
```

`ssl` is `null` for every service without an SSL check
(`services.py:571-572`). `segments` is capped by the 90-day window, not by
count. `response_series` is the last 48 raw results, oldest first, skipping any
with a null `response_ms` (`services.py:548-550`).

One SLO row (`services.py:598-604`):

```jsonc
{"service": "API · DRF", "service_key": "api", "target": 99.9,
 "current": 99.9012, "window_days": 30,
 "error_budget_remaining": 98.8, "breached": false}
```

`ServiceDetailView` (`views.py:137-154`) returns the card fields plus the whole
monitor payload under `uptime` and the last ten alerts under `recent_alerts`.

## 4. Lifecycle / state machine

A service's status has four values and three separate writers.

```text
                    ┌─ run_uptime_checks_task  (every 5 min)
                    │     probe each active check → UptimeCheckResult
                    │     worst_status(latest result per check) → set_status
                    │
current_status ─────┼─ refresh_module_service_statuses  (same task, at the end)
                    │     schools/billing/reports only
                    │     window_status(requests, error_rate, p95) → set_status
                    │
                    └─ capture_queue_snapshot_task  (every minute)
                          celery only
                          workers present → HEALTHY
                          no workers, broker reachable → CRITICAL
                          broker unreachable → UNKNOWN
```

The three writers do not overlap: probe-driven services have checks, module
services appear in `ROUTE_PREFIX_SERVICES`, and `celery` has neither.

The result-to-rollup pipeline:

```text
UptimeCheckResult   (written every 5 min per check)
        │
        ├─ rollup_uptime_daily_task  (hourly at :15, days_back=2)
        │     update_or_create one UptimeDailyRollup per (service, day)
        │     skips a day with zero results entirely - no synthetic row
        │
        └─ prune_health_metrics_task (03:00 daily)
              raw results older than 7 days deleted
```

`days_back = 2` means today plus the two previous days are recomputed on every
hourly run, so a late-arriving result cannot leave a stale rollup behind.

## 5. Derivations

- **Service status from probes** (`tasks.py:55-63`). For each service touched
  this run, take the **latest result of each active check** and collapse with
  `worst_status`. One failing check out of three turns the card. A check with no
  results at all contributes nothing rather than UNKNOWN.

- **Module service status from traffic** (`tasks.py:73-114`). Build a
  `route__startswith` disjunction from the service's prefixes, sum
  `request_count` and `status_5xx` over the trailing 15 minutes, merge the
  histograms, and hand the three numbers to `window_status`. Below 30 requests
  the answer is UNKNOWN - never a claimed green, never a red produced by one
  slow report.

  ```text
  schools  →  /v1/i/
  billing  →  /v1/finance/  +  /v1/payments/
  reports  →  /v1/finance/reports/
  ```

  `reports` is a strict subset of `billing`, so a report failure degrades both.

- **HTTP probe classification** (`probes.py:37-62`):

  ```text
  code >= 500                     → CRITICAL
  code != expected and code >= 400 → WARNING
  elapsed > warn_ms                → WARNING
  otherwise                        → HEALTHY
  exception                        → CRITICAL, message truncated to 500 chars
  ```

  `expected` defaults to `{"status": 200, "warn_ms": 800, "timeout": 10}`
  (`probes.py:44-47`).

- **Redis probe** (`probes.py:82-118`). PING first; then `INFO memory` in a
  nested `try` so an INFO failure cannot turn a successful ping into an outage.
  `mem_pct = used_memory / maxmemory * 100`, CRITICAL at 95%, WARNING at 85%,
  and WARNING on a ping slower than `warn_ms` (default 50ms).

- **SSL probe** (`probes.py:139-165`). Open a TLS connection, read `notAfter`,
  parse `"%b %d %H:%M:%S %Y %Z"`, compute whole days to expiry. CRITICAL at
  ≤ `critical_days` (5), WARNING at ≤ `warn_days` (14).

- **Daily uptime** (`tasks.py:370-389`):

  ```text
  total   = results in the day
  failed  = results whose status is CRITICAL *or WARNING*
  uptime  = (total - failed) / total * 100      rounded to 4dp
  worst   = worst_status(every status that day)
  avg_ms  = mean response_ms, excluding nulls
  ```

  A day with no results writes no row at all, which is why an unprobed service
  has empty `segments` rather than a row of zeroes.

- **Window uptime** (`services.py:552-556`). The mean of the daily
  `uptime_pct` values inside the window - an unweighted mean of days, not of
  checks, so a day with 12 results counts as much as a day with 288.
  **Returns `100.0` when the window is empty.**

- **Global uptime** (`services.py:482-491`). `Avg("uptime_pct")` across every
  service over 30 days, and `None` when there are no rollups - the one place in
  the module that refuses to invent a figure, with the reasoning in its own
  docstring.

- **SLO attainment and error budget** (`services.py:582-604`):

  ```text
  current  = mean daily uptime_pct over window_days     (100.0 if no data)
  allowed  = 100 - target                                # the downtime budget
  used     = max(0, 100 - current)
  budget   = max(0, (allowed - used) / allowed * 100)    # % of budget left
  breached = current < target
  ```

  At a 99.9% target the budget is 0.1 percentage points, so 0.05% downtime
  leaves 50% of the budget. The arithmetic is right; its input is not (§8).

- **Grid ordering** (`services.py:451-458`). CRITICAL, WARNING, UNKNOWN,
  HEALTHY - deliberately putting "no signal" above "fine", which is the correct
  priority for an operator.

- **Posture** (`services.py:463-479`):

  ```text
  any critical → "critical",   "N service(s) down"
  any warning  → "warning",    "N service(s) degraded"
  otherwise    → "operational", "All systems operational"
  ```

  UNKNOWN falls into the third branch.

## 6. What writing writes

| Action | Written by | Rows |
|---|---|---|
| Run one probe | `run_uptime_checks_task` (`tasks.py:45-51`) | one `UptimeCheckResult` |
| Refresh a probed service | `run_uptime_checks_task` (`tasks.py:56-63`) | `MonitoredService.current_status` + `status_changed_at`, only on change |
| Refresh a module service | `refresh_module_service_statuses` (`tasks.py:112`) | same |
| Roll a day up | `rollup_uptime_daily_task` (`tasks.py:381-389`) | one `UptimeDailyRollup`, upserted |
| Retention | `prune_health_metrics_task` (`tasks.py:405-406`) | deletes raw results past 7 days |
| Seed | `seed_services` / `seed_checks` / `seed_slos` (`seed.py:80-190`) | registry rows only, never telemetry |

Every endpoint in this slice is a read. Nothing here writes an audit event and
nothing here is user-authored, so that omission is correct for this slice
(it is not correct for incidents and rules - see `health_incidents_alerts`).

Re-seeding is a repair, not a no-op: `seed_checks` uses `update_or_create` on
`(service, name)` specifically so a stale target - the former
`api.codexvision.io` SSL domain - is corrected rather than preserved forever
(`seed.py:106-113`), and `seed_services` deactivates any registry row no longer
in the list (`seed.py:88-93`). Both behaviours are tested.

## 7. Worked example

The beat task fires at 09:15. Eight active checks run serially.

```text
Postgres SELECT 1      →  4.1ms    HEALTHY
Redis ping             →  1.8ms    HEALTHY   meta={"used_memory":..., "mem_pct": 61.3}
SSL certificate        →  88ms     HEALTHY   meta={"ssl_days_left": 61, "domain": "api.codexng.com"}
SMTP reachability      →  42ms     HEALTHY
Web frontend           →  180ms    HEALTHY
API health             →  96ms     WARNING   status_code=404, error="HTTP 404"
Auth endpoint          →  88ms     WARNING   status_code=401, error="HTTP 401"
Payments gateway       →  91ms     WARNING   status_code=404, error="HTTP 404"
```

The last three are not failures of the platform; they are the seeded probes
pointing at paths that do not resolve or that refuse anonymous callers
(`health_code_issues.md` §7). Each service takes the worst status across its own
checks, so `api`, `auth` and `payments` are set to WARNING.

`refresh_module_service_statuses` then runs. Traffic in the trailing 15 minutes
is 24 requests - below `MIN_P95_SAMPLE` - so `schools`, `billing` and `reports`
are all set to UNKNOWN, correctly.

```text
GET /v1/health/services/?tenant=codex
```

```jsonc
{"success": true, "message": "Services retrieved successfully.",
 "data": {"services": [
   {"key": "api",      "name": "API · DRF",           "status": "warning", ...},
   {"key": "auth",     "name": "Auth / JWT",          "status": "warning", ...},
   {"key": "payments", "name": "Payment Gateway",     "status": "warning", ...},
   {"key": "schools",  "name": "Schools & Onboarding","status": "unknown", ...},
   {"key": "billing",  "name": "Billing & Fees",      "status": "unknown", ...},
   {"key": "reports",  "name": "Report Engine",       "status": "unknown", ...},
   {"key": "web",      "name": "Web Frontend",        "status": "healthy", ...},
   ...]}}
```

And the Command Center banner, from `overall_posture`, reads
**"3 services degraded"** - on a platform where nothing is wrong.

At 10:15 the rollup runs. `api` had 12 results that hour and every one was
WARNING, so the day's rollup records `failed_checks` equal to `total_checks` and
`uptime_pct = 0.0000` (`tasks.py:376-378`). The `api` SLO then reports:

```jsonc
{"service": "API · DRF", "service_key": "api", "target": 99.9,
 "current": 0.0, "window_days": 30,
 "error_budget_remaining": 0.0, "breached": true}
```

and the seeded `UPTIME_PCT < 99.5` rule opens a SEV2 incident that can never
resolve. Three lines of seed configuration produce a permanently red SLO, a
permanently yellow banner and a permanently open incident.

Contrast the honest path: a brand-new service with no checks at all has no
rollups, so its `segments` array is empty - and its `uptime_90d` still reads
`100.0` (§8).

## 8. Gotchas / known limitations

Full evidence for each is in **`error/health/health_code_issues.md`**. The items
belonging to this slice:

- **Three seeded probes point at URLs that do not exist or refuse anonymous
  callers**, so `api`, `auth` and `payments` sit at WARNING forever and the
  platform posture is permanently "3 services degraded"
  (`seed.py:120-125`, issues §7). Confirmed by URL resolution: `/v1/` and
  `/v1/payments/` do not resolve, and `/v1/user/` is `IsAuthenticated`.
- **A WARNING probe counts as downtime** in the daily rollup
  (`tasks.py:376-377`), so a Redis ping at 55ms against a 50ms threshold reads
  as an outage, breaches the SLO and opens an incident (issues §8).
- **An empty window claims 100% uptime.** `_window` returns `100.0` with no
  rollups (`services.py:552-556`) and `slo_status` returns `current = 100.0`
  with no data (`services.py:591`), both contradicting `global_uptime`'s own
  stated principle three functions above them (issues §9).
- **UNKNOWN never reaches the posture banner.** `overall_posture` counts only
  CRITICAL and WARNING (`services.py:468-475`), so a platform where every module
  service is UNKNOWN reports "All systems operational" (issues §9).
- **`interval_sec` is ignored** (`tasks.py:43`): the hourly SSL check runs 288
  times a day, and lengthening any interval has no effect and produces no error
  (issues §12).
- **One monitor costs the whole monitor grid.** `ServiceDetailView`
  (`views.py:145`) and `UptimeMonitorDetailView` (`views.py:175`) both call
  `uptime_monitors()`, which issues three queries per active service and fetches
  90 days of rollups, then discard all but one (issues §17).
- **`ServiceDetailView` does not filter `is_active`** (`views.py:141`), so a
  service `seed_services` deliberately retired - "so the console never shows
  unmonitorable services" (`seed.py:88-89`) - is still fully readable by key
  (issues §24.1).
- **The probes run inside the process they monitor**, and `set_status` writes
  only on change with no `last_checked_at` anywhere
  (`models/registry.py:55-60`), so a total outage records nothing and the grid
  shows stale statuses as current (issues §21).
- **`FRONTEND_BASE_URL` defaults to `http://localhost:3000`**
  (`apps/settings/base.py:305`), so an unset environment variable points the
  Tier 1 `web` probe at localhost from inside the API container (issues §7).
- **`region` and `config` are written by nothing**
  (`models/uptime.py:40`, `models/registry.py:44`).
- **School vocabulary in an engine app**: the `schools` service key and the
  "Schools & Onboarding" display name (`constants.py:90`, `seed.py:28`) put a
  product's word into a domain-neutral engine (issues §22).
- **Justified by design:** a day with zero results writes no rollup row
  (`tasks.py:373-375`), so an unprobed day is absent rather than reported as
  100% or 0%. This is the correct behaviour and it is exactly what `_window`
  then undoes.
- **Justified by design:** module services are derived from request metrics
  rather than probed. Nothing can probe a route group of a monolith
  independently, and the code says so in two places.
- **Justified by design:** probes never raise; every failure becomes a result
  row. A probe that crashed the beat task would take the other seven with it.
- **Justified by design:** re-seeding repairs stale targets and thresholds
  rather than preserving them, while leaving an operator's `is_enabled` toggle
  alone (`seed.py:106-113`, `:150-176`).

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Scope | Seeded to |
|---|---|---|---|---|
| Every route in this slice | `platform.health.view` | NORMAL | PLATFORM | nobody but `xvs_super_admin` |

Nothing in this slice is tenant-scoped, and nothing should be: a service
registry, a probe result and an SLO are facts about the platform, not about a
customer. There is no `tenant` column on `MonitoredService`, `UptimeCheck`,
`UptimeCheckResult`, `UptimeDailyRollup` or `SLO`.

The boundary that matters is therefore the key itself, and it is declared
correctly: `PermissionScope.PLATFORM` at creation (`seed.py:71`), classified
PLATFORM by `vs_rbac/migrations/0007_classify_permission_scope.py`, and refused
to any school role by `assert_tenant_may_hold` (`vs_rbac/models.py:91-110`).

`?tenant=` is still mandatory on every route (the auth-layer default) and is
used for nothing in this slice.

`xvs_platform_admin` is refused (issues §20).

## 10. Code map

| File | Responsibility |
|---|---|
| `models/registry.py:18-21` | `ServiceKind` |
| `models/registry.py:24-60` | `MonitoredService`, `set_status` |
| `models/registry.py:86-98` | `SLO` |
| `models/uptime.py:20-26` | `CheckType` |
| `models/uptime.py:29-48` | `UptimeCheck` |
| `models/uptime.py:51-72` | `UptimeCheckResult` |
| `models/uptime.py:75-93` | `UptimeDailyRollup` |
| `probes.py:26-33` | `_result` - the shape every probe returns |
| `probes.py:37-62` | `run_http` |
| `probes.py:66-78` | `run_tcp` |
| `probes.py:82-118` | `run_redis` - the only source of `mem_pct` |
| `probes.py:122-135` | `run_postgres` |
| `probes.py:139-165` | `run_ssl` |
| `probes.py:169-187` | `execute` - type dispatch |
| `tasks.py:35-69` | `run_uptime_checks_task` |
| `tasks.py:73-114` | `refresh_module_service_statuses` |
| `tasks.py:358-391` | `rollup_uptime_daily_task` |
| `services.py:447-459` | `service_grid` |
| `services.py:463-491` | `overall_posture`, `global_uptime` |
| `services.py:537-574` | `uptime_monitors` |
| `services.py:582-605` | `slo_status` |
| `views.py:128-154` | `ServiceListView`, `ServiceDetailView` |
| `views.py:162-178` | `UptimeMonitorsView`, `UptimeMonitorDetailView` |
| `views.py:398-402` | `SLOView` |
| `seed.py:22-37` | The twelve services |
| `seed.py:80-130` | `seed_services`, `seed_checks` |
| `seed.py:181-190` | `seed_slos` |
| `apps/celery.py:129-144` | The probe and rollup beat entries |

## 11. Test coverage & gaps

Baseline: **`Ran 27 tests in 2.139s` - OK**.

What this slice covers:

- `DailyRollupTests` (`tests.py:342-358`) - ten results, two CRITICAL, produces
  `total_checks = 10`, `failed_checks = 2`, `uptime_pct = 80.0`.
- `HealthSeedTests` (`tests.py:361-421`) - re-seeding repairs a stale SSL target
  (`api.codexvision.io` → `api.codexng.com`), restores the expected payload and
  the hourly interval, and creates no duplicate check.
- `SmallSampleGuardTests` (`tests.py:186-239`) - four tests over
  `refresh_module_service_statuses`: a below-floor window actively demotes a
  CRITICAL service to UNKNOWN, zero traffic demotes a HEALTHY one, 60 requests
  at 3000ms still reports CRITICAL, and 60 at 450ms is HEALTHY under the retuned
  bands.

What it does not cover:

1. **Every probe in `probes.py`.** Not one test executes `run_http`, `run_tcp`,
   `run_redis`, `run_postgres`, `run_ssl` or `execute` - not even with a mocked
   transport. The 404/401 → WARNING classification that turns three service
   cards yellow in production (issues §7) is entirely untested, and so is the
   SSL date parsing, which is the module's only `strptime`.
2. **`run_uptime_checks_task` itself.** The probe loop, the worst-status
   rollup across a service's checks, and the `affected_services` bookkeeping are
   never exercised. `DailyRollupTests` writes `UptimeCheckResult` rows by hand.
3. **The WARNING-counts-as-downtime rule.** `DailyRollupTests` uses CRITICAL and
   HEALTHY only, so the one line that conflates slow with down
   (`tasks.py:376-377`) has no test that would notice it changing either way.
4. **Every view in this slice.** `services/`, `services/<key>/`,
   `uptime/monitors/`, `uptime/monitors/<key>/` and `slos/` have no test at all -
   no 200, no 404 on an unknown key, no permission test.
5. **`uptime_monitors`, `slo_status`, `service_grid`, `overall_posture`,
   `global_uptime`.** None of the five aggregation functions has a direct test.
   The empty-window behaviours in issues §9 - `100.0` uptime, `100.0`
   attainment, "All systems operational" - are all untested, which is why they
   survived.
6. **`seed_services`' retirement of removed keys** (`seed.py:88-93`) and
   `seed_permissions` (`seed.py:46-76`). The seed tests cover checks and rules
   only.
