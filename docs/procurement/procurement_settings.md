# Procurement Settings

## Purpose

Procurement Settings is the entity-scoped control surface for purchasing defaults and enforcement policies. It currently covers:

- default vendor payment terms;
- the default purchase-order delivery address;
- quantity and price matching tolerances;
- whether non-PO invoices are allowed;
- vendor KYC eligibility for purchasing;
- whether a purchase order is required before receiving goods;
- default requisition lead time;
- default RFQ response time;
- the RFQ closing-soon reporting horizon;
- competitive invitation and submitted-bid minimums;
- permission-gated, reason-required competitive exceptions;
- default contract renewal notice and contract-specific expiry visibility.

This document explains how these settings are stored, validated, audited, and enforced in Procurement workflows. It also calls out policies that intentionally remain fixed and Finance-owned configuration that Procurement consumes without duplicating.

## Ownership and scope

Procurement settings belong to a finance `LedgerEntity`. Requests also run inside a tenant. The backend resolves both and confirms that the entity belongs to the tenant before returning or updating settings.

`ProcurementSettings` is a one-to-one entity policy model. The resolver in `apps/vs_procurement/settings.py` returns a settings object with typed model defaults when no row exists. Reading settings does not create a row. The row is created only when a user with update permission saves a change.

This default-without-row behavior matters for new entities. Procurement can operate with known defaults immediately, while the database records only deliberate customization.

## Permissions

| Capability | Backend permission | Frontend key |
| --- | --- | --- |
| View Procurement Settings | `procurement.settings.view` | `PROC_VIEW_SETTINGS` |
| Update Procurement Settings | `procurement.settings.update` | `PROC_UPDATE_SETTINGS` |
| Override a competitive minimum | `procurement.competition.override` | `PROC_OVERRIDE_COMPETITION` |

Accounting integration displays Finance-owned mappings. A user also needs the Finance Settings view permission to inspect that section. Procurement Settings update permission does not grant permission to change Finance account mappings.

Read and update permissions are separate. All writes are authorized on the backend before mutation; disabling a button in the frontend is not a security control.

## Settings reference

| Field | Default | Validation | Main consumers |
| --- | --- | --- | --- |
| `default_payment_terms` | `NET_30` | Supported payment-terms value | New vendors and contracts. |
| `default_delivery_address` | Blank | Maximum 2,000 characters | New purchase orders. |
| `quantity_tolerance_bps` | 0 | Whole number from 0 to 10,000 | Vendor invoice quantity matching. |
| `price_tolerance_bps` | 0 | Whole number from 0 to 10,000 | Vendor invoice price matching. |
| `allow_non_po_invoices` | `true` | Boolean | Creation and matching of invoices without a PO. |
| `vendor_purchase_kyc_requirement` | `PENDING_OR_VERIFIED` | Supported KYC requirement | Vendor eligibility across purchasing. |
| `require_purchase_order_for_receipts` | `false` | Boolean | Goods-receipt creation. |
| `default_requisition_lead_days` | 0 | Whole number from 0 to 365 | New requisition required-by date. |
| `default_rfq_response_days` | 14 | Whole number from 0 to 365 | New RFQ response due date when the caller omits it. |
| `rfq_closing_soon_days` | 7 | Whole number from 0 to 365 | RFQ summary closing-soon horizon. |
| `contract_renewal_notice_days` | 30 | Whole number from 0 to 365 | New contract renewal reminder. |
| `minimum_rfq_invited_vendors` | 1 | Whole number from 1 to 50 | RFQ issue gate. |
| `minimum_submitted_quotations_before_award` | 1 | Whole number from 1 to 50 | Quotation award gate. |

All values are backend-owned and typed. The frontend may format them for usability but must send the documented API representation.

## Default terms and document behavior

### Payment terms

`default_payment_terms` supplies payment terms when a new vendor or procurement contract does not explicitly provide them. Explicit input wins over the default.

The value is an enum, not free text. This prevents slight wording differences from producing policies the rest of the system cannot interpret consistently.

Changing the default affects future records. It does not rewrite existing vendor or contract terms.

### Delivery address

`default_delivery_address` supplies the delivery address for a new purchase order when the request does not specify one. An explicit purchase-order address wins.

The value is capped at 2,000 characters. The backend trims and validates the supplied string rather than relying on an HTML input limit.

Changing the default does not alter existing purchase orders.

### Requisition lead time

`default_requisition_lead_days` provides a required-by date for a new requisition when one is not supplied. Zero means there is no additional lead-day offset.

The setting is a creation default, not a rule that continually moves an existing requisition's required date.

### Contract renewal notice

`contract_renewal_notice_days` supplies the renewal-notice period for new contracts when the caller omits it. It does not retroactively alter existing contracts.

Contract expiry views respect each contract's stored `renewal_notice_days`. A contract becomes due for renewal attention when today reaches its own end date minus its notice period. This replaces the previous fixed 30-day list window and keeps list and summary counts consistent with the contract record.

### RFQ response and reporting windows

`default_rfq_response_days` supplies a response due date for a new RFQ only when the request omits the `response_due_date` key. The date is calculated from the RFQ issue date. Explicit input wins, including an explicit null where the API permits no due date.

`rfq_closing_soon_days` controls the RFQ summary horizon. It is deliberately separate from the response default: one determines a new record's due date, while the other determines which existing RFQs the dashboard calls closing soon.

Changing either value does not rewrite existing RFQ due dates. The closing-soon horizon affects later summary reads immediately because it is a reporting policy.

## Competitive bidding governance

The two competitive minimums preserve the previous behavior at their default value of one. An entity can raise them without changing RFQ creation: the checks run at the decision boundaries where sourcing evidence becomes operative.

`minimum_rfq_invited_vendors` is checked under the RFQ row lock when a draft RFQ is issued. The service counts persisted, distinct invitation rows. A frontend count or duplicate vendor input cannot satisfy the rule.

`minimum_submitted_quotations_before_award` is checked under the same RFQ-first lock order used by award, close, cancel, and quotation submission. Only quotations still in `SUBMITTED` status count. Draft, rejected, expired, or already-awarded records do not manufacture competition.

When the actual count is below policy, the operation fails unless all of the following are true:

- the request supplies a nonblank `competition_exception_reason` of at most 1,000 characters;
- the actor holds `procurement.competition.override` in the request's tenant and branch scope, or is the active Vision super admin;
- the ordinary issue or award permission also succeeds.

Settings update permission does not grant exception authority. Issue permission does not grant award permission, and neither grants the exception permission. This separation allows administrators to set policy without giving themselves a way around it.

Successful exceptions use the normal `RFQ_ISSUED` or `QUOTATION_AWARDED` audit action, with structured metadata containing the actual count, required minimum, exception flag, and written reason. This keeps the business event and its exception evidence in one immutable record. A reason sent when the minimum is already met is not recorded as an exception.

## Invoice matching tolerances

Tolerance values are stored and sent through the API in basis points:

- 1 basis point is 0.01 percent;
- 100 basis points is 1 percent;
- 500 basis points is 5 percent;
- 10,000 basis points is 100 percent.

The settings screen displays percentages for readability, but it must convert to and from basis points exactly. The backend never stores a floating-point percentage.

### Quantity tolerance

Matching calculates cumulative billed quantity as the quantity already invoiced for the purchase-order line plus the quantity on the current invoice.

The allowed quantity is:

```text
comparison quantity * (10000 + quantity_tolerance_bps) / 10000
```

The comparison quantity is the relevant ordered or received quantity for the matching stage. Exceeding the limit produces an `OVER_BILLED` result. Where receipt matching is required and the invoice runs ahead of receipt, the result may be `UNDER_RECEIVED`.

The cumulative calculation is important. Checking only the current invoice would allow several individually acceptable invoices to exceed the PO in total.

### Price tolerance

The price check compares the invoiced unit price with the expected purchase-order unit price and applies `price_tolerance_bps` to the expected value.

A zero expected price is handled explicitly. Any non-zero invoiced price is a variance because multiplying zero by a tolerance still produces zero.

A price variance produces `PRICE_VARIANCE`. This state remains visible and may be resolved through the authorized override flow. Quantity overbilling, missing required receipt, and a blocked non-PO invoice are hard blocks in the matching path.

### Match outcomes

The settings-aware matching flow uses outcomes including:

- `AUTO_MATCHED`;
- `PRICE_VARIANCE`;
- `OVER_BILLED`;
- `UNDER_RECEIVED`;
- `NON_PO_BLOCKED`.

Changing tolerances affects later matching attempts. It does not silently rewrite the historical result of an invoice that has already completed its workflow.

## Non-PO invoice policy

`allow_non_po_invoices` controls whether an invoice without a linked purchase order may enter the payables flow.

When disabled, a non-PO invoice is rejected or receives the blocking `NON_PO_BLOCKED` match state at the appropriate entry point. The backend enforces the policy. A hidden frontend action alone would be bypassable.

This setting does not relax the separate payment controls described below. An allowed non-PO invoice must still pass the normal approval and payment rules.

## Vendor purchase KYC policy

`vendor_purchase_kyc_requirement` has two supported policies:

| Value | Eligible purchase KYC states |
| --- | --- |
| `PENDING_OR_VERIFIED` | Pending or verified vendors. |
| `VERIFIED_ONLY` | Verified vendors only. |

Universal vendor blocks still apply under both policies:

- inactive vendors are ineligible;
- vendors on hold are ineligible;
- vendors with rejected KYC are ineligible.

The policy is enforced across the purchasing lifecycle, including:

- sourcing invitation and eligible-vendor lists;
- quotation submission and award;
- preferred-vendor selection;
- purchase-order creation;
- procurement contract creation.

Bulk RFQ operations resolve the entity policy once and reuse it for the vendor set. They must not execute a settings query for every vendor.

Vendor payment remains stricter than purchase eligibility. A vendor must be verified before payment even when Procurement permits a pending vendor to participate in sourcing or receive a purchase order. This distinction is deliberate and must not be removed by broadening the purchase policy.

## Receipt policy

`require_purchase_order_for_receipts` controls whether goods may be received without a purchase order.

When enabled, the backend blocks receipt creation that has no PO relationship. When disabled, the existing non-PO receipt path remains available subject to its normal validation and permissions.

Changing this setting does not delete or invalidate receipts already recorded.

## Fixed controls that are not settings

Some high-risk procurement controls remain invariant and are not exposed as editable settings:

- a purchase order requires an approved requisition where the purchasing flow calls for one;
- vendor payment requires verified vendor KYC;
- payment requires the applicable approval state.

These are safety and financial-control boundaries, not convenience defaults. Adding a toggle for one of them requires an explicit product and control review, plus security-focused tests. It must not be smuggled in as an ordinary settings field.

## Finance-owned accounting integration

Procurement postings consume Finance account mappings for accounts payable, GR/IR clearing, inventory, inventory adjustment, and purchase price variance.

Procurement does not own a second copy of these values. The shared Finance resolver validates that the effective account belongs to the entity, is active, is postable, and has the expected type. A missing or invalid effective mapping stops the posting rather than falling back to an unsafe account.

The Procurement Settings interface may link to or display the Finance mappings for context, but changes must use the Finance Settings endpoint and Finance update permission.

## Related sections and ownership boundaries

Reference data and workflows such as categories, units, warehouses, approval templates, and other procurement master data remain owned by their existing modules. Procurement Settings links to those areas instead of duplicating their database rows.

This keeps one source of truth for each policy and avoids two screens offering conflicting values.

## API contract

`GET /v1/procurement/settings/`

Returns the complete effective settings object for the resolved tenant and entity. When no row exists, the response contains typed defaults without creating a database record.

`PATCH /v1/procurement/settings/`

Accepts a partial object. Omitted fields keep their effective values. Supplied fields are normalized and validated on the backend.

Example:

```json
{
  "default_payment_terms": "NET_60",
  "quantity_tolerance_bps": 200,
  "price_tolerance_bps": 500,
  "allow_non_po_invoices": false,
  "vendor_purchase_kyc_requirement": "VERIFIED_ONLY",
  "require_purchase_order_for_receipts": true,
  "default_requisition_lead_days": 7,
  "default_rfq_response_days": 21,
  "rfq_closing_soon_days": 10,
  "minimum_rfq_invited_vendors": 3,
  "minimum_submitted_quotations_before_award": 2,
  "contract_renewal_notice_days": 45
}
```

Unknown enum values, booleans of the wrong type, and numeric values outside their documented ranges return validation errors.

## Save, concurrency, and audit behavior

Updates run in a database transaction. An existing policy row is selected with a row lock before changes are applied. When an entity has no row yet, the one-to-one entity constraint prevents two durable policy rows, although simultaneous first saves can still require one request to retry after a uniqueness conflict.

The update helper:

1. resolves the current effective settings;
2. captures a normalized before snapshot;
3. validates supplied fields;
4. creates or updates the entity's single settings row;
5. captures the normalized after snapshot;
6. writes the audit event in the same transaction.

All Procurement settings sections use the audit action `PROCUREMENT_SETTINGS_UPDATED`. The before and after payloads identify the fields that changed, while the audit row also records the actor, tenant, entity, and request context available to the audit service.

Opening the page, issuing a GET, or saving values that do not change the effective state does not write an audit row. Each settings response embeds the ten most recent Procurement settings events for the entity, ordered newest first. The events are stored in `FinanceAuditLog` for a unified financial control trail.

## Security rules

- Enforce view and update permissions on every settings endpoint.
- Resolve the entity inside the authenticated tenant.
- Do not trust an entity id, vendor id, PO id, or related id from the client without checking scope.
- Enforce every policy in the transaction service, not only in the settings screen.
- Keep Finance mappings behind Finance permissions and the shared resolver.
- Preserve the stricter verified-vendor requirement for payment.
- Keep audit rows append-only through the audit service.
- Return only fields required by the settings client.

## Migrations and main implementation files

Database support was introduced and extended by:

- `apps/vs_procurement/migrations/0016_alter_vendorinvoice_match_status_procurementsettings.py`;
- `apps/vs_procurement/migrations/0017_procurementsettings_contract_renewal_notice_days_and_more.py`;
- `apps/vs_procurement/migrations/0018_procurementsettings_default_rfq_response_days_and_more.py`.
- `apps/vs_procurement/migrations/0019_procurementsettings_competitive_bidding.py`.

After deploying the competitive-governance increment, run `seed_actions` followed by `seed_procurement_permissions`. The first registers the canonical `override` action description. The second registers `procurement.competition.override` as a critical permission and additively grants it to the platform administrator roles without reversing any existing denied role link.

The main implementation files are:

- `apps/vs_procurement/models.py`;
- `apps/vs_procurement/settings.py`;
- `apps/vs_procurement/views/settings.py`;
- `apps/vs_procurement/payables.py`;
- `apps/vs_procurement/purchasing.py`;
- `apps/vs_procurement/contracts.py`;
- `apps/vs_procurement/views/contracts.py`;
- `apps/vs_procurement/views/orders.py`;
- `apps/vs_procurement/views/vendors.py`;
- `apps/vs_procurement/tests_settings.py`.

Other procurement services consume the shared policy resolver rather than implementing their own defaults.

## Test coverage

The focused settings tests cover:

- default response without a persisted row;
- view and update permission enforcement;
- partial updates;
- invalid basis-point ranges and enum values;
- audit before and after snapshots;
- quantity and price matching behavior;
- non-PO invoice blocking;
- KYC eligibility policy;
- receipt, requisition, and contract default enforcement;
- RFQ response-date defaults and explicit overrides;
- configurable RFQ closing-soon summaries;
- contract-specific renewal windows in list and summary views.
- invitation and submitted-bid minimum enforcement;
- denied and authorized competitive exception paths;
- structured exception evidence on issue and award audit events.

When extending Procurement Settings, test the endpoint and the real workflow consumer. A green settings endpoint test does not prove that purchase orders, receipts, invoices, vendors, or contracts obey the policy.

## Extension checklist

Before adding another Procurement setting:

1. Confirm Procurement owns the policy and that it is not Finance or master-data configuration.
2. Decide whether it is a default for new records, an enforcement policy, or both.
3. Use a typed model field with an explicit default and backend validation.
4. Return defaults without creating a row during GET.
5. Enforce tenant and entity scope plus separate view and update permissions.
6. Use partial transactional updates and record before and after audit snapshots.
7. Apply the policy in every relevant workflow service.
8. Preserve stricter invariant controls unless a dedicated product and control decision changes them.
9. Add permission-denied, cross-tenant or cross-entity, default, validation, update, audit, and consumer tests.
10. Document whether a changed value affects only future records or also future processing of existing records.
