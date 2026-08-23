# audit_event_stream

The platform's **central activity stream**: one `AuditEvent` row per notable
action, written by 45 call sites across 14 files, read back through the Event
Explorer. This document covers seven of the thirteen routes mounted at
`/v1/audit/` (`apps/urls.py:28`): `events/`, `events/filter-options/`,
`events/<uuid:id>/`, `entity-trails/`, `entity-trails/<type>/<id>/`,
`me/activity/`, `me/activity-on-me/`. The rest belong to the sibling slices:
`dashboard-summary/` to `docs/audit/audit_security_dashboard.md`, and
`exports/` plus `compliance-rules/` to `docs/audit/audit_compliance_exports.md`.

---

## 1. What it is (and what it is NOT)

- **One append-only table plus a catalogue.** `AuditEvent`
  (`models.py:196`) is the row; `EntityAuditTrail` (`models.py:382`) names the
  entities anything has ever been audited against, and carries nothing but
  `entity_type`, `entity_id` and `entity_label`. **It is deliberately not a
  rollup**: it used to store `event_count`, `first_event_at` and
  `last_event_at`, and migration `0011_retire_entity_trail_rollup` dropped all
  three. See §5 and §8.
- **One writer.** Everything goes through `emit_audit_event`
  (`services.py:158`), which resolves proxy identity, resolves the tenant, fills
  in a summary, creates the row and upserts the catalogue entry.
- **This is a mirror, not a system of record.** `emit_audit_event` catches every
  exception and returns `None` (`services.py:267-269`). Finance
  (`vs_finance/audit.py:21-48`), RBAC (`vs_rbac/audit.py:63-88`) and tickets
  (`vs_tickets/services/audit.py:40-56`) each keep their own authoritative log
  and mirror here best-effort. Never answer a compliance question from this
  table when the module has its own trail; see
  `docs/finance/finance_audit_trail.md` §1.
- **The action vocabulary is closed.** `AuditActionType` (`models.py:74`) is a
  choices enum and `AuditEvent.save()` calls `full_clean()`
  (`models.py:370`), so an unregistered `action_type` raises, is swallowed, and
  the event is silently lost. The enum carries a comment saying exactly that
  (`models.py:129-132`).
- **Every read is scoped to the caller's tenant unless the caller is
  platform-kind.** One predicate answers "which rows are this caller's", it
  lives in `scoping.py`, and both the views and the Export Centre dataset call
  it. See §9.
- **`AuditDiffService` (`services.py:272`) is a helper library, not a
  pipeline.** Callers use it to build JSON-safe `before_data`/`diff_data`; the
  audit app itself never calls it.

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `AuditEvent` | `models.py:196` | `id` (UUID pk), `module_key`, `action_type`, `severity`, `status`, `actor_type`, `actor_user?`, `effective_user?`, `tenant?`, `impersonation_session?`, `actor_label`, `entity_type`, `entity_id` (text), `entity_label?`, `summary?`, `before_data`, `diff_data`, `metadata`, `event_at`, `is_locked` |
| `EntityAuditTrail` | `models.py:382` | `id` (UUID pk), `entity_type` + `entity_id` (unique together), `entity_label`. **No counters and no tenant column.** |

`EntityAuditTrailSerializer` still publishes `event_count`, `first_event_at` and
`last_event_at` (`serializers.py:181-216`), so the response shape did not
change when the columns went. They are `SerializerMethodField`s read out of a
context the view fills, deliberately declared that way so that no code path can
persist a count at all, whether it means to or not.

Five enums drive the classification (`models.py:36-167`):

- `AuditSeverity`: `INFO`, `WARNING`, `CRITICAL`.
- `AuditStatus`: `SUCCESS`, `FAILED`, `DENIED`, `PARTIAL`.
- `AuditActorType`: `USER`, `SYSTEM`.
- `AuditModuleKey`: 13 surfaces, from `ONBOARDING` to `PLATFORM`.
- `AuditActionType`: 65 values across CRUD, identity, import, RBAC,
  impersonation, finance/procurement, the Export Centre block and the school
  onboarding block.

**Immutability is model-level only.** `save()` on an existing pk raises and
`delete()` always raises (`models.py:363-375`), but there are no database
triggers, so `AuditEvent.objects.filter(...).update(...)` and `.delete()`
bypass both. Compare `FinanceAuditLog`, which installs BEFORE UPDATE/DELETE
triggers in migration `0025` (`docs/finance/finance_audit_trail.md` §8). Two
data migrations already exercise that bypass on purpose:
`0003_remove_impersonated_request_history` deletes the retired
`IMPERSONATED_REQUEST` rows, and `0004_remove_notification_proxy_fallbacks`
deletes noisy `PROXY_CHANGE` rows. That pair is also the argument that retired
the rollup: 0003 deleted events and left the stored counters standing, and 0004,
one migration later on the same table, had to carry 25 lines of hand-written
recount because its author remembered. Same table, two migrations apart, one
remembered and one forgot.

`entity_id` is a `CharField`, deliberately, so int pks, UUIDs and external refs
all fit (`models.py:292-296`). That is why the list view has to defend itself
when resolving `entity_type="User"` rows (§5).

Six composite indexes on `AuditEvent` (`models.py:338-345`) cover
`(module_key, action_type, event_at)`, `(entity_type, entity_id, event_at)`,
`(actor_type, actor_user, event_at)`, `(severity, status, event_at)`,
`(tenant, event_at)` and `(impersonation_session, event_at)`. Default ordering
is `-event_at`. The second of those has been on the table since migration
`0002_initial`, which is what made computing the trail counters at read time
affordable without adding an index for it.

**Who writes the rows.** `emit_audit_event` is called from 45 sites in 14
files across 12 apps. The heavy ones: `vs_rbac/signals.py` (24 calls),
`schools/vs_schools/serializers.py` (7), `vs_user/services/audit.py` (the whole
identity/auth vocabulary, via `log_auth_event`), `vs_tenants/middleware.py` (the
proxy fallback pair), `vs_rbac/services.py` (2), `vs_admin_console/views.py`
(impersonation bookends), plus the onboarding, finance, tickets, config, exports
and import mirrors and one seeding command.

## 3. Endpoint map

Gate on every keyed route: `IsAuthenticatedAndActive & HasRBACPermission`. No
view sets `tenant_param_required = False`, so **`?tenant=<slug>` is required on
all seven routes** including the two `/me/` ones
(`vs_rbac/authentication.py:136`).

| Method + path | permission key | query params actually read | response |
|---|---|---|---|
| `GET /events/` | `platform.audit.view` | `module_key`, `action_type`, `severity`, `status` (repeatable, any-of), `tenant_slug`, `actor_type`, `actor_user_id`, `impersonation_session_id`, `entity_type`, `entity_id`, `date_from`, `date_to`, `search` | Paginated `AuditEventListSerializer` (`views.py:217-277`) |
| `GET /events/filter-options/` | `platform.audit.view` | - | `{modules, actions, severities, statuses, actor_types, tenants}`, each `[{value,label}]` (`views.py:160-214`) |
| `GET /events/<uuid:id>/` | `platform.audit.view` | - | `AuditEventDetailSerializer`, including raw `metadata` (`views.py:280-301`) |
| `GET /entity-trails/` | `platform.audit.view` | `entity_type` (exact), `search` (id or label) | Paginated `EntityAuditTrailSerializer` (`views.py:308-396`) |
| `GET /entity-trails/<entity_type>/<entity_id>/` | `platform.audit.view` | - | `{trail, events}`, **unpaginated** (`views.py:462-533`) |
| `GET /me/activity/` | authenticated only | `module_key`, `severity`, `search` | Paginated events where the caller is the actor (`views.py:399-426`) |
| `GET /me/activity-on-me/` | authenticated only | `module_key`, `severity`, `search` | Paginated events targeting the caller, actor excluded (`views.py:429-459`) |

**The filter contract is validated, the trail filters are not.** `/events/`
runs every query param through `AuditEventFilterSerializer`
(`serializers.py:426`) with `raise_exception=True`, so a bad enum value or a
malformed date is a clean `400`, `date_from > date_to` is rejected by name
(`serializers.py:502-513`), and an unknown `tenant_slug` is a `400` rather than
an empty page (`serializers.py:484-500`). `/entity-trails/` and both `/me/`
routes read `request.query_params` directly (`views.py:346-352`, `417-425`,
`450-458`) with no validation at all.

`ChoiceListField` (`serializers.py:398`) is what makes
`?status=FAILED&status=DENIED` work while `?status=FAILED` still validates: a
bare string is wrapped in a list before the child `ChoiceField` runs. Repeated
values are OR'd within a group and AND'd across groups
(`views.py:106-157`; tested at `tests.py:107-163`).

**`tenant_slug` narrows, it never widens.** The boundary is applied to the
queryset before the filters are (`views.py:270-277`), so the filter can only
select inside what the caller may already read. It is spelled `tenant_slug` and
not `tenant` because `?tenant=` is the platform-wide tenant *assertion*, read by
`TenantJWTAuthentication` long before this serializer sees the query string
(`serializers.py:462-477`). `tenant_slug=__none__` is a first-class value
meaning "events that belong to no customer" - platform sweeps and management
commands - and without it those rows would be unreachable from the screen
(`serializers.py:415-423`, `views.py:117-125`).

## 4. Lifecycle / state machine

There is no state machine. Rows are inserted once and never transition:

```text
caller ──► emit_audit_event(...)
             │
             └─ transaction.atomic()                 a savepoint, see below
                  ├─ resolve_audit_identity(...)      rewrite actor under proxy
                  ├─ resolve_event_tenant(...)        inherit the request's tenant
                  ├─ _build_summary(...)              only when summary == ""
                  ├─ AuditEvent.objects.create(...)   full_clean() runs here
                  ├─ mark_audit_event_emitted()       suppresses the proxy fallback
                  └─ EntityAuditTrail.get_or_create() catalogue row, nothing counted
                                                      writes only on a label change

any exception anywhere above ──► logged to the "vs_audit" logger, returns None
```

**The savepoint is what makes "never raises" true** (`services.py:204`). Most
callers emit from inside their own `transaction.atomic` block, and a *database*
error here marks the whole enclosing transaction for rollback. Catching the
exception was not enough: the caller carried on and its own legitimate write was
then refused at commit with `TransactionManagementError`, so an audit failure
could destroy the business change it was only supposed to describe. Rolling back
to a savepoint confines the damage to the audit row.

**There is no retention job.** Nothing prunes `AuditEvent`, nothing enforces a
`ComplianceRule` of type `RETENTION`, and the table grows forever. Contrast
`BackgroundJob`, which has a 90-day sweep
(`docs/console/console_task_monitor.md` §2).

## 5. Derivations

- **Actor attribution under proxy** (`services.py:205-208` →
  `vs_tenants/context.py:67-82`). During an impersonation session the
  authentication layer stashes `(actor, effective_user, session)` in a
  contextvar. `resolve_audit_identity` rewrites the event only when the caller
  attributed it to *either* request identity: the row then always names the
  real staff member as `actor_user`, keeps the impersonated account as
  `effective_user`, and pins `impersonation_session`. An event attributed to a
  genuine third party, or to the system, is left alone
  (`tests.py:437-451`).
- **`actor_type`** is derived, never passed: `USER` when a resolved
  `actor_user` exists, otherwise `SYSTEM`, and `actor_user` is nulled in the
  `SYSTEM` branch (`services.py:209,218`).
- **`tenant`** is derived when the caller did not name one
  (`resolve_event_tenant`, `services.py:98-155`, called at `:210`). Three rules,
  in order: an explicit tenant always wins; a SCHOOL or ORGANIZATION tenant in
  the ambient request context is inherited, because `?tenant=` is mandatory and
  authentication has already refused any slug but the caller's own; a PLATFORM
  assertion is **not** inherited and the event stays null, because "I am acting
  as Codex" says nothing about whose data is being touched. Nothing is inherited
  outside a request at all, which is why `log_auth_event` and
  `create_import_audit_log` pass their tenant explicitly: sign-in, password reset
  and lockout happen on unauthenticated endpoints where there is no context to
  inherit.
- **`summary`** falls back to a per-action template
  (`services.py:19-71,74-95`) rendered with `{actor}`, `{entity}`,
  `{entity_type}`. Unknown actions get `"{actor} performed {action_type} on
  {entity}"`. The actor name resolves `full_name` → `get_full_name()` →
  `email` → `"Unknown user"`, and is literally `"System"` when there is no
  actor. Under proxy the template renders the **proxier's** name, which is the
  behaviour asserted at `tests.py:395-396`.
- **`ip_address`** on the list serializer is not a column: it is
  `metadata["ip_address"]` (`serializers.py:73-74`), populated only by callers
  that pass a request, chiefly `log_auth_event`
  (`vs_user/services/audit.py:62-64`).
- **`entity_user`** resolves `entity_type="User"` rows to a name/email block
  through one bulk query built in `list()` rather than per row
  (`views.py:243-253`, `serializers.py:76-83`). The loop coerces `entity_id` to
  `int` and drops anything non-numeric, because `User.id` is a `BigAutoField`
  and a UUID-shaped `entity_id` from older audit code would otherwise make
  `filter(id__in=...)` raise `ValueError` (`views.py:239-253`).
- **`search`** matches any one of seven columns: summary, entity label, entity
  id, actor label, action code, actor email, and a `Concat`-annotated actor
  full name (`views.py:140-155`). The action-code and actor-identity arms are
  covered at `tests.py:139-148`.
- **The trail's three numbers are computed per caller, not stored.**
  `visible_trail_counters` (`scoping.py:143-206`) groups `AuditEvent` by
  `(entity_type, entity_id)` behind the caller's own predicate and returns
  `event_count`, `first_event_at` and `last_event_at` for a whole page. **A
  missing key means zero**, which is the honest answer for a trail whose events
  were deleted underneath it and the answer the old stored figure could not
  give. Cost: **exactly one query per page for either kind of caller.** A tenant
  caller has paid it since the trail was scoped; a platform caller used to read
  the stored rollup for free, and that is the one query this change added. It is
  a per-page cost, not a per-row one, and `tests.py:1477-1547` asserts both the
  single query and that the endpoint does not grow a query per extra trail.
- **The trail detail route's numbers are free** (`views.py:515-521`). The
  events are already scoped, already ordered and already in memory, so the
  header is counted off the list printed underneath it and cannot disagree
  with it.
- **The catalogue's sort key is computed in SQL.**
  `latest_visible_event_at` (`scoping.py:112-140`) is a correlated `Subquery`
  giving each trail's most recent *readable* event, and
  `EntityAuditTrailListView` orders on it with `nulls_last`, tiebroken on
  `entity_type`/`entity_id` so paging over equal timestamps cannot repeat or
  skip a row (`views.py:369-373`). A page cannot be sorted by something computed
  after it has been paginated, which is why this exists alongside
  `visible_trail_counters`; the two agree by construction, being the same table,
  the same predicate and the same `max(event_at)`. **This is the real cost of
  retiring the rollup**: one index probe per catalogue row on
  `(entity_type, entity_id, event_at)`, measured at 11ms over 890 trails, and it
  scales with the size of the catalogue rather than the size of the page (§8).
- **The membership filter is deliberately not fused into the ordering
  annotation** (`views.py:337-344`), even though `latest_event_at__isnull=False`
  would select the same set. Folding them together would make a future change to
  how the catalogue is *sorted* a change to who can *see* it.

## 6. What writing an event actually does

Nothing posts and nothing is corrected. In the steady state **one row is
written**: the `AuditEvent` insert (`services.py:214-231`). The catalogue upsert
below it (`services.py:246-263`) writes only when the entity is new to the
catalogue, or when its `entity_label` has actually changed - a school renamed
from "Bright Star" to "Bright Star Academy" refreshes the handle the trail list
displays. While the counters lived on that table every emitted event cost an
`UPDATE` there to bump a number; now it costs none.

One side effect matters outside this app: `mark_audit_event_emitted()`
(`services.py:236-237`, `vs_tenants/context.py:52-54`) bumps a request-local
counter. `TenantContextCleanupMiddleware` only writes its vague
`PROXY_CHANGE` / `PROXY_ACTION_FAILED` fallback when that counter is still zero
at the end of a proxied request (`vs_tenants/middleware.py:115`). So every
feature that emits a real event suppresses the generic one, which is why the
fallback rows read as "did something on this path" and the real ones do not.

Reads leave no trace. Opening the Event Explorer, pulling one event's full
`metadata`, or listing another person's activity writes no audit row anywhere.

## 7. Worked example

```text
GET /v1/audit/events/?tenant=codex&module_key=IDENTITY&status=FAILED&status=DENIED
    &date_from=2026-08-01T00:00:00Z&search=lekki
```

```json
{ "success": true, "message": "Data retrieved successfully",
  "pagination": { "currentPage": 1, "pageSize": 25, "totalItems": 2,
                  "totalPages": 1, "next": null, "previous": null },
  "data": [
    { "id": "4b1f…", "module_key": "IDENTITY", "action_type": "LOGIN_FAILED",
      "severity": "INFO", "status": "FAILED", "actor_type": "USER",
      "actor_user": { "id": 411, "email": "ngozi@lekki.test", "full_name": "Ngozi Eze" },
      "effective_user": null, "tenant": "bright-star", "impersonation_session": null,
      "actor_label": "", "entity_type": "User", "entity_id": "411",
      "entity_label": "Ngozi Eze",
      "entity_user": { "id": "411", "full_name": "Ngozi Eze", "email": "ngozi@lekki.test" },
      "summary": "Failed login attempt for Ngozi Eze",
      "ip_address": "102.89.34.7", "event_at": "2026-08-13T18:22:04Z" }
  ] }
```

The caller here asserts `?tenant=codex` and is platform staff, so the boundary
does not apply and the listing crosses tenants; `tenant_slug` is what a platform
reviewer would add to narrow it to one school. A Bright Star audit officer
running the same request without `tenant_slug` gets only Bright Star's rows,
because the boundary is her home tenant and not the slug she asserted.

`"tenant": "bright-star"` on an identity event is now the norm: `log_auth_event`
passes the subject's own tenant, and events emitted inside a school request
inherit it (§5). Rows written before `d1ceccb` still carry `null` there, and are
recovered for their owner through `metadata['tenant_id']` (§8).

`GET /v1/audit/entity-trails/User/411/?tenant=codex` returns
`{trail: {...event_count, first_event_at, last_event_at}, events: [...]}` in a
`success_response` envelope with **no pagination block** and every event on that
entity the caller may read. The three numbers in `trail` are counted off the
`events` list in the same response, so the header cannot disagree with the body.
An entity with no event this caller may read is a `404`, not an empty trail.

## 8. Gotchas / known limitations

- **`metadata` is exposed raw on the detail route.**
  `AuditEventDetailSerializer` includes `metadata`, `before_data` and
  `diff_data` verbatim (`serializers.py:138-140`). Callers put IP addresses and
  user agents in there (`vs_user/services/audit.py:62-64`), tenant and school
  ids (`:55-61`), branch ids (`vs_config/services/audit.py:103`), and whatever
  else they felt like. Finance deliberately hides its equivalent field
  (`docs/finance/finance_audit_trail.md` §1); this surface has no FLS at all
  and no `view_sensitive` key to hang one on. Note the asymmetry with the CSV
  export next door, which never carries those three columns at all
  (`views.py:62-66`) and additionally honours `MASKING` compliance rules on
  `summary` (`views.py:691-695`, applied at `:601-602`). The screen is the
  leakier of the two.
- **The ordering subquery scales with the catalogue, not the page.**
  `latest_visible_event_at` runs once per `EntityAuditTrail` row considered, so
  `/entity-trails/` gets slower as the number of audited entities grows even
  when the page size does not. Measured at 11ms over 890 trails when the rollup
  was retired, which is cheap; at 100k trails it wants a different approach, and
  the honest options are a materialised recency column maintained by something
  that also handles deletion, or a covering index. Named here rather than buried
  because it is the price paid for a sort key that agrees with the number on the
  row.
- **Rows written before `d1ceccb` carry `tenant = NULL`, and only some of them
  can be recovered.** That commit populated the column and deliberately did not
  backfill, so the boundary has a second arm matching
  `metadata['tenant_id']` (`scoping.py:34-65`). Only three writers ever recorded
  that id: `vs_user/services/audit.py` (the whole IDENTITY stream, since
  `661a73a`), `vs_rbac/signals.py` and `vs_rbac/services.py`. Finance,
  procurement, payments, imports and tickets never did, so **their pre-`d1ceccb`
  rows stay platform-only and a school cannot see that part of its own
  history.** `vs_tickets` writes a tenant id into `before_data` rather than
  `metadata`, which this lookup correctly cannot reach. Wrong in the safe
  direction, and not worth widening: a school seeing less of its own old history
  is recoverable, a school seeing another's is not.
- **Append-only is enforced in Python only.** `save()`/`delete()` raise
  (`models.py:363-375`) but there is no DB trigger, so
  `AuditEvent.objects.filter(...).update(...)`, `.delete()` and raw SQL all
  succeed. Two migrations rely on that
  (`0003_remove_impersonated_request_history`,
  `0004_remove_notification_proxy_fallbacks`), which is legitimate, but it means
  the guarantee is a convention rather than a control. The finance table solved
  the same problem with triggers. Retiring the rollup removed the *consequence*
  of that bypass (a deletion can no longer leave a wrong number behind) without
  removing the bypass.
- **A typo in `action_type` deletes the audit trail silently.** `save()` runs
  `full_clean()`, an unregistered value fails choices validation, and
  `emit_audit_event` catches it and returns `None`
  (`models.py:370`, `services.py:267-269`). The action lands, the record of it
  does not, and the only trace is a line on the `vs_audit` logger. The enum
  comment warns about this (`models.py:129-132`). One module now guards itself:
  `OnboardingActionTypeRegistrationTests` (`tests.py:465-509`) asserts that every
  constant `vs_onboarding` emits is registered and that emitting each one really
  writes a row. Nothing equivalent covers `vs_rbac/signals.py`,
  `vs_user/services/audit.py`, `vs_exports/audit.py` or the finance mirror.
- **The entity-trail detail route is bounded by tenant but not by size.**
  `EntityAuditTrailDetailView` now applies the same predicate as the Explorer,
  so another tenant's entity is a `404` even when its type and id are guessed
  (`views.py:487-505`) - which mattered, because `entity_type` and `entity_id`
  are enumerable in a way a UUID is not. What has not changed is that it
  serialises every readable event in one response with no pagination and no cap
  (`views.py:498`, `523-528`). For a long-lived user or a busy import job inside
  a large tenant, and for any platform caller, that is still a single response
  holding thousands of rows, each with its actor and tenant joined.
- **`/entity-trails/` and the `/me/` routes take unvalidated filters.**
  `?module_key=nonsense` and `?severity=bogus` return an empty page rather than
  a `400` (`views.py:417-425`, `450-458`), so a frontend typo reads as "no
  activity". The `/events/` route already has the validating serializer; these
  three should use it.
- **`?tenant=` is required on the `/me/` routes and then ignored.** Neither
  self-service view sets `tenant_param_required = False`, so a caller must
  assert a tenant (`vs_rbac/authentication.py:136`) that the queryset never
  uses (`views.py:412-415`, `443-448`). Same shape as the task monitor
  (`docs/console/console_task_monitor.md` §8).
- **The identity stream reads `AuditEvent` through a second copy of the
  predicate.** `AuthEventLogViewSet` (`vs_user/views/security.py:467`) serves
  `GET /v1/user/auth-events/` from this same table, filtered to
  `module_key=IDENTITY`, and gates it on `platform.audit.view` (`:478-491`).
  Until `9227d9e` it named `HasRBACPermission` and set no `rbac_permission` at
  all, so the gate enforced nothing and the queryset was unscoped. Its scoping
  (`vs_user/views/security.py:499-525`) spells out the same two arms by hand
  rather than importing `vs_audit.scoping.tenant_event_predicate`. It is
  currently correct and its comment explains why it is what it is, but a second
  copy is precisely what `scoping.py` was created to end: the Export Centre
  dataset had one, it was narrower, and Bright Star's officer saw rows on screen
  that were missing from her own export. This one should import the shared
  function.
- **Justified by design:** `/me/activity-on-me/` keeps system-authored events.
  `.exclude(actor_user=user)` compiles to
  `NOT (actor_user_id = X AND actor_user_id IS NOT NULL)`, so rows with a null
  actor survive the exclusion (verified against the generated SQL). A lockout
  applied by the system is exactly what belongs on a "things done to your
  account" tab.
- **Justified by design:** the list serializer omits `before_data`,
  `diff_data` and `metadata` and the detail serializer adds them
  (`serializers.py:85-106` vs `120-142`). Splitting the payload is right; the
  problem is that the detail half has no field-level gate, not that the split
  exists.

## 9. Permissions & tenant isolation

| Surface | Gate | Row boundary |
|---|---|---|
| `/events/`, `/events/<id>/` | `platform.audit.view` | `scope_events_to_caller` (`views.py:270`, `298-301`) |
| `/events/filter-options/` | `platform.audit.view` | no rows; the `tenants` roster is offered to platform callers only (`views.py:182-214`) |
| `/entity-trails/` | `platform.audit.view` | an `Exists` over the caller's readable events (`views.py:337-344`) |
| `/entity-trails/<type>/<id>/` | `platform.audit.view` | same predicate; no readable event is a `404` (`views.py:487-505`) |
| `/me/activity/`, `/me/activity-on-me/` | `IsAuthenticatedAndActive` only | the caller themselves |

`platform.audit.view` is seeded `is_restricted=False`, `sensitivity=NORMAL`
(`seed_platform_permissions.py:134-139`), granted by seed to `xvs_super_admin`
and `xvs_platform_admin` (`:277-308`), and is **deliberately
`PermissionScope.TENANT`**: it appears in `TENANT_HOLDABLE_KEYS`
(`:179-188`) because audit officers hold it *inside* a tenant. A school
granting it to its own auditor is intended, not an escalation.

**That is exactly why the key cannot be the boundary.** Holding it says nothing
about whose rows the holder may read, so the queryset has to say it, and until
`1da5c2a` it did not: `AuditEvent` has a plain manager, every read in this app
was `AuditEvent.objects.all()`, and Bright Star's audit officer opening the
Event Explorer was handed Greenfield's purchase-order approvals, Greenfield's
staff names and Greenfield's password resets, could open any of them by id, saw
Greenfield's incidents in her own heatmap, and could export the lot to CSV.
Eight surfaces had it, not one.

The answer now lives in `vs_audit/scoping.py`, in two deliberately separate
layers:

- `tenant_event_predicate(tenant)` (`scoping.py:34-65`) is the boundary: the
  `tenant` column, OR a null column carrying that tenant's pk in
  `metadata['tenant_id']`. It never widens, and a `tenant` of `None` fails
  closed rather than matching every null row.
- `audit_scope_predicate(request)` (`scoping.py:68-103`) is the boundary plus
  one policy: it returns `None` for a **platform-kind** caller, who reads across
  tenants by construction, because that is what the console is for.

The gate is the caller's **home** tenant kind, which no grant and no `?tenant=`
can change. Under impersonation `request.user` is the effective user, so a Codex
staffer proxied as a Bright Star account is confined to Bright Star, which is
what being proxied means.

Two consumers outside the views read the same module. The Export Centre dataset
`audit.events` calls `tenant_event_predicate` directly and never takes the
platform widening (`export_datasets.py:45-49`), because an export always covers
your own organisation; a platform reviewer who narrowed the screen with
`tenant_slug` is told so by name through `Unmapped`
(`export_datasets.py:141-154`). The audit CSV export inside this app takes the
full `scope_events_to_caller`, since it is a copy of the screen
(`views.py:683-688`).

Two things the key's namespace still does not give it. `is_vision_super_admin`
bypasses `HasRBACPermission` before it reads any key at all
(`vs_rbac/permissions.py:344-346`); since `a3b93d6` that role must live on a
PLATFORM-kind tenant (`vs_rbac/permissions.py:21`, clause at `:55`), which
closed the route by which a school admin could mint the platform by naming a
role. And genuinely platform-scoped keys can no longer be attached to a tenant
role at all: the grant models refuse them (`vs_rbac/models.py:91-111`) and the
tenant role serializer turns that refusal into a field error naming every
offending key (`vs_rbac/serializers/tenant.py:347-376`).

The two `/me/` routes are correctly self-scoped without any of this: one filters
`actor_user=request.user`, the other `entity_type="User",
entity_id=str(user.id)`, and user ids are unique platform-wide, so neither
leaks.

## 10. Code map

| File | Responsibility |
|---|---|
| `vs_audit/models.py` | `AuditEvent`, `EntityAuditTrail`, the five enums, the Python-level immutability guards |
| `vs_audit/migrations/0011_retire_entity_trail_rollup.py` | Drops the three stored counters and the `last_event_at` index; its docstring is the argument for doing so |
| `vs_audit/scoping.py` | The one answer to "which audit rows are mine": `tenant_event_predicate`, `audit_scope_predicate`, `scope_events_to_caller`, `latest_visible_event_at`, `visible_trail_counters` |
| `vs_audit/services.py` | `emit_audit_event`, `resolve_event_tenant`, `_build_summary`, `_SUMMARY_TEMPLATES`, `AuditDiffService` |
| `vs_audit/views.py:106-157` | `apply_audit_event_filters` - the one filter implementation, shared with the CSV export |
| `vs_audit/views.py:160-533` | The seven read views in this slice |
| `vs_audit/serializers.py` | List vs detail split, the trail's computed counters, `ChoiceListField`, `AuditEventFilterSerializer` |
| `vs_audit/export_datasets.py` | The Export Centre dataset, reading the shared predicate |
| `vs_audit/urls.py` | Route table under `/v1/audit/` |
| `vs_tenants/context.py:52-93` | `resolve_audit_identity`, `mark_audit_event_emitted`, `add_proxy_audit_metadata` |
| `vs_tenants/middleware.py:98-176` | The proxy fallback events this app's counter suppresses |
| `vs_user/services/audit.py` | `log_auth_event` - the identity/auth vocabulary and the biggest single writer |
| `vs_user/views/security.py:467-556` | `AuthEventLogViewSet` - a second reader of this table, with its own copy of the predicate |
| `vs_rbac/audit.py`, `vs_finance/audit.py`, `vs_tickets/services/audit.py` | Authoritative module logs that mirror here best-effort |

## 11. Test coverage & gaps

`vs_audit/tests.py` is 90 tests in 19 classes, all green at the time of writing
(`python manage.py test vs_audit --settings=apps.settings.local --noinput`). Not
all of them belong to this slice; the export and dashboard classes are covered
by the sibling documents. The ones that do:

- `AuditEventFilterContractTests` (`tests.py:60-168`) - repeated query values
  validate as lists and OR within a group; scalar values stay backward
  compatible and text is trimmed; search reaches the action code, actor email
  and actor full name; filter groups AND across each other; the `EXPORTS`,
  `PLATFORM` and `PROCUREMENT_ACTION` enum members exist.
- `AuditEventTenantFilterTests` (`tests.py:170-243`) and
  `EventExplorerTenantFilterEndpointTests` (`tests.py:596-664`) - `tenant_slug`
  narrows to one customer, `__none__` reaches the ownerless rows, an unknown
  slug is a `400`, matching is case and padding insensitive, and the tenant
  roster in `filter-options/` is offered to platform callers only.
- `AmbientTenantInheritanceTests` (`tests.py:245-336`) - an event inherits the
  request's tenant, an explicit one always wins, a PLATFORM assertion is not
  inherited, and an inherited tenant is findable through the filter.
- `ProxiedAuditAttributionTests` (`tests.py:338-463`) - proxied events are
  rewritten to the real actor; an explicitly real actor gets the same proxy
  context; the authoritative RBAC log and its mirror agree; third-party and
  system events are left alone; clearing request context drops the dual
  identity.
- `OnboardingActionTypeRegistrationTests` (`tests.py:465-509`) - every action
  constant the onboarding module emits is registered in `AuditActionType`, and
  emitting each one really writes a row.
- `AuditEventTenantIsolationTests` (`tests.py:1011-1092`),
  `AuditEventDetailTenantIsolationTests` (`:1094-1123`) and
  `EntityAuditTrailTenantIsolationTests` (`:1125-1160`) - a school officer reads
  only her own tenant's events, still sees her own pre-backfill history, cannot
  reach a null row belonging to nobody, gets a `404` on another tenant's event
  even holding its id, and gets a `404` rather than an empty trail on another
  tenant's entity; a platform reviewer still reads across tenants; `tenant_slug`
  narrows inside the boundary and cannot widen it; the permission gate is
  unchanged for the holder.
- `EntityTrailCounterTests` (`tests.py:1387-1475`) - the trail stores no
  counters and no tenant; a tenant caller counts and dates only what they can
  see; a platform caller counts every tenant's; the detail header agrees with
  the events under it for both.
- `EntityTrailCounterQueryCostTests` (`tests.py:1477-1547`) - one query answers a
  whole page for either caller, and the endpoint does not grow a query per extra
  trail.
- `RetiredTrailRollupTests` (`tests.py:1549-1700`) - after a bulk delete the
  count and both dates move with it, an emptied trail reports zero and stays out
  of a tenant's catalogue, its detail route is a `404`, emitting an event no
  longer writes to the trail table, and a renamed entity still refreshes its
  label.
- `EntityTrailOrderingTests` (`tests.py:1702-1781`) - the catalogue is ordered by
  the most recent event, by the caller's *own* events rather than somebody
  else's, an emptied trail sorts last instead of holding the top, and trails
  sharing a timestamp are ordered deterministically.

Still uncovered:

1. **The closed vocabulary, outside onboarding.** Nothing asserts that the
   action constants used by `vs_rbac/signals.py`, `vs_user/services/audit.py`,
   `vs_exports/audit.py` and the finance mirror are all registered in
   `AuditActionType`, which is the one guard against a silently dropped trail.
   `OnboardingActionTypeRegistrationTests` is the pattern to copy.
2. **Immutability.** No test that `save()` on an existing row raises, that
   `delete()` raises, or that `.update()` bypasses both. Finance tests its
   equivalent (`docs/finance/finance_audit_trail.md` §11).
3. **The savepoint contract.** No test that a database error inside
   `emit_audit_event` leaves the caller's own enclosing transaction committable,
   which is the whole reason `transaction.atomic` is there.
4. Also uncovered: the `entity_user` bulk resolution including the
   non-numeric `entity_id` branch, the unpaginated entity-trail detail route's
   size, both `/me/` routes, and the empty-list response shape.
