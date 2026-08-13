# user_security_monitoring

The read-and-revoke surface: **live sessions, login attempts, account lockouts,
the identity audit trail, and the caller's own background-job queue**. Routes are
at `/v1/user/sessions/`, `/v1/user/auth-attempts/`, `/v1/user/account-lockouts/`,
`/v1/user/auth-events/` and `/v1/user/me/tasks/`.

---

## 1. What it is (and what it is NOT)

- Every endpoint here is a **read** except four: force-logout, the three
  self-service session-ending actions, and the lockout unlock
  (`views/security.py:125-265,429-464`).
- Two audiences share the same viewsets. Self-service actions (`mine`,
  `end-mine`, `end-all-mine`, `end-other-mine`) need nothing but an active
  account; the administrative lists need a permission key
  (`views/security.py:74-81,281-285`).
- `/auth-events/` does **not** read `AuthEventLog`. It reads `IDENTITY`-module
  rows from the central `vs_audit` store (`views/security.py:467-512`).

**This is not the lockout policy.** Failure counting and locking happen during
login (`user_authentication` §4); this slice only shows and clears the result.

## 2. Domain model

The three tables are described in `user_authentication` §2 (`LoginSession`,
`AuthAttempt`, `AccountLockout`). Two more matter here:

| Model | Where | Notes |
|---|---|---|
| `vs_audit.AuditEvent` | `vs_audit/models.py:174-355` | Append-only; edits and deletes raise; `objects` is `TenantAwareManager(include_global=True)`, so rows with a null tenant are visible from **every** tenant (`vs_audit/models.py:587-592`) |
| `core.BackgroundJob` | `views/jobs.py:23-63` | One row per tracked Celery job: kind, label, status, progress, owner, timings, result/error |

`vs_user.AuthEventLog` is still declared, migrated, serialized
(`AuthEventLogReadSerializer`) and referenced by the `Event` enum that
`log_auth_event` switches on - but nothing in the product writes a row to it.
Only the dev-data seeder does (`models.py:641-692`; `serializers.py:661-668`;
`core/management/commands/seed_dev_data.py:664`). Treat the model as a
vocabulary, not as storage.

## 3. Endpoint map

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /sessions/` | `platform.security.view` | Query `is_active`, `user_id`, `search` (email, name, IP, device label, user agent, tenant name/slug) | Paginated sessions; platform actors see the asserted tenant's, everyone else only their own (`views/security.py:83-104`) |
| `GET /sessions/mine/` | any active user | Query `is_active` | The caller's own sessions (`views/security.py:106-123`) |
| `POST /sessions/<pk>/end-mine/` | any active user | - | `{ended_sessions}`; `404` if the session is not the caller's (`views/security.py:125-147`) |
| `POST /sessions/end-all-mine/` | any active user | - | `{ended_sessions}`, every token blacklisted (`views/security.py:149-170`) |
| `POST /sessions/end-other-mine/` | any active user | `current_session_id` | `{ended_sessions}`; `400` if that session is not the caller's active one (`views/security.py:172-217`) |
| `POST /sessions/force-logout/` | `platform.team.suspend` | `user_id?`, `session_id?` (one required), `reason` | `{ended_sessions}` (`views/security.py:219-265`) |
| `GET /auth-attempts/` | `platform.security.view` | Query `user_id`, `tenant_id`, `email`, `ip_address`, `result`, `failure_code`, `date_from`, `date_to` | Paginated attempts (`views/security.py:287-315`) |
| `GET /auth-attempts/mine/` | any active user | - | The caller's own attempts (`views/security.py:317-326`) |
| `GET /account-lockouts/` | `IsVisionStaff` only | Query `user_id`, `locked_reason`, `last_failure_ip`, `is_locked`, `date_from`, `date_to` | Paginated lockout rows (`views/security.py:394-427`) |
| `POST /account-lockouts/unlock/` | `platform.team.reactivate` | `user_id`, `reason?`, `force_password_reset?` | `200` (`views/security.py:429-464`) |
| `GET /auth-events/` | **none in practice** - see §8 | Query `actor_id`, `subject_id`, `school_id`, `event`, `ip_address`, `date_from`, `date_to` | Paginated `AuditEventListSerializer` rows (`views/security.py:467-512`) |
| `GET /me/tasks/` | any active user | Query `scope=mine\|all`, `status`, `kind`, `since` | Paginated background jobs; `scope=all` requires a platform admin role (`views/jobs.py:65-97`) |
| `GET /me/tasks/summary/` | any active user | Same filters | `{by_status, total, can_view_all}` (`views/jobs.py:100-120`) |

Date parameters are parsed strictly: a malformed `date_from`/`date_to` is a
field-level `400`, not a silent no-op (`views/me.py:115-123`).

## 4. Lifecycle / state machine

```text
Session ending, four ways:

  end-mine (one, own)      → session.end('FORCE_LOGOUT') + blacklist that JTI
  end-all-mine (own)       → bulk update + blacklist ALL the caller's tokens
  end-other-mine (own)     → verify current_session_id is the caller's active row,
                             then bulk-end the rest and blacklist their JTIs
  force-logout (admin)     → by session: end + blacklist that JTI
                             by user:    end every active row + blacklist all tokens

Stale sweep: every session list first marks any session whose refresh token has
already expired as ended with reason EXPIRED (services/audit.py:151-171).

Lockout:  failures accumulate → locked_until set → user.status = LOCKED
          unlock (this slice)      → counters cleared, LOCKED → ACTIVE,
                                     optional 24-hour admin reset email
          unlock (user_accounts)   → same, but refuses a non-LOCKED account
```

`end-other-mine` does its read and its write inside one transaction with
`select_for_update()` on the caller's active sessions, so a concurrent login
cannot slip a device past the sweep (`views/security.py:180-205`).

Every one of these writes a `FORCE_LOGOUT` audit event carrying
`ended_sessions` and a reason: `SELF_SIGNOUT`, `SUSPECTED_COMPROMISE`,
`SELF_SIGNOUT_OTHERS`, or the admin's free-text reason
(`views/security.py:139-146,160-167,207-214,254-261`).

## 5. Derivations

- **Who sees all sessions**: actors whose tenant *kind* is `PLATFORM`; everyone
  else has the queryset narrowed to `user=self` regardless of the permission they
  hold (`views/security.py:89-96`).
- **`is_locked` filter**: `locked_until > now` for true, and
  `locked_until IS NULL OR locked_until <= now` for false, so an expired lock reads
  as unlocked without any write (`views/security.py:414-419`).
- **Stale sessions**: any active session whose `refresh_jti` appears in
  `OutstandingToken` with `expires_at <= now` is closed with reason `EXPIRED`
  (`services/audit.py:151-171`).
- **`can_view_all` for queues**: platform-tenant membership **and** an active
  `xvs_super_admin` or `xvs_platform_admin` assignment (`views/jobs.py:29-40`).
- **Queue summary counts**: `values_list('status').annotate(...)` after an explicit
  `.order_by()` reset. Without the reset Django adds the list's `-created_at` to
  the GROUP BY and buckets one row per timestamp - the cards read 1 where the
  table showed 56 (`views/jobs.py:106-112`).

## 6. What posting does to the ledger

Nothing posts. The writes this slice performs are: `LoginSession` closures,
`BlacklistedToken` rows, `AccountLockout` resets, `User.status` returning to
`ACTIVE`, an optional `PasswordResetRequest` on unlock, and one `AuditEvent` per
action (`views/security.py:125-265,429-464`).

Two of the reads write as a side effect: both `GET /sessions/` and
`GET /sessions/mine/` call `expire_stale_login_sessions` before building the
queryset, so listing sessions mutates rows (`views/security.py:88-92,109`).

## 7. Worked example

```json
POST /v1/user/sessions/end-other-mine/?tenant=codex
{ "current_session_id": 4213 }
```

```json
{ "success": true, "message": "Other sessions ended.",
  "data": { "ended_sessions": 3 } }
```

The three other devices are closed with reason `FORCE_LOGOUT` and their refresh
tokens blacklisted in a fixed number of queries via
`blacklist_tokens_by_jti` (`services/audit.py:138-148`), while session 4213 keeps
working. Passing a session id that is not the caller's own active session answers
`400 "Current session not found."` rather than ending anything
(`views/security.py:187-191`).

## 8. Gotchas / known limitations

- **The identity audit trail is readable by every authenticated user, in every
  tenant.** `AuthEventLogViewSet` lists `IsAuthenticatedAndActive` and
  `HasRBACPermission` but never sets `rbac_permission`, and `HasRBACPermission`
  passes when no key is declared (`views/security.py:476` against
  `vs_rbac/permissions.py:172-210`). The queryset adds no tenant filter of its
  own, and although `AuditEvent.objects` is tenant-aware it is configured with
  `include_global=True` while `log_auth_event` never passes a tenant - every
  identity event is written with `tenant = NULL` and therefore counts as global
  (`services/audit.py:70-80`; `vs_audit/models.py:587`;
  `vs_rbac/managers.py:114-118`). A school parent with an active account can read
  every platform login, lock, suspension, password reset and email change,
  including the subject's name and email, the actor, the IP address, and
  `previous_email`/`new_email` in the metadata. Compare `vs_audit`'s own views,
  which all require `platform.audit.view` (`vs_audit/views.py:95-96`). This is the
  one item in this slice worth fixing before anything else.
- **`platform.security.view` does not exist as a seeded permission.** The sessions
  and auth-attempts admin lists require it, but the platform seed catalogue has no
  `security` resource, so no role can be granted the key and only the Vision Super
  Admin - who bypasses RBAC entirely - can use those lists
  (`views/security.py:80,284` against
  `core/management/commands/seed_platform_permissions.py:26-149` and
  `vs_rbac/permissions.py:168-170`). Every other key these views use is seeded.
- **The attempts that matter most are invisible.** A failed login for an unknown
  email is recorded with `user=None` and `tenant=None`
  (`services/auth.py:211-218`), but `AuthAttempt.objects` is tenant-aware
  **without** `include_global`, and every authenticated request has a tenant bound
  (`models.py:523`; `vs_rbac/authentication.py:139`). Password spraying and probe
  traffic therefore never appears in `GET /auth-attempts/` for anyone.
- **Two unlock paths with different rules.** `POST /account-lockouts/unlock/`
  clears the lockout for any account and optionally emails a reset, while
  `POST /<user_id>/unlock/` refuses anything that is not `LOCKED` and routes
  through `UserStatusService` (`views/security.py:429-464` against
  `services/user.py:326-347`). Both are gated by `platform.team.reactivate`, so
  the stricter rule is trivially bypassed by choosing the other endpoint.
- **Force-logout and unlock are not tenant-scoped.** Both resolve their target
  through `PrimaryKeyRelatedField(queryset=User.objects.all())`, the plain
  non-tenant-aware manager, and the view never compares tenants
  (`serializers.py:614-622,655-658`). The `all_objects` call that follows is
  deliberate and commented, but it assumes the target was authorised, and nothing
  authorises it beyond holding the key.
- **Unvalidated integer filters return 500.** `?user_id=abc` on sessions,
  attempts or lockouts, and `?tenant_id=abc` on attempts, reach the ORM as-is and
  raise `ValueError`, which the exception handler reports as an unexpected server
  error (`views/security.py:101-102,291-295,405-406`;
  `core/exceptions.py:157-162`). `views/accounts.py:51-60` already has the helper
  that would fix this.
- **Listing sessions writes rows.** `expire_stale_login_sessions` runs on every
  `GET` and updates every session in scope whose token has expired
  (`views/security.py:88-92,109`). Correct, but a `GET` that mutates and, for a
  platform actor, sweeps the whole asserted tenant, is a surprising cost on a
  screen that polls.
- **The lockout and reset admin lists are gated by tenant kind only.**
  `AccountLockoutViewSet.list` and the password-reset list/revoke use
  `IsVisionStaff` with no RBAC key at all, so every platform-tenant account -
  including a brand new hire with no role - can read them
  (`views/security.py:388-392,336,354`). The tenant filter inside the lockout
  queryset is dead code, since a non-platform actor cannot pass `IsVisionStaff`
  (`views/security.py:402-403`).
- **Justified by design:** `AuthAttempt` keeps the entered email even when no
  account matches. For a spraying campaign that string is the only identifying
  datum available (`models.py:499-505`).
- **Justified by design:** ending a session both closes the row and blacklists the
  JTI. Doing only the first would leave a refresh token that still works after the
  UI says the device was revoked (`views/security.py:234-241`).

## 9. Permissions & tenant isolation

| Surface | Gate | Seeded? |
|---|---|---|
| Session list, auth-attempt list | `platform.security.view` | **No** - see §8 |
| Force logout | `platform.team.suspend` | Yes (restricted) |
| Lockout unlock | `platform.team.reactivate` | Yes (restricted) |
| Lockout list, password-reset list/revoke | `IsVisionStaff` (tenant kind only) | n/a |
| Auth-event list | none effectively | n/a |
| All `mine`/`end-*-mine` actions, `/me/tasks/` | active account only | n/a |

Isolation is enforced three different ways in this one file: by the tenant-aware
manager (sessions, attempts), by an explicit `user=` filter (every self-service
action), and by an explicit tenant filter (lockouts). The audit list uses none of
the three. Where a write must legitimately cross tenants, `all_objects` is used
with a comment explaining why (`views/security.py:152,198,247`).

## 10. Code map

| File | Responsibility |
|---|---|
| `views/security.py` | Session, attempt, lockout and auth-event viewsets; password-reset admin list and revoke |
| `views/jobs.py` | "My queues": background job list, summary, and the admin-scope gate |
| `services/audit.py` | `log_auth_event`, `record_attempt`, the three blacklist helpers, the stale-session sweep |
| `models.py` | `LoginSession`, `AuthAttempt`, `AccountLockout`, and the vestigial `AuthEventLog` |
| `serializers.py` | Session/attempt/lockout read shapes and the force-logout / unlock inputs |
| `export_datasets.py` | `admin.sign_ins` dataset, with IP and user agent marked sensitive |
| `vs_audit/models.py` | The store `/auth-events/` actually reads |

## 11. Test coverage & gaps

`SelfServiceSecurityScopeTests` (`tests.py:765`) is the strongest group: own
sessions and own attempts only, expired refresh tokens being closed by the list,
an ordinary user refused the admin lists, one user unable to end another's
session, `end-all-mine` leaving other users alone, and `end-other-mine` preserving
the current session, revoking the rest and rejecting a session it does not own.
`LiveSessionSearchTests` (`test_session_search.py:15`) pins the six search fields
the live-sessions screen exposes. `QueueSummaryTests` (`tests.py:1437`) covers the
GROUP BY regression, agreement between the summary and the list, shared filters,
and caller scoping.

Two structural gaps:

1. **Nothing tests `/auth-events/` at all**, which is why the missing permission
   key in §8 has gone unnoticed.
2. **The whole `vs_user` suite authenticates with `force_authenticate`**, which
   bypasses `TenantJWTAuthentication` and therefore never sets the ambient tenant
   contextvar (`vs_rbac/authentication.py:139`). Every `TenantAwareManager` in the
   module is unfiltered under test, so tenant scoping on sessions, attempts and
   audit events is effectively unverified here; the pattern that does exercise it
   lives in `vs_rbac/tests/test_tenant_isolation.py:17-43`.

Also uncovered: force-logout by user and by session; the unlock action and its
`force_password_reset` branch; the lockout list filters; and the non-numeric
filter values that currently 500.
