# user_passwords

Everything that sets a password after activation: **the published policy, the
self-service change, the self-service and admin-initiated resets, and the admin
surface for outstanding reset links**. Routes are at `/v1/user/auth/password/`,
`/v1/user/auth/reset-password/`, `/v1/user/<user_id>/password-reset/` and
`/v1/user/password-resets/`.

---

## 1. What it is (and what it is NOT)

- `password_policy.py` is the single source of truth for the rules. The same
  module supplies the validator registered in `AUTH_PASSWORD_VALIDATORS`, the
  human-readable requirement list, and the JSON payload the client renders
  (`password_policy.py:1-79`; `apps/settings/base.py:197-207`).
- `PasswordService` owns the four write paths: change, request reset, admin reset,
  and confirm reset (`services/password.py:20-176`).
- `PasswordResetRequest` records that a reset is outstanding, for which origin,
  and until when (`models.py:583-635`).

**There is no reset token.** Despite what the model and task docstrings say, the
link is `.../reset-password/{user.activation_key}` and the model stores no token
or hash at all - see §8 (`models.py:583-590`; `tasks.py:112-114,132`).

**Password validators do not run on `set_password()`** - only where
`validate_password()` is called, which is the serializers and the services. A
seeded or fixture password is therefore not policy-checked
(`password_policy.py:45-53`).

## 2. Domain model

| Model | Key fields | Rules |
|---|---|---|
| `PasswordResetRequest` | `user`, `expires_at`, `used_at`, `requested_by` (`SELF`/`ADMIN`), `requested_ip`, `requested_user_agent` | Partial unique constraint `one_active_reset_per_user` on `user` where `used_at IS NULL`; cascades with the user (`models.py:592-635`) |

`is_expired()` is `now >= expires_at`, `is_valid` is `used_at is None and not
is_expired()`, and `mark_used()` stamps `used_at` (`models.py:614-621`).

The unique constraint is what forces `_create_and_send_reset` to retire any
outstanding row before writing a new one, by marking it used rather than deleting
it (`services/password.py:143-145`).

## 3. Endpoint map

| Method + path | permission | request body / query | response |
|---|---|---|---|
| `GET /auth/password/policy/` | `AllowAny`, no auth class, no `?tenant=` | - | `{min_length, require_uppercase, require_lowercase, require_digit, require_special, requirements[]}` (`views/passwords.py:35-49`) |
| `POST /auth/password/change/` | `IsAuthenticatedAndActive`, no `?tenant=` | `current_password`, `password`, `confirm_password` | `200`; `400` on a wrong current password, a policy violation, a mismatch, or an unchanged password (`views/passwords.py:51-89`) |
| `POST /auth/password/reset/request/` | `AllowAny`, throttle `password_reset` 3/min | `email` | Always `200` with the same message (`views/passwords.py:92-118`) |
| `GET /auth/reset-password/<uuid:activation_key>/preview/` | `AllowAny`, no auth class | - | `{email, full_name}`; `400` when there is no live reset (`views/passwords.py:121-152`) |
| `POST /auth/password/reset/<uuid:activation_key>/confirm/` | `AllowAny`, no auth class | `password`, `confirm_password` | `200`; `400` with `RESET_KEY_INVALID` or `PASSWORD_POLICY_VIOLATION` (`views/passwords.py:155-190`) |
| `POST /<user_id>/password-reset/` | `platform.team.update` | - | `200` "Password reset email sent."; `404` unknown user; `403` on any failure (`views/passwords.py:193-229`) |
| `GET /password-resets/` | `IsAuthenticatedAndActive` + `IsVisionStaff` | - | Unpaginated list of live reset rows with user, origin, IP and expiry (`views/security.py:329-344`) |
| `POST /password-resets/<pk>/revoke/` | `IsAuthenticatedAndActive` + `IsVisionStaff` | - | `200`; `404` when already used or unknown (`views/security.py:347-366`) |
| `GET /auth/me/password-resets/` | `IsAuthenticatedAndActive`, no `?tenant=` | - | The caller's own last 20 requests, newest first (`views/me.py:88-112`) |

The self-service list omits the `user` field because it is always the requester;
the admin list nests a slim user object (`serializers.py:671-689`).

## 4. Lifecycle / state machine

```text
POST /auth/password/reset/request/   (email unknown or DEACTIVATED → silent no-op)
POST /<user_id>/password-reset/      (admin, any status)
                    │
                    ├─ retire any outstanding unused row (used_at = now)
                    ├─ create PasswordResetRequest(expires_at = now + window,
                    │                              requested_by = SELF | ADMIN)
                    └─ queue user.password_reset email → …/reset-password/{activation_key}
                                   │
      GET  …/preview/  ── no unused row ─▶ 400   ── past expires_at ─▶ 400
                                   │ live
      POST …/confirm/  ─▶ password validated
                          set_password · password_changed_at · activation_key ROTATED
                          LOCKED → clear lockout → ACTIVE ; PENDING → ACTIVE
                          reset row marked used · all refresh tokens blacklisted

POST /password-resets/<pk>/revoke/  ─▶ used_at = now (link dies immediately)
```

`confirm_reset` also doubles as an activation path: a `PENDING` account that never
used its invitation becomes `ACTIVE` through a reset (`services/password.py:104-110`).

A password **change** is the simpler path - validate, set, stamp, blacklist - and
has no reset row at all (`services/password.py:22-42`).

## 5. Derivations

- **Policy**: minimum 12 characters plus an uppercase, a lowercase, a digit and a
  non-alphanumeric character. All five rules are checked and reported together,
  each with its own error code (`password_policy.py:16-73`). Django's
  `UserAttributeSimilarityValidator` and `CommonPasswordValidator` run alongside
  it (`apps/settings/base.py:197-207`).
- **Reset window** = `now + self_reset_expiry_hours` for `SELF` (default 1 hour,
  allowed 1-24) and `now + admin_reset_expiry_hours` for `ADMIN` (default 24
  hours, allowed 1-168), both resolved for the user's tenant and branch
  (`services/password.py:135-141`; `vs_config/runtime_settings.py:28-29,55-56`).
- **Email expiry text** is derived independently in the task as `1 if origin ==
  'SELF' else 24`, not from the setting used to build the row (`tasks.py:133`).
- **Job ownership**: the queue row belongs to the requesting admin on an `ADMIN`
  reset and to the user themselves on a `SELF` reset, never to the target of an
  admin reset (`services/password.py:127-134,159-162`).
- **Which reset a confirm uses**: the newest unused row for that user
  (`.last()` over an unordered queryset resolves to the highest pk), which the
  unique constraint guarantees is the only one (`services/password.py:86-91`).

## 6. What posting does to the ledger

Nothing here posts. A completed reset writes the new password hash,
`password_changed_at`, a rotated `activation_key`, possibly a status change and a
cleared lockout, `used_at` on the reset row, and a blacklist entry for every
outstanding refresh token - all inside one transaction
(`services/password.py:98-115`). A `PASSWORD_RESET_COMPLETED` audit event follows
with the origin in its metadata; a change writes `PASSWORD_CHANGED`, and an
admin-initiated reset writes `PASSWORD_RESET_REQUESTED` naming the initiating
admin (`services/password.py:39-42,71-77,117-122`).

Self-service reset requests write **no** audit event: only the admin path is
logged (`services/password.py:44-57` against `:61-77`).

## 7. Worked example

```json
POST /v1/user/auth/password/change/
{ "current_password": "Old-Pass-2025!", "password": "New-Pass-2026!",
  "confirm_password": "New-Pass-2026!" }
```

The serializer verifies the current password, runs the validators against the new
one, refuses a new password identical to the current one, and refuses a mismatch -
each as a field-level 400 (`serializers.py:528-545`). On success every refresh
token the user holds is blacklisted, so their other devices stop refreshing at the
next 15-minute boundary (`services/password.py:37`). Their `LoginSession` rows are
**not** closed, so those devices keep showing as active until the stale-session
sweep runs (see §8).

## 8. Gotchas / known limitations

- **The "hashed reset token" does not exist.** `PasswordResetRequest`'s docstring
  says "Only the SHA-256 hash is stored - never the raw token" and the email task
  repeats it, but the model has no token field and the link is the user's
  long-lived `activation_key` (`models.py:583-590`; `tasks.py:112-114,132`).
  The practical consequences: the same secret serves the invitation flow, it is
  not per-request, and revoking a reset row does not invalidate the URL - only
  a later rotation does (`views/security.py:347-366`).
- **Changing a password does not end the sessions it says it ends.** Both
  `change()` and `confirm_reset()` blacklist tokens but leave `LoginSession` rows
  `is_active=True`, unlike suspend and email change which close them explicitly
  (`services/password.py:24-42,98-115` against `services/user.py:243-245,277-279`).
  The session list keeps showing dead devices as live until
  `expire_stale_login_sessions` sweeps them, which only happens when someone lists
  sessions (`services/audit.py:151-171`).
- **`confirm_reset` sets `is_active = True` unconditionally.** For a `SUSPENDED`
  or `DEACTIVATED` user that is only neutralised by the model's `_sync_is_active`
  backstop on save (`services/password.py:102` against `models.py:299-306`). Any
  future caller that writes the field without going through `User.save()` would
  reactivate a suspended account through the reset flow.
- **A suspended or deactivated account can still be reset by an admin.**
  `admin_reset` has no status gate at all, so a reset email goes to a suspended
  user who then cannot log in (`services/password.py:59-77`). Self-service only
  excludes `DEACTIVATED` (`services/password.py:52-53`).
- **The admin reset endpoint answers 403 for every failure.** Any exception from
  the service is mapped to `HTTP_403_FORBIDDEN`, so a configuration or delivery
  fault reads as a permission problem (`views/passwords.py:219-227`).
- **The admin reset list is unpaginated and permissioned only by tenant kind.**
  `IsVisionStaff` passes any user whose tenant kind is `PLATFORM`, with no RBAC
  key, and the view serializes every live row in one response
  (`views/security.py:329-344`; `vs_rbac/permissions.py:95-105`). Revoking is
  gated the same way. Compare the rest of the module, where every admin action
  carries a `platform.*` key.
- **A logged-in client cannot call the public reset request without `?tenant=`.**
  `PasswordResetRequestView` sets `AllowAny` but keeps the default authentication
  class, so a request carrying a Bearer header and no `?tenant=` is rejected with
  a tenant error before it reaches the view (`views/passwords.py:104-105`;
  `vs_rbac/authentication.py:122-126`). The sibling public views set
  `authentication_classes = []` and do not have this problem.
- **Justified by design:** the self-service request always answers "If the account
  exists, reset instructions have been sent", which is what keeps the endpoint
  from confirming an address (`views/passwords.py:107-118`;
  `services/password.py:44-57`). Note that the barcode login preview answers the
  same question directly - see `user_authentication` §8.
- **Justified by design:** an email dispatch failure falls back to a synchronous
  send and, failing that, is logged and swallowed. The row exists and the user can
  ask again (`services/password.py:153-176`).

## 9. Permissions & tenant isolation

The policy, request, preview and confirm endpoints are public by necessity - a
locked-out user has no token. Change requires only an authenticated, non-terminal
account. The admin reset carries `platform.team.update`, and like the other
bespoke action views it resolves its target with the unscoped `User.objects`
manager, so a holder of that key can trigger a reset for any account on the
platform (`views/passwords.py:206-211`).

`PasswordResetRequest` has no tenant field and no tenant-aware manager, so the
admin list is inherently platform-wide; the `IsVisionStaff` gate is the only thing
keeping it inside the platform tenant (`models.py:583-635`;
`views/security.py:336-344`).

## 10. Code map

| File | Responsibility |
|---|---|
| `password_policy.py` | Canonical rules, validator, help text, public policy payload |
| `services/password.py` | `PasswordService`: change, request, admin reset, confirm, dispatch |
| `views/passwords.py` | Policy, change, request, preview, confirm, admin reset |
| `views/security.py` | Admin list and revoke for outstanding resets |
| `views/me.py` | The caller's own reset history |
| `models.py` | `PasswordResetRequest` and its one-active-per-user constraint |
| `tasks.py` | `send_password_reset_email_task` and the reset URL |
| `serializers.py` | Change/request/confirm/preview shapes and the two list shapes |

## 11. Test coverage & gaps

`PasswordPolicyTests` (`tests.py:1212`) covers the validator rejecting a password
that misses any single rule, accepting a compliant one, and the public policy
endpoint listing the requirements. `EmailFailureResilienceTests` (`tests.py:975`)
covers the request returning 200 when the eager SMTP send fails and when the
broker is down as well. `JobAttributionTests` (`tests.py:228`) covers job
ownership for both reset origins.

Not covered: the confirm path end to end, including the `LOCKED` and `PENDING`
status transitions; `RESET_KEY_INVALID` for an expired row; the change endpoint's
four rejection branches; revoke; the admin list's permission boundary; and the
empty-list shape of `GET /password-resets/` (`success_response` coerces `[]` to
`{}`, so an empty admin list answers `"data": {}`, not `[]`).
