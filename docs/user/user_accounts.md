# user_accounts

The account record itself: **who exists on the platform, which tenant and branch
owns them, how a new account is provisioned (draft, approval workflow, or direct
invite), and how its status is moved afterwards**. Routes are mounted at
`/v1/user/`; every route in this slice requires `?tenant=<slug>`
(`vs_rbac/authentication.py:95-126`).

---

## 1. What it is (and what it is NOT)

- `User` is the platform-wide `AUTH_USER_MODEL`. Every person who signs in to any
  product - platform staff, school admins, teachers, students, guardians - is one
  row here (`models.py:85-89`).
- The account carries **identity** (email, names, phone, `uid`), **ownership**
  (`tenant`, optional `branch`), a **status machine**, and provenance
  (`invited_by`, `invited_by_name`) (`models.py:119-179`).
- `user_type` is an inert persona marker. It is documented in the field itself as
  something that **must never drive authorization** - every access decision runs
  through tenant RBAC (`models.py:146-153`).
- `role` on the user row is a denormalized display string. The real grant is a
  `TenantUserRoleAssignment` row in `vs_rbac` (`models.py:154`;
  `services/user.py:92-99`).

**This does NOT post money and does NOT grant permissions.** Creating, suspending,
reactivating or deactivating an account writes no journal and no permission; it
writes the user row, an audit event, and (on the terminal transitions) a token
blacklist entry (`services/user.py:259-347`).

**It is also not the login path.** Credentials, sessions and lockout live in
`user_authentication`; the invitation link that turns a `PENDING` row into a
usable account lives in `user_invitations_activation`.

## 2. Domain model

| Model | Key fields | Tenant/uniqueness rules |
|---|---|---|
| `User` | `email`, `first_name`, `last_name`, `gender`, `phone`, `uid`, `user_type`, `role`, `status`, `is_active`, `is_staff`, `activation_key`, `password_changed_at`, `last_login_at`, `invited_by`, `invited_by_name` | `email` unique platform-wide; `tenant` PROTECT; `branch` PROTECT and nullable; `uid` unique per tenant for non-CX users and unique globally for CX staff (`models.py:134-225`) |

Four database constraints carry the shape rules (`models.py:191-220`):

- `ck_vision_staff_no_branch` - CX staff must have no branch.
- `ck_branch_required_for_branch_level_users` - everyone except CX staff and
  school admins must have one.
- `unique_uid_per_tenant` (partial, excludes CX staff) and
  `unique_uid_vision_staff` (partial, CX staff only).

Indexes cover `(tenant, user_type, status)`, `(tenant, branch)` and
`(email, status)`; default ordering is `-created_at` (`models.py:221-226`).

`status` is the source of truth and `is_active` is derived from it on every save,
never set independently: `SUSPENDED`/`DEACTIVATED`/`PENDING`/`PENDING_APPROVAL`/
`REJECTED` force `is_active=False`, `ACTIVE` forces `True`, and `LOCKED`
deliberately leaves it alone because a lock is enforced at the RBAC layer rather
than by Django auth (`models.py:299-306`). A save that passes
`update_fields=['status']` has `is_active` appended automatically so the two can
never drift (`models.py:287-296`).

## 3. Endpoint map

Request bodies below list only fields the views actually read. List endpoints use
the standard paginated `{pagination, data}` envelope (page size 25, `?page_size=`
up to 100).

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /users/` | `platform.team.view` | Query `status`, `exclude_status` (comma list), `user_type`, `scope=school`, `school_id`, `branch_id`, `search` (<=64 chars), `role`, `invited_by`, `date_from`, `date_to`, `ordering` | Paginated `UserListSerializer` rows (`views/accounts.py:103-189`) |
| `POST /users/` | `platform.team.create` | `first_name`, `last_name`, `email`, `gender?`, `user_type?`, `phone?`, `branch?`, `role?`, `position?`, `job_title?`, `employee_id?`, `employment_type?`, `date_joined?`, `date_of_birth?`, `marital_status?`, `nationality?`, `state_of_origin?`, `save_as_draft?` | `201`; shape depends on the branch taken - see below (`views/accounts.py:204-245`) |
| `GET /users/<pk>/` | `platform.team.view` | - | Enveloped `UserReadSerializer` |
| `PATCH /users/<pk>/` | `platform.team.update` | `first_name?`, `last_name?`, `phone?`, `gender?` only | Enveloped updated user (`serializers.py:444-450`) |
| `DELETE /users/<pk>/` | `platform.team.delete` | - | `200` "Deleted successfully"; the row is deactivated, never removed (`views/accounts.py:323-329`) |
| `POST /users/<pk>/submit/` | `platform.team.create` | `role?` (role key), `position?` (Position pk or code) | `200` `{user, workflow_instance?}` (`views/accounts.py:247-321`) |
| `PATCH /<user_id>/email/change/` | `platform.team.update` | `email` | Enveloped `UserListSerializer`; `409` on duplicate (`views/accounts.py:332-379`) |
| `POST /<user_id>/suspend/` | `platform.team.suspend` | - | Enveloped user; `422` on an illegal transition |
| `POST /<user_id>/reactivate/` | `platform.team.reactivate` | - | Enveloped user; `422` on an illegal transition |
| `POST /<user_id>/unlock/` | `platform.team.reactivate` | - | Enveloped user; `422` when the account is not `LOCKED` |

Three create shapes come out of the same `POST /users/`, and none of them uses the
standard envelope (`views/accounts.py:219,233-236,245`):

- `save_as_draft` truthy: bare `UserReadSerializer` body, status `DRAFT`, no
  workflow and no invitation email.
- `user_type == CX_STAFF`: `{"user": {...}, "workflow_instance": {...}}`, status
  `PENDING_APPROVAL`.
- Everything else: bare `UserReadSerializer` body, status `PENDING`, invitation
  dispatched immediately.

Read serializers apply FLS. `password_changed_at`, `last_login_at`,
`invited_by_id` and `invited_by_name` need `platform.team.view` on the detail
serializer; `invited_by_name`, `invitation_email_status` and
`invitation_expires_at` need it on the list serializer (`serializers.py:119-124,
183-187`).

## 4. Lifecycle / state machine

```text
                    save_as_draft ─▶ DRAFT ──submit──┐
                                                     │
POST /users/ ──CX_STAFF──▶ PENDING_APPROVAL ◀────────┘
                                │
                    workflow approve │ reject / withdraw / cancel
                                ▼         ▼
                            PENDING     REJECTED
POST /users/ ──other types──▶ PENDING
                                │ activation (invitation link)
                                ▼
                             ACTIVE ─suspend──▶ SUSPENDED ─reactivate─▶ ACTIVE
                                │                          
                                ├─5 failed logins─▶ LOCKED ─unlock──────▶ ACTIVE
                                │                          
                                └─DELETE /users/<pk>/─▶ DEACTIVATED ─reactivate─▶ ACTIVE
```

Transition rules, all enforced in `UserStatusService` (`services/user.py:259-347`):

- **suspend** accepts only `ACTIVE` or `LOCKED`; blacklists every outstanding
  refresh token and ends every active session with reason `SUSPENDED`.
- **reactivate** accepts only `SUSPENDED` or `DEACTIVATED`.
- **deactivate** refuses self-deactivation and refuses an already-deactivated
  account; blacklists tokens but does **not** end `LoginSession` rows.
- **unlock** accepts only `LOCKED`, clears the `AccountLockout` counters, and
  returns the account to `ACTIVE`.

`LOCKED` is also cleared by a completed password reset (`services/password.py:104-110`)
and by the admin lockout endpoint (`views/security.py:429-464`).

Workflow rejection, withdrawal and cancellation all land on the same handler:
status becomes `REJECTED`, `is_active` false, and every open `PositionAssignment`
the hire was holding is closed so a rejected candidate does not keep occupying a
seat (`workflow_handlers.py:61-81`).

## 5. Derivations

- **`uid` allocation.** On first save the row takes `max(uid) + 1` within its
  scope, starting at 10. The scope is all CX staff for a CX hire and the whole
  tenant (excluding CX staff) for everyone else, computed under
  `select_for_update()` inside a transaction (`models.py:270-291`).
- **Home tenant.** When no tenant is supplied, a branch-bound user inherits
  `branch.tenant`, a CX hire falls back to the `codex` PLATFORM tenant, and
  anyone else is left null so `full_clean()` fails loudly. The derivation runs
  before `super().full_clean()` because `clean_fields()` would otherwise report a
  spurious "tenant cannot be null" (`models.py:239-264`). `save()` re-runs it as a
  backstop and refuses a branch that belongs to another tenant (`models.py:266-269`).
- **Employee ID.** A CX hire without an explicit `employee_id` gets `CX-<n>` where
  `n` is the highest existing numeric suffix plus one, allocated while holding a
  `select_for_update()` lock on the tenant row; the profile's unique constraint is
  the final guard (`services/user.py:20-38,107-110`).
- **Displayed role.** The list serializer prefers the joined names of the user's
  ACTIVE `TenantUserRoleAssignment` rows and only falls back to the denormalized
  `role` string when there are none (`serializers.py:210-216`).
- **`school_id` / `school_name`** on list rows are derived from
  `tenant.school_profile` and are null for platform users (`serializers.py:202-208`).

Example: the first non-CX account created in a brand new tenant gets `uid = 10`
(`max(uid) or 9) + 1`); the eleventh gets `uid = 20` only if ten preceded it, not
because of any spacing rule.

## 6. What posting does to the ledger

Nothing in this slice posts. The durable effects of an account write are:

1. The `User` row and, for CX hires, a `PlatformStaffProfile` plus an
   effective-dated `PositionAssignment` (`services/user.py:101-135`).
2. A `TenantUserRoleAssignment` when a role was resolved (`services/user.py:92-99`).
3. An `AuditEvent` in the `IDENTITY` module for every create, submit, email
   change, suspend, reactivate, deactivate and unlock (`services/audit.py:41-83`).
4. On email change, suspend and deactivate: every outstanding refresh token is
   blacklisted (`services/audit.py:108-118`), and on email change and suspend
   every active `LoginSession` is closed with a reason string
   (`services/user.py:243-245,277-279`).

Deletes are soft. `perform_destroy` calls `UserStatusService.deactivate` rather
than `instance.delete()`, so the row, its audit history and its uid survive
(`views/accounts.py:323-329`).

## 7. Worked example

```json
POST /v1/user/users/?tenant=codex
{
  "first_name": "Ada",
  "last_name": "Lovelace",
  "email": "ada@codexng.com",
  "role": "xvs_platform_admin",
  "position": "BE-ENG",
  "employment_type": "FULL_TIME",
  "date_joined": "2026-08-01"
}
```

The actor's tenant kind is PLATFORM, so `user_type` defaults to `CX_STAFF`
(`serializers.py:286-294`), the target tenant is forced to `codex`
(`serializers.py:306-313`), a branch would be rejected outright
(`serializers.py:320-325`), and the seat code resolves to an active `Position`
(`serializers.py:380-395`). The response is
`{"user": {...}, "workflow_instance": {...}}` with the user at
`PENDING_APPROVAL`, an employee id of `CX-<n>`, and a primary `PositionAssignment`
already written. No invitation email goes out until the workflow approves, at
which point `finalize_invitation` flips the status to `PENDING` and queues the
email (`services/user.py:182-213`; `workflow_handlers.py:50-59`).

## 8. Gotchas / known limitations

- **A malformed user id in the path is a 500, not a 404.** The action routes bind
  `<str:user_id>` and hand it straight to `User.objects.get(id=user_id)`, so
  `/v1/user/not-a-number/suspend/` raises `ValueError` and falls through to the
  generic 500 branch of the exception handler (`urls.py:99-104`;
  `views/accounts.py:347-350,395-398,426-429,457-460`;
  `core/exceptions.py:157-162`). The same file already has `_as_row_id` for query
  parameters (`views/accounts.py:51-60`); the path parameters never got it.
- **The admin action endpoints are not tenant-scoped.** `email/change`,
  `suspend`, `reactivate`, `unlock`, `invite/resend` and `password-reset` all
  resolve the target with the plain, non-tenant-aware `User.objects` manager and
  never compare the target's tenant to the asserted one
  (`views/accounts.py:347-350`; `views/auth.py:400-403`;
  `views/passwords.py:208-211`). Anyone holding a `platform.team.*` key can act
  on any account on the platform. The list/retrieve/update path *is* scoped
  (`views/accounts.py:119-125`), so the hole is only in the bespoke action views.
- **Create responses skip the standard envelope.** `POST /users/` and
  `POST /users/<pk>/submit/` return bare serializer bodies while every other
  endpoint in the module returns `{success, message, data}`
  (`views/accounts.py:219,233-236,245,318-321`). This is frontend-visible and
  differs from `CreateModelMixin` (`core/mixins.py:36-50`).
- **`PENDING_APPROVAL` and `REJECTED` accounts are invisible in the user list.**
  The queryset excludes them unconditionally, so a hire awaiting approval can only
  be found through the workflow worklist (`views/accounts.py:127`).
- **`?status=` is not validated against the choices.** An unknown value silently
  produces an empty page rather than a 400 (`views/accounts.py:129-130`).
- **Justified by design:** `role` and `user_type` are excluded from
  `UserUpdateSerializer`, so a role change must go through the RBAC change-request
  workflow and an email change through its own endpoint (`serializers.py:444-450`).
- **Justified by design:** the single Vision Super Admin is enforced at create
  time by refusing a second `xvs_super_admin` assignment
  (`serializers.py:358-367`); handing the role over is a separate
  `platform.roles.transfer` operation.
- **Operational:** CX hires created before the workflow submission landed can be
  left in `PENDING_APPROVAL` with no workflow instance. `repair_pending_user_approvals`
  re-submits them, is safe to re-run, and refuses a row whose original inviter is
  gone (`management/commands/repair_pending_user_approvals.py:23-72`).

## 9. Permissions & tenant isolation

Every viewset action maps to one `platform.team.*` key, and the keys are seeded
from a single catalogue shared by `create_superuser` and `seed_all_permissions`
(`views/accounts.py:191-202`;
`core/management/commands/seed_platform_permissions.py:65-76`). `view`, `create`
and `update` are unrestricted; `delete`, `suspend` and `reactivate` are marked
restricted/sensitive.

Tenant isolation on the list is by actor tenant *kind*, not by `user_type`: a
platform-tenant actor keeps the platform-wide view and everyone else is filtered
to `request.tenant` (`views/accounts.py:119-125`). Creation resolves the target
tenant from the `?tenant=` assertion rather than from any client-supplied school
key, and the branch is then resolved *inside* that tenant, so another tenant's
branch is indistinguishable from one that does not exist
(`serializers.py:296-334`).

The Vision Super Admin bypasses every RBAC key check
(`vs_rbac/permissions.py:168-170`), which is what makes the unseeded keys noted in
`user_security_monitoring` §8 invisible in day-to-day use.

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py` | `User` + `UserManager`: identity, tenant/branch ownership, uid allocation, status/`is_active` sync |
| `views/accounts.py` | User CRUD, draft/submit, email change, suspend/reactivate/unlock |
| `services/user.py` | `UserCreationService`, `EmailChangeService`, `UserStatusService` |
| `serializers.py` | Create validation (tenant, branch, role, seat, profile prefill), read/list shapes with FLS |
| `workflow_handlers.py` | `PLATFORM_USER_CREATION` approve/reject/withdraw/cancel behaviour |
| `export_datasets.py` | `admin.users` and `admin.role_assignments` datasets for the Export Centre |
| `management/commands/repair_pending_user_approvals.py` | Re-submits orphaned `PENDING_APPROVAL` hires |
| `core/management/commands/seed_platform_permissions.py` | `platform.team.*` key registry and grants |

## 11. Test coverage & gaps

`PlatformUserCreationTests` (`tests.py:25`) covers sequential and explicit employee
ids, the position requirement for a non-draft CX hire, hierarchy/job-title
population, workflow-failure rollback, the sole-admin auto-approval path, and the
repair command. `DraftUserTests` (`tests.py:1247`) covers draft creation without a
role or workflow, submit with a role, and rejection when a role or position is
missing. `UserListScopeTests` (`tests.py:287`) covers the `user_type` filter, the
`scope=school` exclusion and role/placement serialization.
`UserBranchAssignmentTests` (`tests.py:1514`) covers branch persistence, string
ids, foreign and unknown branch references, branchless schools, the create
permission gate, and the non-numeric branch filter.
`UserBranchTenantGuardTests` (`tests.py:1754`) covers tenant inheritance from a
branch and the cross-tenant branch refusal.

Not covered: a malformed `user_id` in the action paths (the 500 above); a
cross-tenant target on `suspend`/`unlock`/`email-change`; the `422` bodies of the
illegal status transitions; and the empty-list response shape for
`GET /users/` (`success_response` coerces `[]` to `{}`, but the list path is
paginated so the envelope differs).
