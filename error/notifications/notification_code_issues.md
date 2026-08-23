# notification_code_issues

Everything wrong with `vs_notifications`, in one place, ordered by how much it
costs. Each item states the defect, the evidence, what actually happens to a
user, and the fix. The three slice reports
(`notification_dispatch_engine`, `notification_feed_history`,
`notification_templates_settings`) point here rather than repeating it.

Baseline: the `vs_notifications` suite is **85 tests, all green**
(`Ran 85 tests in 380.453s` - OK). Every item below is therefore something the
suite does not currently catch. Nothing here is speculative - every claim is
traced to a file and line.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in
the code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | Notifications are filed under the sender's tenant, and the feed filters by tenant, so recipients in another tenant never see them | **Critical** |
| 2 | A school's settings decide whether CX staff get notified | **High** |
| 3 | `acknowledge-route` uses a different manager from every sibling route | **High** |
| 4 | Eight active in-app events have no click destination; three route prefixes match nothing | **Medium** |
| 5 | `metadata` is an unvalidated control surface for attachments, BCC and the From name | **Medium** |
| 6 | `mark-read` leaks a fact about notification ids the caller does not own | **Medium** |
| 7 | Date filters are raw strings, so a malformed value is a 500 | **Medium** |
| 8 | Three list endpoints are unpaginated | **Medium** |
| 9 | No audit event for template edits or settings changes | **Medium** |
| 10 | An engine app imports `vs_schools` | **Medium** |
| 11 | Smaller defects and dead code | **Low** |

---

## 1. Notifications are filed under the sender's tenant, and the feed filters by tenant

**Critical. Confirmed with a concrete caller.**

### The defect

`Notification.objects` is a `TenantAwareManager` (`models.py:544`), which adds
`tenant = <ambient tenant>` to every queryset built through it
(`vs_rbac/managers.py:100-118`). `TenantJWTAuthentication` sets that ambient
tenant on every authenticated request (`vs_rbac/authentication.py:139`).

The feed reads through that manager:

```python
# views.py:133-141
return (
    Notification.objects
    .filter(recipient=self.request.user, channel=ChannelChoices.IN_APP)
    ...
)
```

So the effective filter is `recipient = me AND channel = in_app AND tenant =
the tenant I asserted`. Meanwhile dispatch stamps the row with whatever tenant
the **caller** resolved (`dispatch.py:118-123`), which is the tenant of the
thing that happened, not of the person being told about it.

The class docstring asserts the opposite of what the code does:

> NOTE on managers: view scoping is done EXPLICITLY (recipient=… / tenant=… /
> all_objects) rather than relying on the ambient TenantAwareManager - the
> tenant thread-local is not reliably set for DRF-authenticated requests
> (`views.py:23-26`)

It is reliably set. DRF authenticates on first `request.user` access, which
happens during the permission check, before `get_queryset` runs.

### What actually happens

`vs_tickets` is the proven instance:

```python
# vs_tickets/services/notifications.py:71-77
NotificationService.send(
    event_key=event_key, ..., recipients=recipients, tenant=ticket.tenant, ...
)
```

and the recipients for a new ticket are the CX support queue:

```python
# vs_tickets/services/notifications.py:36-43
User.objects.filter(tenant__kind="PLATFORM", status=ACTIVE, ...)
```

`ticket.created` is active and supports `in_app` (`constants.py`, registry). So
when a school user raises a ticket:

- rows are written with `tenant = <the school>`;
- the CX agent asserts `?tenant=codex` and their feed filters on codex;
- **the agent never sees the ticket in their in-app feed**, and their unread
  badge never moves.
- **On staging, the email is silently dropped too.** The delivery task fetches
  its row through the same manager
  (`Notification.objects.select_for_update().get(id=…)`, `tasks.py:78`). On a
  real Celery worker there is no ambient tenant, so it works. Under
  `CELERY_TASK_ALWAYS_EAGER` - which `local.py:31`, `ci.py:28`, `test.py:43`
  set outright and `staging.py:45` defaults to `True` - the task runs inline in
  the request thread with the tenant still set, `get()` raises
  `DoesNotExist`, and `tasks.py:106-111` logs "not found. Skipping." and
  returns. The row stays `PENDING` forever and nobody is emailed.
- Worse in the other direction: the school admin's history log
  (`tenant = request.tenant`, `views.py:325`) **does** show those rows, so a
  school admin can read the CX agents' email addresses and the message bodies
  addressed to them (`serializers.py:186-192`).

The same shape applies anywhere recipients and the subject tenant differ. The
fallback that picks the **first** recipient with a tenant
(`dispatch.py:120`) makes it worse for mixed lists: every row in the batch
inherits one recipient's tenant.

### Why it exists

Two decisions that were each reasonable alone: `Notification.tenant` means "the
tenant this event belongs to" (right for history), and the feed reads through a
manager that interprets the same column as "the tenant that may see this row"
(right for tenant data). Nothing reconciles them.

### The fix

Fix the class, not the case:

1. **Read the feed through `all_objects`** with the explicit
   `recipient = request.user` filter the docstring already claims
   (`views.py:133`, `205`, `232`, `254`). Ownership is the correct boundary for
   a personal inbox; tenant is not.
2. **Decide what `Notification.tenant` means and write it down.** If it stays
   "the subject's tenant", the history log needs a second condition so a school
   admin does not read CX staff rows - for example `recipient__tenant` matching
   too, or excluding rows whose recipient is on a platform tenant.
3. **Reject or split multi-tenant recipient batches** in
   `NotificationService.send` rather than silently stamping them all with the
   first tenant found.

---

## 2. A school's settings decide whether CX staff get notified

**High. Same root cause as §1, different consequence.**

`resolve_channels(event_type, tenant=tenant)` (`dispatch.py:140`) resolves
against the tenant the caller passed. For `ticket.created` that is the school's
tenant (`vs_tickets/services/notifications.py:75`), while the recipients are CX
staff.

`ticket.created` is **not** transactional, so it is configurable
(`constants.py` registry). A school admin holding
`communication.communication_permissions.enforce` - which the prebuilt
`school_admin` and `branch_admin` roles are seeded with
(`seed_notification_permissions.py:25-29`) - can `PATCH settings/update/` with
`{"event_type_key": "ticket.created", "channel": "email",
"is_enabled": false}` and the CX support team stops being emailed about new
tickets from that school.

**Fix:** resolve channel settings against the **recipient's** tenant, not the
event's. Where a single dispatch spans tenants, resolve per recipient. Failing
that, mark cross-audience events transactional so they bypass settings, which
is what the registry already does for the vendor-facing procurement events
(`constants.py:131-141`).

---

## 3. `acknowledge-route` uses a different manager from every sibling route

**High, and it is the tell that §1 is real.**

```python
# views.py:273 - acknowledge_route
updated = Notification.all_objects.filter(route_q, recipient=request.user, ...)

# views.py:205, 232, 254 - unread_count, mark_read, mark_all_read
Notification.objects.filter(recipient=request.user, ...)
```

Four routes on one viewset, one of them unscoped. The result is that
`acknowledge-route` **can** mark read a row that the same user cannot see in
their feed, cannot count in their badge, and cannot mark read through
`mark-read`. Whichever manager is correct, three of the four are wrong.

**Fix:** pick one - `all_objects` plus the explicit `recipient` filter, per §1 -
and use it in all four places.

---

## 4. Eight active in-app events have no destination; three route prefixes match nothing

**Medium. Dead clicks in the product.**

`_PREFIX_ROUTES` (`services/routing.py:13-20`) maps event-key prefixes to
frontend paths:

```python
"/data-imports/batches": ("import.",),
"/export/files":         ("export.",),
"/team-management":      ("user.", "team."),
"/me/security":          ("security.",),
"/finance":              ("finance.", "payments."),
"/procurement":          ("procurement.",),
```

The registry uses these prefixes: `billing`, `export`, `import`, `onboarding`,
`payments`, `procurement`, `student`, `task`, `ticket`, `todo`, `user`,
`workflow`.

**Three mapped prefixes match no event at all:** `team.`, `security.`,
`finance.`.

**Eight active in-app event types get `action_url: ""`** - the notification
appears in the feed and clicking it goes nowhere:

```
billing.invoice_issued
billing.debit_note_issued
billing.credit_note_issued
billing.payment_received
billing.invoice_overdue
task.completed
task.failed
todo.task_completed
```

The five `billing.*` events are the sharpest case: the route table maps
`finance.` to `/finance`, which is clearly the intended destination, but the
registry keys those events `billing.`. The table was written against the wrong
prefix.

The same map drives `notification_route_q` (`services/routing.py:69-73`), so
`/me/security` is an acknowledge-route path that can never match anything.

**Fix:** replace `finance.` with `billing.` in `_PREFIX_ROUTES`, add
destinations for `task.` and `todo.`, and drop `team.` and `security.` or add
the events they were written for. Then add a test that asserts every active
in-app event key resolves to a non-empty `action_url` - the existing alignment
test (`tests.py:643-694`) checks the two maps agree with each other, not that
they cover the registry.

---

## 5. `metadata` is an unvalidated control surface

**Medium. Internal API, but the blast radius is real.**

`metadata` is documented as "internal-only caller correlation data"
(`models.py:498-505`) and is correctly never serialized. It is also, in
practice, the delivery task's configuration channel:

| Key | Read at | Effect |
|---|---|---|
| `from_name` | `tasks.py:94,156` | Sets the outgoing From display name |
| `bcc` | `tasks.py:100-104` | Overrides the platform `EMAIL_BCC`, including suppressing it entirely with `[]` |
| `attachments` | `tasks.py:105,134-150` | Reads up to 10 files from `default_storage` **by path**, 10 MB each |

Nothing validates any of it. Specifically:

- `attachments[].storage_name` is passed straight to
  `default_storage.open(storage_name, "rb")` (`tasks.py:142`). Any caller that
  puts a user-influenced path there emails the contents of that file to the
  recipient. The two real callers build the path themselves
  (`vs_finance/document_email.py:300`, `vs_procurement/po_email.py:381`), so
  there is no live exploit - but the engine offers no guard, and the pattern is
  one careless caller away from an arbitrary-file read.
- Ten attachments at 10 MB each is up to **100 MB held in memory** in one task
  (`tasks.py:136-150`), read fully before the message is built.
- `bcc` accepts any address list. A caller can silently copy any mailbox on any
  notification.
- The whole `metadata` dict is copied onto **every** row in the batch
  (`dispatch.py:357`), so a per-recipient value cannot be expressed and a
  correlation key meant for one recipient is stored against all of them.

**Fix:** give the engine a typed, validated payload for these three concerns
instead of a free JSON bag - a whitelist of storage prefixes for attachments, a
total size cap across the batch, and address validation on `bcc`. Keep
`metadata` for correlation data only.

---

## 6. `mark-read` leaks a fact about ids the caller does not own

**Medium.**

```python
# serializers.py:125-139
def validate_ids(self, value):
    if Notification.objects.filter(id__in=value, channel=ChannelChoices.EMAIL).exists():
        raise serializers.ValidationError({...READ_STATE_NOT_SUPPORTED_FOR_CHANNEL...})
```

That query is **not** filtered by `recipient`. Anyone can post a UUID and learn
whether it is an email notification in their tenant, regardless of whose it is.
The view itself is correctly scoped (`views.py:232-237`), so nothing is
modified - the leak is the error response.

It is a weak oracle (UUIDs are not guessable) but it is gratuitous: the
surrounding code goes out of its way to return `404` rather than `403`
elsewhere precisely so existence is never disclosed (`views.py:180-184`).

**Fix:** add `recipient=self.context["request"].user` to the guard query, or
drop the guard entirely and let the view's own filter silently skip email ids,
which is what it already does for foreign ids.

---

## 7. Date filters are raw strings, so a malformed value is a 500

**Medium. Same defect class as the console and user modules.**

```python
# views.py:155-161 (feed)          and  views.py:366-369 (history)
qs = qs.filter(created_at__gte=created_after)
qs = qs.filter(created_at__lte=created_before)
```

`?created_after=yesterday` raises a Django `ValidationError`, which is not a DRF
exception and so falls through to the unhandled branch of the handler as
`500 SERVER_ERROR` (`core/exceptions.py`). The same class of defect is recorded
in `docs/console/console_task_monitor.md` §8 and
`docs/user/user_security_monitoring.md` §8; the helper that fixes it is
`vs_user/views/accounts.py:51-60`.

Related, in the same handlers:

- `?is_read=` is `is_read.lower() == "true"` (`views.py:149`), so `?is_read=1`
  and `?is_read=yes` both silently mean "read".
- `?channel=` and `?status=` on history are unvalidated free text
  (`views.py:362-365`), so a typo returns an empty page rather than a `400`.

**Fix:** validate all of them through one small filter serializer, the way
`vs_audit` does with `AuditEventFilterSerializer`
(`docs/audit/audit_event_stream.md` §3).

---

## 8. Three list endpoints are unpaginated

**Medium.**

| Endpoint | Returns | Line |
|---|---|---|
| `GET settings/` | Every active `(event type × channel)` - **56 rows today** | `views.py:522-529` |
| `GET templates/` | Every template matching the filters, up to 56 | `views.py:679-702` |
| `GET event-types/` | Every active event type - 34 today | `views.py:869-873` |

None sets `pagination_class`; the feed and history viewsets both do
(`views.py:119,306`). Each row of the template list is large: a template carries
`body` plus a full `html_body` document, which for a typical seeded email is
about 3.2 KB, so an unfiltered `GET templates/` is roughly **180 KB today** and
grows with every registry entry.

**Fix:** paginate all three, or cap and document them. The matrix is the one
that genuinely wants to stay whole (the settings screen renders it as a grid);
if it does, say so in the response contract rather than leaving it implicit.

---

## 9. No audit event for template edits or settings changes

**Medium.**

Editing a `NotificationTemplate` changes the message **every tenant on the
platform** receives. It records `updated_by` and `updated_at` on the row
(`serializers.py:322`) and nothing else. There is no `emit_audit_event` call
anywhere in `vs_notifications`, and no before/after snapshot, so there is no way
to answer "who changed the invoice email, when, and what did it say before".

The same applies to `PATCH settings/update/`, which turns a tenant's
notification channel off (`views.py:622-634`).

`vs_audit` already has the vocabulary: `CONFIG_CHANGED`
(`vs_audit/models.py:122`) and the `AuditDiffService` helpers for building
before/after snapshots (`vs_audit/services.py:173-376`).

**Fix:** emit `CONFIG_CHANGED` from both write paths with a diff, module key
`CONFIG`, and the template or setting as the entity.

---

## 10. An engine app imports `vs_schools`

**Medium. A stated platform rule.**

`CLAUDE.md`: *"The engines must not import `vs_schools` (or anything under
`apps/schools/`)."*

```python
# management/commands/seed_notification_settings.py:63
from vs_schools.models import School  # Late import - avoids coupling at module load
```

The command then filters `School.objects.filter(status="ACTIVE")` and passes
each to `seed_school_settings(school)` (`services/seed.py:120`), which
immediately reduces it to `school.tenant` (`services/seed.py:152`). The school
object is used for nothing else except its slug in a log line.

`tests.py:24` imports `School` too.

**Fix:** the command should iterate `Tenant.objects.filter(kind="SCHOOL",
status=ACTIVE)` and `seed_school_settings` should take a tenant, not a school.
That removes the import entirely and the function loses nothing, because it
already only wanted the tenant. Rename it `seed_tenant_settings` and the last
school-ism in the engine is gone.

---

## 11. Smaller defects and dead code

- **The URL header comment names the wrong prefix.** `urls.py:5` says
  "All routes are prefixed with `/api/v1/notifications/`". The real mount is
  `/v1/notify/` (`apps/urls.py:29`), which is what
  `vs_tenants/middleware.py` and every client actually use. Every route in that
  16-line summary block is therefore wrong.

- **Duplicate-template detection is string matching on an exception.**
  ```python
  # views.py:719-728
  except Exception as exc:
      if "unique" in str(exc).lower():
          return error_response(..., status=409, code=DUPLICATE_TEMPLATE)
      raise
  ```
  A driver wording change turns a `409` into a `500`. Catch `IntegrityError`, or
  validate the `(event_type, channel)` pair in the serializer.

- **`_resolve_scope` returns a tuple whose second element is always `None`.**
  ```python
  # views.py:443-456
  return None, None   /   return tenant, None
  ```
  Both call sites branch on it (`views.py:524-526`, `543-545`). Dead scaffolding
  from an earlier permission model.

- **`_apply_filters` takes an `is_vision_staff` argument it never reads**
  (`views.py:327`), computed at `views.py:375` from a `User` property
  (`vs_user/models.py:323`). Left over from when CX staff were unscoped.

- **The mandatory-filter guard is decorative.** `?created_after=1970-01-01`
  satisfies it (`views.py:342-346`), so it stops an accidental unfiltered dump
  and nothing else. Either bound the window (a required date range, the way
  `vs_audit`'s export dataset does) or stop describing it as protection.

- **`?scope=platform` is meaningless for a school caller.** It filters
  `tenant__kind="PLATFORM"` on top of `tenant = request.tenant`
  (`views.py:348-349`), so it always returns nothing unless the caller is
  themselves on the platform tenant.

- **`retry_count` counts the successful attempt.** `tasks.py:200` increments it
  on the success path too, so a first-try delivery reads as `retry_count: 1` in
  the history log. Either rename it `attempt_count` or stop incrementing on
  success.

- **The `SENT`-only idempotency guard leaves `FAILED` re-runnable.**
  `tasks.py:82-87` exits early for `SENT` but not for `FAILED`, so a re-queued
  terminal-failed row is retried and re-fires `notification_failed`.
  `NotificationStatus.TERMINAL` (`constants.py:49`) already names both states;
  the guard should use it.

- **The codex fallback raises.** `Tenant.objects.get(slug="codex",
  kind=PLATFORM)` (`dispatch.py:121-123`) is an unguarded `get`. On a database
  without that row - a fresh test fixture, a partially seeded environment - a
  notification with no resolvable tenant raises `Tenant.DoesNotExist` into the
  caller's transaction. Several callers do not guard `send_notification` at all.

- **A missing or deactivated template silently drops its channel.**
  `dispatch.py:164-169` logs a warning and continues. An event whose email
  template was deactivated simply stops notifying, with nothing visible in the
  history log (no row is written) and no way to notice from the console.

- **`export.run_completed` declares a channel it deliberately has no template
  for.** The registry entry lists `["in_app", "email"]` with
  `default_enabled: True` (`constants.py`), but `DEFAULT_TEMPLATES` has only the
  in-app half, on purpose: *"Manual successes are in-app only by design: the
  person is already looking at the screen, so only failures and deliveries earn
  an email"* (`services/seed.py:1061-1073`). The decision is right; expressing
  it by omitting a template rather than by narrowing `supported_channels` is
  not. Three visible consequences: the seed logs
  `No default template defined for event_key=export.run_completed channel=email`
  on every run (observed in the suite output), which reads as a defect; the
  settings matrix shows a tenant an `export.run_completed / email` toggle that
  does nothing (`views.py:499`); and `available-events` offers the pair as
  creatable (`views.py:793-796`), so an admin can hand-create the template and
  switch on a channel the design excluded. **Fix:** drop `"email"` from that
  entry's `supported_channels`.

- **Nothing prunes `Notification`.** Every rendered body and every full HTML
  document ever sent is retained forever. `BackgroundJob` has a 90-day sweep
  (`core/tasks.py:11-24`); this table has nothing.

- **`ComplianceRule`-style dead vocabulary.** Six of the nine constants in
  `NotificationPermission` (`constants.py:93-102`) are enforced nowhere and
  seeded nowhere. That is deliberate and documented
  (`seed_notification_permissions.py:1-14`), but it means the class reads as a
  permission catalogue when only three entries are real.

---

## Recommended order of work

1. **§1 and §3 together** - one decision about which manager the feed uses,
   applied to four call sites, plus the history-log consequence. This is the
   only item that makes the product visibly wrong today.
2. **§2** - resolve settings against the recipient's tenant. Depends on the §1
   decision.
3. **§4** - a five-line fix to `_PREFIX_ROUTES` plus a coverage test. Cheapest
   real win in the module.
4. **§7 and §6** - one filter serializer and one added `recipient=` clause.
5. **§9 and §10** - the audit calls and the `vs_schools` removal, both
   self-contained.
6. **§5 and §8** - the metadata contract and pagination, both of which change
   response or caller contracts and want their own change.
