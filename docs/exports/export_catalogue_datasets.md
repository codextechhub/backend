# export_catalogue_datasets

The half of the Export Centre that decides **what may be exported**: the
catalogue vocabulary (`Field`, `FilterDef`, `Dataset`), the in-process registry
the domain apps publish into, the filter compiler that turns a saved filter into
a `Q`, the value renderer that decides how a cell reads, and the screen bindings
behind "export what this table is showing".

Routes covered here (`apps/urls.py`, mounted at `/v1/exports/`):
`catalogue/`, `catalogue/<key>/`, `from-screen/`.

The builder and the saved recipes are a separate slice
(`export_builder_definitions`); producing the file is
`export_runs_and_files`.

Findings live in **`error/exports/export_code_issues.md`**; §8 below points at
them by number rather than repeating them.

---

## 1. What it is (and what it is NOT)

- **The catalogue is code, not tenant-editable rows.** Every entry names a real
  ORM path, so a label with no column behind it would be a run-time failure
  waiting to happen (`catalogue.py:1-8`). What administrators actually control -
  which datasets a role may export, which fields are restricted - is expressed
  through RBAC keys, and those *are* per tenant.
- **`vs_exports` declares no datasets and imports no domain app.** This module
  holds the vocabulary and the registry only. Each app publishes its own in an
  `export_datasets` module, loaded from that app's `AppConfig.ready()`
  (`catalogue.py:30-35`). That is what keeps the Export Centre domain-neutral:
  adding a school or health dataset never touches `vs_exports`, and `vs_exports`
  never grows a `from vs_finance.models import ...`. There is a test meant to
  enforce it (`tests.py:1577`), though it errors rather than asserts on Windows
  - `export_code_issues` §17.
- **The boundary is a property of the dataset, not an assumption.** Finance,
  payments and procurement rows belong to a `LedgerEntity`; audit events, users
  and configuration belong to the tenant. `DatasetScope`
  (`constants.py:191-204`) declares which, and everything downstream - whether
  `?entity=` is required, whether `ExportDefinition.entity` is null - follows
  from that declaration.
- **Scoping lives in the dataset's `base` factory, not in a filter**
  (`catalogue.py:236-238`). A caller can edit filters; they cannot edit the
  boundary, and there is no code path that reads past it. The one deliberate
  exception is `platform.schools`, which ignores its scope on purpose - see
  §9 and `export_code_issues` §3.
- **The kind, not the column type, decides how a cell reads.** `people` mode is
  what a finance user reads (`26 Jul 2026`, `₦1,240,000.00`, `Overdue`);
  `system` mode is what another system imports (`2026-07-26`, `1240000.00`,
  `OVERDUE`) (`catalogue.py:62-100`).
- **A screen binding is a translation, not a promise.** The module that owns the
  screen writes the translator, because only Finance knows what
  `?bucket=overdue` means. What the contract adds is honesty: a screen filter
  that could not be carried is reported, never dropped (`catalogue.py:391-399`).
- **This is not a query language.** There is no free-text ORM path, no
  caller-supplied lookup, no arbitrary join. A filter is one of six declared
  kinds and a column is one of the dataset's declared fields; anything else is
  refused before it reaches the ORM.

## 2. Domain model

Nothing in this slice is a database table. The catalogue is three frozen
dataclasses and two module-level dicts.

### `Field` (`catalogue.py:109`)

| Attribute | Meaning |
|---|---|
| `id` | Column id, stable, what a definition stores in `columns` |
| `label` | The header a person reads |
| `group` | Picker grouping ("Invoice", "Customer", "Contact") |
| `kind` | `text` `date` `datetime` `money` `number` `choice` (`catalogue.py:53-58`) |
| `source` | ORM lookup path; defaults to `id` (`catalogue.py:126-129`) |
| `locked` | Always exported, cannot be deselected - the row's identity |
| `sensitive` | Needs `exports.sensitive_field.export` as well |
| `choices` | `{code: label}` for `KIND_CHOICE`, resolved once at import |
| `description` | Published to the picker |

`describe()` (`catalogue.py:133`) is what the API publishes: id, label, group,
type, locked, sensitive, description. **`source` is never published** - the ORM
path stays inside the server.

### `FilterDef` (`catalogue.py:158`)

Same shape, plus `required`, `is_primary_date` (marks the filter
`max_date_span_days` is measured against) and `searches` - a tuple of
`(path, label)` pairs used only by `FILTER_SEARCH`. `paths` returns every ORM
path the filter touches: one for most kinds, several for search
(`catalogue.py:179-185`).

Six kinds (`catalogue.py:146-154`):

| Kind | Spec shape | Compiles to |
|---|---|---|
| `date_range` | `{id, start, end}` | `path__gte` / `path__lte` |
| `choice` | `{id, values: [...]}` | `path__in` |
| `text` | `{id, value}` | `path__icontains` |
| `boolean` | `{id, value}` | `path=bool(value)` |
| `number_range` | `{id, min, max}` | `path__gte` / `path__lte` |
| `search` | `{id, value}` | `icontains` OR-ed across `searches` |

`search` is a separate kind rather than several text filters because a search box
means OR across columns, while two text filters would mean AND and would match
nothing (`catalogue.py:151-153`).

### `Dataset` (`catalogue.py:223`)

| Attribute | Meaning |
|---|---|
| `key` | `finance.customer_invoices` - what a definition stores |
| `module` | Grouping in the catalogue response ("Finance", "Audit") |
| `name`, `description` | Published |
| `base` | `(ScopeContext) -> QuerySet`. The boundary |
| `fields`, `filters` | Tuples of the above |
| `formats` | Defaults to `(XLSX, CSV)` |
| `row_cap` | Dataset ceiling; the platform cap applies on top |
| `max_date_span_days` | Guidance, **not** a limit - it only warns (§5) |
| `permission` | RBAC key required to export this dataset at all |
| `default_columns` | What the builder offers when it starts empty |
| `scope` | `ENTITY` or `TENANT` (`constants.py:191-204`) |

### `ScopeContext` (`catalogue.py:201`)

`{tenant, entity=None}`. Passed to `base` instead of a bare entity so a
tenant-scoped dataset is expressible without inventing a fake entity, and an
entity-scoped one still cannot reach past its set of books. `label` is what the
review step and the Filters sheet call the scope: the entity code, else the
tenant name, else "your organisation" (`catalogue.py:215-220`).

### The registry (`catalogue.py:356`, `catalogue.py:450`)

Two module-level dicts, `_REGISTRY` and `_SCREENS`, populated at boot from each
app's `AppConfig.ready()`. `register` is idempotent on key
(`catalogue.py:360-363`), so a double import cannot duplicate a dataset.

**19 datasets and 18 screens are registered today:**

| Module | Datasets | Scope |
|---|---|---|
| Finance | `customer_invoices`, `invoice_lines`, `gl_postings`, `customer_receipts`, `expense_claims`, `customers` | ENTITY |
| Payments | `collections`, `payouts` | ENTITY |
| Procurement | `purchase_orders`, `vendor_invoices`, `vendors`, `requisitions` | ENTITY |
| Audit | `audit.events` | TENANT |
| Support | `support.tickets` | TENANT |
| Administration | `admin.users`, `admin.role_assignments`, `admin.sign_ins`, `platform.schools` | TENANT |
| Workflow | `workflow.approvals` | TENANT |

### `ScreenBinding` (`catalogue.py:411`)

| Attribute | Meaning |
|---|---|
| `key` | `finance.invoices` |
| `label` | "Finance - Invoices" |
| `dataset_key` | The dataset it prepares |
| `translate` | `(params) -> (filters, unmapped)`, written by the owning app |
| `handles` | Every parameter the translator understands, carried or not |
| `ignore` | Screen params that are not filters (tab ids, view modes) |
| `default_window_days` | Fallback date window when the dataset requires one |

`handles` is the honesty mechanism. Anything the screen sends that is not listed
there is reported as unmapped rather than assumed harmless, so adding a filter to
a list screen without adding it here makes the export honest by default instead
of wrong (`catalogue.py:424-430`).

## 3. Endpoint map

Every route inherits `_ExportBase` (`views.py:76`):
`permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]`, the
`XVSPagination` envelope at page size 25 (`views.py:90-97`), and `self.tenant`
from the asserted request tenant (`views.py:82-88`).

| Route | Method | `rbac_permission` | View |
|---|---|---|---|
| `catalogue/` | GET | `exports.catalogue.view` | `CatalogueView` (`views.py:198`) |
| `catalogue/<key>/` | GET | `exports.catalogue.view` | `CatalogueDetailView` (`views.py:228`) |
| `from-screen/` | GET | `exports.catalogue.view` | `FromScreenView` (`views.py:519`) |

### Query parameters actually read

`from-screen/` reads `?screen=<key>` (`views.py:541`), `?entity=` for an
entity-scoped dataset (`views.py:178-196`), and then **the entire remaining
query string** as the screen's own filters (`views.py:561`). Everything in
`COMMON_SCREEN_PARAMS` - `page`, `page_size`, `ordering`, `order`, `sort`,
`entity`, `tenant`, `format`, `screen`, `cursor`, `limit`, `offset`
(`catalogue.py:385-388`) - plus the binding's own `ignore` list is skipped.

### Response shapes

`GET /catalogue/` returns modules, including empty ones, because "Procurement -
no datasets yet" is information (`views.py:203-206`):

```json
{"modules": [{"name": "Finance", "datasets": [ ... ], "available": true}]}
```

Each dataset is `Dataset.describe()` (`catalogue.py:274-300`): id, module, name,
description, scope, `requires_entity`, fields, `field_count`,
`default_columns`, `required_filters`, filters, `supported_formats`,
`format_options`, `max_date_span_days`, `row_cap`.

`GET /from-screen/` returns the prepared configuration plus the estimate and the
sample, in one response (`views.py:577-611`): `screen`, `config`,
`supported_formats`, `fields`, `carried`, `unmapped`, `added`, `exact`,
`warning`, the estimate block, `sample`, `reads_as`. It is designed to be handed
straight to `POST /quick/`, and there is a test that does exactly that
(`tests.py:1743`).

## 4. Lifecycle / state machine

The catalogue has no state. What it has is a boot sequence and a resolution
order, and both matter:

```
Django starts
  └─ each AppConfig.ready() imports its export_datasets module
       ├─ choice_labels("vs_finance.constants.DocumentStatus") resolves NOW
       │    (a typo fails at boot, not halfway through somebody's export)
       └─ register(Dataset(...)) / register_screen(ScreenBinding(...))
```

`choice_labels` (`catalogue.py:329-352`) imports the longest importable prefix of
a dotted path and then walks the rest as attributes, which is how it resolves a
class nested inside another class (`vs_user.models.User.Status`). It raises
`ModuleNotFoundError` on boot for a path it cannot resolve.

Per request, the resolution order for a screen export is:

```
?screen=            → get_screen()            → 400 listing the valid keys if unknown
binding.dataset     → get_dataset()           → 404 if the dataset was withdrawn
may_export_dataset  → the dataset's own key   → 403
resolve_scope       → ?entity= for ENTITY     → 400 missing / 404 unknown
resolve_screen      → filters, unmapped, added, exact
estimate + sample   → the numbers and ten rows
```

## 5. Derivations

| Output | Formula | Where |
|---|---|---|
| Cell value | `kind × values_mode → string` | `render_value` (`catalogue.py:62`) |
| Money cell | `Decimal(kobo) / 100`, `₦1,234.56` for people, `1234.56` for systems | `catalogue.py:88-93` |
| Blank cell | `-` for people, `""` for systems | `catalogue.py:71-72` |
| One filter | `spec dict → Q` | `compile_filter` (`catalogue.py:568`) |
| Filter sentence | `spec dict → "Invoice date is 1 Jul 2026 to 31 Jul 2026"` | `describe_filter` (`catalogue.py:649`) |
| Screen config | `params → {filters, carried, unmapped, added, exact}` | `resolve_screen` (`catalogue.py:469`) |
| `exact` | `not unmapped` | `catalogue.py:538` |
| Search `Q` | `OR` of `path__icontains` over `searches` | `catalogue.py:608-620` |
| Wide-range warning | `span > dataset.max_date_span_days` | `date_span_warning` (`engine.py:234`) |

Three of these are worth spelling out.

**Money is never floated.** `KIND_MONEY` reads the integer kobo and divides a
`Decimal` by 100 (`catalogue.py:88-91`), matching the rest of the platform.

**A search box is OR, and it is one filter.** `FILTER_SEARCH` combines
`icontains` across every declared path with `|=` (`catalogue.py:614-618`). The
labels of the searched columns are published (`catalogue.py:198`) so the UI can
say which columns are being searched rather than leaving it to guesswork, and
`describe_filter` reads it back as `Name, Code, Slug mentions "corona"`
(`catalogue.py:664-666`).

**A wide date range warns, it does not block.** `max_date_span_days` is
guidance about cost. A finance user asking for a year of postings is making an
ordinary request, so `date_span_warning` returns a `WIDE_DATE_RANGE` warning and
the real ceiling is the row cap, measured on the actual result rather than
guessed at from the calendar (`engine.py:234-256`). `FailureCode.DATE_SPAN_EXCEEDED`
survives only so historical runs still render (`constants.py:84-88`).

### `resolve_screen`, step by step (`catalogue.py:469-539`)

1. Drop `COMMON_SCREEN_PARAMS` and the binding's `ignore`, and drop empty values.
2. Call the module's `translate(meaningful)` → `(filters, unmapped)`.
3. **Any parameter the translator never heard of becomes `unmapped`** - assuming
   otherwise would report "we applied your filter" about a filter nobody applied
   (`catalogue.py:497-506`).
4. `carried` = every meaningful parameter not reported unmapped.
5. If the dataset requires a date filter the screen did not supply, add a bounded
   default of `default_window_days` and say so in `added` (`catalogue.py:511-532`).
   Narrowing is safe; widening is not.
6. `exact = not unmapped`.

`unmapped` means the file will be **wider** than the table. `added` means it will
be **narrower**. Both are published; only the first raises a warning
(`views.py:601-606`).

## 6. What reading writes

Reading the catalogue writes nothing. `from-screen/` writes nothing either -
it prepares a configuration and never creates a run.

The one side effect in this slice is telemetry, and only on the preview path
that `from-screen` shares: `ESTIMATE_VIEWED` with bucketed figures
(`views.py:290-297`). `from-screen/` itself records no analytics event.

## 7. Worked example

A finance user is on the invoices screen filtered to overdue invoices for one
customer, and clicks Export.

```
GET /v1/exports/from-screen/?screen=finance.invoices&entity=CSS
    &bucket=overdue&customer_code=CUST-014&page=2&ordering=-due_date
```

1. `get_screen("finance.invoices")` → the binding registered by
   `vs_finance.export_datasets.register_screens` (`vs_finance/export_datasets.py:540`).
2. `page` and `ordering` are dropped as common screen params
   (`catalogue.py:385-388`).
3. `translate({"bucket": "overdue", "customer_code": "CUST-014"})` rebuilds the
   derived tab from the columns behind it - the Overdue tab is a status plus a
   due-date comparison, not a stored flag - and carries the customer code
   through as a text filter.
4. `finance.customer_invoices` requires a date range and the screen supplied
   none, so a 365-day window is added, and `added` explains it: "Customer
   invoices needs a date range. The export covers the last 365 days; widen it in
   the builder if you need more."
5. Nothing was dropped, so `exact: true` and `warning: null`.
6. `estimate` counts (a LIMITed count) and `sample_rows` renders ten rows exactly
   as they will appear in the file.

```json
{
  "screen": {"key": "finance.invoices", "label": "Finance - Invoices", ...},
  "config": {"dataset_key": "finance.customer_invoices", "columns": [...],
             "filters": [{"id": "status", "values": ["SENT", "PART_PAID"]},
                         {"id": "customer_code", "value": "CUST-014"},
                         {"id": "invoice_date", "start": "2025-08-20", "end": "2026-08-20"}]},
  "carried": ["bucket", "customer_code"],
  "unmapped": [],
  "added": [{"id": "invoice_date", "label": "Invoice date", "reason": "..."}],
  "exact": true,
  "matching_rows": 41,
  "reads_as": "Customer invoices in CSS where Invoice date is ... - 8 columns, about 41 rows - as an Excel file. Files stay available for 30 days."
}
```

Had the screen carried a filter nobody wrote a rule for, step 3 would have put it
in `unmapped`, `exact` would be false, and the drawer would say: "Some filters on
this screen cannot be carried into an export, so the file will contain more rows
than the table shows" (`views.py:601-606`).

## 8. Gotchas / known limitations

Full detail in `error/exports/export_code_issues.md`. From this slice:

| # | In one line |
|---|---|
| 3 | `platform.schools` ignores its scope and reads every school on the platform; the only boundary is an unrestricted `platform.*` key a school role can be given |
| 5 | A number-range filter's value goes straight to the ORM, so `{"min": "abc"}` is a 500 on preview and a mislabelled retryable failure on a run |
| 12 | One tenant-wide key unlocks all 25 sensitive fields across all nine datasets that declare any |
| 18.6 | `admin.users` reaches for `tenant__school_profile__name` - school vocabulary in a platform app's catalogue entry |
| 18.7 | `resolve_screen` sees `query_params.dict()`, so a repeated parameter keeps only its last value and is still reported as carried |

Two more that are limitations rather than defects:

- **A withdrawn dataset is a run-time failure, not a migration.** Removing a
  `register(...)` call leaves every definition pointing at a key `get_dataset`
  no longer resolves. The list row states it (`available: false`,
  `serializers.py:63-71`), the run fails with `DATASET_WITHDRAWN` and guidance
  (`constants.py:99-102`), and nothing is silently ignored - but nothing
  migrates the definitions either.
- **`row_cap` and `max_date_span_days` are per dataset and not tenant-tunable.**
  A school with a bigger ledger cannot raise its own ceiling; the only lever is
  a code change.

## 9. Permissions & tenant isolation

Two gates, and they are separate on purpose:

1. **`exports.catalogue.view`** gets you into the catalogue at all
   (`views.py:206, 231, 535`).
2. **The dataset's own `permission`** decides which datasets you see inside it
   (`may_export_dataset`, `engine.py:95-98`). Datasets you may not export are
   filtered out of the list (`views.py:209`), and `catalogue/<key>/` returns
   **404, not 403**, for both unknown and forbidden keys, so a caller cannot
   enumerate the datasets they are not allowed to see (`views.py:235-240`).

Dataset keys borrow the owning module's read key rather than inventing an export
key, with one deliberate exception: `audit.events` requires
`platform.audit.export`, not `platform.audit.view`, because reading the console
and taking the trail out of the building are different decisions
(`vs_audit/export_datasets.py:7-9`).

**Sensitive fields need a second key.** `may_export_sensitive`
(`engine.py:101-103`) checks `exports.sensitive_field.export`, and the catalogue
hides restricted fields from a caller who may not export them
(`describe(include_sensitive=False)`, `catalogue.py:274-282`) so the picker never
offers a column that would be dropped at run time.

**Tenant isolation is the `base` factory's job.** Every entity-scoped dataset
filters on `entity=scope.entity`, and the entity itself is resolved through
`vs_finance.views.resolve_entity`, which requires it to belong to
`request.tenant` and returns 404 for unknown *and* forbidden
(`vs_finance/views.py:56-83`). Tenant-scoped datasets filter on
`tenant=scope.tenant`, with two exceptions that are stated in their own
docstrings: `admin.users` widens for a PLATFORM-kind caller, matching the Users
console (`vs_user/export_datasets.py:42-53`), and `platform.schools` does not
fence at all (see `export_code_issues` §3).

`vs_tickets` and `vs_user.LoginSession` deliberately read through `all_objects`
and filter tenant explicitly, because their default managers are tenant-aware
and would double-scope (`vs_tickets/export_datasets.py:33`,
`vs_user/export_datasets.py:63-67`).

## 10. Code map

| File | What lives there |
|---|---|
| `catalogue.py:62` | `render_value` - kind × values mode → cell |
| `catalogue.py:109` | `Field` |
| `catalogue.py:158` | `FilterDef` |
| `catalogue.py:201` | `ScopeContext` |
| `catalogue.py:223` | `Dataset`, `describe` at :274 |
| `catalogue.py:305` | `FORMAT_OPTION_SCHEMA` - options discriminated by format |
| `catalogue.py:329` | `choice_labels` - dotted path → `{value: label}` |
| `catalogue.py:356-379` | The dataset registry: `register`, `get_dataset`, `all_datasets`, `modules` |
| `catalogue.py:385` | `COMMON_SCREEN_PARAMS` |
| `catalogue.py:391` | `Unmapped` - a screen filter that could not be carried |
| `catalogue.py:411` | `ScreenBinding` |
| `catalogue.py:469` | `resolve_screen` |
| `catalogue.py:545-566` | `FilterError`, `_as_date` |
| `catalogue.py:568` | `compile_filter` |
| `catalogue.py:649` | `describe_filter` |
| `constants.py:191` | `DatasetScope` |
| `engine.py:234` | `date_span_warning` |
| `views.py:198, 228` | `CatalogueView`, `CatalogueDetailView` |
| `views.py:519` | `FromScreenView` |
| `vs_finance/export_datasets.py` | 6 datasets, 5 screens |
| `vs_payments/export_datasets.py` | 2 datasets, 2 screens |
| `vs_procurement/export_datasets.py` | 4 datasets, 4 screens |
| `vs_user/export_datasets.py` | 3 datasets, 3 screens |
| `vs_audit/export_datasets.py` | 1 dataset, 1 screen |
| `vs_tickets/export_datasets.py` | 1 dataset, 1 screen |
| `vs_workflow/export_datasets.py` | 1 dataset, 1 screen |
| `schools/vs_schools/export_datasets.py` | 1 dataset, 1 screen |

## 11. Test coverage & gaps

Covered, and unusually well - these are contract tests over every registered
dataset, not examples:

- `test_the_engine_never_imports_a_domain_app` (`tests.py:1577`) and
  `test_the_catalogue_declares_no_datasets_of_its_own` (`tests.py:1593`) enforce
  the domain-neutrality rule in code - **but both currently error out on Windows
  before their assertion runs**, so on this platform they enforce nothing. See
  `export_code_issues` §17; the fix is one keyword argument in each.
- `test_every_dataset_resolves_against_the_orm` (`tests.py:1613`) walks every
  field and filter path of every dataset - the test that makes a code-declared
  catalogue safe.
- `test_every_dataset_names_a_permission_and_a_locked_column` (`tests.py:1639`).
- `test_every_bound_screen_points_at_a_published_dataset` (`tests.py:1783`),
  `test_every_bound_screen_translates_without_raising` (`tests.py:1792`),
  `test_every_handled_param_actually_does_something` (`tests.py:1816`).
- `test_every_search_filter_names_real_columns` (`tests.py:2343`) and
  `test_every_screen_with_a_search_box_now_carries_it` (`tests.py:2365`).
- The screen contract: unknown screen lists the valid keys (`tests.py:1669`),
  paging params are not filters (`tests.py:1734`), an unrecognised parameter is
  reported as unmapped (`tests.py:1807`), a derived tab is rebuilt from the
  columns behind it (`tests.py:1696`).
- Search semantics: OR not AND (`tests.py:2267`), case insensitive
  (`tests.py:2289`), an empty term does not narrow (`tests.py:2302`).

Not covered:

- **No test sends a malformed filter value.** Every filter test uses a
  well-formed spec, which is why `export_code_issues` §5 is invisible to a green
  suite. Wanted: a number-range with a string, a date-range with a timestamp, a
  choice with a dict.
- **No test asserts `platform.schools` is fenced**, because it is not
  (`export_code_issues` §3). Wanted: a school-tenant caller exporting the schools
  dataset sees one school.
- **No test on `?screen=` with a repeated parameter** (`export_code_issues`
  §18.7).
- **No test that a school role can reach the catalogue at all**
  (`export_code_issues` §1). Every API test in the suite builds its caller by
  granting keys directly, so the seed's own grants are never exercised.
