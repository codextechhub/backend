# notification_templates_settings

The administration half of the module: **who can turn a notification off**
(the effective settings matrix and its overrides), **what it says** (template
CRUD and live preview), and **what events exist at all** (the read-only
catalogue). Routes are mounted at `/v1/notify/` (`apps/urls.py:29`):
`settings/`, `settings/update/`, `templates/`, `templates/available-events/`,
`templates/<uuid>/`, `templates/<uuid>/preview/`, `event-types/`,
`event-types/<uuid>/`.

---

## 1. What it is (and what it is NOT)

- **Settings are a four-layer resolve, exposed as a flat matrix.** `GET
  settings/` returns one row per `(active event type × supported channel)` with
  the resolved value and the layer that produced it. `PATCH settings/update/`
  upserts override rows addressed by `(event_type_key, channel)`, never by row
  id. The layers, most specific first: **branch → tenant → platform → default**.
- **Scope comes from the asserted tenant, narrowed by an optional branch.**
  There is no `?school=`. A business tenant manages its own override rows, and
  `?branch=<id>` narrows that to one of its branches. A `PLATFORM`-kind tenant
  (CX staff) manages the **tenant-NULL default layer** every tenant inherits,
  not codex's own rows; the platform layer has no branches, so `?branch=` there
  is a `404`. CX staff cannot target one school's settings from here.
- **A branch counts only for events that are sent with one.** An email belongs
  to the branch the event is ABOUT: the branch an invoice, receipt, note or
  statement is filed under, the branch a workflow document belongs to. An event
  whose every sender passes that branch is `branch_scoped` and may be set per
  branch. An event sent without one (an export being ready, a ticket reply,
  onboarding) stays a whole-school setting.
- **Templates are a global catalogue, not tenant data.** One
  `NotificationTemplate` per `(event_type, channel)`, enforced by
  `unique_together` (`models.py:256`). Editing one changes the message **every
  tenant** receives. `NotificationTemplateViewSet.get_queryset` applies no
  tenant filter, correctly (`views.py:677-683`).
- **Event types are read-only through the API.** They are installed by
  migration `0008` from `EVENT_TYPE_REGISTRY` and resynced by
  `seed_notification_event_types`; nothing creates one over HTTP
  (`models.py:36-39`).
- **Preview writes nothing.** It renders the stored template, or an unsaved
  draft, against generated sample values and returns JSON. No `Notification`
  row, no mail (`views.py:814-851`).
- **Four things cannot be configured**, and each is refused with its own error
  code rather than silently ignored: a transactional event
  (`TRANSACTIONAL_NOT_CONFIGURABLE`), the in-app channel being switched off
  (`IN_APP_ALWAYS_ENABLED`), a channel the event does not support
  (`UNSUPPORTED_CHANNEL`), and, at a branch, an event that is not
  `branch_scoped` (`BRANCH_NOT_CONFIGURABLE`). Transactional events always send
  and in-app is always on, at every scope.

## 2. Domain model

| Model | File | Notes |
|---|---|---|
| `NotificationEventType` | `models.py` | 47 registry entries, 34 active, 10 transactional, 13 registered-but-inactive; `branch_scoped` on 10 (see §9) |
| `NotificationTemplate` | `models.py:127` | `subject`, `body`, `cta_label`, `cta_url`, `html_body`, `html_is_custom`, `is_active`, `created_by`, `updated_by` |
| `NotificationSetting` | `models.py` | `tenant?`, `branch?`, `event_type`, `channel`, `is_enabled`, `updated_by` |

**`is_active=False` on an event type is an honesty flag, not a bug.** The
registry comment is explicit: an event stays inactive until a domain module
actually emits it, and the flag is flipped in the same change that adds the
`send_notification` call (`constants.py:121-125`). Thirteen entries are
currently in that state, and they are correctly absent from the settings matrix
(`views.py:475`), the catalogue (`views.py:871`) and the creatable template set
(`views.py:796`).

**Three conditional unique constraints and a check** keep the layering honest:
at most one whole-tenant row per `(tenant, event_type, channel)` (branch null),
at most one branch row per `(tenant, branch, event_type, channel)`, at most one
platform row per `(event_type, channel)` (tenant null), and a branch row always
names its tenant. Postgres enforces all four, and the test suite runs on
Postgres for exactly that reason. A deleted branch takes its own rows with it
(`CASCADE`): nothing else reads them.

**`NotificationTemplate.save()` is where the markup stays honest**
(`models.py:265-285`). For an email template with `html_is_custom=False` it
regenerates `html_body` from the shared layout on **every** save, and it forces
`html_body`/`html_is_custom` into any `update_fields` set so a partial save
still persists what it just recomputed. For a non-email channel it blanks both.
That logic sits on the model rather than in the serializer because the API, the
Django admin, the seed command and any future data migration all write through
it.

## 3. Endpoint map

`?tenant=<slug>` is required on all eight routes
(`vs_rbac/authentication.py:123-126`).

### Settings - key `communication.communication_permissions.enforce`

| Method + path | query / body | response |
|---|---|---|
| `GET settings/` | optional `?branch=<id>` | Flat list of matrix rows at that scope, **unpaginated** |
| `PATCH settings/update/` | optional `?branch=<id>`; `{"updates": [{"event_type_key", "channel", "is_enabled"}, …]}`, min 1 | The touched rows, freshly resolved at the same scope |

Matrix row shape: `event_type_key`, `event_type_label`, `source_module`,
`channel`, `is_enabled`, `is_transactional`, `source`, `branch_scoped`,
`can_edit`.

- `source` is `"branch"`, `"tenant"`, `"platform"` or `"default"`: the layer the
  value came from. `"branch"` appears only with `?branch=`, on a
  `branch_scoped` event that branch has set.
- `branch_scoped` says the event is sent with a branch, so a branch may set it.
- `can_edit` says whether **this caller** may change **this row** at **this
  scope**. It is false for a transactional event, for the in-app channel, for
  a non-`branch_scoped` event when `?branch=` is given, and for every row at
  the whole-school scope when the caller's branch reach is limited (a branch
  admin).

`?branch=` must name a branch of the asserted tenant that the caller may work
in (`vs_rbac.scoping.visible_branch_ids`). Anything else - unknown, malformed,
another tenant's, or a branch outside the caller's reach - is the same `404`,
so the parameter cannot confirm an id exists.

At a branch, `is_enabled: true | false` writes that branch's row and
`is_enabled: null` removes it, so the branch follows the school again. Without
a branch, `null` is refused (`RESET_NEEDS_BRANCH`): the whole-school row is on
or off. A caller whose reach is limited to some branches gets `403`
`BRANCH_SCOPE_REQUIRED` on a PATCH without `?branch=`.

The PATCH is **all-or-nothing**: every item is validated first, and any error
returns `400` with a per-index list of `{index, error_code, message}` before a
single row is written. Only then does one atomic block write them all.

### Templates - key `communication.notification_templates.configure` (`views.py:674-675`)

| Method + path | query / body | response |
|---|---|---|
| `GET templates/` | `event_type_key`, `channel`, `search` | All matching templates, **unpaginated** (`views.py:685-708`) |
| `POST templates/` | `event_type`, `channel`, `subject`, `body`, `cta_label`, `cta_url`, `html_body`, `html_is_custom`, `is_active` | `201`, or `409` `DUPLICATE_TEMPLATE` (`views.py:710-740`) |
| `GET templates/available-events/` | - | `(event type, channel)` pairs with no template yet (`views.py:783-812`) |
| `GET templates/<uuid>/` | - | One template, or `404` |
| `PATCH templates/<uuid>/` | same as POST | The updated template |
| `GET|POST templates/<uuid>/preview/` | `{"context": {…}, "draft": {…}}` (POST only) | Rendered subject, body, HTML, source markup, variables, context used (`views.py:814-851`) |

### Event types - `IsAuthenticated` only (`views.py:868`)

| Method + path | response |
|---|---|
| `GET event-types/` | Every active event type, **unpaginated** (`views.py:875-879`) |
| `GET event-types/<uuid>/` | One, or `404` |

## 4. Lifecycle / state machine

Settings have no lifecycle; a row is upserted or it is not. The interesting
state machine is the template's **markup ownership**
(`serializers.py:326-364`, `models.py:265-285`):

```text
                    ┌──────────────── html_is_custom = False ────────────────┐
                    │  html_body regenerated from the shared layout on every │
                    │  save; template keeps following the platform design     │
                    └───────────────────────┬────────────────────────────────┘
                                            │
     PATCH sends html_body that differs from the standard for BOTH the old
     and the new message text  ────────────►│
                                            ▼
                    ┌──────────────── html_is_custom = True ─────────────────┐
                    │  stored markup preserved verbatim; stops inheriting     │
                    │  design changes                                         │
                    └───────────────────────┬────────────────────────────────┘
                                            │
     PATCH sends html_is_custom = false (any html_body in the same payload
     is discarded)  ────────────────────────┘  → regenerated, back to standard
```

The comparison against **both** standards is the subtle part
(`serializers.py:350-362`): an editor that posts its whole form back after the
user only touched the message would otherwise look like a hand edit and freeze
that template on its previous wording forever.

## 5. Derivations

- **The matrix costs two queries, at either scope.** One for active event
  types, one for every relevant settings row (`services/settings.settings_rows`:
  the platform rows, the tenant's whole-tenant rows and, with a branch, that
  branch's rows and no other branch's). `resolve_settings_bulk` is handed the
  pre-fetched rows so it does not re-query.
- **`source` comes out of the resolver with the value**
  (`services/settings.resolve_settings_bulk`), so provenance and value cannot
  disagree: a transactional or inactive event reports `"default"` regardless,
  then a branch row (branch_scoped events only), then a tenant row, then a
  platform row, then `"default"`. Dispatch uses the same function.
- **The branch speaks only inside its own tenant.** The resolver ignores a
  branch whose tenant is not the one being resolved. Dispatch resolves per
  recipient-owner tenant, so a school's branch choice never reaches platform
  staff who receive the same event.
- **The scope resolver** (`NotificationSettingViewSet._resolve_scope`) returns
  `(tenant, branch, whole_reach)`. A `PLATFORM`-kind tenant resolves to `None`,
  meaning the tenant-NULL layer. Writing codex-tenant rows instead would be
  inert for schools, because dispatch resolution only ever reads
  `tenant IS NULL OR tenant = <own>`. `whole_reach` is true once a branch has
  been accepted, and at the whole-school scope only for a caller no branch
  grant narrows.
- **`variables` is derived from the copy, not maintained separately.**
  `template_variables` scans `subject`, `body`, `cta_label`, `cta_url` and
  `html_body` for `{{ name }}`, `{% if name %}` and `{% for x in name %}`
  (`services/preview.py:31-45`). It is a regex scan rather than a parse on
  purpose: it must survive half-written copy in the editor, and a preview of a
  broken template is more useful than an error.
- **Sample values are rule-based** (`services/preview.py:101-130`): an exact
  name match first, then a boolean-shaped prefix (`is_`, `has_`, `can_`,
  `should_`) returning `True` so `{% if %}` branches take the right path, then a
  suffix table (`_url` → a link, `_amount` → `125,000.00`, `_email` → a sample
  address), then the humanised variable name. Caller-supplied context always
  wins, and extra keys are kept.
- **Preview draft handling builds a detached copy** and never mutates or saves
  the stored row (`serializers.py:462-492`). A draft that changes the message
  but leaves the markup alone gets the markup regenerated, so the preview shows
  what saving would actually produce.
- **The preview returns markup twice, on purpose**: `html_body` is what the
  recipient sees, placeholders substituted; `html_source` is what the editor
  puts back in its HTML box, placeholders intact
  (`serializers.py:446-458`). Showing the rendered version in the editor would
  quietly bake the sample data into the template on the next save.
- **`available-events`** subtracts the taken `(event_type_id, channel)` pairs
  from every active event type's supported channels
  (`views.py:792-812`), so the "new template" screen only offers pairs that can
  actually be created.
- **Template syntax is validated on save for every content field**
  (`serializers.py:294-306` → `services/render.py:20-38`), and a `cta_label`
  with no `cta_url` is rejected rather than silently dropped at send time
  (`serializers.py:310-313`).

## 6. What administration writes

- **`PATCH settings/update/`** writes `NotificationSetting` rows through
  `all_objects.update_or_create` inside one atomic block, stamping
  `updated_by`, and at a branch deletes the row for an `is_enabled: null` item.
  It uses `all_objects` because the target tenant may be `None` (the platform
  layer), which the tenant-aware manager would not select.
- **`POST`/`PATCH templates/`** writes the template and stamps `created_by` /
  `updated_by` from `request.user` (`serializers.py:316-323`). Every write
  passes through `NotificationTemplate.save()`, so `html_body` is refreshed or
  preserved per the ownership rules above.
- **Nothing else writes.** The catalogue, `available-events` and preview are
  reads.

**Every change is audited** through `services/audit.py`, as `CONFIG` /
`CONFIG_CHANGED` in `vs_audit`:

- a settings PATCH writes one event per `(event type, channel)` whose stored
  value at that layer changed, entity `NotificationSetting`, id
  `<event_type_key>:<channel>` (with `:branch-<id>` appended for a branch row),
  diff `{"is_enabled": {"before", "after"}}` (`before` is `null` when the layer
  had no row, `after` is `null` when a branch row was removed), filed under the
  tenant or, for a platform caller, the platform. `metadata.layer` is
  `"branch"`, `"tenant"` or `"platform"`, and a branch event also carries
  `branch_id` and `branch_name`; the summary names the branch. Re-sending the
  stored value records nothing;
- a template create or edit writes one event, entity `NotificationTemplate`,
  with the changed columns before and after (`subject`, `body`, `cta_label`,
  `cta_url`, `html_body`, `html_is_custom`, `is_active`). An edit that changes
  none of them records nothing.

`metadata.created` says whether the row was new, and `metadata.removed`
whether a branch row was removed.

## 7. Worked example

```text
GET /v1/notify/settings/?tenant=alpha-nt
```

```json
{ "success": true, "message": "Settings retrieved.",
  "data": [
    { "event_type_key": "billing.invoice_overdue",
      "event_type_label": "Invoice overdue", "source_module": "vs_billing",
      "channel": "email", "is_enabled": true,
      "is_transactional": false, "source": "platform",
      "branch_scoped": true, "can_edit": true },
    { "event_type_key": "user.invited", "event_type_label": "User invited",
      "source_module": "vs_user", "channel": "email", "is_enabled": true,
      "is_transactional": true, "source": "default",
      "branch_scoped": false, "can_edit": false }
  ] }
```

```text
PATCH /v1/notify/settings/update/?tenant=alpha-nt
{ "updates": [ { "event_type_key": "billing.invoice_overdue",
                 "channel": "email", "is_enabled": false } ] }
```

writes one tenant row and returns that entry with `"is_enabled": false,
"source": "tenant"`. Sending the same body for `user.invited` returns `400`
with `TRANSACTIONAL_NOT_CONFIGURABLE`; sending `"channel": "in_app",
"is_enabled": false` returns `IN_APP_ALWAYS_ENABLED`.

Bright Star runs Ikeja and Lekki. Its school admin has switched overdue-invoice
emails off for the whole school; Lekki's parents still want them. The Lekki
branch admin sends:

```text
PATCH /v1/notify/settings/update/?tenant=bright-star&branch=<lekki id>
{ "updates": [ { "event_type_key": "billing.invoice_overdue",
                 "channel": "email", "is_enabled": true } ] }
```

and gets the row back with `"is_enabled": true, "source": "branch",
"can_edit": true`. An overdue notice on a Lekki invoice now emails the parent;
one on an Ikeja invoice, or on an invoice filed for the whole school, does not.
Sending `"is_enabled": null` to the same address removes Lekki's row, and
Lekki follows the school again. The same admin sending
`"event_type_key": "ticket.created"` there gets `400`
`BRANCH_NOT_CONFIGURABLE`; naming Ikeja's id gets `404`; leaving `?branch=` out
gets `403` `BRANCH_SCOPE_REQUIRED`.

```text
GET /v1/notify/templates/<uuid>/preview/?tenant=codex
```

returns `{channel, subject, body, html_body, html_source, html_is_custom,
variables, context_used}` with every `{{ variable }}` filled from the sample
table, so the console can render the real visual in a sandboxed iframe with no
payload at all.

## 8. Gotchas / known limitations

Full evidence in **`docs/notifications/notification_code_issues.md`**. This
slice's items:

- **A school's settings decide whether CX staff get notified.** For events
  dispatched with `tenant=<school>` to platform-tenant recipients, the
  resolution reads the *school's* rows, so a school admin turning off
  `ticket.created` email silences the CX support queue
  (`notification_code_issues.md` §2).
- **Three list endpoints are unpaginated**: the settings matrix, the template
  list and the event-type catalogue (`views.py:528-535,679-702,869-873`). The
  matrix is currently 56 rows and grows with the registry
  (`notification_code_issues.md` §8).
- **Duplicate-template detection is string matching on an exception**:
  `if "unique" in str(exc).lower()` (`views.py:725-734`). A wording change in
  the driver turns a `409` into a `500`.
- **The engine's seed command imports `vs_schools`**
  (`management/commands/seed_notification_settings.py:63`), which the platform
  rules forbid (`notification_code_issues.md` §10).
- **`urls.py`'s header comment names the wrong prefix** - `/api/v1/notifications/`
  where the real mount is `/v1/notify/` (`notification_code_issues.md` §11).
- **Justified by design:** templates are global and un-scoped
  (`views.py:677-683`). Per-tenant copy would multiply the catalogue by the
  tenant count and there is no product requirement for it; the key is seeded to
  platform roles only.
- **Justified by design:** preview returns HTML as a JSON string rather than an
  HTML response (`views.py:825-829`), so the console renders it inside a
  sandboxed iframe and a preview can never execute against the API origin.
- **Justified by design:** the settings PATCH validates everything before
  writing anything (`views.py:570-640`), so a partially applied settings change
  is impossible.

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Seeded to |
|---|---|---|---|
| Settings GET + PATCH | `communication.communication_permissions.enforce` | `SENSITIVE`, restricted | platform roles + `school_admin`, `branch_admin` |
| Template CRUD + preview + available-events | `communication.notification_templates.configure` | `SENSITIVE`, restricted | platform roles **only** |
| Event-type catalogue | `IsAuthenticated` | n/a | everyone |

`seed_notification_permissions.py` seeds only the three keys the views actually
check; the other six constants in `NotificationPermission`
(`constants.py:93-102`) are reserved for future messaging work and are seeded
when something enforces them (`seed_notification_permissions.py:1-14`). The
command also backfills existing tenant role templates whose key matches a
prebuilt school role (`seed_notification_permissions.py:139-164`).

**Who may do what with settings.** The key opens the screen; the caller's
branch reach decides the scope they may write.

| Caller | Whole school (no `?branch=`) | Their own branch | Another branch, or another school's |
|---|---|---|---|
| School admin (whole-tenant grant) | read and write | read and write | read and write any branch of their school; another school's is `404` |
| Branch admin (grant pinned to a branch) | read only (`can_edit` false everywhere), PATCH `403` | read and write `branch_scoped` rows | `404` |
| CX staff (platform tenant) | the platform default layer | `404` (the platform layer has no branches) | `404` |
| Anyone without the key | `403` | `403` | `403` |

The same holds in a school with one branch: its branch admin is still narrowed
to that branch, and its school admin may write either scope.

The events a branch may set are exactly those whose every sender passes the
branch the event is about:

| Event | branch_scoped | Why |
|---|---|---|
| `billing.invoice_issued`, `billing.payment_received`, `billing.statement_issued` | true | Sent only by `vs_finance.document_email`, with the invoice's or receipt's branch, or the customer's for a statement |
| `billing.debit_note_issued`, `billing.credit_note_issued` | true | Sent only by `vs_finance.notifications`, with the note's branch |
| `billing.invoice_overdue` | true | Sent only by `vs_finance.dunning`, with the overdue invoice's branch |
| `workflow.stage_activated`, `workflow.rejected`, `workflow.returned`, `workflow.final_approved` | true | Sent only by `vs_workflow.tasks.dispatch_notification`, with the instance's branch (copied from the document) |
| `ticket.*`, `export.*`, `task.*`, `todo.task_completed`, `onboarding.*` (non-transactional), `payments.unbooked_receipts_digest` | false | Not about a branch: sent for a person, a job, a ticket or the whole tenant |
| Every transactional event | false | Settings never apply to it at any scope |
| Every inactive event (`student.*`, `workflow.submitted` and the rest) | false | No sender exists yet; the flag is set in the change that wires one |

A branch that is `null` on the document (an invoice filed for the whole school)
resolves at the school row, which is what a null branch means.

**Settings isolation holds.** The scope is `request.tenant`, the auth layer
refuses a slug that is not the caller's own with `404`, and this view does not
opt in via `platform_cross_tenant_param` - so not even CX staff can reach one
school's rows from here. A `?branch=` outside the tenant is the same `404`.
Covered in `tests.py` (`SettingsApiTests`) and `tests_branch_settings.py`.

**Template isolation does not exist, and should not.** The catalogue is global
by design. The residual risk is the platform-wide one recorded against
`vs_audit`: nothing in the RBAC write path prevents a `communication.*` key
being attached to a school-tenant role
(`docs/audit/audit_event_stream.md` §8), and a school role holding
`notification_templates.configure` would be editing every tenant's copy.

## 10. Code map

| File | Responsibility |
|---|---|
| `views.py` | `NotificationSettingViewSet` - scope (tenant, branch, reach), matrix build, the validated bulk upsert |
| `views.py:659-851` | `NotificationTemplateViewSet` - CRUD, `available-events`, preview |
| `views.py:858-891` | `NotificationEventTypeViewSet` - the read-only catalogue |
| `serializers.py:238-364` | `NotificationTemplateSerializer` and the markup-ownership resolver |
| `serializers.py:371-492` | Draft + preview serializers, including `_apply_draft` |
| `serializers.py:499-549` | Matrix row shape and the bulk-update payload validator |
| `services/preview.py` | `template_variables`, `sample_context` |
| `services/settings.py` | `settings_rows`, `resolve_settings_bulk` (value and provenance), `resolve_channels_bulk` - shared with dispatch |
| `models.py:265-304` | `NotificationTemplate.save()` and `standard_html()` |
| `services/seed.py` | `seed_event_types`, `seed_platform_settings`, `seed_school_settings`, `seed_notification_templates` |
| `management/commands/` | The four seed commands, including the permissions seed |

## 11. Test coverage & gaps

- `SettingsApiTests` (`tests.py:696-800`) - `403` without the key, cross-school
  read refused, own-school read, matrix shape and the `source` field, upsert
  creating an override row, school-scoped write landing on a school row, and
  all three rejection codes (in-app disable, transactional toggle, unknown
  event).
- `TemplatePreviewApiTests` (`tests.py:1182-1332`) - permission gate on preview
  and on `available-events`, GET preview with no payload, POST context
  overrides, preview writing nothing, the variables list and search, draft
  preview without saving, markup returned alongside the render, a draft that
  only changes the message refreshing the markup, ownership claimed by editing
  the markup, posting the standard markup back **not** counting as a hand edit,
  reset restoring the standard design, `available-events` listing only
  uncovered pairs, and the `cta_label`-without-`cta_url` rejection.
- `StoredEmailHtmlTests` (`tests.py:1095-1180`) - every seeded email template
  stores markup, in-app templates store none, placeholders survive, conditional
  tags survive escaping, a standard template follows its message, a hand-edited
  one is left alone, clearing the flag restores the design, and dispatch sends
  the stored markup.
- `SeedNotificationPermissionsTests` (`tests.py:1349-1390`) - platform roles
  granted in the tenant table, native school role backfilled.
- `ResponseShapeTests` (`tests.py:1340-1347`) - the settings matrix returns a
  list.
- `NotificationChangeAuditTests` - a school switching a channel off is
  recorded with actor, tenant and diff; switching back is a second record;
  re-sending the stored value and a refused PATCH record nothing; the platform
  layer is recorded as such; a template edit records what changed and a create
  is recorded.
- `tests_branch_settings.py` - a branch admin writing their own branch, `404`
  for another branch and for another school's, `403` on the whole-school PATCH,
  `can_edit` at both scopes; a school admin writing both scopes and `null`
  resetting a branch row; `BRANCH_NOT_CONFIGURABLE`, the transactional and
  in-app refusals at a branch, all-or-nothing, `RESET_NEEDS_BRANCH`; the
  platform layer refusing a branch; cross-tenant isolation and a single-branch
  school; branch audit; the resolver's layering; dispatch obeying Ikeja's row
  for Ikeja sends only and never for another tenant's recipients; the
  migration forward and in reverse.

This is the best-covered part of the module. Gaps:

1. **The CX/platform scope.** No test asserts that a `PLATFORM`-kind caller's
   PATCH writes a `tenant=NULL` row and that a school then inherits it; only
   the school-scoped write is covered (`tests.py:748-763`).
2. **Template list and CRUD** - no `403` test on `GET templates/` or
   `POST templates/` for a non-platform caller, and no test of the `409`
   duplicate path or the string-matching that produces it.
3. **`?channel=` and `?event_type_key=` filters** on the template list.
4. **Unpaginated growth** - nothing asserts the matrix size or notices that
   three endpoints return everything.
5. **Inactive event types** - nothing asserts that the 13 registered-but-inactive
   entries stay out of the matrix, the catalogue and `available-events`.
6. **`event-types/`** has no test at all, list or detail.
