# rbac_evaluation_scoping

The runtime half of the module: how a stored grant becomes a yes or no on an
actual request, how the same grant then decides which rows the caller sees, the
DRF permission classes that ask the question, the tenant context the whole thing
hangs off, the field-level mask, and the durable audit table underneath it all.

This slice has **no endpoints of its own**. Everything here is machinery the
other three slices - and every other app in the repo - stand on. Who owns which
key is `rbac_permission_registry`; who holds it is `rbac_roles_assignments`; the
exception layer is `rbac_change_requests_overrides`.

Findings for the whole module are collected in
**`error/rbac/rbac_code_issues.md`**; §8 points at the ones belonging here.

---

## 1. What it is (and what it is NOT)

- **Two questions, one answer between them.** *Access* ("may this person open
  this screen at all?") is `evaluator.has_permission`; *visibility* ("whose rows
  do they then see?") is `scoping.visible_branch_ids`. Before this module they
  came from unrelated places and they disagreed - a grant of "Bursar at Ikeja"
  conferred nothing, while a whole-tenant grant plus a `User.branch` conferred
  everything and showed one site (`scoping.py:1-17`).
- **`ANY_BRANCH` is not `None`, and the distinction is load-bearing.**
  `ANY_BRANCH` means "the caller named no branch, so do not narrow"; an explicit
  `None` means "the entity as a whole", a real scope that only whole-tenant
  grants reach (`evaluator.py:6-23`, `52-58`). Conflating them is what made a
  branch-pinned grant lock its holder out instead of narrowing them.
- **Branch context is never carried by a header or a query parameter.** It is
  derived from what the caller has actually been granted, by the same function
  that decides whether they may open the screen (`scoping.py:253-259`).
- **A NULL branch means "shared across the tenant", never "no branch yet".**
  `BranchScope`'s default is inclusive, and the exclusive form has to be asked
  for by name, because hiding shared rows from a pinned caller looks like
  missing data rather than a permission error and nobody reports it
  (`scoping.py:146-163`).
- **The evaluator is memoised per request, not cached across requests.** Both
  caches hang off the user instance (`user._rbac_effective_perms`,
  `user._rbac_visible_branches`), and `request.user` is rebuilt every request, so
  they can never go stale (`evaluator.py:210-221`, `scoping.py:119-139`).
- **The Vision super admin bypasses everything.** `HasRBACPermission`,
  `HasAnyModuleAccess` and `FieldSecurityMixin` all short-circuit for
  `is_vision_super_admin` before a single key is evaluated
  (`permissions.py:300-302`, `381-383`, `fls.py:79-82`). That includes the
  `PermissionScope` guard - see `rbac_code_issues` §1.
- **`TenantSurfaceAllowed` is not a permission key, deliberately.** A role grant
  must never be able to reopen the platform to a school that has not gone live
  (`permissions.py:120-122`). Its allowlist is opt-in per view
  (`pending_tenant_surface`) and its absence means closed.
- **DRF's `permission_classes` *replaces* the defaults rather than adding to
  them**, which is why the surface gate is installed in four places at once -
  the defaults, `IsAuthenticatedAndActive`, `HasRBACPermission` and
  `HasAnyModuleAccess` (`permissions.py:123-131`).
- **`TenantAwareManager` is ambient and eager.** It filters in `get_queryset()`,
  so `all()`, `filter()`, `get()`, `exists()` and related lookups through the
  default manager are all scoped without per-call machinery
  (`managers.py:1-8`). CX requests and Celery tasks set no tenant context and
  are therefore **never** filtered - which is correct for platform jobs and is
  the mechanism behind several findings in other modules.
- **There is deliberately no `school` ownership path in the manager.** A
  school-shaped fallback in a domain-neutral engine app is the exact leak the
  FAL exists to prevent, and `tests/test_branch_tenant_boundary.py` fails if any
  model regrows one (`managers.py:39-44`).
- **`RBACAuditLog` is the durable trail, `vs_audit` is a mirror.**
  `emit_audit_event` is best-effort by contract - it swallows failures so it can
  never break business logic - and that is the wrong durability contract for
  permission changes. `RBACAuditLog` is written transactionally with the action
  (`models.py:1090-1100`, `audit.py:1-13`).
- **`RBACAuditLog` is write-only.** It is immutable at the ORM level, and no
  view, serializer, export dataset or management command in the repo reads it.

## 2. Domain model

Only one table lives in this slice.

### `RBACAuditLog` (`models.py:1089`)

| Field | Meaning |
|---|---|
| `action_type` | Free string (40), e.g. `ROLE_ASSIGNED`, `OVERRIDE_CREATED` |
| `severity` | Free string (16), default `INFO` |
| `status` | Free string (16), default `SUCCESS` |
| `actor` | FK to the user, SET_NULL |
| `school_id` | `CharField(80)`, **a loose slug, not an FK** - survives school deletion |
| `entity_type`, `entity_id`, `entity_label` | What was changed |
| `summary` | Human sentence |
| `before_data`, `diff_data`, `metadata` | Nullable `JSONField`s |
| `created_at` | `auto_now_add` |

Immutability is enforced in Python, not in the database:

```python
def save(self, *args, **kwargs):
    if self.pk is not None:
        raise ValidationError("RBACAuditLog entries are immutable.")
    super().save(*args, **kwargs)

def delete(self, *args, **kwargs):
    raise ValidationError("RBACAuditLog entries cannot be deleted.")
```

`models.py:1134-1140`. A `queryset.update()` or `queryset.delete()` bypasses both.

Indexes: `(entity_type, entity_id)`, `(action_type, created_at)`,
`(school_id, created_at)`. Ordered `-created_at`.

There is **no tenant column**, and `school_id` is populated only from
`metadata["school_id"]` (`audit.py:46`) - which only the override views supply
(`views.py:1139`). Every other row in the table has `school_id = ""`, so the
third index is nearly useless and the trail cannot be filtered by tenant.

## 3. Endpoint map

**None.** Nothing in this slice is routed. Its callers are:

| Consumer | Entry point |
|---|---|
| Every DRF view in the repo | `HasRBACPermission`, `IsAuthenticatedAndActive`, `TenantSurfaceAllowed` |
| Shared reference reads | `HasAnyModuleAccess` + `rbac_modules` (`vs_finance/views.py` is the only user) |
| Every tenant-owned model | `TenantAwareManager` as `objects` |
| Every list / detail / aggregate that respects branch | `scoping.branch_scope`, `branch_q`, `branch_visible` |
| `vs_workflow` approval routing | `evaluator.resolve_users_with_permission` |
| Serializers with sensitive fields | `fls.FieldSecurityMixin` |
| Authentication | `TenantJWTAuthentication` as the DRF auth class |
| Every RBAC write | `audit.record_rbac_audit` |

The permission classes and the attributes a view declares:

| Class | View attribute it reads | Semantics |
|---|---|---|
| `TenantSurfaceAllowed` | `pending_tenant_surface` | `True` opens the whole view; an iterable opens named ViewSet actions or HTTP methods, compared case-insensitively; absent means closed |
| `PlatformDecisionAllowed` | `platform_decision` | Reserves the decision to a caller whose **own** tenant is `PLATFORM` |
| `IsAuthenticatedAndActive` | - | Blocks `SUSPENDED` / `LOCKED` / `DEACTIVATED`, then defers to `TenantSurfaceAllowed` |
| `IsVisionStaff` | - | Caller's tenant kind is `PLATFORM` |
| `IsVisionSuperAdmin` | - | `is_vision_super_admin(request.user)` |
| `HasRBACPermission` | `rbac_permission`, `rbac_group_permission` | Direct keys are **any-of**; group keys are **all-of**; both must pass when both are set |
| `HasAnyModuleAccess` | `rbac_modules` | Any key whose module prefix matches |
| `ReadOnly` | - | `request.method in SAFE_METHODS` |

`TenantJWTAuthentication` reads two more view attributes:
`tenant_param_required` (default `True`) and `platform_cross_tenant_param`
(default `False`) (`authentication.py:91`, `125`, `132`).

## 4. Lifecycle / state machine

The request path, in order:

```
 1. TenantJWTAuthentication.authenticate()          authentication.py:72
      ├─ super().authenticate()  → (actor, token)
      ├─ reject a token with no tenant_slug, or whose tenant_id no longer
      │  matches the user's home tenant   → "Session predates the tenant upgrade"
      ├─ read ?tenant=<slug>  (lower-cased, stripped)
      │    ├─ slug given → tenant must be ACTIVE or PENDING, else 404
      │    │     ├─ impersonation header → load session, its tenant must match
      │    │     └─ else, a foreign slug needs actor.tenant.kind == PLATFORM
      │    │        AND view.platform_cross_tenant_param
      │    └─ no slug → 400 unless view.tenant_param_required is False
      └─ stamp the request:
           actor_user, effective_user, impersonation_session,
           tenant, rbac_tenant  (= actor.tenant unless impersonating)
         set_current_tenant(tenant)          ← arms TenantAwareManager
         set_current_audit_identity(...)     ← arms proxy attribution

 2. check_permissions()
      IsAuthenticatedAndActive → account status → TenantSurfaceAllowed
      HasRBACPermission        → TenantSurfaceAllowed → super-admin bypass
                                → evaluator.has_permission(any-of)

 3. the view runs
      queryset → TenantAwareManager filters by the ambient tenant
              → branch_visible(request, qs) narrows by grant
      serializer → FieldSecurityMixin strips unreadable fields

 4. writes → record_rbac_audit() → RBACAuditLog (durable) → vs_audit (mirror)
```

Impersonation session validation is its own small state machine
(`authentication.py:16-70`): the session must exist, belong to the actor, be
`ACTIVE`, and not be expired. An open-ended session is expired lazily against
`proxy_idle_timeout_minutes` from `vs_config`, and expiring one **writes**
`status = EXPIRED` and `ended_at` before refusing
(`authentication.py:39-55`). A target who is no longer active ends the session
outright (`authentication.py:66-69`).

The one rule that keeps school impersonation intra-tenant lives here and nowhere
else: a non-`PLATFORM` actor may ride only a session pinned to their own tenant,
and the session's tenant is fixed at start (`authentication.py:56-65`).

## 5. Derivations

### The branch condition on an assignment

`_assignment_branch_q(branch)` (`evaluator.py:61-81`) - the single place this is
expressed:

| `branch` argument | `Q` produced |
|---|---|
| `ANY_BRANCH` | `branch IS NULL` **OR** `branch.status IN IN_SERVICE_STATES` |
| `None` | `branch IS NULL` only |
| a `Branch` | `branch IS NULL` **OR** (`branch = X` **AND** `X.status IN IN_SERVICE_STATES`) |

The liveness test is written as a positive `status IN (…)` rather than an
exclusion on purpose: `branch` is nullable, and a negative filter across that
join would take the whole-tenant grants down with it (`evaluator.py:70-72`).

### Effective permissions

`get_effective_permissions(user, tenant, branch=ANY_BRANCH)`
(`evaluator.py:181-222`):

```
guard:   user authenticated, tenant not None, user.tenant_id == tenant.pk
cache:   key = (tenant.pk, ANY_BRANCH | branch.pk | None)
         ANY_BRANCH keys itself rather than collapsing through getattr(pk, None),
         which would share one entry with the explicit None scope
compute: role_ids  = active assignments on ACTIVE roles matching the branch Q
         granted   = TenantRolePermission(role in ids, granted=True)
         denied    = TenantRolePermission(role in ids, granted=False)
         granted  |= keys of every PermissionGroup attached to those roles
         effective = (granted - denied)
         allows, denies = active overrides for (tenant, user)
         effective = (effective | allows) - denies
```

Four queries on a cold cache: assignments, role permissions, role groups, group
permissions - plus one for overrides. Everything after that is free for the rest
of the request.

`_holdable_filter(tenant)` (`evaluator.py:84-97`) adds
`permission__scope = TENANT` for every non-platform tenant. It is defence in
depth, not the boundary: a row written before `Permission.scope` existed,
restored from an old backup or inserted by raw SQL still confers nothing. The
filter is on an indexed column and costs nothing measurable.

### Which branches a caller may work in

`_grant_scope(user, tenant)` (`scoping.py:57-100`), one query returning
`(branch_id, branch__status)` pairs, read in order, first match wins:

| Condition | Answer |
|---|---|
| The caller's tenant is not *tenant* | `WHOLE_TENANT` (`None`) |
| No active grants at all | `WHOLE_TENANT` - overrides may still admit them |
| Any grant with `branch_id IS NULL` | `WHOLE_TENANT` |
| Otherwise | `frozenset` of the granted branch ids **whose status is in service** |

Liveness is deliberately **not** filtered in SQL: "this person holds no grants"
and "every branch this person was granted has since been withdrawn" are
different answers - the first falls back to their home posting, the second must
show nothing - and a filter that drops the withdrawn rows makes the two
indistinguishable (`scoping.py:76-81`).

`visible_branch_ids` then applies the fallback (`scoping.py:123-131`):

```python
if scope is None:                       # nothing branch-shaped to say
    own_id = getattr(user, "branch_id", None)
    scope = WHOLE_TENANT if own_id is None else frozenset({own_id})
```

`branch_id` rather than `branch`, because dereferencing the relation would fetch
the whole `Branch` on the hot path of every read just to read its pk back.

### Rendering the answer as a filter

`BranchScope` (`scoping.py:166-243`) is built once per request and re-rendered
per relation path, because a report service aggregating several models reaches
`branch` by different routes (`payment__branch`, `grn__branch`) and handing it a
pre-built `Q` would force it to re-derive the rule for every other path.

```python
q(prefix="", field="branch"):
    branch_ids is None            -> Q()                         # identity for &
    include_shared (default True) -> Q(f"{prefix}{field}_id__in"=ids)
                                     | Q(f"{prefix}{field}_id__isnull"=True)
    include_shared=False          -> Q(f"{prefix}{field}_id__in"=ids)  only
```

An empty set renders as `IN ()`, which matches nothing - the right answer for a
caller whose every granted branch has been withdrawn.

`filter(qs, …)` deliberately does **not** call `qs.filter(self.q(...))` for an
unbound caller: filtering on an empty `Q` is semantically a no-op but still
clones the queryset and can perturb a later `exclude()` or aggregate, and the
whole-tenant caller is the common case that must not change at all
(`scoping.py:233-243`). `UNNARROWED` is a shared singleton for the same reason.

`is_narrowed` is the one flag that should turn a branch column, switcher or
facet on in a response (`scoping.py:206-214`): where a caller is unbound - or
the school has one branch and the dimension ought to recede - it stays `False`
and the payload is unchanged.

### Ambient tenant filtering

`TenantAwareManager.get_queryset()` (`managers.py:103-119`):

```python
tenant = get_current_tenant()
if tenant is None:  return qs            # CX request or Celery task
lookup = self.tenant_field or ("tenant" if present else "branch" if present else None)
if lookup is None:  return qs
if lookup == "branch":  lookup = "branch__tenant"    # Branch owns its own tenant
condition = Q(**{lookup: tenant})
if self.include_global:  condition |= Q(**{f"{lookup}__isnull": True})
return qs.filter(condition)
```

`include_global=True` is for models where a NULL tenant means "applies to every
tenant" - global workflow templates, global compliance rules.

### Field-level masking

`FieldSecurityMixin` (`fls.py:45-142`):

- `read_permissions` / `write_permissions` are `{field_name: permission_key}`;
  a field in neither dict is always exposed - FLS is opt-in per field.
- `_resolve_user_permissions()` returns `None` for "skip FLS entirely": no
  request context at all, or a Vision super admin. It returns an empty set for
  an unauthenticated user.
- The result is cached on `request._fls_permissions`, so a list of 200 rows
  costs one evaluator call, not 200.
- `to_representation` pops unreadable fields and appends
  `data["_stripped_fields"] = [...]` naming them.
- `to_internal_value` raises one `ValidationError` naming every unauthorised
  field at once.

### Who may act on a document

`resolve_users_with_permission(tenant, branch, key)` (`evaluator.py:244-307`).
`branch` here is the scope of the *work*, not of a caller, so it is passed
positionally and an explicit `None` keeps its meaning: a document belonging to
the entity as a whole is approved by whole-tenant grant holders, never by
somebody pinned to one site.

```
tenant = getattr(tenant, "tenant", tenant)        # transitional: accepts a School
if tenant is not platform and the key is not scope=TENANT:  return none()
role_ids = (direct grants | grants via group) - explicit denies
users    = active assignments (tenant, role in ids) matching the branch Q
users   |= ALLOW override holders
users   -= DENY override holders
return active users in that tenant
```

The docstring states that routing shares `_assignment_branch_q` with the
permission gate "so a person this function nominates as an approver cannot be
someone `has_permission` would then refuse". It shares the branch condition but
not the role-status condition - see `rbac_code_issues` §2.

### The durable audit write

`record_rbac_audit` (`audit.py:20-91`):

1. `resolve_audit_identity(actor_user)` unpacks the ambient
   `(actor, effective, proxy_session)` triple set by authentication.
2. `add_proxy_audit_metadata` folds the proxy attribution into `metadata`.
3. `school_id = str(metadata.get("school_id", "") or "")`.
4. `RBACAuditLog.objects.create(...)` - **raises on failure**, by design, so the
   caller's transaction rolls back with it.
5. `vs_audit.emit_audit_event(...)` inside a bare `try/except Exception`, logging
   a warning with `exc_info` on failure. `emit_audit_event` already swallows its
   own failures; the second guard is belt and braces at the boundary.

## 6. What writing writes

This slice writes exactly two things.

| Writer | Row |
|---|---|
| `record_rbac_audit` | One `RBACAuditLog`, plus a best-effort `vs_audit.AuditEvent` mirror |
| `TenantJWTAuthentication._load_impersonation` | `ImpersonationSession.status = EXPIRED` + `ended_at`, when an open-ended session has been idle past the configured limit (`authentication.py:51-54`); or `impersonation.end()` when the target is no longer active (`authentication.py:68`) |

Everything else here is read-only. The two caches are in-memory attributes on
the user instance and touch no storage.

Worth noting what authentication writes into the request, because the rest of
the repo depends on the exact meanings (`authentication.py:140-152`):

| Attribute | Value |
|---|---|
| `actor_user` | The person holding the token, always |
| `effective_user` | The impersonated target, or the actor |
| `impersonation_session` | The session, or `None` |
| `tenant` | The tenant being **operated on** - the target's under impersonation, the asserted one for a cross-tenant platform call |
| `rbac_tenant` | `actor.tenant` when not impersonating, otherwise `tenant` |

DRF's `request.user` is `effective_user`, which is why `caller_branch_ids` stays
correct through impersonation: an impersonating platform admin is narrowed by
the grants of the person they are standing in for, which is the point
(`scoping.py:261-265`).

## 7. Worked example

Mrs Adeyemi is Storekeeper at Ikeja and nowhere else (see
`rbac_roles_assignments` §7). She opens the stock list.

**1. The token arrives.** `GET /v1/procurement/stock/?tenant=corona`, bearer
token. `super().authenticate()` returns her user. The token carries
`tenant_slug: "corona"` and a `tenant_id` matching her row, so it is not
rejected as pre-upgrade.

**2. The tenant is resolved.** `slug = "corona"`; Corona is `ACTIVE`, so it is
in `AUTHENTICABLE_STATUSES`. No impersonation header. `actor.tenant_id ==
tenant.pk`, so the cross-tenant branch is not taken. The request is stamped:
`tenant` and `rbac_tenant` are both Corona, `effective_user` is Adeyemi, and
`set_current_tenant(corona)` arms every `TenantAwareManager` for the rest of the
request.

**3. The gate.** `IsAuthenticatedAndActive` sees `status = "ACTIVE"` and defers
to `TenantSurfaceAllowed`, which sees Corona is not `PENDING` and returns True.
`HasRBACPermission` runs the surface check again (harmlessly), finds she is not
a Vision super admin, reads `rbac_permission = "procurement.stock.view"` and
calls `has_permission(user, key, tenant=corona)` with **no branch named**.

**4. The evaluation.** `_assignment_branch_q(ANY_BRANCH)` is
`branch IS NULL OR branch.status IN IN_SERVICE_STATES`. Her one assignment is
pinned to Ikeja, which is in service, so it counts. Her role's grants are read
with `permission__scope = TENANT` appended, groups are flattened in, denies
subtracted, overrides folded. The set contains the key. The gate opens, and the
whole set is now memoised on her user object under `(corona.pk, ANY_BRANCH)`.

**5. The queryset.** `StockItem.objects` is a `TenantAwareManager`, so
`get_queryset()` already appended `tenant = corona`. The view then calls
`branch_visible(request, qs)`. `caller_branch_ids` → `visible_branch_ids` →
`_grant_scope`: one query, one row, `branch_id = 2`, status in service, no NULL
present → `frozenset({2})`. That answer is cached on her user under
`corona.pk`.

**6. The filter.** `BranchScope({2}, include_shared=True).q()` renders
`branch_id IN (2) OR branch_id IS NULL`. So she sees Ikeja's stock **and** the
items Corona publishes for every branch - which is right, because a NULL branch
there means shared, not orphaned.

**7. The mask.** The serializer declares
`read_permissions = {"unit_cost": "procurement.stock.view_sensitive"}`. FLS
resolves her effective set once, caches it on the request, finds she lacks the
key, pops `unit_cost` from all 40 rows and appends
`"_stripped_fields": ["unit_cost"]` to each. One evaluator call for 40 rows.

**8. Contrast: the KPI header above the list.** It aggregates the same model
through a different service. It calls `branch_scope(request)` once and re-renders
it for each path it needs. Because the answer came from the same function, the
header cannot disagree with the list beneath it - which is the entire reason
`BranchScope` hands back an object rather than a `Q`.

**9. Contrast: a colleague with no branch grants.** Mr Eze holds a whole-tenant
Bursar grant. `_grant_scope` sees a `NULL` branch id and returns
`WHOLE_TENANT`. `branch_scope` returns the shared `UNNARROWED` singleton,
`filter()` returns the queryset untouched, and Django compiles **byte-identical**
SQL to what it produced before branch grants existed. A school that has never
pinned a grant is not merely unaffected but indistinguishable from before
(`scoping.py:294-298`).

**10. Contrast: Ikeja is suspended.** `_assignment_branch_q` stops counting her
grant, so step 4 now returns an empty set and the gate **closes** - she gets a
403, not an empty list. Had she also held a Lekki grant, step 5 would return
`frozenset({lekki})` rather than falling back to her `User.branch`: withdrawing
a site is supposed to withdraw the access it carried, not silently widen it
(`scoping.py:34-37`).

## 8. Gotchas / known limitations

Recorded in full in **`error/rbac/rbac_code_issues.md`**. The items belonging to
this slice:

| # in that file | One line |
|---|---|
| §1 | **Critical, confirmed by execution.** `is_vision_super_admin` never checks the tenant's kind, so a role keyed `xvs_super_admin` in *any* tenant confers the platform-wide bypass |
| §2 | `resolve_users_with_permission` ignores `role.status`, so an INACTIVE role still nominates approvers the gate will then refuse - confirmed by execution |
| §7 | The evaluator never reads `Permission.is_active` or `PermissionGroup.is_active` - confirmed by execution |
| §27 | `rbac_group_permission` is documented on `HasRBACPermission`, used by no view, and would raise if it were: it passes group *names* into a UUID `group_id__in` lookup |
| §28 | `HasAnyModuleAccess` loads the caller's **entire** effective key set to answer a prefix question |
| §29 | `RBACAuditLog` has no tenant column, no populated `school_id` outside the override views, and no reader anywhere in the repo |
| §30 | `RBACAuditLog` immutability is Python-only: `queryset.update()` and `queryset.delete()` walk straight past it |
| §31 | `FieldSecurityMixin` caches on `request._fls_permissions` with no tenant in the key, so two serializers resolving different tenants in one request share one answer |
| §32 | `_stripped_fields` tells an unauthorised caller exactly which sensitive fields exist |

Design choices worth stating as choices - this slice is unusually well
reasoned and most of its surprises are deliberate:

- **`ANY_BRANCH` keys itself in the cache** rather than collapsing through
  `getattr(branch, "pk", None)`, because it has no `pk` and folding it in would
  share one cache entry with the explicit `None` scope, which answers a
  different question (`evaluator.py:203-209`).
- **`_grant_scope` does not filter liveness in SQL**, so "no grants" and "all
  granted branches withdrawn" stay distinguishable (`scoping.py:76-81`).
- **`BranchScope.filter` short-circuits instead of filtering on an empty `Q`**,
  so the common case is byte-identical (`scoping.py:233-243`).
- **`PlatformDecisionAllowed` returns `False` rather than raising**, so the
  refusal reads identically to a missing-key 403. A distinct message would be a
  probe: mint the role, call the endpoint, read the wording to learn whether the
  grant landed (`permissions.py:182-186`).
- **`PlatformDecisionAllowed` reads `request.user`, not `request.actor_user`**:
  under impersonation the effective user is the person being impersonated, and
  an actor wearing a school admin's identity holds that admin's authority and no
  more (`permissions.py:176-181`).
- **`TenantSurfaceAllowed` runs before the super-admin bypass** in
  `HasRBACPermission`, because the question is whether the tenant being operated
  on is live, not what the caller holds (`permissions.py:292-298`).

## 9. Permissions & tenant isolation

This slice *is* the isolation mechanism, so the interesting question is where it
can fail open.

- **Three independent boundaries.** The auth layer decides which tenant may be
  asserted; `TenantAwareManager` filters querysets by the ambient tenant;
  `visible_branch_ids` narrows further within it. They are independent, and a
  view that bypasses the manager (`all_objects`) loses the second without
  warning.
- **A `PLATFORM` scope stops a key being granted; it does not stop it being
  bypassed.** `is_vision_super_admin` returns before any key is read, so the
  scope column - the thing the whole security model rests on - is not consulted
  for that caller. That is intended for a real CX super admin and is precisely
  what makes `rbac_code_issues` §1 critical rather than cosmetic.
- **`_holdable_filter` is the backstop, not the boundary** (`evaluator.py:84-97`)
  and says so. The grant models refuse to write such a row in the first place.
- **CX requests are unfiltered by construction.** A platform caller sets no
  ambient tenant on most routes, so `TenantAwareManager` does nothing. Every
  cross-tenant leak recorded against `vs_admin_console`, `vs_audit` and
  `vs_exports` is a variation on this: the manager was assumed to be engaging and
  it was not.
- **Celery tasks see everything.** No thread-local context exists in a worker,
  so any task touching a tenant-owned model must scope explicitly
  (`managers.py:34-37`).
- **Impersonation cannot cross a tenant for a school actor.** The single choke
  point is `authentication.py:56-65`, and the session's tenant is fixed at start
  and immutable, so no `?tenant=` assertion can widen it.
- **A token that predates a tenant move is refused** rather than silently
  operating on the wrong tenant (`authentication.py:82-84`).
- **FLS is opt-in per field and fails open without a request context** - by
  design, so management commands and login payload construction do not silently
  lose fields (`fls.py:29-31`). That does mean any code path that serializes
  without a request gets the unmasked record.

## 10. Code map

| File | What lives there |
|---|---|
| `vs_rbac/evaluator.py:43-58` | `_AnyBranch`, the `ANY_BRANCH` sentinel and why it is not `None` |
| `vs_rbac/evaluator.py:61-97` | `_assignment_branch_q`, `_holdable_filter` |
| `vs_rbac/evaluator.py:113-139` | Override queries |
| `vs_rbac/evaluator.py:142-178` | `get_role_permissions`, `_role_permission_keys` |
| `vs_rbac/evaluator.py:181-241` | `get_effective_permissions` and the three `has_*` helpers |
| `vs_rbac/evaluator.py:244-307` | `resolve_users_with_permission` |
| `vs_rbac/permissions.py:21-45` | `is_vision_super_admin` and its per-request memo |
| `vs_rbac/permissions.py:80-155` | `_view_opens_to_pending_tenant`, `TenantSurfaceAllowed` |
| `vs_rbac/permissions.py:159-200` | `PlatformDecisionAllowed` |
| `vs_rbac/permissions.py:204-255` | `IsAuthenticatedAndActive`, `IsVisionStaff`, `IsVisionSuperAdmin` |
| `vs_rbac/permissions.py:259-348` | `HasRBACPermission` |
| `vs_rbac/permissions.py:352-408` | `HasAnyModuleAccess`, `ReadOnly` |
| `vs_rbac/scoping.py:57-140` | `_grant_scope`, `visible_branch_ids` |
| `vs_rbac/scoping.py:166-248` | `BranchScope`, `UNNARROWED` |
| `vs_rbac/scoping.py:251-315` | `caller_branch_ids`, `branch_scope`, `branch_q`, `branch_visible` |
| `vs_rbac/managers.py` | `TenantAwareQuerySet`, `TenantAwareManager` |
| `vs_rbac/fls.py` | `FieldSecurityMixin` |
| `vs_rbac/authentication.py:16-70` | Impersonation session validation |
| `vs_rbac/authentication.py:72-153` | Tenant assertion and request stamping |
| `vs_rbac/audit.py` | `record_rbac_audit` |
| `vs_rbac/models.py:1089-1143` | `RBACAuditLog` |

## 11. Test coverage & gaps

Module baseline: **`Ran 326 tests in 89.035s` - OK**.

Covered:

- `tests/test_permissions.py` (409 lines) - every permission class, the any-of
  and all-of semantics, the `ImproperlyConfigured` guards on empty lists, the
  super-admin bypass, the account-status refusals.
- `tests/test_authentication.py` (284 lines) - the mandatory `?tenant=`, the
  pre-upgrade token refusal, `tenant_param_required = False`, the cross-tenant
  platform branch, and the impersonation validation ladder.
- `tests/test_branch_scope_filter.py` (412 lines) - `BranchScope` in both modes,
  the empty-set case, the unbound short-circuit, and the multi-path rendering.
- `tests/test_branch_scoped_grants.py` - the access half of the same rule.
- `tests/test_branch_tenant_boundary.py` - includes
  `TenantLookupInvariantTests`, the guard that fails if any model regrows a
  `school`-shaped ownership path.
- `tests/test_fls.py` - read stripping, write rejection, the no-context
  pass-through, the super-admin bypass.
- `tests/test_pending_tenant_surface.py` - the `pending_tenant_surface`
  allowlist in all three forms.
- `tests/test_audit.py` - the durable-first ordering and the swallowed mirror
  failure.

Not covered:

- **No test asserts `is_vision_super_admin` is false for a non-platform
  tenant.** That is the single gap `rbac_code_issues` §1 falls through, and it
  is a one-line test.
- No test compares `resolve_users_with_permission` against `has_permission` for
  an INACTIVE role (§2).
- `rbac_group_permission` has no test at all (§27), which is why nobody has
  noticed it cannot work.
- No test asserts anything about `RBACAuditLog` tenant attribution (§29) or
  attempts a `queryset.update()` against the immutability guard (§30).
- No test puts two serializers with different tenants in one request (§31).
- `resolve_users_with_permission`'s School-instance compatibility shim
  (`evaluator.py:257-258`) has no test.
