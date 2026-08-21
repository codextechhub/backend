# health_code_issues

Everything wrong with `vs_health`, in one place, ordered by how much it costs.
Each item states the defect, the evidence, what actually happens to a user, and
the fix. The four slice reports (`health_signal_collection`,
`health_uptime_availability`, `health_incidents_alerts`, `health_queues_jobs`)
point here rather than repeating it.

Baseline: the `vs_health` suite is **27 tests, all green**
(`Ran 27 tests in 2.139s` - OK, via
`cd apps && DB_NAME=cx_healthslice ../cx/Scripts/python.exe manage.py test
vs_health --settings=apps.settings.local --noinput`). Every item below is
therefore something the suite does not currently catch. Nothing here is
speculative: every claim is traced to a file and line, and the four marked
**confirmed by execution** were reproduced against a real JWT in a throwaway
test module that was deleted afterwards.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in
the code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | `/v1/health/tasks/` 500s on the only `?tenant=` value the auth layer accepts, so the Background Jobs table is unreachable | **Critical** |
| 2 | The `?tenant=` filter on every analytics screen is dead, and `?tenant=all` - which the code special-cases - is a 404 | **High** |
| 3 | Nothing is ever alerted: `channel` is a decorative string and no notification, email or page is ever sent | **High** |
| 4 | Error-rate and p95 rules ignore `target_service` and measure the whole platform, then blame one service | **High** |
| 5 | `duration_sec` ("sustained for") is not implemented, so a flapping metric opens a fresh incident every minute | **High** |
| 6 | Incident codes are allocated by string sort, so the allocator jams permanently at `INC-10000` and takes the alert engine down with it | **High** |
| 7 | Three seeded probes point at URLs that do not exist or refuse anonymous callers, so the platform posture is permanently "3 services degraded" | **High** |
| 8 | A WARNING probe counts as downtime in the daily rollup, so slow-but-up breaches its own SLO | **High** |
| 9 | Empty windows claim 100% uptime, 0% saturation and "All systems operational" - green with no data | **High** |
| 10 | Five of the six monitored queues do not exist; nothing routes tasks anywhere but `celery` | **Medium** |
| 11 | The queue card's throughput, retry and dead figures each measure something other than what they are labelled | **Medium** |
| 12 | `UptimeCheck.interval_sec` is ignored - every check runs every five minutes, hourly ones included | **Medium** |
| 13 | Resolving an incident by hand never stamps `resolved_at`, and nothing ever stamps `acknowledged_at`, so MTTA and MTTR are permanently null | **Medium** |
| 14 | War-room attribution is free text supplied by the caller, and `Incident.owner` is never written | **Medium** |
| 15 | A malformed `?start=` / `?end=` is a 500 | **Medium** |
| 16 | The 30-day range and the 90-day custom window can never show more than seven days of data | **Medium** |
| 17 | The Command Center computes its request series twice, and one uptime monitor costs the whole monitor grid | **Medium** |
| 18 | Unbounded scans and unpaginated lists across the analytics layer | **Medium** |
| 19 | Not one write in the module emits an audit event | **Medium** |
| 20 | `platform.health.*` is granted to nobody but the super admin | **Medium** |
| 21 | The probes run inside the process they monitor, and a frozen service card is indistinguishable from a fresh one | **Medium** |
| 22 | School vocabulary in an engine app | **Low** |
| 23 | Three surfaces display numbers that can never change | **Low** |
| 24 | Smaller defects and dead code | **Low** |

---

## 1. `/v1/health/tasks/` 500s on the only `?tenant=` value the auth layer accepts

**Critical. Confirmed by execution.**

### The defect

Two different things want the query parameter named `tenant`, and they want
incompatible values.

The authentication layer requires it and requires a **slug**:

```python
# vs_rbac/authentication.py:95
slug = (params.get("tenant") or "").strip().lower()
# vs_rbac/authentication.py:109-112
tenant = Tenant.objects.filter(
    slug=slug, status__in=Tenant.AUTHENTICABLE_STATUSES,
).first()
if tenant is None:
    raise NotFound("No tenant matches the requested context.")
```

No view in `vs_health` sets `tenant_param_required = False`, so the default at
`vs_rbac/authentication.py:132` applies and the parameter is mandatory on every
health route.

`TaskListView` then takes that same parameter and feeds it straight to a
numeric foreign key:

```python
# views.py:247-249
tenant = params.get("tenant")
if tenant and tenant != "all":
    qs = qs.filter(tenant_id=tenant)
```

`BackgroundJob.tenant` is a foreign key to `vs_tenants.Tenant`, whose primary
key is numeric (`core/models.py:56-59`). `filter(tenant_id="codex")` raises
`ValueError: Field 'id' expected a number but got 'codex'.`, and
`core/exceptions.py:91-94` has no handler for `ValueError`, so DRF returns
`None` and Django serves a 500.

### What actually happens

An SRE opens the Background Jobs screen. Every value they can send is a
failure:

```text
GET /v1/health/tasks/                 -> 400  "A 'tenant' query parameter is required."
GET /v1/health/tasks/?tenant=codex    -> 500  ValueError: Field 'id' expected a number but got 'codex'.
GET /v1/health/tasks/?tenant=all      -> 404  "No tenant matches the requested context."
GET /v1/health/tasks/?tenant=1        -> 404  "No tenant matches the requested context."
```

That matrix was produced against a real `CustomTokenObtainPairSerializer`
token, not `force_authenticate`. **There is no request that reaches the task
table.** The screen is dead on arrival for every caller.

### Why it exists

`_tenant_id` (`views.py:72-79`) was written for a filter parameter, and the
tenant assertion was added to the platform later. The two now collide on a name.
`_tenant_id` swallows the mismatch (`except (TypeError, ValueError): return
None`), which is why the other screens fail quietly instead of loudly - see §2.
`TaskListView` does not go through `_tenant_id` at all, so it fails loudly.

### The fix

Rename the filter. `?tenant=` belongs to the auth layer platform-wide; the
health screens need their own parameter, and it should take a slug like every
other tenant reference in the codebase:

1. Add `?for_tenant=<slug>` and resolve it through
   `Tenant.objects.filter(slug=...)`, returning 400 on an unknown slug rather
   than silently widening to global.
2. Point `_tenant_id`, `TaskListView`, `ApiEndpointsView` and `TenantListView`
   at the new parameter.
3. Keep `TenantDetailView`'s `<int:tenant_id>` path segment as-is; it is
   already unambiguous.

---

## 2. The `?tenant=` filter on every analytics screen is dead

**High. Confirmed by execution.** Same root cause as §1, different symptom.

### The defect

```python
# views.py:72-79
def _tenant_id(request):
    raw = request.query_params.get("tenant")
    if raw in (None, "", "all"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
```

The auth layer has already forced `raw` to be a slug. `int("codex")` raises,
the exception is swallowed, and the function returns `None` - which every
caller reads as "no tenant filter, show the whole platform".

### What actually happens

Ada, a CX engineer, is looking at Corona Secondary School's traffic. She
selects Corona in the tenant dropdown. The frontend sends
`?tenant=corona-secondary`. The auth layer refuses it - she is on the `codex`
tenant and no health view sets `platform_cross_tenant_param = True` - so she
gets a **404 "No tenant matches the requested context."** on the Command
Center.

She switches back to "All". The frontend sends `?tenant=all`, exactly the value
`_tenant_id` was written to understand, and the auth layer refuses that too
with the same 404, because there is no tenant whose slug is `all`.

The only value that works is her own tenant's slug, `?tenant=codex`, and that
one is silently discarded by `_tenant_id` and shows the whole platform. So the
per-tenant view of the Command Center, the API & Endpoint Health screen and the
Tenant Health grid cannot be reached, and the one reading she can get is the
one she did not ask for.

Verified matrix, real JWT:

```text
GET /v1/health/overview/?tenant=codex  -> 200  (global, not tenant-filtered)
GET /v1/health/overview/?tenant=all    -> 404
GET /v1/health/overview/?tenant=1      -> 404
GET /v1/health/tenants/?tenant=all     -> 404
```

### The fix

Same as §1. Additionally, `platform_cross_tenant_param = True` has to be set on
the health views if a CX engineer is ever to assert a school's slug, and
`_tenant_id` must stop swallowing a bad value: an unparseable filter should be a
400, not a silent widening of scope.

---

## 3. Nothing is ever alerted

**High.**

### The defect

`AlertRule.channel` is documented as the delivery target:

```python
# models/incidents.py:138
channel = models.CharField(max_length=60, blank=True, default="", help_text="e.g. 'PagerDuty', 'Slack #sre'.")
```

and the seeder fills it in for all five default rules
(`seed.py:141-148`): PagerDuty, Slack #sre, Zoho Cliq, Email.

`evaluate_alert_rules_task` reads every enabled rule, decides whether it
breaches, writes an `Alert` row and an `Incident` row, and returns
(`tasks.py:284-313`). It never reads `rule.channel`. Grepping the whole app for
a dispatcher finds nothing: `vs_health` contains no import of
`vs_notifications`, no `send_notification`, no `NotificationService`, and no
email call of any kind.

### What actually happens

At 02:14 on a Sunday the API starts returning 500s. Within a minute the beat
task fires the SEV1 "API error rate" rule, writes `Alert(status=FIRING)` and
opens `INC-2041`, and the rule's `channel` says "PagerDuty".

Nobody is paged. Nobody is emailed. Nothing appears in anyone's in-app feed.
The incident sits in a database table until someone opens the Health screen on
Monday morning - which is precisely the thing an alerting system exists to make
unnecessary.

### The fix

`vs_notifications` already has the delivery engine, the templates and the
channel-resolution logic. Add an event type (`health.alert.fired` /
`health.alert.resolved`), dispatch from `evaluate_alert_rules_task` after the
`Alert` row is written, and address it to the holders of
`platform.health.manage`. Treat `channel` as either a real routing key into
that engine or delete the field; a string nothing reads is worse than no field,
because it reads as a configured page.

---

## 4. Error-rate and p95 rules ignore `target_service`

**High.**

### The defect

`AlertRule.target_service` is documented as the rule's subject
(`models/incidents.py:133-136`, "Null = applies platform-wide"). For the two
request-derived metrics, the resolver never uses it:

```python
# tasks.py:254-261
if rule.metric in (AlertRule.Metric.ERROR_RATE, AlertRule.Metric.P95_LATENCY):
    qs = services._base_qs(tr.start, tr.end)
    totals = services._totals(qs)
    ...
```

`_base_qs(start, end, tenant_id=None, route=None)` (`services.py:124-131`)
filters on the time window and nothing else. No route filter, no service
mapping. The service is then stamped onto the alert and the incident anyway
(`tasks.py:301`, `tasks.py:329-330`).

### What actually happens

The seeded "API error rate" rule targets the `api` service
(`seed.py:141`). A bad deploy to the report engine starts throwing 500s on
`/v1/finance/reports/trial-balance/`. Platform-wide error rate crosses 5%.

`INC-2041 "API error rate: 7.3 > 5"` opens, severity SEV1, attached to
**API · DRF**. The service grid turns the API card red. The engineer who
responds spends the first twenty minutes of the incident looking at a service
that is working perfectly, because the incident says so.

The reverse case is just as bad: a rule targeting the low-traffic `reports`
service is evaluated against the whole platform's traffic, so it will never
notice a reports-only failure that a busy `/v1/i/` drowns out.

### The fix

`ROUTE_PREFIX_SERVICES` (`constants.py:89-93`) already maps a service key to
its route prefixes and `refresh_module_service_statuses` already uses it
correctly (`tasks.py:93-97`). Reuse it: when `rule.target_service` is set and
its key appears in that map, apply the same `route__startswith` disjunction to
`_base_qs` before computing totals. When the key is not in the map, either
refuse to save the rule or state on the response that the rule is
platform-wide.

---

## 5. `duration_sec` is not implemented

**High.**

### The defect

The field's own help text is a promise:

```python
# models/incidents.py:130
duration_sec = models.PositiveIntegerField(default=300, help_text="Sustained-for window before firing.")
```

The evaluator fires on the first breaching evaluation:

```python
# tasks.py:295-304
if breaching and not open_alert:
    ...
    Alert.objects.create(..., status=Alert.Status.FIRING, incident=incident)
```

The only use of `duration_sec` anywhere is to *widen the measurement window*,
and only past fifteen minutes:

```python
# tasks.py:246-248
if rule.duration_sec > 900:
    tr.start = tr.end - timedelta(seconds=rule.duration_sec)
```

So the seeded rules at 300s and 600s (`seed.py:141`, `:145`) use the default
15-minute window and fire on the first tick that breaches. There is no
sustained-for logic, no hysteresis and no dampening anywhere in the module.

### What actually happens

The beat task runs every minute (`apps/celery.py:137-140`). p95 latency on a
busy Monday morning oscillates around the 800ms threshold: 812, 780, 830, 795.

```text
09:01  fires   INC-2041 opened, SEV2, "p95 latency SLO: 812 > 800"
09:02  clears  INC-2041 resolved
09:03  fires   INC-2042 opened
09:04  clears  INC-2042 resolved
```

Four incidents in four minutes, each with its own code and timeline, all of
them noise. Left alone for a day that is up to 1,440 incidents, which is also
how §6 becomes reachable within a week rather than within a decade.

### The fix

Two changes, both small:

1. **Implement sustained-for.** Record the first breaching observation on the
   rule (a `breaching_since` timestamp, cleared on a clean evaluation) and fire
   only once `now - breaching_since >= duration_sec`.
2. **Add resolve hysteresis.** Require N consecutive clean evaluations, or a
   value below a resolve threshold set some margin under the fire threshold,
   before resolving. Firing and resolving on the same number is what produces
   the flap.

---

## 6. Incident codes are allocated by string sort

**High.**

### The defect

```python
# tasks.py:221-230
def _next_incident_code() -> str:
    from .models import Incident
    last = Incident.objects.filter(code__startswith="INC-").order_by("-code").first()
    n = 2000
    if last:
        try:
            n = int(last.code.split("-")[1])
        except (IndexError, ValueError):
            pass
    return f"INC-{n + 1}"
```

`code` is a `CharField` (`models/incidents.py:42`), so `order_by("-code")` is a
string sort. `"INC-9999"` sorts above `"INC-10000"` because `'9' > '1'`.

`code` is also `unique=True`, and the allocator runs inside
`evaluate_alert_rules_task` with no lock and no retry.

### What actually happens

Two failures, one immediate and one delayed.

**Immediate.** An operator files an incident by hand and types the code
themselves - the serializer lets them (`serializers.py:65-77`,
`extra_kwargs = {"code": {"required": False}}`, so it is optional, not
forbidden). They enter `INC-DB-01`. That string sorts above every `INC-2xxx`
code. The next auto-incident calls `_next_incident_code`, `int("DB")` raises
`ValueError`, the `except` swallows it, `n` stays at its 2000 default, and the
allocator returns `INC-2001` - which already exists. `IntegrityError` escapes
`evaluate_alert_rules_task`, the beat task dies, and **no alert fires or
resolves again** until someone deletes that one row.

**Delayed.** After the 8,000th incident the allocator reaches `INC-10000`. From
then on `order_by("-code").first()` keeps returning `INC-9999`, the allocator
keeps computing `INC-10000`, and every call after the first raises
`IntegrityError`. Same outcome, permanently.

### The fix

Stop deriving the number from a string. Add an integer `sequence` column with a
database sequence or a `select_for_update` counter row, format the code from
it, and make the manual-create path allocate from the same source rather than
accepting a client-supplied code. Wrap the alert-evaluation loop in a
`try/except` per rule so one rule's failure cannot stop the other four.

---

## 7. Three seeded probes point at URLs that do not exist

**High. Confirmed by URL resolution.**

### The defect

The seeder configures HTTP probes against three paths
(`seed.py:120`, `:121`, `:125`):

```python
mk("api",      "API health",        CheckType.HTTP, f"{PROBE_BASE}/v1/",         {"status": 200, "warn_ms": 800})
mk("auth",     "Auth endpoint",     CheckType.HTTP, f"{PROBE_BASE}/v1/user/",    {"warn_ms": 800})
mk("payments", "Payments gateway",  CheckType.HTTP, f"{PROBE_BASE}/v1/payments/", {"warn_ms": 900})
```

Resolved against the real URLconf:

```text
/v1/          -> 404 NO MATCH
/v1/payments/ -> 404 NO MATCH
/v1/user/     -> resolves (DRF api-root), and DEFAULT_PERMISSION_CLASSES is
                 IsAuthenticated (apps/settings/base.py:55-63), so an
                 anonymous probe gets 401
```

`run_http` classifies all three the same way:

```python
# probes.py:56-57
if code != want and code >= 400:
    return _result(HealthStatus.WARNING, elapsed, code, error=f"HTTP {code}")
```

`want` defaults to 200 (`probes.py:44`), so 404 and 401 both land on WARNING,
every five minutes, forever.

### What actually happens

Five minutes after `seed_health` runs on a healthy production system:

- **API · DRF** (Tier 1, Core) - WARNING, "HTTP 404"
- **Auth / JWT** (Tier 1, Core) - WARNING, "HTTP 401"
- **Payment Gateway** (External) - WARNING, "HTTP 404"

`overall_posture` (`services.py:463-479`) counts the warnings and returns
`"3 services degraded"`. That string is the Command Center's headline banner and
it is also the console landing card (`vs_admin_console/overview.py:233-242`).
Every CX staff member who logs in sees a permanently degraded platform, which
is the fastest possible way to teach a team to ignore the health screen.

It compounds into §8: the `api` service's daily rollup counts every WARNING as
a failed check, so `api` reports 0% uptime, the "API uptime SLO" rule
(LT 99.5, `seed.py:148`) breaches, and a SEV2 incident opens and can never
resolve.

A fourth probe is conditional rather than certain: `web` targets
`FRONTEND_BASE_URL` (`seed.py:115`), whose default is
`http://localhost:3000` (`apps/settings/base.py:305`). If that variable is not
set in the deployed environment the probe dials localhost from inside the API
container, gets connection-refused, and the Tier 1 "Web Frontend" card sits at
CRITICAL forever.

### The fix

1. Add a real unauthenticated liveness route (`/v1/health/live/` returning 200
   with no auth, excluded from metrics like the rest of `/v1/health/`) and point
   the `api` probe at it.
2. Point the `auth` probe at something anonymous can reach, or set
   `expected={"status": 401}` and mean it - the probe is testing that the auth
   layer answers, and 401 is the correct answer.
3. Point the `payments` probe at a real payments route, or make it a TCP/HTTP
   probe against the actual gateway host, which is what the card claims to
   measure.
4. Fail the seeder loudly when `FRONTEND_BASE_URL` still holds its localhost
   default in a non-local settings module.

---

## 8. A WARNING probe counts as downtime

**High.**

### The defect

```python
# tasks.py:376-378
failed = results.filter(
    status__in=[HealthStatus.CRITICAL, HealthStatus.WARNING]).count()
uptime = round((total - failed) / total * 100, 4)
```

WARNING is the module's word for "up, but slower than the threshold". The Redis
probe warns above 50ms (`seed.py:123`), the HTTP probes above 800ms
(`seed.py:116-121`). None of those states mean the service is unavailable.

### What actually happens

Redis is healthy but the box is busy, and 30 of the day's 288 pings come back at
55ms instead of 20ms. The daily rollup records `uptime_pct = 89.58`.

That figure is what drives the 90-day uptime bar
(`services.py:546`), the 24h/7d/30d/90d windows (`services.py:552-556`),
`global_uptime` on the Command Center (`services.py:482-491`), and SLO
attainment (`services.py:582-604`). So a day on which Redis never once failed is
drawn as a 10% outage, the 99.9% SLO shows as breached with the error budget
exhausted, and the `UPTIME_PCT` alert rule opens an incident about it.

### The fix

Count only CRITICAL as failed uptime. Keep WARNING in `worst_status` for the
day's `worst_status` column, where "degraded" is exactly the right word, and
add a separate `degraded_checks` counter if the degraded fraction is worth
showing. Availability and performance are two different questions and the
rollup currently answers the second one under the first one's name.

---

## 9. Empty windows claim 100% uptime, 0% saturation and "All systems operational"

**High.**

### The defect

The module states the correct principle in one place and breaks it in four.

The principle, from `global_uptime`:

```python
# services.py:482-491
"""...
None when no rollups exist yet - an uptime figure must never be claimed
without a single real check behind it.
"""
```

The four breaches:

1. **Uptime windows.** `_window(d)` returns `100.0` when the service has no
   rollups in the window (`services.py:552-556`), so a service nobody has ever
   probed shows "100% (90d)".
2. **SLO attainment.** `current = ... if vals else 100.0`
   (`services.py:591`), so an SLO with no data reads as fully attained with a
   100% error budget remaining.
3. **Saturation.** `_saturation` starts `worst = 0.0` and returns HEALTHY when
   no probe result carries a `mem_pct` (`services.py:260-275`). A Redis that has
   never been probed shows "0% - Healthy" on the Command Center's fourth KPI
   tile.
4. **Overall posture.** `overall_posture` counts only CRITICAL and WARNING
   (`services.py:468-475`); UNKNOWN falls through to the `else` branch and
   returns `"All systems operational"`. UNKNOWN is the module's *normal* state
   for the three module services on low traffic - that is exactly what the
   small-sample floor is for (`constants.py:66-76`) - so the banner claims
   everything is fine precisely when the system has no idea.

### What actually happens

`seed_health` runs on a fresh environment at 09:00. Celery beat is
misconfigured and no probe has ever executed. At 09:05 a CX engineer opens the
Command Center:

> **All systems operational**
> Global uptime - (no data, correctly)
> Saturation 0% · Healthy
> PostgreSQL 100% (90d) · Redis 100% (90d) · API 100% (90d)
> SLOs: API 99.9% target, 100.0% current, 100% error budget remaining

Every green thing on that screen is fabricated. The one honest field is the
one that returns `None`.

### The fix

Return `None` for a window with no rollups and render it as "-", exactly as
`global_uptime` already does; the frontend already has to handle a null there.
Return `UNKNOWN` from `_saturation` when no result carries `mem_pct`. Make
`overall_posture` count UNKNOWN explicitly and say so
("3 services unmonitored"), because a status vocabulary with four values and a
banner with three is where the fourth value goes to die.

---

## 10. Five of the six monitored queues do not exist

**Medium.**

### The defect

```python
# constants.py:84
KNOWN_QUEUES = ["imports", "exports", "notifications", "provisioning", "reports", "celery"]
```

`apps/celery.py` defines a `beat_schedule` and nothing else - there is no
`task_routes`, no `task_default_queue` override, and no `apply_async(queue=...)`
anywhere in the repo. Every task therefore runs on Celery's default queue,
`celery`.

`_broker_depths` calls `LLEN` on all six names (`tasks.py:122-134`); five of
them are keys that no producer has ever written, so Redis returns 0.

### What actually happens

The Background Jobs screen shows six queue cards. Five of them - Imports,
Exports, Notifications, Provisioning, Reports - are permanently at depth 0,
throughput 0, status HEALTHY, with a flat depth-trend bar, no matter how
backed up the system is. The one queue that carries the actual work, `celery`,
is presented as a sixth peer rather than as the only real one.

The seeded "Notifications backlog" rule
(`QUEUE_DEPTH > 2000`, `target_queue="notifications"`, `seed.py:146`) reads a
key that is always 0, so it can never fire under any circumstances.

`capture_queue_snapshot_task` still writes one row per queue per minute
(`tasks.py:178-198`), which is 8,640 rows/day of which 7,200 are known-zero.

### The fix

Either add the `task_routes` the design assumes, so the six queues are real and
the cards mean something, or cut `KNOWN_QUEUES` down to what actually runs and
delete the notifications rule until there is a queue for it to watch. Both are
defensible; showing five queues that do not exist is not.

---

## 11. The queue card's throughput, retry and dead figures each measure something else

**Medium.**

### The defect

Four labels, four mismatches, all in `capture_queue_snapshot_task`:

```python
# tasks.py:165
recent = BackgroundJob.objects.filter(created_at__gte=window_start)
```

- **`throughput_per_min`** is documented as "Tasks completed in the trailing
  minute" (`models/queues.py:26`). It counts jobs **created** in the trailing
  minute that happen to be `SUCCEEDED` now (`tasks.py:170-171`). A job created
  four minutes ago and finishing this second is never counted by any tick.
- **`retrying`** is assigned the count of `RUNNING` jobs
  (`tasks.py:182`). Running is not retrying.
- **`dead`** is hardcoded to `0` on every row (`tasks.py:194`). Nothing in the
  platform tracks a dead-letter count.
- **`retry_storm`** ("Abnormal retry spike detected", `models/queues.py:33`) is
  `failed >= 50` (`tasks.py:183`) - a failure count, in a one-minute window, with
  no reference to retries at all.

### What actually happens

A school triggers a bulk import that takes six minutes. For all six of those
minutes the Exports/Imports card shows throughput 0 and "1 retrying". When it
finishes, the tick that would have counted it is looking at jobs created in the
last sixty seconds, and the job was created six minutes ago - so throughput
stays 0 for that minute too. The queue processed a job and the throughput chart
never moved.

### The fix

Window on `finished_at` for throughput and failures; count `QUEUED` for backlog
and leave `retrying` unset (or add a real retry counter to `BackgroundJob`)
rather than filling it with a different number; delete `dead` or wire it to a
real dead-letter list; rename `retry_storm` to `failure_spike` and keep the
threshold, or measure retries and keep the name.

---

## 12. `UptimeCheck.interval_sec` is ignored

**Medium.**

### The defect

Every active check runs on every task invocation:

```python
# tasks.py:43
for check in UptimeCheck.objects.filter(is_active=True).select_related("service"):
```

and the task is scheduled every five minutes
(`apps/celery.py:129-132`). `interval_sec` (`models/uptime.py:39`) is read by
nothing.

### What actually happens

The SSL certificate check is deliberately configured hourly
(`seed.py:124`, `interval=3600`) because a certificate's expiry date does not
change between minutes. It runs 288 times a day instead of 24, each run opening
a TLS connection to `api.codexng.com` and doing a full handshake
(`probes.py:148-151`).

The cost is small; the misleading part is that an operator who lengthens a
check's interval to reduce load will see no change at all, and no error telling
them why.

### The fix

Skip a check whose newest result is younger than its `interval_sec`:

```python
last = check.results.order_by("-checked_at").values_list("checked_at", flat=True).first()
if last and (timezone.now() - last).total_seconds() < check.interval_sec:
    continue
```

One extra query per check per tick, against eight checks. Alternatively drop the
field, but the SSL case is a real reason to keep it.

---

## 13. MTTA and MTTR are permanently null

**Medium.**

### The defect

`reliability_stats` computes both from timestamps
(`services.py:613-631`):

```python
if inc.acknowledged_at:
    acks.append(...)
if inc.resolved_at:
    resolves.append(...)
```

Nothing sets `acknowledged_at`. There is no acknowledge endpoint, the update
serializer accepts the field but no UI path is documented to send it, and the
`IncidentEvent.Kind.ACK` timeline kind (`models/incidents.py:90`) is never
written by any code path.

`resolved_at` is set in exactly one place - `_maybe_resolve_auto_incident`
(`tasks.py:347-349`) - which only runs for `source = AUTO` incidents whose
alerts have all cleared. The manual path does not:

```python
# serializers.py:91-102 (IncidentCreateUpdateSerializer.update)
for k, v in validated_data.items():
    setattr(instance, k, v)
instance.save()
...
if "status" in validated_data and validated_data["status"] != prev_status:
    instance.add_event(kind="status", ...)
```

A `PATCH {"status": "resolved"}` writes the status and the timeline entry and
leaves `resolved_at` null.

### What actually happens

An engineer works `INC-2041` for forty minutes and marks it Resolved. The
incident list correctly shows it as resolved. The Reliability screen shows
`mttr_min: null` and `mtta_min: null`, and will keep showing null however many
incidents the team handles, because the only incidents that ever carry a
`resolved_at` are the ones a machine closed and none of them ever carry an
`acknowledged_at`.

### The fix

Stamp both in the serializer's `update`: set `resolved_at = now()` on the
transition into RESOLVED (and clear it on the transition out), and set
`acknowledged_at = now()` on the first transition away from INVESTIGATING if it
is still null. Add an explicit `POST /incidents/<id>/ack/` that writes the
timestamp and an `ACK` timeline row, since acknowledging is a distinct act from
changing status.

---

## 14. War-room attribution is free text supplied by the caller

**Medium.**

### The defect

```python
# serializers.py:36-40
class IncidentEventCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentEvent
        fields = ["kind", "who", "text"]
```

```python
# views.py:307-309
serializer = IncidentEventCreateSerializer(data=request.data)
serializer.is_valid(raise_exception=True)
event = IncidentEvent.objects.create(incident=incident, **serializer.validated_data)
```

`who` is a `CharField` written verbatim from the request body. The view knows
`request.user` and does not use it.

The same pattern runs through the incident itself: `Incident.owner` is a real
foreign key to a real user (`models/incidents.py:50-53`) and **no code anywhere
writes it**. The create serializer exposes `owner_label`, a free-text string
(`serializers.py:65-77`), and `create` never touches `owner`
(`serializers.py:79-89`).

### What actually happens

Two problems from one root.

Any holder of `platform.health.manage` can post a timeline entry reading
`{"kind": "update", "who": "Ada Nwosu", "text": "Confirmed root cause is the
payments migration."}` without being Ada. The war-room timeline - the record a
post-mortem is written from - carries no verifiable authorship at all.

And because `owner` is never populated, `Incident.owner` is a permanently null
column, the `owned_incidents` reverse accessor returns nothing for everyone, and
"who owns this incident" is answerable only by whatever string somebody typed.

### The fix

Set `who` from `request.user` in the view and make it read-only on the
serializer, keeping `owner_label` only as a display fallback for
machine-authored entries ("Alertmanager"). Set `owner = request.user` on manual
create, and expose `owner` on the update serializer so ownership can be handed
over deliberately.

---

## 15. A malformed `?start=` / `?end=` is a 500

**Medium. Confirmed by execution.**

### The defect

```python
# services.py:56-60
if start_raw and end_raw:
    from django.utils.dateparse import parse_datetime
    start, end = parse_datetime(start_raw), parse_datetime(end_raw)
```

`parse_datetime` returns `None` for a string that does not look like a datetime,
which the guard on the next line handles - but it **raises `ValueError`** for a
string that looks right and is not a real date. `core/exceptions.py` has no
`ValueError` handler, so it becomes a 500.

Confirmed:

```text
GET /v1/health/overview/?tenant=codex&start=2026-13-45T00:00:00Z&end=2026-01-01T00:00:00Z  -> 500
```

This is the same defect class as `vs_notifications` §7 and `vs_config`'s UUID
actor filter: an unvalidated raw string from the query params reaching a parser
that raises.

### What actually happens

A date picker that lets someone type the date, or any hand-built URL, turns the
Command Center into a 500. There is no message telling the caller which
parameter was wrong.

### The fix

Validate the range parameters through a small serializer
(`DateTimeField(required=False)` × 2 plus a `ChoiceField` for `range`) and
return a 400 naming the offending field. That also fixes the silent
naive-datetime comparison: `parse_datetime` on a string with no timezone returns
a naive value, which the ORM then interprets in the server timezone with a
`RuntimeWarning`.

---

## 16. The 30-day range can never show more than seven days

**Medium.**

### The defect

The range vocabulary offers up to 30 days, and a custom window up to 90:

```python
# services.py:42-50
"7d":  (timedelta(days=7),  "hour", 28),
"30d": (timedelta(days=30), "day",  30),
# services.py:60
if start and end and start < end and end - start <= timedelta(days=90):
```

Retention deletes the underlying rows at seven days, and nothing rolls them up
first:

```python
# tasks.py:403-404
deleted["request_metrics"] = RequestMetric.objects.filter(
    bucket_start__lt=now - timedelta(days=7)).delete()[0]
```

`UptimeCheckResult` is pruned at seven days too (`tasks.py:405-406`), but that
one is safe: `rollup_uptime_daily_task` writes a durable
`UptimeDailyRollup` first, which is why the 90-day uptime bar works. There is no
equivalent daily rollup for request metrics.

### What actually happens

An engineer selects "30d" on the Command Center to see whether latency has been
creeping up since the last release. The chart draws seven days, labelled 30d,
with no gap and no note. The trend it appears to show is a 7-day trend. The same
applies to the traffic, error-rate and endpoint tables, and to the
`vs-previous` delta on all four KPI tiles: for any range longer than about three
and a half days, the previous window is entirely inside the pruned region, so
`prev` is 0 and `_delta` (`services.py:160-164`) returns a flat `100.0`.

### The fix

Either add a `RequestMetricDailyRollup` (route, method, tenant, day, counts,
merged histogram) written by a beat task before the prune, and read it for
ranges past the retention window - the histogram merges cleanly, which is what
it was designed for - or cut the offered ranges to what the data supports and
cap the custom window at seven days.

---

## 17. The Command Center computes its request series twice

**Medium. Efficiency.**

### The defect

```python
# views.py:113-115
"kpis": services.golden_signals(tr, tenant_id),
"services": services.service_grid(),
"request_series": services.request_series(tr, tenant_id),
```

`golden_signals` already calls `request_series(tr, tenant_id)` internally to
build its three sparklines (`services.py:183-186`) and throws the full series
away. The view then computes the identical series again. Each computation is two
queries plus a full scan of every matching row's `latency_hist` JSON into Python
(`services.py:228-242`).

`ApiEndpointsView` has a milder version of the same: `_tenant_id(request)` is
called twice (`views.py:191`, `:200`).

The uptime detail views are worse in kind:

```python
# views.py:145 (ServiceDetailView, for one service)
monitors = {m["key"]: m for m in services.uptime_monitors()}
# views.py:175 (UptimeMonitorDetailView, for one monitor)
monitor = next((m for m in services.uptime_monitors() if m["key"] == key), None)
```

`uptime_monitors()` loops every active service and issues three queries each
(`services.py:542-561`): rollups, the last 48 results, and the newest SSL
result. With the twelve seeded services that is ~36 queries and a full
90-day rollup fetch, to render one service's card.

### The fix

Have `golden_signals` accept and return the series it already computes, and pass
it through in the view. Give `uptime_monitors` an optional `service_key`
argument and use it from both detail views.

---

## 18. Unbounded scans and unpaginated lists

**Medium.**

### The defect

The analytics layer streams whole columns into Python rather than aggregating in
the database:

- `_saturation` fetches every `UptimeCheckResult.meta` in the window and loops
  in Python to find one maximum (`services.py:263-270`). With probes every five
  minutes across eight checks, a 30-day custom window is ~69,000 JSON blobs.
- `request_series` (`services.py:241-242`) and `endpoint_stats`
  (`services.py:333-334`) each fetch every matching row's `latency_hist` - an
  18-element JSON list - to merge per bucket and per route. Both run a second
  full pass over the same queryset that was already aggregated.
- `refresh_module_service_statuses` does the same for its window
  (`tasks.py:102-106`).

And five list endpoints return everything with no page envelope, because they
are `APIView`s returning dicts rather than `ListAPIView`s:

| Endpoint | Returns | Bound |
|---|---|---|
| `GET /services/` | every active service | 12 today, unbounded |
| `GET /uptime/monitors/` | every service × 90 segments × 48 results | unbounded |
| `GET /api-endpoints/` | one row per (route, method) in the window | unbounded, grows with the URLconf |
| `GET /tenants/` | one row per tenant with traffic | unbounded, grows with the customer base |
| `GET /slos/` | every active SLO | unbounded |

`GET /api-endpoints/` is the one that will hurt first: the platform already has
hundreds of routes and the payload carries a full status/percentile/code
breakdown per row, plus a duplicate `status_code_series`.

### The fix

Push `mem_pct` out of `meta` into a real column on `UptimeCheckResult` so
saturation is a `Max()` aggregate. Fetch the histograms once and reuse them
across the series and the totals. Paginate `api-endpoints` and `tenants`
through `XVSPagination` like the rest of the platform, or cap them explicitly
and say in the response what was dropped.

---

## 19. Not one write in the module emits an audit event

**Medium.**

### The defect

`vs_health` has six write surfaces - open an incident, update an incident,
append a timeline entry, create an alert rule, change an alert rule, annotate a
deployment - and none of them calls `emit_audit_event`. The app contains no
import of `vs_audit` at all.

By comparison, `vs_config` writes an immutable audit row in the same
transaction as every mutation (`vs_config/services/audit.py:19-56`).

### What actually happens

`platform.health.manage` is a SENSITIVE key (`seed.py:62`) that can disable any
alert rule. Someone switches off the SEV1 "API error rate" rule at 23:40 and
switches it back on at 06:10. Nothing anywhere records that it happened, who did
it, or that the platform spent the night with its loudest alarm muted. The rule
row's own `updated_at` moves and that is the entire trail.

### The fix

Emit on the four state-changing paths: rule enable/disable and threshold edit,
incident status transition, timeline append, deployment annotation. Rule
disable is the one that matters most and is a two-line change.

---

## 20. `platform.health.*` is granted to nobody but the super admin

**Medium.**

### The defect

`seed_permissions` creates the two `Permission` rows and stops
(`seed.py:46-76`). It writes no `TenantRolePermission` and no
`PrebuiltRolePermission`. The only thing that ever grants them is the catch-all
reconciliation at the end of `seed_all_permissions`, which targets exactly one
role:

```python
# core/management/commands/seed_all_permissions.py:130-134
role = TenantRoleTemplate.objects.filter(
    key="xvs_super_admin", tenant__slug="codex", tenant__kind="PLATFORM",
).first()
```

and `xvs_super_admin` already bypasses RBAC entirely
(`vs_rbac/permissions.py:300-302`).

### What actually happens

`xvs_platform_admin` - the role `seed_dev_data` hands to the CX C-Suite
(`core/management/commands/seed_dev_data.py:290`) - gets 403 on every health
route. On the console landing screen the health card is simply absent, because
a section the caller cannot see is omitted rather than zeroed
(`vs_admin_console/overview.py:33-35`), so there is no visible clue that
anything was withheld.

This is the same shape as the `vs_config` and `vs_exports` findings: a feature
that exists, is tested, and is unreachable for everyone who is not the single
super-admin account.

### The fix

Grant both keys to `xvs_platform_admin` in `seed_permissions`, the way
`seed_config_permissions` grants to its two platform roles. The scope column
already stops the keys travelling anywhere they should not (see below), so this
is safe.

**Worth recording as a strength:** unlike `platform.schools` in `vs_exports`
and `platform.audit.view` in `vs_audit`, these keys are declared
`PermissionScope.PLATFORM` at creation (`seed.py:71`) and classified PLATFORM by
`vs_rbac/migrations/0007_classify_permission_scope.py`. `assert_tenant_may_hold`
(`vs_rbac/models.py:91-110`) therefore refuses to attach them to any school
role, so the cross-tenant aggregates on `/tenants/` cannot leak into a school's
own console. That boundary is correct here.

---

## 21. The probes run inside the thing they monitor

**Medium. Architectural.**

### The defect

Every probe executes inside the platform's own Celery worker
(`tasks.py:35-69`), against the platform's own public URL
(`seed.py:19-20`, `PROBE_BASE = "https://api.codexng.com"`). And a service's
status is only written when it changes:

```python
# models/registry.py:55-60
def set_status(self, status: str) -> None:
    if status != self.current_status:
        self.current_status = status
        self.status_changed_at = timezone.now()
        self.save(update_fields=[...])
```

There is no `last_checked_at` on `MonitoredService` and no staleness check
anywhere in `service_grid` (`services.py:447-459`) or `overall_posture`.

### What actually happens

The Render instance runs out of memory at 03:00 and the whole process group
dies - web and worker together. No probe runs, so no CRITICAL result is
recorded, so no status changes, so no alert fires.

When the platform comes back at 07:00 and someone opens the Command Center, the
grid shows the statuses as of 02:55 with `status_changed_at` timestamps from
whenever each card last moved. A four-hour total outage is invisible, and worse,
the screen is affirmatively green about it.

### The fix

This cannot be fully solved from inside the monolith - external uptime
monitoring is a different product - but two things make the limitation honest:

1. Add `last_checked_at` to `MonitoredService`, write it on every probe
   regardless of whether the status changed, and demote any service whose last
   check is older than a few intervals to UNKNOWN with a "stale" reason.
2. Say in the UI when the data was last refreshed, which is a one-field change
   once (1) exists.

---

## 22. School vocabulary in an engine app

**Low.**

`vs_health` sits in `apps/` and is therefore an engine app, which under the
standing rule means the word "school" belongs to `apps/schools/` and not here.
Two places break it:

```python
# constants.py:89-93
ROUTE_PREFIX_SERVICES = {
    "schools": ("/v1/i/",),
    ...
}
# seed.py:28
("schools", "Schools & Onboarding", "Modules", "Tier 2", "internal", 40),
```

The service key `schools` and the display name "Schools & Onboarding" are baked
into an engine's constants and seed data, and the second one is what a VIGIL
operator would read on their own health console.

Unlike the `vs_notifications` finding, there is no *import* of `vs_schools`
here - this is vocabulary, not coupling, and it is a two-line rename. The
neutral form is a key of `onboarding` (or a per-domain registry entry) with the
route prefix supplied by whichever domain owns `/v1/i/`.

`RequestMetric`'s docstring is correct on the harder point and worth keeping:
"``tenant`` ... is for slicing, not isolation - the table is global
observability data gated by platform RBAC" (`models/request_metrics.py:26-31`).

---

## 23. Three surfaces display numbers that can never change

**Low.**

- **`affected_tenant_count`** is a real column on `Incident`
  (`models/incidents.py:60`), is serialised into the incident list
  (`serializers.py:42-54`) and is written by nothing. Every incident on every
  screen reports 0 tenants affected.
- **The noisy-neighbour flag** is `reqs > avg_reqs * 3` where the average
  includes the tenant being tested (`services.py:417-428`). With one tenant the
  average equals the value and nothing is ever noisy; with two tenants one would
  have to carry more than 85% of all traffic. The flag only becomes reachable at
  four or more active tenants.
- **The traffic KPI's status is hardcoded** to HEALTHY
  (`services.py:204`), so the Traffic tile is green during a total traffic
  collapse. That is arguably right - traffic is not a health signal on its own -
  but a tile that renders a status badge which is a constant should not render
  one.

---

## 24. Smaller defects and dead code

**Low.** Each is a line or two.

1. **`ServiceDetailView` does not filter on `is_active`**
   (`views.py:141`), so a retired service - the ones `seed_services` explicitly
   deactivates so "the console never shows unmonitorable services"
   (`seed.py:88-91`) - is still fully readable by key.
2. **`_status_for_error_rate` is applied to a window with zero requests.**
   `_totals` returns `error_rate = 0.0` when `reqs` is 0
   (`services.py:150`), which reads as a perfect score rather than no data.
   `window_status` guards this correctly (`services.py:301-312`) but
   `golden_signals` calls `_status_for_error_rate` directly
   (`services.py:209-210`) behind its own `enough_samples` check, so the guard
   is duplicated rather than shared.
3. **`row[2][:HISTOGRAM_SIZE]` assumes a non-null histogram**
   (`tasks.py:105`). `latency_hist` has a callable default so ordinary writes
   are safe, but a row written by a fixture or a data migration with an explicit
   `None` makes the beat task raise `TypeError` and stops every module service
   from updating.
4. **`get_or_create` under `select_for_update` does not prevent a duplicate
   insert** (`collectors.py:123-133`): the lock only exists once the row does. On
   a real collision the `IntegrityError` is caught by the broad handler at
   `collectors.py:159-160` and that bucket's counts are dropped silently, with
   only a `logger.warning` behind it.
5. **Buffered metrics are lost on shutdown.** `_ensure_flusher` starts a daemon
   thread (`collectors.py:185-201`) and nothing flushes on `SIGTERM`, so every
   deploy discards up to `HEALTH_METRICS_FLUSH_SECONDS` of requests. Small, and
   worth a line in the docs rather than a fix.
6. **Unused imports.** `Count` and `F` in `services.py:13`; `Count` in
   `tasks.py:12`; `Incident` in `tasks.py:287`; `settings` in
   `models/registry.py:10`. `views.py:274` re-imports `Q` inside
   `IncidentListCreateView.get_queryset` although it is already imported at
   `views.py:15`.
7. **`MonitoredServiceSerializer` is defined and never used**
   (`serializers.py:21-27`). Every service payload in the module is hand-built
   in `services.service_grid` and `views.ServiceDetailView`.
8. **`reliability_stats` fetches whole `Incident` rows to read two timestamps**
   (`services.py:616-624`) and then issues two more `COUNT` queries over the same
   queryset (`services.py:628-629`).
9. **`Deployment` is writable in full**, including `deployed_at` and `actor`
   (`serializers.py:128-133`, only `id` is read-only), so a deploy annotation can
   be backdated to any time and attributed to anyone. Given deployments are chart
   annotations rather than records of record, this is a note rather than a
   defect - but `actor` should come from `request.user` for the manual case.
10. **`percentile_from_hist` returns the top bound for the overflow bucket**
    (`services.py:109-111`), so any p99 above 10s reports exactly 10000.0. This
    is documented in the docstring and correct by design; it is listed here only
    so nobody reads a wall of 10000.0 values as a coincidence.

---

## Recommended order of work

**Fix immediately - the screens do not work:**

1. §1 and §2 together - one parameter rename unblocks the entire module. The
   Background Jobs table currently 500s on every reachable request.
2. §7 - three probes pointed at nothing keep the platform permanently yellow,
   and feed §8 into a permanent SLO breach.
3. §8 - stop counting "slow" as "down".

**Fix next - the alerting does not alert:**

4. §3 - wire `channel` into `vs_notifications`, or delete the field.
5. §5 - implement sustained-for and resolve hysteresis, which also defuses §6.
6. §6 - allocate incident codes from an integer sequence.
7. §4 - scope request-metric rules to their target service.

**Then - stop claiming green with no data:**

8. §9 - return null/UNKNOWN for empty windows in all four places.
9. §21 - `last_checked_at` plus a staleness demotion.
10. §13 - stamp `resolved_at` and `acknowledged_at`.

**Then - correctness of what is displayed:**

11. §10 and §11 - make the queue cards mean what they say.
12. §16 - a daily rollup for request metrics, or shorter offered ranges.
13. §15 - validate the range parameters.
14. §14 and §19 - real attribution and an audit trail on the manage surface.

**Then - cost and hygiene:**

15. §17, §18 - the duplicate series, the per-monitor grid fetch, pagination.
16. §12, §20, §22, §23, §24.
