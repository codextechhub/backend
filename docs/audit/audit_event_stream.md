# audit_event_stream

The platform's **central activity stream**: one `AuditEvent` row per notable
action, written by eighteen call sites across nine apps, read back through the
Event Explorer. Routes are mounted at `/v1/audit/` (`apps/urls.py:28`):
`events/`, `events/filter-options/`, `events/<uuid:id>/`, `entity-trails/`,
`entity-trails/<type>/<id>/`, `me/activity/`, `me/activity-on-me/`.

---

## 1. What it is (and what it is NOT)

- **One append-only table plus a cached rollup.** `AuditEvent`
  (`models.py:176`) is the row; `EntityAuditTrail` (`models.py:362`) is a
  per-entity counter with first/last timestamps, upserted on every write.
- **One writer.** Everything goes through `emit_audit_event`
  (`services.py:97`), which resolves proxy identity, fills in a summary,
  creates the row and registers it on the trail.
- **This is a mirror, not a system of record.** `emit_audit_event` catches every
  exception and returns `None` (`services.py:168-170`). Finance
  (`vs_finance/audit.py:16-42`), RBAC (`vs_rbac/audit.py:64-88`) and tickets
  (`vs_tickets/services/audit.py:39-51`) each keep their own authoritative log
  and mirror here best-effort. Never answer a compliance question from this
  table when the module has its own trail; see
  `docs/finance/finance_audit_trail.md` §1.
- **The action vocabulary is closed.** `AuditActionType` (`models.py:74`) is a
  choices enum and `AuditEvent.save()` calls `full_clean()`
  (`models.py:350`), so an unregistered `action_type` raises, is swallowed, and
  the event is silently lost. The enum carries a comment saying exactly that
  (`models.py:129-132`).
- **It does NOT scope by tenant.** `AuditEvent` uses the plain default manager
  and every read view queries `AuditEvent.objects.all()` (`views.py:160`,
  `179`, `294`). See §9.
- **`AuditDiffService` (`services.py:173`) is a helper library, not a
  pipeline.** Callers use it to build JSON-safe `before_data`/`diff_data`; the
  audit app itself never calls it.

## 2. Domain model

| Model | File | Key fields |
|---|---|---|
| `AuditEvent` | `models.py:176` | `id` (UUID pk), `module_key`, `action_type`, `severity`, `status`, `actor_type`, `actor_user?`, `effective_user?`, `tenant?`, `impersonation_session?`, `actor_label`, `entity_type`, `entity_id` (text), `entity_label?`, `summary?`, `before_data`, `diff_data`, `metadata`, `event_at`, `is_locked` |
| `EntityAuditTrail` | `models.py:362` | `entity_type` + `entity_id` (unique together), `entity_label`, `event_count`, `first_event_at`, `last_event_at` |

Five enums drive the classification (`models.py:36-147`):

- `AuditSeverity`: `INFO`, `WARNING`, `CRITICAL`.
- `AuditStatus`: `SUCCESS`, `FAILED`, `DENIED`, `PARTIAL`.
- `AuditActorType`: `USER`, `SYSTEM`.
- `AuditModuleKey`: 13 surfaces, from `ONBOARDING` to `PLATFORM`.
- `AuditActionType`: 51 values across CRUD, identity, import, RBAC,
  impersonation, finance/procurement and the Export Centre block.

**Immutability is model-level only.** `save()` on an existing pk raises and
`delete()` always raises (`models.py:343-355`), but there are no database
triggers, so `AuditEvent.objects.filter(...).update(...)` and `.delete()`
bypass both. Compare `FinanceAuditLog`, which installs BEFORE UPDATE/DELETE
triggers in migration `0025` (`docs/finance/finance_audit_trail.md` §8). Two
data migrations already exercise that bypass on purpose:
`0003_remove_impersonated_request_history` deletes the retired
`IMPERSONATED_REQUEST` rows, and `0004_remove_notification_proxy_fallbacks`
deletes noisy `PROXY_CHANGE` rows and then rebuilds the affected
`EntityAuditTrail` counters from `Count`/`Min`/`Max`.

`entity_id` is a `CharField`, deliberately, so int pks, UUIDs and external refs
all fit (`models.py:274-277`). That is why the list view has to defend itself
when resolving `entity_type="User"` rows (§5).

Six composite indexes on `AuditEvent` (`models.py:318-325`) cover
`(module_key, action_type, event_at)`, `(entity_type, entity_id, event_at)`,
`(actor_type, actor_user, event_at)`, `(severity, status, event_at)`,
`(tenant, event_at)` and `(impersonation_session, event_at)`. Default ordering
is `-event_at`.

**Who writes the rows.** `emit_audit_event` is called from 83 sites in 18
files. The heavy ones: `vs_rbac/signals.py` (24 calls), `vs_schools/serializers.py`
(4), `vs_user/services/audit.py` (the whole identity/auth vocabulary, via
`log_auth_event`), `vs_tenants/middleware.py` (the proxy fallback pair),
`vs_admin_console/views.py` (impersonation bookends), plus the finance, tickets,
config, exports and import mirrors.

## 3. Endpoint map

Gate on every keyed route: `IsAuthenticatedAndActive & HasRBACPermission`. No
view sets `tenant_param_required = False`, so **`?tenant=<slug>` is required on
all seven routes** including the two `/me/` ones
(`vs_rbac/authentication.py:123-126`).

| Method + path | permission key | query params actually read | response |
|---|---|---|---|
| `GET /events/` | `platform.audit.view` | `module_key`, `action_type`, `severity`, `status` (repeatable, any-of), `actor_type`, `actor_user_id`, `impersonation_session_id`, `entity_type`, `entity_id`, `date_from`, `date_to`, `search` | Paginated `AuditEventListSerializer` (`views.py:114-167`) |
| `GET /events/filter-options/` | `platform.audit.view` | - | `{modules, actions, severities, statuses, actor_types}`, each `[{value,label}]` (`views.py:92-111`) |
| `GET /events/<uuid:id>/` | `platform.audit.view` | - | `AuditEventDetailSerializer`, including raw `metadata` (`views.py:170-185`) |
| `GET /entity-trails/` | `platform.audit.view` | `entity_type` (exact), `search` (id or label) | Paginated `EntityAuditTrailSerializer` (`views.py:192-214`) |
| `GET /entity-trails/<entity_type>/<entity_id>/` | `platform.audit.view` | - | `{trail, events}`, **unpaginated** (`views.py:278-319`) |
| `GET /me/activity/` | authenticated only | `module_key`, `severity`, `search` | Paginated events where the caller is the actor (`views.py:217-242`) |
| `GET /me/activity-on-me/` | authenticated only | `module_key`, `severity`, `search` | Paginated events targeting the caller, actor excluded (`views.py:245-275`) |

**The filter contract is validated, the trail filters are not.** `/events/`
runs every query param through `AuditEventFilterSerializer`
(`serializers.py:340`) with `raise_exception=True`, so a bad enum value or a
malformed date is a clean `400`, and `date_from > date_to` is rejected by name
(`serializers.py:381-392`). `/entity-trails/` and both `/me/` routes read
`request.query_params` directly (`views.py:205-214`, `230-242`, `259-275`) with
no validation at all.

`ChoiceListField` (`serializers.py:323`) is what makes
`?status=FAILED&status=DENIED` work while `?status=FAILED` still validates: a
bare string is wrapped in a list before the child `ChoiceField` runs. Repeated
values are OR'd within a group and AND'd across groups
(`views.py:50-57`; tested at `tests.py:74-129`).

## 4. Lifecycle / state machine

There is no state machine. Rows are inserted once and never transition:

```text
caller ──► emit_audit_event(...)
             │
             ├─ resolve_audit_identity(...)      rewrite actor under proxy
             ├─ _build_summary(...)              only when summary == ""
             ├─ AuditEvent.objects.create(...)   full_clean() runs here
             ├─ mark_audit_event_emitted()       suppresses the proxy fallback
             └─ EntityAuditTrail.get_or_create() ──► trail.register_event()
                                                      count += 1, first/last moved

any exception anywhere above ──► logged to the "vs_audit" logger, returns None
```

**There is no retention job.** Nothing prunes `AuditEvent`, nothing enforces a
`ComplianceRule` of type `RETENTION`, and the table grows forever. Contrast
`BackgroundJob`, which has a 90-day sweep
(`docs/console/console_task_monitor.md` §2).

## 5. Derivations

- **Actor attribution under proxy** (`services.py:126-130` →
  `vs_tenants/context.py:67-82`). During an impersonation session the
  authentication layer stashes `(actor, effective_user, session)` in a
  contextvar. `resolve_audit_identity` rewrites the event only when the caller
  attributed it to *either* request identity: the row then always names the
  real staff member as `actor_user`, keeps the impersonated account as
  `effective_user`, and pins `impersonation_session`. An event attributed to a
  genuine third party, or to the system, is left alone
  (`tests.py:239-253`).
- **`actor_type`** is derived, never passed: `USER` when a resolved
  `actor_user` exists, otherwise `SYSTEM`, and `actor_user` is nulled in the
  `SYSTEM` branch (`services.py:130,138`).
- **`summary`** falls back to a per-action template
  (`services.py:18-70,73-94`) rendered with `{actor}`, `{entity}`,
  `{entity_type}`. Unknown actions get `"{actor} performed {action_type} on
  {entity}"`. The actor name resolves `full_name` → `get_full_name()` →
  `email` → `"Unknown user"`, and is literally `"System"` when there is no
  actor. Under proxy the template renders the **proxier's** name, which is the
  behaviour asserted at `tests.py:197-198`.
- **`ip_address`** on the list serializer is not a column: it is
  `metadata["ip_address"]` (`serializers.py:71-72`), populated only by callers
  that pass a request, chiefly `log_auth_event`
  (`vs_user/services/audit.py:75`).
- **`entity_user`** resolves `entity_type="User"` rows to a name/email block
  through one bulk query built in `list()` rather than per row
  (`views.py:140-152`, `serializers.py:74-81`). The loop coerces `entity_id` to
  `int` and drops anything non-numeric, because `User.id` is a `BigAutoField`
  and a UUID-shaped `entity_id` from older audit code would otherwise make
  `filter(id__in=...)` raise `ValueError` (`views.py:137-150`).
- **`search`** matches any one of seven columns: summary, entity label, entity
  id, actor label, action code, actor email, and a `Concat`-annotated actor
  full name (`views.py:72-87`). The action-code and actor-identity arms are
  covered at `tests.py:106-115`.
- **`EntityAuditTrail.register_event`** (`models.py:395-404`) increments the
  count and widens the first/last window with a three-field `update_fields`
  save. It is a read-modify-write with no lock, so concurrent writers to the
  same entity can lose an increment (§8).

## 6. What writing an event actually does

Nothing posts and nothing is corrected. Two rows are written per call: the
`AuditEvent` insert and the `EntityAuditTrail` upsert plus counter save
(`services.py:134-164`). Neither runs in an explicit transaction, so a failure
between them leaves the event without a trail increment; the whole block is
inside the `try`, so that failure is logged and swallowed.

One side effect matters outside this app: `mark_audit_event_emitted()`
(`services.py:156-157`, `vs_tenants/context.py:52-54`) bumps a request-local
counter. `TenantContextCleanupMiddleware` only writes its vague
`PROXY_CHANGE` / `PROXY_ACTION_FAILED` fallback when that counter is still zero
at the end of a proxied request (`vs_tenants/middleware.py:118-125`). So every
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
      "effective_user": null, "tenant": null, "impersonation_session": null,
      "actor_label": "", "entity_type": "User", "entity_id": "411",
      "entity_label": "Ngozi Eze",
      "entity_user": { "id": "411", "full_name": "Ngozi Eze", "email": "ngozi@lekki.test" },
      "summary": "Failed login attempt for Ngozi Eze",
      "ip_address": "102.89.34.7", "event_at": "2026-08-13T18:22:04Z" }
  ] }
```

Note `"tenant": null` on an identity event: that is the norm, not a fixture
artefact (§8).

`GET /v1/audit/entity-trails/User/411/?tenant=codex` returns
`{trail: {...event_count, first_event_at, last_event_at}, events: [...]}` in a
`success_response` envelope with **no pagination block** and every event ever
recorded against that user.

## 8. Gotchas / known limitations

- **The Event Explorer has no tenant filter, and its key is not restricted.**
  `AuditEvent` uses the plain manager, `get_queryset` is
  `AuditEvent.objects.select_related("actor_user").all()` (`views.py:160`), and
  the detail view is a bare UUID lookup over the whole table
  (`views.py:179-181`). `platform.audit.view` is seeded `is_restricted=False`,
  `sensitivity=NORMAL`
  (`core/management/commands/seed_platform_permissions.py:131-139`). Nothing in
  the RBAC write path confines a `platform.*` key to a platform tenant:
  `TenantRoleTemplateListCreateView` accepts any `permission_keys`
  (`vs_rbac/views.py:503-518`), the serializer bulk-creates whatever
  `Permission.objects.filter(key__in=...)` returns
  (`vs_rbac/serializers/tenant.py:382-391`), and `validate_role_permissions`
  only checks the dependency graph (`vs_rbac/validators.py:159-192`). So a
  school admin holding `school.roles.create` can mint a school role carrying
  `platform.audit.view`, assign it, and read **every tenant's** audit stream
  including summaries, actor emails and per-event `metadata`. Fix at the choke
  point, not per view: refuse `platform.*` keys on non-platform tenant roles,
  and scope the queryset to `request.tenant` for any non-platform caller.
- **`metadata` is exposed raw on the detail route.**
  `AuditEventDetailSerializer` includes `metadata`, `before_data` and
  `diff_data` verbatim (`serializers.py:136-138`). Callers put IP addresses and
  user agents in there (`vs_user/services/audit.py:70-72`), tenant and school
  ids (`:62-67`), branch ids (`vs_config/services/audit.py:92`), and whatever
  else they felt like. Finance deliberately hides its equivalent field
  (`docs/finance/finance_audit_trail.md` §1); this surface has no FLS at all
  and no `view_sensitive` key to hang one on.
- **Most rows carry `tenant = NULL`.** Only three of the eighteen writer files
  pass `tenant=`: `vs_exports/audit.py:39`, `vs_admin_console/views.py:61`, and
  `vs_tenants/middleware.py`. Identity events put the tenant in `metadata`
  instead of the column (`vs_user/services/audit.py:60-63`), and the 24
  `vs_rbac/signals.py` calls, the finance mirror, the ticket mirror, the config
  mirror and the import mirror all omit it. Consequences: the
  `(tenant, event_at)` index is nearly useless, the `tenant` column in the list
  response is almost always `null`, and the Export Centre dataset, which
  filters `tenant=scope.tenant` (`export_datasets.py:31-34`), returns almost
  nothing. This is the single change that unblocks tenant scoping, so it has to
  land before the previous item can be fixed properly.
- **Append-only is enforced in Python only.** `save()`/`delete()` raise
  (`models.py:343-355`) but there is no DB trigger, so
  `AuditEvent.objects.filter(...).update(...)`, `.delete()` and raw SQL all
  succeed. Two migrations rely on that
  (`0003_remove_impersonated_request_history`,
  `0004_remove_notification_proxy_fallbacks`), which is legitimate, but it means
  the guarantee is a convention rather than a control. The finance table solved
  the same problem with triggers.
- **A typo in `action_type` deletes the audit trail silently.** `save()` runs
  `full_clean()`, an unregistered value fails choices validation, and
  `emit_audit_event` catches it and returns `None`
  (`models.py:350`, `services.py:168-170`). The action lands, the record of it
  does not, and the only trace is a line on the `vs_audit` logger. The enum
  comment warns about this (`models.py:129-132`) but nothing enforces it: there
  is no test asserting that every constant used by every caller exists in
  `AuditActionType`.
- **The entity-trail detail route is unbounded.** `EntityAuditTrailDetailView`
  serialises every event for the entity with no pagination and no cap
  (`views.py:294-314`). For a long-lived user or a busy import job that is a
  single response holding thousands of rows, each with its own actor join.
- **`register_event` can lose increments.** It reads `event_count`, adds one
  and saves (`models.py:399-404`) with no `F()` expression and no `select_for_update`.
  Two events for the same entity in flight at once produce one increment.
  `event_count` is therefore a good-enough display number, not a reconcilable
  total; the fix is `F("event_count") + 1`.
- **`/entity-trails/` and the `/me/` routes take unvalidated filters.**
  `?module_key=nonsense` and `?severity=bogus` return an empty page rather than
  a `400` (`views.py:234-241`, `266-274`), so a frontend typo reads as "no
  activity". The `/events/` route already has the validating serializer; these
  three should use it.
- **`?tenant=` is required on the `/me/` routes and then ignored.** Neither
  self-service view sets `tenant_param_required = False`, so a caller must
  assert a tenant (`vs_rbac/authentication.py:123-126`) that the queryset never
  uses (`views.py:231`, `261-264`). Same shape as the task monitor
  (`docs/console/console_task_monitor.md` §8).
- **Justified by design:** `/me/activity-on-me/` keeps system-authored events.
  `.exclude(actor_user=user)` compiles to
  `NOT (actor_user_id = X AND actor_user_id IS NOT NULL)`, so rows with a null
  actor survive the exclusion (verified against the generated SQL). A lockout
  applied by the system is exactly what belongs on a "things done to your
  account" tab.
- **Justified by design:** the list serializer omits `before_data`,
  `diff_data` and `metadata` and the detail serializer adds them
  (`serializers.py:83-104` vs `118-140`). Splitting the payload is right; the
  problem is that the detail half has no field-level gate, not that the split
  exists.

## 9. Permissions & tenant isolation

| Surface | Gate | Seeded? |
|---|---|---|
| `/events/`, `/events/filter-options/`, `/events/<id>/` | `platform.audit.view` | Yes, `NORMAL`, **not restricted** (`seed_platform_permissions.py:134-139`) |
| `/entity-trails/`, `/entity-trails/<type>/<id>/` | `platform.audit.view` | as above |
| `/me/activity/`, `/me/activity-on-me/` | `IsAuthenticatedAndActive` only | n/a |

`platform.audit.view` is granted by seed to `xvs_super_admin` and
`xvs_platform_admin` only, and `is_vision_super_admin` bypasses the check
entirely (`vs_rbac/permissions.py:169-171`). That is the intended audience.
What is missing is any enforcement that the audience stays that way: see the
first §8 item.

**There is no tenant isolation on the keyed routes.** No queryset in this slice
filters on `request.tenant`, and the column that would make it possible is null
on most rows. The two `/me/` routes are correctly self-scoped: one filters
`actor_user=request.user`, the other `entity_type="User", entity_id=str(user.id)`,
and user ids are unique platform-wide, so neither leaks.

## 10. Code map

| File | Responsibility |
|---|---|
| `vs_audit/models.py` | `AuditEvent`, `EntityAuditTrail`, the five enums, the Python-level immutability guards |
| `vs_audit/services.py` | `emit_audit_event`, `_build_summary`, `_SUMMARY_TEMPLATES`, `AuditDiffService` |
| `vs_audit/views.py:47-89` | `apply_audit_event_filters` - the one filter implementation, shared with the CSV export |
| `vs_audit/views.py:92-319` | The seven read views in this slice |
| `vs_audit/serializers.py` | List vs detail split, `ChoiceListField`, `AuditEventFilterSerializer` |
| `vs_audit/urls.py` | Route table under `/v1/audit/` |
| `vs_tenants/context.py:52-93` | `resolve_audit_identity`, `mark_audit_event_emitted`, `add_proxy_audit_metadata` |
| `vs_tenants/middleware.py:98-175` | The proxy fallback events this app's counter suppresses |
| `vs_user/services/audit.py` | `log_auth_event` - the identity/auth vocabulary and the biggest writer |
| `vs_rbac/audit.py`, `vs_finance/audit.py`, `vs_tickets/services/audit.py` | Authoritative module logs that mirror here best-effort |

## 11. Test coverage & gaps

`tests.py` is 10 tests in two classes, all green at the time of writing
(`python manage.py test vs_audit --settings=apps.settings.local --noinput`).

- `AuditEventFilterContractTests` (`tests.py:26-134`) - repeated query values
  validate as lists and OR within a group; scalar values stay backward
  compatible and text is trimmed; search reaches the action code, actor email
  and actor full name; filter groups AND across each other; the `EXPORTS`,
  `PLATFORM` and `PROCUREMENT_ACTION` enum members exist.
- `ProxiedAuditAttributionTests` (`tests.py:137-264`) - proxied events are
  rewritten to the real actor; an explicitly real actor gets the same proxy
  context; the authoritative RBAC log and its mirror agree; third-party and
  system events are left alone; clearing request context drops the dual
  identity.

Every test drives the service and filter functions directly. **No test in this
app issues an HTTP request**, which is why §8 reads the way it does. Uncovered:

1. **Permissions.** No `403` test for a caller without `platform.audit.view`
   on any of the five keyed routes, and no test that a school role carrying
   the key is refused, which is the finding above.
2. **Cross-tenant isolation.** Every fixture event is written without a tenant
   (`tests.py:37-72`), so nothing would fail if a tenant filter were added and
   nothing fails today for its absence.
3. **The closed vocabulary.** Nothing asserts that the action constants used
   by `vs_rbac/signals.py`, `vs_user/services/audit.py`, `vs_exports/audit.py`
   and the finance mirror are all registered in `AuditActionType`, which is the
   one guard against a silently dropped trail.
4. **Immutability.** No test that `save()` on an existing row raises, that
   `delete()` raises, or that `.update()` bypasses both. Finance tests its
   equivalent (`docs/finance/finance_audit_trail.md` §11).
5. Also uncovered: the `entity_user` bulk resolution including the
   non-numeric `entity_id` branch, the `EntityAuditTrail` counter arithmetic,
   the unpaginated entity-trail detail route and its 404 branch, both `/me/`
   routes, `filter-options/`, and the empty-list response shape.
