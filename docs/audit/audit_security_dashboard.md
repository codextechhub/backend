# audit_security_dashboard

One endpoint, `GET /v1/audit/dashboard-summary/`, that answers the Security
Dashboard in a single request: six KPI counters, a 14-day severity series, a
24-hour module breakdown, a 30-day sign-in series, and a 7x24 heatmap of
critical events. It reads five models across three apps and writes nothing.

---

## 1. What it is (and what it is NOT)

- **A read-only aggregate view.** `AuditDashboardSummaryView`
  (`views.py:423-529`) is a plain `APIView` with one `get`. There is no model,
  no cache, no stored snapshot: every field is recomputed per request.
- **It is not built only on audit events.** Three of the six KPIs come from
  other apps: `LoginSession` and `AccountLockout` from `vs_user`,
  `ImpersonationSession` from `vs_admin_console` (`views.py:441-442`). The
  sign-in series comes from `AuthAttempt`, which is deliberately **not** an
  audit table: it drives rate limiting and lockout policy, not compliance
  (`vs_user/services/audit.py:171-175`).
- **It is not consistently scoped.** Two of the underlying models scope
  themselves to the asserted tenant and three do not, so the numbers on one
  card do not all mean the same thing. This is the headline finding; see §8.
- **It does not paginate or filter.** No query parameters are read at all
  (`views.py:440`). Every window is hard-coded in the view.
- **It is not the overview console.** `/v1/admin/overview/`
  (`docs/console/console_overview.md`) is the platform landing screen; this is
  the security-specific one and the two overlap only on impersonation counts.

## 2. Domain model

This slice owns no table. Five models feed it:

| Model | Where | What it contributes | Manager |
|---|---|---|---|
| `AuditEvent` | `vs_audit/models.py:176` | `events_24h`, `critical_24h`, `failed_denied_24h`, severity series, module breakdown, heatmap | plain default manager |
| `LoginSession` | `vs_user/models.py:434` | `active_sessions` | `TenantAwareManager()` (`:467`) |
| `AuthAttempt` | `vs_user/models.py:492` | `signin_series` | `TenantAwareManager()` (`:523`) |
| `AccountLockout` | `vs_user/models.py:543` | `locked_accounts` | plain default manager, and the model has **no tenant field at all** |
| `ImpersonationSession` | `vs_admin_console/models.py:14` | `active_impersonations` | plain default manager |

`TenantAwareManager.get_queryset` (`vs_rbac/managers.py:100-118`) adds a
`tenant=<current>` condition whenever the request-local tenant is set, which
`TenantJWTAuthentication` always does (`vs_rbac/authentication.py:139`). That
is the whole difference between the scoped and unscoped rows in the table
above: it is a property of the model's manager, not of anything in this view.

## 3. Endpoint map

| Method + path | permission key | query params | response |
|---|---|---|---|
| `GET /dashboard-summary/` | `platform.audit.view` | none read | `{kpis, severity_series, module_breakdown, signin_series, critical_heatmap, generated_at}` in a `success_response` envelope, no pagination block (`views.py:519-529`) |

Gate: `IsAuthenticatedAndActive & HasRBACPermission` (`views.py:437-438`).
`?tenant=<slug>` is required, because the view does not set
`tenant_param_required = False` (`vs_rbac/authentication.py:123-126`) - and
here, unlike the `/me/` routes, the assertion genuinely matters for two of the
six KPIs.

Response shape:

| Key | Type | Meaning |
|---|---|---|
| `kpis.active_sessions` | int | `LoginSession.is_active=True`, **this tenant** |
| `kpis.events_24h` | int | audit events in the trailing 24h, **all tenants** |
| `kpis.critical_24h` | int | of those, `severity=CRITICAL`, all tenants |
| `kpis.failed_denied_24h` | int | of those, `status in (FAILED, DENIED)`, all tenants |
| `kpis.locked_accounts` | int | `AccountLockout.locked_until > now`, all tenants |
| `kpis.active_impersonations` | int | `ImpersonationSession.status="ACTIVE"`, all tenants |
| `severity_series` | list | `[{date, INFO, WARNING, CRITICAL}]`, last 14 days, ascending |
| `module_breakdown` | list | `[{module_key, count}]`, last 24h, busiest first |
| `signin_series` | list | `[{date, SUCCESS, FAIL}]`, last 30 days, ascending |
| `critical_heatmap` | int[7][24] | `[weekday][hour]`, last 30 days |
| `generated_at` | ISO string | `timezone.now()` at the top of the handler |

## 4. Lifecycle / state machine

None. One `GET`, six independent queries plus one Python loop, no writes, no
state carried between calls. `generated_at` is the only thing resembling a
timestamp and it is computed fresh each time (`views.py:444,527`).

## 5. Derivations

All five windows come off one `now = timezone.now()` (`views.py:444-447`):
24 hours, 14 days, 30 days.

- **`events_24h` is reused.** The 24-hour queryset is built once
  (`views.py:449`) and then filtered three more times for `critical_24h`,
  `failed_denied_24h` and `module_breakdown` (`views.py:453-457,481`). Django
  querysets are lazy, so that is four separate `COUNT`/`GROUP BY` round trips,
  not one scan.
- **`severity_series`** groups by `TruncDate("event_at")` and `severity`, then
  pivots in Python into `{date, INFO, WARNING, CRITICAL}` with each day
  seeded to zeros before assignment (`views.py:463-477`). Days with no events
  at all are **absent from the list**, not zero-filled, so the frontend must
  not index by position.
- **`module_breakdown`** is `values("module_key").annotate(Count("id")).order_by("-count")`
  (`views.py:480-488`). Modules with no events in the window are absent.
- **`signin_series`** groups `AuthAttempt` by day and `result`, then buckets
  every non-`SUCCESS` result into `FAIL` with `+=` rather than assignment,
  which is what lets several distinct failure results collapse into one
  number (`views.py:491-506`).
- **`critical_heatmap`** is the one derivation that leaves the database.
  A 7x24 grid of zeros is allocated, then **every** critical event of the last
  30 days is iterated in Python and bucketed by
  `timezone.localtime(event.event_at)` weekday and hour
  (`views.py:509-517`). `.only("event_at")` keeps the row narrow but does not
  bound the count. The local-time conversion is the reason it is not a
  database aggregate: `TruncHour` would bucket in UTC and the grid would be
  shifted by the server's offset.
- **`is_locked_now()` is not used.** `locked_accounts` counts
  `locked_until__gt=now` directly (`views.py:458`), which matches the model
  helper's logic (`vs_user/models.py:557`) but bypasses it.

## 6. What writing does

Nothing is written. No audit event records that the security dashboard was
opened, which is consistent with the rest of `vs_audit` (see
`docs/audit/audit_event_stream.md` §6) and with the task monitor
(`docs/console/console_task_monitor.md` §6).

## 7. Worked example

```text
GET /v1/audit/dashboard-summary/?tenant=codex
```

```json
{ "success": true, "message": "Dashboard summary retrieved.",
  "data": {
    "kpis": { "active_sessions": 37, "events_24h": 1842, "critical_24h": 3,
              "failed_denied_24h": 61, "locked_accounts": 2,
              "active_impersonations": 1 },
    "severity_series": [
      { "date": "2026-08-01", "INFO": 210, "WARNING": 4, "CRITICAL": 0 },
      { "date": "2026-08-02", "INFO": 188, "WARNING": 1, "CRITICAL": 1 }
    ],
    "module_breakdown": [ { "module_key": "IDENTITY", "count": 1204 },
                          { "module_key": "RBAC", "count": 311 } ],
    "signin_series": [ { "date": "2026-07-16", "SUCCESS": 88, "FAIL": 5 } ],
    "critical_heatmap": [[0,0,1,0,"…21 more"], "…6 more rows"],
    "generated_at": "2026-08-14T09:12:44.118Z"
  } }
```

Read that response carefully: `active_sessions: 37` is the codex tenant's,
`events_24h: 1842` is the whole platform's. Nothing in the payload says so.

## 8. Gotchas / known limitations

- **Four of the six KPIs count every tenant, two count one, and the card does
  not distinguish them.** `active_sessions` and `signin_series` are narrowed by
  `TenantAwareManager` (`vs_user/models.py:467,523`); `events_24h`,
  `critical_24h`, `failed_denied_24h`, `locked_accounts`,
  `active_impersonations`, `severity_series`, `module_breakdown` and
  `critical_heatmap` are not, because `AuditEvent`, `AccountLockout` and
  `ImpersonationSession` all use the plain manager. A platform operator reading
  a platform-wide number is fine. The problem is that the same endpoint answers
  a school tenant's assertion with the same platform-wide numbers next to that
  tenant's own session count, so the two halves of one card contradict each
  other. This is the same defect class as `console_overview`
  (`docs/console/console_overview.md` §8), and it cannot be fixed here alone:
  most audit rows carry `tenant = NULL` in the first place
  (`docs/audit/audit_event_stream.md` §8), so a `tenant=` filter added today
  would return zeros rather than correct numbers. Backfill the column, then
  scope.
- **`locked_accounts` can never be scoped without a schema change.**
  `AccountLockout` has no tenant or school field at all
  (`vs_user/models.py:543-556`); it hangs off `user` alone. Scoping it means
  joining `user__tenant`, which is a real change, not a manager swap.
- **The heatmap loads every critical event of the last 30 days into Python.**
  `for event in critical_qs.only("event_at")` (`views.py:515`) is unbounded by
  construction. It is cheap today because critical events are rare, and it
  becomes the slowest thing on the page the first time something starts
  emitting `CRITICAL` in a loop. If it needs bounding, the honest fix is a
  database aggregate over `TruncHour` with the tenant's timezone applied in
  SQL, not a `LIMIT`, which would silently under-count.
- **Six aggregates, no cache, on a screen built to be polled.** Per request:
  three counts on `LoginSession`/`AccountLockout`/`ImpersonationSession`, four
  passes over `AuditEvent` in the 24-hour window, a 14-day `GROUP BY`, a 30-day
  `GROUP BY` on `AuthAttempt`, and the heatmap scan (`views.py:449-517`). The
  `(severity, status, event_at)` and `(module_key, action_type, event_at)`
  indexes cover the audit side (`models.py:318-325`); nothing covers the
  repetition.
- **Series omit empty periods.** A day with no events is missing from
  `severity_series` and a day with no attempts is missing from `signin_series`
  (`views.py:474-477`, `503-506`), and a module with no events is missing from
  `module_breakdown`. Any chart that assumes 14 or 30 evenly spaced points will
  mis-plot a quiet week. This is a frontend-visible contract worth stating
  explicitly rather than changing.
- **`signin_series` buckets every non-`SUCCESS` result as `FAIL`.**
  `AuthAttempt.Result` has more members than two (`vs_user/models.py:494`), and
  the `+=` at `views.py:502` deliberately merges them. Fine for a
  success-versus-failure chart, wrong if anyone later wants to separate a
  locked-out attempt from a bad password.
- **The whole payload is fixed-window.** No `?days=`, no `?from=`/`?to=`, no
  module filter (`views.py:440`). Every question this screen cannot answer
  requires a code change.
- **`PARTIAL` severity and status values never appear.** `failed_denied_24h`
  counts `FAILED` and `DENIED` only (`views.py:455-457`), and
  `severity_series` seeds exactly three severity keys (`views.py:472`). Both
  match the current enums (`models.py:36-54`), but a new member added to either
  enum will be silently dropped from this response, not raised.
- **Justified by design:** `AuthAttempt` rather than `AuditEvent` powers the
  sign-in chart. The two tables record the same events for different purposes,
  and `AuthAttempt` is the one that survives an audit write failure, because
  `emit_audit_event` swallows its own exceptions while `record_attempt` writes
  independently (`vs_user/services/audit.py:171-186`).
- **Justified by design:** the heatmap uses `timezone.localtime`
  (`views.py:516`). A hour-of-day grid rendered in UTC would be actively
  misleading for a Lagos-hours operations team.

## 9. Permissions & tenant isolation

| Surface | Gate | Seeded? |
|---|---|---|
| `/dashboard-summary/` | `platform.audit.view` | Yes, `NORMAL`, **not restricted** (`core/management/commands/seed_platform_permissions.py:134-139`) |

The key is the same one that opens the Event Explorer, and it carries the same
weakness: nothing stops a school-tenant role template from being granted a
`platform.*` key (`docs/audit/audit_event_stream.md` §8), and this endpoint
then hands that caller platform-wide security counters. Read the two §8 items
together: the Event Explorer leaks the detail, this endpoint leaks the shape.

Partial tenant isolation, described in full above: two signals scoped, six not.

## 10. Code map

| File | Responsibility |
|---|---|
| `vs_audit/views.py:423-529` | The whole slice: KPIs, three series, the heatmap |
| `vs_audit/urls.py:22` | Route registration |
| `vs_rbac/managers.py:78-118` | `TenantAwareManager` - why two signals are scoped and the rest are not |
| `vs_user/models.py:434-580` | `LoginSession`, `AuthAttempt`, `AccountLockout` |
| `vs_admin_console/models.py:14-63` | `ImpersonationSession` and its `ACTIVE` status |
| `vs_user/services/audit.py:171-186` | `record_attempt` - what actually fills `signin_series` |

## 11. Test coverage & gaps

**Zero.** No test in `vs_audit/tests.py` touches this view, and no test
elsewhere calls `/v1/audit/dashboard-summary/`. The 10 tests in the app cover
the filter contract and proxy attribution only.

What is needed, in priority order:

1. **`403` without `platform.audit.view`**, driving the real auth path rather
   than `force_authenticate`, so `?tenant=` is exercised too.
2. **Tenant scoping**, with fixtures in two tenants, asserting explicitly which
   KPIs are per-tenant and which are platform-wide. Written today it pins the
   current behaviour; written after the backfill it proves the fix.
3. **The pivots**, which are pure Python and cheap to test: a day with only
   `INFO` events, a day with none at all (asserting it is absent, not zero), a
   non-`SUCCESS` `AuthAttempt` result landing in the `FAIL` bucket.
4. **The heatmap**, with one critical event placed at a known local hour near a
   UTC day boundary, which is the case `timezone.localtime` exists for.
5. **The empty response shape**, since `success_response` coerces `[]` to `{}`
   and every one of the four list fields can legitimately be empty on a fresh
   tenant.
