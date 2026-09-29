# school_settings

A school's own settings: its sign-in security values, whether it runs payroll
centrally or per branch, the time zone its calendar runs in, and which of its
profile fields stop being its own once it has gone live.

The values themselves live in the configuration engine (`vs_config`), and the
console has screens for them. None of those screens is reachable by a school:
every `config.*` permission is platform-only (vs_rbac migration
`0008_config_is_platform_only`), so `/v1/config/security-settings/` and
`/v1/config/values/` answer 403 to every school admin, and that is intended. This
slice is the school's own door to the values that are genuinely the school's
to set, gated on the school's own keys.

Routes covered by this slice, mounted at `/v1/i/` (`apps/urls.py`):
`me/settings/security/`, `me/settings/payroll-scope/`, `me/settings/display/`,
and the go-live lock on `me/profile/`.

---

## 1. What it is (and what it is NOT)

- **The tenant is always `request.tenant`.** There is no slug, pk or `?tenant=`
  of another school to change. The auth layer refuses a school asserting a
  foreign tenant with a 404, and these views do not opt into
  `platform_cross_tenant_param`, so a platform operator reaches a school here
  only by impersonating one, and is then that school for the request.
- **A caller that is not a school gets a 404.** A platform caller acting as
  itself resolves to the platform tenant, which has no school profile. Without
  that check the security form would write the platform baseline every school
  inherits.
- **Live schools only.** No settings view here declares
  `pending_tenant_surface`, so a PENDING school is refused with
  `403 TENANT_NOT_LIVE`, the same as its notification settings. None of these
  settings is part of onboarding.
- **The rules are the engine's, not a copy.** Security saves through
  `vs_config.services.curated_settings.save_security_settings`, the function the
  console uses. Payroll scope writes through `vs_config.services.resolution.set_value`,
  so finance's write guard (`vs_finance.payroll.guard_payroll_scope`) runs
  exactly as it does for the console. Audit is the engine's too: every change is
  a `config.value.updated` or `config.value.cleared` `ConfigurationAuditEvent`,
  mirrored into `vs_audit`.

## 2. Endpoint map

| Route | Verb | `rbac_permission` | Reads |
|---|---|---|---|
| `/v1/i/me/settings/security/` | GET | `school.settings.view` | `?branch=<id>` (optional) |
| `/v1/i/me/settings/security/` | PATCH | `school.settings.update` | `?branch=<id>` (optional), body below |
| `/v1/i/me/settings/payroll-scope/` | GET | `school.settings.view` | none |
| `/v1/i/me/settings/payroll-scope/` | PATCH | `school.settings.update` | `scope`, `reason` |
| `/v1/i/me/settings/display/` | GET | `school.settings.view` | `?branch=<id>` (optional) |
| `/v1/i/me/settings/display/` | PATCH | `school.settings.update` | `?branch=<id>` (optional); `timezone`, `date_format`, `clock`, `reason` |
| `/v1/i/me/settings/display/` | DELETE | `school.settings.update` | `?branch=<id>` (required) |
| `/v1/i/me/profile/` | PATCH | `school.profile.update` | refuses `currency` / `term_structure` changes once live |

Both keys are TENANT-scoped and seeded in
`core/management/commands/seed_school_permissions.py`: `view` for `school_admin`
and `branch_admin`, `update` (SENSITIVE) for `school_admin` only. So a branch
admin can see every screen here and change none of them.

## 3. Security settings

### GET

Exactly the body of the console's `GET /v1/config/security-settings/` for the
same scope (it is `vs_config.runtime_settings.resolve_security_settings`),
wrapped in the success envelope:

```json
{
  "success": true,
  "message": "Security settings retrieved.",
  "data": {
    "settings":      {"failed_login_threshold": 4, "account_lock_minutes": 15, "self_reset_expiry_hours": 1,
                      "admin_reset_expiry_hours": 24, "invitation_expiry_days": 7, "proxy_idle_timeout_minutes": 30},
    "configured":    {"failed_login_threshold": 4, "...": "same six keys, the value as stored"},
    "sources":       {"failed_login_threshold": "database", "account_lock_minutes": "default", "...": "..."},
    "source_scopes": {"failed_login_threshold": "school", "account_lock_minutes": "default", "...": "..."},
    "overrides":     {"failed_login_threshold": true, "account_lock_minutes": false, "...": "..."},
    "compliance": {
      "failed_login_threshold": {"direction": "maximum", "min": 3, "max": 20,
                                 "boundary": 5, "parent_scope": "platform", "clamped": false},
      "...": "one entry per field"
    },
    "scope": {"type": "school", "tenant": "<tenant uuid>", "branch": null}
  }
}
```

- `settings` is the **enforced** value; `configured` is what is stored. They
  differ when a stored value has become weaker than a parent that tightened
  since, and `compliance.<field>.clamped` is then `true`.
- `source_scopes` is `default`, `platform`, `school` or `branch`: the layer the
  value came from. `overrides.<field>` is `true` when that layer is the one
  being read.
- `compliance.<field>.boundary` is the parent's effective value, and
  `direction` says which way is stricter: `maximum` means lower is stricter
  (thresholds and expiries), `minimum` means higher is stricter
  (`account_lock_minutes`).
- With `?branch=<id>`, `scope.type` is `branch`, `scope.branch` is the id, and
  each field's `parent_scope` is `school`.

### PATCH

Any subset of the six fields, plus an optional `reason`:

```json
{"failed_login_threshold": 4, "account_lock_minutes": null, "reason": "Tighten sign-in"}
```

- A number sets this layer's value. `null` removes it, so the field falls back
  to the parent: the school to the platform, a branch to the school.
- Serializer bounds: `failed_login_threshold` 3-20, `account_lock_minutes`
  5-1440, `self_reset_expiry_hours` 1-24, `admin_reset_expiry_hours` 1-168,
  `invitation_expiry_days` 1-30, `proxy_idle_timeout_minutes` 5-120.
- A value weaker than the parent is refused, on its own field, and nothing from
  the same body is saved:

```json
{
  "success": false,
  "message": "Must be 5 or lower to meet the parent security baseline.",
  "error": {"code": "REQUEST_ERROR",
            "detail": {"failed_login_threshold": ["Must be 5 or lower to meet the parent security baseline."]}}
}
```

- A body with none of the six fields is a 400 ("Provide at least one security
  setting.").
- Success answers `"Security settings saved."` with the refreshed GET body for
  the same scope.
- `reason` defaults to "Updated from the school's security settings".

### `?branch=`

The same two rules as the console, from `resolve_request_scope`: the branch must
belong to this school, and it must be one the caller can see. A branch admin
pinned to Ikeja may read Ikeja and not Lekki. Every refusal is the same
`404 "Configuration scope not found."`, whether the id is unknown, malformed,
another school's, or simply not the caller's.

## 4. Payroll scope

A school-level setting only (`payroll.scope` allows no branch value), so
`?branch=` is not read.

### GET

```json
{
  "success": true,
  "message": "Payroll scope retrieved.",
  "data": {
    "scope": "CENTRAL",
    "source": "default",
    "options": [
      {"value": "CENTRAL", "label": "One payroll for the whole school",
       "description": "A single payroll run pays every member of staff, whichever branch they work at."},
      {"value": "PER_BRANCH", "label": "Each branch runs its own payroll",
       "description": "Each branch pays its own staff in a separate run, so every active member of staff must be assigned to a branch before you switch."}
    ]
  }
}
```

`source` is `school` when this school has chosen, `platform` for a platform
value (the definition does not allow one today), and `default` when nobody has
and the definition's CENTRAL applies.

### PATCH

```json
{"scope": "PER_BRANCH", "reason": "Branches pay their own staff"}
```

`scope` is `CENTRAL` or `PER_BRANCH`; anything else is a 400 keyed on `scope`.
Success answers `"Payroll scope saved."` with the refreshed GET body, and writes
one `config.value.updated` audit event with the tenant, the actor, the reason
and the before/after value.

**A refused switch is a 400 keyed on `scope`.** Finance refuses PER_BRANCH while
any active employee has no branch, and names up to ten of them in its sentence
(followed by "and others" past ten). The guard gives the names only inside that
sentence; it provides no structured list.

```json
{
  "success": false,
  "message": "Per-branch payroll needs every active employee on a branch, and these are not on one yet: Chioma Nwosu, Tunde Bello. Assign them a branch, then switch.",
  "error": {
    "code": "INVALID_CONFIGURATION_VALUE",
    "detail": {"scope": ["Per-branch payroll needs every active employee on a branch, and these are not on one yet: Chioma Nwosu, Tunde Bello. Assign them a branch, then switch."]}
  }
}
```

`error.code` is the refusing `ConfigurationError`'s own code:
`INVALID_CONFIGURATION_VALUE` for the guard and for a definition-level refusal,
`INVALID_CONFIGURATION_SCOPE` if the definition ever stops allowing a school
value. Nothing is written and nothing is audited on a refusal. The console
receives the same refusal as a 422 with `error.detail` `{"key": "payroll.scope"}`;
this endpoint answers 400 so a form can show it on the field.

Switching back to CENTRAL is never refused.

## 5. Display: time zones, date format and clock

Three settings, one screen:

| Key | Values | Scope | Read through |
|---|---|---|---|
| `display.timezone` | any IANA zone, default `Africa/Lagos` | platform, school, **branch** | `vs_config.clock` (`docs/config/config_tenant_clock.md`) |
| `display.date_format` | `D_MMM_YYYY` ("29 Sep 2026", default), `DD_MM_YYYY` ("29/09/2026"), `YYYY_MM_DD` ("2026-09-29") | platform, school | `vs_config.display` |
| `display.clock` | `H12` ("8:00 am", default), `H24` ("08:00") | platform, school | `vs_config.display` |

The time zone decides which calendar day "today" is: when a fee falls overdue,
which day the calendar hub highlights, the date an invoice raised just after
midnight carries. The school's zone is the default; a branch in another zone
(a Nairobi branch of a Lagos school) may keep its own, and everything that
belongs to that branch (its students, the staff posted there, its invoices,
its stock movements) is then judged on the branch's day. There is no
month-first date format on purpose: 03/04/2026 would mean two different days
to two readers.

Every signed-in member receives the effective values in the login and `/me`
tenant block (section 5.4), with no settings key. Server-rendered documents
(invoices, receipts, PDFs, emails, exports) do not read the format or the clock
yet.

Live schools only, for all three values and for a branch's zone alike, like the
rest of this slice. A school being onboarded has no settings screen, and it
reads the defaults from its tenant block until it goes live.

### 5.1 GET

Without `?branch=`:

```json
{
  "success": true,
  "message": "Display settings retrieved.",
  "data": {
    "timezone": "Africa/Lagos",
    "source": "default",
    "options": [
      {"value": "Africa/Abidjan", "label": "Abidjan (Greenwich Mean Time, UTC+0)"},
      {"value": "Africa/Lagos", "label": "Lagos (West Africa Time, UTC+1)"},
      {"value": "Africa/Nairobi", "label": "Nairobi (East Africa Time, UTC+3)"},
      "... twenty in all, west to east"
    ],
    "date_format": "D_MMM_YYYY",
    "date_format_options": [
      {"value": "D_MMM_YYYY", "label": "29 Sep 2026"},
      {"value": "DD_MM_YYYY", "label": "29/09/2026"},
      {"value": "YYYY_MM_DD", "label": "2026-09-29"}
    ],
    "clock": "H12",
    "clock_options": [
      {"value": "H12", "label": "12-hour (8:00 am, 2:30 pm)"},
      {"value": "H24", "label": "24-hour (08:00, 14:30)"}
    ],
    "branches": [
      {"id": 11, "name": "Ikeja Branch", "timezone": "Africa/Lagos", "source": "school"},
      {"id": 12, "name": "Nairobi Branch", "timezone": "Africa/Nairobi", "source": "branch"}
    ]
  }
}
```

- `source` is `school` when this school has chosen its zone, `platform` when a
  platform value applies, and `default` when neither is set and the
  definition's Africa/Lagos applies.
- `options` is a short suggested list: the common West, Central, East and
  Southern African zones, Ghana and the UK. A zone outside it is offered first,
  labelled with its IANA name (`{"value": "America/New_York", "label":
  "America/New York"}`), so a select can show the current value.
- `date_format_options` labels are the school's own today written each way.
- `branches` lists the branches the reader can see (a branch-bound reader sees
  only theirs), the main branch first then by name, each with the zone it keeps:
  `source` `branch` for its own, `school` when it follows the school. A school
  with one branch lists none, because that branch's zone is the school's.

With `?branch=<id>`:

```json
{
  "branch": {"id": 12, "name": "Nairobi Branch"},
  "timezone": "Africa/Nairobi",
  "source": "branch",
  "options": ["... as above, the branch's zone first when outside the list"]
}
```

`source` is `branch` or `school`. The branch must be this school's and one the
reader can see, or the answer is a 404 ("No such branch at this school."),
whether it is unknown, another school's, or outside the reader's reach.

### 5.2 PATCH

Without `?branch=`, any of the three (at least one) and an optional reason:

```json
{"timezone": "Africa/Accra", "date_format": "DD_MM_YYYY", "clock": "H24",
 "reason": "Moving to Ghana"}
```

Needs a caller whose reach is the whole school. Answers `"Display settings
saved."` with the refreshed GET body. Each value changed is one
`config.value.updated` audit event with the value before and after; the
reason defaults to "Updated from the school's display settings". Every value
or none is written.

With `?branch=<id>`, the branch's own zone:

```json
{"timezone": "Africa/Nairobi", "reason": "The branch is in Nairobi"}
```

Needs that branch in the caller's reach (a branch administrator holding
`school.settings.update` may set their own branch's zone). Answers "<Branch
name> keeps its own time zone." with the branch GET body, audited as
`config.value.updated` on the branch scope (reason defaults to "Updated from
<Branch name>'s display settings").

### 5.3 DELETE

`DELETE ?branch=<id>` removes the branch's own zone, so the branch follows the
school's again. Answers "<Branch name> follows the school's time zone again."
with the branch GET body, audited as `config.value.cleared`. Removing a zone
the branch never had answers the same and audits nothing.

### 5.4 The tenant block every member receives

The login response and `GET /v1/user/auth/me/` carry, in `tenant`:

```json
"display": {"time_zone": "Africa/Lagos", "date_format": "D_MMM_YYYY",
            "clock": "H12", "branch_zones": {"12": "Africa/Nairobi"}}
```

`branch_zones` lists only the branches with a zone of their own (id to zone),
for the whole school rather than the reader's reach: a zone is no secret, and a
shared record from another branch needs that branch's day. The platform tenant
carries the platform values and no branch zones. Built by
`vs_config.display.display_preferences`, through `vs_tenants.context.tenant_context_block`,
so login and `/me` cannot drift.

### 5.5 Refusals

| Case | Status | Detail |
|---|---|---|
| Caller without `school.settings.view` (GET) or `.update` (PATCH, DELETE) | 403 | the permission refusal |
| School not live | 403 `TENANT_NOT_LIVE` | |
| Not a school (a platform caller as itself) | 404 | "This tenant has no school profile." |
| School-level PATCH by a branch-bound caller | 403 `SHARED_RECORD_READ_ONLY` | "Only a school-wide administrator can change the school's display settings. Choose one of your branches to set its own time zone." |
| Branch unknown, another school's, or outside the caller's reach | 404 | "No such branch at this school." |
| Not a zone | 400 on `timezone` | "'Lagos' is not a recognised time zone. Use an IANA name such as Africa/Lagos." |
| Date format outside the choices | 400 on `date_format` | "'MM_DD_YYYY' is not a date format this school can use. Choose D_MMM_YYYY, DD_MM_YYYY or YYYY_MM_DD." |
| Clock outside the choices | 400 on `clock` | "'H13' is not a clock this school can use. Choose H12 or H24." |
| Nothing to change | 400 on `non_field_errors` | "Send a time zone, a date format or a clock to change." |
| Date format or clock sent with `?branch=` | 400 on the field | "The date format and the clock are the school's, not a branch's. Save them without choosing a branch." |
| Branch zone at a school with one branch | 400 on `branch` | "<School name> has one branch, so its time zone is the school's. Change the school's time zone instead." |
| DELETE without `?branch=` | 400 on `branch` | "Name the branch whose own time zone to remove. The school's time zone is changed, never removed." |

Nothing is written and nothing is audited on a refusal. A new zone applies
from the next read in any request, including later in the same one.

## 6. The profile lock at go-live

`PATCH /v1/i/me/profile/` refuses changes to `currency` and `term_structure`
once the school has been live. "Has been live" is `School.has_ever_been_live()`,
the same test that freezes the slug: `activated_at` is set, or the status is
ACTIVE. It is written once at go-live, so a suspended school stays locked.

```json
{
  "success": false,
  "message": "currency: This is fixed once the school is live. Contact XVS to change it.; term_structure: This is fixed once the school is live. Contact XVS to change it.",
  "error": {
    "code": "REQUEST_ERROR",
    "detail": {
      "currency": ["This is fixed once the school is live. Contact XVS to change it."],
      "term_structure": ["This is fixed once the school is live. Contact XVS to change it."]
    }
  }
}
```

- Only a field whose value would change is refused. A form that posts the
  stored `currency` back alongside a real change saves normally.
- Every other field on the endpoint stays the school's to change after go-live.
- `editable_fields` on `GET /v1/i/me/profile/` drops `currency` and
  `term_structure` for a school that has been live, so a screen can render them
  read-only without waiting for the 400.
- A pending school is unaffected: both fields stay editable, which the
  onboarding step "Complete your school profile" depends on.
- **The platform route is untouched.** `PATCH /v1/i/<slug>/update/`
  (`platform.schools.update`, CodeX only) still changes both on a live school,
  because moving a school's currency or calendar is a data change CodeX runs, not
  a form field.

## 7. Code map

| File | What lives there |
|---|---|
| `schools/vs_schools/views/settings.py` | `SchoolSettingsView` (keys, school-tenant check), `SchoolSecuritySettingsView`, `SchoolPayrollScopeView`, `PAYROLL_SCOPE_OPTIONS` |
| `schools/vs_schools/serializers.py` | `PayrollScopeUpdateSerializer`; `SchoolProfileUpdateSerializer.LOCKED_ONCE_LIVE` and its `validate`; `SchoolProfileSerializer.get_editable_fields` |
| `schools/vs_schools/views/display.py` | `SchoolDisplaySettingsView`, `DisplaySettingsUpdateSerializer`, `TIME_ZONE_OPTIONS` |
| `schools/vs_schools/urls.py` | The `me/settings/` routes |
| `vs_config/clock.py` | `tenant_zone`, `tenant_now`, `tenant_today`, the time zone write guard |
| `vs_config/services/curated_settings.py` | `save_curated_values`, `save_security_settings`, shared with the console |
| `vs_config/runtime_settings.py` | `resolve_security_settings`, `validate_security_compliance`, `SECURITY_COMPLIANCE` |
| `vs_config/services/scopes.py` | `resolve_request_scope`, the two `?branch=` rules |
| `vs_finance/payroll.py` | `PAYROLL_SCOPE_*`, `guard_payroll_scope`, `assert_roster_fully_assigned` |

## 8. Test coverage & gaps

- `schools/vs_schools/tests_settings_endpoints.py`, on two live schools (Bright
  Star with Ikeja and Lekki, Green Field with one branch) and one pending:
  - security: a teacher refused both verbs; a branch admin reads but cannot
    write, and cannot read a branch they are not posted to; one school's write
    never moves another's values; another school's `?branch=` and `?tenant=`
    are 404s; a pending school gets `TENANT_NOT_LIVE`; a platform caller acting
    as itself cannot reach the platform baseline; the console endpoint stays
    403 to a school admin and a `config.security.*` grant to a school role is
    refused; the body equals the console's; tighten (audited), loosen refused,
    a refused field saves nothing from the same form, `null` resets, an empty
    form refused; a branch override on the two-branch school; the single-branch
    school's own values;
  - payroll: teacher refused both verbs; branch admin reads only; one school's
    switch never moves another; pending refused; the default and its options;
    a switch with before/after audit; a single-branch school switching and
    back; the guard refusal's exact 400 shape; an unknown scope; a platform
    value reads as `platform`; a console write reads back here.
- `schools/vs_schools/tests_display_settings.py`, on the same three schools:
  a teacher refused both verbs; a branch admin reads but cannot write; one
  school's save never moves another's zone; another school's `?tenant=` is a
  404 on both verbs; a pending school gets `TENANT_NOT_LIVE` on both verbs; a
  platform caller acting as itself cannot move the platform zone; a new school
  reads Lagos from the default with the suggested options; a save answers with
  the refreshed body and is audited; names that are not zones are refused on
  the field with nothing written; any real zone is accepted and offered back
  first; a platform value reads as `platform`; the single-branch school sets
  its own zone.
- `schools/vs_schools/tests_profile_endpoint.py` `LiveSchoolProfileEndpointTests`:
  a live school reads its profile; `editable_fields` drops the two; the address
  still saves; the two are refused by field; sending the stored value back
  saves; a suspended school stays locked; CodeX's platform route still changes
  both.

Not covered: impersonation (a platform operator proxied into a school) is not
exercised against these two endpoints; it behaves as the impersonated school
through the same `request.tenant` binding every other `me/` view uses.
