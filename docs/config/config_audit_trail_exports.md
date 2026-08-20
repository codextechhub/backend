# config_audit_trail_exports

The module's own history: `ConfigurationAuditEvent`, an append-only table
written in the same transaction as every configuration and capability change,
plus the surfaces that read it - the Event list and detail, the facet
dictionary that drives the filter bar, personal saved views, a synchronous CSV
download, and a queued background export with a temporary file.

Routes (`urls.py:25-33`):
`audit-events/`, `audit-events/facets/`, `audit-events/export/`,
`audit-events/saved-views/`, `audit-events/saved-views/<uuid>/`,
`audit-events/export-jobs/`, `audit-events/export-jobs/<uuid>/download/`,
`audit-events/<uuid>/`.

---

## 1. What it is (and what it is NOT)

- **This table is authoritative, and the platform trail is a mirror.**
  `record_configuration_event` writes the local row first, inside the caller's
  transaction, then pushes a best-effort copy to `vs_audit`
  (`services/audit.py:19-56`). If the mirror fails the config change still
  stands; if the local write fails the config change is rolled back with it.
- **Rows are immutable, and it is enforced twice.** `save()` refuses to update
  an existing row and `delete()` always raises (`models.py:650-656`), and
  migration 0003 installs BEFORE UPDATE / BEFORE DELETE triggers so raw SQL and
  bulk querysets cannot rewrite history either
  (`migrations/0003_configuration_audit_immutability.py`).
- **Snapshots are redacted before storage, not on read.** A secret-reference
  value arrives here already replaced with `"[REDACTED]"`
  (`services/resolution.py:13-16`), which is what makes it safe to expose the
  whole `before_data` / `after_data` payload to `config.audit.view` holders.
- **`target_type` is a loose string, not a content-type FK** (`models.py:629`),
  so history survives model renames and deleted targets. The human label is
  resolved at read time and falls back to `""` when the target is gone
  (`serializers.py:486-534`).
- **The order of the ladder is the point.** A platform event has `tenant` and
  `branch` NULL, so it is visible only to a caller resolving to the platform
  scope; a tenant event is visible only inside that tenant
  (`services/audit_exports.py:67-73`).
- **A saved view is a filter bundle, not a shared report.** It is owned by one
  user, unique by name per user, and there is no sharing route
  (`models.py:659-685`).
- **The queued export is not the same thing as the CSV download.** One is a
  background job with a stored file, a retention window and three audit events;
  the other is a synchronous response capped at 5,000 rows that writes nothing
  at all.
- **Neither export surface is the Export Centre.** `vs_exports` has its own
  nightly purge and its own model; this module keeps its own job table
  (`models.py:688-735`) and has no purge (`config_code_issues.md` §8).

## 2. Domain model

### `ConfigurationAuditEvent` (`models.py:576`)

| Field | Meaning |
|---|---|
| `action` | Stable dotted key, indexed. `legacy.*` actions come from the 0004 data migration |
| `target_type` | Class name of the mutated record |
| `target_id` | String pk of the mutated record, indexed |
| `actor` | User responsible; NULL for system and migration work (SET_NULL) |
| `tenant`, `branch`, `scope_key` | Scope of the mutation, from `ScopedModel` |
| `before_data`, `after_data` | Redacted JSON snapshots (`{}` for a creation) |
| `reason` | Operator justification when supplied |
| `metadata` | Non-secret context: request info, proxy attribution, `{"bulk_schedule": true}` |
| `created_at` | Indexed; default ordering is newest first |

Index on `(tenant, branch, -created_at)` (`models.py:648`), which is exactly the
list query's shape.

The full action vocabulary written today:

```text
config.definition.created   config.definition.updated   config.definition.archived
config.value.updated        config.value.cleared
config.capability.created   config.capability.updated   config.capability.archived
config.entitlement.updated  config.entitlement.cleared
config.override.updated
config.integration.connection_tested
config.audit.export_queued  config.audit.export_completed  config.audit.export_downloaded
```

### `ConfigurationAuditSavedView` (`models.py:659`)

`owner`, `name`, `filters` (JSON), plus the `ScopedModel` scope columns.
`uniq_config_audit_saved_view_owner_name` makes the name unique per owner;
index on `(owner, updated_at)`. `Meta.ordering = ["name", "created_at"]`.

### `ConfigurationAuditExportJob` (`models.py:688`)

| Field | Meaning |
|---|---|
| `requested_by` | Owner, SET_NULL |
| `filters` | The snapshot the task replays |
| `client_key` | Idempotency token supplied by the browser |
| `status` | QUEUED, RUNNING, COMPLETED, FAILED (indexed) |
| `file_name`, `storage_name` | Display name and the storage key |
| `row_count` | Data rows written, excluding the header |
| `failure_message` | Operator-facing reason, 500 chars |
| `requested_at`, `started_at`, `completed_at`, `available_until` | Timeline |

Indexes on `(requested_by, -requested_at)`, `(scope_key, -requested_at)` and
`(client_key)` (`models.py:724-733`).

## 3. Endpoint map

| Method + path | Permission | Notes |
|---|---|---|
| `GET /audit-events/` | `config.audit.view` | Paginated, six filters |
| `GET /audit-events/<uuid>/` | `config.audit.view` | Scoped; a foreign id is 404 |
| `GET /audit-events/facets/` | `config.audit.view` | Filter-bar dictionaries |
| `GET /audit-events/export/` | `config.audit.export` | Synchronous CSV, 5,000-row cap |
| `GET /audit-events/saved-views/` | `config.audit.view` | The caller's own views |
| `POST /audit-events/saved-views/` | `config.audit.view` | Duplicate name is a 400 |
| `DELETE /audit-events/saved-views/<uuid>/` | `config.audit.view` | Own views only |
| `GET /audit-events/export-jobs/` | `config.audit.export` | The caller's own jobs |
| `POST /audit-events/export-jobs/` | `config.audit.export` | Queue a background export |
| `GET /audit-events/export-jobs/<uuid>/download/` | `config.audit.export` | File stream |

### Filters actually read

`GET /audit-events/` and `GET /audit-events/export/` share one filter path
(`views.py:936-950`): `action`, `target_type`, `target_id`, `actor`,
`created_after`, `created_before`. Blank values are dropped before validation.
`ConfigurationAuditFilterSerializer` (`serializers.py:372-401`) validates the
dates as real datetimes, rejects a window whose end is not after its start, and
validates `actor` as a positive integer **or a UUID**. The UUID branch is a
bug: the user primary key is a `BigAutoField`, so a well-formed UUID reaches the
ORM and 500s (`config_code_issues.md` §1).

Categorical filters are exact matches, not `icontains`
(`services/audit_exports.py:55-57`).

`GET /audit-events/facets/` reads only `created_after` and `created_before`,
parsed with `parse_datetime` and rejected with a 400 if unparseable
(`views.py:1107-1113`). It deliberately ignores the categorical filters so that
choosing one facet never empties the others.

### Request bodies actually read

`POST /audit-events/saved-views/` (`serializers.py:404-426`):

```jsonc
{"name": "Last 30 days, value changes",
 "filters": {"window_days": 30, "action": "config.value.updated",
             "actor": "7", "target_type": "", "target_id": ""}}
```

`window_days` is a choice of `7`, `30`, `90` or `"all"`, defaulting to 30.
`target_type` and `target_id` must be saved together or not at all
(`serializers.py:416-421`). Note that the saved filter vocabulary is **not** the
same as the list filter vocabulary: it stores `window_days` where the list takes
`created_after` / `created_before`, and nothing in this module translates one
into the other - that is the client's job.

`POST /audit-events/export-jobs/` (`serializers.py:443-445`):

```jsonc
{"filters": {"action": "config.value.updated",
             "created_after": "2026-08-01T00:00:00Z"},
 "client_key": "abc123"}
```

### Response shapes

The event serializer (`serializers.py:473-484`) exposes `id`, `action`,
`target_type`, `target_id`, `target_label`, `tenant`, `branch`, `actor`
(`id` / `email` / `full_name`), `before_data`, `after_data`, `reason`,
`metadata`, `created_at`.

The facets payload (`views.py:1150-1153`):

```jsonc
{"actions": ["config.value.updated", …],            // <= 100
 "target_types": ["ConfigurationValue", …],          // <= 100
 "actors": [{"id": "7", "full_name": "Ada Nwosu", "email": "…"}, …],  // <= 200
 "targets": [{"type": "ConfigurationValue", "id": "…", "label": "Theme"}, …]}  // <= 200
```

The export job serializer (`serializers.py:448-470`) adds a computed
`download_available`, true only when the job is COMPLETED, has a stored file and
has not passed `available_until`.

## 4. Lifecycle / state machine

Audit events have no lifecycle: they are written once and never change.

Export jobs do:

```text
POST /export-jobs/  ->  QUEUED    (audit: config.audit.export_queued)
   task starts      ->  RUNNING
   task succeeds    ->  COMPLETED (audit: config.audit.export_completed)
                        available_until = completed_at + 7 days
   task fails       ->  FAILED    (failure_message set; any partial file deleted)
   queue unreachable->  FAILED    (set synchronously by the view, HTTP 503)
```

Download rules, in the order the view checks them (`views.py:1057-1073`):

```text
not the caller's own job              -> 404
job.tenant set and caller is neither
   platform nor that tenant           -> 404
status != COMPLETED                   -> 409
available_until passed or missing     -> 410
storage object missing               -> 404
otherwise                             -> file stream + config.audit.export_downloaded
```

A `RUNNING` job whose worker died is never reaped: there is no stuck-job sweep
for this table.

## 5. Derivations

- **Scope** (`services/audit_exports.py:67-73`): a resolved branch filters on
  `branch=`; a resolved tenant filters on `tenant=`; the platform layer filters
  on `tenant__isnull=True`. Note the tenant branch does **not** add
  `branch__isnull=True`, so a tenant-level reader sees their branches' events
  too. That is deliberate for an audit trail and is the opposite of how
  `GET /values/` behaves.

- **Target labels are resolved in bulk.** `build_configuration_target_labels`
  (`serializers.py:537-599`) groups the page's `(target_type, target_id)` pairs
  by model, canonicalises each id through `UUID(...)` so a formatting difference
  cannot cause a miss, and issues at most one query per model. A malformed id is
  skipped rather than raising. `IntegrationConnection` has no table, so its
  label is derived from the id itself (`"Email connection"`).
  `ConfigAPIView.paginate` injects that map into the serializer context for
  audit pages (`views.py:129-134`); the detail view does not, so a single event
  falls back to the per-row lookup inside `get_target_label`
  (`serializers.py:494-534`).

- **Facets are computed from the scoped queryset with the date window applied
  and the categorical filters deliberately omitted** (`views.py:1104-1106`), so
  picking an action does not empty the actor list. `actions` and `target_types`
  are `DISTINCT` queries capped at 100; `actors` is a distinct four-column
  `values_list` capped at 200; `targets` is derived from the **newest 500 events
  only** (`views.py:1129-1131`) and then de-duplicated - a silent sample with no
  flag in the response (`config_code_issues.md` §18).

- **The synchronous CSV fetches `EXPORT_LIMIT + 1` rows to detect truncation**
  and reports it in an `X-Export-Truncated` header rather than the body, because
  the body is a file (`views.py:1160-1166`). Ten columns, with `before` and
  `after` JSON-dumped with `sort_keys=True` so a diff between two exports is
  meaningful (`views.py:1185-1186`).

- **The queued export streams to a temporary file and never holds the result in
  memory** (`services/audit_exports.py:121-157`). Three details earn their
  comments:
  1. The temp file is opened in **binary** mode because storage backends read
     the handle through `django.core.files.File`; a text handle yields `str`
     chunks that the default `DatabaseStorage` cannot put in a `BinaryField`.
     A `TextIOWrapper` is layered on for `csv` and **detached** before the read
     so closing it does not close the file.
  2. Rows are written in batches of 500, and the size ceiling is checked after
     every batch, so an oversized export stops early instead of generating
     everything and failing at `save()`.
  3. The ceiling is `MEDIA_DB_MAX_BYTES` (default 25 MB), the same limit the
     default storage enforces (`services/audit_exports.py:29-40`), because a
     row's two free-form JSON blobs make byte size per row vary by orders of
     magnitude and a row cap alone cannot keep an export storable.

- **Two failure messages are written in the operator's words**, not the
  exception's (`services/audit_exports.py:185-204`): a row-limit message naming
  250,000 rows, and a size-limit message that renders sub-megabyte ceilings in
  bytes because `"0 MB"` tells the reader nothing. Everything else gets one
  generic message, with the traceback in the log.

- **Queue admission is serialised per user.**
  `create_configuration_audit_export` takes a `select_for_update` row lock on the
  actor before counting in-flight jobs (`services/audit_exports.py:215`), so
  three simultaneous requests cannot all observe fewer than three active jobs
  and overfill the queue. The limit is 3 (`:26`).

- **`client_key` makes queueing idempotent for five minutes**
  (`services/audit_exports.py:217-224`): the same key from the same user inside
  the window returns the existing job with HTTP 200 instead of creating another,
  and the response message changes to `"Existing export returned."`.

- **A dead broker is a 503, not a silent loss** (`views.py:1036-1046`). If
  `.delay()` raises, the job is marked FAILED with a message the operator can
  act on before the response is sent.

## 6. What reading writes

Almost nothing, and the exceptions are the interesting part.

| Surface | Writes |
|---|---|
| `GET /audit-events/`, `/<uuid>/`, `/facets/` | nothing |
| `GET /audit-events/export/` | **nothing** - see below |
| `GET /export/` (config snapshot) | **nothing** |
| `POST /audit-events/saved-views/` | the saved view row only, no audit event |
| `DELETE /audit-events/saved-views/<uuid>/` | deletes the row, no audit event |
| `POST /audit-events/export-jobs/` | the job row plus `config.audit.export_queued` |
| the background task | `config.audit.export_completed` on success |
| `GET .../download/` | `config.audit.export_downloaded`, with `row_count` |

So the **queued** path is fully bookended while the **synchronous** CSV - which
can take 5,000 audit rows including every before/after snapshot - leaves no
trace whatsoever (`config_code_issues.md` §7). The same is true of
`GET /v1/config/export/`, the full configuration snapshot.

Note also that `record_configuration_event` never passes `tenant=` to
`emit_audit_event` (`services/audit.py:48-55`), so every mirrored row in the
platform trail carries `tenant = NULL` (`config_code_issues.md` §12).

## 7. Worked example

```text
GET /v1/config/audit-events/?tenant=alpha-nt&action=config.value.updated
```

```json
{ "success": true, "message": "Data retrieved successfully",
  "pagination": { "currentPage": 1, "pageSize": 25, "totalItems": 2,
                  "totalPages": 1, "next": null, "previous": null },
  "data": [
    { "id": "…", "action": "config.value.updated",
      "target_type": "ConfigurationValue", "target_id": "…",
      "target_label": "Theme",
      "tenant": "…", "branch": null,
      "actor": { "id": "7", "email": "admin@alpha.ng", "full_name": "Ada Nwosu" },
      "before_data": { "value": "light" },
      "after_data":  { "value": "dark" },
      "reason": "Brand refresh", "metadata": {},
      "created_at": "2026-08-20T09:14:02Z" }
  ] }
```

Queue the same filter as a background export:

```text
POST /v1/config/audit-events/export-jobs/?tenant=alpha-nt
{"filters": {"action": "config.value.updated"}, "client_key": "abc123"}
```

```json
{ "success": true, "message": "Configuration audit export queued.",
  "data": { "id": "…", "status": "QUEUED", "filters": {"action": "config.value.updated"},
            "scope_key": "tenant:…", "tenant_slug": "alpha-nt",
            "file_name": "", "row_count": 0, "failure_message": "",
            "requested_at": "…", "available_until": null,
            "download_available": false } }
```

Repeating the identical call inside five minutes returns the same `id` with
HTTP 200 and `"Existing export returned."`. A fourth simultaneous job is a 400:

```json
{ "success": false,
  "error": { "code": "REQUEST_ERROR",
             "detail": { "detail": ["Three audit exports are already queued or running. Wait for one to finish."] } } }
```

And a too-large export lands as:

```json
{ "status": "FAILED",
  "failure_message": "This export is larger than the 25 MB file limit. Narrow the date range or filters and try again." }
```

## 8. Gotchas / known limitations

Full evidence in **`error/config/config_code_issues.md`**. Items belonging to
this slice:

- **`?actor=<any-uuid>` is a 500** on the list, the CSV export and the facet
  path, because the filter serializer accepts UUIDs while the user primary key
  is an integer (`serializers.py:380-392`, `services/audit_exports.py:59`). The
  same value saved into a view or an export job makes the background job fail
  with the generic message (§1). This is the module's worst defect.
- **The synchronous CSV and the config snapshot export write no audit event**,
  while their queued sibling writes three (§7).
- **Queued export files are never purged.** `available_until` blocks the
  download but nothing deletes the bytes, and there is no beat entry for this
  table (`apps/celery.py:18-60`) even though `vs_exports` has one (§8).
- **Facets sample only the newest 500 events for the target dictionary** and say
  nothing about it in the response (§18).
- **A `RUNNING` job whose worker died stays RUNNING forever**, and it counts
  against the caller's limit of three (§19).
- **Saved views are created and deleted with no audit event**, and the delete is
  a real delete (`views.py:987-996`) (§19).
- **`ConfigurationAuditSavedFiltersSerializer` speaks a different filter
  vocabulary from the list endpoint** (`window_days` versus
  `created_after`/`created_before`), and nothing in the backend translates
  between them (§19).
- **`ConfigurationAuditEvent` orders on `created_at` alone**
  (`models.py:647`) with no id tiebreaker, so events sharing a timestamp can
  reorder between pages (§19).
- **Config changes reach the platform audit trail with `tenant = NULL`** (§12),
  which is the same root cause `vs_audit`'s own report already flagged across
  the platform.
- **Justified by design:** the tenant scope filter does not exclude branch rows
  (`services/audit_exports.py:71-72`). A school administrator should see what
  happened in their branches.
- **Justified by design:** a foreign event id returns 404, not 403
  (`views.py:1196`). Tested at `tests.py:935-964`.
- **Justified by design:** the download check compares `job.tenant_id` with the
  caller's tenant *in addition to* ownership (`views.py:1064-1067`), so a job
  queued while a CX staffer was inside a school cannot be downloaded later from
  a different context.
- **Justified by design:** failure messages are pre-written strings and the
  exception goes to the log (`services/audit_exports.py:182`, `:201-204`).

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Restricted |
|---|---|---|---|
| Event list, detail, facets, saved views | `config.audit.view` | SENSITIVE | yes |
| CSV export, export jobs, download | `config.audit.export` | SENSITIVE | yes |

Seeded at `seed_config_permissions.py:11`, granted to `xvs_super_admin` and
`xvs_platform_admin` only.

**Isolation holds on every read surface**, because all four of them
(`_scoped_audit_queryset`, `_filtered_audit_queryset`, the facets view and the
detail view) go through `scoped_configuration_audit_queryset`, and the detail
view resolves the pk *inside* that scoped queryset rather than fetching first
and comparing afterwards (`views.py:1196`). That is what makes the 404 correct
rather than incidental.

**Saved views and export jobs are owned, not tenant-scoped.** Both list
querysets filter on the caller (`views.py:960-962`, `:1006-1008`), and the
delete and download routes re-apply that filter rather than trusting the pk.
The download route then adds the tenant check on top.

A school administrator with `config.audit.view` sees only their own tenant's
events. Platform events (tenant NULL) are invisible to them, and a school
caller can never resolve to the platform scope. Note the consequence: **the
platform tenant's own audit trail is the only place definition and capability
catalogue changes appear**, because those events are recorded with no tenant.

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:576-656` | `ConfigurationAuditEvent` and its Python immutability guard |
| `migrations/0003_configuration_audit_immutability.py` | The database triggers |
| `models.py:659-685` | `ConfigurationAuditSavedView` |
| `models.py:688-735` | `ConfigurationAuditExportJob` |
| `services/audit.py:19-56` | `record_configuration_event` - local write plus mirror |
| `services/audit.py:60-113` | `write_audit_log` - the single coupling point to `vs_audit` |
| `services/audit_exports.py:29-73` | Size ceiling, filter snapshot, shared filters, scope |
| `services/audit_exports.py:94-206` | `execute_configuration_audit_export` |
| `services/audit_exports.py:209-250` | `create_configuration_audit_export` - lock, idempotency, limit |
| `tasks.py` | The Celery entry point |
| `views.py:931-950` | `_scoped_audit_queryset`, `_filtered_audit_queryset` |
| `views.py:953-996` | Saved views |
| `views.py:999-1088` | Export jobs and download |
| `views.py:1092-1200` | Event list, facets, synchronous CSV, detail |
| `serializers.py:372-470` | Filter, saved-view and export-job serializers |
| `serializers.py:473-599` | Event serializer and the bulk label resolver |

## 11. Test coverage & gaps

Baseline: **`Ran 61 tests in 94.867s` - OK**. The one traceback in the run is
`test_oversized_export_fails_with_the_size_limit_in_its_own_words` logging its
own expected failure.

What this slice covers:

- `ConfigurationResolutionTests.test_audit_events_cannot_be_changed_or_deleted`
  (`tests.py:153-161`) and
  `test_secret_references_are_redacted_in_audit` (`tests.py:139-152`).
- `ConfigurationAuditDetailAPITests` (`tests.py:882-964`) - the detail payload's
  redacted snapshots and the list filters, a malformed date rejected as 400, a
  malformed actor rejected as 400, facets and the filtered CSV export sharing
  the actor and target filters, and a foreign event id returning 404.
- `ConfigurationAuditSavedViewsAndExportsTests` (`tests.py:1058-1234`) - saved
  views being personal with duplicate names rejected; an export queued,
  generated and downloadable only by its owner; the export written through the
  real configured storage backend rather than a mock; an oversized export
  failing with the size limit in its own words; and the queue being idempotent
  by `client_key` and limited to three active jobs.

That last group is unusually good: it exercises the real storage backend and the
real byte ceiling rather than asserting against a fake.

What it does not cover:

1. **A well-formed UUID in `?actor=`** (issues file §1).
   `test_invalid_actor_filter_is_rejected` (`tests.py:917-919`) uses
   `"not-a-uuid"`, which the serializer correctly refuses. The value that gets
   through is the one nobody tried.
2. **That the synchronous CSV writes no audit event** (issues file §7). Nothing
   asserts either behaviour, so neither is pinned.
3. **File retention.** Nothing asserts what happens to the bytes after
   `available_until`, which is why the missing purge is invisible.
4. **A `RUNNING` job that never completes**, and its effect on the three-job
   limit.
5. **The facets 500-row sample and the 100/200 caps**: the existing facet test
   has a handful of events, so no cap is ever reached.
6. **The 5,000-row cap and the `X-Export-Truncated` header** on the synchronous
   CSV.
7. **A saved view being replayed.** Nothing asserts that the `window_days`
   vocabulary a view stores can actually drive the list endpoint, which is where
   the mismatch in issues file §19 would surface.
8. **The dead-broker 503 branch** (`views.py:1036-1046`) and the
   `available_until` 410 / not-ready 409 branches on download.
9. **Pagination of the event list**, including the missing id tiebreaker.
10. **The empty-list response shape** on the event list, saved views and export
    jobs, which `success_response` renders as `{}` (`core/response.py:6-11`).
