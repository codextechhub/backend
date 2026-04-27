# Backend Gap Analysis — Checklist
> Generated: 2026-04-26 | Codebase: Django REST Framework Backend

---

## HOW TO USE THIS DOCUMENT
Each item has:
- **Where** — file path and approximate line number
- **Problem** — what's wrong / what's missing
- **Fix** — how to resolve it

Priority legend: 🔴 Critical (can crash / security hole) · 🟠 High (data risk / broken logic) · 🟡 Medium (edge case / UX issue)

---

## SECTION 1 — NULL SAFETY / CRASH RISKS

- [ ] 🔴 **PasswordResetPreviewView crashes if no active reset request exists**
  - **Where:** `apps/vs_user/views.py` ~line 346
  - **Problem:** `.last()` returns `None`, but the next line accesses `reset_request.expires_at` without a null check — instant `AttributeError`.
  - **Fix:** Add `if not reset_request: return error_response(...)` immediately after the `.last()` call.

- [ ] 🔴 **`User.objects.get()` followed by `if not user:` dead code**
  - **Where:** `apps/vs_user/views.py` ~line 379
  - **Problem:** `.get()` either returns an object or raises `DoesNotExist`; it never returns `None`. The `if not user:` guard is unreachable — actual null path is unhandled.
  - **Fix:** Wrap the `.get()` call in `try/except User.DoesNotExist` and return the error response there.

- [ ] 🔴 **School `.first()` result used without null check in branch view**
  - **Where:** `apps/vs_schools/views/branch.py` ~lines 140–143
  - **Problem:** `School.objects.filter(slug=i_slug).first()` can return `None`; accessing `.status` on it raises `AttributeError`.
  - **Fix:** `school = School.objects.filter(slug=i_slug).first(); if not school: raise NotFound(...)`.

- [ ] 🟠 **Auth service `.first()` user then accesses user attributes assuming non-null**
  - **Where:** `apps/vs_user/services/auth.py` ~line 46
  - **Problem:** `user = User.objects.filter(email__iexact=email).first()`. If `user` is `None`, later lines access `user.user_type` and `user.school` — crash.
  - **Fix:** Add an early `if not user: return unauthenticated_response(...)` guard before attribute access.

- [ ] 🟠 **`_handle_failed_attempt()` school null dereference**
  - **Where:** `apps/vs_user/services/auth.py` ~line 180
  - **Problem:** `.first()` on school lookup, then school attributes accessed directly.
  - **Fix:** Guard with `if not school: return` or raise appropriate error.

- [ ] 🟡 **EmailChangeService doesn't check `new_email == current_email` first**
  - **Where:** `apps/vs_user/services/user.py` ~line 95
  - **Problem:** Wastes a DB round-trip and returns a confusing "already in use" error when someone submits the same email.
  - **Fix:** Add `if new_email.lower() == user.email.lower(): raise ValidationError("This is already your email.")` at the top of the method.

---

## SECTION 2 — SERIALIZER BUGS

- [ ] 🔴 **`UserUpdateSerializer.validate()` email check never triggers**
  - **Where:** `apps/vs_user/serializers.py` ~line 193
  - **Problem:** The `validate()` method checks `attrs.get('email')` but `email` is not in `Meta.fields` — `attrs` will never contain it. The intended email-change protection is silently bypassed.
  - **Fix:** Either add `email` to `Meta.fields` as `read_only=True`, or remove the dead check and enforce email immutability elsewhere.

- [ ] 🔴 **`PasswordResetPreviewSerializer.get_full_name()` never called**
  - **Where:** `apps/vs_user/serializers.py` ~line 300
  - **Problem:** `full_name` is declared as `CharField(read_only=True)`, not `SerializerMethodField`. The `get_full_name()` method is dead code; the field serialises incorrectly.
  - **Fix:** Change `full_name = serializers.CharField(read_only=True)` to `full_name = serializers.SerializerMethodField()`.

- [ ] 🟠 **`UserInvitationReadSerializer` references non-existent `role_hint` field**
  - **Where:** `apps/vs_user/serializers.py` ~line 335
  - **Problem:** `role_hint` field is declared but does not exist on the model — raises `AttributeError` when serialising.
  - **Fix:** Remove the field or add the corresponding model/property.

- [ ] 🟠 **`gender` field marked `required=True` but model has `default=''`**
  - **Where:** `apps/vs_user/serializers.py` ~line 106
  - **Problem:** Conflicting contract — model allows empty, serializer demands a value. Frontend gets a 400 if it omits the field; backend silently accepts empty on direct model writes.
  - **Fix:** Pick one: make `required=False` in the serializer, or remove the model default.

- [ ] 🟡 **Role not validated as assignable to the given `user_type`**
  - **Where:** `apps/vs_user/serializers.py` ~line 177
  - **Problem:** Role existence is checked but not whether that role is compatible with the user's type. A VISION_STAFF-only role could be assigned to a BRANCH_ADMIN.
  - **Fix:** Add `if role.allowed_user_types and user_type not in role.allowed_user_types: raise ValidationError(...)`.

---

## SECTION 3 — PERMISSIONS / ACCESS CONTROL

- [ ] 🔴 **RBAC permission is empty string on `UserAccountViewSet`**
  - **Where:** `apps/vs_user/views.py` ~line 458
  - **Problem:** `rbac_permission = ""` means no RBAC check is enforced on the entire user management viewset — any authenticated user can list, create, or delete accounts.
  - **Fix:** Set a real permission constant (e.g. `rbac_permission = "manage_users"`) and wire `HasRBACPermission` into `get_permissions()`.

- [ ] 🔴 **`IsAuthenticatedStaff` is a stub — grants access to anyone authenticated**
  - **Where:** `apps/vs_import_data/views.py` ~line 59
  - **Problem:** The class body is just `pass`, inheriting only `IsAuthenticated`. Any logged-in user can trigger bulk data imports.
  - **Fix:** Implement the class to verify `request.user.user_type in ['VISION_STAFF', 'SCHOOL_ADMIN']` or equivalent.

- [ ] 🟠 **12+ endpoints have TODO comments indicating missing RBAC wiring**
  - **Where:** `apps/vs_user/views.py` lines ~239, 282, 405, 531, 579, 610, 641, 679, 707, 760, 812, 852
  - **Affected views:** `InvitationResendView`, `PasswordChangeView`, `AdminPasswordResetView`, `UserEmailChangeView`, `UserSuspendView`, `UserReactivateView`, `UserUnlockView`, `SessionViewSet`, `AccountLockoutViewSet`
  - **Problem:** All marked as TODO; currently any authenticated user can call these actions.
  - **Fix:** Wire `HasRBACPermission` with a specific permission string in each view's `permission_classes`.

- [ ] 🟠 **Import data views don't verify requesting user belongs to the target school**
  - **Where:** `apps/vs_import_data/views.py` ~line 79
  - **Problem:** `school_lookup_url_kwarg = "school_id"` but no check that the requesting school admin owns that school ID. A user can modify the URL to import into another school.
  - **Fix:** Add `get_object_or_404(School, id=school_id, admins=request.user)` or equivalent ownership check.

---

## SECTION 4 — AUTHENTICATION & SESSION BUGS

- [ ] 🔴 **Logout blacklists ALL tokens even if the submitted token is invalid/garbage**
  - **Where:** `apps/vs_user/views.py` ~line 111
  - **Problem:** `blacklist_all_user_tokens(request.user)` runs unconditionally. An attacker can submit a bogus request and invalidate all of a user's sessions.
  - **Fix:** Validate the submitted refresh token first; only proceed with full blacklist if token is valid.

- [ ] 🟠 **Session end and token blacklist happen in separate code paths — can diverge**
  - **Where:** `apps/vs_user/views.py` ~lines 118–124
  - **Problem:** Sessions are ended only if JTI exists, but token blacklist happens regardless. They should be atomic.
  - **Fix:** Wrap both operations in `transaction.atomic()` and handle them together.

---

## SECTION 5 — RACE CONDITIONS

- [ ] 🟠 **Auth lockout check is not atomic — race between check and session creation**
  - **Where:** `apps/vs_user/services/auth.py` ~line 25
  - **Problem:** `@transaction.atomic` is present but `.filter().first()` calls followed by mutations can still race under concurrent requests. Another thread could change lockout state between check and token issuance.
  - **Fix:** Use `select_for_update()` on lockout record: `AccountLockout.objects.select_for_update().filter(user=user).first()`.

- [ ] 🟠 **Invitation `get_or_create` + `reset()` not atomic**
  - **Where:** `apps/vs_user/services/invitation.py` ~line 41
  - **Problem:** Between `get_or_create` and `reset()`, another thread could read the stale invitation.
  - **Fix:** Use `select_for_update()` or a conditional `update()` instead of read-then-write.

---

## SECTION 6 — TRANSACTION / STATE CONSISTENCY

- [ ] 🔴 **`activation_key` UUID rotation can fail mid-save, leaving old link valid**
  - **Where:** `apps/vs_user/services/password.py` ~line 97
  - **Problem:** `user.activation_key = uuid.uuid4(); user.save()` — if the save fails, the old link stays usable.
  - **Fix:** Rotate the key and mark the reset as used in a single `transaction.atomic()` block with rollback safety.

- [ ] 🟠 **Role assignment silently skipped if `RoleTemplate` doesn't exist**
  - **Where:** `apps/vs_user/services/user.py` ~line 52
  - **Problem:** `role = RoleTemplate.objects.filter(...).first(); if role: assign`. If no template exists, user is created without a role and no error is raised — silent misconfiguration.
  - **Fix:** Raise `ValidationError` or log a warning when role assignment is skipped.

---

## SECTION 7 — MODEL CONSTRAINTS

- [ ] 🟠 **`branch` field is nullable at DB level but business logic requires it for BRANCH_ADMIN**
  - **Where:** `apps/vs_user/models.py` ~line 114
  - **Problem:** `clean()` enforces branch presence, but DB has no constraint — direct ORM writes bypass `clean()` and create invalid data.
  - **Fix:** Add a `CheckConstraint` in `Meta.constraints`:
    ```python
    CheckConstraint(
        check=~Q(user_type='BRANCH_ADMIN', branch__isnull=True),
        name='branch_admin_requires_branch'
    )
    ```

- [ ] 🟠 **`role` is a free-text `CharField` with no FK to `RoleTemplate`**
  - **Where:** `apps/vs_user/models.py` ~line 131
  - **Problem:** Any string can be stored; referential integrity is never enforced at DB level.
  - **Fix:** Either change to `ForeignKey(RoleTemplate, ...)` or add a `clean()` validator that checks the string against existing templates.

- [ ] 🟡 **No constraint ensuring `is_active=True` ↔ `status=ACTIVE`**
  - **Where:** `apps/vs_user/models.py` ~line 140
  - **Problem:** A user can be `is_active=True` but `status=LOCKED`, or vice-versa — inconsistent state.
  - **Fix:** Override `save()` to sync these fields, or add a `CheckConstraint` enforcing the relationship.

---

## SECTION 8 — PERFORMANCE / N+1 QUERIES

- [ ] 🟠 **Session logout loop does individual saves instead of a batch update**
  - **Where:** `apps/vs_user/services/auth.py` ~lines 117–124
  - **Problem:** Loops through sessions calling `.save()` on each — O(n) queries.
  - **Fix:** Replace loop with `LoginSession.objects.filter(user=user, is_active=True).update(is_active=False, ended_at=now(), end_reason='LOGOUT')`.

- [ ] 🟡 **No pagination on `SessionViewSet`, `AuthAttemptViewSet`, `AccountLockoutViewSet`**
  - **Where:** `apps/vs_user/views.py` ~lines 689, 770, 823
  - **Problem:** Admin could see thousands of rows with no limit — slow queries and large payloads.
  - **Fix:** Add `pagination_class = StandardResultsSetPagination` (or similar) to each viewset.

- [ ] 🟡 **Missing `prefetch_related` on `UserAccountViewSet`**
  - **Where:** `apps/vs_user/views.py` ~line 474
  - **Problem:** `select_related` fetches direct FKs but if the serializer touches `user.sessions` or other reverse relations, N+1 occurs.
  - **Fix:** Add `prefetch_related('sessions', 'role_assignments')` to the queryset.

---

## SECTION 9 — EMAIL / TASK BUGS

- [ ] 🟠 **Invitation email task called synchronously — blocks request and fails on email outage**
  - **Where:** `apps/vs_user/services/user.py` ~line 69
  - **Problem:** `send_invitation_email_task(str(user.activation_key))` is a direct call, not `.delay()` or `.apply_async()`. If the email service is down, user creation rolls back.
  - **Fix:** Call `send_invitation_email_task.delay(str(user.activation_key))` and handle email failure independently from user creation.

- [ ] 🟠 **No retry logic in email tasks**
  - **Where:** `apps/vs_user/tasks.py` ~lines 65, 116, 128
  - **Problem:** Email failures are swallowed with no retry. Users never receive activation/invitation emails silently.
  - **Fix:** Add Celery retry decorator:
    ```python
    @shared_task(bind=True, max_retries=3, default_retry_delay=60)
    def send_invitation_email_task(self, activation_key):
        try:
            ...
        except Exception as exc:
            raise self.retry(exc=exc)
    ```

- [ ] 🟡 **`FRONTEND_BASE_URL` hardcoded in tasks**
  - **Where:** `apps/vs_user/tasks.py` ~line 116
  - **Problem:** URL won't work in multi-domain / multi-tenant setups.
  - **Fix:** Pass the base URL as a parameter from the caller (which knows the school's domain), or read from a per-school setting.

---

## SECTION 10 — EXCEPTION HANDLING

- [ ] 🟠 **`e.args[0]` parsed as dict in 8+ `except Exception` blocks — can crash**
  - **Where:** `apps/vs_user/views.py` ~lines 263, 390, 424, 593, 624, 654 (and more)
  - **Problem:** `payload = e.args[0]; message = payload.get('detail', ...)` — if `e.args[0]` is a string or tuple (not a dict), this raises another `AttributeError` inside the except block.
  - **Fix:** Guard with `payload = e.args[0] if e.args else {}; message = payload.get('detail', str(e)) if isinstance(payload, dict) else str(e)`.

- [ ] 🟡 **Audit service swallows all exceptions silently**
  - **Where:** `apps/vs_user/services/audit.py` ~line 93
  - **Problem:** Failed audit writes are only logged locally — no alerting. If the DB is down, you lose audit trail with no visibility.
  - **Fix:** Add a Sentry/monitoring hook or at minimum a `logger.critical(...)` call for persistent failures.

---

## SECTION 11 — SETTINGS / CONFIGURATION

- [ ] 🔴 **`CORS_ALLOW_ALL_ORIGINS = True` in base settings**
  - **Where:** `apps/settings/base.py` ~line 97
  - **Problem:** Any origin can call the API — including attacker-controlled sites. This should never be True in production.
  - **Fix:** Set `CORS_ALLOW_ALL_ORIGINS = False` in base and add `CORS_ALLOWED_ORIGINS = [...]` in `production.py`.

- [ ] 🔴 **`SECRET_KEY`, `RENDER_API_KEY`, `TEMP_PASSWORD_PEPPER` visible in source code**
  - **Where:** `apps/settings/base.py` ~lines 15–26
  - **Problem:** Hardcoded secrets are in git history and accessible to anyone with repo access.
  - **Fix:** Use `os.environ.get('SECRET_KEY')` with no fallback in production, and rotate all exposed secrets immediately.

- [ ] 🟠 **No rate limiting on login, password reset, and activation endpoints**
  - **Where:** `apps/settings/base.py` (missing) + `apps/vs_user/views.py`
  - **Problem:** No throttle classes configured — brute force attacks on login/password reset are unrestricted.
  - **Fix:** Add `AnonRateThrottle` / `UserRateThrottle` in `REST_FRAMEWORK` settings and apply to sensitive views:
    ```python
    'DEFAULT_THROTTLE_RATES': {
        'login': '5/minute',
        'password_reset': '3/minute',
    }
    ```

- [ ] 🟡 **Missing security headers in settings**
  - **Where:** `apps/settings/base.py`
  - **Problem:** `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS` are not set.
  - **Fix:** Add these to `production.py`:
    ```python
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    ```

---

## SECTION 12 — MISC / AUDIT

- [ ] 🟡 **Soft-delete (deactivate) has no audit trail of who did it or when**
  - **Where:** `apps/vs_user/views.py` ~line 514
  - **Problem:** `UserStatusService.deactivate()` changes status but doesn't record the acting admin or timestamp.
  - **Fix:** Pass `performed_by=request.user` and log to `AuditLog` with action type `USER_DEACTIVATED`.

- [ ] 🟡 **`AuthAttempt.user` is nullable — failed logins can't be linked to a specific user**
  - **Where:** `apps/vs_user/models.py` ~line 364
  - **Problem:** If user is not found, the attempt record has no user FK — brute-force detection against a specific account is blind.
  - **Fix:** Store the attempted email (hashed or plaintext depending on policy) in a separate `attempted_identifier` field.

- [ ] 🟡 **`invited_by` ForeignKey loses audit trail if the inviting admin is deleted**
  - **Where:** `apps/vs_user/models.py` ~line 150
  - **Problem:** `on_delete` behaviour (likely CASCADE or SET_NULL) wipes the record of who sent the invite.
  - **Fix:** Denormalise — also store `invited_by_name = CharField(...)` at invite time so the audit trail survives deletion.

---

## QUICK STATS

| Severity | Count |
|----------|-------|
| 🔴 Critical | 10 |
| 🟠 High | 16 |
| 🟡 Medium | 11 |
| **Total** | **37** |

---

*This document was auto-generated by codebase analysis on 2026-04-26. Recheck after each fix pass.*
