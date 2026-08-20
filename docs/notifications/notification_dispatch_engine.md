# notification_dispatch_engine

The send path, end to end. One public function, `send_notification`
(`notify.py:52`), turns an event key plus a context dict into `Notification`
rows: it resolves which channels fire, renders the stored template, writes one
row per recipient per channel, and queues email delivery after commit. No view
in this module calls it; eighteen files in other apps do.

This slice covers `notify.py`, `services/dispatch.py`, `services/settings.py`,
`services/render.py`, `services/layout.py`, `tasks.py` and `signals.py`. The
read APIs are in `notification_feed_history`; the admin APIs are in
`notification_templates_settings`.

---

## 1. What it is (and what it is NOT)

- **A fan-out recorder, not a mail queue.** Every dispatch writes durable
  `Notification` rows first (`dispatch.py:253`), then queues work. An in-app
  row is delivered the moment it is written; an email row is `PENDING` until a
  Celery task moves it.
- **Recipient-centric.** There is no school requirement. `send_notification`
  takes `recipients` (User instances) and/or `unregistered_recipients`
  (`UnregisteredRecipient(email, name)`, `dispatch.py:46`), so an invitee with
  no account and a CX staff member with no school are both first-class.
- **The event vocabulary is closed and code-owned.** `EVENT_TYPE_REGISTRY`
  (`constants.py:129`) is the only place an event is written down: 47 entries,
  34 active, 10 transactional. Migration `0008` installs it, and
  `seed_notification_event_types` resyncs (`services/seed.py:19`). An unknown
  or inactive key raises `UnknownEventTypeError` (`dispatch.py:133-136`) - it
  does not silently no-op.
- **It does NOT own the message.** Copy lives in `NotificationTemplate`, edited
  by Vision Staff. Dispatch renders whatever is stored and freezes the result
  on the row, so editing a template never rewrites history
  (`models.py:157-158`).
- **It does NOT swallow its own failures.** Unlike `vs_audit`'s
  `emit_audit_event`, `NotificationService.send` propagates. A render failure
  becomes a `FAILED` row rather than an exception (`dispatch.py:214-229`), but
  an unknown event key, a missing codex tenant, or a database error reaches the
  caller. Callers guard individually (`vs_todo/tasks.py:85-97`,
  `vs_tickets/services/notifications.py:78-80`); several do not.
- **Email delivery is not transactional with the action.** The task is queued
  through `transaction.on_commit` (`dispatch.py:286`), so a rollback never
  sends. Nothing guarantees the reverse: the row can commit and the broker
  never receive the task.

## 2. Domain model

| Model | File | Purpose |
|---|---|---|
| `NotificationEventType` | `models.py:32` | The registry row: `key`, `label`, `source_module`, `supported_channels` (JSON list), `default_enabled`, `is_transactional`, `is_active` |
| `NotificationTemplate` | `models.py:127` | One per `(event_type, channel)`: `subject`, `body`, `cta_label`, `cta_url`, `html_body`, `html_is_custom`, `is_active` |
| `NotificationSetting` | `models.py:311` | The on/off toggle: `tenant?` (null = platform default), `event_type`, `channel`, `is_enabled` |
| `Notification` | `models.py:414` | The dispatch record: `tenant`, `recipient?`, `unregistered_email`, `event_type`, `channel`, rendered `subject`/`body`/`html_body`, `metadata`, `status`, `failure_reason`, `retry_count`, `is_read`, `read_at`, `dispatched_at` |

**Three kill switches, in precedence order** (`services/settings.py:83-106`):

1. `event_type.is_active = False` - every channel off, transactional included.
2. `event_type.is_transactional = True` - every supported channel on, settings
   rows ignored entirely.
3. Otherwise the layered lookup: tenant row → platform row → `default_enabled`.

**Manager choice is load-bearing and inconsistent across the module.**
`Notification.objects` is `TenantAwareManager()` with `all_objects` as the
escape hatch (`models.py:544-545`); `NotificationSetting.objects` is
`TenantAwareManager(include_global=True)` (`models.py:375`);
`NotificationEventType` and `NotificationTemplate` use the plain default
manager, correctly, because both are global catalogues. The service layer reads
settings through `all_objects` on purpose - Celery has no ambient tenant
(`services/settings.py:66`) - but dispatch writes and the feed read through
`Notification.objects`, which does pick up the ambient tenant. See
`notification_code_issues.md` §1.

`Notification.recipient` and `Notification.tenant` are both `PROTECT`
(`models.py:442-451`), so neither a user nor a tenant can be deleted while any
notification references them.

Four indexes on `Notification` (`models.py:551-559`): the feed
`(recipient, channel, is_read, -created_at)`, two history indexes keyed on
tenant, and `(status, channel, -created_at)` for the delivery task.

## 3. The public API

`send_notification` (`notify.py:52`) is a thin pass-through to
`NotificationService.send` (`dispatch.py:77`). Arguments:

| Argument | Meaning |
|---|---|
| `event_key` | Dot-notation key from `EVENT_TYPE_REGISTRY`. Unknown or inactive raises. |
| `context` | Template variables. Keys are per-event and documented only in the registry entry. |
| `recipients` | `User` instances. |
| `tenant` | Explicit tenant for the created rows and for settings resolution. |
| `school` | Legacy convenience: `tenant` is taken from `school.tenant`. |
| `suppress` | `True` returns `[]` immediately (`dispatch.py:114-116`). |
| `unregistered_recipients` | `UnregisteredRecipient(email, name)` for people with no account. |
| `metadata` | Internal-only dict copied onto **every** created row. Never serialized. |

**`metadata` is a control surface, not just correlation data.** Four keys are
read by the engine:

| Key | Read at | Effect |
|---|---|---|
| `from_name` | `tasks.py:94,156` | Replaces the From display name via `build_from_email` |
| `bcc` | `tasks.py:100-104` | An explicit list overrides the platform `EMAIL_BCC`; an empty list suppresses it; an absent key inherits the default |
| `attachments` | `tasks.py:105,134-150` | Up to 10 files read from `default_storage` by `storage_name`, 10 MB each |
| `activation_key` | consumed by `vs_user.receivers` | Correlates an invitation with its delivery signal |
| `ticket_id`, `workflow_instance_id`, `export_run_id` | `services/routing.py:27-38` | Resolve the in-app row's click destination |

None of that is validated (`notification_code_issues.md` §5).

## 4. Lifecycle / state machine

```text
send_notification(event_key, context, recipients, tenant=…, metadata=…)
  │
  ├─ suppress? ───────────────────────────────► []
  ├─ resolve tenant: arg → school.tenant → first recipient with a tenant
  │                  → Tenant(slug="codex")            (dispatch.py:118-123)
  ├─ NotificationEventType.get(key, is_active=True)  → UnknownEventTypeError
  ├─ resolve_channels(event_type, tenant)            (services/settings.py:112)
  │      is_active=False → all off | transactional → all on | tenant→platform→default
  ├─ no enabled channel ──────────────────────► []
  ├─ _fetch_templates(active templates only)         (dispatch.py:304-315)
  │
  └─ for each target × each enabled channel:
        no template            ─► logged warning, channel skipped
        in_app + unregistered  ─► skipped (no inbox to deliver to)   :185-191
        email + no address     ─► FAILED row, reason NO_EMAIL_ADDRESS :195-207
        render raises          ─► FAILED row, reason = the error      :214-229
        in_app                 ─► SENT row, dispatched_at = now       :244-245
        email                  ─► PENDING row
  │
  ├─ bulk_create(all rows)                            (dispatch.py:253)
  └─ transaction.on_commit:                           (dispatch.py:275-286)
        PENDING email ids ─► deliver_email_notification.delay(id)
        pre-flight FAILED ─► notification_failed signal

deliver_email_notification(id)                        (tasks.py:30)
   already SENT ─► return                                      :82-87
   no address   ─► FAILED + notification_failed                :114-128
   send_email   ─► SENT + dispatched_at + notification_sent    :194-210
   exception    ─► retry_count += 1, failure_reason stored     :162-167
                   eager or retries exhausted ─► FAILED + signal :180-192
                   otherwise ─► self.retry(countdown=backoff)    :177-178
```

`NotificationStatus.TERMINAL` is `{SENT, FAILED}` (`constants.py:49`), and the
task's only real idempotency guard is the `SENT` check - a `FAILED` row
re-queued would be retried.

## 5. Derivations

- **Tenant resolution is a three-step fallback** (`dispatch.py:118-123`):
  the explicit `tenant` argument, then `school.tenant`, then the **first**
  recipient that has a tenant, then the codex platform tenant. The third step
  is where multi-tenant recipient lists go wrong (`notification_code_issues.md`
  §1); the fourth raises `Tenant.DoesNotExist` on a database with no codex row.
- **Channel resolution** (`services/settings.py:21-108`) fetches every relevant
  settings row in **one** query scoped to `tenant IS NULL OR tenant = <tenant>`,
  splits them into a platform map and a tenant map, and resolves per channel.
  `resolve_channels` is a single-event wrapper so the layering lives in one
  place (`services/settings.py:112-122`).
- **The IN_APP invariant is a write-side rule only.** `resolve_channels_bulk`
  reads persisted rows verbatim and never overrides them
  (`services/settings.py:39-42`); "in-app cannot be disabled" is enforced in
  the settings PATCH handler (`views.py:605-612`). A row written by any other
  path stays honoured.
- **Rendering** (`services/render.py:79-137`) produces three strings. Subject
  and plain body render with `autoescape=False` - escaping would put `&amp;` in
  front of a reader. The HTML render is the opposite: the stored markup is
  trusted (staff wrote it), the substituted values are not, so it renders with
  `autoescape=True` (`services/render.py:118-124`).
- **The email HTML is stored, not composed at send time.**
  `NotificationTemplate.save()` regenerates `html_body` from the shared layout
  on every save while `html_is_custom` is False, and blanks it for non-email
  channels (`models.py:265-285`). `render_notification_template` calls
  `compose_email_html` only as a safety net for a row written by a path that
  bypassed `save()` (`services/render.py:126-136`).
- **The layout infers structure from plain text** (`services/layout.py:242-284`):
  two or more `Label: value` lines become a details table, `-`/`•` lines become
  a list, a short ALL-CAPS line becomes a heading, a rule line becomes an `<hr>`,
  anything else is a paragraph. Every value passes through
  `urlize(autoescape=True)` (`services/layout.py:411-419`), which escapes first
  and linkifies second, so a rendered value carrying markup is inert.
- **`_TagGuard`** (`services/layout.py:78-118`) swaps Django tags for
  alphanumeric tokens before escaping and restores them afterwards. Without it,
  escaping `{% if origin == 'ADMIN' %}` into `&#x27;` would silently break the
  stored template at the moment a real person is being emailed.
- **A call-to-action only becomes a button for absolute `http(s)`**
  (`services/layout.py:178-180`). A `javascript:` destination is dropped, and
  so is a relative path, which cannot resolve in a mail client anyway. Template
  documents are exempt because their destination is a `{{ placeholder }}`.
- **Retry configuration is read live**, per task execution, from
  `vs_config.runtime_settings`, falling back to `NotificationConfigKey.DEFAULTS`
  (3 retries, 60s) when that fails (`tasks.py:214-231`). Backoff is fixed, not
  exponential.
- **Eager mode is a deliberate special case.** Under
  `CELERY_TASK_ALWAYS_EAGER` the task runs inline in the HTTP request, where
  `self.retry()` would raise `celery.exceptions.Retry` straight through the
  caller. The first failure is therefore treated as final
  (`tasks.py:174-178`).

## 6. What dispatch actually writes

Per call: **one `bulk_create`** of `len(targets) × len(enabled_channels)` rows
minus the skipped combinations (`dispatch.py:253`). No `save()` runs, so no
model-level validation applies to a dispatched row.

Per successful email: three short `select_for_update` transactions in the task
(fetch, then one write for the outcome), with the SMTP call deliberately
**outside** the row lock so a slow network call never holds it
(`tasks.py:76-80,195-204`).

Two signals are published (`signals.py:26-27`), both with a single
`notification` kwarg and `sender=Notification`:

- `notification_sent` - the email reached `SENT` (`tasks.py:210`).
- `notification_failed` - the email reached `FAILED`, whether in the task
  (`tasks.py:127,191`) or pre-flight, fired from `on_commit` because no task
  will run for those rows (`dispatch.py:280-284`).

Nothing here writes an audit event. A notification that went to the wrong
person leaves a `Notification` row and nothing in `vs_audit`.

## 7. Worked example

```python
from vs_notifications.notify import send_notification, UnregisteredRecipient

send_notification(
    event_key="user.invited",
    context={"invitation_url": url, "school_name": school.name,
             "inviter_name": actor.full_name},
    recipients=[],
    tenant=school.tenant,
    unregistered_recipients=[UnregisteredRecipient(email="new@staff.com",
                                                   name="Jane Doe")],
    metadata={"activation_key": key, "from_name": actor.full_name},
)
# → ["3f9c1a2e-…"]
```

`user.invited` is transactional, so the settings matrix is bypassed. The
invitee has no account, so the in-app half is skipped (`dispatch.py:185-191`)
and exactly one `PENDING` email row is written. After commit,
`deliver_email_notification` renders nothing further (the body was already
frozen), builds the From address from `from_name`, sends multipart, flips the
row to `SENT`, and fires `notification_sent` - which is how `vs_user` learns
the invitation was delivered, correlating on `metadata["activation_key"]`.

## 8. Gotchas / known limitations

Every item is stated in full, with evidence, in
**`docs/notifications/notification_code_issues.md`**. The ones that belong to
this slice, in severity order:

- **Rows are written under the initiating tenant, not the recipient's, and the
  feed filters by tenant.** The confirmed instance is `ticket.created`. Under
  eager mode - which staging defaults to - the delivery task cannot find the
  row either, so the email is dropped as well and the row stays `PENDING`
  forever (`notification_code_issues.md` §1).
- **A school's settings decide whether CX staff get notified** for the same
  reason (`notification_code_issues.md` §2).
- **The codex fallback raises on a database without a codex tenant**
  (`dispatch.py:121-123`).
- **`metadata` is an unvalidated control surface**: `attachments` will read any
  path in `default_storage`, `bcc` will mail anyone
  (`notification_code_issues.md` §5).
- **`retry_count` counts attempts including the successful one**
  (`tasks.py:200`), so a first-try success reads as `retry_count: 1`.
- **A missing template silently drops the channel** with only a log line
  (`dispatch.py:164-169`); an event whose template was deactivated stops
  notifying with no visible signal. `export.run_completed` is a live instance:
  it declares `email` but ships no email template, on purpose, so the seed warns
  on every run and the settings matrix offers a toggle that does nothing
  (`notification_code_issues.md` §11).
- **Nothing prunes `Notification`.** The table grows forever, holding every
  rendered body and HTML document ever sent.
- **Justified by design:** in-app rows for unregistered recipients are skipped
  rather than recorded `FAILED` (`dispatch.py:171-191`). The comment there is
  the right argument: nobody meant to send anything, and a `FAILED` row would
  be exactly as unreadable as the `SENT` one it replaces.
- **Justified by design:** `autoescape=False` for text and `True` for HTML
  (`services/render.py:107-124`), with `_text()` as the single escaping choke
  point in the layout (`services/layout.py:411-419`). Tested at
  `tests.py:1002-1020`.
- **Justified by design:** SMTP runs outside the row lock (`tasks.py:43-50`).

## 9. Permissions & tenant isolation

This slice has **no endpoints and therefore no permission keys**.
`send_notification` is an internal Python API: any code that can import it can
notify anyone, on any event, with any context and any `metadata`. That is the
correct shape for an engine, and it means the security boundary is entirely in
the calling module.

Tenant isolation is decided by one argument. `tenant=` (or `school=`) sets the
column that the feed, the history log and the settings resolution all key off.
Getting it wrong is not a validation error; it is a silently misfiled row.

## 10. Code map

| File | Responsibility |
|---|---|
| `notify.py` | `send_notification`, `UnregisteredRecipient` - the only import other apps should use |
| `services/dispatch.py` | `NotificationService.send` and its five private builders |
| `services/settings.py` | `resolve_channels` / `resolve_channels_bulk` - the layering, in one place |
| `services/render.py` | Syntax validation, fragment rendering, and the three-string dispatch render |
| `services/layout.py` | `compose_email_html`, `_TagGuard`, the block parser, the escaping choke point |
| `tasks.py` | `deliver_email_notification`, `_read_retry_config` |
| `signals.py` | `notification_sent`, `notification_failed` |
| `constants.py` | `ChannelChoices`, `NotificationStatus`, error codes, config keys, `EVENT_TYPE_REGISTRY` |
| `exceptions.py` | Typed domain errors, each carrying an error code |
| `core/mail.py` | `send_email`, `build_from_email` - the platform BCC and From conventions |
| `services/seed.py` | `seed_event_types`, `seed_platform_settings`, `seed_notification_templates` |

## 11. Test coverage & gaps

The module ships 85 tests in one file, all green at the time of writing
(`Ran 85 tests in 380.453s` - OK). The ones that exercise this slice:

- `ResolveChannelsTests` (`tests.py:142-190`) - default fallback, platform beats
  default, tenant beats platform, transactional bypasses a disabled row,
  `is_active` kills everything.
- `ResolveChannelsBulkTests` (`tests.py:192-254`) - layering across several event
  types in one call, and the query-count assertions (one settings query for the
  bulk resolve, two for the matrix build).
- `DispatchTests` (`tests.py:256-429`) - records created and email enqueued with
  no school, the unregistered/in-app skip, metadata stored but never
  serialized, HTML stored, the pre-flight `FAILED` signal, all-channels-off
  returning `[]`.
- `DeliveryTaskTests` (`tests.py:431-585`) - `SENT` plus signal, multipart vs
  plain, `from_name`, the three `bcc` cases, attachment loading from storage,
  and the eager-mode no-retry rule.
- `EmailLayoutTests` / `StoredEmailHtmlTests` (`tests.py:954-1180`) - structure
  inference, injection resistance for both generated and hand-written markup,
  the CTA button and its non-http rejection, placeholder survival through
  escaping, and a sweep asserting every seeded email template composes a
  document.

Genuinely uncovered:

1. **Cross-tenant dispatch.** No test sends to a recipient whose tenant differs
   from the `tenant=` argument and then reads that recipient's feed, which is
   why §8's first item is live.
2. **The tenant fallback chain.** Neither the first-recipient step nor the
   codex step is exercised, and nothing asserts what happens when recipients
   span two tenants.
3. **Retry behaviour outside eager mode.** `self.retry` is never driven, so the
   `max_retries` boundary, the fixed backoff and `_read_retry_config`'s
   `vs_config` branch are all untested.
4. **The missing-template path** (`dispatch.py:164-169`) - no test asserts that
   a deactivated template drops its channel silently.
5. **`metadata` validation** - no test for a malformed `attachments` entry, an
   oversized file, a `storage_name` pointing somewhere unexpected, or a
   non-list `bcc`.
6. **Concurrency** - two delivery tasks for the same row, and the `FAILED`
   re-queue path that the `SENT`-only guard does not cover.
