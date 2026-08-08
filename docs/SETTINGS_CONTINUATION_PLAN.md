# Settings Work Continuation Plan

## Purpose

This file is the handoff for continuing the settings work when the current Codex task is no longer available. It records what has already been built, what should happen next, what was discovered about the existing platform settings, and the order in which the work should continue.

The future platform-settings request described later in this document has been researched only. It has not been implemented. Its visual direction is already decided: it must reuse the exact sectioned settings design established by Finance and Procurement. Do not source another design, redesign the visual language, or start implementation until the owner has reviewed the proposed settings scope and approved a plan.

## Repositories and baseline

The work spans two repositories:

- Backend: `/Users/mac/Documents/Dev-Projects/GitHub/backend`
- Frontend: `/Users/mac/Documents/Dev-Projects/GitHub/console-fe`

Relevant committed baselines:

- Backend implementation: `f50a1fa feat: enforce finance and procurement settings`
- Frontend implementation: `79a597a feat: add finance and procurement settings consoles`
- Backend documentation: `12086ad docs: document finance and procurement settings`
- Frontend documentation correction: `15915b9 docs: move settings guide to backend`

Read these module guides before changing the settings behavior:

- `docs/finance/finance_settings.md`
- `docs/procurement/procurement_settings.md`

## Fast resume checklist

The next agent should begin with these steps:

1. Read this file in full.
2. Read the two module settings guides above.
3. Check `git status --short` in both repositories and preserve unrelated user changes.
4. Confirm the four baseline commits are present in local history.
5. Inspect current code before relying on this handoff because later commits may have changed it.
6. Continue Phase 1 only after the owner confirms which candidate policies should be editable.
7. Treat Phase 2 as discussion-first work. Present the platform research in the same finding, proposal, readiness, locked-control, and exclusion format used for the original Finance and Procurement research.
8. Do not perform new visual research for Platform Settings. Reuse the Finance and Procurement settings design exactly, with only module-appropriate content and shared-component refactoring.

## What is already complete

### Finance

The Finance settings console uses a sectioned settings layout with an overview and nested routes. The implemented editable areas are:

- finance ledger entity creation;
- entity-scoped automated posting account mappings;
- invoice due-day and narration defaults;
- automatic posting behavior for manual invoices;
- customer opening-balance policy;
- primary collection bank-account selection;
- permission-controlled saves;
- recent immutable audit history.

Account mappings are enforced by real posting consumers. They are not display-only preferences. Overrides must use active, postable accounts in the same entity and of the required account type. Removing an override returns the role to its starter-chart default.

The main files are:

- `apps/vs_finance/account_mappings.py`
- `apps/vs_finance/document_settings.py`
- `apps/vs_finance/views_settings.py`
- `apps/vs_finance/models/core.py`
- `apps/vs_finance/models/ops.py`
- `apps/vs_finance/tests_settings.py`
- `console-fe/src/pages/protected/finance/settings.tsx`
- `console-fe/src/redux/services/finance/setup-api.ts`

### Procurement

The Procurement settings console uses the same sectioned visual language. The implemented editable areas are:

- default payment terms;
- default delivery address;
- quantity matching tolerance;
- unit-price matching tolerance;
- non-PO invoice policy;
- vendor purchase KYC requirement;
- purchase-order requirement for goods receipts;
- default requisition lead days;
- contract renewal notice days;
- permission-controlled saves;
- recent immutable audit history.

These values are enforced in vendor eligibility, requisitions, receipts, contracts, and invoice matching. Finance-owned posting accounts remain owned by Finance Settings and are only displayed or linked from Procurement.

The main files are:

- `apps/vs_procurement/models.py`
- `apps/vs_procurement/settings.py`
- `apps/vs_procurement/views/settings.py`
- `apps/vs_procurement/payables.py`
- `apps/vs_procurement/purchasing.py`
- `apps/vs_procurement/tests_settings.py`
- `console-fe/src/pages/protected/procurement/settings.tsx`
- `console-fe/src/redux/services/procurement/procurement-api.ts`

### Shared settings design

The shared frontend building blocks currently live in:

- `console-fe/src/components/finance-ui/settings-layout.tsx`

They provide:

- the desktop settings sidebar and responsive section navigation;
- section headers;
- settings panels and rows;
- overview cards;
- policy-state badges;
- recent audit history.

The component is already used by both Finance and Procurement. Its `finance-ui` location is now misleading. A future platform-settings revamp should first move it to a neutral shared location, such as `src/components/settings/`, while preserving its public API and existing screens.

# Phase 1: Expand Finance and Procurement policies

## Goal

Continue turning meaningful entity-level defaults and policies into editable settings. Do not turn every existing management screen into a settings form. A setting belongs here only when it supplies a reusable default, changes a cross-workflow policy, or controls consistent backend enforcement.

## Ownership rule

Use these ownership tests before adding any field:

- If the value configures one transaction, keep it on that transaction.
- If the value manages reusable master data, keep it in the existing master-data module.
- If the value defines an approval ladder, keep it in Workflow.
- If the value determines a Finance posting account, keep it in Finance account mappings.
- If the value applies by default across future records or consistently governs many workflows, it may belong in a typed settings model.
- If a rule protects accounting integrity or payment authorization, it should normally remain invariant rather than become a toggle.

This avoids duplicate sources of truth.

## Current non-editable sections that should remain links

The following sections already have an owner and should not be copied into a second settings model:

### Finance-owned modules

- Fiscal years, periods, posting windows, and year-end close
- Chart of accounts
- Tax codes and obligations
- Currencies
- Cost centres and dimensions
- Fee structures
- Dunning policies and reminder stages
- Workflow templates for journals, refunds, and write-offs

### Procurement-owned modules

- Vendors and vendor categories
- Catalog items
- Warehouses and stock items
- Contracts and milestones
- Requisitions, purchase orders, RFQs, quotations, receipts, and invoices
- Workflow templates for requisitions, orders, invoices, and payments

Settings may show their status and deep-link to them. It should not maintain parallel copies.

## Candidate Finance settings discovered in the code

These candidates require product approval before implementation.

| Candidate | Current behavior | Recommendation | Risk |
| --- | --- | --- | --- |
| Default bank reconciliation date tolerance | Auto-reconcile defaults to 4 days in `views_ops/banking.py` when a request does not supply `tolerance_days`. | Good next setting. Store an entity default, while explicit workbench input continues to win. | Medium because looser matching can create false positives. |
| Group reconciliation matching default | Auto-reconcile defaults `group` to true. | Good next setting if Finance teams repeatedly choose the same behavior. | Medium because grouped matches are more complex to review. |
| Default receipt allocation strategy | AR supports `oldest` and `largest`; requests currently default to `oldest`. | Good candidate. Keep an explicit request override. | Medium because it changes which invoices are settled first. |
| Automatic receipt allocation default | Several receipt paths default to automatic allocation. | Discuss separately from strategy. Turning it off creates unapplied customer credit and changes operating procedures. | High. |
| Petty-cash low-balance threshold | Status reporting currently defaults to 2,500 basis points, or 25 percent, but accepts a query override. | Make entity-wide only if alerts and dashboards need one common threshold. Otherwise leave it as a report filter. | Low to medium. |
| Fiscal close relaxations | Close and posting services contain force or restricted-access paths. | Do not expose these as ordinary toggles. Keep permission-controlled operational actions. | Critical. |

### Recommended first Finance increment

The safest coherent increment is a new Banking and Cash Policy section containing:

1. `default_bank_reconciliation_tolerance_days`, with a conservative bounded range.
2. `default_group_reconciliation_matches`, a boolean.
3. `default_receipt_allocation_strategy`, limited to the supported values.

Do not include `auto_allocate_receipts` in that first increment unless the owner explicitly approves the operational change.

Implementation should extend a typed Finance policy model or introduce a clearly named one-to-one entity policy model. Do not place these entity settings in the generic platform `vs_config` catalogue.

## Candidate Procurement settings discovered in the code

| Candidate | Current behavior | Recommendation | Risk |
| --- | --- | --- | --- |
| Default RFQ response window | RFQ creation requires or accepts a response date, while the summary uses a fixed 7-day closing-soon window. | Add a creation default only after deciding the intended number of days. Explicit RFQ dates must win. | Low. |
| RFQ closing-soon horizon | The summary currently hard-codes 7 days. | Either make it a separate reporting preference or derive it from the RFQ response policy. Do not silently conflate the two. | Low. |
| Minimum invited vendors | An RFQ must have invitees, but there is no configurable competitive minimum. | Valuable governance policy, but it needs an exception path and audit reason for sole-source purchasing. | High. |
| Minimum submitted quotations before award | A submitted quotation can currently be awarded without a configurable competition threshold. | Discuss with the owner and procurement stakeholders. If added, enforce it in the award service with a permission-controlled exception. | High. |
| Receipt over-delivery tolerance | Invoice matching has quantity tolerance, but receipt creation policy is a separate concern. | Consider a dedicated tolerance rather than reusing invoice tolerance. | Medium. |
| Contract expiry list horizon | Contract list filters currently use a fixed 30-day window even though new contracts have `contract_renewal_notice_days`. | First fix the inconsistency: use each contract's own reminder date or a clearly documented list parameter. This may be a defect rather than a new setting. | Low. |
| Senior approval threshold | A seeded fallback workflow uses a fixed threshold. | Keep this in Workflow templates. Do not duplicate it in Procurement Settings. | High if duplicated. |

### Recommended first Procurement increment

The safest coherent increment is a Sourcing and Lifecycle section containing:

1. a default RFQ response period for new RFQs;
2. a clearly named closing-soon reporting horizon, if the owner wants it configurable;
3. correction of the contract-expiry list so it respects contract-level renewal notice behavior.

The competitive-bidding minimums should be a separate governance increment because they require exception permissions, audit reasons, and careful treatment of existing RFQs.

## Backend implementation pattern for Phase 1

For each approved field:

1. Put it in the module's typed, one-to-one entity settings model.
2. Give the model field an explicit safe default.
3. Validate ranges and enum values in the backend.
4. Return effective defaults without creating a row on GET.
5. Use a partial PATCH contract. Omitted fields remain unchanged.
6. Resolve the entity through the existing tenant-aware helper.
7. Enforce the module settings view or update permission at the endpoint.
8. Apply the setting inside every relevant service or transaction view.
9. Let explicit transaction input override a default when that is the documented rule.
10. Run the update and audit write in one database transaction.
11. Lock the existing policy row for concurrent edits and address simultaneous first-save behavior.
12. Record only effective changes with before and after snapshots.
13. Return the updated complete effective state and recent audit history.

Do not stop after adding a field and rendering it in the UI. A setting is incomplete until a real consumer obeys it.

## Frontend implementation pattern for Phase 1

For each approved section:

1. Add a stable nested route and overview card.
2. Use the shared settings layout and established panel components.
3. Keep view and update permission behavior separate.
4. Render read-only values when the user can view but cannot update.
5. Use section-level drafts and an explicit Save action for related fields.
6. Show loading, empty, error, and forbidden states.
7. Explain whether a value is a default, an enforced policy, inherited, or owned by another module.
8. Show recent audit history next to the settings it describes.
9. Keep forms responsive and prevent horizontal overflow.
10. Do not hide a backend invariant merely because it is not editable. Show it as enforced when useful.

## Required Phase 1 tests

Backend tests should cover:

- view permission denied;
- update permission denied;
- another tenant or entity rejected;
- default response without a stored row;
- range and enum validation;
- partial updates;
- no-change save behavior;
- audit before and after snapshots;
- concurrent-edit behavior where relevant;
- explicit transaction input overriding a default;
- at least one real consumer for every new field.

Frontend tests should cover:

- default settings response;
- populated custom settings;
- forbidden view;
- read-only view without update permission;
- successful save;
- failed save preserving the draft;
- reset or default behavior where supported;
- audit rendering.

Because Phase 1 changes screens, run the repository's `verify-design` skill against every changed route and inspect the screenshots. Also run the responsive overflow audit at 390px and 820px and inspect the phone captures. Type-checking alone is not sufficient.

## Phase 1 completion gate

Phase 1 is complete when:

- the owner has approved the exact fields;
- the backend stores and validates them;
- real transaction consumers enforce them;
- permissions and audit behavior are covered;
- desktop and phone views have been driven against the real backend;
- the Finance and Procurement settings guides are updated;
- commits are created separately in the affected repositories.

# Phase 2: Future platform-settings discovery and revamp

## User intent to preserve

After the Finance and Procurement expansion is complete, the owner wants to discuss a complete revamp of the dashboard's Platform Settings. The new experience must use the same sectioned settings design already established by Finance and Procurement. This is a fixed design requirement, not a request for new Dribbble, Figma, or web design research. The work should replace the current platform page's product structure, while preserving useful backend capabilities. It must identify real defaults and policies that platform administrators need to manage, agree on an implementation plan, and only then be built.

This phase is not approved for implementation yet.

## Platform Settings research brief

This section deliberately follows the research rudiment used for the original Finance and Procurement settings work:

1. inspect the real code and identify where configuration is scattered;
2. distinguish working controls from generic or display-only UI;
3. find hard-coded defaults and policies that affect repeated workflows;
4. propose sections with contents and current readiness;
5. identify the highest-value additions;
6. identify safeguards that should be visible but locked;
7. state what must remain outside Settings.

### What I found

Platform administration is currently scattered across several dashboard areas rather than presented as one coherent settings product. The sidebar separates School Management, Users and Team, Workflow, Audit and Security, Health, Notifications Administration, and Settings. Those specialist consoles are valid owners of records and operations, but the existing Platform Settings page does not act as a useful overview of them or explain their boundaries.

The existing Platform Settings page has three broad tabs: System Settings, Features, and Audit Trail. Its System Settings tab is mainly a generic configuration-catalogue editor. The backend catalogue is technically strong, but only two seeded definitions currently drive real product behavior: the email retry count and email retry backoff period. Creating an arbitrary definition in the UI does not make the product obey it unless application code explicitly consumes that key.

Several real platform defaults remain outside this settings engine:

- Finance documents use deployment-provided platform issuer details, including name, tagline, address, email, phone, website, and logo.
- New-school behavior relies on model or frontend defaults for ownership type, academic term structure, currency, and branch country.
- Login lock thresholds, lock duration, password-reset lifetimes, invitation lifetime, and proxy-session timeout are hard-coded or deployment-configured.
- JWT lifetimes, API throttles, payment-provider selection, upload limits, secrets, and infrastructure connections also live in deployment configuration, but most of those should remain deployment-owned rather than become ordinary dashboard settings.

The backend already provides typed definitions, platform-to-school-to-branch inheritance, capability entitlements and overrides, RBAC, tenant checks, transactional audit writes, secret redaction, and immutable audit guards. The revamp should preserve those foundations. Its main job is to turn a generic engineering console into curated administrative settings, close the inheritance and permission gaps, and link cleanly to specialist consoles.

### Proposed Platform Settings

| Section | Contents | Current readiness |
| --- | --- | --- |
| Overview | Configuration health, current scope, customized-value count, active feature summary, recent sensitive changes, export, and links to specialist admin consoles. | New UI required. Most source data exists, but a bounded summary endpoint may be preferable to many list requests. |
| Platform profile | Platform and Finance-document issuer name, tagline, address, contact email, phone, website, logo, and public support identity. | Values exist in deployment settings and are already consumed by document rendering. Database-backed audited settings and fallback rules are new work. |
| School onboarding defaults | Default ownership type, academic term structure, currency, branch country, starter package or onboarding checklist, and administrator invitation lifetime where approved. | Defaults exist across models, forms, and invitation services. There is no central policy or clear new-record-only contract. |
| Access and security | Failed-login threshold, lock duration, self-service and administrator reset lifetimes, invitation lifetime, and proxy-session timeout. | Real consumers exist, but values are hard-coded or deployment-backed. This requires guarded settings, strict validation, and security-flow tests. |
| Communications | Existing email retry and backoff settings, safe sender identity, and a link to Notifications Administration for templates, event routing, and delivery history. | Retry settings work today. Sender identity needs a safe contract. Specialist notification tools already exist and should not be duplicated. |
| Features and access | Effective feature state, entitlements, runtime overrides, dependencies, source, inheritance reset, and optional scheduling if approved. | Backend foundations are strong. The frontend lacks complete permissions, provenance, reset behavior, scheduling, and scalable catalogue loading. |
| Integrations | Safe provider choice where multiple providers are genuinely supported, configured or connected status, callback-domain status, sender identity, health link, and connection test where safe. | Mostly deployment-owned today. Product boundaries and secret-safe APIs must be defined before exposing edits. |
| Users and permissions | Summary of administrator access posture and links to Team, Roles, and Permissions. | Existing specialist screens should remain the source of truth. Platform Settings needs link and status treatment only. |
| Workflow | Summary of workflow configuration health and links to template and instance administration. | Existing specialist screens should remain the source of truth. Do not copy approval ladders into Platform Settings. |
| Audit and compliance | Settings-specific filters, before-and-after detail, redacted export, and links to the wider audit and compliance consoles. | Backend events contain richer data than the current page exposes. Filtering, detail, and scoped export need frontend and possibly API work. |
| Advanced configuration | Definition types, validation rules, allowed scopes, sensitivity, capabilities, dependencies, archives, and consumer ownership notes. | Backend is mostly available. The current UI cannot fully edit validation, choice, dependency, or lifecycle metadata. This should be restricted to platform engineers. |

### Highest-value Platform additions

The best first release is Platform profile plus School onboarding defaults. These are real, repeated administrative defaults, they are currently scattered or deployment-bound, and they carry less operational risk than runtime security policy. Their behavior can be stated clearly: they affect platform identity or seed newly created records, and onboarding-default changes do not rewrite existing schools.

Access and security should be the next, separate increment. Those values already govern real authentication and delegation flows, but a weak range or permission mistake could reduce platform protection. Each approved field needs strict backend bounds, a dedicated update permission, an explicit reason, audit history, environment fallback where appropriate, and consumer tests.

The Features and access redesign can reuse the existing engine, but it should not be presented as complete until permission composition, inheritance reset, source labels, optional scheduling, pagination, and dependency performance are resolved.

### Controls that should be visible but locked

Some controls should be shown as enforced platform safeguards so administrators understand why they cannot edit them in the dashboard:

- tenant and branch isolation;
- backend RBAC enforcement;
- secret redaction in values, audit snapshots, and exports;
- immutable audit history;
- capability entitlement and dependency gates;
- transactional notification delivery guarantees and mandatory in-app behavior where the product requires them;
- password-complexity baseline, token signing, and rate-limit protection unless a separate security-governance design explicitly approves safe configurability;
- deployment security and infrastructure boundaries.

Locked safeguards should use the same `Enforced` or equivalent policy badge style already used in Finance and Procurement. They must not be rendered as disabled controls that imply an unavailable save action.

### What should not move into Platform Settings

Keep records, operational queues, and specialist policy designers in their existing owners:

- school and branch records, packages, and day-to-day school management;
- user records, invitations, teams, roles, and detailed permission assignment;
- workflow templates, approval ladders, workflow instances, and inbox work;
- notification templates, event-channel matrices, delivery history, and per-school notification administration;
- platform-wide audit event exploration and compliance operations beyond settings-specific history;
- health checks, jobs, incidents, and operational monitoring;
- Finance and Procurement module settings;
- transactional work such as creating schools, inviting users, resolving incidents, or editing business documents;
- secrets and infrastructure values such as database or Redis URLs, secret keys, SMTP passwords, provider credentials, CORS and host policy, TLS, signing keys, broker configuration, and storage credentials.

Platform Settings may summarize these areas and link to them. It must not create parallel sources of truth.

## Current platform-settings implementation

The frontend page is:

- `console-fe/src/pages/protected/settings/index.tsx`

Supporting frontend files include:

- `console-fe/src/pages/protected/settings/config-dialog.tsx`
- `console-fe/src/redux/services/config-api.ts`
- `console-fe/src/permissions/index.ts`

The backend is the `vs_config` application:

- `apps/vs_config/models.py`
- `apps/vs_config/views.py`
- `apps/vs_config/serializers.py`
- `apps/vs_config/services/resolution.py`
- `apps/vs_config/services/capabilities.py`
- `apps/vs_config/services/scopes.py`
- `apps/vs_config/management/commands/seed_config_catalogue.py`
- `apps/vs_config/tests.py`

The current frontend has three tabs:

1. System Settings
2. Features
3. Audit Trail

The backend already has strong foundations:

- typed configuration definitions;
- platform, school, and branch scope inheritance;
- platform-only catalogue mutation guards;
- separate capability, entitlement, dependency, and override models;
- backend RBAC on every endpoint;
- cross-tenant scope authorization;
- transactional writes;
- secret-reference redaction;
- soft archives;
- immutable local audit events, including database immutability guards;
- redacted configuration export.

These foundations should be preserved. The revamp should improve the product model and user experience rather than replace the configuration engine without cause.

## Important findings and gaps

### 1. The settings catalogue is almost empty

Only two configuration definitions are currently seeded:

- `notifications.email_max_retries`
- `notifications.email_retry_backoff_seconds`

Both are consumed by notification delivery tasks. Most of the current System Settings page is therefore a generic framework rather than a mature set of real administrative controls.

### 2. Creating a definition does not create behavior

The UI lets a platform administrator create a new configuration definition. A new definition has no product effect until application code calls `get_config()` with that key. The future UI must not imply that adding an arbitrary key automatically configures a module.

Recommended change: separate curated operational settings from an Advanced Configuration Catalogue intended for platform engineers.

### 3. Operational settings and schema authoring are mixed

The same System Settings area is used to change live values, create schema definitions, and archive definitions. Those are different jobs with different risk levels and audiences.

Recommended separation:

- ordinary platform administrators edit curated business settings;
- advanced platform engineers manage definitions, validation rules, and capability metadata;
- each area has separate permissions and explanations.

### 4. Scoped values cannot be reset cleanly

Configuration values can be upserted but there is no reset endpoint that removes the current scope's physical row and returns to the inherited or definition default value. This becomes essential once the UI exposes school or branch inheritance.

Recommended backend addition: an audited reset operation that deletes only the authorized scope row and returns the newly effective value plus its source.

### 5. Entitlements cannot return to inheritance

Capability overrides support `INHERIT`, but tenant entitlements only support `GRANTED` or `DENIED`. Once a tenant-specific entitlement exists, there is no API operation to remove it and fall back to the platform entitlement. Decide whether that is intentional. If not, add an audited reset operation.

### 6. Scheduled entitlements are not writable through the API

The entitlement model has `starts_at` and `ends_at`, and the resolver respects them. The set-entitlement serializer and current frontend do not accept those fields. The future plan must either expose scheduling safely or explicitly remove it from the product promise.

### 7. The generic form does not fully support the backend types

Current frontend behavior includes these gaps:

- CHOICE settings render as free-text inputs instead of selects based on `validation_rules.choices`;
- definition creation always sends an empty validation-rules object;
- the UI cannot define numeric bounds or choice lists;
- malformed JSON is sent as a string and rejected only by the backend;
- definition and capability update endpoints exist but have no full editing experience;
- capability dependencies cannot be managed through the current creation dialog.

### 8. High-impact changes save immediately

Feature entitlements, feature overrides, and boolean settings save on toggle or selection with generic hard-coded reasons. High-impact platform changes should use an explicit review or confirmation step and capture a meaningful reason.

### 9. Permission composition needs correction

The Features tab is visible with capability-view permission, but it also loads entitlement and override lists, which have their own backend view permissions. A user with only capability visibility can trigger forbidden subrequests and see incomplete state.

The revamped page should load each data source only when its permission is present and clearly label protected information.

### 10. Scope support is incomplete in the UI

The backend supports platform, school, and branch configuration inheritance. The current System Settings tab edits the platform layer only. The Features tab supports platform or school selection but no branch selection.

The future UI should make the selected scope obvious and show each effective value's source as Default, Platform, School, or Branch.

### 11. Large catalogues will be truncated

The frontend requests page sizes of 100 for definitions, capabilities, entitlements, overrides, and schools. As these collections grow, rows beyond that page can disappear from the experience.

The revamp should use server search and pagination, or a dedicated bounded settings-summary endpoint.

### 12. Audit review is too shallow

The current audit UI shows the last 30 days, actor, reason, and date, but it lacks:

- action filters;
- target filters;
- before and after detail;
- a change-detail drawer;
- a user-selected date range;
- export of the filtered audit result.

The backend contains richer snapshots than the current screen exposes.

### 13. Capability evaluation may become expensive

The effective-capabilities endpoint evaluates each active capability and recursively resolves dependencies. It is acceptable for the small current catalogue, but it should be profiled before substantially expanding capabilities. Preserve dependency correctness while avoiding query growth per capability.

## Proposed information architecture for discussion

The future Platform Settings should use nested sections, not the current three broad tabs.

### 1. Overview

Show configuration health rather than a list of every row:

- current scope;
- number of customized settings;
- unresolved or invalid configuration warnings;
- active modules and features;
- recent sensitive changes;
- links to specialized admin consoles;
- export snapshot action.

### 2. Platform profile and defaults

Potential real admin controls:

- platform display name;
- invoice and receipt issuer tagline, address, contact email, phone, website, and logo;
- default display timezone;
- default language or locale if the product supports more than one;
- public support contact details.

Current platform issuer identity lives in deployment environment variables under `PLATFORM_ISSUER` and is consumed by Finance document rendering. Moving it to database-backed settings requires an explicit migration and fallback design. Environment values should remain the emergency fallback.

### 3. School onboarding defaults

Potential defaults for future school creation:

- default school currency;
- default academic term structure;
- default branch country;
- default enabled onboarding checklist or starter package;
- invitation lifetime for new administrators.

These values should seed new schools or invitations. They must not rewrite existing schools.

### 4. Access and security

Potential controls currently hard-coded in the backend:

- failed-login lock threshold, currently 5;
- account-lock duration, currently 15 minutes;
- self-service password-reset lifetime, currently 1 hour;
- administrator password-reset lifetime, currently 24 hours;
- invitation lifetime, currently 7 days;
- proxy-session idle timeout, currently 30 minutes.

These are high-risk settings. They need strict ranges, dedicated permissions, mandatory reasons, audit history, and tests proving that active security flows consume them. Password complexity, token signing, and tenant-isolation rules should not become casual toggles.

### 5. Communications

Curated controls can include the two existing notification delivery settings and safe sender identity fields. The page should also link to the specialized Notifications Admin console for:

- event-channel matrices;
- templates;
- delivery history;
- per-school notification overrides.

Do not duplicate that matrix inside Platform Settings.

### 6. Features and access

Retain the current capability engine but present it with clearer scope and provenance:

- module or feature catalogue;
- effective On or Off state;
- entitlement state and source;
- runtime override and inheritance state;
- dependencies and blockers;
- optional start and end dates if scheduling is approved;
- reset-to-platform behavior for school-specific decisions;
- change reason and confirmation.

Separate catalogue authoring from day-to-day feature access.

### 7. Integrations

Show safe operational status and navigation for integrations such as payment and email providers. Only expose values administrators can safely change.

Examples suitable for discussion:

- chosen configured payment provider when more than one provider is actually supported;
- callback-domain status;
- sender identity;
- provider connection status;
- link to health checks or a connection test.

Never return or edit secret keys, SMTP passwords, database URLs, Redis URLs, or secret references as plain text. Secret-reference settings may show only whether a reference is configured.

### 8. Audit and exports

Provide:

- date, action, actor, target, and scope filters;
- a detail drawer with before and after values;
- clear redaction for secrets;
- JSON snapshot export;
- filtered audit export if the backend adds it;
- links to the wider platform audit console when the user needs non-configuration events.

### 9. Advanced configuration catalogue

Reserve this for platform engineers:

- definition creation and editing;
- value type and validation rules;
- allowed scopes;
- sensitivity classification;
- capability metadata and dependencies;
- archived definitions and capabilities;
- consumer or owner documentation.

Every curated definition should identify the backend consumer. A catalogue entry without a consumer should be labeled inactive or development-only rather than presented as a working business control.

## Controls that should remain deployment-owned

Do not move these into an ordinary dashboard settings form:

- Django `SECRET_KEY`;
- password peppers;
- database and Redis connection strings;
- SMTP passwords;
- payment-provider secret keys;
- CORS origins and allowed hosts;
- TLS and HSTS controls;
- debug mode;
- API documentation exposure;
- JWT signing configuration;
- storage backend credentials;
- Celery broker and worker infrastructure;
- health-probe infrastructure targets unless an operations-specific design is approved.

The dashboard may report safe configuration status, but it must not expose secret values or weaken deployment boundaries.

## Required design rudiment

The Platform Settings design is already selected. Reuse the exact design implemented for Finance and Procurement. Do not source new visual references, introduce a competing settings shell, or reinterpret the request as a visual exploration.

The implementation should reuse these existing building blocks from `console-fe/src/components/finance-ui/settings-layout.tsx`:

- `ConsoleSettingsLayout` for the desktop section sidebar and responsive section selector;
- `SettingsSectionHeader` for section titles, descriptions, and actions;
- `SettingsPanel` and `SettingsRow` for grouped settings and their explanatory text;
- `SettingsOverviewCard` for the overview and section entry points;
- `PolicyBadge` for Default, Customized, Inherited, Enforced, and attention states;
- `SettingsAuditHistory` for recent changes.

The resulting Platform Settings should therefore have the same:

- compact section sidebar on desktop;
- horizontally scrollable section selector on small screens;
- overview-card treatment;
- panel spacing, row hierarchy, typography, status badges, and action placement;
- explicit section-level save behavior;
- read-only treatment for viewers without update permission;
- recent audit-history presentation;
- responsive behavior and mobile stacking rules.

Platform-only additions, such as a scope selector, source or inheritance labels, confirmation reason, and configuration-health summaries, must be composed inside that established system. They are content and behavior additions, not a new visual direction.

Before platform adoption, move the shared implementation from `finance-ui` to a neutral location such as `src/components/settings/settings-layout.tsx`, then update Finance and Procurement imports. Preserve the component API and rendered appearance during that move. The refactor is successful only if all three settings consoles share one visual source of truth.

## Decisions to discuss before Phase 2 implementation

The next conversation with the owner should answer these questions:

1. Is Platform Settings only for CodeX platform staff, or should school administrators see a scoped version?
2. Which scope should each proposed setting support: platform only, school override, or branch override?
3. Which five to ten settings are genuinely needed in the first release?
4. Should platform issuer identity move from environment variables to audited database settings?
5. Should school onboarding defaults apply only to newly created schools?
6. Which security controls may be changed at runtime, and which must remain deployment-owned?
7. Should changing a sensitive setting require a typed reason, confirmation, or second approval?
8. Should definitions and capability catalogue editing remain in the main UI or move to Advanced?
9. Should tenant entitlements support reset to platform inheritance and scheduled start or expiry?
10. Does the first revamp include branch-level configuration, or only platform and school?
11. Which specialist consoles should appear as links on the overview?
12. Is JSON export sufficient, or is filtered CSV audit export required?

Do not infer these answers from the existing generic page. The owner asked to discuss and agree on the plan first.

## Proposed Phase 2 implementation sequence after approval

### Step 1: Product and control specification

- Present the research using the same structure as this brief before proposing code.
- Lock the first-release section list.
- Lock the exact setting keys, types, safe defaults, scopes, and consumers.
- Classify each as business default, enforcement policy, advanced metadata, or deployment-only.
- Define view and update permissions per section.
- Define audit and confirmation requirements.
- Treat the Finance and Procurement settings design as fixed. Do not add a visual-research phase.

Deliverable: a reviewed settings contract before code changes.

### Step 2: Shared settings-shell refactor

- Move `settings-layout.tsx` from `finance-ui` to a neutral shared settings folder.
- Update Finance and Procurement imports without changing behavior.
- Add any platform-neutral scope and status components needed by all three consoles.
- Preserve the exact visual language, layout behavior, and responsive rules already in use.
- Verify Finance and Procurement did not regress.

### Step 3: Configuration-engine gaps

- Add audited reset-to-inherited behavior for configuration values.
- Decide and implement entitlement reset behavior.
- Decide whether entitlement scheduling is supported.
- Add effective-value responses suited to a sectioned UI, including source information.
- Add bulk reads that avoid client-side truncation and request storms.
- Profile capability evaluation and remove avoidable query growth.

### Step 4: Curated backend settings

- Seed only approved definitions.
- Add runtime consumers for every new key.
- Preserve environment fallbacks where required.
- Add strict validation and safe ranges.
- Add permission, scope, audit, redaction, reset, and consumer tests.

### Step 5: Platform Settings frontend

- Add nested routes for Overview, Platform profile, Onboarding defaults, Security, Communications, Features, Integrations, Audit, and Advanced as approved.
- Use curated forms for ordinary administrators.
- Keep raw JSON and schema authoring in Advanced.
- Add explicit scope and provenance.
- Add save review and meaningful reasons for sensitive changes.
- Implement all permission combinations and failure states.

### Step 6: Audit and export improvements

- Add action, target, actor, scope, and date filters.
- Add a before-and-after detail drawer.
- Preserve secret redaction.
- Make export scope explicit.
- Add filtered audit export only if approved.

### Step 7: Verification and documentation

- Run backend settings and security-critical tests.
- Run frontend tests and build checks.
- Drive every new route against the real backend using `verify-design`.
- Inspect desktop, 390px phone, and 820px tablet screenshots.
- Run the horizontal overflow probe.
- Update or create a dedicated backend Platform Settings guide.
- Run a `ship-check` if requested.

## Phase 2 security test minimum

Before shipping the platform revamp, prove:

- every read and write has the correct backend permission;
- non-platform users cannot mutate platform-only definitions or capabilities;
- one tenant cannot read or write another tenant's settings by changing scope parameters;
- branch scope cannot escape its tenant;
- reset removes only the authorized physical row;
- inherited resolution follows branch, school, platform, then definition default;
- secrets are redacted in definitions, values, effective reads, audit snapshots, and exports;
- sensitive changes capture the actor and meaningful reason;
- audit rows remain immutable;
- enabling a capability cannot bypass entitlement or dependency checks;
- pagination or bounded summary endpoints cannot silently omit managed items.

## Suggested prompt for the next Codex task

Use this prompt to restore context without authorizing the future platform revamp:

> Read `/Users/mac/Documents/Dev-Projects/GitHub/backend/docs/SETTINGS_CONTINUATION_PLAN.md`, `/Users/mac/Documents/Dev-Projects/GitHub/backend/docs/finance/finance_settings.md`, and `/Users/mac/Documents/Dev-Projects/GitHub/backend/docs/procurement/procurement_settings.md`. Inspect both repositories and confirm the documented baseline still matches the code. Do not implement the Platform Settings revamp yet. For Platform Settings, reuse the exact Finance and Procurement settings design. Do not source a new design. First present the platform research in the same format as the original work: What I found, Proposed Platform Settings with contents and readiness, highest-value additions, visible but locked safeguards, and what must remain outside Settings. Use the Phase 2 findings in the continuation plan as the starting evidence, report any code changes since the handoff, and agree with me on the exact first-release controls before implementation.

## Future request preserved for discussion

The owner's future request, normalized without changing its intent, is:

> After the Finance and Procurement settings expansion is complete, discuss how to transform the existing dashboard Platform Settings. Use the exact same settings design already implemented for Finance and Procurement, with no new design sourcing. Revamp the platform settings product structure rather than copying the current three-tab implementation. Research the major defaults and policies that platform administrators genuinely need to change, using the same research rudiment as the original Finance and Procurement work. Existing platform capabilities may be retained where they still belong, but the result should be a real administrative settings product rather than a generic list. Present the findings and review the plan before implementation.

## Final handoff rule

The next safe action is a decision, not a broad build. Confirm the exact Phase 1 policy bundle first. Complete and verify that work. Then return to the owner for the Phase 2 Platform Settings discussion using the research and questions in this file. When Phase 2 begins, keep the Finance and Procurement design unchanged and discuss only the platform content, behavior, scope, permissions, and rollout plan.
