# Branch Import Template — Reference Document

*Refreshed 2026-06-12. The original version carried a known-bugs checklist;
every item on it is now fixed in code, so this version documents current
behaviour. (Moved here from the repo root.)*

## Overview

The branches import template is fully defined in
`core/management/commands/seed_import.py` under the code
`branches_master_v1`. Seed it (idempotent) with:

```bash
python manage.py seed_import
```

(`./reseed-dev.sh` runs this automatically for local databases.)

Prerequisite: **Schools must be imported first.** Every branch row references
an existing school via `School Slug`.

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

## Column Definitions (13 columns)

### Group 1 — School Linkage

| # | Column Name | target_field | data_type | required | notes |
|---|---|---|---|---|---|
| 1 | School Slug | `school_slug` | string | ✅ | Cross-references `School.slug` (`reference_model=School`, `reference_lookup_field=slug`) |

### Group 2 — Branch Identity

| # | Column Name | target_field | data_type | required | notes |
|---|---|---|---|---|---|
| 2 | Branch Name | `name` | string | ✅ | max 255. Duplicate name within the same school → SKIP |
| 3 | Branch Type | `_type` | string | ❌ | Free-form: Primary, Secondary, Nursery, Combined… max 80. Defaults to "Combined" |
| 4 | Is Main Branch | `is_main` | boolean | ✅ | One TRUE per school. Executor accepts "true"/"1"/"yes" (case-insensitive) |

### Group 3 — Contact & Location

| # | Column Name | target_field | data_type | required | notes |
|---|---|---|---|---|---|
| 5 | Address | `address` | string | ❌ | max 255 |
| 6 | Email | `email` | email | ❌ | Branch contact email |
| 7 | Country | `country` | string | ❌ | default Nigeria, max 80 |
| 8 | State | `state` | string | ❌ | max 120 |

### Group 4 — Lifecycle

| # | Column Name | target_field | data_type | required | notes |
|---|---|---|---|---|---|
| 9 | Opened Date | `opened_at` | date | ❌ | YYYY-MM-DD; defaults to now() when blank. (A former "Status" column was removed — branches are always created PENDING and activated through the lifecycle flow.) |

### Group 5 — Branch Admin

| # | Column Name | target_field | data_type | required | unique | notes |
|---|---|---|---|---|---|---|
| 10 | Admin Full Name | `branch_admin_full_name` | string | ✅ | ❌ | max 120 |
| 11 | Admin Email | `branch_admin_email` | email | ✅ | ✅ | Creates user + queues invite. Must not already exist |
| 12 | Admin Phone | `branch_admin_phone` | string | ❌ | ❌ | max 32 |
| 13 | Admin Role | `branch_admin_role` | string | ❌ | ❌ | default "Head Teacher", max 80 |

---

## Executor Flow (`import_branches_row`)

File: `vs_import_data/services/import_executor.py`

1. **School resolution** (priority order):
   - `import_batch.school` set → use it directly (school-scoped batch)
   - else `payload["school_slug"]` → `School.objects.get(slug=...)`
   - else `payload["school_code"]` → `School.objects.get(code=...)`
   - else → row **FAILS**
2. **Duplicate check**: same `(school, name)` already exists → action **SKIP**
3. Build branch payload (`_type`, `opened_at` included) → `BranchCreateSerializer`
4. **Serializer validation**: second main branch → FAIL; admin email already a
   User → FAIL; missing `primary_admin_data` → FAIL
5. **Atomic create**: Branch (status PENDING, integer code auto-allocated per
   school) + BranchLifecycle + branch_admin RBAC role + ContactInfo +
   BranchPrimaryAdmin + `provision_admin_user` (creates User, queues invite —
   the invite email appears in the sender's queue page as a tracked job)

---

## Validator Checks (`_validate_branches_rules`)

File: `vs_import_data/services/validation_service.py`

| Check | Fires when |
|---|---|
| `school_slug` resolves to an existing school | batch not school-scoped, slug provided |
| `school_code` resolves | no slug, code provided |
| Either slug or code required | batch not school-scoped, both blank |
| `branch_admin_email` not already a User | always |
| Within-file admin-email uniqueness | same email on multiple rows |
| Admin full name not empty when email present | always |
| `is_main=TRUE` once per school (within-file **and** against DB) | always |

---

## Sample CSV Row

```
School Slug,Branch Name,Branch Type,Is Main Branch,Address,Email,Country,State,Opened Date,Admin Full Name,Admin Email,Admin Phone,Admin Role
greenfield-academy,Lekki Campus,Secondary,TRUE,14 Admiralty Way Lekki,lekki@greenfieldacademy.edu.ng,Nigeria,Lagos,2009-09-01,Mr. Emeka Obi,head.lekki@greenfieldacademy.edu.ng,08061234567,Head Teacher
greenfield-academy,Ajah Campus,Primary,FALSE,22 Ajah Expressway,ajah@greenfieldacademy.edu.ng,Nigeria,Lagos,2015-03-15,Mrs. Ngozi Ibe,head.ajah@greenfieldacademy.edu.ng,08062345678,Head Teacher
```

---

## Historical note

The original version of this document listed three executor bugs and two
validator gaps. All are resolved:

- ✅ Branch Type read from the correct `_type` field (the `branch_type` read
  in `import_schools_row` is a *different* template's field and is correct)
- ✅ `opened_at` passed through to the serializer
- ✅ Status column removed from the template (branches always start PENDING)
- ✅ Admin full-name emptiness validated pre-import
- ✅ Within-file + DB `is_main` conflict validated pre-import
