# export_runs_and_files

One attempt to produce a file, from the moment it is triggered to the moment its
bytes are purged: the run row and its frozen configuration, the queue and the
fair-share cap, the engine that reads and writes, the omission and failure
vocabularies, the file and its availability window, the download authorisation
and its log, and the two sweepers.

Routes covered here (`/v1/exports/`): `runs/`, `runs/<pk>/`,
`runs/<pk>/cancel/`, `runs/<pk>/retry/`, `files/`, `files/<pk>/download/`,
`files/<pk>/downloads/`.

Recipes and the builder are `export_builder_definitions`; unattended runs are
`export_schedules`.

Findings live in **`error/exports/export_code_issues.md`**.

---

## 1. What it is (and what it is NOT)

- **`frozen_config` is the whole point of the run row.** It records dataset,
  entity, columns, filters, format and values mode exactly as they were when the
  run began, and it is **mandatory, never back-filled** (`models.py:349-351`).
  It is the only honest answer to "what produced this file", and the only way
  the UI can show how the definition has drifted since.
- **Configuration is frozen at trigger time, not at execution time**
  (`services.py:6-8`). A definition edited while a run sits in the queue does not
  change what that run produces. Test: `tests.py:407`.
- **`expired` is not a run status.** `RunStatus` is the closed set `queued ·
  running · completed · completed_with_omissions · failed · cancelled`
  (`constants.py:24-32`). Expiry is a property of the *file*, derived at read
  time (`models.py:501-513`), so history is never overwritten to represent the
  passage of time and the run stays COMPLETED forever.
- **A file missing two columns beats no file, provided nothing is silent.**
  Losing access to a column produces `COMPLETED_WITH_OMISSIONS` and a structured
  reason list, not a failure (`engine.py:106-156`). The list is structured
  because the UI renders it; it must never infer an omission from prose
  (`engine.py:57-63`).
- **Every failure carries a machine code and a user-safe sentence.**
  `FailureCode` (`constants.py:70-91`) plus `FAILURE_GUIDANCE`
  (`constants.py:98-138`). The API returns the code, the message and the run
  reference; it never returns a traceback. Test: `tests.py:549`.
- **Only transient faults are retryable.** `RETRYABLE_FAILURE_CODES` is
  `{INFRASTRUCTURE, UNKNOWN}` (`constants.py:95`), and `retry_run` enforces it
  (`services.py:328-334`) - re-running a permission or filter failure fails again
  identically, so the UI points at the fix instead.
- **Authorisation is re-checked at run time, as the owner** (`engine.py:22-26`).
  Build-time checks are advisory; run-time checks are authoritative.
- **Downloads are re-authorised against the downloader**, on the run's *frozen*
  entity and dataset, and every attempt is logged before the bytes move -
  refusals included, because "who tried and was told no" is what a compliance
  review asks (`models.py:516-522`, `services.py:663-736`).
- **Cancellation is cooperative.** The flag is set and the worker notices between
  chunks (`services.py:353-360`, `engine.py:468-473`). Nothing is stored until
  `produce` returns, so a cancelled run leaves no partial file.
- **This is not a report scheduler with delivery.** There is no "email me the
  file" - a completion notification links to the run, and the bytes are fetched
  from the download endpoint.

## 2. Domain model

### `ExportRun` (`models.py:317`)

| Field | Meaning |
|---|---|
| `reference` | Human-quotable, unique, `RUN-7F31C2` (`models.py:416-418`) |
| `tenant`, `entity` | PROTECT; entity nullable for tenant-scoped datasets |
| `definition` | SET_NULL - **null for a quick export** |
| `schedule` | SET_NULL - set when a schedule started this run |
| `frozen_config` | The configuration used at run time. Mandatory |
| `trigger` | `MANUAL` `SCHEDULED` `QUICK` `RETRY` `API` (`constants.py:50-57`) |
| `requested_by` | PROTECT. The actor who asked |
| `client_key` | Idempotency key, indexed |
| `status` | `RunStatus` |
| `phase` | `QUEUED` `COUNTING` `READING` `BUILDING` `DONE` (`constants.py:60-67`) |
| `rows_done`, `rows_total` | Progress. `rows_total` null means indeterminate - expected, not an error |
| `row_count` | Rows actually written |
| `omissions` | `[{code, scope, detail, items[]}]` |
| `failure_code`, `failure_message` | User-safe. Never a traceback |
| `attempt` | Carried forward by retry |
| `cancel_requested` | Cooperative cancellation flag |
| `background_job` | SET_NULL to `core.BackgroundJob`, so the run appears in View Queues |
| `queued_at`, `started_at`, `ended_at` | Timing; `duration_seconds` derives from the last two |

Indexes on `(tenant, -queued_at)`, `(definition, -queued_at)`,
`(status, -queued_at)`, `(requested_by, -queued_at)` and `(client_key)`
(`models.py:403-410`).

`scope_context()` (`models.py:440-450`) builds the `ScopeContext` from the run's
own tenant and entity, so a definition moved to another entity after the fact
cannot change what an old run reports having read.

### `ExportFile` (`models.py:462`)

One-to-one with the run. `name` (with extension), `format`, `storage_name` (an
opaque uuid key into the default storage backend), `size_bytes`, `row_count`,
`columns_produced`, `available_until` (indexed), `purged_at`, `download_count`.

Availability is **derived, never stored**: `is_expired`, `is_purged`,
`is_downloadable` are properties (`models.py:501-513`), so nothing depends on a
sweeper being punctual.

### `ExportDownload` (`models.py:516`)

`(file, user, at, ip_address, outcome, refusal_reason)`. `outcome` is
`ALLOWED`/`REFUSED` (`constants.py:238-240`); `refusal_reason` is one of
`EXPIRED`, `PURGED`, `NO_ENTITY_ACCESS`, `NO_DATASET_ACCESS`, `NOT_SHARED`
(`constants.py:243-250`).

## 3. Endpoint map

| Route | Method | `rbac_permission` | View |
|---|---|---|---|
| `runs/` | GET | `exports.run.view` | `RunListView` (`views.py:689`) |
| `runs/<pk>/` | GET | `exports.run.view` | `RunDetailView` (`views.py:705`) |
| `runs/<pk>/cancel/` | POST | `exports.run.cancel` | `views.py:724` |
| `runs/<pk>/retry/` | POST | `exports.run.create` | `views.py:743` |
| `files/` | GET | `exports.run.view` | `FileListView` (`views.py:761`) |
| `files/<pk>/download/` | GET | `exports.file.download` | `views.py:776` |
| `files/<pk>/downloads/` | GET | `exports.run.view` | `views.py:838` |

### Query parameters actually read

`GET /runs/` (`views.py:694-703`): `?status=` (upper-cased), `?trigger=`
(upper-cased), `?definition=` (an id - see `export_code_issues` §6).
`GET /files/` (`views.py:766-774`): `?available=true` →
`available_until__gt=now, purged_at__isnull=True`.

### Response shapes

`ExportRunListSerializer` (`serializers.py:226`): id, reference, `export_name`
(from `frozen_config`, or "Quick export"), `definition_id`, status, trigger,
`requested_by_name`, `queued_at`, `started_at`, `ended_at`, `row_count`,
`attempt`, `progress`, `file`.

`progress` is null for a terminal run; otherwise
`{phase, phase_label, rows_done, rows_total, queue_position}`
(`serializers.py:253-271`). `queue_position` is what lets the UI explain a wait
instead of going quiet - silence is what makes people run the same export twice.

`ExportRunDetailSerializer` (`serializers.py:273`) adds `omissions`, `failure`,
`configuration` and `drift`.

**Nothing publishes a raw JSONField.** `configuration` renders the frozen blob as
labels and filter sentences (`serializers.py:308-327`), and `drift` renders each
change through the same helpers rather than publishing column ids and filter
specs (`serializers.py:329-406`). Test: `tests.py:1538`.

`failure` (`serializers.py:285-306`) is
`{code, message, recommended_action, reference, retryable}`, and `retryable` is
deliberately narrow: `definition_id is not None and failure_code in
RETRYABLE_FAILURE_CODES`.

`ExportFileSerializer` (`serializers.py:210`) publishes the three derived
availability booleans alongside the stored facts.

## 4. Lifecycle / state machine

```
                trigger_run / trigger_quick_run / retry_run
                                │
                                ▼
                            QUEUED ──── cancel ───► CANCELLED   (immediate; no worker involved)
                                │
                     run_export_task → execute_run
                                │
                                ▼
     RUNNING ─ COUNTING ─ READING ─ BUILDING ─────► COMPLETED
        │          │         │         │                 or COMPLETED_WITH_OMISSIONS
        │          └─────────┴─────────┴── cancel ─────► CANCELLED   (no partial file kept)
        │
        └── ExportError ─────────────────────────────► FAILED  (code + guidance)
        └── anything else ───────────────────────────► FAILED  (INFRASTRUCTURE)
        └── worker dies ── sweep_abandoned_runs ─────► FAILED  (INFRASTRUCTURE) or CANCELLED
```

Terminal statuses never change again (`constants.py:36-41`), and `execute_run`
returns early for a run that is already terminal, so a duplicate task delivery
cannot overwrite a finished run's history (`services.py:383-384`).

**The file's own lifecycle runs beside it, and never touches the run:**

```
_store_file  →  available_until = now + 30 days   (constants.py:257)
                    │
                    ├── is_expired derived at read time; downloads refused EXPIRED
                    │
                    └── expire_files (nightly) → storage deleted, purged_at set,
                                                 EXPORT_FILE_EXPIRED audited
                                                 run stays COMPLETED
```

**Guard rails around execution** (`services.py:376-455`):

1. Terminal → return. Cancel already requested → `_finish_cancelled`.
2. Status RUNNING, phase COUNTING, `started_at` stamped, in one save.
3. The owner is resolved (`definition.owner` for a saved export,
   `requested_by` for a quick one) and must be ACTIVE, else `OWNER_INACTIVE`.
4. Everything that decides whether a file exists is inside **one** try block, so
   there is no window in which the run is RUNNING with a file already written. A
   run that reaches this function always leaves it terminal.
5. Post-completion bookkeeping - audit, sensitive-field event, metrics, schedule
   advance, notification - runs after the row is terminal and is wrapped in its
   own catch-all (`services.py:452-475`). A failed audit write or a refused
   mailer must never tell the user their export did not happen.

## 5. Derivations

| Output | Formula | Where |
|---|---|---|
| `reference` | `RUN-` + 3 random bytes as upper hex | `models.py:416-418` |
| Accept a run | idempotent replay inside 60 s, else cap check | `_accept_run` (`services.py:170`) |
| `in_flight` | count of tenant runs in `QUEUED` or `RUNNING` | `services.py:144-148` |
| `queue_position` | tenant's QUEUED runs queued earlier **plus** all its RUNNING runs, + 1 | `services.py:150-168` |
| Effective row cap | `min(dataset.row_cap, DEFAULT_ROW_CAP)` = at most 500,000 | `engine.py:424` |
| `duration_seconds` | `ended_at - started_at`, null until both exist | `models.py:433-437` |
| `available_until` | completion + `FILE_RETENTION_DAYS` (30) | `models.py:498-500` |
| `is_expired` | `now >= available_until` | `models.py:502-505` |
| Drift | frozen config vs `freeze(definition)` over nine watched keys | `config_drift` (`services.py:105`) |
| File stem | `render_file_name` for a definition; `<dataset>-<date>` for a quick export | `services.py:529-533` |
| Storage key | `exports/<tenant_id>/<uuid4>.<ext>` | `services.py:536-538` |

**Reading is done through `values_list`.** Every catalogue field is an ORM path,
so one query streams the whole result set; nothing materialises model instances,
and a 200k-row export costs one query rather than 200k
(`engine.py:12-15`, `engine.py:461`).

**The row cap produces a partial file, not a failure.** `produce` counts, and if
the total exceeds the cap it appends a `ROW_CAP_HIT` omission naming how many
rows were left out, then truncates with `islice` - deliberately, so the cap and
the streaming cursor stay independent (`engine.py:442-457`). The run ends
`COMPLETED_WITH_OMISSIONS`. Test: `tests.py:610`.

**Storage keys are opaque and unguessable** (`services.py:535-538`): the download
endpoint is the only way in, so a leaked key must not be a second door.

**Drift is rendered, never dumped.** `config_drift` returns raw before/after
over `dataset_key, columns, filters, sort, format, format_options, values_mode,
file_name_pattern, name` (`services.py:113-126`); the serializer turns each side
into a sentence - column ids become labels, filter specs become the review
step's own sentences, an options object becomes a count
(`serializers.py:374-406`).

**Failure resolution is stitched server-side.** `_record_failure_resolved`
(`services.py:482-521`) finds the definition's most recent FAILED run, checks no
successful run intervened, and records `FAILURE_RESOLVED` with the elapsed
milliseconds and whether the user got back via retry or by editing. It is the
one headline metric that needs both ends joined, and deriving it server-side
means a user who closes the tab is still counted.

### The writers (`writers.py`)

Both take already-rendered string rows, because value formatting is a catalogue
concern and must not be re-decided per format (`writers.py:5-7`).

- **CSV** (`writers.py:30`): delimiter, encoding, header row, quote-all, line
  ending. `filters_summary` is accepted and **ignored** - CSV has no second sheet
  and prepending commentary rows would break the importers the format exists
  for. Encoding uses `errors="replace"` so one unmappable character cannot fail
  a run that has already read every row.
- **XLSX** (`writers.py:55`): sheet name (sanitised to 31 chars, Excel's
  forbidden punctuation stripped), bold header, frozen panes, auto width clamped
  to 10-48, and an optional **Filters sheet** carrying what produced the file -
  what lets a reader six weeks later know which slice of the data they hold
  (`engine.py:482-496`).
- `write` (`writers.py:109`) raises `ValueError` for an unsupported format; PDF
  and JSON are explicitly post-v1 (`constants.py:208`).

## 6. What running writes

| Moment | Rows | Audit |
|---|---|---|
| Trigger | one `ExportRun` | `EXPORT_REQUESTED` with trigger and dataset (`services.py:224`) |
| Enqueue | one `core.BackgroundJob`, linked back (`services.py:292-315`) | - |
| Progress | `phase`, `rows_done`, `rows_total` updated in place, no transaction | - |
| Completion | run row terminal; one `ExportFile` | `EXPORT_COMPLETED` with rows, bytes, file name (`services.py:461`) |
| Sensitive columns included | - | `EXPORT_SENSITIVE_FIELD_INCLUDED`, WARNING, naming the field ids and labels (`audit.py:66-89`) |
| Omissions | `omissions` JSON on the run | `EXPORT_RUN_OMITTED_FIELDS`, WARNING (`services.py:466`) |
| Failure | status, code, message, `ended_at` | `EXPORT_FAILED`, CRITICAL, status FAILED, detail truncated to 500 chars (`services.py:569`) |
| Download allowed | one `ExportDownload`; `download_count` via `F()` | `EXPORT_FILE_DOWNLOADED`, INFO, SUCCESS (`services.py:723-733`) |
| Download refused | one `ExportDownload` with the reason | `EXPORT_FILE_DOWNLOAD_REFUSED`, WARNING, **status DENIED** - nothing broke, access was declined (`services.py:729`) |
| Expiry | `purged_at`; bytes deleted | `EXPORT_FILE_EXPIRED` (`services.py:762`) |

Notification is best-effort and cannot fail the run (`services.py:617-660`):
`export.run_completed` or `export.run_failed`, to the definition's owner (or the
requester for a quick export), carrying `metadata={"export_run_id": run.pk}` so
the bell deep-links to *this* run rather than to the Files list. Both keys are
registered and templated in `vs_notifications`
(`vs_notifications/constants.py:583, 596`,
`vs_notifications/services/seed.py:1169-1195`), and the route resolver has an
`export.` branch for the run id (`vs_notifications/services/routing.py:37-38`).
Test: `tests.py:1011`.

## 7. Worked example

A month-end invoice export, run by hand, downloaded twice, then expiring.

```
POST /v1/exports/definitions/41/run/  {"client_key": "btn-9f21"}
```

1. `_accept_run` (`services.py:170`): no run with that key in the last 60 s, and
   the tenant has 1 of 3 in flight, so it proceeds. A second click inside the
   window returns **the same run** with a 200 and "This export is already
   running - showing the run in progress." Test: `tests.py:438`.
2. `trigger_run` refuses a draft, creates the run with
   `frozen_config = freeze(definition)`, records `RUN_TRIGGERED` analytics and
   the `EXPORT_REQUESTED` audit event, and enqueues.
3. `enqueue` calls the Celery task with the platform's `_job_*` metadata, so the
   run appears in View Queues attributed to the person who asked
   (`services.py:301-309`).
4. `execute_run` → RUNNING/COUNTING → `produce`:
   - dataset still published, owner may still export it;
   - `resolve_columns` drops "Billing email" because the owner lost the
     sensitive key last week → one `FIELD_FORBIDDEN` omission;
   - 214 rows read in one chunk, rendered for people;
   - `write_xlsx` builds the sheet plus the Filters sheet.
5. `_store_file` writes `exports/12/6b1f….xlsx`, names it
   `overdue-css-2026-08-20.xlsx`, and opens a 30-day window.
6. Status becomes `COMPLETED_WITH_OMISSIONS`; audit gets `EXPORT_COMPLETED` and
   `EXPORT_RUN_OMITTED_FIELDS`; the owner gets `export.run_completed` saying
   "Some columns were left out - open the run to see which."

```
GET /v1/exports/runs/512/
→ {"reference": "RUN-7F31C2", "status": "COMPLETED_WITH_OMISSIONS",
   "row_count": 214, "progress": null,
   "omissions": [{"code": "FIELD_FORBIDDEN", "scope": "columns",
                  "detail": "Billing email was left out because your access to it was removed. Every other row and column was exported in full.",
                  "items": ["Billing email"]}],
   "failure": null,
   "configuration": {"dataset": "Customer invoices", "scope": "CSS",
                     "columns": ["Invoice number", "Customer", ...],
                     "filters": ["Invoice date is 2026-07-01 to 2026-07-31"]},
   "drift": {"count": 1, "fields": ["columns"],
             "changes": [{"field": "columns", "label": "Columns",
                          "then": "Invoice number, Customer, Total",
                          "now": "Invoice number, Customer, Total, Status"}]},
   "file": {"name": "overdue-css-2026-08-20.xlsx", "size_bytes": 24117,
            "available_until": "2026-09-19T...", "is_downloadable": true}}
```

The bursar it was shared with downloads it: `authorise_download` checks purge,
expiry, tenant, the run's frozen entity, the dataset key against *their*
permissions, and finally the share list - then `log_download` writes the row and
bumps the counter with an `F()` so two simultaneous downloads cannot lose a
count.

Thirty-one days later the nightly job deletes the bytes and stamps `purged_at`.
The run still says COMPLETED, because it genuinely did. A download attempt now
answers 403 with "This file has been deleted from storage. The run record is
still here; run the export again to produce a new file." (`views.py:821-826`) -
and that refusal is logged like any other.

## 8. Gotchas / known limitations

Full detail in `error/exports/export_code_issues.md`. From this slice:

| # | In one line |
|---|---|
| 4 | Run references are six hex characters and globally unique, so collisions start around 4,800 runs and surface as "A record with these details already exists" |
| 5 | A malformed filter is swallowed by the catch-all and recorded as INFRASTRUCTURE, which is retryable - so the UI offers Retry on a configuration error |
| 6 | `?definition=` on the runs list is a raw string on an integer column |
| 8 | Retry skips the fair-share cap that both other trigger paths enforce |
| 9 | The cap's refusal message says the export "will be accepted", and nothing queues it |
| 11 | The file is assembled in memory - rows list, then whole workbook - up to the 500,000-row cap, and the download reads it whole again |
| 15 | The idempotency window is a read-then-write race with no constraint behind it |
| 18.2 | A cancel landing between `produce` and `_store_file` is accepted and then ignored |
| 18.3 | `columns_produced` publishes catalogue field ids |

Limitations rather than defects:

- **Progress is polled, not pushed** (`services.py:404-408`). The row is updated
  every 2,000 rows; there is no channel and no websocket.
- **`rows_total` can be null**, and that is the designed indeterminate state, not
  a bug (`models.py:381-383`).
- **A run has no soft delete.** Runs and their audit trail survive indefinitely;
  only the bytes expire.
- **The sweeper purges a dead worker's file rather than handing it over**
  (`services.py:790-799`). The bytes are complete, but the run never recorded
  what is in them, and offering a file the app cannot describe is the silence the
  Export Centre exists to prevent. Deliberate, and worth knowing before someone
  "fixes" it.

## 9. Permissions & tenant isolation

Keys: `exports.run.view`, `.run.create`, `.run.cancel`, `exports.file.download`
(`constants.py:325-328`). `file.download` is seeded SENSITIVE
(`seed_exports_permissions.py:48`).

**Visibility** is `visible_runs` (`views.py:129-141`):

```python
qs = ExportRun.objects.filter(tenant=self.tenant).select_related(...)
if self.is_admin_reader():
    return qs
return qs.filter(Q(requested_by=user) | Q(definition__owner=user)
                 | Q(definition__shares__user=user)).distinct()
```

Every run and file lookup starts from it, including `files/` and both file
detail routes (`views.py:767, 787, 844`), so a pk from the address bar returns
404 rather than someone else's export.

**Cancel is narrower than view**: only the person who started the run, or an
admin reader (`views.py:730-732`).

**Download is authorised separately and later** (`services.py:663-703`), in this
order: purged → expired → tenant → the run's frozen entity still belongs to this
tenant → the dataset key is still exportable *by the downloader* → the share
list. Judging on the run's frozen entity and dataset rather than the definition's
current ones is deliberate: the file contains what it contains.

The audit trail carries `tenant=` on every event this app writes
(`audit.py:45-56` and every call site), which is not true of most apps on the
platform - it is why export events are actually reachable through a
tenant-scoped audit read.

## 10. Code map

| File | What lives there |
|---|---|
| `models.py:317` | `ExportRun`; `new_reference` :416, `scope_context` :440, `failure_guidance` :452 |
| `models.py:462` | `ExportFile`; `default_expiry` :498, the three availability properties :501-513 |
| `models.py:516` | `ExportDownload` |
| `constants.py:24-146` | `RunStatus`, terminal/successful sets, `RunTrigger`, `RunPhase`, `FailureCode`, `RETRYABLE_FAILURE_CODES`, `FAILURE_GUIDANCE`, `OmissionCode` |
| `constants.py:215` | `FORMAT_MEDIA` - extension and MIME per format |
| `constants.py:256-304` | Retention, row cap, concurrency, abandoned-run windows, idempotency window, sync ceiling |
| `engine.py:47, 57` | `ExportError`, `Omission` |
| `engine.py:258` | `build_queryset` |
| `engine.py:385` | `produce` - the read/render/write loop |
| `engine.py:482` | `filters_summary` - the workbook's Filters sheet |
| `writers.py:30, 55, 109` | `write_csv`, `write_xlsx`, `write` |
| `services.py:144-192` | `in_flight`, `queue_position`, `_accept_run` |
| `services.py:194, 233, 317` | `trigger_run`, `trigger_quick_run`, `retry_run` |
| `services.py:292` | `enqueue` and the `BackgroundJob` link |
| `services.py:353` | `request_cancel` |
| `services.py:376` | `execute_run` |
| `services.py:458-620` | `_after_completion`, `_record_failure_resolved`, `_store_file`, `_record_sensitive`, `_finish_failed`, `_finish_cancelled`, `_notify` |
| `services.py:663, 705` | `authorise_download`, `log_download` |
| `services.py:738, 755` | `expire_files`, `_purge_file` |
| `services.py:768` | `sweep_abandoned_runs` |
| `tasks.py:24, 40, 54` | `run_export`, `sweep_abandoned_runs`, `expire_files` |
| `views.py:689-855` | Run and file views, `_refusal_message` at :817 |
| `serializers.py:210, 226, 273, 409` | File, run list, run detail, download-log serializers |

## 11. Test coverage & gaps

Covered:

- Production: CSV row-for-row (`tests.py:375`), XLSX workbook (`tests.py:399`),
  locked column always included (`tests.py:420`), frozen config survives a later
  edit (`tests.py:407`).
- Outcomes: sensitive column omitted not silently dropped (`tests.py:501`) and
  included-plus-audited for a holder (`tests.py:517`), withdrawn filter fails
  with a code and an action (`tests.py:532`), no traceback ever leaks
  (`tests.py:549`), row cap produces a partial file naming what was left out
  (`tests.py:610`).
- Triggering: idempotent run returns the same row (`tests.py:438`), the
  concurrency cap refuses (`tests.py:451`), a draft cannot run (`tests.py:463`),
  a queued cancel is immediate and keeps no file (`tests.py:470`), a cancelled
  run is not executed (`tests.py:482`).
- Retry policy: a configuration failure is not retryable (`tests.py:1489`), a
  transient one is (`tests.py:1504`), the refusal names the next step
  (`tests.py:1514`), a quick export has nothing to retry (`tests.py:1521`).
- Queue position: counts queued runs ahead (`tests.py:816`), running runs occupy
  a place (`tests.py:822`), a started run has none (`tests.py:831`), scoped to
  the tenant (`tests.py:837`).
- Downloads: owner download logged (`tests.py:723`), expired refused **and the
  refusal logged** (`tests.py:735`), unshared cannot (`tests.py:750`), shared can
  (`tests.py:756`), the log shows refusals too (`tests.py:763`).
- Expiry: purges bytes but keeps history (`tests.py:774`), idempotent
  (`tests.py:790`).
- The abandoned-run sweeper, in full: a dead worker's run fails and is retryable
  (`tests.py:893`), a slow run is left alone (`tests.py:906`), a queued run gets
  the longer window (`tests.py:913`), a user-cancelled run ends quietly
  (`tests.py:925`), a file the dead worker stored is not handed over
  (`tests.py:939`), the sweep gives slots back (`tests.py:959`), is idempotent
  (`tests.py:976`), and closes nothing on a healthy platform (`tests.py:984`).
- Audit: every action token is a registered vocabulary token (`tests.py:621`) -
  the test that stops an event being silently swallowed; the run lifecycle is
  written to the trail (`tests.py:636`); a broken audit write cannot strand a
  finished run (`tests.py:1161`).
- Tenant-scoped runs: need no entity (`tests.py:1059`), produce a file
  (`tests.py:1082`), read only their own tenant's rows (`tests.py:1102`).

Not covered:

- **Reference collision** (`export_code_issues` §4). Nothing creates two runs
  with a forced identical reference.
- **Retry and the concurrency cap** (`export_code_issues` §8) - no test retries
  a run while the tenant is at its cap.
- **The idempotency race** (`export_code_issues` §15) - the test is sequential.
- **Large-file behaviour.** Every production test uses a handful of rows, so the
  memory profile at the 500,000-row cap (`export_code_issues` §11) is untested by
  construction.
- **`?status=`, `?trigger=`, `?definition=`, `?available=`** - none of the four
  list filters has a test, and one of them 500s on a bad value.
- **Download of a purged (as opposed to expired) file** - `PURGED` has its own
  refusal reason and message, and no test exercises it.
