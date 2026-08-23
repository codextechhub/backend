# import_code_issues

Everything wrong with `vs_import_data`, in one place, ordered by how much it
costs. Each item states the defect, the evidence, what actually happens to a
user, and the fix. The five slice reports (`import_templates_catalogue`,
`import_batch_upload`, `import_validation`, `import_execution_rollback`,
`import_tasks_notifications_audit`) point here rather than repeating it.

Baseline: the `vs_import_data` suite is **18 tests, all green**
(`Ran 18 tests in 7.596s` - OK, via
`cd apps && DB_NAME=cx_importslice ../cx/Scripts/python.exe manage.py test
vs_import_data --settings=apps.settings.local --noinput`). Eighteen tests for
8,142 lines of code. Every item below is therefore something the suite does not
currently catch, and several are things it walks right past.

The four items marked **confirmed by execution** were reproduced against a real
Postgres test database in a throwaway test module that was deleted afterwards.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in
the code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | Rolling back a branches or CX-users import deletes unrelated schools by primary-key collision, and reports success | **Critical** |
| 2 | `GET /batches/<id>/download/` is a 500 in every environment | **High** |
| 3 | "Platform users see all" is false - the ambient tenant filter silently re-scopes them | **High** |
| 4 | The stuck-job safety net has never run: it raises `FieldError` on every scheduled tick | **High** |
| 5 | A partly failed import is recorded as SUCCEEDED, and the retry guard then refuses to re-run it | **High** |
| 6 | Every optional template column missing from the file is a hard error - `is_required` is never consulted | **High** |
| 7 | Nothing is ever notified: the send task only flips a flag | **High** |
| 8 | Tenant-scoped import keys reach platform-level operations - creating schools and CX staff | **High** |
| 9 | "Mark as resolved" cannot unblock an import, and re-validating destroys the resolution | **Medium** |
| 10 | The entire file body is stored on the batch row and returned by the detail endpoint, unpaginated | **Medium** |
| 11 | Execution costs four writes and one query per row, forever | **Medium** |
| 12 | Cross-reference validation is effectively unscoped across tenants | **Medium** |
| 13 | `_resolve_model` will resolve any model in the platform by class name, first match wins | **Medium** |
| 14 | An engine app imports `schools.vs_schools`, and three of four dataset types are school vocabulary | **Medium** |
| 15 | A retired or draft template can still be used for an upload | **Medium** |
| 16 | Editing a template's columns deletes and re-creates them, skipping every model guard | **Medium** |
| 17 | `.xls` is advertised in three places and cannot be read | **Medium** |
| 18 | `branch` is never set on any batch, so the entire branch dimension is dead | **Medium** |
| 19 | Validation and execution disagree about what a duplicate is | **Medium** |
| 20 | A row whose administrator could not be provisioned is still counted as a success | **Medium** |
| 21 | The execution task auto-retries a non-idempotent, multi-write operation | **Medium** |
| 22 | No school role is granted any `import.*` key out of the box | **Low** |
| 23 | Smaller defects and dead code | **Low** |

---

## 1. Rolling back a branches or CX-users import deletes unrelated schools

**Critical. Confirmed by execution.**

### The defect

The rollback engine knows how to reverse exactly one thing: a `School`.

```python
# services/rollback_service.py:14-41
def reverse_target_record(row_result):
    """
    Reverse one imported row. For schools: delete the School record that was created.
    """
    from schools.vs_schools.models import School

    pk = row_result.target_object_pk
    if not pk:
        return True
    try:
        ref = str(pk).strip()
        qs = (
            School.objects.filter(pk=int(ref))
            if ref.isdigit() and int(ref) <= 9_223_372_036_854_775_807
            else School.objects.filter(slug=ref)
        )
        qs.delete()
        return True
    except Exception:
        return False
```

It is called for **every** row result of **every** dataset type:

```python
# services/rollback_service.py:73-76
for row_result in job.row_results.exclude(target_object_pk=""):
    success = reverse_target_record(row_result)
```

`ImportJobRowResult.target_object_pk` is whatever primary key the row created
(`services/import_executor.py:603`). For the `branches` dataset it is a
`Branch.pk`; for `cx_users` it is a `User.pk`. All three models use
`BigAutoField` integers drawn from independent sequences, so `Branch` 3,
`User` 3 and `School` 3 all exist and all look identical to this function.

`School.objects` has no tenant-aware manager, so the delete is
platform-wide, and only three models point at `School`, all `CASCADE` - nothing
raises `ProtectedError` to stop it.

### What actually happens

Bright Star School is school number 3 on the platform. Corona Secondary School
imports two new branches; those `Branch` rows come out as ids 3 and 4. An
operator notices the branch names are wrong and hits Rollback.

The rollback reads row result 1, sees `target_object_pk = "3"`, and runs
`School.objects.filter(pk=3).delete()`. **Bright Star School is deleted** -
its identity record, its branding, its package and subscription setup, and its
primary-admin link. Its `Tenant` row survives with no `school_profile`, so
every school-scoped screen for Bright Star breaks and its subdomain no longer
resolves to anything.

Then the rollback writes:

```jsonc
{"was_successful": true, "reverted_rows_count": 2}
```

Corona's two branches are still there. Nothing in the response, the audit
event, or the rollback record mentions Bright Star.

Reproduced end to end:

```text
PROBE victim School pk=3  imported Branch pk=3
PROBE reverse_target_record(branch_pk) returned True
PROBE victim school still exists: False
```

### Why it exists

The function was written when `schools` was the only dataset type, and the
executor grew two more (`branches`, `cx_users`) without the rollback growing
with them. The test suite pinned the schools case carefully - there is a whole
class about the slug-to-id cutover
(`tests.py:144-187`) - and the one test that covers a non-matching reference
picks values that **cannot** collide:

```python
# tests.py:185-186
self.assertTrue(reverse_target_record(self._row("9" * 40)))
self.assertTrue(reverse_target_record(self._row("")))
```

A forty-digit number is above the `int64` guard and an empty string returns
early. The test proves "an unknown reference is a no-op" using the only two
kinds of reference that are guaranteed safe.

### The fix

`target_model` is already recorded on every row result
(`models.py:805`) and is already populated by every handler. Use it:

```python
_REVERSERS = {"School": ..., "Branch": ..., "User": ...}
model = _REVERSERS.get(row_result.target_model)
if model is None:
    return False          # unknown target: refuse, do not guess
```

Three further changes belong with it:

1. **Refuse rather than guess.** A row whose `target_model` is not reversible
   must return `False` and be reported, not silently skipped.
2. **Stop lying about the outcome.** `was_successful` is assigned `True`
   unconditionally (`services/rollback_service.py:78`) whatever the loop
   returned. Set it from the results.
3. **Decide what reversing a `User` even means.** Deleting an approved CX
   staff account is not obviously right; deactivating it may be. Until that is
   decided, `cx_users` rollback should refuse outright rather than do something
   arbitrary.

---

## 2. `GET /batches/<id>/download/` is a 500 in every environment

**High. Confirmed by execution.**

### The defect

```python
# views.py:581-583
file_path = batch.file.path
if not os.path.exists(file_path):
    raise Http404("File not found on server.")
```

with a docstring one screen above it that states the assumption:

> Works in both DEBUG and non-DEBUG environments because it reads
> the file from MEDIA_ROOT and serves it directly rather than
> redirecting to a media URL. (`views.py:561-564`)

Media is no longer on disk. `STORAGES["default"]` is
`core.storage.DatabaseStorage` (`apps/settings/base.py:376-377`), and that
backend implements `_open`, `_save`, `exists`, `delete`, `size`, `url` and
`get_available_name` - but **not** `path`. Django's base `Storage.path` raises
`NotImplementedError("This backend doesn't support absolute paths.")`, and
`core/exceptions.py` has no handler for it.

`MEDIA_ROOT` is still defined and the settings file says so outright:
`"unused by DatabaseStorage; kept for tooling"` (`apps/settings/base.py:374`).

### What actually happens

Every attempt to download the file you just uploaded is a 500 with no message:

```text
GET /v1/import/batches/12/download/  ->  500
NotImplementedError: This backend doesn't support absolute paths.
```

An operator whose import failed validation cannot retrieve the file to see what
they sent. The route has never worked since media moved to the database.

### The fix

Stream through the storage API, which works on any backend:

```python
f = batch.file.open("rb")
return FileResponse(f, content_type=..., as_attachment=True,
                    filename=batch.original_filename)
```

Drop the `os.path.exists` check and use `batch.file.storage.exists(batch.file.name)`
if a 404 is still wanted. Then grep the rest of the platform for `.path` on a
`FileField` - this is the kind of assumption that is rarely made only once.

---

## 3. "Platform users see all" is false

**High. Confirmed by execution.**

### The defect

The scoping mixin deliberately returns `None` for a platform caller so that no
tenant filter is applied:

```python
# views.py:98-111
class SchoolContextMixin:
    """
    - School-tenant users: always scoped to their own asserted tenant (request.tenant).
    - Platform-tenant users: unscoped (see every tenant's data).
    """
    def scope_tenant(self):
        if _is_platform(self.request.user):
            return None
        return getattr(self.request, "tenant", None)
```

and the queryset honours it:

```python
# views.py:341-345
tenant = self.scope_tenant()
queryset = ImportBatch.objects.select_related(...)
if tenant is not None:
    queryset = queryset.filter(tenant=tenant)
```

But `ImportBatch.objects` is a `TenantAwareManager` (`models.py:288`), which
adds `tenant = <ambient tenant>` to every queryset built through it
(`vs_rbac/managers.py:103-118`), and `TenantJWTAuthentication` sets that ambient
tenant on every authenticated request (`vs_rbac/authentication.py:146`). So the
filter the view carefully declines to apply is applied anyway, one layer down.

The same shape is in `ImportBatchContextMixin.get_import_batch`
(`views.py:126-137`) and `ImportBatchDetailView.get_queryset`
(`views.py:409-424`).

### What actually happens

Ada is a CX super admin on the `codex` tenant. Corona Secondary School uploads
a bank statement batch and asks support for help with it.

```text
GET /v1/import/batches/?tenant=codex   ->  200, 0 rows
```

Reproduced: a super admin listing batches while one exists on a school tenant
gets an empty list. The batch is invisible, and so is its detail page - which
404s rather than 403s, so there is nothing to tell Ada the row exists.

This one **fails closed**, which is why nobody has noticed. It is still a
defect: the code says one thing, does another, and the support flow the mixin
was written for does not work.

### The fix

Pick one and make it true. If cross-tenant reach is intended, read through
`ImportBatch.all_objects` on the platform path (the permission class already
does exactly this at `permissions.py:36`). If it is not intended, delete the
`_is_platform` branch and the docstring that promises it, and let the ambient
manager be the single scoping mechanism.

Either way, `platform_cross_tenant_param` is not set on any view in this app,
so a CX caller cannot even assert a school's slug to reach its batches. That
has to be part of whichever answer is chosen.

---

## 4. The stuck-job safety net has never run

**High. Confirmed by execution.**

### The defect

`ImportBatch` has no `school` field. It has a `tenant` foreign key
(`models.py:214`) and a `school` **property** that reads
`tenant.school_profile` (`models.py:311-313`). Two Celery tasks try to
`select_related` through it:

```python
# tasks.py:466-473  (mark_stuck_import_jobs_task)
stuck_jobs = ImportJob.objects.filter(
    status=ImportJobStatusChoices.RUNNING,
    started_at__lt=cutoff,
).select_related(
    "import_batch",
    "import_batch__school",      # <- not a field
    "import_batch__template",
)
```

```python
# tasks.py:281-286  (rollback_import_job_task)
job = ImportJob.objects.select_related(
    "import_batch",
    "import_batch__school",      # <- not a field
    ...
).get(id=job_id)
```

Compiling either query raises:

```text
FieldError: Invalid field name(s) given in select_related: 'school'.
Choices are: tenant, branch, uploaded_by, template, bank_statement_context
```

`mark_stuck_import_jobs_task` is scheduled every thirty minutes
(`apps/celery.py:31-34`).

### What actually happens

An import worker is killed mid-run - a deploy, an OOM, a lost broker
connection. The `ImportJob` stays `RUNNING` forever and the batch stays
`IMPORT_RUNNING` forever. `StartImportSerializer` then refuses any new run with
"An import job is already running for this batch."
(`serializers.py:899-903`), so **the batch is permanently stuck and cannot be
restarted by anyone**.

The task written to prevent exactly that has raised a `FieldError` on all 48 of
its daily runs since the `school` foreign key was replaced by a property. The
failures are invisible unless somebody reads the worker log.

`rollback_import_job_task` is dead in the same way, though nothing calls it
today - `RollbackImportJobView` runs the rollback synchronously in the request
(`views.py:874-878`).

### The fix

Delete `"import_batch__school"` from both `select_related` calls. Nothing needs
it: `create_import_audit_log` takes `school=job.import_batch.school`, which is
the property, and a property cannot be select-related in the first place.

Then add the test that would have caught it: one call to each task with a row
in the database.

---

## 5. A partly failed import is recorded as SUCCEEDED and can never be retried

**High.**

### The defect

```python
# services/import_executor.py:714-717
if failed_rows > 0 and succeeded_rows > 0:
    job.status = ImportJobStatusChoices.SUCCEEDED
    import_batch.status = ImportBatchStatusChoices.IMPORT_PARTIAL
```

The batch correctly says `import_partial`. The job says `succeeded`. And the
guard on starting a new run reads the job, not the batch:

```python
# serializers.py:905-911
if ImportJob.objects.filter(
    import_batch=import_batch,
    status=ImportJobStatusChoices.SUCCEEDED,
).exists():
    raise serializers.ValidationError(
        "This batch has already been imported successfully. Re-importing is not allowed."
    )
```

### What actually happens

CodeX imports 500 schools from a spreadsheet. 497 land. Three fail because a
`subscription_expires_at` cell reads `31/12/2026` and the serializer wants
`2026-12-31`.

The operator fixes the three cells and presses Start Import again:

> This batch has already been imported successfully. Re-importing is not allowed.

The batch shows **Import Partial**. The message says **successfully**. There is
no retry-failed-rows action anywhere in the module, so the only way forward is
to build a new file containing just those three rows and upload it as a new
batch - which means finding out which three rows failed, from a job detail page
that returns every row result unpaginated.

### The fix

Three separate changes, all small:

1. Add `PARTIAL` to `ImportJobStatusChoices` and use it, so job and batch agree.
2. Make the re-import guard read the **batch** status
   (`IMPORT_SUCCEEDED` blocks; `IMPORT_PARTIAL` and `IMPORT_FAILED` do not).
3. When re-running, skip rows that already have a `CREATE`/`UPDATE` result on a
   prior job for the same batch, keyed by `row_number`. The
   `unique_together = ("job", "row_number")` on `ImportJobRowResult`
   (`models.py:816`) is per job, so the history is already there to read.

---

## 6. Every optional template column missing from the file is a hard error

**High.**

### The defect

The header comparison never looks at `is_required`:

```python
# services/template_validation.py:29-40
missing = expected_set - uploaded_set
extra = uploaded_set - expected_set

for header in sorted(missing):
    issues.append({
        "severity": "error",
        "code": "column_missing",
        "message": f"Required template column '{header}' is missing.",
        "column_name": header,
    })
```

Every column the template declares is treated as required, and the message
asserts it. `ImportTemplateColumn.is_required` (`models.py:519`) is consulted
only inside `validate_row_against_template` (`services/template_validation.py:66`),
which runs per **value**, not per **column**.

Since `is_ready_for_import` is `error_count == 0`
(`services/validation_service.py:715`), one absent optional column blocks the
whole import.

### What actually happens

The seeded schools template declares around thirty columns, most of them
optional: `motto`, `website`, `registration_id`, `branch_state`,
`school_admin_phone`, `subscription_expires_at`.

CodeX prepares a file for eight schools that have no website and no motto, and
deletes those two columns. Validation returns:

```text
ERROR  column_missing  Required template column 'Motto' is missing.
ERROR  column_missing  Required template column 'Website' is missing.
```

The import is blocked on two fields nobody was ever going to fill in. The only
way through is to add the columns back and leave every cell blank - which is
what everybody eventually learns to do, and which makes the `is_required` flag
pointless.

### The fix

```python
for col in expected_columns:
    if col.column_name in uploaded_set:
        continue
    issues.append({
        "severity": "error" if col.is_required else "warning",
        "code": "column_missing",
        "message": (f"Required template column '{col.column_name}' is missing."
                    if col.is_required else
                    f"Optional template column '{col.column_name}' is not present; "
                    "its values will be left blank."),
        "column_name": col.column_name,
    })
```

`expected_columns` is already fetched on the line above
(`services/template_validation.py:23`), so this costs nothing.

---

## 7. Nothing is ever notified

**High.**

### The defect

`ImportNotification` rows are created at three points - validation finished
(`tasks.py:93-99`), import finished (`tasks.py:180-189`), rollback finished
(`tasks.py:300-306`) - each with a `title`, a `body` and a `recipient`.

The task that "sends" them does not:

```python
# tasks.py:362-386
def send_import_notification_task(self, notification_id: str) -> dict:
    """
    Send one notification.

    For now, this only marks the notification as sent.
    Later you can connect:
    - email
    - websocket
    - in-app push
    """
    ...
    notification.status = NotificationStatusChoices.SENT
    notification.sent_at = timezone.now()
```

`vs_import_data` contains no import of `vs_notifications`, no
`NotificationService`, and no mail call of any kind. The platform's actual
notification engine - with its channel resolution, its templates and its in-app
feed - is never reached.

A beat entry runs every five minutes to hand PENDING rows to that task
(`apps/celery.py:23-26`), so the system's steady state is a table of messages
marked delivered that were never delivered.

### What actually happens

Ada uploads 400 schools at 16:40 and closes the tab. The import runs for
eighteen minutes and finishes at 16:58 with 12 failed rows.

Nothing reaches her. No email, no in-app badge, nothing on her phone. The
`ImportNotification` row says `status = sent, sent_at = 16:58`, and the only
way she learns the import finished is by opening the batch page and refreshing.

### The fix

`vs_notifications` already has everything needed. Register three event types
(`import.validation.completed`, `import.job.completed`,
`import.rollback.completed`), dispatch through `send_notification` from
`send_import_notification_task`, and set `status`/`error_message` from what it
returns. Keep `ImportNotification` as the module's own delivery log, which is
what it is shaped like.

Note the tenant caveat while doing it: `vs_notifications` files a row under the
*initiating* tenant and the feed filters by tenant
(`notification_code_issues.md` §1), which matters here because a CX-run schools
import notifies a CX user about work done on no tenant at all.

---

## 8. Tenant-scoped import keys reach platform-level operations

**High.**

### The defect

Every `import.*` permission is registered as `PermissionScope.TENANT`
(`core/management/commands/seed_import_permissions.py:145`), meaning any
tenant's role may hold it - correctly, since schools are meant to import their
own data one day.

But of the four dataset types the module ships, three are platform operations:

| Dataset type | What importing a row does |
|---|---|
| `schools` | Creates a new `School` **and a new `Tenant`** via `SchoolCreateSerializer` |
| `branches` | Creates a `Branch` and provisions a branch-admin `User` with a role |
| `cx_users` | Creates a **CodeX platform staff account** on the `codex` tenant |
| `bank_statements` | The one genuinely tenant-facing type - and it has its own key |

And nothing ties a dataset type to who may import it. `_is_platform` is checked
in exactly two places, both about *authoring templates*
(`views.py:210-211`, `views.py:252-253`). Neither
`ImportBatchListCreateView` (upload) nor `StartImportBatchView` (execute)
checks it at all.

The CX-users handler names the target tenant outright, whoever queued the
batch:

```python
# services/import_executor.py:136-138
platform_tenant = Tenant.objects.filter(
    slug='codex', kind=Tenant.Kind.PLATFORM,
).first()
```

and the comment above it says so: "This handler names the platform (codex)
tenant as the target regardless of who queued the batch".

### What actually happens

Corona Secondary School's IT lead is given `import.batches.create` and
`import.batches.import` so the school can load its own bank statements. Both
keys are TENANT-scoped, so the grant is legal and nothing refuses it.

He lists templates, sees the three platform templates (the list endpoint only
filters on ACTIVE and download-enabled, `views.py:196-200`), downloads
`cx_users_master`, fills in one row, uploads and imports. A CodeX platform staff
account is created on the `codex` tenant from a school admin's spreadsheet.

The blast radius is limited by one thing: `import_cx_users_row` submits the user
for approval rather than activating them
(`services/import_executor.py:184-187`), so somebody at CodeX has to approve.
The `schools` template has no such backstop - `SchoolCreateSerializer` creates a
live school and tenant immediately.

### The fix

Two layers, both needed:

1. **Gate the dataset type, not just the key.** Add a
   `requires_platform_actor` flag to `ImportTemplate` (or derive it from
   `dataset_type`), and refuse upload *and* execution for a non-platform caller
   on any template that carries it. Filter those templates out of the list for
   school users too, so the id is not discoverable.
2. **Split the keys.** `import.batches.import` covering both "load my bank
   statement" and "create tenants on the platform" is one key doing two jobs.
   The platform datasets deserve `platform.import.*` keys with
   `PermissionScope.PLATFORM`, which `assert_tenant_may_hold`
   (`vs_rbac/models.py:91-110`) then refuses to attach to a school role.

---

## 9. "Mark as resolved" cannot unblock an import

**Medium.**

### The defect

`ImportValidationIssue.is_resolved` has a dedicated endpoint
(`PATCH /batches/<id>/issues/<id>/resolve/`, `views.py:686-715`), a serializer
that stamps `resolved_at` and `resolved_by` (`serializers.py:340-358`), a list
filter (`views.py:660-666`), a column in the CSV export
(`services/template_file.py:130`) and an audit event.

Nothing reads it. Grepping the whole repo, `is_resolved` appears only in the
model, the serializer that sets it, the filter that queries it and the CSV that
prints it. The gate that decides whether an import may run does not:

```python
# services/validation_service.py:707-716
error_count = summary["error_count"]
import_batch.has_critical_errors = error_count > 0
import_batch.is_ready_for_import = error_count == 0 and import_batch.total_rows > 0
```

`summary` comes from `summarize_issues(issues)` over the freshly computed list,
before any of them is saved - so resolution state cannot enter the calculation
even in principle.

And re-validating wipes it:

```python
# services/validation_service.py:31
import_batch.validation_issues.all().delete()
```

### What actually happens

An operator sees `WARNING column_unknown: Uploaded column 'Notes' is not part of
the official template`, decides it does not matter, and clicks Resolve. The
issue shows a tick. The batch is still blocked, because the block was never
about that warning.

Then they fix the real error, re-validate, and the tick is gone - the row was
deleted and a new one created.

### The fix

Decide what the flag means and then make it mean that:

- **If it is an acknowledgement**, say so in the UI and leave the gate alone,
  but stop deleting acknowledged rows on re-validation: match on
  `(row_number, column_name, code)` and carry `is_resolved` forward.
- **If it is an override**, restrict it to WARNING severity, exclude resolved
  issues from `error_count`, and require the resolver to hold something
  stronger than `import.validations.update` (a NORMAL, unrestricted key today).

An error an operator can wave through by clicking a button is not a gate, so the
second reading needs care.

---

## 10. The entire file body is stored on the batch row and returned by the detail endpoint

**Medium.**

### The defect

The field is named and commented as a sample:

```python
# models.py:273
preview_rows = models.JSONField(default=list, blank=True)  # First N parsed rows, stored for validation service use
```

It holds every row:

```python
# serializers.py:835-836
validated_data["preview_rows"] = preview_rows
validated_data["total_rows"] = len(preview_rows)
```

where `preview_rows` is the complete second element of `parse_import_file`, up
to `MAX_IMPORT_ROWS = 50_000` (`services/file_parser.py:13`). The file limit is
50 MB (`serializers.py:739`).

So a 50,000-row spreadsheet is duplicated into a single JSONB column, and every
consumer loads all of it: validation reads it six times over
(`services/validation_service.py:105, 134, 160, 238, 488, 658`), the executor
reads it (`services/import_executor.py:577`), and the detail serializer
**returns** it:

```python
# serializers.py:636
"preview_rows",
```

alongside `validation_issues` and `notifications`, both nested with `many=True`
and neither paginated (`serializers.py:609-610`).

The field-level security entry protecting it is a no-op:

```python
# serializers.py:598-601
read_permissions = {
    "file": ImportPermission.BATCH_VIEW,
    "preview_rows": ImportPermission.BATCH_VIEW,
}
```

`BATCH_VIEW` is the key the endpoint itself already requires
(`views.py:406`), so everyone who can reach the response can read the fields.

### What actually happens

A 50,000-line bank statement is validated and produces two issues per row.
`GET /v1/import/batches/12/` then returns 50,000 rows of statement lines and
100,000 validation issues in one JSON document - hundreds of megabytes,
serialized in a request thread.

For a `cx_users` batch the same field holds every prospective staff member's
name, email address and phone number, returned in full to anyone holding a
NORMAL, unrestricted key.

### The fix

1. Genuinely truncate `preview_rows` to the first 20-50 rows for display, and
   re-parse the stored file for validation and execution. The file is already
   kept and `parse_import_file` already takes a file object.
2. If keeping the full parse is preferred for cost reasons, move it to its own
   table (or a second column) and remove it from the detail serializer; a
   preview endpoint with `?limit=` is the right shape.
3. Paginate `validation_issues` out of the detail payload - there is already a
   dedicated list endpoint for them (`views.py:638-668`).
4. Point the FLS entries at a key that is not the endpoint's own, or drop them
   and stop implying protection that is not there.

---

## 11. Execution costs four writes and one query per row

**Medium. Efficiency.**

### The defect

Inside the per-row loop (`services/import_executor.py:588-679`), every row
performs:

| Work | Where |
|---|---|
| One query for the template's columns | `map_row_to_payload` (`:43`) - `import_batch.template.columns.order_by(...)` builds a fresh queryset each call |
| One `INSERT` into `ImportJobRowResult` | `create_row_result` (`:521`) |
| One `AuditEvent` insert | `create_import_audit_log` (`:622`) |
| One `UPDATE` of the whole job row | `update_job_progress` (`:672`, saving six fields) |

The validation layer explicitly avoids the first of these and says why -
"Columns are fetched once and passed to each row validator to avoid N+1
queries" (`services/validation_service.py:97-98`, `:106`). The executor does
not.

### What actually happens

A 50,000-row import issues at least 200,000 database round trips, of which
50,000 are updates to a single row for a progress bar, and 50,000 are audit
events recording that row *n* of a file succeeded. The audit trail for one
import is larger than most modules' entire history.

### The fix

- Hoist `columns = list(import_batch.template.columns.order_by("column_order"))`
  out of the loop and pass it to `map_row_to_payload`.
- Update progress every N rows (say 1%, or every 100) plus once at the end.
- Batch the row results with `bulk_create` in chunks.
- Emit **one** audit event per job with the counts, and keep the per-row detail
  in `ImportJobRowResult`, which is what that table is for. If a per-row audit
  trail is genuinely required, say so and accept the cost deliberately.

---

## 12. Cross-reference validation is effectively unscoped across tenants

**Medium.**

### The defect

```python
# services/validation_service.py:675-682
qs = model_class.objects.all()

field_names = {f.name for f in model_class._meta.get_fields()}
if "school" in field_names and import_batch.school_id:
    qs = qs.filter(school_id=import_batch.school_id)
elif "branch" in field_names and import_batch.branch_id:
    qs = qs.filter(branch_id=import_batch.branch_id)
```

Both narrowing paths are close to unreachable:

- **`"school" in field_names`** - the platform moved off school-owned rows;
  the site primitive is `vs_tenants.Branch`, owned by `Tenant`. Very few models
  still carry a `school` field, and none of the ones a template is likely to
  reference.
- **`import_batch.branch_id`** - never set on any batch. See §18.

So in practice the valid-value set is `model_class.objects.all()`. Whether that
leaks depends entirely on whether the referenced model happens to have a
tenant-aware manager, and on whether validation is running inside a request
(where an ambient tenant exists) or inside `validate_import_batch_task` (where
one does not).

### What actually happens

A template column references `Branch` by `name`. `Branch.objects` is a
`TenantAwareManager` (`vs_tenants/models.py:385`), so:

- **Validated from the UI**, the ambient tenant is set and the set is correctly
  scoped.
- **Validated from the background task**, there is no ambient tenant, so the
  set is every branch on the platform. A row naming "Lekki Annexe" - which
  belongs to a different school entirely - validates clean, and then fails at
  execution or, worse, resolves to the wrong record.

For any referenced model *without* a tenant-aware manager, both paths are
unscoped.

### The fix

Scope explicitly on the batch's tenant rather than hoping a manager does it:

```python
if "tenant" in field_names:
    qs = model_class.all_objects.filter(tenant_id=import_batch.tenant_id)
elif "branch" in field_names:
    qs = model_class.all_objects.filter(branch__tenant_id=import_batch.tenant_id)
else:
    continue   # unscopable reference: refuse rather than compare platform-wide
```

and delete the `school_id` branch, which is vocabulary this app should not be
using anyway (§14).

---

## 13. `_resolve_model` will resolve any model in the platform by class name

**Medium.**

### The defect

```python
# services/validation_service.py:634-644
def _resolve_model(name: str):
    from django.apps import apps
    name_lower = name.lower()
    for model in apps.get_models():
        if model.__name__.lower() == name_lower:
            return model
    return None
```

`name` is `ImportTemplateColumn.reference_model`, a free-text field
(`models.py:536-540`), and `reference_lookup_field` next to it is likewise free
text fed straight into `values_list` (`services/validation_service.py:685`).

Two consequences:

1. **First match wins across every installed app.** `apps.get_models()` has no
   defined order guarantee across apps, and model names repeat on this platform
   (`TimeStampedModel` subclasses named `Invoice`, `Template`, `Branch` and
   `Alert` exist in more than one place). A template that says
   `reference_model = "Invoice"` may resolve to a different app's `Invoice`
   between deploys.
2. **It is an arbitrary-column read.** `reference_model = "User"` with
   `reference_lookup_field = "password"` loads every user's password hash into a
   Python set and compares row values against it. The `try/except Exception:
   continue` on the line below (`:686-687`) means a bad field name is silent.

Only platform staff can author templates (`views.py:210-211`), which is why
this is Medium and not High. It is still a model-and-column read driven by a
string in a database row.

### The fix

Replace the name search with an allow-list of `(app_label, model, lookup_field)`
triples that templates may reference, keyed by a short token stored in
`reference_model`. Validate the token at template-write time and reject anything
outside it, so a bad reference fails when the template is authored rather than
silently doing nothing during a validation run.

---

## 14. An engine app imports `schools.vs_schools`

**Medium.**

### The defect

`vs_import_data` lives in `apps/`, which makes it an engine app, and engine apps
must not import anything under `apps/schools/`. It does, in five places:

```python
# services/validation_service.py:233
from schools.vs_schools.models import RESERVED_TENANT_SLUGS, PackagePlan, School
# services/validation_service.py:483
from schools.vs_schools.models import School
# services/import_executor.py:247-248
from schools.vs_schools.models import School
from schools.vs_schools.serializers import SchoolCreateSerializer
# services/import_executor.py:399-400
from schools.vs_schools.models import School
from schools.vs_schools.serializers import BranchCreateSerializer
# services/rollback_service.py:19
from schools.vs_schools.models import School
```

It is not incidental. `_validate_schools_rules` is 250 lines of school-specific
business logic (`services/validation_service.py:229-479`) reimplementing
`SchoolCreateSerializer`'s rules; `_validate_branches_rules` is another 150
(`:482-631`). Three of the four `DatasetTypeChoices` are school vocabulary
(`models.py:48-52`), and `ImportBatch` exposes a `school` property
(`models.py:311-317`) that every service and view reads.

### What actually happens

VIGIL (`vs_health`'s domain sibling) or the next domain gets a generic import
pipeline whose validators, executors and rollback all know only about schools,
and whose dataset-type enumeration has no room for their data. Adding a
`patients` dataset means editing an engine's constants, its executor's routing
table and its rollback.

### The fix

The FAL pattern this platform already uses. `vs_import_data` should own the
mechanism - upload, parse, validate against a template, execute row by row,
record results, roll back - and know nothing about what a row *is*. Dataset
handlers register themselves from their own app:

```python
# in schools/vs_schools/apps.py ready()
register_import_dataset("schools", validator=..., executor=..., reverser=...)
```

That also fixes §1 for free, because the reverser arrives from the same place
as the executor and cannot be missing.

This is the largest item in this file and the one least likely to be done
soon. Recording it accurately matters more than scheduling it.

---

## 15. A retired or draft template can still be used for an upload

**Medium.**

### The defect

The list and detail endpoints hide non-active templates from school users:

```python
# views.py:196-200
if self.request.method == "GET" and not _is_platform(self.request.user):
    queryset = queryset.filter(
        status=TemplateStatusChoices.ACTIVE,
        is_download_enabled=True,
    )
```

The upload path does not check `status` at all:

```python
# serializers.py:751-761
template = ImportTemplate.objects.prefetch_related("columns").get(id=value)
...
if not template.is_download_enabled:
    raise serializers.ValidationError("This template is not available for use.")
```

`TemplateStatusChoices.RETIRED` exists, `retired_at` is stamped on transition
(`serializers.py:262-263`), and no code refuses a retired template.

### What actually happens

CodeX retires `schools_master_v1` because the column set changed, and publishes
`schools_master_v2`. Corona's admin still has last term's tab open, or a saved
link, with `template_id=3`. The upload is accepted against the retired
template, validates against its old column set, and imports rows mapped by
target fields the new pipeline no longer expects.

Nothing tells them the template is retired - it is not in the list they can see,
so there is no way to find out except by the import behaving oddly.

### The fix

Add the status check where the template is resolved:

```python
if template.status != TemplateStatusChoices.ACTIVE:
    raise serializers.ValidationError(
        f"This template is {template.get_status_display().lower()} and can no "
        "longer be used. Download the current template for this dataset."
    )
```

`get_active_template_by_dataset` (`services/template.py:6-16`) already filters
on ACTIVE, so the two paths would finally agree.

---

## 16. Editing a template's columns deletes and re-creates them

**Medium.**

### The defect

```python
# serializers.py:270-275
if columns_data is not None:
    instance.columns.all().delete()
    ImportTemplateColumn.objects.bulk_create([
        ImportTemplateColumn(template=instance, **col)
        for col in columns_data
    ])
```

`ImportTemplateCreateSerializer.create` does the same on the way in
(`serializers.py:222-226`).

Three things follow:

1. **`bulk_create` never calls `save()` or `full_clean()`**, so
   `ImportTemplateColumn.clean` (`models.py:558-563`) - the guard that
   `allowed_values` must be a list of strings - never runs. A column can be
   created with `allowed_values: {"a": 1}`, and `validate_choice` then compares
   a cell against a dict.
2. **`ImportTemplate.clean`** ("An active template must have at least one
   column", `models.py:437-439`) is likewise never called, so a template can be
   ACTIVE with zero columns. Uploading against it produces a
   `column_unknown` warning for every header in the file and no errors, so
   `is_ready_for_import` is `True` and the executor maps every row to an empty
   payload.
3. **Column identity is destroyed.** Any batch still in flight validated its
   headers against the old column set and stored
   `template_headers_snapshot` from it (`serializers.py:800-802`). After the
   edit, re-validating the same batch compares its file against a different
   contract with no warning that the template changed underneath it.

### The fix

Replace the delete-and-recreate with an upsert keyed on `column_name`, calling
`full_clean()` per row; call `template.full_clean()` before saving a status
change to ACTIVE; and refuse a column edit outright when any batch referencing
the template is in a pre-import state, or version the template instead (the
`code` field is shaped for `..._v1` / `..._v2` already).

---

## 17. `.xls` is advertised in three places and cannot be read

**Medium.**

### The defect

`FileFormatChoices` offers it (`models.py:45`), the upload validator accepts it
(`serializers.py:745`), and the parser routes it to the Excel branch:

```python
# services/file_parser.py:125-126
elif file_format in ("xlsx", "xls"):
    return parse_xlsx(file_obj, sheet_name=sheet_name, header_row_index=header_row_index)
```

`parse_xlsx` uses `openpyxl.load_workbook` (`services/file_parser.py:76`), and
openpyxl reads `.xlsx`/`.xlsm` only - it raises `InvalidFileException` on the
legacy binary `.xls` format. There is no `xlrd` or equivalent anywhere in the
project.

The failure is caught by the broad handler at `serializers.py:819-822` and
surfaces as:

> Could not read file: openpyxl does not support the old .xls file format,
> please use xlrd to read this file, or convert it to the more recent .xlsx
> file format. Ensure the file is not corrupted and matches the selected format.

### What actually happens

An administrator on an older Excel saves as `.xls`, uploads, and is told the
file is corrupted or the wrong format - after the platform accepted the
extension twice on the way in. The advice in the message is addressed to a
developer, not to them.

### The fix

Cheapest: remove `XLS` from `FileFormatChoices`, from `allowed_extensions`
(`serializers.py:745`) and from the parser dispatch, and refuse it at upload
with "Save this file as .xlsx or .csv and upload again." Alternatively add
`xlrd` and a real `parse_xls`. Either is fine; advertising it and failing at
parse time is not.

---

## 18. `branch` is never set on any batch

**Medium.**

### The defect

```python
# serializers.py:825
validated_data["branch"] = self.context.get("branch")
```

Nothing puts `"branch"` into the serializer context.
`ImportBatchListCreateView` does not override `get_serializer_context`
(`views.py:324-386`), so the default DRF context is `request`, `view` and
`format`. `.get("branch")` is `None` on every upload the API has ever accepted.

What that leaves stranded:

| Thing | Where |
|---|---|
| `ImportBatch.branch` FK | `models.py:219-225` |
| The `(branch, status)` index | `models.py:298` |
| The branch scope in the storage path | `models.py:146-147` |
| `branch=instance.branch` on eight audit calls | `views.py:378`, `:441`, `:466`, `:539`, `:620`, `:707`; `services/import_executor.py:624`, `:747` |
| The branch preference in audit tenant resolution | `services/audit_service.py:77-81` |
| The `branch_id` narrowing in cross-reference validation | `services/validation_service.py:681-682` |

### What actually happens

A multi-branch school cannot say which branch an import belongs to, cannot
filter the batch list by branch, and cannot tell two branches' uploads apart.
Every stored file lands under `imports/<tenant-slug>/...` rather than the
branch path the helper was written to produce, and every audit event resolves
its tenant through the school fallback rather than the branch it was told to
prefer.

Per the platform's own rule, a school with one branch should not see a branch
control at all - so this is only visible on a multi-branch school, which is
exactly the shape least likely to be tested.

### The fix

Accept `branch` on `ImportBatchUploadSerializer` as an optional id, resolve it
through `vs_tenants.references.find_branch_in_tenant` (which collapses
not-a-number, does-not-exist and belongs-to-someone-else into one `None`, so it
cannot be used as an id oracle), and refuse a branch outside the request's
tenant. Add `?branch=` to the batch list filter. Where a school has one branch,
leave the control out.

---

## 19. Validation and execution disagree about what a duplicate is

**Medium.**

### The defect

Validation resolves the slug the way the serializer will:

```python
# services/validation_service.py:267-269
raw_slug = _s("slug")
raw_name = _s("name")
resolved_slug = slugify(raw_slug) if raw_slug else slugify(raw_name)
```

then checks `effective_slug in existing_slugs`
(`services/validation_service.py:289`).

Execution checks the raw cell:

```python
# services/import_executor.py:262
if slug and School.objects.filter(slug=slug).exists():
```

with no `slugify`. It also falls back to matching on `name`
(`:269`), which validation never considers.

### What actually happens

A row has `Slug = "Green Field"`. Validation slugifies it to `green-field`,
finds an existing school with that slug, and reports
`duplicate_record` with helpful suggestions (`green-field-2`, ...). Good.

Now the reverse: the operator clears the Slug cell and leaves
`Name = "Green Field"`. Validation slugifies the name, sees `green-field`
exists, and blocks - correct. But if a school exists whose slug is
`green-field` under a *different* name, execution's `not slug and name`
fallback checks `School.objects.filter(name="Green Field")`, finds nothing, and
proceeds - `SchoolCreateSerializer` then slugifies and hits the unique
constraint, so the row is recorded FAILED with a raw database error rather than
the readable duplicate message and the suggestions validation would have given.

### The fix

Have both sides call one helper. `_validate_schools_rules` already reproduces
the serializer's slug resolution in comments and code
(`services/validation_service.py:266-278`); extract it, and have
`import_schools_row` use the same function for its duplicate check. That is the
same class-fix as §14 in miniature: one rule, one implementation.

---

## 20. A row whose administrator could not be provisioned is still counted as a success

**Medium.**

### The defect

`import_schools_row` returns `CREATE` as soon as `SchoolCreateSerializer.save()`
returns (`services/import_executor.py:356-361`). That serializer calls
`provision_admin_user` for the school admin and the branch admin
(`schools/vs_schools/serializers.py:558`, `:1066`, `:1125`), and that service
**logs and swallows** its own failures
(`schools/vs_schools/services/admin_provisioning.py:174`), leaving the admin
link in `QUEUED`.

So the executor cannot see it. The row result says
`"School created successfully."`, the job counts it in `succeeded_rows`, and
the batch reports IMPORT_SUCCEEDED.

### What actually happens

Visible on every run of the module's own test suite. A green
`Ran 18 tests` prints eight of these:

```text
provision_admin_user: failed for ada@import-bare.test - Refusing to provision
ada@import-bare.test without a role assignment. Expected TenantRoleTemplate
key='' on tenant import-bare.
```

The tests that produce them assert only that the *link* row carries the right
job title (`tests.py:413-419`, `:477`); none asserts that the administrator has
an account. So the suite cannot distinguish "school imported with a working
admin" from "school imported with an admin who does not exist".

In the test database the cause is that no prebuilt role templates are seeded,
which makes `provision_role_from_prebuilt` return `None` and the role key `""`
(`schools/vs_schools/serializers.py:563`). A properly seeded environment does
not hit it. But the *reporting* problem is not environmental: whenever
provisioning fails for any reason, the import says the row succeeded and
nobody is told that a school now exists whose administrator cannot sign in.

### The fix

Have `provision_admin_user` return an outcome rather than swallowing, and have
`import_schools_row` / `import_branches_row` surface it - either as a FAILED row
(if a school without an administrator is unusable, which it is) or as a CREATE
with a warning recorded in `ImportJobRowResult.error_details` and reflected in
the job summary. Then add a test that asserts an imported school's admin can
authenticate.

---

## 21. The execution task auto-retries a non-idempotent operation

**Medium.**

### The defect

```python
# tasks.py:145-151
@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def execute_import_batch_task(self, import_batch_id, queued_by_id=None):
```

The except block sets the running job to FAILED and the batch to IMPORT_FAILED
(`tasks.py:221-240`) and then `raise`s (`:260`), which triggers the retry. The
retry re-enters `execute_import`, which checks only `is_ready_for_import` -
still `True`, because nothing clears it - and starts a **new** `ImportJob` that
re-runs every row from the top.

`validate_import_batch_task` has the same decorator with `max_retries=2`
(`tasks.py:53-58`) and appends a line to `import_batch.notes` on each failure
(`tasks.py:112`).

### What actually happens

A 400-row schools import dies on row 380 because the database connection drops.
The retry runs all 400 rows again. Rows 1-379 are caught by the handlers' own
duplicate checks and come back as SKIP, so the damage is limited - but the
batch now has two `ImportJob` rows, the second reporting 379 skipped and 21
created, and the first stuck at FAILED with partial counters. Reconstructing
what actually happened means reading both jobs' row results side by side.

For `cx_users` the duplicate check is `email_refusal`
(`services/import_executor.py:156`), so a retry that runs after the first
attempt created a pending user correctly skips. There is no dataset where the
retry is *safe by design* - only datasets where a guard happens to catch it.

### The fix

Drop `autoretry_for` from the execution task. An import is a long, multi-write,
user-visible operation; the right response to a failure is a visible failed job
the operator can retry deliberately once §5 makes retrying possible. Keep the
retry on `send_import_notification_task`, which is genuinely idempotent.

---

## 22. No school role is granted any `import.*` key out of the box

**Low.**

`seed_import_permissions` grants every import key to `xvs_super_admin` and only
the `import.templates.*` keys to `xvs_platform_admin`
(`core/management/commands/seed_import_permissions.py:161-166`). No
`PrebuiltRolePermission` anywhere grants an import key to a school role - the
module's own tests build one by hand
(`tests.py:77-79`).

So a school admin gets 403 on every route in this module unless someone
constructs a role for them. Given §8 - that three of four dataset types are
platform operations a school should not be running - that is arguably the
correct default today, and the honest fix is to grant `import.batches.*` to a
school role **at the same time** as gating the dataset types, not before.

Recorded here so that whoever does §8 does not treat the missing grant as an
oversight to be fixed on its own.

---

## 23. Smaller defects and dead code

**Low.** Each is a line or two.

1. **The template-create endpoint returns a different envelope.** Every other
   response in the platform is `{"success": true, "message", "data"}`
   (`core/response.py:6-11`); this one hand-builds
   `{"status": "success", "message", "data"}` (`views.py:226-229`). Any client
   reading `body.success` sees `undefined` on exactly one route.
2. **`get_active_template_by_dataset` uses `.get()` with nothing enforcing
   uniqueness.** The model docstring says "Only one template per dataset type is
   expected to be active at a time" (`models.py:339-341`) and no constraint
   backs it, so a second ACTIVE template makes the call raise
   `MultipleObjectsReturned` - a 500 (`services/template.py:6-16`). Add a
   partial unique index on `(dataset_type)` where `status = active`.
3. **`ImportJob.import_batch` is a `ForeignKey`, documented as a
   `OneToOne`.** "One batch maps to exactly one job (OneToOne)"
   (`models.py:669`) versus `models.ForeignKey(...)` (`models.py:700`). The
   code depends on the FK - re-runs and retries create additional jobs - so the
   docstring is what is wrong.
4. **`ImportBatch.error_count` and `warning_count` are two queries each time
   they are read** (`models.py:324-330`), and both appear in the list
   serializer (`serializers.py:560-561`), so a 25-row page issues 50 extra
   `COUNT` queries. Annotate them on the queryset instead.
5. **`validate_import_batch` is wrapped in `@transaction.atomic`**
   (`services/validation_service.py:740`) and sets
   `status = VALIDATING` inside it (`:754-767`), so that status is never
   externally visible - the row goes straight from its previous state to
   READY_TO_IMPORT or VALIDATION_FAILED.
6. **`structure_matches_template` is set from the total error count**
   (`services/validation_service.py:716`), so a file whose headers match the
   template perfectly reports `False` because one date cell was malformed. The
   field name promises something narrower than it delivers.
7. **`file_parser` is documented as streaming and is not.** "Streaming file
   parser for import batches. Reads CSV and Excel files row by row"
   (`services/file_parser.py:1-4`), while both parsers call `file_obj.read()`
   into memory (`:44`, `:78`) and accumulate a full list of dicts.
   `read_only=True` on `load_workbook` helps openpyxl and not the 50,000-dict
   list.
8. **`dict(zip(headers, values))` silently drops extra cells**
   (`services/file_parser.py:61`, `:101`). A row with more cells than headers
   loses the surplus with no issue raised; a row with fewer simply has missing
   keys, which then read as blank.
9. **`cleanup_old_import_batches_task` misses the states that actually get
   stuck.** It cancels DRAFT, UPLOADED and VALIDATING
   (`tasks.py:433-440`) and ignores MAPPING_REQUIRED, VALIDATION_FAILED and
   READY_TO_IMPORT - so a batch that validated cleanly and was never started
   lives forever, holding its 50 MB file and its 50,000-row JSON payload.
10. **`mark_stuck_import_jobs_task` races the job it is marking.** Even once
    §4 is fixed, it flips a RUNNING job to FAILED without checking whether the
    worker is still alive; `finalize_import_job`
    (`services/import_executor.py:727-734`) will then overwrite the status back
    to SUCCEEDED when the run completes.
11. **`_is_platform` compares against a raw string**, `== "PLATFORM"`
    (`views.py:70`), rather than `Tenant.Kind.PLATFORM`. It works because the
    enum value is that string; it will stop working silently if the value ever
    changes.
12. **`RollbackImportSerializer` hardcodes status strings**
    (`serializers.py:924`) instead of using `ImportJobStatusChoices`.
13. **`MAX_PREVIEW_ROWS` is a backwards-compatible alias nothing imports**
    (`services/file_parser.py:15`), and `validators.py` carries several
    functions no caller uses - `validate_required_columns`,
    `validate_unknown_columns`, `validate_file_not_empty`,
    `validate_sheet_name_provided_for_excel`,
    `find_duplicate_composite_values`, `validate_start_end_order`,
    `validate_min_not_greater_than_max`, `validate_mapping_targets_unique`,
    `validate_required_mappings_present`. The mapping validators are left over
    from a manual column-mapping flow the template-only design replaced.
14. **`ImportTemplateColumn.reference_lookup_field` failures are silent.**
    `except Exception: continue` (`services/validation_service.py:686-687`)
    swallows a bad field name, so a mistyped reference simply validates nothing
    and nobody is told.

---

## Recommended order of work

**Fix immediately - one of these destroys data:**

1. §1 - route the rollback by `target_model` and refuse unknown targets. Until
   this lands, rollback should be disabled for any batch whose dataset type is
   not `schools`.
2. §4 - delete two words from two `select_related` calls and the stuck-job
   safety net starts working.
3. §2 - one line, and the download route works again.

**Then - the flows that do not work:**

4. §5 - partial imports must be retryable.
5. §6 - optional columns must not block an import.
6. §7 - wire notifications into `vs_notifications`.
7. §15 - refuse retired templates at upload.

**Then - the security boundary:**

8. §8 - gate the platform dataset types, split the keys, and grant §22 at the
   same time.
9. §12, §13 - scope cross-reference lookups and replace the model-name search.

**Then - cost and size:**

10. §10 - stop returning the whole file in the detail payload.
11. §11 - hoist the column query, batch the writes, one audit event per job.
12. §23.4, §23.9.

**Then - correctness and honesty of what is recorded:**

13. §9, §19, §20, §16, §18, §21, §17.

**Structural, and not soon:**

14. §14 - registered dataset handlers, so the engine stops importing
    `vs_schools`. Doing this first would make §1, §12 and §19 unnecessary
    rather than fixed, which is worth weighing before spending effort on them
    individually.
