# Branch Import Template — Reference Document

## Overview

The branches import template is already **fully defined** in `seed_import.py` under the code `branches_master_v1`. It must be seeded into the DB before it can be used.

```
python manage.py seed_import_templates --dataset-type branches
```

Prerequisite: **Schools must be imported first.** Every branch row references an existing school via `School Slug`.

---

## Template Metadata

| Field | Value |
|---|---|
| `code` | `branches_master_v1` |
| `name` | Branches Master Import |
| `dataset_type` | `branches` |
| `status` | Active |
| `default_file_format` | XLSX |
| `allow_sample_row` | True |
| `is_download_enabled` | True |

**`validation_rules` (JSON):**
```json
{
  "require_school_slug": true,
  "max_main_branches_per_school": 1,
  "auto_allocate_branch_code": true
}
```

---

## Column Definitions (14 columns)

### Group 1 — School Linkage

| # | Column Name | target_field | data_type | required | unique | default | notes |
|---|---|---|---|---|---|---|---|
| 1 | School Slug | `school_slug` | string | ✅ | ❌ | — | Cross-references `School.slug`. `reference_model=School`, `reference_lookup_field=slug` |

### Group 2 — Branch Identity

| # | Column Name | target_field | data_type | required | unique | default | notes |
|---|---|---|---|---|---|---|---|
| 2 | Branch Name | `name` | string | ✅ | ❌ | — | max 255. Duplicate name within the same school → SKIP |
| 3 | Branch Type | `_type` | string | ❌ | ❌ | — | Free-form: Primary, Secondary, Nursery, Combined, etc. max 80 |
| 4 | Is Main Branch | `is_main` | boolean | ✅ | ❌ | FALSE | Only one TRUE allowed per school. Executor accepts "true", "1", "yes" (case-insensitive) |

### Group 3 — Contact & Location

| # | Column Name | target_field | data_type | required | unique | default | notes |
|---|---|---|---|---|---|---|---|
| 5 | Address | `address` | string | ❌ | ❌ | — | max 255 |
| 6 | Email | `email` | email | ❌ | ❌ | — | Branch contact email |
| 7 | Country | `country` | string | ❌ | ❌ | Nigeria | max 80 |
| 8 | State | `state` | string | ❌ | ❌ | — | max 120 |

### Group 4 — Lifecycle

| # | Column Name | target_field | data_type | required | unique | default | notes |
|---|---|---|---|---|---|---|---|
| 9 | Status | `status` | choice | ✅ | ❌ | Pending | ⚠️ See known issues — executor ignores this, always creates as PENDING |
| 10 | Opened Date | `opened_at` | date | ❌ | ❌ | — | YYYY-MM-DD. ⚠️ See known issues — executor currently ignores this |

### Group 5 — Branch Admin

| # | Column Name | target_field | data_type | required | unique | default | notes |
|---|---|---|---|---|---|---|---|
| 11 | Admin Full Name | `branch_admin_full_name` | string | ✅ | ❌ | — | max 120 |
| 12 | Admin Email | `branch_admin_email` | email | ✅ | ✅ | — | Creates user + sends invite. Must not already exist |
| 13 | Admin Phone | `branch_admin_phone` | string | ❌ | ❌ | — | max 32 |
| 14 | Admin Role | `branch_admin_role` | string | ❌ | ❌ | Head Teacher | max 80. Examples: Head Teacher, Campus Director, Principal |

---

## Executor Flow (`import_branches_row`)

File: `vs_import_data/services/import_executor.py`

1. **School resolution** (in priority order):
   - If `import_batch.school` is set → use it directly (school-scoped batch)
   - Else read `payload["school_slug"]` → look up `School.objects.get(slug=...)`
   - Else read `payload["school_code"]` → look up `School.objects.get(code=...)`
   - Else → row **FAILS** with ValueError

2. **Duplicate check**: if `Branch.objects.filter(school=school, name=branch_name).exists()` → action = **SKIP**

3. **Build branch payload** → pass to `BranchCreateSerializer`

4. **BranchCreateSerializer.validate()** checks:
   - If `is_main=True` and the school already has a main branch → **FAILS**
   - If `branch_admin_email` already exists as a User → **FAILS**
   - `primary_admin_data` is required — if absent → **FAILS**

5. **BranchCreateSerializer.create()** (atomic):
   - Creates `Branch` with `status=PENDING` (hardcoded)
   - Sets `opened_at=now()` if not provided
   - Creates `BranchLifecycle` record
   - Provisions `branch_admin` RBAC role
   - Creates `ContactInfo` + `BranchPrimaryAdmin` link
   - Calls `provision_admin_user` → creates User + queues invite

---

## Validator Checks (`_validate_branches_rules`)

File: `vs_import_data/services/validation_service.py`

| Check | Column(s) | Fires when |
|---|---|---|
| `school_slug` resolves to existing school | School Slug | batch not school-scoped AND slug provided |
| `school_code` resolves to existing school | — | batch not school-scoped AND no slug, but code provided |
| Either slug or code required | — | batch not school-scoped AND both blank |
| `branch_admin_email` not already a User | Admin Email | always |
| Within-file email uniqueness | Admin Email | same email appears in multiple rows |

**Not yet validated** (caught by serializer at execution time):
- `is_main=TRUE` on a school that already has a main branch
- `branch_admin_full_name` empty when email is present (fix: add same check as schools validator)

---

## Known Executor Bugs (must fix before use)

### Bug 1 — Branch Type always defaults to "Combined"

**Root cause:** executor reads `_s("branch_type")` but the template's `target_field` is `_type`.
`map_row_to_payload` puts the value in `payload["_type"]`, so `payload.get("branch_type")` is always empty.

**Fix in `import_executor.py`:**
```python
# BEFORE
"_type": _s("branch_type") or "Combined",

# AFTER
"_type": _s("_type") or "Combined",
```

### Bug 2 — Opened Date column is ignored

**Root cause:** executor never reads `opened_at` from the payload.
`BranchCreateSerializer` has the field; it just never gets passed.

**Fix in `import_executor.py`:** add `opened_at` to the branch payload:
```python
opened_at_raw = _s("opened_at")
if opened_at_raw:
    branch_payload["opened_at"] = opened_at_raw
```

### Bug 3 — Status column is ignored (minor, acceptable)

`BranchCreateSerializer.create()` always sets `status=BranchStatus.PENDING` regardless.
The "Status" column exists in the template but has no effect.

**Options:**
- Remove the "Status" column from the template (simplest)
- Pass `status` through the payload and let the serializer handle it (requires serializer change)

---

## Pre-import Validation Gaps to Add

The following checks exist in `_validate_schools_rules` but are missing from `_validate_branches_rules`:

1. **Admin `full_name` not empty when email present** — same pattern as schools validator
2. **`is_main=TRUE` conflict** — check within-file: if two rows for the same school both have `is_main=TRUE`, flag the second one during validation instead of letting it fail at execution time

---

## Sample CSV Row

```
School Slug,Branch Name,Branch Type,Is Main Branch,Address,Email,Country,State,Status,Opened Date,Admin Full Name,Admin Email,Admin Phone,Admin Role
greenfield-academy,Lekki Campus,Secondary,TRUE,14 Admiralty Way Lekki,lekki@greenfieldacademy.edu.ng,Nigeria,Lagos,Active,2009-09-01,Mr. Emeka Obi,head.lekki@greenfieldacademy.edu.ng,08061234567,Head Teacher
greenfield-academy,Ajah Campus,Primary,FALSE,22 Ajah Expressway,ajah@greenfieldacademy.edu.ng,Nigeria,Lagos,Active,2015-03-15,Mrs. Ngozi Ibe,head.ajah@greenfieldacademy.edu.ng,08062345678,Head Teacher
```

---

## Action Checklist

- [ ] Fix executor bug 1: `_s("branch_type")` → `_s("_type")`
- [ ] Fix executor bug 2: pass `opened_at` from payload to `branch_payload`
- [ ] Decide on Status column (remove from template, or wire through serializer)
- [ ] Add `branch_admin_full_name` emptiness check to `_validate_branches_rules`
- [ ] Add within-file `is_main` conflict check to `_validate_branches_rules`
- [ ] Run `python manage.py seed_import_templates --dataset-type branches` to seed the template
- [ ] Run `python manage.py seed_import_templates --dataset-type branches --dry-run` first to verify
