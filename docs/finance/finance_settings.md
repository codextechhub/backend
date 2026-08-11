# Finance Settings

## Purpose

Finance Settings is the entity-scoped control surface for defaults and policies that affect finance transactions. It currently covers:

- creating a finance ledger entity and its starter data;
- choosing the accounts used by automated posting flows;
- defining invoice and document defaults;
- identifying the primary collection bank account;
- defining bank-reconciliation defaults;
- choosing the default customer-receipt allocation strategy;
- setting the entity-wide petty-cash replenishment alert threshold;
- exposing related finance configuration areas without duplicating their ownership.

This document explains the backend contract, why each setting exists, where it is enforced, and the rules to preserve when extending it.

## Ownership and scope

Finance settings belong to a `LedgerEntity`. Requests still run inside a tenant, so both tenant and entity scope must be resolved and authorized. A user must not be able to read or update another tenant's entity by changing an entity id or query parameter.

The settings APIs use the same scope convention as the rest of Finance:

- the tenant comes from the authenticated request context;
- the entity is resolved from the finance entity query parameter or current finance context;
- the resolved entity must belong to that tenant;
- all reads, writes, account choices, and audit rows use the resolved entity.

Settings rows are not created during a read. Resolver functions return typed model defaults when no stored row exists. This keeps a newly created entity usable without producing rows merely because someone opened the settings page.

## Permissions

The backend is the final authorization boundary. Frontend visibility is only a convenience.

| Capability | Backend permission | Frontend key |
| --- | --- | --- |
| View Finance Settings | `finance.settings.view` | `FIN_VIEW_SETTINGS` |
| Update Finance Settings | `finance.settings.update` | `FIN_UPDATE_SETTINGS` |
| Create a finance entity | `finance.entity.create` | `FIN_CREATE_ENTITY` |

Read and update permissions are deliberately separate. A user may inspect effective settings without being allowed to change them. Each write endpoint checks the update permission before any mutation occurs.

## Entity creation

Finance entity creation accepts the following business inputs:

| Field | Meaning |
| --- | --- |
| `code` | Stable set-of-books code used to identify the ledger entity. |
| `number_code` | Optional reporting code of up to three characters. It is not a live document sequence. |
| `name` | Human-readable entity name. |
| `base_currency` | Functional currency, defaulting to `NGN` in the current product flow. |
| `fiscal_year` | Initial fiscal year to create. |
| `fiscal_year_start_month` | First month of the fiscal year. |
| `fiscal_year_start_day` | First day of the fiscal year. |
| `period_frequency` | Monthly or quarterly accounting periods. |

Creation is atomic. It creates the ledger entity, ensures supported currencies exist, seeds the starter chart of accounts, and builds the initial fiscal calendar. A failure in any of those operations rolls back the whole entity creation.

### Numbering nuance

Three similarly named concepts must not be confused:

1. `LedgerEntity.code` identifies the set of books.
2. `LedgerEntity.number_code` is a compact reporting code and is auto-derived when omitted.
3. Live invoice, receipt, and related document numbers use the tenant numbering sequence in `vs_tenants.numbering`.

Changing `number_code` must therefore never reset, replace, or masquerade as the document sequence.

The platform-reserved ledger entity code is `CODEX`. The entity manager and seed behavior must continue to agree on that exact value.

## Account mappings

### Why mappings exist

Automated finance and procurement flows need accounts for cash, receivables, payables, tax, inventory, clearing, and expense postings. Hard-coding chart codes inside each posting path makes a custom chart unsafe. Account mappings provide one entity-scoped source of truth.

`FinanceAccountMapping` stores overrides only. If an override is absent, the resolver uses the starter chart code for that mapping key. This distinction is returned to clients as `source: "OVERRIDE"` or `source: "DEFAULT"`.

### Supported mappings

| Mapping key | Starter code | Required account type |
| --- | ---: | --- |
| `CASH_BANK` | 1100 | Asset |
| `ACCOUNTS_RECEIVABLE` | 1200 | Asset |
| `ACCOUNTS_PAYABLE` | 2100 | Liability |
| `CUSTOMER_CREDIT` | 2140 | Liability |
| `GRIR_CLEARING` | 2150 | Liability |
| `OUTPUT_VAT` | 2200 | Liability |
| `WHT_PAYABLE` | 2300 | Liability |
| `RETAINED_EARNINGS` | 3200 | Equity |
| `BAD_DEBT_EXPENSE` | 5300 | Expense |
| `BANK_CHARGES` | 5500 | Expense |
| `INVENTORY_ASSET` | 1400 | Asset |
| `INVENTORY_ADJUSTMENT` | 5150 | Expense |
| `PURCHASE_PRICE_VARIANCE` | 5160 | Expense |

### Validation rules

An override account must:

- belong to the same ledger entity;
- be active;
- be postable;
- have the account type required by the mapping specification.

These checks are performed on the backend. A valid-looking account id from another entity is rejected rather than silently accepted.

Sending `null` for a mapping removes its override. The effective value then returns to the starter default. Resetting does not create a second row containing the default because the absence of an override already expresses that state.

### Resolution behavior

All consumers should call the shared functions in `apps/vs_finance/account_mappings.py`, primarily `resolve_mapped_account` or `resolve_default_code_mapping`. Consumers must not reimplement fallback logic.

Current consumers include:

- accounts receivable and customer credit postings;
- refunds and bad-debt write-offs;
- banking and bank charge postings;
- year-end retained earnings;
- cash reporting;
- Procurement payable, GR/IR, inventory, and price variance postings;
- Procurement vendor finance-account creation.

Resolution fails closed when the effective account is missing, inactive, non-postable, or of the wrong type. The transaction should stop with a configuration error instead of creating an incorrect journal.

## Finance document settings

`FinanceDocumentSettings` is a one-to-one entity policy record. As with mappings, reads resolve model defaults without eagerly creating a database row.

| Field | Default | Validation | Effect |
| --- | --- | --- | --- |
| `default_invoice_due_days` | 30 | Whole number from 0 to 365 | Supplies the due date when a caller does not provide one. |
| `default_invoice_narration` | Blank | Maximum 255 characters | Supplies invoice narration when a caller does not provide one. |
| `auto_post_manual_invoices` | `true` | Boolean | Controls whether newly created manual invoices are posted immediately. |
| `allow_customer_opening_balances` | `true` | Boolean | Allows non-zero opening balances on customer creation and editing. |

Explicit transaction input wins over a default. A supplied due date or narration is not overwritten by settings.

The due-days default applies to manual accounts-receivable invoices and fee-generated invoices. A value of zero means the invoice is due on its issue date.

Disabling opening balances blocks creating or changing a customer to a non-zero opening balance. It does not rewrite existing balances and does not prevent normal invoicing and receipt activity.

### Primary collection bank account

The primary collection account is intentionally not duplicated in `FinanceDocumentSettings`. It is owned by `BankAccount.is_primary_collection`, with at most one primary collection account per entity.

Finance document rendering resolves the pay-to account in this order:

1. the account marked as the primary collection account;
2. the first active bank account;
3. no account when the entity has no active bank account.

The explicit primary flag wins even if that account later becomes inactive. The settings update API only permits selecting an active account, but another bank-account update can change activity afterward. This is an important operational nuance: deactivating the selected collection account should be paired with choosing a new primary account.

The settings response shows only the explicitly flagged account as selected. Its selector payload is safe and does not expose sensitive details that the screen does not need, such as the full account number. Invoice and receipt rendering uses the fallback resolver above and accesses the selected model directly because it has a separate business need and authorization path.

## Banking and cash policy

`FinanceBankingSettings` is a separate one-to-one entity policy record. Banking behavior is kept separate from invoice and document defaults because the values govern reconciliation and receipt allocation rather than document creation.

| Field | Default | Validation | Effect |
| --- | --- | --- | --- |
| `default_bank_reconciliation_tolerance_days` | 4 | Whole number from 0 to 30 | Supplies the date window for automatic reconciliation when a request omits `tolerance_days`. |
| `default_group_reconciliation_matches` | `true` | Boolean | Supplies the grouped-matching choice when a request omits `group`. |
| `default_receipt_allocation_strategy` | `oldest` | `oldest` or `largest` | Chooses which open customer invoice is allocated first when a receipt request omits a strategy. |
| `petty_cash_low_balance_threshold_bps` | 2,500 (25%) | Whole number from 0 to 10,000 | Flags an active petty-cash fund when live cash on hand reaches or falls below this share of its imprest float. |

Explicit operational input wins. A caller can use a different reconciliation window, turn grouping on or off, or choose a supported receipt strategy for one operation without changing the entity default. An explicit tolerance of zero is meaningful and must not be replaced by the saved default.

The allocation setting changes the order in which an automatically allocated receipt settles open invoices. It does not enable or disable automatic allocation. The current receipt flows keep their established automatic-allocation behavior, and only the strategy default has moved into settings.

Changing these settings affects later reconciliation and receipt operations. It does not recalculate reconciliations or allocations already recorded.

The petty-cash threshold is stored in basis points so percentages remain exact. The screen converts it to a percentage for editing. The petty-cash status endpoint uses the saved value when `threshold_bps` is omitted, while an explicit query value wins for one report. It validates both saved and explicit values from 0 to 10,000.

The threshold is an alerting rule, not authority to replenish or post cash. Status calculations read live cash on hand from the petty-cash GL account, compare it with the fund's imprest float, and return `needs_replenish`. Establishing, spending, and replenishing a fund retain their existing permissions and posting controls.

Automatic receipt allocation itself intentionally remains fixed. This increment only controls allocation order. A toggle that changes whether receipts allocate automatically would change transaction behavior and requires a separate product and control decision.

## Related sections and ownership boundaries

Some Finance Settings sections link to established modules rather than duplicating their models or APIs:

- fiscal calendars and accounting periods;
- currencies and other reference data;
- approval workflows;
- fee structures;
- dunning policies.

Those modules remain their own source of truth. A future editable settings section should integrate with the existing owner rather than create a parallel setting with similar meaning.

## Consumer ownership metadata

Every live Finance setting now returns code-owned consumer metadata beside its effective value. The registry covers all 13 account mappings, all five document defaults, and all four banking and cash controls. Each entry names the backend service, the concrete code path, and the operational impact of changing the field.

The registry lives in `apps/vs_finance/settings_ownership.py`. Settings endpoints return it as `consumers`; clients display it but cannot edit it. Tests compare the registry keys with the supported mapping and field specifications, so adding a setting without declaring its consumer fails the focused settings suite. The metadata is explanatory only. Runtime enforcement remains in the shared account, document, banking, reconciliation, receipt, and petty-cash services.

## API contract

### Account mappings

`GET /v1/finance/settings/account-mappings/`

Returns every supported mapping, its starter code, expected account type, effective account, whether the value is a default or override, and the complete `consumers` registry. It also returns eligible account choices scoped to the entity.

`PATCH /v1/finance/settings/account-mappings/`

Accepts a partial mapping object. Unmentioned keys remain unchanged. A key with a `null` account removes the override.

Example:

```json
{
  "mappings": {
    "CASH_BANK": 42,
    "BANK_CHARGES": null
  }
}
```

### Document defaults

`GET /v1/finance/settings/documents/`

Returns effective document settings, including defaults when no row is stored, plus the available collection bank accounts.

`PATCH /v1/finance/settings/documents/`

Accepts only supplied fields and preserves all omitted fields.

Example:

```json
{
  "default_invoice_due_days": 14,
  "default_invoice_narration": "Thank you for your business",
  "auto_post_manual_invoices": false,
  "allow_customer_opening_balances": true,
  "primary_collection_bank_account": 18
}
```

The response envelope follows the finance API convention and returns the effective saved settings.

### Banking and cash policy

`GET /v1/finance/settings/banking/`

Returns the complete effective banking policy and recent banking-settings audit events. Typed defaults are returned without creating a row.

`PATCH /v1/finance/settings/banking/`

Accepts a partial policy object. Omitted fields remain unchanged.

Example:

```json
{
  "default_bank_reconciliation_tolerance_days": 2,
  "default_group_reconciliation_matches": false,
  "default_receipt_allocation_strategy": "largest",
  "petty_cash_low_balance_threshold_bps": 4000
}
```

## Save, concurrency, and audit behavior

Settings writes run in a database transaction. Document-default and banking-policy updates lock their existing one-to-one settings row before applying a change. Mapping updates use atomic `update_or_create` and delete operations under the entity-and-key uniqueness constraint. The current mapping implementation does not lock the whole mapping set, so clients should save one coherent partial payload rather than coordinating independent edits to the same keys.

Writes are partial. The backend validates only supplied fields but returns the complete effective state after saving. Unknown mapping keys and invalid field values return a validation error.

Every successful change records before and after snapshots in `FinanceAuditLog`, together with the actor, tenant, entity, action, and request context available to the audit service.

Audit actions are:

- account mappings: `FINANCE_SETTINGS_UPDATED`;
- document defaults: stored value `FIN_DOCUMENT_SETTINGS_UPDATED`, displayed label `Finance document settings updated`;
- banking and cash policy: stored value `FIN_BANK_SETTINGS_UPDATED`, displayed label `Finance banking settings updated`.

The shortened stored value for document settings is intentional because the audit action column has a 32-character limit.

Opening a page, issuing a read, or saving values that do not change the effective state does not produce an audit row. Each settings response embeds the ten most recent audit events for that entity and action, ordered newest first.

## Security rules

- Enforce view and update permissions on the backend.
- Resolve entity scope through the tenant-aware finance helpers.
- Reject accounts from another entity even if their ids are valid.
- Do not expose unnecessary bank details in the settings response.
- Keep the audit trail append-only through the audit service.
- Keep automated posting consumers on the shared mapping resolver.
- Treat frontend permission checks as presentation only.

## Migrations and main implementation files

Database support was introduced by:

- `apps/vs_finance/migrations/0013_alter_financeauditlog_action_financeaccountmapping.py`;
- `apps/vs_finance/migrations/0014_alter_financeauditlog_action_financedocumentsettings.py`;
- `apps/vs_finance/migrations/0015_alter_financeauditlog_action_financebankingsettings.py`.
- `apps/vs_finance/migrations/0016_financebankingsettings_petty_cash_threshold.py`.

The main implementation files are:

- `apps/vs_finance/account_mappings.py`;
- `apps/vs_finance/banking_settings.py`;
- `apps/vs_finance/document_settings.py`;
- `apps/vs_finance/views_settings.py`;
- `apps/vs_finance/models/core.py`;
- `apps/vs_finance/models/ops.py`;
- `apps/vs_finance/tests_settings.py`.

Permission seed commands define the settings permission keys. Posting modules consume the shared resolvers rather than querying mapping rows themselves.

## Test coverage

The focused settings tests cover:

- default responses when no settings row exists;
- permission-denied behavior;
- cross-entity account rejection;
- account type, active, and postable validation;
- partial updates and reset-to-default behavior;
- document default validation;
- audit before and after snapshots;
- enforcement of due dates, auto-posting, and opening-balance policy;
- banking-policy validation and no-row defaults;
- saved and explicit bank-reconciliation behavior;
- saved and explicit customer-receipt allocation strategy.
- saved petty-cash threshold and explicit report-override precedence.

When extending settings, add tests at both boundaries: the settings endpoint and at least one real consumer that proves the policy is enforced outside the settings screen.

## Extension checklist

Before adding another Finance setting:

1. Confirm Finance owns the policy and that an existing module is not already its source of truth.
2. Use a typed model field with an explicit default and validation.
3. Resolve defaults without creating rows on GET.
4. Add tenant and entity scoped permission checks.
5. Use partial transactional updates with row locking where concurrent edits matter.
6. Record before and after audit snapshots.
7. Integrate every relevant transaction consumer, not only the settings UI.
8. Add permission-denied, cross-tenant or cross-entity, default, validation, update, audit, and consumer tests.
9. Avoid returning identifiers or metadata the client does not need.
10. Document whether explicit transaction input overrides the setting.
