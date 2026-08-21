# import_templates_catalogue

The contract half of `vs_import_data`: the system-managed `ImportTemplate` that
declares exactly which columns a dataset accepts, the per-column specification
that drives validation and field mapping, the CRUD surface platform staff use to
author them, and the CSV/XLSX generator that turns a template into the file an
administrator downloads and fills in.

Routes covered by this slice, mounted at `/v1/import/` (`apps/urls.py:30`):
`system-import-templates/`, `system-import-templates/<id>/`,
`system-import-templates/<id>/download/`.

Upload and parsing are `import_batch_upload`; the validation pass is
`import_validation`; execution and rollback are `import_execution_rollback`;
the Celery layer is `import_tasks_notifications_audit`.

---

## 1. What it is (and what it is NOT)

- **A template is the only way in.** There is no free-form column mapping in
  this module. `ImportBatchUploadSerializer` requires `template_id`
  (`serializers.py:717`), the executor refuses a batch without one
  (`services/import_executor.py:38-39`), and the validator makes the same
  refusal an ERROR (`services/validation_service.py:62-69`). The manual-mapping
  validators left in `validators.py` are from an earlier design and are called
  by nothing.
- **Templates are platform property.** Only a caller on the `codex` tenant may
  create one (`views.py:210-211`) or edit one (`views.py:252-253`), and the
  check is in `check_permissions`, on top of the RBAC key.
- **A column is a contract clause, not a hint.** `column_name` is the exact
  header expected in the file; `target_field` is the key the executor's dataset
  handler reads. `map_row_to_payload` builds the payload purely from
  `target_field` (`services/import_executor.py:45-51`), so a handler and a
  template that disagree produce a silently empty field, not an error.
- **`code` is the stable identifier and `id` is what the API uses.** `code` is
  unique (`models.py:373-377`) and shaped for versioning (`schools_master_v1`),
  but every route in this slice is keyed on the integer `id`
  (`urls.py:16`, `:21`).
- **Status is a lifecycle nothing enforces.** `draft → active → retired`
  (`models.py:122-125`) with `published_at` / `retired_at` stamped on
  transition (`serializers.py:262-263`). The list and detail endpoints hide
  non-active templates from school users, and **the upload path does not check
  status at all** (§8).
- **Only one template per dataset type should be active** - the model docstring
  says so (`models.py:339-341`) - and no constraint enforces it.
- **The generated file is not stored.** `template_file` is a `FileField` on the
  model (`models.py:159-161` defines its upload path) and nothing writes it;
  every download is generated on the fly from the column rows
  (`views.py:306-318`).
- **This is not a per-tenant template library.** `ImportTemplate` has no tenant
  column. There is one platform-wide catalogue and every school sees the same
  active templates.

## 2. Domain model

### `ImportTemplate` (`models.py:333`)

| Field | Meaning |
|---|---|
| `code` | Stable unique identifier, e.g. `schools_master_v1` |
| `name` | Display label |
| `dataset_type` | `schools` / `branches` / `cx_users` / `bank_statements` (`models.py:48-52`) |
| `description`, `instructions` | Admin-facing prose; `instructions` is written into the generated file's Instructions sheet |
| `status` | `draft` / `active` / `retired`, default **ACTIVE** |
| `default_file_format` | Preferred download format and the default accepted upload format |
| `template_file` | Declared and never written |
| `allow_sample_row` | Whether the generated file carries an example row |
| `sample_row_data` | JSON object keyed by `column_name`, overriding per-column samples |
| `validation_rules` | Dataset-wide JSON config; three keys are read (see §5) |
| `is_download_enabled` | The only availability flag the upload path checks |
| `created_by` | PROTECT; set from `request.user` on create |
| `published_at`, `retired_at` | Stamped on the matching status transition |

`Meta.ordering = ["dataset_type", "name"]`, indexed on `(dataset_type, status)`
and on `code` (`models.py:427-432`).

`clean()` refuses an ACTIVE template with no columns
(`models.py:437-439`) and is never called by the API (§8).

### `ImportTemplateColumn` (`models.py:442`)

| Field | Meaning |
|---|---|
| `template` | FK, CASCADE |
| `column_name` | Exact spreadsheet header. **This is the matching key** |
| `target_field` | Internal key the dataset handler reads |
| `display_name`, `help_text` | Documentation, written into the Instructions sheet |
| `data_type` | `string` / `integer` / `decimal` / `date` / `datetime` / `email` / `boolean` / `choice` (`models.py:128-136`) |
| `is_required` | Read per **value** during row validation; **not** read during header comparison (§8) |
| `is_unique` | Duplicate values across rows in the same file are errors |
| `max_length` | Applied to any type, not just strings (`services/template_validation.py:122-125`) |
| `allowed_values` | JSON list of strings; used when `data_type = choice` |
| `sample_value` | Injected into the generated file |
| `default_value` | Substituted for a blank cell **at execution, not at validation** (`services/import_executor.py:48-49`) |
| `column_order` | Sort order in the generated file and in every column fetch |
| `reference_model`, `reference_lookup_field` | Free-text cross-reference metadata (see `import_validation` §5) |

`unique_together` on `(template, column_name)` and `(template, target_field)`
(`models.py:549-552`), which is what stops a template mapping two headers to one
field.

`clean()` requires `allowed_values` to be a list of strings
(`models.py:558-563`) and is bypassed by both write paths, which use
`bulk_create` (§8).

### The four seeded templates

Written by `core/management/commands/seed_import.py` (67 column definitions in
total) and by migration `0005_seed_bank_statement_template`:

| Code | Dataset | Format | What one row creates |
|---|---|---|---|
| `schools_master_v1` (`seed_import.py:72`) | `schools` | CSV | A `School`, its `Tenant`, its main `Branch`, both admins, and optional package setup |
| `branches_master_v1` (`:520`) | `branches` | CSV | One `Branch` on an existing school, plus its branch admin |
| `cx_users_master_v1` (`:756`) | `cx_users` | CSV | One CodeX staff account on the `codex` tenant, submitted for approval |
| `bank_statements_v1` (`:873`, and migration `0005`) | `bank_statements` | XLSX | One statement line in the finance reconciliation workbench |

The bank-statement template's seven columns - Transaction Date, Description,
Reference, Money In, Money Out, Transaction ID, Balance
(`migrations/0005_seed_bank_statement_template.py:46-112`) - are seeded by
migration rather than by the command, so they exist on every deployed database
whether or not `seed_import` was ever run.

## 3. Endpoint map

| Method + path | Permission | Extra gate | Notes |
|---|---|---|---|
| `GET /system-import-templates/` | `import.templates.view` | none | Non-platform callers see ACTIVE + download-enabled only; `?dataset_type=` filters |
| `POST /system-import-templates/` | `import.templates.create` | platform tenant | Creates the template and its columns in one call |
| `GET /system-import-templates/<id>/` | `import.templates.view` | none | Same visibility filter as the list |
| `PATCH /system-import-templates/<id>/` | `import.templates.manage` | platform tenant | Columns, when supplied, **replace** all existing ones |
| `GET /system-import-templates/<id>/download/` | `import.templates.view` | none | `?file_format=csv` or `xlsx`; defaults to the template's own |

The RBAC key is chosen per method inside `get_permissions`
(`views.py:180-186`, `:242-248`), and the platform-tenant check is a separate
`check_permissions` override so a non-platform caller holding the key is still
refused with a specific message
(`views.py:208-211`, `:250-253`).

None of these views sets `tenant_param_required = False`, so `?tenant=<slug>` is
mandatory on all of them (`vs_rbac/authentication.py:132`).

### Request bodies actually read

`POST /system-import-templates/` (`serializers.py:195-226`):

```jsonc
{"code": "students_master_v1",
 "name": "Students Master Import",
 "dataset_type": "schools",
 "description": "...", "status": "active",
 "default_file_format": "csv",
 "instructions": "Fill one school per row...",
 "allow_sample_row": true,
 "sample_row_data": {"School Name": "Greenfield Academy"},
 "validation_rules": {"min_rows": 1, "max_rows": 500,
                      "allowed_file_formats": ["csv", "xlsx"]},
 "is_download_enabled": true,
 "columns": [
   {"column_name": "School Name", "target_field": "name",
    "data_type": "string", "is_required": true, "is_unique": false,
    "max_length": 255, "allowed_values": [], "sample_value": "Greenfield Academy",
    "default_value": "", "column_order": 1,
    "reference_model": "", "reference_lookup_field": "",
    "display_name": "", "help_text": "The school's registered name."}
 ]}
```

`created_by` is set from `request.user` (`serializers.py:216`) and cannot be
supplied.

`PATCH /system-import-templates/<id>/` reads the same fields **minus** `code`
and `dataset_type`, which are deliberately excluded - "change them via
migration" (`serializers.py:232-234`).

### Serializer field sets

| Serializer | Fields |
|---|---|
| `ImportTemplateListSerializer` (`serializers.py:111`) | template metadata plus a column count |
| `ImportTemplateDetailSerializer` (`serializers.py:136`) | `id`, `code`, `name`, `dataset_type`, `description`, `status`, `default_file_format`, `instructions`, `allow_sample_row`, `sample_row_data`, `validation_rules`, `is_download_enabled`, `published_at`, `retired_at`, `columns`, `created_at`, `updated_at` - all read-only |
| `ImportTemplateColumnDetailSerializer` (`serializers.py:79`) | the full column specification |
| `ImportTemplateColumnWriteSerializer` (`serializers.py:174`) | the fourteen writable column fields |

`ImportTemplateDetailSerializer` carries a real field-level security rule:

```python
# serializers.py:139-141
read_permissions = {
    "validation_rules": ImportPermission.TEMPLATE_MANAGE,
}
```

so the dataset-wide rules block is masked from anyone holding only
`import.templates.view`. This is the one FLS entry in the module that protects
against a key the endpoint does not already require (contrast
`import_batch_upload` §8).

## 4. Lifecycle / state machine

```text
POST /system-import-templates/            -> status as supplied (default ACTIVE)
PATCH {"status": "active"}                -> published_at stamped if still null
PATCH {"status": "retired"}               -> retired_at stamped if still null
PATCH {"columns": [...]}                  -> ALL existing columns deleted, new set created
```

There is no delete route. A template is retired, never removed - correct, since
`ImportBatch.template` is `PROTECT` (`models.py:233-236`) and batches keep
pointing at the template they were validated against.

What the lifecycle does **not** do:

- A RETIRED template is still accepted by the upload path
  (`import_code_issues.md` §15).
- The status transitions are not one-way: a retired template can be set back to
  ACTIVE, and `retired_at` keeps its old value because the stamp only fires when
  the field is null.
- Nothing checks that the new column set is compatible with batches still in
  flight against the old one (§8).

## 5. Derivations

- **Visibility** (`views.py:196-200`, `:262-264`, `:299-300`). A non-platform
  caller sees `status = ACTIVE AND is_download_enabled = True`; a platform
  caller sees everything. The same filter is applied in three places rather than
  in one helper.

- **Generated CSV** (`services/template_file.py:83-97`). Header row from
  `column_name` in `column_order`, then one sample row when
  `allow_sample_row` is set.

- **Generated XLSX** (`services/template_file.py:24-80`). Two sheets:

  ```text
  Sheet 1  <dataset_type>[:31]   headers (bold, filled #D9EAF7) + optional sample row
  Sheet 2  Instructions          Template Name / Dataset Type / Description / Instructions
                                 then a table: Column Name, Target Field, Required,
                                 Data Type, Allowed Values, Help Text, Sample Value
  ```

  The 31-character truncation is Excel's sheet-name limit.

- **The sample value, in precedence order** (`services/template_file.py:11-21`):

  ```text
  template.sample_row_data[column_name]   ->  column.sample_value  ->  column.default_value  ->  ""
  ```

  The `in` test rather than a truthiness test is deliberate: it preserves an
  intentional blank or zero in `sample_row_data`.

- **The three `validation_rules` keys anything reads**:

  | Key | Read by | Effect |
  |---|---|---|
  | `allowed_file_formats` | `ImportBatchUploadSerializer.validate` (`serializers.py:769`) | Extensions accepted at upload; defaults to `[default_file_format]` |
  | `min_rows` | `_validate_template_rules` (`services/validation_service.py:163-177`) | Error below the floor |
  | `max_rows` | same (`:179-193`) | Error above the ceiling |

  Every other key in that JSON blob is inert.

- **`get_active_template_by_dataset`** (`services/template.py:6-16`) fetches the
  one ACTIVE, download-enabled template for a dataset type with `.get()`. It is
  the only place `status` is used as a lookup, and it will raise
  `MultipleObjectsReturned` if two are active (`import_code_issues.md` §23.2).

- **`get_template_headers`** (`services/template.py:17-22`) returns
  `column_name` in `column_order`, and is what fills
  `ImportBatch.template_headers_snapshot` at validation time.

## 6. What writing writes

| Action | Written by | Rows |
|---|---|---|
| Create a template | `ImportTemplateCreateSerializer.create` (`serializers.py:222-228`) | one `ImportTemplate` + N `ImportTemplateColumn` via `bulk_create` |
| Update metadata | `ImportTemplateUpdateSerializer.update` (`serializers.py:255-268`) | the `ImportTemplate`, plus `published_at`/`retired_at` on transition |
| Replace columns | same (`serializers.py:270-275`) | **delete all** existing columns, then `bulk_create` the new set |

Both write paths also emit a `vs_audit` event through `create_import_audit_log`
(`views.py:217-224`, `:273-281`) with `module_key = IMPORT` and action types
`CREATE` / `UPDATE` (`services/audit_service.py:8-12`). Neither passes a
`school` or `branch`, so `tenant` resolves to `None`
(`services/audit_service.py:77-81`) - correct here, because a template belongs
to no tenant.

Downloading writes nothing: no audit event, no counter, no record that a
template was fetched.

## 7. Worked example

A CX engineer publishes a template for the schools dataset:

```text
POST /v1/import/system-import-templates/?tenant=codex
{"code": "schools_master_v2", "name": "Schools Master Import",
 "dataset_type": "schools", "status": "active", "default_file_format": "csv",
 "instructions": "Fill one school per row. School Slug must be lowercase...",
 "validation_rules": {"min_rows": 1, "max_rows": 500},
 "columns": [
   {"column_name": "School Name", "target_field": "name", "data_type": "string",
    "is_required": true, "sample_value": "Greenfield Academy", "column_order": 1},
   {"column_name": "School Slug", "target_field": "slug", "data_type": "string",
    "is_unique": true, "sample_value": "greenfield-academy", "column_order": 2},
   {"column_name": "Motto", "target_field": "motto", "data_type": "string",
    "column_order": 3}
 ]}
```

Note the response envelope on this one route:

```jsonc
{"status": "success",          // <- not "success": true, see issues §23.1
 "message": "Template created.",
 "data": {"id": 7, "code": "schools_master_v2", ... , "columns": [...]}}
```

An administrator then downloads it:

```text
GET /v1/import/system-import-templates/7/download/?tenant=corona&file_format=csv
```

```csv
School Name,School Slug,Motto
Greenfield Academy,greenfield-academy,
```

The Motto cell is blank because the column declares no `sample_value` and no
`default_value`, and `sample_row_data` is empty. That blank is the correct
signal: the column is optional.

Except that it is not treated as optional. If the administrator deletes the
Motto column from their file - reasonably, since none of their schools has one -
validation returns:

```text
ERROR  column_missing  Required template column 'Motto' is missing.
```

and blocks the import (`import_code_issues.md` §6). The only workable habit is
to keep every column and leave the cells empty.

## 8. Gotchas / known limitations

Full evidence for each is in **`error/import/import_code_issues.md`**. The items
belonging to this slice:

- **`is_required` is ignored where it matters most.** The header comparison
  raises an ERROR for every declared column absent from the file and calls each
  one "Required" (`services/template_validation.py:29-40`), so an optional
  column left out blocks the import (issues §6).
- **A retired or draft template can still be used for an upload.**
  `validate_template_id` checks only `is_download_enabled`
  (`serializers.py:751-761`), never `status` (issues §15).
- **Both write paths use `bulk_create`, so no model guard ever runs.**
  `ImportTemplateColumn.clean`'s `allowed_values` check and
  `ImportTemplate.clean`'s "an active template must have at least one column"
  are both dead (`serializers.py:222-226`, `:270-275`; issues §16).
- **Editing columns deletes them and re-creates them**, silently changing what
  every in-flight batch validates against, with no version bump and no warning
  (issues §16).
- **Nothing enforces one active template per dataset type**, and
  `get_active_template_by_dataset` uses `.get()`, so a second ACTIVE template is
  a 500 (`services/template.py:6-16`; issues §23.2).
- **`reference_model` is a free-text model name resolved by scanning every
  installed model, first match wins** (`services/validation_service.py:634-644`),
  and `reference_lookup_field` next to it is fed straight into `values_list`
  (issues §13).
- **`template_file` is a declared, never-written `FileField`**
  (`models.py:159-161`), so `import_template_file_upload_to` is dead code.
- **`.xls` is offered as a `default_file_format`** and cannot be parsed on the
  way back in (issues §17).
- **The create endpoint returns `{"status": "success"}`** rather than the
  platform's `{"success": true}` envelope (`views.py:226-229`; issues §23.1).
- **Three copies of the same visibility filter** (`views.py:196-200`,
  `:262-264`, `:299-300`) rather than one helper - the kind of duplication that
  drifts.
- **Justified by design:** templates are platform-owned, and the platform-tenant
  check sits in `check_permissions` on top of the RBAC key
  (`views.py:208-211`). A school role holding `import.templates.create` still
  cannot author one.
- **Justified by design:** `code` and `dataset_type` are immutable through the
  API (`serializers.py:232-234`). Both are referenced by stored batches and by
  the executor's routing table.
- **Justified by design:** there is no delete route.
  `ImportBatch.template` is `PROTECT` and retiring is the correct disposal.
- **Justified by design:** `validation_rules` is FLS-masked behind
  `import.templates.manage` (`serializers.py:139-141`), which is a key the view
  does not already require.

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Restricted | Scope | Seeded to |
|---|---|---|---|---|---|
| List / retrieve / download | `import.templates.view` | NORMAL | no | TENANT | `xvs_super_admin`, `xvs_platform_admin` |
| Create | `import.templates.create` | SENSITIVE | no | TENANT | same |
| Update, and read `validation_rules` | `import.templates.manage` | SENSITIVE | yes | TENANT | same |

Seeded by `core/management/commands/seed_import_permissions.py:28-36`, with
`scope = PermissionScope.TENANT` (`:145`). `xvs_platform_admin` gets exactly the
three template keys and nothing else from this module
(`:161-166`), and the seeder actively repairs deployments that previously gave
it more (`:180-186`).

**There is no tenant dimension on a template.** `ImportTemplate` has no tenant
column and the catalogue is platform-wide, which is the intended shape: the
column contract for "what a school looks like" is the platform's to define.

The boundary is therefore carried entirely by the `_is_platform` checks in
`check_permissions`, not by the permission scope - the keys are TENANT-scoped,
so a school role may legitimately hold `import.templates.view` in order to
download a template, and `import.templates.create` would be refused by the view
rather than by RBAC.

That split matters: a school admin holding `import.templates.view` sees every
active template, including the three that create schools, branches and CodeX
staff. The template ids are therefore discoverable by any school user, which is
what makes `import_code_issues.md` §8 reachable.

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:122-136` | `TemplateStatusChoices`, `TemplateColumnDataTypeChoices` |
| `models.py:159-161` | `import_template_file_upload_to` - dead |
| `models.py:333-439` | `ImportTemplate` |
| `models.py:442-566` | `ImportTemplateColumn` |
| `constants.py:11-13` | The three template permission keys |
| `services/template.py:6-22` | `get_active_template_by_dataset`, `get_template_headers` |
| `services/template_file.py:11-21` | `_sample_row` - the four-step precedence |
| `services/template_file.py:24-80` | `generate_template_xlsx` - two sheets |
| `services/template_file.py:83-97` | `generate_template_csv` |
| `serializers.py:60-108` | Column list and detail serializers |
| `serializers.py:111-172` | Template list and detail serializers, and the one real FLS rule |
| `serializers.py:174-192` | `ImportTemplateColumnWriteSerializer` |
| `serializers.py:195-228` | `ImportTemplateCreateSerializer` |
| `serializers.py:230-276` | `ImportTemplateUpdateSerializer` - the column replacement |
| `views.py:171-229` | `SystemImportTemplateListView` |
| `views.py:232-281` | `SystemImportTemplateDetailView` |
| `views.py:284-318` | `SystemImportTemplateDownloadView` |
| `core/management/commands/seed_import.py` | The four templates and their 67 columns |
| `vs_import_data/migrations/0005_seed_bank_statement_template.py` | The bank-statement template, seeded by migration |
| `core/management/commands/seed_import_permissions.py` | The fourteen `import.*` keys and their grants |

## 11. Test coverage & gaps

Baseline: **`Ran 18 tests in 7.596s` - OK**
(`cd apps && DB_NAME=cx_importslice ../cx/Scripts/python.exe manage.py test
vs_import_data --settings=apps.settings.local --noinput`).

What this slice covers:

- `ImportDoesNotWriteARoleLabelTests.test_an_unknown_header_is_a_warning_not_a_refusal`
  (`tests.py:480-514`) - a header the template does not declare produces exactly
  one `column_unknown` **warning**, so a stale spreadsheet column does not fail
  an upload. It is the only test of `compare_uploaded_headers_to_template`, and
  it exercises the `extra` branch only.

That is the whole of it. Templates are constructed as fixtures in most other
tests (`tests.py:33-45`, `:81-86`, `:215-226`, `:275-285`, `:494-504`) but never
as the subject.

What it does not cover:

1. **Every endpoint in this slice.** List, create, retrieve, update and download
   have no test - no 200, no 403 for a school user attempting a create, no
   platform-tenant refusal, no `?dataset_type=` filter, and no assertion that a
   non-platform caller cannot see a DRAFT template.
2. **The `missing` branch of the header comparison.** Nothing asserts what
   happens when a declared column is absent, which is why issues §6 - every
   optional column treated as required - has survived.
3. **Both file generators.** `generate_template_csv` and
   `generate_template_xlsx` are untested: not the header order, not the
   `_sample_row` precedence chain, not the Instructions sheet, not the
   31-character sheet-name truncation, and not the `?file_format=` switch.
4. **The column replacement path.** Nothing asserts that a PATCH carrying
   `columns` deletes the old set, and nothing notices that `bulk_create` skips
   `clean()` (issues §16).
5. **The status lifecycle.** No test stamps `published_at` or `retired_at`, and
   none asserts that a retired template is hidden from a school user - or, more
   to the point, that it is still accepted at upload (issues §15).
6. **The FLS rule on `validation_rules`.** No test confirms it is masked for a
   `view`-only holder, which is the module's only genuine field-level
   protection.
7. **The seeded catalogue.** Nothing runs `seed_import` and asserts the four
   templates exist with the column counts the handlers expect - which is the
   test that would catch a `target_field` renamed on one side only.
