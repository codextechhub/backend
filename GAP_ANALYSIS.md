# Backend Gap Analysis — Checklist
> Last updated: 2026-04-28 · Codebase: Django REST Framework Backend
> Replaces previous version dated 2026-04-26.

---

## HOW TO USE THIS DOCUMENT
Each item has:
- **Where** — file path and approximate line number
- **Problem** — what breaks, when, and what error the frontend will see
- **Fix** — concrete code or one-line action

Priority legend: 🔴 Critical (crash / security / data loss) · 🟠 High (broken logic / bad UX) · 🟡 Medium (edge case / inconsistency) · 🟢 Low (polish)

---

## 0 — STATUS OF PREVIOUSLY-LOGGED ITEMS

Re-verified against current code. Items already fixed are removed; remaining ones move into the sections below.

| Previous item | Status |
|---|---|
| `PasswordResetPreviewView` `.last()` null deref (views.py:346) | ✅ Fixed (null check at line 352) |
| `UserUpdateSerializer.validate()` dead email check | ✅ Fixed (serializer body cleaned up, line 207) |
| `e.args[0]` parsed as dict without guard (PasswordResetConfirmView) | ✅ Fixed (`isinstance(payload, dict)` guards at line 393) |
| Throttling missing on login/reset/activation | ✅ Fixed (commit b502903) |
| `SECRET_KEY` hardcoded fallback | ✅ Fixed (commit 37bc094) |
| Staging security hardening | ✅ Fixed (commit e251dda) |
| `CORS_ALLOW_CREDENTIALS` typo | ✅ Fixed (commit 2e4c126) |
| `invited_by_name` denormalised | ✅ Fixed (commit 2618014) |
| Everything else from previous doc | ⚠️ Still open — see sections below |

---

## 1 — NULL SAFETY / CRASH RISKS (🔴 frontend gets 500)

- [ ] 🔴 **`User.objects.get()` followed by `if not user:` dead code**
  - **Where:** `apps/vs_user/views.py` ~line 379
  - **Problem:** `.get()` raises `DoesNotExist`; never returns `None`. The `if not user:` guard is unreachable, so missing-user path crashes the request → 500.
  - **Fix:** Wrap in `try/except User.DoesNotExist` and return a 404 error response.

- [ ] 🔴 **`School.objects.filter(slug=…).first()` then `.status` accessed unconditionally**
  - **Where:** `apps/vs_schools/views/branch.py` ~lines 140–143
  - **Problem:** `.first()` returns `None` for unknown slug → `AttributeError: 'NoneType' object has no attribute 'status'` → 500 to frontend.
  - **Fix:** `if not school: raise NotFound("School not found.")` before any attribute access.

- [ ] 🟠 **Auth service uses `.first()` user, then accesses `user.user_type` / `user.school`**
  - **Where:** `apps/vs_user/services/auth.py` ~line 46
  - **Problem:** Wrong email returns `None`; next line crashes → 500 instead of clean 401.
  - **Fix:** Early `if not user: return unauthenticated_response()` guard.

- [ ] 🟠 **`_handle_failed_attempt()` school null dereference**
  - **Where:** `apps/vs_user/services/auth.py` ~line 180
  - **Fix:** Guard with `if not school: return` before touching attributes.

- [ ] 🟠 **`PasswordResetRequest.expires_at < timezone.now()` assumes both aware**
  - **Where:** `apps/vs_user/views.py:355`
  - **Problem:** If `USE_TZ` toggles between environments, this comparison silently lets expired tokens through.
  - **Fix:** Add a defensive `if timezone.is_naive(reset_request.expires_at): reset_request.expires_at = timezone.make_aware(...)` or assert `USE_TZ=True` at startup.

- [ ] 🟡 **`request.school` is a `SimpleLazyObject` — exception fires inside view, not middleware**
  - **Where:** `apps/vs_rbac/middleware.py` ~lines 83–89
  - **Problem:** A `PermissionDenied` raised by the lazy resolver is raised inside view code; depending on DRF exception handler completeness, the frontend may see a non-standard error envelope.
  - **Fix:** Resolve eagerly: `school = _get_school_from_request(request); request.school = school`.

---

## 2 — INPUT VALIDATION GAPS (🟠 frontend gets 500 or accepts garbage)

- [ ] 🔴 **`FileField` on import upload has no size or content-type cap**
  - **Where:** `apps/vs_import_data/serializers.py` ~line 605
  - **Problem:** Frontend can upload a multi-GB file; backend stores it and can blow up disk or memory before validation.
  - **Fix:** Add `validate_file()`:
    ```python
    def validate_file(self, f):
        if f.size > 50 * 1024 * 1024:
            raise ValidationError("File exceeds 50MB.")
        if not f.name.lower().endswith(('.csv', '.xlsx')):
            raise ValidationError("Only .csv or .xlsx files allowed.")
        return f
    ```

- [ ] 🟠 **Filename not sanitised before persistence**
  - **Where:** `apps/vs_import_data/serializers.py` ~line 653
  - **Problem:** `uploaded_file.name` may contain `../` or null bytes; later code that uses `original_filename` for paths or display can break.
  - **Fix:** `import os; safe = os.path.basename(uploaded_file.name); if not re.fullmatch(r"[A-Za-z0-9_.\- ]+", safe): raise ValidationError(...)`

- [ ] 🟠 **`gender` is a free-text `CharField` — no choices enforced**
  - **Where:** `apps/vs_user/serializers.py:108` and `apps/vs_user/models.py` ~line 100
  - **Problem:** Accepts any string. Frontend dropdown sends `"M"`, but a malicious or buggy client can store `"🦄"`. Reports/filters break.
  - **Fix:** Replace with `serializers.ChoiceField(choices=[...])` matching the model's choices, or add `choices=` to the model field and migrate.

- [ ] 🟠 **`phone` field has no format validation**
  - **Where:** `apps/vs_user/serializers.py:110`
  - **Problem:** `CharField(max_length=32)` accepts anything. SMS / formatting downstream will misbehave.
  - **Fix:** `from django.core.validators import RegexValidator; phone = serializers.CharField(max_length=32, required=False, allow_blank=True, validators=[RegexValidator(r"^\+?[0-9 ()\-]{7,32}$")])`

- [ ] 🟠 **`role` field on `UserCreateSerializer` is a CharField that the view treats as a UUID**
  - **Where:** `apps/vs_user/serializers.py:114, 180–198`
  - **Problem:** Declared as `CharField(max_length=50)` but the validate logic does `RoleTemplate.objects.get(id=role_id)` — passing a non-UUID string raises `ValidationError` from the DB layer or `ValueError` (caught? no), and it crashes with 500 if the field is something like `"admin"`.
  - **Fix:** Change to `serializers.UUIDField(required=False, allow_null=True)` or wrap the lookup in `try/except (ValueError, ValidationError)`.

- [ ] 🟡 **Search query parameter on `UserAccountViewSet` not length-bounded**
  - **Where:** `apps/vs_user/views.py` ~lines 488–493
  - **Problem:** A 100KB `?search=` value causes a slow `icontains` scan on three columns. Potential DoS surface.
  - **Fix:** Reject early: `if len(search) > 64: return error_response("Search too long.", status=400)`.

- [ ] 🟡 **`ImportTemplateColumn.allowed_values` JSON has no schema**
  - **Where:** `apps/vs_import_data/models.py` (column model)
  - **Problem:** Validators downstream call `value in allowed_values` etc.; malformed JSON causes 500.
  - **Fix:** Add a `clean()` validator on the model or `validate_allowed_values()` on the serializer.

- [ ] 🟡 **No validation on query params for date filters**
  - **Where:** Multiple list views (`AuthAttemptViewSet`, `AuditLogViewSet`)
  - **Problem:** `?created_after=foo` raises `ValueError` and returns 500 instead of 400.
  - **Fix:** Use `django_filters` `DateTimeFilter` or coerce/validate manually before the queryset filter.

---

## 3 — PERMISSIONS / ACCESS CONTROL (🔴 security)

- [ ] 🔴 **`UserAccountViewSet` RBAC permission is commented out**
  - **Where:** `apps/vs_user/views.py:506`
  - **Problem:** `# self.rbac_permission = action_permissions.get(...)` — any authenticated user can list, create, modify, delete user accounts.
  - **Fix:** Uncomment and wire actual action→permission mapping.

- [ ] 🔴 **`AdminPasswordResetView` RBAC commented out**
  - **Where:** `apps/vs_user/views.py:408`
  - **Fix:** Uncomment `rbac_permission = "identity.user_password.reset"`.

- [ ] 🔴 **`IsAuthenticatedStaff` is a stub (`pass`)**
  - **Where:** `apps/vs_import_data/views.py` ~line 59
  - **Problem:** Inherits only `IsAuthenticated`; any logged-in user can run bulk imports.
  - **Fix:** Implement `has_permission` to check `user_type in ('VISION_STAFF', 'SCHOOL_ADMIN')` and that user owns the target school.

- [ ] 🔴 **Empty-list RBAC permission silently grants access**
  - **Where:** `apps/vs_rbac/permissions.py` ~line 155
  - **Problem:** `if rbac_perms and rbac_perms != "" and rbac_perms != []:` — if a view sets `rbac_permission = []`, the entire check is skipped.
  - **Fix:** Treat empty list as misconfiguration:
    ```python
    if rbac_perms is None or rbac_perms == "":
        return True  # explicit no-permission-required
    if isinstance(rbac_perms, list) and not rbac_perms:
        raise ImproperlyConfigured("rbac_permission cannot be an empty list.")
    ```

- [ ] 🟠 **TODO RBAC wiring on 12+ user-management endpoints**
  - **Where:** `apps/vs_user/views.py` lines ~239, 282, 405, 531, 579, 610, 641, 679, 707, 760, 812, 852
  - **Affected:** `InvitationResendView`, `PasswordChangeView`, `UserEmailChangeView`, `UserSuspendView`, `UserReactivateView`, `UserUnlockView`, `SessionViewSet`, `AuthAttemptViewSet`, `AccountLockoutViewSet`
  - **Fix:** Set `permission_classes = [IsAuthenticatedAndActive, HasRBACPermission]` and a real `rbac_permission` on each.

- [ ] 🟠 **Import endpoints don't verify the requesting user belongs to the target school**
  - **Where:** `apps/vs_import_data/views.py` ~line 79
  - **Problem:** `school_id` from URL trusted blindly; a school admin can change the URL and import into another school.
  - **Fix:** `get_object_or_404(School, id=school_id, admins=request.user)` (or equivalent ownership predicate).

- [ ] 🟠 **`AccountLockoutViewSet` queryset not tenant-scoped**
  - **Where:** `apps/vs_user/views.py` ~lines 809–830
  - **Problem:** A school admin may see lockouts from other schools.
  - **Fix:** `if user.user_type != 'VISION_STAFF': qs = qs.filter(user__school=user.school)`.

- [ ] 🟡 **Permission denials emit DRF default message instead of contextual reason**
  - **Where:** `apps/vs_rbac/permissions.py` ~lines 62–72
  - **Problem:** Locked/suspended users see generic "You do not have permission to perform this action." instead of a useful reason.
  - **Fix:** `raise PermissionDenied("Your account is suspended. Contact your admin.")` from `has_permission`.

---

## 4 — AUTH / SESSION FLOW

- [ ] 🔴 **Logout blacklists ALL tokens regardless of submitted token validity**
  - **Where:** `apps/vs_user/views.py` ~line 111
  - **Problem:** A bogus refresh token still triggers `blacklist_all_user_tokens(request.user)` — an attacker who steals an access token can mass-invalidate the victim's sessions.
  - **Fix:** Validate the submitted refresh token first; only run the bulk blacklist if it parses and belongs to the user.

- [ ] 🟠 **Logout is not atomic — token blacklist and session-end can diverge**
  - **Where:** `apps/vs_user/views.py` ~lines 118–124
  - **Fix:** Wrap both in `transaction.atomic()` and use bulk update.

- [ ] 🟠 **Token-refresh errors all collapse into 401 with same message**
  - **Where:** Token refresh path
  - **Problem:** Frontend can't distinguish expired vs. revoked vs. malformed token, so it can't tell the user to log in again vs. retry.
  - **Fix:** Map `TokenError` subclasses to distinct error codes (`TOKEN_EXPIRED`, `TOKEN_REVOKED`, `TOKEN_INVALID`).

- [ ] 🟠 **Auth lockout race — `.filter().first()` then mutate is non-atomic**
  - **Where:** `apps/vs_user/services/auth.py` ~line 25
  - **Fix:** `AccountLockout.objects.select_for_update().filter(user=user).first()` inside `transaction.atomic()`.

- [ ] 🟠 **Invitation `get_or_create` then `reset()` is a TOCTOU**
  - **Where:** `apps/vs_user/services/invitation.py` ~line 41
  - **Fix:** Use `select_for_update()` or a conditional `update()` to atomicise the read-then-write.

- [ ] 🟠 **`activation_key` rotation can fail mid-save, leaving the old link valid**
  - **Where:** `apps/vs_user/services/password.py` ~line 97
  - **Fix:** `with transaction.atomic(): user.activation_key = uuid.uuid4(); user.save(); reset_request.used_at = now(); reset_request.save()`.

---

## 5 — MODEL CONSTRAINTS / DATA INTEGRITY

- [ ] 🟠 **`branch` nullable at DB level but business logic requires it for `BRANCH_ADMIN`**
  - **Where:** `apps/vs_user/models.py` ~line 114
  - **Fix:**
    ```python
    constraints = [
        CheckConstraint(
            check=~Q(user_type='BRANCH_ADMIN', branch__isnull=True),
            name='branch_admin_requires_branch',
        ),
    ]
    ```

- [ ] 🟠 **`User.role` is a free-text `CharField` — no FK, no integrity**
  - **Where:** `apps/vs_user/models.py` ~line 131
  - **Problem:** Any string can land in this field; RBAC resolution on stale role names silently denies / over-grants.
  - **Fix:** Migrate to `ForeignKey(RoleTemplate, on_delete=PROTECT, null=True)` or add a `clean()` validator that proves the string exists in `RoleTemplate`.

- [ ] 🟠 **No constraint that only one active `PasswordResetRequest` per user**
  - **Where:** `apps/vs_user/models.py` (PasswordResetRequest)
  - **Problem:** Concurrent reset requests leave multiple `used_at IS NULL` rows; `.last()` is non-deterministic.
  - **Fix:** `UniqueConstraint(fields=['user'], condition=Q(used_at__isnull=True), name='one_active_reset_per_user')`.

- [ ] 🟡 **`is_active=True` ↔ `status=ACTIVE` not enforced**
  - **Where:** `apps/vs_user/models.py` ~line 140
  - **Fix:** Override `save()` to sync, or add a `CheckConstraint`.

- [ ] 🟡 **No `db_index` on hot-path columns**
  - **Where:** `apps/vs_user/models.py` (LoginSession.is_active, AuthAttempt.email_entered, AuditLog.created_at)
  - **Fix:** Add `db_index=True` or composite `Index(fields=[...])` in `Meta.indexes`.

- [ ] 🟡 **`AuthAttempt.user` nullable — failed-login analytics blind to specific accounts**
  - **Where:** `apps/vs_user/models.py` ~line 364
  - **Fix:** Already store an `attempted_identifier` (email) field separately so brute-force detection works even before user resolution.

- [ ] 🟡 **`ImportBatch.file` not deleted from storage when row is deleted**
  - **Where:** `apps/vs_import_data/models.py` (ImportBatch)
  - **Fix:** Override `delete()` or wire a `post_delete` signal to call `instance.file.delete(save=False)`.

---

## 6 — EXCEPTION HANDLING / FRONTEND ERROR UX

- [ ] 🟠 **Multiple views still parse `e.args[0]` as dict without `isinstance` guard**
  - **Where:** `apps/vs_user/views.py` lines ~263 (ActivationView), ~424 (AdminPasswordResetView), ~558 (UserEmailChangeView), ~621 (UserReactivateView), ~650 (UserUnlockView)
  - **Problem:** If a service raises `ValueError("string")`, then `payload.get(...)` raises `AttributeError` inside the except → 500.
  - **Fix:** Standardise to:
    ```python
    payload = e.args[0] if e.args else {}
    if isinstance(payload, dict):
        message = payload.get('detail', 'Operation failed.')
    else:
        message = str(payload)
        payload = {'detail': message}
    ```
    (PasswordResetConfirmView at line 392 already has this pattern — copy it.)

- [ ] 🟠 **`IntegrityError` on unique-constraint violation returns 500**
  - **Where:** Most create/update endpoints
  - **Problem:** Frontend sees "An error occurred" instead of "Email already in use."
  - **Fix:** Add to `apps/core/exceptions.py` custom handler:
    ```python
    if isinstance(exc, IntegrityError):
        return Response({'success': False, 'message': 'A record with these details already exists.', 'error': {'code': 'DUPLICATE'}}, status=400)
    ```

- [ ] 🟠 **Custom exception handler returns `None` on non-DRF exceptions → Django 500 page**
  - **Where:** `apps/core/exceptions.py` ~lines 9–36
  - **Problem:** A bare `Exception` falls through and the frontend gets HTML 500, not JSON.
  - **Fix:** Final fallback at end of handler:
    ```python
    if response is None:
        logger.exception("Unhandled exception in API")
        return Response({'success': False, 'message': 'Internal error.', 'error': {'code': 'INTERNAL'}}, status=500)
    ```

- [ ] 🟡 **`DjangoValidationError` `args[0]` is a list, not a dict**
  - **Where:** `apps/vs_user/views.py:261-265` (ActivationView)
  - **Fix:** Special-case it: `if isinstance(exc, DjangoValidationError): message = '; '.join(exc.messages)`

- [ ] 🟡 **Audit service swallows all exceptions silently**
  - **Where:** `apps/vs_user/services/audit.py` ~line 93
  - **Fix:** At least `logger.critical(...)` so loss of audit trail is visible to ops.

---

## 7 — PERFORMANCE / N+1

- [ ] 🟠 **Session logout loops with per-row `.save()`**
  - **Where:** `apps/vs_user/services/auth.py` ~lines 117–124, mirrored in `views.py` ~723
  - **Fix:** `LoginSession.objects.filter(user=user, is_active=True).update(is_active=False, ended_at=now(), end_reason='LOGOUT')`.

- [ ] 🟡 **`SessionViewSet`, `AuthAttemptViewSet`, `AccountLockoutViewSet` have no pagination**
  - **Where:** `apps/vs_user/views.py` ~lines 689, 770, 823
  - **Fix:** `pagination_class = StandardResultsSetPagination` (or your project's default).

- [ ] 🟡 **`UserAccountViewSet` queryset missing `prefetch_related`**
  - **Where:** `apps/vs_user/views.py` ~line 474
  - **Fix:** Add `.prefetch_related('sessions', 'role_assignments')` if those reverse relations are touched in the serializer.

- [ ] 🟡 **`AuthAttemptViewSet` likely N+1 on `attempt.user.school.name`**
  - **Where:** `apps/vs_user/views.py` ~line 770
  - **Fix:** `.select_related('user__school')` on the queryset.

- [ ] 🟡 **`PAGE_SIZE = 10` is too small for admin list pages**
  - **Where:** `apps/apps/settings/base.py` ~line 40
  - **Fix:** Bump to 25–50 or per-view custom pagination.

---

## 8 — EMAIL / CELERY TASKS

- [ ] 🟠 **Invitation email sent synchronously — blocks request and rolls back user creation if email fails**
  - **Where:** `apps/vs_user/services/user.py` ~line 69
  - **Fix:** `send_invitation_email_task.delay(str(user.activation_key))`.

- [ ] 🟠 **No retry on transient email errors**
  - **Where:** `apps/vs_user/tasks.py` ~lines 65, 116, 128
  - **Fix:**
    ```python
    @shared_task(bind=True, max_retries=3, default_retry_delay=60)
    def send_invitation_email_task(self, activation_key):
        try:
            ...
        except (smtplib.SMTPException, socket.timeout) as exc:
            raise self.retry(exc=exc)
    ```

- [ ] 🟡 **`FRONTEND_BASE_URL` not declared in settings**
  - **Where:** `apps/vs_user/tasks.py` ~line 116
  - **Problem:** If unset, `getattr(settings, "FRONTEND_BASE_URL")` raises `ImproperlyConfigured` and the task silently fails.
  - **Fix:** Add `FRONTEND_BASE_URL = config("FRONTEND_BASE_URL", default="http://localhost:3000")` in `base.py`.

---

## 9 — SETTINGS / CONFIGURATION

- [ ] 🔴 **`CORS_ALLOW_ALL_ORIGINS = True` hardcoded in base settings**
  - **Where:** `apps/apps/settings/base.py:102`
  - **Problem:** Combined with `CORS_ALLOW_CREDENTIALS = True` this is a CSRF / data-exfil hole — any origin can call the API with the user's cookies.
  - **Fix:**
    ```python
    CORS_ALLOW_ALL_ORIGINS = config("CORS_ALLOW_ALL_ORIGINS", default=False, cast=bool)
    CORS_ALLOWED_ORIGINS = [o.strip() for o in config("CORS_ALLOWED_ORIGINS", default="").split(",") if o.strip()]
    ```

- [ ] 🟠 **`ALLOWED_HOSTS` not asserted in production settings**
  - **Where:** `apps/apps/settings/staging.py`, no `production.py` visible
  - **Fix:** Make sure `ALLOWED_HOSTS` is a non-empty list driven by env in every non-local environment, and add `assert ALLOWED_HOSTS, "ALLOWED_HOSTS required"` at module load.

- [ ] 🟡 **`DATETIME_FORMAT` / `DATE_FORMAT` not pinned in REST_FRAMEWORK**
  - **Where:** `apps/apps/settings/base.py`
  - **Problem:** Frontend parsing breaks if Django defaults change between versions.
  - **Fix:**
    ```python
    REST_FRAMEWORK['DATETIME_FORMAT'] = '%Y-%m-%dT%H:%M:%S.%fZ'
    REST_FRAMEWORK['DATE_FORMAT'] = '%Y-%m-%d'
    ```

- [ ] 🟡 **No `SECURE_HSTS_SECONDS`, `SESSION_COOKIE_SAMESITE` in base**
  - **Where:** `apps/apps/settings/base.py`
  - **Fix:** Set sane defaults: `SESSION_COOKIE_SAMESITE = "Lax"`, `CSRF_COOKIE_SAMESITE = "Lax"`. Production: `SECURE_HSTS_SECONDS = 31536000`.

---

## 10 — MIDDLEWARE / TENANCY

- [ ] 🟠 **`TenantContextMiddleware` runs school resolution for `AnonymousUser`**
  - **Where:** `apps/vs_rbac/middleware.py` ~lines 78–98
  - **Problem:** Wastes a query and can mask logic errors when an anonymous request reaches a view that assumes `request.school` exists.
  - **Fix:** `if not request.user.is_authenticated: request.school = None; return`.

- [ ] 🟡 **Tenant boundary checks live in middleware; raise `PermissionDenied` outside DRF lifecycle**
  - **Where:** `apps/vs_rbac/middleware.py` ~lines 132–134
  - **Problem:** Errors don't go through DRF's exception handler so the response envelope (`{"success": false, ...}`) isn't applied.
  - **Fix:** Move boundary enforcement to a DRF permission class.

---

## 11 — API CONTRACT CONSISTENCY (frontend integration pain)

- [ ] 🟠 **Mixed status codes for create endpoints — sometimes 200, sometimes 201**
  - **Where:** `apps/vs_user/views.py` (custom views) vs DRF `ModelViewSet.create`
  - **Fix:** Custom create paths must `return success_response(..., status=status.HTTP_201_CREATED)`.

- [ ] 🟠 **Inconsistent list response shape — some wrapped in envelope, some bare DRF pagination**
  - **Where:** Various viewsets vs custom `APIView`s
  - **Problem:** Frontend must branch on which endpoint returned which shape.
  - **Fix:** Pick one. Recommended: paginated list inside `success_response`:
    ```python
    paginator = self.paginator
    page = paginator.paginate_queryset(qs, request)
    return success_response(data={'count': paginator.page.paginator.count, 'next': paginator.get_next_link(), 'previous': paginator.get_previous_link(), 'results': serializer.data})
    ```

- [ ] 🟠 **`error_response()` callers don't pass a stable error `code`**
  - **Where:** `apps/core/response.py` (definition), called everywhere
  - **Problem:** Frontend has to regex on the `message` string to distinguish "user locked" from "wrong password".
  - **Fix:** Make `code` mandatory on `error_response` and update callers:
    ```python
    error_response(code="ACCOUNT_LOCKED", message="...", status=423)
    ```

- [ ] 🟡 **Some serializer fields appear/disappear instead of always returning `null`**
  - **Where:** `apps/vs_user/serializers.py` (several `default=None` fields)
  - **Fix:** Always include the key. Use `allow_null=True` consistently and avoid `required=False` without `default`.

- [ ] 🟢 **List endpoints with empty results return 200 — document this for frontend**
  - **Note:** This is correct REST. Add to API docs so frontend doesn't treat it as an error.

---

## 12 — VS_IMPORT_DATA SPECIFICS

- [ ] 🟠 **CSV/Excel parsing memory-loads entire file**
  - **Where:** `apps/vs_import_data/services/`
  - **Problem:** A 100k-row spreadsheet OOMs the worker before validation.
  - **Fix:** Use `openpyxl` read-only mode for `.xlsx`, `csv.DictReader` for `.csv`, and stream row by row.

- [ ] 🟠 **Row-level errors abort the entire batch**
  - **Where:** Import services
  - **Problem:** One bad row in 1000 rejects all of them; frontend has to ask the user to start over.
  - **Fix:** Validate per-row, collect errors, commit valid rows in chunks, return a per-row error report.

- [ ] 🟡 **Encoding assumed to be UTF-8**
  - **Where:** Import services
  - **Problem:** Files exported from Excel on Windows are often `cp1252` or BOM-prefixed UTF-8 → `UnicodeDecodeError` → 500.
  - **Fix:** Try `utf-8-sig`, then `cp1252`, fall back to a clear validation error.

- [ ] 🟡 **`template.columns` accessed without checking template is non-null**
  - **Where:** `apps/vs_import_data/views.py` ~line 272
  - **Fix:** Guard with `if not batch.template: raise ValidationError("Batch missing template.")`.

---

## 13 — MISC

- [ ] 🟡 **Soft-delete (deactivate) writes no audit trail**
  - **Where:** `apps/vs_user/views.py` ~line 514 (`UserStatusService.deactivate()`)
  - **Fix:** Pass `performed_by=request.user`; write an `AuditLog` entry with action `USER_DEACTIVATED`.

- [ ] 🟡 **No `__str__` audit on new models**
  - **Where:** Various models in `vs_audit`, `vs_import_data`
  - **Problem:** Django admin renders `<ModelName object (uuid)>` which is unusable.
  - **Fix:** Add a `__str__` returning the most human-readable identifier.

- [ ] 🟡 **Default `Meta.ordering` missing on list-heavy models**
  - **Where:** `LoginSession`, `AuthAttempt`, `AuditLog`
  - **Problem:** Pagination can repeat rows or skip them across pages because Postgres ordering is unstable without an `ORDER BY`.
  - **Fix:** `class Meta: ordering = ['-created_at']`.

---

## QUICK STATS

| Severity | Count |
|---|---|
| 🔴 Critical | 9 |
| 🟠 High | 26 |
| 🟡 Medium | 23 |
| 🟢 Low | 1 |
| **Total** | **59** |

---

## SUGGESTED FIX ORDER

1. **Day 1 — security gates that protect everything else**
   - § 9 CORS_ALLOW_ALL_ORIGINS
   - § 3 RBAC wiring (UserAccountViewSet, AdminPasswordResetView, IsAuthenticatedStaff, empty-list bypass)
   - § 4 logout token validation

2. **Day 2 — crash class (every one of these is a 500 on a normal-looking request)**
   - § 1 null-deref crashes
   - § 6 `e.args[0]` parsing + IntegrityError handler + non-DRF exception fallback

3. **Day 3 — input validation & uploads**
   - § 2 file size / filename / phone / role / gender
   - § 12 import streaming, encoding, partial-row errors

4. **Day 4 — model integrity & atomicity**
   - § 5 constraints + indexes
   - § 4 lockout / invitation / activation atomicity

5. **Day 5 — frontend contract polish**
   - § 11 status codes, response envelope, error codes
   - § 9 datetime format
   - § 7 pagination & N+1

---

*Re-run this audit after each fix pass. Items marked ⚠️ in §0 are still open from the prior version of this document.*
