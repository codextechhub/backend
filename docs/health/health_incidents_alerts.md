# health_incidents_alerts

The only write surface in `vs_health`, and the only place the module makes a
judgement rather than a measurement: threshold rules, the alerts they fire, the
incidents those alerts open and close automatically, the war-room timeline an
operator writes by hand, the reliability statistics computed from all of it, and
the deployment annotations drawn alongside.

Routes covered by this slice, mounted at `/v1/health/` (`apps/urls.py:39`):
`incidents/`, `incidents/<uuid:id>/`, `incidents/<uuid:id>/events/`,
`incidents/reliability/`, `alerts/`, `alert-rules/`, `alert-rules/<uuid:id>/`,
`deployments/`.

Request metrics are `health_signal_collection`; probes and SLOs are
`health_uptime_availability`; queues are `health_queues_jobs`.

---

## 1. What it is (and what it is NOT)

- **A rule is a threshold, not a policy.** `AlertRule` holds one metric, one
  comparator, one threshold and one target (`models/incidents.py:109-160`).
  There is no expression language, no composition, and no per-tenant rule.
- **An alert is an instance of a breach; an incident is the thing people work.**
  Every auto-fired alert opens exactly one incident and links to it
  (`tasks.py:295-304`), so the two are one-to-one on the way in and
  many-to-one on the way out - several rules can point at one incident, and it
  resolves only when all of them have cleared
  (`tasks.py:337-350`).
- **Incidents come from two sources and behave differently.** `source = AUTO`
  incidents are opened and resolved by the beat task. `source = MANUAL`
  incidents are authored by an operator and are never touched by automation -
  `_maybe_resolve_auto_incident` returns immediately for them
  (`tasks.py:339-340`).
- **The timeline is append-only in practice, not by constraint.**
  `IncidentEvent` has a create endpoint and no update or delete route, but
  nothing in the model prevents an edit and no audit row records one.
- **`duration_sec` does not do what it says.** The field is documented as
  "Sustained-for window before firing" (`models/incidents.py:130`) and is used
  only to widen the *measurement* window past fifteen minutes
  (`tasks.py:246-248`). Alerts fire on the first breaching evaluation.
- **`channel` is a string nothing reads.** No notification, email, page or
  webhook is sent anywhere in this module. The alert engine writes rows and
  stops.
- **`target_service` is honoured for three metrics and ignored for two.**
  Queue depth, SSL days and uptime resolve against the named target; error rate
  and p95 latency measure the whole platform and then attribute the result to
  the target anyway (`tasks.py:254-261`).
- **`breaches(None)` is False, deliberately** (`models/incidents.py:147-150`).
  "No evaluable signal" therefore neither fires a new alert nor blocks an open
  one from resolving - a design choice that is stated in
  `_current_metric_value`'s docstring (`tasks.py:235-238`) and is right.
- **Nothing here is tenant-scoped.** Incidents, alerts, rules and deployments
  have no tenant column. They are facts about the platform.
- **This is not an audit trail.** Not one write in this slice emits an
  `AuditEvent`, including disabling a SEV1 rule.

## 2. Domain model

### `Severity` (`models/incidents.py:21-25`)

An `IntegerChoices`: SEV1 (1, Critical) through SEV4 (4, Low). Lower is worse,
which matters because `Meta.ordering` on `Incident` is by `-started_at`, not by
severity.

### `AlertRule` (`models/incidents.py:109`)

| Field | Meaning |
|---|---|
| `name` | Free text; the seeder matches on it, so it is effectively the key |
| `metric` | `error_rate` / `p95_latency` / `queue_depth` / `ssl_days_left` / `uptime_pct` |
| `comparator` | `gt` / `gte` / `lt` / `lte` |
| `threshold` | Float |
| `duration_sec` | **Not implemented as documented** - see §5 |
| `severity` | Inherited by the alert and the incident it opens |
| `target_service` | FK, nullable. "Null = applies platform-wide" |
| `target_queue` | For `queue_depth` rules; defaults to `celery` at evaluation |
| `channel` | **Read by nothing** |
| `is_enabled` | Only enabled rules are evaluated; preserved across re-seeds |

`breaches(value)` (`models/incidents.py:147-160`) is the whole comparison, and
returns `False` for a `None` value and for an unrecognised comparator.

The five seeded rules (`seed.py:140-149`):

| Name | Metric | Test | Duration | Severity | Target | Channel |
|---|---|---|---|---|---|---|
| API error rate | `error_rate` | `> 5` | 300s | SEV1 | service `api` | PagerDuty |
| p95 latency SLO | `p95_latency` | `> 800` | 600s | SEV2 | platform-wide | Slack #sre |
| Notifications backlog | `queue_depth` | `> 2000` | 0 | SEV2 | queue `notifications` | Zoho Cliq |
| SSL expiry | `ssl_days_left` | `< 14` | 0 | SEV3 | service `dns` | Email |
| API uptime SLO | `uptime_pct` | `< 99.5` | 0 | SEV2 | service `api` | PagerDuty |

The 800ms threshold and its history are documented in the seeder
(`seed.py:142-144`): the rule was originally 400ms, sized for a bigger instance,
and fired on ordinary billing and report aggregates. Re-seeding repairs a
deployed row's threshold rather than leaving the old value firing forever, and
that repair is tested.

### `Alert` (`models/incidents.py:163`)

`rule` FK, `severity`, `title`, `service` FK (SET_NULL), `value`, `threshold`,
`status` (`firing` / `resolved`, indexed), `fired_at`, `resolved_at`, and
`incident` FK (SET_NULL).

At most one FIRING alert per rule at a time - not by constraint, but because the
evaluator checks for an open one before creating another
(`tasks.py:293-295`).

Resolved alerts older than 30 days are deleted (`tasks.py:409-410`); the
incident survives because the FK is SET_NULL.

### `Incident` (`models/incidents.py:28`)

| Field | Meaning |
|---|---|
| `code` | Human reference, unique, `INC-2041` |
| `title`, `summary`, `postmortem` | Free text |
| `severity` | Indexed |
| `status` | `investigating` / `identified` / `monitoring` / `resolved`, indexed |
| `source` | `manual` / `auto` - decides whether automation may close it |
| `owner` | FK to a real user - **written by nothing** |
| `owner_label`, `team` | Free-text stand-ins that are used instead |
| `services` | M2M to `MonitoredService` |
| `affected_tenant_count` | **Written by nothing**; always 0 |
| `started_at` | Indexed, defaults to now |
| `resolved_at` | Set by automation only |
| `acknowledged_at` | Set by nothing |

`is_active` is `status != RESOLVED` (`models/incidents.py:76-78`).
`add_event` is the one helper (`models/incidents.py:80-82`).

### `IncidentEvent` (`models/incidents.py:85`)

`incident` FK (CASCADE), `kind` (`opened` / `ack` / `update` / `status` /
`resolved`), `who` (free text, max 120), `text`, `created_at`. Ordered
chronologically, which is right for a timeline.

`ACK` is a defined kind that no code path ever writes.

### `Deployment` (`models/registry.py:63`)

`version`, `environment` (default "production"), `kind`
(`deploy` / `flag` / `config`), `actor`, `text`, `deployed_at` (indexed).
A chart annotation, not a record of record. Nothing in the platform writes one
automatically; they arrive only through `POST /deployments/`.

## 3. Endpoint map

Read routes use `HealthViewMixin`; write routes use `HealthWriteMixin`, which
resolves the key from the HTTP method:

```python
# views.py:56-63
class HealthWriteMixin:
    permission_classes = PERMS

    @property
    def rbac_permission(self):
        method = getattr(getattr(self, "request", None), "method", "GET")
        return PERM_VIEW if method in SAFE_METHODS else PERM_MANAGE
```

`?tenant=<slug>` is mandatory on all of them (the auth-layer default).

| Method + path | Permission | Notes |
|---|---|---|
| `GET /incidents/` | `platform.health.view` | `?status=active` is a convenience filter over all non-resolved states; any other value matches exactly. `?severity=` is an exact match |
| `POST /incidents/` | `platform.health.manage` | `code` auto-allocated when omitted; `source` forced to MANUAL |
| `GET /incidents/<uuid>/` | `platform.health.view` | Includes the full timeline |
| `PATCH /incidents/<uuid>/` | `platform.health.manage` | A status change appends a `status` timeline entry |
| `POST /incidents/<uuid>/events/` | `platform.health.manage` | 404 on an unknown incident; 201 on success |
| `GET /incidents/reliability/` | `platform.health.view` | MTTA/MTTR/counts, fixed 30-day window |
| `GET /alerts/` | `platform.health.view` | Defaults to `?status=firing`; `resolved` for history; anything else returns everything |
| `GET /alert-rules/` | `platform.health.view` | Paginated |
| `POST /alert-rules/` | `platform.health.manage` | |
| `GET /alert-rules/<uuid>/` | `platform.health.view` | |
| `PATCH /alert-rules/<uuid>/` | `platform.health.manage` | The `is_enabled` toggle |
| `GET /deployments/` | `platform.health.view` | Paginated |
| `POST /deployments/` | `platform.health.manage` | |

`PUT` on the detail routes is available (`RetrieveUpdateAPIView`) and resolves
to the same `manage` key. There is no `DELETE` anywhere in the module.

Ordering note: `incidents/reliability/` is declared **before**
`incidents/<uuid:id>/` in the URLconf (`urls.py:48-49`), which is what stops the
literal segment being swallowed by the converter.

### Request bodies actually read

`POST /incidents/` and `PATCH /incidents/<id>/`
(`serializers.py:65-103`) read exactly:

```jsonc
{"code": "INC-2041",              // optional; auto-allocated when omitted
 "title": "Checkout failing for Corona",
 "severity": 2,
 "status": "investigating",
 "owner_label": "Ada Nwosu",      // free text, not a user reference
 "team": "Platform",
 "services": ["api", "payments"], // slugs, resolved by key
 "summary": "…",
 "postmortem": "…",
 "started_at": "2026-08-20T09:14:00Z",
 "resolved_at": null,
 "acknowledged_at": null}
```

`source` is not in the field list; `create` forces it to MANUAL
(`serializers.py:86`). `owner` is not in the field list and is never set.

`POST /incidents/<id>/events/` (`serializers.py:36-40`) reads three fields, all
supplied by the caller:

```jsonc
{"kind": "update", "who": "Ada Nwosu", "text": "Rolled back the payments migration."}
```

`POST /alert-rules/` (`serializers.py:105-115`) reads `name`, `metric`,
`comparator`, `threshold`, `duration_sec`, `severity`, `target_service_key`
(a slug, nullable), `target_queue`, `channel`, `is_enabled`.

`POST /deployments/` (`serializers.py:128-133`) reads `version`,
`environment`, `kind`, `actor`, `text`, `deployed_at` - only `id` is read-only,
so a deployment can be backdated and attributed to anyone.

### Serializer field sets

| Serializer | Fields |
|---|---|
| `IncidentListSerializer` (`serializers.py:42`) | `id`, `code`, `title`, `severity`, `severity_label`, `status`, `source`, `owner_label`, `team`, `service_keys`, `affected_tenant_count`, `started_at`, `resolved_at` |
| `IncidentDetailSerializer` (`serializers.py:56`) | the above plus `summary`, `postmortem`, `acknowledged_at`, `timeline`, `created_at`, `updated_at` |
| `IncidentEventSerializer` (`serializers.py:29`) | `id`, `kind`, `who`, `text`, `created_at` |
| `AlertRuleSerializer` (`serializers.py:105`) | `id`, `name`, `metric`, `comparator`, `threshold`, `duration_sec`, `severity`, `target_service_key`, `target_queue`, `channel`, `is_enabled` |
| `AlertSerializer` (`serializers.py:117`) | `id`, `rule_name`, `severity`, `title`, `service_key`, `value`, `threshold`, `status`, `fired_at`, `resolved_at`, `incident_id` - all read-only |

`severity_label` is `get_severity_display()`, so the list carries both the
integer and "SEV1 - Critical". `service_keys` is a `SerializerMethodField` over
the prefetched M2M (`serializers.py:53-54`).

No field in this slice is masked or FLS-protected. Nothing here is personal
data: `owner_label` and `who` are operator-typed strings, and the only
identifiers are service keys.

## 4. Lifecycle / state machine

### The alert cycle, evaluated every minute (`apps/celery.py:137-140`)

```text
for each enabled rule:
    value      = _current_metric_value(rule)        # None when no usable signal
    breaching  = rule.breaches(value)               # False when value is None
    open_alert = the rule's FIRING alert, if any

    breaching and no open alert   →  open an auto-incident
                                     create Alert(FIRING, incident=…)
    not breaching and open alert  →  Alert.status = RESOLVED, resolved_at = now
                                     value overwritten with the clearing value
                                     _maybe_resolve_auto_incident(alert.incident)
    otherwise                     →  nothing
```

There is no sustained-for gate on either transition, so a metric hovering at the
threshold flips both ways once a minute.

### The incident lifecycle

```text
                     ┌──────────────┐
 POST /incidents/ ─→ │ investigating │ ─ PATCH status ─→ identified ─→ monitoring ─→ resolved
                     └──────────────┘
        source = MANUAL, code auto-allocated, "opened" timeline entry

 alert fires ──────→ investigating          source = AUTO, owner_label "Alertmanager",
                                            team "Platform", "opened" timeline entry
        │
        └─ every linked alert clears ─→ resolved, resolved_at = now,
                                        "resolved" timeline entry
```

`_maybe_resolve_auto_incident` (`tasks.py:337-350`) is guarded four ways: null
incident, non-AUTO source, already resolved, or any linked alert still firing.
That last check is what makes the many-alerts-to-one-incident case correct.

A manual PATCH to `resolved` appends a `status` timeline entry
(`serializers.py:101-103`) and does **not** stamp `resolved_at`. Nothing anywhere
stamps `acknowledged_at`.

## 5. Derivations

- **Metric resolution** (`tasks.py:234-280`). The window starts as a 15-minute
  range and is widened to `duration_sec` only when that exceeds 900 seconds:

  ```python
  tr = services.parse_range("15m")
  if rule.duration_sec > 900:
      tr.start = tr.end - timedelta(seconds=rule.duration_sec)
  ```

  | Metric | Resolved from | Target honoured? |
  |---|---|---|
  | `error_rate` | `_totals(_base_qs(window))["error_rate"]` - **platform-wide** | no |
  | `p95_latency` | `percentile_from_hist(_merged_hist(_base_qs(window)), 95)` - **platform-wide** | no |
  | `queue_depth` | newest `QueueSnapshot.depth` for `target_queue or "celery"` | yes |
  | `ssl_days_left` | newest SSL `UptimeCheckResult.meta["ssl_days_left"]` for the target | yes, and `None` without a target |
  | `uptime_pct` | `Avg("uptime_pct")` over rollups from yesterday onward | yes, and `None` without a target |

- **The small-sample floor applies to alerting too** (`tasks.py:254-258`), and
  this is the module's best decision repeated in its second-most-important
  place:

  ```python
  if rule.metric in (ERROR_RATE, P95_LATENCY):
      totals = services._totals(qs)
      if totals["requests"] < MIN_P95_SAMPLE:
          return None
  ```

  One 500 out of five requests is a 20% error rate and would breach a 5%
  threshold. Below thirty requests there is nothing to evaluate, so the rule
  neither fires nor blocks an open alert from resolving. Four tests cover this
  (`tests.py:241-315`), including the case that matters most: traffic drying up
  must not pin an auto-incident open forever.

- **Alert title** (`tasks.py:297`):
  `f"{rule.name}: {value} {rule.get_comparator_display()} {rule.threshold}"`,
  e.g. `p95 latency SLO: 1240.0 > 800.0`. It becomes the incident title
  verbatim.

- **Incident code allocation** (`tasks.py:221-230`): take the highest existing
  `INC-` code by **string** sort, parse the digits after the hyphen, add one,
  default to 2000 when there is nothing to parse. Both the auto path and the
  manual create path use it (`serializers.py:79-82`).

- **MTTA / MTTR** (`services.py:613-631`), fixed 30-day window - the view passes
  no argument (`views.py:319-320`):

  ```text
  mtta_min = mean over incidents with acknowledged_at of (acknowledged_at - started_at) in minutes
  mttr_min = mean over incidents with resolved_at    of (resolved_at    - started_at) in minutes
  incidents = count in window
  active    = count in window that are not RESOLVED
  ```

  Both means are `None` when no incident carries the timestamp, which is the
  normal case (§8).

- **Incident filters** (`views.py:269-281`). `?status=active` expands to
  `~Q(status=RESOLVED)`; any other value is an exact match, so `?status=` with a
  typo silently returns nothing rather than an error. `?severity=` is passed
  straight to the ORM as an integer field lookup, so a non-numeric value is a
  `ValueError` and therefore a 500 - the same class of defect as the range
  parameters (`health_code_issues.md` §15).

- **Alert filters** (`views.py:328-333`). `?status=` defaults to `firing`;
  `firing` and `resolved` filter, and **any other value returns every alert**
  including resolved history, because the filter is applied only inside the
  membership test.

## 6. What writing writes

| Action | Written by | Rows written |
|---|---|---|
| Open a manual incident | `IncidentCreateUpdateSerializer.create` (`serializers.py:79-89`) | one `Incident` (source MANUAL), the M2M rows, one `opened` `IncidentEvent` |
| Update an incident | `.update` (`serializers.py:91-103`) | the `Incident`, the M2M rows, one `status` `IncidentEvent` **only if the status changed** |
| Append to the timeline | `IncidentEventCreateView.post` (`views.py:303-311`) | one `IncidentEvent` with caller-supplied `who` |
| Create/edit a rule | DRF generics (`views.py:337-349`) | one `AlertRule` |
| Annotate a deployment | DRF generics (`views.py:391-394`) | one `Deployment` |
| Fire an alert | `evaluate_alert_rules_task` (`tasks.py:295-304`) | one `Incident` (AUTO), its M2M row, one `opened` event, one `Alert` |
| Resolve an alert | same (`tasks.py:305-312`) | `Alert.status`/`resolved_at`/`value`; possibly `Incident.status`/`resolved_at` and one `resolved` event |
| Retention | `prune_health_metrics_task` (`tasks.py:409-410`) | deletes resolved alerts past 30 days |

**None of these writes an audit event.** `platform.health.manage` is a SENSITIVE
key that can disable a SEV1 rule, and the only trace of that is the rule row's
own `updated_at`.

**None of them run in an explicit transaction.** `_open_auto_incident` creates
the incident, adds the M2M row and appends the timeline entry as three separate
statements before the alert is created (`tasks.py:317-333`); a failure between
them leaves an incident with no alert pointing at it.

**Nothing is dispatched anywhere.** No notification, no email, no webhook.

## 7. Worked example

The p95 latency rule is enabled at 800ms, SEV2, platform-wide. At 09:03 the beat
task runs.

```text
_current_metric_value(rule)
  tr = 15-minute window (duration_sec is 600, not > 900, so it is not widened)
  totals["requests"] = 812        →  above MIN_P95_SAMPLE, evaluate
  percentile_from_hist(merged, 95) = 1240.0
rule.breaches(1240.0)  →  1240.0 > 800.0  →  True
no open alert          →  fire
```

`_open_auto_incident` allocates `INC-2041`, creates the incident and appends the
opening entry:

```jsonc
{"code": "INC-2041",
 "title": "p95 latency SLO: 1240.0 > 800.0",
 "severity": 2, "status": "investigating", "source": "auto",
 "owner_label": "Alertmanager", "team": "Platform",
 "summary": "Auto-opened from alert rule 'p95 latency SLO'. Observed 1240.0.",
 "affected_tenant_count": 0}
```

and the `Alert` row is written FIRING, linked to it. **Nobody is told.** The
rule's `channel` says "Slack #sre" and no message is sent (`health_code_issues.md`
§3).

Ada notices it herself at 09:20 and adds an update:

```text
POST /v1/health/incidents/8f1c…/events/?tenant=codex
{"kind": "update", "who": "Ada Nwosu", "text": "Report engine is the source; rolling back."}
```

```jsonc
{"success": true, "message": "Timeline updated.",
 "data": {"id": "…", "kind": "update", "who": "Ada Nwosu",
          "text": "Report engine is the source; rolling back.",
          "created_at": "2026-08-20T09:20:41Z"}}
```

`who` is whatever the request body said; the view knows `request.user` and does
not use it (`views.py:307-309`).

At 09:31 latency recovers to 610ms. The evaluator finds no breach and an open
alert, resolves it, and closes the incident because no other alert points at it:

```jsonc
{"fired": 0, "resolved": 1}
```

Now the failure mode. Latency is not steady - it oscillates around the
threshold:

```text
09:32  830ms  →  fires   INC-2042 opened
09:33  780ms  →  clears  INC-2042 resolved
09:34  815ms  →  fires   INC-2043 opened
09:35  795ms  →  clears  INC-2043 resolved
```

Four incidents in four minutes, each with a code, a summary and a timeline, none
of them information. `duration_sec = 600` was configured precisely to prevent
this and does nothing (`health_code_issues.md` §5). Left running, that
flapping burns through the `INC-` numbering fast enough to reach `INC-10000`
within a week, at which point the string-sorted allocator returns a duplicate,
`IntegrityError` escapes the task, and **the alert engine stops evaluating
anything at all** (`health_code_issues.md` §6).

Meanwhile the Reliability screen:

```text
GET /v1/health/incidents/reliability/?tenant=codex
```

```jsonc
{"mtta_min": null, "mttr_min": null, "incidents": 47, "active": 1, "window_days": 30}
```

47 incidents in the window and no mean time to anything, because
`acknowledged_at` is never written by any code path and `resolved_at` is written
only by automation.

## 8. Gotchas / known limitations

Full evidence for each is in **`error/health/health_code_issues.md`**. The items
belonging to this slice:

- **Nothing is ever alerted.** `channel` is filled in by the seeder for all five
  rules and read by no code (`seed.py:141-148`, `tasks.py:284-313`). An SEV1
  incident opens silently and waits for someone to open the screen (issues §3).
- **Error-rate and p95 rules ignore `target_service`** (`tasks.py:254-261`).
  A report-engine outage opens a SEV1 incident titled "API error rate" attached
  to the API card (issues §4).
- **`duration_sec` is not implemented** (`tasks.py:246-248`), so there is no
  sustained-for gate and no resolve hysteresis; a metric at the threshold opens
  an incident a minute (issues §5).
- **Incident codes are allocated by string sort** (`tasks.py:221-230`), which
  jams permanently at `INC-10000` and crashes immediately if an operator ever
  supplies a non-numeric code. Either way the beat task dies and no alert fires
  or resolves again (issues §6).
- **MTTA and MTTR are permanently null.** Nothing writes `acknowledged_at`,
  there is no acknowledge endpoint, the `ACK` timeline kind is never used, and a
  manual resolve does not stamp `resolved_at` (`serializers.py:91-103`,
  issues §13).
- **Timeline attribution is forgeable and `Incident.owner` is never written.**
  `who` is a caller-supplied string (`serializers.py:36-40`) and the `owner` FK
  has no writer anywhere in the codebase (issues §14).
- **No audit event on any write**, including disabling a SEV1 rule with a
  SENSITIVE permission (issues §19).
- **`affected_tenant_count` is always 0** - a real column on every incident
  payload that nothing computes (`models/incidents.py:60`, issues §23).
- **`?severity=` with a non-numeric value is a 500** (`views.py:278-280`), and
  `?status=` with a typo silently returns an empty list (issues §15, same
  class).
- **`?status=` on `/alerts/` with an unrecognised value returns every alert**,
  resolved history included, rather than the documented firing default
  (`views.py:328-333`).
- **`_open_auto_incident` is not transactional** (`tasks.py:317-333`): incident,
  M2M row, timeline entry and alert are four separate writes.
- **`Deployment` is fully writable including `deployed_at` and `actor`**
  (`serializers.py:128-133`), so an annotation can be backdated and attributed
  to anyone (issues §24.9).
- **`MonitoredServiceSerializer` is defined and never used**
  (`serializers.py:21-27`); every service payload in the module is hand-built
  (issues §24.7).
- **`reliability_stats` fetches whole incident rows to read two timestamps** and
  then issues two more COUNT queries over the same queryset
  (`services.py:616-629`, issues §24.8).
- **Justified by design:** `breaches(None)` is False, so "no signal" neither
  fires nor blocks a resolve (`models/incidents.py:147-150`).
- **Justified by design:** the small-sample floor gates alerting as well as
  status (`tasks.py:254-258`). This is the correct behaviour and it is the
  best-tested thing in the module.
- **Justified by design:** an auto-incident resolves only when every linked
  alert has cleared (`tasks.py:343-346`).
- **Justified by design:** automation never touches a MANUAL incident
  (`tasks.py:339-340`).
- **Justified by design:** re-seeding repairs a stale threshold on a deployed
  rule while preserving the operator's `is_enabled` toggle
  (`seed.py:150-176`). Both halves are tested.

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Scope | Seeded to |
|---|---|---|---|---|
| Every `GET` in this slice | `platform.health.view` | NORMAL | PLATFORM | nobody but `xvs_super_admin` |
| Every `POST` / `PUT` / `PATCH` | `platform.health.manage` | SENSITIVE | PLATFORM | nobody but `xvs_super_admin` |

Both keys are created by `seed_permissions` (`seed.py:46-76`) with
`scope=PermissionScope.PLATFORM` (`seed.py:71`), and `is_restricted` follows
from the sensitivity. `HealthWriteMixin` (`views.py:56-63`) picks between them
per HTTP method, so a caller holding only `view` can list incidents and rules
and cannot change one.

**There is no tenant dimension in this slice at all.** No model here has a
tenant column, no queryset is filtered by tenant, and `?tenant=` - mandatory
though it is - is used for nothing. That is correct: an incident is a fact about
the platform, not about a customer. The boundary is entirely carried by the
PLATFORM scope on the two keys, which `assert_tenant_may_hold`
(`vs_rbac/models.py:91-110`) enforces at every grant path.

Two consequences:

1. **A school can never see, or be told about, an incident that affects it.**
   There is no tenant-facing status page and no notification path (issues §3),
   so the customer-facing half of an incident is entirely manual and entirely
   outside this module.
2. **`xvs_platform_admin` cannot open an incident**, because neither key is
   granted to it (issues §20). In practice the only account that can work an
   incident is the single super-admin.

## 10. Code map

| File | Responsibility |
|---|---|
| `models/incidents.py:21-25` | `Severity` |
| `models/incidents.py:28-82` | `Incident`, `is_active`, `add_event` |
| `models/incidents.py:85-106` | `IncidentEvent` |
| `models/incidents.py:109-160` | `AlertRule`, `breaches` |
| `models/incidents.py:163-191` | `Alert` |
| `models/registry.py:63-80` | `Deployment` |
| `tasks.py:221-230` | `_next_incident_code` |
| `tasks.py:234-280` | `_current_metric_value` - the five metric resolvers |
| `tasks.py:284-313` | `evaluate_alert_rules_task` |
| `tasks.py:317-333` | `_open_auto_incident` |
| `tasks.py:337-350` | `_maybe_resolve_auto_incident` |
| `tasks.py:409-410` | Resolved-alert retention |
| `services.py:613-631` | `reliability_stats` |
| `serializers.py:29-40` | Timeline read and create shapes |
| `serializers.py:42-63` | `IncidentListSerializer`, `IncidentDetailSerializer` |
| `serializers.py:65-103` | `IncidentCreateUpdateSerializer` - code allocation, forced source, timeline side effects |
| `serializers.py:105-126` | `AlertRuleSerializer`, `AlertSerializer` |
| `serializers.py:128-133` | `DeploymentSerializer` |
| `views.py:56-63` | `HealthWriteMixin` - the method-aware key |
| `views.py:266-311` | Incident list/create, detail, timeline append |
| `views.py:315-333` | `ReliabilityView`, `AlertListView` |
| `views.py:337-349` | Alert rule CRUD |
| `views.py:391-394` | `DeploymentListCreateView` |
| `seed.py:134-177` | The five default rules and the repair logic |
| `apps/celery.py:137-140` | The per-minute evaluation beat entry |

## 11. Test coverage & gaps

Baseline: **`Ran 27 tests in 2.139s` - OK**.

What this slice covers:

- `AlertEvaluationTests` (`tests.py:129-165`) - a 50% error rate over 100
  requests fires one alert, the alert is FIRING, an AUTO incident is attached and
  its timeline is non-empty; clearing the metric resolves both.
- `SmallSampleGuardTests` (`tests.py:241-315`) - the strongest block in the
  module and all of it is about alerting: 10 requests with one 5000ms outlier
  does not breach an 800ms p95 rule and opens no incident; 5 requests with one
  500 does not breach a 5% error-rate rule; 60 requests at 2000ms does breach and
  the alert's value exceeds the threshold; the historical 440ms case that kept
  reopening no longer fires; and an open alert resolves once traffic falls below
  the floor, so a quiet night cannot pin an incident open forever.
- `HealthSeedTests` (`tests.py:383-421`) - re-seeding retunes a deployed rule
  from 400ms to 800ms without creating a duplicate, preserves an operator's
  `is_enabled = False`, and creates the defaults at the tuned thresholds.

What it does not cover:

1. **Every endpoint in this slice.** Eight routes - incident list, create,
   detail, patch, timeline append, reliability, alerts, alert rules,
   deployments - and not one has a test. `RBACGatingTests` covers `overview`
   only.
2. **`platform.health.manage`.** Nothing asserts that a caller holding only
   `platform.health.view` is refused a `POST /incidents/` or a
   `PATCH /alert-rules/<id>/`. `HealthWriteMixin`'s method-aware property is
   entirely untested, including the fallback to `"GET"` when `self.request` is
   missing (`views.py:62`).
3. **`_next_incident_code`.** The allocator has no test at all - not the
   sequence, not the `INC-9999` → `INC-10000` string-sort failure, not the
   non-numeric-code path, not concurrency. It is reached in
   `AlertEvaluationTests` only incidentally, with a single incident.
4. **The manual incident path.** `IncidentCreateUpdateSerializer.create` and
   `.update` are never exercised: not the forced `source = MANUAL`, not the
   `opened` timeline entry, not the `status` entry on transition, not the
   `services` slug resolution, and not the missing `resolved_at` stamp that
   makes MTTR null.
5. **Three of the five metric resolvers.** Only `error_rate` and `p95_latency`
   are tested. `queue_depth`, `ssl_days_left` and `uptime_pct`
   (`tasks.py:262-279`) have no coverage, including their `None`-without-a-target
   branches.
6. **`_maybe_resolve_auto_incident`'s guards.** The many-alerts-to-one-incident
   case - the reason the function checks `still_firing` - is never tested with
   more than one alert, and neither is the MANUAL-incident guard.
7. **`reliability_stats`.** No test, so nothing notices that MTTA and MTTR are
   structurally null.
8. **The filters.** `?status=active`, `?severity=`, and the `/alerts/`
   status default are all untested, which is where two of the reported defects
   live.
