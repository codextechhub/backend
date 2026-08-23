# export_builder_definitions

The half of the Export Centre a person actually touches: the saved recipe
(`ExportDefinition`), who it is shared with, the estimate-and-sample loop the
builder runs on every keystroke, the capability flags that let the UI disable
things with a reason, and the quick export that runs a configuration nobody
saved.

Routes covered here (`/v1/exports/`): `capabilities/`, `preview/`,
`definitions/`, `definitions/<pk>/`, `definitions/<pk>/duplicate/`,
`definitions/<pk>/share/`, `definitions/<pk>/run/`, `quick/`.

What may be exported is `export_catalogue_datasets`. What happens after the run
is triggered is `export_runs_and_files`.

Findings live in **`error/exports/export_code_issues.md`**.

---

## 1. What it is (and what it is NOT)

- **A definition is a recipe, not data.** Editing it changes future files only;
  files already produced are never altered, and the response says so out loud
  ("Export updated successfully. Files already produced are unchanged.",
  `views.py:391`).
- **A definition is owned by one person, and runs execute as the owner**
  (`models.py:115-119`). Sharing never widens what the data shows.
- **Sharing grants sight, never edit rights** (`models.py:180-183`,
  `views.py:146-155`). A recipient sees the export and its files; to change
  anything they duplicate it, and the error message says exactly that.
- **A draft is visible but not runnable** (`models.py:132-134`). It is the
  escape hatch for a recipe that is not finished: the write serializer relaxes
  every completeness rule for a draft (`serializers.py:177-195`) and
  `trigger_run` refuses to run one (`services.py:200-204`).
- **Delete means archive.** Runs reference the definition with `SET_NULL`, so a
  hard delete would orphan them; archiving keeps the audit trail intact and
  takes the export out of the list, which is what "delete" means to the person
  clicking it (`views.py:396-403`). It does **not** currently stop the export
  running - see `export_code_issues` §2.
- **A quick export is a run with no recipe.** Started from a module list screen
  with its filters already applied, `definition` stays null, the run's own
  `frozen_config` is the only record of what it produced, and only the person
  who asked for it can see or download the result (`views.py:614-624`,
  `services.py:233-241`).
- **Estimates are advisory; run-time checks are authoritative.** The same
  `resolve_columns` serves both, which is what stops the two drifting apart
  (`engine.py:106-121`). At build time it shapes the picker; at run time it
  shapes the file.
- **This is not a report builder.** There are no computed columns, no grouping,
  no totals row, no joins the dataset did not declare. It selects declared
  columns of declared rows.

## 2. Domain model

### `ExportDefinition` (`models.py:66`)

| Field | Meaning |
|---|---|
| `tenant` | PROTECT. Always the outer boundary |
| `entity` | PROTECT, **nullable**. Set for entity-scoped datasets only |
| `dataset_key` | Key into the catalogue, e.g. `finance.customer_invoices` |
| `name`, `description` | Display |
| `columns` | Ordered list of field ids - this *is* the column order in the file |
| `filters` | List of filter specs, each `{id, ...}` per the filter's kind |
| `sort` | Ordered list of `{field, direction}` |
| `format` | `csv` or `xlsx` (`constants.py:207-211`) |
| `format_options` | Options for **this format only**, discriminated by format |
| `values_mode` | `people` or `system` (`constants.py:224-228`) |
| `file_name_pattern` | Supports `{date} {datetime} {entity} {run}` |
| `owner` | PROTECT. Runs execute as this person |
| `sharing` | Display flag; the share rows are the truth (see §8) |
| `is_draft` | Visible, not runnable |
| `is_archived` | Out of the list |

`Meta.ordering = ["-updated_at"]`, with indexes on `(tenant, -updated_at)`,
`(entity, dataset_key)` and `(owner, -updated_at)` (`models.py:138-144`).

`render_file_name` (`models.py:157-176`) expands the tokens and then strips the
result to alphanumerics, `-`, `_` and `.`, keeping it safe for a
`Content-Disposition` header and for disk. A tenant-scoped export has no entity,
so `{entity}` resolves to the tenant slug rather than leaving a literal
`{entity}` in the name.

### `ExportDefinitionShare` (`models.py:178`)

`(definition, user, shared_by)`, unique on `(definition, user)`
(`models.py:196-201`). Nothing else: sharing is a visibility list, not a
permission grant.

## 3. Endpoint map

| Route | Method | `rbac_permission` | View |
|---|---|---|---|
| `capabilities/` | GET | `exports.catalogue.view` | `CapabilitiesView` (`views.py:248`) |
| `preview/` | POST | `exports.catalogue.view` | `PreviewView` (`views.py:263`) |
| `definitions/` | GET, POST | `definition.view` **or** `definition.create` | `DefinitionListView` (`views.py:314`) |
| `definitions/<pk>/` | GET, PATCH, DELETE | `definition.view` / `.update` / `.delete` | `DefinitionDetailView` (`views.py:362`) |
| `definitions/<pk>/duplicate/` | POST | `exports.definition.create` | `views.py:417` |
| `definitions/<pk>/share/` | POST | `exports.definition.share` | `views.py:452` |
| `definitions/<pk>/run/` | POST | `exports.run.create` | `views.py:493` |
| `quick/` | POST | `exports.run.create` | `QuickExportView` (`views.py:614`) |

`rbac_permission` on a list route is **any-of**, so a viewer gets through the
route gate and the finer check happens inside: `POST /definitions/` re-checks
`definition.create` and 403s with "You cannot create exports"
(`views.py:335-337`). Same shape for `POST /schedules/`.

### Query parameters actually read

`GET /definitions/` (`views.py:319-332`) reads exactly four:

| Param | Effect |
|---|---|
| `?module=` | Resolved to dataset keys through the catalogue, then `dataset_key__in` |
| `?owner=me` | `owner=request.user` |
| `?q=` | `name__icontains` OR `description__icontains` |
| `?include_archived=true` | Otherwise `is_archived=False` |

### Request bodies actually read

**`POST /preview/`** - `PreviewSerializer` (`serializers.py:426`):
`dataset_key` (validated against the catalogue), `columns`, `filters`, `sort`,
`format`, `values_mode`. Nothing else is read.

**`POST /quick/`** - `QuickExportSerializer` (`serializers.py:444`) is
`PreviewSerializer` plus `name`, `format_options`, `client_key` and `sync`, so
the drawer can send exactly what it just previewed.

**`POST` / `PATCH /definitions/`** - `ExportDefinitionWriteSerializer`
(`serializers.py:121`): `name`, `description`, `dataset_key`, `columns`,
`filters`, `sort`, `format`, `format_options`, `values_mode`,
`file_name_pattern`, `sharing`, `is_draft`.

**`tenant`, `entity` and `owner` are never read from the body** - the view sets
them from the request (`views.py:349-351`), which is where mass assignment is
stopped. There is a test (`tests.py:344`).

**`POST /definitions/<pk>/share/`** - `ShareSerializer` (`serializers.py:472`):
`user_ids` only.

**`POST /definitions/<pk>/run/`** - `RunRequestSerializer`
(`serializers.py:463`): `client_key` only.

### Serializer field sets

`ExportDefinitionListSerializer` (`serializers.py:41`) publishes id, name,
description, `dataset` (`{id, name, module, available}`), `scope`,
`entity_code`, format, `values_mode`, `column_count`, `owner_name`, `is_owner`,
`sharing`, `shared_with` (a count), `is_draft`, `is_archived`, `last_run`,
`updated_at`.

`ExportDefinitionDetailSerializer` (`serializers.py:103`) adds `columns`,
`filters`, `filters_readable` (the plain sentences), `sort`, `format_options`,
`file_name_pattern`, `created_at`.

A withdrawn dataset is **stated, not hidden**: `available: false` and the row
still renders (`serializers.py:63-71`). `entity_code` is a method field rather
than `source="entity.code"` because a tenant-scoped export has no entity and DRF
would raise walking through the null (`serializers.py:45-47`).

## 4. Lifecycle / state machine

A definition has no status column. It has three independent booleans, and their
combinations are the states:

```
                 is_draft   is_archived
new, incomplete     true       false     visible, not runnable, relaxed validation
finished            false      false     runnable, schedulable, shareable
"deleted"           either     true      hidden from the list; files it produced survive
```

`is_draft` is what the write serializer keys its completeness rules off
(`serializers.py:177-195`): a non-draft must have at least one column and every
required filter set; a draft may have neither, and the error message points at
the escape hatch - "Save it as a draft if you are not ready."

Sharing has two representations that are kept in step in exactly one place:

```
POST /definitions/<pk>/share/ {user_ids: [...]}
   ├─ users = same tenant only, owner excluded          (views.py:470-473)
   ├─ shares.all().delete() ; bulk_create(new)          (views.py:475-479)
   └─ sharing = SHARED if users else PRIVATE            (views.py:481-483)
```

## 5. Derivations

| Output | Formula | Where |
|---|---|---|
| Resolved columns | locked fields first, then requested, minus withdrawn and forbidden | `resolve_columns` (`engine.py:106`) |
| `matching_rows` | `qs.values("pk")[:EXACT_COUNT_LIMIT + 1].count()` | `engine.py:311` |
| `estimate_confidence` | `exact` when rows ≤ 100,000, else `bucketed` | `engine.py:312, 348` |
| `estimated_bytes` | `rows × max(columns, 1) × bytes-per-cell` (11 xlsx, 9 csv) | `engine.py:295, 317` |
| `row_cap` | `min(dataset.row_cap, DEFAULT_ROW_CAP)` | `engine.py:319` |
| `reads_as` | one sentence a finance user can check | `plain_sentence` (`engine.py:498`) |
| `format_options` | schema defaults, overlaid with the caller's declared keys only | `clean_format_options` (`services.py:128`) |
| `run_inline` | server's own `estimated_bytes ≤ SYNC_EXPORT_MAX_BYTES` | `views.py:647-653` |

**Counting is deliberately cheap.** The count is a LIMITed count - one row past
the limit - so a huge table costs the same as a small one, and it is enough to
tell "exactly N" from "more than N" (`engine.py:309-312`). Above the limit the
builder gets a bucketed figure and a lowered confidence rather than a spinner
where a number should be.

**Locked fields lead and are never dropped.** `resolve_columns` puts the
dataset's locked field ids first and then everything the caller asked for
(`engine.py:117-120`) - they are the row's identity, and a file whose rows
cannot be identified is not an export.

**Withdrawn and forbidden are different omissions.** A column that no longer
exists produces `FIELD_WITHDRAWN`; one the caller has lost access to produces
`FIELD_FORBIDDEN` (`engine.py:132-156`). Keeping them apart is what lets the run
detail say *which* of the two happened.

**Format options stay discriminated by format.** `clean_format_options` starts
from the schema defaults for the chosen format and merges in only the keys that
format declares (`services.py:128-141`), so switching format cannot leave a
stale CSV delimiter sitting on an Excel export. There is a test
(`tests.py:328`).

**The estimate's four warnings** (`engine.py:320-345`): `ESTIMATE_BUCKETED`,
`LARGE_RESULT` (above 250,000 rows), `ROW_CAP_EXCEEDED`, `WIDE_DATE_RANGE`, plus
one entry per column omission. None of them blocks.

## 6. What writing writes

| Action | Rows written | Audit event |
|---|---|---|
| `POST /definitions/` | one `ExportDefinition` | `EXPORT_DEFINITION_CREATED` (`views.py:353`) |
| `PATCH /definitions/<pk>/` | the definition | `EXPORT_DEFINITION_UPDATED`, metadata carrying the **before** columns, filters and format (`views.py:386`) |
| `DELETE /definitions/<pk>/` | `is_archived = True` | `EXPORT_DEFINITION_DELETED`, severity WARNING, metadata `files_kept` (`views.py:406`) |
| `POST .../duplicate/` | one new definition, owned by the caller, `sharing=PRIVATE` | `EXPORT_DEFINITION_CREATED` with `duplicated_from` (`views.py:445`) |
| `POST .../share/` | share rows replaced; `sharing` recomputed | `EXPORT_DEFINITION_SHARED` with the recipient ids (`views.py:485`) |
| `POST /preview/` | nothing | none; one analytics event, bucketed (`views.py:290`) |
| `POST /quick/` | one `ExportRun`, `definition=None` | `EXPORT_REQUESTED` (`services.py:279`) |

A copy is never born shared (`views.py:437`), and a share never crosses a tenant
boundary - the user lookup filters on `tenant=self.tenant` and excludes the
owner (`views.py:470-473`).

## 7. Worked example

Building "Overdue invoices, monthly" from scratch.

**1. What can I do?**

```
GET /v1/exports/capabilities/
→ {"can_create": true, "can_run": true, "can_share": true,
   "can_export_sensitive": false, "can_view_activity": false,
   "allowed_entities": [{"id": 3, "code": "CSS", "name": "Corona Secondary"}],
   "row_cap": 500000, "concurrent_run_limit": 3, "in_flight": 0,
   "retention_days": 30}
```

`can_export_sensitive: false`, so the catalogue will not even offer billing
email or phone (`views.py:210-213`). The UI disables with a reason instead of
failing at submit, which is the entire point of this endpoint
(`services.py:833-841`).

**2. Estimate, on every change.**

```
POST /v1/exports/preview/?entity=CSS
{"dataset_key": "finance.customer_invoices",
 "columns": ["invoice_number", "customer", "invoice_date", "total", "status"],
 "filters": [{"id": "invoice_date", "start": "2026-07-01", "end": "2026-07-31"},
             {"id": "status", "values": ["SENT", "PART_PAID"]}],
 "format": "xlsx", "values_mode": "people"}

→ {"matching_rows": 214, "rows_bucket": null, "estimated_bytes": 11770,
   "estimate_confidence": "exact", "columns": 6, "row_cap": 200000,
   "warnings": [],
   "sample": {"headers": ["Invoice number", "Customer", "Invoice date", "Total", "Status"],
              "rows": [["INV-000412", "Adeyemi O.", "03 Jul 2026", "₦1,240,000.00", "Sent"], ...]},
   "reads_as": "Customer invoices in CSS where Invoice date is 2026-07-01 to 2026-07-31; Status is any of Sent, Part paid - 6 columns, about 214 rows - as an Excel file. Files stay available for 30 days."}
```

Six columns for five requested: the dataset's locked field was prepended
(`engine.py:117-120`). The sample is rendered exactly as it will appear in the
file - `03 Jul 2026` and `₦1,240,000.00` because `values_mode` is `people`.

**3. Save it.**

```
POST /v1/exports/definitions/?entity=CSS
{"name": "Overdue invoices, monthly", "dataset_key": "finance.customer_invoices",
 "columns": [...], "filters": [...], "format": "xlsx", "values_mode": "people",
 "file_name_pattern": "overdue-{entity}-{date}"}
→ 201, tenant/entity/owner taken from the request, never from the body
```

**4. Share it with the bursar.** `POST .../share/ {"user_ids": [88]}` →
`{"shared_with": [{"id": 88, "email": "bursar@corona.example"}]}`. The bursar can
now see the export and its files, cannot edit it, and their downloads are
authorised against *them*, not against the owner.

**5. Or skip all of it.** From the invoices screen, `POST /v1/exports/quick/`
with `sync: true` and the same body. The server re-runs its own estimate; if the
file will be under 4 MB it produces it inline and answers "Export ready." with a
terminal run, otherwise it queues and answers "Export queued successfully."
(`views.py:665-683`). The drawer's claim that the file will be small is a hint,
not an authorisation.

## 8. Gotchas / known limitations

Full detail in `error/exports/export_code_issues.md`. From this slice:

| # | In one line |
|---|---|
| 1 | No school role holds any `exports.*` key, so every route in this slice is a 403 for every school user out of the box |
| 2 | Archiving an export does not stop it - it can still be run by id and its schedule keeps firing |
| 5 | A filter's *shape* is never validated, so a malformed number range is a 500 on preview |
| 7 | `exports.activity.view` - a read key - also lets its holder edit, archive, re-share and re-schedule anybody's export |
| 13 | The saved-exports list is roughly four queries per row |
| 18.1 | `sharing` is writable and nothing enforces it; the share rows are the truth |
| 18.5 | Share replacement is delete-then-create with no transaction around it |

Limitations rather than defects:

- **`sort` is accepted without validation and silently ignored when withdrawn.**
  The write serializer does not check sort entries, and `build_queryset` skips a
  sort field the dataset no longer has (`engine.py:277-283`) - deliberate ("a
  withdrawn sort is not fatal"), but it means a definition can carry a sort that
  does nothing and nothing says so.
- **`?q=` is an unindexed `icontains`** over name and description
  (`views.py:326-328`). Fine at the scale of one tenant's saved exports.
- **Draft completeness is checked at save, not at schedule time.** A definition
  saved complete, then edited to draft, keeps its schedule; the schedule refuses
  at creation (`views.py:976-981`) but nothing re-checks afterwards - the
  dispatcher's own owner check is the only run-time gate.

## 9. Permissions & tenant isolation

Five keys are in play here (`constants.py:316-335`):
`exports.catalogue.view`, `.definition.view`, `.definition.create`,
`.definition.update`, `.definition.delete`, `.definition.share`, plus
`exports.run.create` for the two trigger routes.

**Visibility is one rule, in one place.** `visible_definitions`
(`views.py:119-127`):

```python
qs = ExportDefinition.objects.filter(tenant=self.tenant).select_related("entity", "owner")
if self.is_admin_reader():
    return qs
return qs.filter(Q(owner=user) | Q(shares__user=user)).distinct()
```

Every lookup by pk goes through it (`get_definition`, `views.py:143`), so
changing an id in the address bar returns **404, not someone else's export**.
There are tests for the cross-tenant case (`tests.py:218`) and the unshared case
(`tests.py:242`).

**Write is narrower than read.** `get_definition(for_write=True)` requires
ownership (`views.py:146-155`) - with the `is_admin_reader()` escape that
`export_code_issues` §7 objects to.

**The entity comes from the request, not the body.** `resolve_scope`
(`views.py:178-196`) returns a tenant-only scope for a tenant-scoped dataset and
otherwise calls `vs_finance.views.resolve_entity`, which requires the entity to
belong to `request.tenant` and returns 404 for unknown *and* forbidden.

**Sensitive columns are gated twice.** The catalogue hides them from a caller
without the key so the picker never offers them (`views.py:210-213`,
`catalogue.py:274-282`); if one is somehow selected anyway, `resolve_columns`
drops it at run time and names it in an omission (`engine.py:127-146`).
Including one that the caller *may* export is itself an audit event
(`audit.py:66-89`).

**A quick export is private by construction.** No definition means
`authorise_download` falls through to `run.requested_by_id != user.pk`
(`services.py:698-699`), and `visible_runs` only matches on `requested_by`
(`views.py:129-141`). Test: `tests.py:669`.

## 10. Code map

| File | What lives there |
|---|---|
| `models.py:66` | `ExportDefinition`; `render_file_name` at :157 |
| `models.py:178` | `ExportDefinitionShare` |
| `views.py:119` | `visible_definitions` - the one visibility rule |
| `views.py:143` | `get_definition`, and the ownership gate for writes |
| `views.py:178` | `resolve_scope` |
| `views.py:248` | `CapabilitiesView` |
| `views.py:263` | `PreviewView` |
| `views.py:314` | `DefinitionListView` |
| `views.py:362` | `DefinitionDetailView` (GET / PATCH / DELETE-as-archive) |
| `views.py:417` | `DefinitionDuplicateView` |
| `views.py:452` | `DefinitionShareView` |
| `views.py:493` | `DefinitionRunView` |
| `views.py:614` | `QuickExportView`, including the inline-run decision |
| `serializers.py:41, 103` | List / detail definition serializers |
| `serializers.py:121` | `ExportDefinitionWriteSerializer` - catalogue-validated |
| `serializers.py:426, 444` | `PreviewSerializer`, `QuickExportSerializer` |
| `engine.py:106` | `resolve_columns` - the shared build-time/run-time check |
| `engine.py:298` | `estimate` |
| `engine.py:359` | `sample_rows` |
| `engine.py:498` | `plain_sentence` |
| `services.py:81` | `freeze` |
| `services.py:128` | `clean_format_options` |
| `services.py:233` | `trigger_quick_run` |
| `services.py:833` | `capabilities` |
| `constants.py:256-304` | The platform limits: retention, row cap, warning threshold, concurrency, idempotency window, sync ceiling, preview rows, exact-count limit |

## 11. Test coverage & gaps

Covered:

- Permission gates: catalogue (`tests.py:183`), run (`tests.py:187`), activity is
  admin-only (`tests.py:193`), sensitive fields hidden without the key
  (`tests.py:198`) and offered with it (`tests.py:206`).
- Isolation: another tenant's definition is 404 (`tests.py:218`), another
  tenant's run is 404 (`tests.py:225`), an unshared definition is invisible
  (`tests.py:242`), a recipient cannot edit (`tests.py:231`).
- Validation: unknown column rejected (`tests.py:319`), missing required filter
  blocks a non-draft (`tests.py:299`) but not a draft (`tests.py:309`), format
  options discriminated by format (`tests.py:328`), owner cannot be set from the
  payload (`tests.py:344`).
- The empty-list response shape (`tests.py:357`) - `success_response` coerces
  `[]` to `{}`, so this one is load-bearing.
- Preview: estimate and sample (`tests.py:264`), a withdrawn filter is a
  validation error not a 500 (`tests.py:288`), only bucketed figures reach
  analytics (`tests.py:1274`).
- Quick export: runs without a definition (`tests.py:647`), is private to its
  requester (`tests.py:669`), needs a column (`tests.py:684`), respects the
  dataset gate (`tests.py:692`), and the from-screen config is accepted by it
  (`tests.py:1743`).

Not covered:

- **The `sync` inline path.** No test asserts that `sync: true` under the size
  ceiling produces a terminal run and a file inline, or that a large estimate
  falls back to the queue. That is the only path in the app that runs an export
  inside a web request.
- **Archived definitions.** Nothing asserts an archived export cannot be run
  (`export_code_issues` §2) - because it can.
- **`?module=`, `?owner=me`, `?q=`, `?include_archived=`** - none of the four
  list filters has a test.
- **`duplicate/`** has no test at all.
- **Share replacement semantics** - that a second call replaces rather than
  appends, that a cross-tenant id is dropped, that the owner cannot share with
  themselves.
- **Malformed filter payloads** (`export_code_issues` §5).
