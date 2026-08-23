# rbac_change_requests_overrides

The two ways a tenant changes access without editing a role directly: the
four-state approval queue for role permission edits, and the per-user exception
layer that hands one person a key their role does not carry (or takes one away
their role does).

Roles and assignments are `rbac_roles_assignments`; the registry the keys come
from is `rbac_permission_registry`; how an override is folded into the answer at
request time is `rbac_evaluation_scoping`.

Routes covered by this slice, mounted at `/v1/rbac/` (`apps/urls.py:27`):
`tenants/<tenant_slug>/role-change-requests/`,
`tenants/<tenant_slug>/role-change-requests/approval/`,
`tenants/<tenant_slug>/role-change-requests/<id>/`,
`tenants/<tenant_slug>/role-change-requests/<request_id>/decide/`,
`tenants/<tenant_slug>/users/<user_id>/permission-overrides/`,
`tenants/<tenant_slug>/users/<user_id>/permission-overrides/<id>/`.

Findings for the whole module are collected in
**`error/rbac/rbac_code_issues.md`**; §8 points at the ones belonging here.

---

## 1. What it is (and what it is NOT)

- **The change request is tenant-internal, not a CX escalation.** Both the
  requester and the reviewer are inside the tenant, and the tenant boundary comes
  from `TenantRoleChangeRequest.tenant` with the target role pinned to the same
  tenant (`models.py:946-951`, `1018-1023`).
- **It is optional, not a gate.** Nothing forces a role edit through it. The
  same person can PATCH `roles/<key>/` directly and change the same grants with
  no request and no reviewer - both routes take `ROLE_UPDATE_KEYS`
  (`views.py:568-569`, `885-889`). The queue is a convention, and a tenant that
  ignores it loses nothing.
- **A request is a *delta*, not a target set.** `TenantRoleChangeDeltaItem` rows
  say ADD `x` / REMOVE `y`; the final set is computed at approval time by
  replaying the delta over whatever the role holds then
  (`services.py:132-149`). Two requests approved in sequence therefore compose;
  two approved against a role someone edited in between compose against the new
  state, not the state the requester saw.
- **Approval is destructive to deny rows.** Applying a request deletes *every*
  `TenantRolePermission` on the role and rebuilds only the granted ones
  (`services.py:159-171`), so an explicit `granted=False` deny is silently
  converted to "absent".
- **`APPLY_FAILED` is terminal.** Only a `PENDING` request may be decided
  (`views.py:996-1000`), so a request that failed to apply cannot be retried,
  edited or reopened - even when the cause was a fixable dependency error.
- **An override is an exception pinned to one person inside one tenant**, and
  there are exactly two of them per key at most, because
  `uq_user_permission_override` is on `(user, permission)`: a new override
  **replaces** the old one rather than stacking (`models.py:891-898`).
- **A personal DENY beats everything**, including a personal ALLOW and every
  role grant (`models.py:833-838`, `evaluator.py:216`).
- **There is deliberately no approval workflow on overrides** (owner decision,
  rev 2). Accountability comes from the required `reason`, the `RBACAuditLog`
  trail, and the fact that the `*.overrides.manage` key is CRITICAL and
  restricted (`models.py:842-845`).
- **Expiry is lazy.** An expired override simply stops matching the evaluator's
  filter, so nothing has to sweep it (`models.py:838-840`,
  `evaluator.py:113-124`). It stays in the table and stays visible in the list
  with `is_expired: true`.
- **There is no self-service view of your own exceptions.** Reading them
  requires the viewer's `.view` or `.manage` key even for your own id, so a user
  without it cannot learn that exceptions exist on their account - they only
  observe permissions working or not working (`views.py:1075-1080`). Nothing
  about overrides is on `/me` or any profile serializer.
- **Nobody may override themselves.** `_reject_self` checks *both* the actor and
  the effective user, so an impersonator cannot use a proxy session to edit
  their own or the proxied user's access (`views.py:1106-1120`).
- **Which namespace gates the override endpoints is decided by the actor's home
  tenant, never by the target's** (`views.py:1050-1069`).

## 2. Domain model

### `TenantRoleChangeRequest` (`models.py:946`)

| Field | Meaning |
|---|---|
| `tenant` | FK, PROTECT. The boundary |
| `requested_by` | FK, PROTECT |
| `target_role` | FK to `TenantRoleTemplate`, PROTECT |
| `status` | `PENDING` / `APPROVED` / `DENIED` / `APPLY_FAILED` |
| `justification` | Required, non-blank (`models.py:1022-1023`) |
| `reviewer`, `reviewer_notes` | Outcome metadata |
| `submitted_at` | Defaults to now |
| `decided_at` | Set by the three `mark_*` helpers |
| `impact_summary` | `JSONField(default=dict)`. **Written by nothing** |

`clean()` refuses a target role from another tenant and a blank justification
(`models.py:1018-1023`); the serializer performs the same two checks, and
`clean()` itself is never called on the API path.

Three transition helpers mutate in memory without saving - every caller saves
with an explicit `update_fields` list: `mark_denied`, `mark_approved`,
`mark_apply_failed` (`models.py:1025-1041`).

Indexes: `(tenant, status, submitted_at)` and `(status, submitted_at)`.

### `TenantRoleChangeDeltaItem` (`models.py:1044`)

`request` (CASCADE) + `permission` (PROTECT, `to_field="key"`) + `operation`
(`ADD` / `REMOVE`), unique on all three
(`uq_tenant_request_permission_operation`). A request may therefore carry both
an ADD and a REMOVE of the same key, which the replay resolves by order.

### `UserPermissionOverride` (`models.py:823`)

| Field | Meaning |
|---|---|
| `tenant` | FK, PROTECT. Owns both the override and the user |
| `user` | FK, CASCADE |
| `permission` | FK, PROTECT, `to_field="key"` - so `permission_id` **is** the dotted key |
| `mode` | `ALLOW` / `DENY` |
| `reason` | Required, non-blank |
| `created_by` | FK, SET_NULL |
| `expires_at` | Nullable; `null` means permanent |

Constraint: `uq_user_permission_override` on `(user, permission)`. Note it does
**not** include `tenant` - one row per user per key, platform-wide.

Indexes: `(tenant, user, expires_at)` - the evaluator's hot path - and
`(permission, mode)`. Ordered `-created_at`.

`is_expired` is a property: `expires_at is not None and expires_at <= now`
(`models.py:910-912`).

`assert_scope_allowed` (`models.py:916-925`) applies `assert_tenant_may_hold` to
an ALLOW only. A DENY is exempt, because removing a key cannot escalate anyone
and a school may want a pre-emptive deny on record. The docstring names this as
"the path the escalation used": the serializer's queryset offers every active
key, and tenant membership used to be the only thing checked.

`clean()` (`models.py:927-936`) additionally requires the user to belong to the
override's tenant and the reason to be non-blank.

## 3. Endpoint map

All six routes sit under `TenantScopedRBACMixin` (see `rbac_roles_assignments`
§3) and take `IsAuthenticatedAndActive` + `HasRBACPermission`.

### Change requests

| Route | Verb | Keys | Body / filters actually read |
|---|---|---|---|
| `role-change-requests/` | GET | `ROLE_VIEW_KEYS` | `?status=`, `?target_role=`; ordered `-submitted_at`; paginated |
| | POST | `ROLE_UPDATE_KEYS` | `target_role`, `justification`, `delta_items[]` of `{permission_key, operation}` |
| `role-change-requests/approval/` | GET | `ROLE_VIEW_KEYS` | `?status=`, `?target_role=`. Byte-identical to the list route above |
| `role-change-requests/<id>/` | GET | `ROLE_VIEW_KEYS` | |
| `role-change-requests/<request_id>/decide/` | POST | `ROLE_UPDATE_KEYS` | `action` (`APPROVE` / `DENY`, upper-cased), `notes` |

`impact_summary` and `status` are read-only on the serializer
(`serializers/tenant.py:825-835`), so a requester cannot pre-set either.
`delta_items` is required to be non-empty (`serializers/tenant.py:850-853`).

The decide endpoint's outcomes (`views.py:981-1044`):

| Condition | Response |
|---|---|
| Request not found in this tenant | 404 "Request not found." |
| `status != PENDING` | 409 `"Request already decided (<status>)."` |
| `DENY` with no `notes` | 400 "Denial reason is required." |
| `DENY` with notes | 200, status `DENIED` |
| `APPROVE` and apply succeeds | 200, status `APPROVED` |
| `APPROVE` and apply raises | 500, status `APPLY_FAILED`, `reviewer_notes = str(exc)` |
| Anything else | 400 "Invalid action. Must be APPROVE or DENY." |

### Overrides

`_UserPermissionOverrideBase` (`views.py:1072-1149`) sets
`platform_cross_tenant_param = True`, which lets a CX actor manage a school
user's overrides by asserting `?tenant=<school-slug>`. RBAC still evaluates
against the actor's own tenant (`request.rbac_tenant`), so the *platform* key is
what is required on that call.

`_override_keys(actor)` (`views.py:1055-1069`) picks the namespace from the
actor's home tenant kind:

| Actor's tenant | view key | manage key |
|---|---|---|
| `PLATFORM` | `platform.team_overrides.view` | `platform.team_overrides.manage` |
| anything else | `school.user_overrides.view` | `school.user_overrides.manage` |

The two sets are never unioned - a school role that somehow carried a platform
key still gets no extra reach, because a school actor cannot assert another
tenant at all and a platform actor needs the platform key.

| Route | Verb | Keys | Notes |
|---|---|---|---|
| `users/<user_id>/permission-overrides/` | GET | `[view, manage]` (any-of; managing implies seeing) | `?mode=` (upper-cased); ordered `-created_at`; paginated |
| | POST | `manage` | `permission` (the dotted key), `mode`, `reason`, `expires_at` |
| `users/<user_id>/permission-overrides/<id>/` | DELETE | `manage` | Lifts the override |

There is **no** update route: changing an override means POSTing a new one,
which replaces the old row and audits both halves.

`get_target_user` (`views.py:1091-1104`) resolves the user inside `self.tenant`
and raises the non-enumerating
`NotFound("No user matches the requested context.")` otherwise - a user in
another tenant is indistinguishable from one that does not exist.

Read rows carry `granted_by_role`, computed once per request from the target's
role-only permission set placed in the serializer context
(`views.py:1122-1125`, `serializers/overrides.py:94-103`), so the UI can say
"the role grants this - denied for this user" without a second round trip.

## 4. Lifecycle / state machine

### Change request

```
                     POST role-change-requests/
        (nothing) ─────────────────────────────► PENDING
                                                    │
                        decide {action: DENY}       │       decide {action: APPROVE}
                        notes required              │
                    ┌───────────────────────────────┴────────────────────┐
                    ▼                                                    ▼
                 DENIED                                    apply_role_change_request()
               (terminal)                                       │              │
                                                            succeeds        raises
                                                                │              │
                                                                ▼              ▼
                                                            APPROVED     APPLY_FAILED
                                                            (terminal)    (terminal)
```

All three leaves are terminal, because the decide endpoint refuses anything that
is not `PENDING`. There is no cancel, no withdraw and no edit: a request whose
delta was wrong is abandoned in place and a new one is filed.

### Override

```
   POST permission-overrides/            DELETE .../<id>/
        │                                     │
        ▼                                     ▼
    ALLOW or DENY  ──────────────────────► (row gone; role access restored)
        │
        │ POST again for the same key
        ▼
    old row audited OVERRIDE_LIFTED, deleted; new row audited OVERRIDE_CREATED
        │
        │ expires_at passes
        ▼
    row still present, is_expired = true, matched by nothing in the evaluator
```

Both create and lift take effect on the target's **next request** - the
effective set is memoised per request on the user instance
(`evaluator.py:210-221`), and `request.user` is rebuilt every time.

## 5. Derivations

### Replaying a delta

`services.apply_role_change_request` (`services.py:115-198`), inside one
`transaction.atomic`:

```python
current_keys = {granted keys on target_role}          # before_keys, sorted
for item in obj.delta_items:
    ADD    -> current_keys.add(item.permission_id)
    REMOVE -> current_keys.discard(item.permission_id)
final_keys = sorted(current_keys)
```

Then, in order:

1. `validate_role_permissions(final_keys, attached_group_ids)` - the dependency
   check runs against the **flattened** set, so a prerequisite supplied through
   an attached `PermissionGroup` counts (`services.py:150-156`).
2. `TenantRolePermission.objects.filter(role=target_role).delete()` - every row,
   grants and denies alike.
3. `bulk_create` one granted row per key in `final_keys`, through
   `ScopeGuardedManager`, so a platform key still cannot land in a school role.
4. `version += 1`.
5. `mark_approved` and save.
6. One `RBACAuditLog` PERMISSION_CHANGED row carrying
   `before_data = {"permission_keys": before_keys}` and
   `diff_data = {"permission_keys": {"before": …, "after": …}}`
   (`services.py:183-198`).

Because step 1 raises `ValidationError` for a missing prerequisite and the view
catches every `Exception` (`views.py:1024`), a dependency mistake produces a
500, a terminal `APPLY_FAILED`, and a `reviewer_notes` string of
`str(ValidationError)` - which renders as `{'permission_keys': ['Permission …
requires: …']}`. See `rbac_code_issues` §20.

### The order of authority

`evaluator.get_effective_permissions` (`evaluator.py:181-222`):

```
effective = (role_granted - role_denied) | user_allows - user_denies
```

Later wins. Reading it left to right:

| Layer | Source |
|---|---|
| `role_granted` | `TenantRolePermission(granted=True)` on live roles, plus every key in attached groups |
| `role_denied` | `TenantRolePermission(granted=False)` on those roles |
| `user_allows` | `UserPermissionOverride(mode=ALLOW)`, unexpired, this tenant |
| `user_denies` | `UserPermissionOverride(mode=DENY)`, unexpired, this tenant |

So a personal DENY beats a role grant, a group grant *and* a personal ALLOW -
though the unique constraint means one key can never carry both for one person.

### Which overrides are in force

`_active_override_qs` (`evaluator.py:113-124`):

```python
UserPermissionOverride.objects.filter(
    Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()),
).filter(tenant=tenant)
```

`get_user_override_keys` adds `user=user` and `_holdable_filter(tenant)`, then
partitions by mode in one pass (`evaluator.py:127-139`). One indexed query on
`(tenant, user, expires_at)`.

Routing honours the same rows: `resolve_users_with_permission` unions the ALLOW
holders in and subtracts the DENY holders, so a person denied the key is never
nominated as an approver while `has_permission` says no
(`evaluator.py:294-303`).

### `granted_by_role`

```python
role_keys = context["role_permission_keys"]     # get_role_permissions(target, tenant)
return obj.permission_id in role_keys
```

`serializers/overrides.py:94-103`. `get_role_permissions` is the roles-only
evaluation with overrides deliberately not applied (`evaluator.py:142-153`); its
docstring says plainly *"Never use it for authorisation."*

### Scope refusal on an ALLOW

Two layers, on purpose (`serializers/overrides.py:122-152`):

- the serializer turns it into a 400 with a field name, naming the tenant the
  override will land in (which only the view knows);
- `UserPermissionOverride.save()` refuses it as well, so a non-HTTP caller hits
  the same wall.

A DENY passes both unconditionally.

## 6. What writing writes

| Action | Rows written | Audit |
|---|---|---|
| `POST role-change-requests/` | 1 `TenantRoleChangeRequest` + N `TenantRoleChangeDeltaItem` | UPDATE, actor `requested_by`, justification in metadata (`signals.py:558-573`) |
| `decide {DENY}` | status, reviewer, notes, `decided_at` | UPDATE, severity WARNING, status DENIED, with a `before/after` status diff (`signals.py:579-595`) |
| `decide {APPROVE}` succeeding | all `TenantRolePermission` for the role deleted and rebuilt; `version += 1`; request marked APPROVED | one PERMISSION_CHANGED written by the service with the full before/after key lists (`services.py:183-198`). The `post_save` receiver writes nothing for APPROVED - only DENIED and APPLY_FAILED are handled (`signals.py:579-613`) |
| `decide {APPROVE}` raising | nothing (the inner atomic rolls back); request marked APPLY_FAILED | UPDATE, severity **CRITICAL**, status FAILED (`signals.py:597-613`) |
| `POST permission-overrides/` replacing nothing | 1 `UserPermissionOverride` | OVERRIDE_CREATED, severity WARNING, metadata carries `school_id`, `tenant_id`, target id and email, key, mode, reason, `expires_at`, `replaced` (`views.py:1127-1149`) |
| `POST permission-overrides/` replacing a row | old row audited then deleted, new row created - both inside one `transaction.atomic` with `select_for_update` on the existing row | OVERRIDE_LIFTED (`replaced: true`) **and** OVERRIDE_CREATED |
| `DELETE permission-overrides/<id>/` | row deleted | OVERRIDE_LIFTED (`views.py:1286-1296`) |

The override views are the **only** writers in `vs_rbac` that put a `school_id`
in the audit metadata (`views.py:1139`), which is what makes
`RBACAuditLog.school_id` non-empty for these rows and empty for essentially
everything else (`audit.py:46`).

Both override paths write the audit row **before** the mutation inside the same
`transaction.atomic`, so a failed audit write rolls the change back with it -
the durability contract `record_rbac_audit` exists for (`audit.py:1-13`).

## 7. Worked example

Corona's Bursar role grants `finance.invoice.view` and `finance.invoice.create`.
Mrs Okafor, the deputy bursar, needs to approve invoices while the bursar is on
maternity leave until 31 October. Mr Nwosu, a bursar who is under investigation,
must stop seeing invoices immediately without disturbing anyone else.

**1. The wrong tool.** Adding `finance.invoice.approve` to the Bursar role gives
it to every bursar, including Nwosu. Minting a "Deputy Bursar" role for one
person for two months, then remembering to delete it, is the churn overrides
exist to avoid (`models.py:826-831`).

**2. Okafor gets a time-boxed ALLOW.**

```http
POST /v1/rbac/tenants/corona/users/58/permission-overrides/?tenant=corona
{ "permission": "finance.invoice.approve",
  "mode": "ALLOW",
  "reason": "Covering for the bursar's maternity leave, approved by the Head.",
  "expires_at": "2026-10-31T23:59:59Z" }
```

The serializer requires the reason, requires the expiry to be in the future
(`serializers/overrides.py:112-115`), and - because Corona is not a platform
tenant - checks that `finance.invoice.approve` is `PermissionScope.TENANT`. It
is. `_reject_self` passes because 58 is not the admin. The row lands, an
OVERRIDE_CREATED audit row lands with it, and Okafor's very next request finds
`finance.invoice.approve` in her effective set.

**3. Nwosu gets a DENY.**

```http
POST /v1/rbac/tenants/corona/users/41/permission-overrides/?tenant=corona
{ "permission": "finance.invoice.view", "mode": "DENY",
  "reason": "Suspended pending audit, ref HR-2026-114." }
```

No expiry, so it is permanent until lifted. His role still grants the key, so
the list shows `granted_by_role: true` next to the DENY - which is the whole
point of that flag. And `resolve_users_with_permission` stops nominating him for
invoice approvals in the same breath, so the workflow engine does not route him
work he will then be refused (`evaluator.py:301-303`).

**4. Somebody tries to be clever.** The admin POSTs an ALLOW of
`platform.audit.manage` for themselves. Two things stop it: `_reject_self`
returns 403 "You cannot create or lift permission overrides on yourself", and
had they targeted someone else, the scope check would refuse it as
*"'platform.audit.manage' is platform-scoped and cannot be granted to a user
inside a tenant."*

**5. Making it permanent, properly.** In November the school decides deputy
bursars should approve invoices as a rule. The admin files a change request:

```http
POST /v1/rbac/tenants/corona/role-change-requests/?tenant=corona
{ "target_role": 7, "justification": "Deputy bursars now approve up to N500,000.",
  "delta_items": [{"permission_key": "finance.invoice.approve", "operation": "ADD"}] }
```

The head of finance reviews it and POSTs `{"action": "APPROVE"}` to
`role-change-requests/12/decide/`. The service snapshots the role's current four
keys, replays the ADD to five, validates that
`finance.invoice.approve`'s dependency on `finance.invoice.view` is satisfied
(it is - it is in the set), wipes and rebuilds the role's permission rows, bumps
`version` from 3 to 4, marks the request APPROVED, and writes one audit row with
both key lists.

**6. What that quietly cost.** The role also carried an explicit deny row -
`granted=False` on `finance.report.export`, added months earlier to stop bursars
exporting. Step 2 of the apply deletes every row on the role, and step 3
recreates only the granted ones. The deny is gone, and nothing in the audit
diff mentions it: `before_keys` was built from `granted=True` rows only
(`services.py:132-137`). Bursars can export again and nobody knows why. This is
`rbac_code_issues` §21.

**7. Had a prerequisite been missing** - say the delta added
`finance.invoice.approve` to a role that did *not* hold `finance.invoice.view` -
step 1 would raise, the view would catch it, return a 500 reading "Approval
failed while applying changes", and mark the request `APPLY_FAILED`. The
reviewer cannot retry, because only PENDING requests can be decided. They must
file a fresh request with both keys in it (`rbac_code_issues` §20).

## 8. Gotchas / known limitations

Recorded in full in **`error/rbac/rbac_code_issues.md`**. The items belonging to
this slice:

| # in that file | One line |
|---|---|
| §20 | A fixable dependency error kills a change request permanently and reports it as a 500 |
| §21 | **Confirmed by execution.** Approving a request deletes the role's explicit deny rows and does not mention them in the diff |
| §22 | A change request may ADD a platform-scoped key: it is accepted, queued, reviewed, and only fails at apply time - as an APPLY_FAILED 500 |
| §23 | The approval queue and the plain list are the same query with the same key, so the "reviewer" screen is not a separate authority |
| §24 | `impact_summary` is on the model, the serializer and the API contract, and is written by nothing |
| §25 | The APPROVED transition writes no `post_save` audit row - only the service's own row covers it, so a direct `mark_approved()` in code would be silent |
| §26 | `uq_user_permission_override` omits `tenant`, so a user who moves tenant carries their override with them |
| §6 | `?target_role=` on both change-request lists is fed straight into `filter(target_role_id=…)`, so a non-numeric value is a 500 |

Design choices worth stating as choices:

- **No approval workflow on overrides** is an explicit owner decision recorded
  in the model docstring (`models.py:842-845`), not an omission.
- **Replace rather than stack** keeps the effective set a simple set operation
  and makes the audit trail read as a sequence of complete states rather than a
  pile of partial ones.
- **No self-service visibility** is deliberate and is stated at
  `views.py:1075-1080`: a user who cannot see their own exceptions cannot infer
  that one exists, which matters when the exception is a DENY.
- **`get_role_permissions` exists only to power `granted_by_role`** and warns in
  its own docstring never to use it for authorisation (`evaluator.py:142-147`).

## 9. Permissions & tenant isolation

- **Change requests reuse the role keys.** List and detail take
  `ROLE_VIEW_KEYS`; create and decide take `ROLE_UPDATE_KEYS`
  (`views.py:885-889`, `919-921`, `950-952`, `977-979`). There is no separate
  "approve a role change" key, so anyone who can file a request can also decide
  one - including their own.
- **Overrides have four dedicated keys, all CRITICAL and all restricted**:
  `school.user_overrides.view` / `.manage`
  (`seed_school_permissions.py:104-105`, seeded to `school_admin`) and
  `platform.team_overrides.view` / `.manage`
  (`seed_platform_permissions.py:77-86`, granted to both codex roles).
  `.view` is CRITICAL on purpose: without that bar, a user could learn that
  exceptions exist on their own account.
- **`platform.team_overrides.*` is `PermissionScope.PLATFORM`** (it is not in
  `TENANT_HOLDABLE_KEYS`), so a school role cannot hold it.
  `school.user_overrides.*` is `TENANT`.
- **Cross-tenant reach is one-way and gated.** Only the override views set
  `platform_cross_tenant_param = True` (`views.py:1086`). A CX actor asserting
  `?tenant=corona` is admitted by `TenantJWTAuthentication` only because of that
  flag and only because their tenant kind is `PLATFORM`
  (`authentication.py:119-128`); RBAC then evaluates against
  `request.rbac_tenant`, which is the **actor's** tenant when there is no
  impersonation (`authentication.py:145`), so the platform key is what is
  required. A school actor never reaches this branch at all.
- **Target resolution is non-enumerating** on both the user
  (`views.py:1091-1104`) and the override row itself, which is filtered by
  `tenant` and `user` as well as pk (`views.py:1274-1279`).
- **Self-editing is refused on both identities** (`views.py:1106-1120`), which
  closes the impersonation route as well as the direct one.
- **Change-request references are tenant-scoped**: `target_role` is a
  `TenantScopedRelatedField` resolving inside the request's tenant
  (`serializers/tenant.py:800-804`), and the fallback check raises the identical
  message so neither path is an oracle.
- **The gap.** Nothing checks that a change request's ADD list is within what
  the requester themselves holds, and nothing separates requester from reviewer.
  A single school admin can file and approve their own request in two calls -
  which is no worse than the direct PATCH they already have, but it means the
  queue provides no additional control.

## 10. Code map

| File | What lives there |
|---|---|
| `vs_rbac/models.py:823-940` | `UserPermissionOverride`, its constraint, `is_expired`, the scope guard |
| `vs_rbac/models.py:946-1041` | `TenantRoleChangeRequest` and its three transition helpers |
| `vs_rbac/models.py:1044-1082` | `TenantRoleChangeDeltaItem` |
| `vs_rbac/views.py:874-960` | Change-request list, approval queue, detail |
| `vs_rbac/views.py:964-1044` | The decide endpoint and its six outcomes |
| `vs_rbac/views.py:1050-1069` | The two override key namespaces and how one is chosen |
| `vs_rbac/views.py:1072-1149` | Target resolution, the self-override ban, the audit helper |
| `vs_rbac/views.py:1153-1252` | Override list and create (with replacement) |
| `vs_rbac/views.py:1256-1298` | Override lift |
| `vs_rbac/serializers/overrides.py` | The override serializer, `granted_by_role`, the ALLOW scope refusal |
| `vs_rbac/serializers/tenant.py:763-874` | Change-request and delta-item serializers |
| `vs_rbac/services.py:115-198` | `apply_role_change_request` |
| `vs_rbac/evaluator.py:113-139` | `_active_override_qs`, `get_user_override_keys` |
| `vs_rbac/evaluator.py:142-153` | `get_role_permissions` (roles only, never for authorisation) |
| `vs_rbac/signals.py:546-613` | Change-request lifecycle audit receivers |
| `vs_rbac/migrations/0003_userpermissionoverride.py` | The override table |

## 11. Test coverage & gaps

Module baseline: **`Ran 326 tests in 89.035s` - OK**.

Covered:

- `tests/test_overrides.py` (536 lines, the largest test file in the app) - both
  modes, the later-wins order, expiry, replacement and its two audit rows, the
  self-override ban on both identities, `granted_by_role`, the ALLOW scope
  refusal, and the platform / school key split.
- `tests/test_services.py` - `apply_role_change_request` happy path, the version
  bump, and the group-flattened dependency check.
- `tests/test_views.py` - change-request creation, the decide endpoint's
  APPROVE and DENY branches, the already-decided 409, the missing-reason 400.
- `tests/test_audit.py` - that the durable row is written first and that a
  central-mirror failure is swallowed (the deliberate `RuntimeError: boom` in
  the suite output).

Not covered:

- **No test approves a request against a role holding an explicit deny row**,
  which is why §21 is invisible.
- No test files a change request adding a platform-scoped key (§22).
- No test asserts an `APPLY_FAILED` request cannot be retried (§20) - the
  terminal-ness is exercised only through the already-decided 409 on an
  APPROVED row.
- `impact_summary` has no test, because it has no writer (§24).
- No test moves a user between tenants with an override on their account (§26).
- The approval queue endpoint has no test distinguishing it from the plain list
  (§23) - correctly, since there is nothing to distinguish.
