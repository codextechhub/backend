# user_authentication

Signing in and staying signed in: **credential checking, brute-force lockout, JWT
issuance and rotation, the `LoginSession` record behind every device, and the
"who am I" payload the client rebuilds its state from**. Routes are mounted at
`/v1/user/auth/`.

---

## 1. What it is (and what it is NOT)

- `LoginService.login()` is the only credential path in the product. It checks the
  password itself rather than calling Django's `authenticate()`, because
  `authenticate()` returns `None` for `is_active=False` users and would hide the
  real reason (`services/auth.py:59-64`).
- `CodeXRefreshToken` is the token class. Every token carries `tenant_id`,
  `tenant_slug`, `branch_id`, `account_status` and `full_name` so the client can
  route to the right workspace without another call (`tokens.py:27-54`).
- `LoginSession` is an application-level device record keyed to the refresh
  token's JTI, which is what makes "sign this one device out" possible
  (`models.py:434-486`).
- `AccountLockout` holds one row per user: the failure counter and the lock
  expiry (`models.py:543-576`).

**This is NOT the authorization layer.** The token asserts identity and home
tenant; what the caller may do is decided by `vs_rbac` on every request, and the
`?tenant=` query parameter is a separate, mandatory assertion checked in
`TenantJWTAuthentication` (`vs_rbac/authentication.py:95-131`).

**Password setting is not here either** - see `user_passwords` for change/reset
and `user_invitations_activation` for first-time activation.

## 2. Domain model

| Model | Key fields | Rules |
|---|---|---|
| `LoginSession` | `user`, `tenant`, `ip_address`, `user_agent`, `device_label`, `last_seen_at`, `refresh_jti`, `is_active`, `ended_at`, `end_reason` | Tenant-aware default manager plus an unscoped `all_objects`; indexed on `(user, is_active)` and `(is_active, last_seen_at)` (`models.py:434-486`) |
| `AuthAttempt` | `email_entered`, `user?`, `tenant?`, `ip_address`, `user_agent`, `result`, `failure_code`, `metadata` | `user` is null when the email is unknown, deliberately, so a probe does not confirm existence; indexed on `email_entered`, `created_at`, `(user, result)` (`models.py:492-534`) |
| `AccountLockout` | `locked_until`, `locked_reason`, `failure_count`, `last_failure_at`, `last_failure_ip` | One row per user (`OneToOneField`), created on first failure (`models.py:543-576`) |

`end_reason` is a free string written by the callers, not a choices field. The
values in use are `LOGOUT`, `FORCE_LOGOUT`, `EXPIRED`, `SUSPENDED` and
`EMAIL_CHANGE` (`views/auth.py:187-193`; `services/audit.py:167-171`;
`services/user.py:243-245,277-279`).

## 3. Endpoint map

| Method + path | permission | request body / query | response |
|---|---|---|---|
| `POST /auth/login/` | `AllowAny`, throttle `login` 5/min | `email`, `password`, optional `tenant` | `{access, refresh, session_id, user, tenant, school, permissions}` (`views/auth.py:43-84`) |
| `GET /auth/special_login/preview/` | `AllowAny`, throttle `login_preview` 10/min | Query `email` | CX staff only (PLATFORM tenant): `200 {full_name}` / `403` status message / `404` unknown **or non-CX** / `400` missing param (`views/auth.py`) |
| `POST /auth/logout/` | `IsAuthenticated`, no `?tenant=` | `refresh` | `200` always, even for an already-blacklisted token (`views/auth.py:148-205`) |
| `POST /auth/token/refresh/` | `AllowAny`, no `?tenant=` | `refresh` | `{access, refresh?}`; `401` with `TOKEN_EXPIRED` / `TOKEN_REVOKED` / `TOKEN_INVALID` (`views/auth.py:208-308`) |
| `GET /auth/me/` | `IsAuthenticatedAndActive`, no `?tenant=` | - | `{user, tenant, school, permissions}` (`views/me.py:32-62`) |
| `GET /auth/me/stats/` | `IsAuthenticatedAndActive`, no `?tenant=` | - | `{failed_attempts_7d}` (`views/me.py:65-85`) |

`tenant_param_required = False` on logout, refresh, `me` and `me/stats` is what
lets a client call them with only a Bearer header; every other authenticated
endpoint in the module raises `{"tenant": "A 'tenant' query parameter is
required."}` without it (`vs_rbac/authentication.py:122-126`).

The login payload's `permissions` array is the caller's full effective permission
set for their home tenant, and `/auth/me/` returns the same array so the client
can refresh its cache after a token refresh (`services/auth.py:134-145`;
`views/me.py:46-62`).

## 4. Lifecycle / state machine

```text
POST /auth/login/
  0. resolve the asserted `tenant` slug (ACTIVE or PENDING) if one was sent
  1. find user by email (case-insensitive), scoped to that tenant when one was asserted
     ── no match ─▶ AuthAttempt(FAIL, TENANT_MISMATCH) ─▶ INVALID_CREDENTIALS
  2. tenant-kind gate: a non-PLATFORM tenant with no school profile → INVALID_CREDENTIALS
  3. check_password  ── fail ─▶ register_failure ─(count ≥ threshold)─▶ user.status = LOCKED
  4. lockout check (only AFTER a correct password)  ── locked ─▶ 403 ACCOUNT_LOCKED
  5. status gate: PENDING / LOCKED / SUSPENDED / DEACTIVATED ─▶ 403
  6. [atomic] clear lockout · issue tokens · create LoginSession · stamp last_login_at
  7. record AuthAttempt(SUCCESS) + LOGIN_SUCCESS audit event

Session:  active ──logout (this JTI)──────▶ ended  LOGOUT
                 ├─force logout / suspend ─▶ ended  FORCE_LOGOUT / SUSPENDED
                 ├─email change ───────────▶ ended  EMAIL_CHANGE
                 └─refresh token expired ──▶ ended  EXPIRED  (swept lazily on list)
```

The `tenant` body key is a slug the frontend reads off the subdomain it is
served from - a school's page at `bright-star.xvs.codexng.com` sends
`bright-star` - and is not the `?tenant=` query assertion the authenticated
endpoints take, because there is no token yet to check one against.

It is optional. Two frontends call this endpoint and neither sends it yet, so a
request that omits it behaves exactly as before: the tenant is derived from the
row found by email, which is correct only while `User.email` is unique across the
platform. A request that sends it is scoped and checked, and an account that
belongs to a different tenant is refused with the wrong-password message and no
hint of where it really lives - the audit row names only the tenant the caller
asserted, with no user FK. The single switch that makes `tenant` mandatory is
`REQUIRE_TENANT_ON_SIGN_IN` in `services/sign_in_scope.py`; the same resolver
scopes `PasswordService.request_reset`.

The ordering of steps 3 and 4 is the security-relevant part: the lock is only
revealed to a caller who has already proved they know the password, so the
endpoint is not an account-state oracle (`services/auth.py:59-80`).

`LoginService.login` is deliberately **not** wrapped in a single transaction: the
audit writes have to survive the `ValueError` that a failed login raises. Only the
success path (lockout clear, token, session, `last_login_at`) is atomic
(`services/auth.py:21-40,95-118`).

Refresh rotation is on (`ROTATE_REFRESH_TOKENS` and `BLACKLIST_AFTER_ROTATION`,
`apps/settings/base.py:61-67`). After a rotation the view registers the new JTI in
`OutstandingToken` so a later force-logout can reach it, and moves the owning
`LoginSession` onto the new JTI, leaving other devices' sessions alone
(`views/auth.py:266-308`).

## 5. Derivations

- **Lock threshold and duration** come from the live security settings resolved
  for the user's tenant and branch, defaulting to 5 failures and 15 minutes
  (`services/auth.py:183-199`; `vs_config/runtime_settings.py:26-30`). Tenants may
  narrow but not widen them: `failed_login_threshold` is capped between 3 and 20
  and `account_lock_minutes` between 5 and 1440
  (`vs_config/runtime_settings.py:53-57`).
- **`locked_until` = now + `account_lock_minutes`** once `failure_count` reaches
  the threshold; the counter is never reset by time, only by a successful login,
  an admin unlock, or a completed reset (`models.py:559-572`;
  `services/auth.py:96-100`).
- **`device_label`** is built from the user agent plus Client Hints. Modern
  Android Chrome reports the model as `K`, so the parser falls back to
  `Sec-CH-UA-Model`, then to the platform hint, then to the OS family, rather than
  printing something misleadingly wrong. The final shape is
  `Browser: X · OS: Y · Class: z` (`services/audit.py:195-262`).
- **`ip_address`** prefers the first entry of `X-Forwarded-For` and falls back to
  `REMOTE_ADDR` (`services/audit.py:174-184`).
- **`failed_attempts_7d`** counts this user's non-`SUCCESS` `AuthAttempt` rows in
  the last seven days (`views/me.py:76-85`).
- **Token lifetimes**: access 15 minutes, refresh 1 day
  (`apps/settings/base.py:61-64`).

## 6. What posting does to the ledger

Nothing here posts. Each sign-in leaves four durable traces: an `AuthAttempt` row
(success or failure), a `LoginSession` row, an `AuditEvent` in the `IDENTITY`
module, and - on the failure path - an updated `AccountLockout` counter. All four
are written outside the success transaction precisely so a rejected login still
leaves evidence (`services/auth.py:120-132,172-218`).

Logging out is a two-part revocation and both parts matter: the refresh token is
added to SimpleJWT's blacklist **and** the `LoginSession` carrying that JTI is
closed. Blacklisting alone would leave a live session row; closing the row alone
would leave a working refresh token (`views/auth.py:182-193`). Logout also ends
any impersonation session riding on that user (`views/auth.py:194-195`).

## 7. Worked example

```json
POST /v1/user/auth/login/
{ "email": "ada@codexng.com", "password": "correct horse battery staple 9!" }
```

```json
{
  "success": true,
  "message": "Login successful.",
  "data": {
    "access": "…", "refresh": "…", "session_id": 4213,
    "user": { "id": 42, "uid": 11, "email": "ada@codexng.com", "status": "ACTIVE", … },
    "tenant": { "slug": "codex", "name": "CodeX" },
    "school": null,
    "permissions": ["platform.team.create", "platform.team.view", …]
  }
}
```

`school` is null for a platform user; for a school user it is
`{id, name, slug, logo}` with an absolute logo URL, and every level of that
lookup is null-safe (`serializers.py:39-68`). The fifth consecutive wrong password
on this account instead returns `403 ACCOUNT_LOCKED`, flips `user.status` to
`LOCKED`, and writes an `ACCOUNT_LOCKED` audit event (`services/auth.py:189-209`).

## 8. Gotchas / known limitations

- **A tenant without a school profile cannot log in at all, and the message says
  "Invalid credentials".** Step 2 rejects any non-PLATFORM tenant whose
  `school_profile` is missing before the password is even checked, recording
  `SCHOOL_CONTEXT_REQUIRED` in `AuthAttempt` but returning the generic error
  (`services/auth.py:47-57`). The failure code is only visible to someone reading
  the attempts table, and the admin lists that would show it are themselves gated
  by an unseeded permission (see `user_security_monitoring` §8).
- **Lockout is per account, never per IP.** One attacker can spray a single
  password across a thousand accounts and never trip a lock; the only brake is the
  5/minute `login` throttle, which `ScopedRateThrottle` keys on the client IP
  (`services/auth.py:172-209`; `apps/settings/base.py:44-48`).
- **The barcode preview endpoint confirms account existence.** `200` with a real
  name, `403` with a status-specific message, and `404` for an unknown email are
  three distinguishable answers to an unauthenticated caller
  (`views/auth.py:123-145`). It is a deliberate trade for the ID-card login flow
  and is throttled to 10/minute, but it undoes the enumeration protection that
  `POST /auth/password/reset/request/` goes to some trouble to provide
  (`views/passwords.py:107-118`). Worth an explicit product decision rather than a
  code comment.
- **`LOCKED` accounts keep `is_active` unchanged.** `_sync_is_active` intentionally
  skips `LOCKED`, so the block is enforced by `LoginService._check_status` and by
  `IsAuthenticatedAndActive`, not by Django's auth flag (`models.py:299-306`;
  `vs_rbac/permissions.py:83-90`). Any code path that trusts `is_active` alone
  will treat a locked account as usable.
- **Rotation bookkeeping is best-effort.** If the `OutstandingToken` write or the
  session JTI sync fails, the exception is swallowed and the new tokens are still
  returned; a later force-logout will then miss that token
  (`views/auth.py:302-306`).
- **`session_id` is an incrementing integer returned to the client.** It is only
  used as an input to `end-other-mine`, which checks ownership before acting
  (`views/security.py:172-217`), so the exposure is enumeration of session
  counts rather than of other people's sessions.
- **Justified by design:** the unknown-email failure records the entered email
  with `user=None`. For a spraying campaign that string is the only identifying
  datum the security team has (`models.py:499-505`; `services/auth.py:211-218`).

## 9. Permissions & tenant isolation

Login, the barcode preview and token refresh are public by necessity;
`authentication_classes = []` on login, preview and activation keeps a stale
Bearer header from turning a public call into a tenant assertion failure
(`views/auth.py:55-56,112-114,325-326`). Logout requires only
`IsAuthenticated` (a suspended user must still be able to end their own session),
while `/auth/me/` and `/auth/me/stats/` require `IsAuthenticatedAndActive`, which
raises 403 with a specific message for suspended, locked and deactivated accounts
(`vs_rbac/permissions.py:78-91`).

Logout is scoped to the submitted token's own session and first checks that the
token belongs to the calling user (`views/auth.py:170-193`). Cross-device
revocation is a separate, permissioned operation in `user_security_monitoring`.

`LoginSession.objects` is tenant-aware, so an ordinary listing can never reach
another tenant's sessions; the places that must cross that line (suspend, email
change, admin force-logout) use `all_objects` explicitly and say why in a comment
(`models.py:467-472`; `services/user.py:243-245`).

## 10. Code map

| File | Responsibility |
|---|---|
| `services/auth.py` | `LoginService`: credential check, lockout, status gate, session + token issue |
| `views/auth.py` | Login, barcode preview, logout, token refresh |
| `views/me.py` | `/auth/me/`, `/auth/me/stats/`, and the shared `_get_date_param` helper |
| `tokens.py` | `CodeXRefreshToken` claims and the SimpleJWT obtain serializer/view |
| `models.py` | `LoginSession`, `AuthAttempt`, `AccountLockout` |
| `services/audit.py` | `record_attempt`, `blacklist_*`, `expire_stale_login_sessions`, IP and device-label helpers |
| `vs_rbac/authentication.py` | Mandatory `?tenant=` assertion, impersonation, tenant contextvar |
| `vs_config/runtime_settings.py` | Live lockout/expiry security values and their allowed ranges |

## 11. Test coverage & gaps

`LoginLockoutOracleTests` (`tests.py:672`) proves the ordering of the password and
lockout checks in both directions and that a successful login returns tokens.
`FailedAttemptAuditTests` (`tests.py:706`) covers the recorded email for unknown
and known accounts. `SessionScopedLogoutTests` (`tests.py:724`) covers logout
ending only the submitted session and refresh updating only the matching session
JTI. `SchoolBrandingPayloadTests` (`tests.py:874`) pins the `school` object on both
login and `/auth/me/`, including the null cases and the unchanged flat fields.

Not covered: the `SCHOOL_CONTEXT_REQUIRED` branch; the barcode preview's four
response codes; throttle behaviour on any scope; `TOKEN_EXPIRED` versus
`TOKEN_REVOKED` versus `TOKEN_INVALID` on refresh; and `device_label` derivation
from Client Hints.
