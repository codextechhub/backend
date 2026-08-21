# import_validation

The gate between an uploaded file and a database write. Seven checking passes
run against one batch, every finding becomes an `ImportValidationIssue` row, and
one number - the error count - decides whether the import may proceed. This
slice covers the pass order, the per-type value validators, the dataset-specific
business rules, the issue API, the CSV export, and the resolve action.

Routes covered by this slice, mounted at `/v1/import/` (`apps/urls.py:30`):
`batches/<id>/validate/`, `batches/<id>/issues/`,
`batches/<id>/issues/export/`, `batches/<id>/issues/<id>/`,
`batches/<id>/issues/<id>/resolve/`.

Templates are `import_templates_catalogue`; upload and parsing are
`import_batch_upload`; execution and rollback are `import_execution_rollback`;
the Celery layer is `import_tasks_notifications_audit`.

---

## 1. What it is (and what it is NOT)

- **Validation reads the stored rows, not the file.** Every pass iterates
  `import_batch.preview_rows` (`services/validation_service.py:105`, `:134`,
  `:160`, `:238`, `:488`, `:658`), which the upload serializer filled with the
  whole file. The file itself is never re-opened.
- **One number is the gate.** `is_ready_for_import` is
  `error_count == 0 and total_rows > 0`
  (`services/validation_service.py:715`). Warnings never block, and the code
  says why: dataset-wide errors have no row number, so "some rows are valid"
  could publish a structurally invalid batch (`:711-714`).
- **Every run replaces the previous one.** `_save_validation_issues` deletes all
  existing issues for the batch and bulk-creates the new set
  (`services/validation_service.py:31`, `:53`). There is no history and no
  carry-forward - including of `is_resolved`.
- **The whole pass is one transaction.** `validate_import_batch` is
  `@transaction.atomic` (`services/validation_service.py:740`), so the
  `VALIDATING` status it writes at the top is committed together with the final
  state and is never externally visible.
- **Validation mutates the data it is checking.** Any `date` column is
  normalised to `YYYY-MM-DD` in place and written back to `preview_rows`
  (`services/template_validation.py:70-74`,
  `services/validation_service.py:117-120`), so the executor later reads the
  corrected values.
- **Row numbers are positions in `preview_rows`, not lines in the file.** Blank
  lines are dropped at parse time and the header offset is applied there
  (`services/file_parser.py:54-62`), so "Row 12" means the twelfth data row.
- **Severity is decided by the check, not by the column.** `is_required` is read
  when checking a *value* and ignored when checking whether a *column* is
  present, which is the module's most consequential validation defect (§8).
- **This is not a preview of what execution will do.** Validation and execution
  disagree about duplicates (§8), the dataset rules reimplement the serializers
  rather than calling them, and a clean validation is no guarantee that a row
  will import.

## 2. Domain model

### `ImportValidationIssue` (`models.py:572`)

| Field | Meaning |
|---|---|
| `import_batch` | FK, CASCADE |
| `severity` | `error` / `warning` / `info` (`models.py:78-81`) |
| `code` | One of thirteen `ValidationCodeChoices` (`models.py:84-97`) |
| `message` | The sentence shown to the operator |
| `help_text` | Optional guidance; populated by nothing today |
| `row_number` | 1-based index into `preview_rows`; **null for file-level issues** |
| `column_name` | The spreadsheet header; blank for file-level issues |
| `field_name` | The internal target field; populated by nothing today |
| `raw_value` | The cell as read, coerced to a string |
| `normalized_value` | The cleaned value; populated by nothing today |
| `metadata` | Structured extras - `first_seen_row`, slug `suggestions` |
| `is_resolved`, `resolved_at`, `resolved_by` | The acknowledgement flag and its provenance |

Ordered by `(row_number, column_name, created_at)` and indexed on
`(import_batch, severity)`, `(import_batch, code)` and
`(import_batch, row_number)` (`models.py:645-651`).

Three of the thirteen codes are never emitted anywhere:
`file_type_invalid`, `file_empty`, `sheet_missing` - the upload serializer
refuses those cases before a batch exists. `duplicate_mapping` is left over from
the manual-mapping design.

### The batch fields this slice owns (`models.py:274-281`)

| Field | Written by | Meaning |
|---|---|---|
| `validation_summary` | `_update_batch_validation_state` | The `summarize_issues` dict, plus a `dataset` key for bank statements |
| `has_critical_errors` | same | `error_count > 0` |
| `is_ready_for_import` | same | `error_count == 0 and total_rows > 0` |
| `structure_matches_template` | same | Also `error_count == 0` - see §8 |
| `validation_started_at`, `validation_completed_at` | `validate_import_batch` | Both inside the same transaction |
| `template_headers_snapshot` | `validate_import_batch` (`:757-758`) | Refreshed from the template on every run |

## 3. Endpoint map

| Method + path | Permission | Notes |
|---|---|---|
| `POST /batches/<id>/validate/` | `import.batches.run` | Runs the full pass synchronously and returns summary + issues inline |
| `GET /batches/<id>/issues/` | `import.validations.view` | `?severity=`, `?is_resolved=true|false|1|0`; paginated |
| `GET /batches/<id>/issues/<issue_id>/` | `import.validations.view` | One issue |
| `PATCH /batches/<id>/issues/<issue_id>/resolve/` | `import.validations.update` | Only accepts `is_resolved: true` |
| `GET /batches/<id>/issues/export/` | `import.validations.view` | CSV of every issue, unpaginated |

All five use `HasImportBatchRBACPermission` and inherit
`ImportBatchContextMixin`, so the batch is resolved under the caller's tenant
before anything else runs (`views.py:121-137`).

`?tenant=<slug>` is mandatory on all of them.

### Request bodies actually read

`POST /batches/<id>/validate/` (`serializers.py:876-881`) declares two fields:

```jsonc
{"run_full_validation": true, "include_warnings": true}
```

**Neither is read.** `ValidateImportBatchView` validates the serializer and then
calls `validate_import_batch(import_batch)` with no arguments
(`views.py:612-615`), so the full pass always runs and warnings are always
included.

`PATCH .../resolve/` (`serializers.py:340-358`) reads one field:

```jsonc
{"is_resolved": true}
```

and refuses anything else with "This action only supports setting is_resolved to
true." `resolved_at` is stamped and `resolved_by` is taken from
`request.user` - both server-side.

### Serializer field sets

| Serializer | Fields |
|---|---|
| `ImportValidationIssueListSerializer` (`serializers.py:279`) | `id`, `severity`, `code`, `message`, `row_number`, `column_name`, `raw_value`, `is_resolved`, `created_at` |
| `ImportValidationIssueDetailSerializer` (`serializers.py:300`) | the above plus `help_text`, `field_name`, `normalized_value`, `metadata`, `is_resolved`, `resolved_at`, `resolved_by` |

The `validate/` endpoint does not use either. It returns a hand-built shape
through `_format_validation_issues` (`views.py:73-92`), which renames the keys:

```jsonc
{"severity": "error", "code": "required_value_missing",
 "row": 12, "column": "School Name",
 "message": "...", "raw_value": "", "help_text": ""}
```

`row_number` becomes `row` and `column_name` becomes `column`, so the inline
response and the list endpoint speak different field names for the same thing.
The sort key puts file-level issues first, then rows in order, then columns
alphabetically (`views.py:91`).

## 4. Lifecycle / state machine

```text
POST /batches/<id>/validate/
        |
        v
[ validating ]                      written inside the transaction, never visible
        |
   seven passes, in order
        |
        v
delete every existing issue, bulk-create the new set
        |
        +-- error_count == 0 and total_rows > 0 --> [ ready_to_import ]
        |                                            is_ready_for_import = True
        |                                            structure_matches_template = True
        |
        +-- otherwise ----------------------------> [ validation_failed ]
                                                     has_critical_errors = True
```

Re-validation is unrestricted: any batch can be validated again at any time,
from any status, including `import_succeeded`. Nothing checks the current state
before running, so re-validating an already-imported batch will move it back to
`ready_to_import` and delete the record of what was wrong with it.

Resolving an issue changes the issue row and nothing else - not the summary, not
`has_critical_errors`, not `is_ready_for_import` (§8).

## 5. Derivations

### The seven passes, in order (`services/validation_service.py:771-777`)

| # | Pass | Function | Emits |
|---|---|---|---|
| 1 | Template present | `_validate_template_presence` (`:56`) | one `business_rule` error if no template |
| 2 | Dataset-wide row counts | `_validate_template_rules` (`:143`) | `business_rule` errors from `min_rows` / `max_rows` |
| 3 | Headers | `_validate_headers_against_template` (`:74`) | `duplicate_mapping`, `column_missing`, `column_unknown` |
| 4 | Row values | `_validate_rows_against_template` (`:95`) | the per-type errors below; **normalises dates in place** |
| 5 | Uniqueness in file | `_validate_template_uniqueness_rules` (`:125`) | `duplicate_record` per column marked `is_unique` |
| 6 | Cross references | `_validate_cross_references` (`:647`) | `cross_reference_missing` |
| 7 | Dataset business rules | `_validate_dataset_specific_rules` (`:208`) | routed by `dataset_type` |

Pass 4 fetches the template's columns once and passes the list down, with a
comment saying why (`:97-98`, `:106`). Pass 7 for `bank_statements` delegates to
`vs_finance.statement_imports.validate_bank_statement_import_batch` and stashes
its summary on the instance for the final payload (`:220-225`, `:780-782`).

### Per-value validators (`services/template_validation.py:55-125`)

Applied only when the cell is non-blank; a blank required cell short-circuits
with `required_value_missing` and skips the rest of the column's checks
(`:66-71`).

| `data_type` | Function | Accepts |
|---|---|---|
| `email` | `validate_email` (`validators.py:196`) | exactly one `@`, non-empty local part, a dot in the domain, no leading/trailing/doubled dots, no spaces. Hand-rolled, not Django's validator |
| `integer` | `validate_integer` (`:233`) | |
| `decimal` | `validate_decimal` (`:254`) | |
| `boolean` | `validate_boolean` (`:276`) | `true`, `false`, `yes`, `no`, `1`, `0` - case-insensitive |
| `choice` | `validate_choice` (`:398`) | membership in `allowed_values`, **case-insensitively** |
| `date` | `validate_date` (`:347`) | after normalisation, below |
| `datetime` | `validate_datetime` (`:374`) | |

`max_length` is checked for **every** type, not only strings
(`services/template_validation.py:122-125`).

### Date normalisation, and the ambiguity it resolves silently

`normalize_date_value` (`validators.py:318-346`) tries fourteen formats in
order, then falls back to `datetime.fromisoformat` for the ISO-like strings
openpyxl produces from Excel date cells:

```text
%Y-%m-%d  %Y/%m/%d  %d/%m/%Y  %d-%m-%Y  %d.%m.%Y  %d %b %Y  %d %B %Y
%b %d, %Y  %B %d, %Y  %b %d %Y  %B %d %Y  %Y%m%d  %d/%m/%y  %m/%d/%Y
```

`%d/%m/%Y` is deliberately ahead of `%m/%d/%Y` "to match the Nigerian
convention", and `%d/%m/%y` is ahead of it too. The result is written back into
`preview_rows` and persisted.

So `03/04/2026` becomes `2026-04-03` - the third of April - and the operator is
never told which reading was taken. A file authored in a US locale is silently
converted to different dates rather than rejected. The trade is deliberate and
the ordering is right for the market; the silence is the part worth knowing.

### Uniqueness and cross references

- **In-file duplicates** (`validators.py:443-483`): first occurrence wins,
  comparison is case-insensitive on the stripped value, blanks are skipped, and
  the issue's `metadata` carries `first_seen_row`.
- **Cross references** (`services/validation_service.py:647-700`): for each
  column declaring both `reference_model` and `reference_lookup_field`, resolve
  the model by class name across every installed app, pull the whole lookup
  column into a Python set, and compare each row's value case-insensitively
  (`validators.py:531-566`). The scoping attempt on `school`/`branch` almost
  never engages (§8), and a bad `reference_lookup_field` is swallowed by
  `except Exception: continue` (`:686-687`).

### The summary (`validators.py:707-729`)

```jsonc
{"total_issues": 47, "error_count": 12, "error_rows": 9,
 "warning_count": 35, "info_count": 0, "has_critical_errors": true}
```

`error_rows` counts **distinct** row numbers carrying at least one error, so
nine bad rows producing twelve errors reads correctly. File-level errors have no
row number and are excluded from that count while still counting in
`error_count` - which is the right split.

### Dataset business rules

**`schools`** (`services/validation_service.py:229-479`), 250 lines
reimplementing `SchoolCreateSerializer`'s rules ahead of time. Three lookups are
hoisted out of the row loop: active package plans, active module capability
keys, and every existing school slug (`:243-248`). Per row it checks:

- **Slug**, resolved exactly as the serializer will
  (`slugify(slug)` or `slugify(name)`, with `-school` appended when an
  auto-generated slug is reserved), then tested against
  `RESERVED_TENANT_SLUGS`, against existing slugs - with up to five free
  alternatives offered in `metadata.suggestions` - and against earlier rows.
- **Package plan** exists and is active, then the three capacities against the
  plan's `max_students` / `max_teachers` / `max_admins`, and a `min_value=1`
  floor mirroring the serializer.
- **Module keys**, each comma-separated entry against active MODULE capabilities.
- **`subscription_expires_at`**, ISO format and strictly in the future.
- **Admin names**, required whenever the matching email is present.
- **Admin emails**, normalised with `normalize_email` rather than `.lower()` -
  the comment records why: a bare `.lower()` missed a stored `Ada@gmail.com`
  and passed the row as importable. Then `email_refusal(email, tenant=None)`,
  because every row creates a brand-new tenant and there is no tenant for the
  address to be taken in yet.

**`branches`** (`:482-631`) resolves the target school from the batch, then
`school_slug`, then `school_code`; enforces one `is_main = TRUE` per school both
within the file and against the database; requires an admin name when an admin
email is present; and scopes `email_refusal` to the resolved school's tenant,
which is the difference from the schools rules and is deliberately explained in
place (`:596-605`).

**`cx_users`** has no dataset rules at all. The only checks a CX-user row gets
are the generic per-column ones, so a duplicate email inside the file is caught
only if the template marks that column `is_unique`, and an address already held
on the platform is caught at execution rather than at validation.

## 6. What writing writes

| Action | Written by | Rows |
|---|---|---|
| Validate | `_save_validation_issues` (`services/validation_service.py:27-53`) | **delete all** existing issues, then `bulk_create` the new set |
| Validate | `_validate_rows_against_template` (`:119-120`) | `preview_rows`, with dates rewritten |
| Validate | `validate_import_batch` (`:760-767`) | `status`, `validation_started_at`, `template_headers_snapshot` |
| Validate | `_update_batch_validation_state` (`:724-734`) | `validation_summary`, `validation_completed_at`, `has_critical_errors`, `is_ready_for_import`, `structure_matches_template`, `status` |
| Validate (view) | `ValidateImportBatchView.post` (`views.py:617-627`) | one `AuditEvent`, `batch_validated` -> `AuditActionType.CUSTOM` |
| Resolve | `ImportValidationIssueResolveSerializer.update` (`serializers.py:353-358`) | `is_resolved`, `resolved_at`, `resolved_by` |
| Resolve (view) | `perform_update` (`views.py:700-715`) | one `AuditEvent`, `issue_resolved` -> `UPDATE` |

All of the validation writes happen inside the single `@transaction.atomic`
wrapper; the audit event is written by the view **after** it commits, so a
failure between the two loses the record of a validation that happened.

`bulk_create` on the issues means `ImportValidationIssue` has no `full_clean`
applied - harmless here, since every field is set server-side from a controlled
dict.

Exporting and listing write nothing.

## 7. Worked example

CodeX validates a schools file with eight rows. Row 3 has a blank School Name,
row 5 reuses row 2's admin email, and the file omits the optional Motto column.

```text
POST /v1/import/batches/12/validate/?tenant=codex
{}
```

```jsonc
{"success": true, "message": "Validation completed successfully.",
 "data": {
   "summary": {"total_issues": 4, "error_count": 3, "error_rows": 2,
               "warning_count": 1, "info_count": 0, "has_critical_errors": true},
   "issues": [
     {"severity": "error", "code": "column_missing", "row": null,
      "column": "Motto",
      "message": "Required template column 'Motto' is missing.",
      "raw_value": null, "help_text": ""},
     {"severity": "warning", "code": "column_unknown", "row": null,
      "column": "Notes",
      "message": "Uploaded column 'Notes' is not part of the official template.",
      "raw_value": null, "help_text": ""},
     {"severity": "error", "code": "required_value_missing", "row": 3,
      "column": "School Name",
      "message": "'School Name' is required.", "raw_value": "", "help_text": ""},
     {"severity": "error", "code": "duplicate_record", "row": 5,
      "column": "School Admin Email",
      "message": "Email 'ada.okoye@example.test' is already used in row 2. Each admin email must be unique across rows.",
      "raw_value": "ada.okoye@example.test", "help_text": ""}
   ]}}
```

Two of those three errors are real. The first is not: Motto is an optional
column and leaving it out is a reasonable thing to do, but the header comparison
calls every declared column "Required" (§8). The batch is now
`validation_failed` and `is_ready_for_import` is `false` partly because of a
field nobody was going to fill in.

The operator decides the Motto error is nonsense and resolves it:

```text
PATCH /v1/import/batches/12/issues/41/resolve/?tenant=codex
{"is_resolved": true}
```

The issue shows a tick. `is_ready_for_import` is still `false`, because nothing
reads `is_resolved` (§8). They fix rows 3 and 5, add the Motto column back with
every cell blank, and re-validate:

```jsonc
{"summary": {"total_issues": 1, "error_count": 0, "error_rows": 0,
             "warning_count": 1, "info_count": 0, "has_critical_errors": false},
 "issues": [{"severity": "warning", "code": "column_unknown", "row": null,
             "column": "Notes", "message": "Uploaded column 'Notes' is not part of the official template.", ...}]}
```

The batch is `ready_to_import`. The tick is gone - the resolved issue row was
deleted and a fresh set created.

And a date detail worth knowing: if row 4's `Subscription Expires At` cell read
`03/04/2027`, the batch now stores `2027-04-03` in `preview_rows`, permanently.
Nothing in the response says a value was rewritten.

## 8. Gotchas / known limitations

Full evidence for each is in **`error/import/import_code_issues.md`**. The items
belonging to this slice:

- **Every optional template column missing from the file is a hard error.**
  `compare_uploaded_headers_to_template` never consults `is_required`, and its
  message asserts the opposite (`services/template_validation.py:29-40`;
  issues §6).
- **"Mark as resolved" cannot unblock an import**, and re-validating destroys
  the resolution. `is_resolved` is written, listed, filtered and exported, and
  read by no gate (`services/validation_service.py:31`, `:707-716`;
  issues §9).
- **Cross-reference validation is effectively unscoped.** The `school` narrowing
  targets a field most models no longer have and the `branch` narrowing targets
  a column nothing populates, so the valid-value set is whatever
  `model_class.objects.all()` returns - which in a Celery run has no ambient
  tenant behind it (`services/validation_service.py:675-682`; issues §12).
- **`_resolve_model` scans every installed model by class name, first match
  wins**, and `reference_lookup_field` is fed straight into `values_list`
  (`services/validation_service.py:634-644`, `:685`; issues §13).
- **Validation and execution disagree about duplicates.** Validation slugifies
  before comparing (`:267-269`); the executor compares the raw cell and adds a
  name fallback validation never considers
  (`services/import_executor.py:262-275`; issues §19).
- **The two request fields are declared and ignored.**
  `run_full_validation` and `include_warnings` (`serializers.py:876-881`) are
  validated and discarded (`views.py:612-615`).
- **`structure_matches_template` is set from the total error count**
  (`services/validation_service.py:716`), so a file whose headers match
  perfectly reports `false` because one date cell was malformed
  (issues §23.6).
- **`VALIDATING` is never observable**, because the whole pass is one
  transaction (`services/validation_service.py:740`, `:754-767`;
  issues §23.5).
- **Re-validation is unrestricted by status**, so an already-imported batch can
  be pushed back to `ready_to_import` and its issue history erased.
- **Dates are silently rewritten in place** with `%d/%m/%Y` ahead of
  `%m/%d/%Y` (`validators.py:305-346`). Correct for the market, invisible to the
  operator, and persisted to `preview_rows`.
- **`validate_email` is hand-rolled** (`validators.py:196-231`) rather than
  Django's `EmailValidator`, so what validation accepts and what the user
  serializer accepts at execution can differ.
- **`validate_choice` compares case-insensitively** (`validators.py:398-419`)
  while the executor hands the raw cell to a DRF `ChoiceField`, which does not -
  so `male` validates and then fails at execution against `MALE`.
- **The issues export is unpaginated and built in memory**
  (`services/template_file.py:100-133`), so a 50,000-row file with two issues
  per row produces a 100,000-line CSV assembled in a request thread.
- **`help_text`, `field_name` and `normalized_value` are columns nothing
  writes** (`models.py:623`, `:627`, `:630`), yet all three are in the detail
  serializer, so every issue detail carries three permanently empty fields.
- **Three declared codes are never emitted**: `file_type_invalid`,
  `file_empty`, `sheet_missing` (`models.py:85-87`), because the upload
  serializer refuses those cases before a batch exists.
- **`cx_users` has no dataset rules**, so a duplicate address inside the file or
  already on the platform is caught at execution, not here.
- **Justified by design:** warnings never block. The publish gate is
  `error_count == 0`, with the reasoning written out in place
  (`services/validation_service.py:711-714`).
- **Justified by design:** dataset-wide errors have no row number and are
  excluded from `error_rows` while still counting in `error_count`
  (`validators.py:718-723`).
- **Justified by design:** admin emails are normalised with `normalize_email`
  rather than `.lower()`, because the model applies the same fold on write and a
  bare lowercase missed stored addresses
  (`services/validation_service.py:439-443`).
- **Justified by design:** the schools rules scope `email_refusal` to
  `tenant=None` and the branches rules scope it to the row's school
  (`:454`, `:605-606`). Every schools row creates a new tenant; every branches
  row joins an existing one. Both are tested.
- **Justified by design:** the column list is fetched once and passed down
  (`services/validation_service.py:97-98`, `:106`), avoiding the N+1 the
  executor still has.

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Restricted | Scope |
|---|---|---|---|---|
| Run validation | `import.batches.run` | NORMAL | no | TENANT |
| List / retrieve / export issues | `import.validations.view` | NORMAL | no | TENANT |
| Resolve an issue | `import.validations.update` | NORMAL | no | TENANT |

Seeded by `core/management/commands/seed_import_permissions.py:49-57` with
`scope = PermissionScope.TENANT`.

Isolation is inherited from the batch and it holds. Every view in this slice
resolves the batch through `ImportBatchContextMixin.get_import_batch`
(`views.py:121-137`), which filters on the caller's tenant, and every issue
queryset is then filtered by that batch
(`views.py:652-654`, `:682-683`, `:697-698`). There is no path to another
tenant's issues, and a foreign batch id is a 404 rather than a 403.

Two things worth stating:

1. **`import.validations.update` is NORMAL and unrestricted**, and the flag it
   sets is inert. If issues §9 is resolved in the direction of "resolution is an
   override", the key has to be re-graded at the same time - an unrestricted
   NORMAL key that can wave through a validation error is not the same thing as
   one that records an acknowledgement.
2. **The dataset rules read across tenants by design.** `_validate_schools_rules`
   loads every existing school slug (`services/validation_service.py:248`) and
   `_validate_branches_rules` resolves schools by slug or code with no tenant
   filter (`:511-513`, `:521`). Both are correct - a slug is globally unique and
   these are platform operations - but they mean a school user who somehow
   reached a schools batch could learn whether a given slug exists, one row at a
   time. That is a reason to gate the dataset type rather than to change these
   lookups (issues §8).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py:78-97` | `ValidationSeverityChoices`, `ValidationCodeChoices` |
| `models.py:572-655` | `ImportValidationIssue` |
| `services/validation_service.py:27-53` | `_save_validation_issues` - the delete-and-replace |
| `services/validation_service.py:56-72` | Pass 1, template presence |
| `services/validation_service.py:74-92` | Pass 3, headers |
| `services/validation_service.py:95-122` | Pass 4, rows - and the date write-back |
| `services/validation_service.py:125-140` | Pass 5, in-file uniqueness |
| `services/validation_service.py:143-195` | Pass 2, `min_rows` / `max_rows` |
| `services/validation_service.py:198-205` | `_build_col_resolver` - target field to header |
| `services/validation_service.py:208-226` | Pass 7 routing |
| `services/validation_service.py:229-479` | `_validate_schools_rules` |
| `services/validation_service.py:482-631` | `_validate_branches_rules` |
| `services/validation_service.py:634-700` | `_resolve_model`, pass 6 |
| `services/validation_service.py:703-734` | `_update_batch_validation_state` - the gate |
| `services/validation_service.py:740-790` | `validate_import_batch` |
| `services/template_validation.py:17-52` | `compare_uploaded_headers_to_template` |
| `services/template_validation.py:55-125` | `validate_row_against_template` |
| `validators.py:11-38` | `normalize_string`, `is_empty`, `normalize_header` |
| `validators.py:151-190` | Required-value checks |
| `validators.py:196-437` | The per-type validators |
| `validators.py:305-346` | `_DATE_FORMATS`, `normalize_date_value` |
| `validators.py:443-483` | `find_duplicate_values` |
| `validators.py:531-566` | `validate_foreign_key_reference` |
| `validators.py:707-729` | `summarize_issues` |
| `services/template_file.py:100-133` | `generate_validation_issues_csv` |
| `serializers.py:279-363` | Issue list, detail and resolve serializers |
| `views.py:73-92` | `_format_validation_issues` - the inline response shape |
| `views.py:600-733` | The five views in this slice |

## 11. Test coverage & gaps

Baseline: **`Ran 18 tests in 7.596s` - OK**.

What this slice covers:

- `ImportValidationPublishGateTests` (`tests.py:29-66`) - the single most
  important assertion in the module: one valid row does not hide another row's
  error. Two rows, one blank required cell, and the test pins
  `error_count == 1`, `has_critical_errors`, `not is_ready_for_import`, and
  `status == validation_failed`. It is a full `validate_import_batch` run.
- `ImportAdminEmailScopeTests` (`tests.py:190-326`) - six tests over the two
  dataset rule sets, and they are careful work. They cover the tenant scoping of
  `email_refusal` in both directions (an address held by another tenant is
  accepted for a branches row into a different school; the same address in the
  *same* tenant is refused), the transitional cross-tenant refusal while
  `REQUIRE_TENANT_ON_SIGN_IN` is off, the schools case where every row creates a
  new tenant, and the within-file repeat rule - with the row number asserted.
- `ImportDoesNotWriteARoleLabelTests.test_an_unknown_header_is_a_warning_not_a_refusal`
  (`tests.py:480-514`) - the `extra` branch of the header comparison.

What it does not cover:

1. **The `missing` branch of the header comparison.** Nothing asserts what
   happens when a declared column is absent from the file, which is exactly why
   issues §6 - every optional column treated as a required one - has never
   surfaced. One test with an optional column omitted would fail today.
2. **Every per-type validator.** `validate_email`, `validate_integer`,
   `validate_decimal`, `validate_boolean`, `validate_choice`, `validate_date`,
   `validate_datetime` and `validate_max_length` have no direct test - and
   neither does `normalize_date_value`, which silently rewrites data across
   fourteen formats with a locale-dependent ordering.
3. **Passes 2, 5 and 6.** `min_rows` / `max_rows`, in-file uniqueness and cross
   references are never exercised. The cross-reference scoping defect
   (issues §12) and the model-name resolution (issues §13) both live in
   untested code.
4. **Every endpoint in this slice.** `validate/`, `issues/`, `issues/<id>/`,
   `resolve/` and `issues/export/` have no request test. `ImportValidationPublishGateTests`
   calls the service directly.
5. **The resolve flow.** Nothing asserts that resolving does or does not change
   the gate, and nothing asserts what happens to a resolved issue on
   re-validation - the two halves of issues §9.
6. **The CSV export.** `generate_validation_issues_csv` is untested: not the
   column order, not the "File" placeholder for a null row number, not the
   uppercased severity.
7. **The schools rules beyond emails.** Slug resolution and its suggestions,
   reserved slugs, package plan and capacity limits, module keys and
   `subscription_expires_at` are all untested - roughly 200 of the 250 lines.
8. **Re-validation.** No test runs `validate_import_batch` twice on the same
   batch, so neither the delete-and-replace nor the loss of `is_resolved` is
   pinned.
