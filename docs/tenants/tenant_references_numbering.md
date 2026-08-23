# tenant_references_numbering

Two small, heavily-used services and the table behind one of them: turning a
user-supplied tenant or branch reference into a row without leaking which ids
exist, and allocating human-facing document numbers that cannot collide between
tenants sharing a document code.

The tenant model is `tenant_identity_lifecycle`; sites are
`tenant_sites_branches`; the request context is `tenant_request_context`.

**No HTTP routes.** Both modules are imported by other apps' serializers, views
and management commands.

Findings for the whole module are collected in
**`error/tenants/tenant_code_issues.md`**; §8 below points at the ones belonging
here rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **A branch reference is only ever a decimal id.** `Branch` is keyed by an
  ordinary auto-incrementing integer, so anything arriving from a request body, a
  query parameter or an import row is a base-10 string and nothing else
  (`references.py:3-5`).
- **Three questions, one function.** Is the value a usable integer, does the row
  exist, and does it belong to the tenant the caller is entitled to? Getting any
  of them wrong has bitten this repo before, and the module docstring lists all
  three failures by name (`references.py:9-19`).
- **Foreign and unknown are deliberately indistinguishable.** Checking tenancy
  *after* the row is fetched lets a caller tell "someone else's branch" apart
  from "no such branch", which is an id oracle (`references.py:14-16`).
- **A blank reference is a valid answer, not missing data.**
  `resolve_branch_reference` returns `None` for it - "no branch" is a real,
  first-class scope (`references.py:88-90`).
- **`find_tenant` accepts a pk *or* a slug; `find_branch_in_tenant` accepts only
  a pk.** The asymmetry is intentional: `Tenant.slug` is the stable
  human-readable identifier and the sign-in subdomain, while a branch has no
  slug at all (`references.py:36-46`).
- **`find_tenant` was written for operator commands**, because one email address
  can now be a login at several tenants, so an email no longer identifies one
  account (`references.py:43-46`).
- **`find_branch_in_tenant` reads `all_objects` on purpose.** The explicit tenant
  filter is the security boundary, and it must not depend on ambient
  request-local tenant state (`references.py:79-83`).
- **Document numbers are allocated per tenant, never per branch or per entity.**
  Routing every human-facing reference through one tenant-level counter is what
  stops ledger entities and branches owned by the same tenant opening competing
  series for the same document code (`models.py:754-759`).
- **The sequence row is protected twice**: a database uniqueness constraint on
  `(tenant, document_code, date)` and a `SELECT … FOR UPDATE` row lock
  (`numbering.py:16-18`).
- **The counter restarts every local date.** `IV-72607221` and `IV-72607231` are
  the first invoice of two consecutive days, not the first and eleventh of one
  (`numbering.py:14-16`).
- **This is not a uniqueness guarantee for the produced string.** The unique
  constraint is on the sequence *row*; nothing checks the assembled reference,
  and the assembled reference has no separator between the tenant id and the date
  (`tenant_code_issues` §14).

## 2. Domain model

### `TenantDocumentSequence` (`models.py:753`)

| Field | Meaning |
|---|---|
| `tenant` | FK, PROTECT, `related_name="document_sequences"` |
| `document_code` | `CharField(16)`, upper-cased by the allocator |
| `date` | `DateField` - the *local* date the number was allocated for |
| `last_number` | `PositiveIntegerField`, default 0 |
| `created_at`, `updated_at` | Standard |

Constraint: `uniq_tenant_docseq_code_date` on
`(tenant, document_code, date)`. Index: `tenant_docseq_tenant_date_idx` on
`(tenant, date)`.

`__str__` is `f"{tenant_id}/{document_code}/{date}: {last_number}"`.

There is one row per tenant per document code per day, for ever. Nothing prunes
them (`tenant_code_issues` §15).

### `TenantOwnedModel` (`models.py:739`)

```python
class TenantOwnedModel(models.Model):
    """Abstract contract for rows owned by exactly one tenant."""
    tenant = models.ForeignKey(
        "vs_tenants.Tenant", on_delete=models.PROTECT, related_name="+", db_index=True,
    )
    class Meta:
        abstract = True
```

Declared as a contract; **no model in the repo inherits it**. Every tenant-owned
model in the codebase declares its own `tenant` FK by hand, and they do not all
agree - `Branch.tenant` uses `related_name="branches"`,
`TenantRoleTemplate.tenant` uses `"role_templates"`, and this base would have
given all of them `"+"` (`tenant_code_issues` §8).

### The reference constants (`references.py:25-31`)

| Constant | Value |
|---|---|
| `_MAX_BIGINT` | `9_223_372_036_854_775_807` - anything above it is a bad request, not a lookup, because PostgreSQL raises rather than returning no rows |
| `BRANCH_NOT_FOUND` | `"No such branch in this tenant."` |
| `TENANT_NOT_FOUND` | `"No such tenant."` - **defined and used by nothing** |

## 3. Endpoint map

**None.** The consumers:

| Function | Called by |
|---|---|
| `find_tenant(ref)` | `core.management.commands.create_superuser`, `core.management.commands.delete_user`, `vs_user.management.commands.repair_pending_user_approvals` |
| `find_branch_in_tenant(tenant, ref)` | `vs_config.services.scopes` |
| `resolve_branch_reference(tenant, ref, field="branch")` | `vs_user.serializers`, `vs_procurement.views.base` (via a thin local wrapper), and through it `vs_procurement.views.stock` |
| `next_tenant_document_number(tenant=…, document_code=…, allocation_date=None)` | `schools.vs_schools.models.School.save()` for `SC-…` codes, and the finance / procurement / payments numbering |

Two `core` management commands additionally define their own private
`_resolve_tenant(ref)` wrapper around `find_tenant`, identical in both files
(`core/management/commands/create_superuser.py:36-43`,
`core/management/commands/delete_user.py:52-59`) - see `tenant_code_issues` §16.

## 4. Lifecycle / state machine

Neither service has one. The sequence row's only transition is
`last_number += 1`, and it never goes down or resets - a new day gets a new row
rather than resetting an old one.

```
next_tenant_document_number(tenant=T, document_code="iv", allocation_date=None)
        │
        ├─ tenant is None or unsaved      → ValueError
        ├─ document_code blank            → ValueError
        │
        ├─ code = "IV"          (stripped, upper-cased)
        ├─ day  = timezone.localdate()    (or the supplied date)
        │
        ├─ get_or_create(tenant=T, document_code="IV", date=day, last_number=0)
        ├─ SELECT … FOR UPDATE on that row
        ├─ last_number += 1  →  save(update_fields=["last_number", "updated_at"])
        │
        └─ return f"IV-{T.pk}{day:%y%m%d}{last_number}"
```

The whole function is wrapped in `@transaction.atomic`
(`numbering.py:10`), so the lock is held to the end of the enclosing
transaction and two concurrent callers serialise.

## 5. Derivations

### The document number

```python
return f"{code}-{tenant.pk}{day:%y%m%d}{sequence.last_number}"
```

`numbering.py:39`. Four parts, one separator:

| Part | Example |
|---|---|
| `code` | `IV` - stripped and upper-cased, so `"iv"` and `"IV"` share a series |
| `-` | the only separator in the string |
| `tenant.pk` | `7` - variable width |
| `YYMMDD` | `260722` - fixed six digits |
| `last_number` | `1` - variable width, **unpadded** |

So tenant 7's first invoice on 22 July 2026 is `IV-72607221`, its second is
`IV-72607222`, and its first on the 23rd is `IV-72607231`. Those three strings
are exactly what `tests.py::TenantDocumentNumberTests` asserts
(`tests.py:81-113`).

Two tenants get independent series for the same code, and one tenant gets
independent series for two codes - both asserted at `tests.py:91-103`.

The `-` sits between the code and everything else, and there is no separator
between the tenant id and the date, nor between the date and the sequence. The
fixed six digits in the middle are what keeps the string parseable in practice;
uniqueness is not guaranteed by construction (`tenant_code_issues` §14).

### Resolving a branch

```python
def find_branch_in_tenant(tenant, ref):
    if tenant is None or ref in (None, ""):
        return None
    raw = str(ref).strip()
    if not raw.isdigit() or int(raw) > _MAX_BIGINT:
        return None
    return Branch.all_objects.filter(tenant=tenant, pk=int(raw)).first()
```

`references.py:62-83`. Five inputs collapse to `None`:

| Input | Why |
|---|---|
| No tenant | Nothing to scope against |
| Blank reference | "No branch" is a valid answer |
| Non-numeric (`"abc"`, `"3a"`, `"-1"`) | `.isdigit()` is false, so it never reaches the ORM |
| Larger than a signed bigint | PostgreSQL raises on the cast, so a 500 would replace a 400 |
| A real id owned by another tenant | The `tenant=` filter excludes it |

The last two rows are the ones that make it a security helper rather than a
convenience: the oversized guard turns a 500 into a 400, and the tenant filter
being *in the query* rather than a post-fetch comparison is what removes the id
oracle.

`resolve_branch_reference` puts the standard error on top
(`references.py:86-98`):

```python
if ref in (None, ""):
    return None
branch = find_branch_in_tenant(tenant, ref)
if branch is None:
    raise ValidationError({field: BRANCH_NOT_FOUND})
return branch
```

`field` is a parameter so a serializer with two branch columns can key the error
correctly.

### Resolving a tenant

```python
def find_tenant(ref):
    if ref in (None, ""):
        return None
    raw = str(ref).strip()
    if not raw:
        return None
    if raw.isdigit():
        if int(raw) > _MAX_BIGINT:
            return None
        return Tenant.objects.filter(pk=int(raw)).first()
    return Tenant.objects.filter(slug=raw.lower()).first()
```

`references.py:34-59`. Numeric goes to the pk, anything else is lower-cased and
tried as a slug. It is **not** tenant-scoped - it cannot be, since it is
resolving the tenant itself - and it is not status-filtered either, so a
SUSPENDED tenant resolves. That is right for the operator commands it was
written for and wrong for anything request-facing, which is why nothing
request-facing uses it.

Note the asymmetry with `find_branch_in_tenant`: a branch id above `_MAX_BIGINT`
returns `None`, and so does a tenant id above it - but a *tenant* reference that
is non-numeric falls through to a slug lookup, while a non-numeric branch
reference is refused outright.

### The two ValueErrors

```python
if tenant is None or tenant.pk is None:
    raise ValueError("Cannot allocate a document number without a saved tenant.")
code = (document_code or "").strip().upper()
if not code:
    raise ValueError("A document code is required.")
```

`numbering.py:22-26`. These are programming errors, not user input, so they raise
`ValueError` rather than a DRF `ValidationError` - and `core.exceptions` has no
branch for `ValueError`, so either would surface as an unhandled 500 if it ever
reached a request. Both are unreachable from a request path: every caller
supplies a saved tenant and a literal code.

## 6. What writing writes

| Operation | Rows |
|---|---|
| `next_tenant_document_number` | One `TenantDocumentSequence` on first use for a `(tenant, code, date)`, then one UPDATE per call |
| Everything else in this slice | Nothing. All three reference functions are pure reads |

No audit events, no signals, no cache.

The `get_or_create` followed by a separate `select_for_update().get()`
(`numbering.py:29-36`) is two queries where one would do, and there is a narrow
window between them - but the unique constraint makes a concurrent `get_or_create`
lose rather than duplicate, and the subsequent `get()` then finds the winner's
row and locks it. The pattern is correct; it is just not the tightest version.

## 7. Worked example

**1. A school's code.** `School.save()` allocates one on first save
(`schools/vs_schools/models.py:288-295`):

```python
self.code = next_tenant_document_number(tenant=self.tenant, document_code="SC")
```

Bright Star's tenant is pk 7 and it is created on 22 July 2026, so the code is
`SC-72607221`. Greenfield, created the same day on tenant 8, gets `SC-82607221`.
`School.code` is `unique=True` platform-wide, and it is safe to fill from a
per-tenant counter precisely because the tenant pk is inside the string.

**2. A second school on the same tenant.** Cannot happen -
`School.tenant` is a `OneToOneField` - but the mechanism would handle it:
`SC-72607222`.

**3. Two invoices at once.** Two bursars at Bright Star post an invoice in the
same second. Both calls enter `@transaction.atomic`, both `get_or_create` the
same `(7, "IV", 2026-07-22)` row - one creates it, the other loses the race and
finds it - and both then attempt `SELECT … FOR UPDATE`. One acquires the lock,
increments to 1, commits; the other blocks, then reads `last_number = 1` and
increments to 2. `IV-72607221` and `IV-72607222`. Neither can read a stale value,
because the lock is taken *after* the row is guaranteed to exist.

**4. Midnight.** The next invoice, at 00:01 on the 23rd, gets a fresh row and
`IV-72607231`. `timezone.localdate()` means the boundary follows the project
timezone, not UTC - so a tenant in a different timezone still rolls over at the
platform's midnight, not its own.

**5. A branch reference from a request body.** A user-creation payload carries
`{"branch": "14"}`. `vs_user.serializers` calls
`resolve_branch_reference(target_tenant, "14")`. `"14".isdigit()` passes, 14 is
under the bigint ceiling, and `Branch.all_objects.filter(tenant=target_tenant,
pk=14)` finds Bright Star's Lekki site. The branch is returned.

**6. The same payload against the wrong tenant.** A Greenfield admin submits
`{"branch": "14"}` for a Greenfield user. The filter excludes it - branch 14
belongs to Bright Star - so `find_branch_in_tenant` returns `None` and
`resolve_branch_reference` raises
`{"branch": "No such branch in this tenant."}`. That is byte-identical to the
error for `{"branch": "999999"}`, which does not exist anywhere, and for
`{"branch": "abc"}`, which is not an id at all. The Greenfield admin cannot use
the field to learn that branch 14 exists.

**7. The oversized case.** `{"branch": "99999999999999999999"}` is all digits, so
it passes `.isdigit()`, and is then caught by the `_MAX_BIGINT` check. Without
that line, PostgreSQL would raise on the cast and the caller would get a 500
instead of the 400 they deserve.

**8. An operator finds a tenant.** `manage.py delete_user --tenant_id corona` -
`find_tenant("corona")` is non-numeric, so it looks up the slug. `--tenant_id 7`
is numeric, so it looks up the pk. Both work, which is what the command's
`--tenant_id` name obscures: the argument accepts either.

## 8. Gotchas / known limitations

Recorded in full in **`error/tenants/tenant_code_issues.md`**. The items
belonging to this slice:

| # in that file | One line |
|---|---|
| §8 | `TenantOwnedModel` is an abstract contract no model in the repo signs |
| §14 | The document reference has no separator between the tenant id and the date, so its uniqueness rests on the date's fixed width rather than on construction |
| §15 | `TenantDocumentSequence` rows accumulate one per tenant per code per day, for ever, with no pruning |
| §16 | Four tenant-reference resolvers exist across the repo, two of them byte-identical copies |
| §17 | `TENANT_NOT_FOUND` is defined and used by nothing |

Design choices worth stating as choices - both modules are small and almost
entirely right:

- **Collapsing five failure modes to `None`** (`references.py:14-19`) is what
  removes the id oracle; the docstring names all three prior failures.
- **The `_MAX_BIGINT` guard** (`references.py:25-27`, `76`) turns a database
  error into a validation error, and the same constant appears independently in
  `vs_rbac.serializers.tenant` for the same reason.
- **`all_objects` with an explicit `tenant=`** (`references.py:79-83`) makes the
  boundary the argument rather than the ambient request state.
- **A blank reference resolving to `None` rather than raising**
  (`references.py:88-90`), because "no branch" is a real scope in this platform.
- **Tenant-level, not entity-level, numbering** (`models.py:754-759`), so two
  entities under one tenant cannot open competing series.
- **The lock after the `get_or_create`** (`numbering.py:29-36`), so the row is
  guaranteed to exist before it is locked.
- **`timezone.localdate()` rather than `date.today()`** (`numbering.py:27`), so
  the day boundary follows the project timezone.

## 9. Permissions & tenant isolation

- **Neither module enforces a permission**, and neither should: they are called
  *after* a view has decided the caller may act, to turn a reference into a row.
- **`find_branch_in_tenant` is a security boundary**, and the boundary is the
  `tenant=` argument the caller supplies. It reads `all_objects` precisely so the
  answer does not silently change with the ambient request context - a caller
  that passes the wrong tenant gets the wrong answer loudly rather than the right
  answer by accident.
- **`find_tenant` is not scoped and cannot be.** It resolves the boundary itself,
  so every caller is responsible for deciding whether the resulting tenant is one
  they may act on. All three callers are management commands, where shell access
  is the authorisation.
- **`find_tenant` does not filter by status**, so a SUSPENDED or INACTIVE tenant
  resolves. Correct for an operator repairing a suspended school; wrong for
  anything request-facing, and nothing request-facing uses it.
- **The document number leaks the tenant's primary key.** `SC-72607221` names
  tenant 7 to anyone holding the string. Primary keys are described as
  "deliberately internal" (`models.py:73`), and this is the one place one is
  published. It is an internal counter rather than a secret, and the alternative -
  embedding the slug - would break the reference on a pre-go-live rename, so the
  trade is defensible; it is worth knowing it was made.
- **The sequence table is tenant-owned and `PROTECT`ed**, so a tenant with any
  allocated numbers cannot be deleted - consistent with every other tenant-owned
  table.

## 10. Code map

| File | What lives there |
|---|---|
| `vs_tenants/references.py:1-20` | The module docstring, which is the clearest statement of the three failure modes in the repo |
| `vs_tenants/references.py:25-31` | `_MAX_BIGINT`, `BRANCH_NOT_FOUND`, `TENANT_NOT_FOUND` |
| `vs_tenants/references.py:34-59` | `find_tenant` |
| `vs_tenants/references.py:62-83` | `find_branch_in_tenant` |
| `vs_tenants/references.py:86-98` | `resolve_branch_reference` |
| `vs_tenants/numbering.py` | `next_tenant_document_number`, in full |
| `vs_tenants/models.py:739-750` | `TenantOwnedModel` (unused) |
| `vs_tenants/models.py:753-784` | `TenantDocumentSequence` |
| `vs_tenants/migrations/0003_tenantdocumentsequence.py` | The table |
| `schools/vs_schools/models.py:288-297` | The `SC-…` caller |
| `vs_config/services/scopes.py:68` | The `find_branch_in_tenant` caller |
| `vs_user/serializers.py:347` | The `resolve_branch_reference` caller |
| `vs_procurement/views/base.py:148-160` | A thin local wrapper over `resolve_branch_reference` |
| `core/management/commands/create_superuser.py:36-43` | A private `_resolve_tenant` over `find_tenant` |
| `core/management/commands/delete_user.py:52-59` | The same function again |
| `vs_rbac/serializers/tenant.py:56-59` | An independent copy of the `_MAX_BIGINT` reasoning |

## 11. Test coverage & gaps

Module baseline: **`Ran 62 tests in 4.805s` - OK**.

Covered for this slice:

- `tests_branch_references.py::FindBranchInTenantTests` - the blank reference,
  the non-numeric reference, the oversized reference, the foreign branch and the
  happy path, each asserting `None` rather than an exception.
- `tests_branch_references.py::ResolveBranchReferenceTests` - that a blank
  resolves to `None`, that anything unresolvable raises with the standard
  message, and that the `field` parameter keys the error.
- `tests.py::TenantDocumentNumberTests` - the exact format string, that a
  lower-case code shares a series with its upper-case form, that two tenants and
  two document codes have independent series, and that the next date restarts at
  one.

Not covered:

- **`find_tenant` has no test at all.** Neither the pk branch, the slug branch,
  the blank guard, the oversized guard, nor the fact that it resolves a suspended
  tenant.
- No concurrency test on `next_tenant_document_number` - the row lock is
  asserted only by reading the code. `Branch.allocate_next_code` has one
  (`tests.py::BranchDatabaseConstraintTests`), and this allocator does not.
- No test for either `ValueError` branch in `numbering.py`.
- `TenantOwnedModel` has no test, because it has no subclass (§8).
- Nothing asserts anything about `TenantDocumentSequence` row growth (§15).
- `TENANT_NOT_FOUND` has no test, because it has no caller (§17).
