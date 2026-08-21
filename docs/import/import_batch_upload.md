# import_batch_upload

The front door of `vs_import_data`: what happens between an administrator
choosing a file and a row appearing in the batch list. The `ImportBatch` record
and its fourteen-state lifecycle, the upload serializer's five layers of
refusal, the CSV/Excel parser, how the file is stored, how the batch list and
detail are scoped to a tenant, and the cancel/delete/download surfaces.

Routes covered by this slice, mounted at `/v1/import/` (`apps/urls.py:30`):
`batches/`, `batches/<id>/`, `batches/<id>/download/`, `batches/<id>/cancel/`.

Templates are `import_templates_catalogue`; the validation pass is
`import_validation`; execution and rollback are `import_execution_rollback`;
the Celery layer is `import_tasks_notifications_audit`.

---

## 1. What it is (and what it is NOT)

- **A batch is one file, and the file is parsed at upload.** `create` reads the
  whole file, extracts the headers and every data row, and stores them on the
  record before it is saved (`serializers.py:810-837`). Nothing is deferred: a
  file that cannot be parsed never becomes a batch.
- **The parsed rows are stored on the row, not just the file.** The field is
  called `preview_rows` and commented "First N parsed rows"
  (`models.py:273`), and it holds **all** of them, up to 50,000
  (`serializers.py:835`, `services/file_parser.py:13`). Every later stage reads
  the JSON column rather than re-reading the file.
- **A batch is owned by a tenant, always.** `tenant` is a non-null `PROTECT`
  foreign key (`models.py:214-217`) and `save()` back-fills it from the uploader
  if a caller forgets (`models.py:319-322`).
- **`school` is a property, not a column.** `ImportBatch.school` reads
  `tenant.school_profile` (`models.py:311-317`), which is `None` for the `codex`
  tenant. Every service and view reads it, and two Celery tasks try to
  `select_related` through it, which does not work (§8).
- **`branch` is a column nothing ever sets.** The upload serializer reads it
  from a serializer-context key no view supplies (`serializers.py:825`), so the
  whole branch dimension of this module is inert (§8).
- **Scoping is doubled, and the second copy wins.** Views filter by tenant
  explicitly *and* read through a `TenantAwareManager`
  (`models.py:288`), so the platform-wide path the mixin promises does not
  exist (§8).
- **Cancelling and deleting are different acts.** Cancel is available to the
  uploader before execution starts and only moves the status
  (`views.py:481-552`); delete removes the row and, through a `post_delete`
  signal, the stored file (`signals.py:7-10`).
- **This is not a resumable upload.** There is no chunking, no draft file, and
  no way to replace a file on an existing batch - `ImportBatchUpdateSerializer`
  accepts three metadata fields and nothing else (`serializers.py:853-870`).

## 2. Domain model

### `ImportBatch` (`models.py:167`)

**Identity and ownership**

| Field | Meaning |
|---|---|
| `tenant` | FK, PROTECT, **not null**. Back-filled from `uploaded_by.tenant_id` on save |
| `branch` | FK, CASCADE, nullable - **never populated** (§8) |
| `uploaded_by` | FK, PROTECT |
| `template` | FK, PROTECT, nullable - required in practice by the upload serializer |
| `dataset_type` | Copied from the template at upload (`serializers.py:828`) |

**The file**

| Field | Meaning |
|---|---|
| `file` | `FileField` stored through `DatabaseStorage` at the path `import_file_upload_to` builds |
| `file_format` | Derived from the extension, not from the template |
| `original_filename` | `os.path.basename` of the upload, character-restricted |
| `file_size_bytes` | From the upload, capped at 50 MB |

**Parse results**

| Field | Meaning |
|---|---|
| `uploaded_headers` | The header row exactly as read |
| `template_headers_snapshot` | The template's `column_name` list at upload time, refreshed at validation |
| `preview_rows` | Every parsed data row as a list of dicts |
| `total_rows` | `len(preview_rows)` - data rows, not file lines |
| `total_columns` | `len(uploaded_headers)` |
| `header_row_index` | 1-based; rows before it are skipped |
| `sheet_name` | Excel only; falls back to the active sheet |

**Validation state** (owned by `import_validation`)

`structure_matches_template`, `has_critical_errors`, `is_ready_for_import`,
`validation_summary`, `validation_started_at`, `validation_completed_at`.

**Managers** (`models.py:288-293`)

```python
objects = TenantAwareManager()
all_objects = models.Manager()

class Meta:
    default_manager_name = "objects"
    base_manager_name = "all_objects"
```

`base_manager_name = "all_objects"` is important and correct: it is what related
descriptors and `select_related` use, so a batch reachable from a job is not
silently hidden by the ambient tenant filter.

Indexed on `(tenant, status)`, `(tenant, dataset_type)`, `(branch, status)` -
the third is dead - and `created_at` (`models.py:295-300`).

`clean()` restricts `file_format` to the three known values
(`models.py:306-309`) and is never called by any write path.

`error_count` and `warning_count` are properties, each a `COUNT` query
(`models.py:324-330`), and both appear in the list serializer (§8).

### The storage path (`models.py:142-157`)

```text
imports/<scope>/<dataset_type>/<basename>_<YYYYMMDD_HHMM><ext>
```

`scope` is the tenant slug when `tenant_id` is set, `branch_<slug>` when only
`branch_id` is, and `internal` otherwise. Since `tenant` is non-null the first
branch always wins and the other two are unreachable.

Storage is `core.storage.DatabaseStorage`
(`apps/settings/base.py:376-377`): files live in a `StoredFile` table, not on
disk. That backend implements `_open`, `_save`, `exists`, `delete`, `size`,
`url` and `get_available_name` - and **not** `path`, which is why the download
route is a 500 (§8).

## 3. Endpoint map

| Method + path | Permission | Notes |
|---|---|---|
| `GET /batches/` | `import.batches.view` | `?status=`, `?template_id=`; paginated |
| `POST /batches/` | `import.batches.create` | Multipart upload; returns the list shape |
| `GET /batches/<id>/` | `import.batches.view` | Full detail including every parsed row (§8) |
| `PATCH /batches/<id>/` | `import.batches.update` | `sheet_name`, `header_row_index`, `notes` only |
| `DELETE /batches/<id>/` | `import.batches.delete` | Refused for a published bank-statement batch |
| `POST /batches/<id>/cancel/` | any of `create` / `update` / `delete` | Plus an ownership check |
| `GET /batches/<id>/download/` | `import.batches.view` | **500 in every environment** (§8) |

Everything except the list/create pair uses `HasImportBatchRBACPermission`
(`permissions.py:8-38`), which widens the gate for one specific case:

```python
def has_permission(self, request, view):
    if super().has_permission(request, view):
        return True
    # ... else: the caller may hold finance.bankaccount.import instead,
    # but only for a bank-statement batch whose typed finance context
    # resolves to the request's asserted tenant
    return ImportBatch.all_objects.filter(
        pk=batch_id, tenant=tenant,
        dataset_type=DatasetTypeChoices.BANK_STATEMENTS,
        bank_statement_context__bank_account__entity__tenant=tenant,
    ).exists()
```

That fallback is object-aware on purpose: a finance user finishing a bank
statement wizard should not need broad `import.*` access, and the extra clauses
mean the widened key cannot reach any other dataset type or any other tenant's
row. It reads through `all_objects` with explicit filters, which is the correct
way to do this.

### Request bodies actually read

`POST /batches/` (`serializers.py:707-727`) is multipart and reads five fields:

```text
template_id       int, required
file              the upload, required
sheet_name        optional, Excel only
header_row_index  optional, default 1, must be > 0
notes             optional free text
```

Nothing else. `tenant`, `uploaded_by`, `dataset_type`, `file_format`,
`original_filename`, `file_size_bytes`, `uploaded_headers`,
`template_headers_snapshot`, `preview_rows`, `total_rows` and `total_columns`
are all derived server-side (`serializers.py:824-837`), so none of them can be
asserted by the caller.

`PATCH /batches/<id>/` reads `sheet_name`, `header_row_index`, `notes`
(`serializers.py:859-865`). The template cannot be changed after upload, which
is correct - the parse results were produced against it.

### Serializer field sets

| Serializer | Fields |
|---|---|
| `ImportBatchListSerializer` (`serializers.py:556`) | `id`, `template`, `template_name`, `template_code`, `original_filename`, `file_format`, `status`, `file_size_bytes`, `total_rows`, `total_columns`, `structure_matches_template`, `has_critical_errors`, `is_ready_for_import`, `error_count`, `warning_count`, `imported_at`, `created_at`, `updated_at` |
| `ImportBatchDetailSerializer` (`serializers.py:590`) | the above plus `school`, `branch`, `uploaded_by`, `domain_context`, `file`, `dataset_type`, `header_row_index`, `sheet_name`, `uploaded_headers`, `template_headers_snapshot`, **`preview_rows`**, `validation_summary`, `validation_started_at`, `validation_completed_at`, `notes`, **`validation_issues`** (nested, unpaginated), **`notifications`** (nested, unpaginated) |

`domain_context` (`serializers.py:682-704`) is populated only for
`bank_statements` and returns the linked bank account, entity, statement period
and opening/closing balances in both kobo and naira.

The detail serializer declares field-level security:

```python
# serializers.py:598-601
read_permissions = {
    "file": ImportPermission.BATCH_VIEW,
    "preview_rows": ImportPermission.BATCH_VIEW,
}
```

`BATCH_VIEW` is the key the endpoint itself requires (`views.py:406`), so the
rule masks nothing from anyone who can reach the response (§8).

## 4. Lifecycle / state machine

`ImportBatchStatusChoices` declares fourteen states (`models.py:55-69`). Six of
them are reachable:

```text
                    POST /batches/
                          |
                          v
                     [ uploaded ]  <- the default; DRAFT, DETECTING and
                          |            MAPPING_REQUIRED are never assigned
              POST .../validate/
                          |
              +-----------+-----------+
              |                       |
              v                       v
    [ validation_failed ]     [ ready_to_import ]
              |                       |
      (fix + re-validate)     POST .../start-import/
                                      |
                                      v
                             [ import_running ]
                                      |
                    +-----------------+-----------------+
                    |                 |                 |
                    v                 v                 v
          [ import_succeeded ] [ import_partial ] [ import_failed ]
                    |                 |                 |
                    +--------- POST .../rollback/ ------+
                                      |
                                      v
                               [ rolled_back ]

    POST .../cancel/  from any of: draft, uploaded, detecting,
                                   mapping_required, validating,
                                   validation_failed, ready_to_import
                                      |
                                      v
                                [ cancelled ]
```

`VALIDATING` is written inside the validation transaction and committed with the
final state, so it is never externally observable
(`services/validation_service.py:754-767`). `IMPORT_QUEUED` is declared and
never assigned - the async path goes straight from `ready_to_import` to
`import_running` when the worker calls `start_import_job`
(`services/import_executor.py:500-501`).

**Cancellation** (`views.py:481-552`) has two gates:

1. **Ownership.** The uploader may always cancel their own batch. Anyone else
   needs `import.batches.update` or `import.batches.delete`
   (`views.py:506-522`) - "a creator may abandon their own work; managing
   somebody else's batch remains a sensitive operation".
2. **State.** Only the seven pre-execution states are cancellable
   (`views.py:491-499`); anything from `import_queued` onward is refused with
   "This import can no longer be cancelled because execution has already started
   or finished."

Cancelling clears `is_ready_for_import` as well as setting the status
(`views.py:533-535`), so the batch cannot be started afterwards.

**Deletion** (`views.py:452-475`) refuses exactly one case: a bank-statement
batch whose context carries a `published_statement_id`, with the message "Use
rollback before any statement line is acted upon." Every other batch, in any
state - including one whose import created 400 schools - can be deleted, taking
its jobs, row results and validation issues with it by CASCADE. Only the file is
handled deliberately, by the `post_delete` signal
(`signals.py:7-10`).

## 5. Derivations

- **Five layers of refusal at upload**, in the order they run:

  | Check | Where | Refusal |
  |---|---|---|
  | Size | `serializers.py:739-740` | "File exceeds the 50 MB limit." |
  | Extension | `serializers.py:742-747` | "Only .csv, .xlsx, and .xls files are allowed." |
  | Template exists and is download-enabled | `serializers.py:751-761` | "Selected template does not exist." / "This template is not available for use." |
  | Extension against the template's accepted formats | `serializers.py:763-778` | "This template accepts CSV files. You uploaded a XLSX file." |
  | Filename characters | `serializers.py:804-806` | "Filename contains invalid characters. Use only letters, numbers, spaces, hyphens, underscores, and dots." |

  The fourth reads `template.validation_rules["allowed_file_formats"]` and falls
  back to `[template.default_file_format]`.

  The fifth is a real guard: `os.path.basename` first, then
  `re.fullmatch(r"[A-Za-z0-9_.\- ]+", safe_name)`, so a traversal attempt or a
  control character in the name is refused rather than sanitised - and
  `original_filename` is what the download route sets as the
  `Content-Disposition` filename.

- **Format detection is by extension, not by content**
  (`serializers.py:786-794`):

  ```text
  .csv   -> CSV
  .xlsx  -> XLSX
  anything else that got this far -> XLS
  ```

  So a `.xls` file reaches `parse_xlsx`, which uses openpyxl and cannot read the
  legacy format (§8).

- **CSV decoding tries four encodings in order**
  (`services/file_parser.py:25-34`): `utf-8-sig`, `utf-8`, `cp1252`,
  `latin-1`, then raises "File encoding could not be detected. Please save as
  UTF-8 and re-upload." `latin-1` decodes any byte sequence, so in practice the
  final failure is unreachable and a genuinely broken file becomes mojibake
  rather than an error.

- **Row extraction** (`services/file_parser.py:37-108`), the same shape for
  both formats:

  ```text
  row_number < header_row_index   -> skipped
  row_number == header_row_index  -> headers, stripped
  a row with no non-blank cell    -> skipped, and does not consume a row number
  otherwise                       -> dict(zip(headers, stripped values))
  len(rows) > 50_000              -> ValueError, the whole upload is refused
  ```

  The row limit refuses rather than truncates, deliberately: "rows must never be
  silently omitted because the executor publishes exactly the parsed rows"
  (`services/file_parser.py:4-6`).

  `dict(zip(...))` is the quiet part: a row with more cells than headers loses
  the surplus, and a row with fewer simply lacks those keys (§8).

- **Excel sheet selection** (`services/file_parser.py:81-84`): the named sheet
  if it exists, otherwise `wb.active`. A `sheet_name` that does not match any
  sheet falls back silently rather than raising, so a typo produces the wrong
  sheet's data with no message.

- **Tenant scoping** (`views.py:98-137`):

  ```text
  school-tenant caller  ->  scope_tenant() = request.tenant, filtered explicitly
  platform caller       ->  scope_tenant() = None, no explicit filter
  ```

  and then `ImportBatch.objects` applies `tenant = <ambient tenant>` underneath
  both (§8).

- **`get_serializer_context` caches the batch** (`views.py:139-142`,
  `:426-428`) so a detail request does not fetch it twice - `get_object` stores
  `_cached_import_batch` and the context reads it.

## 6. What writing writes

| Action | Written by | Rows |
|---|---|---|
| Upload | `ImportBatchUploadSerializer.create` (`serializers.py:780-850`) | one `ImportBatch` + one `StoredFile`, then one `AuditEvent` (`views.py:376-386`) |
| Metadata edit | `ImportBatchDetailView.perform_update` (`views.py:435-450`) | the batch + one `AuditEvent` carrying a before/after diff |
| Cancel | `CancelImportBatchView.post` (`views.py:532-548`) | `status` + `is_ready_for_import` + one `AuditEvent` |
| Delete | `perform_destroy` (`views.py:452-475`) | one `AuditEvent`, then the batch (CASCADE takes jobs, row results, issues, notifications) and the file via signal |

Every one of those audit events is written **after** the change and outside any
transaction wrapping it, so a failure between the two leaves the change without
its record. The upload path is the widest gap: `serializer.save()` commits, then
`create_import_audit_log` runs (`views.py:373-386`).

Audit rows are `module_key = IMPORT` with the action taken from `_ACTION_MAP`
(`services/audit_service.py:8-25`), and `tenant` derived from the batch's branch
or school (`services/audit_service.py:77-81`). Since `branch` is always `None`
and `school` is `None` for the `codex` tenant, a CX-run schools or cx_users
import writes audit rows with `tenant = NULL`.

Reading writes nothing: no access log, no last-viewed stamp, no download record.

## 7. Worked example

Corona Secondary School's finance officer uploads a bank statement.

```text
POST /v1/import/batches/?tenant=corona
Content-Type: multipart/form-data

template_id: 4
file: August 2026 Statement.xlsx   (612 KB, 1,340 data rows)
header_row_index: 1
```

The serializer checks size (fine), extension (`.xlsx`, allowed), the template
(id 4, `bank_statements_v1`, download-enabled), the extension against
`allowed_file_formats` (the template's default is XLSX), and the filename
against the character pattern - spaces are permitted, so
`August 2026 Statement.xlsx` passes.

`parse_import_file` opens the workbook read-only, takes row 1 as headers and
reads 1,340 rows. The batch is created:

```jsonc
{"success": true, "message": "Import batch uploaded successfully.",
 "data": {"id": 12, "template": 4,
          "template_name": "Bank Statement Import",
          "template_code": "bank_statements_v1",
          "original_filename": "August 2026 Statement.xlsx",
          "file_format": "xlsx", "status": "uploaded",
          "file_size_bytes": 626688,
          "total_rows": 1340, "total_columns": 7,
          "structure_matches_template": false,
          "has_critical_errors": false, "is_ready_for_import": false,
          "error_count": 0, "warning_count": 0,
          "imported_at": null,
          "created_at": "2026-08-21T09:14:02Z", "updated_at": "..."}}
```

`structure_matches_template` is `false` and `error_count` is `0` because
validation has not run yet - the two fields are both defaults, and nothing in
the shape says which. The file is now in the `StoredFile` table, and its 1,340
rows are also in `preview_rows` on this record.

The officer opens the batch to check what was read:

```text
GET /v1/import/batches/12/?tenant=corona
```

and the response carries all 1,340 rows under `preview_rows`, plus `file`,
plus `domain_context` with the bank account and the period's opening and closing
balances. On a 50,000-row statement that same response would be hundreds of
megabytes (§8).

Then they try to retrieve the file they just sent:

```text
GET /v1/import/batches/12/download/?tenant=corona
->  500   NotImplementedError: This backend doesn't support absolute paths.
```

They change their mind about the whole thing:

```text
POST /v1/import/batches/12/cancel/?tenant=corona
```

```jsonc
{"success": true, "message": "Import cancelled.",
 "data": {"batch_id": "12", "status": "cancelled"}}
```

They uploaded it, so the ownership check passes without needing
`import.batches.update`.

## 8. Gotchas / known limitations

Full evidence for each is in **`error/import/import_code_issues.md`**. The items
belonging to this slice:

- **`GET /batches/<id>/download/` is a 500 in every environment.**
  `batch.file.path` (`views.py:581`) against `DatabaseStorage`, which does not
  implement `path` - while the view's own docstring says it reads from
  `MEDIA_ROOT` (`views.py:561-564`). Confirmed by execution (issues §2).
- **"Platform users see all" is false.** `scope_tenant()` returns `None` for a
  CX caller (`views.py:107-111`) and `ImportBatch.objects` re-applies the
  ambient tenant filter underneath. A super admin listing batches while a school
  batch exists gets zero rows. Confirmed by execution (issues §3).
- **The whole file body is stored on the row and returned by the detail
  endpoint**, together with every validation issue and notification, none of
  them paginated (`models.py:273`, `serializers.py:609-610`, `:636`;
  issues §10).
- **The field-level security on `file` and `preview_rows` protects nothing** -
  both are gated on `BATCH_VIEW`, the key the endpoint already requires
  (`serializers.py:598-601`; issues §10).
- **`branch` is never set on any batch.** `self.context.get("branch")`
  (`serializers.py:825`) reads a key no view supplies, stranding the FK, the
  `(branch, status)` index, the branch storage path, eight audit calls and the
  branch narrowing in cross-reference validation (issues §18).
- **`.xls` is accepted at upload and cannot be parsed** - openpyxl does not
  read the legacy format, and the failure surfaces as "Could not read file"
  (`serializers.py:745`, `services/file_parser.py:125-126`; issues §17).
- **A retired or draft template is still accepted.** `validate_template_id`
  checks `is_download_enabled` and never `status`
  (`serializers.py:751-761`; issues §15).
- **`error_count` and `warning_count` are two `COUNT` queries per row**
  (`models.py:324-330`) and both are in the list serializer, so a 25-row page
  issues 50 extra queries (issues §23.4).
- **The parser is documented as streaming and is not.** Both parsers call
  `file_obj.read()` and accumulate a full list of dicts
  (`services/file_parser.py:1-6`, `:44`, `:78`; issues §23.7).
- **`dict(zip(headers, values))` silently drops surplus cells**
  (`services/file_parser.py:61`, `:101`; issues §23.8).
- **A `sheet_name` that matches nothing falls back to the active sheet**
  without a warning (`services/file_parser.py:81-84`), so a typo silently
  imports a different sheet.
- **`latin-1` is the last encoding tried** (`services/file_parser.py:27`) and
  decodes any byte sequence, so the "encoding could not be detected" error is
  unreachable and a mis-encoded file becomes mojibake in the data.
- **`ImportBatch.clean()` is never called**, so the `file_format` guard
  (`models.py:306-309`) is dead - harmless today because the value is derived
  server-side.
- **Deleting a batch destroys its execution history.** Jobs, row results,
  validation issues and notifications all CASCADE
  (`models.py:703`, `:799`, `:610`, `:918`), so the only record that 400
  schools were created by an import disappears with the batch. Only the
  published-bank-statement case is refused (`views.py:452-463`).
- **Justified by design:** the row limit refuses rather than truncates
  (`services/file_parser.py:18-22`). A silently shortened import is worse than a
  rejected one.
- **Justified by design:** the filename is `basename`d and then character-
  restricted (`serializers.py:804-806`) rather than sanitised, and the same
  string is what `Content-Disposition` carries.
- **Justified by design:** the template cannot be changed after upload
  (`serializers.py:853-857`). The parse results belong to it.
- **Justified by design:** `HasImportBatchRBACPermission`'s finance fallback is
  object-aware and reads through `all_objects` with explicit tenant filters
  (`permissions.py:29-38`). A finance user finishing a statement wizard should
  not need broad import access.
- **Justified by design:** the cancel gate lets a creator abandon their own work
  without `update` or `delete` (`views.py:504-522`).

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Restricted | Scope |
|---|---|---|---|---|
| List, retrieve, download | `import.batches.view` | NORMAL | no | TENANT |
| Upload | `import.batches.create` | NORMAL | no | TENANT |
| Metadata edit | `import.batches.update` | NORMAL | no | TENANT |
| Delete | `import.batches.delete` | SENSITIVE | yes | TENANT |
| Cancel | any of create / update / delete | - | - | TENANT |

Seeded by `core/management/commands/seed_import_permissions.py:37-48` with
`scope = PermissionScope.TENANT` (`:145`), granted to `xvs_super_admin` only -
`xvs_platform_admin` receives the template keys and nothing else
(`:161-166`), and **no school role is granted any of them** out of the box
(issues §22).

**Isolation holds for school callers, and it holds twice.** A school user's
requests are filtered explicitly by `scope_tenant()` and again by the ambient
`TenantAwareManager`. `tests.py:124-141` pins the important case: a user in
another tenant posting to `/batches/<id>/cancel/` with a valid id gets a **404**,
not a 403, so the endpoint is not an id oracle.

Three things worth knowing:

1. **The doubled filter is not redundant in effect - it changes the platform
   path.** The mixin's platform branch is defeated by the manager, so cross-
   tenant support access does not work (issues §3). No view in this app sets
   `platform_cross_tenant_param`, so a CX caller cannot assert a school's slug
   either.
2. **The dataset type is not part of the permission model.** `import.batches.create`
   lets its holder upload against any active template, including the three that
   create schools, branches and CodeX staff. Combined with
   `import.batches.import`, that is a tenant-scoped key reaching platform-level
   operations (issues §8).
3. **`ImportBatch.school` is derived, so it cannot be filtered on.** Anything
   wanting school scoping has to go through `tenant`, and two Celery tasks that
   tried to go through `school` instead simply fail
   (`import_tasks_notifications_audit` §8).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:42-69` | `FileFormatChoices`, `DatasetTypeChoices`, `ImportBatchStatusChoices` |
| `models.py:142-157` | `import_file_upload_to` - the scope-derived storage path |
| `models.py:167-330` | `ImportBatch`, its managers, `save()` back-fill, the `school` property, the two count properties |
| `signals.py:7-10` | `post_delete` - removes the stored file with the batch |
| `services/file_parser.py:13-22` | `MAX_IMPORT_ROWS` and the refusal |
| `services/file_parser.py:25-34` | `_open_csv_text` - the four-encoding ladder |
| `services/file_parser.py:37-66` | `parse_csv` |
| `services/file_parser.py:69-108` | `parse_xlsx` |
| `services/file_parser.py:111-128` | `parse_import_file` - format dispatch |
| `serializers.py:556-588` | `ImportBatchListSerializer` |
| `serializers.py:590-704` | `ImportBatchDetailSerializer`, `domain_context` |
| `serializers.py:707-850` | `ImportBatchUploadSerializer` - the five refusals and the derived fields |
| `serializers.py:853-870` | `ImportBatchUpdateSerializer` |
| `permissions.py:8-38` | `HasImportBatchRBACPermission` - the finance fallback |
| `views.py:68-70` | `_is_platform` |
| `views.py:98-142` | `SchoolContextMixin`, `ImportBatchContextMixin` |
| `views.py:324-386` | `ImportBatchListCreateView` |
| `views.py:389-475` | `ImportBatchDetailView` |
| `views.py:481-552` | `CancelImportBatchView` |
| `views.py:558-594` | `ImportBatchFileDownloadView` |
| `apps/settings/base.py:374-377` | `MEDIA_ROOT` (unused) and `DatabaseStorage` |

## 11. Test coverage & gaps

Baseline: **`Ran 18 tests in 7.596s` - OK**.

What this slice covers:

- `ImportBatchCancellationTests` (`tests.py:69-141`) - three tests, and they are
  the best in the module:
  - the uploader can cancel a `ready_to_import` batch, and
    `is_ready_for_import` is cleared as well as the status;
  - a batch already at `import_running` is refused with a 400 and does not
    change state;
  - **a user in another tenant posting to the same batch id gets a 404**, which
    is the cross-tenant isolation test the rest of the module lacks.

What it does not cover:

1. **Upload.** `ImportBatchUploadSerializer` has no test at all: not the size
   limit, not the extension check, not the `allowed_file_formats` cross-check,
   not the filename pattern, and not the derived-field block that sets tenant,
   dataset type and parse results. Every batch in the suite is built with
   `ImportBatch.objects.create(...)` and hand-written `preview_rows`.
2. **The parser.** `parse_csv`, `parse_xlsx` and `parse_import_file` are
   untested - not the header-row offset, not the blank-row skip, not the
   encoding ladder, not the 50,000-row refusal, and not the `.xls` dead end
   (issues §17).
3. **Every endpoint except cancel.** List, create, retrieve, patch, delete and
   download have no test. The download route's 500 (issues §2) would have been
   caught by a single request assertion.
4. **The platform-scoping branch.** Nothing exercises `_is_platform` on a batch
   list, which is why issues §3 - the platform path returning nothing - has
   never surfaced.
5. **The finance permission fallback.** `HasImportBatchRBACPermission`'s
   `finance.bankaccount.import` path is untested, including the negative cases
   that make it safe: a non-bank-statement batch, and a bank account belonging
   to another tenant.
6. **Deletion.** Neither the published-bank-statement refusal nor the cascade is
   asserted, and nothing checks that the stored file is removed with the row.
7. **`preview_rows` size.** No test uploads anything larger than two rows, so
   nothing exercises the payload problem in issues §10.
