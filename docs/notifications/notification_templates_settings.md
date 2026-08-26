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

- **Settings are a three-layer resolve, exposed as a flat matrix.** `GET
  settings/` returns one row per `(active event type × supported channel)` with
  the resolved value and the layer that produced it
  (`views.py:464-524`). `PATCH settings/update/` upserts override rows addressed
  by `(event_type_key, channel)`, never by row id (`views.py:539-652`).
- **Scope comes from the asserted tenant, not a parameter.** There is no
  `?school=`. A business tenant manages its own override rows; a `PLATFORM`-kind
  tenant (CX staff) manages the **tenant-NULL default layer** every tenant
  inherits, not codex's own rows (`views.py:449-462`). CX staff cannot target
  one school's settings from here.
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
- **Three things cannot be configured**, and each is refused with its own error
  code rather than silently ignored: a transactional event
  (`TRANSACTIONAL_NOT_CONFIGURABLE`), the in-app channel being switched off
  (`IN_APP_ALWAYS_ENABLED`), and a channel the event does not support
  (`UNSUPPORTED_CHANNEL`) (`views.py:584-618`).

## 2. Domain model

| Model | File | Notes |
|---|---|---|
| `NotificationEventType` | `models.py:32` | 47 registry entries, 34 active, 10 transactional, 13 registered-but-inactive |
| `NotificationTemplate` | `models.py:127` | `subject`, `body`, `cta_label`, `cta_url`, `html_body`, `html_is_custom`, `is_active`, `created_by`, `updated_by` |
| `NotificationSetting` | `models.py:311` | `tenant?`, `event_type`, `channel`, `is_enabled`, `updated_by` |

**`is_active=False` on an event type is an honesty flag, not a bug.** The
registry comment is explicit: an event stays inactive until a domain module
actually emits it, and the flag is flipped in the same change that adds the
`send_notification` call (`constants.py:121-125`). Thirteen entries are
currently in that state, and they are correctly absent from the settings matrix
(`views.py:475`), the catalogue (`views.py:871`) and the creatable template set
(`views.py:796`).

**Two conditional unique constraints** keep the layering honest
(`models.py:381-394`): at most one row per `(tenant, event_type, channel)` where
tenant is not null, and at most one per `(event_type, channel)` where it is.
Postgres enforces both, and the test suite runs on Postgres for exactly that
reason (`tests.py:12-13`).

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

### Settings - key `communication.communication_permissions.enforce` (`views.py:444-445`)

| Method + path | body | response |
|---|---|---|
| `GET settings/` | - | Flat list of matrix rows, **unpaginated** (`views.py:528-535`) |
| `PATCH settings/update/` | `{"updates": [{"event_type_key", "channel", "is_enabled"}, …]}`, min 1 | The touched rows, freshly resolved (`views.py:539-652`) |

Matrix row shape (`serializers.py:499-514`): `event_type_key`,
`event_type_label`, `source_module`, `channel`, `is_enabled`,
`is_transactional`, `source` - where `source` is `"tenant"`, `"platform"` or
`"default"`.

The PATCH is **all-or-nothing**: every item is validated first, and any error
returns `400` with a per-index list of `{index, error_code, message}` before a
single row is written (`views.py:570-625`). Only then does one atomic block
upsert them all (`views.py:628-640`).

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

- **The matrix costs two queries.** One for active event types, one for every
  relevant settings row under `tenant IS NULL OR tenant = <tenant>`, then
  `resolve_channels_bulk` is handed the pre-fetched rows so it does not
  re-query (`views.py:474-500`). Asserted at `tests.py:236-243`.
- **`source` is computed from the same rows** that produced `is_enabled`
  (`views.py:489-514`): a transactional or inactive event reports `"default"`
  regardless, then a tenant row wins, then a platform row, then `"default"`.
- **The scope resolver is two lines and one rule** (`views.py:449-462`): a
  `PLATFORM`-kind tenant resolves to `None`, meaning the tenant-NULL layer.
  Writing codex-tenant rows instead would be inert for schools, because dispatch
  resolution only ever reads `tenant IS NULL OR tenant = <own>`.
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
  `updated_by` (`views.py:628-640`). It uses `all_objects` because the target
  tenant may be `None` (the platform layer), which the tenant-aware manager
  would not select.
- **`POST`/`PATCH templates/`** writes the template and stamps `created_by` /
  `updated_by` from `request.user` (`serializers.py:316-323`). Every write
  passes through `NotificationTemplate.save()`, so `html_body` is refreshed or
  preserved per the ownership rules above.
- **Nothing else writes.** The catalogue, `available-events` and preview are
  reads.

**No audit event is written for any of it.** Changing the copy that every
tenant on the platform receives, or turning off a tenant's notification channel,
leaves `updated_by` and `updated_at` on the row and nothing in `vs_audit`
(`notification_code_issues.md` §9).

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
      "is_transactional": false, "source": "platform" },
    { "event_type_key": "user.invited", "event_type_label": "User invited",
      "source_module": "vs_user", "channel": "email", "is_enabled": true,
      "is_transactional": true, "source": "default" }
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
- **No audit event for template or settings changes**
  (`notification_code_issues.md` §9).
- **`branch_admin` holds the same settings key as `school_admin`** with no
  branch narrowing (`seed_notification_permissions.py:25-29`), because
  `NotificationSetting` has no branch column. A branch admin edits the whole
  tenant's settings.
- **Duplicate-template detection is string matching on an exception**:
  `if "unique" in str(exc).lower()` (`views.py:725-734`). A wording change in
  the driver turns a `409` into a `500`.
- **`_resolve_scope` returns a `(tenant, denied)` tuple whose second element is
  always `None`** (`views.py:449-462`), and both call sites branch on it
  (`views.py:530-532,543-545`). Dead scaffolding from an earlier permission
  model.
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

**Settings isolation holds.** The scope is `request.tenant`, the auth layer
refuses a slug that is not the caller's own with `404`, and this view does not
opt in via `platform_cross_tenant_param` - so not even CX staff can reach one
school's rows from here. Tested at `tests.py:702-715,748-763`.

**Template isolation does not exist, and should not.** The catalogue is global
by design. The residual risk is the platform-wide one recorded against
`vs_audit`: nothing in the RBAC write path prevents a `communication.*` key
being attached to a school-tenant role
(`docs/audit/audit_event_stream.md` §8), and a school role holding
`notification_templates.configure` would be editing every tenant's copy.

## 10. Code map

| File | Responsibility |
|---|---|
| `views.py:419-652` | `NotificationSettingViewSet` - scope, matrix build, the validated bulk upsert |
| `views.py:659-851` | `NotificationTemplateViewSet` - CRUD, `available-events`, preview |
| `views.py:858-891` | `NotificationEventTypeViewSet` - the read-only catalogue |
| `serializers.py:238-364` | `NotificationTemplateSerializer` and the markup-ownership resolver |
| `serializers.py:371-492` | Draft + preview serializers, including `_apply_draft` |
| `serializers.py:499-549` | Matrix row shape and the bulk-update payload validator |
| `services/preview.py` | `template_variables`, `sample_context` |
| `services/settings.py` | `resolve_channels_bulk` - shared with dispatch |
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
7. **A settings PATCH mixing a valid and an invalid item** - the all-or-nothing
   guarantee is implemented (`views.py:620-625`) but never asserted.
