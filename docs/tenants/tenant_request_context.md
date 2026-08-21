# tenant_request_context

The request-local state every other app scopes against: the five context
variables that carry the current tenant and the dual identity behind an
impersonated request, the middleware that guarantees none of it leaks between
requests, the proxy audit fallback that fires when a proxied request produced no
event of its own, and the one query-parameter resolver this app still ships.

The tenant model is `tenant_identity_lifecycle`; sites are
`tenant_sites_branches`; reference resolution and numbering are
`tenant_references_numbering`.

**This app publishes no HTTP routes.** What it publishes here is one middleware
class, installed at `apps/settings/base.py:146`, and a module of module-level
functions that `vs_rbac`, `vs_audit` and `vs_user` import.

Findings for the whole module are collected in
**`error/tenants/tenant_code_issues.md`**; §8 below points at the ones belonging
here rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **The tenant context is a `ContextVar`, not thread-local storage.**
  `contextvars` follow `async` tasks correctly and are per-context rather than
  per-thread, which is what makes them safe under ASGI (`context.py:1-10`).
- **Nothing in this app *sets* the tenant.** `set_current_tenant` is called by
  exactly one production caller: `vs_rbac.authentication.TenantJWTAuthentication`
  (`vs_rbac/authentication.py:146`). This app owns the storage and the cleanup;
  `vs_rbac` owns the decision.
- **A request carries two identities, not one.** Under impersonation the *actor*
  is the CX engineer holding the token and the *effective user* is the person
  they are standing in for. Both are stored, plus the session that links them
  (`context.py:36-49`).
- **`clear_current_tenant()` clears the identity too.** It predates dual-identity
  context and was already the cleanup hook used by authentication tests and
  request boundaries, so clearing both prevents a proxy identity surviving after
  its tenant scope is removed (`context.py:25-33`).
- **The middleware clears twice: before and after.** Before, so a context left
  behind by anything upstream cannot be inherited; after, in a `finally`, so a
  view raising cannot leak one forward (`middleware.py:104-177`).
- **A feature-level audit event always beats a request-level one.** The
  middleware's fallback fires only when a proxied request produced **zero**
  audit events of its own, which is what `mark_audit_event_emitted` /
  `get_current_audit_event_count` are for (`middleware.py:111-115`).
- **Successful proxied *reads* are not audit events.** They land in the
  session's own access trail instead, deduped by path so browsing stays readable
  (`middleware.py:28-31`). Writes and failures do reach the audit stream.
- **Three notification paths are excluded from the write fallback by name.**
  Marking a notification read is automatic UI bookkeeping, not a business change
  (`middleware.py:12-19`). Failed calls on those paths are still audited.
- **The fallback's module key follows the *initiating surface*, not the target.**
  A school-initiated session writes `SCHOOL` rows; without that, only
  `platform.audit.view` holders could ever read them
  (`middleware.py:122-127`).
- **`resolve_tenant` is not what authenticates a request.** It is a
  query-parameter resolver that predates
  `TenantJWTAuthentication`'s own assertion handling, and it has no production
  caller left (`tenant_code_issues` §3).
- **Bookkeeping never breaks a response.** `_record_proxy_activity` swallows
  every exception, by design and with the reason stated
  (`middleware.py:28-31`, `53-54`).

## 2. Domain model

No tables. Five module-level `ContextVar`s (`context.py:6-10`):

| Variable | Holds | Set by |
|---|---|---|
| `_current_tenant` | The `Tenant` being operated on | `vs_rbac.authentication` |
| `_current_audit_actor` | The `User` holding the token | `set_current_audit_identity` |
| `_current_effective_user` | The impersonated target, or the actor | `set_current_audit_identity` |
| `_current_impersonation_session` | The `ImpersonationSession`, or `None` | `set_current_audit_identity` |
| `_current_audit_event_count` | How many feature-level events this request emitted | `mark_audit_event_emitted` |

All five default to `None` (the counter to `0`), so an unauthenticated or
non-HTTP context reads as "no tenant, no identity, nothing emitted".

### The public surface (`context.py`)

| Function | Contract |
|---|---|
| `get_current_tenant()` | The tenant, or `None`. Read by `TenantAwareManager.get_queryset` on every ORM call |
| `set_current_tenant(tenant)` | Returns the token, so a caller *could* reset it; nobody does |
| `reset_current_tenant(token)` | Restores a previous value. No production caller |
| `clear_current_tenant()` | Clears the tenant **and** all four identity vars |
| `set_current_audit_identity(*, actor_user, effective_user, impersonation_session=None)` | Stores the triple |
| `get_current_audit_identity()` | Returns `(actor, effective, session)` |
| `mark_audit_event_emitted()` | Increments the counter |
| `get_current_audit_event_count()` | Reads it |
| `resolve_audit_identity(actor_user, effective_user=None, impersonation_session=None)` | See §5 |
| `add_proxy_audit_metadata(metadata, effective_user, session)` | Copies and appends proxy attribution |
| `clear_current_audit_identity()` | Clears the four identity vars |
| `clear_request_context()` | Calls `clear_current_tenant()` - the middleware's entry point |
| `tenant_context_block(tenant)` | `{"slug", "name", "kind"}` or `{}` |

`tenant_context_block` exists in one place because two callers must agree: the
login response and `/user/auth/me/`. The console treats a fresh login as
equivalent to a `/me` sync and skips the round trip, so any field present in one
and missing from the other is silently absent for a whole session
(`context.py:108-114`).

### Middleware constants (`middleware.py:10-22`)

| Constant | Value |
|---|---|
| `SAFE_METHODS` | `{"GET", "HEAD", "OPTIONS"}` |
| `NON_BUSINESS_PROXY_WRITE_PATHS` | `/v1/notify/mark-read/`, `/v1/notify/mark-all-read/`, `/v1/notify/acknowledge-route/` |
| `ACCESS_LOG_MAX_PATHS` | `200` - distinct paths kept per session; existing entries keep counting past the cap |

## 3. Endpoint map

**None.** The middleware sits in the stack at position 7 of 9
(`apps/settings/base.py:137-154`):

```
corsheaders.CorsMiddleware
django.middleware.security.SecurityMiddleware
django.contrib.sessions.SessionMiddleware
django.middleware.common.CommonMiddleware
django.middleware.csrf.CsrfViewMiddleware
django.contrib.auth.AuthenticationMiddleware
vs_tenants.middleware.TenantContextCleanupMiddleware        ← here
vs_health.middleware.RequestMetricsMiddleware
django.middleware.clickjacking.XFrameOptionsMiddleware
```

Its position matters in one direction only: `vs_health`'s metrics middleware is
placed *after* it deliberately, so the tenant dimension is available by the time
metrics are recorded (`apps/settings/base.py:147-150`).

It is placed after `AuthenticationMiddleware`, but that is incidental - DRF
resolves the JWT lazily inside the view's `initial()`, not in middleware, so the
tenant is set well after this middleware's pre-phase has run and well before its
post-phase.

`resolve_tenant(request)` (`resolution.py:8-19`) is importable and imported by
nothing outside this app's own tests.

## 4. Lifecycle / state machine

One request, end to end:

```
TenantContextCleanupMiddleware.__call__
  │
  ├─ clear_request_context()          ← nothing inherited from a prior request
  │
  ├─ get_response(request)
  │     └─ DRF view.initial()
  │           └─ TenantJWTAuthentication.authenticate()
  │                 ├─ request.actor_user / effective_user /
  │                 │  impersonation_session / tenant / rbac_tenant
  │                 ├─ set_current_tenant(tenant)        ← arms TenantAwareManager
  │                 └─ set_current_audit_identity(...)   ← arms proxy attribution
  │           └─ permissions → view body → serializers
  │                 └─ any emit_audit_event() also calls mark_audit_event_emitted()
  │
  ├─ session = request.impersonation_session
  │     ├─ not None → _record_proxy_activity(session, request, response)
  │     │              last_activity_at always; access_log for successful reads
  │     │
  │     └─ not None AND get_current_audit_event_count() == 0 → the fallback:
  │            status >= 400            → PROXY_ACTION_FAILED  (WARNING, DENIED/FAILED)
  │            unsafe method, not in    → PROXY_CHANGE
  │              NON_BUSINESS_PROXY_WRITE_PATHS
  │            otherwise (a safe read)  → nothing; the access trail has it
  │
  └─ finally: clear_request_context()  ← nothing leaks forward, even on an exception
```

The counter is the whole state machine: a request that emitted a real event
suppresses the fallback, and a request that emitted none gets one only if it was
proxied.

## 5. Derivations

### Which identity an audit row is attributed to

```python
def resolve_audit_identity(actor_user, effective_user=None, impersonation_session=None):
    request_actor, request_effective, request_session = get_current_audit_identity()
    if request_session is None:
        return actor_user, effective_user, impersonation_session
    if actor_user is None or not (
        _same_user(actor_user, request_actor) or _same_user(actor_user, request_effective)
    ):
        return actor_user, effective_user, impersonation_session
    return request_actor, request_effective, request_session
```

`context.py:67-82`. Read as a table:

| Situation | Result |
|---|---|
| No proxy session on this request | The caller's own arguments, untouched |
| A proxy session, and `actor_user` is `None` (a system event) | Untouched - system events stay system events |
| A proxy session, and `actor_user` is somebody unrelated | Untouched - an explicitly third-party-attributed event is not rewritten |
| A proxy session, and `actor_user` matches **either** request identity | Rewritten to the request's `(actor, effective, session)` triple |

The fourth row is the point: a service that writes `actor_user=request.user`
during an impersonated request is passing the *effective* user, and the durable
row must name the real engineer. Matching on either identity is what catches both
spellings.

`_same_user` compares primary keys and treats a `None` pk as no match
(`context.py:61-64`), so two unsaved instances are never considered the same
person.

### Proxy metadata

```python
resolved = dict(metadata or {})
if impersonation_session is not None:
    resolved.update({
        "impersonation_session_id": impersonation_session.pk,
        "effective_user_id": getattr(effective_user, "pk", None),
    })
return resolved
```

`context.py:85-93`. It **copies** rather than mutating, so a caller's dict is not
altered underneath it - which matters because `vs_rbac.audit.record_rbac_audit`
passes the same metadata to two sinks (`vs_rbac/audit.py:44`).

### The access trail

```python
now = timezone.now()
session.last_activity_at = now
update_fields = ["last_activity_at"]
if request.method in SAFE_METHODS and response.status_code < 400:
    log = list(session.access_log or [])
    entry = next((e for e in log if e.get("path") == request.path), None)
    if entry is not None:
        entry["count"] += 1
        entry["last_at"] = now.isoformat()
        update_fields.append("access_log")
    elif len(log) < ACCESS_LOG_MAX_PATHS:
        log.append({"path": ..., "count": 1, "first_at": ..., "last_at": ...})
        update_fields.append("access_log")
    session.access_log = log
session.save(update_fields=update_fields)
```

`middleware.py:25-54`. Three properties:

- **`last_activity_at` is written on every proxied request**, safe or not,
  successful or not. That is what keeps an open-ended session alive against the
  idle timeout `vs_rbac.authentication` enforces
  (`vs_rbac/authentication.py:39-55`).
- **Only successful reads enter the trail**, because writes and failures already
  land in the audit stream and would otherwise be recorded twice.
- **The path list is deduped and capped at 200 distinct paths.** Existing entries
  keep counting past the cap; a 201st distinct path is silently dropped
  (`tenant_code_issues` §12).

The whole function is wrapped in `try/except Exception: pass` - bookkeeping must
not break the proxied response.

### The readable operation name

```python
verb = {"POST": "submitted", "PUT": "updated", "PATCH": "updated",
        "DELETE": "deleted"}.get(request.method, "changed")
raw_name = getattr(request.resolver_match, "url_name", "") or ""
parts = re.split(r"[-_]", raw_name) if raw_name else request.path.strip("/").split("/")
# ... drop a leading v<digits>, then drop noise words, digits and long hex ids
resource = " ".join(words) or "record"
return f"{verb} {resource}"
```

`middleware.py:68-95`. It prefers the resolved URL name and falls back to the
path. The words it drops are `list`, `detail`, `create`, `update`, `delete`,
`destroy`, anything all-digits, and anything matching `[0-9a-fA-F-]{16,}` (a
UUID or long hex id). So `PATCH` on `user-account-detail` reads
*"updated user account"*, and an unnamed route
`/v1/finance/invoices/4f2c…/void/` reads *"submitted finance invoices void"*.

It is a heuristic producing an audit `summary`, not an identifier, and it is
labelled as such.

### `resolve_tenant`

```python
slug = (request.query_params.get("tenant") or "").strip().lower()
if not slug:
    raise ValidationError({"tenant": "A 'tenant' query parameter is required."})
tenant = Tenant.objects.filter(slug=slug, status=Tenant.Status.ACTIVE).first()
effective_user = getattr(request, "effective_user", None) or request.user
if tenant is None or getattr(effective_user, "tenant_id", None) != tenant.pk:
    raise NotFound("No tenant matches the requested context.")
request.tenant = tenant
return tenant
```

`resolution.py:8-19`. Non-enumerating - an unknown slug, an inactive tenant and
somebody else's tenant all produce the same 404.

It differs from `TenantJWTAuthentication` in two ways that matter, and it has no
production caller, so the divergence is latent rather than live
(`tenant_code_issues` §3):

| | `resolve_tenant` | `TenantJWTAuthentication` |
|---|---|---|
| Statuses admitted | `ACTIVE` only | `AUTHENTICABLE_STATUSES` = `ACTIVE` **and** `PENDING` |
| Cross-tenant | Never - the effective user must own the tenant | A `PLATFORM` actor may assert another tenant on a view declaring `platform_cross_tenant_param` |
| Sets | `request.tenant` only | `actor_user`, `effective_user`, `impersonation_session`, `tenant`, `rbac_tenant`, plus both contextvars |

## 6. What writing writes

The middleware is the only writer in this slice, and it writes two kinds of row.

| Trigger | Row |
|---|---|
| Any proxied request | `ImpersonationSession.last_activity_at`, and `access_log` for a successful safe read |
| A proxied request that emitted no feature event and returned ≥ 400 | One `AuditEvent`: `PROXY_ACTION_FAILED`, severity `WARNING`, status `DENIED` for 401/403 and `FAILED` otherwise |
| A proxied request that emitted no feature event, used an unsafe method, and is not a notification bookkeeping path | One `AuditEvent`: `PROXY_CHANGE` |

Both fallback events carry `entity_type="ImpersonationSession"`,
`entity_id=str(session.pk)`, `entity_label` = the target's label,
`actor_user` = the real engineer, `effective_user` = the target,
`tenant` = `request.tenant`, and metadata
`{"method", "path", "status_code", "fallback_event": True}` - plus
`change_description` on the `PROXY_CHANGE` variant.

`_user_label` prefers `full_name`, then `get_full_name()`, then `email`, then
the literal `"Unknown user"` (`middleware.py:57-65`).

Everything else this slice does is in-memory contextvar writes, which persist
nothing.

## 7. Worked example

A CX engineer, Bola, proxies into Corona Secondary as their bursar, Chidi, to
reproduce a reported bug.

**1. The session starts.** `vs_admin_console` creates an `ImpersonationSession`
with `staff_user=Bola`, `target_user=Chidi`, `tenant=corona`, and its own audit
bookends. Nothing in this slice is involved yet.

**2. Bola opens the invoice list.**
`GET /v1/finance/invoices/?tenant=corona`, with the session id in the
`X-Impersonation-Session` header.

The middleware clears the context, then hands off. `TenantJWTAuthentication`
validates the session, resolves Chidi as the effective user, stamps five
attributes on the request, calls `set_current_tenant(corona)` - which arms every
`TenantAwareManager` in the repo for this request - and
`set_current_audit_identity(actor_user=Bola, effective_user=Chidi,
impersonation_session=session)`.

The view runs. It is a read; it emits no audit event, so the counter stays at 0.

**3. The middleware's post-phase.** `_record_proxy_activity` stamps
`last_activity_at` and appends `{"path": "/v1/finance/invoices/", "count": 1, …}`
to the session's access trail. Then the fallback check: the counter is 0, but
this was a `GET` that returned 200, so **no audit event is written**. The trail
records what Bola looked at; the audit stream is reserved for changes and
failures.

**4. Bola opens the same list twice more.** The trail entry's `count` becomes 3
and `last_at` moves. No new entries, no new audit rows.

**5. Bola tries something Chidi cannot do.**
`POST /v1/finance/payments/.../approve/` returns 403 because Chidi lacks the key.
The view emitted no audit event. The fallback fires: `PROXY_ACTION_FAILED`,
`WARNING`, status `DENIED`, summary *"Bola Adewale's action was blocked while
proxied as Chidi Obi"*, with the method, path and status code in metadata. Bola
initiated from the platform console, so `is_platform_actor(Bola)` is true and the
row is filed under `PLATFORM`.

**6. Bola fixes the data.** `PATCH /v1/finance/invoices/91/` succeeds, and the
finance serializer emits its own `INVOICE/UPDATE` audit event - calling
`mark_audit_event_emitted()` as it does. The counter is now 1, so the middleware
writes **no** `PROXY_CHANGE` row: a feature-level event is always more useful
than a request-level one.

That finance event is attributed correctly because `emit_audit_event` runs
`resolve_audit_identity` first. The serializer passed `actor_user=request.user`,
which is *Chidi*; `resolve_audit_identity` sees a live proxy session, matches
Chidi against the request's effective user, and rewrites the triple so the
durable row names **Bola** as the actor with Chidi as the effective user and the
session id in metadata.

**7. Bola marks a notification read.** `POST /v1/notify/mark-read/` succeeds and
emits nothing. It is an unsafe method and the counter is 0, so the fallback
*would* fire - except the path is in `NON_BUSINESS_PROXY_WRITE_PATHS`. Nothing is
written. Had it failed, the ≥ 400 branch would have caught it, because that
branch is checked first and does not consult the exclusion list.

**8. The request ends.** The `finally` clears every contextvar. If step 6 had
raised instead of returning, the `finally` would still have cleared them - which
is the whole reason the cleanup is in a `finally` and not after the return.

**9. What the next request cannot see.** A completely different user, on a
different tenant, served by the same worker process, starts with
`get_current_tenant() is None`. `TenantAwareManager` therefore filters nothing
until authentication sets a tenant - which is the correct default for a CX
request and for a Celery task, and the reason the cleanup exists.

## 8. Gotchas / known limitations

Recorded in full in **`error/tenants/tenant_code_issues.md`**. The items
belonging to this slice:

| # in that file | One line |
|---|---|
| §3 | **Confirmed by execution.** `resolve_tenant` is unreferenced dead code that admits fewer statuses than the auth layer it duplicates |
| §12 | The proxy access trail silently stops recording new paths after 200 distinct ones |
| §13 | The middleware imports `vs_admin_console` and `vs_audit` from inside the foundation app's request path |

Design choices worth stating as choices:

- **`ContextVar` over thread-local** (`context.py:3-10`), so the context follows
  async tasks.
- **Clearing before *and* after** (`middleware.py:105`, `176-177`), so neither an
  inherited context nor an exception can leak one.
- **The audit counter** (`context.py:52-58`, `middleware.py:111-115`): a
  feature-level event is always more useful than a request-level fallback, so
  the fallback must be able to tell whether one happened.
- **Successful reads in the trail, not the stream** (`middleware.py:28-31`),
  which keeps the audit stream about changes while still recording what a
  proxier saw.
- **The module key follows the initiating surface** (`middleware.py:122-127`) -
  without it a school-initiated session would write PLATFORM rows only
  `platform.audit.view` holders could read.
- **Matching either identity in `resolve_audit_identity`**
  (`context.py:77-81`), because a service under impersonation may pass either
  spelling of "the current user".
- **`add_proxy_audit_metadata` copies rather than mutates**
  (`context.py:87`), because the same dict reaches two sinks.
- **Bookkeeping swallows its own failures** (`middleware.py:53-54`), stated in
  the docstring rather than left as a bare `except`.

## 9. Permissions & tenant isolation

- **This slice is the mechanism, not a policy.** It enforces nothing; it carries
  the value that `vs_rbac.managers.TenantAwareManager` filters on and that
  `vs_audit` attributes rows with.
- **The default is unscoped, deliberately.** `get_current_tenant()` returns
  `None` when nothing set it, and `TenantAwareManager` then filters nothing
  (`vs_rbac/managers.py:105-107`). That is correct for CX requests and for Celery
  tasks, and it is why every cross-tenant leak recorded against
  `vs_admin_console`, `vs_audit` and `vs_exports` is a variation on "the manager
  was assumed to be engaging and it was not".
- **The cleanup is the isolation guarantee.** Without the `finally`, a worker
  reused across requests could serve request B under request A's tenant. The
  middleware's docstring says exactly this: *"Guarantee that request-local tenant
  state cannot leak between requests"* (`middleware.py:98-99`).
- **The dual identity is an accountability control.** `resolve_audit_identity`
  is what stops an impersonated action being recorded as the victim's own; it is
  the reason `vs_rbac.audit.record_rbac_audit` and `vs_audit.emit_audit_event`
  both call it before writing.
- **`request.tenant` and `request.rbac_tenant` are different on purpose**, and
  the difference is set by `vs_rbac`, not here: `tenant` is the tenant being
  operated on, `rbac_tenant` is the actor's own unless impersonating
  (`vs_rbac/authentication.py:144-145`).
- **`resolve_tenant` would be a *narrower* boundary than the live one** - ACTIVE
  only, no cross-tenant - so its divergence is a correctness bug for pending
  tenants rather than a security hole. It is still dead code
  (`tenant_code_issues` §3).

## 10. Code map

| File | What lives there |
|---|---|
| `vs_tenants/context.py:6-10` | The five `ContextVar`s |
| `vs_tenants/context.py:13-33` | Tenant get/set/reset/clear |
| `vs_tenants/context.py:36-58` | Dual identity and the audit-event counter |
| `vs_tenants/context.py:61-82` | `_same_user`, `resolve_audit_identity` |
| `vs_tenants/context.py:85-100` | `add_proxy_audit_metadata`, `clear_current_audit_identity` |
| `vs_tenants/context.py:103-118` | `clear_request_context`, `tenant_context_block` |
| `vs_tenants/middleware.py:10-22` | The three constants |
| `vs_tenants/middleware.py:25-54` | `_record_proxy_activity` |
| `vs_tenants/middleware.py:57-95` | `_user_label`, `_proxy_change_description` |
| `vs_tenants/middleware.py:98-177` | `TenantContextCleanupMiddleware` |
| `vs_tenants/resolution.py` | `resolve_tenant` (unreferenced) |
| `apps/settings/base.py:137-154` | The middleware stack and its ordering comment |
| `vs_rbac/authentication.py:140-152` | The one production caller of `set_current_tenant` and `set_current_audit_identity` |
| `vs_rbac/managers.py:103-119` | The one consumer of `get_current_tenant` |
| `vs_rbac/audit.py:41-46` | `resolve_audit_identity` + `add_proxy_audit_metadata` in the durable path |
| `vs_admin_console/views.py` | `is_platform_actor`, imported by the middleware at request time |

## 11. Test coverage & gaps

Module baseline: **`Ran 62 tests in 4.805s` - OK**.

Covered for this slice:

- `tests.py::ProxyAuditMiddlewareTests` (roughly 100 lines) - the fallback for a
  failed proxied request, the `PROXY_CHANGE` fallback for a write, the
  suppression when a feature event already fired, and the notification-path
  exclusion.
- `tests.py::TenantFoundationTests` - `resolve_tenant`'s three behaviours
  (missing parameter, foreign slug, matching slug).

Not covered:

- **No test asserts the access trail's 200-path cap** (§12), or what happens at
  the 201st path.
- No test calls `resolve_tenant` with a PENDING tenant (§3) - which is how the
  divergence from the auth layer went unnoticed.
- `resolve_audit_identity` has no direct unit test; it is exercised only through
  `vs_rbac` and `vs_audit`, so the "unrelated actor is not rewritten" branch has
  no coverage.
- `_proxy_change_description` has no test of its own, so the hex-id and
  version-prefix stripping rules are unverified.
- No test asserts that a raised exception inside a view still clears the context
  - the `finally` is untested.
- `tenant_context_block` has no test, despite existing precisely because two
  callers must agree.
