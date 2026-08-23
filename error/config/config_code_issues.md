# config_code_issues

Everything wrong with `vs_config`, in one place, ordered by how much it costs.
Each item states the defect, the evidence, what actually happens to a user, and
the fix. The four slice reports (`config_settings_catalogue`,
`config_platform_runtime_settings`, `config_capabilities_entitlements`,
`config_audit_trail_exports`) point here rather than repeating it.

Baseline: the `vs_config` suite is **61 tests, all green**
(`Ran 61 tests in 94.867s` - OK, via
`cd apps && DB_NAME=cx_configslice ../cx/Scripts/python.exe manage.py test
vs_config --settings=apps.settings.local --noinput`). The single traceback in
that run is `test_oversized_export_fails_with_the_size_limit_in_its_own_words`
logging its own expected failure. Every item below is therefore something the
suite does not currently catch. Nothing here is speculative: every claim is
traced to a file and line.

**Status: recorded, not yet fixed.** Nothing in this file has been changed in
the code.

---

## Summary

| # | Issue | Severity |
|---|---|---|
| 1 | A well-formed UUID in `?actor=` is a 500 on three audit surfaces, and poisons saved views and queued exports | **High** |
| 2 | Bulk entitlement scheduling silently grants denied capabilities and erases their provenance | **High** |
| 3 | No school role is ever granted a `config.*` permission, so every school-facing config surface is a 403 | **High** |
| 4 | `default_enabled` is ignored for every entitlement-gated capability, contradicting its own contract | Medium |
| 5 | Entitlement and override lists paginate an unordered queryset | Medium |
| 6 | `SECRET_REFERENCE` is a label with no resolver and no format check | Medium |
| 7 | The two synchronous exports write no audit event | Medium |
| 8 | Queued export files are never purged | Medium |
| 9 | `config.definition.update` can archive and un-archive definitions, making the archive key decorative | Medium |
| 10 | Effective-value reads and the config snapshot run one query per definition | Medium |
| 11 | Security settings are re-resolved from scratch on every read, on the authentication path | Medium |
| 12 | Config changes reach the platform audit trail with `tenant = NULL` | Medium |
| 13 | The catalogue seed and migration 0006 disagree about where security settings may be written | Medium |
| 14 | An engine app imports `vs_schools` | Medium |
| 15 | Clearing a value files its audit row against the definition, splitting one setting's history | Low |
| 16 | `platform.profile.name` is the one profile field that cannot be cleared | Low |
| 17 | A CHOICE definition with no `choices` rule can never hold a value | Low |
| 18 | Audit facets silently sample only the newest 500 events | Low |
| 19 | Smaller defects and dead code | Low |

---

## 1. A well-formed UUID in `?actor=` is a 500

**High. Confirmed against the real field type.**

### The defect

`ConfigurationAuditFilterSerializer.validate_actor` accepts two shapes, a
positive integer **or a UUID**:

```python
# serializers.py:380-392
def validate_actor(self, value):
    if not value:
        return value
    valid = value.isdigit() and int(value) > 0
    if not valid:
        try:
            UUID(value)
            valid = True
        except (ValueError, TypeError):
            valid = False
    if not valid:
        raise serializers.ValidationError("Use a valid actor ID.")
    return value
```

The validated value goes straight to the ORM:

```python
# services/audit_exports.py:59
queryset = queryset.filter(actor_id=filters["actor"])
```

`actor` points at `AUTH_USER_MODEL`, whose primary key is a `BigAutoField`
(`vs_user/migrations/0001_initial.py:27-33`). Verified directly:

```text
>>> User._meta.pk.get_prep_value("3f1b2c4d-0000-4000-8000-000000000001")
ValueError: Field 'id' expected a number but got '3f1b2c4d-0000-4000-8000-000000000001'.
```

`core/exceptions.py` has no branch for a bare `ValueError`, so it falls to the
final catch-all (`core/exceptions.py:157-163`) and the caller gets a 500 with
`{"code": "SERVER_ERROR"}`.

### What actually happens

Three surfaces share that filter path (`views.py:936-950`), so all three break
on the same input:

- `GET /v1/config/audit-events/?actor=<uuid>` - 500.
- `GET /v1/config/audit-events/export/?actor=<uuid>` - 500.
- `POST /v1/config/audit-events/export-jobs/` with `filters.actor` set to a
  UUID - the job is created and queued successfully, then the Celery task
  raises inside `apply_configuration_audit_filters`, the generic handler catches
  it (`services/audit_exports.py:201-204`), and the operator is told
  *"The export could not be generated. Try again or narrow the filters."* -
  which is wrong advice, because narrowing will not help.
- `POST /v1/config/audit-events/saved-views/` with a UUID actor is accepted
  (`serializers.py:413-414` delegates to the same validator), so the view is
  stored and 500s every time it is replayed.

Any client that stores user ids as UUIDs elsewhere in the platform, or any
operator pasting an identifier from another screen, hits this.

### Why it exists

The validator was written to be permissive about identifier formats across a
platform that mixes integer and UUID primary keys, and nobody checked which one
`User` actually uses. The test that should have caught it,
`test_invalid_actor_filter_is_rejected` (`tests.py:917-919`), passes
`"not-a-uuid"` - a value the validator correctly refuses. The value that gets
through is the one nobody tried.

### The fix

Fix the class, not the case:

1. **Delete the UUID branch** from `validate_actor` and validate against the
   actual `AUTH_USER_MODEL` pk type. A single
   `serializers.IntegerField(min_value=1)` says it better than the hand-rolled
   check.
2. **Make the ORM boundary safe anyway.** `apply_configuration_audit_filters`
   is called from a view *and* from a Celery task replaying a stored snapshot;
   a filter that was valid when saved must not be able to crash the task later.
   Coerce there, and turn a coercion failure into a validation error.
3. **Add a test with a well-formed UUID** on the list, the CSV export and a
   queued job, asserting 400 rather than 500.

---

## 2. Bulk entitlement scheduling silently grants

**High.**

### The defect

`POST /v1/config/entitlements/bulk-schedule/` is documented and named as a
schedule change. Its serializer requires only dates and a reason
(`serializers.py:325-348`); `state` and `source` are not accepted fields. But
the service writes both unconditionally:

```python
# services/capabilities.py:190-191
row.state = CapabilityEntitlement.State.GRANTED
row.source = CapabilityEntitlement.Source.MANUAL
```

### What actually happens

Two separate losses, on every call:

1. **A denied tenant is granted.** `CapabilityEntitlement.State.DENIED` at
   tenant scope is the documented way to carve an exception out of a
   platform-wide grant (`models.py:415-418`). A platform admin extending an
   expiry across a list of schools - the exact use case the endpoint exists for
   - flips any DENIED row in that list to GRANTED. Nothing in the request said
   to, nothing in the response points it out (the response is just the serialized
   rows), and the capability becomes effective for that tenant the moment the
   window opens, because `effective_capability` returns `True` for an entitled
   capability with no override (`services/capabilities.py:99-101`).
2. **Provenance is erased.** `source = PACKAGE` records that a grant came from
   school package setup. Every bulk-scheduled row is rewritten to `MANUAL`, so
   the platform can no longer tell a contract-driven grant from a hand-made one.
   The before/after audit snapshot does record the old source
   (`services/capabilities.py:163-168`), so the information is recoverable, but
   only by reading history.

### Why it exists

`bulk_schedule_entitlements` was written for the renewal workflow, where every
target is a grant being extended, and creating a missing row needs *some* state.
The `row is None` branch (`services/capabilities.py:184-189`) legitimately needs
to default to GRANTED. The two assignments were then hoisted above the branch
and applied to existing rows as well.

### The fix

1. **Only default the state when creating a row.** Move both assignments inside
   the `if row is None:` branch. An existing row keeps its `state` and its
   `source`; only the dates change.
2. **Decide explicitly what a DENIED target means** and say so: either skip it
   and report it in the response, or refuse the whole batch naming the tenant.
   Silently granting is the one option that should not be on the table.
3. **Test it.** Every fixture row in
   `test_bulk_schedule_updates_every_selected_grant_atomically`
   (`tests.py:1010-1036`) is already GRANTED, which is exactly why this
   survived. Add a DENIED target and a PACKAGE-sourced target.

---

## 3. No school role is ever granted a `config.*` permission

**High, and it is a delivery gap rather than a security hole.**

### The defect

`seed_config_permissions.py` creates all 19 permission keys and grants them to
exactly two roles:

```python
# seed_config_permissions.py:16
PLATFORM_ROLE_IDS = ["xvs_super_admin", "xvs_platform_admin"]
```

both attached to the `codex` PLATFORM tenant (`:59-75`). A repository-wide
search for any `config.` permission key outside `vs_config` itself returns
nothing, and a search for the `"config"` permission module in `vs_rbac`,
`core`, `vs_tenants` and `vs_schools` returns nothing either.

### What actually happens

`Capability`'s own docstring names the contract:

> Application code asks `vs_config.conf.is_capability_enabled(key, ...)`;
> the frontend reads GET /v1/config/effective-capabilities/.
> (`models.py:283-284`)

That endpoint requires `config.capability.view` (`views.py:922`). No school role
ships with it. So on a freshly seeded tenant:

- a school administrator calling `/v1/config/effective-capabilities/` gets 403,
  and the frontend has no way to learn which modules the school actually has;
- `/v1/config/security-settings/` is a 403 too, so the compliance-clamp
  machinery built and tested for schools (`tests.py:615-724`) is unreachable in
  production;
- so is every override route, which is the documented way for a school to pause
  a feature it owns.

The tests reach these surfaces by hand-building roles
(`tests.py:280-283`, `:331-334`, `:343-346`), which is why the gap is invisible
from inside the suite.

The security consequence is the reverse of alarming: the module's cross-tenant
surface is currently unreachable for schools. But the product does not work as
documented until this is closed.

### Why it exists

`vs_config` was seeded as a platform-administration module, and the school-side
consumption story (capabilities driving the school UI, schools pausing their own
features, schools tightening their own security) arrived later without a
corresponding change to the seed.

### The fix

1. **Decide the school-facing contract explicitly** and write it into
   `seed_config_permissions.py`: at minimum `config.capability.view` for the
   school and branch admin prebuilts, and a considered answer for
   `config.override.manage`, `config.security.view` / `.manage` and
   `config.value.view`.
2. **Grant through the prebuilt role templates**, the same way
   `seed_notification_permissions.py:23-35` does, rather than only through the
   two platform roles.
3. **Add a test that a seeded school admin can read
   `/v1/config/effective-capabilities/`**, which is the assertion that would
   have caught this.

---

## 4. `default_enabled` is ignored for entitlement-gated capabilities

**Medium.**

### The defect

The model says:

> `default_enabled`: The runtime state used when no override exists at any
> scope in the chain. (`models.py:298-299`)

The evaluator says otherwise:

```python
# services/capabilities.py:99-101
if capability.requires_entitlement:
    return True
return capability.default_enabled
```

and `BulkCapabilityEvaluator` repeats it (`services/capabilities.py:308`).

### What actually happens

For every capability with `requires_entitlement = True` - which is all ten
seeded MODULE rows (`seed_config_catalogue.py:117-126`) - `default_enabled` is
dead. A platform admin who creates a module with `default_enabled: false`,
grants it to a tenant, and expects it to stay off until switched on will find it
already on. The only lever that turns it off is an explicit `DISABLED` override,
which means the "off by default, opt in per branch" rollout pattern is not
available for modules at all.

### Why it exists

The inline comment shows this was a deliberate decision
(`services/capabilities.py:96-98`): being in the plan is what switches a
plan-gated module on. The decision is defensible. What was not done is
reconciling the field's own documentation, its API surface (the serializer still
exposes `default_enabled` as writable for every capability,
`serializers.py:226-231`) or the admin experience with it.

### The fix

Pick one and make everything agree:

- **Keep the behaviour**: make `default_enabled` read-only, or reject it
  outright, when `requires_entitlement` is True, and rewrite the field docstring
  to say "consulted only for capabilities that do not require entitlement".
- **Or honour the field**: return `capability.default_enabled` in both branches
  and let the seed set `default_enabled=True` on the ten modules, which
  preserves today's behaviour while making the field mean what it says.

Either way, add a test asserting the chosen semantics; there is none today.

---

## 5. Entitlement and override lists paginate an unordered queryset

**Medium.**

### The defect

Neither `CapabilityEntitlement.Meta` (`models.py:483-494`) nor
`CapabilityOverride.Meta` (`models.py:566-573`) declares `ordering`, and neither
list view adds one:

```python
# views.py:686-689
qs = CapabilityEntitlement.all_objects.select_related("capability", "updated_by")
qs = qs.filter(tenant=tenant) if tenant else qs.filter(tenant__isnull=True)
return self.paginate(request, qs, CapabilityEntitlementSerializer)
```

```python
# views.py:893-901
qs = CapabilityOverride.all_objects.select_related("capability", "updated_by")
...
return self.paginate(request, qs, CapabilityOverrideSerializer)
```

Compare `ValueListSetView.get`, which does it correctly:
`qs.order_by("definition__key")` (`views.py:264`).

### What actually happens

PostgreSQL is free to return rows in any order for an unordered query, and it
changes that order under `LIMIT`/`OFFSET` as the plan changes. So paging through
a tenant's entitlements can show the same row twice and skip another. DRF emits
`UnorderedObjectListWarning` for exactly this, which nothing in this repo turns
into an error. It only bites at more than 25 rows, which no test creates.

### Why it exists

Both models were given careful constraints and indexes and no ordering, and the
list views were written assuming small result sets.

### The fix

1. Add `ordering = ["capability__label", "scope_key"]` (or equivalent) to both
   `Meta` classes, so every caller inherits it - including the shell and any
   future view.
2. Because `label` is not unique, append a unique tiebreaker (`"id"`), the same
   lesson `vs_notifications` learned in its feed
   (`docs/notifications/notification_feed_history.md` §5).
3. While in there: `Capability.Meta.ordering = ["kind", "label"]`
   (`models.py:328`) has the same tie problem on the paginated catalogue.

---

## 6. `SECRET_REFERENCE` is a label with no resolver

**Medium.**

### The defect

The model documents the type as pointing at a secret:

> `SECRET_REFERENCE` marks the setting as pointing at a secret (e.g.
> `env://PAYMENTS_SECRET`) (`models.py:70-73`)

but validation treats it as an ordinary string:

```python
# services/resolution.py:23-26
if kind in {definition.ValueType.STRING, definition.ValueType.SECRET_REFERENCE}:
    if not isinstance(value, str) or not value.strip():
        raise ValueError
```

and nothing anywhere resolves it. `resolve_value` returns the stored string
(`services/resolution.py:93`), and `get_config` hands that string to the caller
(`conf.py:15-16`).

### What actually happens

Three consequences:

1. **There is no `env://` enforcement.** An administrator can type a live API
   key into the field. It is stored in cleartext in `ConfigurationValue.value`,
   a plain `JSONField`, with no encryption at rest beyond whatever the database
   provides.
2. **Redaction protects the API, not the data.** Every read path masks the value
   (`serializers.py:102-106`, `views.py:338-339`, `:581-583`, `:1214-1216`) and
   the audit snapshot is redacted before storage
   (`services/resolution.py:13-16`), which is genuinely good. But anyone with
   database access, a `dumpdata`, or a backup has the plaintext.
3. **Any internal caller gets the raw string.** `get_config("some.secret")`
   returns `"env://PAYMENTS_SECRET"` verbatim, so the first consumer that
   actually uses one has to write its own resolver, and the second will write a
   different one.

Today the risk is latent: the seeded catalogue contains no SECRET_REFERENCE
definition (`seed_config_catalogue.py:8-113`), and nothing calls `get_config`
for one. The type is available to any platform admin through
`POST /definitions/`.

### Why it exists

The type and its redaction were built first, on the assumption that a resolver
would follow. It did not, and nothing marks the gap.

### The fix

1. **Validate the shape.** Require a recognised scheme (`env://`, and whatever
   else the deployment supports) and reject anything else, so a pasted secret is
   a 422 rather than a stored plaintext.
2. **Add the resolver** in `resolve_value` (or a thin wrapper `get_secret`),
   so `get_config` returns the dereferenced secret and callers never see the
   reference. Fail closed when the environment variable is absent.
3. **If neither is going to happen, remove the type** rather than leaving a
   field that looks like a secrets integration and is not one.

---

## 7. The two synchronous exports write no audit event

**Medium.**

### The defect

`AuditEventExportView.get` (`views.py:1156-1188`) streams up to 5,000 audit
rows, including every `before_data` and `after_data` snapshot, and calls
`record_configuration_event` nowhere. `ConfigExportView.get`
(`views.py:1204-1222`) returns every effective configuration value plus the full
capability state for a scope, and likewise records nothing.

Their queued sibling does the opposite: `config.audit.export_queued`,
`config.audit.export_completed` and `config.audit.export_downloaded` are all
written (`services/audit_exports.py:241-249`, `:170-178`, `views.py:1074-1082`).

### What actually happens

Reading the entire configuration history of a tenant, or a snapshot of every
setting and capability it has, leaves no trace in `ConfigurationAuditEvent`, in
`vs_audit`, or anywhere else. An operator who wants a copy without being
recorded simply uses the synchronous route.

This is the same shape `vs_audit`'s own report flagged for its compliance export
(`docs/audit/audit_compliance_exports.md`), so it is a platform pattern, not a
one-off.

### Why it exists

The queued export was built later and with more care; the synchronous routes
predate the idea that reading is an auditable event.

### The fix

1. Record `config.audit.exported` and `config.export.generated` in both views,
   with the filter snapshot and the row count in `metadata`, before the response
   is returned.
2. Target the export at the scope, not a row, the same way the connection test
   uses a synthetic `IntegrationConnection` target
   (`services/connections.py:14-18`).
3. Add a test asserting the event exists, so the behaviour is pinned.

---

## 8. Queued export files are never purged

**Medium.**

### The defect

`ConfigurationAuditExportJob.available_until` is set to seven days after
completion (`services/audit_exports.py:165`) and is checked on download
(`views.py:1070-1071`). Nothing ever deletes the file. There is no purge task in
`vs_config/tasks.py` (13 lines, one task) and no beat entry for this table
(`apps/celery.py:18-60`), while `vs_exports` has exactly that
(`vs_exports/models.py:465` - "A nightly job purges").

### What actually happens

The default storage backend is `core.storage.DatabaseStorage`, so each export is
a blob row in the database, capped at `MEDIA_DB_MAX_BYTES` (25 MB by default)
**each**. Every export ever generated stays there. After the seventh day the
download returns 410 and the bytes become unreachable through the API while
remaining fully readable to anyone with database access. Over a year of routine
compliance exports this is the largest table in the module by a wide margin.

### Why it exists

The retention window was designed as an availability rule for the download
endpoint, and the storage side was never wired to it.

### The fix

1. Add `purge_expired_configuration_audit_exports_task` to `vs_config/tasks.py`:
   for every job past `available_until` with a `storage_name`, delete the stored
   object, blank `storage_name`, and keep the job row as history.
2. Register it in `apps/celery.py` alongside `cleanup-old-import-batches`.
3. Make it idempotent (a missed window must be safe), matching the note at
   `apps/celery.py:15-17`.
4. Consider reusing `vs_exports`' purge rather than writing a second one.

---

## 9. `config.definition.update` can archive and un-archive

**Medium.**

### The defect

`is_active` is a writable field on `ConfigurationDefinitionSerializer`
(`serializers.py:34-39`: it appears in `fields` and not in `read_only_fields`),
and `PATCH /definitions/<key>/` requires only `config.definition.update`
(`views.py:184`).

So `PATCH {"is_active": false}` archives a definition without holding
`config.definition.archive`, and `PATCH {"is_active": true}` un-archives one -
an operation the DELETE route does not offer at all.

### What actually happens

- The separate `config.definition.archive` permission (`constants.py:6`,
  seeded at `seed_config_permissions.py:6`) can be withheld from a role and the
  role can still archive.
- The audit trail records `config.definition.updated`, not
  `config.definition.archived` (`views.py:220-223` versus `:238-242`), so
  searching history for archivals misses these.
- The curated-key guard does cover this: `protected_fields` includes
  `is_active` (`views.py:204-207`), so Platform Settings keys are safe. Only
  ordinary definitions are exposed.
- Un-archiving matters because a definition's stored values are not deleted on
  archive, so flipping `is_active` back makes old values effective again with no
  archive-level authorisation.

Both keys are currently seeded only to the two platform roles, so the practical
blast radius today is small. The permission split is nonetheless decorative.

### Why it exists

The serializer lists the model's fields, and the archive verb was added later as
a separate route without narrowing what PATCH may write.

### The fix

1. Move `is_active` into `read_only_fields` on `ConfigurationDefinitionSerializer`
   and let the DELETE route own archiving.
2. Add an explicit un-archive route (`POST /definitions/<key>/restore/`) gated on
   `config.definition.archive`, writing `config.definition.restored`, since
   un-archiving is a real need with no current home.
3. Check the same pattern on `CapabilitySerializer` (`serializers.py:226-231`).
   There it is harmless because PATCH and DELETE share
   `config.capability.manage` (`views.py:631-632`), but the shape is identical
   and would break the same way if the keys were ever split.

---

## 10. Effective-value reads run one query per definition

**Medium.**

### The defect

```python
# views.py:579-588
for definition in definitions:
    value, source = resolve_value(definition, tenant=tenant, branch=branch)
```

and `resolve_value` issues its own query per call
(`services/resolution.py:86-89`). `ConfigExportView` does the same
(`views.py:1212-1217`).

### What actually happens

`GET /v1/config/effective-values/` with no key resolves the entire active
catalogue: 21 definitions today (`seed_config_catalogue.py:8-113`), so 22
queries per request, and it grows one query per definition anyone adds. Neither
endpoint is paginated, so there is no ceiling. `GET /v1/config/export/` pays the
same cost and then evaluates every capability on top.

The module already knows how to avoid this twice over:
`BulkCapabilityEvaluator` (`services/capabilities.py:239-310`) fixes the query
budget for capabilities, and `runtime_settings._scoped_values`
(`runtime_settings.py:172-196`) fetches an entire scope chain for a key set in
two queries. Neither is used here.

### Why it exists

`resolve_value` was written for the single-key case, which is what
`get_config` needs, and the list endpoints were built by looping it.

### The fix

1. Add `resolve_values(definitions, *, tenant, branch)` modelled on
   `_scoped_values`: one query for all candidate rows across all definitions
   (`definition_id__in=[...], scope_key__in=[...]`), then pick per definition in
   Python. Keep `resolve_value` as a thin wrapper so `get_config` is unchanged.
2. Use it in `EffectiveValueView` and `ConfigExportView`.
3. Add a query-count assertion, the way `test_bulk_evaluation_matches_single_evaluation_with_bounded_queries`
   (`tests.py:246-264`) already does for capabilities.
4. Paginate `/effective-values/` while there, or state a documented cap.

---

## 11. Security settings are re-resolved on every read, on the authentication path

**Medium.**

### The defect

`resolve_security_settings` recurses to resolve its parent baseline:

```python
# runtime_settings.py:265-266
if tenant is not None:
    parent = resolve_security_settings(tenant=tenant) if branch is not None else resolve_security_settings()
```

Each level costs two queries (`runtime_settings.py:174-188`), so a branch-scoped
read is six queries. `validate_security_compliance` calls it again on every
saved field (`runtime_settings.py:336`). Nothing is cached.

### What actually happens

The callers are not administration screens. They are hot paths:

| Caller | When |
|---|---|
| `vs_rbac/authentication.py:41` | every authenticated request riding an open-ended proxy session |
| `vs_user/services/auth.py:184` | every login attempt |
| `vs_user/models.py:408` | account lock evaluation |
| `vs_user/services/password.py:135` | every password reset issue |
| `vs_user/services/invitation.py:42` | every invitation issue |
| `vs_admin_console/services.py:10` | console proxy screens |

So every impersonated request pays up to six extra queries to read six small
integers that change perhaps twice a year. `PATCH /security-settings/` with all
six fields set pays the recursion once per field on top of the writes.

### Why it exists

The read-time clamp is the right design (`runtime_settings.py:222-233` explains
why write-time validation alone cannot hold a baseline), and correctness came
first. Caching was never added.

### The fix

1. Cache the resolved settings per `(tenant_id, branch_id)` with a short TTL in
   `django.core.cache`, and invalidate on any write to a `security.*` key inside
   `set_value` / `clear_value`.
2. Hoist the parent resolution out of the per-field loop in
   `validate_security_compliance`: resolve the parent once and pass it in.
3. Keep the `try/except` fail-safe wrappers
   (`runtime_settings.py:348-377`) in front of the cache, so a cache outage
   degrades to product defaults rather than an exception.

---

## 12. Config changes reach the platform audit trail with `tenant = NULL`

**Medium.**

### The defect

`record_configuration_event` knows the tenant - it is a parameter, and it is
written onto the local row (`services/audit.py:36-45`). It is not passed on to
the mirror:

```python
# services/audit.py:48-55
write_audit_log(
    actor=actor, action=action, target_type=event.target_type,
    target_id=event.target_id,
    detail={"before": before or {}, "after": after or {}, **(metadata or {})},
    branch=branch,
)
```

and `write_audit_log` calls `emit_audit_event` without `tenant=`
(`services/audit.py:94-101`), even though the parameter exists
(`vs_audit/services.py:105`). The branch survives only as a string in
`metadata["branch_id"]` (`services/audit.py:92-93`).

### What actually happens

Every `CONFIG_CHANGED` row in `AuditEvent` carries `tenant = NULL`. The
consequences are the ones `vs_audit`'s own report already documented for the
platform as a whole: the tenant-scoped Export Centre dataset returns almost
nothing, and scoping the admin console is blocked until the column is
backfilled. `vs_config` is one of the writers responsible.

The local `ConfigurationAuditEvent` table is unaffected and remains correctly
scoped, which is why nothing visible in this module breaks.

### Why it exists

`write_audit_log` was written as a minimal coupling point (its own docstring
says so, `services/audit.py:1-11`) before `emit_audit_event` grew a tenant
parameter, and was never revisited.

### The fix

1. Thread `tenant` through `write_audit_log` and pass it to `emit_audit_event`.
   It is a two-line change in the one file the module deliberately keeps as its
   single coupling point.
2. Backfill existing `CONFIG_CHANGED` rows from
   `ConfigurationAuditEvent.tenant`, which has the correct value for every one
   of them, joined on `(target_type, target_id, created_at)`.
3. This belongs in the platform-wide sweep `vs_audit` §4 describes, not as a
   `vs_config`-only fix.

---

## 13. The catalogue seed and migration 0006 disagree

**Medium.**

### The defect

`seed_config_catalogue.py` creates every definition with platform-only scope:

```python
# seed_config_catalogue.py:150
"validation_rules": rules, "allowed_scopes": ["platform"],
```

Migration 0006 then widens the six security keys:

```python
# migrations/0006_enable_scoped_security_overrides.py:13-18
definition_model.objects.filter(key__in=SECURITY_KEYS).update(
    allowed_scopes=["platform", "school", "branch"],
)
```

The seed was never updated to match.

### What actually happens

Today nothing, because the seed uses `get_or_create`
(`seed_config_catalogue.py:145`) and the rows already exist with the widened
scopes. The failure is latent and arrives the first time a security definition
row is recreated - a deleted row, a rebuilt catalogue, a new environment seeded
from the command rather than from migrations, a `loaddata` from a fixture built
before 0006. The row comes back platform-only, and from that moment:

- `PATCH /security-settings/?tenant=<school>` raises
  `InvalidConfigurationScope` (`services/resolution.py:104-107`) - a 422 saying
  the key cannot be configured at school scope;
- the compliance clamp becomes unreachable, so schools silently lose the ability
  to be stricter than the platform;
- nothing in the suite fails, because every test that exercises scoped security
  builds its own definition rows.

### Why it exists

The migration was the right way to change existing databases and the seed is the
right description of a new one. Only one of the two was updated.

### The fix

1. Give `DEFINITIONS` a per-row `allowed_scopes` field and set the six security
   keys to `["platform", "school", "branch"]`, so the seed and the migration
   describe the same catalogue.
2. Add an assertion in the suite that a freshly seeded
   `security.failed_login_threshold` is writable at school scope.
3. Treat the general lesson as the class fix: any data migration that edits
   seeded rows must be mirrored in the seed command, or the seed command must
   stop being a second source of truth.

---

## 14. An engine app imports `vs_schools`

**Medium.**

### The defect

```python
# views.py:91
from vs_schools.models import Currency, OwnershipType, TermStructure
```

```python
# serializers.py:20
from vs_schools.models import Currency, OwnershipType, TermStructure
```

Both are module-level imports, not lazy ones.

### What actually happens

`vs_config` is a domain-neutral platform app - it knows about settings,
capabilities and tenants, and it is imported by `vs_finance`, `vs_user`,
`vs_rbac`, `vs_notifications`, `vs_procurement` and `core`. Importing
`vs_schools` at module load makes the schools app a hard dependency of the
configuration engine, which is precisely the coupling `CLAUDE.md` forbids and
the FAL exists to prevent.

Concretely: the second domain standing on this foundation (`vs_health`) cannot
use the Platform Settings screen without dragging the schools app in, and the
onboarding enumerations it would need are the wrong ones.

The values themselves are school vocabulary too: `OwnershipType`,
`TermStructure` and `Currency` are onboarding defaults for schools, exposed
through `PlatformSettingsView.payload()` (`views.py:362-372`) and validated by
`SchoolOnboardingSettingsSerializer` (`serializers.py:129-133`).

This is the same finding `vs_notifications` carries as its §10.

### Why it exists

The onboarding defaults were the first real consumer of Platform Settings, and
the enumerations lived in `vs_schools`, so the screen reached for them directly.

### The fix

1. **Move the onboarding block behind the FAL.** The school-shaped part of
   Platform Settings belongs in `apps/schools/`, contributed to the config
   screen through a registry rather than imported into it - the same shape
   `vs_payments.provisioning` uses to register its payout ladder with finance.
2. **If that is too large a change now**, make the imports lazy (inside the
   methods that use them, the way every consumer of `vs_config` already imports
   `vs_config`) and add a module-level comment naming the debt. That removes the
   load-time dependency without solving the vocabulary leak.
3. **Do not** solve it by copying the enumerations into `vs_config`. That
   creates two sources of truth for a school's currency list.

---

## 15. Clearing a value files its audit row against the definition

**Low.**

### The defect

```python
# services/resolution.py:123-125
record_configuration_event(
    action="config.value.updated",
    target=row,                     # ConfigurationValue
```

```python
# services/resolution.py:155-157
record_configuration_event(
    action="config.value.cleared",
    target=definition,              # ConfigurationDefinition
```

`record_configuration_event` derives `target_type` and `target_id` from the
object it is handed (`services/audit.py:36-38`), so the two events land under
different types and different ids.

### What actually happens

Filtering the audit trail by `?target_type=ConfigurationValue&target_id=<id>` -
which is exactly what the facets endpoint offers as a one-click filter
(`views.py:1142-1146`) - returns the setting's updates and hides its clears. The
clears are filed under the definition, mixed in with schema changes to that
definition. There is no way to see one setting's full history in one filtered
view.

The same asymmetry exists for entitlements: `config.entitlement.updated` targets
the row, `config.entitlement.cleared` targets the capability
(`services/capabilities.py:135`, `:233`).

### Why it exists

The row is deleted before the event is written, so there is no live object to
point at, and the definition was the nearest thing to hand.

### The fix

Capture `row.pk` before the delete and pass a lightweight target carrying that
id - the same trick `IntegrationConnection` uses
(`services/connections.py:14-18`). A deleted target already resolves to an empty
label rather than an error (`serializers.py:499-534`), so nothing downstream
breaks.

---

## 16. `platform.profile.name` cannot be cleared

**Low.**

### The defect

Every profile field except `name` allows a blank string:

```python
# serializers.py:119-126
name     = serializers.CharField(max_length=160, required=False)
tagline  = serializers.CharField(max_length=255, required=False, allow_blank=True)
address  = serializers.CharField(max_length=255, required=False, allow_blank=True)
email    = serializers.EmailField(required=False, allow_blank=True)
phone    = serializers.CharField(max_length=80,  required=False, allow_blank=True)
website  = serializers.URLField(required=False, allow_blank=True)
logo_url = serializers.URLField(required=False, allow_blank=True)
```

and the clearing branch keys on the blank string:

```python
# views.py:406-411
if key in PROFILE_FIELDS.values() and value == "":
    clear_value(...)
```

### What actually happens

`PATCH {"profile": {"name": ""}}` is a 400 from the serializer, so the branch is
unreachable for `name`. Once a platform name has been saved, there is no
supported way to remove the override and fall back to
`settings.PLATFORM_ISSUER["name"]` - which is the documented fallback
(`platform_settings.py:68-69`) and the whole reason `sources` distinguishes
`"database"` from `"environment"`. The generic
`DELETE /values/platform.profile.name/` is not an escape either: the key is in
`ALL_FIELDS` and the reset route refuses curated keys
(`views.py:322-327`).

The test that should have caught it,
`test_blank_optional_profile_value_clears_database_override`
(`tests.py:491-515`), deliberately uses an *optional* field.

### Why it exists

`name` was treated as mandatory when the screen was written, and the clear
mechanism arrived afterwards.

### The fix

Add `allow_blank=True` to `name`, and extend the existing test to cover every
profile field rather than one.

---

## 17. A CHOICE definition with no `choices` rule can never hold a value

**Low.**

### The defect

```python
# services/resolution.py:35-37
elif kind == definition.ValueType.CHOICE:
    if value not in definition.validation_rules.get("choices", []):
        raise ValueError
```

`ConfigurationDefinitionSerializer` does not require `choices` when
`value_type` is CHOICE (`serializers.py:62-81` validates the default value but
imposes no rule-shape requirement).

### What actually happens

`POST /definitions/ {"value_type": "CHOICE", "validation_rules": {}}` succeeds.
Every subsequent write against it is a 422 reading
*"Value for 'x' is not a valid choice."* with no hint that the definition itself
is the problem. A `default_value` cannot be supplied either, because it would be
validated by the same rule at create time - so the key resolves to `None`
forever.

### Why it exists

Rule shape is validated per value type nowhere; `validation_rules` is a free
`JSONField` and the type check reads whatever happens to be there.

### The fix

Validate rule shape alongside the default in
`ConfigurationDefinitionSerializer.validate`: CHOICE requires a non-empty
`choices` list; numeric types reject a `min` greater than `max`; other types
reject rules they do not use. That is one place, and it closes the whole class.

---

## 18. Audit facets silently sample only the newest 500 events

**Low.**

### The defect

```python
# views.py:1129-1131
target_rows = list(
    qs.select_related(None).only("target_type", "target_id", "metadata")[:500]
)
```

The `targets` dictionary is built from those rows only, then de-duplicated and
capped at 200 (`views.py:1132-1149`). The response carries no truncation flag
(`views.py:1150-1153`), unlike the CSV export, which reports truncation in an
`X-Export-Truncated` header (`views.py:1166`), and unlike the entitlement
calendar, which returns a `truncated` boolean (`views.py:786`, `:827`).

### What actually happens

On a tenant with more than 500 configuration events, the target filter offers
only the targets touched most recently. A setting changed six months ago is
simply absent from the dropdown, and the screen gives the reader no reason to
suspect the list is partial. The `actions` and `target_types` caps (100 each)
and the `actors` cap (200) have the same silence, though they are far less
likely to bind.

### Why it exists

The cap is a sensible guard against an unbounded scan. Reporting it was
overlooked.

### The fix

1. Return `"truncated": true` alongside the dictionaries when any cap binds, so
   the UI can say "showing the 200 most recent targets".
2. Better: derive `targets` from a `DISTINCT ON (target_type, target_id)` query
   rather than a row sample, so the cap applies to distinct targets rather than
   to events.

---

## 19. Smaller defects and dead code

**Low. Grouped by theme.**

### Dead and duplicated code

- **`CapabilitySerializer` defines `to_representation` twice**
  (`serializers.py:213-222` and `:276-282`). Python keeps the second; the first
  is unreachable. They differ - the dead one has no ordering, the live one
  orders by `requires__key` - so the duplication is not even harmless
  copy-paste.
- **`from uuid import UUID` in `views.py:6` is never used.**
- **`BRANCH_SCOPE`, `PLATFORM_SCOPE` and `SCHOOL_SCOPE` are imported into
  `services/resolution.py:5` and never used** (`scope_name` is imported from
  `scopes` instead).
- **`PRODUCT_OWNED_KEYS` is an alias for `SPECIAL_MANAGED_KEYS`**
  (`runtime_settings.py:47`), and `views.py:208` and `:230` rebuild
  `set(ALL_FIELDS.values()) | PRODUCT_OWNED_KEYS` on every request for a set
  that is constant. Compute it once at module level.
- **`_save_curated_values` reverse-maps a key to its field name with a linear
  scan inside the loop** (`views.py:447`). Invert the map once.

### Three implementations of one rule

`_active_entitlement` (`services/capabilities.py:20-38`),
`entitlement_resolution` (`:41-60`) and `BulkCapabilityEvaluator._entitled`
(`:275-285`) each re-implement "which entitlement wins and is it currently
active". They agree today. Nothing keeps them agreeing, and the second one
exists only to return a status string the first one throws away. Collapse them:
`entitlement_resolution` can be the single implementation, with the other two
reading its result.

Similarly, the scope-key construction
`f"branch:{branch.pk}" if branch else f"tenant:{tenant.pk}" if tenant else "platform"`
appears verbatim at `services/resolution.py:110-112`, `:145-147`,
`services/capabilities.py:334-336` and `runtime_settings.py:214-219`, alongside
`ScopedModel.set_scope_key` (`models.py:187-193`) which already does it. One
helper, five callers.

### Duplicated defaults

`SECURITY_DEFAULTS` (`runtime_settings.py:25-32`) restates the same six numbers
that `seed_config_catalogue.py:20-48` writes as definition defaults. Two sources
of truth for the product baseline; a change to one is silently a no-op if the
database has the other.

### Inert enumeration members

`ConfigurationDefinition.Sensitivity` has three values and only
`SECRET_REFERENCE` changes any behaviour (`models.py:92-95`). `PUBLIC` and
`INTERNAL` are indistinguishable everywhere: no serializer, no view and no
service branches on them. Either give `INTERNAL` a masking rule (there is a
`FieldSecurityMixin` pattern already in use elsewhere in the platform) or reduce
the field to a boolean.

### Missing fail-safes and sweeps

- **`GET /security-settings/` has no fail-safe.** It calls
  `resolve_security_settings` directly (`views.py:465-470`), which does
  `int(value)` on whatever is stored (`runtime_settings.py:255`). A value that
  is no longer an integer - after a definition's `value_type` changed, or a
  hand-edited row - makes the screen 500, while every internal consumer degrades
  to product defaults through `get_security_settings`
  (`runtime_settings.py:348-353`). The screen should use the safe reader and
  report the source as degraded.
- **A `RUNNING` export job whose worker died is never reaped.**
  `execute_configuration_audit_export` guards on `status != QUEUED`
  (`services/audit_exports.py:101-102`), so a job stuck in RUNNING stays there
  forever and permanently consumes one of the caller's three slots
  (`services/audit_exports.py:225-233`). `vs_import_data` has
  `mark_stuck_import_jobs_task` for exactly this shape
  (`apps/celery.py:31-34`).

### Audit and ordering gaps

- **Saved views are created and deleted with no audit event**
  (`views.py:965-984`, `:990-996`), and the delete is a hard delete. Minor, but
  it is the one place in the module where a row disappears without a trace.
- **`ConfigurationAuditEvent.Meta.ordering = ["-created_at"]`**
  (`models.py:647`) has no unique tiebreaker, so events sharing a timestamp can
  reorder between pages. `bulk_schedule_entitlements` writes up to 100 events in
  one transaction, which is where the collision would come from.

### Contract mismatches

- **The model docstring names the wrong migration.**
  `ConfigurationAuditEvent` says "migration 0006 installs BEFORE UPDATE /
  BEFORE DELETE triggers" (`models.py:588-591`). The triggers are installed by
  **0003** (`migrations/0003_configuration_audit_immutability.py:12-16`); 0006
  is the security-scope widening. A reader chasing the immutability guarantee
  is sent to the wrong file.
- **The saved-view filter vocabulary is not the list filter vocabulary.**
  `ConfigurationAuditSavedFiltersSerializer` stores `window_days`
  (`serializers.py:405-407`) while the list endpoint reads `created_after` and
  `created_before` (`views.py:938-942`). Nothing in the backend translates
  between them, so replaying a saved view is entirely the client's problem, and
  a client that gets it wrong fails silently by showing the wrong window.
- **The `config.audit.export_downloaded` event is written before the file is
  streamed** (`views.py:1074-1088`). If the stream fails the audit trail records
  a download that did not happen. Defensible - recording an attempt is arguably
  the point - but it should be a deliberate choice, and the action name should
  say `attempted` if it is.
- **`write_audit_log` catches only `ImportError`** (`services/audit.py:102`)
  despite its comment promising best-effort behaviour. In practice
  `emit_audit_event` swallows everything internally and returns `None`
  (`vs_audit/services.py:168-170`), so the promise holds - but it holds because
  of a guarantee made in another module, and `vs_config` never notices that a
  mirror write silently failed.
