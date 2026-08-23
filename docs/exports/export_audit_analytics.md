# export_audit_analytics

The two pipelines that watch the Export Centre, and why they are two: the
compliance audit trail (immutable, forever, one row per meaningful act) and
product analytics (prunable, bucketed, many rows per session). Plus the admin
activity screen that reads other people's exports - itself an audit event - and
the four metrics that judge whether this feature shipped well.

Routes covered here (`/v1/exports/`): `activity/`, `analytics/`,
`analytics/summary/`.

Findings live in **`error/exports/export_code_issues.md`**.

---

## 1. What it is (and what it is NOT)

The module's own table says it best (`analytics.py:6-15`):

| | Audit (`vs_exports.audit`) | Analytics (`vs_exports.analytics`) |
|---|---|---|
| Question | "Who took what data out?" | "Is this feature working?" |
| Retention | Forever, beyond file expiry | 180 days (`analytics.py:45`) |
| Mutability | Append-only, immutable, validated | Prunable, droppable |
| Volume | One row per meaningful act | Many rows per session |
| Contents | Actor, tenant, object id, IP | Counts and bucket labels only |
| Losing a row | A compliance gap | A rounding error |

Merging them damages both: analytics volume would drown the trail and dilute its
evidentiary value, and audit's immutability and retention would make telemetry
expensive and impossible to correct.

- **Audit failures never block business logic, and that is enforced rather than
  assumed** (`audit.py:7-14`). `emit_audit_event` swallows its own errors, but
  the *metadata* is built by this module and building it reads the object being
  audited - a null relation raises before the swallowing code is reached. That is
  exactly what stranded export runs once: a tenant-scoped run has no entity,
  `run.entity.code` raised, and a file that had already been written was left
  attached to a run stuck in RUNNING. `record` now catches and logs
  (`audit.py:57-62`). Test: `tests.py:1161`.
- **The audit vocabulary is closed and validated on save.** Every name in
  `AuditAction` (`constants.py:338-363`) must be a registered
  `AuditActionType`, and `MODULE_KEY = "EXPORTS"` must be a registered
  `AuditModuleKey` (`constants.py:310-313`) - an unregistered token makes
  `emit_audit_event` swallow a validation error and lose the event entirely.
  There is a test that walks every token (`tests.py:621`).
- **Two rules the design is explicit about, enforced by construction**
  (`audit.py:16-18`): including a sensitive field is an event in its own right,
  and an administrator reading *someone else's* export activity is itself an
  event.
- **The privacy rule in analytics is enforced, not promised**
  (`analytics.py:21-26`). `SCHEMA` whitelists the property keys per event and
  `record` drops everything else, so a well-meaning caller cannot leak a customer
  name into telemetry by adding a keyword argument. Continuous quantities are
  bucketed so a row count can never act as a fingerprint for a particular query.
- **Two of the four headline metrics are computed from the run rows, not from
  events** (`analytics.py:28-34`). Reuse is `definition_id IS NOT NULL` and the
  unhappy share is a status count; duplicating them into events would only create
  two numbers that can disagree. Events exist for what the database genuinely
  cannot know: how far people get through the builder before giving up, and how
  long a failure takes to resolve.
- **Analytics never raises** (`analytics.py:154-176`). An unknown event name, a
  database hiccup or a serialisation problem is logged and swallowed. That is the
  opposite of the audit trail's contract, and it is deliberate. Test:
  `tests.py:1450`.
- **This is not a BI surface.** `summary` answers four questions and nothing
  else; there is no cohort, no funnel builder, no export of the telemetry.

## 2. Domain model

### The audit side

No table of its own. `record` (`audit.py:30`) writes a `vs_audit.AuditEvent`
with:

| Argument | Value |
|---|---|
| `module_key` | `"EXPORTS"` |
| `action_type` | one of `AuditAction` |
| `entity_type` | `type(obj).__name__`, or `"ExportCentre"` when there is no object |
| `entity_id` | `obj.pk`, or **`"-"`** - the column is not nullable and is validated on save, so an object-less event still needs an id (`audit.py:41-44`) |
| `entity_label` | truncated to 255 |
| `actor_user`, `tenant` | passed on every call site in this app |
| `severity`, `status` | `INFO`/`WARNING`/`CRITICAL`, `SUCCESS`/`FAILED`/`DENIED` |
| `metadata` | built per event |

Fifteen actions (`constants.py:338-363`), grouped:

```
definitions   EXPORT_DEFINITION_CREATED / _UPDATED / _DELETED / _SHARED
sensitivity   EXPORT_SENSITIVE_FIELD_INCLUDED
schedules     EXPORT_SCHEDULE_CREATED / _PAUSED / _RESUMED
runs          EXPORT_REQUESTED / EXPORT_COMPLETED / EXPORT_RUN_OMITTED_FIELDS / EXPORT_FAILED
files         EXPORT_FILE_DOWNLOADED / EXPORT_FILE_DOWNLOAD_REFUSED / EXPORT_FILE_EXPIRED
admin         EXPORT_ADMIN_VIEWED_ACTIVITY
```

`record_sensitive_fields` (`audit.py:66-89`) is the one specialised writer. It
carries the field **ids and labels**, not a count, which is what makes "did
anything sensitive leave the building last month" a one-query question. Its
`entity` metadata is null for a tenant-scoped dataset, guarded the same way every
other reader of that relation in the app is.

### `ExportAnalyticsEvent` (`models.py:551`)

| Field | Meaning |
|---|---|
| `tenant` | CASCADE - telemetry dies with the tenant |
| `actor` | SET_NULL, so a deleted account does not take the metric with it |
| `name` | A key from `analytics.Event`; unknown names are dropped, indexed |
| `properties` | Whitelisted, bucketed scalars only. Never row data or field values |
| `session_key` | Opaque client-generated id, so one builder session can be stitched |
| `occurred_at` | Indexed |

Indexes on `(tenant, name, -occurred_at)` and `(session_key)`
(`models.py:586-589`).

**There is no entity column on purpose** (`models.py:559-561`): this measures
behaviour, not data, so the boundary that matters is the tenant.

Thirteen event names (`analytics.py:48-63`), eight of which the browser may
report (`CLIENT_EVENTS`, `analytics.py:86-90`) - the wizard steps and dwell
times only it knows. The other five are written server-side:
`QUICK_EXPORT_USED`, `RUN_TRIGGERED`, `FILE_DOWNLOADED`, `FAILURE_VIEWED`,
`FAILURE_RESOLVED`.

## 3. Endpoint map

| Route | Method | `rbac_permission` | View |
|---|---|---|---|
| `activity/` | GET | `exports.activity.view` | `ActivityView` (`views.py:856`) |
| `analytics/` | POST | `exports.catalogue.view` | `AnalyticsIngestView` (`views.py:889`) |
| `analytics/summary/` | GET | `exports.activity.view` | `AnalyticsSummaryView` (`views.py:918`) |

Ingest is open to anyone who can open the Export Centre on purpose: refusing
telemetry from ordinary users would bias the very funnel it measures
(`views.py:893-897`).

### Query parameters actually read

`GET /activity/` (`views.py:872-883`): `?actor=` (a user id - see
`export_code_issues` §6), `?dataset=` (matched against `frozen_config__dataset_key`),
`?status=` (upper-cased), `?since=` (a date, compared with `queued_at__date__gte`).

`GET /analytics/summary/` (`views.py:929-943`): `?days=`, clamped to 1-365, with
a 400 for a non-integer. Default window is 30 days (`analytics.py:202`).

### Request bodies actually read

`AnalyticsIngestSerializer` (`serializers.py:478`): `events` (a list of dicts,
1-50) and an optional `session_key`. Validation is deliberately narrow - an
event name outside `CLIENT_EVENTS` is rejected here (`serializers.py:493-502`),
and `sanitise` drops any property the event does not declare, so this endpoint
cannot become a channel for arbitrary data.

### Response shapes

`activity/` returns the paginated `ExportRunListSerializer` shape - the same rows
as the Files list, across the whole tenant.

`analytics/` returns `{accepted, received}`, so a client can see that some of its
batch was dropped.

`analytics/summary/` returns the four metrics (§5).

## 4. Lifecycle / state machine

Neither pipeline has a state machine. Both have a fixed path, and the difference
between them is exactly what happens when that path fails:

```
audit.record(...)
   └─ build metadata (reads the object)  ── raises? ─► caught + logged (audit.py:57-62)
   └─ emit_audit_event(...)              ── invalid vocabulary? ─► swallowed there, row lost
        └─ AuditEvent row: immutable, kept forever, never pruned

analytics.record(...)
   └─ name not in SCHEMA?               ─► warning, dropped (analytics.py:161-163)
   └─ sanitise(name, properties)        ─► unknown keys and non-scalars dropped
   └─ ExportAnalyticsEvent row          ─► pruned after 180 days by a nightly task
        └─ anything raises              ─► warning, swallowed (analytics.py:174-176)
```

The pruning task (`tasks.py:67-83`) deletes events older than `RETENTION_DAYS`
and returns `{deleted, older_than_days}`; the beat entry is nightly at 03:45
(`apps/celery.py:96-99`). It is deliberately not applied to the audit trail,
which is kept indefinitely and pruned by nothing.

## 5. Derivations

### Bucketing (`analytics.py:96-127`)

| Function | Buckets |
|---|---|
| `bucket_rows` | `0`, `1-100`, `101-1k`, `1k-10k`, `10k-100k`, `100k+` |
| `bucket_bytes` | `0`, `<100KB`, `100KB-1MB`, `1-10MB`, `10MB+` |
| `bucket_ms` | `<1m`, `1m-1h`, `1h-1d`, `1d-7d`, `7d+` |

Coarse on purpose: "how big are people's exports" is answerable from these,
"which query did Ada run" is not. `None` becomes `"unknown"`. Test:
`tests.py:1233`.

### `sanitise` (`analytics.py:134-151`)

Two halves, both load-bearing: the key whitelist stops a new field name leaking
data, and the scalar check stops a whole serialised row arriving under an allowed
key. Strings are truncated to 64 characters, because a bucket label or an enum
token is never long, so anything long is a mistake and should not be stored
whole. Tests: `tests.py:1212`, `tests.py:1219`, `tests.py:1226`.

### `summary(tenant, since=None)` (`analytics.py:182-253`)

| Metric | Source | Formula |
|---|---|---|
| **Reuse** | run rows | `definition__isnull=False` over all runs in the window; `share = reused / total` |
| **Builder abandonment** | events | `BUILDER_ABANDONED` grouped by `properties__last_step`, ordered by count; plus `entered`, `completed` and `completion_share` |
| **Failure resolution** | events | median of `FAILURE_RESOLVED.properties__ms_to_resolve`, plus its bucket label |
| **Unhappy runs** | run rows | `status in {FAILED, COMPLETED_WITH_OMISSIONS}`; `share = unhappy / total` |

Every share is `None` rather than `0` when the denominator is zero
(`analytics.py:233, 238, 251`), so an empty window reads as "no data" and not as
"nobody reused anything". Test: `tests.py:1423`.

`_median` (`analytics.py:257-263`) takes a pre-sorted list and averages the two
middle values for an even count.

### The one metric that needs stitching

`FAILURE_RESOLVED` is written by `_record_failure_resolved`
(`services.py:482-521`), not by the browser. On every successful run of a
definition it looks back for that definition's most recent FAILED run, checks
that no successful run intervened - an older failure already followed by a
success is a story that closed long ago - and records the elapsed milliseconds
plus the route the user took back: `retry` if the run's trigger was RETRY, else
`edit_and_run`. Deriving it server-side means a user who closes the tab is still
counted. Tests: `tests.py:1303`, `tests.py:1328`.

The other end of that measurement is `FAILURE_VIEWED`, recorded when a run with a
failure code is opened (`views.py:711-716`) - which is what starts the clock the
metric measures against.

## 6. What reading writes

This is the one slice where **reading writes**, twice, and both are deliberate:

| Read | What it writes |
|---|---|
| `GET /runs/<pk>/` on a failed run | `FAILURE_VIEWED` analytics with the reason code (`views.py:711-716`) |
| `GET /activity/` | `EXPORT_ADMIN_VIEWED_ACTIVITY` audit, **before the response is built** (`views.py:867-871`) |

Everything else in this slice writes on the path it describes:

| Moment | Analytics | Audit |
|---|---|---|
| Run triggered | `RUN_TRIGGERED` `{trigger, from_definition}` (`services.py:210`) | `EXPORT_REQUESTED` |
| Quick export | also `QUICK_EXPORT_USED` `{saved_as_definition: false}` (`services.py:274`) | `EXPORT_REQUESTED` |
| Preview | `ESTIMATE_VIEWED` `{rows_bucket, size_bucket}` (`views.py:290-297`) | - |
| Sensitive columns in a run | - | `EXPORT_SENSITIVE_FIELD_INCLUDED`, WARNING, with field ids and labels |
| Download allowed | `FILE_DOWNLOADED` `{age_days}` (`services.py:713-719`) | `EXPORT_FILE_DOWNLOADED` |
| Client batch | up to 50 events, each sanitised | - |

`FILE_DOWNLOADED` carries the file's age in days because how old files are when
people fetch them is the one product question the run rows cannot answer - it is
what tells us whether 30 days is the right retention window.

## 7. Worked example

A super admin reviewing last quarter.

**"Did anything sensitive leave the building?"** - one query on the audit trail,
because the event names the columns rather than counting them:

```
AuditEvent.objects.filter(module_key="EXPORTS",
                          action_type="EXPORT_SENSITIVE_FIELD_INCLUDED",
                          tenant=corona, created_at__gte=...)
→ metadata: {"dataset": "procurement.vendors", "entity": "CSS",
             "fields": ["bank_account_number", "tax_id"],
             "field_labels": ["Account number", "Tax ID"]}
```

**"Who has been exporting, and what?"**

```
GET /v1/exports/activity/?dataset=procurement.vendors&since=2026-06-01
```

The read is recorded first (`EXPORT_ADMIN_VIEWED_ACTIVITY`), then the runs come
back in the Files-list shape across the whole tenant, not just the admin's own.

**"Is the feature working?"**

```
GET /v1/exports/analytics/summary/?days=90

→ {"window_start": "2026-05-22T...", "runs": 418,
   "reuse":  {"from_saved_definition": 301, "rebuilt": 117, "share": 0.72},
   "builder": {"entered": 96, "completed": 71, "completion_share": 0.74,
               "abandoned_by_step": [{"step": "columns", "count": 14},
                                     {"step": "filters", "count": 7}]},
   "failure_resolution": {"resolved": 9, "median_ms": 5400000, "median_bucket": "1m-1h"},
   "unhappy_runs": {"count": 33, "share": 0.079}}
```

Read as: most runs come from a saved recipe rather than being rebuilt (the
feature is being reused); a quarter of builder sessions are abandoned and the
column step is where; a failure takes about an hour and a half to put right; and
eight percent of runs end unhappily.

Nothing in that response - or in any row behind it - contains a customer name, a
filter value or a row of exported data. `properties` for the abandonment events
holds one key, `last_step`, and `SCHEMA` is why (`analytics.py:73`).

## 8. Gotchas / known limitations

Full detail in `error/exports/export_code_issues.md`. From this slice:

| # | In one line |
|---|---|
| 6 | `?actor=` on the activity list is a raw string on an integer column, so a typo is a 500 |
| 7 | `exports.activity.view` gates this screen **and** silently confers write over everybody's exports |
| 16 | The activity read writes every query parameter the caller sent into the immutable audit trail, unfiltered and unbounded |

Limitations rather than defects:

- **`ActivityView` reads `ExportRun.objects.filter(tenant=…)` directly**
  (`views.py:872-874`), bypassing `visible_runs`. That is the point of the screen
  - it is the all-activity view - but it means the ordinary visibility rule is
  enforced in one place and deliberately not enforced in another, which is worth
  knowing before someone refactors them together.
- **The activity list has no export.** The one screen that answers a compliance
  question is the one screen whose answer cannot be taken out of the app, except
  through `audit.events` in the Export Centre itself.
- **`?dataset=` is a JSON key lookup** on `frozen_config__dataset_key`
  (`views.py:876`), which no index covers - a sequential scan once the run table
  is large.
- **Analytics ingest is unrate-limited.** Fifty events per request, one INSERT
  each, available to anyone who can open the Export Centre. The retention window
  bounds the damage; nothing else does.
- **Audit metadata is not itself size-checked** anywhere in this module. The
  activity read is the worst case (§16), but `EXPORT_DEFINITION_UPDATED` also
  stores the definition's whole previous columns and filters
  (`views.py:379-383`), which for a wide export is a sizeable row kept forever.
  Deliberate - it is what makes the change reviewable - but it is a growth curve
  nobody is watching.
- **The four metrics are per tenant only.** There is no platform-wide roll-up, so
  "is the Export Centre working across XVS" cannot be answered from this endpoint
  without querying every tenant.

## 9. Permissions & tenant isolation

Two keys, both seeded CRITICAL and both granted to the super-admin role only
(`seed_exports_permissions.py:38, 51-52`):

- **`exports.activity.view`** - the activity screen and the metrics summary.
  Reading other people's export activity is an administrator's power, and the
  read is itself audited.
- **`exports.sensitive_field.export`** - a separate decision from being allowed to
  export at all; granting it by default would make "sensitive" mean nothing
  (`seed_exports_permissions.py:6-14`).

Both screens are tenant-bounded: `activity/` filters on `tenant=self.tenant`
(`views.py:872`) and `summary` filters both the runs and the events on the tenant
(`analytics.py:203, 211`). The audit rows this app writes always carry
`tenant=`, at every call site, which is what makes a tenant-scoped audit read
return export events at all - most apps on this platform do not.

`analytics/` ingest is gated on `exports.catalogue.view` and stamps
`tenant=self.tenant` and `actor=request.user` server-side
(`views.py:905-910`); the client supplies only a name from a closed set, a
session key and whitelisted scalars, so it cannot write into another tenant or
attribute an event to another person.

## 10. Code map

| File | What lives there |
|---|---|
| `constants.py:310-313` | `MODULE_KEY` and why it must be registered |
| `constants.py:338` | `AuditAction` - the fifteen tokens |
| `audit.py:30` | `record` - the single emission point, and the catch that stops bookkeeping failing the work |
| `audit.py:66` | `record_sensitive_fields` |
| `analytics.py:45` | `RETENTION_DAYS` |
| `analytics.py:48` | `Event` - the closed name set |
| `analytics.py:69` | `SCHEMA` - the per-event property whitelist |
| `analytics.py:86` | `CLIENT_EVENTS` - what the browser may report |
| `analytics.py:96-127` | The three bucket tables and their helpers |
| `analytics.py:134` | `sanitise` |
| `analytics.py:154` | `record` - never raises |
| `analytics.py:182` | `summary` - the four metrics |
| `models.py:551` | `ExportAnalyticsEvent` |
| `services.py:482` | `_record_failure_resolved` - the stitched metric |
| `tasks.py:67` | `prune_analytics_task` |
| `views.py:856` | `ActivityView` |
| `views.py:889, 918` | `AnalyticsIngestView`, `AnalyticsSummaryView` |
| `serializers.py:478` | `AnalyticsIngestSerializer` |
| `apps/celery.py:96-99` | The nightly prune beat entry |

## 11. Test coverage & gaps

Covered:

- Audit vocabulary: every action token is registered (`tests.py:621`) - the test
  that stops an event being silently swallowed by a validation error.
- The run lifecycle reaches the trail (`tests.py:636`); a sensitive column is
  audited when included (`tests.py:517`); a broken audit write cannot strand a
  finished run (`tests.py:1161`).
- Sanitising: unknown property keys dropped (`tests.py:1212`), non-scalars
  dropped (`tests.py:1219`), long strings truncated (`tests.py:1226`), an unknown
  event is dropped not raised (`tests.py:1230`), buckets never reveal the exact
  figure (`tests.py:1233`).
- Emission: a run emits `RUN_TRIGGERED` (`tests.py:1246`), a quick export is
  marked as not from a definition (`tests.py:1253`), preview records only
  bucketed figures (`tests.py:1274`), viewing a failure records the reason code
  (`tests.py:1292`), a download records the file's age (`tests.py:1339`).
- Failure resolution: recorded after a good run (`tests.py:1303`), and a second
  success does not re-record an old failure (`tests.py:1328`).
- Ingest: the client may report builder events (`tests.py:1353`), may not report
  a server-owned event (`tests.py:1368`), and undeclared properties are stripped
  (`tests.py:1376`).
- Summary: admin-only (`tests.py:1386`), reports the four metrics
  (`tests.py:1394`), copes with no activity at all (`tests.py:1423`).
- Pruning drops only events past retention (`tests.py:1430`); analytics never
  breaks the request that emits it (`tests.py:1450`).
- `activity/` is admin-only (`tests.py:193`).

Not covered:

- **The activity screen's behaviour.** Only its permission gate is tested.
  Nothing asserts it returns other people's runs, that it is tenant-bounded, that
  its four filters work, or that reading it writes
  `EXPORT_ADMIN_VIEWED_ACTIVITY` - which is one of the two rules the audit module
  claims to enforce by construction.
- **`?actor=` with a non-numeric value** (`export_code_issues` §6).
- **What the activity audit event stores** (`export_code_issues` §16).
- **Audit `tenant=` on every event.** Nothing asserts the column is populated,
  which is the defect that makes other apps' audit rows unreachable; here it is
  right by convention rather than by test.
- **Ingest volume** - no test posts the maximum batch, and none posts twice in
  quick succession.
