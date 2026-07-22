# procurement_master_data

Procurement master data defines **who the entity buys from, how those vendors are
classified, reusable buying defaults, and the commercial contracts that may govern
future purchase orders**. Routes are mounted at `/v1/procurement/`; every route in this
slice requires `?entity=<id|code>`.

---

## 1. What it is (and what it is NOT)

- `VendorCategory` is a maximum-three-level purchasing taxonomy with an optional
  default expense account (`models.py:75-117`).
- `Vendor` is an entity's supplier/AP party, including payment defaults and KYC/risk/
  hold governance (`models.py:123-221`).
- `CatalogItem` is a reusable set of line defaults: description, expense account, tax,
  preferred vendor, unit price, and lead time (`models.py:231-322`).
- `VendorContract` and `ContractMilestone` hold a commercial term, renewal lineage, and
  delivery checkpoints (`models.py:486-575`).

**This does NOT post money.** Categories, vendors, catalog items, contracts, activation,
renewal, termination, and milestone completion create no journal. A category/catalog
default becomes accounting only after it is copied to a downstream document line and
that document later posts (`contracts.py:1-8`; `models.py:231-240`).

## 2. Domain model

| Model | Key fields | Tenant/uniqueness rules |
|---|---|---|
| `VendorCategory` | `code`, `name`, `parent`, `default_expense_account`, `is_active` | Protected `entity`; case-insensitive unique `(entity, code)`; indexed `(entity, parent)` (`models.py:82-110`) |
| `Vendor` | contact/bank data; `payable_account`; expense/WHT defaults; `payment_terms`; `kyc_status`; `risk`; `on_hold`; `is_active` | Protected `entity`; case-insensitive unique code; normalized nonblank tax id unique per entity (`models.py:135-209`) |
| `CatalogItem` | description/UOM; category; preferred vendor; expense/tax defaults; `standard_unit_price`; `lead_time_days`; `is_active` | Protected `entity`; case-insensitive unique code (`models.py:243-292`) |
| `VendorContract` | vendor, reference, title, status, dates, value, terms, renewal settings, predecessor | Protected entity/vendor; unique `(entity, reference)`; status/end-date indexes (`models.py:497-539`) |
| `ContractMilestone` | name, due date, amount, status, completed date, note, line number | Cascades with its contract; indexed by contract and `(status, due_date)` (`models.py:551-572`) |

`standard_unit_price`, `contract_value`, and milestone `amount` are integer **kobo**.
Category/catalog values are defaults, not immutable prices or posting instructions.

## 3. Endpoint map

Request bodies below list only fields the views actually read. List endpoints use the
standard paginated `{pagination, data}` envelope.

| Method + path | permission key | request body / query | response |
|---|---|---|---|
| `GET /categories/` | `procurement.category.view` | Query `is_active`, `search`/`q` | Paginated categories + direct vendor/child/catalog counts (`views/vendors.py:247-265`) |
| `POST /categories/` | `procurement.category.create` | `code`, `name`, `parent?`, `default_expense_account?`, `is_active?` | `201` category (`views/vendors.py:268-293`) |
| `GET /categories/<pk>/` | `procurement.category.view` | — | Category + direct counts |
| `PATCH /categories/<pk>/` | `procurement.category.update` | `code?` (must be unchanged), `name?`, `parent?`, `default_expense_account?`, `is_active?` | Updated category (`views/vendors.py:326-367`) |
| `GET /categories/insights/` | `procurement.report.view` | — | Category activity/spend insight (`views/vendors.py:370-411`) |
| `GET /vendors/` | `procurement.vendor.view` | Query `is_active`, `on_hold`, `kyc_status`, `purchase_eligible`, `search`/`q` | Paginated non-sensitive vendor rows (`views/vendors.py:426-451`) |
| `POST /vendors/` | `procurement.vendor.create` | `code`, `name`, category/contact/bank/tax fields, `payable_account?`, `default_expense_account?`, `default_wht_tax_code?`, `payment_terms?` | `201` vendor; KYC/risk/hold/activity are server defaults (`views/vendors.py:454-493`) |
| `GET /vendors/<pk>/` | `procurement.vendor.view` | — | Vendor detail; sensitive fields are FLS-masked |
| `PATCH /vendors/<pk>/` | `procurement.vendor.update` | All mutable detail fields, including KYC/risk/hold/activity; `code` cannot change | Updated vendor (`views/vendors.py:557-614`) |
| `GET /vendors/summary/` | `procurement.report.view` | — | Counts, YTD posted spend, average terms (`views/vendors.py:501-517`) |
| `GET /vendors/<pk>/insights/` | `procurement.report.view` | — | Spend/performance insight (`views/vendors.py:617-632`) |
| `GET /catalog-items/` | `procurement.catalog_item.view` | Query `is_active`, `vendor`, `category`, `search`/`q` | Paginated items; `stock_status` only with `procurement.stock.view` (`views/catalog.py:207-227`) |
| `POST /catalog-items/` | `procurement.catalog_item.create` | `code`, `name`, `description?`, `unit_of_measure?`, category/vendor/account/tax defaults, `lead_time_days?`, `standard_unit_price?`, `is_active?` | `201` item (`views/catalog.py:229-259`) |
| `GET /catalog-items/<pk>/` | `procurement.catalog_item.view` | — | Item with permission-gated stock overlay |
| `PATCH /catalog-items/<pk>/` | `procurement.catalog_item.update` | Same mutable defaults; `code` cannot change | Updated item (`views/catalog.py:281-323`) |
| `GET /catalog-items/<pk>/insights/` | `procurement.report.view` | — | Usage and approved-PO price history (`views/catalog.py:326-370`) |
| `GET /contracts/` | `procurement.contract.view` | Query `status`, `expiring`, `vendor`, `search`/`q` | Paginated contract rows (`views/contracts.py:133-156`) |
| `POST /contracts/` | `procurement.contract.create` | `vendor`, `reference?`, `title`, dates, `contract_value`, `payment_terms?`, `auto_renew?`, `renewal_notice_days?`, `notes?`, `milestones?` | `201` DRAFT contract (`views/contracts.py:159-186`) |
| `GET /contracts/<pk>/` | `procurement.contract.view` | — | Header, milestones, renewal lineage, audit activity |
| `PATCH /contracts/<pk>/` | `procurement.contract.update` | Header fields above + appended `milestones?` | Updated non-terminal contract (`views/contracts.py:215-246`) |
| `GET /contracts/summary/` | `procurement.contract.view` | — | Active/expiring/expired counts + active kobo value (`views/contracts.py:255-284`) |
| `GET /contracts/renewals/` | `procurement.contract.view` | Query `as_of?`, `within_days?` | Active contracts in renewal window (`views/contracts.py:440-452`) |
| `GET /contracts/<pk>/linked-pos/` | `procurement.purchase_order.view` | — | Explicit call-offs + unlinked same-vendor term associations (`views/contracts.py:287-334`) |
| `POST /contracts/<pk>/activate/` | `procurement.contract.activate` | — | ACTIVE contract |
| `POST /contracts/<pk>/terminate/` | `procurement.contract.terminate` | `reason?` | TERMINATED contract |
| `POST /contracts/<pk>/renew/` | `procurement.contract.renew` | `reference?`, `start_date`, `end_date`, `contract_value?`, `copy_milestones?` | `201` ACTIVE successor (`views/contracts.py:373-404`) |
| `POST /contracts/<pk>/milestones/<milestone_id>/complete/` | `procurement.contract.update` | `completed_date?` | Contract with completed milestone (`views/contracts.py:407-429`) |

Vendor detail FLS removes email, phone, address, tax id, and bank fields without
`procurement.vendor.view_sensitive` (`serializers.py:85-125`). Writing any of those
fields requires the same permission (`views/vendors.py:83-97`).

## 4. Lifecycle / state machine

```text
Category / vendor / catalog item: ACTIVE ⇄ INACTIVE (master-data availability)

Contract: DRAFT ─activate─▶ ACTIVE ─terminate─▶ TERMINATED
                             ├─renew─────────▶ RENEWED + new ACTIVE successor
                             └─expiry sweep──▶ EXPIRED ─renew─▶ RENEWED

Milestone: PENDING ─complete─▶ COMPLETED
             └─overdue sweep─▶ MISSED ─complete─▶ COMPLETED
```

Activation requires dates in order and an active, non-held vendor whose KYC is not
rejected (`contracts.py:45-84`). Renewal accepts ACTIVE or EXPIRED, creates an ACTIVE
successor, optionally copies only pending milestones, and marks the source RENEWED in
one transaction (`contracts.py:110-166`). End dates are inclusive; batch expiry/missed
checks use strictly-before comparisons (`contracts.py:190-221`).

## 5. Calculations

- Category `level` is root `1`, child `2`, grandchild `3`; API hierarchy validation
  considers both ancestry and descendant height (`serializers.py:70-74`;
  `views/vendors.py:171-231`).
- `renewal_window_start = end_date − renewal_notice_days`. A contract is due when
  `window_start ≤ as_of ≤ end_date`; `within_days` replaces the per-contract notice with
  `end_date ≤ as_of + within_days` (`models.py:541-545`; `contracts.py:224-248`).
- Vendor summary spend is the sum of POSTED invoice gross totals from January 1; average
  payment days is the mean of configured term days (`views/vendors.py:501-517`).
- Catalog insight min/max prices and quantities come only from APPROVED POs whose source
  requisition line points to the catalog item (`views/catalog.py:338-358`).

Example: a ₦2,400,000 one-year contract is `240000000` kobo. With a 30-day notice and
an end date of 2027-06-30, its renewal window starts 2027-05-31.

## 6. What posting does to the ledger

Nothing in this slice posts. The durable effects are master rows, status changes, and
finance audit events for contract lifecycle actions. Catalog/category expense and tax
accounts are merely copied as defaults; the later GRN/invoice/payment services decide
and validate the real journal (`models.py:299-322`; `contracts.py:74-80`).

Contract-linked POs also remain separate documents. Terminating or renewing a contract
does not reverse or rewrite POs/invoices already raised under it (`contracts.py:87-107`).

## 7. Worked example

```json
POST /v1/procurement/catalog-items/?entity=LEKKI
{
  "code": "LAPTOP-14",
  "name": "14-inch laptop",
  "unit_of_measure": "each",
  "category": "IT-EQUIP",
  "preferred_vendor": "TECH01",
  "default_expense_account": "5100",
  "standard_unit_price": 240000000,
  "lead_time_days": 7
}
```

The response stores `240000000` kobo and returns a Naira display string. A later buyer
may seed a requisition line from these defaults, but changing this catalog item does not
rewrite that line and this request creates no journal (`models.py:231-240,299-322`).

## 8. Gotchas / known limitations

- ✅ **Strict `auto_renew` validation:** contract create/update now accepts only real JSON
  booleans; strings, numbers, and `null` return a field-level 400 instead of being
  truthiness-coerced (`views/contracts.py:74-79,176,236`).
- ✅ **Bounded renewal horizon:** `within_days` now accepts only whole days from 0 through
  3650; malformed, negative, boolean, and excessive values return a field-level 400
  instead of reaching `int(...)` unsafely (`views/contracts.py:81-96,440-446`).
- **Recommend fix:** termination and milestone completion do not lock their rows, so a
  concurrent renewal/termination or duplicate completion can race even though renewal
  and activation are serialized (`contracts.py:87-107,173-187`).
- **Judgment call:** vendor `update` can change KYC, risk, hold, and activity directly.
  There is FLS for PII/bank data, but no separate compliance permission for these
  governance fields (`views/vendors.py:598-607`).
- **Justified by design:** category depth is enforced by API governance rather than a DB
  constraint; direct ORM/import writers must reuse that boundary (`models.py:88-92`;
  `views/vendors.py:201-231`).
- **Justified by design:** contract expiry/missed status needs a scheduled call to
  `mark_expired` / `flag_missed_milestones`; serializers and summary views still derive
  date-honest display state before that sweep runs (`contracts.py:190-221`;
  `serializers.py:163-175`).

## 9. Permissions & tenant isolation

All ordinary views inherit authentication + RBAC from `_ProcBase`, then resolve the
selected `LedgerEntity` and filter every target/query by it (`views/base.py:279-295`).
Related category/vendor/account/tax references are resolved inside the same entity;
foreign ids therefore behave as missing rather than leaking another tenant's row.

Resources/verbs are distinct: category and catalog have view/create/update; vendor adds
`view_sensitive`; contract adds activate/renew/terminate; insight/summary endpoints use
`procurement.report.view`, except contract summary/renewals use contract view. The
linked-PO panel correctly requires `procurement.purchase_order.view`, not merely contract
visibility (`views/contracts.py:264-272`).

## 10. Code map

| File | Responsibility |
|---|---|
| `models.py` | Category, vendor, catalog, contract, milestone storage and constraints |
| `views/vendors.py` | Taxonomy/vendor CRUD, governance, FLS write gate, summaries |
| `views/catalog.py` | Catalog CRUD, eligible defaults, stock-visibility overlay, insights |
| `views/contracts.py` | Contract CRUD, action endpoints, summaries, linked POs |
| `contracts.py` | Contract state machine, numbering, milestone/expiry/renewal services |
| `serializers.py` | Public shapes, vendor FLS, contract/catalog display overlays |
| `constants.py` | KYC/risk/terms and contract/milestone enums |
| `urls.py` | `/v1/procurement/` route map |
| `management/commands/seed_procurement_permissions.py` | Permission registry/grants |

## 11. Test coverage & gaps

The current procurement suite is **195 green**. Relevant groups are
`VendorConsoleAPITests` (`tests.py:197`), `VendorCategoryConsoleAPITests` (`:357`),
`VendorEligibilityTests` (`:668`), `CatalogItemTests` / `CatalogItemConsoleAPITests`
(`:3008`, `:3043`), and `VendorContractTests` / `ContractConsoleAPITests`
(`:3113`, `:3247`). They cover permissions, empty list shape, cross-entity access,
inactive-link preservation, vendor eligibility, catalog bounds, contract lifecycle, and
linked-PO scoping.

The fixed validation paths now cover real `false` on create/update, non-boolean
rejection, valid renewal horizons, and malformed/negative/excessive horizon rejection.
Still needed before shipping any remaining §8 changes:

- concurrent renew-versus-terminate and milestone completion tests;
- explicit permission tests for any future KYC/risk/hold governance verb split.
