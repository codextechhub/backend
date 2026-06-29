# <slice_name> — report template

> Copy this file to `docs/finance/<slice_name>.md` and fill every section.
> The three sections that catch the mistakes our docs keep making are **§3
> (only the fields the serializer/view actually reads)**, **§5 (each formula →
> the function that computes it)**, and **§6 (what survives posting)**. Do not
> write what you *think* happens — open the code and trace it. Cite `file:line`
> on every calculation, field, and posting claim.

---

## 1. What it is (and what it is NOT)

- One-paragraph plain-language description of the concept.
- An explicit **"this does NOT do X"** line. (Example from cost centers: *it is
  not a replacement for the chart of accounts.*) Overclaiming is how the last
  drafts went wrong — state the boundary.

## 2. Domain model

- The model(s)/table(s) and the file they live in (`models/*.py:line`).
- Key fields, types, and **units** — money is always **kobo** (integer minor
  units); say so. Note the `status`/state field and its choices.
- Relationships and **entity/tenant scoping**: every row is scoped to a
  `LedgerEntity`; note any uniqueness constraints (`uniq_*`).

## 3. Endpoint map

One row per route. Pull the path from `urls.py`, the permission key and request
fields from the **view** (not from imagination), the response shape from the
**serializer** / `success_response(...)` call.

| Method + path | permission key | what it does | request body (fields actually read) | response shape |
|---|---|---|---|---|

- Note `?entity=<id\|code>` requirement, and the **global** exceptions
  (currencies, fx-rates) that take no `entity`.
- Under "request body", list **only** fields the view/serializer reads. If the
  view prices a line from `unit_price`×`quantity`, do not document an `amount`
  field — it is silently ignored.

## 4. Lifecycle / state machine

- The states a record moves through and **which action endpoint** drives each
  transition (`draft → submitted → posted → settled …`).
- This is the "how it navigates" part — the path you trace by hand from the
  endpoints.

## 5. Calculations

- Every formula, **in kobo**, with its rounding rule, each pointing at the
  function that computes it (`file:line`). Examples: WHT, net-settled, fee,
  depreciation, budget variance, running balance.
- Show the formula symbolically **and** with one real number.

## 6. What posting does to the ledger

- The exact journal lines generated (**Dr / Cr**, which account, by code).
- **CRITICAL:** what is *carried* vs *dropped* on posting — aggregation keys,
  whether `cost_center` survives, what control accounts are resolved. Trace the
  `post_*` function; do not assume the posted journal mirrors the source
  document field-for-field.

## 7. Worked example

- A real request → real response JSON → resulting journal, with numbers traced
  through the code (not hand-written).

## 8. Gotchas / known limitations

- Discrepancies between intent and behavior, edge cases, things that silently
  no-op or get discarded. Park any reviewer-flagged issues here until fixed.

## 9. Permissions & tenant isolation

- The `rbac_permission` verbs each endpoint requires (view vs create/update).
- How cross-tenant access is blocked — usually the entity resolver. Can a `pk`
  or `?entity=` swap read/write another tenant's rows?

## 10. Code map

- `file` → responsibility, so a future reader knows where to look.

## 11. Test coverage & gaps

- What's tested (403, cross-tenant isolation, happy path, empty-list shape) and
  what is not.
