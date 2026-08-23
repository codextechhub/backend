# export_code_issues

Everything wrong with `vs_exports` and the datasets published into it, in one
place, ordered by how much it costs. Each item states the defect, the evidence,
what actually happens to a user, and the fix. The five slice reports
(`export_catalogue_datasets`, `export_builder_definitions`,
`export_runs_and_files`, `export_schedules`, `export_audit_analytics`) point
here rather than repeating it.

Every claim is traced to a file and line. Nothing here is speculative.

**How this was established, and what that is worth.** Every finding comes from
reading the code, and every coverage claim comes from reading `tests.py` - the
suite was **not run to completion** while this was written (see the playbook's
`vs_exports` entry for why and for the command). So "the suite does not catch
this" means "no test in the file exercises this path", which is a claim about
the test code, not a claim about a green run. The suite is large and genuinely
good - it covers cross-tenant 404s, sensitive-column omission, the row cap, the
abandoned-run sweeper, clock changes and every screen translator - which is why
each item below names what a covering test would have to do.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in
the code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | No school role is ever granted an `exports.*` key, so the whole Export Centre is a 403 for every school user out of the box | **High** |
| 2 | Archiving an export does not stop it - its schedule keeps producing a file every night, forever | **High** |
| 3 | The schools dataset reads every school on the platform, and nothing stops a school role holding its key | **High** |
| 4 | Run references are six hex characters and globally unique, so they start colliding at a few thousand runs | **High** |
| 5 | A malformed number filter is a 500 on preview, and on a run it is recorded as a "temporary system problem" with a Retry button | **Medium** |
| 6 | `?definition=` and `?actor=` are raw strings on integer columns, so a typo is a 500 | **Medium** |
| 7 | `exports.activity.view` is a read key that also confers write over everybody's exports | **Medium** |
| 8 | Retry skips the fair-share cap that every other trigger path enforces | **Medium** |
| 9 | The cap's refusal message tells the user the export was accepted | **Medium** |
| 10 | `skip_when_empty` is stored, published and never read | **Medium** |
| 11 | The whole file is assembled in memory, twice, up to the 500,000-row cap | **Medium** |
| 12 | One sensitive-field key unlocks every restricted column in every dataset | **Medium** |
| 13 | The saved-exports and files lists are N+1 by construction | **Medium** |
| 14 | The schedule dispatcher takes no lock, and scheduled runs carry no idempotency key | **Medium** |
| 15 | The idempotency window is a read-then-write race with no constraint behind it | **Medium** |
| 16 | Reading the activity list writes caller-controlled data into the immutable audit trail | **Medium** |
| 17 | The two tests that enforce the domain-neutrality rule error out on Windows instead of running | **Medium** |
| 18 | Smaller defects and dead surface | **Low** |

---

## 1. No school role is ever granted an `exports.*` key

**High. The feature does not exist for the customer.**

### The defect

`seed_exports_permissions` registers all fifteen keys and then grants them to
exactly two roles, both of them on the Codex platform tenant:

```python
# management/commands/seed_exports_permissions.py:29
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
```

```python
# management/commands/seed_exports_permissions.py:126-132
codex = Tenant.objects.filter(slug="codex", kind=Tenant.Kind.PLATFORM).first()
...
for role_id in PLATFORM_ROLE_IDS:
    role, _ = TenantRoleTemplate.objects.get_or_create(tenant=codex, key=role_id, ...)
```

School roles are provisioned by copying `PrebuiltRolePermission` rows for
`school_admin`, `branch_admin` and `teacher`
(`vs_rbac/services.py:54`, `core/management/commands/seed_school_permissions.py`
phase 2). This command writes no `PrebuiltRolePermission` row at all, and
nothing else in `seed_all_permissions`
(`core/management/commands/seed_all_permissions.py:53-69`) adds one either.

### What actually happens

Every view in the app inherits `_ExportBase`, whose
`permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]`
(`views.py:79`) gates on `rbac_permission`. A School Admin at Corona holds no
`exports.*` key, so `GET /v1/exports/catalogue/` is a 403, and so is every other
route. The builder screen, the saved exports, the Files list, quick export from
a module screen - none of it is reachable by anyone at any school, on a fresh
install, until somebody hand-builds a role.

This is the same shape as the `vs_config` finding (`config_code_issues.md` §3),
and it lands harder here: the Export Centre is a *user-facing* feature with a
whole design deliverable behind it, not an admin console.

### The fix

Add school defaults to the seed the way `seed_ticket_permissions` does
(`vs_tickets/management/commands/seed_ticket_permissions.py:17,132`): give
`school_admin` the catalogue, definition, run, file and schedule keys; give
`branch_admin` and `teacher` the read-and-run subset. Keep
`exports.sensitive_field.export` and `exports.activity.view` out of the defaults
- the seed's own docstring is right about those two.

---

## 2. Archiving an export does not stop it

**High. The one thing "delete" is supposed to do.**

### The defect

`DELETE /v1/exports/definitions/<pk>/` archives rather than destroys, for good
reasons the docstring states:

```python
# views.py:404-405
definition.is_archived = True
definition.save(update_fields=["is_archived", "updated_at"])
```

`is_archived` is then read in exactly two places in the whole app: the list
filter (`views.py:331`) and schedule *creation* (`views.py:982`).

It is **not** read by:

* `trigger_run` (`services.py:194`), which checks `is_draft` and nothing else;
* `DefinitionRunView` (`views.py:498`), which resolves the definition through
  `get_definition`, and `visible_definitions` (`views.py:119`) does not exclude
  archived rows;
* `dispatch_due_schedules` (`services.py:867-871`), which selects on
  `state=ACTIVE, next_run_at__lte=now` and never looks at the definition at all.

### What actually happens

A person archives an export - very often *because* it was producing something
it should not - and its schedule fires that night, and every night after, with
no owner watching the Files list any more. The definition is invisible in the
list, so nothing on screen explains where the new files are coming from. The
same export can also still be run by id, and retried, since neither path
consults the flag.

The response even promises the opposite: "Export archived. Files it already
produced stay available until they expire" (`views.py:411`) reads as a
statement that nothing new will be produced.

### The fix

Refuse the run in `trigger_run` when `definition.is_archived`, with the same
shape as the draft guard; and in `dispatch_due_schedules`, skip (and pause, with
a new `PauseReason`) any schedule whose definition is archived. Archiving should
also pause the definition's schedules in the same transaction as the archive, so
the Schedules list explains itself.

---

## 3. The schools dataset reads every school on the platform

**High. Cross-tenant, and reachable by a school that can edit its own roles.**

### The defect

Every other dataset fences its rows. This one does not, deliberately:

```python
# schools/vs_schools/export_datasets.py:43-46
def _schools(scope):
    from .models import School

    return School.objects.all()
```

`School.objects` is a plain manager (`schools/vs_schools/models.py:105`), the
`scope` argument is ignored, and the dataset's own comment says so: "Declared
TENANT because it has no ledger entity - not because the rows are tenant-fenced.
`_schools` ignores the scope; the permission is the boundary."

So the whole boundary is one key, `platform.schools.view`. That key is seeded
**NORMAL and unrestricted**:

```python
# core/management/commands/seed_platform_permissions.py:113-121
("schools", "Customer school management", [
    ("view",   "View school list and detail", False, _NORMAL),
    ...
```

and nothing in the RBAC write path stops a `platform.*` key being attached to a
school-tenant role. `validate_role_permissions` checks the permission dependency
graph and nothing else:

```python
# vs_rbac/validators.py:172-178
effective_keys = flatten_permission_keys(permission_keys, group_ids)
if not effective_keys:
    return
validator = PermissionDependencyValidator()
result = validator.validate_permission_set(effective_keys)
```

`is_restricted` is metadata: it is stored (`vs_rbac/models.py:158`), indexed and
offered as a list filter (`vs_rbac/views.py:269`), and enforced nowhere.

### What actually happens

A school admin who holds `school.roles.create` mints a role carrying
`platform.schools.view`, `exports.catalogue.view` and `exports.run.create`,
assigns it to themselves, and exports the platform register of every school on
XVS - name, code, status, ownership, activation dates, and with the sensitive
key, each school's external registration number. Nothing refuses, and the run is
recorded as an ordinary successful export in their own tenant.

This is the same root as the `vs_audit` finding (`audit_code_issues` §2), and it
matters more here because the Export Centre turns the read into a file that
leaves the building.

### The fix

Two independent moves, both worth making:

1. Fence the dataset. A school tenant should see its own school; only a
   PLATFORM-kind tenant should see the register. `_users`
   (`vs_user/export_datasets.py:42-53`) already does exactly this and is the
   pattern to copy.
2. Enforce `is_restricted` (and platform-vs-school key namespaces) in the role
   write path, which fixes the class rather than this one case.

---

## 4. Run references collide after a few thousand runs

**High. A hard failure with a misleading message, and it can stall the
dispatcher.**

### The defect

```python
# models.py:416-418
@staticmethod
def new_reference() -> str:
    return f"RUN-{secrets.token_hex(3).upper()}"
```

Three bytes is six hex characters: 16,777,216 possible references. The column is
globally unique across every tenant (`models.py:324-326`), and `save()`
allocates once with no retry:

```python
# models.py:420-423
def save(self, *args, **kwargs):
    if not self.reference:
        self.reference = self.new_reference()
    return super().save(*args, **kwargs)
```

The birthday bound on 16.7 million values puts the first expected collision at
roughly **4,800 rows**, not 16 million. Runs are cheap and schedules are
nightly, so a platform with a few dozen active schedules reaches that inside a
year.

### What actually happens

The `INSERT` violates the unique constraint. The platform handler classifies a
unique violation as the client's fault:

```python
# core/exceptions.py:114-120
if isinstance(exc, IntegrityError):
    if _is_unique_violation(exc):
        return Response({... "message": "A record with these details already exists."},
                        status=400)
```

so a person clicking Run gets a 400 telling them their export already exists.
Retrying works (a fresh six characters), but nothing says so.

Inside `dispatch_due_schedules` it is worse. That loop catches
`ExportServiceError` only:

```python
# services.py:920-926
try:
    trigger_run(definition=definition, actor=owner, trigger=RunTrigger.SCHEDULED,
                schedule=schedule)
except ExportServiceError:
    deferred += 1
    continue
```

An `IntegrityError` is not that, so it escapes the loop and the Celery task
dies: every schedule still due on that tick is skipped, and because the failing
schedule's `next_run_at` was never advanced it collides again on the next tick
too.

### The fix

Widen the token (`secrets.token_hex(5)` is 1.1 trillion and still quotable), and
allocate in a short retry loop that catches the unique violation, the way
document numbers are allocated elsewhere in finance. Separately, catch
`Exception` per schedule in the dispatcher loop so one bad schedule cannot take
the tick down with it.

---

## 5. A malformed number filter is a 500, then a mislabelled retryable failure

**Medium. Two symptoms, one root: filter *shapes* are never validated.**

### The defect

The catalogue compiles a number-range filter by handing the caller's value
straight to the ORM:

```python
# catalogue.py:632-638
if fdef.kind == FILTER_NUMBER_RANGE:
    q = Q()
    if spec.get("min") is not None:
        q &= Q(**{f"{path}__gte": spec["min"]})
    if spec.get("max") is not None:
        q &= Q(**{f"{path}__lte": spec["max"]})
    return q
```

Nothing upstream checks the shape. `PreviewSerializer` accepts any dict:

```python
# serializers.py:429
filters = serializers.ListField(child=serializers.DictField(), required=False, default=list)
```

and `ExportDefinitionWriteSerializer.validate` checks only that each filter has
an `id` naming a filter the dataset declares (`serializers.py:165-176`) - never
that a date range holds dates or a number range holds numbers.

Every other filter kind is defended: `_as_date` raises a `FilterError`
(`catalogue.py:554-566`), the choice kind rejects unknown values
(`catalogue.py:601-610`), search and boolean coerce safely. The number range is
the hole.

### What actually happens

`finance.customer_invoices` publishes `total` as a number range
(`vs_finance/export_datasets.py:142`). `POST /v1/exports/preview/` with
`{"id": "total", "min": "abc"}` reaches `Q(total__gte="abc")`, Django raises
`ValueError`, `PreviewView` catches only `ExportError` (`views.py:283-288`), and
the platform handler renders an unhandled exception as a 500
(`core/exceptions.py:157-163`).

Save the same filter and run it, and the ValueError is swallowed by
`execute_run`'s defensive catch-all:

```python
# services.py:544-550
except Exception as exc:
    return _finish_failed(run, FailureCode.INFRASTRUCTURE,
        "This run stopped because of a temporary system problem. Try again; ...")
```

`INFRASTRUCTURE` is in `RETRYABLE_FAILURE_CODES` (`constants.py:95`), so
`get_failure` publishes `retryable: true` (`serializers.py:302-305`) and the UI
offers a Retry button on a configuration error that will fail identically every
time - the exact behaviour `retry_run`'s docstring says was fixed
(`services.py:328-331`).

### The fix

Validate the filter payload against the filter's own kind, in one place, and
call it from both the write serializer and the preview serializer: a date range
takes ISO dates, a number range takes numbers, a choice takes a list, a search
takes a string. `compile_filter` should raise `FilterError` rather than let a
raw value reach the ORM, so the run-time path degrades to `FILTER_INVALID`
(non-retryable, with the right guidance) rather than `INFRASTRUCTURE`.

---

## 6. `?definition=` and `?actor=` are raw strings on integer columns

**Medium. Same class as the `vs_config` `?actor=` defect.**

Three list endpoints pass a query parameter straight into an integer column:

```python
# views.py:700       (RunListView)
qs = qs.filter(definition_id=request.query_params["definition"])
# views.py:962       (ScheduleListView)
qs = qs.filter(definition_id=request.query_params["definition"])
# views.py:874       (ActivityView)
qs = qs.filter(requested_by_id=request.query_params["actor"])
```

Django's `IntegerField.get_prep_value` raises `ValueError` for a non-numeric
value, which is neither a DRF exception nor a Django `ValidationError`, so
`custom_exception_handler` falls through to its last branch and returns a 500
(`core/exceptions.py:157-163`).

`?since=` on the same view is safe by accident: a bad date raises Django's
`ValidationError`, which the handler renders as a 400
(`core/exceptions.py:85-91`). `?status=` and `?trigger=` are safe because an
unmatched choice is simply an empty result.

**Fix:** a small filter serializer per list view, the way the rest of the
platform does it, with `IntegerField` for the id parameters. Doing it once in
`_ExportBase` covers all three.

---

## 7. `exports.activity.view` is a read key that confers write

**Medium.**

```python
# views.py:115-116
def is_admin_reader(self) -> bool:
    return self.can(ExportPermission.ACTIVITY_VIEW)
```

```python
# views.py:146-155
if for_write and definition.owner_id != self.request.user.pk and not self.is_admin_reader():
    raise PermissionDenied(
        "Only the owner can change this export. Duplicate it to make your own version."
    )
```

`for_write=True` is what guards PATCH, DELETE, share and schedule
(`views.py:377, 396, 462, 976, 1018, 1029, 1044`). So the key the seed describes
as "Admin-only: read other people's export activity. Reading it is itself
audited" (`constants.py:334-335`) also lets its holder rewrite, archive,
re-share and re-schedule anybody's export - and the audit event it emits is
`ADMIN_VIEWED_ACTIVITY`, which says nothing about a write.

The blast radius is small today because the seed grants the key to
`xvs_super_admin` only (`seed_exports_permissions.py:38`), but the shape is
wrong: a view verb is doing update authorisation, and the next person who grants
this key to a platform-admin role will not expect it.

**Fix:** either give the write path its own key (`exports.definition.manage`),
or drop the `is_admin_reader()` escape from `get_definition(for_write=True)` and
let administrators duplicate like everybody else.

---

## 8. Retry skips the fair-share cap

**Medium.**

`_accept_run` is where the idempotency window and the concurrency cap live:

```python
# services.py:180-192
if in_flight(tenant) >= CONCURRENT_RUN_LIMIT:
    raise ExportServiceError(...)
```

`trigger_run` calls it (`services.py:197`). `trigger_quick_run` calls it
(`services.py:243`). `retry_run` does not - it validates the failure and then
creates and enqueues unconditionally:

```python
# services.py:337-347
new_run = ExportRun.objects.create(
    tenant=run.tenant, entity=run.entity, definition=run.definition,
    frozen_config=freeze(run.definition), trigger=RunTrigger.RETRY,
    requested_by=actor, attempt=run.attempt + 1,
)
enqueue(new_run, actor)
```

So `CONCURRENT_RUN_LIMIT = 3` (`constants.py:266`) is enforceable only until
somebody clicks Retry, and a person with several failed 500k-row runs can put
them all in flight at once - which is the starvation the cap exists to prevent.
The retry also carries no `client_key`, so a double-click makes two runs.

**Fix:** route `retry_run` through `_accept_run` like its two siblings, and let
`RunRetryView` pass a client key.

---

## 9. The cap's refusal says the export was accepted

**Medium. Small code, and the user reads it.**

```python
# services.py:188-191
raise ExportServiceError(
    f"{CONCURRENT_RUN_LIMIT} exports are already running for your organisation. "
    f"This one will be accepted as soon as one of them finishes."
)
```

Both trigger views turn that into a 400 (`views.py:509-510`, `views.py:663-664`).
Nothing is queued, nothing retries, no run row exists. The sentence describes a
queueing behaviour the code does not have, so the honest reading of the screen -
"it has been accepted, it will start shortly" - is wrong, and the user goes to
the Files list to find nothing there.

**Fix:** either say what happens ("Three exports are already running. Wait for
one to finish and run this again."), or make the sentence true by queueing the
run and letting the worker pool apply the cap.

---

## 10. `skip_when_empty` does nothing

**Medium. A control surface with nothing behind it.**

The field is declared with a careful explanation of the trade-off:

```python
# models.py:242-250
skip_when_empty = models.BooleanField(
    default=False,
    help_text=(
        "Produce no file when nothing matched, instead of an empty one. Off by "
        "default: an empty file is itself evidence that nothing happened, and a "
        "missing file is indistinguishable from a schedule that never ran."
    ),
)
```

It is migrated, and it is published as a writable field on the schedule
serializer (`serializers.py:522`), so the UI offers the toggle. Grep the tree
and there are exactly two hits: that declaration and that serializer line.
Nothing in `execute_run`, `produce`, `_store_file` or `_advance_schedule` ever
reads it.

A person turns it on and still gets an empty spreadsheet every morning.

**Fix:** honour it in `execute_run` - when the run came from a schedule with
`skip_when_empty` and `row_count == 0`, finish the run without storing a file
(a new terminal outcome, or COMPLETED with a `row_count` of zero and no
`ExportFile`), and say so in the run detail. Or delete the field and the toggle.

---

## 11. The whole file is assembled in memory, twice

**Medium. It will take a worker down before it takes the row cap.**

`produce`'s docstring is right that reading is streamed, and then the rows are
accumulated into one list anyway:

```python
# engine.py:459-467
rows = []
source = islice(qs.values_list(*paths).iterator(chunk_size=CHUNK_SIZE), row_cap)
for index, record in enumerate(source, start=1):
    rows.append([...])
```

The writer then builds the whole workbook in memory beside it:

```python
# writers.py:66
wb = Workbook(write_only=False)
```

and the finished bytes are held a third time by `_store_file`
(`services.py:539-541`).

`DEFAULT_ROW_CAP` is 500,000 (`constants.py:260`) and four datasets declare a
cap that high - `audit.events`, `finance.invoice_lines`, `finance.gl_postings`,
`admin.sign_ins`. A 500k × 20-column export is ten million Python strings in a
list plus an openpyxl cell object per value: gigabytes, in one worker, for one
run, and three of those may be in flight per tenant.

The download side repeats the pattern:

```python
# views.py:808-812
with default_storage.open(file.storage_name, "rb") as handle:
    body = handle.read()
response = HttpResponse(body, content_type=content_type)
```

so serving a large file also holds it whole in a web worker.

**Fix:** stream the writer (`openpyxl`'s `write_only=True` workbook, and the CSV
writer straight into a `SpooledTemporaryFile`) so `produce` never holds more
than a chunk; and serve downloads with `FileResponse(default_storage.open(...))`,
which streams. Neither change alters a single response shape.

---

## 12. One key unlocks every restricted column in every dataset

**Medium. A design coarseness, stated here because it is not stated anywhere else.**

```python
# engine.py:100-103
def may_export_sensitive(user, tenant) -> bool:
    """Sensitive fields need this key *in addition* to the dataset's key."""
    return _holds(user, ExportPermission.SENSITIVE_EXPORT, tenant)
```

`exports.sensitive_field.export` is a single tenant-wide key. Twenty-five fields
across nine datasets are marked `sensitive=True` - vendor bank account numbers
and tax IDs (`vs_procurement/export_datasets.py:184-186`), payout beneficiary
account numbers (`vs_payments/export_datasets.py`), sign-in IP addresses and
user agents (`vs_user/export_datasets.py`), customer billing email and phone
(`vs_finance/export_datasets.py:116-122`), audit actor emails
(`vs_audit/export_datasets.py:72`), a school's registration id
(`schools/vs_schools/export_datasets.py:85`).

Granting it so the finance team can export billing contacts also grants
procurement bank details and every sign-in IP address on the platform, to the
same person, in one click. The audit event names which fields were included
(`audit.py:66-89`), which is good, but that is detection, not prevention.

**Fix:** if a finer grain is wanted, scope the key per module
(`exports.sensitive_field.export` gated by the dataset's own module, or a
`sensitive_permission` on the `Field`). If the coarse key is the accepted
trade-off, say so in the seed docstring and in the FRD, because today the only
place it is visible is this function.

---

## 13. The saved-exports and files lists are N+1 by construction

**Medium.**

`visible_definitions` selects two relations (`views.py:119-121`), and then the
list serializer asks for four more things per row:

| Per row | Cost |
|---|---|
| `get_shared_with` - `obj.shares.count()` (`serializers.py:90`) | one `COUNT` |
| `get_last_run` - `obj.runs.order_by("-queued_at").first()` (`serializers.py:96`) | one query |
| `get_scope` - `obj.tenant.name` (`serializers.py:76`), and `tenant` is not selected | one query |
| `get_dataset` / `get_column_count` | free (in-process) |

At the page size of 25 (`views.py:92`) that is about 75 extra queries for one
screen. The Files list adds its own: `get_progress` calls `queue_position` for
every non-terminal run (`serializers.py:253-271`), and `queue_position` is two
counts (`services.py:150-168`), so a page of queued runs costs 50 more.
`ActivityView` uses the same serializer over an unfiltered tenant queryset
(`views.py:872-883`).

**Fix:** `select_related("tenant")` and `annotate(Count("shares"))` on
`visible_definitions`; a `Prefetch` for the latest run; and compute queue
positions for the whole page in one query rather than per row.

---

## 14. The schedule dispatcher takes no lock

**Medium.**

```python
# services.py:869-874
due = ExportSchedule.objects.select_related(...).filter(
    state=ScheduleState.ACTIVE, next_run_at__lte=now,
)
```

No `select_for_update`, no advisory lock, and `trigger_run` is called with no
`client_key` (`services.py:920-923`), so the idempotency window that protects
every other trigger path is not in play. Two beat workers, a beat plus a
manually invoked `dispatch_due_schedules()`, or one tick overlapping the next on
a slow database, all produce two runs for the same window - two files, two
notifications, two rows against the tenant's cap.

**Fix:** `select_for_update(skip_locked=True)` over the due set inside a
transaction, and give the scheduled run a deterministic client key
(`f"sched-{schedule.pk}-{schedule.next_run_at.isoformat()}"`), which makes the
duplicate a no-op even if two workers get through.

---

## 15. The idempotency window is a read-then-write race

**Medium.**

```python
# services.py:172-179
if client_key:
    window_start = timezone.now() - datetime.timedelta(seconds=IDEMPOTENCY_WINDOW_SECONDS)
    existing = ExportRun.objects.filter(
        tenant=tenant, client_key=client_key, queued_at__gte=window_start,
    ).order_by("-queued_at").first()
    if existing is not None:
        return existing
```

There is an index on `client_key` (`models.py:409`) but no unique constraint
anywhere, so two requests carrying the same key that arrive together both see
nothing and both create. The double-click this exists to absorb is exactly the
case that arrives together.

**Fix:** a partial unique constraint on `(tenant, client_key)` where
`client_key <> ''`, and catch the resulting integrity error as "the other one
won" rather than as an error.

---

## 16. Reading the activity list writes caller-controlled data into the audit trail

**Medium.**

```python
# views.py:867-871
audit.record(
    AuditAction.ADMIN_VIEWED_ACTIVITY, actor=request.user, tenant=self.tenant,
    obj=self.tenant, label="Export activity",
    metadata={k: request.query_params.getlist(k) for k in request.query_params},
)
```

Recording *that* the list was read is right, and it is one of the two rules the
audit module enforces by construction. Recording every query parameter the
caller sent, unfiltered and unbounded, is not: `AuditEvent` is append-only and
kept forever, so a caller can pad a request with megabytes of query string and
have it stored permanently, and any personal data they happen to type into a
filter goes into the immutable trail with it.

**Fix:** record a whitelist - the four filters the view actually reads (`actor`,
`dataset`, `status`, `since`) - truncated, which is also what makes the event
useful to read back.

---

## 17. The domain-neutrality guard tests error out on Windows

**Medium. Observed, not inferred - this one was caught by actually running it.**

### The defect

Both tests read source files without naming an encoding:

```python
# tests.py:1587   (test_the_engine_never_imports_a_domain_app)
source = path.read_text()
# tests.py:1600   (test_the_catalogue_declares_no_datasets_of_its_own)
source = pathlib.Path(catalogue.__file__).read_text()
```

`Path.read_text()` with no `encoding=` uses the platform default, which on this
box is cp1252. `catalogue.py` is UTF-8 and contains characters cp1252 cannot
decode - the curly quotes in `_as_date`'s message (`catalogue.py:561-564`), the
naira sign in `render_value` (`catalogue.py:91`), and the arrow in
`choice_labels`' docstring (`catalogue.py:331`). Byte 22379 is the `\x9d` of a
UTF-8 `”`.

### What actually happens

```
ERROR: test_the_engine_never_imports_a_domain_app
ERROR: test_the_catalogue_declares_no_datasets_of_its_own
UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d in position 22379
Ran 19 tests in 0.586s
FAILED (errors=2)
```

An **error** is not a failure: the assertion never runs. So the two tests that
enforce this repo's first architectural rule - that the engine apps stay
domain-neutral and `vs_exports` never grows a `from vs_finance.models import
...` - are not enforcing anything on the platform the work is done on. Somebody
could add that import and both guards would keep erroring in exactly the same
way they do now, which reads as pre-existing environmental noise rather than as
the guard firing.

It is the same shape as the `vs_user` item already recorded in the playbook
(`seed_organogram` printing a `→` into a cp1252 stream), and it is why that
class of failure is worth fixing rather than tolerating: an environmental error
that sits permanently red is indistinguishable from a real one.

### The fix

`path.read_text(encoding="utf-8")` in both tests. One word each, and it turns two
permanently-erroring tests back into the guards they were written to be. Worth a
sweep for other unqualified `read_text()`/`open()` calls in test code at the same
time.

---

## 18. Smaller defects and dead surface

**Low. Worth a sweep, not worth a release.**

1. **`sharing` is a writable flag that nothing enforces.** It is listed among
   the write serializer's fields (`serializers.py:130`), so a PATCH can set
   `SHARED` on a definition with no share rows. Access is decided by the rows
   (`views.py:143`, `services.py:687`), never by the flag, so the two can
   disagree and the list column lies. `DefinitionShareView` is the only thing
   that keeps them in step (`views.py:483-485`).

2. **A cancel can be accepted and then ignored.** `request_cancel` on a RUNNING
   run sets the flag (`services.py:353-374`) and answers "Cancellation
   requested. No partial file is kept" (`views.py:735`). If the flag lands after
   `produce` returns but before `_store_file` finishes (`services.py:538`), the
   run completes with a file and the promise was false. The window is small; the
   sentence should be conditional rather than absolute.

3. **`columns_produced` publishes catalogue field ids** (`serializers.py:213`),
   which is the one place the serializer module's own "nothing here exposes a
   raw JSONField, an internal id cannot leak" rule (`serializers.py:1-14`) is
   not applied. Publish labels, or say why ids are wanted.

4. **`FailureCode.DATE_SPAN_EXCEEDED` is dead for new runs.** It is kept
   deliberately so historical runs still render (`constants.py:84-88`) and still
   carries guidance (`constants.py:125-127`). Correct, but nothing outside that
   comment says it can no longer occur, so a reader of `FAILURE_GUIDANCE` will
   assume it can.

5. **`ExportDefinitionShare` rows are replaced without a transaction.**
   `definition.shares.all().delete()` then `bulk_create` (`views.py:479-483`):
   a failure between the two leaves the export shared with nobody. One
   `transaction.atomic` fixes it.

6. **A school concept reaches into a platform app's catalogue entry.**
   `admin.users` reads `tenant__school_profile__name` and `…__code`
   (`vs_user/export_datasets.py:106-108, 128`) and the users dataset filters on
   them. It is an ORM path across the `School.tenant` one-to-one rather than an
   import, and it is deliberate and tested
   (`tests.py:1839`), but it is the one place school vocabulary appears in a
   non-school app's export surface. Worth a decision, not a fix.

7. **`resolve_screen` flattens multi-valued parameters.** `FromScreenView`
   passes `request.query_params.dict()` (`views.py:561`), which keeps only the
   last value of a repeated parameter. A screen with `?status=DRAFT&status=SENT`
   therefore exports one of the two, and because the parameter *is* in the
   binding's `handles` list it is reported as carried, not unmapped - the one
   silent narrowing in a design built around never silently widening.

---

## What is right, and should not be "fixed"

Recorded because a future reader will be tempted:

* **Expiry is not a run status.** `RunStatus` has no `EXPIRED`
  (`constants.py:24-32`); availability is derived from `ExportFile.available_until`
  at read time (`models.py:501-513`). Nothing rewrites history to represent the
  passage of time, and a missed sweeper night changes nothing a user sees.
* **`frozen_config` is mandatory and never back-filled** (`models.py:349-351`).
  It is why the run detail can describe *this* file rather than what the
  definition has since become, and why `config_drift` can be honest
  (`services.py:105-126`).
* **Audit failures cannot fail the work they describe.** `audit.record` catches
  and logs (`audit.py:57-62`), added after a tenant-scoped run with no entity
  stranded itself in RUNNING with a file already written; the regression test is
  `test_a_broken_audit_write_cannot_strand_a_finished_run` (`tests.py:1161`).
* **The engine imports no domain app.** The catalogue holds vocabulary only and
  each app registers its own datasets from `AppConfig.ready`
  (`catalogue.py:30-35`), with a test that enforces it
  (`tests.py:1577`). This is the pattern `vs_tickets` later copied for
  onboarding context, and it is what keeps the Export Centre domain-neutral.
* **Analytics and audit are two pipelines on purpose** (`analytics.py:1-35`),
  with the privacy rule enforced by a property whitelist rather than promised
  (`analytics.py:69-83, 134-151`).
* **Downloads are re-authorised against the downloader**, on the run's *frozen*
  entity and dataset, and every refusal is logged like a success
  (`services.py:663-736`).
