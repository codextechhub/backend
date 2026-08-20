# config_platform_runtime_settings

The three curated screens that sit on top of the generic catalogue: **Platform
Settings** (issuer profile plus school-onboarding defaults), **Security
Settings** (authentication controls, with a scoped compliance clamp), and
**Integration Settings** (outbound mail defaults plus secret-free deployment
readiness, with a bounded connection test).

Routes (`urls.py:11-14`):
`platform-settings/`, `security-settings/`, `integration-settings/`,
`integration-settings/test/`.

The storage engine underneath is `config_settings_catalogue`. This slice is
about the product contract: which keys exist, who consumes them, and the rules
that make a scoped override safe.

---

## 1. What it is (and what it is NOT)

- **Curated, not generic.** These screens do not let an administrator invent a
  key. Each field name maps to a fixed configuration key through a dictionary in
  code (`platform_settings.py:14-31`, `runtime_settings.py:16-39`), and the
  serializer bounds the value before the definition's own rules ever run.
- **The keys are product-owned.** A curated key cannot have its schema edited
  (`views.py:204-214`) and cannot be archived (`views.py:230-233`) through the
  generic definition endpoints, and its value cannot be written through the
  generic `POST /values/` route (`views.py:278-289`) or reset through
  `DELETE /values/<key>/` (`views.py:322-327`). Security and integration keys
  have their own `manage` permissions and their own screens; that separation is
  enforced in both directions.
- **Only settings with a real consumer belong here.** The module docstring is
  explicit (`runtime_settings.py:1-6`), and `SETTING_CONSUMERS`
  (`runtime_settings.py:63-169`) names the exact function that reads each key
  and what breaks if it changes. Administrators can read that map; they cannot
  author it.
- **This is not a credentials store.** SMTP hosts, provider secrets, callback
  URLs and the frontend base URL stay deployment-owned. The integrations screen
  reports only readiness booleans and the host name
  (`runtime_settings.py:309-327`).
- **Security settings are the only scoped ones.** Platform and integration
  settings resolve at platform scope unconditionally
  (`platform_settings.py:41-54`, `runtime_settings.py:288`). Security settings
  resolve through the full branch/tenant/platform chain and are the only place
  in the module where a school may legitimately write.
- **A scoped security value is clamped at read time, not just at write time.**
  `resolve_security_settings` returns the *enforced* value under `settings` and
  the *stored* value under `configured` (`runtime_settings.py:222-283`). A
  school override saved while the platform was lax stays in the database when
  the platform later tightens; clamping at read closes that gap for every
  consumer at once.
- **The connection test never sends anything.** Email opens and closes the
  configured backend; payments calls the provider's read-only healthcheck
  (`services/connections.py:27-42`). Neither creates money movement or mail.

## 2. Domain model

No model is owned here. Everything is `ConfigurationDefinition` plus
`ConfigurationValue` (see `config_settings_catalogue` §2). What matters is the
key map.

### Platform Settings (`platform_settings.py:14-31`)

| Field | Key | Type |
|---|---|---|
| `profile.name` | `platform.profile.name` | STRING |
| `profile.tagline` | `platform.profile.tagline` | STRING |
| `profile.address` | `platform.profile.address` | STRING |
| `profile.email` | `platform.profile.email` | STRING |
| `profile.phone` | `platform.profile.phone` | STRING |
| `profile.website` | `platform.profile.website` | STRING |
| `profile.logo_url` | `platform.profile.logo_url` | STRING |
| `onboarding.ownership_type` | `platform.onboarding.default_ownership_type` | CHOICE |
| `onboarding.term_structure` | `platform.onboarding.default_term_structure` | CHOICE |
| `onboarding.currency` | `platform.onboarding.default_currency` | CHOICE |
| `onboarding.branch_country` | `platform.onboarding.default_branch_country` | STRING |

### Security Settings (`runtime_settings.py:16-32`)

| Field | Key | Product default | Bounds | Clamp direction |
|---|---|---|---|---|
| `failed_login_threshold` | `security.failed_login_threshold` | 5 | 3-20 | maximum |
| `account_lock_minutes` | `security.account_lock_minutes` | 15 | 5-1440 | minimum |
| `self_reset_expiry_hours` | `security.self_reset_expiry_hours` | 1 | 1-24 | maximum |
| `admin_reset_expiry_hours` | `security.admin_reset_expiry_hours` | 24 | 1-168 | maximum |
| `invitation_expiry_days` | `security.invitation_expiry_days` | 7 | 1-30 | maximum |
| `proxy_idle_timeout_minutes` | `security.proxy_idle_timeout_minutes` | 30 | 5-120 | maximum |

"maximum" means a child scope may only go **lower**; "minimum" means only
**higher**. Both directions mean the same thing in practice: stricter than the
parent (`runtime_settings.py:52-59`).

### Integration Settings (`runtime_settings.py:34-44`)

| Field | Key | Fallback when unset |
|---|---|---|
| `email_sender_name` | `integrations.email.sender_name` | parsed from `DEFAULT_FROM_EMAIL`, else `"CodeX System"` |
| `email_sender_address` | `integrations.email.sender_address` | parsed from `DEFAULT_FROM_EMAIL` |
| `email_max_retries` | `notifications.email_max_retries` | 3 |
| `email_retry_backoff_seconds` | `notifications.email_retry_backoff_seconds` | 60 |

`SPECIAL_MANAGED_KEYS` is the union of the security and integration keys
(`runtime_settings.py:46`), and `PRODUCT_OWNED_KEYS` is an alias for it
(`:47`). `ALL_FIELDS` covers the platform keys (`platform_settings.py:31`).
Together they are what the generic endpoints refuse to touch.

## 3. Endpoint map

| Method + path | Permission | Platform-only | Scoped |
|---|---|---|---|
| `GET /platform-settings/` | `config.value.view` | yes (`views.py:353`) | no, always platform |
| `PATCH /platform-settings/` | `config.value.update` | yes | no |
| `GET /security-settings/` | `config.security.view` | no | yes |
| `PATCH /security-settings/` | `config.security.manage` | no | yes |
| `GET /integration-settings/` | `config.integration.view` | yes (`views.py:495`) | no |
| `PATCH /integration-settings/` | `config.integration.manage` | yes | no |
| `POST /integration-settings/test/` | `config.integration.manage` | yes (`views.py:519`) | no |

### Request bodies actually read

`PATCH /platform-settings/` (`serializers.py:119-146`):

```jsonc
{"profile": {"name": "CodeX Vision", "email": "hello@codexng.com"},
 "onboarding": {"currency": "NGN"},
 "reason": "Rebrand"}
```

At least one of `profile` or `onboarding` is required. Every profile field
except `name` accepts `""` to clear the stored override; `name` does not, and
that is a defect (`config_code_issues.md` §16).

`PATCH /security-settings/` (`serializers.py:149-173`) takes the six flat
integer fields plus `reason`. `null` clears the override at the current scope
(`views.py:439-444`). At least one field is required.

`PATCH /integration-settings/` (`serializers.py:176-197`) takes the four flat
fields plus `reason`; `null` clears. `email_sender_name` is stripped and a
whitespace-only value is a 400.

`POST /integration-settings/test/` takes only
`{"connection": "email" | "payments"}` (`serializers.py:200-201`).

### Response shapes

`GET /platform-settings/` (`views.py:359-373`):

```jsonc
{"profile": {…}, "onboarding": {…},
 "sources": {"profile": {"name": "database" | "environment" | "default", …},
             "onboarding": {"currency": "database" | "default", …}},
 "options": {"ownership_types": [{"value","label"}, …],
             "term_structures": […], "currencies": […]}}
```

`options` exists so the frontend does not hard-code enumerations that live in
`vs_schools.models`.

`GET /security-settings/` (`runtime_settings.py:237-283`):

```jsonc
{"settings":      {"failed_login_threshold": 3, …},   // enforced
 "configured":    {"failed_login_threshold": 8, …},   // as stored
 "sources":       {"failed_login_threshold": "database" | "default", …},
 "source_scopes": {"failed_login_threshold": "platform"|"school"|"branch"|"default", …},
 "overrides":     {"failed_login_threshold": true, …},  // set at THIS scope
 "compliance":    {"failed_login_threshold": {"direction":"maximum","min":3,"max":20,
                                              "boundary":3,"parent_scope":"platform",
                                              "clamped":true}, …},
 "scope": {"type": "school", "tenant": "…", "branch": null}}
```

`compliance` is present only for a tenant or branch scope; at platform scope it
is an empty object because there is no parent to compare with.

`GET /integration-settings/` (`runtime_settings.py:295-328`):

```jsonc
{"settings": {…}, "sources": {…},
 "status": {"email":    {"configured": true, "host": "smtp.…", "credentials_managed_by": "deployment"},
            "payments": {"provider": "PAYSTACK", "configured": true, "credentials_managed_by": "deployment"},
            "public_application": {"base_url": "https://…", "managed_by": "deployment"}}}
```

No secret appears in that payload. `configured` is a boolean derived from
whether the deployment supplied credentials at all.

## 4. Lifecycle / state machine

There is no lifecycle. Each field is in one of four states, and the response
names which one:

```text
"database"    a ConfigurationValue row exists at some scope in the chain
"environment" no row; a deployment value filled in (platform profile, sender name/address)
"default"     no row and no deployment value; the definition default or the product constant
(cleared)     PATCH with "" (profile) or null (security/integration) deletes the row
              at the current scope, and the layer above becomes effective again
```

For security settings there is a fifth, orthogonal state: **clamped**. The row
still exists and `configured` still shows it, but `settings` reports the parent
baseline because the stored value is weaker.

## 5. Derivations

- **`_scoped_values` fetches one layer set in two queries**
  (`runtime_settings.py:172-196`): one for the definitions, one for every
  candidate row across the whole scope chain, then picks the most specific per
  key in Python. It is the same precedence order as `resolve_value`, computed
  in bulk.

- **The clamp is transitive** (`runtime_settings.py:265-282`). A branch resolves
  its parent by calling `resolve_security_settings(tenant=tenant)`, which itself
  calls `resolve_security_settings()` for the platform layer. So a branch can
  never be weaker than the platform even if the school layer in between is lax:
  the school's own value is already clamped before the branch is compared with
  it. That recursion costs six queries at branch scope, uncached
  (`config_code_issues.md` §11).

- **Write-time validation and read-time clamping are both present, on purpose**
  (`runtime_settings.py:331-341` and `:265-282`). `validate_security_compliance`
  rejects a weakening write with a 400 naming the boundary
  (`views.py:446-451`), so an operator gets told rather than silently ignored.
  The read-time clamp catches the case validation cannot: a value that was legal
  when written and became illegal when the parent tightened.

- **The compliance validator is skipped at platform scope**
  (`runtime_settings.py:334`). The platform layer *is* the baseline, so there is
  nothing above it to compare against; the serializer's own `min_value` /
  `max_value` bounds are the only ceiling there.

- **Clearing does not run the compliance validator**
  (`views.py:439-444`). Removing an override can only ever return the scope to
  its parent, which is by definition compliant.

- **Platform profile falls back to the deployment issuer**
  (`platform_settings.py:60-74`): a stored row wins, then
  `settings.PLATFORM_ISSUER[field]`, then the definition default, then `""`.
  Onboarding falls back to the definition default, then to
  `ONBOARDING_PRODUCT_DEFAULTS` (`platform_settings.py:33-38`, `:76-87`).

- **Integration sender falls back to `DEFAULT_FROM_EMAIL`**, parsed with
  `email.utils.parseaddr` so a `"Name <addr@host>"` setting yields both halves
  (`runtime_settings.py:199-201`).

- **The safe readers never raise.** `get_security_settings`,
  `get_security_value`, `get_integration_settings` and `get_integration_value`
  wrap resolution in `try/except Exception` and fall back to the product
  constants (`runtime_settings.py:348-377`). That matters because these are
  called from the authentication path: a broken configuration table must not
  lock everyone out. Note that the HTTP `GET /security-settings/` calls
  `resolve_security_settings` **directly** and does not get that safety net.

- **The connection test is rate limited per actor per connection**
  (`services/connections.py:21-24`): a 30 second cache slot, claimed with
  `cache.add`. A second attempt inside the window is a 400 telling the operator
  to wait (`views.py:531-534`).

- **Test failures are deliberately opaque to the caller**
  (`views.py:535-544`). Provider and SMTP exceptions can carry endpoints and
  response bodies, so the response says only that the test failed and the
  traceback goes to the server log. The audit event records the outcome, not
  the reason.

## 6. What writing writes

Every PATCH here funnels into `set_value` / `clear_value`, so each changed field
produces its own `config.value.updated` or `config.value.cleared` audit row,
inside the request transaction (`views.py:378`, `472`, `504`).

Consequences worth knowing:

- Saving five security fields writes five audit rows, each with the same
  `reason`. The default reason is `"Updated from Security Settings"` when the
  caller supplies none (`views.py:477`).
- The audit target for an update is the value row; for a clear it is the
  definition. So a field that has been set and later cleared has its history
  under two different target ids (`config_code_issues.md` §15).
- The connection test writes `config.integration.connection_tested` against a
  synthetic `IntegrationConnection` target (`views.py:546-551`,
  `services/connections.py:14-18`), with the result in `metadata`. The audit
  serializer renders that target as `"Email connection"` /
  `"Payments connection"` (`serializers.py:525-526`).

Reads write nothing.

## 7. Worked example

A platform admin tightens the baseline:

```text
PATCH /v1/config/security-settings/
{"failed_login_threshold": 3, "reason": "PCI review"}
```

The caller's tenant is the platform tenant, so `resolve_request_scope` returns
`(None, None)`; the compliance validator returns early; `set_value` writes at
`scope_key = "platform"`.

A school that had previously set 8 now reads:

```text
GET /v1/config/security-settings/?tenant=alpha-nt
```

```json
{ "success": true, "message": "Security settings retrieved.",
  "data": {
    "settings":      { "failed_login_threshold": 3,  "account_lock_minutes": 15, … },
    "configured":    { "failed_login_threshold": 8,  "account_lock_minutes": 15, … },
    "sources":       { "failed_login_threshold": "database", … },
    "source_scopes": { "failed_login_threshold": "school", … },
    "overrides":     { "failed_login_threshold": true, … },
    "compliance":    { "failed_login_threshold": { "direction": "maximum",
                                                   "min": 3, "max": 20,
                                                   "boundary": 3,
                                                   "parent_scope": "platform",
                                                   "clamped": true }, … },
    "scope": { "type": "school", "tenant": "…", "branch": null } } }
```

The school's 8 is still on the row, the screen can still show it, and **3 is
what locks the account**. Tested at `tests.py:641-694`.

If the same school now tries to save 8 again:

```text
PATCH /v1/config/security-settings/?tenant=alpha-nt
{"failed_login_threshold": 8}
```

```json
{ "success": false,
  "message": "An error occurred. Check the error details for more information.",
  "error": { "code": "REQUEST_ERROR",
             "detail": { "failed_login_threshold":
                         ["Must be 3 or lower to meet the parent security baseline."] } } }
```

## 8. Gotchas / known limitations

Full evidence in **`error/config/config_code_issues.md`**. Items belonging to
this slice:

- **`platform.profile.name` cannot be cleared.** Its serializer field is the one
  profile field without `allow_blank=True` (`serializers.py:120`), so the
  clearing branch at `views.py:406` is unreachable for it (§16).
- **Security settings are re-resolved from scratch on every read**, up to three
  levels deep with no caching, and one of the callers is
  `TenantJWTAuthentication` (§11).
- **`GET /security-settings/` has no fail-safe.** It calls
  `resolve_security_settings` directly, so a value the definition no longer
  types as an integer makes `int(value)` raise and the screen 500s, while every
  internal consumer of the same data degrades gracefully
  (`runtime_settings.py:255`, `:348-353`) (§19).
- **The catalogue seed and migration 0006 disagree** about where security
  settings may be written: the seed says `["platform"]`
  (`seed_config_catalogue.py:150`), the migration widened it to
  `["platform","school","branch"]`
  (`migrations/0006_enable_scoped_security_overrides.py:13-18`). A rebuilt
  definition row silently loses scoped overrides (§13).
- **This module imports `vs_schools`** for the onboarding enumerations
  (`views.py:91`, `serializers.py:20`), which is the leak the FAL exists to
  prevent (§14).
- **`PRODUCT_OWNED_KEYS` is an alias for `SPECIAL_MANAGED_KEYS`**
  (`runtime_settings.py:47`), so `views.py:208` and `views.py:230` compute
  `set(ALL_FIELDS.values()) | PRODUCT_OWNED_KEYS` on every request for a set
  that never changes (§19).
- **Justified by design:** integration settings ignore tenant scope entirely.
  Outbound mail identity is a platform property, and the keys are seeded
  platform-only.
- **Justified by design:** the connection test tells the operator nothing about
  *why* it failed (`views.py:535-544`). The traceback is the single place that
  answer lives, and it is server-side.
- **Justified by design:** the platform screen is gated on both GET and PATCH
  (`views.py:353`), even though `config.value.view` is a NORMAL permission. The
  issuer profile is platform identity, not tenant data. Tested at
  `tests.py:452-464`.

## 9. Permissions & tenant isolation

| Surface | Key | Sensitivity | Restricted |
|---|---|---|---|
| Platform settings read/write | `config.value.view` / `config.value.update` | NORMAL / SENSITIVE | no / yes |
| Security read | `config.security.view` | SENSITIVE | yes |
| Security write | `config.security.manage` | **CRITICAL** | yes |
| Integration read | `config.integration.view` | SENSITIVE | yes |
| Integration write + test | `config.integration.manage` | **CRITICAL** | yes |

Seeded at `seed_config_permissions.py:13-14`, granted to `xvs_super_admin` and
`xvs_platform_admin` only (`:16`, `:59-75`).

**The two-key split is real and tested.** A holder of `config.value.update`
cannot save security settings: the generic value route refuses
`SPECIAL_MANAGED_KEYS` outright (`views.py:278-289`, tested at
`tests.py:725-743`), and the security route demands `config.security.manage`
(tested at `tests.py:601-614`). A holder of `config.security.view` cannot write
either.

**Security settings are the one place a school may legitimately write**, and the
compliance clamp is what makes that safe: a school can only ever be stricter
than the platform, and a branch stricter than its school. Both directions are
tested (`tests.py:615-640`, `tests.py:695-724`).

Platform and integration settings are gated on the caller's **home** tenant
being a PLATFORM tenant (`views.py:137-144`), which means an impersonated CX
staffer cannot reach them: during impersonation `request.user` is the effective
school user.

## 10. Code map

| File | Responsibility |
|---|---|
| `platform_settings.py:14-38` | Platform key map and product defaults |
| `platform_settings.py:41-99` | `resolve_platform_settings`, `get_platform_profile`, `get_school_onboarding_defaults` |
| `runtime_settings.py:16-59` | Security and integration key maps, defaults, compliance policy |
| `runtime_settings.py:63-169` | `SETTING_CONSUMERS` - the code-owned ownership map |
| `runtime_settings.py:172-219` | `_scoped_values`, deployment sender, scope labels |
| `runtime_settings.py:222-283` | `resolve_security_settings` - including the transitive clamp |
| `runtime_settings.py:286-328` | `resolve_integration_settings` - including deployment status |
| `runtime_settings.py:331-377` | `validate_security_compliance` and the four fail-safe readers |
| `services/connections.py` | Cooldown slot, email backend probe, provider healthcheck |
| `views.py:352-420` | `PlatformSettingsView` |
| `views.py:423-455` | `_save_curated_values` - shared save/clear/validate loop |
| `views.py:459-490` | `SecuritySettingsView` |
| `views.py:494-563` | `IntegrationSettingsView`, `IntegrationConnectionTestView` |
| `serializers.py:119-201` | The four curated write serializers |

### Who actually reads these settings

| Key group | Consumer | File |
|---|---|---|
| `platform.profile.*` | Finance document issuer block | `vs_finance/documents.py:86` |
| `platform.onboarding.*` | School and branch creation defaults | `vs_schools/serializers.py:448`, `:796` |
| `security.failed_login_threshold`, `security.account_lock_minutes` | Login lockout | `vs_user/services/auth.py:184`, `vs_user/models.py:408` |
| `security.self_reset_expiry_hours`, `security.admin_reset_expiry_hours` | Password reset link lifetime | `vs_user/services/password.py:135` |
| `security.invitation_expiry_days` | Invitation lifetime | `vs_user/services/invitation.py:42` |
| `security.proxy_idle_timeout_minutes` | Idle impersonation expiry | `vs_rbac/authentication.py:41`, `vs_admin_console/services.py:10` |
| `integrations.email.*` | Outbound From header | `core/mail.py:22` |
| `notifications.email_*` | Delivery retry policy | `vs_notifications/tasks.py:220` |

Every one of those consumers imports lazily inside a function, which is what
keeps `vs_config` a low-level module that others may depend on without a cycle.

## 11. Test coverage & gaps

Baseline: **`Ran 61 tests in 94.867s` - OK**.

What this slice covers:

- `PlatformSettingsAPITests` (`tests.py:420-534`) - the environment/product
  fallback chain, a platform request with no `?tenant=` defaulting to the home
  tenant, a school user with `config.value.view` still refused (403), a PATCH
  writing typed values and recording audit, a blank profile value clearing the
  database override, and the product-owned schema/lifecycle guard.
- `RuntimeSettingsAPITests` (`tests.py:573-808`) - the dedicated permission
  split in both directions, a school override that may only be stricter, the
  platform tightening clamping an existing school override, a branch override
  that cannot weaken its school, the generic value route refusing a special
  key, an integration save changing the runtime sender while the status stays
  redacted, `null` resetting to default, and the connection test being
  permission-controlled, safe and audited.

What it does not cover:

1. **`platform.profile.name` clearing.** `test_blank_optional_profile_value_clears_database_override`
   (`tests.py:491-515`) uses an *optional* field, which is precisely why the
   `name` defect survived (issues file §16).
2. **`GET /security-settings/` with a corrupt stored value**, where the safe
   readers degrade but the endpoint 500s.
3. **The connection cooldown.** `test_connection_test_is_permission_controlled_safe_and_audited`
   patches the tester; nothing asserts a second call inside 30 seconds is a 400,
   and nothing asserts the failure branch hides the provider message.
4. **`?branch=` on the security screen from a platform caller**, which resolves
   the tenant from the branch rather than the assertion
   (`services/scopes.py:71-72`).
5. **A three-level clamp in one assertion.** The branch case is tested against
   its school, and the school case against the platform, but not the case the
   docstring claims to solve: branch strict, school lax, platform strict.
6. **`options` in the platform payload**, and the response shape when the
   catalogue has no curated definitions at all (`views.py:400-402` raises a
   validation error naming the missing keys, which nothing asserts).
7. **The seed-versus-migration `allowed_scopes` drift** (issues file §13):
   nothing asserts that a freshly seeded security definition is writable at
   school scope.
