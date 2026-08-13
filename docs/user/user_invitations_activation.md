# user_invitations_activation

How a created account becomes a usable one: **the invitation record, the emailed
activation link, the first password, and the resend path**. Activation routes are
public and live at `/v1/user/auth/activate/<activation_key>/`; the resend action
is an admin route at `/v1/user/<user_id>/invite/resend/`.

---

## 1. What it is (and what it is NOT)

- `UserInvitation` is one row per user recording whether the activation link is
  still live: unused and inside the configured window. On a resend the same row
  is reset rather than a second one created, which is why the link the user
  already has keeps working (`models.py:336-428`).
- **There is no invitation token.** The link is keyed by `User.activation_key`, a
  UUID stored on the user row, and the invitation record only carries the expiry
  and used flag (`models.py:164-165`; `services/invitation.py:76-115`).
- Activation is where the account gets its first password and moves to `ACTIVE`
  (`services/invitation.py:119-177`).
- Email delivery is asynchronous, goes through `vs_notifications`, and reports
  back into the invitation row through delivery signals (`tasks.py:43-97`;
  `receivers.py:27-68`).

**This does NOT create the account** (see `user_accounts`) and **does not reset a
forgotten password** (see `user_passwords`), although both flows share the same
`activation_key` secret - see §8.

## 2. Domain model

| Model | Key fields | Rules |
|---|---|---|
| `UserInvitation` | `user` (OneToOne), `invited_by`, `expires_at`, `is_used`, `email_status`, `email_sent_at`, `email_last_error`, `email_attempts` | One live invitation per user by construction; cascades with the user; `invited_by` survives as `SET_NULL` (`models.py:336-379`) |

State helpers: `is_expired` is `now > expires_at`, `is_valid` is
`not is_used and not is_expired`, `consume()` flips `is_used`, and `reset()`
re-opens the row with a fresh window and clears all four email-tracking fields
(`models.py:383-425`).

`email_status` is `PENDING` -> `SENT` | `FAILED`, written only by the delivery
receivers, never by the dispatch call (`receivers.py:40-53`).

## 3. Endpoint map

| Method + path | permission | request body / query | response |
|---|---|---|---|
| `GET /auth/activate/<uuid:activation_key>/preview/` | `AllowAny`, no auth class | - | `{email, first_name, last_name, full_name}`; `400` with `INVITATION_NOT_FOUND` / `INVITATION_ALREADY_USED` / `INVITATION_EXPIRED` (`views/auth.py:315-339`) |
| `POST /auth/activate/<uuid:activation_key>/` | `AllowAny`, throttle `activation` 10/min | `password`, `confirm_password` | `{message}` on success; `400` with `PASSWORD_POLICY_VIOLATION` or the invitation error codes (`views/auth.py:342-380`) |
| `POST /<user_id>/invite/resend/` | `platform.team.create` | - | `200`; `404` unknown user; `422` when the account is not `PENDING` (`views/auth.py:383-422`) |

The preview deliberately returns the name and email so the activation screen can
render them as read-only fields and the user only has to choose a password
(`serializers.py:477-487`).

Both the serializer and the view compare `password` against `confirm_password`,
so a mismatch is caught twice and answered identically
(`serializers.py:467-474`; `views/auth.py:363-368`).

## 4. Lifecycle / state machine

```text
create user (non-CX)  ─────────────┐
workflow approval (CX hire) ───────┤
                                   ▼
                    finalize_invitation: status → PENDING
                                   │
                    InvitationService.create → UserInvitation(is_used=False,
                                   │              expires_at = now + invitation_expiry_days)
                    send_invitation_email_task.delay(activation_key)
                                   │
                 ┌─────────────────┴──────────────────┐
        notification_sent                     notification_failed
        email_status = SENT                   email_status = FAILED
                                   │
              user opens {FRONTEND_BASE_URL}/activate/{activation_key}
                                   │
   GET …/preview/  ── used ─▶ INVITATION_ALREADY_USED
                   ── past expires_at ─▶ INVITATION_EXPIRED
                                   │ valid
   POST …/         ─▶ password validated → set_password → status ACTIVE,
                      is_active True, activation_key ROTATED, invitation.consume()
```

Resend is the only way back into that flow, and only for a `PENDING` account: it
resets the same row (new window, tracking cleared, `invited_by` updated to whoever
resent it) and re-queues the email. The URL does not change
(`models.py:403-425`; `services/invitation.py:181-230`).

If the invitation row is missing when a resend is requested, one is created rather
than the request failing (`services/invitation.py:192-200`).

## 5. Derivations

- **Expiry window** = `now + invitation_expiry_days`, resolved live for the user's
  tenant and branch, default 7 days, tenant-configurable between 1 and 30
  (`services/invitation.py:50-57`; `models.py:408-415`;
  `vs_config/runtime_settings.py:30,57`).
- **Activation link** = `{FRONTEND_BASE_URL}/activate/{user.activation_key}`.
  `FRONTEND_BASE_URL` is required; the task raises `ImproperlyConfigured` without
  it (`tasks.py:73-77`).
- **From-name parity**: the invitation email carries
  `metadata.from_name = user.invited_by_name` so the delivery task builds a From
  header naming the inviter rather than a generic system address
  (`tasks.py:92-95`).
- **Job ownership**: the background job row belongs to the admin who asked for the
  invite, not to the invitee, and `_job_notify=False` keeps a bulk approval from
  dropping one bell notification per invited row
  (`services/user.py:197-208`; `services/invitation.py:203-212`).
- **Password rules** are Django's validators plus the platform complexity rule:
  12 characters with an uppercase, a lowercase, a digit and a special character
  (`password_policy.py:16-79`; `apps/settings/base.py:197-207`).

## 6. What posting does to the ledger

Nothing here posts. A completed activation writes, in one transaction: the
hashed password, `password_changed_at`, `is_active=True`, `status=ACTIVE`, a
**new** `activation_key`, and `is_used=True` on the invitation
(`services/invitation.py:150-164`). An `ACCOUNT_ACTIVATED` audit event follows,
and an `INVITATION_SENT` event is written on every resend
(`services/invitation.py:166-173,222-228`).

For a CX hire, `InvitationService.create` also ensures the
`PlatformStaffProfile` exists and settles its cached `position` from the current
primary assignment, so department and line-manager derivation work from the
moment the invite goes out (`services/invitation.py:61-70`).

Rotating `activation_key` on activation is what kills the old link: a second visit
to the same URL cannot even find a user (`services/invitation.py:155`;
`models.py:394-401`).

## 7. Worked example

```json
POST /v1/user/auth/activate/2f1c9a54-8f0e-4a2a-9a70-2b5d5f6c1e77/
{ "password": "Correct-Horse-9!", "confirm_password": "Correct-Horse-9!" }
```

```json
{ "success": true, "message": "Account activated successfully.",
  "data": { "message": "Account activated. You can now log in." } }
```

The user is now `ACTIVE` but **not signed in**: no tokens are returned and the
client must call `POST /auth/login/` next (`services/invitation.py:175-177`). A
second POST to the same URL answers `INVITATION_NOT_FOUND`, because the key it
addresses no longer exists.

## 8. Gotchas / known limitations

- **The email states a 7-day expiry regardless of the configured window.** The
  notification context hardcodes `expiry_days: 7` while the invitation row is
  built from the tenant's `invitation_expiry_days` setting (`tasks.py:87` against
  `services/invitation.py:50-57`). A tenant that shortens the window to 3 days
  sends invitations that promise 7 and stop working on day 4.
- **One secret serves two flows.** The invitation link and the password-reset link
  are both `.../{user.activation_key}`, so an outstanding invitation and an
  outstanding reset are the same secret with different paths, and issuing a reset
  rotates the key and silently kills the invitation link
  (`services/password.py:101,156-162`; `tasks.py:132`). Neither flow mints a
  per-request token.
- **The activation code no longer returns tokens, but the docstrings still say it
  does.** `ActivationView`'s docstring promises "JWT tokens are returned so the
  user is logged in immediately" and the service's step list says "6. Issue JWT
  tokens"; the return value is a message only
  (`views/auth.py:344-347`; `services/invitation.py:131-135,175-177`). Stale
  contract documentation on a public endpoint.
- **Activation errors return 400, not 410/404.** Expired, used and unknown links
  are all `400` with distinguishable `error_code` values, so the client has to
  read the body to tell them apart (`views/auth.py:330-334,375-378`).
- **The preview endpoint is unthrottled.** `ActivationView` carries
  `throttle_scope = 'activation'`; `ActivationPreviewView` carries none
  (`views/auth.py:315-339`). The key is a v4 UUID, so this is a small exposure,
  but the two endpoints answer the same question.
- **A dead branch in `create()`.** The `user.tenant_id is None` case selects a
  tenant-less security value, but `tenant` is non-nullable and derived on save, so
  it cannot be reached from the API (`services/invitation.py:50-57`;
  `models.py:239-269`).
- **Justified by design:** an email dispatch failure never fails the request. The
  invitation row already exists, the exception is logged, and the failure surfaces
  through `email_status`/`email_last_error` for the admin to retry
  (`services/user.py:197-213`; `services/invitation.py:213-220`).
- **Justified by design:** resend is refused for anything other than `PENDING`. A
  `DRAFT` or `PENDING_APPROVAL` hire has no live invitation to resend, and the
  view answers `422` rather than quietly creating one (`views/auth.py:405-409`).

## 9. Permissions & tenant isolation

The activation pair is fully public (`permission_classes = [AllowAny]` and
`authentication_classes = []`), which is what keeps a stale Bearer header from
turning the call into a tenant assertion failure (`views/auth.py:325-326,354-356`).
The link itself is the credential.

Resend requires `platform.team.create` and, like the other bespoke action views,
resolves its target with the unscoped `User.objects` manager and never compares
the target's tenant to the asserted one (`views/auth.py:396-403`) - the same
class of gap recorded in `user_accounts` §8.

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py` | `UserInvitation`: window, used flag, email tracking, `consume`/`reset` |
| `services/invitation.py` | Create/validate/activate/resend, plus the CX profile and seat sync |
| `views/auth.py` | Activation preview, activation, resend |
| `tasks.py` | `send_invitation_email_task`: builds the link and hands off to `vs_notifications` |
| `receivers.py` | `notification_sent`/`notification_failed` -> invitation email tracking |
| `password_policy.py` | The complexity rules every password-set flow enforces |
| `serializers.py` | `ActivationSerializer`, `ActivationPreviewSerializer` |

## 11. Test coverage & gaps

`InvitationEngineDispatchTests` (`tests.py:1102`) covers dispatch creating a
notification carrying the activation key, the receiver updating the invitation on
successful delivery, and the inviter's name reaching the outgoing From header.
`EmailFailureResilienceTests` (`tests.py:975`) covers an eager SMTP failure
marking the invitation `FAILED` without raising. `JobAttributionTests`
(`tests.py:228`) covers job ownership for both the first invite and the resend.
`PasswordPolicyTests` (`tests.py:1212`) covers the validator in both directions
and the public policy endpoint.

Not covered: the activation happy path end to end (preview -> activate -> login);
the used and expired branches of `get_valid_invitation`; the resend `422` for a
non-`PENDING` account; and the expiry-window mismatch described in §8.
