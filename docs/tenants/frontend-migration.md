# Tenant refactor — frontend migration contract

Audience: the intranet frontend (intranet.codexng.com) and school-fe. This is
the complete list of breaking API changes from the vs_tenants platform
refactor. The cutover is deliberately breaking: there is no school-context
compatibility mode.

## 1. Every session must re-authenticate once

Tokens minted before the cutover are rejected with 401
(`"Session predates the tenant upgrade. Sign in again."`). New access tokens
carry `tenant_id` and `tenant_slug` claims; `user_type` and `school_id` claims
are gone from the token. The login response still returns `branch_id`,
`account_status`, `full_name`; school display data comes from the tenant's
school profile.

## 2. `?tenant=<slug>` is required on (almost) every call

Append `?tenant=<the user's tenant slug>` (from the login response /
`tenant_slug` claim) to every authenticated request. Semantics:

- Missing → `400 {"tenant": "A 'tenant' query parameter is required."}`
- Unknown, inactive, or not the caller's own tenant → non-enumerating `404`
- The slug is an assertion, not a selector: sending another tenant's slug can
  never grant access.

Exempt (no `?tenant=` needed — they operate purely on the caller):
`GET /v1/user/auth/me/`, `GET /v1/user/auth/me/stats/`,
`GET /v1/user/auth/me/password-resets/`, logout, self-service password change.
Login/refresh/activation are unauthenticated and unaffected.

`GET /v1/user/auth/me/` now also returns `data.tenant = {slug, name}` — cache
it as the session's tenant context.

## 3. `?school=` / school ids are dead as scope parameters

Everywhere the app previously passed `?school=<id>` for scoping (notifications
settings/history, imports, config, health), the scope is now the asserted
`?tenant=`. School endpoints that operate on the School *profile* itself
(names, branches, onboarding) still use school identifiers.

## 4. Role management moved to one unified API

The `/rbac/schools/<slug>/roles/...` and `/rbac/platform/roles/...` route
families are DELETED. Replacement (same envelope conventions, requires
`?tenant=`):

| Method + path | Purpose | Permission (any-of) |
|---|---|---|
| GET/POST `rbac/tenants/<slug>/roles/` | list / create roles | `school.roles.view/create`, `platform.roles.view/create` |
| GET/PUT/PATCH/DELETE `rbac/tenants/<slug>/roles/<key>/` | one role (addressed by per-tenant key) | view/update/delete equivalents |
| GET/POST `rbac/tenants/<slug>/role-assignments/` | list / assign | `*.roles.view` / `*.roles.assign` |
| GET/PATCH `rbac/tenants/<slug>/role-assignments/<id>/` | one assignment | view / assign |
| POST `rbac/tenants/<slug>/role-assignments/<id>/revoke/` | revoke (reason_note required) | `*.roles.assign` |
| GET/POST `rbac/tenants/<slug>/role-change-requests/` | change requests | view / `*.roles.update` |
| GET `rbac/tenants/<slug>/role-change-requests/approval/` | approval queue | view |
| POST `rbac/tenants/<slug>/role-change-requests/<id>/decide/` | approve/deny | `*.roles.update` |
| POST `rbac/platform/transfer-super-admin/` | unchanged | `platform.roles.transfer` |

Roles are addressed by their **key** (slug), not numeric id. Role bodies never
accept `tenant` (read-only, from the URL); `branch`, `user`, `role` inputs are
validated to belong to the tenant. School admins manage role templates through
explicit `school.roles.create/update/delete` grants now (seeded onto the
school_admin prebuilt role) — the old implicit `SCHOOL_ADMIN` authority is gone.

## 5. Codex staff and cross-tenant access

A Codex (platform-tenant) user asserts `?tenant=codex` for platform work. They
may assert a school's slug ONLY on endpoints that opt in: impersonation
management and the config API (managing a school's entitlements/values).
Everything else (finance books, tickets-as-user, school data) is reached by
**impersonation**:

1. `POST /v1/admin/impersonations/start/?tenant=<target-tenant-slug>` with
   `{target_user, justification, duration_minutes?}` (needs
   `platform.impersonation.start`) → session id, `201`.
2. Send `X-Impersonation-Session: <id>` on subsequent calls with
   `?tenant=<target tenant slug>`. The request runs as the target user with
   EXACTLY the target's permissions (no union with the admin's own).
3. `POST /v1/admin/impersonations/end/` `{session_id}` (only the actor who
   started it), or it expires automatically.

Codex-on-Codex impersonation is allowed. Every impersonated request is audited
with both identities.

## 6. Config API

Scope strings changed: persisted/returned scope keys are now `platform`,
`tenant:<id>`, `branch:<id>` (was `school:<id>` with the school pk). The
`allowed_scopes` vocabulary on definitions still says `"school"` for the
tenant level (unchanged shape). `?school=` targeting is gone; Codex staff
target a school by asserting its tenant slug (this API opts into cross-tenant
assertion). Branch scope requires an explicit `?branch=` (no more defaulting
to the user's branch).

## 7. Notifications

- Settings and history scope from the asserted tenant. `?school=` is ignored.
- A platform (Codex) assertion on the settings API manages the platform
  DEFAULT layer — the rows every school inherits.
- Notification/history rows expose `tenant` instead of `school`.
- History supports `scope=platform`.

## 8. Finance / payments / procurement

- `?entity=` addressing is unchanged, but the entity must belong to the
  asserted tenant — Codex staff can no longer address a school's books
  directly (use impersonation).
- Entity payloads: school-derived display fields now come from the owning
  tenant's school profile; `source_school` is removed at contract time.

## 9. Users

- User creation: the target tenant comes from the request context; `role` in
  the payload is a tenant role **key** (legacy slugs kept working because the
  migration preserved them as keys). School selection input is gone (a
  school's tenant is implied); `branch` must belong to the tenant.
- `user_type` remains in read payloads as a domain marker (student/parent/
  staff) but grants nothing; role/permission data is the only authority.
- Jobs/tasks payloads expose `tenant` instead of `school`.

## 10. Tickets

- Support console visibility = platform-tenant staff holding
  `tickets.ticket.manage`. Ticket payloads keep `branch`/`branch_name`;
  school fields are gone (derive from the tenant).
