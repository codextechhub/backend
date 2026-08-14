# audit_compliance_exports

The two write-capable surfaces in `vs_audit`: **compliance rules**
(`/v1/audit/compliance-rules/`), a configurable policy table, and **audit
exports** (`/v1/audit/exports/`), which turn a filtered event list into CSV.
Plus the third way audit data leaves the building: the **Export Centre
dataset** `audit.events`, registered from this app but executed by
`vs_exports`.

Read `docs/audit/audit_event_stream.md` first; this slice reuses its filter
contract verbatim.

---

## 1. What it is (and what it is NOT)

- **`ComplianceRule` (`models.py:523`) is configuration, not enforcement.**
  Four rule types exist (`RETENTION`, `MASKING`, `ACCESS`, `EXPORT`,
  `models.py:164-169`). Exactly one of them does anything anywhere in the
  codebase: an active `MASKING` rule can redact the `summary` column of a CSV
  export (`views.py:374-379,399-400`). Retention, access and export rules are
  stored, listed, edited and never read. `applies_to_event`
  (`models.py:608-622`) has no caller at all.
- **`AuditExportJob` (`models.py:411`) is named for a background job it is
  not.** The `POST` handler builds the entire CSV inline, in the request
  (`views.py:352-420`). `PENDING`, `FAILED` and `EXPIRED` are declared statuses
  that nothing writes; `mark_running` and `mark_failed` (`models.py:480-511`)
  have no caller.
- **There is no download endpoint.** The CSV body is stored in the job row's
  `file_path` column and handed back inline by the detail serializer
  (`views.py:412`, `serializers.py:226`). `file_name` is a label the frontend
  is expected to save under, not a key in storage.
- **The Export Centre dataset is the other, better path.** `audit.events`
  (`export_datasets.py:44-91`) is registered into `vs_exports` from
  `VsAuditConfig.ready` (`apps.py:8-18`), is tenant-scoped, requires a date
  range, caps at 500,000 rows and marks `actor_email` sensitive. The in-app
  `/exports/` route shares none of those properties.
- **These two surfaces do not know about each other.** A `MASKING` rule
  affects the in-app CSV and not the Export Centre file; the Export Centre's
  field-level sensitivity gate affects its file and not the in-app CSV.

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `ComplianceRule` | `models.py:523` | `id` (UUID pk), `tenant?` (null = global), `name` (**globally unique**), `description`, `rule_type`, `module_key?`, `action_type?`, `is_active`, `retention_days?`, `masking_fields` (JSON list), `config` (JSON), `created_at`/`updated_at` |
| `AuditExportJob` | `models.py:411` | `id` (UUID pk), `requested_by?`, `export_format` (CSV only), `status`, `filter_payload` (JSON), `file_name`, `file_path` (**varchar(500)**), `row_count`, `failure_reason`, `requested_at`, `started_at?`, `completed_at?`, `expires_at?` |

`ComplianceRule` is the only tenant-aware model in `vs_audit`:
`objects = TenantAwareManager(include_global=True)` with
`all_objects = models.Manager()` (`models.py:587-588`). The manager narrows to
`tenant = <asserted tenant> OR tenant IS NULL`
(`vs_rbac/managers.py:100-118`), so every rule view is scoped without the view
doing anything. Indexes on `(rule_type, is_active)` and `(tenant, rule_type)`.

`AuditExportJob` has **no tenant field and no tenant-aware manager**. Default
ordering `-requested_at`; `status` is the only indexed column
(`models.py:444-449,473-474`).

`file_path` is declared `max_length=500` with the help text "Path/object key in
storage" (`models.py:459-463`, migration `0001_initial:252-258`, never altered).
What the view actually stores in it is the complete CSV document. See §8.

## 3. Endpoint map

Gate on every route: `IsAuthenticatedAndActive & HasRBACPermission`, and
`?tenant=<slug>` is required throughout (`vs_rbac/authentication.py:123-126`).

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /compliance-rules/` | `platform.audit.manage` | `i_slug`, `rule_type`, `is_active` (`"true"`/`"false"`), `module_key` | Paginated `ComplianceRuleListSerializer`, ordered by name (`views.py:565-588`) |
| `POST /compliance-rules/` | `platform.audit.manage` | `name`, `description`, `rule_type`, `tenant`, `module_key`, `action_type`, `is_active`, `retention_days`, `masking_fields`, `config` (`serializers.py:287-305`) | `201` + created rule in the success envelope |
| `GET /compliance-rules/<uuid:id>/` | `platform.audit.manage` | - | `ComplianceRuleDetailSerializer` (adds `masking_fields`, `config`, `created_at`) |
| `PUT`/`PATCH /compliance-rules/<uuid:id>/` | `platform.audit.manage` | same body as create | Updated rule |
| `DELETE /compliance-rules/<uuid:id>/` | `platform.audit.manage` | - | **Hard delete**, `200` + `"Deleted successfully."` (`core/mixins.py:86-98`) |
| `GET /exports/` | `platform.audit.export` | `status` | Paginated `AuditExportJobListSerializer` (no `file_path`) |
| `POST /exports/` | `platform.audit.export` | `{export_format?, filter_payload}` | `201` + `AuditExportJobDetailSerializer` **including the whole CSV in `file_path`** (`views.py:416-420`) |
| `GET /exports/<uuid:id>/` | `platform.audit.export` | - | `AuditExportJobDetailSerializer`, likewise including `file_path` |

`filter_payload` is not free-form: it goes through the same
`AuditEventFilterSerializer` as the Event Explorer, with
`raise_exception=True`, so an invalid enum or date is a `400` before any job
row is created (`views.py:363-365`). Any `export_format` other than `CSV` is
rejected with a `400` (`views.py:357-361`).

The Export Centre surface is not routed here. `audit.events` appears under
`vs_exports` with `permission="platform.audit.export"`,
`scope=DatasetScope.TENANT`, `row_cap=500_000`, and a **required** `event_at`
range filter (`export_datasets.py:56-90`). Its screen binding maps the audit
console's own query params onto that dataset with a 90-day default window, and
explicitly reports `actor` as unmapped rather than silently dropping it
(`export_datasets.py:98-144`).

## 4. Lifecycle / state machine

`ExportJobStatus` declares five states (`models.py:155-161`). The code reaches
two:

```text
POST /exports/
   │
   ├─ validate filter_payload ─────────► 400, no row written
   ├─ create(status=RUNNING, started_at=now)          ← PENDING is skipped
   ├─ stream qs.iterator() into a StringIO CSV
   └─ mark_completed(row_count, file_name,
                     file_path=<the CSV body>,
                     expires_in_days=7)  ─► COMPLETED, expires_at = now + 7d

any exception during the write ──► the RUNNING row stays RUNNING forever
                                   (no atomic block, mark_failed never called)

FAILED   - declared, never written
EXPIRED  - declared, never written; nothing reads expires_at or is_expired
```

`ComplianceRule` has no lifecycle: create, edit, hard delete.

## 5. Derivations

- **Masking.** Every active `MASKING` rule visible to the caller's tenant
  contributes its `masking_fields` list to one flat set
  (`views.py:374-378`). Then exactly one check is performed, once per row:
  `if "summary" in masked_fields: summary = "[REDACTED]"`
  (`views.py:399-400`). Listing `actor_label`, `entity_label` or `metadata` in
  `masking_fields` has no effect on anything.
- **The CSV shape** is fixed at 13 columns (`views.py:390-394`): `event_id`,
  `event_at`, `module_key`, `action_type`, `severity`, `status`, `actor_type`,
  `actor_email`, `actor_label`, `entity_type`, `entity_id`, `entity_label`,
  `summary`. `actor_email` is read off the joined user or left blank for system
  events (`views.py:397`); `before_data`, `diff_data` and `metadata` are not
  exported at all.
- **`row_count`** is counted while writing, not queried
  (`views.py:395,406`), so it always matches the file.
- **`expires_at`** is `now + 7 days`, hard-coded at the call site
  (`views.py:413`); `mark_completed` leaves it null if `expires_in_days <= 0`
  (`models.py:493`).
- **`is_active` parsing** is a string comparison, not a boolean field:
  `"true"` filters to active, `"false"` to inactive, and **anything else is
  ignored entirely** (`views.py:579-583`), so `?is_active=1` silently returns
  both.
- **`ComplianceRule.applies_to_event`** (`models.py:608-622`) would resolve a
  rule against one event by active flag, tenant, module and action. It is
  written, it is correct, and nothing calls it.

## 6. What writing does

**The export writes nothing to the audit trail.** `POST /exports/` creates the
job row and returns; no `emit_audit_event` call appears anywhere in this file.
The vocabulary for it already exists and is even given summary templates -
`EXPORT_REQUESTED`, `EXPORT_COMPLETED`, `EXPORT_FAILED`
(`models.py:125-127`, `services.py:66-68`) - but the only emitter of those
constants is the Export Centre (`vs_exports/constants.py:329-332`). So taking
the entire platform's audit trail out of the building through
`/v1/audit/exports/` leaves no record that it happened, while doing the same
thing through the Export Centre does. The module's own docstring argues the
opposite position: "reading the console and taking the trail out of the
building are different decisions" (`export_datasets.py:7-9`).

**Compliance rule changes write nothing either.** Create, update and hard
delete of a policy row produce no audit event, despite `CONFIG_CHANGED`
existing for exactly this shape (`models.py:122`).

Neither handler opens a transaction. The export in particular creates its job
row and then does the expensive work outside any atomic block
(`views.py:380-414`).

## 7. Worked example

```text
POST /v1/audit/exports/?tenant=codex
{ "export_format": "CSV",
  "filter_payload": { "module_key": ["IDENTITY"], "status": ["FAILED"],
                      "date_from": "2026-08-01T00:00:00Z" } }
```

```json
{ "success": true, "message": "Export job completed.",
  "data": {
    "id": "9c2e…", "requested_by": { "id": 12, "email": "ops@codex.test",
                                     "full_name": "Ada Ops" },
    "export_format": "CSV", "status": "COMPLETED",
    "filter_payload": { "module_key": ["IDENTITY"], "status": ["FAILED"],
                        "date_from": "2026-08-01T00:00:00Z" },
    "file_name": "audit_export_9c2e….csv",
    "file_path": "event_id,event_at,module_key,…\n4b1f…,2026-08-13T18:22:04+00:00,IDENTITY,…",
    "row_count": 61, "failure_reason": "",
    "requested_at": "2026-08-14T09:30:02Z", "started_at": "2026-08-14T09:30:02Z",
    "completed_at": "2026-08-14T09:30:04Z", "expires_at": "2026-08-21T09:30:04Z" }
}
```

That response is what the endpoint is *designed* to return. What it actually
returns for 61 rows is a `500`: see the first item in §8.

## 8. Gotchas / known limitations

- **Any export bigger than a few rows fails with a database error, and leaves a
  stuck job behind.** `mark_completed(file_path=buffer.getvalue())`
  (`views.py:412`) writes the full CSV document into a column declared
  `max_length=500` (`models.py:459-463`; migration `0001_initial:252-258`, never
  altered). The header row alone is 134 characters and one event row is
  roughly 150 to 250, so the limit is breached at around three rows. Django
  does not validate on `save(update_fields=...)`, so PostgreSQL raises
  `DataError: value too long for type character varying(500)`, the view 500s,
  and because there is no atomic block and no `mark_failed` call the `RUNNING`
  job row created at `views.py:380-386` is committed and orphaned. **This is
  the fix-first item in the module.** The root cause is the design choice
  behind it, not the column width: a CSV body does not belong in a
  `CharField` at all. Write the file through storage and return a download
  route, or store the body in a `TextField` and stop calling the column
  `file_path`.
- **Every holder of `platform.audit.export` can read everyone else's export
  files.** `AuditExportJob` has no tenant column and the list queryset is
  `AuditExportJob.objects.select_related("requested_by").all()`
  (`views.py:337-347`) with no `requested_by` filter; the detail route is a
  bare UUID lookup (`views.py:539-545`); and both detail responses include
  `file_path`, which is the export's entire contents
  (`serializers.py:217-231`). So one person's unfiltered dump of the platform
  audit trail is readable by anyone else holding the key. The list serializer
  correctly omits `file_path` (`serializers.py:194-205`); the detail one does
  not.
- **The in-app export has no date requirement, no row cap and no field-level
  gate.** `POST /exports/` with `filter_payload: {}` validates cleanly
  (every filter field is `required=False`, `serializers.py:351-379`) and
  streams the entire `AuditEvent` table through `qs.iterator()`
  (`views.py:368-406`), inline, in the web process, including `actor_email` for
  every row. The Export Centre dataset registered by the same app makes the
  date range **required** precisely because "an unbounded audit export is never
  what anyone actually wants" (`export_datasets.py:76-79`), caps at 500,000
  rows, and marks `actor_email` sensitive (`export_datasets.py:71-73`). The
  in-app route is the weaker of the two doors into the same data and should
  either adopt those guards or be retired in favour of the dataset.
- **Exporting the audit trail is not itself audited.** No `emit_audit_event`
  call exists in this file, though `EXPORT_REQUESTED`/`COMPLETED`/`FAILED` are
  defined and templated for it (§6). Nor is creating, editing or deleting a
  compliance rule.
- **`MASKING` rules mask exactly one column.** `masking_fields` is a free JSON
  list with no validation (`models.py:576-580`, `serializers.py:287-305`), and
  the only key the code ever tests for is `"summary"`
  (`views.py:399-400`). A rule listing `actor_email` reads as enforced in the
  UI and does nothing. Either validate the list against the export's column
  set, or apply it generically over the row dict.
- **`RETENTION` rules are inert, and there is no retention anywhere.**
  `retention_days` is validated on write in two places
  (`models.py:603-606`, `serializers.py:307-316`) and read in none. Nothing
  prunes `AuditEvent`, `EntityAuditTrail` or `AuditExportJob`
  (see `docs/audit/audit_event_stream.md` §4). A configured 365-day retention
  policy is, today, a row in a table.
- **`?i_slug=` cannot work.** The queryset is already narrowed by
  `TenantAwareManager` to "this tenant or global" before the filter is applied
  (`models.py:587`, `views.py:566-577`), so `?i_slug=<another tenant>` always
  returns empty and `?i_slug=<own slug>` merely drops the global rules. The
  parameter promises cross-tenant rule administration and cannot deliver it.
- **A platform admin cannot see or manage a school tenant's rules, but can
  create them.** `tenant` is a writable field on the create/update serializer
  with no validation (`serializers.py:287-305`), so a `platform.audit.manage`
  holder can `POST` a rule owned by any tenant - and then never see it again
  through this API, because the manager scopes reads to their own tenant. The
  rule is live and invisible. Either drop `tenant` from the writable set and
  derive it from `request.tenant`, or give platform actors an explicit
  cross-tenant read path.
- **`ComplianceRule.name` is globally unique** (`models.py:546`). Two tenants
  cannot both have a rule called "PII masking"; the second one gets a
  uniqueness error naming a row it is not allowed to see. The constraint
  should be `unique_together("tenant", "name")`.
- **Compliance rules are hard-deleted.** `DestroyModelMixin.perform_destroy`
  calls `instance.delete()` (`core/mixins.py:97-98`) with no soft-delete flag
  and, as above, no audit event. A deleted policy leaves no trace that it ever
  existed.
- **Three of five export statuses are dead vocabulary.** `PENDING` is skipped
  (the row is created `RUNNING`), `FAILED` is never written because
  `mark_failed` has no caller, and `EXPIRED` is never written because nothing
  reads `expires_at` or the `is_expired` property (`models.py:513-516`). The
  `?status=` filter on the list route advertises all five.
- **`?status=` on `/exports/` is unvalidated.** `?status=bogus` returns an
  empty page rather than a `400` (`views.py:342-345`).
- **Justified by design:** `ComplianceRuleDetailView` uses the tenant-aware
  manager for its lookup queryset (`views.py:606`), so a pk from another tenant
  is a clean `404`, not a leak. This is the one properly isolated surface in
  the module and it got there for free, by using the right manager.
- **Justified by design:** the export reuses `apply_audit_event_filters`
  rather than reimplementing the filters (`views.py:368-371`). One contract for
  the list and the file is exactly right, and the comment at `views.py:367`
  says so.

## 9. Permissions & tenant isolation

| Surface | Gate | Seeded? |
|---|---|---|
| `/compliance-rules/` (all verbs) | `platform.audit.manage` | Yes, `SENSITIVE`, restricted (`core/management/commands/seed_platform_permissions.py:134-139`) |
| `/exports/` (list + create), `/exports/<id>/` | `platform.audit.export` | Yes, `SENSITIVE`, restricted (same) |
| Export Centre dataset `audit.events` | `platform.audit.export` | same key, enforced by `vs_exports` |

Both keys are correctly marked restricted and sensitive in the seed, and both
are granted to `xvs_super_admin` and `xvs_platform_admin` only. The gap is the
same one recorded in `docs/audit/audit_event_stream.md` §8: nothing in the RBAC
write path prevents a `platform.*` key from being attached to a school-tenant
role template, and `HasRBACPermission` will honour it.

Tenant isolation is split:

- **Compliance rules: isolated.** `TenantAwareManager(include_global=True)`
  scopes list, retrieve, update and delete to the asserted tenant plus global
  rules, and the masking lookup in the export inherits the same scoping
  (`views.py:375`). The gaps are the writable `tenant` field and the global
  `name` uniqueness, both above.
- **Export jobs: not isolated at all.** No tenant column, no tenant filter, no
  owner filter, and the file body is exposed on both detail responses.
- **Export Centre dataset: isolated by construction.** `_audit_events` filters
  `tenant=scope.tenant` (`export_datasets.py:31-34`) - which, given that most
  audit rows carry `tenant = NULL` (`docs/audit/audit_event_stream.md` §8),
  currently means it returns almost nothing. Correct and near-empty, rather
  than useful and leaky.

## 10. Code map

| File | Responsibility |
|---|---|
| `vs_audit/models.py:411-516` | `AuditExportJob`, its five statuses and the three unused transition helpers |
| `vs_audit/models.py:523-622` | `ComplianceRule`, the tenant-aware manager, the unused `applies_to_event` |
| `vs_audit/views.py:326-420` | Export list + the synchronous CSV builder |
| `vs_audit/views.py:532-545` | Export detail (exposes `file_path`) |
| `vs_audit/views.py:552-614` | Compliance rule CRUD |
| `vs_audit/serializers.py:185-231` | Export job list vs detail split |
| `vs_audit/serializers.py:238-316` | Compliance rule read/write serializers and the retention check |
| `vs_audit/export_datasets.py` | The `audit.events` dataset, its fields/filters, and the console screen binding |
| `vs_audit/apps.py:8-18` | `ready()` - where the dataset and screen are registered |
| `vs_rbac/managers.py:78-118` | `TenantAwareManager` - why rules are scoped and jobs are not |
| `core/mixins.py:86-98` | `DestroyModelMixin` - the hard delete behind `DELETE /compliance-rules/<id>/` |

## 11. Test coverage & gaps

**Zero.** `vs_audit/tests.py` covers the filter contract and proxy attribution
only; nothing in this app tests compliance rules, export jobs or the dataset
registration. `vs_todo/tests.py:296` references `platform.audit.export` while
testing something else entirely.

The absence explains the §8 list: the `file_path` overflow would be caught by
the very first end-to-end export test with more than three events in the
fixtures.

Needed, in priority order:

1. **An export with enough rows to exceed 500 characters**, asserting a
   `COMPLETED` job and a body that round-trips through `csv.reader`. This test
   fails today and should.
2. **`403` without `platform.audit.export` / `platform.audit.manage`** on all
   eight routes, driving the real auth path so `?tenant=` is exercised.
3. **Cross-tenant isolation**, in both directions: a compliance rule from
   another tenant must 404 on detail and be absent from the list (this should
   pass), and an export job from another tenant must not be readable (this
   will fail, and should).
4. **Masking**, asserting that an active `MASKING` rule listing `summary`
   redacts the column, that an inactive one does not, and that a rule listing
   any other field currently changes nothing - pinning the limitation until it
   is fixed.
5. **The compliance rule contract**: `retention_days` required for
   `RETENTION`, `?is_active=` string parsing including the ignored third case,
   the global `name` uniqueness collision across tenants, and the hard delete.
6. **The dataset registration**: that `audit.events` is registered at
   `ready()`, that its `event_at` filter is required, and that `actor_email` is
   flagged sensitive.
7. **The empty-list response shape** on `/exports/` and `/compliance-rules/`,
   since `success_response` coerces `[]` to `{}` (`core/response.py:6-11`).
