# ticket_context_integrations

Everything that crosses the module boundary: the product-context allowlist and
the registry other modules extend it through, the eight notification events, the
Export Centre dataset and screen binding, the platform audit mirror, and the
admin console card. The ticket itself is `ticket_lifecycle`; the thread on it is
`ticket_conversation_attachments`; the rules are `ticket_visibility_permissions`.

The theme of this slice is a single constraint: **`vs_tickets` is a
domain-neutral engine app and must not import anything under `apps/schools/`.**
Every mechanism below exists because of it, and one test pins it
(`tests.py:865-882`).

---

## 1. What it is (and what it is NOT)

- **`Ticket.context` is not a metadata bag.** It is a closed allowlist, validated
  on write by `TicketContextSerializer` (`serializers.py:125`) and filtered again
  on read by `TicketSerializer.get_context` (`serializers.py:67-72`), so a key
  that stops being allowed also stops being returned on rows that already carry
  it.
- **The registry is not a plugin API for arbitrary fields.** Every registered key
  is a `ChoiceField` over a fixed tuple (`context.py:40-77`). Not a regex, not
  free text. A registered key that accepted arbitrary strings would undo the
  allowlist for every module at once.
- **The registry is not discoverable by the frontend.** There is no endpoint that
  lists the allowed keys or their vocabularies, and the `description` a module
  passes when registering is accepted and discarded (`context.py:40`, §8).
- **Notifications are best-effort and out-of-band.** They are queued on
  `transaction.on_commit` and their exceptions are caught and logged
  (`services/notifications.py:78-82`), so a broken template cannot roll back a
  ticket. The flip side is that a lost notification is a log line, nothing more.
- **The export is not the API in file form.** It is tenant-scoped where the API
  is participant-scoped, which is the module's worst live defect (§8).
- **The audit mirror is not the ticket's own log.** `record_ticket_audit` writes
  both, and only the local `TicketAuditLog` is shown on the ticket
  (`ticket_conversation_attachments` §2).

## 2. The context allowlist

Four keys are declared by this app (`context.py:31`, validated at
`serializers.py:135-146`):

| Key | Shape |
|---|---|
| `guide_id` | `^[a-z0-9][a-z0-9.-]{0,119}$` |
| `route_pattern` | `^/[a-z0-9_./:-]{0,199}$`, and then no `?`, no `#`, and **no digit at all** (`serializers.py:168-173`) |
| `product_area` | one of twenty fixed values - Account … Workflow |
| `app_version` | `^[A-Za-z0-9._+-]{1,40}$` |

`to_internal_value` rejects a non-object outright and names every unknown key
individually (`serializers.py:158-166`), so a client learns which field was
refused rather than being told "context is invalid".

`route_pattern`'s digit ban is the interesting rule: a placeholder route
(`/finance/invoices/:id`) proves the record identifier was removed, and a digit
is the evidence that it was not. It is a blunt instrument - see §8.

### The registry (`context.py:40`)

```python
register_context_choice_field("onboarding_task_key", choices=TaskKey.values)
```

Called from the owning app's `AppConfig.ready()`. Rules the function enforces:

- the key matches `^[a-z][a-z0-9_]{2,39}$` and must be namespaced by its module;
- it may not shadow one of the four `CORE_KEYS`;
- it must publish at least one value, and every value must match
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` - a plain identifier, never a sentence;
- re-registering the identical vocabulary is a no-op (`ready()` can run twice in
  a test process); re-registering **different** values raises
  `ImproperlyConfigured`, because two modules disagreeing about what a key means
  is not something to resolve silently at import time.

`TicketContextSerializer.__init__` reads the registry per instance rather than at
import time (`serializers.py:148-156`), because a module registers from its own
`ready()` and URL loading can import this file first.

Today there is exactly one registrant: `schools/vs_onboarding/ticket_context.py`
publishes `onboarding_task_key` and `onboarding_readiness_state` from its own
constants. That is the direction that keeps the rule: the module that owns the
vocabulary is the module that publishes it, and `vs_tickets` learns nothing about
schools.

## 3. Notifications - the eight events

Registered in `vs_notifications/constants.py:198-262`. All eight support
`in_app` and `email`, all are `default_enabled: True`, and **none is marked
`is_transactional`**, which means all eight can be switched off per tenant
through notification settings.

| Event | Fired by | Recipients |
|---|---|---|
| `ticket.created` | `create_ticket` | `support_recipients()` - the CX triage queue |
| `ticket.assigned` | `assign_ticket`, only when an assignee is set | the new assignee |
| `ticket.status_changed` | `transition_ticket`, when no more specific key applies | requester + assignee + active followers |
| `ticket.resolved` | transition to `RESOLVED` | requester + assignee + active followers |
| `ticket.closed` | transition to `CLOSED` | requester + assignee + active followers |
| `ticket.reopened` | any transition **out of** `CLOSED` | requester + assignee + active followers |
| `ticket.commented` | `add_comment`, both visibilities | see below |
| `ticket.attachment_added` | `add_attachment` | requester + assignee + active followers, plus the queue while unassigned |

`notify_commented` builds its audience from the requester, current assignee and
active ticket subscriptions:

- an **internal** note reaches only recipients who currently pass
  `can_view_internal_notes`, so it never leaks to an ordinary requester;
- a **public** reply reaches every active, unmuted recipient who can still view
  the ticket; and when the ticket is still
  unassigned and the author is the requester, the whole support queue is added
  as the other side of the conversation. Without that, a requester chasing
  their own unanswered ticket would
  produce a recipient list of exactly themselves, which
  `_unique_recipients` would then empty as an actor echo.

`notify_attachment_added` repeats that unassigned-queue rule and applies the
comment's internal visibility to an attachment linked to an internal note.

These behaviours apply at the shared dispatch boundary:

- **`_unique_recipients` (19)** drops `None`s, duplicates, and the actor - you are
  never told about your own action.
- inactive users, muted subscriptions and users who have lost ticket access are
  removed before dispatch; internal events additionally require current
  internal-note access. A subscription never grants ticket visibility.
- **`dispatch_ticket_event`** exits early on an empty list, builds
  `context_for(ticket)` (48) - number, title, status, priority, category,
  requester and assignee names - and stamps
  `metadata = {"ticket_id", "ticket_number"}`. That metadata is what
  `vs_notifications`'s router turns into the in-app destination
  `/support/tickets/<id>` (`vs_notifications/services/routing.py:27-29`) and what
  `acknowledge-route` reads back to mark exactly that ticket's rows read (52-59).

`add_comment` creates or reactivates a `TicketSubscription` for its author.
`POST /tickets/<pk>/follow/` follows without commenting, while `DELETE` on the
same route records a mute preference. A later comment follows the ticket again.

**Dispatch passes the ticket's tenant** (`services/notifications.py:123`) while
the recipients of `ticket.created` are on the platform tenant. That is now the
message's ORIGIN rather than its owner: since `373a918` the engine stamps each
row with its recipient's own tenant and records the ticket's tenant separately,
so the mismatch that used to be this module's critical inherited defect is
closed - see §8 and `error/notifications/notification_code_issues.md` §1.

## 4. The Export Centre

Registered from `AppConfig.ready()` (`apps.py`), never from `vs_exports`, so the
engine never imports a domain app.

### Dataset `support.tickets` (`export_datasets.py:42`)

- **Scope** `DatasetScope.TENANT`; base queryset
  `Ticket.all_objects.filter(tenant=scope.tenant)` (30-32).
- **Permission** `tickets.ticket.view`. Row cap 200,000.
- **Columns**: `ticket_number` (locked), `title`, `category`, `priority`,
  `status`, `source`, `created_at`, `resolved_at`, `closed_at`,
  `requester_email` (**sensitive** - needs `exports.sensitive_field.export` as
  well), `assignee_email` (not sensitive).
- **Filters**: a **required** `created_at` date range (the primary date), plus
  status, priority, category, a search over number and title, and a title
  contains.
- **The ticket body is deliberately not offered**, as a column or as a search
  target. The module docstring says why: descriptions are free text that people
  paste account numbers and screenshots of payslips into, and a catalogue that
  offers it invites exactly that data out of the building in a spreadsheet.

### Screen binding (`export_datasets.py:145`)

Binds the ticket list screen so a filtered table becomes a one-click export.
`_translate_tickets` (91) maps `state`, `status`, `priority`, `category`,
`created_from`/`created_to` (into the one date-range filter that also satisfies
the required window) and `q`.

Two translations are worth knowing:

- **`state=active` expands through `ACTIVE_TICKET_STATUSES`** (103-105), the same
  constant the list view filters on. Treating "active" as a literal status would
  match nothing and produce an empty file from a screen full of rows.
- **Four screen filters are reported as unmapped** rather than silently dropped:
  `assignee`, `requester`, `assigned_to_me`, `school` (129-136), each with a
  sentence saying what the file will therefore contain.

## 5. The audit mirror and the console card

`record_ticket_audit` (`services/audit.py:13`) writes the local row and then
`emit_audit_event(module_key="SYSTEM", action_type="CUSTOM",
entity_type="Ticket", entity_id=<pk>, entity_label=<ticket_number>,
tenant=ticket.tenant, …)`.

`tenant=ticket.tenant` is explicit and load-bearing (27-31): a CX agent working a
Bright Star ticket asserts `?tenant=codex`, and inheriting the request's tenant
would file the school's support history under Codex.

`vs_admin_console`'s landing overview adds a tickets card
(`vs_admin_console/overview.py:212-231,514-517`) for anyone who is support or
holds `tickets.ticket.view`. Its two numbers come from `visible_tickets_qs`
filtered by `ACTIVE_TICKET_STATUSES`, so the card obeys the same boundary as the
list and cannot show a count the reader could not open. It is the correct
pattern, and the contrast with the export in §4 is exactly the point.

## 6. What crossing the boundary writes

| Direction | Writes |
|---|---|
| → `vs_notifications` | one `Notification` row per recipient per enabled channel, owned by that *recipient's* tenant, with the ticket's tenant recorded as the message's origin |
| → `vs_audit` | one event per ticket write, stamped with the ticket's tenant, carrying before/after and impersonation metadata |
| → `vs_exports` | nothing at registration; an export run writes its own rows, and a run including `requester_email` is recorded as a sensitive-field event (`vs_exports/audit.py:66`) |
| ← `vs_onboarding` | nothing persistent - two entries in a process-local dict (`context.py:84`) |

The registry is in-memory and rebuilt at every process start. Nothing about it is
stored, so a key that stops being registered simply disappears from both
validation and output - which is the intended behaviour, and why the read path
filters through `allowed_keys()` rather than returning whatever the column holds.

## 7. Worked example

A school admin is stuck on the go-live checklist and files a ticket from the
onboarding screen:

```text
POST /v1/support/tickets/?tenant=bright-star
{"title": "Cannot mark payroll set up",
 "description": "The tick does not save.",
 "category": "SUPPORT", "priority": "HIGH",
 "context": {"product_area": "Onboarding",
             "onboarding_task_key": "PAYROLL_SETUP",
             "onboarding_readiness_state": "BLOCKED",
             "route_pattern": "/onboarding/checklist"}}
```

All four keys pass: two are core, two are registered by `vs_onboarding`. Change
`"onboarding_task_key"` to a value outside `TaskKey` and the response is a `400`
naming that field; add `"user_email"` and the response is a `400` saying *This
context field is not allowed.*; change `route_pattern` to
`/onboarding/checklist/2026` and it is refused for the digit.

On commit, `ticket.created` dispatches to the CX triage queue with
`metadata = {"ticket_id": 4471, "ticket_number": "TK-72608213"}`, and each in-app
row resolves to `/support/tickets/4471`.

The CX agents' rows are owned by the platform tenant, so their in-app feed and
queue badge pick them up, and Bright Star's own delivery history never returns
them. One thing still goes wrong, documented in §8: Bright Star is still PENDING,
so when the admin follows the email reply back into the app, the ticket's detail
page refuses them.

## 8. Gotchas / known limitations

Full evidence in **`error/tickets/ticket_code_issues.md`**.

- **FIXED (`373a918`): new tickets did not reach the CX in-app queue, and the
  school could read CX's mail instead.** Rows were stamped with the ticket's
  tenant while the recipients were on the platform tenant. Ownership now follows
  the recipient and the ticket's tenant is recorded as the origin, so the queue
  badge moves and the school's history log no longer returns those rows,
  including the body of an internal note
  (`error/notifications/notification_code_issues.md` §1,
  `ticket_code_issues.md` §1a).
- **FIXED (`373a918`): a school could switch off the CX support team's
  notifications.** None of the eight events is `is_transactional`, but channel
  settings now resolve per owning tenant rather than against the event's, so a
  school's toggle reaches its own people only (`ticket_code_issues.md` §1b).
- **The export ignores the participant boundary.** `tickets.ticket.view` is
  seeded to every teacher and gates a dataset over every ticket in the tenant
  (`ticket_code_issues.md` §3).
- **`assignee_email` is not marked sensitive** while `requester_email` is
  (`export_datasets.py:73-77`), so a school export carries CX staff addresses
  without the extra key.
- **`support_recipients` is the weakest of three "who is support" queries**
  (`ticket_code_issues.md` §2): it reads only direct role permissions, so it
  misses group-granted agents, and it keeps notifying agents whose grant was
  denied or whose role was archived.
- **An internal note written by the assignee notifies nobody**
  (`services/notifications.py:141-142`), so a second-line note reaches no other
  agent.
- **`route_pattern` refuses any digit**, which also refuses every versioned or
  numbered route - `/v1/...` included (`serializers.py:171`). The intent (strip
  record ids) is right; the test is broader than the intent
  (`ticket_code_issues.md` §16 item 4).
- **`register_context_choice_field`'s `description` argument is discarded**
  (`context.py:40`), and there is no endpoint exposing the allowlist, so the
  frontend has to hardcode the twenty `product_area` values and every registered
  vocabulary (`ticket_code_issues.md` §16 item 5).
- **A failed dispatch is a `logger.warning` and nothing else**
  (`services/notifications.py:78-82`) - no retry, no dead-letter, no counter.
- **Justified by design:** the ticket body is neither exportable nor searchable
  in the Export Centre (`export_datasets.py:1-9,52-56`).
- **Justified by design:** the audit mirror stamps the ticket's tenant rather
  than the request's (`services/audit.py:27-31`).
- **Justified by design:** the registry raises rather than merging when two
  modules claim one key with different values (`context.py:75-79`).

## 9. Permissions & tenant isolation

| Surface | Gate |
|---|---|
| Writing context on a ticket | none beyond filing a ticket - the allowlist is the control |
| Reading context back | whatever lets you read the ticket; the allowlist filters again |
| Receiving a notification | being a resolved recipient; channel settings resolve against the ticket's tenant |
| Exporting `support.tickets` | `tickets.ticket.view`, plus `exports.sensitive_field.export` for `requester_email` |
| Reading the audit mirror | `vs_audit`'s own keys, not this module's |
| The console card | support, or `tickets.ticket.view` |

Isolation is inherited everywhere except the export. Notifications carry the
ticket's tenant; audit events carry the ticket's tenant; the console card reuses
`visible_tickets_qs`. The export substitutes `tenant = scope.tenant` for the
participant rule, which is why it is the one integration that widens access
rather than passing it through.

## 10. Code map

| File | Responsibility |
|---|---|
| `context.py` | `CORE_KEYS`, `register_context_choice_field`, `registered_choice_fields`, `allowed_keys` |
| `serializers.py:125-173` | `TicketContextSerializer` - the allowlist, enforced |
| `serializers.py:65-72` | `get_context` - the read-side filter |
| `services/notifications.py` | recipients, context, `dispatch_ticket_event`, the six `notify_*` entry points |
| `services/audit.py:13-58` | the local log plus the `vs_audit` mirror |
| `export_datasets.py:30-142` | the `support.tickets` dataset |
| `export_datasets.py:145-157` | the screen binding and `_translate_tickets` |
| `apps.py` | `ready()` - dataset and screen registration |
| `vs_notifications/constants.py:198-262` | the eight event definitions |
| `vs_notifications/services/routing.py:27-29,54-59` | ticket URL out, ticket route back |
| `schools/vs_onboarding/ticket_context.py` | the one registrant, and why it lives there |
| `vs_admin_console/overview.py:212-231` | the tickets card |

## 11. Test coverage & gaps

- `test_ticket_creation_keeps_only_validated_product_context` (`tests.py:203`)
  and `test_ticket_context_rejects_unknown_keys_and_live_url_data` (239) - the
  allowlist in both directions.
- `test_a_ticket_may_carry_the_registered_onboarding_context` (262),
  `test_a_registered_key_still_refuses_a_value_outside_its_vocabulary` (293),
  `test_an_unregistered_key_is_still_rejected_after_registration_exists` (309).
- `TicketContextRegistryTests` (`tests.py:793-882`) - onboarding's two keys and
  their closed vocabularies; a non-identifier value is refused; a key cannot
  shadow a core key; identical re-registration is a no-op and a conflicting one
  raises; an empty vocabulary raises; and
  `test_vs_tickets_does_not_import_the_school_package` - the import rule itself.
- `test_requester_reply_on_unassigned_ticket_notifies_support_queue`
  (`tests.py:328-353`) - the queue is added when the requester chases an
  unassigned ticket, asserted on the recipient set with `NotificationService.send`
  mocked and `captureOnCommitCallbacks`.
- `vs_exports/tests.py` exercises the catalogue generally, including
  tenant-scoped datasets and the sensitive-field gate.

What the suite does not cover:

1. **Any notification except the commented-on-unassigned case.** Seven of the
   eight events have no test: not the recipient set, not the event key chosen by
   `notify_status_changed`, not the reopen mapping, not the internal-note
   audience of one, not the actor-echo suppression.
2. **`support_recipients` itself** - no test grants the triage keys through a
   group, or denies them, and then asserts who is notified. This is the test that
   turns §8's third item red.
3. **The tenant a notification is stamped with.** Nothing here asserts what
   `NotificationService.send` receives as `tenant=`. Since `373a918` that
   argument is only the message's origin, and ownership is covered where it is
   decided (`vs_notifications.tests.NotificationOwnershipTests`).
4. **The `support.tickets` dataset** - no test in this module runs it, and none
   asserts that its rows match what the API would show the same caller.
5. **`_translate_tickets`** - neither the `state=active` expansion nor the four
   unmapped reports.
6. **The audit mirror** - `emit_audit_event` is never asserted, so nothing pins
   the `tenant=ticket.tenant` decision that §5 calls load-bearing.
7. **`route_pattern`'s digit rule** against a realistic route, and `guide_id` /
   `app_version` shapes.
