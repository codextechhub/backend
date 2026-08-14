# console_impersonation

The **proxy-session** surface: starting, listing, searching targets for and
ending an audited session in which one person acts as another. Routes are at
`/v1/admin/impersonations/`.

---

## 1. What it is (and what it is NOT)

- Two audiences share one viewset. **Platform (CX) staff** proxy across tenants
  under the `platform.impersonation.*` namespace; a **school actor** proxies only
  inside their own tenant under `school.impersonation.*`. The two sets are chosen
  by the actor's **home tenant kind** and are never unioned
  (`views.py:108-134`).
- The session record is only the *permission slip*. The actual identity
  substitution happens in the auth layer: the client resends the session id in
  the `X-Impersonation-Session` header, and `TenantJWTAuthentication` swaps
  `request.user` for the target (`vs_rbac/authentication.py:12,16-69,96-145`).
- The **access trail and the per-request audit rows** are written by
  `TenantContextCleanupMiddleware`, not by this app
  (`vs_tenants/middleware.py:25-53,110-172`).

**This is not the RBAC evaluator.** While a proxy is live, permissions are
evaluated as the *target*, never as the actor, and never as a union of the two
(`vs_rbac/authentication.py:133-137`).

## 2. Domain model

| Model | Where | Notes |
|---|---|---|
| `ImpersonationSession` | `models.py:8-63` | The whole app. `staff_user` (actor), `target_user`, `tenant`, `justification`, `status`, `started_at`, `ends_at`, `ended_at`, `last_activity_at`, `access_log` |
| `vs_audit.AuditEvent` | `vs_audit/models.py:252-258` | Carries a nullable `impersonation_session` FK, so every row written during a proxy points back at the session |
| `vs_tenants.Tenant` | `receivers.py:15-19` | A `post_save` leaving `ACTIVE` ends every session in that tenant |

Three statuses only: `ACTIVE`, `ENDED`, `EXPIRED` (`models.py:15-19`). All three
FKs are `PROTECT`, so a session pins its actor, target and tenant permanently.

`Model.clean()` carries the real invariants - `ends_at > started_at`, a
non-blank justification, and **the target must belong to the session's tenant**
(`models.py:46-52`). DRF never calls `full_clean()`, so none of the three is
enforced on the generic router routes (see §8).

The table has **no indexes and no `Meta.ordering`** (`models.py:8-63`), while the
list filters on `tenant` + `status` and orders by `-started_at, -pk`
(`views.py:84-101,272-278`).

## 3. Endpoint map

Every route requires `?tenant=<slug>`; the viewset sets
`platform_cross_tenant_param = True`, which is what lets a platform actor assert
a school's slug (`views.py:106`; `vs_rbac/authentication.py:112-121`).

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /impersonations/` | `platform.impersonation.view` / `school.impersonation.view` | Query `status` | Paginated sessions for the asserted tenant; sweeps stale rows first (`views.py:265-279`) |
| `GET /impersonations/<pk>/` | same | - | One session (`views.py:70-101`) |
| `GET /impersonations/targets/` | any of `start_all`, `start_cx`, `start_school` (platform) / `school.impersonation.start` | Query `search` (2-64 chars) | Paginated candidate users (`views.py:177-263`) |
| `POST /impersonations/start/` | see §5 - depends on the asserted tenant's kind | `target_user`, `justification?`, `duration_minutes?` (5-240) | `201` with the session (`views.py:282-380`) |
| `POST /impersonations/end/` | `*.impersonation.end` **or** any start key (owner-only) | `session_id` | `200` with the ended session (`views.py:384-454`) |
| `POST /impersonations/` | falls back to the **view** key | Full `ImpersonationSessionSerializer` body | `201`. **Unintended - see §8** |
| `PUT`/`PATCH` `/impersonations/<pk>/` | falls back to the **view** key | Same | `200`. **Unintended - see §8** |
| `DELETE /impersonations/<pk>/` | falls back to the **view** key | - | `204`. **Unintended - see §8** |

`targets` rejects a query under 2 characters or over 64 with a `400`
(`views.py:193-203`). `start` and `end` answer `404 "…not found"` rather than
`403` for anything out of scope, so neither confirms that a tenant, user or
session exists (`views.py:310-333,426-427`).

## 4. Lifecycle / state machine

```text
                      start/  ────────────────────────────────► ACTIVE
                                (any prior ACTIVE session owned by the
                                 same actor is ENDED in the same commit)

ACTIVE ──► ENDED    owner calls end/                      + audit bookend
       ──► ENDED    end-key holder terminates it          + audit bookend
       ──► ENDED    target or actor logs out / is suspended   (silent)
       ──► ENDED    tenant leaves ACTIVE                      (silent)
       ──► EXPIRED  ends_at passed, or idle past the limit    (silent)
```

Four different code paths end a session:

1. `POST /end/` - the only path that writes an audit row
   (`views.py:440-449`).
2. `end_impersonations_for_user(user)` - bulk `.update()` when the actor or the
   target logs out, is suspended, deactivated, force-logged-out, has their
   password reset or their email changed (`services.py:23-28`, called from
   `vs_user/services/audit.py:114` and `vs_user/views/auth.py:194`).
3. `end_impersonations_for_tenant(tenant)` - bulk `.update()` from the tenant
   `post_save` receiver (`services.py:32-37`; `receivers.py:15-19`).
4. `sweep_stale_impersonations()` - flips overdue and idle rows to `EXPIRED`
   (`services.py:8-19`), run on every `list` (`views.py:265-270`) and, for one
   session at a time, inside authentication (`vs_rbac/authentication.py:34-55`).

**Starting is a switch, not a stack.** `start` ends every `ACTIVE` session the
actor owns before creating the new one, and emits one `IMPERSONATION_ENDED`
event per replaced session even though the database update was a single bulk
`UPDATE` (`views.py:336-356`). Validation runs *before* the switch, so a failed
target selection leaves the current proxy untouched (`views.py:319-333`).

The actor row is locked with `select_for_update()` first, so two simultaneous
start requests cannot both create an `ACTIVE` session (`views.py:318`).

## 5. Derivations

- **Which permission key `start` needs** is decided by the *asserted tenant's*
  kind, because the target is pinned to that tenant: `PLATFORM` requires
  `start_all` or `start_cx`, anything else requires `start_all` or
  `start_school` (`views.py:145-161`). School actors have no tiering at all -
  one `school.impersonation.start` covers the whole own-tenant pool
  (`views.py:119-133`).
- **The target pool is a predicate, never a caller-supplied filter.** For a
  platform actor it starts as `Q(pk__in=[])` and is widened only by the keys
  actually held; for a school actor it is pinned to `Q(tenant_id=actor.tenant_id)`
  and is therefore immune to `?tenant=` (`views.py:205-225`).
- **Search matching**: one term matches first name, last name or email
  (`icontains`); two or more terms are split into first/rest and matched in both
  orders, so "ada obi" and "obi ada" both hit (`views.py:227-248`).
- **`staff_type_label` / `target_type_label`**: "XVS Staff" when the user holds
  any `ACTIVE` role whose key starts with `xvs_`, otherwise the user-type display
  label (`serializers.py:24-39`). The list prefetches those assignments into
  `_active_proxy_roles`, so the label costs no query per row
  (`views.py:86-101`).
- **Idle expiry**: an open-ended session (`ends_at IS NULL`) dies when
  `last_activity_at + proxy_idle_timeout_minutes <= now`. The setting defaults to
  **30 minutes**, is bounded 5-120, and is tenant/branch overridable
  (`vs_config/runtime_settings.py:22,31,58`). Authentication reads it **per
  session** with `tenant=` and `branch=`; the list sweep reads it with **no
  scope at all** (`vs_rbac/authentication.py:39-47` against `services.py:10-15`).
- **`last_activity_at`** is bumped by the middleware after *every* proxied
  response, success or failure (`vs_tenants/middleware.py:33-35,50`).
- **`access_log`** records only successful safe-method requests, deduped by
  path, capped at **200 distinct paths** with existing entries continuing to
  count past the cap (`vs_tenants/middleware.py:22,36-49`).

## 6. What posting does to the ledger

Nothing posts. The writes are: `ImpersonationSession` rows and status
transitions, `last_activity_at`/`access_log` updates on every proxied request,
and audit events.

Four audit action types belong to this surface
(`vs_audit/models.py:116-119`):

| Action type | Written by | When |
|---|---|---|
| `IMPERSONATION_STARTED` | `views.py:367-374` | `start/` succeeds |
| `IMPERSONATION_ENDED` | `views.py:346-356,442-449` | `end/`, and once per session replaced by a switch |
| `PROXY_CHANGE` | `vs_tenants/middleware.py:159-172` | A successful proxied write that emitted no event of its own |
| `PROXY_ACTION_FAILED` | `vs_tenants/middleware.py:138-152` | Any proxied response `>= 400`; `DENIED` for 401/403, else `FAILED` |

The last two are **fallbacks**: they fire only when the feature itself wrote no
audit event (`vs_tenants/middleware.py:115`), and three notification
read-state paths are excluded as UI bookkeeping
(`vs_tenants/middleware.py:15-20`).

Every row is scoped by the *initiating* surface, not the asserted tenant:
`module_key="PLATFORM"` for a platform actor, `"SCHOOL"` otherwise, so a
school-initiated proxy never lands in the platform-only audit stream
(`views.py:54`; `vs_tenants/middleware.py:126-127`).

Two reads write as a side effect: `GET /impersonations/` runs the stale sweep
(`views.py:265-270`), and every proxied `GET` updates the session row
(`vs_tenants/middleware.py:110`).

## 7. Worked example

```text
POST /v1/admin/impersonations/start/?tenant=corona
{ "target_user": 811, "justification": "Investigating a reported billing defect." }
```

```json
{ "success": true, "message": "Impersonation session started.",
  "data": { "id": 44, "staff_user": 12, "staff_email": "ada@codexng.com",
            "staff_type_label": "XVS Staff", "tenant": 3,
            "tenant_name": "Corona Secondary School", "tenant_slug": "corona",
            "target_user": 811, "target_email": "bursar@corona.test",
            "target_type_label": "School Staff",
            "justification": "Investigating a reported billing defect.",
            "status": "ACTIVE", "started_at": "2026-08-14T09:14:02Z",
            "ends_at": null, "ended_at": null,
            "last_activity_at": "2026-08-14T09:14:02Z", "access_log": [] } }
```

The client then replays every request with both the tenant assertion and the
session header:

```text
GET /v1/finance/invoices/?tenant=corona&entity=CORONA
X-Impersonation-Session: 44
```

`request.user` is now the bursar, `request.actor_user` is still Ada, and RBAC is
evaluated against the bursar's grants alone
(`vs_rbac/authentication.py:133-137`). `ends_at` was omitted, so the session runs
until Ada exits or goes idle for 30 minutes.

## 8. Gotchas / known limitations

- **The generic CRUD routes are a full privilege-escalation path, and they are
  gated by the read key.** `ImpersonationSessionViewSet` is a plain
  `ModelViewSet`, so the router publishes `POST /impersonations/`,
  `PUT`/`PATCH /impersonations/<pk>/` and `DELETE /impersonations/<pk>/`
  (`views.py:70`; `urls.py:14`). `get_permissions` maps only `targets`, `start`,
  `end`, `list` and `retrieve`; every other action falls through
  `.get(self.action, "…impersonation.view")` to the **view** key
  (`views.py:119-133,162-174`). `ImpersonationSessionSerializer` leaves
  `staff_user`, `tenant`, `target_user`, `justification`, `status`, `started_at`
  and `ends_at` writable (`serializers.py:41-66`), and DRF never calls
  `Model.clean()`, so the "target must belong to the impersonation tenant" rule
  (`models.py:51-52`) does not run. A holder of `school.impersonation.view` -
  seeded to the **School Admin** role
  (`core/management/commands/seed_school_permissions.py:109`) - can therefore
  `POST` a row with `staff_user` = themselves, `tenant` = their own tenant and
  `target_user` = **any active user in any tenant, including a Vision Super
  Admin**. `_load_impersonation` then accepts it: it checks only
  `staff_user=actor`, `status="ACTIVE"`, expiry, and that a non-platform actor's
  session tenant equals their own - which it does
  (`vs_rbac/authentication.py:24-69`). Riding that session makes
  `is_vision_super_admin(request.user)` true, which bypasses RBAC entirely
  (`vs_rbac/permissions.py:168-170`). None of the `start` guardrails apply: no
  justification, no self-proxy exclusion, no single-session switch, no tenant
  pinning of the target, and **no audit bookend at all**, because
  `_emit_proxy_lifecycle_event` is only called from `start`/`end`
  (`views.py:46-66`). `PATCH` is worse still - it can flip an `ENDED` session
  back to `ACTIVE` or re-point a live session at a new target. This is the one
  item in this slice worth fixing before anything else; the fix is to publish
  only the actions the module intends (`list`, `retrieve`, `targets`, `start`,
  `end`).
- **Justification is optional in practice.** The model requires a non-blank
  justification (`models.py:49-50`), but `ImpersonationStartSerializer` declares
  it `required=False, allow_blank=True` and `start` substitutes the literal
  `"Started from proxy user menu."` when it is missing
  (`serializers.py:101`; `views.py:362`). Every impersonation key is seeded at
  `_CRITICAL` sensitivity
  (`core/management/commands/seed_platform_permissions.py:58-62`), yet the
  default record of *why* someone read a customer's data is a placeholder.
- **The stale sweep is global and reads the platform default.** `get_queryset`
  calls `sweep_stale_impersonations()` on every `list`, and the sweep filters on
  nothing but `status="ACTIVE"` plus the deadline, so one school admin opening
  their monitoring screen expires rows in **every** tenant
  (`views.py:265-270`; `services.py:16-19`). It also calls
  `get_security_value("proxy_idle_timeout_minutes")` with no `tenant=` or
  `branch=`, while authentication reads the same setting scoped to the session
  (`services.py:10-15` against `vs_rbac/authentication.py:39-47`). A tenant that
  raised its idle limit to 120 minutes gets its live sessions killed at 30 by
  anyone's list call.
- **Three of the four ways a session ends write no audit row.** Only
  `POST /end/` emits a bookend. Logout, suspension, tenant deactivation and idle
  expiry all use bulk `.update()` (`services.py:19,28,37`;
  `vs_rbac/authentication.py:51-53`), so the audit timeline shows an
  `IMPERSONATION_STARTED` with no matching end and no record of who or what
  closed it.
- **Listing sessions writes rows.** Same shape as the sessions list in
  `user_security_monitoring`: a `GET` that mutates, on a screen built to be
  polled (`views.py:265-270`).
- **Authorisation during a proxy is read off the wrong identity.**
  `get_permissions` picks the namespace from `request.actor_user`
  (`views.py:113-116`), but `HasRBACPermission` evaluates `request.user`, which
  during a live proxy is the **target** (`vs_rbac/permissions.py:164-193`).
  `start` then writes `staff_user=actor` (`views.py:304,357-366`). The safe
  direction is covered by a test - a platform actor proxied as a school user is
  refused, because the school target does not hold the platform key
  (`tests.py:357`) - but the mixed reading is still a subtlety: a school actor
  proxied as a peer is authorised by *the peer's* grant while the resulting
  session is owned by the actor.
- **The table carries no index.** The list filters `tenant` and `status` and
  orders by `-started_at, -pk`, and the sweep scans on `status` +
  `last_activity_at`, all against an unindexed table (`models.py:8-63`).
  `?status=` is also unvalidated free text: `?status=bogus` returns an empty
  page rather than a `400` (`views.py:277-278`).
- **The tenant receiver fires on every save.** `on_tenant_saved` runs an
  `UPDATE` for any `post_save` where the tenant is not `ACTIVE`, whether or not
  the status actually changed and whether or not any session exists
  (`receivers.py:15-19`).
- **`access_log` is serialised raw.** The full JSON trail of every path the
  proxier visited is returned to any holder of the view key
  (`serializers.py:60`). The audience is right - the key is `_CRITICAL` - but it
  is exactly the raw `JSONField` that the ship-check asks to be looked at, and
  it is never masked by FLS.
- **`permissions.py` is dead weight here.** `IsVisionStaff` and
  `StaffReadOnlyOrSuperuserWrite` gate on Django's `is_staff`/`is_superuser`
  flags (`permissions.py:7-37`). Nothing in this slice uses them; only the task
  monitor does, and that carries its own finding
  (see `console_task_monitor` §8).
- **Justified by design:** the `404`s. Out-of-scope tenants, targets and
  sessions all answer "not found" rather than `403`, so no response confirms
  that a tenant, user or session id exists (`views.py:310-314,329-333,426-427`).
- **Justified by design:** the two namespaces are disjoint. A school role
  carrying a stray `platform.impersonation.*` key gains nothing, because the
  actor's home tenant kind - not the key held - selects which namespace is
  checked (`views.py:108-134`), including for the kill switch
  (`views.py:416-419`).
- **Justified by design:** `start` re-asserts the school actor's own-tenant rule
  even though `TenantJWTAuthentication` already blocks a foreign `?tenant=`
  (`views.py:310-314`). The rule then survives independently of the view flag.

## 9. Permissions & tenant isolation

| Surface | Platform actor | School actor | Seeded? |
|---|---|---|---|
| List / retrieve | `platform.impersonation.view` | `school.impersonation.view` | Yes, both `_CRITICAL` |
| Target search | any of `start_all` / `start_cx` / `start_school` | `school.impersonation.start` | Yes |
| Start (school target) | `start_all` or `start_school` | `school.impersonation.start` | Yes |
| Start (platform target) | `start_all` or `start_cx` | not reachable | Yes |
| End (own session) | any start key | `school.impersonation.start` | Yes |
| End (anyone's session) | `platform.impersonation.end` | `school.impersonation.end` | Yes |
| Create / update / delete | **view key only** | **view key only** | see §8 |

The school keys are seeded to the School Admin role
(`core/management/commands/seed_school_permissions.py:107-109`); the platform
keys are seeded as restricted and granted to the platform admin roles
(`core/management/commands/seed_platform_permissions.py:51-64`).

Isolation is enforced at four separate points, which is why it holds under a
forged `?tenant=`:

1. `get_queryset` filters on `request.tenant` (`views.py:272-276`).
2. The target pool is pinned to `actor.tenant_id` for school actors
   (`views.py:225`).
3. `start` re-checks `tenant.pk == actor.tenant_id` for non-platform actors
   (`views.py:310-314`) and pins the target to the asserted tenant
   (`views.py:319-328`).
4. `_load_impersonation` refuses to let a non-platform actor ride a session whose
   tenant is not their own - the single choke point that keeps school
   impersonation intra-tenant whatever `?tenant=` says
   (`vs_rbac/authentication.py:56-64`).

`end` adds a fifth: a school actor's queryset is narrowed to their own tenant
before the pk lookup, so the kill switch cannot reach out by guessing an id
(`views.py:402-407`).

## 10. Code map

| File | Responsibility |
|---|---|
| `views.py` | The viewset, the two-namespace permission matrix, `targets`, `start`, `end`, and the audit bookends |
| `models.py` | `ImpersonationSession` and its (uninvoked) `clean()` invariants |
| `serializers.py` | Session read/write shape, the target picker payload, start/end inputs |
| `services.py` | `sweep_stale_impersonations`, `end_impersonations_for_user`, `end_impersonations_for_tenant` |
| `receivers.py` | Ends sessions when a tenant leaves `ACTIVE` |
| `permissions.py` | `IsVisionStaff` / `StaffReadOnlyOrSuperuserWrite` - used only by the task monitor |
| `vs_rbac/authentication.py` | Header handling, session validation, expiry, and the identity swap |
| `vs_tenants/middleware.py` | `last_activity_at`, the access trail, and the `PROXY_CHANGE` / `PROXY_ACTION_FAILED` fallbacks |
| `vs_audit/models.py` | `impersonation_session` FK and the four action types |

## 11. Test coverage & gaps

`tests.py` is the strongest suite in the module and, unusually, drives the real
JWT layer rather than `force_authenticate` - the auth path is the subject under
test (`tests.py:1-11,83-88`). Six groups:

- `ImpersonationStartTests` (`tests.py:105`) - permission gate, foreign-tenant
  refusal, target/tenant validation, inactive targets, the atomic switch, a
  failed switch leaving the current session alive, and Codex-on-Codex sessions.
- `ImpersonationScopeTests` (`tests.py:226`) - the tiered matrix: no key, only
  `start_cx`, only `start_school`, and `start_all` covering both.
- `ImpersonationTargetSearchTests` (`tests.py:276`) - scope per key, self
  exclusion, the 2-character floor, and cross-field name matching.
- `ImpersonatedRequestTests` (`tests.py:334`) - effective-user substitution,
  **no union with the actor's grants**, forged session ids, actor mismatch,
  expiry flipping the row, a suspended target terminating the session, and the
  audit fallbacks including the access trail.
- `SchoolImpersonationTests` (`tests.py:479`) - the whole school namespace, 20
  cases: own-tenant search, foreign and platform targets refused, riding another
  tenant's session, RBAC evaluated as the target, idle expiry, the view-key
  gate, owner exit, the kill switch and its tenant edge, and the two lifecycle
  terminations.
- `ImpersonationIdleExpiryTests` / `ImpersonationLifecycleTests`
  (`tests.py:772,834`) - idle rejection, the monitoring-list sweep, audit
  bookends, and the kill-switch vs owner-exit split.

Four gaps, in order of what they cost:

1. **Nothing exercises the generic CRUD routes.** There is no test that `POST`,
   `PATCH` or `DELETE` on `/impersonations/` is refused, which is exactly why
   the escalation path in §8 has gone unnoticed.
2. **Nothing pins the sweep's blast radius.** `test_monitoring_list_sweeps_stale_sessions`
   (`tests.py:809`) proves the sweep runs; no test proves it should not touch
   another tenant's rows, or that a tenant-level idle override is honoured.
3. **No test asserts an audit row for the silent endings** - logout, suspension,
   tenant deactivation and idle expiry are all covered for their *effect*
   (`tests.py:903,909,917,794`) but not for their absent audit trail.
4. **The `duration_minutes` path is thin.** `test_start_without_reason_or_duration_is_manual_until_exit`
   (`tests.py:206`) covers the open-ended case; a session with an explicit
   `ends_at` reaching its deadline is not tested, and neither is the 5-240
   bound.
