# health_signal_collection

The measurement backbone of `vs_health`: how a request becomes a number. The
timing middleware, the in-process buffer that keeps the hot path cheap, the
`RequestMetric` rollup table and its latency histogram, the percentile maths,
and the four golden-signal tiles, endpoint tables and tenant grids computed
from them.

Routes covered by this slice, mounted at `/v1/health/` (`apps/urls.py:39`):
`overview/`, `api-endpoints/`, `api-endpoints/detail/`, `tenants/`,
`tenants/<tenant_id>/`.

The service registry, uptime probes and SLOs are a separate slice
(`health_uptime_availability`); alerts and incidents are
`health_incidents_alerts`; queues and the task table are `health_queues_jobs`.

---

## 1. What it is (and what it is NOT)

- **Nothing here stores a request.** `RequestMetric` is an aggregate: one row
  per `(minute, route, method, tenant)` carrying counters and a latency
  histogram (`models/request_metrics.py:22-71`). There is no raw request log
  anywhere in the module, by design, and no way to answer "what did this one
  call do".
- **The route is the URL *pattern*, never the path.** `_route_for` reads
  `resolver_match.route` (`middleware.py:37-47`), so
  `/v1/finance/invoices/9f2c.../` is recorded as `/v1/finance/invoices/<uuid:id>/`.
  That is what keeps the table's cardinality bounded by the size of the URLconf
  rather than by traffic.
- **An unmatched request is not recorded at all.** No `resolver_match` means
  `_route_for` returns `None` and `_record` returns early
  (`middleware.py:39-41`, `:51-52`), so 404s against paths that do not exist are
  invisible to the error rate. Deliberate: recording them would put attacker-
  controlled strings into the `route` column.
- **The module does not measure itself.** Any route under `/v1/health/` is
  skipped (`middleware.py:54-56`).
- **Measurement can never break a request.** The middleware's whole recording
  block is inside a bare `except Exception` that logs at DEBUG
  (`middleware.py:30-33`), and `record` has its own
  (`collectors.py:80-81`). A metrics failure is silent by design.
- **`tenant` on a metric row is a slicing dimension, not an isolation
  boundary.** The model says so outright
  (`models/request_metrics.py:26-31`): the table is global observability data,
  and the only thing protecting it is that every route in the module requires a
  PLATFORM-scoped permission. There is no `TenantAwareManager` here and there
  should not be - a per-tenant filter would make the platform-wide error rate
  unanswerable.
- **Percentiles are estimates, and the module says so.** They come from
  17 fixed exponential buckets plus an overflow bucket
  (`constants.py:57-61`), interpolated linearly inside the matched bucket
  (`services.py:94-116`). Anything past 10s reports exactly 10000.0, because a
  bucketed histogram cannot know how far past the last bound a sample landed.
- **A small window is refused a verdict, not given a green one.** Below
  `MIN_P95_SAMPLE = 30` requests (`constants.py:76`) every ratio- and
  percentile-driven *status* reports UNKNOWN (`services.py:301-312`). The
  numbers themselves stay visible; only the badge is withheld. This is the
  module's best decision and the reason four of its 27 tests exist.
- **This is not a tenant-scoped screen.** `/tenants/` returns every tenant's
  traffic to any caller who reaches it. That is safe only because
  `platform.health.view` is `PermissionScope.PLATFORM` and therefore cannot be
  attached to a school role (`seed.py:71`, `vs_rbac/models.py:91-110`).

## 2. Domain model

### `RequestMetric` (`models/request_metrics.py:22`)

| Field | Meaning |
|---|---|
| `bucket_start` | Start of the 1-minute window, UTC, indexed |
| `route` | Resolved URL pattern, indexed, truncated to 255 at write |
| `method` | HTTP verb, truncated to 10 at write |
| `tenant` | FK, `SET_NULL`, nullable - null means unauthenticated or platform-anonymous |
| `request_count` | Total requests folded into this row |
| `status_2xx` / `3xx` / `4xx` / `5xx` | Status families, counted separately so an error rate needs no log |
| `throttled_count` | 429s, tracked as saturation pressure |
| `latency_sum_ms` | For the mean |
| `latency_max_ms` | For the worst case |
| `latency_hist` | 18 integer counts against `LATENCY_BUCKETS_MS` + overflow |

`unique_together = ("bucket_start", "route", "method", "tenant")`
(`models/request_metrics.py:56`) is the contract that makes the row canonical
and lets several gunicorn workers merge into it. Indexes on
`(bucket_start, route)` and `(bucket_start, tenant)`
(`models/request_metrics.py:57-60`) cover both drill-down directions.

Two computed properties exist and are unused by any view:
`error_count` (`:67`) and `avg_latency_ms` (`:71`).

### The latency histogram (`constants.py:57-61`)

```text
LATENCY_BUCKETS_MS = [5, 10, 25, 50, 75, 100, 150, 200, 300, 500,
                      750, 1000, 1500, 2000, 3000, 5000, 10000]
HISTOGRAM_SIZE     = 18          # 17 bounds + one overflow slot
```

Upper bounds, not midpoints. A 90ms request increments index 4 (the `<=100`
bucket). Anything over 10000ms increments index 17.

### `_Agg` - the in-memory accumulator (`collectors.py:36`)

Not a model. One mutable dataclass per `(bucket, route, method, tenant_id)` key
held in a process-global dict behind a `threading.Lock`
(`collectors.py:27-32`), with the same fields as `RequestMetric` in short form.

### The status vocabulary (`constants.py:18-45`)

```text
HEALTHY  <  WARNING  <  CRITICAL          rank 1 < 2 < 3
UNKNOWN                                   rank 0 - only wins when nothing else appears
```

`worst_status` (`constants.py:36-45`) collapses a list to the most severe. An
empty iterable returns UNKNOWN rather than HEALTHY, which is the correct answer
to "how is a thing nobody checked".

## 3. Endpoint map

Every route in this slice inherits `HealthViewMixin` (`views.py:49-52`):

- `permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]`
- `rbac_permission = "platform.health.view"`
- `tenant_param_required` is **not** set, so the auth-layer default of `True`
  applies (`vs_rbac/authentication.py:132`) and `?tenant=<slug>` is mandatory on
  every one of them.
- `platform_cross_tenant_param` is **not** set, so a CX caller may assert only
  their own tenant's slug.

| Method + path | Permission | Notes |
|---|---|---|
| `GET /overview/` | `platform.health.view` | The whole Command Center in one payload |
| `GET /api-endpoints/` | `platform.health.view` | Endpoint table + top-5 slowest + top-5 errored + code series |
| `GET /api-endpoints/detail/` | `platform.health.view` | `?route=` required; 400 without it (`views.py:210-212`) |
| `GET /tenants/` | `platform.health.view` | One row per tenant with traffic in the window |
| `GET /tenants/<int:tenant_id>/` | `platform.health.view` | Golden signals + series + endpoints for one tenant |

### Query parameters actually read

| Parameter | Read at | Behaviour |
|---|---|---|
| `range` | `views.py:68` → `services.parse_range` | One of `live`, `15m`, `1h`, `6h`, `24h`, `7d`, `30d`; anything else silently falls back to `1h` (`services.py:65`) |
| `start`, `end` | `views.py:68` | Only honoured **together**, only if `start < end` and the span is ≤ 90 days (`services.py:56-63`) |
| `tenant` | `views.py:72-79` | **Broken - see §8.** Claimed as a filter; consumed by the auth layer as a slug |
| `route` | `views.py:210` | `api-endpoints/detail/` only |

None of these go through a serializer. `parse_range` calls `parse_datetime`
directly on the raw strings.

### Response shapes

These endpoints return hand-built dicts, not serializers. The four KPI tiles
each carry the same five keys (`services.py:196-217`):

```jsonc
"latency":    {"value": 312.4, "unit": "ms",   "delta": -8.1, "status": "healthy", "spark": [...]},
"traffic":    {"value": 1.7,   "unit": "/min", "delta": 12.0, "status": "healthy", "spark": [...]},
"errors":     {"value": 0.42,  "unit": "%",    "delta": 0.0,  "status": "healthy", "spark": [...]},
"saturation": {"value": 61.3,  "unit": "%",    "delta": 0.0,  "status": "healthy", "spark": []}
```

`saturation.delta` is always `0.0` and `saturation.spark` is always empty
(`services.py:213-216`); `traffic.status` is the literal `HealthStatus.HEALTHY`
(`services.py:204`) whatever the traffic is doing.

One series point (`services.py:248-255`):

```jsonc
{"t": "2026-08-20T09:14:00Z", "requests": 102, "status_2xx": 100, "status_3xx": 0,
 "status_4xx": 1, "status_5xx": 1, "error_rate": 0.98, "p95": 312.4}
```

One endpoint row (`services.py:344-361`) adds `route`, `method`, `rpm`, `p50`,
`p99`, `throttled`, a nested `codes` object and a `status`.

## 4. Lifecycle / state machine

A request has no state; it has a path through four stages, three of them
in-process.

```text
request ends
   │
   ├─ middleware._record          resolve route, read request.tenant, skip /v1/health/
   │        │
   │        └─ collectors.record  fold into the process-global _Agg under a lock
   │                                  (no database contact at all)
   │
   ├─ ~30s later, daemon thread
   │        └─ collectors.flush   _drain() swaps the buffer out, then per key:
   │                              select_for_update + get_or_create + additive merge
   │
   └─ 7 days later
            └─ prune_health_metrics_task    row deleted, no rollup written first
```

`_ensure_flusher` starts exactly one daemon thread per worker process
(`collectors.py:185-202`), lazily on the first recorded request, at
`HEALTH_METRICS_FLUSH_SECONDS` or half the bucket width by default
(`collectors.py:199`). It refuses to start under the test runner
(`collectors.py:174-183`) because a thread holding a connection blocks test
database teardown, and it can be switched off with
`HEALTH_METRICS_BACKGROUND_FLUSH = False` (set in `ci.py:41` and `test.py:55`).

`_drain` swaps the whole dict out under the lock and returns the old one
(`collectors.py:99-105`), so flushing never blocks recording.

The merge is additive on every counter and takes the maximum of
`latency_max_ms`, element-wise summing the histograms
(`collectors.py:210-226`). That is what makes a bucket safe to flush from four
workers.

## 5. Derivations

- **Bucket assignment** - `_floor_minute` truncates seconds and microseconds
  (`collectors.py:59-60`); `METRIC_BUCKET_SECONDS = 60` (`constants.py:80`)
  documents the width.

- **Histogram index** - first bound the latency does not exceed, else the
  overflow slot (`collectors.py:50-55`).

- **Percentile from a histogram** (`services.py:94-116`):

  ```text
  target     = total * (p / 100)
  walk buckets accumulating counts until cumulative >= target
  result     = lower_bound + (upper - lower) * (target - prev) / count
  overflow   = LATENCY_BUCKETS_MS[-1]      # a floor, not an estimate
  empty      = 0.0
  ```

- **Merging** - `merge_hist` sums element-wise and tolerates a short or missing
  list (`services.py:82-90`), which is what lets any set of rows across any time
  range be collapsed into one percentile.

- **Time ranges** (`services.py:42-69`). Each key carries a duration, a `Trunc`
  granularity and a nominal point count. `prev_start = start - duration`, which
  is what every `delta` compares against.

  | key | window | trunc | points |
  |---|---|---|---|
  | `live` / `15m` | 15 min | minute | 15 |
  | `1h` | 1 hour | minute | 60 |
  | `6h` | 6 hours | minute | 36 |
  | `24h` | 24 hours | hour | 24 |
  | `7d` | 7 days | hour | 28 |
  | `30d` | 30 days | day | 30 |
  | custom | ≤ 90 days | hour if ≤ 7d else day | 48 |

- **Totals** - one `aggregate()` producing request count, the four status
  families, throttles and the latency sum (`services.py:135-152`). `error_rate`
  is `status_5xx / requests * 100`: **4xx does not count as an error**, which is
  right (a 403 is the system working) and worth knowing when the number looks
  low.

- **Delta** - `(curr - prev) / prev * 100`, with `prev = 0` returning `0.0` when
  the current value is also zero and `100.0` otherwise
  (`services.py:160-164`).

- **Traffic** - `requests / minutes`, where `_minutes` floors the divisor at 1.0
  so a `live` window cannot divide by zero (`services.py:73-74`).

- **The status bands** (`services.py:279-297`), tuned for the Render starter
  instance (0.5 CPU) the platform actually runs on:

  | Signal | HEALTHY | WARNING | CRITICAL |
  |---|---|---|---|
  | p95 latency | < 800ms | 800-1499ms | ≥ 1500ms |
  | 5xx rate | < 1% | 1-4.99% | ≥ 5% |
  | Saturation | < 75% | 75-89.9% | ≥ 90% |

- **The small-sample floor** - `window_status` is the single choke point
  (`services.py:301-312`):

  ```python
  if not requests or requests < MIN_P95_SAMPLE:
      return HealthStatus.UNKNOWN
  return worst_status([_status_for_error_rate(error_rate), _status_for_latency(p95)])
  ```

  The reasoning is written out at `constants.py:63-76` and it is sound:
  production traffic is 1-2 requests/minute, so a 15-minute window holds a
  couple of dozen requests, and one slow report used to flip p95 past the SLO
  and open a SEV2. `endpoint_stats` (`services.py:360`) and `tenant_stats`
  (`services.py:437`) both route through it.

- **Saturation** is not a request signal at all. `_saturation`
  (`services.py:260-275`) reads `mem_pct` out of `UptimeCheckResult.meta`, which
  only the Redis probe ever writes (`probes.py:104-110`). With no Redis probe
  result in the window it returns `0.0 / HEALTHY`.

- **Noisy neighbour** - `reqs > avg_reqs * 3` where the mean includes the tenant
  being tested (`services.py:417-428`).

## 6. What writing writes

Only one thing in this slice writes, and it writes only to `RequestMetric`:

| Action | Written by | Row |
|---|---|---|
| Fold a request into memory | `collectors.record` (`collectors.py:64-97`) | nothing - process memory only |
| Persist a bucket | `collectors.flush` (`collectors.py:108-162`) | insert or additive update of one `RequestMetric` |
| Retention | `prune_health_metrics_task` (`tasks.py:403-404`) | deletes rows older than 7 days |

**No audit event is written anywhere in this slice, and none should be** - these
are machine counters, not business acts.

Every read endpoint here writes nothing at all.

## 7. Worked example

Ada opens the Command Center at 09:20 with the default hour window:

```text
GET /v1/health/overview/?tenant=codex&range=1h
```

`?tenant=codex` is consumed by `TenantJWTAuthentication` as Ada's own tenant
assertion, and then read a second time by `_tenant_id`, which cannot parse it as
an integer and returns `None` - so the payload is platform-wide (§8, issue §2).

`parse_range("1h")` returns `start = 08:20`, `end = 09:20`,
`prev_start = 07:20`, `trunc = "minute"`.

`golden_signals` runs two `_totals` aggregates (current and previous), merges
the histograms of each window for a p95, computes the series, and reads the
worst `mem_pct` from the window's probe results:

```jsonc
{"success": true, "message": "Overview retrieved successfully.",
 "data": {
   "range": "1h",
   "posture": {"overall": "warning", "label": "3 services degraded",
               "critical": 0, "warning": 3, "active_incidents": 1},
   "global_uptime": 99.412,
   "kpis": {
     "latency":    {"value": 312.4, "unit": "ms",   "delta": -8.1,  "status": "healthy", "spark": [...60 points]},
     "traffic":    {"value": 1.7,   "unit": "/min", "delta": 12.0,  "status": "healthy", "spark": [...]},
     "errors":     {"value": 0.98,  "unit": "%",    "delta": 0.0,   "status": "unknown", "spark": [...]},
     "saturation": {"value": 61.3,  "unit": "%",    "delta": 0.0,   "status": "healthy", "spark": []}
   },
   "services": [...12 cards, worst first...],
   "request_series": [...the same 60 points, computed a second time...],
   "deployments": [], "queues": {...}, "active_incidents": [...]}}
```

`errors.status` is `unknown` while `errors.value` is `0.98`: the hour held 102
requests, above the floor, so this window would normally be judged - but on a
quieter hour with 24 requests the badge goes UNKNOWN and the number stays. That
asymmetry is intentional and it is the module's best behaviour.

`posture` reads "3 services degraded" on a healthy platform, for reasons that
belong to the uptime slice (`health_code_issues.md` §7).

Drilling into one route:

```text
GET /v1/health/api-endpoints/detail/?tenant=codex&route=/v1/finance/invoices/&range=24h
```

```jsonc
{"route": "/v1/finance/invoices/",
 "totals": {"requests": 812, "s2": 800, "s3": 0, "s4": 11, "s5": 1,
            "throttled": 0, "error_rate": 0.123, "avg_ms": 240.6},
 "p50": 180.0, "p95": 460.2, "p99": 940.0,
 "histogram": {"buckets": [5, 10, ..., 10000], "counts": [0, 0, ..., 3]},
 "series": [...24 hourly points...],
 "affected_tenants": [{"tenant_id": 4, "name": "Corona Secondary School",
                       "requests": 690, "error_rate": 0.145}, ...]}
```

`affected_tenants` is capped at ten and excludes the null-tenant rows
(`services.py:371-376`), so anonymous traffic on a route is counted in `totals`
and absent from the breakdown.

## 8. Gotchas / known limitations

Full evidence for each is in **`error/health/health_code_issues.md`**. The items
belonging to this slice:

- **The `?tenant=` filter is dead on every screen here**, and `?tenant=all` -
  the value `_tenant_id` was written to understand - is a 404 from the auth
  layer. The auth layer wants a slug, `_tenant_id` wants an integer, and the
  mismatch is swallowed (`views.py:72-79`, issues §2).
- **A malformed `?start=` is a 500**, not a 400: `parse_datetime` raises on a
  well-shaped but impossible date and nothing catches it
  (`services.py:56-60`, issues §15). Confirmed by execution.
- **The 30-day range can never show more than seven days.** Retention deletes
  `RequestMetric` at 7 days (`tasks.py:403-404`) and nothing rolls it up first,
  so `7d`, `30d` and any custom window past ~3.5 days also flatten every
  `delta` to 100.0 (issues §16).
- **The Command Center computes `request_series` twice** - once inside
  `golden_signals` for the sparklines (`services.py:183`) and again in the view
  (`views.py:115`) - each pass scanning every matching row's histogram JSON
  (issues §17).
- **`_saturation` pulls every probe `meta` in the window into Python** to find
  one maximum (`services.py:263-270`), and returns `0.0 / HEALTHY` when there is
  no data rather than UNKNOWN (issues §9, §18).
- **`/api-endpoints/` and `/tenants/` are unpaginated** and grow with the
  URLconf and the customer base respectively (issues §18).
- **The noisy-neighbour flag cannot fire below four active tenants**, because
  the mean it compares against includes the tenant being tested
  (`services.py:419`, issues §23).
- **`traffic.status` is hardcoded HEALTHY** (`services.py:204`), so the tile
  renders a badge that is a constant (issues §23).
- **`get_or_create` under `select_for_update` does not stop a duplicate insert**
  - the lock only exists once the row does - and the resulting `IntegrityError`
  is swallowed with a `logger.warning`, dropping that bucket's counts
  (`collectors.py:123-160`, issues §24.4).
- **Buffered metrics are lost on shutdown**: the flusher is a daemon thread with
  no `SIGTERM` hook, so a deploy discards up to one flush interval of requests
  (issues §24.5).
- **Justified by design:** unmatched paths are not recorded
  (`middleware.py:39-41`). Recording them would put attacker-controlled strings
  into an indexed column.
- **Justified by design:** the module does not measure its own routes
  (`middleware.py:54-56`).
- **Justified by design:** `RequestMetric` uses no tenant-aware manager. A
  platform-wide error rate has to see every tenant's rows, and the table is
  gated by a PLATFORM-scoped key instead.
- **Justified by design:** the overflow bucket reports the last bound as a
  floor, so every p99 above 10s reads exactly `10000.0`
  (`services.py:109-111`).

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Scope | Seeded to |
|---|---|---|---|---|
| Every route in this slice | `platform.health.view` | NORMAL | PLATFORM | nobody but `xvs_super_admin` |

Seeded by `seed.py:46-76`. The key is created with
`scope=PermissionScope.PLATFORM` (`seed.py:71`) and classified PLATFORM by
`vs_rbac/migrations/0007_classify_permission_scope.py`, so
`assert_tenant_may_hold` (`vs_rbac/models.py:91-110`) refuses to attach it to
any school role. **That is the isolation story for this slice, and it holds**:
`/tenants/` returns every tenant's traffic, and the only reason that is
acceptable is that no school role can ever hold the key that reaches it.

Three consequences worth stating plainly:

1. **There is no per-tenant view of this data for a tenant.** A school admin
   cannot see their own school's latency, by design; there is no tenant-facing
   equivalent of this screen.
2. **A CX caller cannot assert another tenant's slug**, because no health view
   sets `platform_cross_tenant_param = True`. Combined with the broken filter
   (§8), the per-tenant reading is unreachable from any direction.
3. **`xvs_platform_admin` gets a 403 here.** `seed_permissions` grants the key
   to no role; the only grant is the catch-all reconciliation in
   `seed_all_permissions`, which targets `xvs_super_admin` alone
   (issues §20).

`request.tenant` is used for exactly one thing in this slice: stamping the
`tenant` column on a metric row (`middleware.py:58-60`). It never filters a
read.

## 10. Code map

| File | Responsibility |
|---|---|
| `middleware.py:22-69` | `RequestMetricsMiddleware` - timing, route resolution, tenant read, the two skips |
| `collectors.py:36-47` | `_Agg` - the in-memory accumulator |
| `collectors.py:50-60` | `bucket_index`, `_floor_minute` |
| `collectors.py:64-105` | `record`, `_drain` - the hot path and the buffer swap |
| `collectors.py:108-162` | `flush` - locked upsert and additive merge |
| `collectors.py:165-202` | The daemon flusher and its two opt-outs |
| `models/request_metrics.py:22-71` | `RequestMetric` |
| `constants.py:18-45` | `HealthStatus`, `STATUS_RANK`, `worst_status` |
| `constants.py:48-80` | Histogram layout, `MIN_P95_SAMPLE`, bucket width |
| `services.py:32-74` | `TimeRange`, `parse_range`, `_minutes` |
| `services.py:82-116` | `merge_hist`, `percentile_from_hist` |
| `services.py:124-164` | `_base_qs`, `_totals`, `_merged_hist`, `_delta` |
| `services.py:168-256` | `golden_signals`, `request_series` |
| `services.py:260-312` | `_saturation`, the status bands, `window_status` |
| `services.py:320-392` | `endpoint_stats`, `endpoint_detail` |
| `services.py:400-439` | `tenant_stats` |
| `views.py:49-79` | `HealthViewMixin`, `_range`, `_tenant_id` |
| `views.py:87-120` | `OverviewView` |
| `views.py:186-215` | `ApiEndpointsView`, `ApiEndpointDetailView` |
| `views.py:357-383` | `TenantListView`, `TenantDetailView` |
| `apps/settings/base.py:150` | Middleware registration, after tenant context |
| `apps/settings/base.py:309-312` | `HEALTH_PROBE_BASE_URL`, `HEALTH_SSL_DOMAIN` |

## 11. Test coverage & gaps

Baseline: **`Ran 27 tests in 2.139s` - OK**
(`cd apps && DB_NAME=cx_healthslice ../cx/Scripts/python.exe manage.py test
vs_health --settings=apps.settings.local --noinput`).

What this slice covers:

- `PercentileMathTests` (`tests.py:53-72`) - monotonic p50 < p95 ≤ p99 on a
  uniform 1-1000ms distribution, an empty histogram returning 0.0, element-wise
  merge.
- `GoldenSignalsTests` (`tests.py:75-103`) - the four-tile shape, a ~2% error
  rate computed correctly, non-zero traffic, and a series carrying `p95` and
  `error_rate`.
- `CollectorFlushTests` (`tests.py:106-126`) - record-then-flush upserts one
  row, a second batch into the same bucket merges rather than duplicating, and
  the 5xx counter is separate. It drains the process-global buffer in `setUp`
  because the middleware feeds it during the whole suite.
- `SmallSampleGuardTests` (`tests.py:168-339`) - the strongest block in the
  module. It proves the floor actively *demotes* a CRITICAL service to UNKNOWN
  rather than merely matching the model default, that zero traffic does not keep
  a green service green, that 60 requests at 3000ms still reports CRITICAL, that
  the retuned 800/1500 bands leave 450ms healthy, that an endpoint's status is
  withheld below the floor while its p95 stays visible, and the exact boundary
  at `MIN_P95_SAMPLE - 1` / `MIN_P95_SAMPLE`.

What it does not cover:

1. **The middleware.** Nothing exercises `RequestMetricsMiddleware` at all -
   not route resolution, not the `/v1/health/` skip, not the unmatched-path
   skip, not the tenant read. `CollectorFlushTests` calls `collectors.record`
   directly, bypassing every decision the middleware makes.
2. **Every view in this slice except `overview`.** There is one API test
   (`tests.py:424-446`) and it asserts a 401 unauthenticated and a 200 with
   `has_permission` patched to `True`. `api-endpoints`, `api-endpoints/detail`,
   `tenants` and `tenants/<id>` have no test of any kind.
3. **The permission surface.** `RBACGatingTests` patches
   `vs_rbac.permissions.has_permission` to return `True`, so it proves the view
   consults RBAC and nothing about *which* key or *which* caller. There is no
   test that a school-tenant user is refused, no test that
   `platform.health.manage` is required for a write, and no test that the
   PLATFORM scope actually blocks the key from a school role.
4. **`?tenant=`, `?range=`, `?start=`/`?end=`.** No test sends any of them.
   The two 500s in issues §1 and §15 both live in untested parameter handling.
5. **`endpoint_detail` and `tenant_stats`.** Neither function has a direct test;
   `tenant_stats` is only reached through the untested view.
6. **`_delta` and the previous-window comparison.** Never asserted, including
   the `prev == 0 → 100.0` branch that every long range now takes.
