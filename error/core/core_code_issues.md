# core_code_issues

Everything wrong with `core`, in one place, ordered by how much it costs. Each
item states the defect, the evidence, what actually happens to a person, and the
fix. The six slice reports (`core_response_contract`, `core_error_handling`,
`core_file_storage`, `core_background_jobs`, `core_bootstrap_seeds`,
`core_operations_and_mail`) point here rather than repeating it.

Baseline: the `core` suite is **`Ran 64 tests in 112.087s` - FAILED (errors=1)**
(`cd apps && DB_NAME=cx_core_doc ../cx/Scripts/python.exe manage.py test core
--settings=apps.settings.local --noinput`). The one error is **not**
environmental in the way the `vs_user` and `vs_todo` baselines were: the cause is
in `core` itself, and it is §11 below. Every other item is something the other 63
tests do not catch. Every claim is traced to a file and line.

**What `core` is, and why that shapes the list.** This is not a domain module.
It has one model with an endpoint, one exception handler, one storage backend,
one Celery base class and seventeen management commands - and almost everything
in it is *imported* rather than called over HTTP. A defect here does not break
one screen; it either breaks every screen or it silently fails to apply to a
module that forgot to opt in. Most of what follows is the second kind.

**Status: §1 and §16 are fixed; everything else is recorded, not yet fixed.**

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | ~~A media capability URL can never be revoked, and nothing deletes the bytes~~ **FIXED** | ~~High~~ |
| 2 | Storage validation answers 500, so every upload endpoint must remember to validate first | **High** |
| 3 | `ValueError` from an ORM filter is a 500 on every endpoint in the repo | **High** |
| 11 | `seed_all_permissions` crashes on a Windows console, and takes `core`'s own suite red | **High** |
| 12 | `create_superuser` has a committed default password | **High** |
| 16 | ~~`reset_db` will drop any database it is pointed at, with one flag and no environment guard~~ **FIXED** | ~~High~~ |
| 4 | The response envelope is a convention nothing enforces, and one module ignores it | **Medium** |
| 10 | Beat floods the user-facing job queue with system rows | **Medium** |
| 13 | `create_superuser` is commented out of `build.sh`, so a fresh deploy has no account | **Medium** |
| 14 | The notification registry is seeded outside the master chain | **Medium** |
| 17 | `delete_user`'s docstring says "local only" and explains how to run it on production | **Medium** |
| 18 | `build.sh` still carries a commented database-wipe block | **Medium** |
| 5 | Two error shapes ship from one package | **Low** |
| 6 | `data or {}` erases the difference between an empty list and no data | **Low** |
| 7 | The API-docs folder map has drifted from the URL table | **Low** |
| 8 | A 500 carries no reference anybody can quote | **Low** |
| 9 | The stored content type is guessed from the filename, not carried from validation | **Low** |
| 15 | The master seed's docstring and its code disagree about the chain | **Low** |
| 19 | Smaller defects and dead code | **Low** |

The numbering is by topic, not by severity; the table above is sorted by cost.

---

## 1. A media capability URL can never be revoked, and nothing deletes the bytes

**High. FIXED** - see *The fix, as shipped* at the end of this item.

### The defect

`StoredFile` has no owner, no tenant and no back-reference
(`core/models.py:15-28`). `MediaView` authenticates the caller and then serves
any row whose `name` matches (`core/views.py:124-138`). The module is honest
about this - the access model is a capability URL, and the unguessability comes
from a 64-bit token in the name (`core/storage.py:15-21,107-120`).

Two things follow that the design note does not address:

1. **Nothing deletes the bytes.** Django has not deleted files on model delete
   since 1.3, and there is no sweeper, no reference count and no
   `post_delete` hook anywhere in the repo for `StoredFile`. Deleting the record
   that owned a file leaves the row - and the working URL - in place forever.
2. **A capability cannot be withdrawn.** Removing somebody's access to a record
   does not remove their access to its file, because the file's protection is
   the string they already have.

### What actually happens

Ngozi is Bright Star's bursar. She uploads a scanned bank mandate to an expense
claim; the serializer returns
`/media/expense-receipts/mandate-4f1a9c22d0e71b83.pdf`. Her browser caches
nothing (it is a PDF, so `Content-Disposition: attachment`), but the URL is in
her history, in the API response her frontend logged, and in the support ticket
she pasted it into.

She leaves the school. Her account is deactivated - so `IsAuthenticatedAndActive`
now refuses her. Fine. But the claim is deleted six months later during a
cleanup, and the file stays. Anybody still holding that URL and any active
account on any tenant fetches it: the bank mandate for a school that has no
record of it any more.

Meanwhile the table grows. Every import spreadsheet, every superseded logo, every
receipt on a deleted claim is still there, in the database, in every backup.

### The fix, as shipped

The capability model is gone. A read is now authorised, not merely
authenticated, and the URL expires.

1. **`StoredFile` knows whose file it is** - `tenant`, `owner_content_type` /
   `owner_object_id` / `owner_field`, `created_by`, `revoked_at`
   (`core/models.py`). The tenant is stamped by `DatabaseStorage._save` from the
   request's tenant context; the owning record is stamped by `core/binding.py`
   on the owner's `post_save`, because a new record has no primary key while its
   file is being written.
2. **`MediaView` refuses unless four things agree** - a live session, a
   signature issued to *this* caller and unexpired, the file's own tenant, and
   the owning record's registered read policy (`core/media.py`). There is no
   default policy: a model that registers none is not served, so adding a
   `FileField` cannot publish it by accident. Every refusal is a 404, so the
   route never confirms that a name exists.
3. **URLs are signed and short-lived** - `core.media.signed_url` binds each URL
   to one user for `MEDIA_SIGNED_URL_TTL_SECONDS` (default 900). A forwarded
   link is dead for whoever receives it, and a stale one is dead for everybody.
   Where no identity can be resolved the helper returns `""` rather than an
   unsigned path.
4. **Files are retired with their record** - deleting the owner, or replacing
   the upload on it, revokes the row: URL closed, bytes emptied, row kept so an
   audit still sees the file existed (`core/binding.py`).
5. **The `vs_tickets` pattern is preserved and now enforced.** Tickets and audit
   exports still serve their own bytes through record-scoped views, register no
   media policy, and are therefore refused by `/media/` rather than offered a
   second way in that checks less.

Ngozi's story now ends differently at every step. Her deactivated account fails
the session check; her Greenfield account fails the tenant check; her old URL
fails the signature check; and the deleted claim's receipt was revoked when the
claim went.

6. **Existing media is rescued, not stranded.** Migration `0006` binds every
   `StoredFile` an existing record still points at, walking from the record to
   the file because that is the only direction carrying the answer. Orphans -
   rows nothing points at, the exact residue this finding was about - stay
   unbound and unreadable.

**Archiving, handled separately and deliberately differently.** Deleting a
record destroys its evidence; archiving must not, because a record is archived
precisely so it can be read later. So an archived owner's file is refused at
read time - the URL closes - while the bytes stay whole for whoever opens the
archived record itself. A module whose archived records should keep serving
their files declares `serve_when_retired=True` on its policy rather than getting
it by omission. No file-owning model archives today; the rule is at the choke
point so it holds the day one does.

---

## 2. Storage validation answers 500, so every upload endpoint must remember to validate first

**High.**

`DatabaseStorage._save` raises Django's `ValidationError` from inside `_save`
(`core/storage.py:74-83`). That is not a DRF exception and it is not caught by
`custom_exception_handler`'s Django-validation branch either - by the time it is
raised, the request is inside `serializer.save()`, and what surfaces is a
`ValidationError` Django raises during a **storage** operation, which the handler
turns into a 400 only if it propagates cleanly. In practice the endpoints that
skipped validation 500'd, which is why `core/uploads.py` exists at all - its own
docstring names the three offenders (`core/uploads.py:1-13`):

> the vendor portal checked magic bytes, vs_tickets checked extension and size
> only, and the expense-claim receipt endpoint checked nothing at all (so an
> oversized or unsupported file 500'd from storage).

The fix that was applied was a shared helper every caller must remember to
invoke. The failure mode is unchanged for any caller that forgets: an oversized
or unsupported file reaches storage and the user gets "An unexpected error
occurred."

**What actually happens.** A new endpoint is written that accepts a document. Its
author writes `serializers.FileField()` and saves. A user uploads a 30 MB scan.
`_save` raises, the request 500s, the log fills with a traceback, and the user is
told nothing they can act on. Nothing in review catches it, because the code
looks complete.

**The fix.** Make the choke point unavoidable rather than conventional:

1. **A `ValidatedFileField`** in `core` that runs `validate_upload` in its own
   `to_internal_value` with a policy set at declaration - so declaring the field
   *is* validating.
2. Failing that, **have `_save` raise a DRF `ValidationError`** so the fallback
   answers 400 rather than 500. The storage layer is the wrong place to shape an
   API response, which is why it was not done this way - but a 400 from the wrong
   layer beats a 500.

---

## 3. `ValueError` from an ORM filter is a 500 on every endpoint in the repo

**High, because it is one line in `core` and a bug in four modules.**

`custom_exception_handler` has ten branches (`core/exceptions.py:91-195`) and
none of them is `ValueError`. So:

```python
qs.filter(assignee_id="abc")
# ValueError: Field 'id' expected a number but got 'abc'
```

falls to the final branch: `500`, code `SERVER_ERROR`, and a logged traceback.

The same shape appears in at least four modules, each recorded in its own issues
file:

| Module | Parameter | Reference |
|---|---|---|
| `vs_tickets` | `?assignee=`, `?requester=`, `?school=` | `error/tickets/ticket_code_issues.md` §9 |
| `vs_todo` | `?assignee=`, `?focus=` | `error/todo/todo_code_issues.md` §7 |
| `vs_workflow` | `?requested_by=` | `error/workflow/workflow_code_issues.md` §16 |
| `vs_config` | `?actor=` (a UUID against a `BigAutoField`) | `error/config/config_code_issues.md` |

Four modules independently made the same mistake, which is the signal that the
guard belongs in one place.

`DataError` has the same problem from the other direction: a value too long for a
column is `django.db.utils.DataError`, not `IntegrityError`, so it takes branch 7
as well - which is how a 300-character filename becomes a 500 rather than a 400
(`error/tickets/ticket_code_issues.md` §11).

**What actually happens.** A frontend sends `?assignee=null` - which frontends
do, when a filter is cleared - and the ticket list 500s. The user sees a generic
failure on a screen that worked a second ago, support cannot reproduce it because
they clear filters differently, and the log has a traceback with no request id
(§8).

**The fix.** Two branches in the handler, above the final one:

```python
if isinstance(exc, ValueError):
    return Response({... "code": "INVALID_PARAMETER" ...}, status=400)
if isinstance(exc, DataError):
    return Response({... "code": "VALUE_TOO_LONG" ...}, status=400)
```

A blanket `ValueError` → 400 is broad, and a genuine internal `ValueError` would
be mislabelled. The narrower version is to add a shared `int_param(request, name)`
helper in `core` and fix the four call sites - more work, and it does not protect
the fifth module that has not been written yet. Doing both is right: the helper
for the known cases, the branch as the floor.

---

## 4. The response envelope is a convention nothing enforces, and one module ignores it

**Medium.**

`DEFAULT_RENDERER_CLASSES` is plain `JSONRenderer`
(`apps/settings/base.py:46-49`). Nothing wraps a response the view did not wrap
itself. The envelope is produced by three opt-in mechanisms:

- calling `success_response` directly;
- importing the mixins from `core.mixins` instead of `rest_framework.mixins`
  (they share DRF's class names, so the import is the whole difference);
- being paginated, which is automatic.

`vs_workflow` imports `rest_framework.mixins` (`vs_workflow/views.py:8,13`) and
`core.response` nowhere, so it answers four different shapes across one module -
enveloped for lists (from pagination), bare objects for details and every action,
`{"results": [...], "count": N}` for one dashboard, and bare lists for two more.
Its errors *are* enveloped, because they are hand-built. Recorded at
`error/workflow/workflow_code_issues.md` §13.

**What actually happens.** A frontend developer writes a shared
`unwrap(response)` helper that reads `body.data`. It works for tickets, finance,
exports, notifications and todo. It returns `undefined` for every workflow detail
page, so the workflow screens grow their own client, and the next module's author
copies whichever one they happen to look at.

**The fix.**

1. **A response renderer, not a convention.** A `JSONRenderer` subclass that
   wraps any un-enveloped dict or list in `{"success": true, "message": …,
   "data": …}` at render time would make the shape universal and let the mixins
   become a way to set the *message*, not the shape.
2. Failing that, **a test that walks the URL conf** and asserts every 2xx body
   has a `success` key. It is the only thing that would have caught the drift.

---

## 5. Two error shapes ship from one package

**Low.**

```python
# core/response.py:14-21
def error_response(message, error=None, status=400, code=None):
    body = {"success": False, "message": message, "error": error or {}}
    if code is not None:
        body["code"] = code          # ← top level
```

```python
# core/exceptions.py:99-105
return Response({
    "success": False, "message": …,
    "error": {"code": "TOKEN_INVALID", "detail": …},   # ← inside error
}, ...)
```

A client reading `body.error.code` gets nothing from a view that used
`error_response(..., code=...)`; a client reading `body.code` gets nothing from
anything the exception handler produced. Both are `core`, two files apart.

Most views do not pass `code` at all, so the divergence is latent rather than
widespread - which is exactly when it is cheap to fix.

**The fix.** Put `code` inside `error` in `error_response`, matching the handler,
and keep the top-level key too for one release if anything depends on it.

---

## 6. `data or {}` erases the difference between an empty list and no data

**Low, and it surprises everybody once.**

```python
# core/response.py:5-11
def success_response(message, data=None, status=200):
    return Response({"success": True, "message": message, "data": data or {}}, ...)
```

`success_response("Comments retrieved successfully.", [])` and
`success_response("Comments retrieved successfully.", None)` produce identical
bodies. So `data` is an **array** when a collection has rows and an **object**
when it does not - a JSON type that changes with the row count.

Several modules pin the shape in a test precisely because it catches people:
`vs_tickets/tests.py:634-638`, and both `vs_tickets` and `vs_todo` list it as a
gap where the empty case is untested.

**What actually happens.** A frontend does `data.map(...)`. It works all through
development, because the fixtures have rows. The first real user with an empty
inbox gets `data.map is not a function`.

**The fix.** `data if data is not None else {}`. One line, and it changes the
body only for the callers passing an empty collection - which is the change
wanted. Anything relying on `0`, `""` or `False` becoming `{}` would need
checking first, which is a one-command grep.

---

## 7. The API-docs folder map has drifted from the URL table

**Low.**

`_TAG_MAP` (`core/schema.py:40-70`) maps URL prefixes to documentation folders.
`apps/urls.py` mounts seventeen module prefixes. Four of them have no entry:

| Mounted | Slice |
|---|---|
| `/v1/onboarding/` | `apps/urls.py:24` |
| `/v1/exports/` | `apps/urls.py:36` |
| `/v1/support/` | `apps/urls.py:38` |
| `/v1/health/` | `apps/urls.py:39` |

So four whole modules - onboarding, the Export Centre, support tickets and
platform health - fall through to drf-spectacular's default tagging and land in
whatever folder it infers, rather than a named one.

Nothing keeps the two lists in step: a new module is mounted in one file and
tagged in another, and the docs build succeeds either way.

**The fix.** Add the four entries, and a test that asserts every prefix in
`urlpatterns` has a `_TAG_MAP` entry. The test is six lines and it is the only
thing that stops the fifth module drifting.

---

## 8. A 500 carries no reference anybody can quote

**Low, and it costs support time on every one of §3's 500s.**

```python
# core/exceptions.py:189-195
logger.exception("Unhandled exception in request", exc_info=exc)
return Response({"success": False, "message": "An unexpected error occurred.",
                 "error": {"code": "SERVER_ERROR"}}, status=500)
```

The body carries no identifier and the log line carries no request id, so there
is no way to connect "the screen broke at about half past two" to a traceback.

**The fix.** Mint a short id per unhandled exception, put it in
`error.detail.reference` and in the log message. Ten lines, and it turns every
future 500 report into a lookup.

---

## 9. The stored content type is guessed from the filename, not carried from validation

**Low.**

`validate_upload` returns a **verified** content type from a fixed map, having
proved the bytes match the extension (`core/uploads.py:162-176,257`).
`DatabaseStorage._save` then independently does
`mimetypes.guess_type(name)[0] or "application/octet-stream"`
(`core/storage.py:84`) and stores that.

So `StoredFile.content_type` - the value `MediaView` serves the bytes as
(`core/views.py:129-130`) - is derived from the **path**, a second time, rather
than from the verification.

In practice the two agree, because the path still ends in the same extension. The
cost is that the verification's result is discarded, so a caller that wants the
verified type has to store it themselves - which `vs_tickets` does, on its own
column (`vs_tickets/services/tasks.py:219-222`), and a plain `ImageField` cannot.

**The fix.** Let `_save` accept the verified type (Django's storage API allows
extra state through the file object), or record it on `StoredFile` from the
caller. Small, and it removes a second source of truth from the path that decides
`Content-Type`.

---

## 10. Beat floods the user-facing job queue with system rows

**Medium.**

`TrackedTask` is the base of every task (`apps/celery.py:10`), and
`before_start` writes a `BackgroundJob` row for any task that does not already
have one (`core/tasks_base.py:108-128`). Beat tasks pass no `_job_*` kwargs, so
every scheduled run creates a system row.

The schedule (`apps/celery.py:18-149`):

| Cadence | Tasks | Rows/day |
|---|---|---|
| every minute | `capture_queue_snapshot`, `evaluate_alert_rules` | 2,880 |
| every 5 minutes | pending-import notifications, export dispatch, uptime checks | 864 |
| every 15 / 30 minutes | import retries, stuck imports, abandoned export runs | ~200 |
| hourly | RFQ reminders, unbooked surge, uptime rollup | 72 |
| daily and weekly | the rest | ~10 |

Roughly **4,000 rows a day** before any person triggers anything. The prune keeps
terminal rows for 90 days (`core/tasks.py:20-32`), so the steady state is a table
of around 360,000 rows, all of them `owner=None`, `kind="system"`.

**What actually happens.** `BackgroundJob` is a *user-facing* queue - that is its
docstring's first line (`core/models.py:32-37`) - with two screens on it. The
admin task monitor's default view is dominated by health snapshots. Anybody
looking for the import that failed this morning is scrolling past a thousand
one-line heartbeats to find it, and the `(status, -created_at)` index is doing
most of its work on rows nobody will ever read.

**The fix.**

1. **Do not track what nobody asked for.** Move the row creation in
   `before_start` behind the same condition `apply_async` already uses - an owner
   or a label - and give the genuinely interesting system tasks a `_job_label`.
   Health heartbeats are Celery's business, not the queue's.
2. Or **prune by kind**: keep `system` rows for a week and everything else for
   ninety days.
3. Either way, **default the monitor's view to non-system rows**.

---

## 11. `seed_all_permissions` crashes on a Windows console, and takes `core`'s own suite red

**High. This is the failing test in the baseline.**

```python
# seed_all_permissions.py:95-99
self.stdout.write(self.style.MIGRATE_HEADING(
    "\n  ╔══════════════════════════════════════╗\n"
    "  ║      seed_all_permissions            ║\n"
    "  ╚══════════════════════════════════════╝\n"
))
...
# seed_all_permissions.py:163
self.stdout.write(f"\n  ✔ Super Admin reconciled with all {len(active_keys)} active permissions.")
```

`✔` is U+2714 and the box-drawing characters are U+2550-255D. A Windows console
stream is cp1252, which encodes none of them, so `self.stdout.write` raises
`UnicodeEncodeError`.

It is not one file. Fourteen of the seventeen commands in `core` contain
cp1252-unencodable characters, most of them in output:

```text
clear_permissions      → ─
create_superuser       → → ⏳ ─ ═ ⚠ ✅
delete_user            → → ─ ⚠ ✅
reset_db               → ✓ ✗
seed_actions           → → ↔ ─
seed_all_permissions   → → ─ ═ ║ ╔ ╗ ╚ ╝ ⚠ ✔ ✗
seed_consultant_role   → ⚠
seed_dev_data          → ← →
seed_import            → →
seed_import_permissions→ ⚠
seed_platform_permissions → → ─ ⚠
seed_school_permission_groups → ─
seed_school_permissions → ─ ⚠
seed_vision_staff      → →
```

### What actually happens

Three things, in increasing order of how much they cost:

1. **`core`'s own test suite is red on Windows.**
   `test_super_admin_gets_every_active_permission_without_expanding_platform_admin`
   calls `Command()._ensure_super_admin_has_every_permission()` directly, with no
   captured stdout, and dies at line 163. That is the `errors=1` in the baseline.
   The same failure is recorded against `vs_todo`
   (`error/todo/todo_code_issues.md`) and `vs_user` (the playbook), from the same
   line.
2. **A test only passes when it captures output.**
   `core/test_seed_import_permissions.py:15-16` wraps every `call_command` in
   `stdout=StringIO()`, which accepts any Unicode - so it passes. The two tests
   that call the command without redirecting are the two that fail. That is a
   trap: the suite's greenness depends on a test-helper detail, not on the code.
3. **An operator on Windows sees a traceback at the end of a run that mostly
   worked.** All eighteen sub-seeds have committed. The reconciliation is
   `@transaction.atomic`, so its rows roll back. The command exits non-zero. The
   registry is seeded, the super admin is not reconciled, and the last thing on
   screen is a `UnicodeEncodeError` - which reads like the whole thing failed.

### The fix

The right fix is one line, not fourteen files:

1. **Make the output stream encoding-safe.** Django's `OutputWrapper` writes to
   `self._out`; setting `PYTHONIOENCODING=utf-8`, or reconfiguring
   `sys.stdout` in `manage.py` with
   `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`, fixes every
   command at once and every future one.
2. **Then decide about the characters themselves.** `errors="replace"` keeps them
   and renders `?` where the console cannot draw them; replacing `✔`/`✗`/`⚠`
   with `[ok]`/`[x]`/`[!]` and the box-drawing with ASCII is the more
   conservative choice and costs nothing anybody will miss.
3. **Add the regression test**: run `seed_all_permissions` against a stream that
   cannot encode beyond cp1252 and assert it completes.

---

## 12. `create_superuser` has a committed default password

**High.**

```python
# create_superuser.py:90-101
DEFAULT_EMAIL      = 'admin@codexng.com'  # ⚠️ Change in production!
DEFAULT_PASSWORD   = 'Admin@123456'  # ⚠️ Change in production!
DEFAULT_FIRST_NAME = 'System'
DEFAULT_LAST_NAME  = 'Administrator'
...
# DEFAULT_EMAIL      = os.getenv('SUPERUSER_EMAIL', 'admin@codexng.com')
# DEFAULT_PASSWORD   = os.getenv('SUPERUSER_PASSWORD', 'Admin@123456')
```

The environment-variable version is present, two lines below, **commented out**.
The active constants are used as `argparse` defaults
(`create_superuser.py:110,116`), so:

```text
$ python manage.py create_superuser
```

with no arguments creates `admin@codexng.com` with password `Admin@123456` - and
assigns it the Vision Super Admin role, the account the RBAC evaluator gives a
runtime authorization bypass to.

`reset_db`'s default post-commands include `create_superuser` with no arguments
(`reset_db.py:67-72`), so the bare form is not hypothetical - it is what the
reset path runs.

The password is in the repository. `build.sh` names the same value in its
commented-out invocation as a "known default - change it after first login",
which is the right instinct expressed in the wrong place: the comment is in a
block that does not execute.

**What actually happens.** Somebody brings up a new environment - a demo, a
staging rebuild, a customer trial - and runs the obvious command. The platform's
most privileged account now has a password that anybody who has read the repo
knows. Nothing forces a change at first login and nothing reports that the
default was used.

**The fix.**

1. **Uncomment the environment-variable version and drop the password
   fallback.** `SUPERUSER_PASSWORD` with no default, and a `CommandError` when
   it is absent and the command is non-interactive. An operator who wants a quick
   local account uses `--interactive`.
2. **Never print or default a password in help text.** `create_superuser.py:116`
   currently advertises it in `--help`.
3. **Force a change at first sign-in** for an account created this way, if the
   platform has that mechanism.

---

## 13. `create_superuser` is commented out of `build.sh`, so a fresh deploy has no account

**Medium.**

`build.sh` runs `migrate`, `seed_all_permissions`, the three notification seeds,
`seed_config_catalogue` and `seed_package` - and then:

```bash
# python manage.py create_superuser \
#   --email "${SUPERUSER_EMAIL:-chidera.ohanenye@codexng.com}" \
#   --password "${SUPERUSER_PASSWORD:-Admin@123456}" \
```

commented out, under a comment explaining that the command "self-skips (exits
cleanly) once a platform-tenant staff account exists, so it is safe to leave in
permanently".

So the reasoning for leaving it in is written directly above the line that takes
it out.

**What actually happens.** A new environment deploys cleanly. Every permission is
seeded, every role exists, and nobody can sign in. Fixing it needs shell access
to the deployed service, which on the target platform means a one-off run
somebody has to remember how to do.

`seed_all_permissions` even warns about the adjacent case - "Platform role(s) not
found... Run: python manage.py create_superuser" - so the chain is aware of the
dependency and the deploy script is not.

**The fix.** Uncomment it, with §12's fix applied first so it fails loudly on a
missing `SUPERUSER_PASSWORD` rather than minting a known credential.

---

## 14. The notification registry is seeded outside the master chain

**Medium.**

`seed_all_permissions` seeds `seed_notification_permissions` - the
`communication.*` keys - but **not** `seed_notification_event_types`,
`seed_notification_templates` or `seed_notification_settings`. Those three are
run by `build.sh` afterwards.

So a deployed environment is correct, and any environment brought up by
`migrate` + `seed_all_permissions` - a developer's database, a CI fixture, a
one-off restore - has notification permissions over an empty event registry.

**What actually happens.** Every dispatch in the platform raises
`UnknownEventTypeError`, and almost every caller swallows it:

| Caller | Behaviour |
|---|---|
| `core.tasks_base._notify_owner` | `logger.warning`, job still succeeds |
| `vs_todo.tasks` | returns `{"skipped": "event-not-seeded"}` |
| `vs_workflow.tasks.dispatch_notification` | `logger.exception`, task still succeeds |
| `vs_tickets.services.notifications` | `logger.warning`, ticket still created |

Each of those swallows is individually correct - a notification must never fail
the work. Together they mean a misconfigured environment is **completely
silent**: nothing is sent, nothing errors, and the only evidence is a log line
per attempt.

The master seed's own precedent argues for including them: `seed_import` is in
the chain with a comment saying required reference data belongs in the master
bootstrap so "a migrated environment cannot expose working import endpoints
backed by an empty template catalogue" (`seed_all_permissions.py:61-64`). An
empty event registry is the same failure.

**The fix.** Add the three notification seeds to `SEED_STEPS` after
`seed_notification_permissions`, and remove them from `build.sh` (they are
idempotent, so leaving them would also be harmless). Then consider
`seed_config_catalogue` and `seed_package`, which are in `build.sh` for the same
reason and out of the chain for no stated one.

---

## 15. The master seed's docstring and its code disagree about the chain

**Low.**

The module docstring numbers **fourteen** steps and does not mention
`seed_health` (`seed_all_permissions.py:15-46`). `SEED_STEPS` has **eighteen**
entries (`54-79`), and the progress line prints `[i/18]`.

Anybody reading the docstring to understand the bootstrap order gets a list that
is four entries short and missing the one that is least obvious.

**The fix.** Generate the docstring list from `SEED_STEPS`, or delete it and let
the list speak. A comment per entry inside `SEED_STEPS` cannot drift.

---

## 16. RESOLVED: `reset_db` could drop any selected database with one flag

**Formerly High. Fixed 30 August 2026.**

**Original failure.** `--yes` suppressed every prompt, the selected Django alias
was used without checking its resolved target, and every table returned by that
connection was dropped. A developer whose shell still carried staging database
credentials could therefore run what they believed was a local reset and erase
every tenant's data. The same file could delete migration source files if its
empty app list was populated, and a connection failure could be hidden by an
`UnboundLocalError` while closing a cursor that had never been assigned.

**Resolution.** The command now refuses every settings module except local,
test, and CI; refuses `DEBUG=False` outside the approved test modules; resolves
the selected alias and requires its database name to appear in
`RESET_DB_ALLOWED_DATABASES`; prints the resolved name and host; and always
requires the operator to type that exact name. `--yes` skips only the later step
prompts. It cannot bypass target confirmation.

The migration-file deletion and `makemigrations` steps were removed, so a reset
can no longer rewrite committed schema history. Cursor ownership now uses a
context manager, preserving the real connection error. Render builds remove
`reset_db.py` from the deployed artifact before Django command discovery.

Eight focused tests cover both environment refusals, the approved CI path, the
database allowlist, mandatory exact confirmation under `--yes`, target display,
connection-error preservation, and production-artifact exclusion. They passed
on 30 August 2026.

---

## 17. `delete_user`'s docstring says "local only" and explains how to run it on production

**Medium.**

The header (`delete_user.py:1-3`):

> Hard-delete one or more users and all traces of their work. **Local testing
> only.**

The usage section, thirty-nine lines later (`delete_user.py:40-42`):

> The command will run against whatever `DATABASE_URL` / DB env vars Render has
> configured for that service - **so it hits the live Render DB.**

Both sentences are in the same docstring. One of them is a warning and the other
is an instruction, and a reader cannot tell which is current.

The command itself is careful in the ways that matter - it refuses an address
that exists at more than one tenant until `--tenant_id` names one, prints exactly
what it will delete with tenant and status, and requires a typed `YES` - but it
has no environment guard, and `--force` skips the confirmation entirely.

**What actually happens.** Somebody reads the header, believes it is a local
tool, and runs it with `--force` against a shell that has production credentials.
A user and every row they touched are gone, in one transaction per user, with no
audit event anywhere.

**The fix.** Decide which sentence is true. If it is a production tool, remove
"Local testing only", require a typed confirmation that includes the database
name, and write an audit event. If it is not, add a `DEBUG`/settings-module guard
and delete the Render instructions.

---

## 18. `build.sh` still carries a commented database-wipe block

**Medium.**

```bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  ONE-TIME DATABASE REBUILD - REMOVE THIS BLOCK AFTER THE FIRST DEPLOY.   ║
# ║  ⚠️  IT WIPES ALL DATA. Leaving it in place wipes the database on EVERY  ║
# ║  deploy. As soon as this deploy succeeds, delete this whole block        ║
# ╚══════════════════════════════════════════════════════════════════════════╝
# RESET_DB=true python manage.py rebuild_database --yes
# ╚═══════════════════════════ END ONE-TIME BLOCK ═══════════════════════════╝
```

The block says, in its own words, to delete it after the first deploy. It was
commented out instead, which is not the same thing: the line is still there, it
still carries the `RESET_DB=true` that satisfies `rebuild_database`'s second
guard, and uncommenting it - by a merge, a revert, or somebody wanting a clean
staging database once - wipes the production database on **every** deploy
thereafter.

The block's own banner is the argument for removing it.

**The fix.** Delete the block. The cutover it was written for has happened; if it
ever needs doing again, the command still exists and the two guards are exactly
what make it safe to run deliberately.

---

## 19. Smaller defects and dead code

**Low, individually.**

1. **`BackgroundJob.Status.CANCELLED` is never set** (`core/models.py:45`).
   Nothing in `core` cancels a job, so the enum promises an operation that does
   not exist.
2. **`BackgroundJob.progress` is never written except as `100` on success**
   (`core/tasks_base.py:174`), though the field's help text promises "0-100 when
   the task reports progress". A task that wants to report progress must update
   the row itself.
3. **`_resolve_job_tenant_id` queries for the `codex` tenant by slug on every
   unattributed task start** (`core/tasks_base.py:58-59`) - roughly 4,000 times a
   day given §10.
4. **`XVSPagination` reports `totalPages: 0` for an empty result set.**
   `math.ceil(0 / 25)` is 0, and the `if page_size else 1` guard protects against
   a zero page size, not a zero count (`core/pagination.py:19`). A client
   rendering "page 1 of N" has to special-case it.
5. **`EnvelopeAutoSchema._looks_enveloped` tests for a `success` property**
   (`core/schema.py:21-22`), so a serializer with a field genuinely called
   `success` is never wrapped in the docs.
6. **The `docstring-name:` convention is enforced by nothing.** A view without it
   falls back to a heuristic over the first meaningful docstring line
   (`core/schema.py:101-108`), which is exactly the "leaked from implementation
   prose" outcome the tag was introduced to prevent.
7. **`custom_exception_handler`'s domain-exception branch is duck-typed on two
   attribute names** (`core/exceptions.py:159-165`). Any exception that happens
   to define `error_code` and `message` has its `message` returned to the caller
   verbatim.
8. **`_is_unique_violation`'s last resort is a substring search** of the
   exception text (`core/exceptions.py:34-36`), so a non-unique integrity error
   whose message contains "unique constraint" answers `400 DUPLICATE`.
9. **`ALLOWED_EXTENSIONS` (storage) and `TICKET_EXTENSIONS` (uploads) are two
   spellings of one list**, kept in step by a comment
   (`core/uploads.py:151-154`).
10. **`MediaView` materialises the whole file in memory** and supports no range
    requests or ETags (`core/views.py:128-132`), so a 25 MB PDF is a 25 MB
    allocation per request, unthrottled.
11. **Media reads are never audited**, on a route that serves receipts, scans and
    import spreadsheets.
12. **`seed_vision_staff` sets a fixed password** (`Vision@2025`,
    `seed_vision_staff.py:53`) for a roster of real staff addresses, with no
    environment override.
13. **`seed_dev_data` prints one shared password for every school user it
    creates** (`School@2025`, `seed_dev_data.py:61,101`). Correct for a
    development database; nothing stops it running elsewhere.
14. **`_check_platform_roles` swallows every exception**
    (`seed_all_permissions.py:187-188`), so a database error during the check is
    indistinguishable from the roles existing.
15. **`create_superuser` prints its "already exists" message with
    `self.style.ERROR` and returns 0** (`create_superuser.py:235-236`), so a
    successful self-skip reads like a failure in a deploy log.
16. **`build_from_email` calls `get_integration_settings()` per email**
    (`core/mail.py:24-27`) rather than caching it for a batch.
17. **`reset_db` is written in a different house style from the rest of the
    package** - no type hints, `Args:` docstrings, commented-out configuration
    lists, trailing whitespace. It reads like a recipe pasted in, which is worth
    saying because it is also the most dangerous file in `core` (§16).

---

## What the test suite does not know

64 tests, one failing (§11). The three areas with real tests -
`test_exceptions.py`, `test_jobs.py`, `tests.py` (storage and media) and the four
seed test files - are focused and good. What has **no test at all**:

1. **The response contract.** `success_response`, `error_response`,
   `XVSPagination` and all four mixins are untested - the shape every endpoint in
   the platform answers in. §4, §5 and §6 all live in that gap.
2. **`validate_upload`.** The module written to be the shared first line of
   upload validation has no test in `core`; its behaviour is asserted only
   indirectly, from `vs_tickets`.
3. **`EnvelopeAutoSchema`** - the wrapping, the tag map (§7) and the
   `docstring-name:` parsing.
4. **`create_superuser`** - including the default credentials in §12.
5. **`send_email`** - including the BCC default that is the whole of the policy.
6. **`prune_background_jobs_task`** - including the property that matters, that
   it leaves `QUEUED`/`RUNNING` rows alone.
7. **Every destructive command.** Defensible for the ones that drop schemas; less
   so for `delete_user`'s two guards, which are what stand between a mistyped
   command and a deleted account.

And one gap that is not about coverage but about the harness: **the suite's
greenness depends on whether a test captures stdout.** `test_seed_import_permissions`
wraps `call_command` in `StringIO()` and passes; `test_seed_all_permissions`
calls the method directly and fails. Fixing §11 removes the dependency.
